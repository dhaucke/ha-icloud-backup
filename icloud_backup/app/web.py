"""iCloud Backup: sichert iCloud Drive und iCloud-Fotos per rclone auf eine Netzwerkfreigabe."""
from __future__ import annotations

import html
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("ICB_DATA_DIR", "/data"))
RCLONE = os.environ.get("ICB_RCLONE", "rclone")
CONF = DATA_DIR / "rclone.conf"
STATE_FILE = DATA_DIR / "state.json"
LOG_DIR = DATA_DIR / "logs"
REMOTE = "icloud"
TARGET_ROOTS = (Path("/share"), Path("/media"))
INGRESS_PROXY = "172.30.32.2"
ALLOW_ANY_CLIENT = os.environ.get("ICB_ALLOW_ANY_CLIENT") == "1"  # nur für lokale Entwicklung
TOKEN_DAYS = 30  # rclone-Doku: „The trust token is valid for 30 days.“
WARN_DAYS = 5
APP_CSS = (APP_ROOT / "app.css").read_text(encoding="utf-8")

JOBS = {
    # iCloud meldet für Paket-Dokumente (Pages, Numbers, Pixelmator …) die entpackte Größe, liefert aber ein
    # ZIP; ohne --ignore-size endet das als „corrupted on transfer: sizes differ“.
    "drive": {"label": "iCloud Drive", "folder": "iCloud Drive", "args": ["--ignore-size"]},
    # Nur „All Photos“ jeder Mediathek, sonst landet jedes Foto zusätzlich in jedem Album-Ordner.
    "photos": {"label": "iCloud-Fotos", "folder": "iCloud Fotos",
               "args": ["--iclouddrive-service", "photos", "--include", "/*/All Photos/**"]},
}
DEFAULT_SETTINGS = {"target": "", "drive": True, "photos": True, "interval_hours": 24}
STATUS_LABELS = {"running": "Läuft", "ok": "Erfolgreich", "error": "Fehler", "cancelled": "Abgebrochen"}

LOCK = threading.RLock()
AUTH_LOCK = threading.Lock()  # eine Anmeldung zur Zeit, rclone.conf und pending gehören zusammen
RUN = {"active": False, "cancel": False, "proc": None}
STATE: dict = {}


# ---------------------------------------------------------------- Zustand

def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        state = {}
    state["settings"] = {**DEFAULT_SETTINGS, **state.get("settings", {})}
    state.setdefault("login", {})
    state.setdefault("jobs", {})
    if not CONF.exists():  # rclone.conf ist vom HA-Backup ausgenommen, nach Wiederherstellung neu anmelden
        state["login"].pop("login_at", None)
        state.pop("pending", None)
    for job in state["jobs"].values():
        if job.get("status") == "running":  # Container wurde während einer Sicherung neu gestartet
            job["status"] = "cancelled"
    return state


def save_state() -> None:
    with LOCK:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(STATE, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- Ziel

def validate_target(raw: str) -> Path:
    path = Path(os.path.realpath(raw.strip() or "/"))
    if not any(path.is_relative_to(root) and path != root for root in TARGET_ROOTS):
        raise ValueError("Das Ziel muss ein Ordner unterhalb von /share oder /media sein.")
    if not path.is_dir():
        raise ValueError(f"Den Ordner {path} gibt es nicht. Ist die Netzwerkfreigabe in Home Assistant eingebunden?")
    return path


def on_mount(path: Path) -> bool:
    """True, wenn das Ziel auf einer eingebundenen Freigabe liegt und nicht auf der HA-Festplatte."""
    while path not in TARGET_ROOTS and path != path.parent:
        if os.path.ismount(path):
            return True
        path = path.parent
    return False


def target_candidates() -> list[tuple[str, bool]]:
    found = []
    for root in TARGET_ROOTS:
        if root.is_dir():
            found += [(str(p), on_mount(p)) for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")]
    return found


# ---------------------------------------------------------------- Anmeldung

def clean_error(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in reversed(lines):
        if "Error:" in line or "ERROR" in line:
            return line.split("Error:", 1)[-1].split("ERROR :", 1)[-1].strip()
    return lines[-1] if lines else "Unbekannter Fehler von rclone"


def rclone_config(*args: str) -> dict:
    cmd = [RCLONE, "--config", str(CONF), "config", *args, "--non-interactive"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        # Nicht str(exc) weitergeben: enthält die Befehlszeile samt Passwort
        raise ValueError("Apple hat nicht rechtzeitig geantwortet. Bitte erneut versuchen.") from None
    if proc.returncode != 0:
        raise ValueError(clean_error(proc.stderr))
    out = proc.stdout.strip()
    return json.loads(out) if out.startswith("{") else {"State": ""}


def login_start(apple_id: str, password: str) -> bool:
    if not apple_id or not password:
        raise ValueError("Apple-ID und Passwort sind Pflicht.")
    with LOCK:  # gleiche Sperre wie die Zulassung in run_backup
        if RUN["active"]:
            raise ValueError("Während einer Sicherung ist keine Neuanmeldung möglich.")
        STATE["login"].pop("login_at", None)  # bis zur neuen 2FA keine Sicherungen mit halber Konfiguration
        save_state()
    CONF.unlink(missing_ok=True)
    out = rclone_config("create", REMOTE, "iclouddrive", f"apple_id={apple_id}", f"password={password}", "--obscure")
    return login_step(out, apple_id)


def login_continue(answer: str) -> bool:
    pending = STATE.get("pending")
    if not pending:
        raise ValueError("Keine Anmeldung offen. Bitte neu beginnen.")
    if not answer.strip():
        raise ValueError("Bitte eine Antwort eingeben.")
    out = rclone_config("update", REMOTE, "--continue", "--state", pending["state"], "--result", answer.strip())
    return login_step(out, pending["apple_id"])


def login_step(out: dict, apple_id: str) -> bool:
    """Wertet eine rclone-Antwort aus. True = fertig angemeldet, False = weitere Frage offen."""
    if out.get("Error"):
        raise ValueError(out["Error"])  # offene Frage bleibt bestehen, Eingabe kann wiederholt werden
    with LOCK:
        if out.get("State"):
            STATE["pending"] = {"state": out["State"], "option": out.get("Option") or {}, "apple_id": apple_id}
            save_state()
            return False
        STATE.pop("pending", None)
        STATE.pop("expiry_notified", None)
        STATE["login"] = {"apple_id": apple_id, "login_at": now_iso()}
        save_state()
    CONF.chmod(0o600)
    ha_notification("dismiss", {"notification_id": "icloud_backup_login"})
    return True


def login_days_left(now: datetime) -> int | None:
    login_at = STATE["login"].get("login_at")
    if not login_at:
        return None
    return TOKEN_DAYS - (now - datetime.fromisoformat(login_at)).days


# ---------------------------------------------------------------- Benachrichtigungen

def ha_notification(action: str, payload: dict) -> None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    request = urllib.request.Request(
        f"http://supervisor/core/api/services/persistent_notification/{action}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except OSError as exc:
        print(f"HA-Benachrichtigung fehlgeschlagen: {exc}", flush=True)


def notify(notification_id: str, title: str, message: str) -> None:
    ha_notification("create", {"notification_id": notification_id, "title": title, "message": message})


def check_expiry(now: datetime) -> None:
    left = login_days_left(now)
    if left is None or left > WARN_DAYS or STATE.get("expiry_notified") == date.today().isoformat():
        return
    with LOCK:
        STATE["expiry_notified"] = date.today().isoformat()
        save_state()
    text = (f"Die iCloud-Anmeldung läuft in {left} Tagen ab." if left > 0
            else "Die iCloud-Anmeldung ist abgelaufen, Sicherungen schlagen fehl.")
    notify("icloud_backup_login", "iCloud Backup: neu anmelden",
           f"{text} Bitte im Add-on unter „Anmeldung“ neu anmelden.")


# ---------------------------------------------------------------- Sicherung

def parse_log_line(line: str) -> tuple[dict | None, str | None]:
    """Liefert (Statistik, Fehlermeldung) aus einer rclone-JSON-Logzeile."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None, None
    if not isinstance(entry, dict):
        return None, None
    error = None
    if entry.get("level") in ("error", "critical"):
        error = entry.get("msg", "").strip()
        if entry.get("object"):
            error = f"{entry['object']}: {error}"
    return entry.get("stats"), error


def set_job(key: str, **values) -> None:
    with LOCK:
        STATE["jobs"].setdefault(key, {}).update(values)
        save_state()


def run_job(key: str, target: Path) -> dict:
    job = JOBS[key]
    dest = target / job["folder"]
    dest.mkdir(exist_ok=True)
    if not on_mount(Path(os.path.realpath(dest))):  # z. B. Symlink auf lokale Platte
        raise ValueError(f"{dest} zeigt nicht auf eine eingebundene Freigabe.")
    cmd = [
        RCLONE, "--config", str(CONF), "copy", f"{REMOTE}:", str(dest), *job["args"],
        "--cache-dir", str(DATA_DIR / "cache"), "--use-json-log", "--log-level", "NOTICE",
        "--stats", "15s", "--stats-log-level", "NOTICE",
    ]
    with LOCK:
        STATE["jobs"][key] = {"status": "running", "started": now_iso(), "finished": "", "stats": {}, "error": ""}
        save_state()
    last_error = ""
    with (LOG_DIR / f"{key}.log").open("w", encoding="utf-8") as log, \
            subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
        with LOCK:
            RUN["proc"] = proc
            if RUN["cancel"]:
                proc.terminate()
        for line in proc.stdout:
            log.write(line)
            log.flush()
            stats, error = parse_log_line(line)
            last_error = error or last_error
            if stats:
                set_job(key, stats=stats)
    RUN["proc"] = None
    if RUN["cancel"]:
        status, error = "cancelled", ""
    elif proc.returncode == 0:
        status, error = "ok", ""
    else:
        status, error = "error", last_error or f"rclone wurde mit Code {proc.returncode} beendet."
    set_job(key, status=status, finished=now_iso(), error=error)
    return STATE["jobs"][key]


def run_backup() -> None:
    with LOCK:
        if RUN["active"] or not is_ready():
            return
        RUN.update(active=True, cancel=False)
        STATE["last_start"] = now_iso()
        STATE["run_error"] = ""
        save_state()
    try:
        settings = STATE["settings"]
        target = validate_target(settings["target"])
        if not on_mount(target):
            raise ValueError(f"{target} liegt nicht auf einer eingebundenen Freigabe. Sicherung übersprungen, "
                             "damit die Home-Assistant-Festplatte nicht vollläuft.")
        failures = []
        for key, job in JOBS.items():
            if settings[key] and not RUN["cancel"]:
                result = run_job(key, target)
                if result["status"] == "error":
                    failures.append(f"{job['label']}: {result['error']}")
        if failures:
            notify("icloud_backup_error", "iCloud Backup fehlgeschlagen", "\n".join(failures))
        elif not RUN["cancel"]:
            ha_notification("dismiss", {"notification_id": "icloud_backup_error"})
    except (ValueError, OSError) as exc:
        with LOCK:
            STATE["run_error"] = str(exc)
            save_state()
        notify("icloud_backup_error", "iCloud Backup fehlgeschlagen", str(exc))
    finally:
        RUN["active"] = False


def is_ready() -> bool:
    settings = STATE["settings"]
    return bool(settings["target"] and (settings["drive"] or settings["photos"]) and STATE["login"].get("login_at"))


def next_run() -> datetime | None:
    if not is_ready():
        return None
    last = STATE.get("last_start")
    if not last:
        return datetime.now()
    return datetime.fromisoformat(last) + timedelta(hours=STATE["settings"]["interval_hours"])


def start_backup_thread() -> None:
    threading.Thread(target=run_backup, daemon=True, name="backup").start()


def scheduler() -> None:
    while True:
        now = datetime.now()
        check_expiry(now)
        due = next_run()
        if due and now >= due and not RUN["active"]:
            start_backup_thread()  # eigener Thread, damit check_expiry auch während langer Sicherungen läuft
        time.sleep(60)


# ---------------------------------------------------------------- Oberfläche

def h(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def fmt_time(value: str | None) -> str:
    return datetime.fromisoformat(value).strftime("%d.%m.%Y, %H:%M") if value else "–"


def fmt_bytes(value) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}".replace(".", ",")
        size /= 1024
    return f"{size:.2f} TB".replace(".", ",")


def layout(base: str, title: str, body: str, active: str, refresh: bool = False) -> str:
    nav = [("/", "dashboard", "Übersicht"), ("/login", "login", "Anmeldung"),
           ("/settings", "settings", "Einstellungen"), ("/log", "log", "Protokoll")]
    links = "".join(
        f'<a class="{"active" if key == active else ""}" href="{base}{url}">{label}</a>' for url, key, label in nav
    )
    refresh_tag = '<meta http-equiv="refresh" content="5">' if refresh else ""
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  {refresh_tag}
  <title>{h(title)} · iCloud Backup</title>
  <style>{APP_CSS}</style>
</head>
<body>
  <aside class="sidebar">
    <div class="brand"><div class="brand-placeholder">☁</div><strong>iCloud Backup</strong></div>
    <nav>{links}</nav>
    <div class="sidebar-foot">iCloud Drive und Fotos · rclone</div>
  </aside>
  <main>
    <header class="topbar"><div><span>HOME ASSISTANT</span><h1>{h(title)}</h1></div></header>
    <section class="content">{body}</section>
  </main>
</body>
</html>"""


def alert(message: str, level: str = "") -> str:
    return f'<div class="alert {h(level)}">{message}</div>' if message else ""


def login_status_text(now: datetime) -> tuple[str, str]:
    left = login_days_left(now)
    if left is None:
        return "Nicht angemeldet", "error"
    if left <= 0:
        return "Abgelaufen", "error"
    return f"Noch {left} Tage gültig", "warn" if left <= WARN_DAYS else "ok"


def job_card(key: str) -> str:
    job = STATE["jobs"].get(key)
    label = JOBS[key]["label"]
    if not STATE["settings"][key]:
        return f'<article class="card job"><div class="job-head"><h2>{label}</h2><span class="status">Deaktiviert</span></div></article>'
    if not job:
        return (f'<article class="card job"><div class="job-head"><h2>{label}</h2><span class="status">Noch nie gesichert</span></div>'
                f'<p class="muted">Ziel: {h(STATE["settings"]["target"] or "–")}/{h(JOBS[key]["folder"])}</p></article>')
    stats = job.get("stats") or {}
    progress = ""
    if job["status"] == "running":
        total = stats.get("totalBytes") or 0
        percent = int(100 * (stats.get("bytes") or 0) / total) if total else 0
        eta = stats.get("eta")
        eta_text = f" · noch ca. {timedelta(seconds=int(eta))}" if eta else ""
        progress = (f'<div class="progress"><div style="width:{percent}%"></div></div>'
                    f'<p class="muted">{percent} % · {fmt_bytes(stats.get("speed"))}/s{eta_text}</p>')
    error = alert(h(job.get("error")), "error") if job.get("error") else ""
    return f"""<article class="card job">
  <div class="job-head"><h2>{label}</h2><span class="status {h(job['status'])}">{STATUS_LABELS.get(job['status'], job['status'])}</span></div>
  {progress}
  <dl class="facts">
    <div><dt>Gestartet</dt><dd>{fmt_time(job.get('started'))}</dd></div>
    <div><dt>Beendet</dt><dd>{fmt_time(job.get('finished'))}</dd></div>
    <div><dt>Dateien kopiert</dt><dd>{stats.get('transfers', 0)} / {stats.get('totalTransfers', 0)}</dd></div>
    <div><dt>Datenmenge</dt><dd>{fmt_bytes(stats.get('bytes'))}</dd></div>
    <div><dt>Geprüft (unverändert)</dt><dd>{stats.get('checks', 0)}</dd></div>
    <div><dt>Fehler</dt><dd>{stats.get('errors', 0)}</dd></div>
  </dl>
  {error}
</article>"""


def dashboard(base: str) -> str:
    now = datetime.now()
    settings = STATE["settings"]
    notes = ""
    if not STATE["login"].get("login_at"):
        notes += alert(f'Noch nicht bei iCloud angemeldet. <a href="{base}/login">Jetzt anmelden</a>')
    if not settings["target"]:
        notes += alert(f'Noch kein Sicherungsziel gewählt. <a href="{base}/settings">Ziel festlegen</a>')
    notes += alert(h(STATE.get("run_error")), "error")
    login_text, login_level = login_status_text(now)
    due = next_run()
    next_text = "Läuft gerade" if RUN["active"] else (fmt_time(due.isoformat()) if due else "–")
    if RUN["active"]:
        buttons = f'<form method="post" action="{base}/cancel"><button class="button danger">Sicherung abbrechen</button></form>'
    else:
        disabled = "" if is_ready() else "disabled"
        buttons = f'<form method="post" action="{base}/run"><button class="button primary" {disabled}>Jetzt sichern</button></form>'
    return f"""{notes}
<div class="actions">{buttons}</div>
<div class="stats">
  <article><span>Anmeldung</span><strong class="{login_level}">{h(login_text)}</strong></article>
  <article><span>Letzte Sicherung</span><strong>{fmt_time(STATE.get('last_start'))}</strong></article>
  <article><span>Nächste Sicherung</span><strong>{h(next_text)}</strong></article>
  <article><span>Ziel</span><strong class="path">{h(settings['target'] or '–')}</strong></article>
</div>
<div class="jobs">{job_card('drive')}{job_card('photos')}</div>"""


QUESTION_LABELS = {
    "config_2fa": ("Bestätigungscode",
                   "Apple hat einen sechsstelligen Code an deine Apple-Geräte geschickt. "
                   "Kommt keiner an, gib <b>sms</b> ein, dann kommt der Code per SMS."),
    "config_2fa_sms": ("Code aus der SMS", "Gib den Code aus der SMS ein."),
    "config_2fa_phone": ("Telefonnummer", "An welche Nummer soll die SMS gehen?"),
}


def question_field(option: dict) -> str:
    label, hint = QUESTION_LABELS.get(option.get("Name", ""), ("Antwort", h(option.get("Help", ""))))
    examples = option.get("Examples") or []
    if examples:
        choices = "".join(f'<option value="{h(e.get("Value"))}">{h(e.get("Help") or e.get("Value"))}</option>' for e in examples)
        field = f'<select name="answer">{choices}</select>'
    else:
        kind = "password" if option.get("IsPassword") else "text"
        field = f'<input type="{kind}" name="answer" required autofocus autocomplete="one-time-code">'
    return f'<p class="muted">{hint}</p><label class="wide"><span>{label}</span>{field}</label>'


def login_page(base: str) -> str:
    pending = STATE.get("pending")
    if pending:
        return f"""<article class="card form">
  <div class="setup-step"><span>2</span><div><strong>Anmeldung bestätigen</strong><small>{h(pending['apple_id'])}</small></div></div>
  <form method="post" action="{base}/login/code">
    <div class="form-grid">{question_field(pending['option'])}</div>
    <p class="muted">Ist Erweiterter Datenschutz (ADP) aktiv, musst du den Zugriff danach eventuell noch auf einem Apple-Gerät erlauben.</p>
    <div class="form-actions"><button class="button primary">Bestätigen</button></div>
  </form>
  <form method="post" action="{base}/login/reset" class="form-actions"><button class="button">Abbrechen und neu beginnen</button></form>
</article>"""
    login = STATE["login"]
    text, level = login_status_text(datetime.now())
    status = ""
    if login.get("login_at"):
        valid_until = datetime.fromisoformat(login["login_at"]) + timedelta(days=TOKEN_DAYS)
        status = (f'<article class="card form"><h2>Aktuelle Anmeldung</h2><dl class="facts">'
                  f'<div><dt>Apple-ID</dt><dd>{h(login.get("apple_id"))}</dd></div>'
                  f'<div><dt>Angemeldet am</dt><dd>{fmt_time(login["login_at"])}</dd></div>'
                  f'<div><dt>Gültig bis</dt><dd>{fmt_time(valid_until.isoformat())}</dd></div>'
                  f'<div><dt>Status</dt><dd class="{level}">{h(text)}</dd></div></dl></article>')
    return f"""{status}
<article class="card form">
  <div class="setup-step"><span>1</span><div><strong>{'Neu anmelden' if login.get('login_at') else 'Bei iCloud anmelden'}</strong>
  <small>Apple verlangt die Anmeldung alle {TOKEN_DAYS} Tage neu</small></div></div>
  <ul class="setup-guide">
    <li>Auf dem iPhone muss <b>Einstellungen → Apple-Account → iCloud → „Auf iCloud-Daten im Web zugreifen“</b> eingeschaltet sein.</li>
    <li>Normales Apple-ID-Passwort verwenden, <b>app-spezifische Passwörter funktionieren nicht</b>.</li>
    <li>Das Passwort wird nur an rclone übergeben und verschleiert in <code>/data/rclone.conf</code> abgelegt.
        Diese Datei ist von Home-Assistant-Backups ausgenommen.</li>
  </ul>
  <form method="post" action="{base}/login">
    <div class="form-grid">
      <label><span>Apple-ID</span><input type="email" name="apple_id" required autocomplete="username" value="{h(login.get('apple_id'))}"></label>
      <label><span>Passwort</span><input type="password" name="password" required autocomplete="current-password"></label>
    </div>
    <div class="form-actions"><button class="button primary">Anmelden</button></div>
  </form>
</article>"""


def settings_page(base: str) -> str:
    settings = STATE["settings"]
    candidates = target_candidates()
    datalist = "".join(f'<option value="{h(path)}">' for path, _ in candidates)
    rows = "".join(
        f'<tr><td><code>{h(path)}</code></td><td><span class="status {"ok" if mounted else "error"}">'
        f'{"Freigabe eingebunden" if mounted else "Lokaler Ordner"}</span></td></tr>'
        for path, mounted in candidates
    ) or '<tr><td colspan="2" class="empty">Keine Ordner unter /share oder /media gefunden.</td></tr>'
    checked = {key: "checked" if settings[key] else "" for key in JOBS}
    return f"""<article class="card form">
  <form method="post" action="{base}/settings">
    <div class="form-grid">
      <label class="wide"><span>Sicherungsziel</span>
        <input name="target" list="targets" required placeholder="/share/nas-backup" value="{h(settings['target'])}">
        <datalist id="targets">{datalist}</datalist></label>
      <label class="check"><input type="checkbox" name="drive" {checked['drive']}><span>iCloud Drive sichern</span></label>
      <label class="check"><input type="checkbox" name="photos" {checked['photos']}><span>iCloud-Fotos sichern</span></label>
      <label><span>Intervall in Stunden</span>
        <input type="number" name="interval_hours" min="1" max="168" required value="{h(settings['interval_hours'])}"></label>
    </div>
    <p class="muted">Im Ziel entstehen die Ordner <code>iCloud Drive</code> und <code>iCloud Fotos</code>.
    Es wird nur kopiert: Was in iCloud gelöscht wird, bleibt in der Sicherung erhalten.
    Geänderte Dateien werden in der Sicherung überschrieben.</p>
    <div class="form-actions"><button class="button primary">Speichern</button></div>
  </form>
</article>
<article class="card">
  <div class="card-head"><h2>Gefundene Ordner</h2>
  <p class="muted">Netzwerkfreigaben bindest du in Home Assistant unter <b>Einstellungen → System → Speicher →
  Netzwerkspeicher hinzufügen</b> ein (Verwendung: <b>Freigabe</b>). Danach dieses Add-on neu starten.
  Auf „Lokaler Ordner“ wird nicht gesichert, damit die Home-Assistant-Festplatte nicht vollläuft.</p></div>
  <div class="table-wrap"><table><tr><th>Ordner</th><th>Art</th></tr>{rows}</table></div>
</article>"""


def log_page() -> str:
    parts = []
    for key, job in JOBS.items():
        path = LOG_DIR / f"{key}.log"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:] if path.exists() else []
        out = []
        for line in lines:
            try:
                entry = json.loads(line)
                obj = f"{entry['object']}: " if entry.get("object") else ""
                out.append(f"{entry.get('time', '')[:19].replace('T', ' ')}  {entry.get('level', '').upper():7} "
                           f"{obj}{entry.get('msg', '').strip()}")
            except ValueError:
                out.append(line)
        text = h("\n".join(out)) or "Noch kein Protokoll vorhanden."
        parts.append(f'<article class="card form"><h2>{job["label"]}</h2><pre class="command-block">{text}</pre></article>')
    return "".join(parts)


# ---------------------------------------------------------------- HTTP

def read_form(environ) -> dict:
    size = int(environ.get("CONTENT_LENGTH") or 0)
    body = environ["wsgi.input"].read(size).decode("utf-8")
    return {key: values[-1] for key, values in urllib.parse.parse_qs(body, keep_blank_values=True).items()}


def respond(start_response, body: str, status: int = 200, headers=None):
    raw = body.encode("utf-8")
    start_response(f"{status} {HTTPStatus(status).phrase}",
                   [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(raw))), *(headers or [])])
    return [raw]


def redirect(start_response, base: str, path: str, message: str = "", level: str = "success"):
    query = "?" + urllib.parse.urlencode({"msg": message, "level": level}) if message else ""
    return respond(start_response, "", 303, [("Location", f"{base}{path}{query}")])


def handle_post(path: str, form: dict) -> tuple[str, str, str]:
    """Führt eine Aktion aus und liefert (Ziel, Meldung, Stufe)."""
    if path == "/run":
        if not is_ready():
            raise ValueError("Erst anmelden und ein Ziel festlegen.")
        start_backup_thread()
        return "/", "Sicherung gestartet.", "success"
    if path == "/cancel":
        with LOCK:
            RUN["cancel"] = True
            if RUN["proc"]:
                RUN["proc"].terminate()
        return "/", "Sicherung wird abgebrochen.", "success"
    if path.startswith("/login") and not AUTH_LOCK.acquire(blocking=False):
        raise ValueError("Es läuft bereits eine Anmeldung. Bitte kurz warten.")
    try:
        return handle_login(path, form) if path.startswith("/login") else handle_settings(path, form)
    finally:
        if path.startswith("/login"):
            AUTH_LOCK.release()


def handle_login(path: str, form: dict) -> tuple[str, str, str]:
    if path == "/login":
        done = login_start(form.get("apple_id", "").strip(), form.get("password", ""))
        return "/login", "Angemeldet." if done else "", "success"
    if path == "/login/code":
        done = login_continue(form.get("answer", ""))
        return ("/", "Angemeldet. Die Sicherung startet automatisch.", "success") if done else ("/login", "", "")
    if path == "/login/reset":
        with LOCK:
            STATE.pop("pending", None)
            save_state()
        return "/login", "", ""
    raise LookupError(path)


def handle_settings(path: str, form: dict) -> tuple[str, str, str]:
    if path == "/settings":
        target = validate_target(form.get("target", ""))
        hours = int(form.get("interval_hours") or 24)
        if not 1 <= hours <= 168:
            raise ValueError("Das Intervall muss zwischen 1 und 168 Stunden liegen.")
        with LOCK:
            STATE["settings"] = {"target": str(target), "drive": "drive" in form, "photos": "photos" in form,
                                 "interval_hours": hours}
            save_state()
        warning = "" if on_mount(target) else " Achtung: Das Ziel ist keine eingebundene Freigabe, es wird nicht gesichert."
        return "/settings", "Gespeichert." + warning, "success" if not warning else "error"
    raise LookupError(path)


PAGES = {
    "/": ("Übersicht", "dashboard"),
    "/login": ("Anmeldung", "login"),
    "/settings": ("Einstellungen", "settings"),
    "/log": ("Protokoll", "log"),
}


def application(environ, start_response):
    if environ.get("REMOTE_ADDR") != INGRESS_PROXY and not ALLOW_ANY_CLIENT:
        return respond(start_response, "Nur über Home Assistant erreichbar.", 403)
    base = environ.get("HTTP_X_INGRESS_PATH", "").rstrip("/")
    path = environ.get("PATH_INFO", "/") or "/"
    if environ["REQUEST_METHOD"] == "POST":
        try:
            target, message, level = handle_post(path, read_form(environ))
        except LookupError:
            return respond(start_response, "Nicht gefunden", 404)
        except (ValueError, OSError) as exc:
            back = "/login" if path.startswith("/login") else ("/settings" if path == "/settings" else "/")
            return redirect(start_response, base, back, str(exc), "error")
        return redirect(start_response, base, target, message, level)
    if path not in PAGES:
        return respond(start_response, "Nicht gefunden", 404)
    title, active = PAGES[path]
    query = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    flash = alert(h(query["msg"][-1]), query.get("level", ["success"])[-1]) if "msg" in query else ""
    body = {"dashboard": lambda: dashboard(base), "login": lambda: login_page(base),
            "settings": lambda: settings_page(base), "log": log_page}[active]()
    refresh = active == "dashboard" and RUN["active"]
    return respond(start_response, layout(base, title, flash + body, active, refresh))


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):  # Zugriffslog würde das Add-on-Protokoll zumüllen
        pass


if __name__ == "__main__":
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE.update(load_state())
    save_state()
    threading.Thread(target=scheduler, daemon=True, name="scheduler").start()
    port = int(os.environ.get("ICB_PORT", "8099"))
    print(f"iCloud Backup läuft auf Port {port}", flush=True)
    with make_server("0.0.0.0", port, application, server_class=ThreadingWSGIServer, handler_class=QuietHandler) as server:
        server.serve_forever()

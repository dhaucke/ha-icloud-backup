"""iCloud Backup: sichert iCloud Drive und iCloud-Fotos mehrerer Apple-IDs per rclone auf Netzwerkfreigaben."""
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
LEGACY_REMOTE = "icloud"  # Remote-Name bis 2026.10.3, bleibt die ID des übernommenen Kontos
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
STATUS_LABELS = {"running": "Läuft", "ok": "Erfolgreich", "error": "Fehler", "cancelled": "Abgebrochen"}

LOCK = threading.RLock()
AUTH_LOCK = threading.Lock()  # eine Kontoänderung zur Zeit, rclone.conf und pending gehören zusammen
RUN = {"active": False, "cancel": False, "proc": None}
STATE: dict = {}


# ---------------------------------------------------------------- Zustand

def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        state = {}
    old = state.get("settings", {})
    state["settings"] = {"interval_hours": old.get("interval_hours", 24)}
    accounts = state.setdefault("accounts", {})
    login = state.pop("login", {})
    if not accounts and (login or old.get("target")):  # bis 2026.10.3: genau ein Konto, Ziel bleibt gleich
        accounts[LEGACY_REMOTE] = {
            "name": login.get("apple_id") or "iCloud", "apple_id": login.get("apple_id", ""),
            "target": old.get("target", ""), "drive": old.get("drive", True), "photos": old.get("photos", True),
            "jobs": state.get("jobs", {}),
            **({"login_at": login["login_at"]} if login.get("login_at") else {}),
        }
        for key in JOBS:
            if (LOG_DIR / f"{key}.log").exists():
                (LOG_DIR / f"{key}.log").replace(log_path(LEGACY_REMOTE, key))
    for key in ("jobs", "expiry_notified", "run_error"):
        state.pop(key, None)
    if "account" not in state.get("pending", {"account": ""}):  # offene Anmeldung aus alter Version
        state.pop("pending")
    if not CONF.exists():  # rclone.conf ist vom HA-Backup ausgenommen, nach Wiederherstellung neu anmelden
        state.pop("pending", None)
    for acc in accounts.values():
        acc.setdefault("jobs", {})
        if not CONF.exists():
            acc.pop("login_at", None)
        for job in acc["jobs"].values():
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


def account_items() -> list[tuple[str, dict]]:
    """Schnappschuss der Konten, damit Anlegen/Löschen keine laufende Schleife sprengt."""
    with LOCK:
        return list(STATE["accounts"].items())


def get_account(acc_id: str) -> dict:
    acc = STATE["accounts"].get(acc_id)
    if acc is None:
        raise ValueError("Dieses Konto gibt es nicht mehr.")
    return acc


def new_account_id() -> str:
    n = 1
    while f"icloud{n}" in STATE["accounts"]:
        n += 1
    return f"icloud{n}"  # zugleich Name des rclone-Remotes


# ---------------------------------------------------------------- Ziel

def validate_target(raw: str, create: bool = False) -> Path:
    path = Path(os.path.realpath(raw.strip() or "/"))
    if not any(path.is_relative_to(root) and path != root for root in TARGET_ROOTS):
        raise ValueError("Das Ziel muss ein Ordner unterhalb von /share oder /media sein.")
    if create and not path.exists() and path.parent.is_dir() and on_mount(path.parent):
        path.mkdir()  # z. B. /share/nas-backup/Anna für ein weiteres Konto, nur auf eingebundener Freigabe
    if not path.is_dir():
        raise ValueError(f"Den Ordner {path} gibt es nicht. Ist die Netzwerkfreigabe in Home Assistant eingebunden?")
    return path


def validate_account_target(raw: str, acc_id: str) -> Path:
    target = validate_target(raw, create=True)
    for other_id, other in account_items():
        if other_id == acc_id or not other.get("target"):
            continue
        theirs = Path(other["target"])
        # Daneben oder darunter ist erlaubt, nur nicht im selben Ordner oder in den Sicherungsordnern des anderen
        if target == theirs or any(a.is_relative_to(b / job["folder"]) for a, b in ((target, theirs), (theirs, target))
                                   for job in JOBS.values()):
            raise ValueError(f"{target} überschneidet sich mit dem Ziel von „{other['name']}“ ({theirs}). "
                             "Bitte je Konto einen eigenen Ordner wählen.")
    return target


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


def rclone_delete(remote: str) -> None:
    """Entfernt ein Remote samt Token; „config create“ würde alte Schlüssel sonst übernehmen."""
    if not CONF.exists():
        return
    proc = subprocess.run([RCLONE, "--config", str(CONF), "config", "delete", remote],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise ValueError(f"Zugangsdaten konnten nicht entfernt werden: {clean_error(proc.stderr)}")


def login_start(acc_id: str, apple_id: str, password: str) -> bool:
    if not apple_id or not password:
        raise ValueError("Apple-ID und Passwort sind Pflicht.")
    with LOCK:  # gleiche Sperre wie die Zulassung in run_backup
        if RUN["active"]:
            raise ValueError("Während einer Sicherung ist keine Neuanmeldung möglich.")
        get_account(acc_id).pop("login_at", None)  # bis zur neuen 2FA keine Sicherungen mit halber Konfiguration
        STATE.pop("pending", None)
        save_state()
    rclone_delete(acc_id)
    out = rclone_config("create", acc_id, "iclouddrive", f"apple_id={apple_id}", f"password={password}", "--obscure")
    return login_step(out, acc_id, apple_id)


def login_continue(answer: str) -> bool:
    pending = STATE.get("pending")
    if not pending:
        raise ValueError("Keine Anmeldung offen. Bitte neu beginnen.")
    if not answer.strip():
        raise ValueError("Bitte eine Antwort eingeben.")
    get_account(pending["account"])
    out = rclone_config("update", pending["account"], "--continue", "--state", pending["state"], "--result", answer.strip())
    return login_step(out, pending["account"], pending["apple_id"])


def login_step(out: dict, acc_id: str, apple_id: str) -> bool:
    """Wertet eine rclone-Antwort aus. True = fertig angemeldet, False = weitere Frage offen."""
    if out.get("Error"):
        raise ValueError(out["Error"])  # offene Frage bleibt bestehen, Eingabe kann wiederholt werden
    with LOCK:
        if out.get("State"):
            STATE["pending"] = {"account": acc_id, "state": out["State"], "option": out.get("Option") or {},
                                "apple_id": apple_id}
            save_state()
            return False
        STATE.pop("pending", None)
        acc = get_account(acc_id)
        acc.pop("expiry_notified", None)
        acc.update(apple_id=apple_id, login_at=now_iso())
        save_state()
    CONF.chmod(0o600)
    ha_notification("dismiss", {"notification_id": f"icloud_backup_login_{acc_id}"})
    return True


def login_days_left(acc: dict, now: datetime) -> int | None:
    login_at = acc.get("login_at")
    if not login_at:
        return None
    return TOKEN_DAYS - (now - datetime.fromisoformat(login_at)).days


# ---------------------------------------------------------------- Konten

def add_account(form: dict) -> str:
    name = form.get("name", "").strip()
    if not name:
        raise ValueError("Bitte einen Namen für das Konto eingeben.")
    acc_id = new_account_id()
    target = validate_account_target(form.get("target", ""), acc_id)
    with LOCK:
        STATE["accounts"][acc_id] = {"name": name, "apple_id": form.get("apple_id", "").strip(), "target": str(target),
                                     "drive": "drive" in form, "photos": "photos" in form, "jobs": {}}
        save_state()
    return acc_id


def update_account(acc_id: str, form: dict) -> Path:
    acc = get_account(acc_id)
    name = form.get("name", "").strip()
    if not name:
        raise ValueError("Bitte einen Namen für das Konto eingeben.")
    target = validate_account_target(form.get("target", ""), acc_id)
    with LOCK:
        if RUN["active"]:  # sonst könnte ein anderes Konto das noch beschriebene Ziel übernehmen
            raise ValueError("Während einer Sicherung können Konten nicht geändert werden.")
        acc.update(name=name, target=str(target), drive="drive" in form, photos="photos" in form)
        save_state()
    return target


def delete_account(acc_id: str) -> str:
    with LOCK:  # durchgehend, damit run_backup das Konto nicht mitten im Löschen zulässt
        if RUN["active"]:
            raise ValueError("Während einer Sicherung kann kein Konto entfernt werden.")
        get_account(acc_id)
        rclone_delete(acc_id)  # zuerst, damit bei einem Fehler das Konto sichtbar bleibt
        acc = STATE["accounts"].pop(acc_id)
        if STATE.get("pending", {}).get("account") == acc_id:
            STATE.pop("pending")
        save_state()
        for key in JOBS:  # die ID wird wiederverwendet, sonst erbt das nächste Konto diese Protokolle
            log_path(acc_id, key).unlink(missing_ok=True)
    ha_notification("dismiss", {"notification_id": f"icloud_backup_login_{acc_id}"})
    return acc["name"]


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
    for acc_id, acc in account_items():
        left = login_days_left(acc, now)
        if left is None or left > WARN_DAYS or acc.get("expiry_notified") == date.today().isoformat():
            continue
        with LOCK:
            acc["expiry_notified"] = date.today().isoformat()
            save_state()
        text = (f"Die iCloud-Anmeldung für „{acc['name']}“ läuft in {left} Tagen ab." if left > 0
                else f"Die iCloud-Anmeldung für „{acc['name']}“ ist abgelaufen, Sicherungen schlagen fehl.")
        notify(f"icloud_backup_login_{acc_id}", "iCloud Backup: neu anmelden",
               f"{text} Bitte im Add-on unter „Konten“ neu anmelden.")


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


def set_job(acc: dict, key: str, **values) -> None:
    with LOCK:
        acc["jobs"].setdefault(key, {}).update(values)
        save_state()


def log_path(acc_id: str, key: str) -> Path:
    return LOG_DIR / f"{acc_id}-{key}.log"


def run_job(acc_id: str, acc: dict, key: str, target: Path) -> dict:
    job = JOBS[key]
    dest = target / job["folder"]
    dest.mkdir(exist_ok=True)
    if not on_mount(Path(os.path.realpath(dest))):  # z. B. Symlink auf lokale Platte
        raise ValueError(f"{dest} zeigt nicht auf eine eingebundene Freigabe.")
    cmd = [
        RCLONE, "--config", str(CONF), "copy", f"{acc_id}:", str(dest), *job["args"],
        "--cache-dir", str(DATA_DIR / "cache"), "--use-json-log", "--log-level", "NOTICE",
        "--stats", "15s", "--stats-log-level", "NOTICE",
    ]
    with LOCK:
        acc["jobs"][key] = {"status": "running", "started": now_iso(), "finished": "", "stats": {}, "error": ""}
        save_state()
    last_error = ""
    with log_path(acc_id, key).open("w", encoding="utf-8") as log, \
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
                set_job(acc, key, stats=stats)
    RUN["proc"] = None
    if RUN["cancel"]:
        status, error = "cancelled", ""
    elif proc.returncode == 0:
        status, error = "ok", ""
    else:
        status, error = "error", last_error or f"rclone wurde mit Code {proc.returncode} beendet."
    set_job(acc, key, status=status, finished=now_iso(), error=error)
    return acc["jobs"][key]


def backup_account(acc_id: str, acc: dict) -> list[str]:
    """Sichert ein Konto und liefert die Fehlermeldungen für die Benachrichtigung."""
    failures = []
    try:
        target = validate_target(acc["target"])
        if not on_mount(target):
            raise ValueError(f"{target} liegt nicht auf einer eingebundenen Freigabe. Sicherung übersprungen, "
                             "damit die Home-Assistant-Festplatte nicht vollläuft.")
        for key, job in JOBS.items():
            if acc[key] and not RUN["cancel"]:
                result = run_job(acc_id, acc, key, target)
                if result["status"] == "error":
                    failures.append(f"{acc['name']} – {job['label']}: {result['error']}")
        error = ""
    except (ValueError, OSError) as exc:
        error = str(exc)
        failures.append(f"{acc['name']}: {error}")
    with LOCK:
        acc["run_error"] = error
        save_state()
    return failures


def run_backup() -> None:
    with LOCK:
        if RUN["active"] or not is_ready():
            return
        RUN.update(active=True, cancel=False)
        STATE["last_start"] = now_iso()
        save_state()
    try:
        failures = []
        for acc_id, acc in account_items():
            if account_ready(acc) and not RUN["cancel"]:
                failures += backup_account(acc_id, acc)
        if failures:
            notify("icloud_backup_error", "iCloud Backup fehlgeschlagen", "\n".join(failures))
        elif not RUN["cancel"]:
            ha_notification("dismiss", {"notification_id": "icloud_backup_error"})
    finally:
        RUN["active"] = False


def account_ready(acc: dict) -> bool:
    return bool(acc.get("target") and (acc["drive"] or acc["photos"]) and acc.get("login_at"))


def is_ready() -> bool:
    return any(account_ready(acc) for _, acc in account_items())


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
    nav = [("/", "dashboard", "Übersicht"), ("/accounts", "accounts", "Konten"),
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


def login_status_text(acc: dict, now: datetime) -> tuple[str, str]:
    left = login_days_left(acc, now)
    if left is None:
        return "Nicht angemeldet", "error"
    if left <= 0:
        return "Abgelaufen", "error"
    return f"Noch {left} Tage gültig", "warn" if left <= WARN_DAYS else "ok"


def job_card(acc: dict, key: str) -> str:
    job = acc["jobs"].get(key)
    label = JOBS[key]["label"]
    if not acc[key]:
        return f'<article class="card job"><div class="job-head"><h2>{label}</h2><span class="status">Deaktiviert</span></div></article>'
    if not job:
        return (f'<article class="card job"><div class="job-head"><h2>{label}</h2><span class="status">Noch nie gesichert</span></div>'
                f'<p class="muted">Ziel: {h(acc["target"] or "–")}/{h(JOBS[key]["folder"])}</p></article>')
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


def account_section(base: str, acc: dict, now: datetime) -> str:
    login_text, login_level = login_status_text(acc, now)
    notes = alert(h(acc.get("run_error")), "error")
    if not acc.get("login_at"):
        notes += alert(f'„{h(acc["name"])}“ ist nicht angemeldet. <a href="{base}/accounts">Jetzt anmelden</a>')
    return f"""<h2 class="account-title">{h(acc['name'])}</h2>
{notes}
<div class="stats">
  <article><span>Apple-ID</span><strong class="path">{h(acc.get('apple_id') or '–')}</strong></article>
  <article><span>Anmeldung</span><strong class="{login_level}">{h(login_text)}</strong></article>
  <article><span>Ziel</span><strong class="path">{h(acc['target'] or '–')}</strong></article>
</div>
<div class="jobs">{job_card(acc, 'drive')}{job_card(acc, 'photos')}</div>"""


def dashboard(base: str) -> str:
    now = datetime.now()
    accounts = STATE["accounts"]
    notes = "" if accounts else alert(f'Noch kein iCloud-Konto eingerichtet. <a href="{base}/accounts">Konto hinzufügen</a>')
    due = next_run()
    next_text = "Läuft gerade" if RUN["active"] else (fmt_time(due.isoformat()) if due else "–")
    if RUN["active"]:
        buttons = f'<form method="post" action="{base}/cancel"><button class="button danger">Sicherung abbrechen</button></form>'
    else:
        disabled = "" if is_ready() else "disabled"
        buttons = f'<form method="post" action="{base}/run"><button class="button primary" {disabled}>Alle Konten jetzt sichern</button></form>'
    sections = "".join(account_section(base, acc, now) for _, acc in account_items())
    return f"""{notes}
<div class="actions">{buttons}</div>
<div class="stats">
  <article><span>Konten</span><strong>{len(accounts)}</strong></article>
  <article><span>Letzte Sicherung</span><strong>{fmt_time(STATE.get('last_start'))}</strong></article>
  <article><span>Nächste Sicherung</span><strong>{h(next_text)}</strong></article>
</div>
{sections}"""


QUESTION_LABELS = {
    "config_2fa": ("Bestätigungscode",
                   "Apple hat einen sechsstelligen Code an die Apple-Geräte dieser Apple-ID geschickt. "
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


def target_datalist() -> str:
    return '<datalist id="targets">' + "".join(f'<option value="{h(p)}">' for p, _ in target_candidates()) + "</datalist>"


def account_fields(acc: dict) -> str:
    checked = {key: "checked" if acc.get(key, True) else "" for key in JOBS}
    return f"""<label><span>Name</span><input name="name" required placeholder="Denis" value="{h(acc.get('name'))}"></label>
      <label><span>Sicherungsziel</span>
        <input name="target" list="targets" required placeholder="/share/nas-backup/Denis" value="{h(acc.get('target'))}"></label>
      <label class="check"><input type="checkbox" name="drive" {checked['drive']}><span>iCloud Drive sichern</span></label>
      <label class="check"><input type="checkbox" name="photos" {checked['photos']}><span>iCloud-Fotos sichern</span></label>"""


def account_card(base: str, acc_id: str, acc: dict, now: datetime) -> str:
    text, level = login_status_text(acc, now)
    valid = "–"
    if acc.get("login_at"):
        valid = fmt_time((datetime.fromisoformat(acc["login_at"]) + timedelta(days=TOKEN_DAYS)).isoformat())
    hidden = f'<input type="hidden" name="id" value="{h(acc_id)}">'
    return f"""<article class="card form">
  <h2>{h(acc['name'])}</h2>
  <dl class="facts">
    <div><dt>Apple-ID</dt><dd>{h(acc.get('apple_id') or '–')}</dd></div>
    <div><dt>Status</dt><dd class="{level}">{h(text)}</dd></div>
    <div><dt>Angemeldet am</dt><dd>{fmt_time(acc.get('login_at'))}</dd></div>
    <div><dt>Gültig bis</dt><dd>{valid}</dd></div>
  </dl>
  <form method="post" action="{base}/accounts/login">{hidden}
    <div class="form-grid">
      <label><span>Apple-ID</span><input type="email" name="apple_id" required autocomplete="username" value="{h(acc.get('apple_id'))}"></label>
      <label><span>Passwort</span><input type="password" name="password" required autocomplete="current-password"></label>
    </div>
    <div class="form-actions"><button class="button primary">{'Neu anmelden' if acc.get('login_at') else 'Anmelden'}</button></div>
  </form>
  <form method="post" action="{base}/accounts/save">{hidden}
    <div class="form-grid">{account_fields(acc)}</div>
    <div class="form-actions"><button class="button">Speichern</button></div>
  </form>
  <form method="post" action="{base}/accounts/delete" class="form-actions">{hidden}
    <button class="button danger">Konto entfernen</button>
    <span class="muted">Bereits gesicherte Dateien bleiben im Ziel erhalten.</span>
  </form>
</article>"""


def accounts_page(base: str) -> str:
    pending = STATE.get("pending")
    if pending and pending["account"] in STATE["accounts"]:
        name = STATE["accounts"][pending["account"]]["name"]
        return f"""<article class="card form">
  <div class="setup-step"><span>2</span><div><strong>Anmeldung bestätigen: {h(name)}</strong><small>{h(pending['apple_id'])}</small></div></div>
  <form method="post" action="{base}/accounts/code">
    <div class="form-grid">{question_field(pending['option'])}</div>
    <p class="muted">Ist Erweiterter Datenschutz (ADP) aktiv, musst du den Zugriff danach eventuell noch auf einem Apple-Gerät erlauben.</p>
    <div class="form-actions"><button class="button primary">Bestätigen</button></div>
  </form>
  <form method="post" action="{base}/accounts/reset" class="form-actions"><button class="button">Abbrechen und neu beginnen</button></form>
</article>"""
    now = datetime.now()
    cards = "".join(account_card(base, acc_id, acc, now) for acc_id, acc in account_items())
    return f"""{target_datalist()}{cards}
<article class="card form">
  <div class="setup-step"><span>+</span><div><strong>Konto hinzufügen</strong>
  <small>Je Apple-ID ein Konto mit eigenem Zielordner. Apple verlangt die Anmeldung alle {TOKEN_DAYS} Tage neu.</small></div></div>
  <ul class="setup-guide">
    <li>Auf dem iPhone dieser Apple-ID muss <b>Einstellungen → Apple-Account → iCloud → „Auf iCloud-Daten im Web zugreifen“</b> eingeschaltet sein.</li>
    <li>Normales Apple-ID-Passwort verwenden, <b>app-spezifische Passwörter funktionieren nicht</b>.</li>
    <li>Der 2FA-Code kommt auf den Geräten <b>dieser</b> Apple-ID an, die Person muss also erreichbar sein.</li>
    <li>Das Passwort wird nur an rclone übergeben und verschleiert in <code>/data/rclone.conf</code> abgelegt.
        Diese Datei ist von Home-Assistant-Backups ausgenommen.</li>
  </ul>
  <form method="post" action="{base}/accounts/add">
    <div class="form-grid">
      {account_fields({})}
      <label><span>Apple-ID</span><input type="email" name="apple_id" required autocomplete="off"></label>
      <label><span>Passwort</span><input type="password" name="password" required autocomplete="new-password"></label>
    </div>
    <div class="form-actions"><button class="button primary">Hinzufügen und anmelden</button></div>
  </form>
</article>"""


def settings_page(base: str) -> str:
    rows = "".join(
        f'<tr><td><code>{h(path)}</code></td><td><span class="status {"ok" if mounted else "error"}">'
        f'{"Freigabe eingebunden" if mounted else "Lokaler Ordner"}</span></td></tr>'
        for path, mounted in target_candidates()
    ) or '<tr><td colspan="2" class="empty">Keine Ordner unter /share oder /media gefunden.</td></tr>'
    return f"""<article class="card form">
  <form method="post" action="{base}/settings">
    <div class="form-grid">
      <label><span>Intervall in Stunden</span>
        <input type="number" name="interval_hours" min="1" max="168" required value="{h(STATE['settings']['interval_hours'])}"></label>
    </div>
    <p class="muted">Alle Konten werden nacheinander gesichert. Im Ziel jedes Kontos entstehen die Ordner
    <code>iCloud Drive</code> und <code>iCloud Fotos</code>. Es wird nur kopiert: Was in iCloud gelöscht wird,
    bleibt in der Sicherung erhalten. Geänderte Dateien werden in der Sicherung überschrieben.</p>
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
    for acc_id, acc in account_items():
        for key, job in JOBS.items():
            path = log_path(acc_id, key)
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
            parts.append(f'<article class="card form"><h2>{h(acc["name"])} – {job["label"]}</h2>'
                         f'<pre class="command-block">{text}</pre></article>')
    return "".join(parts) or alert("Noch kein Konto eingerichtet.")


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
            raise ValueError("Erst ein Konto anmelden und ein Ziel festlegen.")
        start_backup_thread()
        return "/", "Sicherung gestartet.", "success"
    if path == "/cancel":
        with LOCK:
            RUN["cancel"] = True
            if RUN["proc"]:
                RUN["proc"].terminate()
        return "/", "Sicherung wird abgebrochen.", "success"
    if path == "/settings":
        hours = int(form.get("interval_hours") or 24)
        if not 1 <= hours <= 168:
            raise ValueError("Das Intervall muss zwischen 1 und 168 Stunden liegen.")
        with LOCK:
            STATE["settings"]["interval_hours"] = hours
            save_state()
        return "/settings", "Gespeichert.", "success"
    if not path.startswith("/accounts/"):
        raise LookupError(path)
    if not AUTH_LOCK.acquire(blocking=False):
        raise ValueError("Es läuft bereits eine Anmeldung. Bitte kurz warten.")
    try:
        return handle_accounts(path, form)
    finally:
        AUTH_LOCK.release()


def handle_accounts(path: str, form: dict) -> tuple[str, str, str]:
    acc_id = form.get("id", "")
    if path == "/accounts/add":
        acc_id = add_account(form)
        done = login_start(acc_id, form.get("apple_id", "").strip(), form.get("password", ""))
        return "/accounts", "Konto angelegt und angemeldet." if done else "", "success"
    if path == "/accounts/login":
        done = login_start(acc_id, form.get("apple_id", "").strip(), form.get("password", ""))
        return "/accounts", "Angemeldet." if done else "", "success"
    if path == "/accounts/code":
        done = login_continue(form.get("answer", ""))
        return ("/", "Angemeldet. Die Sicherung startet automatisch.", "success") if done else ("/accounts", "", "")
    if path == "/accounts/reset":
        with LOCK:
            STATE.pop("pending", None)
            save_state()
        return "/accounts", "", ""
    if path == "/accounts/save":
        target = update_account(acc_id, form)
        warning = "" if on_mount(target) else " Achtung: Das Ziel ist keine eingebundene Freigabe, es wird nicht gesichert."
        return "/accounts", "Gespeichert." + warning, "success" if not warning else "error"
    if path == "/accounts/delete":
        return "/accounts", f"Konto „{delete_account(acc_id)}“ entfernt.", "success"
    raise LookupError(path)


PAGES = {
    "/": ("Übersicht", "dashboard"),
    "/accounts": ("Konten", "accounts"),
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
            back = "/accounts" if path.startswith("/accounts") else ("/settings" if path == "/settings" else "/")
            return redirect(start_response, base, back, str(exc), "error")
        return redirect(start_response, base, target, message, level)
    if path not in PAGES:
        return respond(start_response, "Nicht gefunden", 404)
    title, active = PAGES[path]
    query = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    flash = alert(h(query["msg"][-1]), query.get("level", ["success"])[-1]) if "msg" in query else ""
    body = {"dashboard": lambda: dashboard(base), "accounts": lambda: accounts_page(base),
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

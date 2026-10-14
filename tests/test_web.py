import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

TMP = Path(tempfile.mkdtemp()).resolve()
os.environ["ICB_DATA_DIR"] = str(TMP / "data")
os.environ["ICB_RCLONE"] = str(Path(__file__).with_name("fake_rclone.py"))
os.environ["FAKE_RCLONE_CALLS"] = str(TMP / "calls.jsonl")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "icloud_backup" / "app"))

import web  # noqa: E402


def calls() -> list[list[str]]:
    path = Path(os.environ["FAKE_RCLONE_CALLS"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=TMP))
        (self.root / "nas").mkdir()
        (self.root / "nas2").mkdir()
        web.TARGET_ROOTS = (self.root,)
        web.DATA_DIR.mkdir(parents=True, exist_ok=True)
        web.LOG_DIR.mkdir(parents=True, exist_ok=True)
        web.CONF.unlink(missing_ok=True)
        web.STATE_FILE.unlink(missing_ok=True)
        Path(os.environ["FAKE_RCLONE_CALLS"]).unlink(missing_ok=True)
        os.environ.pop("FAKE_RCLONE_FAIL", None)
        web.STATE.clear()
        web.STATE.update(web.load_state())
        self.notes = mock.patch.object(web, "ha_notification").start()
        self.addCleanup(mock.patch.stopall)

    def add(self, name="Denis", folder="nas", apple_id="a@example.com") -> str:
        acc_id = web.add_account({"name": name, "target": str(self.root / folder), "drive": "on", "photos": "on"})
        web.login_start(acc_id, apple_id, "geheim")
        web.login_continue("123456")
        return acc_id


class TargetTests(Base):
    def test_rejects_outside_root_and_root_itself(self):
        for bad in ("/etc", str(self.root), str(self.root / "fehlt"), str(self.root / "nas" / ".." / "..")):
            with self.assertRaises(ValueError, msg=bad):
                web.validate_target(bad)
        self.assertEqual(web.validate_target(str(self.root / "nas")), self.root / "nas")

    def test_mount_detection_walks_up_to_root(self):
        sub = self.root / "nas" / "unter"
        sub.mkdir()
        self.assertFalse(web.on_mount(sub))
        with mock.patch("os.path.ismount", side_effect=lambda p: Path(p) == self.root / "nas"):
            self.assertTrue(web.on_mount(sub))

    def test_account_subfolder_is_created_only_on_mounted_share(self):
        with self.assertRaisesRegex(ValueError, "gibt es nicht"):
            web.validate_account_target(str(self.root / "nas" / "Anna"), "x")
        with mock.patch("os.path.ismount", side_effect=lambda p: Path(p) == self.root / "nas"):
            self.assertEqual(web.validate_account_target(str(self.root / "nas" / "Anna"), "x"), self.root / "nas" / "Anna")
        self.assertTrue((self.root / "nas" / "Anna").is_dir())

    def test_account_targets_must_not_overlap_backup_folders(self):
        self.add()
        (self.root / "nas" / "iCloud Drive" / "x").mkdir(parents=True)
        (self.root / "nas" / "Anna").mkdir()
        for bad in ("nas", "nas/iCloud Drive", "nas/iCloud Drive/x"):
            with self.assertRaisesRegex(ValueError, "überschneidet sich mit dem Ziel von „Denis“", msg=bad):
                web.add_account({"name": "Anna", "target": str(self.root / bad)})
        web.add_account({"name": "Anna", "target": str(self.root / "nas" / "Anna")})  # daneben ist erlaubt


class LoginTests(Base):
    def test_two_factor_flow(self):
        acc_id = web.add_account({"name": "Denis", "target": str(self.root / "nas")})
        self.assertFalse(web.login_start(acc_id, "a@example.com", "geheim"))
        self.assertEqual(web.STATE["pending"]["state"], "2fa_do")
        self.assertEqual(web.STATE["pending"]["account"], acc_id)
        create = [c for c in calls() if "create" in c][0]
        self.assertIn("password=geheim", create)
        self.assertIn("--obscure", create)
        self.assertEqual(create[create.index("create") + 1], acc_id)

        with self.assertRaisesRegex(ValueError, "Code falsch"):
            web.login_continue("000000")
        self.assertEqual(web.STATE["pending"]["state"], "2fa_do")  # Eingabe darf wiederholt werden

        self.assertTrue(web.login_continue("123456"))
        self.assertNotIn("pending", web.STATE)
        self.assertEqual(web.STATE["accounts"][acc_id]["apple_id"], "a@example.com")
        self.assertEqual(calls()[-1][-8:],
                         ["update", acc_id, "--continue", "--state", "2fa_do", "--result", "123456", "--non-interactive"])
        self.assertEqual(oct(web.CONF.stat().st_mode & 0o777), "0o600")

    def test_relogin_deletes_only_own_remote(self):
        first = self.add()
        second = self.add("Anna", "nas2", "b@example.com")
        web.login_start(first, "a@example.com", "geheim")
        deletes = [c for c in calls() if "delete" in c]
        self.assertEqual(deletes[-1][-1], first)
        self.assertNotIn("login_at", web.STATE["accounts"][first])
        self.assertIn("login_at", web.STATE["accounts"][second])
        self.assertTrue(web.is_ready())  # das andere Konto sichert weiter

    def test_wrong_password_shows_clean_error(self):
        acc_id = web.add_account({"name": "Denis", "target": str(self.root / "nas")})
        with self.assertRaisesRegex(ValueError, "^failed to create config: Apple-ID oder Passwort falsch$"):
            web.login_start(acc_id, "a@example.com", "falsch")

    def test_restore_without_rclone_conf_requires_login(self):
        acc_id = self.add()
        web.save_state()
        web.CONF.unlink()
        self.assertNotIn("login_at", web.load_state()["accounts"][acc_id])

    def test_timeout_does_not_leak_password(self):
        acc_id = web.add_account({"name": "Denis", "target": str(self.root / "nas")})
        error = web.subprocess.TimeoutExpired(["rclone", "password=geheim"], 180)
        with mock.patch.object(web.subprocess, "run", side_effect=error):
            with self.assertRaises(ValueError) as ctx:
                web.login_start(acc_id, "a@example.com", "geheim")
        self.assertNotIn("geheim", str(ctx.exception))

    def test_expiry_notification_once_per_day_per_account(self):
        first = self.add()
        second = self.add("Anna", "nas2", "b@example.com")
        for acc_id in (first, second):
            web.STATE["accounts"][acc_id]["login_at"] = (datetime.now() - timedelta(days=27)).isoformat()
        web.check_expiry(datetime.now())
        web.check_expiry(datetime.now())
        created = [c for c in self.notes.call_args_list if c.args[0] == "create"]
        self.assertEqual([c.args[1]["notification_id"] for c in created],
                         [f"icloud_backup_login_{first}", f"icloud_backup_login_{second}"])
        self.assertIn("„Anna“ läuft in 3 Tagen ab", created[1].args[1]["message"])

    def test_delete_account_removes_remote_and_state(self):
        acc_id = self.add()
        self.assertEqual(web.delete_account(acc_id), "Denis")
        self.assertEqual(web.STATE["accounts"], {})
        self.assertEqual([c for c in calls() if "delete" in c][-1][-1], acc_id)

    def test_failed_remote_delete_keeps_account(self):
        acc_id = self.add()
        failed = web.subprocess.CompletedProcess([], 1, "", "Error: config file is locked")
        with mock.patch.object(web.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(ValueError, "nicht entfernt werden: config file is locked"):
                web.delete_account(acc_id)
        self.assertIn(acc_id, web.STATE["accounts"])

    def test_no_account_changes_during_backup(self):
        acc_id = self.add()
        with mock.patch.dict(web.RUN, active=True):
            with self.assertRaisesRegex(ValueError, "Während einer Sicherung"):
                web.update_account(acc_id, {"name": "Denis", "target": str(self.root / "nas2")})
        self.assertEqual(web.STATE["accounts"][acc_id]["target"], str(self.root / "nas"))

    def test_deleted_account_logs_are_not_inherited(self):
        acc_id = self.add()
        web.log_path(acc_id, "drive").write_text("alt\n")
        web.delete_account(acc_id)
        self.assertEqual(self.add("Anna"), acc_id)  # ID wird wiederverwendet
        self.assertNotIn("alt", web.log_page())


class MigrationTests(Base):
    def test_single_account_state_is_taken_over_with_same_target(self):
        web.CONF.write_text("[icloud]\ntype = iclouddrive\n")
        (web.LOG_DIR / "drive.log").write_text("alt\n")
        web.STATE_FILE.write_text(json.dumps({
            "settings": {"target": "/share/nas", "drive": True, "photos": False, "interval_hours": 12},
            "login": {"apple_id": "a@example.com", "login_at": "2026-10-12T10:00:00"},
            "jobs": {"drive": {"status": "running"}}, "last_start": "2026-10-12T10:00:00",
            "expiry_notified": "2026-10-12", "run_error": "",
        }))
        state = web.load_state()
        self.assertEqual(state["settings"], {"interval_hours": 12})
        acc = state["accounts"]["icloud"]
        self.assertEqual((acc["name"], acc["target"], acc["drive"], acc["photos"], acc["login_at"]),
                         ("a@example.com", "/share/nas", True, False, "2026-10-12T10:00:00"))
        self.assertEqual(acc["jobs"]["drive"]["status"], "cancelled")
        self.assertFalse({"login", "jobs", "expiry_notified", "run_error"} & state.keys())
        self.assertEqual((web.LOG_DIR / "icloud-drive.log").read_text(), "alt\n")


class BackupTests(Base):
    def test_refuses_unmounted_target(self):
        acc_id = self.add()
        web.run_backup()
        self.assertIn("nicht auf einer eingebundenen Freigabe", web.STATE["accounts"][acc_id]["run_error"])
        self.assertFalse([c for c in calls() if "copy" in c])

    def test_backup_copies_both_services(self):
        acc_id = self.add()
        with mock.patch.object(web, "on_mount", return_value=True):
            web.run_backup()
        jobs = web.STATE["accounts"][acc_id]["jobs"]
        self.assertEqual({jobs["drive"]["status"], jobs["photos"]["status"]}, {"ok"})
        self.assertEqual(jobs["drive"]["stats"]["transfers"], 1)
        self.assertTrue((self.root / "nas" / "iCloud Fotos" / "datei.txt").exists())
        copies = [c for c in calls() if "copy" in c]
        self.assertIn("/*/All Photos/**", copies[1])
        self.assertNotIn("--ignore-size", copies[1])
        self.assertIn("--ignore-size", copies[0])
        self.assertIn("photos", copies[1])
        self.assertEqual(web.next_run(), datetime.fromisoformat(web.STATE["last_start"]) + timedelta(hours=24))

    def test_each_account_uses_own_remote_and_target(self):
        first = self.add()
        second = self.add("Anna", "nas2", "b@example.com")
        with mock.patch.object(web, "on_mount", return_value=True):
            web.run_backup()
        copies = [(c[c.index("copy") + 1], c[c.index("copy") + 2]) for c in calls() if "copy" in c]
        self.assertEqual(copies, [
            (f"{first}:", str(self.root / "nas" / "iCloud Drive")), (f"{first}:", str(self.root / "nas" / "iCloud Fotos")),
            (f"{second}:", str(self.root / "nas2" / "iCloud Drive")), (f"{second}:", str(self.root / "nas2" / "iCloud Fotos")),
        ])

    def test_account_without_login_is_skipped(self):
        self.add()
        web.add_account({"name": "Anna", "target": str(self.root / "nas2"), "drive": "on"})
        with mock.patch.object(web, "on_mount", return_value=True):
            web.run_backup()
        self.assertFalse((self.root / "nas2" / "iCloud Drive").exists())

    def test_failure_is_reported_with_account_name(self):
        acc_id = self.add()
        os.environ["FAKE_RCLONE_FAIL"] = "1"
        with mock.patch.object(web, "on_mount", return_value=True):
            web.run_backup()
        self.assertEqual(web.STATE["accounts"][acc_id]["jobs"]["drive"]["error"], "Pages/Brief.pages: corrupted on transfer")
        created = [c for c in self.notes.call_args_list if c.args[0] == "create"]
        self.assertIn("Denis – iCloud Drive: Pages/Brief.pages: corrupted on transfer", created[-1].args[1]["message"])
        self.assertIn("Pages/Brief.pages: corrupted on transfer", web.log_page())


class HttpTests(Base):
    def request(self, method, path, body="", addr=web.INGRESS_PROXY):
        status = {}
        raw = body.encode()
        environ = {
            "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "", "REMOTE_ADDR": addr,
            "HTTP_X_INGRESS_PATH": "/api/hassio_ingress/abc", "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }
        out = web.application(environ, lambda s, headers: status.update(code=s, headers=dict(headers)))
        return status["code"], status["headers"], b"".join(out).decode()

    def test_only_ingress_proxy_allowed(self):
        self.assertTrue(self.request("GET", "/", addr="192.168.1.20")[0].startswith("403"))

    def test_pages_render_with_ingress_links(self):
        self.add()
        for path in ("/", "/accounts", "/settings", "/log"):
            code, _, body = self.request("GET", path)
            self.assertTrue(code.startswith("200"), path)
            self.assertIn('href="/api/hassio_ingress/abc/settings"', body)

    def test_settings_post_redirects_into_ingress(self):
        code, headers, _ = self.request("POST", "/settings", "interval_hours=12")
        self.assertTrue(code.startswith("303"))
        self.assertTrue(headers["Location"].startswith("/api/hassio_ingress/abc/settings?"))
        self.assertEqual(web.STATE["settings"], {"interval_hours": 12})

    def test_add_account_via_form_starts_two_factor(self):
        body = f"name=Anna&target={self.root}/nas2&photos=on&apple_id=b%40example.com&password=geheim"
        code, headers, _ = self.request("POST", "/accounts/add", body)
        self.assertTrue(headers["Location"].endswith("/accounts"))
        acc = web.STATE["accounts"][web.STATE["pending"]["account"]]
        self.assertEqual((acc["name"], acc["drive"], acc["photos"]), ("Anna", False, True))
        self.assertIn("Anmeldung bestätigen: Anna", self.request("GET", "/accounts")[2])

    def test_save_unknown_account_is_an_error(self):
        _, headers, _ = self.request("POST", "/accounts/save", f"id=weg&name=x&target={self.root}/nas")
        self.assertIn("level=error", headers["Location"])

    def test_pending_question_page_escapes_help(self):
        acc_id = web.add_account({"name": "<b>Denis</b>", "target": str(self.root / "nas")})
        web.STATE["pending"] = {"account": acc_id, "state": "x", "apple_id": "a@example.com",
                                "option": {"Name": "neu", "Help": "<script>"}}
        body = self.request("GET", "/accounts")[2]
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>", body)
        self.assertNotIn("<b>Denis</b>", body)


if __name__ == "__main__":
    unittest.main()

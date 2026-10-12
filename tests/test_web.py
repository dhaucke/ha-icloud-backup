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

    def login(self):
        web.login_start("a@example.com", "geheim")
        web.login_continue("123456")

    def configure(self):
        web.STATE["settings"].update(target=str(self.root / "nas"))


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


class LoginTests(Base):
    def test_two_factor_flow(self):
        self.assertFalse(web.login_start("a@example.com", "geheim"))
        self.assertEqual(web.STATE["pending"]["state"], "2fa_do")
        create = calls()[0]
        self.assertIn("password=geheim", create)
        self.assertIn("--obscure", create)

        with self.assertRaisesRegex(ValueError, "Code falsch"):
            web.login_continue("000000")
        self.assertEqual(web.STATE["pending"]["state"], "2fa_do")  # Eingabe darf wiederholt werden

        self.assertTrue(web.login_continue("123456"))
        self.assertNotIn("pending", web.STATE)
        self.assertEqual(web.STATE["login"]["apple_id"], "a@example.com")
        self.assertEqual(calls()[-1][-6:], ["--continue", "--state", "2fa_do", "--result", "123456", "--non-interactive"])
        self.assertEqual(oct(web.CONF.stat().st_mode & 0o777), "0o600")

    def test_wrong_password_shows_clean_error(self):
        with self.assertRaisesRegex(ValueError, "^failed to create config: Apple-ID oder Passwort falsch$"):
            web.login_start("a@example.com", "falsch")

    def test_restore_without_rclone_conf_requires_login(self):
        self.login()
        web.save_state()
        web.CONF.unlink()
        self.assertNotIn("login_at", web.load_state()["login"])

    def test_relogin_blocks_backups_until_finished(self):
        self.login()
        self.configure()
        web.login_start("a@example.com", "geheim")
        self.assertFalse(web.is_ready())

    def test_timeout_does_not_leak_password(self):
        error = web.subprocess.TimeoutExpired(["rclone", "password=geheim"], 180)
        with mock.patch.object(web.subprocess, "run", side_effect=error):
            with self.assertRaises(ValueError) as ctx:
                web.login_start("a@example.com", "geheim")
        self.assertNotIn("geheim", str(ctx.exception))

    def test_expiry_notification_once_per_day(self):
        self.login()
        web.STATE["login"]["login_at"] = (datetime.now() - timedelta(days=27)).isoformat()
        web.check_expiry(datetime.now())
        web.check_expiry(datetime.now())
        created = [c for c in self.notes.call_args_list if c.args[0] == "create"]
        self.assertEqual(len(created), 1)
        self.assertIn("3 Tagen", created[0].args[1]["message"])


class BackupTests(Base):
    def test_refuses_unmounted_target(self):
        self.login()
        self.configure()
        web.run_backup()
        self.assertIn("nicht auf einer eingebundenen Freigabe", web.STATE["run_error"])
        self.assertFalse([c for c in calls() if "copy" in c])

    def test_backup_copies_both_services(self):
        self.login()
        self.configure()
        with mock.patch.object(web, "on_mount", return_value=True):
            web.run_backup()
        jobs = web.STATE["jobs"]
        self.assertEqual({jobs["drive"]["status"], jobs["photos"]["status"]}, {"ok"})
        self.assertEqual(jobs["drive"]["stats"]["transfers"], 1)
        self.assertTrue((self.root / "nas" / "iCloud Fotos" / "datei.txt").exists())
        photos = [c for c in calls() if "copy" in c][1]
        self.assertIn("/*/All Photos/**", photos)
        self.assertIn("photos", photos)
        self.assertEqual(web.next_run(), datetime.fromisoformat(web.STATE["last_start"]) + timedelta(hours=24))

    def test_failure_is_reported(self):
        self.login()
        self.configure()
        os.environ["FAKE_RCLONE_FAIL"] = "1"
        with mock.patch.object(web, "on_mount", return_value=True):
            web.run_backup()
        self.assertEqual(web.STATE["jobs"]["drive"]["error"], "trust token abgelaufen")
        created = [c for c in self.notes.call_args_list if c.args[0] == "create"]
        self.assertIn("iCloud Drive: trust token abgelaufen", created[-1].args[1]["message"])


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
        for path in ("/", "/login", "/settings", "/log"):
            code, _, body = self.request("GET", path)
            self.assertTrue(code.startswith("200"), path)
            self.assertIn('href="/api/hassio_ingress/abc/settings"', body)

    def test_settings_post_redirects_into_ingress(self):
        code, headers, _ = self.request("POST", "/settings", f"target={self.root}/nas&drive=on&interval_hours=12")
        self.assertTrue(code.startswith("303"))
        self.assertTrue(headers["Location"].startswith("/api/hassio_ingress/abc/settings?"))
        self.assertEqual(web.STATE["settings"], {"target": str(self.root / "nas"), "drive": True, "photos": False, "interval_hours": 12})

    def test_pending_question_page_escapes_help(self):
        web.STATE["pending"] = {"state": "x", "apple_id": "a@example.com", "option": {"Name": "neu", "Help": "<script>"}}
        body = self.request("GET", "/login")[2]
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>", body)


if __name__ == "__main__":
    unittest.main()

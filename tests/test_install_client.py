"""install-hook merging, and Jev client safety (against a LOCAL fake HTTP server only)."""
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from helpers import TempProject, run_cli

from jevmem import install
from jevmem.jev_client import BillingError, JevClient, JevError, JevTimeout

SECRET = "fake-test-SECRET-KEY-0123456789"


class TestInstall(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.proj = Path(self._td.name)
        (self.proj / ".claude").mkdir()
        self.settings = self.proj / ".claude" / "settings.json"
        self.cfg = self.proj / "jevmem.toml"
        self.cfg.write_text("", encoding="utf-8")
        self.quiet = lambda *_: None

    def tearDown(self):
        self._td.cleanup()

    def test_merges_without_touching_other_hooks_and_backs_up(self):
        original = {"model": "x", "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo other"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]}}
        self.settings.write_text(json.dumps(original), encoding="utf-8")
        res = install.install(self.proj, self.cfg, out=self.quiet)
        self.assertEqual(res["action"], "added")
        self.assertTrue(Path(res["backup"]).exists())
        self.assertEqual(json.loads(Path(res["backup"]).read_text()), original)
        new = json.loads(self.settings.read_text())
        self.assertEqual(new["model"], "x")
        self.assertEqual(new["hooks"]["Stop"], original["hooks"]["Stop"])
        cmds = [h["command"] for g in new["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertIn("echo other", cmds)
        self.assertEqual(sum("-m jevmem hook " in c for c in cmds), 1)
        self.assertEqual(sum("-m jevmem capture inbound " in c for c in cmds), 1)
        post = new["hooks"]["PostToolUse"]
        self.assertEqual(len(post), 1)
        self.assertEqual(set(post[0]["matcher"].split("|")),
                         {"mcp__plugin_telegram_telegram__reply", "mcp__plugin_discord_discord__reply"})
        self.assertIn("-m jevmem capture outbound ", post[0]["hooks"][0]["command"])

    def test_idempotent_and_updates_in_place(self):
        install.install(self.proj, self.cfg, out=self.quiet)
        res = install.install(self.proj, self.cfg, out=self.quiet)
        self.assertEqual(res["action"], "unchanged")
        self.assertIsNone(res["backup"])
        res = install.install(self.proj, self.cfg, python="/other/python3", out=self.quiet)
        self.assertEqual(res["action"], "updated")
        hooks = json.loads(self.settings.read_text())["hooks"]
        cmds = [h["command"] for ev in hooks.values() for g in ev for h in g["hooks"]]
        self.assertEqual(len(cmds), 3, "updated in place, not duplicated")
        self.assertTrue(all(c.startswith("/other/python3 -m jevmem ") for c in cmds))

    def test_no_capture_installs_only_recall(self):
        install.install(self.proj, self.cfg, capture_hooks=False, out=self.quiet)
        hooks = json.loads(self.settings.read_text())["hooks"]
        self.assertNotIn("PostToolUse", hooks)
        self.assertEqual(len(hooks["UserPromptSubmit"]), 1)

    def test_creates_settings_when_missing_and_uninstalls(self):
        self.assertFalse(self.settings.exists())
        install.install(self.proj, self.cfg, out=self.quiet)
        self.assertTrue(self.settings.exists())
        res = install.install(self.proj, self.cfg, uninstall=True, out=self.quiet)
        self.assertEqual(res["action"], "removed 3")
        hooks = json.loads(self.settings.read_text())["hooks"]
        self.assertEqual(hooks["UserPromptSubmit"], [])
        self.assertEqual(hooks["PostToolUse"], [])

    def test_dry_run_writes_nothing(self):
        install.install(self.proj, self.cfg, dry_run=True, out=self.quiet)
        self.assertFalse(self.settings.exists())

    def test_cli_prints_schedule_help(self):
        tp = TempProject()
        try:
            r = run_cli("install-hook", "--project", str(tp.root), cfg_path=tp.cfg_path)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("cron", r.stdout)
            self.assertIn("jevmem index", r.stdout)
            self.assertTrue((tp.root / ".claude" / "settings.json").exists())
        finally:
            tp.cleanup()


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"
    last_user_agent = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Handler.last_user_agent = self.headers.get("User-Agent")
        auth = self.headers.get("Authorization", "")
        if self.mode == "slow":
            time.sleep(1.0)
        if self.mode == "402":
            body, code = b'{"detail":"payment required"}', 402
        elif self.mode == "echo500":
            body, code = json.dumps({"error": f"bad request with {auth}"}).encode(), 500
        elif self.mode == "detail":
            body, code = json.dumps({"detail": f"invalid key {auth}"}).encode(), 200
        else:
            body, code = json.dumps({"answers": {"q": {"probabilities": {"a": 1.0}}},
                                     "usage": {"input_tokens": 7}}).encode(), 200
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class TestClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}/v1/systemone"
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def client(self, mode):
        _Handler.mode = mode
        return JevClient(SECRET, self.url)

    def test_ok(self):
        a, u = self.client("ok").call("s", {"q": {"type": "choice"}}, timeout=2)
        self.assertEqual(a["q"]["probabilities"], {"a": 1.0})
        self.assertEqual(u["input_tokens"], 7)
        from jevmem import __version__
        self.assertEqual(_Handler.last_user_agent, f"jev-agent-memory/{__version__}")

    def test_402_is_billing_error(self):
        with self.assertRaises(BillingError):
            self.client("402").call("s", {}, timeout=2)

    def test_timeout_does_not_leak_key(self):
        with self.assertRaises(JevTimeout) as cm:
            self.client("slow").call("s", {}, timeout=0.2)
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertNotIn(SECRET, repr(cm.exception))
        self.assertIsNone(cm.exception.__cause__)

    def test_error_bodies_are_masked(self):
        for mode in ("echo500", "detail"):
            with self.assertRaises(JevError) as cm:
                self.client(mode).call("s", {}, timeout=2)
            self.assertNotIn(SECRET, str(cm.exception))
            self.assertIn("***", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

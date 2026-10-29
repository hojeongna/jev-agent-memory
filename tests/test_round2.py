"""Pre-release round 2: subscription-only `claude -p` and its outage breaker, linear-time
<channel> parsing, a hard Jev deadline, digest locking and midnight, consolidation dedupe and
filters, config validation, hook/CLI details. Fakes and local servers only: no Jev API, no
real `claude` (subprocess is mocked or the binary path does not exist), no Ollama, no
crontab or launchctl."""
import contextlib
import io
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from helpers import (DEFAULT_RULES, NO_KEY_ENV, SRC, FakeClock, FakeJev, JevError, TempProject,
                     hook_error_log_text, keyword_labeler, run_cli)

from jevmem import (capture, cli, config, consolidate, digest, fsutil, hook, indexer, install, llm, raw,
                    recall, schedule, status, store, text)
from jevmem.jev_client import BillingError, JevClient, JevTimeout, is_billing_error
from test_labeler import FakeHaiku
from test_stages import FakeLLM, tg

NO_CLAUDE = "/nonexistent/jevmem-test/claude"  # doctor's `claude --version` can't start anything


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def set_cfg(tp: TempProject, old: str, new: str) -> None:
    """Edit the TempProject's jevmem.toml in place (it already has [llm] and [index] tables)."""
    t = tp.cfg_path.read_text(encoding="utf-8")
    assert old in t, old
    tp.cfg_path.write_text(t.replace(old, new, 1), encoding="utf-8")


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess(["claude"], rc, stdout=out, stderr=err)


def write_chat(cfg, recs) -> None:
    p = Path(cfg["capture"]["path"])
    fsutil.ensure_dir(p.parent)
    with open(p, "a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def units(cfg, sql):
    with closing(sqlite3.connect(cfg["data"]["episodic_db"])) as con:
        return con.execute(sql).fetchall()


# =========================================================================== #
# S1 — claude -p always uses the subscription login
# =========================================================================== #
class TestSubscriptionOnly(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)

    def tearDown(self):
        self.tp.cleanup()

    def test_api_key_variables_are_removed_unless_opted_in(self):
        seen = []

        def fake_run(argv, **kw):
            seen.append(kw["env"])
            return cp(0, '{"topics": ["ok"]}')

        env = {"ANTHROPIC_API_KEY": "sk-test-not-a-real-key", "ANTHROPIC_AUTH_TOKEN": "tok-test", "JEVMEM_T_OTHER": "x"}
        with mock.patch.dict(os.environ, env), mock.patch.object(llm, "run_child", side_effect=fake_run), \
                mock.patch.object(llm.shutil, "which", return_value="/fake/bin/claude"):
            llm.claude_backend(self.tp.cfg()).complete("P")
            llm.claude_backend(self.tp.cfg({"llm": {"claude_use_api_key": True}})).complete("P")
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-test-not-a-real-key", "parent env untouched")
        child, opted = seen
        for k in llm.API_KEY_ENVS:
            self.assertNotIn(k, child)
        self.assertEqual((child["JEVMEM_T_OTHER"], child[llm.DISABLE_ENV]), ("x", "1"))
        self.assertEqual(opted["ANTHROPIC_API_KEY"], "sk-test-not-a-real-key")
        self.assertIs(config.DEFAULTS["llm"]["claude_use_api_key"], False)

    def test_doctor_reports_an_ignored_key_and_an_open_breaker(self):
        set_cfg(self.tp, 'backend = "none"', f'backend = "claude"\nclaude_bin = "{NO_CLAUDE}"')
        cfg = self.tp.cfg()
        llm.breaker(cfg).failure("usage limit")
        r = run_cli("doctor", cfg_path=self.tp.cfg_path, env_extra={"ANTHROPIC_API_KEY": "sk-test-not-a-real-key"})
        self.assertIn("info claude -p: $ANTHROPIC_API_KEY is set but ignored", r.stdout)
        self.assertIn("uses the login of your claude CLI", r.stdout)
        self.assertIn("warn claude -p: paused until", r.stdout)
        self.assertIn("(usage limit)", r.stdout)
        self.assertNotIn("sk-test-not-a-real-key", r.stdout + r.stderr)
        r = run_cli("doctor", cfg_path=self.tp.cfg_path)
        self.assertNotIn("is set but ignored", r.stdout)


# =========================================================================== #
# S3 — outages pause claude -p instead of burning the chunks' tries
# =========================================================================== #
class TestCallBreaker(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()
        self.cfg = self.tp.cfg({"index": {"labeler": "haiku", "batch_size": 5, "max_tries": 2}})
        self.clock = FakeClock(1_800_000_000.0)
        self.brk = llm.CallBreaker(self.cfg["data"]["llm_breaker"], 30, 240, clock=self.clock)
        self.claude = llm.ClaudeBackend(breaker=self.brk)
        self.calls = []
        self.answer = cp(1, err="Claude AI usage limit reached|1800003600")
        self.out = []

    def tearDown(self):
        self.tp.cleanup()

    def fake_run(self, prompt, timeout):
        self.calls.append(prompt)
        a = self.answer
        return a(prompt) if callable(a) else a

    def index(self):
        return indexer.run(self.cfg, labeler_backend=self.claude, jev_available=False, out=self.out.append)

    def test_backoff_schedule(self):
        self.assertEqual([self.brk.pause_minutes(n) for n in range(1, 7)], [30, 60, 120, 240, 240, 240])

    def test_five_usage_limit_errors_change_no_tries_then_labeling_resumes(self):
        with mock.patch.object(self.claude, "_run", side_effect=self.fake_run):
            for n in range(1, 6):
                res = self.index()
                self.assertEqual((res["labeled"], res["failed"], res["gave_up"]), (0, 0, 0), n)
                self.assertIn("usage limit", res["haiku_paused"])
                self.assertEqual(len(self.calls), n, "one call, then the run stops")
                again = self.index()  # still paused: no call at all
                self.assertEqual(len(self.calls), n)
                self.assertIn("paused until", again["haiku_paused"])
                st = self.brk.state()
                self.assertEqual(st["failures"], n)
                self.assertAlmostEqual(st["next_try"] - self.clock(), self.brk.pause_minutes(n) * 60)
                self.clock.advance(self.brk.pause_minutes(n) * 60 + 1)
            self.assertEqual(units(self.cfg, "select max(coalesce(tries,0)), max(coalesce(hfail,0)) from units"),
                             [(0, 0)], "outages never count against a chunk")
            self.assertEqual(indexer.stats(self.cfg)["gave_up"], 0)
            self.assertTrue(any("not counted as failed attempts" in str(o) for o in self.out))
            self.assertEqual(mode(self.cfg["data"]["llm_breaker"]), 0o600)
            self.answer = lambda prompt: cp(0, FakeHaiku().complete(prompt))
            res = self.index()
        self.assertEqual(res["labeled"], res["units"], "labeling resumes after the outage")
        self.assertNotIn("haiku_paused", res)
        self.assertFalse(Path(self.cfg["data"]["llm_breaker"]).exists(), "a success resets the breaker")

    def test_unusable_answers_still_count_and_give_up(self):
        self.answer = cp(0, "Sorry, I can't label these right now.")
        with mock.patch.object(self.claude, "_run", side_effect=self.fake_run):
            self.index()
            res = self.index()
        self.assertEqual(res["gave_up"], res["units"], "a bad answer is the chunk's attempt (max_tries 2)")
        self.assertFalse(Path(self.cfg["data"]["llm_breaker"]).exists(), "an answer is not an outage")

    def test_what_counts_as_a_failed_call(self):
        b = self.claude
        for answer, reason in ((cp(1, err="Invalid API key · Please run /login"), "login"),
                               (cp(0, "Claude AI usage limit reached|1800003600"), "usage limit"),
                               (cp(1, err="API Error: Connection error."), "network"),
                               (cp(1, err="API Error: 529 Overloaded"), "overloaded"),
                               (cp(2), "exit 2")):
            self.brk.success()
            with mock.patch.object(b, "_run", return_value=answer):
                with self.assertRaises(llm.LLMUnavailable) as cm:
                    b.call("P")
            self.assertIn(f"({reason})", str(cm.exception))
        for exc, reason in ((subprocess.TimeoutExpired("claude", 1), "timeout"), (FileNotFoundError(2, "x"), "FileNotFoundError")):
            self.brk.success()
            with mock.patch.object(b, "_run", side_effect=exc):
                self.assertIsNone(b.complete("P"), "complete() hides the outage")
            self.assertEqual(self.brk.state()["reason"], reason)
        self.brk.success()
        long_json = json.dumps({"topics": ["The user hit the usage limit of the export API; rate limit is 10/s."]})
        with mock.patch.object(b, "_run", return_value=cp(0, long_json)):
            self.assertEqual(b.call("P"), long_json, "a JSON answer that mentions limits is an answer")

    def test_digest_skips_a_paused_claude_and_falls_back(self):
        cfg = self.tp.cfg({"digest": {"index_after": False}})
        self.brk.failure("usage limit")
        write_chat(cfg, [{"ts": "2026-09-10T09:20:00", "dir": "in", "user": "sam", "text": "please fix the backup"}])
        fallback = FakeLLM('{"topics": ["The user asked to fix the backup."]}')
        with mock.patch.object(self.claude, "_run", side_effect=self.fake_run):
            res = digest.run(cfg, backend=llm.ChainBackend([self.claude, fallback]),
                             now=datetime(2026, 9, 10, 9, 34), out=self.out.append)
        self.assertEqual(self.calls, [], "no claude -p call while paused")
        self.assertEqual((res["result"], len(fallback.prompts)), ("ok", 1))
        write_chat(cfg, [{"ts": "2026-09-10T09:40:00", "dir": "in", "user": "sam", "text": "and the restore test?"}])
        with mock.patch.object(self.claude, "_run", side_effect=self.fake_run):
            res = digest.run(cfg, backend=self.claude, now=datetime(2026, 9, 10, 10, 4), out=self.out.append)
        self.assertEqual(res["result"], "raw", "raw summary fallback is kept")
        self.assertTrue(any("claude -p paused until" in str(o) for o in self.out))


# =========================================================================== #
# R1 — <channel> parsing is linear and capped
# =========================================================================== #
class TestChannelParsing(unittest.TestCase):
    BIG = '<channel source="x" chat_id="1">backup ' * 20000

    def test_same_blocks_as_the_old_regex(self):
        old = re.compile(r"<channel[^>]*>(.*?)</channel>", re.DOTALL)
        cases = ['a <channel source="s" chat_id="1">one</channel> b <channel>two</channel>',
                 '<channel x="1"><channel y="2">nested</channel> tail</channel>',
                 '<channel a="b">no close', "<channel", "", "plain text",
                 '<channel s="1">x</channel><channel s="2"></channel><channel s="3">\n multi\nline </channel>']
        for s in cases:
            self.assertEqual([b for _, b in text.channel_blocks(s)], old.findall(s), s)
        self.assertEqual(text.channel_blocks('<channel source="a" id="1">x</channel>'), [('source="a" id="1"', "x")])

    def test_many_opening_tags_finish_fast_in_hook_and_capture(self):
        tp = TempProject()
        try:
            cfg = tp.cfg()
            indexer.run(cfg, client_factory=lambda: FakeJev(labeler=keyword_labeler(DEFAULT_RULES)),
                        out=lambda *_: None)
            for name, fn in (("message_text", lambda: text.message_text(self.BIG)),
                             ("extract_query", lambda: text.extract_query(self.BIG)),
                             ("capture", lambda: capture.inbound(cfg, {"prompt": self.BIG})),
                             ("hook", lambda: hook.run(cfg, {"prompt": self.BIG}, client=FakeJev(),
                                                       clock=FakeClock(), today=date(2026, 9, 10)))):
                t0 = time.monotonic()
                fn()
                self.assertLess(time.monotonic() - t0, 1.0, name)
        finally:
            tp.cleanup()

    def test_only_the_first_256kb_are_searched(self):
        tp = TempProject(logs=False)
        try:
            cfg = tp.cfg()
            late = "x" * text.MAX_PARSE_CHARS + tg("arrives too late", "2")
            self.assertEqual(capture.inbound(cfg, {"prompt": tg("first", "1") + late}), 1)
            self.assertEqual([r["text"] for r in capture.tail(cfg)], ["first"])
        finally:
            tp.cleanup()


# =========================================================================== #
# R2 — the Jev call has a hard deadline; R7 — 402 means status 402 only
# =========================================================================== #
class _JevServer(BaseHTTPRequestHandler):
    mode = "trickle"

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            if self.mode == "trickle":  # headers at once, then one byte every 0.5 s
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "200")
                self.end_headers()
                for _ in range(200):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.5)
                return
            if self.mode == "502":
                body, code = b"<html>502 Bad Gateway. Cloudflare Ray ID: 8c4021f9 (402 tries)</html>", 502
            elif self.mode == "402":
                body, code = b'{"detail": "payment required"}', 402
            else:  # 200 with an error detail that mentions 402
                body, code = b'{"detail": "upstream said 402 once"}', 200
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class TestJevDeadlineAnd402(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _JevServer)
        cls.srv.daemon_threads = True
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}/v1/systemone"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def client(self, mode):
        _JevServer.mode = mode
        return JevClient("test-key-not-real", self.url)

    def test_trickling_response_hits_the_hard_deadline(self):
        t0 = time.monotonic()
        with self.assertRaises(JevTimeout):
            self.client("trickle").call("s", {}, timeout=1.0)
        self.assertLess(time.monotonic() - t0, 1.5)

    def test_hook_keeps_its_4_second_deadline_against_a_slow_server(self):
        tp = TempProject()
        try:
            cfg = tp.cfg({"jev": {"url": self.url}, "fallback": {"bm25_threshold": 0.0}})
            self.assertEqual(cfg["hook"]["deadline_sec"], 4.0)
            indexer.run(cfg, client_factory=lambda: FakeJev(labeler=keyword_labeler(DEFAULT_RULES)),
                        out=lambda *_: None)
            _JevServer.mode = "trickle"
            with mock.patch.dict(os.environ, {NO_KEY_ENV: "test-key-not-real"}):
                t0 = time.monotonic()
                out = hook.run(cfg, {"prompt": "why did the nightly backup fail?"}, today=date(2026, 9, 10))
                took = time.monotonic() - t0
            self.assertLess(took, 4.5, took)
            self.assertIn("FTS5 fallback", out)
            log = json.loads(Path(cfg["data"]["log"]).read_text().splitlines()[-1])
            self.assertTrue(log["error"].startswith("JevTimeout"), log["error"])
        finally:
            tp.cleanup()

    def test_only_http_402_is_a_billing_error(self):
        with self.assertRaises(JevError) as cm:
            self.client("502").call("s", {}, timeout=2)
        self.assertNotIsInstance(cm.exception, BillingError)
        self.assertFalse(is_billing_error(cm.exception))
        with self.assertRaises(JevError) as cm:
            self.client("detail").call("s", {}, timeout=2)
        self.assertNotIsInstance(cm.exception, BillingError)
        with self.assertRaises(BillingError):
            self.client("402").call("s", {}, timeout=2)
        self.assertFalse(is_billing_error(RuntimeError("Jev HTTP 502: Ray ID: 8c4021f9")))

    def test_502_with_a_ray_id_does_not_open_the_hook_breaker(self):
        tp = TempProject()
        try:
            cfg = tp.cfg()
            indexer.run(cfg, client_factory=lambda: FakeJev(labeler=keyword_labeler(DEFAULT_RULES)),
                        out=lambda *_: None)
            bad = FakeJev(error=JevError("Jev HTTP 502: <html>Cloudflare Ray ID: 8c4021f9</html>"))
            hook.run(cfg, {"prompt": "nightly backup relative path"}, client=bad, clock=FakeClock())
            self.assertFalse(Path(cfg["data"]["breaker"]).exists())
            hook.run(cfg, {"prompt": "nightly backup relative path"}, client=FakeJev(error=BillingError("Jev HTTP 402: x")),
                     clock=FakeClock())
            self.assertTrue(Path(cfg["data"]["breaker"]).exists())
        finally:
            tp.cleanup()


# =========================================================================== #
# R3 — one digest at a time; a new daily file never loses a section
# =========================================================================== #
class _SlowOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        time.sleep(2.0)
        body = json.dumps({"message": {"content": '{"topics": ["The user asked about the nightly backup."]}'}}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class TestDigestConcurrency(unittest.TestCase):
    def test_three_digests_at_once_write_one_section(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _SlowOllama)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        tp = TempProject(logs=False, extra_cfg="[digest]\nindex_after = false\n")
        try:
            set_cfg(tp, 'backend = "none"',
                    f'backend = "ollama"\nollama_url = "http://127.0.0.1:{srv.server_address[1]}"')
            cfg = tp.cfg()
            write_chat(cfg, [{"ts": datetime.now().replace(microsecond=0).isoformat(), "dir": "in", "user": "sam",
                              "text": "why did the nightly backup fail?"}])
            env = dict(os.environ, PYTHONPATH=str(SRC))
            env.pop("JEVMEM_CONFIG", None)
            procs = [subprocess.Popen([sys.executable, "-m", "jevmem", "--config", str(tp.cfg_path), "digest"],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                     for _ in range(3)]
            outs = [p.communicate(timeout=60) for p in procs]
            self.assertEqual([p.returncode for p in procs], [0, 0, 0], outs)
            logs = list((tp.root / "memory").glob("*.md"))
            self.assertEqual(len(logs), 1)
            body = logs[0].read_text(encoding="utf-8")
            self.assertEqual(len(re.findall(r"^## \d\d:\d\d summary", body, re.M)), 1, body)
            self.assertEqual(body.count("daily log\n"), 1)
            self.assertTrue(body.startswith("# "), "header first")
            self.assertGreaterEqual(sum("another digest is running" in o for o, _ in outs), 1, outs)
        finally:
            tp.cleanup()
            srv.shutdown()
            srv.server_close()

    def test_new_file_header_never_overwrites_an_earlier_append(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "2026-09-10.md"
            real = os.fdopen

            def racing_fdopen(fd, *a, **kw):
                with open(p, "a", encoding="utf-8") as other:  # another writer appends first
                    other.write("## 09:04 summary\n- from the other writer\n")
                return real(fd, *a, **kw)

            with mock.patch.object(fsutil.os, "fdopen", side_effect=racing_fdopen):
                self.assertTrue(fsutil.create_new(p, "# 2026-09-10 daily log\n"))
            text_ = p.read_text(encoding="utf-8")
            self.assertIn("- from the other writer\n", text_)
            self.assertIn("# 2026-09-10 daily log\n", text_)
            self.assertEqual(mode(p), 0o600)


# =========================================================================== #
# R10 — midnight: earlier days' chat goes to earlier days' files
# =========================================================================== #
class TestDigestMidnight(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        self.cfg = self.tp.cfg({"digest": {"index_after": False}})
        # look only at the conversation: the prompt's own examples mention a backup too
        self.llm = FakeLLM(lambda p: json.dumps({"topics": [
            "Late: backup question." if "backup" in p.split("<conversation>")[-1]
            else "Early: restaurant question."]}))

    def tearDown(self):
        self.tp.cleanup()

    def daily(self, day):
        return (self.tp.root / "memory" / f"{day}.md").read_text(encoding="utf-8")

    def test_records_before_midnight_go_to_the_previous_day(self):
        write_chat(self.cfg, [
            {"ts": "2026-09-09T23:40:00", "dir": "in", "user": "sam", "text": "why did the nightly backup fail?"},
            {"ts": "2026-09-09T23:41:00", "dir": "out", "user": "Assistant", "text": "relative path in cron"},
            {"ts": "2026-09-10T00:02:00", "dir": "in", "user": "sam", "text": "book the restaurant for friday"}])
        res = digest.run(self.cfg, backend=self.llm, now=datetime(2026, 9, 10, 0, 4), out=lambda *_: None)
        self.assertEqual([(s["date"], s["title"]) for s in res["sections"]],
                         [("2026-09-09", "23:41 summary"), ("2026-09-10", "00:04 summary")])
        self.assertIn("## 23:41 summary\n- Late: backup question.", self.daily("2026-09-09"))
        self.assertNotIn("restaurant", self.daily("2026-09-09"))
        self.assertIn("## 00:04 summary\n- Early: restaurant question.", self.daily("2026-09-10"))
        self.assertNotIn("backup", self.daily("2026-09-10"))
        # the raw windows line up with the new headings
        late = "\n".join(raw.lookup(self.cfg, "2026-09-09", "23:41"))
        early = "\n".join(raw.lookup(self.cfg, "2026-09-10", "00:04"))
        self.assertIn("nightly backup", late)
        self.assertNotIn("restaurant", late)
        self.assertIn("restaurant", early)
        self.assertNotIn("nightly backup", early)

    def test_assistant_only_lines_join_a_group_with_a_user_message(self):
        write_chat(self.cfg, [
            {"ts": "2026-09-09T23:58:00", "dir": "out", "user": "Assistant", "text": "good night"},
            {"ts": "2026-09-10T00:01:00", "dir": "in", "user": "sam", "text": "one more backup question"}])
        res = digest.run(self.cfg, backend=self.llm, now=datetime(2026, 9, 10, 0, 4), out=lambda *_: None)
        self.assertEqual([s["date"] for s in res["sections"]], ["2026-09-10"])
        self.assertIn("good night", self.llm.prompts[0])
        self.assertFalse((self.tp.root / "memory" / "2026-09-09.md").exists())

    def test_split_by_day_helper(self):
        recs = [{"ts": "2026-09-08T10:00:00", "user": True}, {"ts": "bad", "user": False},
                {"ts": "2099-01-01T00:00:00", "user": True}]
        self.assertEqual([d for d, _ in digest.split_by_day(recs, "2026-09-10")], ["2026-09-08", "2026-09-10"])


# =========================================================================== #
# N1 — dry-runs never call an LLM
# =========================================================================== #
class TestDryRunsNeverCallTheLLM(unittest.TestCase):
    def test_digest_dry_run(self):
        tp = TempProject(logs=False)
        try:
            cfg = tp.cfg({"digest": {"index_after": False}})
            write_chat(cfg, [{"ts": "2026-09-10T09:20:00", "dir": "in", "user": "sam", "text": "fix the backup please"}])
            fake, out = FakeLLM('{"topics": ["x"]}'), []
            res = digest.run(cfg, backend=fake, dry_run=True, now=datetime(2026, 9, 10, 9, 34), out=out.append)
            self.assertEqual(fake.prompts, [])
            self.assertEqual((res["result"], res["llm_called"], res["user_messages"]), ("dry-run", False, 1))
            self.assertIn("LLM not called", "\n".join(out))
            self.assertIn("1 new record(s) (1 user message(s)) from 2026-09-10", "\n".join(out))
            self.assertFalse((tp.root / "memory" / "2026-09-10.md").exists())
        finally:
            tp.cleanup()

    def test_cli_dry_runs_with_a_claude_backend_start_no_process(self):
        tp = TempProject()
        try:
            set_cfg(tp, 'backend = "none"', 'backend = "claude"')
            cfg = tp.cfg()
            write_chat(cfg, [{"ts": "2026-09-02T09:20:00", "dir": "in", "user": "sam", "text": "hello there friend"}])
            with mock.patch.object(llm, "run_child", side_effect=AssertionError("claude started")):
                for argv in (["digest", "--dry-run"], ["profile", "consolidate", "--date", "2026-09-02", "--dry-run"]):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        self.assertEqual(cli.main(["--config", str(tp.cfg_path)] + argv), 0, argv)
                    self.assertIn("LLM not called", buf.getvalue(), argv)
        finally:
            tp.cleanup()


# =========================================================================== #
# R4, R11, R12 — consolidation
# =========================================================================== #
LOG = """# 2026-09-02 daily log

## 10:15 summary
- User said: "keep status updates to short bullet summaries, no long paragraphs".
"""


class TestConsolidateRound2(unittest.TestCase):
    GOOD = {"kind": "preference", "statement": "Prefers short bullet summaries for status updates",
            "quote": "keep status updates to short bullet summaries", "relation": "new"}

    def setUp(self):
        self.tp = TempProject(logs=False)
        (self.tp.root / "memory" / "2026-09-02.md").write_text(LOG, encoding="utf-8")
        self.cfg = self.tp.cfg()
        self.out = []

    def tearDown(self):
        self.tp.cleanup()

    def traits(self):
        with closing(sqlite3.connect(self.cfg["data"]["profile_db"])) as con:
            return con.execute("SELECT id, statement, evidence_count FROM traits WHERE status='active'").fetchall()

    def test_two_identical_candidates_make_one_trait(self):
        cands = [self.GOOD, dict(self.GOOD, quote="no long paragraphs")]
        res = consolidate.run(self.cfg, day="2026-09-02", backend=FakeLLM(json.dumps({"traits": cands})),
                              out=self.out.append)
        rows = self.traits()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 2, "the second candidate became evidence for the first")
        self.assertEqual([d["action"] for d in res["decisions"]], ["new", "merge"])
        self.assertEqual(res["decisions"][1]["target_id"], rows[0][0])

    def test_same_quote_twice_is_still_one_trait(self):
        cands = [self.GOOD, dict(self.GOOD, statement="prefers short bullet summaries for status updates")]
        consolidate.run(self.cfg, day="2026-09-02", backend=FakeLLM(json.dumps({"traits": cands})),
                        out=self.out.append)
        self.assertEqual(len(self.traits()), 1)

    def test_decide_marks_the_twin(self):
        cands = [self.GOOD, dict(self.GOOD, quote="no long paragraphs"),
                 {"kind": "goal", "statement": "Wants a weekly export report", "quote": "short bullet summaries"}]
        d = consolidate.decide(self.cfg, cands, LOG, [])
        self.assertEqual([x["action"] for x in d], ["new", "merge", "new"])
        self.assertEqual(d[1]["same_as"], 0)
        self.assertNotIn("same_as", d[2])

    def test_empty_log_makes_no_llm_call(self):
        (self.tp.root / "memory" / "2026-09-03.md").write_text("  \n\n\t\n", encoding="utf-8")
        fake = FakeLLM('{"traits": []}')
        res = consolidate.run(self.cfg, day="2026-09-03", backend=fake, out=self.out.append)
        self.assertEqual((res["result"], fake.prompts), ("empty-log", []))
        self.assertEqual(status.read(self.cfg)["consolidate"]["result"], "empty-log")
        r = run_cli("profile", "consolidate", "--date", "2026-09-03", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unavailable_llm_is_reported_as_such(self):
        brk = llm.CallBreaker(self.cfg["data"]["llm_breaker"], clock=FakeClock(1_800_000_000.0))
        brk.failure("usage limit")
        res = consolidate.run(self.cfg, day="2026-09-02", backend=llm.ClaudeBackend(breaker=brk),
                              out=self.out.append)
        self.assertEqual(res["result"], "llm-unavailable")
        self.assertIn("--date 2026-09-02", "\n".join(self.out))

    def test_emotion_filter_false_positives_and_misses(self):
        for s in ("화가 지망생과 그림 수업을 함께 들음", "Runs stress tests before every release",
                  "Writes load-test guidelines (부하 테스트 지침 작성)", "스트레스 테스트를 매주 돌림",
                  "Prefers short bullet summaries", "Works on the export API on Fridays"):
            self.assertFalse(consolidate.is_emotional(s), s)
        for s in ("요즘 일이 답답하다고 함", "회의가 괴로워함", "주말엔 행복해 보임", "천둥을 무서워함",
                  "Feels happier after morning runs", "Finds long meetings frustrating", "회의 때 화가 남",
                  "Was stressed about the deadline", "Under stress at work", "요즘 우울하다고 함"):
            self.assertTrue(consolidate.is_emotional(s), s)


# =========================================================================== #
# R5, R14, V4 — install-hook
# =========================================================================== #
class TestInstallRound2(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.proj = Path(self._td.name)
        (self.proj / ".claude").mkdir()
        self.settings = self.proj / ".claude" / "settings.json"
        self.cfg = self.proj / "jevmem.toml"
        self.cfg.write_text("", encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def test_rewrite_keeps_file_mode(self):
        self.settings.write_text('{"model": "x"}', encoding="utf-8")
        os.chmod(self.settings, 0o600)
        res = install.install(self.proj, self.cfg, out=lambda *_: None)
        self.assertEqual(res["action"], "added")
        self.assertEqual(mode(self.settings), 0o600)
        os.chmod(self.settings, 0o640)
        install.install(self.proj, self.cfg, python="/other/python3", out=lambda *_: None)
        self.assertEqual(mode(self.settings), 0o640)

    def test_no_capture_removes_existing_capture_hooks(self):
        install.install(self.proj, self.cfg, out=lambda *_: None)
        msgs = []
        res = install.install(self.proj, self.cfg, capture_hooks=False, out=msgs.append)
        self.assertEqual(res["action"], "updated")
        self.assertTrue(any("removed 2 jevmem capture hook(s)" in m for m in msgs), msgs)
        cmds = install.jevmem_commands(self.settings)
        self.assertEqual(len(cmds), 1)
        self.assertIn("-m jevmem hook ", cmds[0])
        hooks = json.loads(self.settings.read_text())["hooks"]
        self.assertEqual(hooks.get("PostToolUse"), [], "empty groups removed")

    def test_doctor_checks_hooks_installed_with_project_elsewhere(self):
        tp = TempProject()
        home = tempfile.TemporaryDirectory()
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(["--config", str(tp.cfg_path), "install-hook", "--project", str(self.proj)]), 0)
            self.assertEqual(install.recorded_settings_paths(tp.cfg()), [str(self.settings.resolve())])
            s = json.loads(self.settings.read_text())
            for groups in s["hooks"].values():
                for g in groups:
                    for h in g["hooks"]:
                        h["command"] = h["command"].replace(shlex.quote(sys.executable), "/nonexistent/venv/bin/python")
            self.settings.write_text(json.dumps(s), encoding="utf-8")
            r = run_cli("doctor", cfg_path=tp.cfg_path, env_extra={"HOME": home.name})
            self.assertIn(f"FAIL hook interpreter ({self.settings.resolve()}): /nonexistent/venv/bin/python", r.stdout)
            self.assertNotIn("info hooks: no jevmem hooks", r.stdout)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["--config", str(tp.cfg_path), "install-hook", "--project", str(self.proj), "--uninstall"])
            self.assertEqual(install.recorded_settings_paths(tp.cfg()), [])
        finally:
            tp.cleanup()
            home.cleanup()


# =========================================================================== #
# R8, R9, R15 — capture and hook entries
# =========================================================================== #
class TestHookEntriesWithoutConfig(unittest.TestCase):
    def test_no_config_file_anywhere_means_nothing_is_done(self):
        with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as home:
            env = {"HOME": home}
            before = len(hook_error_log_text())
            payload = json.dumps({"prompt": tg("zq-private-words nightly backup", "1")})
            for args in (("hook",), ("capture", "inbound")):
                r = run_cli(*args, stdin=payload, cwd=empty, env_extra=env)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), args)
            self.assertEqual(list(Path(empty).iterdir()), [], "no .jevmem/ written into the current dir")
            log = hook_error_log_text()[before:]
            self.assertIn("hook: no recall (ConfigError: no jevmem.toml found", log)
            self.assertIn("capture inbound: not recorded (ConfigError: no jevmem.toml found", log)
            self.assertNotIn("zq-private-words", log)
            r = run_cli("index", "--stats", cwd=empty, env_extra=env)
            self.assertEqual(r.returncode, 2, "index needs a config file too (it would write into the cwd)")
            self.assertIn("no jevmem.toml found", r.stderr)
            self.assertEqual(list(Path(empty).iterdir()), [])


class TestCaptureSaveThenMark(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        self.cfg = self.tp.cfg()
        self.seen = Path(self.cfg["data"]["dir"]) / capture.SEEN_NAME

    def tearDown(self):
        self.tp.cleanup()

    def test_failed_write_is_not_marked_seen(self):
        p = {"prompt": tg("remember the dentist on friday", "31")}
        with mock.patch.object(capture, "append", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                capture.inbound(self.cfg, p)
        self.assertFalse(self.seen.exists() and "telegram:111:31" in self.seen.read_text())
        self.assertEqual(capture.inbound(self.cfg, p), 1, "the resent prompt is saved")
        self.assertEqual(capture.inbound(self.cfg, p), 0, "and then deduplicated")
        self.assertEqual(self.seen.read_text().split(), ["telegram:111:31"])

    def test_a_stuck_lock_holder_does_not_stall_the_hook(self):
        import fcntl
        lock = self.seen.with_name(capture.SEEN_NAME + ".lock")
        fsutil.ensure_dir(lock.parent)
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        before = len(hook_error_log_text())
        try:
            with mock.patch.object(capture, "SEEN_LOCK_TIMEOUT", 0.3):
                t0 = time.monotonic()
                n = capture.inbound(self.cfg, {"prompt": tg("message while another hook hangs", "40")})
                self.assertLess(time.monotonic() - t0, 1.0)
        finally:
            os.close(fd)
        self.assertEqual(n, 1, "saved without the duplicate check rather than lost")
        self.assertIn("duplicate-check lock busy", hook_error_log_text()[before:])

    def test_fsutil_locked_timeout(self):
        import fcntl
        lp = Path(self.cfg["data"]["dir"]) / "t.lock"
        fd = fsutil.try_lock(lp)
        self.assertIsNotNone(fd)
        try:
            self.assertIsNone(fsutil.try_lock(lp, timeout=0.1))
            with self.assertRaises(fsutil.LockTimeout):
                with fsutil.locked(lp, timeout=0.1):
                    pass
        finally:
            os.close(fd)
        with fsutil.locked(lp, timeout=0.1):
            pass
        self.assertTrue(fcntl)


# =========================================================================== #
# N3 — search without a key; N5 — help text; N8 — init modes; V1 — help on hook entries
# =========================================================================== #
class TestCliRound2(unittest.TestCase):
    def test_search_without_a_key_uses_fts(self):
        tp = TempProject(extra_cfg="[fallback]\nbm25_threshold = 0.0\n")
        try:
            run_cli("index", "--no-label", cfg_path=tp.cfg_path)
            r = run_cli("search", "nightly backup relative path", cfg_path=tp.cfg_path)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("no Jev API key", r.stderr)
            self.assertIn("FTS keyword fallback", r.stderr)
            self.assertIn('"fts_hits"', r.stdout)
            self.assertIn("FTS5 fallback", r.stdout)
        finally:
            tp.cleanup()

    def test_help_states_the_real_config_search_order(self):
        r = run_cli("--help")
        flat = " ".join(r.stdout.split())
        self.assertIn("Without it: $JEVMEM_CONFIG, then ./jevmem.toml, then ~/.config/jevmem/jevmem.toml", flat)
        self.assertEqual(config.SEARCH_ORDER,
                         "--config, then $JEVMEM_CONFIG, then ./jevmem.toml, then ~/.config/jevmem/jevmem.toml")

    def test_init_files_are_private(self):
        old = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as d:
                r = run_cli("init", "--dir", d)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(mode(Path(d) / "jevmem.toml"), 0o600)
                self.assertEqual(mode(Path(d) / ".jevmem" / "taxonomy.json"), 0o600)
                self.assertNotIn("price_usd_per_mtok", (Path(d) / "jevmem.toml").read_text())
        finally:
            os.umask(old)

    def test_help_on_hook_entries(self):
        for args in (("hook", "--help"), ("capture", "inbound", "-h"), ("capture", "outbound", "--help")):
            r = run_cli(*args, stdin="")
            self.assertEqual(r.returncode, 0, args)
            self.assertIn("usage: jevmem", r.stdout, args)
        # nothing piped in (a person at a terminal): help instead of waiting on stdin
        tty = mock.Mock()
        tty.isatty.return_value = True
        buf = io.StringIO()
        with mock.patch.object(sys, "stdin", tty), contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["hook"]), 0)
        self.assertIn("usage: jevmem hook", buf.getvalue())
        tty.read.assert_not_called()
        # a real hook call (JSON piped in) stays silent
        r = run_cli("hook", stdin=json.dumps({"prompt": "hi"}), cfg_path="/nonexistent/jevmem.toml")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


# =========================================================================== #
# R13 — config validation
# =========================================================================== #
class TestConfigValidation(unittest.TestCase):
    def test_values_are_checked_or_fixed(self):
        root = Path(".")
        cfg = config.from_dict({"hook": {"skip_prefixes": "<task-notification"},
                                "raw": {"skip_prefixes": "Alert:"}}, root)
        self.assertEqual(cfg["hook"]["skip_prefixes"], ["<task-notification"])
        self.assertEqual(cfg["raw"]["skip_prefixes"], ["Alert:"])
        for data, want in (({"schedule": {"consolidate_time": "4am"}}, "consolidate_time must be HH:MM"),
                           ({"schedule": {"consolidate_time": "25:00"}}, "consolidate_time must be HH:MM"),
                           ({"schedule": {"digest_minutes": [4, 75]}}, "digest_minutes"),
                           ({"logs": "memory"}, "[logs] must be a table"),
                           ({"logs": {"dir": 5}}, "[logs] dir must be a string"),
                           ({"hook": {"skip_prefixes": 3}}, "[hook] skip_prefixes must be a list")):
            with self.assertRaises(config.ConfigError) as cm:
                config.from_dict(data, root)
            self.assertIn(want, str(cm.exception), data)

    def test_cli_shows_the_error_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "jevmem.toml"
            for body, want in (('logs = "memory"\n', "[logs] must be a table"),
                               ('[schedule]\nconsolidate_time = "4:00am"\n', "consolidate_time must be HH:MM")):
                p.write_text(body, encoding="utf-8")
                r = run_cli("index", "--stats", cfg_path=p)
                self.assertEqual(r.returncode, 2, body)
                self.assertIn(want, r.stderr)
                self.assertNotIn("Traceback", r.stderr)


# =========================================================================== #
# V2 — disallowed tools as array or comma string
# =========================================================================== #
class TestDisallowedTools(unittest.TestCase):
    def test_array_and_string_forms(self):
        # tools_off=True (`--tools ""` in use): the configured list is passed exactly as configured
        b = llm.ClaudeBackend(disallowed_tools=["Bash(rm -rf *)", "WebFetch", " "])
        argv = b.argv("claude", "/tmp/empty.json", "P", tools_off=True)
        i = argv.index("--disallowedTools")
        self.assertEqual(argv[i + 1:i + 4], ["Bash(rm -rf *)", "WebFetch", "--strict-mcp-config"])
        b = llm.ClaudeBackend(disallowed_tools="Bash(rm -rf *), WebFetch,Edit")
        argv = b.argv("claude", "/tmp/empty.json", "P", tools_off=True)
        i = argv.index("--disallowedTools")
        self.assertEqual(argv[i + 1:i + 3], ["Bash(rm -rf *),WebFetch,Edit", "--strict-mcp-config"],
                         "spaces inside a pattern are kept; only commas split")
        # without `--tools ""` the read/search/agent tools are added, in the same form
        argv = b.argv("claude", "/tmp/empty.json", "P")
        i = argv.index("--disallowedTools")
        self.assertTrue(argv[i + 1].startswith("Bash(rm -rf *),WebFetch,Edit,Read,Glob,Grep"), argv)
        self.assertEqual(argv[i + 2], "--strict-mcp-config")
        b = llm.ClaudeBackend(disallowed_tools=["Bash(rm -rf *)", "WebFetch"])
        argv = b.argv("claude", "/tmp/empty.json", "P")
        i = argv.index("--disallowedTools")
        self.assertEqual(argv[i + 1:i + 5], ["Bash(rm -rf *)", "WebFetch", "Read", "Glob"])
        self.assertEqual(argv.count("WebFetch"), 1)
        self.assertEqual(llm.tool_args(""), [])
        self.assertEqual(llm.tool_args([]), [])

    def test_toml_array_reaches_the_backend(self):
        tp = TempProject(logs=False)
        try:
            set_cfg(tp, 'backend = "none"', 'backend = "none"\nclaude_disallowed_tools = ["Bash(git push:*)", "Edit"]')
            self.assertEqual(llm.claude_backend(tp.cfg()).disallowed_tools, ["Bash(git push:*)", "Edit"])
        finally:
            tp.cleanup()


# =========================================================================== #
# V3 — logs never keep raw exception text
# =========================================================================== #
class TestSafeErrorLogs(unittest.TestCase):
    SECRET = "zq-secret-chat-words"

    def test_safe_reason(self):
        self.assertEqual(fsutil.safe_reason(ValueError(f"bad {self.SECRET}")), "ValueError: <message hidden, 24 chars>")
        self.assertEqual(fsutil.safe_reason(JevError(f"Jev HTTP 422: echo {self.SECRET}")), "JevError: Jev HTTP 422")
        self.assertEqual(fsutil.safe_reason(config.ConfigError("config file not found: /x")),
                         "ConfigError: config file not found: /x")
        self.assertTrue(fsutil.safe_reason(OSError(28, "No space left")).startswith("OSError: errno 28"))
        self.assertEqual(fsutil.safe_reason(KeyError()), "KeyError")

    def test_hook_logs_and_error_log_hide_messages(self):
        tp = TempProject()
        try:
            cfg = tp.cfg()
            indexer.run(cfg, client_factory=lambda: FakeJev(labeler=keyword_labeler(DEFAULT_RULES)),
                        out=lambda *_: None)
            hook.run(cfg, {"prompt": "nightly backup relative path"}, client=FakeJev(error=ValueError(self.SECRET)),
                     clock=FakeClock())
            hook.run(cfg, {"prompt": "nightly backup relative path"},
                     client=FakeJev(error=JevError(f"Jev HTTP 422: {self.SECRET}")), clock=FakeClock())
            lines = [json.loads(x) for x in Path(cfg["data"]["log"]).read_text().splitlines()]
            self.assertEqual([x["error"] for x in lines[-2:]],
                             [f"ValueError: <message hidden, {len(self.SECRET)} chars>", "JevError: Jev HTTP 422"])
            before = len(hook_error_log_text())

            def bad_loader():
                raise TypeError(f"unsupported {self.SECRET}")

            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"prompt": "x"}))):
                hook.main(bad_loader)
            with mock.patch.object(cli, "cmd_hook", side_effect=RuntimeError(self.SECRET)), \
                    mock.patch.object(sys, "stdin", io.StringIO("{}")):
                self.assertEqual(cli.main(["hook", "--config", str(tp.cfg_path)]), 0)
            log = hook_error_log_text()[before:]
            self.assertIn("hook: no recall (TypeError: <message hidden", log)
            self.assertIn("hook: RuntimeError: <message hidden", log)
            self.assertNotIn(self.SECRET, log)
        finally:
            tp.cleanup()


# =========================================================================== #
# N4 — doctor
# =========================================================================== #
class TestDoctorRound2(unittest.TestCase):
    def test_unlabeled_index_warns_and_login_is_not_claimed(self):
        tp = TempProject()
        try:
            set_cfg(tp, 'labeler = "jev"', f'labeler = "haiku"')
            set_cfg(tp, 'backend = "none"', f'backend = "none"\nclaude_bin = "{NO_CLAUDE}"')
            run_cli("index", "--no-label", cfg_path=tp.cfg_path)
            r = run_cli("doctor", cfg_path=tp.cfg_path)
            self.assertRegex(r.stdout, r"warn episodic index: \d+ chunks, 0 labeled — Jev recall needs labeled chunks")
            self.assertNotIn("ok   episodic index", r.stdout)
            self.assertIn("FAIL labeler: haiku (", r.stdout)
            self.assertIn("login status not checked", r.stdout)
        finally:
            tp.cleanup()


# =========================================================================== #
# V5 — the episodic DB wait shrinks with the time left (remaining − 0.5 s)
# =========================================================================== #
class TestHookDbWaitShrinks(unittest.TestCase):
    def run_with_latency(self, latency):
        tp = TempProject()
        try:
            cfg = tp.cfg()
            indexer.run(cfg, client_factory=lambda: FakeJev(labeler=keyword_labeler(DEFAULT_RULES)),
                        out=lambda *_: None)
            seen, real = [], store.connect

            def spy(path, readonly=False, timeout=5.0):
                seen.append(round(timeout, 6))
                return real(path, readonly=readonly, timeout=timeout)

            clock = FakeClock()
            fake = FakeJev(route={"coding/bugs-debugging": 0.8}, pick=lambda c: {}, latency=latency, clock=clock)
            with mock.patch.object(store, "connect", side_effect=spy):
                hook.run(cfg, {"prompt": "nightly backup relative path"}, client=fake, clock=clock,
                         today=date(2026, 9, 10))
            return seen, cfg["hook"]["deadline_sec"]
        finally:
            tp.cleanup()

    def test_wait_is_remaining_minus_the_fallback_reserve(self):
        seen, deadline = self.run_with_latency(2.0)
        self.assertEqual(deadline, 4.0)
        self.assertEqual(seen[:2], [recall.HOOK_DB_TIMEOUT, 1.15],
                         "before routing: the 3 s cap; after a 2 s route call: 4 - 2 - (0.5 + 0.3 + 0.05)")

    def test_wait_never_drops_below_a_twentieth_of_a_second(self):
        seen, _ = self.run_with_latency(3.6)
        self.assertEqual(seen[:2], [recall.HOOK_DB_TIMEOUT, 0.05], "4 - 3.6 - 0.85 < 0.05 -> 0.05")


if __name__ == "__main__":
    unittest.main()

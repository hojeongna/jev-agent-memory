"""Pre-release hardening: config/taxonomy errors, hook entries that never block, private file
modes, capture details, several <channel> blocks in one prompt, Ollama backend, API key file,
emotion filter, doctor output, crontab quoting. Fakes and local servers only: no Jev API, no
`claude`, no real crontab or launchctl."""
import contextlib
import io
import json
import os
import re
import shlex
import stat
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from helpers import (DEAD_URL, DEFAULT_RULES, FakeClock, FakeJev, JevError, TempProject,
                     hook_error_log_text, keyword_labeler, run_cli)

from jevmem import (__version__, capture, cli, config, consolidate, digest, fsutil, hook, indexer, llm,
                    profile, recall, schedule, status, store)
from jevmem.jev_client import JevClient, load_api_key
from jevmem.text import extract_query, message_blocks, message_text


def tg(text, mid, chat="111", user="someone", ts="2026-09-10T00:05:00Z"):
    return (f'<channel source="plugin:telegram:telegram" chat_id="{chat}" message_id="{mid}" '
            f'user="{user}" ts="{ts}">{text}</channel>')


class FakeLLM(llm.Backend):
    name = "fake"

    def __init__(self, reply):
        self.reply = reply

    def complete(self, prompt):
        return self.reply


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


# --------------------------------------------------------------------------- #
class TestConfigErrors(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        self.missing = self.tp.root / "moved-away" / "jevmem.toml"
        self._empty = tempfile.TemporaryDirectory()
        self.empty = Path(self._empty.name)

    def tearDown(self):
        self.tp.cleanup()
        self._empty.cleanup()

    def test_load_raises_for_missing_explicit_or_env_path(self):
        with self.assertRaises(config.ConfigError) as cm:
            config.load(str(self.missing))
        self.assertIn("config file not found", str(cm.exception))
        self.assertIn("--config", str(cm.exception))
        with mock.patch.dict(os.environ, {config.ENV_CONFIG: str(self.missing)}):
            with self.assertRaises(config.ConfigError) as cm:
                config.load()
            self.assertIn("$JEVMEM_CONFIG", str(cm.exception))
        self.assertIsNone(config.defaults(self.empty).source)

    def test_cli_exits_2_and_writes_nothing(self):
        for args in (("index", "--stats"), ("profile", "list"), ("digest", "--dry-run"), ("search", "--fts", "x")):
            r = run_cli(*args, cfg_path=self.missing, cwd=self.empty)
            self.assertEqual(r.returncode, 2, (args, r.stdout, r.stderr))
            self.assertIn("config file not found", r.stderr)
            self.assertNotIn("Traceback", r.stderr)
        r = run_cli("profile", "list", env_extra={config.ENV_CONFIG: str(self.missing)}, cwd=self.empty)
        self.assertEqual(r.returncode, 2)
        self.assertIn("$JEVMEM_CONFIG", r.stderr)
        self.assertEqual(list(self.empty.iterdir()), [], "no .jevmem/ created in the current directory")

    def test_invalid_toml_is_a_readable_error(self):
        bad = self.tp.root / "bad.toml"
        bad.write_text("[logs\ndir = 1\n", encoding="utf-8")
        r = run_cli("index", "--stats", cfg_path=bad)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not valid TOML", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_install_and_schedule_name_the_missing_file(self):
        for args in (("install-hook", "--dry-run"), ("schedule", "status")):
            r = run_cli(*args, cfg_path=self.missing, cwd=self.empty)
            self.assertEqual(r.returncode, 2)
            self.assertIn(str(self.missing), r.stderr)

    def test_hook_entries_exit_0_silently_and_log_the_reason(self):
        before = len(hook_error_log_text())
        secret_text = "nightly backup relative path zq-unique-words"
        r = run_cli("hook", cfg_path=self.missing, stdin=json.dumps({"prompt": secret_text}), cwd=self.empty)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        r = run_cli("capture", "inbound", cfg_path=self.missing, cwd=self.empty,
                    stdin=json.dumps({"prompt": tg(secret_text, "1")}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        log = hook_error_log_text()[before:]
        self.assertIn("hook: no recall (ConfigError: config file not found", log)
        self.assertIn("capture inbound: not recorded (ConfigError", log)
        self.assertNotIn("zq-unique-words", log, "chat text never goes to the error log")
        self.assertEqual(list(self.empty.iterdir()), [], "nothing written into the current directory")

    def test_doctor_shows_fail_and_keeps_going(self):
        r = run_cli("doctor", cfg_path=self.missing, cwd=self.empty)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        first = r.stdout.splitlines()[0]
        self.assertTrue(first.startswith("FAIL config: config file not found"), first)
        self.assertIn("info last digest:", r.stdout, "the remaining checks still run")


class TestHookEntryArguments(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)

    def tearDown(self):
        self.tp.cleanup()

    def test_detects_hook_entries(self):
        self.assertEqual(cli._hook_entry(["hook", "--config", "x"]), "hook")
        self.assertEqual(cli._hook_entry(["--config", "hook", "hook"]), "hook")
        self.assertEqual(cli._hook_entry(["capture", "inbound", "--config", "x"]), "capture inbound")
        self.assertEqual(cli._hook_entry(["capture", "outbound"]), "capture outbound")
        for argv in (["capture", "log-out", "hook"], ["search", "hook"], ["capture", "tail"], [], ["--version"]):
            self.assertIsNone(cli._hook_entry(argv), argv)

    def test_bad_arguments_never_exit_2_on_hook_entries(self):
        before = len(hook_error_log_text())
        for args in (("hook", "--no-such-flag"), ("capture", "inbound", "--nope"), ("capture", "outbound", "x"),
                     ("hook", "--config")):
            r = run_cli(*args, cfg_path=self.tp.cfg_path, stdin=json.dumps({"prompt": "hello there friend"}))
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), args)
        log = hook_error_log_text()[before:]
        self.assertIn("hook: bad arguments, nothing done", log)
        self.assertIn("capture inbound: bad arguments", log)
        # ordinary commands keep argparse's usual exit code 2
        self.assertEqual(run_cli("index", "--no-such-flag", cfg_path=self.tp.cfg_path).returncode, 2)

    def test_hook_main_logs_loader_failure(self):
        before = len(hook_error_log_text())

        def boom():
            raise config.ConfigError("config file not found: /x/jevmem.toml (given by --config)")

        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"prompt": "some message here"}))):
            self.assertEqual(hook.main(boom), 0)
        self.assertIn("hook: no recall (ConfigError", hook_error_log_text()[before:])

    def test_error_log_stays_small(self):
        with mock.patch.object(fsutil, "HOOK_ERROR_LOG_MAX_BYTES", 2000):
            for i in range(200):
                fsutil.hook_error_log(f"line {i} " + "x" * 40)
        self.assertLess(fsutil.hook_error_log_path().stat().st_size, 4000)
        self.assertIn("line 199", hook_error_log_text())


# --------------------------------------------------------------------------- #
class TestTaxonomyErrors(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()
        (self.tp.root / ".jevmem" / "taxonomy.json").write_text('{"labeling": {', encoding="utf-8")

    def tearDown(self):
        self.tp.cleanup()

    def test_index_search_and_doctor_report_bad_json_without_traceback(self):
        for args in (("index",), ("index", "--dry-run"), ("search", "nightly backup job")):
            r = run_cli(*args, cfg_path=self.tp.cfg_path)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("is not valid JSON", r.stderr)
            self.assertIn("line 1 column", r.stderr)
            self.assertNotIn("Traceback", r.stderr)
        r = run_cli("doctor", cfg_path=self.tp.cfg_path)
        self.assertIn("FAIL taxonomy: taxonomy", r.stdout)
        self.assertIn("is not valid JSON", r.stdout)

    def test_init_with_bad_taxonomy(self):
        target = self.tp.root / "fresh"
        r = run_cli("init", "--dir", str(target), "--taxonomy", str(self.tp.root / ".jevmem" / "taxonomy.json"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("is not valid JSON", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


# --------------------------------------------------------------------------- #
class TestProfileCli(unittest.TestCase):
    QUOTE = "User prefers short bullet summaries over long paragraphs"

    def setUp(self):
        self.tp = TempProject()
        r = run_cli("profile", "new", "--kind", "preference", "--statement", "Prefers short bullet summaries",
                    "--source", "memory/2026-09-02.md", "--quote", self.QUOTE, "--commit",
                    cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "", "quote is in the source file: no warning")

    def tearDown(self):
        self.tp.cleanup()

    def test_config_accepted_before_or_after_the_subcommand(self):
        cfg = str(self.tp.cfg_path)
        for argv in (["profile", "list", "--config", cfg], ["profile", "--config", cfg, "list"],
                     ["--config", cfg, "profile", "list"], ["profile", "show", "1", "--config", cfg],
                     ["profile", "stats", "--config", cfg]):
            r = run_cli(*argv)
            self.assertEqual(r.returncode, 0, (argv, r.stderr))
            self.assertIn("Prefers short bullet summaries" if argv[1] != "stats" else "traits", r.stdout, argv)

    def test_db_accepted_before_or_after_the_subcommand(self):
        other = self.tp.root / "other.sqlite"
        for argv in (["profile", "list", "--db", str(other)], ["profile", "--db", str(other), "list"]):
            r = run_cli(*argv, cfg_path=self.tp.cfg_path)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("Prefers short", r.stdout, argv)

    def test_manual_write_warns_when_quote_is_not_in_the_source(self):
        r = run_cli("profile", "merge", "1", "--source", "memory/2026-09-02.md",
                    "--quote", "a sentence that is not in that log", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, "warning only, never a refusal")
        self.assertIn("warning: --quote not found verbatim", r.stderr)
        r = run_cli("profile", "merge", "1", "--source", "memory/2099-01-01.md",
                    "--quote", "no file to check against", cfg_path=self.tp.cfg_path)
        self.assertEqual((r.returncode, r.stderr), (0, ""), "missing source file: nothing to check")

    def test_quote_check_helper(self):
        self.assertTrue(profile.quote_in_text('"short   bullet\nsummaries"', "prefers short bullet summaries."))
        self.assertFalse(profile.quote_in_text("", "anything"))
        self.assertTrue(consolidate.quote_in_log("10k row limit", "pagination, 10k  row limit per file"))


class TestConsolidateCli(unittest.TestCase):
    def test_exception_is_reported_and_recorded(self):
        tp = TempProject()
        try:
            err = io.StringIO()
            with mock.patch.object(consolidate, "run", side_effect=RuntimeError("llm exploded")), \
                    contextlib.redirect_stderr(err):
                rc = cli.main(["--config", str(tp.cfg_path), "profile", "consolidate", "--date", "2026-09-02"])
                rc_dry = cli.main(["--config", str(tp.cfg_path), "profile", "consolidate", "--dry-run"])
            self.assertEqual((rc, rc_dry), (1, 1))
            # stderr is not a terminal here (scheduled jobs log it): class name only, no message
            self.assertIn("error: RuntimeError: <message hidden", err.getvalue())
            self.assertNotIn("llm exploded", err.getvalue())
            rec = status.read(tp.cfg())["consolidate"]
            self.assertEqual(rec["result"], "error")
            self.assertIn("RuntimeError", rec["detail"])
            self.assertNotIn("llm exploded", rec["detail"], "status.json never keeps raw exception text")
        finally:
            tp.cleanup()


# --------------------------------------------------------------------------- #
class TestCaptureDetails(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        self.cfg = self.tp.cfg()

    def tearDown(self):
        self.tp.cleanup()

    def out(self, resp, tool="mcp__plugin_telegram_telegram__reply", ti=None):
        payload = {"tool_name": tool, "tool_input": ti if ti is not None else {"chat_id": "111", "text": "hi"},
                   "tool_response": resp}
        return capture.parse_outbound(self.cfg, payload)

    def test_message_id_never_taken_from_chat_id(self):
        cases = [
            ('{"chat_id": "111", "message_id": 853}', "853"),
            ({"chat_id": 111, "message_id": 853}, "853"),
            ({"ok": True, "result": {"chat": {"id": 111}, "message_id": 77}}, "77"),
            ({"messageId": "42", "chatId": "9"}, "42"),
            ("sent (id: 853)", "853"),
            ("sent 2 parts (ids: 853, 854)", "853"),
            ({"id": 5}, "5"),
            ({"chat_id": 111}, ""),
            ('{"user_id": 1, "thread_id": 2}', ""),
            ("", ""),
        ]
        for resp, want in cases:
            self.assertEqual(self.out(resp)["message_id"], want, resp)
            self.assertEqual(self.out(resp)["chat_id"], "111")

    def test_parse_outbound_edge_cases(self):
        self.assertIsNone(self.out("id: 1", tool="Bash"))
        self.assertIsNone(self.out("id: 1", ti={"chat_id": "1", "text": "   "}))
        self.assertIsNone(self.out("id: 1", ti="not a dict"))
        self.assertIsNone(capture.parse_outbound(self.cfg, ["not", "a", "dict"]))
        rec = self.out("id: 3")
        self.assertEqual((rec["dir"], rec["channel"], rec["user"], rec["text"]), ("out", "telegram", "Assistant", "hi"))
        self.assertEqual(capture.inbound(self.cfg, ["not a dict"]), 0)
        self.assertEqual(capture.inbound(self.cfg, {"prompt": 42}), 0)

    def test_duplicate_check_is_atomic_across_concurrent_hooks(self):
        start = threading.Barrier(8)
        results = []

        def worker():
            start.wait()
            results.append(capture.inbound(self.cfg, {"prompt": tg("same message from two hooks", "10")}))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [0] * 7 + [1])
        self.assertEqual(len(capture.tail(self.cfg)), 1)
        seen = (Path(self.cfg["data"]["dir"]) / capture.SEEN_NAME).read_text(encoding="utf-8").split()
        self.assertEqual(seen, ["telegram:111:10"])


class TestSeveralChannelBlocks(unittest.TestCase):
    PROMPT = (tg("Why did the nightly backup fail?", "21") + "\n"
              + tg("also, what did we decide in the 1:1 meeting?", "22"))

    def test_text_helpers(self):
        self.assertEqual(message_blocks(self.PROMPT), ["Why did the nightly backup fail?",
                                                       "also, what did we decide in the 1:1 meeting?"])
        self.assertEqual(message_text(self.PROMPT),
                         "Why did the nightly backup fail?\nalso, what did we decide in the 1:1 meeting?")
        self.assertTrue(extract_query(self.PROMPT).startswith("also, what did we decide"),
                        "keyword query: newest message first")
        self.assertEqual(message_text(tg("only one", "1")), "only one")
        self.assertEqual(message_text('<channel source="x"></channel>'), "")
        self.assertEqual(message_text("plain prompt"), "plain prompt")

    def test_hook_recalls_all_blocks_and_does_not_repeat_them_as_context(self):
        tp = TempProject()
        try:
            cfg = tp.cfg()
            indexer.run(cfg, client_factory=lambda: FakeJev(labeler=keyword_labeler(DEFAULT_RULES)),
                        out=lambda *_: None)
            capture.inbound(cfg, {"prompt": tg("earlier message about the laptop", "20")})
            capture.inbound(cfg, {"prompt": self.PROMPT})  # the capture hook ran first
            fake = FakeJev(route={"coding/bugs-debugging": 0.8},
                           pick=lambda crit: {k: 0.6 for k, v in crit.items() if "backup" in v.lower()})
            hook.run(cfg, {"prompt": self.PROMPT}, client=fake, clock=FakeClock(), today=date(2026, 9, 10))
            state = fake.calls[0]["state"]
            recent, current = state.split("[Current message]")
            self.assertIn("nightly backup", current)
            self.assertIn("1:1 meeting", current)
            self.assertIn("earlier message about the laptop", recent)
            self.assertNotIn("nightly backup", recent)
            self.assertNotIn("1:1 meeting", recent)
            self.assertEqual(len(capture.tail(cfg)), 3, "capture keeps one record per block")
        finally:
            tp.cleanup()


class TestHookDbWait(unittest.TestCase):
    def test_episodic_db_wait_is_shorter_than_the_deadline(self):
        tp = TempProject()
        try:
            cfg = tp.cfg()
            indexer.run(cfg, client_factory=lambda: FakeJev(labeler=keyword_labeler(DEFAULT_RULES)),
                        out=lambda *_: None)
            seen, real = [], store.connect

            def spy(path, readonly=False, timeout=5.0):
                seen.append(timeout)
                return real(path, readonly=readonly, timeout=timeout)

            fake = FakeJev(route={"coding/bugs-debugging": 0.8}, pick=lambda crit: {})
            with mock.patch.object(store, "connect", side_effect=spy):
                hook.run(cfg, {"prompt": "nightly backup relative path"}, client=fake, clock=FakeClock(),
                         today=date(2026, 9, 10))
            self.assertGreaterEqual(len(seen), 2)
            self.assertTrue(all(t <= recall.HOOK_DB_TIMEOUT < cfg["hook"]["deadline_sec"] for t in seen), seen)
        finally:
            tp.cleanup()


# --------------------------------------------------------------------------- #
class TestPrivateFiles(unittest.TestCase):
    def setUp(self):
        self.old_umask = os.umask(0o022)
        self.tp = TempProject(logs=False)
        self.cfg = self.tp.cfg({"data": {"dir": "private-data"}, "logs": {"dir": "new-logs"},
                                "digest": {"index_after": False}})
        self.data = Path(self.cfg["data"]["dir"])

    def tearDown(self):
        os.umask(self.old_umask)
        self.tp.cleanup()

    def test_new_files_0600_and_new_dirs_0700(self):
        capture.inbound(self.cfg, {"prompt": tg("please remember the dentist on Friday", "1")})
        hook._write_log(self.cfg, {"ts": "x"})
        res = digest.run(self.cfg, backend=FakeLLM('{"topics": ["Dentist on Friday."]}'),
                         now=datetime(2026, 9, 10, 9, 34), out=lambda *_: None)
        self.assertEqual(res["result"], "ok")
        (self.data / "taxonomy.json").write_bytes((self.tp.root / ".jevmem" / "taxonomy.json").read_bytes())
        indexer.run(self.cfg, no_label=True, out=lambda *_: None)  # episodic DB + index.lock
        profile.connect(Path(self.cfg["data"]["profile_db"])).close()
        self.assertEqual(mode(self.data / "index.lock"), 0o600)
        self.assertEqual(mode(self.data), 0o700)
        self.assertEqual(mode(self.tp.root / "new-logs"), 0o700)
        for p in (self.data / "chat.jsonl", self.data / capture.SEEN_NAME, self.data / "inject_log.jsonl",
                  self.data / "digest_state.json", self.data / "status.json",
                  self.tp.root / "new-logs" / "2026-09-10.md",
                  Path(self.cfg["data"]["episodic_db"]), Path(self.cfg["data"]["profile_db"])):
            self.assertEqual(mode(p), 0o600, p)
        self.assertEqual([p.name for p in self.data.iterdir() if ".tmp" in p.name], [], "no temp files left")

    def test_existing_files_and_dirs_keep_their_modes(self):
        self.data.mkdir(mode=0o755)
        os.chmod(self.data, 0o755)
        chat = self.data / "chat.jsonl"
        chat.write_text("", encoding="utf-8")
        os.chmod(chat, 0o644)
        st = self.data / "status.json"
        st.write_text("{}", encoding="utf-8")
        os.chmod(st, 0o640)
        capture.inbound(self.cfg, {"prompt": tg("hello again, a longer message", "2")})
        status.record(self.cfg, "digest", "ok")
        self.assertEqual((mode(self.data), mode(chat), mode(st)), (0o755, 0o644, 0o640))
        self.assertEqual(len(capture.tail(self.cfg)), 1)
        self.assertEqual(status.read(self.cfg)["digest"]["result"], "ok")


# --------------------------------------------------------------------------- #
class TestScheduleQuoting(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name) / "my agent 100%"
        self.dir.mkdir()
        self.cfg_path = self.dir / "jevmem.toml"
        self.cfg_path.write_text("", encoding="utf-8")
        self.cfg = config.load(str(self.cfg_path))

    def tearDown(self):
        self._td.cleanup()

    def test_percent_and_spaces_in_paths(self):
        block = schedule.cron_block(self.cfg, self.cfg_path, "/opt/py 3/bin/python3")
        lines = block.splitlines()
        self.assertEqual(len(lines), 4, block)
        self.assertEqual(lines[0], f"# >>> jevmem {schedule._hash(self.cfg_path)} "
                                   f"({shlex.quote(str(self.cfg_path.resolve()))}) >>>")
        self.assertFalse(any(ln.startswith("PATH=") for ln in lines),
                         "no crontab environment line: it would apply to every entry below the block")
        for job_line in lines[1:3]:
            self.assertIsNone(re.search(r"(?<!\\)%", job_line), job_line)
            self.assertIn("100\\%", job_line)
            self.assertIn("'/opt/py 3/bin/python3'", job_line)
            # what the shell receives after cron turns "\%" into "%"
            words = shlex.split(job_line.split(" ", 5)[5].replace("\\%", "%"))
            self.assertIn(str(self.cfg_path.resolve()), words)
            path_word = words[words.index("&&") + 1]
            self.assertTrue(path_word.startswith("PATH="), "PATH is set for this command only")
            self.assertEqual(words[words.index("&&") + 2], "/opt/py 3/bin/python3")
        text = schedule.merge_crontab("0 * * * * /usr/bin/true\n", block)
        self.assertTrue(all(schedule.installed(self.cfg, self.cfg_path, "linux", crontab_text=text).values()))
        self.assertEqual(schedule.remove_block(text, self.cfg_path), "0 * * * * /usr/bin/true\n")

    def test_line_breaks_are_refused(self):
        with self.assertRaises(ValueError):
            schedule.cron_block(self.cfg, self.dir / "a\nb" / "jevmem.toml", "/usr/bin/python3")
        with self.assertRaises(ValueError):
            schedule.cron_block(self.cfg, self.cfg_path, "/usr/bin/python3\n* * * * * evil")

    def test_install_prints_the_pinned_interpreter(self):
        out = []
        store_ = {"text": ""}
        schedule.install(self.cfg, self.cfg_path, platform="linux", write=True, python="/venv/bin/python3",
                         crontab_io=(lambda: store_["text"], lambda t: store_.update(text=t)), out=out.append)
        self.assertTrue(any("/venv/bin/python3" in o and "schedule install --write" in o for o in out), out)
        self.assertEqual(mode(Path(self.cfg["data"]["dir"]) / "logs"), 0o700)


# --------------------------------------------------------------------------- #
class _OllamaHandler(BaseHTTPRequestHandler):
    mode = "ok"
    bodies = []

    def log_message(self, *a):
        pass

    def _send(self, obj, raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        _OllamaHandler.bodies.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
        if self.mode == "slow":
            time.sleep(1.0)
        if self.mode == "garbage":
            return self._send(None, raw=b"<html>not json</html>")
        self._send({"message": {"role": "assistant", "content": '  {"topics": ["x"]}  '}})

    def do_GET(self):
        self._send({"models": [{"name": "gemma3:latest"}, {"name": "qwen3:4b"}]})


class TestOllamaBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _OllamaHandler)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        _OllamaHandler.mode = "ok"
        _OllamaHandler.bodies = []

    def test_complete_sends_a_json_chat_request(self):
        b = llm.OllamaBackend(self.url + "/", "gemma3", timeout=5)
        self.assertEqual(b.complete("PROMPT"), '{"topics": ["x"]}')
        body = _OllamaHandler.bodies[-1]
        self.assertEqual((body["model"], body["format"], body["stream"]), ("gemma3", "json", False))
        self.assertEqual(body["messages"], [{"role": "user", "content": "PROMPT"}])

    def test_timeout_and_bad_answers_return_none(self):
        _OllamaHandler.mode = "slow"
        t0 = time.monotonic()
        self.assertIsNone(llm.OllamaBackend(self.url, "gemma3", timeout=0.2).complete("p"))
        self.assertLess(time.monotonic() - t0, 0.9)
        _OllamaHandler.mode = "garbage"
        self.assertIsNone(llm.OllamaBackend(self.url, "gemma3", timeout=5).complete("p"))
        self.assertIsNone(llm.OllamaBackend(DEAD_URL, "gemma3", timeout=1).complete("p"))

    def test_check_model_pulled(self):
        self.assertTrue(llm.OllamaBackend(self.url, "gemma3").check()[0])
        self.assertTrue(llm.OllamaBackend(self.url, "qwen3:4b").check()[0])
        ok, msg = llm.OllamaBackend(self.url, "llama3").check()
        self.assertFalse(ok)
        self.assertIn("ollama pull llama3", msg)
        ok, msg = llm.OllamaBackend(DEAD_URL, "gemma3").check()
        self.assertFalse(ok)
        self.assertIn("not reachable", msg)

    def test_config_sets_url_model_and_timeout_and_fallback_chain(self):
        tp = TempProject(logs=False)
        try:
            cfg = tp.cfg({"llm": {"backend": "claude", "fallback": "ollama", "ollama_url": self.url,
                                  "ollama_model": "qwen3:4b", "timeout_sec": 7}})
            chain = llm.get_backend(cfg)
            o = chain.backends[1]
            self.assertEqual((o.name, o.url, o.model, o.timeout), ("ollama", self.url, "qwen3:4b", 7.0))
            chain.backends[0] = FakeLLM(None)  # primary (claude) fails -> ollama answers
            self.assertEqual(chain.complete("p"), '{"topics": ["x"]}')
            self.assertEqual(chain.last, "ollama")
        finally:
            tp.cleanup()


# --------------------------------------------------------------------------- #
class TestApiKey(unittest.TestCase):
    ENV = "JEVMEM_TEST_KEY_VAR_FOR_KEYFILE_TESTS"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        os.environ.pop(self.ENV, None)

    def tearDown(self):
        os.environ.pop(self.ENV, None)
        self._td.cleanup()

    def cfg(self, key_file="key"):
        return config.from_dict({"jev": {"api_key_env": self.ENV, "api_key_file": key_file}}, self.root)

    def test_key_file_relative_to_config_and_stripped(self):
        (self.root / "key").write_text("  file-key-123\n", encoding="utf-8")
        self.assertEqual(load_api_key(self.cfg()), "file-key-123")
        self.assertEqual(JevClient.from_config(self.cfg()).model, "jev-latest")

    def test_env_var_wins_over_file(self):
        (self.root / "key").write_text("file-key", encoding="utf-8")
        with mock.patch.dict(os.environ, {self.ENV: " env-key "}):
            self.assertEqual(load_api_key(self.cfg()), "env-key")

    def test_missing_or_empty_file_is_an_error_without_the_key(self):
        with self.assertRaises(JevError) as cm:
            load_api_key(self.cfg("does-not-exist"))
        self.assertIn(self.ENV, str(cm.exception))
        (self.root / "empty").write_text("  \n", encoding="utf-8")
        with self.assertRaises(JevError):
            load_api_key(self.cfg("empty"))
        with self.assertRaises(JevError):
            load_api_key(config.from_dict({"jev": {"api_key_env": self.ENV}}, self.root))

    def test_user_agent_uses_package_version(self):
        self.assertEqual(JevClient.USER_AGENT, f"jev-agent-memory/{__version__}")


class TestEmotionFilter(unittest.TestCase):
    def test_moods_are_detected(self):
        for s in ("Was stressed about the deadline", "Feels anxious before demos", "Gets tired after long calls",
                  "Mood swings on Mondays", "요즘 우울하다고 함", "스트레스를 많이 받음", "회의 때마다 불안해함"):
            self.assertTrue(consolidate.is_emotional(s), s)

    def test_durable_traits_pass(self):
        for s in ("Prefers short bullet summaries", "Works on the export API on Fridays",
                  "Wants weekly reports on Thursday", "짧은 불릿 요약을 선호", "주간 보고를 목요일에 받기를 원함",
                  "Uses a standing desk"):
            self.assertFalse(consolidate.is_emotional(s), s)

    def test_decide_rejects_moods_even_with_a_valid_quote(self):
        cfg = config.from_dict({}, Path("."))
        log = "- User said they were stressed about the launch all week."
        d = consolidate.decide(cfg, [{"kind": "pattern", "statement": "Is stressed about launches",
                                      "quote": "stressed about the launch"}], log, [])
        self.assertEqual(d[0]["action"], "reject")
        self.assertIn("emotion", d[0]["reason"])


# --------------------------------------------------------------------------- #
class TestDoctor(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()

    def tearDown(self):
        self.tp.cleanup()

    def test_report_lines_and_exit_code(self):
        r = run_cli("doctor", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 1, "no key, no index, no LLM -> FAIL lines")
        self.assertNotIn("Traceback", r.stderr)
        out = r.stdout
        self.assertIn(f"ok   config: {self.tp.cfg_path}", out)
        self.assertIn("ok   logs: ", out)
        self.assertIn("ok   taxonomy: ", out)
        self.assertIn("FAIL api key: No Jev API key", out)
        self.assertIn("FAIL episodic index: 0 chunks", out)
        self.assertIn("FAIL llm backend: none", out)
        self.assertIn("info labeler: jev", out)
        self.assertIn("info hooks: no jevmem hooks", out)
        for job in ("digest", "index", "consolidate"):
            self.assertIn(f"info last {job}: never", out)

    def test_hook_interpreter_and_given_up_chunks(self):
        (self.tp.root / ".claude").mkdir()
        cmds = [f"{shlex.quote(sys.executable)} -m jevmem hook --config x",
                "/nonexistent/venv/bin/python -m jevmem capture inbound --config x"]
        (self.tp.root / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": c} for c in cmds]}]}}), encoding="utf-8")
        cfg = self.tp.cfg({"index": {"max_tries": 1}})
        indexer.run(cfg, client_factory=lambda: FakeJev(error=JevError("boom")), out=lambda *_: None)
        with open(self.tp.cfg_path, "a", encoding="utf-8") as f:
            f.write("max_tries = 1\n")  # appended to the [index] table the helper wrote last
        fsutil.hook_error_log("hook: no recall (ConfigError: example)")
        out = run_cli("doctor", cfg_path=self.tp.cfg_path).stdout
        self.assertIn(f"ok   hook interpreter (.claude/settings.json): {sys.executable}", out)
        self.assertIn("FAIL hook interpreter (.claude/settings.json): /nonexistent/venv/bin/python  (missing", out)
        self.assertRegex(out, r"warn labeling: \d+ chunk\(s\) given up after 1 failed attempts")
        self.assertIn("warn hook error log", out)
        self.assertIn("ConfigError: example", out)


if __name__ == "__main__":
    unittest.main()

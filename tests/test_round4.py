"""Pre-release round 4: the digest never re-creates a logs folder that existed (and indexing
holds when most indexed files vanish at once), resuming a lost digest state for records
without a time, digest/index without a config file, copy-pasteable docs, chat capture while
the config has an error, damaged databases, and smaller details (UTF-8 children, process
groups, redirects, breaker bounds, the hook's time budget, whole-number floats, cut-off chat
lines, settings backups, doctor wording). Fakes and local servers only: no Jev API, no real
`claude` (a fake executable or a mocked call), no Ollama, no crontab or launchctl."""
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from helpers import (DEAD_URL, DEFAULT_RULES, REPO, SRC, FakeJev, TempProject, hook_error_log_text,
                     keyword_labeler, run_cli)

from jevmem import capture, config, digest, hook, indexer, install, jev_client, llm, profile, schedule, status
from jevmem.jev_client import JevError
from test_stages import FakeLLM, tg

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib


def write_chat(path, recs) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def n_units(cfg) -> int:
    con = sqlite3.connect(cfg["data"]["episodic_db"])
    try:
        return con.execute("select count(*) from units").fetchone()[0]
    finally:
        con.close()


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


# =========================================================================== #
# 1 — the digest never re-creates a logs folder that existed
# =========================================================================== #
class TestDigestKeepsTheLogsFolder(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()
        self.cfg = self.tp.cfg({"digest": {"index_after": True}})
        self.jev = FakeJev(labeler=keyword_labeler(DEFAULT_RULES))
        self.first = indexer.run(self.cfg, client_factory=lambda: self.jev, out=lambda *_: None)
        self.logs = self.tp.root / "memory"
        self.away = self.tp.root / "memory.away"

    def tearDown(self):
        self.tp.cleanup()

    def chat(self, text, ts="2026-10-29T10:20:00"):
        write_chat(self.cfg["capture"]["path"], [{"ts": ts, "dir": "in", "text": text}])

    def test_a_briefly_missing_folder_holds_the_run_and_keeps_every_label(self):
        self.logs.rename(self.away)
        self.chat("a new chat message")
        fake = FakeLLM('{"topics": ["The user sent a new chat message."]}')
        out = []
        res = digest.run(self.cfg, backend=fake, now=datetime(2026, 10, 29, 10, 34), out=out.append,
                         client_factory=lambda: self.jev)
        self.assertEqual(res["result"], "logs-missing")
        self.assertFalse(self.logs.exists(), "the folder is not re-created")
        self.assertEqual(fake.prompts, [], "no LLM call while held")
        self.assertEqual(n_units(self.cfg), self.first["units"], "no label deleted")
        self.assertTrue(any("bring it back" in o and "index holds" in o for o in out), out)
        self.assertEqual(status.read(self.cfg)["digest"]["result"], "logs-missing")
        dry = digest.run(self.cfg, dry_run=True, now=datetime(2026, 10, 29, 10, 40), out=out.append)
        self.assertEqual(dry["result"], "logs-missing")
        # the folder is back: the held message is summarized now, nothing was lost
        self.away.rename(self.logs)
        res = digest.run(self.cfg, backend=fake, now=datetime(2026, 10, 29, 11, 4), out=lambda *_: None,
                         client_factory=lambda: self.jev)
        self.assertEqual(res["result"], "ok")
        self.assertIn("a new chat message", fake.prompts[0])
        self.assertGreater(n_units(self.cfg), self.first["units"])
        self.assertIn("## 11:04 summary", (self.logs / "2026-10-29.md").read_text(encoding="utf-8"))

    def test_the_first_run_creates_the_folder_then_remembers_it(self):
        tp = TempProject(logs=False)
        try:
            shutil.rmtree(tp.root / "memory")
            cfg = tp.cfg({"digest": {"index_after": False}})
            write_chat(cfg["capture"]["path"], [{"ts": "2026-10-29T10:20:00", "dir": "in", "text": "hello"}])
            res = digest.run(cfg, backend=FakeLLM('{"topics": ["t"]}'), now=datetime(2026, 10, 29, 10, 34),
                             out=lambda *_: None)
            self.assertEqual(res["result"], "ok")
            self.assertEqual(mode(tp.root / "memory"), 0o700)
            self.assertTrue(digest.load_state(cfg)["logs_seen"])
            # no index at all (index_after = false), but the folder existed: never re-created
            shutil.rmtree(tp.root / "memory")
            write_chat(cfg["capture"]["path"], [{"ts": "2026-10-29T11:00:00", "dir": "in", "text": "again"}])
            out = []
            res = digest.run(cfg, backend=FakeLLM('{"topics": ["t"]}'), now=datetime(2026, 10, 29, 11, 4),
                             out=out.append)
            self.assertEqual(res["result"], "logs-missing")
            self.assertIn("it existed before", out[-1])
            self.assertFalse((tp.root / "memory").exists())
        finally:
            tp.cleanup()

    def test_append_never_creates_the_folder_when_told_not_to(self):
        shutil.rmtree(self.logs)
        with self.assertRaises(FileNotFoundError):
            digest.append_daily(self.cfg, "\n## 10:00 summary\n- x\n", "2026-10-29", create_dir=False)
        self.assertFalse(self.logs.exists())


# =========================================================================== #
# 1b — indexing holds when most indexed files vanish at once
# =========================================================================== #
class TestIndexHoldsWhenMostFilesVanish(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()
        self.cfg = self.tp.cfg()
        self.jev = FakeJev(labeler=keyword_labeler(DEFAULT_RULES))
        self.first = indexer.run(self.cfg, client_factory=lambda: self.jev, out=lambda *_: None)
        self.files = sorted((self.tp.root / "memory").glob("*.md"))

    def tearDown(self):
        self.tp.cleanup()

    def run_index(self, **kw):
        return indexer.run(self.cfg, client_factory=lambda: self.jev, out=lambda *_: None, **kw)

    def test_more_than_half_gone_is_held_unless_forced(self):
        self.assertEqual(len(self.files), 5)
        for p in self.files[:3]:
            p.unlink()
        res = self.run_index()
        self.assertIn("3 of the 5 indexed log files are gone", res["logs_held"])
        self.assertIn("nothing deleted", res["logs_held"])
        self.assertEqual(n_units(self.cfg), self.first["units"])
        self.assertEqual(indexer.status_word(res), "logs-missing")
        dry = indexer.run(self.cfg, dry_run=True, out=lambda *_: None)
        self.assertEqual(dry["removed"], 0)
        self.assertIn("3 of the 5", dry["logs_held"])
        r = run_cli("index", "--force-prune", "--no-label", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        con = sqlite3.connect(self.cfg["data"]["episodic_db"])
        try:
            left = {row[0] for row in con.execute("select distinct relpath from units")}
        finally:
            con.close()
        self.assertEqual(left, {p.name for p in self.files[3:]})

    def test_one_file_gone_is_pruned_as_usual(self):
        self.files[0].unlink()
        res = self.run_index()
        self.assertNotIn("logs_held", res)
        self.assertGreater(res["removed"], 0)

    def test_a_folder_back_with_only_one_new_file_is_held(self):
        logs = self.tp.root / "memory"
        shutil.rmtree(logs)
        logs.mkdir()
        (logs / "2026-10-29.md").write_text("# 2026-10-29 daily log\n\n## 10:34 summary\n"
                                            "- The user restarted the nightly backup job by hand.\n")
        res = self.run_index()
        self.assertIn("5 of the 5 indexed log files are gone", res["logs_held"])
        self.assertEqual(n_units(self.cfg), self.first["units"])


# =========================================================================== #
# 2 — a lost digest state resumes without re-summarizing, also without timestamps
# =========================================================================== #
class TestResumeWithoutTimestamps(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        (self.root / "memory").mkdir()
        self.chat = self.root / "chat.jsonl"
        self.cfg = config.from_dict({"logs": {"dir": "memory"}, "data": {"dir": "d"},
                                     "llm": {"backend": "none", "fallback": "none"},
                                     "digest": {"source": "jsonl", "jsonl_path": str(self.chat),
                                                "index_after": False}}, self.root)
        self.llm = FakeLLM(lambda p: json.dumps({"topics": [f"summary call {len(self.llm.prompts)}"]}))
        self.add([f"day1 message {i}" for i in range(3)])
        self.assertEqual(self.run_at(27)["result"], "ok")
        self.add([f"day2 message {i}" for i in range(3)])
        self.assertEqual(self.run_at(28)["result"], "ok")
        self.state = Path(self.cfg["data"]["dir"]) / digest.STATE_NAME
        self.backup = Path(self.cfg["data"]["dir"]) / digest.BACKUP_NAME

    def tearDown(self):
        self.td.cleanup()

    def add(self, texts, ts=None):
        write_chat(self.chat, [dict({"role": "user", "text": t}, **({"ts": ts} if ts else {})) for t in texts])

    def run_at(self, day, out=None, hour=10):
        return digest.run(self.cfg, backend=self.llm, now=datetime(2026, 10, day, hour, 4),
                          out=out or (lambda *_: None))

    def sections(self):
        return sum(p.read_text(encoding="utf-8").count("\n## ") for p in (self.root / "memory").glob("*.md"))

    def test_the_backup_copy_is_written_with_every_run(self):
        self.assertEqual(json.loads(self.backup.read_text()), json.loads(self.state.read_text()))
        self.assertEqual(mode(self.backup), 0o600)

    def test_a_damaged_or_deleted_state_resumes_from_the_backup_copy(self):
        self.state.write_text("")
        self.add(["day3 new message"])
        out = []
        res = self.run_at(29, out.append)
        self.assertEqual((res["result"], res["records"]), ("ok", 1), out)
        self.assertIn("day3 new message", self.llm.prompts[-1])
        self.assertNotIn("day1", self.llm.prompts[-1])
        self.assertTrue(any(digest.BACKUP_NAME in o for o in out), out)
        self.assertFalse(any("not summarized again" in o for o in out), "no time-based claim on this path")
        self.assertEqual(len(list(self.state.parent.glob(digest.STATE_NAME + ".bak-*"))), 1)
        self.state.unlink()
        self.add(["day3 second message"])
        res = self.run_at(29, hour=11)
        self.assertEqual((res["result"], res["records"]), ("ok", 1))
        self.assertEqual(self.sections(), 4, "one section per run, no duplicate")

    def test_without_any_copy_untimed_records_are_skipped_and_counted(self):
        self.state.write_text("")
        self.backup.write_text("{")
        self.add(["day3 new message"])
        before = len(self.llm.prompts)
        out = []
        res = self.run_at(29, out.append)
        self.assertEqual(res["result"], "skip")
        self.assertEqual(len(self.llm.prompts), before, "the 6 old records are not summarized again")
        warn = [o for o in out if "no readable time" in o]
        self.assertEqual(len(warn), 1, out)
        self.assertIn("7 record(s)", warn[0])
        self.assertIn("skipped (not summarized)", warn[0])
        self.assertEqual(self.sections(), 2)
        self.add(["day4 message"])
        res = self.run_at(30)
        self.assertEqual((res["result"], res["records"]), ("ok", 1), "reading goes on from there")

    def test_mixed_times_resume_at_the_first_new_timed_record(self):
        self.state.unlink()
        self.backup.unlink()
        self.add(["timed new message"], ts="2026-10-29T09:00:00")
        self.add(["untimed message after it"])
        out = []
        res = self.run_at(29, out.append)
        self.assertEqual((res["result"], res["records"]), ("ok", 2), out)
        self.assertIn("timed new message", self.llm.prompts[-1])
        self.assertIn("untimed message after it", self.llm.prompts[-1])
        self.assertNotIn("day2", self.llm.prompts[-1])
        self.assertTrue(any("6 record(s)" in o and "no readable time" in o for o in out), out)


# =========================================================================== #
# 3 — digest and index need a config file, like every other command but init/doctor
# =========================================================================== #
class TestDigestAndIndexNeedAConfig(unittest.TestCase):
    def test_exit_2_and_nothing_written(self):
        with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as home:
            for args in (("digest",), ("digest", "--dry-run"), ("index",), ("index", "--stats"),
                         ("index", "--dry-run")):
                r = run_cli(*args, cwd=empty, env_extra={"HOME": home})
                self.assertEqual(r.returncode, 2, (args, r.stdout, r.stderr))
                self.assertIn("no jevmem.toml found (searched --config", r.stderr, args)
                self.assertNotIn("taxonomy", r.stderr, args)
            self.assertEqual(list(Path(empty).iterdir()), [], "no .jevmem/ in the current dir")


# =========================================================================== #
# 4 — every config value shown in the docs can be pasted into jevmem.toml
# =========================================================================== #
INLINE_RE = re.compile(r"`((?:\[[A-Za-z_.]+\]\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^`]+)`")


def toml_of_snippet(snippet: str) -> dict:
    m = re.match(r"\[([A-Za-z_.]+)\]\s+(.*)", snippet, re.S)
    return tomllib.loads(f"[{m.group(1)}]\n{m.group(2)}\n" if m else snippet + "\n")


def table_defaults(name: str) -> list[tuple[str, str, str]]:
    """(section, key, code) for every config-reference row whose default is one code span."""
    rows, cur = [], None
    for line in (REPO / "docs" / name).read_text(encoding="utf-8").splitlines():
        if line.startswith("## `["):
            cur = line.split("`[", 1)[1].split("]", 1)[0]
        elif cur and line.startswith("| `"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            m = re.fullmatch(r"`([^`]*)`", cells[1])
            if m:
                rows.append((cur, cells[0].strip("`"), m.group(1)))
    return rows


class TestDocsCopyPaste(unittest.TestCase):
    DOCS = ["README.md", "README.ko.md"] + [f"docs/{p.name}" for p in sorted((REPO / "docs").glob("*.md"))]

    def test_the_checker_catches_a_double_quoted_regex(self):
        with self.assertRaises(tomllib.TOMLDecodeError):
            toml_of_snippet(r'[index] one_chunk_per_bullet_heading = "^#{1,4}\s*\d{1,2}:\d{2}\s+summary\b"')

    def test_every_table_default_is_valid_toml_equal_to_the_code(self):
        for name in ("config-reference.md", "config-reference.ko.md"):
            rows = table_defaults(name)
            self.assertGreater(len(rows), 80, name)
            for section, key, code in rows:
                if code.startswith("<"):
                    continue  # a description such as `<dir>/episodic.sqlite`
                with self.subTest(doc=name, key=f"[{section}] {key}"):
                    value = toml_of_snippet(f"[{section}] {key} = {code}")[section][key]
                    if "\u2026" not in code:  # "…" marks a shortened text
                        self.assertEqual(value, config.DEFAULTS[section][key])
                    with tempfile.TemporaryDirectory() as td:
                        config.from_dict({section: {key: value}}, Path(td))

    def test_every_inline_setting_is_valid_toml(self):
        n = 0
        for name in self.DOCS:
            for m in INLINE_RE.finditer((REPO / name).read_text(encoding="utf-8")):
                n += 1
                with self.subTest(doc=name, snippet=m.group(1)):
                    toml_of_snippet(m.group(1))
        self.assertGreater(n, 40)


# =========================================================================== #
# 5 — a config error elsewhere doesn't stop the chat log
# =========================================================================== #
class TestCaptureDespiteAConfigError(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        self.good = self.tp.cfg_path.read_text()
        self.chat = self.tp.root / ".jevmem" / "chat.jsonl"

    def tearDown(self):
        self.tp.cleanup()

    def set_cfg(self, extra, base=None):
        self.tp.cfg_path.write_text((self.good if base is None else base) + "\n" + extra)

    def capture(self, text, mid):
        return run_cli("capture", "inbound", cfg_path=self.tp.cfg_path, stdin=json.dumps({"prompt": tg(text, mid)}))

    def texts(self, path=None):
        p = path or self.chat
        return [json.loads(x)["text"] for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_a_wrong_value_elsewhere(self):
        self.set_cfg('[recall]\nmax_picks = "four"\n')
        before = len(hook_error_log_text())
        r = self.capture("still recorded after a typo", "1")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.texts(), ["still recorded after a typo"])
        log = hook_error_log_text()[before:]
        self.assertIn("recorded using only [data] and [capture]", log)
        self.assertIn("max_picks must be a number", log)
        self.assertNotIn("still recorded", log, "chat text never goes to the error log")
        r = run_cli("hook", cfg_path=self.tp.cfg_path, stdin=json.dumps({"prompt": "nightly backup relative path"}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), "recall is off meanwhile")
        r = run_cli("doctor", cfg_path=self.tp.cfg_path)
        self.assertIn("FAIL config: [recall] max_picks must be a number", r.stdout)
        self.assertIn("chat capture goes on using only [data] and [capture]", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_toml_syntax_error_elsewhere(self):
        # the trap of the old docs: a regex in double quotes
        self.set_cfg('[capture]\npath = "logs/chat.jsonl"\n\n'
                     '[raw]\nheading_re = "^#{1,4}\\s*(\\d{1,2}:\\d{2})\\s+summary\\b"\n')
        self.assertEqual(self.capture("recorded despite the bad regex", "2").returncode, 0)
        self.assertEqual(self.texts(self.tp.root / "logs" / "chat.jsonl"), ["recorded despite the bad regex"],
                         "[capture] path was read from the broken file")
        cfg = config.load_for_capture(str(self.tp.cfg_path))
        self.assertIn("is not valid TOML", cfg.partial_error)
        self.assertEqual(Path(cfg["data"]["dir"]).resolve(), (self.tp.root / ".jevmem").resolve())
        out = run_cli("capture", "outbound", cfg_path=self.tp.cfg_path, stdin=json.dumps(
            {"tool_name": "mcp__plugin_telegram_telegram__reply", "tool_input": {"chat_id": "1", "text": "a reply"},
             "tool_response": "sent (id: 9)"}))
        self.assertEqual(out.returncode, 0)
        self.assertEqual(self.texts(self.tp.root / "logs" / "chat.jsonl")[-1], "a reply")

    def test_nothing_is_recorded_when_where_to_write_is_unclear(self):
        cases = {"an error in [capture]": '[capture]\nsources = 3\n',
                 "a syntax error in [data]": '[data]\ndir = "x\n',
                 "data keys before the first table": None}
        for why, extra in cases.items():
            with self.subTest(why=why):
                if extra is None:
                    self.tp.cfg_path.write_text('data.dir = "elsewhere"\n' + self.good + '\n[raw]\nheading_re = "\\d"\n')
                else:
                    self.set_cfg(extra)
                before = len(hook_error_log_text())
                self.assertEqual(self.capture("must not be guessed", "3").returncode, 0)
                self.assertEqual(self.texts(), [])
                self.assertFalse((self.tp.root / "elsewhere").exists())
                self.assertIn("capture inbound: not recorded (ConfigError", hook_error_log_text()[before:])
        r = run_cli("doctor", cfg_path=self.tp.cfg_path)
        self.assertIn("FAIL hooks: until the config is fixed, the hooks record no chat", r.stdout)


# =========================================================================== #
# 6 — a damaged database is a readable error, never a traceback
# =========================================================================== #
class TestDamagedDatabases(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()
        self.cfg = self.tp.cfg()
        indexer.run(self.cfg, no_label=True, out=lambda *_: None)
        self.edb = Path(self.cfg["data"]["episodic_db"])
        self.pdb = Path(self.cfg["data"]["profile_db"])

    def tearDown(self):
        self.tp.cleanup()

    def test_episodic_db_garbage(self):
        self.edb.write_bytes(b"not a database at all " * 200)
        r = run_cli("doctor", cfg_path=self.tp.cfg_path)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("FAIL episodic index: episodic DB", r.stdout)
        self.assertIn("file is not a database", r.stdout)
        self.assertIn("run `jevmem index`", r.stdout)
        self.assertIn("info last digest:", r.stdout, "the remaining checks still run")
        for args in (("index",), ("index", "--stats"), ("index", "--dry-run"), ("index", "--no-label")):
            r = run_cli(*args, cfg_path=self.tp.cfg_path)
            self.assertEqual(r.returncode, 1, (args, r.stderr))
            self.assertNotIn("Traceback", r.stderr, args)
            self.assertIn("move it aside", r.stderr, args)
        r = run_cli("hook", cfg_path=self.tp.cfg_path, stdin=json.dumps({"prompt": "nightly backup relative path"}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        cfg = self.tp.cfg({"digest": {"index_after": True}})
        write_chat(cfg["capture"]["path"], [{"ts": "2026-09-10T09:20:00", "dir": "in", "text": "hello there"}])
        out = []
        res = digest.run(cfg, backend=FakeLLM('{"topics": ["t"]}'), now=datetime(2026, 9, 10, 9, 34), out=out.append)
        self.assertEqual(res["result"], "ok", "the summary is written; labeling waits")
        self.assertTrue(any(o.startswith("index: deferred") for o in out), out)
        # the documented fix: move it aside, index again
        self.edb.rename(self.edb.with_name("episodic.sqlite.broken"))
        r = run_cli("index", "--no-label", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreater(n_units(self.cfg), 0)
        self.assertNotIn("FAIL episodic index", run_cli("doctor", cfg_path=self.tp.cfg_path).stdout)

    def test_episodic_db_cut_in_half(self):
        data = self.edb.read_bytes()
        self.edb.write_bytes(data[: len(data) // 2])
        for args in (("doctor",), ("index", "--stats"), ("index", "--no-label")):
            r = run_cli(*args, cfg_path=self.tp.cfg_path)
            self.assertNotIn("Traceback", r.stderr, args)

    def test_profile_db_garbage(self):
        self.pdb.write_bytes(b"\x00garbage" * 300)
        # consolidation reads the profile only with a text LLM configured (a dry run calls none)
        self.tp.cfg_path.write_text(self.tp.cfg_path.read_text().replace(
            'backend = "none"', 'backend = "claude"\nclaude_bin = "/nonexistent/claude"'))
        for args in (("profile", "list"), ("profile", "stats"), ("profile", "show", "1"),
                     ("profile", "consolidate", "--date", "2026-09-02", "--dry-run")):
            r = run_cli(*args, cfg_path=self.tp.cfg_path)
            self.assertEqual(r.returncode, 1, (args, r.stdout, r.stderr))
            self.assertNotIn("Traceback", r.stderr, args)
            self.assertIn("restore it from a backup", r.stderr, args)
        r = run_cli("doctor", cfg_path=self.tp.cfg_path)
        self.assertIn("FAIL profile: profile DB", r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        r = run_cli("hook", cfg_path=self.tp.cfg_path, stdin=json.dumps({"prompt": "nightly backup relative path"}))
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_a_locked_db_is_not_called_damaged(self):
        from jevmem import cli, store
        reason, damaged = store.db_problem(sqlite3.OperationalError("database is locked"))
        self.assertFalse(damaged)
        self.assertIn("try again", cli.db_error_text(sqlite3.OperationalError("database is locked"), "x", "episodic"))
        self.assertTrue(store.db_problem(sqlite3.DatabaseError("database disk image is malformed"))[1])
        self.assertNotIn("SELECT", store.db_problem(sqlite3.OperationalError('near "SELECT secret": syntax'))[0],
                         "other messages are not repeated (they could quote a query)")


# =========================================================================== #
# Minor — `claude` children: UTF-8 whatever the locale, process groups, no Jev key
# =========================================================================== #
FAKE_CLAUDE_PY = """#!{python}
import json, sys
if '--help' in sys.argv:
    print('  --tools <tools...>  tools'); sys.exit(0)
if '--version' in sys.argv:
    print('0.0.0 (fake)'); sys.exit(0)
data = sys.stdin.buffer.read().decode('utf-8')
open({log!r}, 'w', encoding='utf-8').write(data)
sys.stdout.buffer.write(json.dumps({{'topics': ['요약: ' + data]}}, ensure_ascii=False).encode('utf-8'))
"""


@unittest.skipIf(os.name != "posix", "POSIX fake executables")
class TestClaudeChildren(unittest.TestCase):
    def setUp(self):
        llm._HELP_CACHE.clear()
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_prompt_and_answer_are_utf8_in_a_latin1_locale(self):
        log = self.dir / "stdin.txt"
        fake = self.dir / "claude"
        fake.write_text(FAKE_CLAUDE_PY.format(python=sys.executable, log=str(log)), encoding="utf-8")
        os.chmod(fake, 0o755)
        # the prompt as an ASCII escape: argv itself is decoded with the (Latin-1) locale
        code = ("import json, sys\nfrom jevmem import llm\n"
                f"out = llm.ClaudeBackend(binary={str(fake)!r}).complete({json.dumps('한글 요약해줘')})\n"
                "sys.stdout.write(json.dumps(out))\n")
        env = dict(os.environ, PYTHONPATH=str(SRC), LANG="en_US.ISO8859-1", LC_ALL="en_US.ISO8859-1",
                   PYTHONUTF8="0")
        env.pop("PYTHONIOENCODING", None)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(json.loads(r.stdout), json.dumps({"topics": ["요약: 한글 요약해줘"]}, ensure_ascii=False))
        self.assertEqual(log.read_text(encoding="utf-8"), "한글 요약해줘")

    def test_a_unicode_error_is_a_failed_call_not_a_crash(self):
        b = llm.ClaudeBackend(binary="claude")
        with mock.patch.object(b, "_run", side_effect=UnicodeEncodeError("ascii", "한", 0, 1, "no")):
            with self.assertRaises(llm.LLMUnavailable) as cm:
                b.call("P")
            self.assertIn("UnicodeEncodeError", str(cm.exception))
            self.assertIsNone(b.complete("P"))

    def test_a_timeout_stops_everything_the_child_started(self):
        pidfile = self.dir / "grandchild.pid"
        fake = self.dir / "claude"
        fake.write_text("#!/bin/sh\ncase \"$1\" in --help) echo '  --tools <t>'; exit 0;; esac\n"
                        f"sleep 30 &\necho $! > '{pidfile}'\nsleep 30\n")
        os.chmod(fake, 0o755)
        b = llm.ClaudeBackend(binary=str(fake), timeout=1)
        t0 = time.monotonic()
        with self.assertRaises(llm.LLMUnavailable) as cm:
            b.call("P")
        self.assertIn("timeout", str(cm.exception))
        self.assertLess(time.monotonic() - t0, 10)
        pid = int(pidfile.read_text().strip())
        gone = False
        for _ in range(60):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                gone = True
                break
            time.sleep(0.05)
        self.assertTrue(gone, "the grandchild was killed with the child's process group")

    def test_the_jev_key_variable_is_not_passed_on(self):
        tp = TempProject(logs=False)
        try:
            cfg = tp.cfg({"jev": {"api_key_env": "JEVMEM_T_KEY"}})
            with mock.patch.dict(os.environ, {"JEVMEM_T_KEY": "not-a-real-key", "JEVMEM_T_OTHER": "x"}):
                env = llm.claude_backend(cfg).child_env()
            self.assertNotIn("JEVMEM_T_KEY", env)
            self.assertEqual(env["JEVMEM_T_OTHER"], "x")
        finally:
            tp.cleanup()


# =========================================================================== #
# Minor — Jev: redirects are not followed; URLs with a user part or a backslash are refused
# =========================================================================== #
class _Server:
    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestJevRedirects(unittest.TestCase):
    def test_a_redirect_is_an_error_and_the_key_goes_nowhere(self):
        seen = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.command, self.headers.get("Authorization")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = do_GET

            def log_message(self, *a):
                pass

        target = _Server(Target)

        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{target.port}/steal")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        redirect = _Server(Redirect)
        try:
            c = jev_client.JevClient("not-a-real-key", f"http://127.0.0.1:{redirect.port}/x")
            with self.assertRaises(JevError) as cm:
                c.call("state", {}, timeout=5)
            self.assertIn("redirect, not followed", str(cm.exception))
            self.assertNotIn("not-a-real-key", str(cm.exception))
            self.assertEqual(seen, [], "the other server got nothing")
        finally:
            redirect.close()
            target.close()

    def test_url_checks(self):
        with tempfile.TemporaryDirectory() as td:
            for bad in ("http://evil.com\\@127.0.0.1/x", "http://user:pw@127.0.0.1:9/x",
                        "https://user@api.example.com/v1", "https://api.example.com/v 1",
                        "https://api.example.com/v1\n"):
                with self.subTest(url=bad), self.assertRaises(config.ConfigError):
                    config.from_dict({"jev": {"url": bad}}, Path(td))
            config.from_dict({"jev": {"url": "https://api.example.com/v1?x=1"}}, Path(td))
            for empty in ("", "   "):
                with self.subTest(claude_bin=empty), self.assertRaises(config.ConfigError) as cm:
                    config.from_dict({"llm": {"claude_bin": empty}}, Path(td))
                self.assertIn("must not be empty", str(cm.exception))


# =========================================================================== #
# Minor — the 402 breaker is bounded; the hook keeps its time budget with locked DBs
# =========================================================================== #
class TestHookBreakerAndBudget(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()
        self.cfg = self.tp.cfg()

    def tearDown(self):
        self.tp.cleanup()

    def test_breaker_values_are_bounded(self):
        path = Path(self.cfg["data"]["breaker"])
        path.parent.mkdir(parents=True, exist_ok=True)
        now = 1_800_000_000.0
        limit = now + 30 * 60
        for stored in ("inf", str(now + 5 * 365 * 86400)):
            path.write_text(stored)
            self.assertTrue(hook._breaker_open(self.cfg, now), stored)
            self.assertLessEqual(float(path.read_text()), limit, "pulled back and saved")
            self.assertFalse(hook._breaker_open(self.cfg, limit + 1))
        for stored in ("nan", "garbage", "-inf"):
            path.write_text(stored)
            self.assertFalse(hook._breaker_open(self.cfg, now), stored)
        path.write_text(str(time.time() + 600))
        self.assertRegex(hook.breaker_until(self.cfg), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        path.write_text("inf")
        r = run_cli("doctor", cfg_path=self.tp.cfg_path)
        self.assertRegex(r.stdout, r"warn jev: paused until \d{4}-\d{2}-\d{2} \d{2}:\d{2} after an HTTP 402")
        self.assertEqual(path.read_text(), "inf", "doctor only reads")

    def test_lock_waits_share_one_budget_and_end_before_the_deadline(self):
        from helpers import FakeClock
        from jevmem import store
        indexer.run(self.cfg, no_label=True, out=lambda *_: None)
        pconn = profile.connect(Path(self.cfg["data"]["profile_db"]))
        profile.init_schema(self.cfg, pconn)
        pconn.close()
        clock, waits = FakeClock(), []

        def locked(path, timeout=5.0):  # every database is locked: each open waits its full timeout
            waits.append(round(timeout, 6))
            clock.advance(timeout)
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(store, "connect_ro", side_effect=locked):
            out = hook.run(self.cfg, {"prompt": "nightly backup relative path"}, clock=clock)
        self.assertEqual(out, "")
        self.assertEqual(len(waits), 3, "profile, episodic index, FTS fallback")
        self.assertLessEqual(sum(waits), self.cfg["hook"]["deadline_sec"] - hook.LOCK_RESERVE + 1e-9, waits)

    def test_locked_databases_stay_inside_the_deadline(self):
        indexer.run(self.cfg, no_label=True, out=lambda *_: None)
        pconn = profile.connect(Path(self.cfg["data"]["profile_db"]))
        profile.init_schema(self.cfg, pconn)
        pconn.close()
        cfg = self.tp.cfg({"hook": {"deadline_sec": 2.0}})
        locks = []
        for db in (cfg["data"]["episodic_db"], cfg["data"]["profile_db"]):
            con = sqlite3.connect(db, timeout=0.1, isolation_level=None)
            con.execute("BEGIN EXCLUSIVE")
            locks.append(con)
        try:
            t0 = time.monotonic()
            out = hook.run(cfg, {"prompt": "nightly backup relative path"})
            took = time.monotonic() - t0
        finally:
            for con in locks:
                con.rollback()
                con.close()
        self.assertEqual(out, "")
        self.assertLess(took, 2.0 + 0.3, "profile, episodic and fallback waits share the 2 s budget")
        self.assertGreater(took, 1.0, "the locks were really waited for")


# =========================================================================== #
# Minor — whole numbers written as floats, a cut-off chat line, settings backups
# =========================================================================== #
class TestSmallDetails(unittest.TestCase):
    def test_whole_number_floats_become_ints(self):
        tp = TempProject()
        try:
            cfg = tp.cfg({"index": {"hard_cut": 900.0, "cap": 400.0, "max_chunk": 450.0},
                          "hook": {"breaker_minutes": 7.5}})
            self.assertEqual((type(cfg["index"]["hard_cut"]), cfg["index"]["hard_cut"]), (int, 900))
            self.assertIs(type(cfg["index"]["cap"]), int)
            self.assertEqual(cfg["hook"]["breaker_minutes"], 7.5, "fractions stay where they are allowed")
            self.assertGreater(len(indexer.collect_chunks(cfg)), 0)
            with self.assertRaises(config.ConfigError):
                tp.cfg({"index": {"hard_cut": 900.5}})
        finally:
            tp.cleanup()

    def test_a_cut_off_last_line_does_not_swallow_the_next_record(self):
        tp = TempProject(logs=False)
        try:
            cfg = tp.cfg({"digest": {"index_after": False}})
            p = Path(cfg["capture"]["path"])
            write_chat(p, [{"ts": "2026-09-10T09:00:00", "dir": "in", "text": "first"}])
            with open(p, "a", encoding="utf-8") as f:
                f.write('{"ts": "2026-09-10T09:01:00", "dir": "in", "text": "half')
            self.assertEqual(capture.inbound(cfg, {"prompt": tg("after the crash", "7",
                                                                ts="2026-09-10T00:05:00Z")}), 1)
            capture.log_out(cfg, "an alert after the crash")
            self.assertEqual([r["text"] for r in capture.tail(cfg)],
                             ["first", "after the crash", "an alert after the crash"])
            fake = FakeLLM('{"topics": ["t"]}')
            res = digest.run(cfg, backend=fake, now=datetime(2026, 9, 10, 9, 34), out=lambda *_: None)
            self.assertEqual(res["result"], "ok")
            self.assertIn("after the crash", "".join(fake.prompts))
        finally:
            tp.cleanup()

    def test_settings_backups(self):
        tp = TempProject(logs=False)
        proj = Path(tempfile.mkdtemp())
        try:
            def inst(**kw):
                return install.install(proj, tp.cfg_path, python="/usr/bin/python3", out=lambda *_: None, **kw)

            settings = proj / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text('{"env": {"SOME_TOKEN": "x"}}')
            os.chmod(settings, 0o644)
            res = inst()
            backup = Path(res["backup"])
            self.assertEqual(mode(backup), 0o600, "the copy is private from the start")
            self.assertEqual(json.loads(backup.read_text()), {"env": {"SOME_TOKEN": "x"}})
            self.assertEqual(mode(settings), 0o644, "the settings file keeps its mode")
            self.assertIsNone(inst()["backup"], "nothing changed: no write, no backup")
            self.assertEqual(len(list(settings.parent.glob("settings.json.bak-*"))), 1)
        finally:
            tp.cleanup()
            shutil.rmtree(proj)

    def test_install_hook_error_names_the_file(self):
        tp = TempProject(logs=False)
        proj = Path(tempfile.mkdtemp())
        try:
            (proj / ".claude").mkdir()
            (proj / ".claude" / "settings.json").write_text('{"hooks": [1,')
            r = run_cli("install-hook", "--project", str(proj), cfg_path=tp.cfg_path)
            self.assertEqual(r.returncode, 2)
            self.assertIn(str(proj / ".claude" / "settings.json"), r.stderr)
            self.assertIn("is not valid JSON", r.stderr)
        finally:
            tp.cleanup()
            shutil.rmtree(proj)

    def test_launchd_plists_are_private_and_a_failed_load_is_reported(self):
        tp = TempProject(logs=False)
        agents = Path(tempfile.mkdtemp())
        try:
            cfg = tp.cfg()
            fail = subprocess.CompletedProcess(["launchctl"], 0, "", "Load failed: 5: Input/output error\n")
            out = []
            with mock.patch.object(schedule.subprocess, "run", return_value=fail):
                schedule.install(cfg, tp.cfg_path, platform="macos", agents_dir=agents, write=True,
                                 python="/usr/bin/python3", out=out.append)
            plists = list(agents.glob("*.plist"))
            self.assertEqual(len(plists), 2)
            self.assertTrue(all(mode(p) == 0o600 for p in plists))
            self.assertTrue(all("launchctl load failed: Load failed: 5" in o for o in out if o.startswith("wrote")))
            ok = subprocess.CompletedProcess(["launchctl"], 0, "", "")
            out = []
            with mock.patch.object(schedule.subprocess, "run", return_value=ok):
                schedule.install(cfg, tp.cfg_path, platform="macos", agents_dir=agents, write=True,
                                 python="/usr/bin/python3", out=out.append)
            self.assertTrue(all(o.endswith("(loaded)") for o in out if o.startswith("wrote")))
        finally:
            tp.cleanup()
            shutil.rmtree(agents)

    def test_profile_stats_shows_both_caps(self):
        tp = TempProject()
        try:
            r = run_cli("profile", "stats", cfg_path=tp.cfg_path)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("left today: 3 (and at most 3 per cited date)", r.stdout)
            self.assertNotIn("budget left", r.stdout)
        finally:
            tp.cleanup()


# =========================================================================== #
# Minor — doctor wording on a fresh install, risky tool settings, old hook errors
# =========================================================================== #
class TestDoctorWording(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.env = {"HOME": str(self.root / "home"), "PATH": "/usr/bin:/bin"}
        (self.root / "home").mkdir()
        self.assertEqual(run_cli("init", "--dir", str(self.root), env_extra=self.env).returncode, 0)
        self.cfg_path = self.root / "jevmem.toml"
        t = self.cfg_path.read_text()
        for old, new in (('backend = "claude"', 'backend = "none"'), ('fallback = "ollama"', 'fallback = "none"'),
                         ('labeler = "haiku"', 'labeler = "jev"'),
                         ('url = "https://api.typesafe.ai/v1/systemone"', f'url = "{DEAD_URL}"')):
            self.assertIn(old, t)
            t = t.replace(old, new)
        self.cfg_path.write_text(t)

    def tearDown(self):
        self.td.cleanup()

    def doctor(self):
        r = run_cli("doctor", cfg_path=self.cfg_path, env_extra=self.env, cwd=self.root)
        self.assertNotIn("Traceback", r.stderr)
        return r.stdout

    def test_fresh_install(self):
        out = self.doctor()
        self.assertIn("FAIL capture log:", out)
        self.assertIn("`jevmem install-hook` adds the capture hooks", out)
        self.assertIn("info logs:", out)
        self.assertIn("the first digest summary creates it", out)
        self.assertIn("info episodic index: empty", out)
        r = run_cli("install-hook", "--project", str(self.root), cfg_path=self.cfg_path, env_extra=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.doctor()
        self.assertNotIn("FAIL capture log", out)
        self.assertIn("nothing captured yet; the capture hooks are installed", out)

    def test_tools_and_deny_list_both_off_is_a_warning(self):
        with open(self.cfg_path, "a") as f:
            f.write('\n[llm]\n' if "[llm]" not in self.cfg_path.read_text() else "")
        t = self.cfg_path.read_text().replace('backend = "none"', 'backend = "claude"\nclaude_bin = "/nonexistent/claude"'
                                              '\nclaude_tools_off = false\nclaude_disallowed_tools = ""')
        self.cfg_path.write_text(t)
        self.assertIn("warn claude -p: claude_tools_off = false and claude_disallowed_tools", self.doctor())

    def test_an_old_hook_error_is_info(self):
        state = self.root / "state"
        env = dict(self.env, XDG_STATE_HOME=str(state))
        log = state / "jevmem" / "hook-errors.log"
        log.parent.mkdir(parents=True)
        log.write_text("2020-01-01T00:00:00 hook: no recall (ConfigError: old)\n")
        out = run_cli("doctor", cfg_path=self.cfg_path, env_extra=env).stdout
        self.assertIn("info hook error log", out)
        self.assertIn("delete it to clear", out)
        log.write_text(f"{datetime.now().isoformat(timespec='seconds')} hook: no recall (ConfigError: new)\n")
        self.assertIn("warn hook error log", run_cli("doctor", cfg_path=self.cfg_path, env_extra=env).stdout)


if __name__ == "__main__":
    unittest.main()

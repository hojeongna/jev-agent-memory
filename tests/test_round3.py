"""Pre-release round 3: special characters in paths, lossless digest fallback, a missing logs
folder, a damaged digest state, evidence dates, Jev outages during labeling, call breaker
bounds; config trust and validation, `claude -p` isolation and stdin prompts, docs/CLI
details. Fakes and local servers only: no Jev API, no real `claude` (a fake executable or a
mocked subprocess), no Ollama, no crontab or launchctl."""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from helpers import (DEAD_URL, DEFAULT_RULES, EXAMPLES, NO_KEY_ENV, FakeClock, FakeJev, JevError,
                     TempProject, keyword_labeler, run_cli)

from jevmem import (config, consolidate, digest, fsutil, hook, indexer, install, jev_client, llm,
                    profile, raw, recall, store)
from jevmem.jev_client import BillingError, JevTimeout, JevUnavailable
from test_stages import FakeLLM

TODAY = date(2026, 9, 10)


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def write_chat(cfg, recs) -> None:
    p = Path(cfg["capture"]["path"])
    fsutil.ensure_dir(p.parent)
    with open(p, "a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def units(cfg, sql):
    with closing(sqlite3.connect(cfg["data"]["episodic_db"])) as con:
        return con.execute(sql).fetchall()


class NamedProject(TempProject):
    """TempProject whose project folder has a given (awkward) name inside a fresh parent."""

    def __init__(self, name: str, extra_cfg: str = ""):
        self._td = tempfile.TemporaryDirectory()
        self.parent = Path(self._td.name)
        self.root = self.parent / name
        self.root.mkdir()
        (self.root / "memory").mkdir()
        for p in sorted((EXAMPLES / "logs").glob("*.md")):
            shutil.copy(p, self.root / "memory" / p.name)
        (self.root / ".jevmem").mkdir()
        shutil.copy(EXAMPLES / "taxonomy.example.json", self.root / ".jevmem" / "taxonomy.json")
        self.cfg_path = self.root / "jevmem.toml"
        self.cfg_path.write_text(
            f'[logs]\ndir = "memory"\n\n[jev]\napi_key_env = "{NO_KEY_ENV}"\nurl = "{DEAD_URL}"\n\n'
            '[llm]\nbackend = "none"\nfallback = "none"\n\n[index]\nlabeler = "jev"\n\n' + extra_cfg,
            encoding="utf-8")


# =========================================================================== #
# P1 — read-only SQLite URIs survive #, ?, %, spaces and non-ASCII in the path
# =========================================================================== #
class TestSpecialCharacterPaths(unittest.TestCase):
    NAMES = ["proj #2", "what?", "100%25", "한글 폴더 & co"]

    def test_ro_uri_is_percent_encoded(self):
        uri = store.ro_uri("/tmp/a #b/c?d/100%/x.sqlite")
        self.assertTrue(uri.startswith("file:///"), uri)
        self.assertTrue(uri.endswith("?mode=ro"))
        self.assertEqual(uri.count("?"), 1)
        self.assertNotIn("#", uri)
        self.assertIn("%2523", store.ro_uri("/tmp/%23/x"), "a literal % is escaped too")

    def test_recall_profile_consolidate_and_doctor_work_in_awkward_folders(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                tp = NamedProject(name)
                try:
                    cfg = tp.cfg()
                    res = indexer.run(cfg, client_factory=lambda: FakeJev(labeler=keyword_labeler(DEFAULT_RULES)),
                                      out=lambda *_: None)
                    self.assertGreater(res["labeled"], 0)
                    self.assertTrue(recall.has_labeled_units(cfg))
                    self.assertGreater(indexer.stats(cfg)["labeled"], 0)
                    fake = FakeJev(route={"coding/bugs-debugging": 0.8},
                                   pick=lambda crit: {k: 0.6 for k, v in crit.items() if "backup" in v.lower()})
                    out = hook.run(cfg, {"prompt": "Why did the nightly backup job fail last week?"}, client=fake,
                                   clock=FakeClock(), wall=lambda: 1_000_000.0, today=TODAY)
                    self.assertIn("# Related memory (auto-recall, Jev", out)
                    self.assertGreater(len(recall.fts_fallback(cfg, ["nightly", "backup"], TODAY)), 0)
                    args = Namespace(db=None, kind="preference", statement="Prefers short bullet summaries",
                                     subject=None, source="memory/2026-09-02.md", quote="short bullets",
                                     emotion=None, commit=True, backfill=True)
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(profile.cmd_new(cfg, args), 0)
                    self.assertIn("Prefers short bullet summaries", profile.render_inject_block(cfg))
                    self.assertEqual([r["statement"] for r in consolidate.active_traits(cfg)],
                                     ["Prefers short bullet summaries"])
                    r = run_cli("doctor", cfg_path=tp.cfg_path)
                    self.assertNotIn("Traceback", r.stderr)
                    self.assertIn("episodic index:", r.stdout)
                    self.assertEqual(sorted(p.name for p in tp.parent.iterdir()), [name],
                                     "no stray database file next to the project folder")
                finally:
                    tp.cleanup()


# =========================================================================== #
# P2 — no LLM: every message is kept (raw lines, offsets advance only past them)
# =========================================================================== #
class TestLosslessRawFallback(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        self.cfg = self.tp.cfg({"digest": {"index_after": False}})

    def tearDown(self):
        self.tp.cleanup()

    def daily(self, day="2026-09-10"):
        return (self.tp.root / "memory" / f"{day}.md").read_text(encoding="utf-8")

    def chat(self, n, minute0=10, user=True):
        write_chat(self.cfg, [{"ts": f"2026-09-10T09:{minute0 + i:02d}:00", "dir": "in" if user else "out",
                               "user": "sam", "text": f"message number {i} about topic {i}"}
                              for i in range(n)])

    def test_twenty_messages_while_claude_is_down_lose_nothing(self):
        self.chat(20)
        res = digest.run(self.cfg, backend=llm.NoneBackend(), now=datetime(2026, 9, 10, 9, 34), out=lambda *_: None)
        self.assertEqual((res["result"], res["deferred"]), ("raw", 0))
        text = self.daily()
        self.assertIn("## 09:34 summary (raw)", text)
        for i in range(20):
            self.assertIn(f"message number {i} about", text)

    def test_over_the_per_run_limit_the_rest_waits_for_the_next_run(self):
        cfg = self.tp.cfg({"digest": {"index_after": False, "raw_fallback_lines": 8}})
        self.chat(20)
        out = []
        res = digest.run(cfg, backend=llm.NoneBackend(), now=datetime(2026, 9, 10, 9, 34), out=out.append)
        self.assertEqual(res["deferred"], 12)
        self.assertTrue(any("left in the chat log for the next run" in o for o in out))
        text = self.daily()
        self.assertEqual(sum(f"message number {i} about" in text for i in range(20)), 8)
        self.assertIn("## 09:17 summary (raw)", text, "a partial section is headed with its last message's time")
        # the LLM is back: the next run summarizes exactly the 12 that were left
        fake = FakeLLM('{"topics": ["The rest was summarized."]}')
        res = digest.run(cfg, backend=fake, now=datetime(2026, 9, 10, 10, 4), out=lambda *_: None)
        self.assertEqual((res["result"], res["deferred"]), ("ok", 0))
        self.assertEqual(len(fake.prompts), 1)
        for i in range(20):
            self.assertEqual(f"message number {i} about" in fake.prompts[0], i >= 8, i)
        self.assertIn("## 10:04 summary\n- The rest was summarized.", self.daily())
        # and nothing is summarized twice
        res = digest.run(cfg, backend=fake, now=datetime(2026, 9, 10, 10, 34), out=lambda *_: None)
        self.assertEqual(res["result"], "skip")
        # raw windows line up with the partial heading
        self.assertEqual(raw.window(cfg, "2026-09-10", "10:04")[0], datetime(2026, 9, 10, 9, 17, 59))

    def test_a_long_window_is_summarized_in_parts_not_cut(self):
        cfg = self.tp.cfg({"digest": {"index_after": False, "max_chars": 400}})
        self.chat(20)
        fake = FakeLLM(lambda p: json.dumps({"topics": [f"part with {p.count('message number')} lines"]}))
        res = digest.run(cfg, backend=fake, now=datetime(2026, 9, 10, 9, 34), out=lambda *_: None)
        self.assertEqual(res["result"], "ok")
        self.assertGreater(len(fake.prompts), 1)
        seen = "".join(fake.prompts)
        for i in range(20):
            self.assertEqual(seen.count(f"message number {i} about"), 1, i)
        self.assertTrue(all(len(p.split("<conversation>")[1]) < 460 for p in fake.prompts))
        self.assertTrue(self.daily().rstrip().splitlines()[-2].startswith("## 09:34 summary"))

    def test_a_bot_only_tail_is_consumed_within_the_limit(self):
        cfg = self.tp.cfg({"digest": {"index_after": False, "raw_fallback_lines": 5}})
        self.chat(3)
        self.chat(4, minute0=20, user=False)
        res = digest.run(cfg, backend=llm.NoneBackend(), now=datetime(2026, 9, 10, 9, 34), out=lambda *_: None)
        self.assertEqual(res["deferred"], 0, "a tail with no user message would never be summarized later")
        text = self.daily()
        self.assertEqual(text.count("message number"), 5, "the 3 user lines + 2 assistant lines: the limit holds")
        self.assertIn("[09:22-09:23] (2 more message(s) from Assistant, none from User: not written out here, "
                      "the chat log keeps them)", text)
        self.assertEqual(len(raw.lookup(cfg, "2026-09-10", "09:34", max_lines=50)) - 1, 7,
                         "jevmem raw still shows all 7")

    def test_a_long_bot_only_tail_never_exceeds_the_limit(self):
        cfg = self.tp.cfg({"digest": {"index_after": False, "raw_fallback_lines": 5}})
        write_chat(cfg, [{"ts": "2026-09-10T09:00:00", "dir": "in", "text": "hi"}]
                   + [{"ts": f"2026-09-10T09:{1 + i // 60:02d}:{i % 60:02d}", "dir": "out", "via": "script",
                       "text": f"alert {i}"} for i in range(3000)])
        res = digest.run(cfg, backend=llm.NoneBackend(), now=datetime(2026, 9, 10, 10, 4), out=lambda *_: None)
        self.assertEqual((res["deferred"], res["records"]), (0, 3001))
        self.assertEqual([s["bullets"] for s in res["sections"]], [6], "5 lines + one counting line")
        self.assertIn("(2996 more message(s) from Assistant (script), none from User", self.daily())
        res = digest.run(cfg, backend=llm.NoneBackend(), now=datetime(2026, 9, 10, 10, 34), out=lambda *_: None)
        self.assertEqual(res["result"], "skip", "nothing left over")


# =========================================================================== #
# P3 — a missing logs folder never deletes the labels
# =========================================================================== #
class TestLogsFolderMissing(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()
        self.cfg = self.tp.cfg()
        self.fake = FakeJev(labeler=keyword_labeler(DEFAULT_RULES))
        self.first = indexer.run(self.cfg, client_factory=lambda: self.fake, out=lambda *_: None)

    def tearDown(self):
        self.tp.cleanup()

    def test_missing_or_empty_folder_keeps_everything(self):
        n_calls = len(self.fake.calls)
        logs = self.tp.root / "memory"
        moved = self.tp.root / "memory.away"
        logs.rename(moved)
        out = []
        res = indexer.run(self.cfg, client_factory=lambda: self.fake, out=out.append)
        self.assertIn("does not exist", res["logs_held"])
        self.assertEqual(units(self.cfg, "select count(*), count(labeled_at) from units"),
                         [(self.first["units"], self.first["units"])])
        self.assertEqual(indexer.status_word(res), "logs-missing")
        dry = indexer.run(self.cfg, dry_run=True, out=lambda *_: None)
        self.assertEqual(dry["removed"], 0)
        self.assertIn("logs_held", dry)
        logs.mkdir()  # back, but empty (a sync folder that is not populated yet)
        res = indexer.run(self.cfg, client_factory=lambda: self.fake, out=lambda *_: None)
        self.assertIn("no files match", res["logs_held"])
        logs.rmdir()
        moved.rename(logs)
        res = indexer.run(self.cfg, client_factory=lambda: self.fake, out=lambda *_: None)
        self.assertTrue(res.get("noop"), res)
        self.assertEqual(len(self.fake.calls), n_calls, "nothing had to be labeled again")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read anything")
    def test_unreadable_folder_keeps_everything(self):
        logs = self.tp.root / "memory"
        os.chmod(logs, 0)
        try:
            res = indexer.run(self.cfg, client_factory=lambda: self.fake, out=lambda *_: None)
        finally:
            os.chmod(logs, 0o755)
        self.assertIn("cannot read logs dir", res["logs_held"])
        self.assertEqual(units(self.cfg, "select count(labeled_at) from units"), [(self.first["units"],)])

    def test_force_prune_deletes_on_purpose(self):
        shutil.rmtree(self.tp.root / "memory")
        r = run_cli("index", "--force-prune", "--no-label", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(units(self.cfg, "select count(*) from units"), [(0,)])

    def test_digest_reports_it(self):
        cfg = self.tp.cfg({"digest": {"index_after": True}})
        (self.tp.root / "memory").rename(self.tp.root / "elsewhere")
        out = []
        digest.run(cfg, backend=llm.NoneBackend(), out=out.append, client_factory=lambda: self.fake)
        self.assertTrue(any("nothing deleted" in o for o in out), out)
        self.assertEqual(units(cfg, "select count(*) from units"), [(self.first["units"],)])


# =========================================================================== #
# P4 — an empty or damaged digest state resumes, never replays the whole chat log
# =========================================================================== #
class TestDigestStateRecovery(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        self.cfg = self.tp.cfg({"digest": {"index_after": False}})
        self.fake = FakeLLM(lambda p: json.dumps({"topics": [f"summary call {len(self.fake.prompts)}"]}))
        for day in ("2026-09-01", "2026-09-15", "2026-10-01"):
            write_chat(self.cfg, [{"ts": f"{day}T10:00:00", "dir": "in", "text": f"hello on {day}"}])
            digest.run(self.cfg, backend=self.fake, now=datetime.fromisoformat(f"{day}T10:04:00"), out=lambda *_: None)
        self.state = Path(self.cfg["data"]["dir"]) / digest.STATE_NAME

    def tearDown(self):
        self.tp.cleanup()

    def check_resume(self, damage, hour=10):
        damage()
        write_chat(self.cfg, [{"ts": f"2026-10-01T{hour}:20:00", "dir": "in", "text": "a new message"}])
        before = len(self.fake.prompts)
        out = []
        res = digest.run(self.cfg, backend=self.fake, now=datetime(2026, 10, 1, hour, 34), out=out.append)
        self.assertEqual(res["result"], "ok")
        self.assertEqual(len(self.fake.prompts), before + 1, "one call, not one per past day")
        self.assertIn("a new message", self.fake.prompts[-1])
        self.assertNotIn("hello on", self.fake.prompts[-1])
        for day in ("2026-09-01", "2026-09-15"):
            text = (self.tp.root / "memory" / f"{day}.md").read_text(encoding="utf-8")
            self.assertEqual(text.count("\n## "), 1, "no duplicate section in old logs")
        return out

    def test_empty_state_file(self):
        out = self.check_resume(lambda: self.state.write_text(""))
        self.assertTrue(any("was unreadable" in o for o in out), out)
        baks = list(self.state.parent.glob(digest.STATE_NAME + ".bak-*"))
        self.assertEqual(len(baks), 1, "the damaged file is kept aside")
        self.assertTrue(json.loads(self.state.read_text())["offsets"])

    def test_corrupt_and_wrong_shape_and_deleted(self):
        self.check_resume(lambda: self.state.write_text('{"offsets": {"x": "not a number"'), 11)
        self.check_resume(lambda: self.state.write_text('["a", "list"]'), 12)
        self.check_resume(lambda: self.state.unlink(), 13)

    def test_dry_run_moves_nothing(self):
        self.state.write_text("")
        out = []
        digest.run(self.cfg, dry_run=True, now=datetime(2026, 10, 1, 10, 34), out=out.append)
        self.assertEqual(self.state.read_text(), "")
        self.assertTrue(any("a real run moves it" in o for o in out))

    def test_atomic_write_flushes_to_disk(self):
        calls = []
        real = os.fsync
        with mock.patch.object(fsutil.os, "fsync", side_effect=lambda fd: (calls.append(fd), real(fd))):
            fsutil.write_text_atomic(self.state, '{"offsets": {}}')
        self.assertGreaterEqual(len(calls), 2, "the file and its directory")
        self.assertEqual(mode(self.state), 0o600)


# =========================================================================== #
# P5 — the evidence date comes from the log file's name, not from a folder above it
# =========================================================================== #
class TestEvidenceDate(unittest.TestCase):
    def test_source_date_of(self):
        f = profile.source_date_of
        self.assertEqual(f("/x/export-2025-01-01/memory/2026-09-02.md"), "2026-09-02")
        self.assertEqual(f("archive/2024-12-31/notes.md", "2026-09-03"), "2026-09-03")
        self.assertEqual(f("memory/2026-13-45.md", "2026-09-03"), "2026-09-03", "not a real date")
        self.assertEqual(f("2026-09-04-standup.md"), "2026-09-04")

    def test_consolidation_in_a_dated_folder_keeps_growing(self):
        proj = NamedProject("export-2025-01-01")
        try:
            cfg = proj.cfg()
            out = {}
            for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
                reply = json.dumps({"traits": [
                    {"kind": "preference", "statement": f"Trait from {day} number {i}",
                     "quote": quote, "relation": "new"}
                    for i, quote in enumerate(self.quotes(cfg, day)[:2])]})
                res = consolidate.run(cfg, day=day, backend=FakeLLM(reply), backfill=True, out=lambda *_: None)
                out[day] = [d.get("status") for d in res["decisions"]]
            with closing(sqlite3.connect(cfg["data"]["profile_db"])) as con:
                dates = dict(con.execute("select source_date, count(*) from evidence group by 1").fetchall())
            self.assertNotIn("2025-01-01", dates)
            self.assertEqual(sorted(dates), ["2026-09-01", "2026-09-02", "2026-09-03"], out)
            self.assertEqual(sum(dates.values()), 6, out)
        finally:
            proj.cleanup()

    @staticmethod
    def quotes(cfg, day):
        text = (Path(cfg["logs"]["dir"]) / f"{day}.md").read_text(encoding="utf-8")
        return [ln[2:42] for ln in text.splitlines() if ln.startswith("- ") and len(ln) > 45]

    def test_budget_message_names_the_cap_that_was_hit(self):
        tp = TempProject()
        try:
            today = date.today().isoformat()
            for i in range(3):
                r = run_cli("profile", "new", "--kind", "goal", "--statement", f"goal {i}", "--source",
                            f"memory/2026-08-0{i + 1}.md", "--quote", f"quote {i}", "--commit", "--backfill",
                            cfg_path=tp.cfg_path)
                self.assertEqual(r.returncode, 0, r.stderr)
            r = run_cli("profile", "new", "--kind", "goal", "--statement", "goal x", "--source",
                        "memory/2026-08-09.md", "--quote", "quote x", "--commit", cfg_path=tp.cfg_path)
            self.assertEqual(r.returncode, 2)
            self.assertIn("no budget left today", r.stderr)
            self.assertIn(today, r.stderr)
            self.assertIn("--backfill", r.stderr)
            self.assertNotIn("2026-08-09: no budget", r.stderr)
        finally:
            tp.cleanup()


# =========================================================================== #
# P6 — Jev outages during labeling don't count against chunks
# =========================================================================== #
class TestJevOutageLabeling(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()
        self.cfg = self.tp.cfg({"index": {"max_tries": 2, "workers": 1}})

    def tearDown(self):
        self.tp.cleanup()

    def test_outages_stop_the_run_without_tries(self):
        for err in (JevTimeout("Jev timeout after 20.00s"), JevUnavailable("Jev connection error: refused"),
                    JevUnavailable("Jev HTTP 503: down"), JevUnavailable("Jev HTTP 429: slow down")):
            with self.subTest(err=str(err)):
                for _ in range(4):  # more runs than max_tries
                    out = []
                    res = indexer.run(self.cfg, client_factory=lambda: FakeJev(error=err), out=out.append)
                    self.assertEqual(res["gave_up"], 0)
                    self.assertIn("jev_outage", res)
                    self.assertEqual(indexer.status_word(res), "jev-outage")
                self.assertEqual(units(self.cfg, "select max(coalesce(tries,0)) from units"), [(0,)])
        res = indexer.run(self.cfg, client_factory=lambda: FakeJev(labeler=keyword_labeler(DEFAULT_RULES)),
                          out=lambda *_: None)
        self.assertEqual(res["labeled"], res["units"])

    def test_unusable_answers_still_count(self):
        for _ in range(2):
            res = indexer.run(self.cfg, client_factory=lambda: FakeJev(error=JevError("Jev returned non-JSON: x")),
                              out=lambda *_: None)
        self.assertEqual(res["gave_up"], res["units"])

    def test_client_classifies_by_status_and_transport(self):
        import urllib.error

        def raising(exc):
            def fake(req, timeout):
                raise exc
            return fake

        c = jev_client.JevClient("k", "http://127.0.0.1:9/x")
        cases = [(urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b"down")), True),
                 (urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b"")), True),
                 (urllib.error.HTTPError("u", 422, "x", {}, io.BytesIO(b"bad")), False),
                 (urllib.error.URLError(ConnectionRefusedError(61, "refused")), True),
                 (urllib.error.URLError(OSError(8, "nodename nor servname provided")), True),
                 (ConnectionResetError(54, "reset"), True)]
        for exc, outage in cases:
            with self.subTest(exc=repr(exc)), mock.patch.object(jev_client, "_open", raising(exc)):
                with self.assertRaises(JevError) as cm:
                    c.call("s", {}, timeout=2)
                self.assertEqual(jev_client.is_outage(cm.exception), outage)
                self.assertFalse(jev_client.is_billing_error(cm.exception))
        with mock.patch.object(jev_client, "_open",
                               raising(urllib.error.HTTPError("u", 402, "x", {}, io.BytesIO(b"")))):
            with self.assertRaises(BillingError):
                c.call("s", {}, timeout=2)


# =========================================================================== #
# P7 — the call breaker is bounded whatever its state file says
# =========================================================================== #
class TestBreakerBounds(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name) / "claude_breaker.json"
        self.clock = FakeClock(1_800_000_000.0)
        self.brk = llm.CallBreaker(self.path, 30, 240, clock=self.clock)

    def tearDown(self):
        self.td.cleanup()

    def test_huge_failure_counts(self):
        for n in (1025, 10 ** 6, 10 ** 30, "x", -5, None, float("inf")):
            self.assertLessEqual(self.brk.pause_minutes(n), 240, n)
        self.assertEqual(self.brk.pause_minutes(1025), 240)
        self.path.write_text(json.dumps({"failures": 1024, "next_try": 0}))
        st = self.brk.failure("usage limit")
        self.assertEqual(st["failures"], llm.CallBreaker.MAX_FAILURES)
        self.assertAlmostEqual(st["next_try"] - self.clock(), 240 * 60)

    def test_damaged_state_values(self):
        for bad in ({"failures": "x", "next_try": "y"}, {"failures": -3}, {"failures": [1]}, {"failures": 1e400},
                    {"failures": 10 ** 40, "since": 5}):
            self.path.write_text(json.dumps(bad))
            st = self.brk.failure("network")
            self.assertTrue(1 <= st["failures"] <= llm.CallBreaker.MAX_FAILURES, bad)
            self.assertLessEqual(st["next_try"] - self.clock(), 240 * 60)

    def test_far_future_resume_time_is_clamped(self):
        self.path.write_text(json.dumps({"failures": 3, "next_try": self.clock() + 10 * 365 * 86400}))
        st = self.brk.blocked()
        self.assertAlmostEqual(st["next_try"] - self.clock(), 240 * 60)
        self.clock.advance(240 * 60 + 1)
        self.assertIsNone(self.brk.blocked(), "the pause ends within the configured maximum")
        self.path.write_text(json.dumps({"failures": 3, "next_try": float("nan")}))
        self.assertIsNone(self.brk.blocked())

    def test_recording_failures_always_ends_in_llm_unavailable(self):
        b = llm.ClaudeBackend(breaker=self.brk)
        with mock.patch.object(self.brk, "failure", side_effect=OverflowError("boom")), \
                mock.patch.object(b, "_run", side_effect=FileNotFoundError(2, "x")):
            with self.assertRaises(llm.LLMUnavailable):
                b.call("P")
        self.path.write_text(json.dumps({"failures": 1024, "next_try": 0}))
        fallback = FakeLLM('{"topics": ["fallback answered"]}')
        with mock.patch.object(b, "_run", side_effect=FileNotFoundError(2, "x")):
            self.assertEqual(llm.ChainBackend([b, fallback]).complete("P"), '{"topics": ["fallback answered"]}')


# =========================================================================== #
# S2 — an automatically found jevmem.toml must be yours; claude_bin and jev.url checks
# =========================================================================== #
FAKE_HELP_WITH_TOOLS = "Usage: claude [options]\n  --tools <tools...>   Specify the list of available tools\n"
FAKE_HELP_OLD = "Usage: claude [options]\n  --disallowedTools <tools...>  deny\n"


class TestConfigTrust(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()
        self.home = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tp.cleanup()
        self.home.cleanup()

    def cli(self, *args, **kw):
        return run_cli(*args, cwd=self.tp.root, env_extra={"HOME": self.home.name}, **kw)

    def test_group_writable_config_in_the_current_dir_is_refused(self):
        os.chmod(self.tp.cfg_path, 0o664)
        for args in (("search", "--fts", "nightly backup"), ("index", "--stats"), ("install-hook", "--dry-run")):
            r = self.cli(*args)
            self.assertEqual(r.returncode, 2, (args, r.stdout, r.stderr))
            self.assertIn("found automatically but is not used", r.stderr)
            self.assertIn("writable by group or others", r.stderr)
        r = self.cli("search", "--fts", "nightly backup", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, "named explicitly with --config: trusted as given")
        os.chmod(self.tp.cfg_path, 0o600)
        self.assertEqual(self.cli("index", "--stats").returncode, 0)

    def test_owner_check(self):
        with mock.patch.object(config.os, "getuid", return_value=os.getuid() + 1):
            self.assertIn("another user", config.untrusted_reason(self.tp.cfg_path))
        self.assertIsNone(config.untrusted_reason(self.tp.cfg_path))

    def test_claude_bin_and_jev_url(self):
        root = Path(self.tp.root)
        for bad in ({"llm": {"claude_bin": "./claude"}}, {"llm": {"claude_bin": "bin/claude"}},
                    {"jev": {"url": "http://api.example.com/v1"}}, {"jev": {"url": "ftp://x"}},
                    {"jev": {"url": "not a url"}}):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.from_dict(bad, root)
        ok = config.from_dict({"llm": {"claude_bin": "~/bin/claude"}, "jev": {"url": "http://localhost:8080/x"}}, root)
        self.assertEqual(ok["llm"]["claude_bin"], str(Path.home() / "bin" / "claude"))
        self.assertEqual(config.from_dict({"llm": {"claude_bin": "claude"}}, root)["llm"]["claude_bin"], "claude")
        config.from_dict({"jev": {"url": "https://api.example.com/v1"}}, root)

    def test_hook_commands_name_the_config_explicitly(self):
        cmd = install.hook_command(self.tp.cfg_path, python="/usr/bin/python3")
        self.assertIn(f"--config {self.tp.cfg_path}", cmd)


# =========================================================================== #
# S3 + S6 — `claude -p` runs without tools and reads the prompt from stdin
# =========================================================================== #
class FakeClaudeBinary:
    """A small executable standing in for `claude`: answers --help/--version, records argv
    and stdin of every print-mode call. No model is called."""

    def __init__(self, help_text=FAKE_HELP_WITH_TOOLS, help_rc=0):
        self.td = tempfile.TemporaryDirectory()
        d = Path(self.td.name)
        self.log = d / "calls.jsonl"
        self.path = d / "claude"
        self.path.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"HELP = {help_text!r}\n"
            "if '--help' in sys.argv:\n"
            f"    print(HELP); sys.exit({help_rc})\n"
            "if '--version' in sys.argv:\n"
            "    print('0.0.0 (fake)'); sys.exit(0)\n"
            "data = sys.stdin.read()\n"
            f"with open({str(self.log)!r}, 'a') as f:\n"
            "    f.write(json.dumps({'argv': sys.argv[1:], 'stdin': data}) + '\\n')\n"
            "print(json.dumps({'topics': ['fake answer']}))\n", encoding="utf-8")
        os.chmod(self.path, 0o755)

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def cleanup(self):
        self.td.cleanup()


class TestClaudeIsolation(unittest.TestCase):
    def setUp(self):
        llm._HELP_CACHE.clear()

    def run_fake(self, fake, prompt="PROMPT", **kw):
        b = llm.ClaudeBackend(binary=str(fake.path), **kw)
        return b.call(prompt), fake.calls()[-1]

    def test_tools_off_when_help_lists_it(self):
        fake = FakeClaudeBinary()
        try:
            out, call = self.run_fake(fake)
            self.assertIn("fake answer", out)
            argv = call["argv"]
            i = argv.index("--tools")
            self.assertEqual(argv[i + 1], "", "--tools \"\": every built-in tool off")
            self.assertEqual(argv[i + 2:i + 4], ["--disallowedTools", llm.DEFAULT_DISALLOWED_TOOLS])
            self.assertEqual(argv[-3:-1], ["--strict-mcp-config", "--mcp-config"])
        finally:
            fake.cleanup()

    def test_deny_list_extended_when_tools_flag_is_missing_or_unknown(self):
        for help_text, rc in ((FAKE_HELP_OLD, 0), ("broken", 1)):
            llm._HELP_CACHE.clear()
            fake = FakeClaudeBinary(help_text, rc)
            try:
                _, call = self.run_fake(fake)
                self.assertNotIn("--tools", call["argv"])
                deny = call["argv"][call["argv"].index("--disallowedTools") + 1].split(",")
                for t in ("Bash", "Edit", "Write", "WebFetch", "Read", "Glob", "Grep", "Task", "NotebookEdit"):
                    self.assertIn(t, deny)
                self.assertEqual(len(deny), len(set(deny)))
            finally:
                fake.cleanup()

    def test_setting_overrides_detection_and_empty_deny_list_stays_off(self):
        fake = FakeClaudeBinary(FAKE_HELP_OLD)
        try:
            _, call = self.run_fake(fake, tools_off=True)
            self.assertIn("--tools", call["argv"])
            llm._HELP_CACHE.clear()
            fake2 = FakeClaudeBinary()
            _, call = self.run_fake(fake2, tools_off=False)
            self.assertNotIn("--tools", call["argv"])
            _, call = self.run_fake(fake2, disallowed_tools="")
            self.assertNotIn("--disallowedTools", call["argv"])
            fake2.cleanup()
        finally:
            fake.cleanup()
        tp = TempProject(logs=False)
        try:
            self.assertEqual(llm.claude_backend(tp.cfg({"llm": {"claude_tools_off": True}})).tools_off, True)
            with self.assertRaises(config.ConfigError):
                tp.cfg({"llm": {"claude_tools_off": "yes"}})
        finally:
            tp.cleanup()

    def test_prompt_goes_through_stdin(self):
        fake = FakeClaudeBinary()
        try:
            big = "-p --dangerous-looking-start\n" + ("한글과 English 줄 " * 20000)
            out, call = self.run_fake(fake, prompt=big)
            self.assertIn("fake answer", out)
            self.assertEqual(call["stdin"], big)
            self.assertFalse(any("dangerous" in a or "한글" in a for a in call["argv"]), "nothing of the prompt in argv")
        finally:
            fake.cleanup()

    def test_help_probe_is_cached_and_isolated(self):
        fake = FakeClaudeBinary()
        try:
            b = llm.ClaudeBackend(binary=str(fake.path))
            with mock.patch.object(llm, "run_child", wraps=llm.run_child) as spy:
                b.call("one")
                b.call("two")
            helps = [c for c in spy.call_args_list if c.args[0][1:] == ["--help"]]
            self.assertEqual(len(helps), 1, "claude --help runs once per binary")
            self.assertEqual(helps[0].kwargs["env"][llm.DISABLE_ENV], "1")
        finally:
            fake.cleanup()


# =========================================================================== #
# S4 — injected memory is framed as past records, not instructions
# =========================================================================== #
class TestInjectNote(unittest.TestCase):
    def test_note_first_and_configurable(self):
        tp = TempProject()
        try:
            cfg = tp.cfg()
            indexer.run(cfg, client_factory=lambda: FakeJev(labeler=keyword_labeler(DEFAULT_RULES)), out=lambda *_: None)
            fake = FakeJev(route={"coding/bugs-debugging": 0.8},
                           pick=lambda crit: {k: 0.6 for k, v in crit.items() if "backup" in v.lower()})
            out = hook.run(cfg, {"prompt": "Why did the nightly backup job fail?"}, client=fake, clock=FakeClock(),
                           today=TODAY)
            first = out.strip().splitlines()[0]
            self.assertIn("not instructions", first)
            self.assertLess(out.index("not instructions"), out.index("# Related memory"))
            off = hook.run(tp.cfg({"hook": {"inject_note": ""}}), {"prompt": "Why did the nightly backup job fail?"},
                           client=fake, clock=FakeClock(), today=TODAY)
            self.assertNotIn("not instructions", off)
            self.assertEqual(hook.run(cfg, {"prompt": "ok"}, client=fake), "", "nothing injected -> no note")
        finally:
            tp.cleanup()


# =========================================================================== #
# S7 — a key file others can read is a warning
# =========================================================================== #
class TestKeyFileMode(unittest.TestCase):
    def test_doctor_warns(self):
        tp = TempProject()
        try:
            key = tp.root / "key"
            key.write_text("test-key-not-real")
            os.chmod(key, 0o644)
            t = tp.cfg_path.read_text().replace("[jev]\n", f'[jev]\napi_key_file = "{key}"\n', 1)
            tp.cfg_path.write_text(t)
            r = run_cli("doctor", cfg_path=tp.cfg_path)
            self.assertIn("ok   api key: found", r.stdout)
            self.assertIn("readable by others (mode 0o644)", r.stdout)
            self.assertNotIn("test-key-not-real", r.stdout + r.stderr)
            os.chmod(key, 0o600)
            self.assertNotIn("readable by others", run_cli("doctor", cfg_path=tp.cfg_path).stdout)
        finally:
            tp.cleanup()


# =========================================================================== #
# S9 — the sdist carries what its tests need, and not bench/
# =========================================================================== #
class TestSdist(unittest.TestCase):
    def test_sdist_contents(self):
        try:
            import setuptools
            from setuptools import build_meta
        except ImportError:
            self.skipTest("setuptools not installed")
        if int(setuptools.__version__.split(".")[0]) < 77:
            self.skipTest("setuptools < 77 (the build backend this project requires)")
        import tarfile
        from helpers import REPO
        skip = shutil.ignore_patterns(".git", "bench", "build", "dist", "*.egg-info", "__pycache__", ".jevmem",
                                      "memory", "*.sqlite")
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src_copy"
            shutil.copytree(REPO, src, ignore=skip)
            cwd = os.getcwd()
            try:
                os.chdir(src)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    name = build_meta.build_sdist(str(Path(td) / "dist"))
            finally:
                os.chdir(cwd)
            with tarfile.open(Path(td) / "dist" / name) as tf:
                names = {n.split("/", 1)[1] for n in tf.getnames() if "/" in n}
        for want in ("tests/helpers.py", "tests/test_round3.py", "examples/logs/2026-09-01.md",
                     "examples/capture.example.jsonl", "examples/taxonomy.example.json", "src/jevmem/cli.py",
                     "src/jevmem/data/example_taxonomy.json", "README.ko.md", "docs/config-reference.md"):
            self.assertIn(want, names)
        self.assertFalse([n for n in names if n.startswith("bench")])


# =========================================================================== #
# S10 — settings.json: private temp file, symlinks kept, every backup kept
# =========================================================================== #
class TestInstallWrite(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        self.proj = Path(tempfile.mkdtemp())

    def tearDown(self):
        self.tp.cleanup()
        shutil.rmtree(self.proj)

    def install(self, **kw):
        return install.install(self.proj, self.tp.cfg_path, python="/usr/bin/python3", out=lambda *_: None, **kw)

    def test_new_file_is_private_and_symlink_survives(self):
        res = self.install()
        settings = Path(res["settings"])
        self.assertEqual(mode(settings), 0o600)
        self.assertEqual([p.name for p in settings.parent.iterdir() if ".tmp" in p.name], [])
        real = self.proj / "dotfiles-settings.json"
        settings.rename(real)
        os.chmod(real, 0o640)
        settings.symlink_to(real)
        self.install(capture_hooks=False)
        self.assertTrue(settings.is_symlink(), "the link is kept")
        self.assertEqual(mode(real), 0o640, "the target keeps its mode")
        self.assertNotIn("capture inbound", real.read_text())

    def test_backups_in_the_same_second_are_all_kept(self):
        fixed = datetime(2026, 10, 29, 13, 29, 11)
        with mock.patch.object(install, "datetime") as dt:
            dt.now.return_value = fixed
            self.install()
            self.install(capture_hooks=False)
            self.install()
        baks = sorted(p.name for p in (self.proj / ".claude").glob("settings.json.bak-*"))
        self.assertEqual(baks, ["settings.json.bak-20261029-132911", "settings.json.bak-20261029-132911-1"])


# =========================================================================== #
# U1/U2 — wrong values are readable errors; unknown keys are doctor warnings
# =========================================================================== #
class TestConfigValidation(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()

    def tearDown(self):
        self.tp.cleanup()

    def with_cfg(self, text):
        self.tp.cfg_path.write_text(self.tp.cfg_path.read_text() + "\n" + text)

    def test_backend_typo(self):
        t = self.tp.cfg_path.read_text().replace('backend = "none"', 'backend = "claude-sonnet"')
        self.tp.cfg_path.write_text(t)
        r = run_cli("doctor", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 1)
        self.assertIn('FAIL config: [llm] backend must be one of "claude", "ollama", "none"', r.stdout)
        self.assertIn("claude_model", r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        r = run_cli("profile", "consolidate", "--date", "2026-09-02", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 2)
        self.assertIn("[llm] backend must be one of", r.stderr)

    def test_wrong_types_and_ranges(self):
        root = self.tp.root
        cases = [({"recall": {"max_picks": "four"}}, "[recall] max_picks must be a number"),
                 ({"hook": {"log_text": "false"}}, "must be true or false"),
                 ({"recall": {"pick_min_p": 1.5}}, "between 0 and 1"),
                 ({"index": {"batch_size": 0}}, "at least 1"),
                 ({"index": {"max_tries": 2.5}}, "whole number"),
                 ({"context": {"source": "chat"}}, "[context] source must be one of"),
                 ({"recall": {"expand": "all"}}, "[recall] expand must be one of"),
                 ({"consolidate": {"prompt": "log {log} {oops}"}}, "bad template"),
                 ({"raw": {"heading_re": "(("}}, "not a valid regular expression"),
                 ({"capture": {"sources": {"x": 1}}}, "[capture] sources must be a table"),
                 ({"schedule": {"label_prefix": "com jevmem"}}, "label_prefix"),
                 ({"recall": {"pick_round_min_p": []}}, "pick_round_min_p")]
        for data, want in cases:
            with self.subTest(data=data), self.assertRaises(config.ConfigError) as cm:
                config.from_dict(data, root)
            self.assertIn(want, str(cm.exception))
        cfg = config.from_dict({"hook": {"breaker_minutes": 7.5}, "llm": {"backend": "OLLAMA"},
                                "fallback": {"bm25_threshold": -3}}, root)
        self.assertEqual((cfg["llm"]["backend"], cfg["hook"]["breaker_minutes"]), ("ollama", 7.5))

    def test_cli_exit_2_hook_exit_0(self):
        self.with_cfg('[recall]\nmax_picks = "four"\n')
        r = run_cli("search", "--fts", "backup", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 2)
        self.assertIn("error: [recall] max_picks must be a number", r.stderr)
        r = run_cli("hook", cfg_path=self.tp.cfg_path, stdin=json.dumps({"prompt": "nightly backup relative path"}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_unknown_sections_and_keys_are_warnings(self):
        self.with_cfg("[recal]\nmax_picks = 2\n\n[hook]\ndeadline = 3\n")
        cfg = self.tp.cfg()
        self.assertEqual(cfg.unknown, ["[recal]", "[hook] deadline"])
        r = run_cli("doctor", cfg_path=self.tp.cfg_path)
        self.assertIn("warn config: unknown [recal]", r.stdout)
        self.assertIn("warn config: unknown [hook] deadline", r.stdout)
        self.assertIn("ok   config:", r.stdout)
        r = run_cli("capture", "--help", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0)


# =========================================================================== #
# U3 — doctor flags the example taxonomy
# =========================================================================== #
class TestExampleTaxonomyWarning(unittest.TestCase):
    def test_doctor(self):
        tp = TempProject()
        try:
            r = run_cli("doctor", cfg_path=tp.cfg_path)
            self.assertIn("warn taxonomy: example taxonomy in use", r.stdout)
            p = Path(tp.cfg()["data"]["taxonomy"])
            tax = json.loads(p.read_text())
            tax["version"] = "mine-1"
            p.write_text(json.dumps(tax))
            self.assertNotIn("example taxonomy in use", run_cli("doctor", cfg_path=tp.cfg_path).stdout)
        finally:
            tp.cleanup()


# =========================================================================== #
# U4 — reading commands without any config file say so (exit 2)
# =========================================================================== #
class TestNoConfigFound(unittest.TestCase):
    def test_exit_2_with_the_search_order(self):
        with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as home:
            env = {"HOME": home}
            for args in (("search", "--fts", "backup"), ("raw", "2026-09-10"), ("profile", "list"),
                         ("capture", "tail"), ("profile", "consolidate", "--dry-run")):
                r = run_cli(*args, cwd=empty, env_extra=env)
                self.assertEqual(r.returncode, 2, args)
                self.assertIn("no jevmem.toml found (searched --config, then $JEVMEM_CONFIG, then ./jevmem.toml",
                              r.stderr, args)
                self.assertEqual(r.stdout, "", args)
            for args in (("hook",), ("capture", "inbound")):
                r = run_cli(*args, cwd=empty, env_extra=env, stdin=json.dumps({"prompt": "hello there"}))
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), args)
            self.assertEqual(list(Path(empty).iterdir()), [])

    def test_empty_capture_tail_says_so(self):
        tp = TempProject(logs=False)
        try:
            r = run_cli("capture", "tail", cfg_path=tp.cfg_path)
            self.assertEqual(r.returncode, 0)
            self.assertIn("no records yet", r.stderr)
        finally:
            tp.cleanup()


# =========================================================================== #
# U5 — source = "jsonl" without a usable jsonl_path is reported as such
# =========================================================================== #
class TestJsonlSource(unittest.TestCase):
    def test_missing_path(self):
        tp = TempProject(logs=False)
        try:
            out = []
            res = digest.run(tp.cfg({"digest": {"source": "jsonl", "index_after": False}}), backend=FakeLLM("{}"),
                             out=out.append)
            self.assertEqual(res["result"], "no-source")
            self.assertIn("jsonl_path is not set", out[0])
            out = []
            res = digest.run(tp.cfg({"digest": {"source": "jsonl", "jsonl_path": "nope.jsonl", "index_after": False}}),
                             backend=FakeLLM("{}"), out=out.append)
            self.assertIn("does not exist", out[0])
            shutil.copy(EXAMPLES / "chat.example.jsonl", tp.root / "chat.jsonl")
            res = digest.run(tp.cfg({"digest": {"source": "jsonl", "jsonl_path": "chat.jsonl", "index_after": False}}),
                             backend=FakeLLM('{"topics": ["ok"]}'), out=lambda *_: None)
            self.assertEqual(res["result"], "ok")
            t = tp.cfg_path.read_text() + '\n[context]\nsource = "jsonl"\n'
            tp.cfg_path.write_text(t)
            r = run_cli("doctor", cfg_path=tp.cfg_path)
            self.assertIn("warn context source: jsonl, but [context] jsonl_path is not set", r.stdout)
        finally:
            tp.cleanup()


# =========================================================================== #
# U6 — consolidate --date, raw's gap message, Jev 402 in doctor, dry-run help
# =========================================================================== #
class TestSmallCliDetails(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()

    def tearDown(self):
        self.tp.cleanup()

    def test_consolidate_rejects_bad_dates(self):
        for bad in ("2026-13-01", "yesterday", "2026-02-30"):
            r = run_cli("profile", "consolidate", "--date", bad, "--dry-run", cfg_path=self.tp.cfg_path)
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn("error:", r.stderr)

    def test_consolidate_dry_run_help(self):
        r = run_cli("profile", "consolidate", "--help", cfg_path=self.tp.cfg_path)
        self.assertIn("no LLM call", r.stdout)

    def test_raw_gap_message(self):
        for p in (EXAMPLES / "summaries").glob("*.md"):
            shutil.copy(p, self.tp.root / "memory" / p.name)
        shutil.copy(EXAMPLES / "capture.example.jsonl", Path(self.tp.cfg()["capture"]["path"]))
        cfg = self.tp.cfg()
        head = raw.lookup(cfg, "2026-09-10", "10:00")[0]
        self.assertIn("no summary window contains 10:00; showing the next one, summary 11:30", head)
        head = raw.lookup(cfg, "2026-09-10", "09:20")[0]
        self.assertIn("showing summary 09:34, whose window contains 09:20", head)
        self.assertIn("no summary window contains it", raw.lookup(cfg, "2026-09-10", around="10:30")[0])

    def test_doctor_shows_the_jev_402_pause(self):
        cfg = self.tp.cfg()
        fsutil.write_text_atomic(cfg["data"]["breaker"], str(__import__("time").time() + 1200))
        r = run_cli("doctor", cfg_path=self.tp.cfg_path)
        self.assertIn("warn jev: paused until", r.stdout)
        self.assertIn("HTTP 402", r.stdout)


# =========================================================================== #
# U6 — the config reference lists every key, in both languages
# =========================================================================== #
class TestConfigReference(unittest.TestCase):
    def test_every_key_in_both_languages(self):
        from helpers import REPO
        for name in ("config-reference.md", "config-reference.ko.md"):
            text = (REPO / "docs" / name).read_text(encoding="utf-8")
            sections = {}
            cur = None
            for line in text.splitlines():
                if line.startswith("## `["):
                    cur = line.split("`[", 1)[1].split("]", 1)[0]
                    sections[cur] = set()
                elif cur and line.startswith("| `"):
                    sections[cur].add(line.split("`", 2)[1])
            for section, keys in config.DEFAULTS.items():
                self.assertIn(section, sections, (name, section))
                self.assertEqual(sections[section], set(keys), (name, section))
            self.assertNotRegex(text, r"\$\s?\d|달러|과금")


# =========================================================================== #
# M1 — re-syncing the FTS mirror is one pass, not one table scan per chunk
# =========================================================================== #
class TestFtsResync(unittest.TestCase):
    def test_single_delete_pass_and_mirror_stays_exact(self):
        tp = TempProject()
        try:
            cfg = tp.cfg()
            indexer.run(cfg, no_label=True, out=lambda *_: None)
            cfg2 = tp.cfg({"index": {"max_chunk": 120}})
            con = store.connect(cfg2["data"]["episodic_db"])
            store.init_db(con)
            stmts = []
            con.set_trace_callback(stmts.append)
            added, removed = store.sync_units(con, indexer.collect_chunks(cfg2))
            con.commit()
            con.set_trace_callback(None)
            self.assertGreater(removed, 1)
            self.assertEqual(sum(1 for s in stmts if s.startswith("DELETE FROM units_fts")), 1)
            fts = sorted(con.execute("select hash from units_fts").fetchall())
            self.assertEqual(fts, sorted(con.execute("select hash from units").fetchall()))
            con.close()
        finally:
            tp.cleanup()


# =========================================================================== #
# M2 — consolidation reports "already recorded" and the superseding trait correctly
# =========================================================================== #
class TestConsolidateAccounting(unittest.TestCase):
    def test_already_recorded_and_supersede(self):
        tp = TempProject()
        try:
            cfg = tp.cfg()
            q = "Prefers short bullet summaries over long paragraphs"
            log = (tp.root / "memory" / "2026-09-02.md")
            log.write_text(log.read_text() + f"\n- The user said: {q}.\n")
            new = json.dumps({"traits": [{"kind": "preference", "statement": "Likes short bullet summaries",
                                          "quote": q, "relation": "new"}]})
            r1 = consolidate.run(cfg, day="2026-09-02", backend=FakeLLM(new), out=lambda *_: None)
            self.assertEqual(r1["decisions"][0]["status"], "written")
            tid = r1["decisions"][0]["trait_id"]
            merge = json.dumps({"traits": [{"kind": "preference", "statement": "Likes short bullet summaries",
                                            "quote": q, "relation": "merge", "target_id": tid}]})
            r2 = consolidate.run(cfg, day="2026-09-02", backend=FakeLLM(merge), out=lambda *_: None)
            self.assertEqual(r2["decisions"][0]["status"], "already recorded")
            sup = json.dumps({"traits": [{"kind": "preference", "statement": "Wants one-line answers only",
                                          "quote": q[:30], "relation": "supersede", "target_id": tid}]})
            r3 = consolidate.run(cfg, day="2026-09-02", backend=FakeLLM(sup), backfill=True, out=lambda *_: None)
            d = r3["decisions"][0]
            self.assertEqual(d["status"], "written")
            self.assertNotEqual(d["trait_id"], tid, "the new (superseding) trait, not the old one")
        finally:
            tp.cleanup()


# =========================================================================== #
# M3 — iterative pick: a failing later round keeps the earlier picks
# =========================================================================== #
class TestPickRoundFailure(unittest.TestCase):
    def test_round_two_timeout_keeps_round_one(self):
        cfg = config.from_dict({"recall": {"pick_rounds": 2, "max_picks": 4}}, Path("."))
        cands = [{"hash": f"h{i}", "relpath": f"s{i}.md", "date": "2026-09-01", "chunk": f"chunk {i}"} for i in range(5)]

        class TwoRounds(FakeJev):
            def call(self, state, questions, timeout=2.0):
                if self.calls:
                    self.calls.append(1)
                    raise JevTimeout("Jev timeout after 1.00s")
                return super().call(state, questions, timeout)

        client = TwoRounds(pick=lambda crit: {"c0": 0.9})
        hits, info = recall.pick_rounds(cfg, client, recall.Deadline(30), "s", cands, [], TODAY)
        self.assertEqual([c["hash"] for c in hits], ["h0"])
        with self.assertRaises(JevTimeout):
            recall.pick_rounds(cfg, FakeJev(error=JevTimeout("t")), recall.Deadline(30), "s", cands, [], TODAY)


# =========================================================================== #
# M4 — "date:N" pseudo tokens don't break the FTS AND query
# =========================================================================== #
class TestFtsDateToken(unittest.TestCase):
    def test_and_query_ignores_date_tokens(self):
        tp = TempProject(logs=False)
        try:
            lines = ["# 2026-09-01 daily log", "", "## 10:00 notes",
                     "- The nightly backup failed because the cron entry used a relative path."]
            lines += [f"## 1{i}:00 notes\n- Backup volume check number {i} for the photo archive went fine."
                      for i in range(1, 7)]
            (tp.root / "memory" / "2026-09-01.md").write_text("\n".join(lines) + "\n")
            cfg = tp.cfg({"fallback": {"bm25_threshold": 0}})
            indexer.run(cfg, no_label=True, out=lambda *_: None)
            plain = recall.fts_fallback(cfg, ["nightly", "backup"], TODAY)
            dated = recall.fts_fallback(cfg, ["nightly", "backup", "date:9"], TODAY)
            self.assertEqual(len(plain), 1, "AND: only the chunk with both words")
            self.assertEqual([h["chunk"] for h in dated], [h["chunk"] for h in plain],
                             "a date:N pseudo token must not turn the AND query into an OR")
        finally:
            tp.cleanup()


# =========================================================================== #
# M5, M6, M7 — capture details
# =========================================================================== #
class TestCaptureDetails(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        self.cfg = self.tp.cfg()

    def tearDown(self):
        self.tp.cleanup()

    def block(self, text, mid):
        return (f'<channel source="plugin:telegram:telegram" chat_id="1" message_id="{mid}" user="sam" '
                f'ts="2026-09-10T00:05:00Z">{text}</channel>')

    def test_lone_surrogate_is_saved_replaced(self):
        from jevmem import capture
        self.assertEqual(capture.inbound(self.cfg, {"prompt": self.block("broken \ud83d emoji", "1")}), 1)
        self.assertEqual([r["text"] for r in capture.tail(self.cfg)], ["broken ? emoji"])

    def test_tag_that_only_starts_with_channel_is_not_a_block(self):
        from jevmem import capture, text
        p = "<channel-note> " + self.block("the real one", "2") + " <channels>x</channel>"
        self.assertEqual([b for _, b in text.channel_blocks(p)], ["the real one"])
        self.assertEqual(capture.inbound(self.cfg, {"prompt": p}), 1)
        self.assertEqual(text.message_text(p), "the real one")

    def test_lock_wait_is_one_budget_per_prompt(self):
        from jevmem import capture
        lock = Path(self.cfg["data"]["dir"]) / (capture.SEEN_NAME + ".lock")
        fd = fsutil.try_lock(lock)
        try:
            prompt = "".join(self.block(f"message {i}", str(10 + i)) for i in range(4))
            with mock.patch.object(capture, "SEEN_LOCK_TIMEOUT", 0.4):
                t0 = __import__("time").monotonic()
                n = capture.inbound(self.cfg, {"prompt": prompt})
                took = __import__("time").monotonic() - t0
        finally:
            os.close(fd)
        self.assertEqual(n, 4, "saved without the duplicate check rather than lost")
        self.assertLess(took, 1.0, "4 blocks share one 0.4 s wait, not 4 x 0.4 s")


class TestTranscriptFirstRunStaysBaseline(unittest.TestCase):
    def test_missing_state_with_old_summaries(self):
        tp = TempProject(logs=False)
        try:
            proj = tp.root / "proj"
            sessions = tp.root / "projects" / digest.project_slug(proj)
            sessions.mkdir(parents=True)
            proj.mkdir()
            (sessions / "s.jsonl").write_text(json.dumps({"type": "user", "timestamp": "2026-10-01T10:00:00",
                                                          "message": {"content": "old session text"}}) + "\n")
            (tp.root / "memory" / "2026-09-01.md").write_text("# 2026-09-01 daily log\n\n## 09:04 summary\n- x\n")
            cfg = tp.cfg({"digest": {"source": "claude_transcript", "claude_projects_dir": str(tp.root / "projects"),
                                     "project_dir": str(proj), "index_after": False}})
            fake = FakeLLM('{"topics": ["t"]}')
            res = digest.run(cfg, backend=fake, now=datetime(2026, 10, 1, 10, 34), out=lambda *_: None)
            self.assertEqual(res["result"], "baseline")
            self.assertEqual(fake.prompts, [])
        finally:
            tp.cleanup()


if __name__ == "__main__":
    unittest.main()

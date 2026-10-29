"""Raw pointer (`jevmem raw`, pointer line under recalled summaries), whole bullet/section
injection with a character budget, and the digest's speaker/status/number handling.
Fictional example data only (examples/summaries, examples/capture.example.jsonl); fakes
only (no LLM, no Jev)."""
import json
import shutil
import unittest
from datetime import date, datetime
from pathlib import Path

from helpers import EXAMPLES, FakeClock, FakeJev, TempProject, keyword_labeler, run_cli

from jevmem import capture, chunking, digest, hook, indexer, raw, recall
from test_stages import FakeLLM, reply_payload, tg

TODAY = date(2026, 9, 11)
RULES = [("backup", "coding", "bugs-debugging"), ("dinner", "family-home", "events-plans"),
         ("demo", "work", "meetings"), ("reminder", "work", "planning-docs")]


def raw_project(extra_cfg=""):
    """Summaries of 2026-09-09/10 as the daily logs + the matching capture log."""
    tp = TempProject(logs=False, extra_cfg=extra_cfg)
    for p in sorted((EXAMPLES / "summaries").glob("*.md")):
        shutil.copy(p, tp.root / "memory" / p.name)
    shutil.copy(EXAMPLES / "capture.example.jsonl", tp.root / ".jevmem" / "chat.jsonl")
    return tp


class TestWindows(unittest.TestCase):
    def setUp(self):
        self.tp = raw_project()
        self.cfg = self.tp.cfg()

    def tearDown(self):
        self.tp.cleanup()

    def test_headings_and_window_rules(self):
        self.assertEqual(raw.headings(self.cfg, "2026-09-10"),
                         [(None, "09:04"), (None, "09:34"), ("11:00", "11:30")])
        # first summary of the day starts at the previous day's last summary
        self.assertEqual(raw.window(self.cfg, "2026-09-10", "09:04"),
                         (datetime(2026, 9, 9, 21, 34, 59), datetime(2026, 9, 10, 9, 4, 59)))
        self.assertEqual(raw.window(self.cfg, "2026-09-10", "9:34"),
                         (datetime(2026, 9, 10, 9, 4, 59), datetime(2026, 9, 10, 9, 34, 59)))
        # explicit range in the heading
        self.assertEqual(raw.window(self.cfg, "2026-09-10", "11:30"),
                         (datetime(2026, 9, 10, 10, 59, 59), datetime(2026, 9, 10, 11, 30, 59)))
        # no previous day's log -> from midnight
        self.assertEqual(raw.window(self.cfg, "2026-09-09", "21:34")[0], datetime(2026, 9, 8, 23, 59, 59))
        self.assertIsNone(raw.window(self.cfg, "2026-09-10", "10:00"))
        self.assertEqual(raw.window_around(self.cfg, "2026-09-10", "09:20")[1], "09:34")
        self.assertIsNone(raw.window_around(self.cfg, "2026-09-10", "13:00")[1], "after the last summary: ±30 min")

    def test_lookup_lines_speakers_and_cross_midnight_window(self):
        out = raw.lookup(self.cfg, "2026-09-10", "09:04")
        self.assertIn("window 21:35~09:04 (2026-09-09~2026-09-10), 4 lines", out[0])
        self.assertEqual(out[1:], [
            "[08:50] User: why did the nightly backup fail?",
            "[08:52] Assistant: The cron entry uses a relative path. Want me to switch it to an absolute path?",
            "[08:55] Assistant (script): Alert: backup volume disk usage 80%",
            "[09:01] User: yes do it"])
        out = raw.lookup(self.cfg, "2026-09-10", "09:34")
        self.assertIn("[09:20] User (dana): team dinner budget is 1,200", out, "several senders -> names")
        self.assertIn("[09:22] User (lee): no, 1,500 for the venue", out)
        self.assertIn("[09:10] Assistant: Switched the cron entry", "\n".join(out))

    def test_lookup_keywords_around_whole_day_and_max(self):
        out = raw.lookup(self.cfg, "2026-09-10", "09:34", ["1,500"])
        self.assertIn("3 lines · keywords ['1,500']: 1 matching line (±1 line of context)", out[0])
        self.assertEqual([ln[:2] for ln in out[1:]], ["  ", "» "], "match marked, ±1 line of context")
        out = raw.lookup(self.cfg, "2026-09-10", around="09:20")
        self.assertIn("summary 09:34", out[0])
        out = raw.lookup(self.cfg, "2026-09-10", "10:00")
        self.assertIn("no '10:00 summary' heading", out[0])
        out = raw.lookup(self.cfg, "2026-09-10")
        self.assertIn("whole day", out[0])
        self.assertEqual(len(out) - 1, 9)
        out = raw.lookup(self.cfg, "2026-09-10", max_lines=2)
        self.assertEqual(len(out), 4)
        self.assertIn("+7 lines", out[-1])
        self.assertIn("no line matches", raw.lookup(self.cfg, "2026-09-10", "09:04", ["zzz"])[1])

    def test_not_covered_and_bad_input(self):
        msg = raw.lookup(self.cfg, "2026-09-01", "09:00")[0]
        self.assertIn("[no raw lines]", msg)
        self.assertIn("There is no daily log memory/2026-09-01.md either", msg,
                      "no pretending a summary exists when its file is missing")
        (self.tp.root / "memory" / "2026-09-01.md").write_text("# 2026-09-01 daily log\n", encoding="utf-8")
        self.assertIn("Only the summary in memory/2026-09-01.md is available",
                      raw.lookup(self.cfg, "2026-09-01", "09:00")[0])
        with self.assertRaises(ValueError):
            raw.lookup(self.cfg, "10-09-2026")
        with self.assertRaises(ValueError):
            raw.lookup(self.cfg, "2026-09-10", around="9h")
        for bad in ("9:99", "24:00"):
            with self.assertRaises(ValueError) as cm:
                raw.lookup(self.cfg, "2026-09-10", bad)
            self.assertIn("between 00:00 and 23:59", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            raw.lookup(self.cfg, "2026-13-45")
        self.assertEqual(str(cm.exception), "2026-13-45 is not a real date (YYYY-MM-DD)")

    def test_singular_line_counts(self):
        out = raw.lookup(self.cfg, "2026-09-10", "11:30")
        self.assertIn("window 11:00~11:30, 1 line", out[0])
        self.assertNotIn("1 lines", out[0])
        out = raw.lookup(self.cfg, "2026-09-10", max_lines=8)
        self.assertEqual(out[-1], "... (+1 line; raise --max to see more)")

    def test_skip_prefixes_hide_status_noise(self):
        cfg = self.tp.cfg({"raw": {"skip_prefixes": ["Alert:"]}})
        self.assertNotIn("Alert", "\n".join(raw.lookup(cfg, "2026-09-10", "09:04")))

    def test_pointer_line(self):
        chunk = "## 09:34 summary\n- Dana said the team dinner budget is 1,200; Lee said 1,500 for the venue."
        self.assertEqual(raw.pointer_line(self.cfg, "2026-09-10.md", "2026-09-10", chunk),
                         "  ↳ raw: jevmem raw 2026-09-10 09:34")
        self.assertEqual(raw.pointer_line(self.cfg, "2026-09-10.md", "2026-09-10",
                                          "## 11:00~11:30 summary\n- demo question"),
                         "  ↳ raw: jevmem raw 2026-09-10 11:30")
        self.assertIsNone(raw.pointer_line(self.cfg, "2026-09-10.md", "2026-09-10", "## 09:05 check-in\n- x"))
        self.assertIsNone(raw.pointer_line(self.cfg, "2026-09-01.md", "2026-09-01", "## 09:00 summary\n- x"),
                          "date before the chat log starts")
        self.assertIsNone(raw.pointer_line(self.cfg, "CHANGELOG.md", "2026-09-10", chunk))
        off = self.tp.cfg({"recall": {"raw_pointer": False}})
        self.assertIsNone(raw.pointer_line(off, "2026-09-10.md", "2026-09-10", chunk))
        custom = self.tp.cfg({"recall": {"raw_pointer_command": "jevmem --config /x/jevmem.toml raw"}})
        self.assertIn("jevmem --config /x/jevmem.toml raw 2026-09-10 09:34",
                      raw.pointer_line(custom, "2026-09-10.md", "2026-09-10", chunk))

    def test_pointer_needs_a_chat_log(self):
        Path(self.cfg["capture"]["path"]).unlink()
        raw._since_cache.clear()
        self.assertIsNone(raw.pointer_line(self.cfg, "2026-09-10.md", "2026-09-10", "## 09:34 summary\n- x y z"))

    def test_cli(self):
        r = run_cli("raw", "2026-09-10", "09:34", "budget", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[2026-09-10 09:34 raw] window 09:05~09:34", r.stdout)
        self.assertIn("» [09:20] User (dana): team dinner budget is 1,200", r.stdout)
        r = run_cli("raw", "2026-09-10", "--around", "11:10", "--max", "1", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("demo", r.stdout)
        r = run_cli("raw", "yesterday", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 2)
        self.assertIn("YYYY-MM-DD", r.stderr)
        for args, want in ((("2026-09-10", "9:99"), "error: time must be HH:MM between 00:00 and 23:59"),
                           (("2026-02-30",), "error: 2026-02-30 is not a real date")):
            r = run_cli("raw", *args, cfg_path=self.tp.cfg_path)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn(want, r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertNotIn("does not match format", r.stderr, "no raw Python message")

    def test_script_label_same_in_raw_and_capture_tail(self):
        r = run_cli("capture", "tail", "-n", "20", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Assistant (script): Alert: backup volume disk usage 80%", r.stdout)
        self.assertIn("User (sam): why did the nightly backup fail?", r.stdout)
        self.assertIn("Assistant (script): Alert: backup volume disk usage 80%",
                      "\n".join(raw.lookup(self.cfg, "2026-09-10", "09:04")))


LONG_BULLET = ("- The user asked for a full review of the backup setup; " + "the assistant listed every job, " * 40
               + "and the last item was the offsite copy.")


class TestExpandAndRender(unittest.TestCase):
    def setUp(self):
        self.tp = raw_project()
        log = self.tp.root / "memory" / "2026-09-10.md"
        log.write_text(log.read_text() + "\n## 14:04 summary\n" + LONG_BULLET + "\n- A short second topic about lunch plans.\n")
        self.cfg = self.tp.cfg()
        text = log.read_text()
        self.chunks = chunking.chunk_text(text, "2026-09-10.md", hard_cut=300,
                                          split_heading_re=self.cfg["index"]["one_chunk_per_bullet_heading"])

    def tearDown(self):
        self.tp.cleanup()

    def hit(self, needle, i=0):
        found = [c for c in self.chunks if needle in c[2]]
        rel, d, ch = found[i]
        return {"relpath": rel, "date": d, "chunk": ch}

    def test_bullet_mode_undoes_hard_cut(self):
        tail = self.hit("offsite copy")
        self.assertNotIn("full review", tail["chunk"], "a hard-cut piece of the long bullet")
        full = recall.expand(self.cfg, tail, "bullet")
        self.assertTrue(full.startswith("## 14:04 summary\n- The user asked for a full review"))
        self.assertTrue(full.endswith("offsite copy."))
        self.assertNotIn("lunch", full, "only the bullet, not the section")

    def test_section_mode_and_missing_file(self):
        h = self.hit("Lee said 1,500")
        sec = recall.expand(self.cfg, h, "section")
        self.assertEqual(sec.splitlines()[0], "## 09:34 summary")
        self.assertIn("absolute path", sec)
        self.assertIn("1,500", sec)
        self.assertEqual(recall.expand(self.cfg, h, "bullet"), h["chunk"])
        self.assertIsNone(recall.expand(self.cfg, dict(h, relpath="gone.md"), "section"))
        self.assertIsNone(recall.expand(self.cfg, dict(h, chunk="text that is no longer in the log"), "bullet"))
        self.assertIsNone(recall.expand(self.cfg, h, "none"))

    def test_render_default_keeps_chunks_and_adds_pointer(self):
        hits = [self.hit("Lee said 1,500"), self.hit("full review")]
        out = recall.render_hits(self.cfg, "# H", hits, 400)
        self.assertIn("## 09:34 summary\n- Dana said", out)
        self.assertIn("↳ raw: jevmem raw 2026-09-10 09:34", out)
        self.assertIn("↳ raw: jevmem raw 2026-09-10 14:04", out)
        self.assertNotIn("offsite copy", out, "expand = none: the picked piece only")

    def test_render_section_dedupes_and_respects_budget(self):
        cfg = self.tp.cfg({"recall": {"expand": "section"}})
        hits = [self.hit("Lee said 1,500"), self.hit("absolute path and said")]
        out = recall.render_hits(cfg, "# H", hits, 400)
        self.assertEqual(out.count("## 09:34 summary"), 1, "two picks from one section -> injected once")
        self.assertEqual(out.count("↳ raw:"), 1)
        cfg = self.tp.cfg({"recall": {"expand": "bullet", "expand_max_chars": 5000, "max_inject_chars": 700}})
        hits = [self.hit("offsite copy"), self.hit("Lee said 1,500"), self.hit("demo")]
        out = recall.render_hits(cfg, "# H", hits, 400)
        self.assertIn("offsite copy.", out, "the first item is always kept whole")
        self.assertNotIn("demo", out, "budget exhausted")
        cfg = self.tp.cfg({"recall": {"expand": "bullet", "expand_max_chars": 200}})
        out = recall.render_hits(cfg, "# H", [self.hit("offsite copy")], 400)
        self.assertLess(len(out), 330)
        self.assertIn(" ...", out)


class TestHookPointer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tp = raw_project()
        fake = FakeJev(labeler=keyword_labeler(RULES))
        res = indexer.run(cls.tp.cfg(), client_factory=lambda: fake, out=lambda *_: None)
        assert res["labeled"] > 0, res

    @classmethod
    def tearDownClass(cls):
        cls.tp.cleanup()

    def run_hook(self, cfg):
        fake = FakeJev(route={"coding/bugs-debugging": 0.8},
                       pick=lambda crit: {k: 0.6 for k, v in crit.items() if "backup" in v.lower()})
        return hook.run(cfg, {"prompt": "did the nightly backup run fine after the path fix?"}, client=fake,
                        clock=FakeClock(), wall=lambda: 1_000_000.0, today=TODAY)

    def test_injection_carries_raw_pointer(self):
        out = self.run_hook(self.tp.cfg())
        self.assertIn("# Related memory (auto-recall, Jev", out)
        self.assertIn("↳ raw: jevmem raw 2026-09-10 09:34", out)
        self.assertIn("↳ raw: jevmem raw 2026-09-10 09:04", out)
        self.assertNotIn("Dana", out)

    def test_section_mode_through_the_hook(self):
        out = self.run_hook(self.tp.cfg({"recall": {"expand": "section"}}))
        self.assertIn("Dana said", out, "whole 09:34 section injected")
        self.assertEqual(out.count("## 09:34 summary"), 1)

    def test_fts_fallback_gets_the_pointer_too(self):
        cfg = self.tp.cfg({"fallback": {"bm25_threshold": 0.0}})
        fake = FakeJev(error=RuntimeError("down"))
        out = hook.run(cfg, {"prompt": "nightly backup cron absolute path"}, client=fake, clock=FakeClock(),
                       wall=lambda: 1_000_000.0, today=TODAY)
        self.assertIn("FTS5 fallback", out)
        self.assertIn("↳ raw: jevmem raw 2026-09-10", out)


class TestDigestSpeakersAndCuts(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        self.cfg = self.tp.cfg({"digest": {"index_after": False}})
        self.now = datetime(2026, 9, 10, 9, 34)

    def tearDown(self):
        self.tp.cleanup()

    def run_d(self, cfg=None):
        fake = FakeLLM('{"topics": ["ok"]}')
        digest.run(cfg or self.cfg, backend=fake, now=self.now, out=lambda *_: None)
        return fake.prompts[0]

    def test_prompt_rules(self):
        capture.inbound(self.cfg, {"prompt": tg("should we move the demo to friday?", "1")})
        p = self.run_d()
        for rule in ("Keep the status exact", "is planned, not done", "question or proposal, not a decision",
                     "Keep negations", "same notation", "never swap which number",
                     "no topic is left out", "Never credit the user's idea to the assistant",
                     "keep each person separate", "was cut off; do not guess",
                     "check every item against the lines it came from", '{"topics": [',
                     "[11:15] Assistant (script): PR #412 was merged."):
            self.assertIn(rule, p)
        for placeholder in ("{user_label}", "{assistant_label}", "{script_label}", "{truncation_marker}"):
            self.assertNotIn(placeholder, p)
        self.assertIn('ending in "\u2026(truncated)" was cut off', p)

    def test_script_messages_and_named_senders_are_labeled(self):
        capture.inbound(self.cfg, {"prompt": tg("team dinner budget is 1,200", "1", user="dana")})
        capture.inbound(self.cfg, {"prompt": tg("no, 1,500 for the venue", "2", user="lee")})
        capture.outbound(self.cfg, reply_payload("Noted both numbers."))
        rec = capture.log_out(self.cfg, "Alert: PR #412 was merged", channel="telegram")
        self.assertEqual(rec["via"], "script")
        p = self.run_d()
        self.assertIn("User (dana): team dinner budget is 1,200", p)
        self.assertIn("User (lee): no, 1,500 for the venue", p)
        self.assertIn("] Assistant: Noted both numbers.", p)
        self.assertIn("] Assistant (script): Alert: PR #412 was merged", p)

    def test_single_sender_has_no_name_and_option_off(self):
        capture.inbound(self.cfg, {"prompt": tg("first message here", "1", user="sam")})
        capture.inbound(self.cfg, {"prompt": tg("second message here", "2", user="sam")})
        self.assertIn("] User: first message here", self.run_d())
        cfg = self.tp.cfg({"digest": {"index_after": False, "name_speakers": False}})
        recs = [{"ts": "", "user": True, "name": "a", "text": "x"}, {"ts": "", "user": True, "name": "b", "text": "y"}]
        self.assertNotIn("(a)", digest.conversation_text(cfg, recs))

    def test_long_lines_marked_and_budget_keeps_whole_recent_lines(self):
        cfg = self.tp.cfg({"digest": {"max_line_chars": 50, "max_chars": 120}})
        recs = [{"ts": "", "user": True, "text": f"message number {i} " + "x" * 80} for i in range(5)]
        text = digest.conversation_text(cfg, recs)
        lines = text.splitlines()
        self.assertTrue(all(ln.endswith("…(truncated)") for ln in lines))
        self.assertIn("message number 4", lines[-1])
        self.assertNotIn("message number 0", text, "oldest lines dropped whole")
        self.assertTrue(lines[0].startswith("[--:--] User: message number"), "no half line at the top")
        short = digest.conversation_text(cfg, [{"ts": "", "user": False, "text": "short reply"}])
        self.assertEqual(short, "[--:--] Assistant: short reply")

    def test_defaults_send_nearly_full_messages(self):
        self.assertGreaterEqual(self.cfg["digest"]["max_line_chars"], 1000)
        capture.inbound(self.cfg, {"prompt": tg("a" * 900, "1")})
        self.assertIn("a" * 900, self.run_d())


if __name__ == "__main__":
    unittest.main()

"""Indexer (dry-run, incremental, budgets, billing) and hook (Jev path, smalltalk skip,
fallbacks, breaker) — all with a fake Jev client."""
import json
import sqlite3
from contextlib import closing
import unittest
from datetime import date
from pathlib import Path

from helpers import (DEFAULT_RULES, BillingError, FakeClock, FakeJev, JevError, JevTimeout,
                     TempProject, keyword_labeler, run_cli)

from jevmem import hook, indexer

TODAY = date(2026, 9, 10)


def labeled_project(extra_cfg=""):
    tp = TempProject(extra_cfg=extra_cfg)
    fake = FakeJev(labeler=keyword_labeler(DEFAULT_RULES))
    res = indexer.run(tp.cfg(), client_factory=lambda: fake, out=lambda *_: None)
    assert res["labeled"] > 0, res
    return tp


class TestIndex(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()
        self.out = []

    def tearDown(self):
        self.tp.cleanup()

    def db(self):
        return closing(sqlite3.connect(self.tp.cfg()["data"]["episodic_db"]))

    def test_dry_run_writes_nothing_and_calls_nothing(self):
        called = []
        res = indexer.run(self.tp.cfg(), client_factory=lambda: called.append(1), dry_run=True,
                          out=self.out.append)
        self.assertEqual(called, [])
        self.assertFalse(Path(self.tp.cfg()["data"]["episodic_db"]).exists())
        self.assertGreater(res["to_label_this_run"], 0)
        self.assertGreater(res["est_jev_cost_usd_all_pending"], 0)
        self.assertIn("est_jev_cost_usd_this_run", json.loads(self.out[0]))

    def test_incremental_labeling(self):
        fake = FakeJev(labeler=keyword_labeler(DEFAULT_RULES))
        r1 = indexer.run(self.tp.cfg(), client_factory=lambda: fake, out=self.out.append)
        self.assertEqual(r1["labeled"], r1["units"])
        n_calls = len(fake.calls)
        r2 = indexer.run(self.tp.cfg(), client_factory=lambda: fake, out=self.out.append)
        self.assertTrue(r2.get("noop"))
        self.assertEqual(len(fake.calls), n_calls, "unchanged logs -> no API calls")
        # change one file: only its new chunks get labeled, vanished ones are deleted
        p = self.tp.root / "memory" / "2026-09-08.md"
        p.write_text(p.read_text() + "\n## 23:00 check-in\n- Late note: the backup ran fine tonight after the fix.\n")
        r3 = indexer.run(self.tp.cfg(), client_factory=lambda: fake, out=self.out.append)
        self.assertEqual(r3["labeled"], r3["added"])
        self.assertGreaterEqual(r3["added"], 1)
        self.assertLessEqual(r3["added"], 2)

    def test_dry_run_after_labeling_uses_measured_average(self):
        fake = FakeJev(labeler=keyword_labeler(DEFAULT_RULES), in_tokens=5000)
        indexer.run(self.tp.cfg(), client_factory=lambda: fake, out=self.out.append)
        res = indexer.run(self.tp.cfg(), relabel=True, dry_run=True, out=self.out.append)
        self.assertGreaterEqual(res["to_label_total"], 10)
        self.assertIn("measured", res["jev_estimate_method"])
        self.assertEqual(res["est_jev_input_tokens_this_run"], 5000 * res["to_label_this_run"])

    def test_billing_error_stops_without_burning_retries(self):
        fake = FakeJev(error=BillingError("Jev HTTP 402: out of credit"))
        res = indexer.run(self.tp.cfg(), client_factory=lambda: fake, workers=1, out=self.out.append)
        self.assertTrue(res["billing_stop"])
        with self.db() as con:
            self.assertEqual(con.execute("select max(tries) from units").fetchone()[0], 0)
            self.assertEqual(con.execute("select count(*) from units where labeled_at is not null").fetchone()[0], 0)

    def test_other_errors_increment_tries(self):
        fake = FakeJev(error=JevError("boom"))
        indexer.run(self.tp.cfg(), client_factory=lambda: fake, workers=2, out=self.out.append)
        with self.db() as con:
            self.assertEqual(con.execute("select min(tries) from units").fetchone()[0], 1)

    def test_cost_limits_trim_run_and_yes_lifts_them(self):
        cfg = self.tp.cfg({"index": {"max_cost_usd_per_run": 0.000001}})
        fake = FakeJev(labeler=keyword_labeler(DEFAULT_RULES))
        res = indexer.run(cfg, client_factory=lambda: fake, out=self.out.append)
        self.assertEqual(res["labeled"], 0)
        self.assertGreater(res["trimmed_by_budget"], 0)
        self.assertEqual(fake.calls, [])
        res = indexer.run(cfg, client_factory=lambda: fake, yes=True, out=self.out.append)
        self.assertEqual(res["labeled"], res["units"])

    def test_daily_budget_counts_actual_spend(self):
        fake = FakeJev(labeler=keyword_labeler(DEFAULT_RULES), in_tokens=10_000_000)  # $0.42 per chunk
        cfg = self.tp.cfg({"index": {"max_cost_usd_per_day": 1.0, "max_cost_usd_per_run": 100.0}})
        # heuristic estimate is small, so the first run labels everything and records the spend
        res = indexer.run(cfg, client_factory=lambda: fake, out=self.out.append)
        self.assertGreater(res["cost_usd"], 1.0)
        p = self.tp.root / "memory" / "2026-09-08.md"
        p.write_text(p.read_text() + "\n## 23:00 check-in\n- Another note about the backup job being fine.\n")
        res = indexer.run(cfg, client_factory=lambda: fake, out=self.out.append)
        self.assertEqual(res["labeled"], 0, "today's spend already exceeds the daily limit")

    def test_relabel_topic_marks_only_that_topic(self):
        fake = FakeJev(labeler=keyword_labeler(DEFAULT_RULES))
        indexer.run(self.tp.cfg(), client_factory=lambda: fake, out=self.out.append)
        with self.db() as con:
            n_health = con.execute("select count(*) from units where top1='health' or top2='health'").fetchone()[0]
        before = len(fake.calls)
        res = indexer.run(self.tp.cfg(), client_factory=lambda: fake, relabel_topic="health", out=self.out.append)
        self.assertEqual(res["marked_stale"], n_health)
        self.assertEqual(len(fake.calls) - before, n_health)
        with self.db() as con:
            self.assertEqual(con.execute("select count(*) from units where stale=1").fetchone()[0], 0)


class TestHook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tp = labeled_project()

    @classmethod
    def tearDownClass(cls):
        cls.tp.cleanup()

    def setUp(self):
        cfg = self.tp.cfg()
        for key in ("breaker", "log"):
            Path(cfg["data"][key]).unlink(missing_ok=True)

    def run_hook(self, prompt, fake, clock=None, cfg=None):
        clock = clock or FakeClock()
        return hook.run(cfg or self.tp.cfg(), {"prompt": prompt}, client=fake, clock=clock,
                        wall=lambda: 1_000_000.0, today=TODAY)

    def last_log(self):
        lines = Path(self.tp.cfg()["data"]["log"]).read_text().splitlines()
        return json.loads(lines[-1])

    def test_jev_path_injects_picked_chunks(self):
        def pick(crit):
            return {k: 0.6 for k, v in crit.items() if "backup" in v.lower()}
        fake = FakeJev(route={"coding/bugs-debugging": 0.8}, pick=pick)
        out = self.run_hook("Why did the nightly backup job fail last week?", fake)
        self.assertIn("# Related memory (auto-recall, Jev", out)
        self.assertIn("relative path", out)
        self.assertEqual(len(fake.calls), 2)
        log = self.last_log()
        self.assertFalse(log["fallback"])
        self.assertEqual(log["route"][0][1], "coding/bugs-debugging")
        self.assertGreaterEqual(log["n_injected"], 1)

    def test_smalltalk_injects_nothing(self):
        fake = FakeJev(route={"smalltalk/greetings-thanks": 0.9})
        out = self.run_hook("thanks a lot, good night!", fake)
        self.assertEqual(out, "")
        self.assertEqual(len(fake.calls), 1, "route only; no pick call")
        self.assertTrue(self.last_log()["smalltalk"])

    def test_low_probability_picks_inject_nothing(self):
        fake = FakeJev(route={"coding/bugs-debugging": 0.8}, pick=lambda crit: {k: 0.05 for k in crit})
        out = self.run_hook("Why did the nightly backup job fail last week?", fake)
        self.assertNotIn("# Related memory", out)

    def test_short_and_skip_prefix_messages_are_ignored(self):
        fake = FakeJev()
        self.assertEqual(self.run_hook("ok", fake), "")
        self.assertEqual(self.run_hook("<task-notification>agent finished the backup analysis</task-notification>", fake), "")
        self.assertEqual(fake.calls, [])

    def test_jev_error_falls_back_to_fts(self):
        fake = FakeJev(error=JevError("Jev HTTP 500: oops"))
        cfg = self.tp.cfg({"fallback": {"bm25_threshold": 0.0}})
        out = self.run_hook("nightly backup relative path", fake, cfg=cfg)
        self.assertIn("FTS5 fallback", out)
        self.assertIn("backup", out)
        self.assertTrue(self.last_log()["fallback"])

    def test_timeout_over_deadline_falls_back(self):
        clock = FakeClock()
        fake = FakeJev(route={"coding/bugs-debugging": 0.8}, pick=lambda c: {k: 0.9 for k in c},
                       latency=2.5, clock=clock)
        cfg = self.tp.cfg({"fallback": {"bm25_threshold": 0.0}})
        out = self.run_hook("nightly backup relative path", fake, clock=clock, cfg=cfg)
        self.assertIn("FTS5 fallback", out)
        self.assertNotIn("auto-recall, Jev", out)
        self.assertIn("Deadline", self.last_log()["error"])

    def test_client_timeout_falls_back(self):
        fake = FakeJev(error=JevTimeout("Jev timeout after 3.85s"))
        cfg = self.tp.cfg({"fallback": {"bm25_threshold": 0.0}})
        self.assertIn("FTS5 fallback", self.run_hook("nightly backup relative path", fake, cfg=cfg))

    def test_billing_error_opens_breaker(self):
        cfg = self.tp.cfg({"fallback": {"bm25_threshold": 0.0}})
        fake = FakeJev(error=BillingError("Jev HTTP 402: payment required"))
        self.run_hook("nightly backup relative path", fake, cfg=cfg)
        self.assertTrue(Path(cfg["data"]["breaker"]).exists())
        fake2 = FakeJev(route={"coding/bugs-debugging": 0.8})
        out = self.run_hook("nightly backup relative path", fake2, cfg=cfg)
        self.assertEqual(fake2.calls, [], "breaker open -> Jev is not called")
        self.assertIn("FTS5 fallback", out)

    def test_profile_block_comes_first(self):
        cfg = self.tp.cfg()
        r = run_cli("profile", "new", "--kind", "preference", "--statement",
                    "Prefers short bullet summaries over long paragraphs",
                    "--source", "memory/2026-09-02.md", "--quote", "short bullet summaries", "--commit",
                    cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        try:
            fake = FakeJev(route={"coding/bugs-debugging": 0.8},
                           pick=lambda crit: {k: 0.6 for k, v in crit.items() if "backup" in v.lower()})
            out = self.run_hook("Why did the nightly backup job fail last week?", fake, cfg=cfg)
            self.assertLess(out.index("# About the user"), out.index("# Related memory"))
        finally:
            Path(cfg["data"]["profile_db"]).unlink()


class TestHookUnlabeled(unittest.TestCase):
    def test_no_labeled_chunks_skips_jev_and_uses_fts(self):
        tp = TempProject(extra_cfg="[fallback]\nbm25_threshold = 0.0\n")
        try:
            indexer.run(tp.cfg(), no_label=True, out=lambda *_: None)
            fake = FakeJev(route={"coding/bugs-debugging": 0.9})
            out = hook.run(tp.cfg(), {"prompt": "nightly backup relative path"}, client=fake,
                           clock=FakeClock(), today=TODAY)
            self.assertEqual(fake.calls, [], "no labels -> don't pay for a routing call")
            self.assertIn("FTS5 fallback", out)
        finally:
            tp.cleanup()


class TestHookCli(unittest.TestCase):
    """Real entry point in a subprocess. No key is configured and the URL is a closed local
    port, so the real API is unreachable by construction."""

    @classmethod
    def setUpClass(cls):
        cls.tp = labeled_project(extra_cfg="[fallback]\nbm25_threshold = 0.0\n")

    @classmethod
    def tearDownClass(cls):
        cls.tp.cleanup()

    def test_no_key_falls_back_and_exits_zero(self):
        r = run_cli("hook", cfg_path=self.tp.cfg_path,
                    stdin=json.dumps({"prompt": "nightly backup relative path"}))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")
        self.assertIn("FTS5 fallback", r.stdout)
        log = Path(self.tp.cfg()["data"]["log"]).read_text().splitlines()[-1]
        self.assertIn("No Jev API key", log)

    def test_garbage_stdin_exits_zero(self):
        for payload in ("not json", "[]", json.dumps({"prompt": 42}), ""):
            r = run_cli("hook", cfg_path=self.tp.cfg_path, stdin=payload)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")
            self.assertEqual(r.stderr, "")

    def test_index_dry_run_cli(self):
        r = run_cli("index", "--dry-run", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("est_jev_cost_usd_this_run", r.stdout)
        self.assertIn("est_haiku_calls_this_run", r.stdout)


if __name__ == "__main__":
    unittest.main()

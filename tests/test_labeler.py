"""Haiku batch labeling: hierarchical parser, p mapping, batching, retries and Jev fallback,
and the claude→ollama text-backend chain. Fakes only."""
import json
import re
import sqlite3
import unittest
from contextlib import closing
from datetime import date

from helpers import DEFAULT_RULES, EXAMPLES, FakeJev, TempProject, keyword_labeler

from jevmem import indexer, labeler, llm, recall

TAX = json.loads((EXAMPLES / "taxonomy.example.json").read_text(encoding="utf-8"))
ITEM_RE = re.compile(r'<item id="(c\d+)">(.*?)</item>', re.DOTALL)


class FakeHaiku(llm.Backend):
    """Answers a labeling prompt with keyword rules; can return empty answers (unusable,
    counted against the chunks) or drop items. Outages (failed `claude -p` calls, which are
    NOT counted) are tested in test_round2.TestCallBreaker."""
    name = "fake-haiku"

    def __init__(self, fail_calls=0, drop=(), second=None):
        self.fail_calls = fail_calls
        self.drop = set(drop)
        self.second = second
        self.prompts = []
        self.lab = keyword_labeler(DEFAULT_RULES)

    def complete(self, prompt):
        self.prompts.append(prompt)
        if len(self.prompts) <= self.fail_calls:
            return None
        labels = {}
        for cid, text in ITEM_RE.findall(prompt):
            if any(d in text for d in self.drop):
                continue
            top, sub = self.lab(text)
            labels[cid] = {"top": top, "sub": sub, "second": self.second}
        return "Here you go:\n" + json.dumps({"labels": labels})


class TestParser(unittest.TestCase):
    def test_hierarchical_validation_and_p_mapping(self):
        text = json.dumps({"labels": {
            "c1": {"top": "work", "sub": "meetings", "second": None},
            "c2": {"top": "work", "sub": "bugs-debugging"},                      # sub of another topic
            "c3": {"top": "coding", "sub": "coding/code-review-deploy",
                   "second": {"top": "work", "sub": "tasks-deadlines"}},
            "c4": {"top": "nope", "sub": "meetings"},
            "c5": {"top": "health", "sub": "medical",
                   "second": {"top": "health", "sub": "medical"}},              # same as first: dropped
            "c6": {"top": "health", "sub": "medical",
                   "second": {"top": "work", "sub": "games"}},                  # invalid second: ignored
            "c7": {"top": "health", "sub": "sleep-energy",
                   "second": {"top": "health", "sub": "medical"}},              # same top, other sub
        }})
        got = labeler.parse_labels(TAX, text, [f"c{i}" for i in range(1, 9)])
        self.assertEqual(got["c1"], ("work", 0.9, None, None, "work/meetings", 0.9, None, None))
        self.assertNotIn("c2", got, "a subtopic outside the chosen top-level topic is rejected")
        self.assertEqual(got["c3"], ("coding", 0.9, "work", 0.3, "coding/code-review-deploy", 0.9,
                                     "work/tasks-deadlines", 0.3))
        self.assertNotIn("c4", got)
        self.assertEqual(got["c5"][2:], (None, None, "health/medical", 0.9, None, None))
        self.assertEqual(got["c6"][6:], (None, None))
        self.assertEqual(got["c7"], ("health", 0.9, None, None, "health/sleep-energy", 0.9,
                                     "health/medical", 0.3))
        self.assertNotIn("c8", got, "missing ids are simply absent")
        self.assertEqual(labeler.parse_labels(TAX, "garbage", ["c1"]), {})

    def test_clean_chunk_strips_repeated_digest_heading(self):
        split = r"^#{1,4}\s*\d{1,2}:\d{2}\s+summary\b"
        self.assertEqual(labeler.clean_chunk("## 09:34 summary\n- The backup job was fixed.", split),
                         "- The backup job was fixed.")
        self.assertEqual(labeler.clean_chunk("## Notes\n- keep this heading", split), "## Notes - keep this heading")

    def test_prompt_is_hierarchical_and_lists_items(self):
        p = labeler.build_prompt(TAX, [("c1", "first"), ("c2", "second")])
        self.assertIn("ONE subtopic that belongs to THAT top-level topic", p)
        self.assertIn("[work] ", p)
        self.assertIn("  - meetings: ", p)
        self.assertIn('<item id="c2">second</item>', p)

    def test_recall_thresholds_accept_haiku_second_choice(self):
        u = {"hash": "a", "relpath": "x", "date": "2026-09-01", "chunk": "c",
             "top1": "coding", "top1_p": 0.9, "top2": "work", "top2_p": 0.3,
             "sub1": "coding/code-review-deploy", "sub1_p": 0.9, "sub2": "work/tasks-deadlines", "sub2_p": 0.3}
        from jevmem import config
        from pathlib import Path
        cfg = config.from_dict({"recall": {"pool_min": 0}}, Path("."))
        cands, _ = recall.build_pool(cfg, [u], [("work", "work/tasks-deadlines", 0.8)], [], date(2026, 9, 10))
        self.assertEqual(len(cands), 1)


class TestHaikuIndexing(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject()
        self.cfg = self.tp.cfg({"index": {"labeler": "haiku", "batch_size": 5}})
        self.out = []

    def tearDown(self):
        self.tp.cleanup()

    def rows(self, sql):
        with closing(sqlite3.connect(self.cfg["data"]["episodic_db"])) as con:
            return con.execute(sql).fetchall()

    def test_dry_run_shows_haiku_calls_without_calling(self):
        fake = FakeHaiku()
        res = indexer.run(self.cfg, dry_run=True, labeler_backend=fake, out=self.out.append)
        self.assertEqual(fake.prompts, [])
        self.assertEqual(res["labeler"], "haiku")
        self.assertEqual(res["est_haiku_calls_this_run"], -(-res["haiku_chunks_this_run"] // 5))
        self.assertEqual(res["est_jev_cost_usd_this_run"], 0.0)

    def test_batches_store_labeler_and_fixed_probabilities(self):
        fake = FakeHaiku()
        res = indexer.run(self.cfg, labeler_backend=fake, jev_available=False, out=self.out.append)
        n = res["units"]
        self.assertEqual(res["haiku_labeled"], n)
        self.assertEqual(res["haiku_calls"], -(-n // 5))
        self.assertEqual(len(fake.prompts), res["haiku_calls"])
        items = "".join(p.split("Items:")[1] for p in fake.prompts)
        self.assertNotIn("check-in", items, "repeated time headings are stripped before sending")
        self.assertEqual(self.rows("select distinct labeler, top1_p, sub1_p from units"), [("haiku", 0.9, 0.9)])
        self.assertTrue(all(sub.startswith(top + "/") for top, sub in self.rows("select top1, sub1 from units")))
        again = indexer.run(self.cfg, labeler_backend=fake, jev_available=False, out=self.out.append)
        self.assertTrue(again.get("noop"), "no relabeling without an explicit flag")

    def test_failed_batches_retry_then_fall_back_to_jev(self):
        # every Haiku answer is empty (unusable for every chunk)
        dead = FakeHaiku(fail_calls=10_000)
        jev = FakeJev(labeler=keyword_labeler(DEFAULT_RULES))
        r1 = indexer.run(self.cfg, labeler_backend=dead, client_factory=lambda: jev, jev_available=True,
                         out=self.out.append)
        self.assertEqual((r1["labeled"], r1["jev_labeled"]), (0, 0))
        self.assertEqual(self.rows("select min(hfail), max(hfail) from units"), [(1, 1)])
        self.assertEqual(jev.calls, [], "first failure: retry with Haiku next run")
        r2 = indexer.run(self.cfg, labeler_backend=dead, client_factory=lambda: jev, jev_available=True,
                         out=self.out.append)
        self.assertEqual(r2["labeled"], 0)
        self.assertEqual(self.rows("select min(hfail) from units"), [(2,)])
        r3 = indexer.run(self.cfg, labeler_backend=dead, client_factory=lambda: jev, jev_available=True,
                         out=self.out.append)
        self.assertEqual(r3["jev_labeled"], r3["units"], "after 2 Haiku failures, Jev labels them")
        self.assertEqual(self.rows("select distinct labeler, hfail from units"), [("jev", 0)])

    def test_without_jev_key_haiku_gives_up_after_max_tries(self):
        cfg = self.tp.cfg({"index": {"labeler": "haiku", "batch_size": 5, "max_tries": 3}})
        dead = FakeHaiku(fail_calls=10_000)
        results = [indexer.run(cfg, labeler_backend=dead, jev_available=False, out=self.out.append)
                   for _ in range(3)]
        n = results[0]["units"]
        self.assertEqual(self.rows("select min(hfail), max(hfail), min(tries), max(tries) from units"),
                         [(3, 3, 3, 3)], "Haiku failures count toward max_tries")
        self.assertEqual([r["gave_up"] for r in results], [0, 0, n])
        self.assertEqual(results[-1]["pending_next_run"], 0)
        self.assertTrue(any("gave up on" in str(o) and "--retry-failed" in str(o) for o in self.out))
        self.assertTrue(all("gave up after 3 failed attempts" in e for (e,) in self.rows("select err from units")))
        calls = len(dead.prompts)
        for _ in range(3):  # later scheduled runs: no claude -p call at all
            res = indexer.run(cfg, labeler_backend=dead, jev_available=False, out=self.out.append)
            self.assertTrue(res.get("noop"), res)
        self.assertEqual(len(dead.prompts), calls, "given-up chunks are not retried automatically")
        self.assertEqual(indexer.stats(cfg)["gave_up"], n)
        # a new chunk still gets its own attempts
        p = self.tp.root / "memory" / "2026-09-08.md"
        p.write_text(p.read_text() + "\n## 23:00 check-in\n- Late note: the backup ran fine tonight after the fix.\n")
        indexer.run(cfg, labeler_backend=dead, jev_available=False, out=self.out.append)
        self.assertEqual(len(dead.prompts), calls + 1)
        # --retry-failed tries everything again, and a working labeler fixes it
        res = indexer.run(cfg, labeler_backend=FakeHaiku(), jev_available=False, retry_failed=True,
                          out=self.out.append)
        self.assertEqual(res["labeled"], res["units"])
        self.assertEqual(indexer.stats(cfg)["gave_up"], 0)

    def test_dry_run_respects_max_tries(self):
        cfg = self.tp.cfg({"index": {"labeler": "haiku", "batch_size": 5, "max_tries": 1}})
        indexer.run(cfg, labeler_backend=FakeHaiku(fail_calls=10_000), jev_available=False, out=self.out.append)
        self.assertEqual(indexer.run(cfg, dry_run=True, out=self.out.append)["to_label_total"], 0)
        self.assertGreater(indexer.run(cfg, dry_run=True, retry_failed=True, out=self.out.append)["to_label_total"], 0)

    def test_stale_relabel_failures_are_capped_too(self):
        cfg = self.tp.cfg({"index": {"labeler": "haiku", "batch_size": 5, "max_tries": 2}})
        indexer.run(cfg, labeler_backend=FakeHaiku(), jev_available=False, out=self.out.append)
        dead = FakeHaiku(fail_calls=10_000)
        indexer.run(cfg, relabel=True, labeler_backend=dead, jev_available=False, out=self.out.append)
        res = indexer.run(cfg, labeler_backend=dead, jev_available=False, out=self.out.append)
        self.assertEqual(res["gave_up"], res["units"])
        calls = len(dead.prompts)
        self.assertTrue(indexer.run(cfg, labeler_backend=dead, jev_available=False, out=self.out.append).get("noop"))
        self.assertEqual(len(dead.prompts), calls)
        self.assertEqual(self.rows("select count(*) from units where labeled_at is null"), [(0,)],
                         "a stale chunk that can't be relabeled keeps its old label")

    def test_partial_batch_failure_only_counts_missing_chunks(self):
        fake = FakeHaiku(drop=("backup",))
        res = indexer.run(self.cfg, labeler_backend=fake, jev_available=False, out=self.out.append)
        failed = self.rows("select count(*) from units where hfail = 1")[0][0]
        self.assertEqual(res["failed"], failed)
        self.assertGreater(failed, 0)
        self.assertEqual(res["haiku_labeled"] + failed, res["units"])
        self.assertEqual(self.rows("select count(*) from units where labeled_at is not null and hfail > 0"), [(0,)])


class TestBackendChain(unittest.TestCase):
    class Fixed(llm.Backend):
        def __init__(self, name, reply):
            self.name, self.reply, self.calls = name, reply, 0

        def complete(self, prompt):
            self.calls += 1
            return self.reply

    def test_primary_then_fallback(self):
        a, b = self.Fixed("claude", None), self.Fixed("ollama", '{"topics": ["x"]}')
        chain = llm.ChainBackend([a, b])
        self.assertEqual(chain.complete("p"), '{"topics": ["x"]}')
        self.assertEqual((a.calls, b.calls, chain.last), (1, 1, "ollama"))
        a.reply = "from claude"
        self.assertEqual(chain.complete("p"), "from claude")
        self.assertEqual(b.calls, 1, "fallback not called when the primary answers")

    def test_get_backend_composition(self):
        tp = TempProject()
        try:
            c = tp.cfg({"llm": {"backend": "claude", "fallback": "ollama"}})
            b = llm.get_backend(c)
            self.assertIsInstance(b, llm.ChainBackend)
            self.assertEqual([x.name for x in b.backends], ["claude", "ollama"])
            self.assertIsInstance(llm.get_backend(tp.cfg({"llm": {"backend": "claude", "fallback": "none"}})),
                                  llm.ClaudeBackend)
            self.assertIsInstance(llm.get_backend(tp.cfg()), llm.NoneBackend)
            from jevmem import config
            self.assertEqual((config.DEFAULTS["llm"]["backend"], config.DEFAULTS["llm"]["fallback"],
                              config.DEFAULTS["index"]["labeler"]), ("claude", "ollama", "haiku"))
        finally:
            tp.cleanup()


if __name__ == "__main__":
    unittest.main()

"""Candidate pooling, recency ordering, pick ordering, routing aggregation, context sources."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from helpers import EXAMPLES, FakeClock, FakeJev

from jevmem import config, recall

TODAY = date(2026, 9, 10)


def cfg(**recall_over):
    return config.from_dict({"recall": recall_over}, Path("."))


def unit(h, date_, sub1, p1=0.8, sub2=None, p2=0.1, chunk=None):
    top1 = sub1.split("/")[0]
    top2 = sub2.split("/")[0] if sub2 else None
    return {"hash": h, "relpath": f"{date_}.md", "date": date_, "chunk": chunk or f"chunk {h} text",
            "top1": top1, "top1_p": p1, "top2": top2, "top2_p": p2,
            "sub1": sub1, "sub1_p": p1, "sub2": sub2, "sub2_p": p2}


class TestPool(unittest.TestCase):
    def test_sub1_match_and_sub2_threshold(self):
        units = [unit("a", "2026-09-01", "work/meetings"),
                 unit("b", "2026-09-01", "coding/tools-setup", sub2="work/meetings", p2=0.15),
                 unit("c", "2026-09-01", "coding/tools-setup", sub2="work/meetings", p2=0.05),
                 unit("d", "2026-09-01", "health/medical")]
        c = cfg(pool_min=0)
        cands, info = recall.build_pool(c, units, [("work", "work/meetings", 0.9)], [], TODAY)
        self.assertEqual([x["hash"] for x in cands], ["a", "b"])
        self.assertEqual(info["pool"], 2)

    def test_top_level_fill_when_pool_small(self):
        units = [unit("a", "2026-09-01", "work/meetings"),
                 unit("b", "2026-09-01", "work/communication"),
                 unit("c", "2026-09-01", "health/medical")]
        cands, _ = recall.build_pool(cfg(pool_min=40), units, [("work", "work/meetings", 0.9)], [], TODAY)
        self.assertEqual([x["hash"] for x in cands], ["a", "b"], "sub match first, then top-level fill")
        cands, _ = recall.build_pool(cfg(pool_min=1), units, [("work", "work/meetings", 0.9)], [], TODAY)
        self.assertEqual([x["hash"] for x in cands], ["a"], "no fill when pool is big enough")

    def test_recency_reorders_but_never_drops(self):
        units = [unit("old", "2025-01-01", "work/meetings", p1=0.9),
                 unit("new", "2026-09-09", "work/meetings", p1=0.9)]
        cands, _ = recall.build_pool(cfg(pool_min=0), units, [("work", "work/meetings", 0.9)], [], TODAY)
        self.assertEqual([x["hash"] for x in cands], ["new", "old"])

    def test_recency_floor_keeps_old_sub_matches_above_recent_top_fills(self):
        # old sub match: (1+0.95) * >=0.5 = ~0.98 ; new top-level fill: 0.5*0.9 * ~1.0 = 0.45
        units = [unit("old", "2020-01-01", "work/meetings", p1=0.95),
                 unit("new", "2026-09-10", "work/communication", p1=0.9)]
        cands, _ = recall.build_pool(cfg(pool_min=40), units, [("work", "work/meetings", 0.9)], [], TODAY)
        self.assertEqual([c["hash"] for c in cands], ["old", "new"])

    def test_rare_words_go_first_and_common_words_do_not(self):
        units = [unit(f"u{i}", "2026-09-01", "work/meetings", chunk=f"common word entry {i}") for i in range(30)]
        units.append(unit("rare", "2024-01-01", "health/medical", chunk="The ZX-81 Invoice mentioned once"))
        cands, info = recall.build_pool(cfg(pool_min=0, rare_max_df=15), units,
                                        [("work", "work/meetings", 0.9)], ["invoice", "common"], TODAY)
        self.assertEqual(cands[0]["hash"], "rare", "case-insensitive rare hit is prepended")
        self.assertEqual(info["rare"], 1, "'common' appears in 30 chunks > rare_max_df")

    def test_cap(self):
        units = [unit(f"u{i}", "2026-09-01", "work/meetings") for i in range(150)]
        cands, info = recall.build_pool(cfg(pool_min=0, cap=100), units, [("work", "work/meetings", 0.9)], [], TODAY)
        self.assertEqual(len(cands), 100)
        self.assertEqual(info["pool"], 150)


class TestPick(unittest.TestCase):
    def test_threshold_max_and_recency_weight(self):
        byid = {"c0": unit("a", "2025-01-01", "work/meetings"),
                "c1": unit("b", "2026-09-09", "work/meetings"),
                "c2": unit("c", "2026-09-09", "work/meetings"),
                "c3": unit("d", "2026-09-09", "work/meetings")}
        probs = {"c0": 0.30, "c1": 0.28, "c2": 0.05, "c3": 0.12}
        order = recall.order_picks(cfg(max_picks=2), probs, byid, TODAY)
        # c0 old: 0.30*(0.7+0.3*~0.07)=~0.216 ; c1 new: 0.28*~1.0=~0.279 -> c1 first
        self.assertEqual(order, ["c1", "c0"])
        order = recall.order_picks(cfg(max_picks=4), probs, byid, TODAY)
        self.assertNotIn("c2", order, "p < pick_min_p is never injected")
        self.assertEqual(len(order), 3, "does not pad up to max_picks")

    def test_excerpt_strips_time_headings_and_markdown(self):
        c = unit("a", "2026-09-01", "work/meetings", chunk="## 09:00 check-in\n- **Decided** to ship `export`")
        self.assertEqual(recall.excerpt(cfg(), c), "[2026-09-01] - Decided to ship export")


class TestRoute(unittest.TestCase):
    def setUp(self):
        self.tax = json.loads((EXAMPLES / "taxonomy.example.json").read_text(encoding="utf-8"))

    def test_aggregates_to_top_level_and_adds_second_topic(self):
        fake = FakeJev(route={"work/meetings": 0.30, "work/communication": 0.25, "coding/bugs-debugging": 0.35})
        routed, tops = recall.route(cfg(), self.tax, fake, "s", recall.Deadline(4, FakeClock()), [])
        self.assertEqual(routed[0][:2], ("work", "work/meetings"))
        self.assertEqual(routed[1][:2], ("coding", "coding/bugs-debugging"))

    def test_second_topic_never_smalltalk(self):
        fake = FakeJev(route={"work/meetings": 0.6, "smalltalk/chitchat": 0.35})
        routed, _ = recall.route(cfg(), self.tax, fake, "s", recall.Deadline(4, FakeClock()), [])
        self.assertEqual([r[0] for r in routed], ["work"])


class TestFallbackThreshold(unittest.TestCase):
    def test_auto_scales_with_corpus_size(self):
        fb = cfg()["fallback"]
        self.assertAlmostEqual(recall.bm25_threshold(fb, 2000), -10.0)
        self.assertAlmostEqual(recall.bm25_threshold(fb, 50000), -10.0, msg="never stricter than the reference")
        small = recall.bm25_threshold(fb, 20)
        self.assertTrue(-10.0 < small < 0.0)
        self.assertEqual(recall.bm25_threshold(dict(fb, bm25_threshold=-4.5), 20), -4.5)


class TestContext(unittest.TestCase):
    def test_none_by_default(self):
        self.assertEqual(recall.prior_lines(cfg(), "hi"), [])

    def test_jsonl_source_drops_current_message(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "chat.jsonl"
            rows = [{"role": "user", "text": "how did the deploy go"},
                    {"role": "assistant", "text": "staging is green"},
                    {"dir": "in", "text": "and the status endpoint?"}]
            p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            c = config.from_dict({"context": {"source": "jsonl", "jsonl_path": str(p), "lines": 4}}, Path(td))
            lines = recall.prior_lines(c, "and the status endpoint?")
            self.assertEqual(lines, ["User: how did the deploy go", "Assistant: staging is green"])

    def test_claude_transcript_source(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            rows = [{"type": "user", "message": {"content": "check the backup job"}},
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": "it failed at 3am"}]}},
                    {"type": "system", "message": {"content": "ignored"}}]
            p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            c = config.from_dict({"context": {"source": "claude_transcript", "lines": 1}}, Path(td))
            self.assertEqual(recall.prior_lines(c, "why?", {"transcript_path": str(p)}),
                             ["Assistant: it failed at 3am"])


if __name__ == "__main__":
    unittest.main()

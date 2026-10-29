"""Iterative pick (+session exclusion) and rare-word normalisation: both opt-in, default off."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import FakeJev  # noqa: E402
from jevmem import config, recall  # noqa: E402
from jevmem.text import clean_tokens, date_token_regex, normalize_word, stem_english  # noqa: E402

TODAY = date(2026, 9, 10)


def cfg(**over):
    return config.from_dict({"recall": over}, Path("."))


def chunk(i, rel, text="x"):
    return {"hash": f"h{i}", "relpath": rel, "date": "2026-09-01", "chunk": text, "top1": "work", "top1_p": 0.9,
            "top2": None, "top2_p": None, "sub1": "work/meetings", "sub1_p": 0.9, "sub2": None, "sub2_p": None}


class TestStem(unittest.TestCase):
    def test_light_rules(self):
        for w, want in [("museums", "museum"), ("doctors", "doctor"), ("visited", "visit"), ("visiting", "visit"),
                        ("stopped", "stop"), ("galleries", "galler"), ("dishes", "dish")]:
            self.assertEqual(stem_english(w), want, w)

    def test_no_overstemming(self):
        for w in ["class", "bus", "this", "does", "news", "boxes", "bed", "ring", "sing"]:
            self.assertEqual(stem_english(w), w, w)

    def test_korean_never_uses_english_rules(self):
        self.assertEqual(normalize_word("회의를", english=True), "회의")
        self.assertEqual(normalize_word("museums회의가", english=True), "museums회의")
        self.assertEqual(clean_tokens("서울 박물관들을 방문했다", rare_normalize=True),
                         clean_tokens("서울 박물관들을 방문했다"))
        self.assertEqual(clean_tokens("은행에서 visited", rare_normalize=True)[:1], ["은행"])

    def test_default_off_is_legacy(self):
        q = "How many different museums did I visit in February?"
        self.assertEqual(clean_tokens(q), ["different", "museums", "visit", "february"])
        self.assertEqual(clean_tokens(q, rare_normalize=True), ["different", "museum", "visit", "february", "date:2"])

    def test_stopwords_checked_before_stemming(self):
        self.assertNotIn("doe", clean_tokens("what does it do", rare_normalize=True))


class TestDates(unittest.TestCase):
    def test_regex(self):
        rx = date_token_regex("date:2")
        self.assertEqual(rx.findall("on 2/8, 02/15 and 2/28."), ["2/8", "02/15", "2/28"])
        for no in ["12/5", "1/2 cup", "2023/2/8", "2/45", "22/3"]:
            self.assertFalse(rx.search(no), no)

    def test_pool_rare_date_hit(self):
        units = [chunk(i, "2026-09-01.md", "common entry") for i in range(30)]
        units.append(chunk("d", "2026-02-01.md", "took my niece to the museum on 2/8"))
        c = cfg(pool_min=0)
        cands, info = recall.build_pool(c, units, [("work", "work/meetings", 0.9)], ["date:2"], TODAY)
        self.assertEqual(info["rare"], 1)


def pick_fn(table):
    return lambda crit: {k: table[k] for k in crit if k in table}


class TestRounds(unittest.TestCase):
    def setUp(self):
        # c0,c1 same session s1; c2 session s2; c3 session s3
        self.cands = [chunk(0, "s1.md"), chunk(1, "s1.md"), chunk(2, "s2.md"), chunk(3, "s3.md")]
        self.dl = recall.Deadline(10)

    def run_rounds(self, table, **over):
        over.setdefault("pick_rounds", 2)
        client = FakeJev(pick=pick_fn(table))
        hits, rinfo = recall.pick_rounds(cfg(**over), client, self.dl, "state", self.cands, [], TODAY)
        return [c["hash"] for c in hits], rinfo, client

    def test_same_session_competes_without_flag(self):
        client = FakeJev(pick=pick_fn({"c0": 0.9, "c1": 0.06, "c2": 0.02, "c3": 0.02}))
        recall.pick_rounds(cfg(pick_rounds=2), client, self.dl, "s", self.cands, [], TODAY)
        self.assertEqual(sorted(client.calls[1]["questions"]["q"]["criteria"]), ["c1", "c2", "c3"])

    def test_exclude_picked_sessions(self):
        client = FakeJev(pick=pick_fn({"c0": 0.9, "c1": 0.06, "c2": 0.02, "c3": 0.02}))
        hits, rinfo = recall.pick_rounds(cfg(pick_rounds=2, exclude_picked_sessions=True), client, self.dl,
                                         "s", self.cands, [], TODAY)
        self.assertEqual(sorted(client.calls[1]["questions"]["q"]["criteria"]), ["c2", "c3"],
                         "c1 shares session s1 with the injected c0")
        self.assertNotIn("h1", [c["hash"] for c in hits])

    def test_pool_empty_ends_loop(self):
        cands = [chunk(0, "s1.md"), chunk(1, "s1.md")]
        client = FakeJev(pick=pick_fn({"c0": 0.95, "c1": 0.05}))
        hits, rinfo = recall.pick_rounds(cfg(pick_rounds=3, exclude_picked_sessions=True), client, self.dl,
                                         "s", cands, [], TODAY)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual([c["hash"] for c in hits], ["h0"])

    def test_round1_min_one_and_later_rounds_need_cut(self):
        table = {"c0": 0.05, "c1": 0.04, "c2": 0.03, "c3": 0.02}
        client = FakeJev(pick=pick_fn(table))
        hits, _ = recall.pick_rounds(cfg(pick_rounds=2), client, self.dl, "s", self.cands, [], TODAY)
        self.assertEqual(len(hits) >= 1, True)
        self.assertEqual(hits[0]["hash"] in {"h0", "h1", "h2", "h3"}, True)
        self.assertLessEqual(len(hits), 1 + 0, "FakeJev renormalises: nothing reaches the round-2 cut (0.3) except by chance")

    def test_cap_and_removal_rule(self):
        cands = [chunk(i, f"s{i}.md") for i in range(8)]
        client = FakeJev(pick=pick_fn({f"c{i}": 0.15 for i in range(6)}))
        hits, rinfo = recall.pick_rounds(cfg(pick_rounds=2, max_picks=3), client, self.dl, "s", cands, [], TODAY)
        self.assertEqual(len(hits), 3)
        self.assertEqual(len(client.calls), 1, "max_picks already reached: no second round")
        client = FakeJev(pick=pick_fn({f"c{i}": 0.15 for i in range(6)}))
        hits, rinfo = recall.pick_rounds(cfg(pick_rounds=2, max_picks=8), client, self.dl, "s", cands, [], TODAY)
        self.assertLessEqual(len(hits), 8)
        self.assertEqual(len(client.calls[1]["questions"]["q"]["criteria"]), 2, "p>=0.1 chunks removed after round 1")

    def test_default_is_single_pick(self):
        self.assertEqual(config.from_dict({}, Path("."))["recall"]["pick_rounds"], 1)
        self.assertFalse(config.from_dict({}, Path("."))["recall"]["exclude_picked_sessions"])
        self.assertFalse(config.from_dict({}, Path("."))["recall"]["rare_normalize"])


class TestIncludeToday(unittest.TestCase):
    def test_disabled_state_unchanged(self):
        c = cfg(include_today=False)
        self.assertEqual(recall.build_state(c, "hi", ["a"]), recall.build_state(c, "hi", ["a"], TODAY))
        self.assertNotIn("Today's date", recall.build_state(c, "hi", [], TODAY))

    def test_default_is_on(self):
        self.assertTrue(config.from_dict({}, Path("."))["recall"]["include_today"])
        self.assertIn("Today's date: 2026-09-10 (Thursday)", recall.build_state(cfg(), "hi", [], TODAY))

    def test_on_adds_one_neutral_line(self):
        c = cfg(include_today=True)
        s = recall.build_state(c, "last month?", [], date(2023, 5, 30))
        self.assertTrue(s.endswith("Today's date: 2023-05-30 (Tuesday)"))
        self.assertEqual(s.count("Today's date"), 1)
        self.assertNotIn("Today", recall.build_state(c, "x", []), "no today given -> no line")

    def test_jev_recall_passes_today_to_every_call(self):
        from helpers import FakeJev
        from jevmem import taxonomy as taxmod
        tax = taxmod.load(Path(__file__).resolve().parent.parent / "examples" / "taxonomy.example.json") if (Path(__file__).resolve().parent.parent / "examples" / "taxonomy.example.json").exists() else None
        if tax is None:
            self.skipTest("no example taxonomy")
        c = cfg(include_today=True)
        fake = FakeJev(route={})
        try:
            recall.jev_recall(c, tax, fake, lambda: [], "q about last week", ["q"], [], recall.Deadline(10), TODAY)
        except Exception:
            pass
        self.assertTrue(fake.calls and all("Today's date: 2026-09-10 (Thursday)" in call["state"] for call in fake.calls))


if __name__ == "__main__":
    unittest.main()

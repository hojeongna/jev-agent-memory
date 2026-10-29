import copy
import json
import unittest

from helpers import EXAMPLES  # noqa: F401  (sets sys.path)

from jevmem import taxonomy
from jevmem.chunking import chunk_text
from jevmem.text import clean_tokens, extract_query, message_text


class TestText(unittest.TestCase):
    def test_english_stopwords_and_possessive(self):
        self.assertEqual(clean_tokens("What did we decide about the team's deploy pipeline?"),
                         ["decide", "team", "deploy", "pipeline"])

    def test_korean_particles_and_stopwords(self):
        # invented sentence: "근데/지금/싶어서/말야" are stopwords, "화분이" -> "화분",
        # "그거보다" -> "그거" (stopword), "햇빛을" -> "햇빛", "더"/"받나" -> too short
        self.assertEqual(clean_tokens("근데 지금 베란다 화분이 그거보다 햇빛을 더 받나 싶어서 말야"),
                         ["베란다", "화분", "햇빛"])

    def test_dedupe_and_limit(self):
        toks = clean_tokens("alpha beta alpha gamma delta epsilon zeta theta iota kappa", limit=8)
        self.assertEqual(len(toks), 8)
        self.assertEqual(toks[0], "alpha")
        self.assertEqual(toks.count("alpha"), 1)

    def test_extract_query_strips_channel_urls_markdown(self):
        raw = '<channel source="x" chat_id="1">**Check** https://example.com/a the `deploy` log</channel>'
        self.assertEqual(message_text(raw), "**Check** https://example.com/a the `deploy` log")
        self.assertEqual(extract_query(raw), "Check the deploy log")


class TestChunking(unittest.TestCase):
    def test_heading_prefix_and_bullet_items(self):
        text = "# day\n\n## 09:00 check-in\n- first item that is long enough to keep around\n" \
               "- second item that is also long enough to keep\n"
        out = chunk_text(text, "2026-09-01.md")
        self.assertEqual(len(out), 1)
        rel, d, ch = out[0]
        self.assertEqual((rel, d), ("2026-09-01.md", "2026-09-01"))
        self.assertTrue(ch.startswith("## 09:00 check-in\n- first item"))
        self.assertIn("- second item", ch)

    def test_packs_up_to_max_chunk(self):
        items = "\n".join(f"- item {i} " + "x" * 100 for i in range(10))
        out = chunk_text("## h\n" + items, "2026-09-01.md", max_chunk=450)
        self.assertGreater(len(out), 1)
        for _, _, ch in out:
            body = ch.split("\n", 1)[1]
            self.assertLessEqual(len(body), 450)

    def test_hard_cut_and_min_drop(self):
        long_item = "- " + "y" * 2000
        out = chunk_text("## h\n" + long_item + "\n\n## tiny\n- short\n", "2026-09-01.md",
                         max_chunk=450, hard_cut=900, min_chunk=30)
        self.assertTrue(all(len(ch) <= 900 for _, _, ch in out))
        self.assertEqual(sum(len(ch) for _, _, ch in out), len("## h\n" + long_item))
        self.assertFalse(any("short" in ch for _, _, ch in out), "bodies < min_chunk are dropped")

    SPLIT = r"^#{1,4}\s*\d{1,2}:\d{2}\s+summary\b"

    def test_digest_section_keeps_one_chunk_per_bullet(self):
        text = ("# 2026-09-10 daily log\n\n## 09:34 summary\n"
                "- Backup job: cron used a relative path; fixed to absolute.\n"
                "- Restaurant for Friday booked.\n"
                "- ok\n"
                "\n## Notes\n"
                "- first ordinary note that is long enough to keep\n"
                "- second ordinary note that is long enough to keep\n")
        out = chunk_text(text, "2026-09-10.md", split_heading_re=self.SPLIT)
        chunks = [ch for _, _, ch in out]
        self.assertEqual(chunks[:2], ["## 09:34 summary\n- Backup job: cron used a relative path; fixed to absolute.",
                                      "## 09:34 summary\n- Restaurant for Friday booked."],
                         "each topic bullet is its own chunk, even when short")
        self.assertFalse(any("- ok" in c for c in chunks), "near-empty bullet dropped")
        self.assertEqual(len(chunks), 3, "other sections still pack bullets together")
        self.assertIn("first ordinary note", chunks[2])
        self.assertIn("second ordinary note", chunks[2])
        # without the option: old packing behaviour
        packed = chunk_text(text, "2026-09-10.md")
        self.assertIn("Restaurant", packed[0][2])
        self.assertIn("Backup job", packed[0][2])

    def test_default_config_enables_per_bullet_for_digest_sections(self):
        from helpers import TempProject
        from jevmem import indexer
        tp = TempProject(logs=False)
        try:
            (tp.root / "memory" / "2026-09-10.md").write_text(
                "## 09:34 summary\n- Topic one about the deploy pipeline.\n- Topic two about dinner plans.\n"
                "\n## 10:04 summary (raw)\n- [10:00] User: raw line one here\n- [10:01] Assistant: raw line two\n",
                encoding="utf-8")
            chunks = sorted(ch for _, _, ch in indexer.collect_chunks(tp.cfg()).values())
            self.assertEqual(len(chunks), 4)
        finally:
            tp.cleanup()

    def test_date_from_entries_when_filename_has_none(self):
        text = ("[2026-08-01] changed the backup script to use absolute paths everywhere\n"
                "[2026-08-03] tuned the memory hook threshold after noisy results appeared\n")
        out = chunk_text(text, "CHANGELOG.md")
        self.assertEqual([d for _, d, _ in out], ["2026-08-01", "2026-08-03"])

    def test_example_logs_chunk(self):
        from pathlib import Path
        from jevmem.chunking import chunk_file
        logs = EXAMPLES / "logs"
        total = sum(len(chunk_file(p, logs)) for p in sorted(Path(logs).glob("*.md")))
        self.assertGreaterEqual(total, 15)


class TestTaxonomy(unittest.TestCase):
    def setUp(self):
        self.tax = json.loads((EXAMPLES / "taxonomy.example.json").read_text(encoding="utf-8"))

    def test_example_is_valid(self):
        self.assertEqual(taxonomy.validate(self.tax), [])
        self.assertIn("smalltalk/chitchat", taxonomy.routing_criteria(self.tax))
        top, flat = taxonomy.labeling_criteria(self.tax)
        self.assertEqual(set(flat), set(taxonomy.routing_criteria(self.tax)))

    def test_packaged_copy_matches_example(self):
        from helpers import SRC
        packaged = json.loads((SRC / "jevmem" / "data" / "example_taxonomy.json").read_text(encoding="utf-8"))
        self.assertEqual(packaged, self.tax)

    def test_mismatched_names_rejected(self):
        t = copy.deepcopy(self.tax)
        t["routing"]["work"]["sub"]["renamed"] = t["routing"]["work"]["sub"].pop("meetings")
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.validate(t)

    def test_slash_in_name_rejected(self):
        t = copy.deepcopy(self.tax)
        for side in ("labeling", "routing"):
            t[side]["a/b"] = t[side].pop("health")
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.validate(t)

    def test_unknown_smalltalk_topic_rejected(self):
        t = copy.deepcopy(self.tax)
        t["smalltalk_topic"] = "nope"
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.validate(t)


if __name__ == "__main__":
    unittest.main()

"""Profile memory (ported from the original traits test suite).

Every DB lives in a temp dir. The CLI runs as a subprocess (`python -m jevmem profile ...`),
same as the daily consolidation job or an agent would call it.
"""
import concurrent.futures
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from helpers import SRC, TempProject, run_cli

TODAY = date.today().isoformat()
KINDS = ("preference", "pattern", "constraint", "skill", "relationship", "goal")
HEADER = "# About the user (profile memory)"
PROMPT = "backup relative path threshold tuning"


class _Case(unittest.TestCase):
    extra_cfg = "[profile]\ninject_line_width = 40\n\n[fallback]\nbm25_threshold = 0.0\n"

    def setUp(self):
        self.tp = TempProject(extra_cfg=self.extra_cfg)
        self.db_path = Path(self.tp.cfg()["data"]["profile_db"])
        r = self.cli("init")
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        c = getattr(self, "_conn", None)
        if c is not None:
            c.close()
        self.tp.cleanup()

    def cli(self, *args):
        return run_cli("profile", *args, cfg_path=self.tp.cfg_path)

    def hook(self, prompt):
        return run_cli("hook", cfg_path=self.tp.cfg_path, stdin=json.dumps({"prompt": prompt}))

    def conn(self):
        """One connection per test: several open ones leave write transactions that make the
        subprocess CLI fail with 'database is locked'."""
        if getattr(self, "_conn", None) is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def evidence_count(self, source_date=TODAY):
        return self.conn().execute("SELECT count(*) FROM evidence WHERE source_date = ?", (source_date,)).fetchone()[0]

    def make_trait(self, **kw):
        r = self.cli("new", "--kind", kw.get("kind", "preference"),
                     "--statement", kw.get("statement", "Prefers short text briefings over long meetings"),
                     "--source", kw.get("source", f"memory/{TODAY}.md"),
                     "--quote", kw.get("quote", "long meetings are not great"), "--commit")
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.conn().execute("SELECT max(id) FROM traits").fetchone()[0]


class TestSchema(_Case):
    def test_traits_has_no_emotion_column(self):
        cols = [r[1] for r in self.conn().execute("PRAGMA table_info(traits)")]
        for c in cols:
            self.assertNotIn("emotion", c.lower())
            self.assertNotIn("mood", c.lower())
        for required in ("kind", "statement", "confidence", "evidence_count", "first_seen",
                         "last_seen", "status", "superseded_by"):
            self.assertIn(required, cols)

    def test_kind_check_constraint_lists_exactly_six_kinds(self):
        sql = self.conn().execute("SELECT sql FROM sqlite_master WHERE name = 'traits'").fetchone()[0]
        for k in KINDS:
            self.assertIn(f"'{k}'", sql)
        self.assertRegex(sql, r"CHECK\s*\(\s*kind\s+IN\s*\(")

    def test_emotion_kind_is_rejected_by_integrity_error(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn().execute(
                "INSERT INTO traits(subject, kind, statement, confidence, evidence_count,"
                " first_seen, last_seen, status, created_at, updated_at)"
                " VALUES ('user','emotion','feels tired lately',0.3,1,?,?,'active',?,?)",
                (TODAY, TODAY, TODAY, TODAY))

    def test_evidence_has_nullable_emotion_column(self):
        info = {r[1]: r for r in self.conn().execute("PRAGMA table_info(evidence)")}
        self.assertIn("emotion", info)
        self.assertEqual(info["emotion"][3], 0, "emotion must be nullable")

    def test_source_never_aggregates_on_emotion(self):
        src = (SRC / "jevmem" / "profile.py").read_text(encoding="utf-8")
        for pattern in (r"GROUP\s+BY\s+.*emotion", r"ORDER\s+BY\s+.*emotion", r"COUNT\s*\(\s*.*emotion",
                        r"DISTINCT\s+emotion", r"WHERE\s+.*emotion\s*="):
            self.assertEqual(re.findall(pattern, src, re.IGNORECASE), [], pattern)


class TestWrites(_Case):
    def test_dry_run_is_the_default_and_writes_nothing(self):
        r = self.cli("new", "--kind", "preference", "--statement", "short briefings",
                     "--source", f"memory/{TODAY}.md", "--quote", "keep it short")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dry-run", r.stdout.lower())
        self.assertEqual(self.conn().execute("SELECT count(*) FROM traits").fetchone()[0], 0)

    def test_dry_run_does_not_create_the_db_file(self):
        tp = TempProject()
        try:
            db = Path(tp.cfg()["data"]["profile_db"])
            self.assertFalse(db.exists())
            for argv in (["new", "--kind", "goal", "--statement", "x", "--source", f"memory/{TODAY}.md",
                          "--quote", "q"], ["list"], ["stats"], ["candidates", "anything"]):
                r = run_cli("profile", *argv, cfg_path=tp.cfg_path)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertFalse(db.exists(), f"{argv[0]} created the DB")
        finally:
            tp.cleanup()

    def test_new_creates_trait_with_initial_confidence(self):
        tid = self.make_trait()
        row = self.conn().execute("SELECT * FROM traits WHERE id = ?", (tid,)).fetchone()
        self.assertAlmostEqual(row["confidence"], 0.30, places=6)
        self.assertEqual(row["evidence_count"], 1)
        self.assertEqual((row["first_seen"], row["last_seen"]), (TODAY, TODAY))
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["subject"], "user")
        self.assertIsNone(row["superseded_by"])

    def test_merge_increments_confidence_and_evidence(self):
        tid = self.make_trait()
        r = self.cli("merge", str(tid), "--source", f"memory/{TODAY}.md", "--quote", "said it again", "--commit")
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self.conn().execute("SELECT * FROM traits WHERE id = ?", (tid,)).fetchone()
        self.assertEqual(row["evidence_count"], 2)
        self.assertAlmostEqual(row["confidence"], 0.40, places=6)

    def test_merge_confidence_is_capped_at_one(self):
        tid = self.make_trait()
        self.conn().execute("UPDATE traits SET confidence = 1.0 WHERE id = ?", (tid,))
        self.conn().commit()
        self.cli("merge", str(tid), "--source", f"memory/{TODAY}.md", "--quote", "once more", "--commit")
        self.assertAlmostEqual(self.conn().execute("SELECT confidence FROM traits WHERE id = ?", (tid,)).fetchone()[0], 1.0)

    def test_supersede_inherits_confidence_and_preserves_old(self):
        tid = self.make_trait()
        self.conn().execute("UPDATE traits SET confidence = 0.60 WHERE id = ?", (tid,))
        self.conn().commit()
        r = self.cli("supersede", str(tid), "--statement", "Prefers short voice briefings",
                     "--source", f"memory/{TODAY}.md", "--quote", "voice would be better", "--commit")
        self.assertEqual(r.returncode, 0, r.stderr)
        old = self.conn().execute("SELECT * FROM traits WHERE id = ?", (tid,)).fetchone()
        self.assertEqual(old["status"], "superseded")
        new = self.conn().execute("SELECT * FROM traits WHERE id = ?", (old["superseded_by"],)).fetchone()
        self.assertAlmostEqual(new["confidence"], 0.60, places=6)
        self.assertEqual((new["evidence_count"], new["status"]), (1, "active"))
        self.assertGreaterEqual(self.conn().execute(
            "SELECT count(*) FROM evidence WHERE trait_id = ?", (tid,)).fetchone()[0], 1)

    def test_superseded_trait_is_excluded_from_injection(self):
        tid = self.make_trait(statement="judgment before replacement")
        self.cli("supersede", str(tid), "--statement", "replacement judgment", "--source",
                 f"memory/{TODAY}.md", "--quote", "why it changed", "--commit")
        out = self.cli("inject", "--preview").stdout
        self.assertIn("replacement judgment", out)
        self.assertNotIn("judgment before replacement", out)

    def test_new_does_not_treat_a_soft_deleted_trait_as_already_recorded(self):
        tid = self.make_trait(statement="to be deleted")
        self.cli("delete", str(tid), "--commit")
        r = self.cli("new", "--kind", "preference", "--statement", "to be deleted", "--source",
                     f"memory/{TODAY}.md", "--quote", "long meetings are not great", "--commit")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("already recorded", r.stdout)
        self.assertEqual(self.conn().execute(
            "SELECT count(*) FROM traits WHERE status='active' AND statement=?", ("to be deleted",)).fetchone()[0], 1)

    def test_merge_on_superseded_trait_is_rejected(self):
        tid = self.make_trait()
        self.conn().execute("UPDATE traits SET status = 'superseded' WHERE id = ?", (tid,))
        self.conn().commit()
        before = self.evidence_count()
        r = self.cli("merge", str(tid), "--source", f"memory/{TODAY}.md", "--quote", "revive", "--commit")
        self.assertEqual(r.returncode, 2)
        self.assertIn("superseded", r.stderr)
        self.assertEqual(self.evidence_count(), before)

    def test_supersede_on_deleted_trait_is_rejected(self):
        tid = self.make_trait()
        self.conn().execute("UPDATE traits SET status = 'deleted' WHERE id = ?", (tid,))
        self.conn().commit()
        before = self.conn().execute("SELECT count(*) FROM traits").fetchone()[0]
        r = self.cli("supersede", str(tid), "--statement", "revive", "--source", f"memory/{TODAY}.md",
                     "--quote", "q", "--commit")
        self.assertEqual(r.returncode, 2)
        self.assertIn("deleted", r.stderr)
        self.assertEqual(self.conn().execute("SELECT count(*) FROM traits").fetchone()[0], before)


class TestBudget(_Case):
    def fill_budget(self):
        tid = self.make_trait()
        for i in (2, 3):
            r = self.cli("merge", str(tid), "--source", f"memory/{TODAY}.md", "--quote", f"evidence {i}", "--commit")
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.evidence_count(), 3)
        return tid

    def test_fourth_write_is_rejected_with_exit_code_2(self):
        tid = self.fill_budget()
        r = self.cli("merge", str(tid), "--source", f"memory/{TODAY}.md", "--quote", "fourth", "--commit")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no budget left", r.stderr)
        self.assertEqual(self.evidence_count(), 3)

    def test_new_is_also_capped(self):
        self.fill_budget()
        r = self.cli("new", "--kind", "pattern", "--statement", "fourth trait", "--source",
                     f"memory/{TODAY}.md", "--quote", "q4", "--commit")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.evidence_count(), 3)

    def test_identical_rerun_is_idempotent(self):
        tid = self.make_trait()
        before = self.evidence_count()
        r = self.cli("merge", str(tid), "--source", f"memory/{TODAY}.md", "--quote",
                     "long meetings are not great", "--commit")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already recorded", r.stdout)
        self.assertEqual(self.evidence_count(), before)
        row = self.conn().execute("SELECT * FROM traits WHERE id = ?", (tid,)).fetchone()
        self.assertEqual(row["evidence_count"], 1)
        self.assertAlmostEqual(row["confidence"], 0.30, places=6)

    def test_cap_cannot_be_bypassed_by_varying_source_date(self):
        self.fill_budget()
        for src in ("memory/2026-08-01.md", "memory/2026-07-15.md"):
            r = self.cli("new", "--kind", "goal", "--statement", f"bypass {src}", "--source", src,
                         "--quote", "q", "--commit")
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.conn().execute("SELECT count(*) FROM evidence").fetchone()[0], 3)

    def test_backfill_uses_per_source_date_cap_only(self):
        self.fill_budget()
        r = self.cli("new", "--kind", "goal", "--statement", "old log insight", "--source",
                     "memory/2026-08-01.md", "--quote", "q", "--commit", "--backfill")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_cap_survives_concurrent_writes(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            futs = [ex.submit(self.cli, "new", "--kind", "pattern", "--statement", f"concurrent {i}",
                              "--source", f"memory/{TODAY}.md", "--quote", f"concurrent quote {i}", "--commit")
                    for i in range(5)]
            procs = [f.result() for f in futs]
        total = self.conn().execute("SELECT count(*) FROM evidence").fetchone()[0]
        self.assertLessEqual(total, 3, "\n".join(p.stdout + p.stderr for p in procs))
        self.assertEqual(sum(1 for p in procs if p.returncode == 0), total)

    def test_stats_reports_the_day_the_nightly_job_actually_wrote(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        r = self.cli("new", "--kind", "preference", "--statement", "yesterday's trait",
                     "--source", f"memory/{yesterday}.md", "--quote", "yesterday quote", "--commit")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.cli("stats").stdout
        self.assertIn(yesterday, out)
        self.assertIn("evidence 1,", out)


class TestCandidates(_Case):
    def test_empty_db_returns_empty_json_array(self):
        tp = TempProject()
        try:
            r = run_cli("profile", "candidates", "anything at all", cfg_path=tp.cfg_path)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout), [])
        finally:
            tp.cleanup()

    def test_jaccard_candidate_is_returned(self):
        tid = self.make_trait(statement="Prefers short text briefings over long meetings")
        data = json.loads(self.cli("candidates", "prefers short briefings over meetings").stdout)
        self.assertEqual(data[0]["id"], tid)
        self.assertGreaterEqual(data[0]["score"], 0.20)
        for key in ("id", "statement", "kind", "confidence", "score"):
            self.assertIn(key, data[0])

    def test_korean_jaccard_candidate(self):
        tid = self.make_trait(statement="긴 회의보다 짧은 텍스트 브리핑을 선호")
        data = json.loads(self.cli("candidates", "회의보다 짧은 브리핑을 선호한다").stdout)
        self.assertEqual(data[0]["id"], tid)
        self.assertGreaterEqual(data[0]["score"], 0.20)

    def test_substring_fallback_scores_below_jaccard_threshold(self):
        self.make_trait(statement="Sensitive to response delays and dislikes waiting long")
        data = json.loads(self.cli(
            "candidates", "response issue ticket filed assigned owner handled").stdout)
        self.assertTrue(data)
        self.assertAlmostEqual(data[0]["score"], 0.15, places=6)

    def test_superseded_traits_are_not_candidates(self):
        tid = self.make_trait()
        self.conn().execute("UPDATE traits SET status = 'superseded' WHERE id = ?", (tid,))
        self.conn().commit()
        self.assertEqual(json.loads(self.cli("candidates", "prefers short briefings over meetings").stdout), [])

    def test_at_most_five_candidates(self):
        for i in range(8):
            self.cli("new", "--kind", "pattern", "--statement", f"prefers short meeting briefings variant {i}",
                     "--source", f"memory/2026-0{i + 1}-01.md", "--quote", f"q{i}", "--commit", "--backfill")
        self.assertLessEqual(len(json.loads(self.cli("candidates", "prefers short meeting briefings").stdout)), 5)


class TestAudit(_Case):
    def test_list_has_fixed_pipe_columns(self):
        self.make_trait(statement="a very long statement " * 12)
        lines = [l for l in self.cli("list").stdout.splitlines() if l.strip()]
        self.assertEqual(lines[0], "id | conf | ev | kind | statement")
        cells = [c.strip() for c in lines[1].split("|")]
        self.assertEqual(len(cells), 5)
        self.assertLessEqual(len(cells[4]), 60)

    def test_pipe_in_statement_does_not_break_the_column_format(self):
        self.make_trait(statement="meetings short | documents long")
        lines = [l for l in self.cli("list").stdout.splitlines() if l.strip()]
        self.assertEqual(len(lines[1].split("|")), 5)

    def test_show_prints_full_statement_and_evidence(self):
        stmt = "a long judgment that must be shown in full " * 3
        tid = self.make_trait(statement=stmt, quote="verbatim source quote")
        out = self.cli("show", str(tid)).stdout
        self.assertIn(stmt.strip(), out)
        self.assertIn("verbatim source quote", out)
        self.assertIn(f"memory/{TODAY}.md", out)

    def test_delete_dry_run_changes_nothing(self):
        tid = self.make_trait()
        self.assertEqual(self.cli("delete", str(tid)).returncode, 0)
        self.assertEqual(self.conn().execute("SELECT status FROM traits WHERE id = ?", (tid,)).fetchone()[0], "active")

    def test_delete_is_soft_by_default(self):
        tid = self.make_trait()
        self.assertEqual(self.cli("delete", str(tid), "--commit").returncode, 0)
        self.assertEqual(self.conn().execute("SELECT status FROM traits WHERE id = ?", (tid,)).fetchone()[0], "deleted")
        self.assertEqual(self.conn().execute("SELECT count(*) FROM evidence WHERE trait_id = ?", (tid,)).fetchone()[0], 1)
        self.assertEqual([l for l in self.cli("list").stdout.splitlines()[1:] if l.strip()], [])
        body = [l for l in self.cli("list", "--include-deleted").stdout.splitlines()[1:] if l.strip()]
        self.assertIn("[deleted]", body[0])
        self.assertNotIn(HEADER, self.cli("inject").stdout)

    def test_hard_delete_requires_yes(self):
        tid = self.make_trait()
        self.assertNotEqual(self.cli("delete", str(tid), "--hard", "--commit").returncode, 0)
        self.assertEqual(self.conn().execute("SELECT count(*) FROM traits WHERE id = ?", (tid,)).fetchone()[0], 1)
        self.assertEqual(self.cli("delete", str(tid), "--hard", "--commit", "--yes").returncode, 0)
        self.assertEqual(self.conn().execute("SELECT count(*) FROM traits WHERE id = ?", (tid,)).fetchone()[0], 0)
        self.assertEqual(self.conn().execute("SELECT count(*) FROM evidence WHERE trait_id = ?", (tid,)).fetchone()[0], 0)

    def test_hard_delete_of_referenced_trait_fails_cleanly(self):
        old_id = self.make_trait()
        self.cli("supersede", str(old_id), "--statement", "changed", "--source", f"memory/{TODAY}.md",
                 "--quote", "new evidence", "--commit")
        new_id = self.conn().execute("SELECT superseded_by FROM traits WHERE id = ?", (old_id,)).fetchone()[0]
        r = self.cli("delete", str(new_id), "--hard", "--commit", "--yes")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn(str(old_id), r.stderr)


ALPHABET = "abcdefghijklmnopqrstuvwxyz"


def seed_traits(db_path: Path, spec, tie=False):
    """Insert active traits with unique 3-letter prefixes (so we can tell which were picked)."""
    conn = sqlite3.connect(db_path)
    conf, rows = 0.99, [(kind, i) for kind, n in spec.items() for i in range(n)]
    for pos, (kind, _) in enumerate(rows):
        value = round(conf, 4)
        if tie and pos == len(rows) - 1:
            value = round(conf + 0.011, 4)
        conn.execute("INSERT INTO traits(subject, kind, statement, confidence, evidence_count,"
                     " first_seen, last_seen, status, created_at, updated_at)"
                     " VALUES ('user', ?, ?, ?, 1, ?, ?, 'active', ?, ?)",
                     (kind, f"{ALPHABET[pos] * 3}{pos:02d} {kind} trait " + "z" * 60, value,
                      TODAY, TODAY, TODAY, TODAY))
        conf -= 0.011
    conn.commit()
    conn.close()


class TestInject(_Case):
    def setUp(self):
        super().setUp()
        r = run_cli("index", "--no-label", cfg_path=self.tp.cfg_path)  # FTS only, free
        self.assertEqual(r.returncode, 0, r.stderr)
        seed_traits(self.db_path, {"preference": 4, "pattern": 4, "constraint": 4,
                                   "skill": 3, "relationship": 3, "goal": 2})

    def trait_lines(self, out):
        lines = out.splitlines()
        i = lines.index(HEADER)
        body = []
        for line in lines[i + 1:]:
            if not line.strip():
                break
            body.append(line)
        return body

    def test_block_has_exactly_eight_lines_within_width(self):
        r = self.hook(PROMPT)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self.trait_lines(r.stdout)
        self.assertEqual(len(body), 8)
        for line in body:
            self.assertLessEqual(len(line), 40)
            self.assertTrue(line.endswith("…"))

    def test_top_two_per_kind_then_top_eight_by_confidence(self):
        body = [l for l in self.cli("inject", "--preview").stdout.splitlines()[1:] if l.strip()]
        self.assertEqual(len(body), 8)
        rows = self.conn().execute("SELECT id, kind, statement, confidence FROM traits WHERE status='active'").fetchall()
        by_kind = {}
        for r in sorted(rows, key=lambda r: (-r["confidence"], r["id"])):
            by_kind.setdefault(r["kind"], []).append(r)
        pool = [r for rs in by_kind.values() for r in rs[:2]]
        expected = sorted(pool, key=lambda r: (-r["confidence"], r["id"]))[:8]
        self.assertEqual([l[:5] for l in body], [r["statement"][:5] for r in expected])

    def test_ties_break_by_id_ascending(self):
        tp = TempProject()
        try:
            run_cli("profile", "init", cfg_path=tp.cfg_path)
            db = Path(tp.cfg()["data"]["profile_db"])
            seed_traits(db, {"preference": 1, "pattern": 1, "constraint": 1}, tie=True)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT id, confidence, statement FROM traits ORDER BY id").fetchall()
            conn.close()
            confs = [r["confidence"] for r in rows]
            self.assertEqual(len(set(confs)), len(confs) - 1)
            tied = sorted([r for r in rows if confs.count(r["confidence"]) > 1], key=lambda r: r["id"])
            body = [l for l in run_cli("profile", "inject", "--preview", cfg_path=tp.cfg_path).stdout.splitlines()[1:] if l.strip()]
            order = [next(i for i, l in enumerate(body) if l.startswith(t["statement"][:5])) for t in tied]
            self.assertEqual(order, sorted(order))
        finally:
            tp.cleanup()

    def test_contextual_match_comes_first(self):
        self.make_trait(kind="goal", statement="Wants the backup job to alert only on failure",
                        source="memory/2026-08-01.md")
        body = [l for l in self.cli("inject", "--preview", "--query", "backup job alert").stdout.splitlines()[1:] if l.strip()]
        self.assertTrue(body[0].startswith("Wants the backup"), body)

    def test_block_absent_when_no_active_traits(self):
        self.conn().execute("UPDATE traits SET status = 'deleted'")
        self.conn().commit()
        out = self.hook(PROMPT).stdout
        self.assertNotIn(HEADER, out)
        self.assertIn("# Related memory", out)

    def test_fewer_than_eight_traits_prints_what_exists(self):
        self.conn().execute("UPDATE traits SET status = 'deleted' WHERE id NOT IN (1, 2, 5)")
        self.conn().commit()
        self.assertEqual(len(self.trait_lines(self.hook(PROMPT).stdout)), 3)


class TestDegrade(unittest.TestCase):
    """Missing / corrupt / locked profile DB never breaks episodic injection."""

    def setUp(self):
        self.tp = TempProject(extra_cfg="[fallback]\nbm25_threshold = 0.0\n")
        r = run_cli("index", "--no-label", cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.db = Path(self.tp.cfg()["data"]["profile_db"])

    def tearDown(self):
        self.tp.cleanup()

    def assert_healthy(self):
        r = run_cli("hook", cfg_path=self.tp.cfg_path, stdin=json.dumps({"prompt": PROMPT}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.strip(), "")
        self.assertIn("# Related memory", r.stdout)
        self.assertNotIn(HEADER, r.stdout)

    def test_missing_profile_db(self):
        self.assertFalse(self.db.exists())
        self.assert_healthy()

    def test_corrupted_profile_db(self):
        run_cli("profile", "init", cfg_path=self.tp.cfg_path)
        seed_traits(self.db, {"preference": 2})
        raw = bytearray(self.db.read_bytes())
        raw[:16] = b"\x00" * 16
        self.db.write_bytes(bytes(raw))
        self.assert_healthy()

    def test_locked_profile_db(self):
        run_cli("profile", "init", cfg_path=self.tp.cfg_path)
        seed_traits(self.db, {"preference": 2})
        blocker = sqlite3.connect(self.db, isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            self.assert_healthy()
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()


if __name__ == "__main__":
    unittest.main()

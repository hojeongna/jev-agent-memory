"""Profile memory: "who the user is" — the only read/write gateway to the profile DB.

Episodic memory (the daily logs) records *what happened*. Profile memory holds
evidence-backed judgments about the user: each trait has a kind, a confidence, an
evidence count, first/last seen dates, and a status. Every write needs a verbatim quote
from a dated source. It grows slowly on purpose: at most `daily_cap` (default 3) pieces
of evidence per day.

DO NOT add an emotion column to `traits`.
    Emotions are states of a moment, not lasting attributes. A design that turned
    emotions into nodes collapsed: one "sadness" node absorbed dozens of entries and the
    whole profile became useless. Here emotion exists only as optional context on a piece
    of evidence (`evidence.emotion`) and is never grouped, sorted, counted or filtered on.
    A regression test statically checks this file for such queries.

Write subcommands (new / merge / supersede / delete) are DRY-RUN by default; pass
`--commit` to write. A dry-run never creates the DB file.

What the manual write commands check: the kind (one of KINDS), the daily budget, that a
merge/supersede target is an ACTIVE trait, and duplicate evidence. They also WARN (without
refusing) when --quote is not found verbatim in an existing --source file. The stricter
checks -- quote must be in the log, no mood/emotion statements, a near-duplicate "new"
becomes a merge -- are applied only by the automatic daily consolidation
(`consolidate.decide`), which then writes through these same commands.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

from . import fsutil, store
from .text import clean_tokens

KINDS = ("preference", "pattern", "constraint", "skill", "relationship", "goal")
STATUSES = ("active", "superseded", "deleted")
MAX_CONFIDENCE = 1.0

# Lexical similarity (no embedding model; zero extra dependencies).
JACCARD_MIN = 0.20
SUBSTRING_SCORE = 0.15  # below JACCARD_MIN so substring-only hits always rank lower
CANDIDATE_LIMIT = 5
LIST_STATEMENT_WIDTH = 60
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def schema(cfg) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS traits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    subject        TEXT    NOT NULL DEFAULT 'user',
    kind           TEXT    NOT NULL CHECK (kind IN {KINDS!r}),
    statement      TEXT    NOT NULL,
    confidence     REAL    NOT NULL DEFAULT {float(cfg['profile']['initial_confidence'])},
    evidence_count INTEGER NOT NULL DEFAULT 0,
    first_seen     TEXT    NOT NULL,
    last_seen      TEXT    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'active' CHECK (status IN {STATUSES!r}),
    superseded_by  INTEGER REFERENCES traits(id),
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trait_id    INTEGER NOT NULL REFERENCES traits(id) ON DELETE CASCADE,
    source_path TEXT    NOT NULL,
    source_date TEXT    NOT NULL,
    quote       TEXT    NOT NULL,
    emotion     TEXT,
    created_at  TEXT    NOT NULL,
    UNIQUE (trait_id, source_date, quote)
);

CREATE INDEX IF NOT EXISTS idx_traits_status_conf ON traits(status, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_trait     ON evidence(trait_id);
CREATE INDEX IF NOT EXISTS idx_evidence_date      ON evidence(source_date);
"""


# --------------------------------------------------------------------------- #
# common
# --------------------------------------------------------------------------- #
def today_str() -> str:
    return date.today().isoformat()


def _real_date(s: str) -> str | None:
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def source_date_of(source_path: str, fallback: str | None = None) -> str:
    """The evidence date: a YYYY-MM-DD in the source file's NAME (memory/2026-09-01.md), never
    one from a folder above it (a logs folder such as export-2025-01-01/ would otherwise give
    every piece of evidence the same date and use up that date's budget for good). No date
    in the name -> `fallback`, else today."""
    name = Path(str(source_path or "")).name
    for m in DATE_RE.finditer(name):
        d = _real_date(m.group(1))
        if d:
            return d
    return fallback or today_str()


def evidence_day(args) -> str:
    """`args.source_date` when the caller knows it (daily consolidation passes the log's
    date), else the date in the --source file name."""
    return getattr(args, "source_date", None) or source_date_of(args.source)


def db_path(cfg, args=None) -> Path:
    return Path(getattr(args, "db", None) or cfg["data"]["profile_db"])


def connect(path: Path, timeout: float = 5.0) -> sqlite3.Connection:
    fsutil.precreate(path)  # a new DB file is created 0600
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(cfg, conn) -> None:
    conn.executescript(schema(cfg))
    conn.commit()


def open_db(cfg, args, *, allow_create: bool) -> sqlite3.Connection:
    """If the file is missing and creation is not allowed (dry-run / read-only commands),
    return an in-memory DB with an empty schema so nothing is created on disk."""
    path = db_path(cfg, args)
    if not path.exists() and not allow_create:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_schema(cfg, conn)
        return conn
    conn = connect(path)
    init_schema(cfg, conn)
    return conn


def remaining_budget(cfg, conn, day: str, backfill: bool = False) -> int:
    """Remaining writes = the tighter of two caps.

    (1) per source_date: re-running the nightly job for the same day can't exceed the cap.
    (2) per wall-clock day (created_at): otherwise citing many different source dates
        would allow unlimited writes today. `backfill=True` skips (2) to allow processing
        old logs, while (1) still caps each source date.
    """
    cap = int(cfg["profile"]["daily_cap"])
    by_source = conn.execute("SELECT count(*) FROM evidence WHERE source_date = ?", (day,)).fetchone()[0]
    if backfill:
        return max(0, cap - by_source)
    by_wallclock = conn.execute("SELECT count(*) FROM evidence WHERE created_at = ?", (today_str(),)).fetchone()[0]
    return max(0, cap - max(by_source, by_wallclock))


def budget_refusal(cfg, conn, day: str, backfill: bool = False) -> str:
    """Why `remaining_budget` is 0, naming the cap that was actually hit."""
    cap = int(cfg["profile"]["daily_cap"])
    by_source = conn.execute("SELECT count(*) FROM evidence WHERE source_date = ?", (day,)).fetchone()[0]
    by_wallclock = conn.execute("SELECT count(*) FROM evidence WHERE created_at = ?", (today_str(),)).fetchone()[0]
    if by_source < cap and not backfill and by_wallclock >= cap:
        return (f"no budget left today: {by_wallclock} of {cap} pieces of evidence were already written "
                f"today ({today_str()}), whatever date they cite — refused. For old logs, --backfill "
                f"skips this daily total (the cap of {cap} per cited date, {day}, still applies).")
    return (f"{day}: no budget left — {by_source} of {cap} pieces of evidence already cite this date "
            "— refused.")


def begin_write(conn) -> None:
    """Budget check + insert in ONE serialized write transaction; otherwise concurrent
    processes both read '1 left' and both insert."""
    conn.execute("BEGIN IMMEDIATE")


def fail(msg: str, code: int = 2) -> int:
    print(msg, file=sys.stderr)
    return code


def get_trait(conn, trait_id: int):
    return conn.execute("SELECT * FROM traits WHERE id = ?", (trait_id,)).fetchone()


def evidence_exists(conn, trait_id: int, day: str, quote: str) -> bool:
    return conn.execute("SELECT 1 FROM evidence WHERE trait_id = ? AND source_date = ? AND quote = ?",
                        (trait_id, day, quote)).fetchone() is not None


def insert_evidence(conn, trait_id, source_path, day, quote, feeling):
    conn.execute("INSERT INTO evidence(trait_id, source_path, source_date, quote, emotion, created_at)"
                 " VALUES (?, ?, ?, ?, ?, ?)", (trait_id, source_path, day, quote, feeling, today_str()))


def _norm_ws(s: str) -> str:
    return " ".join(str(s).split())


def quote_in_text(quote: str, text: str) -> bool:
    """Verbatim after whitespace normalization; surrounding quote marks are ignored."""
    q = _norm_ws(quote).strip("\"'“”‘’ ")
    return bool(q) and q in _norm_ws(text)


def _source_file(cfg, source: str) -> Path | None:
    """--source as a file: absolute, else relative to the config directory, else to cwd."""
    p = Path(source or "").expanduser()
    if not source:
        return None
    root = Path(getattr(cfg, "root", None) or Path.cwd())
    for cand in ([p] if p.is_absolute() else [root / p, Path.cwd() / p]):
        if cand.is_file():
            return cand
    return None


def warn_if_quote_missing(cfg, source: str, quote: str) -> bool:
    """Manual writes: warn on stderr (never refuse) when --source is an existing file that
    does not contain --quote verbatim. True if a warning was printed."""
    f = _source_file(cfg, source)
    if f is None:
        return False
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if quote_in_text(quote, text):
        return False
    print(f"warning: --quote not found verbatim in {f} (daily consolidation would reject this "
          "evidence; manual writes are not blocked)", file=sys.stderr)
    return True


# --------------------------------------------------------------------------- #
# injection rendering (single source of truth; the hook imports this)
# --------------------------------------------------------------------------- #
def _truncate(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _decayed_confidence(cfg, row, today: date) -> float:
    """confidence x 0.5 ** (days since last_seen / half-life). Stored confidence is not
    changed; a new piece of evidence (merge) refreshes last_seen and revives the trait."""
    try:
        age = max(0, (today - date.fromisoformat(str(row["last_seen"])[:10])).days)
    except (ValueError, TypeError, IndexError, KeyError):
        return row["confidence"]
    return row["confidence"] * 0.5 ** (age / float(cfg["profile"]["decay_half_life_days"]))


def score_rows_against_tokens(rows, q_tokens: set) -> list:
    scored = []
    for r in rows:
        t_tokens = set(clean_tokens(r["statement"], limit=64))
        score = 0.0
        if q_tokens and t_tokens:
            union = q_tokens | t_tokens
            jac = len(q_tokens & t_tokens) / len(union) if union else 0.0
            if jac >= JACCARD_MIN:
                score = jac
        low = r["statement"].lower()
        if score == 0.0 and any(tok.lower() in low for tok in q_tokens):
            score = SUBSTRING_SCORE
        if score > 0.0:
            scored.append({"id": r["id"], "statement": r["statement"], "kind": r["kind"],
                           "confidence": r["confidence"], "score": round(score, 4)})
    scored.sort(key=lambda d: (-d["score"], -d["confidence"], d["id"]))
    return scored


def select_inject_traits(cfg, conn, query_tokens: set | None = None) -> list:
    """Traits lexically related to this message first (up to inject_max_contextual), the
    rest filled by decayed confidence. Per-kind cap keeps one kind from dominating."""
    pc = cfg["profile"]
    rows = conn.execute("SELECT id, kind, statement, confidence, last_seen FROM traits"
                        " WHERE status = 'active' ORDER BY confidence DESC, id ASC").fetchall()
    if not rows:
        return []
    today = date.today()
    rows = sorted(rows, key=lambda r: (-_decayed_confidence(cfg, r, today), r["id"]))
    per_kind: dict = {}
    max_per_kind = int(pc["inject_max_per_kind"])
    contextual = []
    if query_tokens:
        by_id = {r["id"]: r for r in rows}
        for s in score_rows_against_tokens(rows, query_tokens)[: int(pc["inject_max_contextual"])]:
            r = by_id.get(s["id"])
            if r is None or per_kind.get(r["kind"], 0) >= max_per_kind:
                continue
            per_kind[r["kind"]] = per_kind.get(r["kind"], 0) + 1
            contextual.append(r)
    budget = int(pc["inject_max_lines"]) - len(contextual)
    fill, taken = [], {r["id"] for r in contextual}
    for r in rows:
        if budget <= 0:
            break
        if r["id"] in taken or per_kind.get(r["kind"], 0) >= max_per_kind:
            continue
        per_kind[r["kind"]] = per_kind.get(r["kind"], 0) + 1
        fill.append(r)
        budget -= 1
    return contextual + fill


def render_inject_block(cfg, path=None, timeout: float = 0.5, query_tokens: set | None = None) -> str:
    """Block text, or "" if there is nothing to inject. Does not swallow DB errors."""
    path = Path(path or cfg["data"]["profile_db"])
    if not path.exists():
        return ""
    conn = store.connect_ro(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        rows = select_inject_traits(cfg, conn, query_tokens)
    finally:
        conn.close()
    if not rows:
        return ""
    width = int(cfg["profile"]["inject_line_width"])
    return "\n".join([cfg["profile"]["header"]] + [_truncate(r["statement"], width) for r in rows])


def active_count(cfg) -> int | None:
    """Active traits in the profile DB, read-only (for `jevmem doctor`); None when there is no
    DB file yet. A damaged file raises sqlite3.DatabaseError."""
    path = Path(cfg["data"]["profile_db"])
    if not path.exists() or path.stat().st_size == 0:
        return None
    conn = store.connect_ro(path, timeout=1.0)
    try:
        return conn.execute("SELECT count(*) FROM traits WHERE status = 'active'").fetchone()[0]
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return 0
        raise
    finally:
        conn.close()


def safe_block(cfg, tokens, timeout: float = 0.5) -> str:
    """For the hook: any failure (missing, corrupt, locked DB) degrades to ''. `timeout`: the
    longest wait for a locked DB."""
    try:
        return render_inject_block(cfg, timeout=timeout, query_tokens=set(tokens) if tokens else None)
    except (sqlite3.DatabaseError, OSError, ValueError):
        return ""


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
def cmd_init(cfg, args) -> int:
    conn = connect(db_path(cfg, args))
    init_schema(cfg, conn)
    conn.close()
    print(f"profile DB ready: {db_path(cfg, args)}")
    return 0


def _active_trait(conn, trait_id):
    trait = get_trait(conn, trait_id)
    if trait is None:
        return None, fail(f"trait {trait_id} not found")
    if trait["status"] != "active":
        return None, fail(f"trait {trait_id} has status '{trait['status']}' — refused. "
                          "Dead traits are never revived automatically; create a new one instead.")
    return trait, None


def cmd_new(cfg, args) -> int:
    if args.kind not in KINDS:
        return fail(f"kind must be one of {KINDS} (emotions cannot be traits): {args.kind}")
    conn = open_db(cfg, args, allow_create=args.commit)
    if args.commit:
        begin_write(conn)
    day = evidence_day(args)
    # Only ACTIVE traits count as duplicates: a soft-deleted false positive must not block
    # re-creating the trait deliberately.
    dup = conn.execute("SELECT t.id FROM traits t JOIN evidence e ON e.trait_id = t.id"
                       " WHERE t.statement = ? AND t.status = 'active' AND e.source_date = ? AND e.quote = ?",
                       (args.statement, day, args.quote)).fetchone()
    if dup:
        print(f"already recorded — trait {dup['id']} already has this evidence ({day}).")
        conn.close()
        return 0
    budget = remaining_budget(cfg, conn, day, backfill=args.backfill)
    if budget <= 0:
        msg = budget_refusal(cfg, conn, day, args.backfill)
        conn.close()
        return fail(msg)
    warn_if_quote_missing(cfg, args.source, args.quote)
    init_conf = float(cfg["profile"]["initial_confidence"])
    if not args.commit:
        print(f"[dry-run] new: kind={args.kind} conf={init_conf} statement={args.statement!r}\n"
              f"          evidence: {args.source} ({day}) {args.quote!r}\n"
              f"          {day} budget {budget} -> {budget - 1}. Pass --commit to write.")
        conn.close()
        return 0
    now = today_str()
    subject = args.subject or cfg["profile"]["subject"]
    cur = conn.execute("INSERT INTO traits(subject, kind, statement, confidence, evidence_count,"
                       " first_seen, last_seen, status, superseded_by, created_at, updated_at)"
                       " VALUES (?, ?, ?, ?, 1, ?, ?, 'active', NULL, ?, ?)",
                       (subject, args.kind, args.statement, init_conf, now, now, now, now))
    trait_id = cur.lastrowid
    insert_evidence(conn, trait_id, args.source, day, args.quote, args.emotion)
    conn.commit()
    conn.close()
    print(f"new: trait {trait_id} created (conf {init_conf}, evidence 1)")
    return 0


def cmd_merge(cfg, args) -> int:
    conn = open_db(cfg, args, allow_create=args.commit)
    if args.commit:
        begin_write(conn)
    day = evidence_day(args)
    trait, err = _active_trait(conn, args.id)
    if err is not None:
        conn.close()
        return err
    if evidence_exists(conn, trait["id"], day, args.quote):
        print(f"already recorded — trait {trait['id']} already has this evidence ({day}).")
        conn.close()
        return 0
    budget = remaining_budget(cfg, conn, day, backfill=args.backfill)
    if budget <= 0:
        msg = budget_refusal(cfg, conn, day, args.backfill)
        conn.close()
        return fail(msg)
    warn_if_quote_missing(cfg, args.source, args.quote)
    new_conf = min(MAX_CONFIDENCE, trait["confidence"] + float(cfg["profile"]["merge_increment"]))
    if not args.commit:
        print(f"[dry-run] merge: trait {trait['id']} conf {trait['confidence']} -> {new_conf}, "
              f"evidence {trait['evidence_count']} -> {trait['evidence_count'] + 1}\n"
              f"          evidence: {args.source} ({day}) {args.quote!r}\n"
              f"          {day} budget {budget} -> {budget - 1}. Pass --commit to write.")
        conn.close()
        return 0
    insert_evidence(conn, trait["id"], args.source, day, args.quote, args.emotion)
    conn.execute("UPDATE traits SET evidence_count = evidence_count + 1, confidence = ?,"
                 " last_seen = ?, updated_at = ? WHERE id = ?", (new_conf, today_str(), today_str(), trait["id"]))
    conn.commit()
    conn.close()
    print(f"merge: trait {trait['id']} conf -> {new_conf}")
    return 0


def cmd_supersede(cfg, args) -> int:
    conn = open_db(cfg, args, allow_create=args.commit)
    if args.commit:
        begin_write(conn)
    day = evidence_day(args)
    trait, err = _active_trait(conn, args.id)
    if err is not None:
        conn.close()
        return err
    budget = remaining_budget(cfg, conn, day, backfill=args.backfill)
    if budget <= 0:
        msg = budget_refusal(cfg, conn, day, args.backfill)
        conn.close()
        return fail(msg)
    kind = args.kind or trait["kind"]
    if kind not in KINDS:
        conn.close()
        return fail(f"kind must be one of {KINDS}: {kind}")
    warn_if_quote_missing(cfg, args.source, args.quote)
    if not args.commit:
        print(f"[dry-run] supersede: trait {trait['id']} -> superseded, new trait "
              f"(kind={kind}, inherits conf {trait['confidence']})\n"
              f"          statement={args.statement!r}\n"
              f"          evidence: {args.source} ({day}) {args.quote!r}\n"
              f"          {day} budget {budget} -> {budget - 1}. Pass --commit to write.")
        conn.close()
        return 0
    now = today_str()
    cur = conn.execute("INSERT INTO traits(subject, kind, statement, confidence, evidence_count,"
                       " first_seen, last_seen, status, superseded_by, created_at, updated_at)"
                       " VALUES (?, ?, ?, ?, 1, ?, ?, 'active', NULL, ?, ?)",
                       (trait["subject"], kind, args.statement, trait["confidence"], now, now, now, now))
    new_id = cur.lastrowid
    insert_evidence(conn, new_id, args.source, day, args.quote, args.emotion)
    # The old row and its evidence are kept: when and why a judgment changed is audit data.
    conn.execute("UPDATE traits SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE id = ?",
                 (new_id, now, trait["id"]))
    conn.commit()
    conn.close()
    print(f"supersede: trait {trait['id']} -> {new_id} (inherits conf {trait['confidence']})")
    return 0


def cmd_candidates(cfg, args) -> int:
    """Top-5 lexically similar ACTIVE traits (JSON). Makes no decision: the caller (your
    daily consolidation job, or your own agent prompt) picks merge / new / supersede."""
    path = db_path(cfg, args)
    if not path.exists():
        print("[]")
        return 0
    conn = open_db(cfg, args, allow_create=False)
    rows = conn.execute("SELECT id, kind, statement, confidence FROM traits WHERE status = 'active'").fetchall()
    conn.close()
    scored = score_rows_against_tokens(rows, set(clean_tokens(args.sentence, limit=64))) if rows else []
    print(json.dumps(scored[:CANDIDATE_LIMIT], ensure_ascii=False))
    return 0


def cmd_list(cfg, args) -> int:
    conn = open_db(cfg, args, allow_create=False)
    where = "1=1" if args.include_deleted else "status = 'active'"
    params: list = []
    if args.kind:
        where += " AND kind = ?"
        params.append(args.kind)
    if args.min_confidence is not None:
        where += " AND confidence >= ?"
        params.append(args.min_confidence)
    params.append(args.limit)
    rows = conn.execute(f"SELECT id, confidence, evidence_count, kind, statement, status FROM traits"
                        f" WHERE {where} ORDER BY confidence DESC, id ASC LIMIT ?", params).fetchall()
    conn.close()
    print("id | conf | ev | kind | statement")
    for r in rows:
        # '|' is the column separator; replace it so the format never breaks.
        stmt = " ".join(r["statement"].replace("|", "/").split())[:LIST_STATEMENT_WIDTH]
        suffix = "" if r["status"] == "active" else f"  [{r['status']}]"
        print(f"{r['id']} | {r['confidence']:.2f} | {r['evidence_count']} | {r['kind']} | {stmt}{suffix}")
    return 0


def cmd_show(cfg, args) -> int:
    conn = open_db(cfg, args, allow_create=False)
    trait = get_trait(conn, args.id)
    if trait is None:
        conn.close()
        return fail(f"trait {args.id} not found")
    rows = conn.execute("SELECT id, source_path, source_date, quote, emotion FROM evidence"
                        " WHERE trait_id = ? ORDER BY source_date ASC, id ASC", (args.id,)).fetchall()
    conn.close()
    print(f"id          : {trait['id']}")
    print(f"subject     : {trait['subject']}")
    print(f"kind        : {trait['kind']}")
    print(f"confidence  : {trait['confidence']:.2f}  (evidence {trait['evidence_count']})")
    print(f"status      : {trait['status']}" + (f" -> {trait['superseded_by']}" if trait["superseded_by"] else ""))
    print(f"first/last  : {trait['first_seen']} ~ {trait['last_seen']}")
    print(f"statement   : {trait['statement']}")
    print(f"\nevidence ({len(rows)})")
    for r in rows:
        mood = f"  ({r['emotion']})" if r["emotion"] else ""
        print(f"  - [{r['source_date']}] {r['source_path']}{mood}")
        print(f"    \"{r['quote']}\"")
    return 0


def cmd_delete(cfg, args) -> int:
    conn = open_db(cfg, args, allow_create=args.commit)
    trait = get_trait(conn, args.id)
    if trait is None:
        conn.close()
        return fail(f"trait {args.id} not found")
    n_ev = conn.execute("SELECT count(*) FROM evidence WHERE trait_id = ?", (args.id,)).fetchone()[0]
    if not args.commit:
        mode = "hard delete (with evidence)" if args.hard else "soft delete (status='deleted')"
        print(f"[dry-run] delete: trait {args.id} — {mode}, evidence {n_ev}. "
              f"Pass --commit" + (" --yes" if args.hard else "") + " to apply.")
        conn.close()
        return 0
    if args.hard:
        if not args.yes:
            conn.close()
            return fail("--hard --commit cannot be undone. Add --yes to confirm.")
        refs = [str(r["id"]) for r in conn.execute("SELECT id FROM traits WHERE superseded_by = ?", (args.id,))]
        if refs:
            conn.close()
            return fail(f"trait {args.id} is referenced as superseded_by by trait {', '.join(refs)}; "
                        "hard delete would break the history. Use a soft delete (--commit only).")
        conn.execute("DELETE FROM evidence WHERE trait_id = ?", (args.id,))
        conn.execute("DELETE FROM traits WHERE id = ?", (args.id,))
        conn.commit()
        conn.close()
        print(f"delete(hard): trait {args.id} and {n_ev} evidence rows removed")
        return 0
    conn.execute("UPDATE traits SET status = 'deleted', updated_at = ? WHERE id = ?", (today_str(), args.id))
    conn.commit()
    conn.close()
    print(f"delete(soft): trait {args.id} -> status='deleted' (excluded from injection, evidence kept)")
    return 0


def cmd_stats(cfg, args) -> int:
    conn = open_db(cfg, args, allow_create=False)
    cap = cfg["profile"]["daily_cap"]
    total = conn.execute("SELECT count(*) FROM traits").fetchone()[0]
    by_status = {s: 0 for s in STATUSES}
    for r in conn.execute("SELECT status, count(*) AS n FROM traits GROUP BY status"):
        by_status[r["status"]] = r["n"]
    total_ev = conn.execute("SELECT count(*) FROM evidence").fetchone()[0]
    # A nightly job usually writes evidence dated YESTERDAY; show the latest source date too.
    latest = conn.execute("SELECT max(source_date) FROM evidence").fetchone()[0]
    days = [args.date] if args.date else sorted({d for d in (today_str(), latest) if d})
    written_today = conn.execute("SELECT count(*) FROM evidence WHERE created_at = ?", (today_str(),)).fetchone()[0]
    lines = []
    for day in days:
        n = conn.execute("SELECT count(*) FROM evidence WHERE source_date = ?", (day,)).fetchone()[0]
        lines.append(f"{day} : evidence {n}, {n}/{cap} of the per-date cap used")
    conn.close()
    print(f"traits      : {total} (active {by_status['active']} / "
          f"superseded {by_status['superseded']} / deleted {by_status['deleted']})")
    print(f"evidence    : {total_ev}")
    for line in lines:
        print(line)
    print(f"written today ({today_str()}): {written_today} / cap {cap} (whatever date they cite)")
    print(f"left today: {max(0, cap - written_today)} (and at most {cap} per cited date)")
    return 0


def cmd_inject(cfg, args) -> int:
    tokens = set(clean_tokens(args.query)) if args.query else None
    block = render_inject_block(cfg, db_path(cfg, args), query_tokens=tokens)
    if block:
        print(block)
    elif args.preview:
        print("(no traits to inject — block omitted)")
    return 0


def add_parser(sub, parents=()) -> None:
    """`parents`: shared option parsers (the CLI passes one with --config). They are added to
    `profile` AND to every profile subcommand, so options work in either position:
    `jevmem profile --config X list` and `jevmem profile list --config X`."""
    db_help = "profile DB path (default: from config)"
    p = sub.add_parser("profile", help="profile memory (evidence-backed user traits)", parents=list(parents))
    p.add_argument("--db", default=None, help=db_help)
    # Subcommand copies use SUPPRESS so they don't reset a value given before the subcommand.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--db", default=argparse.SUPPRESS, help=db_help)
    common = list(parents) + [shared]
    ps = p.add_subparsers(dest="pcmd", required=True)

    def add_sub(name, **kw):
        return ps.add_parser(name, parents=common, **kw)

    add_sub("init", help="create schema (idempotent)").set_defaults(pfunc=cmd_init)

    def add_write_common(sp):
        sp.add_argument("--source", required=True, help="evidence source, e.g. memory/2026-09-01.md")
        sp.add_argument("--quote", required=True, help="verbatim quote supporting the judgment")
        sp.add_argument("--emotion", default=None,
                        help="optional mood at the time; context only, never a trait")
        sp.add_argument("--commit", action="store_true", help="actually write (default: dry-run)")
        sp.add_argument("--backfill", action="store_true",
                        help="processing old logs: skip today's total cap, keep the per-source-date cap")

    sp = add_sub("new", help="create a trait")
    sp.add_argument("--kind", required=True, choices=KINDS)
    sp.add_argument("--statement", required=True)
    sp.add_argument("--subject", default=None)
    add_write_common(sp)
    sp.set_defaults(pfunc=cmd_new)

    sp = add_sub("merge", help="add evidence to an existing trait")
    sp.add_argument("id", type=int)
    add_write_common(sp)
    sp.set_defaults(pfunc=cmd_merge)

    sp = add_sub("supersede", help="replace a trait (old one kept for audit)")
    sp.add_argument("id", type=int)
    sp.add_argument("--statement", required=True)
    sp.add_argument("--kind", default=None, choices=KINDS)
    add_write_common(sp)
    sp.set_defaults(pfunc=cmd_supersede)

    sp = add_sub("candidates", help="top-5 similar active traits as JSON (no decision)")
    sp.add_argument("sentence")
    sp.set_defaults(pfunc=cmd_candidates)

    sp = add_sub("list", help="list traits")
    sp.add_argument("--kind", default=None, choices=KINDS)
    sp.add_argument("--min-confidence", type=float, default=None)
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--include-deleted", action="store_true")
    sp.set_defaults(pfunc=cmd_list)

    sp = add_sub("show", help="full trait + all evidence")
    sp.add_argument("id", type=int)
    sp.set_defaults(pfunc=cmd_show)

    sp = add_sub("delete", help="remove a false-positive trait (soft by default)")
    sp.add_argument("id", type=int)
    sp.add_argument("--hard", action="store_true", help="also delete evidence rows")
    sp.add_argument("--commit", action="store_true")
    sp.add_argument("--yes", action="store_true", help="confirm --hard --commit")
    sp.set_defaults(pfunc=cmd_delete)

    sp = add_sub("stats", help="counts and remaining daily budget")
    sp.add_argument("--date", default=None)
    sp.set_defaults(pfunc=cmd_stats)

    sp = add_sub("consolidate", help="daily consolidation: distill up to 3 evidence-backed "
                       "user traits from one day's log (LLM + verification)")
    sp.add_argument("--date", default=None, help="YYYY-MM-DD (default: yesterday)")
    sp.add_argument("--dry-run", action="store_true",
                    help="show what would be sent (log size, active traits, remaining budget); "
                         "no LLM call, no writes")
    sp.add_argument("--backfill", action="store_true", help="old logs: per-source-date cap only")
    sp.set_defaults(pfunc=None, consolidate=True)

    sp = add_sub("inject", help="preview the injection block")
    sp.add_argument("--preview", action="store_true")
    sp.add_argument("--query", default=None, help="message text for contextual matching")
    sp.set_defaults(pfunc=cmd_inject)

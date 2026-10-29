"""Episodic store: one SQLite file with labeled chunks (`units`) and an FTS5 mirror.

The FTS5 table is filled at chunking time (no API call), so the keyword fallback works
even before any chunk has been labeled or when no API key is configured.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from . import fsutil

UNIT_COLS = ["hash", "relpath", "date", "chunk", "top1", "top1_p", "top2", "top2_p",
             "sub1", "sub1_p", "sub2", "sub2_p"]


# SQLite's own fixed messages for a file that is not (or no longer) a usable database.
_DAMAGE_MESSAGES = ("file is not a database", "database disk image is malformed", "malformed database schema",
                    "file is encrypted or is not a database")


def db_problem(e: BaseException) -> tuple[str, bool]:
    """(short reason, damaged?) for an sqlite3 error. Only SQLite's own fixed texts are kept
    (other messages could quote a query): 'file is not a database', 'database is locked'..."""
    msg = " ".join(str(e).split()).lower()
    for m in _DAMAGE_MESSAGES:
        if m in msg:
            return m, True
    if "locked" in msg or "busy" in msg:
        return "database is locked by another jevmem process; try again in a moment", False
    if "unable to open" in msg:
        return "unable to open the database file", False
    return f"{type(e).__name__}", False


def unit_hash(relpath: str, chunk: str) -> str:
    return hashlib.sha1((relpath + "\n" + chunk).encode("utf-8")).hexdigest()


def ro_uri(path) -> str:
    """Read-only SQLite URI for a file path. The path is percent-encoded: a raw `#`, `?` or
    `%` in a folder name would otherwise end the path early (fragment / query / escape), and
    SQLite would open, and create, a different file in read-write mode."""
    return Path(os.path.abspath(os.fspath(path))).as_uri() + "?mode=ro"


def connect_ro(path, timeout: float = 5.0) -> sqlite3.Connection:
    """Open an existing database read-only (never creates a file)."""
    return sqlite3.connect(ro_uri(path), uri=True, timeout=timeout)


def connect(path, readonly: bool = False, timeout: float = 5.0) -> sqlite3.Connection:
    """`timeout` = how long to wait for a lock. The hook path passes a shorter one
    (recall.HOOK_DB_TIMEOUT, bounded by its deadline); CLI commands use the default 5 s.
    A new database file is created 0600 (in a 0700 directory if that is new too)."""
    p = Path(path)
    if readonly:
        con = connect_ro(p, timeout=timeout)
    else:
        fsutil.precreate(p)
        con = sqlite3.connect(p, timeout=timeout)
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS units(
        hash TEXT PRIMARY KEY, relpath TEXT, date TEXT, chunk TEXT,
        top1 TEXT, top1_p REAL, top2 TEXT, top2_p REAL,
        sub1 TEXT, sub1_p REAL, sub2 TEXT, sub2_p REAL,
        labeled_at TEXT, tries INTEGER DEFAULT 0, err TEXT, in_tok INTEGER,
        stale INTEGER DEFAULT 0)""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(units)")}
    for col, decl in (("labeler", "TEXT"), ("hfail", "INTEGER DEFAULT 0"), ("stale", "INTEGER DEFAULT 0")):
        if col not in cols:  # databases created by older versions
            con.execute(f"ALTER TABLE units ADD COLUMN {col} {decl}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_sub1 ON units(sub1)")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5("
                    "hash UNINDEXED, relpath UNINDEXED, date UNINDEXED, chunk, tokenize='trigram')")
    except sqlite3.OperationalError:
        # SQLite < 3.34 has no trigram tokenizer; word tokenizer still works for English.
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5("
                    "hash UNINDEXED, relpath UNINDEXED, date UNINDEXED, chunk)")
    con.commit()


def fts_is_trigram(con) -> bool:
    row = con.execute("SELECT sql FROM sqlite_master WHERE name='units_fts'").fetchone()
    return bool(row and "trigram" in row[0])


def get_meta(con, k):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def set_meta(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))


def sync_units(con, current: dict) -> tuple[int, int]:
    """current = {hash: (relpath, date, chunk)}. Insert new, delete vanished."""
    have = {r[0] for r in con.execute("SELECT hash FROM units")}
    gone = have - set(current)
    new = set(current) - have
    con.executemany("DELETE FROM units WHERE hash=?", [(h,) for h in gone])
    if gone:
        # `hash` is UNINDEXED in the FTS table: one DELETE per hash would scan the whole table
        # each time (quadratic when a chunking change replaces every chunk). One pass instead.
        con.execute("CREATE TEMP TABLE IF NOT EXISTS gone_hashes(hash TEXT PRIMARY KEY)")
        con.execute("DELETE FROM temp.gone_hashes")
        con.executemany("INSERT OR IGNORE INTO temp.gone_hashes VALUES(?)", [(h,) for h in gone])
        con.execute("DELETE FROM units_fts WHERE hash IN (SELECT hash FROM temp.gone_hashes)")
        con.execute("DELETE FROM temp.gone_hashes")
    con.executemany("INSERT INTO units(hash,relpath,date,chunk) VALUES(?,?,?,?)",
                    [(h, *current[h]) for h in new])
    con.executemany("INSERT INTO units_fts(hash,relpath,date,chunk) VALUES(?,?,?,?)",
                    [(h, *current[h]) for h in new])
    return len(new), len(gone)


def labeled_units(con) -> list[dict]:
    rows = con.execute(f"SELECT {','.join(UNIT_COLS)} FROM units WHERE labeled_at IS NOT NULL").fetchall()
    return [dict(zip(UNIT_COLS, r)) for r in rows]

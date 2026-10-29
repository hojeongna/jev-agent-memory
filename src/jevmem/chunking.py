"""Split daily markdown logs into small, mostly single-topic chunks.

Rules (the old approach glued paragraphs up to ~600 chars, so one chunk mixed several
unrelated check-ins; splitting finer roughly doubled the chunk count and raised the share
of useful injected chunks):
  1. Start a new section at every markdown heading (#..####). In files without a date in
     the filename (e.g. a CHANGELOG), a line starting with "[YYYY-MM-DD" also starts one.
  2. Inside a section, split into items at bullets ("- ", "* ", "1. ", "[YYYY-").
  3. Pack items into chunks of at most `max_chunk` chars.
  4. Hard-cut anything longer than `hard_cut` chars.
  5. Drop bodies shorter than `min_chunk` chars.
  6. The heading line is prefixed to every chunk of its section.
  7. Sections whose heading matches `split_heading_re` (by default the digest's
     "## HH:MM summary") skip step 3: every bullet is its own chunk, because the digest
     writes exactly one topic per bullet and packing them would mix topics in one label.
     Only near-empty bullets (< 10 chars) are dropped there.
Date = date in the filename; otherwise the most recent date seen in a heading/entry.
"""
from __future__ import annotations

import re
from pathlib import Path

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
HEAD_RE = re.compile(r"^#{1,4}\s")
ENTRY_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2}")
ITEM_RE = re.compile(r"^(- |\* |\[\d{4}-|\d+\. )")
NO_DATE = "0000-00-00"


SPLIT_MIN = 10


def sections(text: str, relpath: str = "") -> list[tuple[str, list[str]]]:
    """Split a log into [(heading line or "", body lines)], using rule 1 above."""
    fdate = DATE_RE.search(Path(relpath).name) if relpath else None
    sections, head, buf = [], "", []
    for line in text.splitlines():
        is_head = bool(HEAD_RE.match(line))
        is_entry = (not fdate) and bool(ENTRY_RE.match(line))
        if is_head or is_entry:
            if buf or head:
                sections.append((head, buf))
            head, buf = (line.strip(), []) if is_head else ("", [line])
        else:
            buf.append(line)
    sections.append((head, buf))
    return sections


def split_items(lines: list[str]) -> list[str]:
    """Rule 2: split a section body into items at bullets (continuation lines stay attached)."""
    items, cur = [], []
    for line in lines:
        if ITEM_RE.match(line) and cur:
            items.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        items.append("\n".join(cur))
    return [it.strip() for it in items if it.strip()]


def chunk_text(text: str, relpath: str, max_chunk: int = 450, hard_cut: int = 900,
               min_chunk: int = 30, split_heading_re: str | None = None) -> list[tuple[str, str, str]]:
    """Return [(relpath, date, chunk), ...]."""
    m = DATE_RE.search(Path(relpath).name)
    fdate = m.group(1) if m else None
    cur_date = fdate or NO_DATE
    secs = sections(text, relpath)

    split_re = re.compile(split_heading_re) if split_heading_re else None
    out = []
    for head, lines in secs:
        per_bullet = bool(split_re and head and split_re.search(head))
        if not fdate:
            mm = DATE_RE.search(head) or DATE_RE.search(" ".join(lines[:1]))
            if mm:
                cur_date = mm.group(1)
        packs, pack = [], ""
        for it in split_items(lines):
            if per_bullet:
                if len(it) >= SPLIT_MIN:
                    packs.append(it)
                continue
            if pack and len(pack) + len(it) > max_chunk:
                packs.append(pack)
                pack = it
            else:
                pack = (pack + "\n" + it).strip()
        if pack:
            packs.append(pack)
        for body in packs:
            if len(body) < (SPLIT_MIN if per_bullet else min_chunk):
                continue
            ch = (head + "\n" + body).strip() if head else body
            for i in range(0, len(ch), hard_cut):
                out.append((relpath, cur_date, ch[i:i + hard_cut]))
    return out


def relpath_of(path: Path, logs_dir: Path) -> str:
    """A log file's path relative to the logs folder (just its name if it lies outside)."""
    try:
        return str(Path(path).resolve().relative_to(Path(logs_dir).resolve()))
    except ValueError:
        return Path(path).name


def chunk_file(path: Path, logs_dir: Path, **kw) -> list[tuple[str, str, str]]:
    rel = relpath_of(path, logs_dir)
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return chunk_text(text, rel, **kw)

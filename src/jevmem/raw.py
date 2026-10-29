"""Raw pointer: from a summary back to the chat lines it was written from.

A digest bullet is an index entry, not the source of truth. Summaries get speakers and
status wrong now and then ("agreed" when it was only proposed, the user's idea credited to
the assistant), so recall shows the summary plus a one-line pointer, and the agent reads
the original lines only when the details matter:

    [2026-09-10]
    ## 09:34 summary
    - The user asked why the nightly backup failed; ...
      ↳ raw: jevmem raw 2026-09-10 09:34

`jevmem raw 2026-09-10 09:34` then prints the chat lines of that summary's window.

Window rule: the summary headed HH:MM covers (the previous summary heading of the same
day, HH:MM]; the first summary of a day starts at the last summary heading of the previous
day's log, or at midnight when there is none. A heading with an explicit range
("## 09:00~09:34 summary") uses that range. Times are local, like the digest's headings.

Lines come from the digest source (the Stage 1 capture log by default), read in full; the
pointer is shown only when that source reaches back to the chunk's date.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import digest

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FILE_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.md$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _head_re(cfg) -> re.Pattern:
    return re.compile(cfg["raw"]["heading_re"], re.M)


def _norm_time(t: str) -> str:
    return t.zfill(5)


def headings(cfg, day: str) -> list[tuple[str | None, str]]:
    """Summary headings of one daily log: [(explicit start or None, end HH:MM)]."""
    path = Path(cfg["logs"]["dir"]) / f"{day}.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for m in _head_re(cfg).finditer(text):
        a, b = m.group(1), m.group(2) if m.lastindex and m.lastindex >= 2 else None
        out.append((_norm_time(a), _norm_time(b)) if b else (None, _norm_time(a)))
    return out


def _dt(day: str, hhmm: str, sec: int = 0) -> datetime:
    return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(second=sec)


def _prev_day(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def window(cfg, day: str, hhmm: str) -> tuple[datetime, datetime] | None:
    """(start exclusive, end inclusive) of the summary headed `hhmm`; None if no such heading."""
    hhmm = _norm_time(hhmm)
    hs = headings(cfg, day)
    for i, (a, b) in enumerate(hs):
        if b != hhmm:
            continue
        if a:
            return _dt(day, a) - timedelta(seconds=1), _dt(day, b, 59)
        prev = [h[1] for h in hs[:i] if h[1] < hhmm]
        if prev:
            return _dt(day, prev[-1], 59), _dt(day, hhmm, 59)
        pd = _prev_day(day)
        ph = headings(cfg, pd)
        if ph:
            return _dt(pd, ph[-1][1], 59), _dt(day, hhmm, 59)
        return _dt(day, "00:00") - timedelta(seconds=1), _dt(day, hhmm, 59)
    return None


def window_around(cfg, day: str, hhmm: str) -> tuple[tuple[datetime, datetime], str | None]:
    """The window of the summary that contains `hhmm`; if `hhmm` falls between two windows
    (an explicit range such as "## 11:00~11:30 summary" leaves a gap), the next summary's
    window; after the last summary, ±30 minutes (heading None). `contains` tells which."""
    hhmm = _norm_time(hhmm)
    t = _dt(day, hhmm)
    nxt = None
    for _, b in headings(cfg, day):
        if b < hhmm:
            continue
        w = window(cfg, day, b)
        if not w:
            continue
        if w[0] < t <= w[1]:
            return w, b
        if nxt is None:
            nxt = (w, b)
    if nxt:
        return nxt
    return (t - timedelta(minutes=30), t + timedelta(minutes=30)), None


def contains(w: tuple[datetime, datetime], day: str, hhmm: str) -> bool:
    t = _dt(day, _norm_time(hhmm))
    return w[0] < t <= w[1]


def local_dt(ts) -> datetime | None:
    """Record timestamp -> naive local datetime (aware timestamps are converted)."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def records(cfg, start: datetime, end: datetime) -> list[dict]:
    """Chat records with start < time <= end, oldest first: {dt, user, script, name, text}."""
    dc = cfg["digest"]
    days = set()
    d = start.date() - timedelta(days=1)
    while d <= end.date() + timedelta(days=1):
        days.add(d.isoformat())
        d += timedelta(days=1)
    skip = tuple(cfg["raw"].get("skip_prefixes") or ())
    out = []
    for path in digest.source_files(cfg):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = [ln for ln in f if any(x in ln for x in days)]  # cheap prefilter
        except OSError:
            continue
        for r in digest.parse_records(lines, dc["source"]):
            dt = local_dt(r["ts"])
            if dt is None or not (start < dt <= end):
                continue
            if skip and r["text"].startswith(skip):
                continue
            out.append({**r, "dt": dt})
    out.sort(key=lambda r: r["dt"])
    return out


def format_line(cfg, rec: dict, named: bool) -> str:
    return f"[{rec['dt']:%H:%M}] {digest.speaker(cfg, rec, named)}: " + " ".join(rec["text"].split())


_since_cache: dict = {}


def covered_since(cfg) -> str | None:
    """Date of the oldest timestamped record in the digest source (first lines only)."""
    files = digest.source_files(cfg)
    key = tuple((str(p), p.stat().st_mtime) for p in files if p.exists())
    if key in _since_cache:
        return _since_cache[key]
    best = None
    for p in files:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for _, line in zip(range(50), f):
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(r, dict):
                        continue
                    dt = local_dt(r.get("ts", r.get("timestamp", "")))
                    if dt:
                        d = dt.date().isoformat()
                        best = d if best is None or d < best else best
                        break
        except OSError:
            continue
    _since_cache.clear()
    _since_cache[key] = best
    return best


def covered(cfg, day: str) -> bool:
    since = covered_since(cfg)
    return bool(since and since <= day)


def heading_time(cfg, chunk: str) -> str | None:
    """End time of the summary heading a chunk starts with, else None."""
    first = (chunk or "").lstrip().split("\n", 1)[0]
    m = _head_re(cfg).match(first)
    if not m:
        return None
    b = m.group(2) if m.lastindex and m.lastindex >= 2 else None
    return _norm_time(b or m.group(1))


def pointer_line(cfg, relpath: str, day: str, chunk: str) -> str | None:
    """The pointer line for a recalled chunk, or None. Never raises."""
    try:
        rc = cfg["recall"]
        if not rc.get("raw_pointer", True):
            return None
        m = FILE_DATE_RE.search(relpath or "")
        if not m or m.group(1) != str(day)[:10]:
            return None
        t = heading_time(cfg, chunk)
        if not t or not covered(cfg, m.group(1)):
            return None
        return rc["raw_pointer_format"].format(command=rc.get("raw_pointer_command", "jevmem raw"),
                                               date=m.group(1), time=t)
    except Exception:
        return None


def _n(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def valid_time(t: str) -> bool:
    if not TIME_RE.match(t or ""):
        return False
    h, m = (int(x) for x in t.split(":"))
    return h < 24 and m < 60


def check_date(day: str) -> None:
    if not DATE_RE.match(day or ""):
        raise ValueError(f"date must be YYYY-MM-DD, got {day!r}")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{day} is not a real date (YYYY-MM-DD)") from None


def lookup(cfg, day: str, hhmm: str | None = None, keywords=(), around: str | None = None,
           max_lines: int | None = None) -> list[str]:
    """Output lines for `jevmem raw` (header first). ValueError (with a readable message)
    for a bad date or time."""
    check_date(day)
    for t in (hhmm, around):
        if t and not valid_time(t):
            raise ValueError(f"time must be HH:MM between 00:00 and 23:59, got {t!r}")
    since = covered_since(cfg)
    if not since or since > day:
        src = cfg["digest"]["source"]
        log = Path(cfg["logs"]["dir"]) / f"{day}.md"
        where = f"{Path(cfg['logs']['dir']).name}/{day}.md"
        tail = (f"Only the summary in {where} is available." if log.exists()
                else f"There is no daily log {where} either.")
        return [f"[no raw lines] {day} is not covered by the chat log (digest source: {src}"
                + (f", records start {since}" if since else ", no timestamped records") + "). " + tail]
    note, w = "", None
    if hhmm and not around:
        w = window(cfg, day, hhmm)
        if not w:
            asked = hhmm
            w, b = window_around(cfg, day, asked)
            hhmm = b or asked
            if b and contains(w, day, asked):
                note = f"(no '{asked} summary' heading; showing summary {b}, whose window contains {asked})"
            elif b:
                note = f"(no '{asked} summary' heading and no summary window contains {asked}; showing the next one, summary {b})"
            else:
                note = f"(no '{asked} summary' heading and no summary after it; ±30 min around {asked})"
    if around:
        w, b = window_around(cfg, day, around)
        hhmm = b or around
        if b and contains(w, day, around):
            note = f"(--around {around} -> summary {b})"
        elif b:
            note = f"(--around {around}: no summary window contains it; the next one is summary {b})"
        else:
            note = f"(--around {around}: no summary heading, ±30 min)"
    if not w:
        w = (_dt(day, "00:00") - timedelta(seconds=1), _dt(day, "23:59", 59))
        note = "(no time given: whole day)"
    recs = records(cfg, *w)
    named = digest.several_senders(cfg, recs)
    lines = [format_line(cfg, r, named) for r in recs]
    start = w[0] + timedelta(seconds=1)
    span = f"{start:%H:%M}~{w[1]:%H:%M}" + (f" ({start:%Y-%m-%d}~{w[1]:%Y-%m-%d})"
                                               if start.date() != w[1].date() else "")
    head = f"[{day} {hhmm or ''} raw] window {span}, {_n(len(lines), 'line')} {note}".replace("  ", " ").rstrip()
    body = lines
    if keywords:
        low = [k.lower() for k in keywords]
        hit = [i for i, ln in enumerate(lines) if any(k in ln.lower() for k in low)]
        keep = sorted({j for i in hit for j in (i - 1, i, i + 1) if 0 <= j < len(lines)})
        head += f" · keywords {list(keywords)}: {_n(len(hit), 'matching line')} (±1 line of context)"
        body, last = [], None
        for j in keep:
            if last is not None and j != last + 1:
                body.append("  ...")
            body.append(("» " if j in hit else "  ") + lines[j])
            last = j
    if not body:
        return [head, "(no messages in this window)" if not keywords
                else "(no line matches; run again without keywords to see the whole window)"]
    if max_lines and len(body) > max_lines:
        return [head] + body[:max_lines] + [f"... (+{_n(len(body) - max_lines, 'line')}; raise --max to see more)"]
    return [head] + body

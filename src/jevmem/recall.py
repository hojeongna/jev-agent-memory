"""Episodic recall: the per-message pipeline.

  message (+ recent chat lines)
    -> ROUTE : one Jev `choice` over all subtopics (short "routing" descriptions);
               sum to top-level; if the top topic is the smalltalk topic -> inject nothing
    -> POOL  : labeled chunks whose sub-label matches (sub1, or sub2 with p >= 0.1);
               top-level fill if fewer than `pool_min`; sort by label prob x recency;
               rare-word hits (words in 1..rare_max_df chunks) go first; cap at `cap`
    -> PICK  : one Jev `choice` over ~200-char dated excerpts of the candidates;
               keep p >= pick_min_p, at most max_picks, reordered by p x recency
    -> INJECT

Time decay is a weight, not a date cut-off: recency = 0.5 ** (age / recency_half_life_days).
  - pool order: label score x (recency_floor + (1 - recency_floor) x recency), so an old
    chunk keeps at least `recency_floor` (half) of its weight. The pool is then cut to `cap`
    (rare-word hits first), so when more chunks match than fit, the oldest/weakest are the
    ones left out of the candidates.
  - pick order: p x (1 - w + w x recency) with w = pick_recency_weight, among excerpts with
    p >= pick_min_p; when more than max_picks qualify, recency helps decide which are kept.
(An explicit "only the last N days" cut made old-but-relevant memories unreachable.)

`fts_search` is the keyword fallback (SQLite FTS5 bm25 + time decay), used when Jev fails,
times out, answered HTTP 402 (breaker open), or no key is configured.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

from . import chunking, store
from . import taxonomy as taxmod
from .text import DATE_TOKEN, date_token_regex, message_blocks

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# Longest wait for a locked episodic DB on the hook path; kept under the hook's 4 s deadline
# (the hook passes min(this, time left)). CLI commands keep store.connect's 5 s default.
HOOK_DB_TIMEOUT = 3.0
# Every wait on the hook path (a Jev call, a locked database) ends at least this many seconds
# before the deadline: room for starting Python and printing the answer, which the deadline
# doesn't measure, so the whole hook process stays within `deadline_sec`.
END_RESERVE = 0.3


class DeadlineExceeded(Exception):
    pass


class Deadline:
    def __init__(self, seconds: float, clock=time.monotonic):
        self.clock = clock
        self.start = clock()
        self.seconds = float(seconds)

    def remaining(self) -> float:
        return self.seconds - (self.clock() - self.start)

    def elapsed(self) -> float:
        return self.clock() - self.start


def recency(d, today: date, half_life_days: float) -> float:
    """0.5 ** (age_days / half_life). Unparseable date -> 1.0 (no decay)."""
    try:
        age = max(0, (today - datetime.strptime(str(d)[:10], "%Y-%m-%d").date()).days)
    except (ValueError, TypeError):
        return 1.0
    return 0.5 ** (age / float(half_life_days))


def build_state(cfg, message: str, prior: list[str], today: date | None = None) -> str:
    """Jev state: recent chat lines + current message. With config recall.include_today (default
    ON) and a `today`, one neutral line "Today's date: YYYY-MM-DD (Weekday)" is added to the
    state of every route and pick call, so relative time words in the message ("last month",
    "yesterday") can be judged against the excerpt dates. Set it to false to leave it out."""
    ctx = cfg["context"]
    out = ("[Recent conversation]\n" + "\n".join(prior) + "\n\n[Current message]\n"
           + f"{ctx['user_label']}: " + message[:1500])
    if today is not None and cfg["recall"].get("include_today"):
        out += f"\n\nToday's date: {today.isoformat()} ({today.strftime('%A')})"
    return out


def _jev(client, deadline: Deadline, state, questions, calls: list, tag: str, margin=END_RESERVE):
    left = deadline.remaining() - margin
    if left <= 0.1:
        raise DeadlineExceeded(f"no time left before {tag}")
    t0 = time.monotonic()
    answers, usage = client.call(state, questions, timeout=left)
    calls.append({"tag": tag, "lat": round(time.monotonic() - t0, 3),
                  "in": (usage or {}).get("input_tokens", 0)})
    return answers


def route(cfg, tax, client, state, deadline, calls) -> tuple[list, dict]:
    """Return ([(top, sub, top_prob), ...], top_probs)."""
    rc = cfg["recall"]
    crit = taxmod.routing_criteria(tax)
    q = {"q": {"type": "choice", "instructions": rc["route_instructions"], "criteria": crit}}
    p = _jev(client, deadline, state, q, calls, "route")["q"]["probabilities"]
    tops: dict = {}
    for k, v in p.items():
        t = k.split("/")[0]
        tops[t] = tops.get(t, 0.0) + v
    order = sorted(tops, key=lambda k: -tops[k])

    def best(t):
        return max((k for k in p if k.startswith(t + "/")), key=lambda k: p[k])

    small = taxmod.smalltalk(tax)
    routed = [(order[0], best(order[0]), tops[order[0]])]
    if len(order) > 1 and tops[order[1]] > rc["second_topic_p"] and order[1] != small:
        routed.append((order[1], best(order[1]), tops[order[1]]))
    return routed, tops


def build_pool(cfg, units: list[dict], routed: list, tokens: list[str], today: date) -> tuple[list, dict]:
    """Candidate list for the final pick (pure function; unit-tested)."""
    rc = cfg["recall"]
    sub2_min = rc["sub2_min_p"]
    subs = {s for _, s, _ in routed}
    topset = {t for t, _, _ in routed}
    score, pool = {}, []
    for c in units:
        if c["sub1"] in subs:
            s = c["sub1_p"] or 0
        elif c["sub2"] in subs and (c["sub2_p"] or 0) >= sub2_min:
            s = c["sub2_p"]
        else:
            s = 0
        if s:
            pool.append(c)
            score[c["hash"]] = 1 + s
    if len(pool) < rc["pool_min"]:
        have = {c["hash"] for c in pool}
        for c in units:
            if c["hash"] in have:
                continue
            if c["top1"] in topset:
                s = c["top1_p"] or 0
            elif c["top2"] in topset and (c["top2_p"] or 0) >= sub2_min:
                s = c["top2_p"]
            else:
                s = 0
            if s:
                pool.append(c)
                score[c["hash"]] = 0.5 * s
    floor, hl = rc["recency_floor"], rc["recency_half_life_days"]
    pool.sort(key=lambda c: -score[c["hash"]] * (floor + (1 - floor) * recency(c["date"], today, hl)))

    # Rare words: topic routing finds by meaning, rare words find by spelling (names, IDs).
    rare, seen = [], set()
    lowered = None
    for t in tokens:
        if lowered is None:
            lowered = [(c, c["chunk"].lower()) for c in units]
        tl = t.lower()
        if tl.startswith(DATE_TOKEN):  # "date:2": numeric m/d dates of that month (recall.rare_normalize)
            rx = date_token_regex(tl)
            hits = [c for c, low in lowered if rx.search(low)]
        else:
            hits = [c for c, low in lowered if tl in low]
        if 1 <= len(hits) <= rc["rare_max_df"]:
            for c in hits:
                if c["hash"] not in seen:
                    seen.add(c["hash"])
                    rare.append(c)
    rest = [c for c in pool if c["hash"] not in seen]
    cands = (rare + rest)[: int(rc["cap"])]
    return cands, {"pool": len(pool), "rare": len(rare), "cands": len(cands)}


def excerpt(cfg, c: dict) -> str:
    rc = cfg["recall"]
    text = c["chunk"]
    for pat in rc["excerpt_strip_patterns"]:
        text = re.sub(pat, " ", text)
    return f"[{c['date']}] " + " ".join(text.split())[: int(rc["excerpt_chars"])]


def order_picks(cfg, probs: dict, byid: dict, today: date) -> list[str]:
    rc = cfg["recall"]
    w, hl = rc["pick_recency_weight"], rc["recency_half_life_days"]
    ok = [k for k in probs if k in byid and probs[k] >= rc["pick_min_p"]]
    ok.sort(key=lambda k: -probs[k] * (1 - w + w * recency(byid[k]["date"], today, hl)))
    return ok[: int(rc["max_picks"])]


def pick_rounds(cfg, client, deadline: Deadline, state, cands: list, calls: list, today: date) -> tuple[list, dict]:
    """Iterative pick (config recall.pick_rounds >= 2; the default 1 is the legacy single pick).

    Round r asks the same `choice` question over the candidates left. Chunks with
    p >= pick_round_min_p[r] are injected (round 1: if none reach the cut, the single best
    one is injected). Then every chunk with p >= pick_min_p (or, if none, the best one) is
    removed from the pool; with `exclude_picked_sessions` the other chunks of the *injected*
    chunks' sessions (same relpath) are removed as well, and an empty pool ends the loop.
    Result: injected chunks in round order (p order inside a round), at most max_picks.
    Returns (hits, {"rounds": [{id: p}, ...], "round_of": {id: round}})."""
    rc = cfg["recall"]
    cuts = list(rc["pick_round_min_p"])
    nrounds = max(1, min(int(rc["pick_rounds"]), len(cuts)))
    byid = {f"c{i}": c for i, c in enumerate(cands)}
    rem = list(byid)
    out, rounds, round_of = [], [], {}
    for r in range(nrounds):
        if not rem or len(out) >= int(rc["max_picks"]):
            break
        crit = {k: excerpt(cfg, byid[k]) for k in rem}
        q = {"q": {"type": "choice", "instructions": rc["pick_instructions"], "criteria": crit}}
        try:
            probs = _jev(client, deadline, state, q, calls, "pick" if r == 0 else f"pick{r + 1}")["q"]["probabilities"]
        except Exception:
            if r == 0:
                raise  # no result yet: the caller falls back
            break  # keep what the earlier rounds picked
        pr = {k: probs.get(k, 0.0) for k in rem}
        rounds.append(pr)
        ok = sorted((k for k in rem if pr[k] >= cuts[r]), key=lambda k: -pr[k])
        if r == 0 and not ok:
            ok = [max(rem, key=lambda k: pr[k])]
        for k in ok:
            out.append(k)
            round_of[k] = r + 1
        gone = {k for k in rem if pr[k] >= rc["pick_min_p"]} or {max(rem, key=lambda k: pr[k])}
        if rc["exclude_picked_sessions"]:
            sessions = {byid[k]["relpath"] for k in ok}
            gone |= {k for k in rem if byid[k]["relpath"] in sessions}
        rem = [k for k in rem if k not in gone]
    out = out[: int(rc["max_picks"])]
    return [byid[k] for k in out], {"rounds": rounds, "round_of": round_of, "picked": out}


def jev_recall(cfg, tax, client, units_loader, message: str, tokens: list[str], prior: list[str],
               deadline: Deadline, today: date | None = None, info: dict | None = None,
               calls: list | None = None) -> tuple[list, dict, list]:
    """Returns (hits, info, calls). Raises on any Jev failure (caller falls back); pass
    `info`/`calls` to keep partial diagnostics when that happens."""
    today = today or date.today()
    info = {} if info is None else info
    calls = [] if calls is None else calls
    state = build_state(cfg, message, prior, today)
    routed, _ = route(cfg, tax, client, state, deadline, calls)
    info["route"] = [(t, s, round(p, 3)) for t, s, p in routed]
    if routed[0][0] == taxmod.smalltalk(tax):
        info["smalltalk"] = True
        return [], info, calls
    units = units_loader()
    cands, pinfo = build_pool(cfg, units, routed, tokens, today)
    info.update(pinfo)
    if not cands:
        return [], info, calls
    if int(cfg["recall"]["pick_rounds"]) > 1:
        hits, rinfo = pick_rounds(cfg, client, deadline, state, cands, calls, today)
        info["picks"] = [(c["relpath"], c["date"], round(rinfo["rounds"][rinfo["round_of"][k] - 1][k], 3))
                         for k, c in zip(rinfo["picked"], hits)]
        return hits, info, calls
    byid = {f"c{i}": c for i, c in enumerate(cands)}
    crit = {k: excerpt(cfg, c) for k, c in byid.items()}
    q = {"q": {"type": "choice", "instructions": cfg["recall"]["pick_instructions"], "criteria": crit}}
    probs = _jev(client, deadline, state, q, calls, "pick")["q"]["probabilities"]
    picks = order_picks(cfg, probs, byid, today)
    info["picks"] = [(byid[k]["relpath"], byid[k]["date"], round(probs[k], 3)) for k in picks]
    return [byid[k] for k in picks], info, calls


def has_labeled_units(cfg, timeout: float = HOOK_DB_TIMEOUT) -> bool:
    con = store.connect(cfg["data"]["episodic_db"], readonly=True, timeout=timeout)
    try:
        return con.execute("SELECT 1 FROM units WHERE labeled_at IS NOT NULL LIMIT 1").fetchone() is not None
    finally:
        con.close()


def load_units(cfg, timeout: float = HOOK_DB_TIMEOUT) -> list[dict]:
    con = store.connect(cfg["data"]["episodic_db"], readonly=True, timeout=timeout)
    try:
        return store.labeled_units(con)
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# FTS5 fallback
# --------------------------------------------------------------------------- #
def _decay_penalty(d, today: date, weight: float, tau: float) -> float:
    """weight * (1 - exp(-age/tau)); tau is a time constant, not a half-life.
    Unknown date -> max penalty; future date -> 0."""
    m = DATE_RE.search(str(d or ""))
    if not m:
        return weight
    try:
        age = (today - date.fromisoformat(m.group(1))).days
    except ValueError:
        return weight
    if age <= 0:
        return 0.0
    return weight * (1 - math.exp(-age / tau))


def bm25_threshold(fb: dict, n_chunks: int) -> float:
    """Resolve the bm25 cut-off. "auto" = reference threshold x (max IDF at this corpus
    size / max IDF at the reference size); bm25 scores shrink roughly with IDF."""
    t = fb.get("bm25_threshold", "auto")
    if t != "auto":
        return float(t)
    ref_n = max(2, int(fb.get("bm25_ref_corpus", 2000)))
    n = max(2, int(n_chunks))

    def max_idf(k):
        return math.log((k - 0.5) / 1.5 + 1)

    return float(fb.get("bm25_ref_threshold", -10.0)) * min(1.0, max_idf(n) / max_idf(ref_n))


def fts_search(cfg, con: sqlite3.Connection, tokens: list[str], today: date | None = None) -> list[dict]:
    """Keyword fallback. Filter by raw bm25 first, then rerank with time decay (decay never
    affects recall). Trigram FTS cannot match 2-char tokens (common in Korean), so those
    use LIKE with a document-frequency cap to skip overly common words."""
    fb = cfg["fallback"]
    today = today or date.today()
    pool = int(fb["candidate_pool"])
    trigram = store.fts_is_trigram(con)
    total = con.execute("SELECT count(*) FROM units_fts").fetchone()[0]
    threshold = bm25_threshold(fb, total)
    tokens = [w for w in tokens if not w.startswith(DATE_TOKEN)]  # rare-word matching only
    match_toks = [w for w in tokens if len(w) >= 3] if trigram else list(tokens)

    def match(joiner):
        if not match_toks:
            return []
        q = joiner.join(f'"{w}"' for w in match_toks)
        try:
            return con.execute(
                "SELECT relpath, date, chunk, bm25(units_fts) AS score FROM units_fts "
                "WHERE units_fts MATCH ? ORDER BY score LIMIT ?", (q, pool)).fetchall()
        except sqlite3.OperationalError:
            return []

    rows = match(" AND ") if len(match_toks) >= 2 else []
    if not rows:
        rows = match(" OR ")
    hits = [r for r in rows if r[3] is not None and r[3] <= threshold]

    like_rows = []
    short = [w for w in tokens if len(w) == 2] if trigram else []
    if short:
        cap = max(total * fb["like_max_df_ratio"], 5)
        kept = [w for w in short
                if con.execute("SELECT count(*) FROM units_fts WHERE chunk LIKE ?", (f"%{w}%",)).fetchone()[0] <= cap]
        if kept:
            where = " OR ".join(["chunk LIKE ?"] * len(kept))
            like_rows = con.execute(
                f"SELECT relpath, date, chunk, ? AS score FROM units_fts WHERE ({where}) LIMIT ?",
                [threshold] + [f"%{w}%" for w in kept] + [pool]).fetchall()

    seen, merged = set(), []
    for r in hits + like_rows:
        key = (r[0], r[2])
        if key in seen:
            continue
        seen.add(key)
        merged.append({"relpath": r[0], "date": r[1], "chunk": r[2], "score": r[3]})
    merged.sort(key=lambda r: r["score"] + _decay_penalty(r["date"], today, fb["decay_weight"], fb["decay_tau_days"]))
    return merged[: int(fb["max_hits"])]


def fts_fallback(cfg, tokens, today=None, timeout: float = 0.5) -> list[dict]:
    db = Path(cfg["data"]["episodic_db"])
    if not db.exists() or not tokens:
        return []
    try:
        con = store.connect(db, readonly=True, timeout=timeout)
        try:
            return fts_search(cfg, con, tokens, today)
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return []


# --------------------------------------------------------------------------- #
# Injection text: full bullet / section, raw pointer, character budget
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    return " ".join((text or "").split())


def expand(cfg, c: dict, mode: str) -> str | None:
    """The whole bullet(s) ("bullet") or the whole section ("section") a chunk came from,
    re-read from its log file. None when the file or the chunk can no longer be found
    (e.g. the log was edited after labeling); the caller then uses the chunk."""
    if mode not in ("bullet", "section"):
        return None
    try:
        path = Path(cfg["logs"]["dir"]) / c["relpath"]
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, KeyError, TypeError):
        return None
    chunk = c["chunk"].strip()
    if not chunk:
        return None
    for head, lines in chunking.sections(text, c["relpath"]):
        items = chunking.split_items(lines)
        full = "\n".join(([head] if head else []) + items)
        # A chunk is "heading + some of the section's bullets" (per-bullet summary chunks
        # skip the bullets in between) or a hard-cut piece of that; match its body.
        if head and chunk.startswith(head):
            body = chunk[len(head):]
        elif chunk.startswith("#"):
            continue  # a chunk that starts with another section's heading
        else:
            body = chunk
        nb = _norm(body)
        if not nb or nb not in _norm("\n".join(items)):
            if not (head and _norm(chunk) in _norm(full)):
                continue
        if mode == "section":
            return full
        sel = []
        for it in items:
            ni = _norm(it)
            if ni and (ni in nb or (nb and nb in ni) or ni[:60] in nb or ni[-60:] in nb):
                sel.append(it)
        if not sel:
            return None
        return "\n".join(([head] if head else []) + sel)
    return None


def render_hits(cfg, header: str, hits: list[dict], max_chars: int = 400) -> str:
    """Injection block for recalled chunks. With `recall.expand` = "bullet"/"section" each
    pick is replaced by its whole bullet/section (identical expansions injected once), cut
    at `expand_max_chars`; otherwise the chunk is cut at `max_chars`. All items together
    stay within `max_inject_chars` (the first item is always kept). Summary chunks get the
    raw pointer line when `recall.raw_pointer` is on and the chat log covers their date."""
    from . import raw
    rc = cfg["recall"]
    mode = rc.get("expand", "none") or "none"
    per = int(rc.get("expand_max_chars", 1200)) if mode != "none" else int(max_chars)
    budget = int(rc.get("max_inject_chars", 4000))
    lines, used, shown = [header], 0, []
    for c in hits:
        text = None
        if mode != "none":
            try:
                text = expand(cfg, c, mode)
            except Exception:
                text = None
        text = (text or c["chunk"]).strip()
        nt = _norm(text)
        if any(nt in s for s in shown):
            continue  # already injected (two picks from one section)
        shown.append(nt)
        if len(text) > per:
            text = text[:per] + " ..."
        if used and used + len(text) > budget:
            left = budget - used
            if left < 80:
                break
            text = text[:left] + " ..."
        used += len(text)
        ptr = raw.pointer_line(cfg, c.get("relpath", ""), c.get("date", ""), c["chunk"])
        lines.append(f"\n[{c['date']}]\n{text}" + (f"\n{ptr}" if ptr else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Recent-conversation sources (pluggable)
# --------------------------------------------------------------------------- #
def _tail_lines(path: Path, nbytes: int = 65536) -> list[str]:
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        data = f.read().decode("utf-8", errors="replace").splitlines()
    return data[1:] if size > nbytes else data


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def prior_lines(cfg, current: str, hook_input: dict | None = None) -> list[str]:
    """Last N conversation lines before the current message; [] on any problem."""
    ctx = cfg["context"]
    n = int(ctx["lines"])
    src = ctx.get("source", "none")
    if n <= 0 or src == "none":
        return []
    try:
        recs = []  # (is_user, text)
        if src in ("jsonl", "capture"):
            path = cfg["capture"]["path"] if src == "capture" else ctx.get("jsonl_path")
            if not path or not Path(path).exists():
                return []
            for line in _tail_lines(Path(path)):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                role = r.get("role", r.get("dir"))
                text = r.get("text", r.get("content"))
                if role is None or text is None:
                    continue
                recs.append((role in ("user", "in", "human"), _text_of(text)))
        elif src == "claude_transcript":
            tp = (hook_input or {}).get("transcript_path")
            if not tp:
                return []
            for line in _tail_lines(Path(tp).expanduser(), 262144):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") not in ("user", "assistant"):
                    continue
                text = _text_of((r.get("message") or {}).get("content"))
                if text.strip():
                    recs.append((r["type"] == "user", text))
        else:
            return []
        # The current message may already be in the log (the capture hook can run first).
        # With several <channel> blocks in one prompt, each block is its own record.
        prompt = hook_input.get("prompt") if isinstance(hook_input, dict) else None
        blocks = message_blocks(prompt) if isinstance(prompt, str) else []
        current_texts = {" ".join(current.split())} | {" ".join(b.split()) for b in blocks}
        for _ in range(max(1, len(blocks))):
            if recs and recs[-1][0] and " ".join(recs[-1][1].split()) in current_texts:
                recs = recs[:-1]
        return [(ctx["user_label"] if u else ctx["assistant_label"]) + ": " + " ".join(t.split())[:200]
                for u, t in recs[-n:]]
    except Exception:
        return []

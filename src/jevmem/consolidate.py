"""`jevmem profile consolidate`: daily consolidation of profile memory.

Like memory consolidation during sleep: once a day, distill at most 3 evidence-backed
user traits from ONE day's log into profile memory. (This is not the agent reflecting on
itself; it only extracts durable facts about the user.)

  1. read <logs>/<date>.md (default: yesterday)
  2. ask the text LLM ([llm].backend, then [llm].fallback: claude -p / ollama) for up to
     `max_traits` candidates in strict JSON: kind, statement, quote, optional emotion,
     relation (new|merge|supersede), target_id. Only the first `max_log_chars` (16,000)
     characters of the log are sent; the rest of a long day is not seen.
  3. validate every candidate in code, whatever the LLM said:
       - kind is one of the six kinds; the statement is not a mood/emotion
       - the quote occurs VERBATIM in that day's log (whitespace-normalized)
       - merge/supersede targets must be ACTIVE traits; otherwise treated as new
       - a "new" that closely matches an existing trait (lexical similarity >= 0.6) is merged
       - a "new" that closely matches an earlier candidate of the same run is merged into the
         trait that candidate just created (or skipped), so one run never creates the same
         trait twice
  4. write through the same gateway as `jevmem profile new|merge|supersede`
     (per-day budget, idempotency).

An empty (or whitespace-only) log means no LLM call. `--dry-run` never calls the LLM and
writes nothing: it reports what would be sent (log size, active traits, remaining budget).
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sqlite3
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path

from . import llm as llmmod
from . import profile, status, store
from .text import clean_tokens

# Mood words. English: whole words with their common inflections ("happier", "frustrating"),
# except "stress" used as a technical noun ("stress test"). Korean: word stems; "화가" only
# as "화가 나다" (화가 alone is "painter"), "지침" left out ("guideline").
EMOTION_EN_RE = re.compile(
    r"\b(?:sad(?:ness|der|dest|ly)?|happ(?:y|ier|iest|ily|iness)|unhappy|angr(?:y|ier|ily)|anger|"
    r"upset(?:ting)?|stress(?:ed|ful)|stress(?!\s*-?\s*test)|anxi(?:ous|ety|eties)|"
    r"worr(?:y|ied|ies|ying)|depress(?:ed|ion|ing)|lonel(?:y|iness)|frustrat(?:ed|ing|ion)|"
    r"annoy(?:ed|ing|ance)|irritat(?:ed|ing|ion)|excited|tired|exhausted|overwhelm(?:ed|ing)|"
    r"moods?|moody|furious|scared|afraid|fear(?:ful|s)?|nervous|hurt|jealous|guilty|"
    r"miserable|lonesome|grumpy|cheerful|joyful)\b", re.I)
EMOTION_KO_RE = re.compile(
    r"슬프|슬퍼|슬픔|우울|불안|짜증|화가\s*(?:나|났|날|남)|화난|화났|화내|분노|"
    r"스트레스(?!\s*테스트)|외로|피곤|지쳐|지친|기분|속상|서운|걱정|초조|신나|신난|기뻐|기쁨|기쁘|"
    r"답답|괴로|행복|무서|무섭|두려|긴장")


def is_emotional(statement: str) -> bool:
    return bool(EMOTION_EN_RE.search(statement) or EMOTION_KO_RE.search(statement))


def _norm(s: str) -> str:
    return " ".join(str(s).split())


def quote_in_log(quote: str, log_text: str) -> bool:
    return profile.quote_in_text(quote, log_text)


def find_log(cfg, day: str) -> Path | None:
    logs = Path(cfg["logs"]["dir"])
    p = logs / f"{day}.md"
    if p.exists():
        return p
    for q in sorted(logs.glob(cfg["logs"]["glob"])):
        if day in q.name:
            return q
    return None


def source_ref(cfg, path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(cfg.root).resolve()))
    except ValueError:
        return str(path)


def active_traits(cfg) -> list:
    p = Path(cfg["data"]["profile_db"])
    if not p.exists():
        return []
    con = store.connect_ro(p, timeout=2)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("SELECT id, kind, statement, confidence FROM traits WHERE status='active'"
                           " ORDER BY confidence DESC, id ASC").fetchall()
    finally:
        con.close()


def build_prompt(cfg, day: str, log_text: str, existing) -> str:
    cc = cfg["consolidate"]
    ex = "\n".join(f"#{r['id']} [{r['kind']}] {r['statement']}"
                   for r in existing[: int(cc["max_existing_traits"])]) or "(none yet)"
    return cc["prompt"].format(date=day, max_traits=cc["max_traits"], language=cc["language"],
                               existing=ex, log=log_text[: int(cc["max_log_chars"])])


def parse_candidates(text: str | None, limit: int) -> list[dict] | None:
    data = llmmod.parse_json(text)
    if isinstance(data, dict):
        data = data.get("traits")
    if not isinstance(data, list):
        return None
    out = []
    for c in data:
        if isinstance(c, dict):
            out.append(c)
        if len(out) >= limit:
            break
    return out


def decide(cfg, cands: list[dict], log_text: str, existing) -> list[dict]:
    """Validate and turn LLM candidates into actions. Pure (no writes).

    A "new" that is as similar to an EARLIER "new" candidate of this same list as a merge
    would require gets `same_as` = that candidate's index (run() merges it into the trait
    the earlier one created, or skips it)."""
    cc = cfg["consolidate"]
    active = {r["id"]: r for r in existing}
    fresh: list[dict] = []  # earlier "new" candidates of this run, as rows for scoring
    decisions = []
    for c in cands:
        kind = str(c.get("kind") or "").strip().lower()
        stmt = _norm(c.get("statement") or "")
        quote = _norm(c.get("quote") or "").strip("\"'“”‘’ ")
        emotion = c.get("emotion") or None
        d = {"kind": kind, "statement": stmt, "quote": quote, "emotion": emotion}
        if kind not in profile.KINDS:
            decisions.append({**d, "action": "reject", "reason": f"kind {kind!r} not allowed"})
            continue
        if not stmt or len(stmt) > 200:
            decisions.append({**d, "action": "reject", "reason": "empty or overlong statement"})
            continue
        if is_emotional(stmt):
            decisions.append({**d, "action": "reject", "reason": "emotion/mood cannot be a trait"})
            continue
        if len(quote) < int(cc["min_quote_chars"]) or not quote_in_log(quote, log_text):
            decisions.append({**d, "action": "reject", "reason": "quote not found verbatim in the log"})
            continue
        rel = str(c.get("relation") or "new").lower()
        try:
            tid = int(c.get("target_id")) if c.get("target_id") is not None else None
        except (TypeError, ValueError):
            tid = None
        toks = set(clean_tokens(stmt, limit=64))
        scored = profile.score_rows_against_tokens(existing, toks)[:5]
        if rel in ("merge", "supersede") and tid in active:
            action, target = rel, tid
        else:
            action, target = "new", None
            if scored and scored[0]["score"] >= float(cc["merge_if_similarity"]):
                action, target = "merge", scored[0]["id"]
        dec = {**d, "action": action, "target_id": target,
               "similar": [(s["id"], s["score"]) for s in scored[:3]]}
        if action == "new":
            same = [f["id"] for f in fresh if f["statement"].lower() == stmt.lower()]
            twin = profile.score_rows_against_tokens(fresh, toks)[:1]
            if not same and twin and twin[0]["score"] >= float(cc["merge_if_similarity"]):
                same = [twin[0]["id"]]
            if same:
                dec.update(action="merge", target_id=None, same_as=same[0])
            else:
                fresh.append({"id": len(decisions), "statement": stmt, "kind": kind, "confidence": 0.0})
        decisions.append(dec)
    return decisions


def run(cfg, day: str | None = None, backend=None, dry_run: bool = False, backfill: bool = False,
        out=print) -> dict:
    day = day or (date.today() - timedelta(days=1)).isoformat()
    log_path = find_log(cfg, day)
    if log_path is None:
        out(f"no log for {day} — nothing to consolidate")
        if not dry_run:
            status.record(cfg, "consolidate", "no-log", day)
        return {"result": "no-log", "date": day}
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    if not log_text.strip():
        out(f"log for {day} is empty — nothing to consolidate (no LLM call)")
        if not dry_run:
            status.record(cfg, "consolidate", "empty-log", day)
        return {"result": "empty-log", "date": day}
    backend = backend or llmmod.get_backend(cfg)
    if isinstance(backend, llmmod.NoneBackend):
        out("llm backend is 'none' — daily consolidation needs ollama or claude")
        return {"result": "no-backend", "date": day}
    existing = active_traits(cfg)
    budget = _budget(cfg, day, backfill)
    src = source_ref(cfg, log_path)
    if dry_run:
        limit = int(cfg["consolidate"]["max_log_chars"])
        res = {"result": "dry-run", "date": day, "source": src, "log_chars": len(log_text),
               "sent_chars": min(len(log_text), limit), "active_traits": len(existing),
               "max_traits": int(cfg["consolidate"]["max_traits"]), "budget_left": budget,
               "llm_called": False}
        out(json.dumps(res, ensure_ascii=False, indent=2))
        out(f"[dry-run] would send the first {res['sent_chars']} of {len(log_text)} characters of {src} "
            f"and {len(existing)} active trait(s) to the LLM ({getattr(backend, 'name', '?')}) for up to "
            f"{res['max_traits']} candidates; LLM not called, nothing written")
        return res
    answer = backend.complete(build_prompt(cfg, day, log_text, existing))
    cands = parse_candidates(answer, int(cfg["consolidate"]["max_traits"]))
    if cands is None:
        note = llmmod.unavailable_note(backend) if answer is None else None
        if note:
            out(f"LLM unavailable ({note}) — nothing written; "
                f"run `jevmem profile consolidate --date {day}` later")
            status.record(cfg, "consolidate", "llm-unavailable", f"{day}: {note}")
            return {"result": "llm-unavailable", "date": day, "note": note}
        out("LLM returned no parsable JSON — nothing written")
        status.record(cfg, "consolidate", "bad-llm-output", day)
        return {"result": "bad-llm-output", "date": day}

    decisions = decide(cfg, cands, log_text, existing)
    used = 0
    for d in decisions:
        if d["action"] == "reject":
            continue
        if "same_as" in d:
            first = decisions[d["same_as"]]
            if not first.get("trait_id"):
                d["action"], d["reason"] = "skip", f"same trait as candidate {d['same_as'] + 1} of this run"
                continue
            d["target_id"] = first["trait_id"]
        if used >= budget:
            d["action"], d["reason"] = "skip", "daily budget exhausted"
            continue
        args = Namespace(db=None, kind=d["kind"], statement=d["statement"], subject=None, source=src,
                         source_date=day, quote=d["quote"], emotion=d["emotion"], commit=True,
                         backfill=backfill, id=d["target_id"])
        fn = {"new": profile.cmd_new, "merge": profile.cmd_merge}.get(d["action"], profile.cmd_supersede)
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = fn(cfg, args)
        d["detail"] = " ".join((buf_out.getvalue() + buf_err.getvalue()).split())[:300]
        already = rc == 0 and d["detail"].startswith("already recorded")
        d["status"] = "already recorded" if already else ("written" if rc == 0 else f"refused (exit {rc})")
        if rc == 0:
            if not already:
                used += 1  # nothing was written for an "already recorded": no budget used
            # "new: trait 7 created", "merge: trait 7 conf -> 0.4", "already recorded — trait 7 …",
            # "supersede: trait 3 -> 7 (…)": the trait that now holds the evidence
            m = (re.search(r"\btrait \d+ -> (\d+)\b", d["detail"]) or re.search(r"\btrait (\d+)\b", d["detail"]))
            d["trait_id"] = int(m.group(1)) if m else d.get("target_id")
    summary = {"result": "ok", "date": day, "source": src, "candidates": len(cands), "decisions": decisions}
    out(json.dumps(summary, ensure_ascii=False, indent=2))
    written = sum(1 for d in decisions if d.get("status") == "written")
    status.record(cfg, "consolidate", "ok", f"{day}: {written} written, {len(decisions)} candidates")
    return summary


def _budget(cfg, day: str, backfill: bool) -> int:
    """Same rule as the CLI gateway (per source date + per wall-clock day)."""
    db = Path(cfg["data"]["profile_db"])
    if not db.exists():
        return int(cfg["profile"]["daily_cap"])
    con = profile.connect(db)
    try:
        return profile.remaining_budget(cfg, con, day, backfill=backfill)
    finally:
        con.close()

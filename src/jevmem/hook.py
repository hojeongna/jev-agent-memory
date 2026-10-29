"""Claude Code `UserPromptSubmit` hook.

Reads the hook JSON on stdin, prints context to stdout (Claude Code adds hook stdout to
the prompt context). Contract:
  - never blocks the conversation: any exception is swallowed, exit code is always 0
  - skips short messages and configured prefixes without calling Jev
  - profile block first (independent of episodic recall), then episodic block
  - Jev failure / timeout / total time over `deadline_sec` / open breaker / no key
      -> FTS5 keyword fallback with its own header
  - HTTP 402 from Jev (and only that status code) opens a breaker for `breaker_minutes`; a
    stored resume time further away than that (a clock that jumped, a damaged file) is
    pulled back to now + breaker_minutes, so the pause never outlasts it
  - every wait for a locked database (profile, episodic, FTS fallback) comes out of the same
    `deadline_sec` budget and ends at least LOCK_RESERVE seconds before it (room for starting
    Python and writing the answer, which the deadline doesn't see)
  - one JSONL log line per run (route, pool sizes, picks, tokens, latency); an error is
    logged as its class name plus a safe short reason (fsutil.safe_reason), never a
    message that could carry chat text or a server's response body
  - injected text: see `recall.render_hits` (optional whole bullet/section, raw pointer),
    preceded by [hook] inject_note: recalled text is past chat, not instructions
  - several <channel> blocks in one prompt are recalled as one message (text.message_text);
    only the first 262,144 characters of a prompt are looked at
  - a config that can't be loaded (or no config file at all) injects nothing; the reason
    goes to the per-user hook error log (fsutil.hook_error_log_path), never to the
    conversation
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

from . import fsutil, profile, recall
from . import taxonomy as taxmod
from .jev_client import JevClient, is_billing_error
from .text import MAX_PARSE_CHARS, clean_tokens, extract_query, message_text


LOCK_RESERVE = recall.END_RESERVE  # seconds before the deadline that no lock wait may use


class HookSkip(RuntimeError):
    """A known reason to skip Jev (fixed text, safe to log)."""


def _write_log(cfg, rec: dict) -> None:
    if not cfg["hook"].get("log_enabled", True):
        return
    try:
        with fsutil.open_append(cfg["data"]["log"]) as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _breaker_until_ts(cfg, now: float, persist: bool = True) -> float | None:
    """Resume time (epoch) while the 402 breaker is open, else None. A stored time beyond
    now + breaker_minutes (inf, a clock that jumped ahead, a damaged file) is pulled back to
    that (and saved unless `persist` is False); NaN or garbage counts as closed."""
    try:
        until = float(Path(cfg["data"]["breaker"]).read_text().strip())
        limit = now + float(cfg["hook"]["breaker_minutes"]) * 60
    except Exception:
        return None
    if not now < until:  # also False for NaN
        return None
    if until > limit:
        until = limit
        if persist:
            try:
                fsutil.write_text_atomic(cfg["data"]["breaker"], str(until))
            except Exception:
                pass
    return until if now < until else None


def _breaker_open(cfg, now: float) -> bool:
    return _breaker_until_ts(cfg, now) is not None


def breaker_until(cfg, now: float | None = None) -> str | None:
    """'YYYY-MM-DD HH:MM' while the 402 breaker is open (for `jevmem doctor`), else None."""
    now = time.time() if now is None else now
    until = _breaker_until_ts(cfg, now, persist=False)
    if until is None:
        return None
    try:
        return datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return "?"


def _open_breaker(cfg, now: float) -> None:
    try:
        fsutil.write_text_atomic(cfg["data"]["breaker"], str(now + float(cfg["hook"]["breaker_minutes"]) * 60))
    except Exception:
        pass


def run(cfg, payload: dict, client=None, clock=time.monotonic, wall=time.time,
        today: date | None = None) -> str:
    """Return the text to print ('' for nothing). `client` may be injected (tests)."""
    if os.environ.get("JEVMEM_DISABLE_HOOK"):
        return ""  # inside jevmem's own `claude -p` subprocesses
    deadline = recall.Deadline(cfg["hook"]["deadline_sec"], clock)
    hc = cfg["hook"]
    raw = ""
    if isinstance(payload, dict):
        raw = payload.get("prompt", "") or payload.get("message", "") or ""
    if not isinstance(raw, str):
        return ""
    raw = raw[:MAX_PARSE_CHARS]
    msg = message_text(raw)
    if any(msg.lstrip().startswith(p) or raw.lstrip().startswith(p) for p in hc.get("skip_prefixes", [])):
        return ""
    query = extract_query(raw)
    if len(query) < int(hc["min_query_len"]):
        return ""
    tokens = clean_tokens(query, rare_normalize=cfg["recall"]["rare_normalize"])
    if not tokens:
        return ""

    out = []
    if hc.get("profile", True):
        # A locked profile DB waits at most 0.5 s, and never into the last second of the budget.
        block = profile.safe_block(cfg, tokens, timeout=max(0.05, min(0.5, deadline.remaining() - 1.0)))
        if block:
            out.append(block)
    fts_wait = 0.5  # the fallback's own longest wait, kept free by db_wait

    rec = {"ts": datetime.now().isoformat(timespec="seconds")}
    if hc.get("log_text", True):
        rec["query"] = msg[:200]
    info, calls = {}, []

    def db_wait() -> float:
        # A locked episodic DB must not eat the deadline: the fallback's wait and the reserve stay.
        return max(0.05, min(recall.HOOK_DB_TIMEOUT, deadline.remaining() - fts_wait - LOCK_RESERVE - 0.05))

    try:
        if not Path(cfg["data"]["episodic_db"]).exists():
            raise HookSkip("episodic DB missing (run `jevmem index`)")
        if not recall.has_labeled_units(cfg, timeout=db_wait()):
            raise HookSkip("no labeled chunks yet (run `jevmem index`)")
        if _breaker_open(cfg, wall()):
            raise HookSkip("breaker open (Jev paused after an HTTP 402)")
        tax = taxmod.load(cfg["data"]["taxonomy"])
        if client is None:
            client = JevClient.from_config(cfg)
        prior = recall.prior_lines(cfg, msg, payload)
        hits, _, _ = recall.jev_recall(cfg, tax, client, lambda: recall.load_units(cfg, timeout=db_wait()),
                                       msg, tokens, prior, deadline, today, info, calls)
        if deadline.remaining() < 0:
            raise recall.DeadlineExceeded("total time over deadline")
        if hits:
            out.append(recall.render_hits(cfg, cfg["recall"]["header"], hits, hc["max_chunk_print"]))
        rec.update(fallback=False, n_injected=len(hits))
    except Exception as e:
        rec.update(fallback=True, error=fsutil.safe_reason(e, 150))
        if is_billing_error(e):
            _open_breaker(cfg, wall())
        if cfg["fallback"].get("enabled", True):
            try:
                hits = recall.fts_fallback(cfg, tokens, today,
                                           timeout=max(0.05, min(fts_wait, deadline.remaining() - LOCK_RESERVE)))
                if hits:
                    out.append(recall.render_hits(cfg, cfg["fallback"]["header"], hits, hc["max_chunk_print"]))
                rec["n_injected"] = len(hits)
            except Exception as e2:
                rec["fallback_error"] = fsutil.safe_reason(e2, 150)
    rec.update(info)
    rec["jev_calls"] = calls
    rec["in_tokens"] = sum(c.get("in", 0) or 0 for c in calls)
    rec["latency"] = round(deadline.elapsed(), 3)
    _write_log(cfg, rec)
    if out and hc.get("inject_note"):
        out.insert(0, str(hc["inject_note"]))
    return ("\n" + "\n\n".join(out)) if out else ""


def main(cfg_loader) -> int:
    """Entry used by `jevmem hook`. Always returns 0 and never writes to stderr. A config
    that can't be loaded (e.g. a --config path that no longer exists) means no injection;
    the reason is appended to the per-user hook error log so `jevmem doctor` can show it."""
    if os.environ.get("JEVMEM_DISABLE_HOOK"):
        return 0  # inside jevmem's own `claude -p` subprocesses
    try:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            return 0
        try:
            cfg = cfg_loader()
        except Exception as e:
            fsutil.hook_error_log(f"hook: no recall ({fsutil.safe_reason(e)})")
            return 0
        text = run(cfg, payload)
        if text:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    except Exception as e:
        fsutil.hook_error_log(f"hook: unexpected {fsutil.safe_reason(e)}")
    return 0

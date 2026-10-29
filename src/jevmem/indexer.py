"""`jevmem index`: chunk daily logs, then label new/changed chunks (Claude Haiku by
default, or Jev).

- Key = sha1(relpath + chunk). Only new or changed chunks are labeled; vanished ones are
  deleted, except when the logs folder is missing, unreadable or has no matching file, or
  when at least 2 log files and more than half of all indexed ones are gone at once: such a
  run keeps everything (`logs_held`) unless `--force-prune`. Unchanged files + nothing
  pending -> exits immediately (safe for a 30-min cron).
- Labeler (`[index].labeler`):
    haiku (default): batches of ~20 chunks per isolated `claude -p --model haiku` call,
      hierarchical (top-level, then a subtopic within it, optional second choice); stored
      p = 0.9 / 0.3. A chunk Haiku answered for without a valid label is retried next run;
      after `haiku_max_failures` such answers it falls back to Jev when a key is configured.
    jev: ONE Jev call per chunk with two `choice` questions (top-level and flat subtopic,
      long "labeling" descriptions); top-2 of each with probabilities.
  Each unit records its labeler.
- Outages are not the chunks' fault: a `claude -p` call that fails (network, login expired,
  usage limit, timeout) or is skipped because the call breaker is open (llm.CallBreaker:
  30 min, 1 h, 2 h, then 4 h between attempts) stops Haiku labeling for this run and
  changes no chunk's `tries`/`hfail`. Jev answering HTTP 402, or not answering at all
  (timeout, connection/DNS error, HTTP 5xx or 429), likewise stops this run's Jev labeling
  without counting against any chunk.
- Failure cap: every attempt whose answer came back unusable (Haiku answered without a valid
  label for the chunk; Jev answered non-JSON, an error object or another 4xx) counts toward
  `index.max_tries`. A chunk that reaches it is given up: it stays unlabeled (or keeps its old
  label if it was stale), is reported as `gave_up`, and is not retried until `--retry-failed`.
- Relabeling is explicit: `--relabel` (all) or `--relabel-topic T` marks chunks stale.
  Stale chunks keep their old labels (still searchable) until a run relabels them, so a
  large relabel can be spread over many capped cron runs.
- Limits per run: at most `index.cap` chunks (default 400). Jev labeling is additionally
  trimmed to the call budget limits `index.max_cost_usd_per_run` and
  `index.max_cost_usd_per_day` (estimated from token counts, recorded per day); `--yes`
  lifts those two. Haiku labeling is limited by `cap` only. `--dry-run` prints counts and
  estimates without writing or calling anything.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from . import chunking, fsutil, labeler, store
from . import llm as llmmod
from . import taxonomy as taxmod
from .jev_client import is_billing_error, is_outage

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


def est_tokens(text: str) -> int:
    """Rough input-token estimate: ~1 token per non-ASCII char (CJK-heavy text measured
    ~1.1 chars/token), ~3.5 ASCII chars per token."""
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return int(math.ceil(non_ascii + (len(text) - non_ascii) / 3.5))


def label_state(relpath: str, chunk: str) -> str:
    return f"[file] {relpath}\n{chunk[:1500]}"


def label_questions(cfg, top: dict, flat: dict) -> dict:
    ins = cfg["index"]["label_instructions"]
    return {"top": {"type": "choice", "instructions": ins, "criteria": top},
            "flat": {"type": "choice", "instructions": ins, "criteria": flat}}


class LogsUnavailable(RuntimeError):
    """The logs directory is missing or can't be listed (fixed text, safe to log)."""


def log_files(cfg) -> list[Path]:
    """Daily log files matching [logs] glob. LogsUnavailable if the directory is missing or
    unreadable (an unmounted disk, a sync folder that is briefly gone, a typo in [logs] dir)."""
    logs = Path(cfg["logs"]["dir"])
    try:
        os.listdir(logs)
    except FileNotFoundError:
        raise LogsUnavailable(f"logs dir {logs} does not exist") from None
    except NotADirectoryError:
        raise LogsUnavailable(f"logs dir {logs} is not a directory") from None
    except OSError as e:
        raise LogsUnavailable(f"cannot read logs dir {logs} ({fsutil.safe_reason(e)})") from None
    return sorted(p for p in logs.glob(cfg["logs"]["glob"]) if p.is_file())


# A run that would delete the chunks of at least this many log files, and of more than half
# of all indexed files, is held unless --force-prune (a folder that came back empty or half
# synced, a mount with only some files): see vanished_problem.
PRUNE_HOLD_MIN_FILES = 2


def vanished_problem(cfg, indexed_relpaths, files: list[Path]) -> str | None:
    """Why deleting the chunks of the log files that are gone is held, or None."""
    logs = Path(cfg["logs"]["dir"])
    indexed = set(indexed_relpaths)
    gone = indexed - {chunking.relpath_of(p, logs) for p in files}
    if len(gone) >= PRUNE_HOLD_MIN_FILES and 2 * len(gone) > len(indexed):
        return (f"{len(gone)} of the {len(indexed)} indexed log files are gone from {logs} at once "
                "(a folder that came back empty or half synced?)")
    return None


def logs_problem(cfg) -> tuple[list[Path], str | None]:
    """(files, problem): problem when the folder is missing/unreadable or nothing matches."""
    try:
        files = log_files(cfg)
    except LogsUnavailable as e:
        return [], str(e)
    if not files:
        return [], f"no files match {cfg['logs']['glob']!r} in {cfg['logs']['dir']}"
    return files, None


def collect_chunks(cfg, files: list[Path] | None = None) -> dict:
    logs = Path(cfg["logs"]["dir"])
    ix = cfg["index"]
    cur = {}
    if files is None:
        if not logs.exists():
            return cur
        files = sorted(logs.glob(cfg["logs"]["glob"]))
    for p in files:
        if not p.is_file():
            continue
        for rel, d, ch in chunking.chunk_file(p, logs, max_chunk=ix["max_chunk"],
                                              hard_cut=ix["hard_cut"], min_chunk=ix["min_chunk"],
                                              split_heading_re=ix.get("one_chunk_per_bullet_heading") or None):
            cur[store.unit_hash(rel, ch)] = (rel, d, ch)
    return cur


def _signature(cfg) -> str:
    logs = Path(cfg["logs"]["dir"])
    files = sorted(p for p in logs.glob(cfg["logs"]["glob"]) if p.is_file()) if logs.exists() else []
    h = hashlib.sha1(json.dumps([(str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files]).encode())
    h.update(json.dumps([cfg["index"].get(k) for k in ("max_chunk", "hard_cut", "min_chunk",
                                                       "one_chunk_per_bullet_heading")]).encode())
    return h.hexdigest()


def per_chunk_tokens(cfg, tax, rows, con=None) -> tuple[list[int], str]:
    """Estimated input tokens per row [(hash, relpath, chunk), ...]."""
    top, flat = taxmod.labeling_criteria(tax)
    fixed = est_tokens(json.dumps(label_questions(cfg, top, flat), ensure_ascii=False))
    if con is not None and rows:
        try:
            r = con.execute("SELECT count(*), avg(in_tok) FROM units WHERE in_tok IS NOT NULL").fetchone()
        except Exception:
            r = None
        if r and r[0] and r[0] >= 10 and r[1]:
            return [int(r[1])] * len(rows), f"measured avg {int(r[1])} tok/chunk over {r[0]} labeled chunks"
    return [fixed + est_tokens(label_state(rel, ch)) for _, rel, ch in rows], "heuristic"


def _usd(cfg, tokens: int) -> float:
    return tokens / 1e6 * float(cfg["jev"]["price_usd_per_mtok"])


def run(cfg, client_factory=None, cap=None, workers=None, relabel=False, relabel_topic=None,
        retry_failed=False, dry_run=False, no_label=False, yes=False, out=print,
        labeler_backend=None, jev_available: bool | None = None, force_prune: bool = False) -> dict:
    """labeler_backend: text backend for Haiku labeling (default: isolated claude -p haiku).
    jev_available: whether Jev may be used (default: an API key is configured).
    force_prune: delete the indexed chunks even when the logs folder is missing or no file
    matches (otherwise those runs keep everything and report `logs_held`)."""
    tax = taxmod.load(cfg["data"]["taxonomy"])
    cap = cfg["index"]["cap"] if cap is None else cap
    workers = workers or cfg["index"]["workers"]
    max_tries = int(cfg["index"]["max_tries"])
    db_path = Path(cfg["data"]["episodic_db"])

    if dry_run:
        return _dry_run(cfg, tax, db_path, cap, relabel, relabel_topic, retry_failed, max_tries, out,
                        _jev_ok(cfg, jev_available), force_prune)

    fsutil.ensure_dir(cfg["data"]["dir"])
    lock_path = Path(cfg["data"]["lock"])
    fsutil.ensure_dir(lock_path.parent)
    lf = os.fdopen(os.open(lock_path, os.O_WRONLY | os.O_CREAT, fsutil.FILE_MODE), "w")
    try:
        if fcntl is not None:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                out("another indexer is running — exiting")
                return {"skipped": "locked"}
        con = store.connect(db_path)
        store.init_db(con)
        try:
            return _run_locked(cfg, tax, con, client_factory, cap, workers, relabel, relabel_topic,
                               retry_failed, no_label, yes, max_tries, out, labeler_backend,
                               _jev_ok(cfg, jev_available), force_prune)
        finally:
            con.close()
    finally:
        lf.close()


def _jev_ok(cfg, jev_available) -> bool:
    if jev_available is not None:
        return bool(jev_available)
    from .jev_client import JevError, load_api_key
    try:
        load_api_key(cfg)
        return True
    except JevError:
        return False


def _use_haiku(cfg) -> bool:
    return str(cfg["index"].get("labeler", "haiku")).lower() == "haiku"


def _split_rows(cfg, rows, jev_ok):
    """rows: [(hash, relpath, chunk, hfail)] -> (haiku_rows, jev_rows) as (hash, relpath, chunk)."""
    if not _use_haiku(cfg):
        return [], [(h, r, c) for h, r, c, _ in rows]
    limit = int(cfg["index"]["haiku_max_failures"])
    haiku, jev = [], []
    for h, r, c, hf in rows:
        (jev if (hf or 0) >= limit and jev_ok else haiku).append((h, r, c))
    return haiku, jev


def _pending_where(max_tries, retry_failed=False) -> str:
    """Unlabeled or stale chunks, minus those that already failed `max_tries` times."""
    todo = "(labeled_at IS NULL OR stale = 1)"
    return todo if retry_failed else f"({todo} AND COALESCE(tries, 0) < {int(max_tries)})"


def _gave_up_where(max_tries) -> str:
    return f"((labeled_at IS NULL OR stale = 1) AND COALESCE(tries, 0) >= {int(max_tries)})"


def _record_failure(con, h, err: str, max_tries: int, haiku: bool = False) -> bool:
    """Count one failed attempt; True if the chunk just reached `max_tries` (given up)."""
    con.execute("UPDATE units SET tries=COALESCE(tries,0)+1, err=?"
                + (", hfail=COALESCE(hfail,0)+1" if haiku else "") + " WHERE hash=?", (err, h))
    tries = con.execute("SELECT tries FROM units WHERE hash=?", (h,)).fetchone()[0]
    if tries >= max_tries:
        con.execute("UPDATE units SET err=? WHERE hash=?",
                    (f"{err} -- gave up after {tries} failed attempts "
                     f"(jevmem index --retry-failed tries again)", h))
        return True
    return False


def _mark_stale(con, relabel, relabel_topic) -> int:
    if relabel:
        return con.execute("UPDATE units SET stale=1, tries=0 WHERE labeled_at IS NOT NULL").rowcount
    if relabel_topic:
        return con.execute("UPDATE units SET stale=1, tries=0 WHERE top1=? OR top2=?",
                           (relabel_topic, relabel_topic)).rowcount
    return 0


def _dry_run(cfg, tax, db_path, cap, relabel, relabel_topic, retry_failed, max_tries, out, jev_ok,
             force_prune=False) -> dict:
    """Read-only: never writes the DB, never calls Jev."""
    files, problem = logs_problem(cfg)
    cur = collect_chunks(cfg, files)
    have, con, relpaths = {}, None, set()
    if db_path.exists():
        con = store.connect(db_path, readonly=True)
        try:
            for h, lab, tries, stale, t1, t2, hf, rel in con.execute(
                    "SELECT hash, labeled_at, tries, stale, top1, top2, hfail, relpath FROM units"):
                have[h] = (lab, tries or 0, stale or 0, t1, t2, hf or 0)
                relpaths.add(rel)
        except sqlite3.OperationalError:
            have = {}  # an empty file / older schema: nothing indexed yet
    if not problem and have and not force_prune:
        problem = vanished_problem(cfg, relpaths, files)
    held = bool(problem and have and not force_prune)
    new = [h for h in cur if h not in have]
    removed = [] if held else [h for h in have if h not in cur]
    todo = list(new)
    for h, (lab, tries, stale, t1, t2, _hf) in have.items():
        if h not in cur:
            continue
        under_cap = retry_failed or tries < max_tries
        if lab is None:
            if under_cap:
                todo.append(h)
        elif relabel or (relabel_topic and relabel_topic in (t1, t2)):
            todo.append(h)  # a real run marks these stale and resets their tries
        elif stale and under_cap:
            todo.append(h)
    todo.sort(key=lambda h: cur[h][1], reverse=True)
    this_run = todo[:cap]
    haiku_rows, jev_rows = _split_rows(
        cfg, [(h, cur[h][0], cur[h][2], have.get(h, (0,) * 6)[5]) for h in this_run], jev_ok)
    toks, method = per_chunk_tokens(cfg, tax, jev_rows, con)
    if con is not None:
        con.close()
    n_budget = _fit_budget(cfg, toks, _spent_today(cfg, db_path))
    batch = max(1, int(cfg["index"]["batch_size"]))
    res = {"dry_run": True, "labeler": cfg["index"].get("labeler", "haiku"),
           "chunks_total": len(cur), "new": len(new), "removed": len(removed),
           "to_label_total": len(todo), "to_label_this_run": len(haiku_rows) + n_budget,
           "haiku_chunks_this_run": len(haiku_rows),
           "est_haiku_calls_this_run": math.ceil(len(haiku_rows) / batch),
           "est_haiku_calls_all_pending": math.ceil(len(todo) / batch) if _use_haiku(cfg) else 0,
           "jev_chunks_this_run": n_budget,
           "est_jev_input_tokens_this_run": sum(toks[:n_budget]),
           "est_jev_cost_usd_this_run": round(_usd(cfg, sum(toks[:n_budget])), 4),
           "jev_estimate_method": method,
           "max_cost_usd_per_run": cfg["index"]["max_cost_usd_per_run"],
           "max_cost_usd_per_day": cfg["index"]["max_cost_usd_per_day"]}
    if not _use_haiku(cfg):
        all_rows = [(h, cur[h][0], cur[h][2]) for h in todo]
        res["est_jev_cost_usd_all_pending"] = round(_usd(cfg, sum(per_chunk_tokens(cfg, tax, all_rows)[0])), 4)
    else:
        res["note_haiku"] = ("Haiku calls go through claude -p with your own claude login; "
                             f"~{batch} chunks per call")
    if n_budget < len(jev_rows):
        res["note"] = "Jev labeling would be trimmed by the call budget limits (pass --yes to lift them)"
    if held:
        res["logs_held"] = (f"{problem}; a real run keeps all {len(have)} indexed chunks "
                            "(--force-prune deletes the chunks of files that are gone)")
    out(json.dumps(res, ensure_ascii=False, indent=2))
    return res


def _spent_today(cfg, db_path) -> float:
    if not Path(db_path).exists():
        return 0.0
    try:
        con = store.connect(db_path, readonly=True)
        v = store.get_meta(con, f"spent:{date.today().isoformat()}")
        con.close()
        return float(v or 0)
    except Exception:
        return 0.0


def _fit_budget(cfg, toks: list[int], spent_today: float) -> int:
    """How many of `toks` (in order) fit both the per-run and remaining per-day budget."""
    per_run = float(cfg["index"]["max_cost_usd_per_run"])
    per_day = float(cfg["index"]["max_cost_usd_per_day"]) - spent_today
    limit = min(per_run, per_day)
    acc, n = 0.0, 0
    for t in toks:
        c = _usd(cfg, t)
        if acc + c > limit:
            break
        acc += c
        n += 1
    return n


def _write_labels(con, h, r, labeler_name, in_tok=None):
    con.execute("""UPDATE units SET top1=?,top1_p=?,top2=?,top2_p=?,sub1=?,sub1_p=?,sub2=?,sub2_p=?,
                   in_tok=?,labeler=?,labeled_at=datetime('now','localtime'),err=NULL,stale=0,hfail=0
                   WHERE hash=?""", (*r, in_tok, labeler_name, h))


def _label_haiku(cfg, tax, con, rows, backend, out, max_tries) -> dict:
    """Batches of `batch_size`. An answer without a valid label for a chunk counts one
    failure for that chunk (toward both `haiku_max_failures` and `max_tries`). A call that
    fails or is skipped by the breaker (llm.LLMUnavailable) stops this run's Haiku labeling
    and counts nothing: those chunks simply stay pending."""
    batch = max(1, int(cfg["index"]["batch_size"]))
    split_re = cfg["index"].get("one_chunk_per_bullet_heading") or None
    ok = fail = calls = gave_up = 0
    paused = None
    for start in range(0, len(rows), batch):
        part = rows[start:start + batch]
        ids = [f"c{i + 1}" for i in range(len(part))]
        items = [(cid, labeler.clean_chunk(ch, split_re)) for cid, (_, _, ch) in zip(ids, part)]
        try:
            calls += 1
            text = llmmod.call(backend, labeler.build_prompt(tax, items))
        except llmmod.LLMUnavailable as e:
            calls -= 1 if e.skipped else 0
            paused = str(e)
            out(f"haiku labeling: {paused}; {len(rows) - start} chunk(s) stay pending "
                "(not counted as failed attempts)")
            break
        except Exception:
            text = None
        got = labeler.parse_labels(tax, text, ids) if text else {}
        for cid, (h, _, _) in zip(ids, part):
            if cid in got:
                _write_labels(con, h, got[cid], "haiku")
                ok += 1
            else:
                err = "haiku: no valid label" if text else "haiku: empty answer"
                gave_up += _record_failure(con, h, err, max_tries, haiku=True)
                fail += 1
        con.commit()
    return {"labeled": ok, "failed": fail, "calls": calls, "gave_up": gave_up, "paused": paused}


def _label_jev(cfg, tax, con, rows, client_factory, workers, max_tries) -> dict:
    """One Jev call per chunk. HTTP 402 (billing) and outages (timeout, connection or DNS
    error, HTTP 5xx/429: jev_client.JevUnavailable) stop this run's Jev labeling and count
    against no chunk; the chunks stay pending for the next run. Only an answer that came
    back unusable (non-JSON, an error object, another 4xx, missing fields) counts one try."""
    ok = fail = tok = gave_up = 0
    billing = False
    outage = None
    if not rows:
        return {"labeled": 0, "failed": 0, "in_tokens": 0, "billing": False, "gave_up": 0, "outage": None}
    client = client_factory()
    top, flat = taxmod.labeling_criteria(tax)
    qs = label_questions(cfg, top, flat)

    def label_one(rel, chunk):
        a, u = client.call(label_state(rel, chunk), qs, timeout=20)
        tp = a["top"]["probabilities"]
        t = sorted(tp, key=lambda k: -tp[k]) + [None]
        fp = a["flat"]["probabilities"]
        f = sorted(fp, key=lambda k: -fp[k]) + [None]
        return (t[0], tp[t[0]], t[1], tp.get(t[1]) if t[1] else None,
                f[0], fp[f[0]], f[1], fp.get(f[1]) if f[1] else None), u.get("input_tokens")

    with ThreadPoolExecutor(max(1, int(workers))) as ex:
        futs = {ex.submit(label_one, rel, ch): h for h, rel, ch in rows}
        for fu in as_completed(futs):
            h = futs[fu]
            if fu.cancelled():
                continue
            try:
                r, t = fu.result()
                _write_labels(con, h, r, "jev", t)
                ok += 1
                tok += t or 0
            except Exception as e:
                msg = str(e)[:200]
                if is_billing_error(e) or is_outage(e):
                    # Not the chunk's fault: don't burn a retry; stop this run.
                    tag = "billing: " if is_billing_error(e) else "outage: "
                    con.execute("UPDATE units SET err=? WHERE hash=?", (tag + msg, h))
                    fail += 1
                    if is_billing_error(e):
                        billing = True
                    else:
                        outage = outage or fsutil.safe_reason(e, 120)
                    for f2 in futs:
                        f2.cancel()
                    continue
                gave_up += _record_failure(con, h, msg, max_tries)
                fail += 1
    day_key = f"spent:{date.today().isoformat()}"
    store.set_meta(con, day_key, str(float(store.get_meta(con, day_key) or 0) + _usd(cfg, tok)))
    con.commit()
    return {"labeled": ok, "failed": fail, "in_tokens": tok, "billing": billing, "gave_up": gave_up,
            "outage": outage}


def _run_locked(cfg, tax, con, client_factory, cap, workers, relabel, relabel_topic, retry_failed,
                no_label, yes, max_tries, out, labeler_backend, jev_ok, force_prune=False) -> dict:
    t0 = time.time()
    sig = _signature(cfg)
    prev = store.get_meta(con, "sig")
    added = removed = 0
    held = None
    if prev != sig:
        files, problem = logs_problem(cfg)
        have = con.execute("SELECT count(*) FROM units").fetchone()[0]
        if not problem and have and not force_prune:
            problem = vanished_problem(cfg, (r[0] for r in con.execute("SELECT DISTINCT relpath FROM units")),
                                       files)
        if problem and have and not force_prune:
            # Never read "the folder is gone" (or "came back nearly empty") as "every log was
            # deleted": that would drop the labels and relabel everything once it is back.
            held = (f"{problem}; kept the {have} indexed chunks and their labels (nothing deleted). "
                    "Fix [logs] dir/glob or bring the files back; if they were removed on purpose, "
                    "`jevmem index --force-prune` deletes their chunks")
            out(f"index: {held}")
        else:
            added, removed = store.sync_units(con, collect_chunks(cfg, files))
            store.set_meta(con, "sig", sig)
            con.commit()
    marked = _mark_stale(con, relabel, relabel_topic)
    con.commit()
    total = con.execute("SELECT count(*) FROM units").fetchone()[0]
    where = _pending_where(max_tries, retry_failed)
    pending = con.execute(f"SELECT count(*) FROM units WHERE {where}").fetchone()[0]
    if prev == sig and not pending:
        out("no changes — nothing to do")
        return {"noop": True, "units": total}
    if no_label:
        res = {"units": total, "added": added, "removed": removed, "labeled": 0, "pending": pending}
        if held:
            res["logs_held"] = held
        out(json.dumps(res, ensure_ascii=False))
        return res

    rows = con.execute(f"SELECT hash, relpath, chunk, hfail FROM units WHERE {where} "
                       f"ORDER BY date DESC LIMIT ?", (cap,)).fetchall()
    haiku_rows, jev_rows = _split_rows(cfg, rows, jev_ok)
    trimmed = 0
    if jev_rows and not yes:
        toks, _ = per_chunk_tokens(cfg, tax, jev_rows, con)
        n = _fit_budget(cfg, toks, float(store.get_meta(con, f"spent:{date.today().isoformat()}") or 0))
        trimmed = len(jev_rows) - n
        jev_rows = jev_rows[:n]
        if trimmed:
            out(f"call budget limits: Jev-labeling {n} of {n + trimmed} chunks this run "
                f"(index.max_cost_usd_per_run / max_cost_usd_per_day; --yes lifts them)")

    hres = {"labeled": 0, "failed": 0, "calls": 0, "gave_up": 0, "paused": None}
    if haiku_rows:
        backend = labeler_backend or labeler.default_backend(cfg)
        hres = _label_haiku(cfg, tax, con, haiku_rows, backend, out, max_tries)
    jres = _label_jev(cfg, tax, con, jev_rows, client_factory, workers, max_tries)
    left = con.execute(f"SELECT count(*) FROM units WHERE {_pending_where(max_tries)}").fetchone()[0]
    if jres["billing"]:
        out("Jev answered HTTP 402 — Jev labeling stopped; the next run will continue")
    if jres.get("outage"):
        out(f"Jev unavailable ({jres['outage']}) — Jev labeling stopped for this run; the chunks stay "
            "pending (not counted as failed attempts) and the next run tries again")
    gave_up = hres["gave_up"] + jres["gave_up"]
    if gave_up:
        out(f"labeling: gave up on {gave_up} chunk(s) after {max_tries} failed attempts each; "
            f"they stay unlabeled until `jevmem index --retry-failed` (see `jevmem doctor`)")
    tok = jres["in_tokens"]
    res = {"units": total, "added": added, "removed": removed, "marked_stale": marked,
           "labeler": cfg["index"].get("labeler", "haiku"),
           "labeled": hres["labeled"] + jres["labeled"], "failed": hres["failed"] + jres["failed"],
           "haiku_labeled": hres["labeled"], "haiku_calls": hres["calls"],
           "jev_labeled": jres["labeled"], "trimmed_by_budget": trimmed,
           "gave_up": gave_up, "pending_next_run": left, "in_tokens": tok,
           "cost_usd": round(_usd(cfg, tok), 4), "sec": round(time.time() - t0, 1),
           "billing_stop": jres["billing"]}
    if hres.get("paused"):
        res["haiku_paused"] = hres["paused"]
    if jres.get("outage"):
        res["jev_outage"] = jres["outage"]
    if held:
        res["logs_held"] = held
    out(json.dumps(res, ensure_ascii=False))
    return res


def status_word(res: dict) -> str:
    """Result word for status.json / `jevmem doctor`."""
    if res.get("billing_stop"):
        return "billing-stop"
    if res.get("logs_held"):
        return "logs-missing"
    if res.get("jev_outage"):
        return "jev-outage"
    return "ok"


def status_detail(res: dict) -> str:
    if res.get("logs_held"):
        return res["logs_held"]
    return json.dumps({k: res.get(k) for k in ("labeled", "added", "gave_up", "pending_next_run", "noop")})


def stats(cfg) -> dict:
    db = Path(cfg["data"]["episodic_db"])
    if not db.exists():
        return {"units": 0, "labeled": 0, "pending": 0}
    con = store.connect(db, readonly=True)
    r = con.execute("SELECT count(*), sum(labeled_at IS NOT NULL), sum(labeled_at IS NULL), "
                    "sum(stale), sum(in_tok) FROM units").fetchone()
    by_top = dict(con.execute("SELECT top1, count(*) FROM units WHERE top1 IS NOT NULL "
                              "GROUP BY top1 ORDER BY 2 DESC").fetchall())
    by_labeler = dict(con.execute("SELECT COALESCE(labeler, 'jev'), count(*) FROM units "
                                  "WHERE labeled_at IS NOT NULL GROUP BY 1").fetchall())
    gave_up = con.execute("SELECT count(*) FROM units WHERE "
                          + _gave_up_where(cfg["index"]["max_tries"])).fetchone()[0]
    spent = store.get_meta(con, f"spent:{date.today().isoformat()}")
    con.close()
    return {"units": r[0], "labeled": r[1] or 0, "unlabeled": r[2] or 0, "stale": r[3] or 0,
            "gave_up": gave_up, "label_tokens_total": r[4] or 0,
            "spent_usd_today": round(float(spent or 0), 4),
            "by_labeler": by_labeler, "by_topic": by_top}

"""Stage 2 — Digest: `jevmem digest`, every 30 minutes (:04 and :34 by default).

One run does both halves of turning conversation into searchable memory:
  1. read only what was appended to the conversation source since the last run
     (per-file byte offsets in <data>/digest_state.json; with the claude_transcript
     source the very first run only records a baseline so old sessions are not dumped)
       source = capture (default): the Stage 1 chat log (chat-app messages only)
       source = jsonl: any chat JSONL ([digest] jsonl_path)
       source = claude_transcript: Claude Code session files (fallback, off by default;
                user + assistant text only, tool calls/results, thinking and subagents dropped)
     A state file that is missing (while the daily logs already have summaries), empty or
     unreadable never means "start over": it is moved aside and the run continues from the
     copy written next to it after every run (digest_state.backup.json); without a usable
     copy, from the records after the newest summary heading in the daily logs (records
     without a readable time can't be placed that way: they are skipped and counted).
  2. no new USER message -> no summary, no LLM call
  3. otherwise summarize with the text LLM into ONE BULLET PER TOPIC (max 5) and append
     "## HH:MM summary" to <logs>/YYYY-MM-DD.md. A window longer than `max_chars` is
     summarized in several parts. No usable summary (LLM unavailable) -> every record as a
     raw line under "(raw)", at most `raw_fallback_lines` per run; the offsets advance only
     past the records written, so the rest is handled by the next run, never skipped. One
     exception: assistant/script lines after the last user message beyond that limit are
     counted in one line instead (the next run would drop a window without a user message;
     the chat log keeps them for `jevmem raw`).
     The logs folder is never re-created once it existed (or the index holds chunks from it):
     a folder that is briefly gone holds the run instead, so indexing can't mistake a fresh
     folder with one new file for "every other log was deleted".
     One topic per bullet matters: the chunker keeps each bullet of these sections as its
     own chunk, so every chunk gets a clean single-topic label.
     Midnight: records are grouped by their local date. A group from an earlier day (the
     00:04 run summarizing 23:40 chat, or a catch-up after the machine was off) goes to
     THAT day's file, headed with the time of its last record ("## 23:52 summary"), so
     `jevmem raw`'s windows still line up.
  4. then, in the same run, chunk + label new/changed chunks with the configured labeler
     (Claude Haiku by default, or Jev; the same work as `jevmem index`). If labeling fails,
     the summary is kept and labeling retries next run, up to `index.max_tries` attempts
     per chunk.

Runs are serialized by <data>/digest.lock: a second digest that starts while one is running
exits at once (the running one consumes the same new lines). `--dry-run` never calls an LLM
and writes nothing: it reports what would be summarized.
"""
from __future__ import annotations

import errno
import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import fsutil, status
from . import llm as llmmod
from .text import message_text

STATE_NAME = "digest_state.json"
# A second copy of the state, written right after it: a damaged state file resumes exactly
# from here (byte offsets, so it works for records without a time as well).
BACKUP_NAME = "digest_state.backup.json"
LOCK_NAME = "digest.lock"


def project_slug(path) -> str:
    """Claude Code stores sessions under ~/.claude/projects/<slug>/, where the slug is the
    absolute project path with every non-alphanumeric character replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).expanduser().resolve()))


def transcript_dir(cfg) -> Path:
    dc = cfg["digest"]
    proj = dc.get("project_dir") or str(cfg.root)
    return Path(dc["claude_projects_dir"]).expanduser() / project_slug(proj)


def source_files(cfg) -> list[Path]:
    dc = cfg["digest"]
    if dc["source"] in ("capture", "jsonl"):
        path = cfg["capture"]["path"] if dc["source"] == "capture" else dc.get("jsonl_path")
        p = Path(path) if path else None
        return [p] if p and p.exists() else []
    d = transcript_dir(cfg)
    return sorted(d.glob("*.jsonl")) if d.exists() else []


def source_problem(cfg) -> str | None:
    """Why the configured digest source can't be read at all, or None. (A capture log that
    doesn't exist yet is not a problem: nothing was captured so far.)"""
    dc = cfg["digest"]
    if dc["source"] == "jsonl":
        path = dc.get("jsonl_path")
        if not path:
            return '[digest] source = "jsonl" but [digest] jsonl_path is not set'
        if not Path(path).is_file():
            return f"[digest] jsonl_path {path} does not exist"
    return None


def _state_path(cfg) -> Path:
    return Path(cfg["data"]["dir"]) / STATE_NAME


def _backup_path(cfg) -> Path:
    return Path(cfg["data"]["dir"]) / BACKUP_NAME


def _read_state_file(p: Path) -> tuple[dict | None, str | None]:
    if not p.exists():
        return None, None
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, fsutil.safe_reason(e, 80) if isinstance(e, OSError) else "not valid JSON (empty or cut short)"
    offs = st.get("offsets") if isinstance(st, dict) else None
    if not isinstance(offs, dict) or not all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in offs.values()):
        return None, "not a digest state (no valid 'offsets' object)"
    skip = st.get("skip", {})
    if not isinstance(skip, dict) or not all(
            isinstance(v, list) and all(isinstance(x, int) for x in v) for v in skip.values()):
        st["skip"] = {}
    return st, None


def read_state(cfg) -> tuple[dict | None, str | None]:
    """(state, problem). (None, None): no state file yet. (None, reason): the file exists but
    can't be used (empty, cut short by a crash, not JSON, wrong shape)."""
    return _read_state_file(_state_path(cfg))


def read_backup(cfg) -> dict | None:
    """The backup copy of the state, or None if it is missing or unusable."""
    return _read_state_file(_backup_path(cfg))[0]


def load_state(cfg) -> dict | None:
    return read_state(cfg)[0]


def save_state(cfg, state: dict) -> None:
    text = json.dumps(state, indent=2)
    fsutil.write_text_atomic(_state_path(cfg), text)
    try:
        fsutil.write_text_atomic(_backup_path(cfg), text)
    except OSError:
        pass  # the state itself was written


def read_new_spans(path: Path, offset: int) -> tuple[list[tuple[str, int]], int, int]:
    """Complete lines appended after `offset`: ([(line, start byte)], first byte read, end).
    A partially written last line is left for the next run. A shrunken file restarts from 0
    (then `first byte read` is 0, not `offset`). Lines are split on b"\\n" only."""
    size = path.stat().st_size
    if offset > size:
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end == -1:
        return [], offset, offset
    out, pos = [], offset
    for raw in data[: end + 1].split(b"\n")[:-1]:
        out.append((raw.decode("utf-8", errors="replace"), pos))
        pos += len(raw) + 1
    return out, offset, pos


def read_new(path: Path, offset: int) -> tuple[list[str], int]:
    """Complete lines appended after `offset`; returns (lines, new_offset)."""
    spans, _, end = read_new_spans(path, offset)
    return [line for line, _ in spans], end


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_tool_result(content) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


NOISE_PREFIXES = ("<command-", "<local-command", "<system-reminder", "Caveat:", "[Request interrupted")


def parse_records(lines: list[str], source: str, skip_prefixes=()) -> list[dict]:
    """-> [{"ts", "user": bool, "text"}] with tool noise removed."""
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        if source == "claude_transcript":
            typ = r.get("type")
            if typ not in ("user", "assistant") or r.get("isSidechain") or r.get("isMeta"):
                continue
            content = (r.get("message") or {}).get("content")
            if typ == "user" and _is_tool_result(content):
                continue
            text = _text(content)
            ts = r.get("timestamp", "")
            is_user = typ == "user"
        else:  # capture / jsonl
            role = r.get("role", r.get("dir"))
            if role is None:
                continue
            text = _text(r.get("text", r.get("content")))
            ts = r.get("ts", r.get("timestamp", ""))
            is_user = role in ("user", "in", "human")
            script = not is_user and (r.get("via") == "script" or role == "script")
            name = str(r.get("user") or r.get("name") or "") if is_user else ""
        text = message_text(text).strip()
        if not text or text.startswith(NOISE_PREFIXES):
            continue
        if is_user and any(text.startswith(p) for p in skip_prefixes):
            continue
        rec = {"ts": ts, "user": is_user, "text": text}
        if source != "claude_transcript":
            if script:
                rec["script"] = True
            if name:
                rec["name"] = name
        out.append(rec)
    return out


def _local(ts) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt


def _hhmm(ts: str) -> str:
    dt = _local(ts)
    return dt.strftime("%H:%M") if dt else "--:--"


def split_by_day(recs: list[dict], today: str) -> list[tuple[str, list[dict]]]:
    """[(YYYY-MM-DD, records)], oldest first: one group per local date that has a user
    message. Records of a date without one join the previous group (or the next, if none
    came before). Unparseable or future timestamps count as `today`."""
    groups: list[tuple[str, list[dict]]] = []
    for r in recs:
        dt = _local(r["ts"])
        d = dt.date().isoformat() if dt else today
        d = min(d, today)
        if groups and groups[-1][0] == d:
            groups[-1][1].append(r)
        else:
            groups.append((d, [r]))
    out: list[tuple[str, list[dict]]] = []
    carry: list[dict] = []
    for d, rs in groups:
        if any(r["user"] for r in rs):
            out.append((d, carry + rs))
            carry = []
        elif out:
            out[-1][1].extend(rs)
        else:
            carry.extend(rs)
    if carry and out:
        out[0] = (out[0][0], carry + out[0][1])
    return out


def section_time(day: str, recs: list[dict], now: datetime) -> str:
    """HH:MM for a section's heading: now for today's group, else the last record's time."""
    if day == f"{now:%Y-%m-%d}":
        return f"{now:%H:%M}"
    times = [dt for dt in (_local(r["ts"]) for r in recs) if dt and dt.date().isoformat() == day]
    return f"{max(times):%H:%M}" if times else "23:59"


def speaker(cfg, rec: dict, name_users: bool = False) -> str:
    """Line label: user (optionally with the sender's name), assistant, or script."""
    ctx = cfg["context"]
    if rec.get("user"):
        name = rec.get("name")
        return f"{ctx['user_label']} ({name})" if name_users and name else ctx["user_label"]
    if rec.get("script"):
        return ctx.get("script_label") or ctx["assistant_label"]
    return ctx["assistant_label"]


def several_senders(cfg, recs: list[dict]) -> bool:
    if not cfg["digest"].get("name_speakers", True):
        return False
    return len({r.get("name") for r in recs if r.get("user") and r.get("name")}) > 1


def cut_line(text: str, limit: int, marker: str) -> str:
    text = " ".join(str(text).split())
    if limit and len(text) > limit:
        return text[:limit].rstrip() + marker
    return text


def render_lines(cfg, recs: list[dict], named: bool | None = None) -> list[str]:
    """One `[HH:MM] Speaker: text` line per record; long messages end in the truncation
    marker (`max_line_chars`)."""
    dc = cfg["digest"]
    named = several_senders(cfg, recs) if named is None else named
    marker = dc.get("truncation_marker", " ...")
    return [f"[{_hhmm(r['ts'])}] {speaker(cfg, r, named)}: "
            + cut_line(r["text"], int(dc["max_line_chars"]), marker) for r in recs]


def conversation_text(cfg, recs: list[dict], named: bool | None = None) -> str:
    """`[HH:MM] Speaker: text` lines. Long messages end in the truncation marker; over
    `max_chars` the oldest whole lines are dropped (the most recent part is kept). A digest
    run never hands this more than `max_chars` at once: `parts` splits a longer window."""
    dc = cfg["digest"]
    lines = render_lines(cfg, recs, named)
    limit = int(dc["max_chars"])
    kept, size = [], 0
    for line in reversed(lines):
        if kept and size + len(line) + 1 > limit:
            break
        kept.append(line[-limit:] if not kept else line)
        size += len(line) + 1
    return "\n".join(reversed(kept))


def summarize(cfg, backend, convo: str) -> list[str] | None:
    dc = cfg["digest"]
    ctx = cfg["context"]
    prompt = dc["prompt"].format(max_topics=dc["max_topics"], language=dc["language"],
                                 conversation=convo, user_label=ctx["user_label"],
                                 assistant_label=ctx["assistant_label"],
                                 script_label=ctx.get("script_label") or ctx["assistant_label"],
                                 truncation_marker=str(dc.get("truncation_marker", "")).strip())
    data = llmmod.parse_json(backend.complete(prompt))
    if isinstance(data, dict):
        # {"topics": [...]} (current); {"bullets": [...]}; legacy {"summary": "..."} = one bullet
        data = next((data[k] for k in ("topics", "bullets", "summary") if data.get(k)), None)
    if isinstance(data, str):
        data = [data]
    if not isinstance(data, list):
        return None
    items = []
    for b in data:
        if isinstance(b, dict):  # tolerate {"topic": "...", "summary": "..."} items
            b = b.get("summary") or b.get("text") or b.get("topic") or ""
        b = " ".join(str(b).split()).lstrip("-•* ").strip()
        if b:
            items.append(b)
    return items[: int(dc["max_topics"])] or None


def omitted_line(cfg, recs: list[dict]) -> str:
    """One line standing for assistant/script messages that were not written out as raw lines."""
    who = ", ".join(dict.fromkeys(speaker(cfg, r) for r in recs))
    return (f"[{_hhmm(recs[0]['ts'])}-{_hhmm(recs[-1]['ts'])}] ({len(recs)} more message(s) from {who}, none "
            f"from {cfg['context']['user_label']}: not written out here, the chat log keeps them)")


def render_section(title: str, bullets: list[str]) -> str:
    return f"\n## {title}\n" + "\n".join(f"- {b}" for b in bullets) + "\n"


def daily_path(cfg, day: str) -> Path:
    return Path(cfg["logs"]["dir"]) / f"{day}.md"


def append_daily(cfg, section: str, day, create_dir: bool = True) -> Path:
    """Append to <logs>/YYYY-MM-DD.md (`day`: a date string or a datetime). A new log file
    is created 0600 (a new logs dir 0700) with the header and the section in one append-mode
    write; existing files keep their permissions and are only appended to. With
    `create_dir=False` a missing logs folder raises FileNotFoundError instead of being made."""
    day = day if isinstance(day, str) else f"{day:%Y-%m-%d}"
    path = daily_path(cfg, day)
    if not create_dir and not path.parent.is_dir():
        raise FileNotFoundError(errno.ENOENT, "the logs folder disappeared during the run", str(path.parent))
    if not fsutil.create_new(path, f"# {day} daily log\n" + section):
        with fsutil.open_append(path) as f:
            f.write(section)
    return path


def run(cfg, backend=None, dry_run: bool = False, now: datetime | None = None, out=print,
        client_factory=None) -> dict:
    """One digest run. Real runs hold <data>/digest.lock; if another digest holds it, this
    one exits without reading or writing anything."""
    if dry_run:
        return _run(cfg, backend, True, now, out, client_factory)
    fd = fsutil.try_lock(Path(cfg["data"]["dir"]) / LOCK_NAME)
    if fd is None:
        out("another digest is running — exiting")
        return {"result": "locked"}
    try:
        return _run(cfg, backend, False, now, out, client_factory)
    finally:
        os.close(fd)


DAY_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


def last_summary_time(cfg) -> datetime | None:
    """Local time of the newest summary heading ("## HH:MM summary", [raw].heading_re) in
    the daily logs: when the digest last wrote something."""
    logs = Path(cfg["logs"]["dir"])
    try:
        days = sorted((p for p in logs.iterdir() if DAY_FILE_RE.match(p.name)), reverse=True)
    except OSError:
        return None
    rx = re.compile(cfg["raw"]["heading_re"], re.M)
    for p in days:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        times = []
        for m in rx.finditer(text):
            t = (m.group(2) if m.lastindex and m.lastindex >= 2 and m.group(2) else m.group(1)).zfill(5)
            times.append(t)
        for t in sorted(times, reverse=True):
            try:
                return datetime.strptime(f"{p.name[:10]} {t}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue
    return None


def resume_offsets(cfg, files: list[Path], since: datetime) -> tuple[dict, dict]:
    """Where to continue after the digest state was lost: ({file: byte offset}, {file: n}).
    The offset is that of the first record whose time is at or after `since`, else the end of
    the file's complete lines. A record without a readable time can't be placed against
    `since`: such records before that offset are skipped (never summarized a second time)
    and counted in the second dict; those after it are read as usual."""
    offsets, untimed = {}, {}
    for p in files:
        spans, _, end = read_new_spans(p, 0)
        off, n_untimed = end, 0
        for line, start in spans:
            recs = parse_records([line], cfg["digest"]["source"])
            times = [_local(r["ts"]) for r in recs]
            if any(t is not None and t >= since for t in times):
                off = start
                break
            n_untimed += sum(1 for t in times if t is None)
        offsets[str(p)] = off
        if n_untimed:
            untimed[str(p)] = n_untimed
    return offsets, untimed


def _recover_state(cfg, files, problem: str | None, dry_run: bool, now: datetime, out) -> dict | None:
    """No usable state but a history exists. First choice: the backup copy written after
    every run (the exact byte offsets where the last completed run stopped). Without one: resume
    after the last summary heading instead of re-summarizing the whole chat log (which would
    append a duplicate section to every past daily log and call the LLM once per day). An
    unreadable state file is moved aside to <name>.bak-YYYYmmdd-HHMMSS first. None: nothing
    to recover from (a genuine first run)."""
    backup = read_backup(cfg)
    if backup is None and problem is None and cfg["digest"]["source"] == "claude_transcript":
        return None  # first run on session files: baseline only (never dump old sessions)
    since = why = None
    if backup is None:
        since = last_summary_time(cfg)
        if problem is None and since is None:
            return None
        if since is None:  # the state existed but no summary heading was ever written
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
            why = "start of today: no summary heading found in the daily logs"
        else:
            why = "the newest summary heading in the daily logs"
    p = _state_path(cfg)
    if problem is not None:
        bak = p.with_name(f"{p.name}.bak-{now:%Y%m%d-%H%M%S}")
        if dry_run:
            out(f"[dry-run] {p} is unreadable ({problem}); a real run moves it to {bak.name}")
        else:
            try:
                os.replace(p, bak)
            except OSError:
                pass
            out(f"warning: {p} was unreadable ({problem}); moved to {bak.name}")
            status.record(cfg, "digest", "state-reset", f"unreadable state moved to {bak.name}")
    else:
        out(f"note: no {p.name}, but " + ("its backup copy exists" if backup is not None
                                            else "the daily logs already have summaries"))
    if backup is not None:
        out(f"continuing from the read position saved in {BACKUP_NAME} (a copy written with every run): "
            "exactly where the last completed run stopped")
        return backup
    offsets, untimed = resume_offsets(cfg, files, since)
    out(f"resuming with chat records from {since:%Y-%m-%d %H:%M} ({why}); "
        "older records are not summarized again")
    for path, n in untimed.items():
        out(f"warning: {n} record(s) in {path} before that point have no readable time, so it can't be "
            "told whether they were summarized already: they are skipped (not summarized)")
    return {"offsets": offsets}


def parts(cfg, recs: list[dict], named: bool) -> list[list[dict]]:
    """Split a day's records into consecutive parts whose lines fit `max_chars` together, so
    a long window is summarized in several calls instead of losing its oldest lines."""
    limit = int(cfg["digest"]["max_chars"])
    out, cur, size = [], [], 0
    for r, line in zip(recs, render_lines(cfg, recs, named)):
        if cur and size + len(line) + 1 > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += len(line) + 1
    if cur:
        out.append(cur)
    return out


def _time_of(recs: list[dict], day: str, fallback: str) -> str:
    times = [dt for dt in (_local(r["ts"]) for r in recs) if dt and dt.date().isoformat() == day]
    return f"{max(times):%H:%M}" if times else fallback


def _new_state(old: dict, recs: list[dict], ends: dict, skipped: dict, consumed: set,
               logs_seen: bool = False) -> dict:
    """Offsets after this run: each file advances to the first record NOT consumed (or to its
    end). Records consumed beyond that point (rare: a file's records out of time order) are
    listed under "skip" so the next run doesn't summarize them twice."""
    offsets = dict(old.get("offsets", {}))
    skip = {}
    by_file: dict = {}
    for r in recs:
        by_file.setdefault(r["_src"][0], []).append(r)
    for key, end in ends.items():
        rs = by_file.get(key, [])
        pending = [r["_src"][1] for r in rs if id(r) not in consumed]
        if not pending:
            offsets[key] = end
            continue
        off = min(pending)
        offsets[key] = off
        done = {r["_src"][1] for r in rs if id(r) in consumed and r["_src"][1] >= off}
        done |= {s for s in skipped.get(key, []) if s >= off}
        if done:
            skip[key] = sorted(done)
    out = {"offsets": offsets, "skip": skip}
    if logs_seen or old.get("logs_seen"):
        out["logs_seen"] = True  # the logs folder existed: never re-create it (see logs_dir_problem)
    return out


def _index_has_chunks(cfg) -> int | None:
    """Chunks in the episodic index (0 if there is none), None if the file can't be read."""
    db = Path(cfg["data"]["episodic_db"])
    if not db.exists():
        return 0
    from . import store
    try:
        con = store.connect_ro(db, timeout=1.0)
        try:
            return con.execute("SELECT count(*) FROM units").fetchone()[0]
        finally:
            con.close()
    except Exception:
        return None


def logs_dir_problem(cfg, state: dict | None) -> str | None:
    """Why this run must not write into the logs folder, or None. A missing folder is created
    only on a genuine first run. Once it existed (`logs_seen` in the state) or the index holds
    chunks from it, a missing folder means a sync folder that is briefly gone, an unmounted
    disk or a typo: re-creating it with one new file would make the next index run read
    "every other log was deleted"."""
    logs = Path(cfg["logs"]["dir"])
    if logs.is_dir():
        return None
    if logs.exists():
        return f"logs dir {logs} is not a directory"
    n = _index_has_chunks(cfg)
    if state and state.get("logs_seen"):
        why = "it existed before"
    elif n:
        why = f"the index holds {n} chunks from it"
    elif n is None:
        why = "the episodic index exists but can't be read"
    else:
        return None  # first run: the digest creates the folder
    return (f"logs dir {logs} does not exist, but {why} (a sync folder that is briefly gone, an unmounted "
            "disk, a typo in [logs] dir?); bring it back, or create it (mkdir) if you removed it on purpose")


def _run(cfg, backend, dry_run: bool, now: datetime | None, out, client_factory) -> dict:
    now = now or datetime.now()
    dc = cfg["digest"]
    files = source_files(cfg)
    note = source_problem(cfg)
    if note:
        out(f"summary: skipped, {note}")
        if not dry_run:
            status.record(cfg, "digest", "no-source", note)
        return {"result": "no-source", "note": note}
    state, problem = read_state(cfg)
    if state is None:
        state = _recover_state(cfg, files, problem, dry_run, now, out)
    if state is None and dc["source"] != "claude_transcript":
        state = {"offsets": {}}  # first run: the chat log only holds messages captured since install
    if state is None:
        # First run on Claude Code transcripts: baseline only (don't dump old sessions).
        state = {"offsets": {str(p): p.stat().st_size for p in files}}
        if not dry_run:
            save_state(cfg, state)
            status.record(cfg, "digest", "baseline")
        out(("[dry-run] baseline: would record " if dry_run else "baseline: recorded ")
            + f"{len(files)} source file(s); new conversation will be captured from now on")
        return {"result": "baseline", "files": len(files)}

    old_offsets = state.get("offsets", {})
    old_skip = {k: set(v) for k, v in (state.get("skip") or {}).items()}
    logs_seen = Path(cfg["logs"]["dir"]).is_dir()
    hook_skip = cfg["hook"].get("skip_prefixes", [])
    recs, ends, skipped = [], {}, {}
    for p in files:
        key = str(p)
        want = int(old_offsets.get(key, 0))
        spans, first, ends[key] = read_new_spans(p, want)
        sk = old_skip.get(key, set()) if first == want else set()
        for line, start in spans:
            if start in sk:
                skipped.setdefault(key, []).append(start)
                continue
            for r in parse_records([line], dc["source"], hook_skip):
                r["_src"] = (key, start)
                recs.append(r)
    recs.sort(key=lambda r: str(r["ts"]))
    n_user = sum(1 for r in recs if r["user"])
    if not n_user:
        if not dry_run:
            save_state(cfg, _new_state(state, recs, ends, skipped, {id(r) for r in recs}, logs_seen))
            status.record(cfg, "digest", "skip", f"{len(recs)} new non-user records")
        out(f"summary: skipped, no new user messages ({len(recs)} other records)")
        res = {"result": "skip", "records": len(recs)}
        if not dry_run:
            res["index"] = index_step(cfg, client_factory, out)
        return res

    groups = split_by_day(recs, f"{now:%Y-%m-%d}")
    held = logs_dir_problem(cfg, state)
    if held:
        if dry_run:
            out(f"[dry-run] a real run would write nothing now: {held}")
            return {"result": "logs-missing", "dry_run": True, "note": held, "user_messages": n_user,
                    "records": len(recs), "llm_called": False}
        out(f"summary: held, {held}. Nothing written and no LLM call: the {n_user} new user message(s) stay "
            "in the chat log for the next run")
        status.record(cfg, "digest", "logs-missing", held)
        return {"result": "logs-missing", "note": held, "user_messages": n_user, "records": len(recs)}
    create_logs_dir = not Path(cfg["logs"]["dir"]).exists()  # a first run (checked just above)
    if dry_run:
        plan = []
        for day, grecs in groups:
            title = dc["title"].format(time=section_time(day, grecs, now), date=day)
            u = sum(1 for r in grecs if r["user"])
            plan.append({"date": day, "title": title, "path": str(daily_path(cfg, day)),
                         "records": len(grecs), "user_messages": u})
            out(f"[dry-run] {len(grecs)} new record(s) ({u} user message(s)) from {day} would be "
                f"summarized and appended to {daily_path(cfg, day)} as '## {title}'")
        out("[dry-run] LLM not called; nothing written, offsets unchanged, no labeling")
        return {"result": "dry-run", "dry_run": True, "user_messages": n_user, "records": len(recs),
                "sections": plan, "llm_called": False}

    backend = backend or llmmod.get_backend(cfg)
    # Summarize first, then write: an exception half-way leaves no section behind whose
    # lines the next run (offsets unchanged) would summarize again.
    # Nothing is dropped: a window longer than max_chars is summarized in several parts, and
    # when no summary can be written (LLM unavailable, unusable answer, backend "none") every
    # record becomes a raw line, at most raw_fallback_lines per run. Records beyond that stay
    # in the source and the next run continues from the first one not written.
    raw_left = max(1, int(dc["raw_fallback_lines"]))
    sections, any_raw, texts, consumed = [], False, [], set()
    stop = False
    for gi, (day, grecs) in enumerate(groups):
        named = several_senders(cfg, grecs)
        gparts = parts(cfg, grecs, named)
        for pi, part in enumerate(gparts):
            bullets = summarize(cfg, backend, conversation_text(cfg, part, named))
            take, last = part, pi == len(gparts) - 1
            raw = not bullets
            if raw:
                if raw_left <= 0:
                    stop = True
                    break
                rest_of_day = part + [r for q in gparts[pi + 1:] for r in q]
                take = part[:raw_left]
                omitted = []
                if not any(r["user"] for r in rest_of_day[len(take):]):
                    # A tail without a user message would be dropped as "no new user
                    # messages" by the next run: consume it now. Its lines count against the
                    # same limit; the rest of it becomes one counting line (the chat log keeps
                    # those messages for `jevmem raw`).
                    written, omitted = rest_of_day[:raw_left], rest_of_day[raw_left:]
                    take, last, stop_after = rest_of_day, True, False
                else:
                    written = take
                    stop_after = len(take) < len(part)
                raw_left -= len(written)
                any_raw = True
                bullets = render_lines(cfg, written, named)
                if omitted:
                    bullets.append(omitted_line(cfg, omitted))
                note = llmmod.unavailable_note(backend)
                if note:
                    out(f"summary: {note}")
            else:
                stop_after = False
            consumed |= {id(r) for r in take}
            hhmm = (section_time(day, grecs, now) if last and not stop_after
                    else _time_of(take, day, section_time(day, take, now)))
            title = dc["title"].format(time=hhmm, date=day) + (" (raw)" if raw else "")
            texts.append(render_section(title, bullets))
            sections.append({"date": day, "title": title, "bullets": len(bullets), "raw": raw,
                             "records": len(take)})
            if stop_after:
                stop = True
            if stop or (raw and last):
                break
        if stop:
            break
    deferred = sum(1 for r in recs if id(r) not in consumed)
    paths = []
    for s, section in zip(sections, texts):
        paths.append(append_daily(cfg, section, s["date"], create_dir=create_logs_dir))
        s["path"] = str(paths[-1])
    res = {"result": "raw" if any_raw else "ok", "user_messages": n_user, "records": len(recs),
           "bullets": sum(s["bullets"] for s in sections), "backend": getattr(backend, "name", "?"),
           "sections": sections, "path": str(paths[-1]), "deferred": deferred}
    save_state(cfg, _new_state(state, recs, ends, skipped, consumed, logs_seen=True))
    status.record(cfg, "digest", res["result"],
                  f"{n_user} user msgs -> " + ", ".join(dict.fromkeys(p.name for p in paths))
                  + (f"; {deferred} records left for the next run" if deferred else ""))
    for s in sections:
        out(f"summary: {'raw' if s['raw'] else 'ok'}, appended {s['bullets']} "
            f"{'raw line' if s['raw'] else 'bullet'}(s) to {s['path']}")
    if deferred:
        out(f"summary: {deferred} record(s) left in the chat log for the next run "
            f"(at most {int(dc['raw_fallback_lines'])} raw lines per run while no summary can be "
            "written; nothing is skipped)")
    return {**res, "index": index_step(cfg, client_factory, out)}


def index_step(cfg, client_factory=None, out=print) -> dict:
    """Chunk + label right after summarizing. Failures never undo the summary; unlabeled
    chunks stay pending and are retried on the next run."""
    if not cfg["digest"].get("index_after", True):
        return {"skipped": "index_after = false"}
    from . import indexer
    from .jev_client import JevClient
    factory = client_factory or (lambda: JevClient.from_config(cfg))
    try:
        res = indexer.run(cfg, client_factory=factory, out=lambda *_: None)
    except Exception as e:
        msg = fsutil.safe_reason(e, 150)
        out(f"index: deferred ({msg}); will retry next run")
        status.record(cfg, "index", "error", msg)
        return {"error": msg}
    keys = ("labeled", "added", "failed", "gave_up", "pending_next_run", "noop")
    status.record(cfg, "index", indexer.status_word(res), indexer.status_detail(res))
    out("index: " + json.dumps({k: res[k] for k in keys if k in res}))
    if res.get("logs_held"):
        out(f"index: {res['logs_held']}")
    if res.get("gave_up"):
        out(f"index: gave up on {res['gave_up']} chunk(s) after index.max_tries failed attempts; "
            "`jevmem index --retry-failed` tries them again")
    return res

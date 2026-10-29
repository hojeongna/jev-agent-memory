"""Stage 1 — Capture: save ONLY the chat-channel conversation as JSONL.

What is saved: what the user typed in the chat app (Telegram / Discord via the Claude Code
channel plugins) and what the agent actually sent back to that chat. What is NOT saved:
Claude Code's internal thinking, tool calls, or replies that never reached the chat.

Hooks (installed by `jevmem install-hook`):
  UserPromptSubmit                     -> jevmem capture inbound
      parses <channel source="plugin:telegram:telegram" chat_id=".." message_id=".."
      user=".." ts="..">text</channel> blocks in the prompt (dedupe by message_id)
  PostToolUse (matcher: reply tools)   -> jevmem capture outbound
      logs tool_input.text of mcp__plugin_telegram_telegram__reply /
      mcp__plugin_discord_discord__reply
Scripts that message the user outside the plugin (cron alerts, notifications):
  jevmem capture log-out "text" [--channel telegram] [--chat-id 123]

Record: {"ts", "dir": "in"|"out", "channel", "chat_id", "message_id", "user", "text"}
        (+ "via": "script" for log-out records, so summaries can tell alerts a script sent
        apart from the assistant's own replies)

Several <channel> blocks in one prompt (messages that arrived while the agent was busy)
become one record each (only the first 262,144 characters of a prompt are searched, in
linear time).
New files are created 0600 (see fsutil). A log whose last line was cut short (a crash, a
full disk) gets a newline before the next record, so that record is not merged into it.

Duplicates: an inbound message id is recorded in capture_seen.txt only AFTER its record was
appended, under one lock, so a message whose write failed (disk error) is saved when the
same prompt comes again. The lock wait is capped (SEEN_LOCK_TIMEOUT, one budget for all
blocks of a prompt): if another hook process holds it too long, the messages are appended
without the duplicate check (a rare duplicate line beats a lost message) and the reason goes
to the hook error log. A lone surrogate in a message (a broken emoji) is saved as "?".
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from . import fsutil
from .text import channel_blocks

ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
SEEN_LOCK_TIMEOUT = 2.0  # seconds; the capture hooks run with a 5 s timeout
# The sent message's id in a reply tool's response. An explicit message id wins
# ({"message_id": 853}, {"messageId": "853"}); otherwise a bare id ("sent (id: 853)",
# "ids: 853, 854", {"id": 5}). Never chat_id / user_id / thread_id.
REPLY_MSG_ID_RE = re.compile(r"(?<![A-Za-z0-9_])message_?ids?[\"'\s:=]+(\d+)", re.IGNORECASE)
REPLY_ID_RE = re.compile(r"(?<![A-Za-z0-9_])ids?[\"'\s:=]+(\d+)", re.IGNORECASE)
SEEN_NAME = "capture_seen.txt"
SEEN_KEEP = 1000


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def to_local(ts: str) -> str:
    if not ts:
        return now_iso()
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return now_iso()


def log_path(cfg) -> Path:
    return Path(cfg["capture"]["path"])


def _ends_mid_line(path: Path) -> bool:
    """True if the file's last byte is not a newline (a write cut short by a crash or a full
    disk): the next record must then start on a new line, or it would merge into that one."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return False
            f.seek(-1, os.SEEK_END)
            return f.read(1) != b"\n"
    except OSError:
        return False


def append(cfg, rec: dict) -> None:
    # A lone surrogate (broken emoji from some clients) can't be written as UTF-8: it is
    # replaced instead of losing the whole message.
    line = json.dumps(rec, ensure_ascii=False).encode("utf-8", "replace").decode("utf-8")
    path = log_path(cfg)
    lead = "\n" if _ends_mid_line(path) else ""
    with fsutil.open_append(path) as f:
        f.write(lead + line + "\n")  # one write: the cut-off line stays a line of its own


def label(cfg, rec: dict) -> str:
    """Speaker label for display, the same as `jevmem raw` and the digest use:
    "User (name)", "Assistant", or "Assistant (script)" for `log-out` records."""
    ctx = cfg["context"]
    if rec.get("dir") == "in":
        name = str(rec.get("user") or "")
        return f"{ctx['user_label']} ({name})" if name else ctx["user_label"]
    if rec.get("via") == "script" or rec.get("dir") == "script":
        return ctx.get("script_label") or ctx["assistant_label"]
    return ctx["assistant_label"]


def _seen_path(cfg) -> Path:
    return Path(cfg["data"]["dir"]) / SEEN_NAME


def _save_once(cfg, key: str, rec: dict, timeout: float = SEEN_LOCK_TIMEOUT) -> bool:
    """Append `rec` unless `key` was already recorded (the same prompt can pass the hook
    twice, possibly at the same time). Check, append and record run under one lock, and the
    id is recorded only after the append succeeded. True if appended. `timeout` is what is
    left of the whole hook call's lock budget (one try when it is 0)."""
    p = _seen_path(cfg)
    try:
        fd = fsutil.try_lock(p.with_name(SEEN_NAME + ".lock"), max(0.0, timeout))
        why = "busy" if fd is None else ""
    except OSError as e:
        fd, why = None, fsutil.safe_reason(e)
    if fd is None:
        fsutil.hook_error_log(f"capture inbound: duplicate-check lock {why}; message saved without the check")
        append(cfg, rec)
        return True
    try:
        try:
            ids = p.read_text(encoding="utf-8").split() if p.exists() else []
        except OSError:
            ids = []
        if key in ids:
            return False
        append(cfg, rec)  # raises on a write error: the id below is then NOT recorded
        ids.append(key)
        try:
            fsutil.write_text_atomic(p, "\n".join(ids[-SEEN_KEEP:]))
        except OSError:
            pass  # saved but not marked: a resent prompt may log it twice, never zero times
        return True
    finally:
        os.close(fd)  # releases the lock


def parse_inbound(cfg, prompt: str) -> list[dict]:
    sources = cfg["capture"]["sources"]
    out = []
    for attrs_raw, body in channel_blocks(prompt or ""):
        attrs = dict(ATTR_RE.findall(attrs_raw))
        channel = sources.get(attrs.get("source", ""))
        if not channel:
            continue
        text = body.strip()
        if not text:
            continue
        out.append({"ts": to_local(attrs.get("ts", "")), "dir": "in", "channel": channel,
                    "chat_id": attrs.get("chat_id", ""), "message_id": attrs.get("message_id", ""),
                    "user": attrs.get("user", ""), "text": text})
    return out


def inbound(cfg, payload: dict) -> int:
    if not isinstance(payload, dict):
        return 0
    n = 0
    prompt = payload.get("prompt", "")
    # The lock wait is one budget for the whole prompt (several blocks must not add up past
    # the hook's own timeout).
    deadline = time.monotonic() + SEEN_LOCK_TIMEOUT
    for rec in parse_inbound(cfg, prompt if isinstance(prompt, str) else ""):
        mid = rec["message_id"]
        if not mid:
            append(cfg, rec)
            n += 1
        elif _save_once(cfg, f"{rec['channel']}:{rec['chat_id']}:{mid}", rec, deadline - time.monotonic()):
            n += 1
    return n


def parse_outbound(cfg, payload: dict) -> dict | None:
    if not isinstance(payload, dict):
        return None
    tools = cfg["capture"]["reply_tools"]
    channel = tools.get(payload.get("tool_name", ""))
    if not channel:
        return None
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        return None
    text = str(ti.get("text") or "").strip()
    if not text:
        return None
    resp = payload.get("tool_response", "")
    if not isinstance(resp, str):
        resp = json.dumps(resp, ensure_ascii=False)
    m = REPLY_MSG_ID_RE.search(resp) or REPLY_ID_RE.search(resp)
    return {"ts": now_iso(), "dir": "out", "channel": channel, "chat_id": str(ti.get("chat_id", "")),
            "message_id": m.group(1) if m else "", "user": cfg["context"]["assistant_label"], "text": text}


def outbound(cfg, payload: dict) -> int:
    rec = parse_outbound(cfg, payload)
    if rec is None:
        return 0
    append(cfg, rec)
    return 1


def log_out(cfg, text: str, channel: str = "script", chat_id: str = "") -> dict:
    rec = {"ts": now_iso(), "dir": "out", "channel": channel, "chat_id": chat_id, "message_id": "",
           "user": cfg["context"]["assistant_label"], "via": "script", "text": text.strip()}
    append(cfg, rec)
    return rec


def tail(cfg, n: int = 20) -> list[dict]:
    p = log_path(cfg)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out

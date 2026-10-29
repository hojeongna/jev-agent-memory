"""Chunk labeling with Claude Haiku (default) — batched and hierarchical.

One `claude -p --model haiku` call (isolated: see llm.ClaudeBackend) labels ~20 chunks:
  1. pick ONE top-level topic,
  2. then ONE subtopic that belongs to THAT topic (never a subtopic of another topic),
  3. a second (topic, subtopic) only if the chunk is genuinely about two topics.
Strict JSON keyed by chunk id. Invalid or missing entries count as a failure for that
chunk only and are retried next run. A call that fails outright (network, login, usage
limit) counts against no chunk; `claude -p` pauses and retries later (llm.CallBreaker).

Haiku returns choices, not probabilities, so fixed values are stored: 0.9 for the first
choice and 0.3 for the second. Recall's thresholds (e.g. second label used when p >= 0.1)
keep working unchanged. Each unit records which labeler produced it.

Why Haiku by default (a small, non-blind internal test on the original author's taxonomy):
its top-level labels were about as good as Jev's, and with the hierarchical prompt its
subtopic always belongs to the chosen topic. Jev stays the recall-time router/picker.
"""
from __future__ import annotations

import re

from . import llm as llmmod

P_FIRST = 0.9
P_SECOND = 0.3
TIME_HEADING = re.compile(r"^#{1,4}\s*\d{1,2}:\d{2}[^\n]*\n?")

PROMPT = """You label excerpts of an AI assistant's daily log with topics from a fixed taxonomy.

For EACH item:
1. Choose exactly ONE top-level topic.
2. Then choose exactly ONE subtopic that belongs to THAT top-level topic. Never pick a
   subtopic listed under a different topic.
3. Only if the item is genuinely about two different topics, add a second choice
   (top-level + its own subtopic). Otherwise "second" is null.
Use the "{smalltalk}" topic only for casual chat with no lasting information.

Taxonomy (top-level topic, then its subtopics):
{taxonomy}

Answer ONLY with JSON, one entry for EVERY item id:
{{"labels": {{"c1": {{"top": "<topic>", "sub": "<subtopic>", "second": null}},
             "c2": {{"top": "<topic>", "sub": "<subtopic>", "second": {{"top": "<topic>", "sub": "<subtopic>"}}}}}}}}

Items:
{items}"""


def taxonomy_text(tax: dict) -> str:
    lines = []
    for top, node in tax["labeling"].items():
        lines.append(f"[{top}] {node['desc']}")
        for sub, desc in node["sub"].items():
            lines.append(f"  - {sub}: {desc}")
    return "\n".join(lines)


def clean_chunk(chunk: str, split_heading_re: str | None = None, limit: int = 900) -> str:
    """Drop a leading "## HH:MM summary"-style heading: it repeats on every digest chunk and
    carries no topic information."""
    text = chunk
    if split_heading_re:
        first, _, rest = text.partition("\n")
        if re.search(split_heading_re, first):
            text = rest
    text = TIME_HEADING.sub("", text, count=1)
    return " ".join(text.split())[:limit]


def build_prompt(tax: dict, items: list[tuple[str, str]]) -> str:
    body = "\n".join(f'<item id="{cid}">{text}</item>' for cid, text in items)
    return PROMPT.format(smalltalk=tax.get("smalltalk_topic") or "smalltalk",
                         taxonomy=taxonomy_text(tax), items=body)


def _norm_sub(top: str, sub) -> str | None:
    if not isinstance(sub, str):
        return None
    sub = sub.strip()
    if "/" in sub:
        t, _, s = sub.partition("/")
        if t.strip() != top:
            return None
        sub = s.strip()
    return sub


def _valid_pair(tax: dict, top, sub) -> tuple[str, str] | None:
    lab = tax["labeling"]
    if not isinstance(top, str) or top.strip() not in lab:
        return None
    top = top.strip()
    s = _norm_sub(top, sub)
    if s is None or s not in lab[top]["sub"]:
        return None
    return top, s


def parse_labels(tax: dict, text: str | None, ids: list[str]) -> dict:
    """-> {id: (top1, p1, top2, p2, sub1, p1, sub2, p2)} for valid entries only.
    A subtopic that doesn't belong to the chosen top-level topic invalidates the entry."""
    data = llmmod.parse_json(text)
    if isinstance(data, dict) and isinstance(data.get("labels"), dict):
        data = data["labels"]
    if not isinstance(data, dict):
        return {}
    out = {}
    for cid in ids:
        e = data.get(cid)
        if not isinstance(e, dict):
            continue
        first = _valid_pair(tax, e.get("top"), e.get("sub"))
        if first is None:
            continue
        t1, s1 = first
        t2 = s2 = None
        sec = e.get("second")
        if isinstance(sec, dict):
            pair = _valid_pair(tax, sec.get("top"), sec.get("sub"))
            if pair and pair != first:
                t2, s2 = pair
        out[cid] = (t1, P_FIRST,
                    t2 if t2 and t2 != t1 else None, P_SECOND if t2 and t2 != t1 else None,
                    f"{t1}/{s1}", P_FIRST,
                    f"{t2}/{s2}" if t2 else None, P_SECOND if t2 else None)
    return out


def default_backend(cfg):
    """Isolated `claude -p` (same flags, login and call breaker as the
    digest/consolidation backend). There is no Ollama fallback for labeling; unusable
    answers count toward `index.max_tries`, failed calls don't."""
    return llmmod.claude_backend(cfg)

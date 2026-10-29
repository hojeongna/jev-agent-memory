"""Taxonomy loading and validation.

Format (JSON):
{
  "version": "...",
  "smalltalk_topic": "smalltalk",
  "labeling": { "<topic>": {"desc": "long description", "sub": {"<sub>": "long desc", ...}}, ...},
  "routing":  { "<topic>": {"desc": "short description", "sub": {"<sub>": "short desc", ...}}, ...}
}

- `labeling` (long descriptions) is used once per chunk at index time. Long, concrete
  descriptions made labels reproducible (re-runs gave the same labels far more often).
- `routing` (short descriptions) is used on every message. Short descriptions routed
  better and use fewer tokens.
- Topic/sub names must be identical in both. Changing names requires `jevmem index --relabel`.
- The smalltalk topic is how "no memory needed" is detected: if a message routes there,
  nothing is injected.
"""
from __future__ import annotations

import json
from pathlib import Path

MAX_CHOICES = 255  # Jev `choice` limit


class TaxonomyError(ValueError):
    pass


def load(path) -> dict:
    p = Path(path)
    if not p.exists():
        raise TaxonomyError(f"taxonomy not found: {p}")
    try:
        tax = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise TaxonomyError(f"taxonomy {p} is not valid JSON: {e.msg} at line {e.lineno} "
                            f"column {e.colno}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise TaxonomyError(f"cannot read taxonomy {p}: {e}") from None
    if not isinstance(tax, dict):
        raise TaxonomyError(f"taxonomy {p} must be a JSON object")
    validate(tax)
    return tax


def validate(tax: dict) -> list[str]:
    """Raise TaxonomyError on hard errors; return a list of warnings."""
    warnings = []
    for key in ("labeling", "routing"):
        if key not in tax or not isinstance(tax[key], dict) or not tax[key]:
            raise TaxonomyError(f"taxonomy needs a non-empty '{key}' object")
    lab, rou = tax["labeling"], tax["routing"]
    if set(lab) != set(rou):
        raise TaxonomyError(
            f"topic names differ between labeling and routing: "
            f"{sorted(set(lab) ^ set(rou))}"
        )
    n_sub = 0
    for t in lab:
        if "/" in t:
            raise TaxonomyError(f"topic name may not contain '/': {t!r}")
        for side, tree in (("labeling", lab), ("routing", rou)):
            node = tree[t]
            if not isinstance(node, dict) or "desc" not in node or not node.get("sub"):
                raise TaxonomyError(f"{side}.{t} needs 'desc' and non-empty 'sub'")
        if set(lab[t]["sub"]) != set(rou[t]["sub"]):
            raise TaxonomyError(f"sub names differ for topic {t!r}")
        for s in lab[t]["sub"]:
            if "/" in s:
                raise TaxonomyError(f"sub name may not contain '/': {t}/{s}")
        n_sub += len(lab[t]["sub"])
    small = tax.get("smalltalk_topic")
    if not small:
        warnings.append("no smalltalk_topic: every message will trigger memory lookup")
    elif small not in lab:
        raise TaxonomyError(f"smalltalk_topic {small!r} is not a topic")
    if n_sub > MAX_CHOICES:
        raise TaxonomyError(f"{n_sub} subtopics > Jev choice limit {MAX_CHOICES}")
    if n_sub > 80:
        warnings.append(f"{n_sub} subtopics: routing prompt gets large; ~20-60 works well")
    return warnings


EXAMPLE_VERSION_PREFIX = "example"


def is_example(tax: dict) -> bool:
    """The English example taxonomy `jevmem init` installs (version "example-1")."""
    return str(tax.get("version") or "").startswith(EXAMPLE_VERSION_PREFIX)


def smalltalk(tax: dict) -> str | None:
    return tax.get("smalltalk_topic")


def labeling_criteria(tax: dict):
    """Two criteria sets for index-time labeling: top-level and flat subtopics."""
    t = tax["labeling"]
    top = {k: v["desc"] for k, v in t.items()}
    flat = {f"{k}/{s}": f"[{v['desc'][:60]}] {d}" for k, v in t.items() for s, d in v["sub"].items()}
    return top, flat


def routing_criteria(tax: dict) -> dict:
    t = tax["routing"]
    return {f"{k}/{s}": f"{v['desc']} — {d}" for k, v in t.items() for s, d in v["sub"].items()}

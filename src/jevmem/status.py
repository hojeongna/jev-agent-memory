"""Last-run bookkeeping for `jevmem doctor` (data/status.json)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import fsutil


def _path(cfg) -> Path:
    return Path(cfg["data"]["dir"]) / "status.json"


def read(cfg) -> dict:
    try:
        return json.loads(_path(cfg).read_text(encoding="utf-8"))
    except Exception:
        return {}


def record(cfg, job: str, result: str, detail: str = "") -> None:
    try:
        data = read(cfg)
        data[job] = {"ts": datetime.now().isoformat(timespec="seconds"), "result": result,
                     "detail": str(detail)[:200]}
        fsutil.write_text_atomic(_path(cfg), json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        pass

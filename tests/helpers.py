"""Shared test helpers: a FAKE Jev client (no network, no cost), a fake clock, temp projects.

No test in this suite ever calls the real Jev API. Subprocess tests additionally point the
client at a closed local port and an unset key variable as a second safety net.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
EXAMPLES = REPO / "examples"
sys.path.insert(0, str(SRC))

from jevmem import config  # noqa: E402
from jevmem.jev_client import BillingError, JevError, JevTimeout  # noqa: E402

NO_KEY_ENV = "JEVMEM_TEST_KEY_THAT_IS_NEVER_SET"
DEAD_URL = "http://127.0.0.1:9/never"

# The per-user hook error log ($XDG_STATE_HOME/jevmem/hook-errors.log) must never be the
# real one during tests: point XDG_STATE_HOME at a temp dir for this process and every
# subprocess started by run_cli.
_STATE_HOME = tempfile.TemporaryDirectory(prefix="jevmem-test-state-")
os.environ["XDG_STATE_HOME"] = _STATE_HOME.name


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _uniform(criteria: dict, boost: dict | None = None) -> dict:
    boost = boost or {}
    rest = max(0.0, 1.0 - sum(boost.values()))
    others = [k for k in criteria if k not in boost]
    each = rest / len(others) if others else 0.0
    return {k: boost.get(k, each) for k in criteria}


class FakeJev:
    """Stand-in for JevClient.

    route: dict {"topic/sub": prob} given to the routing question (others share the rest)
    pick : callable(criteria: dict[id -> excerpt]) -> dict[id -> prob]
    labeler: callable(state) -> (top, sub) for index-time labeling
    error: exception instance raised on every call (or on call number `error_on`)
    latency: seconds added to `clock` per call
    """

    def __init__(self, route=None, pick=None, labeler=None, error=None, error_on=None,
                 latency=0.0, clock=None, in_tokens=1000):
        self.route = route or {}
        self.pick = pick
        self.labeler = labeler
        self.error = error
        self.error_on = error_on
        self.latency = latency
        self.clock = clock
        self.in_tokens = in_tokens
        self.calls = []

    def call(self, state, questions, timeout=2.0):
        self.calls.append({"state": state, "questions": questions, "timeout": timeout})
        n = len(self.calls)
        if self.clock is not None and self.latency:
            self.clock.advance(self.latency)
        if self.error is not None and (self.error_on is None or self.error_on == n):
            raise self.error
        answers = {}
        for key, q in questions.items():
            crit = q["criteria"]
            if key in ("top", "flat"):  # index-time labeling
                top, sub = self.labeler(state) if self.labeler else (next(iter(crit)).split("/")[0], None)
                target = top if key == "top" else f"{top}/{sub}"
                answers[key] = {"probabilities": _uniform(crit, {target: 0.7} if target in crit else None)}
            elif any("/" in k for k in crit):  # routing
                answers[key] = {"probabilities": _uniform(crit, {k: v for k, v in self.route.items() if k in crit})}
            else:  # pick
                probs = self.pick(crit) if self.pick else {}
                answers[key] = {"probabilities": _uniform(crit, probs)}
        return answers, {"input_tokens": self.in_tokens}


def keyword_labeler(rules):
    """rules = [(substring, top, sub), ...]; first match wins, else smalltalk/chitchat."""
    def lab(state):
        low = state.lower()
        for needle, top, sub in rules:
            if needle.lower() in low:
                return top, sub
        return "smalltalk", "chitchat"
    return lab


DEFAULT_RULES = [
    ("backup", "coding", "bugs-debugging"),
    ("hotfix", "coding", "code-review-deploy"),
    ("pr #", "coding", "code-review-deploy"),
    ("ci failed", "coding", "code-review-deploy"),
    ("memory hook", "coding", "agent-automation"),
    ("laptop", "coding", "tools-setup"),
    ("export api spec", "work", "planning-docs"),
    ("meeting", "work", "meetings"),
    ("1:1", "work", "meetings"),
    ("blocker", "work", "communication"),
    ("q4", "work", "planning-docs"),
    ("side project", "side-projects", "building"),
    ("invoice", "learning", "research"),
    ("run", "health", "exercise-diet"),
    ("knee", "health", "medical"),
    ("slept", "health", "sleep-energy"),
    ("birthday", "family-home", "events-plans"),
    ("restaurant", "family-home", "events-plans"),
    ("insurance", "admin-money", "paperwork"),
    ("series", "hobbies-media", "music-video"),
    ("board game", "hobbies-media", "games"),
]


class TempProject:
    """A temp directory with jevmem.toml, example logs and the example taxonomy."""

    def __init__(self, logs=True, extra_cfg: str = ""):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        (self.root / "memory").mkdir()
        if logs:
            for p in sorted((EXAMPLES / "logs").glob("*.md")):
                shutil.copy(p, self.root / "memory" / p.name)
        (self.root / ".jevmem").mkdir()
        shutil.copy(EXAMPLES / "taxonomy.example.json", self.root / ".jevmem" / "taxonomy.json")
        self.cfg_path = self.root / "jevmem.toml"
        self.cfg_path.write_text(
            f'[logs]\ndir = "memory"\n\n[jev]\napi_key_env = "{NO_KEY_ENV}"\nurl = "{DEAD_URL}"\n\n'
            # Safety net: tests never start a real `claude` or ollama process. Tests that
            # exercise those paths inject fakes or override these per test.
            '[llm]\nbackend = "none"\nfallback = "none"\n\n[index]\nlabeler = "jev"\n\n'
            + extra_cfg, encoding="utf-8")

    def cfg(self, overrides=None):
        return config.load(str(self.cfg_path), overrides)

    def cleanup(self):
        self._td.cleanup()


def run_cli(*args, cfg_path=None, stdin=None, env_extra=None, cwd=None):
    env = dict(os.environ)
    env.pop("TYPESAFE_API_KEY", None)
    env.pop("JEV_API_KEY", None)
    env.pop("JEVMEM_CONFIG", None)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    cmd = [sys.executable, "-m", "jevmem"]
    if cfg_path is not None:
        cmd += ["--config", str(cfg_path)]
    return subprocess.run(cmd + list(args), input=stdin, capture_output=True, text=True, env=env, cwd=cwd)


def hook_error_log_text() -> str:
    """Contents of the test-run hook error log ('' if nothing was logged)."""
    p = Path(_STATE_HOME.name) / "jevmem" / "hook-errors.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


__all__ = ["BillingError", "JevError", "JevTimeout", "FakeJev", "FakeClock", "TempProject",
           "keyword_labeler", "DEFAULT_RULES", "run_cli", "json"]

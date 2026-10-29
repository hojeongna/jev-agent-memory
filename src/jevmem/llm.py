"""Text-generation backends for digest summaries and daily consolidation.

Jev cannot write text, so these two jobs use a separate LLM ([llm].backend, then
[llm].fallback for both jobs; chunk labeling uses ClaudeBackend directly, see labeler.py):
  - ollama : a local model through Ollama's /api/chat (default model gemma3)
  - claude : `claude -p --model haiku` (Claude Code CLI with its own login;
             ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are removed from the child's
             environment unless [llm] claude_use_api_key = true)
  - none   : no LLM. Digest writes raw conversation lines; daily consolidation is skipped.

Outages: when a `claude -p` call itself fails (non-zero exit, timeout, missing binary, usage
limit or login error), CallBreaker pauses further `claude -p` calls with an exponential
backoff (30 min, 1 h, 2 h, then every 4 h by default) and a success resets it. Callers see
LLMUnavailable from `ClaudeBackend.call`, so labeling does not count the outage against the
chunks (see indexer._label_haiku); `complete` just returns None, so the digest falls back to
Ollama, then to raw lines.

`claude -p` is isolated six ways (see ClaudeBackend):
  - EMPTY temporary working directory: run from your agent's project, Claude Code would load
    that project's CLAUDE.md and hooks, and the headless process could take on the agent's
    persona and start acting on its own (this happened in the original deployment);
  - JEVMEM_DISABLE_HOOK=1: jevmem's own hooks are no-ops even if installed at user scope;
  - --strict-mcp-config with an empty MCP config: no plugins/MCP servers start in the child,
    so a Telegram/Discord channel plugin can't steal the main session's message polling;
  - no tools: the conversation being summarized is untrusted text. `--tools ""` (every
    built-in tool off) when `claude --help` lists that option ([llm] claude_tools_off =
    "auto"), plus --disallowedTools (default Bash, Edit, Write, NotebookEdit, WebFetch,
    WebSearch; extended with Read, Glob, Grep, Task and the other built-in tools when
    `--tools` can't be used);
  - the prompt goes through stdin, not the command line (UTF-8 both ways, whatever the
    locale), and a call that times out is killed with its whole process group (run_child);
  - the Jev key variable ([jev] api_key_env) is removed from the child's environment.

Any object with `.complete(prompt) -> str | None` can be used instead (tests use fakes).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import fsutil

DISABLE_ENV = "JEVMEM_DISABLE_HOOK"
# Removed from the `claude -p` child's environment (it uses its own login) unless
# [llm] claude_use_api_key = true.
API_KEY_ENVS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class LLMUnavailable(Exception):
    """The call itself failed or was skipped because the breaker is open (`skipped`). Not
    the prompt's fault: callers must not count it against the input (e.g. a chunk's
    `tries`). The message is a short fixed reason, never model output or prompt text."""

    def __init__(self, msg: str, skipped: bool = False):
        super().__init__(msg)
        self.skipped = skipped


class Backend:
    name = "base"

    def complete(self, prompt: str) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError


def call(backend, prompt: str) -> str | None:
    """`backend.call(prompt)` when the backend has it (it may raise LLMUnavailable),
    otherwise `backend.complete(prompt)`. None means "answered, but nothing usable"."""
    fn = getattr(backend, "call", None)
    return fn(prompt) if callable(fn) else backend.complete(prompt)


class CallBreaker:
    """Exponential backoff for `claude -p` call failures, shared by every process through a
    small JSON state file in the data dir (created 0600):
        {"failures": n, "next_try": epoch, "reason": "...", "since": "..."}
    After failure n the next call waits start * 2**(n-1) minutes, at most `max_minutes`.
    A successful call deletes the state."""

    MAX_FAILURES = 1000  # the stored count stops here (the pause stopped growing long before)

    def __init__(self, path, start_minutes: float = 30, max_minutes: float = 240, clock=time.time):
        self.path = Path(path)
        self.start = max(0.0, float(start_minutes))
        self.max = max(self.start, float(max_minutes))
        self.clock = clock

    def state(self) -> dict:
        try:
            st = json.loads(self.path.read_text(encoding="utf-8"))
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            return {}

    @classmethod
    def failures_of(cls, st: dict) -> int:
        """The stored failure count, made safe: not a number / negative -> 0, huge -> capped."""
        try:
            n = int(float(st.get("failures", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            return 0
        return min(max(n, 0), cls.MAX_FAILURES)

    def blocked(self, persist: bool = True) -> dict | None:
        """The state while calls are paused, else None. A resume time further away than the
        longest pause (a clock that was set wrong, a damaged file) is pulled back to
        now + max_minutes (and saved unless `persist` is False), so a pause can never outlast
        the configured maximum."""
        st = self.state()
        try:
            now = self.clock()
            nxt = float(st.get("next_try", 0))
            if not now < nxt:  # also False for NaN
                return None
            if nxt > now + self.max * 60:
                st = {**st, "next_try": now + self.max * 60}
                if persist:
                    try:
                        fsutil.write_text_atomic(self.path, json.dumps(st))
                    except (OSError, ValueError):
                        pass
            return st
        except (TypeError, ValueError, OverflowError):
            return None

    def pause_minutes(self, failures) -> float:
        """start * 2**(failures-1), at most max. The exponent stops growing once the maximum
        is reached, so any failure count (even a huge or damaged one) is safe."""
        try:
            k = max(0, int(failures) - 1)
        except (TypeError, ValueError, OverflowError):
            k = 0
        m = self.start
        for _ in range(min(k, 64)):
            if m >= self.max:
                break
            m *= 2
        return min(self.max, m)

    def failure(self, reason: str) -> dict:
        """Record one failed call; never raises (a broken state file just restarts the count)."""
        now = self.clock()
        try:
            st = self.state()
            n = min(self.failures_of(st) + 1, self.MAX_FAILURES)
            since = st.get("since") if isinstance(st.get("since"), str) else None
            st = {"failures": n, "next_try": now + self.pause_minutes(n) * 60, "reason": str(reason)[:80],
                  "since": since or datetime.fromtimestamp(now).isoformat(timespec="seconds")}
        except Exception:
            st = {"failures": 1, "next_try": now + self.start * 60, "reason": str(reason)[:80], "since": ""}
        try:
            fsutil.write_text_atomic(self.path, json.dumps(st))
        except (OSError, ValueError):
            pass
        return st

    def success(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def pause_text(st: dict) -> str:
    """'paused until 14:30 after 2 failed calls (usage limit)'."""
    try:
        until = datetime.fromtimestamp(float(st["next_try"])).strftime("%H:%M")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        until = "?"
    n = CallBreaker.failures_of(st) if "failures" in st else "?"
    return f"paused until {until} after {n} failed call{'s' if n != 1 else ''} ({st.get('reason') or '?'})"


# Output that means the call failed even when `claude` exited 0 (only checked on short,
# non-JSON output: every jevmem prompt asks for JSON, so a real answer is never mistaken).
_FAILURE_PATTERNS = (
    ("usage limit", re.compile(r"usage limit|limit reached|rate limit|out of (?:extra )?usage|"
                               r"quota|too many requests|\b429\b", re.I)),
    ("login", re.compile(r"/login|log ?in|logged out|not logged|oauth|authenticat|"
                         r"invalid api key|credential|\b401\b|\b403\b", re.I)),
    ("network", re.compile(r"network|connection|ECONN|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|offline|"
                           r"socket|fetch failed", re.I)),
    ("overloaded", re.compile(r"overloaded|\b529\b|\b5\d\d\b|api error|internal server", re.I)),
)


def failure_reason(text: str, rc: int | None = None) -> str:
    """Short fixed label for a failed call; the raw output is never kept."""
    for label, rx in _FAILURE_PATTERNS:
        if rx.search(text or ""):
            return label
    return f"exit {rc}" if rc not in (None, 0) else "error"


# Stricter: what `claude` itself prints instead of an answer (checked when it exits 0).
_CLI_ERROR_RE = re.compile(r"usage limit|limit reached|rate limit|invalid api key|please run /login|"
                           r"not logged in|oauth token|authentication_error|credit balance|"
                           r"api error|overloaded_error", re.I)


def looks_like_failure(text: str) -> bool:
    """`claude` exited 0 but printed an error message instead of an answer."""
    t = (text or "").strip()
    if not t or len(t) > 400 or parse_json(t) is not None:
        return False
    return bool(_CLI_ERROR_RE.search(t))


class NoneBackend(Backend):
    name = "none"

    def complete(self, prompt: str) -> str | None:
        return None

    def check(self) -> tuple[bool, str]:
        return False, "no text LLM: digest writes raw lines, consolidation is disabled"


class OllamaBackend(Backend):
    name = "ollama"

    def __init__(self, url: str, model: str, timeout: float = 120, max_tokens: int = 800):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

    def complete(self, prompt: str) -> str | None:
        payload = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                   "format": "json", "stream": False, "think": False,
                   "options": {"num_predict": self.max_tokens, "temperature": 0.2}}
        req = urllib.request.Request(self.url + "/api/chat", data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        return (body.get("message", {}).get("content") or "").strip() or None

    def check(self) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=3) as r:
                tags = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            return False, f"ollama not reachable at {self.url} ({type(e).__name__})"
        names = [m.get("name", "") for m in tags.get("models", [])]
        if not any(n == self.model or n.split(":")[0] == self.model.split(":")[0] for n in names):
            return False, f"ollama is up but model {self.model!r} is not pulled (ollama pull {self.model})"
        return True, f"ollama {self.url} with model {self.model}"


EMPTY_MCP = {"mcpServers": {}}
DEFAULT_DISALLOWED_TOOLS = "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch"
# Added to the deny list when `--tools ""` (no tools at all) can't be used: the built-in tools
# that read files, search, browse or start sub-agents, on top of the write/exec/network ones.
EXTRA_DENIED_TOOLS = ("Read", "Glob", "Grep", "LS", "NotebookRead", "NotebookEdit", "WebFetch", "WebSearch",
                      "Task", "Agent", "TodoWrite", "Skill", "MultiEdit", "BashOutput", "KillShell",
                      "Bash", "Edit", "Write")
_HELP_CACHE: dict = {}


def tool_args(value) -> list[str]:
    """[llm] claude_disallowed_tools -> argv words after --disallowedTools.
    A TOML array: one argument per entry. A string: split on commas ONLY (never on spaces,
    so `Bash(rm -rf *)` survives) and passed as one comma-separated argument."""
    if isinstance(value, (list, tuple)):
        return [str(t).strip() for t in value if str(t).strip()]
    parts = [t.strip() for t in str(value or "").split(",") if t.strip()]
    return [",".join(parts)] if parts else []


def merged_deny(words: list[str], as_list: bool, extra=EXTRA_DENIED_TOOLS) -> list[str]:
    """The configured deny list plus `extra` (names already listed are not repeated), in the
    configured form: a string setting stays ONE comma-separated argument, an array setting
    gets one argument per entry. An empty setting (the flag turned off) stays empty."""
    if not words:
        return []
    have = {w.strip() for arg in words for w in (arg.split(",") if not as_list else [arg])}
    add = [t for t in extra if t not in have]
    if as_list:
        return list(words) + add
    return [",".join([words[0]] + add)] if add else list(words)


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the child and everything it started (its own process group, see run_child)."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_child(argv: list[str], *, cwd: str, env: dict, input: str | None = None,
              timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a `claude` child: `input` (or nothing: stdin is /dev/null) as UTF-8 on stdin,
    stdout/stderr read as UTF-8 (undecodable bytes replaced), whatever the locale. The child
    gets its own process group, and on a timeout the whole group is killed, so processes a
    wrapper script or Node started don't linger; TimeoutExpired is then raised (without the
    output: nothing of the prompt or answer is in its text)."""
    kw = {"start_new_session": True} if os.name == "posix" else {}
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", cwd=cwd, env=env, **kw)
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        _reap(proc)
        raise subprocess.TimeoutExpired(argv[:1], timeout) from None
    except BaseException:
        _kill_group(proc)
        _reap(proc)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _reap(proc: subprocess.Popen) -> None:
    for f in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if f:
                f.close()
        except (OSError, ValueError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def help_lists_tools_flag(help_text: str) -> bool:
    """Does `claude --help` list the `--tools` option (whose "" value turns every tool off)?"""
    return bool(re.search(r"^\s*--tools\b", help_text or "", re.M))


class ClaudeBackend(Backend):
    """`claude -p` in an isolated, plugin-free, tool-free child process.

    Every call runs, with the prompt on STDIN (never on the command line, where other users of
    the machine could read it with `ps` and where Linux limits one argument to 128 KiB):
        claude -p --model haiku --tools "" --disallowedTools Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch
               --strict-mcp-config --mcp-config <tmp>/empty.json
    from an empty temporary directory, with JEVMEM_DISABLE_HOOK=1.

    Tools: the chat being summarized is untrusted text (anyone in a group chat can write it).
    `--tools ""` removes every built-in tool. Whether this `claude` has that option is read
    from `claude --help` (no model call; cached per binary) when `tools_off` is "auto"; when
    it is missing or the help can't be read, the deny list is extended with the tools that
    read files, search, browse or start agents (EXTRA_DENIED_TOOLS). The configured deny
    list (`disallowed_tools`, [llm] claude_disallowed_tools) is always passed as well; set to
    "" it turns the --disallowedTools flag off entirely.

    Why --strict-mcp-config + an empty MCP config: without them the child loads the same
    user-scope plugins as your main session. A chat channel plugin (Telegram/Discord) in the
    child then competes for the bot's message polling, and the main session's plugin
    disconnects. The empty config means the child starts no MCP servers at all.
    Another flag always follows a variadic list (--tools, --disallowedTools), so it can't
    swallow anything. `disallowed_tools` as a string is split on commas only and passed as
    ONE comma-separated argument (a pattern such as `Bash(rm -rf *)` keeps its spaces); as a
    list (TOML array) every entry is its own argument.
    Not --bare: in that mode `claude` reads only an API key, never the OAuth login.

    Login: ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN are removed from the child's environment
    (so `claude` uses its own login) unless `use_api_key` ([llm] claude_use_api_key) is true.
    `breaker` (CallBreaker): while it is open, `call` raises LLMUnavailable without starting
    `claude`; a failed call opens/extends it, a successful one resets it.
    """

    name = "claude"

    def __init__(self, model: str = "haiku", binary: str = "claude", timeout: float = 180,
                 disallowed_tools=DEFAULT_DISALLOWED_TOOLS, use_api_key: bool = False,
                 breaker: CallBreaker | None = None, tools_off="auto", drop_env=()):
        self.model = model
        self.binary = binary
        self.timeout = timeout
        self.disallowed_tools = tool_args(disallowed_tools)
        self._deny_as_list = isinstance(disallowed_tools, (list, tuple))
        self.use_api_key = bool(use_api_key)
        self.breaker = breaker
        self.tools_off = tools_off
        self.drop_env = tuple(k for k in drop_env if k)  # e.g. the Jev key variable: the child never needs it
        self.last_error: str | None = None

    def deny_list(self, tools_off: bool | None) -> list[str]:
        """--disallowedTools words: the configured list, extended unless `--tools ""` is used."""
        if tools_off:
            return list(self.disallowed_tools)
        return merged_deny(self.disallowed_tools, self._deny_as_list)

    def argv(self, exe: str, mcp_config: str, prompt: str | None = None,
             tools_off: bool | None = None) -> list[str]:
        """`prompt` None: the `--version` check. Otherwise print mode; the prompt itself goes
        to stdin, not into argv."""
        if prompt is None:
            head = [exe, "--version"]
        else:
            head = [exe, "-p", "--model", self.model]
            if tools_off:
                head += ["--tools", ""]
            deny = self.deny_list(tools_off)
            if deny:
                head += ["--disallowedTools", *deny]
        return head + ["--strict-mcp-config", "--mcp-config", mcp_config]

    def child_env(self) -> dict:
        env = dict(os.environ)
        env[DISABLE_ENV] = "1"
        if not self.use_api_key:
            for k in API_KEY_ENVS:
                env.pop(k, None)
        for k in self.drop_env:
            env.pop(k, None)
        return env

    def supports_tools_flag(self, exe: str) -> bool | None:
        """True/False from `claude --help` (no model call), None if the help can't be read.
        Cached per binary path, size and modification time."""
        try:
            st = os.stat(exe)
            key = (os.path.realpath(exe), st.st_size, st.st_mtime_ns)
        except OSError:
            return None
        if key in _HELP_CACHE:
            return _HELP_CACHE[key]
        try:
            with tempfile.TemporaryDirectory(prefix="jevmem-isolated-") as cwd:
                cp = run_child([exe, "--help"], timeout=15, cwd=cwd, env=self.child_env())
            found = help_lists_tools_flag(cp.stdout) if cp.returncode == 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            found = None
        _HELP_CACHE[key] = found
        return found

    def use_tools_off(self, exe: str) -> bool:
        mode = self.tools_off
        if isinstance(mode, bool):
            return mode
        return bool(self.supports_tools_flag(exe))

    def _run(self, prompt: str | None, timeout: float):
        exe = shutil.which(self.binary) or self.binary
        tools_off = self.use_tools_off(exe) if prompt is not None else None
        with tempfile.TemporaryDirectory(prefix="jevmem-isolated-") as cwd:
            cfg = os.path.join(cwd, "empty.json")
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump(EMPTY_MCP, f)
            return run_child(self.argv(exe, cfg, prompt, tools_off), input=prompt, timeout=timeout, cwd=cwd,
                             env=self.child_env())

    def _failed(self, reason: str):
        msg = f"claude -p failed ({reason})"
        if self.breaker is not None:
            try:
                msg += "; " + pause_text(self.breaker.failure(reason))
            except Exception:  # recording the pause must never turn an outage into a crash
                pass
        self.last_error = msg
        raise LLMUnavailable(msg)

    def call(self, prompt: str) -> str | None:
        """The answer text (None if `claude` printed nothing). Raises LLMUnavailable when the
        call itself failed or the breaker is open. `last_error` keeps that short message."""
        self.last_error = None
        if self.breaker is not None:
            st = self.breaker.blocked()
            if st:
                self.last_error = f"claude -p {pause_text(st)}"
                raise LLMUnavailable(self.last_error, skipped=True)
        reason = None
        try:
            cp = self._run(prompt, self.timeout)
        except subprocess.TimeoutExpired:
            reason = "timeout"
        except (OSError, ValueError) as e:  # ValueError: e.g. a UnicodeError
            reason = type(e).__name__
        if reason:
            self._failed(reason)  # raised outside the except block: no chained exception
        out = (cp.stdout or "").strip()
        if cp.returncode != 0:
            self._failed(failure_reason((cp.stderr or "") + "\n" + out, cp.returncode))
        if looks_like_failure(out):
            self._failed(failure_reason(out))
        if self.breaker is not None:
            self.breaker.success()
        return out or None

    def complete(self, prompt: str) -> str | None:
        try:
            return self.call(prompt)
        except LLMUnavailable:
            return None

    def check(self) -> tuple[bool, str]:
        exe = shutil.which(self.binary)
        if not exe:
            return False, f"`{self.binary}` not found on PATH"
        try:
            cp = self._run(None, 15)
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            return False, f"`{self.binary} --version` failed ({type(e).__name__})"
        return cp.returncode == 0, f"{exe} ({(cp.stdout or cp.stderr).strip()[:40]}), model {self.model}"


def unavailable_note(backend) -> str | None:
    """The last LLMUnavailable message of a ClaudeBackend (alone or inside a chain)."""
    for b in getattr(backend, "backends", None) or [backend]:
        note = getattr(b, "last_error", None)
        if note:
            return note
    return None


class ChainBackend(Backend):
    """Try backends in order; the first non-empty answer wins. `last` = who answered."""

    def __init__(self, backends: list[Backend]):
        self.backends = backends
        self.name = ">".join(b.name for b in backends)
        self.last = None

    def complete(self, prompt: str) -> str | None:
        for b in self.backends:
            out = b.complete(prompt)
            if out:
                self.last = b.name
                return out
        return None


def make_backend(cfg, name: str) -> Backend:
    lc = cfg["llm"]
    name = (name or "none").lower()
    if name == "ollama":
        return OllamaBackend(lc["ollama_url"], lc["ollama_model"], float(lc["timeout_sec"]))
    if name == "claude":
        return claude_backend(cfg)
    return NoneBackend()


def breaker(cfg, clock=time.time) -> CallBreaker:
    lc = cfg["llm"]
    return CallBreaker(cfg["data"]["llm_breaker"], float(lc.get("claude_pause_minutes", 30)),
                       float(lc.get("claude_pause_max_minutes", 240)), clock)


def claude_backend(cfg) -> ClaudeBackend:
    lc = cfg["llm"]
    return ClaudeBackend(lc["claude_model"], lc["claude_bin"], float(lc["timeout_sec"]),
                         lc.get("claude_disallowed_tools", DEFAULT_DISALLOWED_TOOLS),
                         use_api_key=bool(lc.get("claude_use_api_key", False)), breaker=breaker(cfg),
                         tools_off=lc.get("claude_tools_off", "auto"),
                         drop_env=(cfg["jev"].get("api_key_env") or "TYPESAFE_API_KEY",))


def ignored_api_key_envs(cfg) -> list[str]:
    """API key variables set in this environment that `claude -p` children won't see."""
    if cfg["llm"].get("claude_use_api_key", False):
        return []
    return [k for k in API_KEY_ENVS if os.environ.get(k)]


def backends(cfg) -> list[Backend]:
    lc = cfg["llm"]
    out, seen = [], set()
    for name in (lc.get("backend"), lc.get("fallback")):
        name = (name or "none").lower()
        if name != "none" and name not in seen:
            seen.add(name)
            out.append(make_backend(cfg, name))
    return out


def get_backend(cfg) -> Backend:
    """Primary [llm].backend, then [llm].fallback; NoneBackend if neither is configured.
    A digest with no answer from either writes raw lines."""
    bs = backends(cfg)
    if not bs:
        return NoneBackend()
    return bs[0] if len(bs) == 1 else ChainBackend(bs)


def parse_json(text: str | None):
    """Lenient JSON extraction: strips code fences, finds the first {...} or [...] block."""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```\w*\s*|\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    for open_, close in (("{", "}"), ("[", "]")):
        i, j = t.find(open_), t.rfind(close)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except ValueError:
                continue
    return None

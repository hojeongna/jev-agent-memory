"""`jevmem install-hook`: merge jevmem's hooks into Claude Code settings.

  UserPromptSubmit : jevmem capture inbound   (stage 1: log chat-app messages)
                     jevmem hook              (stage 3: recall injection)
  PostToolUse      : jevmem capture outbound  (matcher: the chat plugins' reply tools)

- Backs up the existing settings file (settings.json.bak-YYYYmmdd-HHMMSS, created 0600)
  before writing, and keeps its permissions (e.g. 0600) when rewriting it. A run that
  changes nothing writes nothing and makes no backup.
- Idempotent: an existing jevmem hook entry is updated in place, never duplicated.
- `--no-capture` installs only the recall hook and removes jevmem capture hooks already
  in that settings file.
- Leaves every other setting and hook untouched.
- Remembers which settings files it wrote (<data>/hook_settings.json) so `jevmem doctor`
  can check them even when `--project` pointed somewhere else.
- Each hook command starts with the ABSOLUTE path of the Python running this command
  (sys.executable), so hooks work without your shell's PATH. If that interpreter goes away
  (virtualenv recreated, pipx reinstall, Python upgrade), run `jevmem install-hook` again;
  `jevmem doctor` reports a hook whose interpreter is missing.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path

from . import fsutil

HOOK_TIMEOUT_SEC = 10
CAPTURE_TIMEOUT_SEC = 5
CAPTURE_KEYS = ("capture inbound", "capture outbound")
RECORD_NAME = "hook_settings.json"


def _cmd(config_path: Path, sub: str, python: str | None = None) -> str:
    py = python or sys.executable
    return f"{shlex.quote(py)} -m jevmem {sub} --config {shlex.quote(str(config_path))}"


def hook_command(config_path: Path, python: str | None = None) -> str:
    return _cmd(config_path, "hook", python)


def desired_hooks(config_path: Path, reply_tools, python: str | None = None, capture_hooks: bool = True):
    """[(event, matcher or None, key, entry)] — `key` identifies our entry across updates."""
    out = []
    if capture_hooks:
        out.append(("UserPromptSubmit", None, "capture inbound",
                    {"type": "command", "command": _cmd(config_path, "capture inbound", python),
                     "timeout": CAPTURE_TIMEOUT_SEC}))
    out.append(("UserPromptSubmit", None, "hook",
                {"type": "command", "command": hook_command(config_path, python), "timeout": HOOK_TIMEOUT_SEC}))
    if capture_hooks and reply_tools:
        out.append(("PostToolUse", "|".join(sorted(reply_tools)), "capture outbound",
                    {"type": "command", "command": _cmd(config_path, "capture outbound", python),
                     "timeout": CAPTURE_TIMEOUT_SEC}))
    return out


def settings_path(project_dir: Path, scope: str) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    name = "settings.local.json" if scope == "local" else "settings.json"
    return Path(project_dir) / ".claude" / name


def _key_of(h) -> str | None:
    cmd = h.get("command", "") if isinstance(h, dict) else ""
    for key in ("capture inbound", "capture outbound", "hook"):
        if f"-m jevmem {key} " in cmd + " ":
            return key
    return None


def merge_settings(settings: dict, wanted) -> tuple[dict, str]:
    """Insert/update every wanted entry. action: 'added' | 'updated' | 'unchanged'."""
    s = json.loads(json.dumps(settings)) if settings else {}
    hooks = s.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("'hooks' in settings is not an object; refusing to modify")
    actions = []
    for event, matcher, key, entry in wanted:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"'hooks.{event}' is not a list; refusing to modify")
        found = False
        for g in groups:
            if not isinstance(g, dict):
                continue
            for i, h in enumerate(g.get("hooks", [])):
                if _key_of(h) == key:
                    found = True
                    same_matcher = (g.get("matcher") or None) == matcher
                    if h == entry and same_matcher:
                        actions.append("unchanged")
                    else:
                        g["hooks"][i] = entry
                        if matcher is not None:
                            g["matcher"] = matcher
                        actions.append("updated")
                    break
            if found:
                break
        if not found:
            group = {"hooks": [entry]}
            if matcher is not None:
                group = {"matcher": matcher, "hooks": [entry]}
            groups.append(group)
            actions.append("added")
    if all(a == "unchanged" for a in actions):
        return s, "unchanged"
    return s, "added" if "added" in actions else "updated"


def jevmem_commands(path: Path) -> list[str]:
    """The jevmem hook commands in a settings file ([] if missing or unreadable)."""
    try:
        s = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    hooks = s.get("hooks") if isinstance(s, dict) else None
    for groups in (hooks or {}).values() if isinstance(hooks, dict) else []:
        for g in groups if isinstance(groups, list) else []:
            for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                if _key_of(h):
                    out.append(h["command"])
    return out


def command_python(command: str) -> str | None:
    """First word of a hook command (the interpreter path)."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    return words[0] if words else None


def remove_from_settings(settings: dict, keys=None) -> tuple[dict, int]:
    """Remove jevmem hook entries (all, or only those whose key is in `keys`)."""
    s = json.loads(json.dumps(settings)) if settings else {}
    n = 0
    hooks = s.get("hooks", {})
    for event, groups in list(hooks.items() if isinstance(hooks, dict) else []):
        if not isinstance(groups, list):
            continue
        for g in list(groups):
            if not isinstance(g, dict):
                continue
            before = len(g.get("hooks", []))
            g["hooks"] = [h for h in g.get("hooks", [])
                          if _key_of(h) is None or (keys is not None and _key_of(h) not in keys)]
            n += before - len(g["hooks"])
            if not g["hooks"]:
                groups.remove(g)
    return s, n


def command_config(command: str) -> str | None:
    """The --config value of a hook command."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for i, w in enumerate(words[:-1]):
        if w == "--config":
            return words[i + 1]
    return None


def _same_path(a, b) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except (OSError, RuntimeError):
        return False


def _record_path(cfg) -> Path:
    return Path(cfg["data"]["dir"]) / RECORD_NAME


def recorded_settings_paths(cfg) -> list[str]:
    try:
        data = json.loads(_record_path(cfg).read_text(encoding="utf-8"))
        return [str(p) for p in data.get("settings", []) if isinstance(p, str)]
    except (OSError, ValueError, AttributeError):
        return []


def record_settings_path(cfg, settings, removed: bool = False) -> None:
    """Remember (or forget) a settings file install-hook wrote, for `jevmem doctor`."""
    paths = [p for p in recorded_settings_paths(cfg) if not _same_path(p, settings)]
    if not removed:
        paths.append(str(Path(settings).expanduser().resolve()))
    try:
        fsutil.write_text_atomic(_record_path(cfg), json.dumps({"settings": paths}, indent=2))
    except OSError:
        pass


def find_hook_commands(cfg) -> list[tuple[Path, list[str]]]:
    """[(settings file, jevmem hook commands)] for `jevmem doctor`. Searched: the config
    directory's .claude/settings(.local).json (every jevmem hook there), and — only hooks
    that use THIS config file — ./.claude in the current directory, ~/.claude/settings.json
    and every settings file `jevmem install-hook` recorded (e.g. via --project)."""
    root = Path(cfg.root)
    own = [settings_path(root, "project"), settings_path(root, "local")]
    others = [settings_path(Path.cwd(), "project"), settings_path(Path.cwd(), "local"),
              settings_path(root, "user")] + [Path(p) for p in recorded_settings_paths(cfg)]
    out, seen = [], set()
    for p in own + others:
        try:
            key = str(Path(p).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
        if key in seen:
            continue
        seen.add(key)
        cmds = jevmem_commands(p)
        if p not in own:
            src = getattr(cfg, "source", None)
            cmds = [c for c in cmds if src is not None and _same_path(command_config(c) or "", src)]
        if cmds:
            out.append((Path(p), cmds))
    return out


def short_path(p, root) -> str:
    p = Path(p)
    for base, prefix in ((Path(root), ""), (Path.home(), "~/")):
        try:
            return prefix + str(p.resolve().relative_to(base.resolve()))
        except (ValueError, OSError, RuntimeError):
            continue
    return str(p)


def _backup(target: Path, path: Path) -> Path:
    """Copy `target` to <path>.bak-YYYYmmdd-HHMMSS (-1, -2... for runs in the same second).
    The copy is created exclusively with mode 0600 before any byte is written (settings may
    hold secrets in "env"), and keeps the original's timestamps."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 0
    while True:
        backup = path.with_name(f"{path.name}.bak-{stamp}" + (f"-{n}" if n else ""))
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, fsutil.FILE_MODE)
            break
        except FileExistsError:
            n += 1
    with os.fdopen(fd, "wb") as dst, open(target, "rb") as src:
        shutil.copyfileobj(src, dst)
    try:
        st = target.stat()
        os.utime(backup, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass
    return backup


def install(project_dir: Path, config_path: Path, scope: str = "project", dry_run: bool = False,
            uninstall: bool = False, python: str | None = None, out=print, reply_tools=None,
            capture_hooks: bool = True) -> dict:
    path = settings_path(project_dir, scope)
    current = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        current = json.loads(text) if text.strip() else {}
    if uninstall:
        new, n = remove_from_settings(current)
        action = f"removed {n}" if n else "unchanged"
    else:
        if reply_tools is None:
            from .config import DEFAULTS
            reply_tools = DEFAULTS["capture"]["reply_tools"]
        new, action = merge_settings(current, desired_hooks(config_path.resolve(), reply_tools, python,
                                                            capture_hooks))
        if not capture_hooks:
            new, n = remove_from_settings(new, keys=CAPTURE_KEYS)
            if n:
                action = "updated" if action == "unchanged" else action
                out(f"--no-capture: removed {n} jevmem capture hook(s)")
    res = {"settings": str(path), "action": action, "backup": None}
    if dry_run:
        out(json.dumps(new, indent=2, ensure_ascii=False))
        out(f"[dry-run] would write {path} ({action})")
        return res
    if action == "unchanged":
        out(f"{path}: jevmem hook already up to date")
        return res
    # A settings file that is a symlink (dotfiles) stays a link: the file it points to is
    # rewritten. The new content goes through a private (0600, O_EXCL) temp file, then gets the
    # old file's mode (or 0600 for a new file) before it replaces the old one.
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    if target.exists():
        res["backup"] = str(_backup(target, path))
    fsutil.ensure_dir(target.parent)
    keep = stat.S_IMODE(target.stat().st_mode) if target.exists() else fsutil.FILE_MODE
    fsutil.write_text_atomic(target, json.dumps(new, indent=2, ensure_ascii=False) + "\n", mode=keep)
    out(f"{path}: hook {action}" + (f" (backup: {res['backup']})" if res["backup"] else ""))
    return res


def schedule_help(config_path: Path, python: str | None = None) -> str:
    py = python or sys.executable
    return f"""The hooks run {py} -m jevmem. If that Python goes away (virtualenv recreated,
pipx reinstall, Python upgrade), run `jevmem install-hook` again.

Next: run the loop unattended (digest :04/:34 = summarize new chat + label new
chunks; daily consolidation 04:00) with launchd (macOS) or cron (Linux):

  jevmem schedule install            # prints the launchd plists / crontab block
  jevmem schedule install --write    # writes and loads them

launchd/cron don't read your shell profile: set [jev].api_key_file in jevmem.toml.
`jevmem index --dry-run` shows how many chunks and calls labeling your existing logs takes."""

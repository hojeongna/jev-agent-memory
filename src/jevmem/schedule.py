"""`jevmem schedule install|status|uninstall`: run the whole loop unattended.

Jobs (defaults):
  digest       at :04 and :34   new chat -> summary in the daily log -> new chunks labeled
  consolidate  daily at 04:00   yesterday's log -> up to 3 profile traits
(`jevmem index` is still available for manual runs and backfills.)

macOS: one launchd agent per job in ~/Library/LaunchAgents. Linux/other: one marked block
in your crontab (PATH is set per job command, so it never applies to your other entries).
Nothing is written unless `--write` is given.

launchd and cron do not read your shell profile: put the Jev key in a file and set
[jev].api_key_file, and make sure `claude`/`ollama` are on the PATH written below.

The jobs run the ABSOLUTE path of the Python that ran `jevmem schedule install`
(sys.executable). If that interpreter goes away (virtualenv recreated, pipx reinstall,
Python upgrade), run `jevmem schedule install --write` again.
"""
from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from . import fsutil

JOBS = ("digest", "consolidate")


def _hash(cfg_path: Path) -> str:
    return hashlib.sha1(str(cfg_path.resolve()).encode()).hexdigest()[:8]


def label(cfg, cfg_path: Path, job: str) -> str:
    return f"{cfg['schedule']['label_prefix']}.{_hash(cfg_path)}.{job}"


def job_args(cfg_path: Path, job: str, python: str | None = None) -> list[str]:
    py = python or sys.executable
    base = [py, "-m", "jevmem", "--config", str(cfg_path.resolve())]
    return base + (["profile", "consolidate"] if job == "consolidate" else [job])


def times(cfg, job: str) -> list[tuple[int | None, int]]:
    """[(hour or None, minute), ...]"""
    sc = cfg["schedule"]
    if job == "digest":
        return [(None, int(m)) for m in sc["digest_minutes"]]
    h, m = str(sc["consolidate_time"]).split(":")  # validated as HH:MM by config.validate
    return [(int(h), int(m))]


def _path_env() -> str:
    dirs = []
    for tool in ("claude", "ollama"):
        w = shutil.which(tool)
        if w:
            dirs.append(str(Path(w).parent))
    dirs += [str(Path(sys.executable).parent), "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return ":".join(out)


def plist(cfg, cfg_path: Path, job: str, python: str | None = None) -> str:
    log_dir = Path(cfg["data"]["dir"]) / "logs"
    intervals = []
    for h, m in times(cfg, job):
        entry = (f"<key>Hour</key><integer>{h}</integer>" if h is not None else "") + \
                f"<key>Minute</key><integer>{m}</integer>"
        intervals.append(f"      <dict>{entry}</dict>")
    args = "\n".join(f"      <string>{escape(a)}</string>" for a in job_args(cfg_path, job, python))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>{escape(label(cfg, cfg_path, job))}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>WorkingDirectory</key><string>{escape(str(cfg_path.resolve().parent))}</string>
    <key>StartCalendarInterval</key>
    <array>
{chr(10).join(intervals)}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key><string>{escape(_path_env())}</string>
    </dict>
    <key>StandardOutPath</key><string>{escape(str(log_dir / (job + '.log')))}</string>
    <key>StandardErrorPath</key><string>{escape(str(log_dir / (job + '.log')))}</string>
  </dict>
</plist>
"""


def _cron_safe(value: str, what: str) -> str:
    """A crontab entry is one line: a newline in a path would end it (and could start
    another entry), so refuse instead of writing a broken or surprising crontab."""
    if "\n" in value or "\r" in value:
        raise ValueError(f"{what} contains a line break; cron can't run it: {value!r}")
    return value


def _cron_word(value: str, what: str) -> str:
    """One shell word for the command part of a crontab line: shell-quoted, and `%` escaped
    because cron turns an unescaped % in the command into a newline."""
    return shlex.quote(_cron_safe(value, what)).replace("%", "\\%")


def cron_block(cfg, cfg_path: Path, python: str | None = None) -> str:
    """The marked crontab block. PATH is set inside each job's command (`PATH=... python ...`)
    rather than as a `PATH=` line: in a crontab an environment line applies to every entry
    BELOW it, including your own entries after this block."""
    log_dir = Path(cfg["data"]["dir"]) / "logs"
    tag = _hash(cfg_path)
    where = _cron_safe(str(cfg_path.resolve()), "config path")
    lines = [f"# >>> jevmem {tag} ({shlex.quote(where)}) >>>"]
    path_env = "PATH=" + _cron_word(_path_env(), "PATH")
    for job in JOBS:
        ts = times(cfg, job)
        if job == "consolidate":
            (h, m), = ts
            when = f"{m} {h} * * *"
        else:
            when = f"{','.join(str(m) for _, m in ts)} * * * *"
        cmd = " ".join(_cron_word(a, "job argument") for a in job_args(cfg_path, job, python))
        lines.append(f"{when} cd {_cron_word(str(cfg_path.resolve().parent), 'project dir')} && "
                     f"{path_env} {cmd} >> {_cron_word(str(log_dir / (job + '.log')), 'log path')} 2>&1")
    lines.append(f"# <<< jevmem {tag} <<<")
    return "\n".join(lines) + "\n"


def merge_crontab(existing: str, block: str) -> str:
    """Replace this config's marked block if present, else append. Other lines untouched."""
    start = block.splitlines()[0]
    tag = start.split()[3]
    out, skipping = [], False
    for line in existing.splitlines():
        if line.startswith(f"# >>> jevmem {tag} "):
            skipping = True
            continue
        if skipping:
            if line.startswith(f"# <<< jevmem {tag} "):
                skipping = False
            continue
        out.append(line)
    body = "\n".join(out).rstrip("\n")
    return (body + "\n" if body else "") + block


def remove_block(existing: str, cfg_path: Path) -> str:
    tag = _hash(cfg_path)
    out, skipping = [], False
    for line in existing.splitlines():
        if line.startswith(f"# >>> jevmem {tag} "):
            skipping = True
            continue
        if skipping:
            if line.startswith(f"# <<< jevmem {tag} "):
                skipping = False
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if out else "")


def default_platform() -> str:
    return "macos" if sys.platform == "darwin" else "linux"


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _read_crontab() -> str:
    try:
        cp = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except OSError:
        return ""
    return cp.stdout if cp.returncode == 0 else ""


def install(cfg, cfg_path: Path, platform: str | None = None, write: bool = False,
            agents_dir: Path | None = None, load: bool = True, python: str | None = None,
            crontab_io=None, out=print) -> dict:
    platform = platform or default_platform()
    py = python or sys.executable
    note = (f"jobs run {py} -m jevmem; if that Python goes away (virtualenv recreated, pipx "
            "reinstall, Python upgrade), run `jevmem schedule install --write` again")
    if write:
        fsutil.ensure_dir(Path(cfg["data"]["dir"]) / "logs")
    if platform == "macos":
        agents_dir = agents_dir or launch_agents_dir()
        files = {}
        for job in JOBS:
            path = agents_dir / f"{label(cfg, cfg_path, job)}.plist"
            files[str(path)] = plist(cfg, cfg_path, job, python)
        if not write:
            for p, text in files.items():
                out(f"--- {p}\n{text}")
            out("[dry-run] pass --write to create these launchd agents")
            return {"platform": platform, "written": False, "files": list(files)}
        agents_dir.mkdir(parents=True, exist_ok=True)
        for p, text in files.items():
            # 0600 like jevmem's other files (the plist names the config and the PATH)
            fsutil.write_text_atomic(p, text, mode=fsutil.FILE_MODE)
            out(f"wrote {p}" + (_launchctl_reload(p) if load else ""))
        out(note)
        return {"platform": platform, "written": True, "files": list(files)}
    block = cron_block(cfg, cfg_path, python)
    if not write:
        out(block)
        out("[dry-run] pass --write to add this block to your crontab")
        return {"platform": platform, "written": False, "block": block}
    read, write_fn = crontab_io or (_read_crontab, _write_crontab)
    write_fn(merge_crontab(read(), block))
    out("crontab updated")
    out(note)
    return {"platform": platform, "written": True, "block": block}


def _launchctl_reload(plist_path: str) -> str:
    """unload + load one agent: " (loaded)", or why it was not."""
    try:
        subprocess.run(["launchctl", "unload", plist_path], capture_output=True)
        cp = subprocess.run(["launchctl", "load", plist_path], capture_output=True, text=True)
    except OSError as e:
        return f" (not loaded: launchctl could not run, {fsutil.safe_reason(e)})"
    # `launchctl load` can exit 0 and only print "Load failed: ..." on stderr.
    err = (cp.stderr or "").strip()
    if cp.returncode != 0 or re.search(r"fail|error", err, re.I):
        why = err.splitlines()[0][:120] if err else f"exit {cp.returncode}"
        return f" (launchctl load failed: {why}; run `launchctl load {plist_path}` to see why)"
    return " (loaded)"


def _write_crontab(text: str) -> None:
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


def uninstall(cfg, cfg_path: Path, platform: str | None = None, write: bool = False,
              agents_dir: Path | None = None, crontab_io=None, out=print) -> dict:
    platform = platform or default_platform()
    if platform == "macos":
        agents_dir = agents_dir or launch_agents_dir()
        paths = [agents_dir / f"{label(cfg, cfg_path, j)}.plist" for j in JOBS]
        for p in paths:
            if p.exists():
                if write:
                    subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
                    p.unlink()
                out(("removed " if write else "[dry-run] would remove ") + str(p))
        return {"platform": platform, "written": write}
    read, write_fn = crontab_io or (_read_crontab, _write_crontab)
    new = remove_block(read(), cfg_path)
    if write:
        write_fn(new)
        out("crontab block removed")
    else:
        out("[dry-run] pass --write to remove the jevmem block from your crontab")
    return {"platform": platform, "written": write}


def installed(cfg, cfg_path: Path, platform: str | None = None, agents_dir: Path | None = None,
              crontab_text: str | None = None) -> dict:
    platform = platform or default_platform()
    if platform == "macos":
        agents_dir = agents_dir or launch_agents_dir()
        return {j: (agents_dir / f"{label(cfg, cfg_path, j)}.plist").exists() for j in JOBS}
    text = _read_crontab() if crontab_text is None else crontab_text
    has = f"# >>> jevmem {_hash(cfg_path)} " in text
    return {j: has for j in JOBS}

"""Private local files: new files are created 0600, new directories 0700.

jevmem's data (the chat log, recall log, summaries, databases) is personal conversation.
Only files and directories that these helpers *create* get the private modes; anything that
already exists keeps the permissions it has (you may have set them on purpose), and an
atomic rewrite keeps the old file's mode.

Also: `hook_error_log`, a small per-user log for problems the hook entries must not print
(a hook has to exit 0 silently, so a missing config file would otherwise go unnoticed).
"""
from __future__ import annotations

import contextlib
import os
import stat
import tempfile
import time
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

FILE_MODE = 0o600
DIR_MODE = 0o700
HOOK_ERROR_LOG_MAX_BYTES = 256 * 1024


def ensure_dir(path) -> Path:
    """mkdir -p; every directory created here gets 0700. Existing ones are not touched."""
    p = Path(path)
    missing = []
    q = p
    while not q.exists():
        missing.append(q)
        if q.parent == q:
            break
        q = q.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=DIR_MODE)
        except FileExistsError:
            pass
    return p


def open_append(path):
    """Open a text file for appending; a new file is created 0600."""
    p = Path(path)
    ensure_dir(p.parent)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
    return os.fdopen(fd, "a", encoding="utf-8")


def create_new(path, text: str = "") -> bool:
    """Create `path` with `text` only if it does not exist yet (0600). True if created.
    The text is written through the O_EXCL descriptor in append mode, so if another process
    appends to the brand-new file first, nothing it wrote is overwritten."""
    p = Path(path)
    ensure_dir(p.parent)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, FILE_MODE)
    except FileExistsError:
        return False
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(text)
    return True


def precreate(path) -> None:
    """Create an empty 0600 file if missing, for files another library opens itself (SQLite
    treats an empty file as a new database and gives its journal/WAL files the same mode)."""
    create_new(path)


def fsync_dir(path) -> None:
    """Flush a directory entry (a rename) to disk; a no-op where that isn't supported."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_text_atomic(path, text: str, mode: int | None = None) -> None:
    """Write via a temp file + fsync + rename (+ fsync of the directory), so a crash or power
    loss leaves either the old or the new content, never an empty file. The temp file is
    created exclusively (O_EXCL, 0600, never through a symlink). Mode: `mode` if given, else
    the existing file's mode, else 0600."""
    p = Path(path)
    ensure_dir(p.parent)
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        elif p.exists():
            os.chmod(tmp, stat.S_IMODE(p.stat().st_mode))
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    fsync_dir(p.parent)


class LockTimeout(OSError):
    """The lock was not acquired in time (another process holds it)."""


def try_lock(lock_path, timeout: float = 0.0, poll: float = 0.02):
    """Exclusive advisory lock on a separate lock file: an open fd, or None if another
    process still holds it after `timeout` seconds (0 = one non-blocking try). Release with
    os.close(fd). Where fcntl is missing, always succeeds."""
    p = Path(lock_path)
    ensure_dir(p.parent)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT, FILE_MODE)
    if fcntl is None:  # pragma: no cover - Windows
        return fd
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(poll)
        except BaseException:
            os.close(fd)
            raise


@contextlib.contextmanager
def locked(lock_path, timeout: float | None = None):
    """Exclusive advisory lock on a separate lock file (no-op where fcntl is missing).
    timeout=None waits as long as it takes; a number raises LockTimeout after that many
    seconds instead of blocking forever behind a stuck process."""
    if timeout is None:
        p = Path(lock_path)
        ensure_dir(p.parent)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT, FILE_MODE)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
    else:
        fd = try_lock(lock_path, timeout)
        if fd is None:
            raise LockTimeout(f"lock {Path(lock_path).name} busy for {timeout:g}s")
    try:
        yield
    finally:
        os.close(fd)  # closing releases the lock


# Exceptions whose messages jevmem builds itself from fixed text, paths and numbers (never
# from chat text or a server's response body): their message is kept in logs.
_KEEP_MESSAGE = {"ConfigError", "TaxonomyError", "DeadlineExceeded", "JevTimeout", "LockTimeout",
                 "HookSkip", "LLMUnavailable"}


def safe_reason(e: BaseException, limit: int = 160) -> str:
    """'ClassName: short reason' for log files. A message that may contain input text (a
    parser error quoting its input, a server error body echoing the request) is not
    written: JevError keeps only its fixed head ("Jev HTTP 502"), OSError its errno, and
    anything else just its length."""
    name = type(e).__name__
    msg = " ".join(str(e).split())
    if not msg:
        return name
    if name in _KEEP_MESSAGE:
        return f"{name}: {msg[:limit]}"
    if any(c.__name__ == "JevError" for c in type(e).__mro__):
        return f"{name}: {msg.split(':', 1)[0][:limit]}"
    if isinstance(e, OSError) and e.errno is not None:
        return f"{name}: errno {e.errno} ({os.strerror(e.errno)})"
    return f"{name}: <message hidden, {len(msg)} chars>"


def hook_error_log_path() -> Path:
    """$XDG_STATE_HOME/jevmem/hook-errors.log (default ~/.local/state/jevmem/hook-errors.log)."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base).expanduser() / "jevmem" / "hook-errors.log"


def hook_error_log(message: str) -> None:
    """Append one line; never raises. Kept small: over the size limit, the old half is dropped.
    Callers pass reasons only (config path, exception type and message), never chat text."""
    try:
        p = hook_error_log_path()
        if p.exists() and p.stat().st_size > HOOK_ERROR_LOG_MAX_BYTES:
            keep = p.read_bytes()[-HOOK_ERROR_LOG_MAX_BYTES // 2:]
            keep = keep[keep.find(b"\n") + 1:]
            write_text_atomic(p, keep.decode("utf-8", errors="replace"))
        line = " ".join(str(message).split())[:500]
        with open_append(p) as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except Exception:
        pass

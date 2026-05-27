"""
File-based lock for preventing concurrent backup runs.

Uses a PID-based lock file with defense-in-depth stale-lock detection.
If another instance is genuinely running, exits gracefully (not an error).

## Why this is more than a PID check

The naive "is a process with this PID alive?" test has a fatal gap: **PID
reuse**. If a backup dies without releasing its lock (crash, ``kill``, or a
computer restart mid-backup), the OS eventually reassigns that PID to some
other long-lived process. A plain liveness check then sees "PID alive" and
treats the lock as held *forever* -- every subsequent backup silently skips,
the session index freezes, and new sessions become invisible to ``csb
search``. This happened on a real machine (a backup's PID was recycled to
``WindowsTerminal.exe`` after a restart; backups silently skipped for two
days).

The lock therefore records process *identity*, not just a number, and
reclaims a lock when any of these fire:

  1. the PID is not alive (clean death),
  2. the PID is alive but its executable name differs from what we recorded
     (reuse by a *different* program),
  3. the PID is alive but its start-time differs from what we recorded
     (reuse by the *same* program name -- a second ``python``),
  4. the lock is older than ``STALE_LOCK_AGE_SECONDS`` (a hung backup, or a
     platform where 2/3 can't be read -- the portable backstop).

Process-identity introspection is best-effort and dependency-free (ctypes
on Windows, ``/proc`` on Linux, ``None`` elsewhere). When identity can't be
read, the age backstop still guarantees eventual recovery.
"""

import json
import os
import socket
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

LOCK_FILENAME = ".csb-backup.lock"

# A backup of a few hundred sessions takes seconds-to-minutes; even a large
# vault with FTS5 work is low-tens-of-minutes. 30 min is ~10-100x headroom
# over a normal run yet recovers a truly stuck lock within a humane window.
# Identity checks (2/3) reclaim reused PIDs INSTANTLY; this age backstop only
# fires for hung-but-alive processes or platforms without introspection.
STALE_LOCK_AGE_SECONDS = 1800

# Two start-times within this many seconds are treated as the same process
# incarnation (guards against float round-trip / measurement jitter). A
# reused PID after a crash/restart starts far more than this later, so the
# tolerance never masks real reuse in practice.
_START_TIME_TOLERANCE_SECONDS = 1.0


# ── Process introspection (best-effort, dependency-free) ──────────────


def _pid_alive(pid: int) -> bool:
    """Check if a process with the given PID currently exists."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            handle = k32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            if handle:
                k32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, PermissionError):
        return False
    except Exception:
        return False


def _win_open_query_handle(pid: int):
    """Open a PROCESS_QUERY_LIMITED_INFORMATION handle (Win64-safe types).

    Returns the handle or None. Caller must CloseHandle. Windows-only.
    """
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    return handle or None


def _proc_name(pid: int) -> "str | None":
    """Best-effort executable basename for ``pid`` (e.g. ``python.exe``).

    Windows: ``QueryFullProcessImageNameW``. Linux: ``/proc/<pid>/comm``.
    Returns None on other platforms, on failure, or if the process is gone.
    Self-identity uses the SAME path (``_proc_name(os.getpid())``) so the
    recorded and live names are always produced comparably.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            handle = _win_open_query_handle(pid)
            if not handle:
                return None
            try:
                size = wintypes.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
                k32.QueryFullProcessImageNameW.argtypes = [
                    wintypes.HANDLE, wintypes.DWORD,
                    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
                ]
                ok = k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
                if not ok:
                    return None
                full = buf.value
                return os.path.basename(full) if full else None
            finally:
                k32.CloseHandle(handle)
        else:
            try:
                with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as f:
                    return f.read().strip() or None
            except OSError:
                return None
    except Exception:
        return None


def _proc_start_time(pid: int) -> "float | None":
    """Best-effort process start-time as Unix epoch seconds.

    Windows: ``GetProcessTimes`` creation FILETIME. Linux: ``ctime`` of
    ``/proc/<pid>``. None on other platforms or failure. Used to detect PID
    reuse by comparing the recorded start-time to the live one for the same
    PID (different incarnations have different start-times).
    """
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            handle = _win_open_query_handle(pid)
            if not handle:
                return None
            try:
                creation = wintypes.FILETIME()
                exit_t = wintypes.FILETIME()
                kernel_t = wintypes.FILETIME()
                user_t = wintypes.FILETIME()
                k32.GetProcessTimes.restype = wintypes.BOOL
                ok = k32.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(exit_t),
                    ctypes.byref(kernel_t), ctypes.byref(user_t),
                )
                if not ok:
                    return None
                ft = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                if ft == 0:
                    return None
                # FILETIME is 100-ns intervals since 1601-01-01 UTC.
                EPOCH_DIFF_100NS = 116444736000000000  # 1601 -> 1970
                return (ft - EPOCH_DIFF_100NS) / 1e7
            finally:
                k32.CloseHandle(handle)
        else:
            try:
                return os.stat(f"/proc/{pid}").st_ctime
            except OSError:
                return None
    except Exception:
        return None


# ── Lock data (read / write / age) ────────────────────────────────────


def _self_identity() -> dict:
    """Identity record for the current process, written into the lock."""
    pid = os.getpid()
    return {
        "pid": pid,
        "acquired_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proc_name": _proc_name(pid),
        "start_time": _proc_start_time(pid),
        "host": socket.gethostname() or None,
    }


def _read_lock(path: Path) -> "dict | None":
    """Parse a lock file into a dict, or None if absent / corrupted.

    The lock is a JSON object with an integer ``pid``. Anything else --
    missing file, empty, non-JSON garbage, a bare integer, or a JSON
    value that isn't an object with an int pid -- returns None, which the
    caller treats as "corrupted or foreign, safe to take over."
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("pid"), int):
        return data
    return None


def _parse_iso(s: "str | None") -> "float | None":
    """Parse an ISO-8601 UTC timestamp to epoch seconds (tolerant of 'Z')."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, AttributeError):
        return None


def _lock_age(acquired_at: "str | None", now: float,
              lock_mtime: "float | None") -> "float | None":
    """Age of the lock in seconds. Prefers the in-lock ``acquired_at``
    timestamp; falls back to the file mtime (legacy locks). Returns None
    if neither is available. Negative ages (clock skew) clamp to 0.0
    (treated as fresh -- never reap on a nonsensical age)."""
    ts = _parse_iso(acquired_at)
    if ts is None:
        ts = lock_mtime
    if ts is None:
        return None
    age = now - ts
    return 0.0 if age < 0 else age


# ── Staleness decision ────────────────────────────────────────────────


def _is_stale(lock: dict, now: float,
              lock_mtime: "float | None") -> "tuple[bool, str]":
    """Decide whether a lock is stale (safe to reclaim).

    Returns ``(stale, reason)``. Reclaim if ANY signal fires; skip only
    when the lock looks live by every available signal AND is younger
    than the age threshold. See module docstring for the four checks.
    """
    pid = lock.get("pid")
    if not isinstance(pid, int):
        return True, "no PID in lock"

    if not _pid_alive(pid):
        return True, "dead PID"

    # PID is alive -- defeat reuse via recorded identity.
    recorded_name = lock.get("proc_name")
    if recorded_name:
        live_name = _proc_name(pid)
        if live_name and live_name != recorded_name:
            return True, f"PID reused by {live_name}"

    recorded_start = lock.get("start_time")
    if recorded_start is not None:
        live_start = _proc_start_time(pid)
        if (live_start is not None
                and abs(live_start - recorded_start) > _START_TIME_TOLERANCE_SECONDS):
            return True, "PID reused (start-time mismatch)"

    # Backstop: hung-but-alive, or a platform where identity is unreadable.
    age = _lock_age(lock.get("acquired_at"), now, lock_mtime)
    if age is not None and age > STALE_LOCK_AGE_SECONDS:
        return True, (f"lock age {age / 3600:.1f}h exceeds "
                      f"{STALE_LOCK_AGE_SECONDS / 3600:.1f}h threshold")

    return False, ""


def _emit_skip(lock: dict, now: float, lock_mtime: "float | None") -> None:
    """Print an informative 'already running' skip line (not silent)."""
    pid = lock.get("pid")
    age = _lock_age(lock.get("acquired_at"), now, lock_mtime)
    if age is None:
        when = ""
    elif age < 60:
        when = ", started <1m ago"
    else:
        when = f", started {age / 60:.0f}m ago"
    print(f"Another csb backup is running (PID {pid}{when}). Skipping.",
          file=sys.stderr)


# ── Public API ─────────────────────────────────────────────────────────


@contextmanager
def backup_lock(claude_dir: str, *, quiet: bool = False):
    """
    Context manager for the backup lock.

    Usage::

        with backup_lock(claude_dir, quiet=quiet) as acquired:
            if not acquired:
                return  # another instance is running; message already emitted
            # ... do backup work ...

    Yields True if the lock was acquired (including after reclaiming a
    stale lock), False if another instance is genuinely running. The
    skip / reclaim messages are emitted here (suppressed when ``quiet``)
    so callers don't need to print their own.
    """
    lock_path = Path(claude_dir) / LOCK_FILENAME
    my_pid = os.getpid()
    now = time.time()

    existing = _read_lock(lock_path)
    if existing is not None:
        try:
            lock_mtime = lock_path.stat().st_mtime
        except OSError:
            lock_mtime = None
        stale, reason = _is_stale(existing, now, lock_mtime)
        if not stale:
            if not quiet:
                _emit_skip(existing, now, lock_mtime)
            yield False
            return
        # Reclaiming a stale lock -- announce it. The original failure mode
        # was invisible; this makes "a prior backup was interrupted" legible.
        if not quiet:
            print(f"csb: reclaimed stale backup lock (PID {existing.get('pid')}, "
                  f"{reason}) -- a prior backup was interrupted", file=sys.stderr)

    # Acquire: write our identity.
    try:
        lock_path.write_text(json.dumps(_self_identity()), encoding="utf-8")
    except OSError:
        # Can't write the lock. Proceed UNLOCKED rather than skip -- silently
        # not backing up is exactly the failure we're eliminating. Accept the
        # small concurrency risk.
        if not quiet:
            print("csb: warning -- could not write backup lock; proceeding "
                  "without it", file=sys.stderr)
        yield True
        return

    # Readback-verify ownership: if two runs raced to take over a stale lock,
    # the last writer wins the file and everyone else sees a foreign PID here.
    verify = _read_lock(lock_path)
    if verify is None or verify.get("pid") != my_pid:
        if not quiet:
            print("Another csb backup is running. Skipping.", file=sys.stderr)
        yield False
        return

    try:
        yield True
    finally:
        # Release only if we still own it.
        try:
            current = _read_lock(lock_path)
            if current is not None and current.get("pid") == my_pid:
                lock_path.unlink()
        except OSError:
            pass

"""
File-based lock for preventing concurrent backup runs.

Uses a PID-based lock file with stale lock detection.
If another instance is running, exits gracefully (not an error).
"""

import os
import sys
from contextlib import contextmanager
from pathlib import Path

LOCK_FILENAME = ".csb-backup.lock"


def _pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, PermissionError):
        return False


@contextmanager
def backup_lock(claude_dir: str):
    """
    Context manager for backup lock.

    Usage:
        with backup_lock(claude_dir) as acquired:
            if not acquired:
                return  # Another instance is running
            # ... do backup work ...

    Yields True if lock was acquired, False if another instance is running.
    """
    lock_path = Path(claude_dir) / LOCK_FILENAME
    my_pid = os.getpid()

    # Check for existing lock
    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip())
            if _pid_alive(existing_pid):
                # Another instance is genuinely running
                yield False
                return
            # Stale lock -- process is dead, we can take over
        except (ValueError, OSError):
            pass  # Corrupted lock file, take over

    # Acquire lock
    try:
        lock_path.write_text(str(my_pid), encoding="utf-8")
        yield True
    finally:
        # Release lock (only if we own it)
        try:
            if lock_path.exists():
                current_pid = int(lock_path.read_text().strip())
                if current_pid == my_pid:
                    lock_path.unlink()
        except (ValueError, OSError):
            pass  # Best effort cleanup

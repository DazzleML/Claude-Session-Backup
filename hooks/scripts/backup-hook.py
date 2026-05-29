#!/usr/bin/env python3
"""
Claude Code hook script for claude-session-backup.

Fires on SessionStart, PreCompact, and SessionEnd (see hooks.json) and runs
`csb backup --quiet` in the BACKGROUND, so the session is never blocked --
the hook returns immediately after spawning.

Why background + detached (v0.3.7 -> v0.3.8): SessionEnd runs as the exiting
session tears down. Claude Code hard-kills the session's process-tree
descendants by PID lineage, so a plain background child is killed before it
finishes (leaving a stale lock + an un-indexed session). v0.3.8 spawns the
backup decoupled (Windows: CREATE_NO_WINDOW|CREATE_NEW_PROCESS_GROUP; POSIX:
start_new_session) and returns immediately -- so this hook process is gone
before teardown walks the tree, the backup is orphaned (not found by the
kill), and CREATE_NEW_PROCESS_GROUP shields it from the group Ctrl-C/Break, so
it completes on its own. CREATE_NO_WINDOW gives it a HIDDEN console that its
git children inherit, so nothing flashes a window on Windows (DETACHED_PROCESS
was wrong: a consoleless csb makes each git child pop its own console).
Verified empirically: private/snippets/sessionend_escape_probe.py + a real
backup that finished 12.7s AFTER teardown (rc=0). Backing up on
SessionStart/PreCompact (both fire while the session is alive) stays as the
catch-up net, and the v0.3.6 lock reclaim covers OS-shutdown/logout kills.
"Backups just happen, nothing lost."

SessionStart fires after compaction too (source="compact"); we SKIP that one
because PreCompact already captured the pre-compaction transcript.

Cross-platform PATH resolution:
  1. Try `csb` directly (if on PATH)
  2. Fall back to `python -m claude_session_backup` (works if pip-installed)

Output (and a one-line note per fire) goes to ~/.claude/csb-logs/backup-<event>.log
so a background run is never silent.
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


def find_csb():
    """Find the csb executable, with fallback to module invocation."""
    csb_path = shutil.which("csb")
    if csb_path:
        return [csb_path]

    # Fallback: invoke as Python module using the same Python that runs this script
    return [sys.executable, "-m", "claude_session_backup"]


def _read_hook_input():
    """Parse the hook JSON from stdin -> (hook_event_name, source).

    Tolerates a TTY (manual invocation), empty stdin, or non-JSON garbage by
    returning ("", "") -- which `_should_run_backup` treats as "run". The TTY
    guard prevents a blocking read when the script is run by hand.
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return "", ""
        raw = sys.stdin.buffer.read().decode("utf-8")
    except Exception:
        return "", ""
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return data.get("hook_event_name") or "", data.get("source") or ""


def _detach_kwargs():
    """Popen kwargs that decouple the spawned backup from this process tree
    AND keep it (and everything it spawns) window-free on Windows.

    Survival: the backup must outlive Claude Code's SessionEnd teardown, which
    kills the session's descendants. Because this hook returns immediately, the
    child is orphaned before the kill walks the tree -- and CREATE_NEW_PROCESS_
    GROUP (0x200) isolates it from the group-wide Ctrl-C/Break signal sent at
    teardown. (Survival verified empirically:
    private/snippets/sessionend_escape_probe.py -- a real backup completed 12.7s
    AFTER teardown, rc=0.)

    No window: CREATE_NO_WINDOW (0x08000000) -- NOT DETACHED_PROCESS. A console
    app run DETACHED has *no* console, so every console child IT spawns (csb ->
    git, git, git ...) gets its own fresh console -> popups. CREATE_NO_WINDOW
    gives csb one HIDDEN console that all its git children INHERIT -> zero
    windows anywhere. (DETACHED_PROCESS would be ignored if OR'd with
    CREATE_NO_WINDOW, so we use only the latter for console control.)

    POSIX: start_new_session=True (new session/group); no window concept.
    """
    if sys.platform == "win32":
        return {"creationflags": 0x08000000 | 0x00000200}
    return {"start_new_session": True}


def _should_run_backup(hook_event_name, source):
    """Decide whether to back up for this hook fire.

    Skip ONLY the post-compaction SessionStart (source="compact") -- PreCompact
    already captured the pre-compaction transcript, so backing up again is
    redundant and would contend for the lock right at compaction time.
    Everything else runs: SessionStart startup/resume/clear (resume covers
    /fork + /rewind), PreCompact, SessionEnd, and manual invocation (empty).
    """
    if hook_event_name == "SessionStart" and source == "compact":
        return False
    return True


def main():
    hook_event_name, source = _read_hook_input()

    log_dir = Path.home() / ".claude" / "csb-logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_dir = None

    run_log = log_dir / f"backup-{hook_event_name or 'manual'}.log" if log_dir else None

    def note(msg):
        if run_log is None:
            return
        try:
            with run_log.open("a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except OSError:
            pass

    if not _should_run_backup(hook_event_name, source):
        note(f"skip: {hook_event_name} source={source} (PreCompact already covered compaction)")
        return

    cmd = find_csb() + ["--quiet", "backup"]
    note(f"start background backup: event={hook_event_name or 'manual'} source={source or '-'}")

    # Redirect the background backup's output to the log (never the session's
    # console). DEVNULL stdin so it can't read from the hook's stdin.
    try:
        out = run_log.open("a", encoding="utf-8") if run_log is not None else subprocess.DEVNULL
    except OSError:
        out = subprocess.DEVNULL

    try:
        # Fire-and-don't-wait + DETACH: return immediately (session never
        # blocked) and spawn the backup decoupled from this process tree so it
        # SURVIVES SessionEnd teardown -- the root of the un-indexed-session
        # bug -- and never flashes a console window on Windows. At
        # SessionStart/PreCompact the backup would complete anyway; detaching
        # makes every trigger uniformly durable.
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            **_detach_kwargs(),
        )
    except FileNotFoundError:
        note("csb not found. Install with: pip install claude-session-backup")
    except Exception as e:  # noqa: BLE001 -- a hook must never raise into Claude Code
        note(f"backup spawn error: {e!r}")


if __name__ == "__main__":
    main()

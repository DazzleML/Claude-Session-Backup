#!/usr/bin/env python3
"""
Claude Code hook script for claude-session-backup.

Fires on SessionStart, PreCompact, and SessionEnd (see hooks.json) and runs
`csb backup --quiet` in the BACKGROUND, so the session is never blocked --
the hook returns immediately after spawning.

Why background (v0.3.7): SessionEnd runs as the exiting session tears down and
is hard-killed before it finishes (leaving a stale lock + an un-indexed
session). The fix is to also back up on SessionStart (which Claude Code fires
on startup/resume/clear AND on /fork + /rewind, via source="resume") and on
PreCompact -- both run while the session is alive, so a fire-and-don't-wait
backup completes. SessionEnd stays best-effort; if teardown kills it, the next
SessionStart reclaims the lock (v0.3.6) and catches up. "Backups just happen,
nothing lost."

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
        # Fire-and-don't-wait: return immediately so the session is never
        # blocked. No detach flags -- at SessionStart/PreCompact the session
        # stays alive and the child completes; at SessionEnd it's best-effort.
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
        )
    except FileNotFoundError:
        note("csb not found. Install with: pip install claude-session-backup")
    except Exception as e:  # noqa: BLE001 -- a hook must never raise into Claude Code
        note(f"backup spawn error: {e!r}")


if __name__ == "__main__":
    main()

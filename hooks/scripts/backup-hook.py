#!/usr/bin/env python3
"""
Claude Code hook script for claude-session-backup.

Fires on SessionStart, PreCompact, and SessionEnd (see hooks.json) and runs
`csb backup --quiet` in the BACKGROUND, so the session is never blocked --
the hook returns immediately after spawning.

Durable spawn (v0.3.8): SessionEnd runs as the exiting session tears down, and
Claude Code hard-kills the session's process-tree descendants. The backup is
spawned decoupled (Windows: CREATE_NO_WINDOW|CREATE_NEW_PROCESS_GROUP; POSIX:
start_new_session) and the hook returns immediately, so the backup is orphaned
before the kill walks the tree, CREATE_NEW_PROCESS_GROUP shields it from the
group Ctrl-C/Break, and it completes on its own. CREATE_NO_WINDOW gives it a
HIDDEN console that its git children inherit, so nothing flashes a window
(DETACHED_PROCESS was wrong: a consoleless csb makes each git child pop its
own console). Verified: sessionend_escape_probe.py + a real
backup that finished 12.7s AFTER teardown (rc=0).

Per-event behavior:
  PreCompact / SessionEnd  -> always back up (detached). These are the durable
                              triggers; SessionEnd now reliably completes.
  SessionStart (v0.3.9)    -> NOT an unconditional backup. It is a HEALTH
                              CHECK: run `csb _check` (internal) to detect a
                              prior session with un-backed-up changes (an
                              unclean shutdown where SessionEnd never ran).
                              Only on a detected gap does it WARN (a
                              systemMessage Claude Code shows the user) AND run
                              a recovery backup. The clean path does nothing.
                              This surfaces the bug instead of silently
                              re-backing-up to mask it.
  SessionStart source=compact -> SKIP (PreCompact already captured the
                              pre-compaction transcript).

The v0.3.6 lock reclaim covers OS-shutdown/logout kills.
"Backups just happen, nothing lost -- and if one ever is, you're told."

Cross-platform PATH resolution:
  1. Try `csb` directly (if on PATH)
  2. Fall back to `python -m claude_session_backup` (works if pip-installed)

Output (and a one-line note per fire) goes to ~/.claude/csb-logs/backup-<event>.log
so a background run is never silent.
"""

import json
import os
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
    """Parse the hook JSON from stdin -> (hook_event_name, source, session_id).

    Tolerates a TTY (manual invocation), empty stdin, or non-JSON garbage by
    returning ("", "", "") -- which `_should_run_backup` treats as "run". The
    TTY guard prevents a blocking read when the script is run by hand.
    session_id lets the SessionStart check EXCLUDE the currently-active session
    (whose JSONL is mid-write and would always look newer than the index).
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return "", "", ""
        raw = sys.stdin.buffer.read().decode("utf-8")
    except Exception:
        return "", "", ""
    raw = (raw or "").strip()
    if not raw:
        return "", "", ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return "", "", ""
    if not isinstance(data, dict):
        return "", "", ""
    return (
        data.get("hook_event_name") or "",
        data.get("source") or "",
        data.get("session_id") or "",
    )


def _detach_kwargs():
    """Popen kwargs that decouple the spawned backup from this process tree
    AND keep it (and everything it spawns) window-free on Windows.

    Survival: the backup must outlive Claude Code's SessionEnd teardown, which
    kills the session's descendants. Because this hook returns immediately, the
    child is orphaned before the kill walks the tree -- and CREATE_NEW_PROCESS_
    GROUP (0x200) isolates it from the group-wide Ctrl-C/Break signal sent at
    teardown. (Survival verified empirically:
    sessionend_escape_probe.py -- a real backup completed 12.7s
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
    """Whether this hook fire is actionable at all.

    Skips ONLY the post-compaction SessionStart (source="compact") -- PreCompact
    already captured the pre-compaction transcript, so acting again is redundant
    and would contend for the lock right at compaction time. Everything else is
    actionable: SessionStart startup/resume/clear (resume covers /fork +
    /rewind, where it runs the health check), PreCompact, SessionEnd, and manual
    invocation (empty). NOTE: "actionable" != "always backs up" -- SessionStart
    only backs up when the health check finds a gap (see main()).
    """
    if hook_event_name == "SessionStart" and source == "compact":
        return False
    return True


# `csb check` exit code meaning "un-backed-up session(s) found" (commands.py
# CHECK_GAP_EXIT). Distinct from 1 (error) so we can tell "gap" from "broke".
_CHECK_GAP_EXIT = 10


def _run_check(session_id):
    """Run `csb _check` (internal) to detect a prior session with un-backed-up
    changes.

    Returns (status, detail): status is "clean" | "gap" | "error"; detail is
    the human summary (for "gap") or an error string. Synchronous with a short
    timeout -- it's a fast index/mtime comparison, not a backup -- and its
    output is captured (not inherited) so only the warning we emit reaches
    Claude Code. The current session is excluded (its JSONL is mid-write).
    """
    cmd = find_csb() + ["_check", "--quiet"]
    if session_id:
        cmd += ["--exclude", session_id]
    try:
        r = subprocess.run(
            cmd, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:  # noqa: BLE001 -- never raise into Claude Code
        return "error", repr(e)
    if r.returncode == 0:
        return "clean", ""
    if r.returncode == _CHECK_GAP_EXIT:
        return "gap", (r.stdout or "").strip()
    return "error", (r.stderr or r.stdout or f"rc={r.returncode}").strip()


def _emit_sessionstart_warning(detail):
    """Print a SessionStart hook JSON payload to stdout so Claude Code surfaces
    a systemMessage to the user. run-hook.mjs uses stdio:inherit, so this
    stdout reaches Claude Code; the hook prints nothing else to stdout, so the
    payload isn't corrupted. Best-effort -- never raise into Claude Code.
    """
    msg = detail or ("claude-session-backup: a prior session was not backed up "
                     "(unclean shutdown); recovering now.")
    try:
        sys.stdout.write(json.dumps({"systemMessage": msg}) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _spawn_backup(out, note):
    """Spawn `csb --quiet backup` detached + window-free, return immediately.

    Fire-and-don't-wait: the backup is decoupled from this process tree so it
    SURVIVES SessionEnd teardown and never flashes a console window (see
    _detach_kwargs). Returns True if the spawn was issued.
    """
    cmd = find_csb() + ["--quiet", "backup"]
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            **_detach_kwargs(),
        )
        return True
    except FileNotFoundError:
        note("csb not found. Install with: pip install claude-session-backup")
    except Exception as e:  # noqa: BLE001 -- a hook must never raise into Claude Code
        note(f"backup spawn error: {e!r}")
    return False


def _claude_dir() -> Path:
    """The Claude data directory, honoring relocation (#46).

    Env-only on purpose: this hook stays dependency-free (it can't import
    the csb package), and relocated setups run Claude Code with CLAUDE_DIR
    or CLAUDE_CONFIG_DIR set -- the same precedence csb's CLI uses. The
    "csb-logs" name mirrors ClaudePaths.CSB_LOGS in pathkit.py.
    """
    env = os.environ.get("CLAUDE_DIR") or os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def main():
    hook_event_name, source, session_id = _read_hook_input()

    log_dir = _claude_dir() / "csb-logs"
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

    def open_out():
        # Backup output -> the log (never the session's console).
        try:
            return run_log.open("a", encoding="utf-8") if run_log is not None else subprocess.DEVNULL
        except OSError:
            return subprocess.DEVNULL

    if not _should_run_backup(hook_event_name, source):
        note(f"skip: {hook_event_name} source={source} (PreCompact already covered compaction)")
        return

    # SessionStart is a HEALTH CHECK, not an unconditional backup (v0.3.9):
    # detect a prior un-backed-up session; only then warn + recover. The clean
    # path does nothing -- SessionEnd (v0.3.8) is the durable backup trigger.
    if hook_event_name == "SessionStart":
        status, detail = _run_check(session_id)
        if status == "clean":
            note(f"SessionStart source={source}: all sessions backed up -- no action")
            return
        if status == "gap":
            note(f"SessionStart source={source}: GAP detected -> warn user + recover backup")
            _emit_sessionstart_warning(detail)
            _spawn_backup(open_out(), note)
            return
        # status == "error": the detector itself failed. Back up defensively
        # (don't risk data loss because the check broke) but DON'T warn the
        # user -- we have no confirmed gap to report.
        note(f"SessionStart source={source}: check failed ({detail!r}) -> defensive backup")
        _spawn_backup(open_out(), note)
        return

    # PreCompact / SessionEnd / manual: always back up (detached).
    note(f"start background backup: event={hook_event_name or 'manual'} source={source or '-'}")
    _spawn_backup(open_out(), note)


if __name__ == "__main__":
    main()

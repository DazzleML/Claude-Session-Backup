#!/usr/bin/env python3
"""Probe: can csb tell whether a session is LIVE right now?

Read-only. Compares three candidate liveness signals against reality:

  1. PROCESS TABLE  -- a running `claude --resume <uuid>` names its session
                       in argv. Authoritative when present.
  2. BARE CLI PROC  -- a running `claude` with no --resume is a live session
                       whose UUID is NOT in argv (fresh, not resumed).
  3. STATE MTIME    -- ~/.claude/session-states/<uuid>.json mtime, i.e. the
                       activity proxy. Cheap, cross-platform, and WRONG for
                       idle-but-open sessions.

The point of the probe is signal (3)'s false-negative rate: how many sessions
are provably live (1) yet look dead by mtime?

Usage:
    python tests/one-offs/probe_session_liveness.py [--stale-minutes N]

Written for the #66 design (csb resume liveness warning). Windows first --
POSIX branch included but only smoke-checked on this box.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)

# Claude Desktop is a different product that also ships a "Claude.exe".
# Its processes carry Electron switches and live under WindowsApps.
DESKTOP_MARKERS = ("--type=", "WindowsApps", "crashpad", "--user-data-dir")


def _windows_procs():
    """[(pid, name, cmdline)] via CIM. Empty list if the query fails."""
    ps = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        ).stdout
        rows = json.loads(out) if out.strip() else []
    except Exception as exc:  # noqa: BLE001 - probe: report, never raise
        print(f"  ! process query failed: {exc}", file=sys.stderr)
        return []
    if isinstance(rows, dict):
        rows = [rows]
    return [
        (r.get("ProcessId"), r.get("Name") or "", r.get("CommandLine") or "")
        for r in rows
    ]


def _posix_procs():
    """[(pid, name, cmdline)] via ps.

    NOTE the flags: `-axo`, never `-eo`. On Linux `-e` means "every process",
    but on **FreeBSD `-e` means "show the environment"** -- so `ps -eo ...`
    silently does the wrong thing there. `-axo` means all-processes
    (with and without a tty) on Linux, macOS, and the BSDs alike.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,args="], capture_output=True,
            text=True, timeout=30,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        print(f"  ! ps failed: {exc}", file=sys.stderr)
        return []
    procs = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            pid, args = parts
            procs.append((pid, args.split()[0] if args else "", args))
    return procs


def _psutil_procs():
    """[(pid, name, cmdline)] via psutil -- uniform across Win/Linux/macOS/BSD.

    Preferred when available: no subprocess spawn (the Windows CIM query is
    the slow path), and per-process AccessDenied degrades to a skip rather
    than losing the whole enumeration.
    """
    try:
        import psutil
    except ImportError:
        return None
    procs = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(p.info.get("cmdline") or [])
            procs.append((p.info["pid"], p.info.get("name") or "", cmdline))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs


def is_claude_cli(name: str, cmdline: str) -> bool:
    """True for the Claude Code CLI; False for Claude Desktop and everything else."""
    if not cmdline:
        return False
    if any(m in cmdline for m in DESKTOP_MARKERS):
        return False
    exe = cmdline.split()[0].strip('"').lower()
    return exe.endswith("claude") or exe.endswith("claude.exe")


def _resume_identifier(cmdline: str):
    """Token following --resume / -r, or None.

    Claude Code accepts EITHER a UUID or a session NAME here -- observed
    live on this box:
        claude --resume 37f09835-4c0a-4bdb-9049-b379ea6b5771
        claude --resume DAZZLECMD__git-actual-cmd
    So a UUID regex alone under-detects; the name form needs csb's own
    name->uuid resolution to be matched against the index.
    """
    toks = cmdline.split()
    for i, tok in enumerate(toks):
        if tok in ("--resume", "-r") and i + 1 < len(toks):
            nxt = toks[i + 1]
            if not nxt.startswith("-"):
                return nxt.strip('"')
    return None


def scan_processes():
    """-> (by_uuid: {uuid: pid}, by_name: {name: pid}, bare: [(pid, cmdline)])"""
    procs = _psutil_procs()
    if procs is None:  # no psutil -- fall back to the platform-native path
        procs = _windows_procs() if platform.system() == "Windows" else _posix_procs()
    by_uuid, by_name, bare = {}, {}, []
    for pid, name, cmdline in procs:
        if not is_claude_cli(name, cmdline):
            continue
        ident = _resume_identifier(cmdline)
        if ident is None:
            bare.append((pid, cmdline.strip()))
        elif UUID_RE.fullmatch(ident):
            by_uuid[ident.lower()] = pid
        else:
            by_name[ident] = pid
    return by_uuid, by_name, bare


def scan_states(claude_dir: Path):
    """-> {uuid: (mtime_utc, name)} from session-states/*.json."""
    states = {}
    d = claude_dir / "session-states"
    if not d.is_dir():
        return states
    for f in d.glob("*.json"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        name = ""
        try:
            name = (json.loads(f.read_text(encoding="utf-8")) or {}).get(
                "current_name", ""
            )
        except Exception:  # noqa: BLE001 - a half-written state file is expected
            pass
        states[f.stem.lower()] = (mtime, name)
    return states


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-minutes", type=int, default=30,
                    help="mtime older than this = 'looks dead' (default 30)")
    ap.add_argument("--claude-dir", default=str(Path.home() / ".claude"))
    args = ap.parse_args()

    claude_dir = Path(args.claude_dir)
    live, by_name, bare = scan_processes()
    states = scan_states(claude_dir)
    now = datetime.now(timezone.utc)

    # Resolve name-resumed processes against the state files' current_name,
    # which is what csb's own resolver would do against the index.
    name_to_uuid = {n: u for u, (_, n) in states.items() if n}
    unresolved_names = {}
    for nm, pid in by_name.items():
        uuid = name_to_uuid.get(nm)
        if uuid:
            live[uuid] = pid
        else:
            unresolved_names[nm] = pid

    print(f"Platform : {platform.system()}")
    print(f"Claude dir: {claude_dir}")
    print(f"session-states files : {len(states)}")
    print(f"live resumed sessions (uuid or name in argv) : {len(live)}")
    print(f"  of which resumed BY NAME, resolved         : {len(by_name) - len(unresolved_names)}")
    print(f"unresolved name-resumes                      : {len(unresolved_names)}")
    print(f"bare `claude` (no identifier in argv)        : {len(bare)}\n")

    print("PROVABLY LIVE (process table is authoritative)")
    false_neg = 0
    for uuid, pid in sorted(live.items()):
        mtime, name = states.get(uuid, (None, ""))
        if mtime is None:
            verdict, age = "no state file", "-"
        else:
            mins = (now - mtime).total_seconds() / 60
            age = f"{mins:7.1f} min"
            looks_dead = mins > args.stale_minutes
            verdict = "MTIME SAYS DEAD  <-- false negative" if looks_dead else "mtime agrees"
            false_neg += looks_dead
        print(f"  {uuid[:8]}  pid {pid:<7} age {age}  {verdict}   {name}")

    if bare:
        print("\nLIVE BUT UNIDENTIFIED (fresh session; UUID absent from argv)")
        for pid, cmdline in bare:
            print(f"  pid {pid:<7} {cmdline[:70]}")

    print("\n--- verdict ---")
    print(f"  process-table detections : {len(live)}")
    print(f"  mtime false negatives    : {false_neg}"
          f"  (live sessions a {args.stale_minutes}-min mtime rule would miss)")
    print(f"  undetectable by argv     : {len(bare) + len(unresolved_names)}"
          "  (needs correlation, see DWP)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

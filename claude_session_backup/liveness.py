"""Process-table liveness: which Claude Code sessions have a live process.

Productionized from ``tests/one-offs/probe_session_liveness.py``, whose
measurements drive the design (#64, #67):

* A live *resumed* session names its session in its own argv
  (``claude --resume <uuid-or-name>``) -- observed fact, the strongest
  available signal, no storage or cooperation required.
* A *fresh* session (bare ``claude``) is unattributable from argv -- the
  registry covers that blind spot; this module covers the registry's
  (an entry whose session crashed this boot has no process).
* Activity mtime is NOT a liveness signal: measured 25% false-negative
  on real data (a live session idle ~64h). Nothing here infers from
  timestamps.
* Claude Desktop also ships a ``Claude.exe`` -- matching keys on
  command-line SHAPE (Electron/WindowsApps markers), never process name.
* The ``--resume`` identifier may be a UUID **or a session name** --
  callers must match names through the index, not a UUID regex.

Failure posture: liveness is advisory everywhere it is used. Every path
degrades to "no verification available" rather than raising -- a broken
process table must never break `csb set show current`, and a scan is
time-boxed so a slow WMI service cannot hang a roster.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)

# Claude Desktop is a different product that also ships a "Claude.exe";
# its processes carry Electron switches and live under WindowsApps.
DESKTOP_MARKERS = ("--type=", "WindowsApps", "crashpad", "--user-data-dir")

_SCAN_TIMEOUT = 20.0


@dataclass(frozen=True)
class LiveScan:
    """One pass over the process table.

    ``by_uuid``: lowercased session UUID -> pid, for ``--resume <uuid>``.
    ``by_name``: verbatim identifier -> pid, for ``--resume <name>`` --
    resolve through the index before matching.
    ``bare_pids``: Claude CLI processes with no ``--resume`` identifier
    (fresh sessions; provably live, not attributable).
    ``ok``: False when the process table could not be read at all --
    callers must then say "unverified", never "not running".
    """

    by_uuid: dict = field(default_factory=dict)
    by_name: dict = field(default_factory=dict)
    bare_pids: tuple = ()
    ok: bool = True


def _enumerate_processes() -> Optional[list[tuple]]:
    """[(pid, name, cmdline)] or None on failure. THE mock seam for tests."""
    try:
        if sys.platform == "win32":
            ps = (
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,Name,CommandLine | "
                "ConvertTo-Json -Compress"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=_SCAN_TIMEOUT,
                check=False,
            ).stdout
            rows = json.loads(out) if out.strip() else []
            if isinstance(rows, dict):
                rows = [rows]
            return [
                (r.get("ProcessId"), r.get("Name") or "",
                 r.get("CommandLine") or "")
                for r in rows
            ]
        # POSIX: `ps -axo`, never `-eo` -- on FreeBSD `-e` means "show the
        # environment", so `-eo` silently does the wrong thing there.
        out = subprocess.run(
            ["ps", "-axo", "pid=,args="],
            capture_output=True, text=True, timeout=_SCAN_TIMEOUT,
            check=False,
        ).stdout
        procs = []
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                pid, args = parts
                procs.append((pid, args.split()[0] if args else "", args))
        return procs
    except Exception:  # noqa: BLE001 -- advisory; degrade, never raise
        return None


def is_claude_cli(cmdline: str) -> bool:
    """True for the Claude Code CLI; False for Claude Desktop and the rest."""
    if not cmdline:
        return False
    if any(marker in cmdline for marker in DESKTOP_MARKERS):
        return False
    exe = cmdline.split()[0].strip('"').lower()
    return exe.endswith("claude") or exe.endswith("claude.exe")


def resume_identifier(cmdline: str) -> Optional[str]:
    """The token following ``--resume`` / ``-r``, or None (fresh session)."""
    tokens = cmdline.split()
    for i, token in enumerate(tokens):
        if token in ("--resume", "-r") and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            if not nxt.startswith("-"):
                return nxt.strip('"')
    return None


def scan() -> LiveScan:
    """One pass over the process table -> a LiveScan verdict."""
    procs = _enumerate_processes()
    if procs is None:
        return LiveScan(ok=False)
    by_uuid: dict = {}
    by_name: dict = {}
    bare: list = []
    for pid, _name, cmdline in procs:
        if not is_claude_cli(cmdline):
            continue
        ident = resume_identifier(cmdline)
        if ident is None:
            bare.append(pid)
        elif UUID_RE.fullmatch(ident):
            by_uuid[ident.lower()] = pid
        else:
            by_name[ident] = pid
    return LiveScan(by_uuid=by_uuid, by_name=by_name,
                    bare_pids=tuple(bare), ok=True)


def verify_member(scan_result: LiveScan, session_id: str,
                  session_name: Optional[str]) -> Optional[int]:
    """PID when this session provably has a live process, else None.

    None means "no proof", NOT "not running" -- a fresh session (bare
    ``claude``) is invisible here by construction, which is exactly why
    the registry exists. Callers word their output accordingly.
    """
    pid = scan_result.by_uuid.get(session_id.lower())
    if pid is not None:
        return pid
    if session_name:
        return scan_result.by_name.get(session_name)
    return None

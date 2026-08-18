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
from datetime import datetime, timedelta, timezone
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
class ProcInfo:
    """One claude CLI process, for pid-based verification (#72)."""

    cmdline: str = ""
    created: Optional[datetime] = None  # aware UTC, or None (no guard)


@dataclass(frozen=True)
class LiveScan:
    """One pass over the process table.

    ``by_uuid``: lowercased session UUID -> pid, for ``--resume <uuid>``.
    ``by_name``: verbatim identifier -> pid, for ``--resume <name>`` --
    resolve through the index before matching.
    ``by_pid``: int pid -> ProcInfo for every claude CLI process (#72) --
    the lookup table for registry entries that recorded their host pid.
    ``bare_pids``: Claude CLI processes with no ``--resume`` identifier
    (fresh sessions; provably live, not attributable from argv).
    ``ok``: False when the process table could not be read at all --
    callers must then say "unverified", never "not running".
    """

    by_uuid: dict = field(default_factory=dict)
    by_name: dict = field(default_factory=dict)
    by_pid: dict = field(default_factory=dict)
    bare_pids: tuple = ()
    ok: bool = True


def _enumerate_processes() -> Optional[list[tuple]]:
    """[(pid, name, cmdline[, created_utc])] or None on failure.

    THE mock seam for tests. The 4th element (#72) is the process
    creation instant as aware UTC, or None where the platform cannot
    say -- ``scan()`` tolerates 3-tuples so older mocks stay valid.
    """
    try:
        if sys.platform == "win32":
            # CreationDate is projected to epoch seconds in PowerShell
            # ([DateTimeOffset] handles the local-time kind correctly) --
            # parsing CIM datetime strings in Python is strictly worse.
            ps = (
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,Name,CommandLine,"
                "@{n='Created';e={ if ($_.CreationDate) { "
                "[long]([DateTimeOffset]$_.CreationDate)"
                ".ToUnixTimeSeconds() } }} | "
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
                 r.get("CommandLine") or "", _epoch_utc(r.get("Created")))
                for r in rows
            ]
        # POSIX: `ps -axo`, never `-eo` -- on FreeBSD `-e` means "show the
        # environment", so `-eo` silently does the wrong thing there.
        # etimes= (elapsed seconds) is the portable creation signal on
        # Linux and modern BSDs; platforms without it (some macOS) fall
        # through to the no-creation form -- the guard degrades, the
        # scan does not.
        now = datetime.now(timezone.utc)
        out = subprocess.run(
            ["ps", "-axo", "pid=,etimes=,args="],
            capture_output=True, text=True, timeout=_SCAN_TIMEOUT,
            check=False,
        ).stdout
        procs = []
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[1].isdigit():
                pid, etimes, args = parts
                procs.append((pid, args.split()[0] if args else "", args,
                              now - timedelta(seconds=int(etimes))))
        if procs:
            return procs
        out = subprocess.run(
            ["ps", "-axo", "pid=,args="],
            capture_output=True, text=True, timeout=_SCAN_TIMEOUT,
            check=False,
        ).stdout
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                pid, args = parts
                procs.append((pid, args.split()[0] if args else "", args))
        return procs
    except Exception:  # noqa: BLE001 -- advisory; degrade, never raise
        return None


def _epoch_utc(value) -> Optional[datetime]:
    """Epoch seconds -> aware UTC datetime, or None (advisory data)."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def is_claude_cli(cmdline: str) -> bool:
    """True for the Claude Code CLI; False for Claude Desktop and the rest."""
    if not cmdline:
        return False
    if any(marker in cmdline for marker in DESKTOP_MARKERS):
        return False
    exe = cmdline.split()[0].strip('"').lower()
    return exe.endswith("claude") or exe.endswith("claude.exe")


def is_claude_process_name(name: str) -> bool:
    """True when the EXECUTABLE NAME alone identifies the Claude Code CLI.

    The elevation escape hatch. A process running at a higher integrity
    level than csb hides its PEB: Win32_Process returns an empty
    ``CommandLine`` (and ``ExecutablePath``) while ``Name`` still reads
    -- observed live, a whole ancestry unreadable
    (claude.exe -> cmd.exe -> WindowsTerminal.exe) under an admin
    terminal owned by the same user.

    Name is a WEAKER identifier than the command line -- it cannot
    distinguish Claude Desktop's ``claude.exe`` -- so it qualifies a
    process for PID verification ONLY (``by_pid``), never for the
    argv-derived maps. That is sound because a pid-bearing entry did not
    guess its pid: the hook captured it from INSIDE the process tree,
    and the creation-time guard still applies. Desktop's claude.exe
    cannot collide, because no registry entry ever records its pid.
    """
    if not name:
        return False
    return name.strip().lower() in ("claude", "claude.exe")


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
    by_pid: dict = {}
    bare: list = []
    for row in procs:
        pid, name, cmdline = row[0], row[1], row[2]
        created = row[3] if len(row) > 3 else None
        cli = is_claude_cli(cmdline)
        # by_pid admits a NAME-only match too: an elevated session hides
        # its cmdline from a non-elevated scan, and dropping it here made
        # pid verification inherit the very argv dependency #72 exists to
        # escape -- a live session rendering [no exit observed] while
        # running (caught live, 2026-08-08). The argv maps below stay
        # cmdline-only: they genuinely need argv, and a nameless match
        # carries no identifier to file under.
        if not cli and not (not (cmdline or "").strip()
                            and is_claude_process_name(name)):
            continue
        try:
            by_pid[int(pid)] = ProcInfo(cmdline=cmdline, created=created)
        except (TypeError, ValueError):
            pass
        if not cli:
            continue
        ident = resume_identifier(cmdline)
        if ident is None:
            bare.append(pid)
        elif UUID_RE.fullmatch(ident):
            by_uuid[ident.lower()] = pid
        else:
            by_name[ident] = pid
    return LiveScan(by_uuid=by_uuid, by_name=by_name, by_pid=by_pid,
                    bare_pids=tuple(bare), ok=True)


def verify_member(scan_result: LiveScan, session_id: str,
                  session_name: Optional[str]) -> Optional[int]:
    """PID when this session provably has a live process, else None.

    None means "no proof", NOT "not running" -- a fresh session (bare
    ``claude``) is invisible here by construction, which is exactly why
    the registry exists. Callers word their output accordingly.

    argv-only: use ``verify_entry`` when a registry entry is in hand --
    entries that recorded their host pid must NOT fall back to this
    (#72: a frozen argv can name a session its process no longer hosts).
    """
    pid = scan_result.by_uuid.get(session_id.lower())
    if pid is not None:
        return pid
    if session_name:
        return scan_result.by_name.get(session_name)
    return None


# The host necessarily predates its own hook fire; the allowance only
# absorbs clock skew between the scanner and the entry's stamp.
_CREATION_SKEW_S = 60


def verify_entry(scan_result: LiveScan, entry: dict,
                 session_name: Optional[str]) -> Optional[int]:
    """The #72 verification ladder for one registry entry -> pid or None.

    An entry that recorded its host pid verifies by THAT pid alone:
    alive + claude CLI + creation predates the entry (pid-reuse guard;
    skipped when the platform gave no creation time). argv matching is
    DISABLED for pid-bearing entries -- trusting argv there is exactly
    the ghost bug (an in-app-switched process still argv-names the
    session it abandoned). Pid-less entries (pre-upgrade, old plugin)
    keep the argv path unchanged.
    """
    pid = entry.get("pid")
    if pid is not None:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        info = scan_result.by_pid.get(pid)
        if info is None:
            return None  # dead, or reused by a non-claude process
        if info.created is not None:
            from .live_registry import parse_entry_ts

            # Anchor the reuse guard on WHEN THE PID WAS RECORDED, not on
            # when the session opened. A RESUMED session's host is
            # legitimately younger than the session -- that is what
            # resuming means -- so anchoring on ``started_at`` rejected a
            # host 39 hours "too young" and reported `[no exit observed]`
            # for the very process csb was running inside of (measured
            # 2026-08-17 against a session resumed after a forced
            # restart). ``pid_at`` is the pid's own provenance: a process
            # created AFTER csb recorded that pid cannot be the one csb
            # saw, which is precisely what pid reuse means. Falls back to
            # ``started_at`` for pre-#72 entries that carry no pid_at.
            # The LATER of the two, never simply "pid_at if present".
            # pid_at cannot legitimately precede started_at -- a session's
            # pid is recorded at or after it opens -- so an earlier pid_at
            # means the entry is internally inconsistent. Letting it win
            # there permanently rejects a genuinely live host, and this
            # module's stated posture is that a false "not running" is the
            # worse error: liveness is advisory, and hiding a live session
            # is exactly the failure the pid ladder exists to end.
            stamps = [t for t in (parse_entry_ts(entry.get("pid_at")),
                                  parse_entry_ts(entry.get("started_at")))
                      if t is not None]
            anchor = max(stamps) if stamps else None
            if anchor is not None and info.created > anchor + timedelta(
                    seconds=_CREATION_SKEW_S):
                return None  # pid reused by a younger claude process
        return pid
    return verify_member(scan_result, entry.get("session_id") or "",
                         session_name)


def arbitrate_pid_claims(pairs) -> None:
    """Resolve multiple roster rows claiming ONE pid (#72 arbitration).

    A pid is one process hosting one conversation -- two rows wearing it
    means at most one is right. Rules, strongest evidence first:

    * Among pid-BEARING entries claiming the same verified pid, the
      freshest ``pid_at`` stamp keeps it: an in-app switch strands the
      old entry's stale pid while the new conversation's hook re-stamps
      the same host.
    * A pid proven by any pid-bearing entry cannot also be claimed by a
      pid-LESS entry's argv match -- capture beats argv, because the
      frozen argv naming a session its process abandoned IS the ghost.

    Claims that are argv-only on both sides are left alone: there is no
    stronger evidence to arbitrate with. Demoted rows fall to
    unverified/None in place. ``pairs`` is ``(member, entry)`` with
    members already verified.
    """
    by_pid: dict = {}
    for member, entry in pairs:
        if member.get("pid") is not None:
            by_pid.setdefault(member["pid"], []).append((member, entry))
    for claims in by_pid.values():
        if len(claims) < 2:
            continue
        captured = [(m, e) for m, e in claims if e.get("pid") is not None]
        if not captured:
            continue
        keep = max(captured, key=lambda me: me[1].get("pid_at") or "")[0]
        for member, _entry in claims:
            if member is not keep:
                member["live_status"] = "unverified"
                member["pid"] = None

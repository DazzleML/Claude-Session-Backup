"""Boot epochs: reconstruct "what was active at the last shutdown" (#60, #61).

An *epoch* is the span between two machine restarts. This module reads the
boot/shutdown fences from the OS and answers, from the existing session
index, which sessions had activity inside an epoch's window -- the
read-only heuristic that powers ``csb set show last``.

Design constraints (see the #60 epic and the session-sets DWP):

* **Pure logic.** No printing, no database opening -- callers pass rows in
  and render results out. Everything here is unit-testable without a
  console or an index.
* **Fences are read live, never stored.** The Windows System event log
  already keeps the history (event IDs 6005 boot / 6006 clean shutdown /
  6008 unexpected shutdown / 1074 restart initiated by a process -- the
  Windows Update signature). Reading at query time means no schema change
  and no staleness.
* **Activity is not liveness.** A session "active within the window" was
  *probably* open at the shutdown; one idle longer than the window is
  missed, one closed shortly before is wrongly included. Callers must
  render this honestly ("active within ...", never "open"). Exact
  open/close events arrive with the observation phase (#64).
* **Windows-first.** POSIX fence reading (journalctl / ``last``) is
  planned under #60; until then :func:`read_fences` raises
  :class:`FenceUnavailableError` off-Windows with a message that says so.

Timestamp hardening: csb supports Python 3.10, whose
``datetime.fromisoformat`` accepts neither a trailing ``Z`` nor the
7-digit fractional seconds PowerShell's ``'{0:o}'`` format emits.
:func:`parse_index_ts` and :func:`_parse_fence_lines` normalize both
before parsing. (Python 3.11+ parses these natively -- which is exactly
how the bug hides on dev boxes.)

Seeded from ``tests/one-offs/probe_epoch_reconstruction.py``, the read-only
probe that validated this reconstruction against real fences and real
index data before the feature was built.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

# Event IDs read from the System log. 1074 is not itself a fence -- it
# labels a nearby 6006 as "initiated by a process" (update/restart).
FENCE_EVENT_IDS = (6005, 6006, 6008, 1074)

# Update restarts often double-cycle (shutdown/boot/shutdown/boot within
# minutes). Shutdown fences closer together than this collapse into one.
SHUTDOWN_COLLAPSE_SECONDS = 600

# 1074 events can be chatty on update-heavy machines; the probe found 4
# fences in 60 events -- 200 gives headroom without a slow query.
MAX_EVENTS = 200

# How close (seconds) a 1074 must sit to a shutdown fence to label it.
_INITIATED_PROXIMITY_SECONDS = 300

# Window fallback when the previous fence is unknown (event log truncated):
# empirically a 24h window missed sessions idle-but-open for 2 days, so the
# fallback errs generous.
FALLBACK_WINDOW_HOURS = 72.0

_POWERSHELL_SCRIPT = (
    "Get-WinEvent -FilterHashtable @{{LogName='System'; ID=6005,6006,6008,1074}} "
    "-MaxEvents {max_events} -ErrorAction SilentlyContinue | "
    "ForEach-Object {{ '{{0:o}}|{{1}}' -f $_.TimeCreated.ToUniversalTime(), $_.Id }}"
)


class FenceUnavailableError(RuntimeError):
    """Boot-fence reading is not possible here (platform / PowerShell)."""


@dataclass(frozen=True)
class Fence:
    """One boot/shutdown event, in UTC."""

    at_utc: datetime
    event_id: int


@dataclass(frozen=True)
class Epoch:
    """The most recent completed epoch: what ended at ``shutdown_utc``."""

    shutdown_utc: datetime
    cause: str  # "clean" | "unexpected" | "initiated-by-process"
    boot_utc: Optional[datetime]  # first boot after the shutdown
    prev_fence_utc: Optional[datetime]  # boot that started the epoch


def _normalize_iso(s: str) -> str:
    """Make an ISO-8601 string parseable by Python 3.10's fromisoformat.

    3.10 rejects a trailing ``Z`` and any fractional-seconds field that is
    not exactly 3 or 6 digits -- PowerShell's round-trip format emits 7.
    """
    s = s.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    # Truncate fractional seconds to 6 digits (keep any offset suffix).
    return re.sub(r"\.(\d{6})\d+", r".\1", s)


def parse_index_ts(s: Optional[str]) -> Optional[datetime]:
    """Index/JSONL timestamp string -> aware UTC datetime, or None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(_normalize_iso(s))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_fence_lines(text: str) -> list[Fence]:
    """Parse ``<iso-utc>|<event-id>`` lines from the PowerShell query.

    Tolerates garbage lines (PowerShell warnings, blank lines) by skipping
    anything that does not parse. Returns fences newest-first.
    """
    fences: list[Fence] = []
    for line in text.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        ts_s, _, id_s = line.rpartition("|")
        ts = parse_index_ts(ts_s)
        if ts is None:
            continue
        try:
            event_id = int(id_s)
        except ValueError:
            continue
        if event_id in FENCE_EVENT_IDS:
            fences.append(Fence(at_utc=ts, event_id=event_id))
    fences.sort(key=lambda f: f.at_utc, reverse=True)
    return fences


def _run_powershell(script: str, timeout: float = 30.0) -> str:
    """Run a PowerShell one-liner, return stdout. THE mock seam for tests."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return result.stdout or ""


def read_fences(max_events: int = MAX_EVENTS, timeout: float = 30.0) -> list[Fence]:
    """Read boot/shutdown fences from the OS, newest-first.

    Raises :class:`FenceUnavailableError` off-Windows or when PowerShell
    is missing, hangs, or yields nothing usable. An empty *event* result
    (no fences in the log) returns ``[]`` -- that is a valid answer, not
    an error.
    """
    if sys.platform != "win32":
        raise FenceUnavailableError(
            "Boot-fence detection reads the Windows event log and is "
            "Windows-only in this version. POSIX support (journalctl/`last`) "
            "is planned -- see epic #60."
        )
    script = _POWERSHELL_SCRIPT.format(max_events=max_events)
    try:
        out = _run_powershell(script, timeout=timeout)
    except FileNotFoundError as exc:
        raise FenceUnavailableError(
            "PowerShell was not found on PATH -- cannot read boot fences."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FenceUnavailableError(
            f"Reading the event log timed out after {timeout:.0f}s."
        ) from exc
    return _parse_fence_lines(out)


def latest_epoch(fences: list[Fence]) -> Optional[Epoch]:
    """The most recent completed epoch, or None when no shutdown fence exists.

    Update restarts DOUBLE-CYCLE: shutdown / boot / shutdown / boot within
    minutes (the real 2026-07-15 log: 6006 at 4:16:21, 6005 at 4:17:09,
    6006 at 4:17:38, 6005 at 4:18:17). All fences chained closer together
    than :data:`SHUTDOWN_COLLAPSE_SECONDS` form ONE restart *cluster*:

    * ``shutdown_utc`` is the newest shutdown fence in the cluster.
    * ``prev_fence_utc`` -- the boot that STARTED the ended epoch -- must
      be the latest boot *older than the whole cluster*. Taking merely
      "the latest boot before the shutdown" would pick the intermediate
      mid-update boot and collapse the default window to seconds,
      producing an empty roster after exactly the restart this feature
      exists for.
    * The cause is ``unexpected`` when the cluster holds a 6008, else
      ``initiated-by-process`` when it holds a 1074 (or one sits within
      :data:`_INITIATED_PROXIMITY_SECONDS` of the shutdown), else clean.
    """
    ordered = sorted(fences, key=lambda f: f.at_utc, reverse=True)
    shutdowns = [f for f in ordered if f.event_id in (6006, 6008)]
    if not shutdowns:
        return None
    shut = shutdowns[0]

    # Walk older fences (any event id), extending the cluster while the
    # chain gap stays inside the collapse threshold. cluster_start ends
    # at the oldest member's timestamp.
    cluster_start = shut.at_utc
    cluster_ids = {shut.event_id}
    for f in ordered:
        if f.at_utc >= shut.at_utc:
            continue
        if (cluster_start - f.at_utc).total_seconds() <= SHUTDOWN_COLLAPSE_SECONDS:
            cluster_start = f.at_utc
            cluster_ids.add(f.event_id)

    if 6008 in cluster_ids:
        cause = "unexpected"
    elif 1074 in cluster_ids or any(
        f.event_id == 1074
        and abs((f.at_utc - shut.at_utc).total_seconds())
        < _INITIATED_PROXIMITY_SECONDS
        for f in ordered
    ):
        cause = "initiated-by-process"
    else:
        cause = "clean"

    boots_after = [
        f.at_utc for f in ordered if f.event_id == 6005 and f.at_utc > shut.at_utc
    ]
    boots_before_cluster = [
        f.at_utc for f in ordered
        if f.event_id == 6005 and f.at_utc < cluster_start
    ]
    return Epoch(
        shutdown_utc=shut.at_utc,
        cause=cause,
        boot_utc=min(boots_after) if boots_after else None,
        prev_fence_utc=(
            max(boots_before_cluster) if boots_before_cluster else None
        ),
    )


def epoch_window(
    epoch: Epoch, override_hours: Optional[float] = None
) -> tuple[datetime, datetime, str]:
    """The activity window ``(lo, hi, source)`` for an epoch.

    Default is *since the previous fence* -- the whole prior epoch counts,
    because idle-but-open for days is normal for heavy multi-window use
    (empirically: a 24h window missed two sessions that were genuinely
    open at a real restart). ``override_hours`` narrows or widens it;
    when the previous fence is unknown, a generous fixed fallback applies.
    Source is one of ``"previous-fence" | "override" | "fallback-72h"``.
    """
    hi = epoch.shutdown_utc
    if override_hours is not None:
        return hi - timedelta(hours=override_hours), hi, "override"
    if epoch.prev_fence_utc is not None:
        return epoch.prev_fence_utc, hi, "previous-fence"
    return hi - timedelta(hours=FALLBACK_WINDOW_HOURS), hi, "fallback-72h"


def build_roster(
    rows, lo: datetime, hi: datetime
) -> tuple[list[dict], int]:
    """Sessions with activity inside ``[lo, hi]`` -> (members, missing_count).

    ``rows`` is any iterable of mappings with the ``sessions`` table's
    columns (sqlite3.Row works). Members are ordered by last activity
    ascending -- a timeline ending at the shutdown -- tie-broken by
    session_id for determinism, and numbered 1-based. ``missing_count``
    is the number of rows whose ``last_active_at`` could not be parsed:
    they are counted, never silently dropped (#61 AC).
    """
    candidates: list[tuple[datetime, str, dict]] = []
    missing = 0
    for row in rows:
        la = parse_index_ts(row["last_active_at"])
        if la is None:
            missing += 1
            continue
        if not (lo <= la <= hi):
            continue
        sid = row["session_id"]
        candidates.append(
            (
                la,
                sid,
                {
                    "session_id": sid,
                    "session_name": row["session_name"],
                    "project": row["project"],
                    "start_folder": row["start_folder"],
                    "started_at": row["started_at"],
                    "last_active_at": row["last_active_at"],
                    "jsonl_path": row["jsonl_path"],
                    "jsonl_mtime": row["jsonl_mtime"],
                    "purged": bool(row["deleted_at"]),
                    "is_fork": bool(row["is_fork"]),
                    "in_index": True,
                },
            )
        )
    candidates.sort(key=lambda t: (t[0], t[1]))
    members = []
    for index, (_la, _sid, member) in enumerate(candidates, start=1):
        member["index"] = index
        members.append(member)
    return members, missing


def format_gap(delta: timedelta) -> str:
    """Compact duration label: '1d22h', '3h36m', '5m', '46s'."""
    total = max(0, int(delta.total_seconds()))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"

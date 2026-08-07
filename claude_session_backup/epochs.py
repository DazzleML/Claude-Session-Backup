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

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

# Named event ids (Windows System-log values ARE the wire format; POSIX
# sources map into them -- R2 DWP E1). 1074 is not itself a fence -- it
# labels a nearby 6006 as "initiated by a process" (update/restart).
EVENT_BOOT = 6005
EVENT_SHUTDOWN_CLEAN = 6006
EVENT_SHUTDOWN_UNEXPECTED = 6008
EVENT_RESTART_INITIATED = 1074
# Synthetic (never emitted by Windows): a shutdown was observed but no
# source can say HOW it ended -- the honest floor on journal-only boxes
# whose wtmp is rotated or absent.
EVENT_SHUTDOWN_UNKNOWN = 9006

SHUTDOWN_EVENT_IDS = frozenset(
    {EVENT_SHUTDOWN_CLEAN, EVENT_SHUTDOWN_UNEXPECTED, EVENT_SHUTDOWN_UNKNOWN}
)

FENCE_EVENT_IDS = (
    EVENT_BOOT, EVENT_SHUTDOWN_CLEAN, EVENT_SHUTDOWN_UNEXPECTED,
    EVENT_RESTART_INITIATED, EVENT_SHUTDOWN_UNKNOWN,
)

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

    Dispatches to the Windows event-log reader or the POSIX chain
    (journalctl -> wtmp -- R2 DWP E2/E3). Raises
    :class:`FenceUnavailableError` when no source can answer. An empty
    *event* result (no fences in the log) returns ``[]`` -- that is a
    valid answer, not an error.
    """
    if sys.platform == "win32":
        return _read_fences_windows(max_events=max_events, timeout=timeout)
    return _read_fences_posix(timeout=timeout)


def _read_fences_windows(max_events: int = MAX_EVENTS,
                         timeout: float = 30.0) -> list[Fence]:
    """Windows: the System event log via PowerShell (the original path)."""
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


# ── POSIX fence reading (R2: journalctl -> wtmp, evidence-first) ─────────

# How close (seconds) a wtmp shutdown record must sit to a journal boot's
# last entry to corroborate a CLEAN shutdown. Mirrors the cluster scale.
_CORROBORATION_SECONDS = 600


def _run_command(argv: list, timeout: float = 15.0) -> tuple[int, str]:
    """(rc, stdout) for a POSIX source command. THE mock seam for tests.

    LC_ALL=C is forced: ``last`` prints locale month names otherwise and
    strptime's %a/%b only speak C-locale English.
    """
    env = dict(os.environ, LC_ALL="C")
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout,
        check=False, env=env,
    )
    return result.returncode, result.stdout or ""


def _usec_to_utc(value) -> Optional[datetime]:
    """journalctl JSON microsecond epoch (int or str) -> aware UTC."""
    try:
        return datetime.fromtimestamp(int(value) / 1_000_000,
                                      tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _parse_journalctl_json(out: str) -> Optional[list[tuple]]:
    """``--list-boots -o json`` -> [(index, first_utc, last_utc)] or None.

    None means "this is not JSON" -- which systemd < 254 produces with
    rc 0 by silently IGNORING ``-o json`` for --list-boots (capture
    finding F1). Callers must then hand THE SAME OUTPUT to the text
    parser, never re-invoke. Spec-frozen (systemd 254+ shape: objects
    with index/boot_id/first_entry/last_entry, usec ints -- tolerate
    strings); flagged for live validation on a 24.04-era box.
    """
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    boots = []
    for row in data:
        if not isinstance(row, dict):
            continue
        first = _usec_to_utc(row.get("first_entry"))
        last = _usec_to_utc(row.get("last_entry"))
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        if first is None or last is None:
            continue
        boots.append((index, first, last))
    return boots


_JOURNAL_IDX_RE = re.compile(r"^\s*(-?\d+)\s+[0-9a-fA-F]{32}\s")
_JOURNAL_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC")


def _parse_journalctl_text(out: str) -> list[tuple]:
    """``--utc --list-boots`` text -> [(index, first_utc, last_utc)].

    Capture-frozen against real systemd 249 output (finding F2):
    headerless ``IDX BOOTID  <first>—<last>`` with an em-dash joining
    the timestamps. The parser anchors on the index+bootid prefix and
    the ``YYYY-MM-DD HH:MM:SS UTC`` shape ONLY -- never day names, TZ
    abbreviations, or the dash (a headered variant's header line simply
    fails the prefix match). Only --utc output parses, by design.
    """
    boots = []
    for line in out.splitlines():
        m = _JOURNAL_IDX_RE.match(line)
        if not m:
            continue
        stamps = _JOURNAL_TS_RE.findall(line)
        if len(stamps) < 2:
            continue
        first = datetime.strptime(
            stamps[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        last = datetime.strptime(
            stamps[-1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        boots.append((int(m.group(1)), first, last))
    return boots


def _local_naive_to_utc(dt: datetime) -> datetime:
    """Naive LOCAL time -> aware UTC. THE tz seam for tests.

    ``last`` prints local times; a naive datetime's ``astimezone``
    interprets it in the system zone, which is correct here and
    monkeypatchable for deterministic tests.
    """
    return dt.astimezone(timezone.utc)


_LAST_FULL_YEAR_RE = re.compile(
    r"[A-Z][a-z]{2} [A-Z][a-z]{2} [ \d]?\d \d{2}:\d{2}:\d{2} \d{4}"
)
_LAST_NO_YEAR_RE = re.compile(
    r"[A-Z][a-z]{2} [A-Z][a-z]{2} [ \d]?\d \d{2}:\d{2}"
)


def _parse_last_records(out: str) -> tuple[list[tuple], Optional[datetime]]:
    """``last -xF reboot shutdown`` -> ([(kind, utc)], wtmp_begins_utc).

    Full-year timestamps (-F) parsed under LC_ALL=C, local -> UTC via
    the tz seam. Rows take the line's FIRST timestamp (a record's begin
    instant); "still running" contributes no second stamp and no
    meaning beyond the boot itself (WSL keeps EVERY historical boot
    "still running" -- capture finding F3). The ``wtmp begins`` trailer
    is the rotation-coverage bound; unparseable -> None (callers then
    take the honest-unknown floor, never the crash label).
    """
    records = []
    begins = None
    for line in out.splitlines():
        stripped = line.strip()
        m = _LAST_FULL_YEAR_RE.search(stripped)
        if m is None:
            continue
        try:
            dt = datetime.strptime(re.sub(r"\s+", " ", m.group(0)),
                                   "%a %b %d %H:%M:%S %Y")
        except ValueError:
            continue
        utc = _local_naive_to_utc(dt)
        if stripped.startswith("wtmp begins"):
            begins = utc
        elif stripped.startswith("reboot"):
            records.append(("reboot", utc))
        elif stripped.startswith("shutdown"):
            records.append(("shutdown", utc))
    return records, begins


def _parse_last_no_year(out: str, now_local: datetime
                        ) -> tuple[list[tuple], Optional[datetime]]:
    """BSD/macOS ``last`` (no -F, no year) -> ([(kind, utc)], begins).

    Current-year walk, newest-first: a stamp that would land in the
    future (beyond a day of skew) belongs to the previous year, and any
    stamp NEWER than the one above it crossed a Dec->Jan boundary --
    decrement and re-place. Spec-frozen; rollover is red-green tested.
    """
    raw = []
    begins_raw = None
    for line in out.splitlines():
        stripped = line.strip()
        m = _LAST_NO_YEAR_RE.search(stripped)
        if m is None:
            continue
        try:
            dt = datetime.strptime(re.sub(r"\s+", " ", m.group(0)),
                                   "%a %b %d %H:%M")
        except ValueError:
            continue
        if stripped.startswith("wtmp begins"):
            begins_raw = dt
        elif stripped.startswith("reboot"):
            raw.append(("reboot", dt))
        elif stripped.startswith("shutdown"):
            raw.append(("shutdown", dt))

    def _walk(items):
        year = now_local.year
        prev = None
        placed = []
        for kind, naive in items:
            for _attempt in range(3):
                try:
                    dt = naive.replace(year=year)
                except ValueError:  # Feb 29 into a non-leap year
                    dt = None
                if dt is not None and dt <= now_local + timedelta(days=1) \
                        and (prev is None or dt <= prev):
                    break
                year -= 1
            if dt is None:
                continue
            prev = dt
            placed.append((kind, _local_naive_to_utc(dt)))
        return placed

    placed = _walk(raw)
    begins = None
    if begins_raw is not None:
        oldest = placed[-1][1] if placed else None
        year = (oldest.year if oldest is not None else now_local.year)
        for _attempt in range(2):
            try:
                candidate = _local_naive_to_utc(begins_raw.replace(year=year))
            except ValueError:
                candidate = None
            if candidate is not None and (
                    oldest is None or candidate <= oldest):
                begins = candidate
                break
            year -= 1
    return placed, begins


def _corroborate_shutdown(instant: datetime, wtmp_shutdowns: list,
                          wtmp_begins: Optional[datetime]) -> int:
    """The POSIX cause ladder for one journal-observed shutdown (E4).

    A matching wtmp shutdown record -> clean. wtmp COVERED the instant
    and stayed silent -> unexpected (crash-by-absence). wtmp rotated
    past it or absent -> unknown, the honest floor. The coverage guard
    is mandatory: stock logrotate keeps ONE wtmp file, and without the
    guard every pre-rotation shutdown would read as a crash.
    """
    if any(abs((s - instant).total_seconds()) <= _CORROBORATION_SECONDS
           for s in wtmp_shutdowns):
        return EVENT_SHUTDOWN_CLEAN
    if wtmp_begins is not None and wtmp_begins <= instant:
        return EVENT_SHUTDOWN_UNEXPECTED
    return EVENT_SHUTDOWN_UNKNOWN


def _read_fences_posix(timeout: float = 15.0) -> list[Fence]:
    """The POSIX chain: journalctl (JSON-sniffed) -> wtmp (E3).

    journalctl is the boot-count authority when it can answer with
    history (its spans define boot AND shutdown instants); wtmp
    corroborates causes. A volatile 1-boot journal falls through so
    wtmp gets its chance. Terminal failure names every rung tried.
    """
    tried = []

    journal_boots = None
    try:
        rc, out = _run_command(
            ["journalctl", "--utc", "--list-boots", "-o", "json"],
            timeout=timeout)
        if rc == 0 and out.strip():
            journal_boots = _parse_journalctl_json(out)
            if journal_boots is None:
                # F1: systemd < 254 ignores -o json -- same output, text.
                journal_boots = _parse_journalctl_text(out) or None
            tried.append("journalctl (answered)")
        else:
            tried.append(f"journalctl (rc={rc})")
    except FileNotFoundError:
        tried.append("journalctl (not found)")
    except subprocess.TimeoutExpired:
        tried.append("journalctl (timed out)")

    wtmp_records: list = []
    wtmp_begins = None
    try:
        rc, out = _run_command(["last", "-xF", "reboot", "shutdown"],
                               timeout=timeout)
        if rc == 0 and out.strip():
            wtmp_records, wtmp_begins = _parse_last_records(out)
            if not wtmp_records and wtmp_begins is None:
                # BSD/macOS: no -F support prints usage or no-year rows.
                rc2, out2 = _run_command(["last", "reboot", "shutdown"],
                                         timeout=timeout)
                if rc2 == 0 and out2.strip():
                    wtmp_records, wtmp_begins = _parse_last_no_year(
                        out2, datetime.now())
            tried.append("last/wtmp (answered)")
        else:
            rc2, out2 = _run_command(["last", "reboot", "shutdown"],
                                     timeout=timeout)
            if rc2 == 0 and out2.strip():
                wtmp_records, wtmp_begins = _parse_last_no_year(
                    out2, datetime.now())
                tried.append("last/wtmp (no-year form)")
            else:
                tried.append(f"last/wtmp (rc={rc})")
    except FileNotFoundError:
        tried.append("last (not found)")
    except subprocess.TimeoutExpired:
        tried.append("last (timed out)")

    wtmp_shutdowns = [at for kind, at in wtmp_records if kind == "shutdown"]

    if journal_boots and len(journal_boots) > 1:
        fences: list[Fence] = []
        for index, first, last in journal_boots:
            fences.append(Fence(at_utc=first, event_id=EVENT_BOOT))
            if index != 0:  # a completed boot's span END is its shutdown
                fences.append(Fence(
                    at_utc=last,
                    event_id=_corroborate_shutdown(
                        last, wtmp_shutdowns, wtmp_begins),
                ))
        fences.sort(key=lambda f: f.at_utc, reverse=True)
        return fences

    if wtmp_records:
        fences = []
        reboots = sorted((at for kind, at in wtmp_records
                          if kind == "reboot"))
        for at in wtmp_shutdowns:
            fences.append(Fence(at_utc=at, event_id=EVENT_SHUTDOWN_CLEAN))
        for i, boot_at in enumerate(reboots):
            fences.append(Fence(at_utc=boot_at, event_id=EVENT_BOOT))
            window_lo = reboots[i - 1] if i > 0 else wtmp_begins
            if window_lo is None:
                continue  # cannot bound the gap -- no synthesis
            if any(window_lo <= s < boot_at for s in wtmp_shutdowns):
                continue  # observed shutdown already fences this gap
            # Covered silence before this boot = crash-by-absence.
            fences.append(Fence(at_utc=boot_at - timedelta(seconds=1),
                                event_id=EVENT_SHUTDOWN_UNEXPECTED))
        fences.sort(key=lambda f: f.at_utc, reverse=True)
        return fences

    if journal_boots:  # exactly one boot, no wtmp: valid, shutdown-less
        return [Fence(at_utc=journal_boots[0][1], event_id=EVENT_BOOT)]

    raise FenceUnavailableError(
        "No POSIX fence source could answer -- tried: "
        + "; ".join(tried or ["nothing runnable"])
        + ". (`csb set show current` and `boot` do not need fences and "
        "still work.)"
    )


def enumerate_epochs(fences: list[Fence]) -> list[Epoch]:
    """Every completed epoch, newest first (R3 H1).

    Update restarts DOUBLE-CYCLE: shutdown / boot / shutdown / boot within
    minutes (the real 2026-07-15 log: 6006 at 4:16:21, 6005 at 4:17:09,
    6006 at 4:17:38, 6005 at 4:18:17). All fences chained closer together
    than :data:`SHUTDOWN_COLLAPSE_SECONDS` form ONE restart *cluster*, so
    that log is ONE epoch boundary -- naive per-shutdown enumeration
    would mint two epochs 77 seconds apart. Per cluster:

    * ``shutdown_utc`` is the newest shutdown fence in the cluster.
    * ``prev_fence_utc`` -- the boot that STARTED the ended epoch -- must
      be the latest boot *older than the whole cluster*. Taking merely
      "the latest boot before the shutdown" would pick the intermediate
      mid-update boot and collapse the default window to seconds,
      producing an empty roster after exactly the restart this feature
      exists for.
    * The cause is ``unexpected`` when the cluster holds a 6008, else
      ``initiated-by-process`` when it holds a 1074 (or one sits within
      :data:`_INITIATED_PROXIMITY_SECONDS` of the shutdown), else
      ``clean`` on a 6006, else the POSIX honest floor ``unknown``.

    Enumeration repeats the collapse walking newest -> oldest: each
    round's shutdown must be OLDER than the previous cluster's start, so
    clusters never overlap and ``last~N`` addresses are stable within
    one fence read.
    """
    ordered = sorted(fences, key=lambda f: f.at_utc, reverse=True)
    result: list[Epoch] = []
    bound: Optional[datetime] = None  # next shutdown must be older
    while True:
        shutdowns = [
            f for f in ordered if f.event_id in SHUTDOWN_EVENT_IDS
            and (bound is None or f.at_utc < bound)
        ]
        if not shutdowns:
            return result
        shut = shutdowns[0]

        # Walk older fences (any event id), extending the cluster while
        # the chain gap stays inside the collapse threshold.
        cluster_start = shut.at_utc
        cluster_ids = {shut.event_id}
        for f in ordered:
            if f.at_utc >= shut.at_utc:
                continue
            if (cluster_start - f.at_utc).total_seconds() \
                    <= SHUTDOWN_COLLAPSE_SECONDS:
                cluster_start = f.at_utc
                cluster_ids.add(f.event_id)

        if EVENT_SHUTDOWN_UNEXPECTED in cluster_ids:
            cause = "unexpected"
        elif EVENT_RESTART_INITIATED in cluster_ids or any(
            f.event_id == EVENT_RESTART_INITIATED
            and abs((f.at_utc - shut.at_utc).total_seconds())
            < _INITIATED_PROXIMITY_SECONDS
            for f in ordered
        ):
            cause = "initiated-by-process"
        elif EVENT_SHUTDOWN_CLEAN in cluster_ids:
            cause = "clean"
        elif EVENT_SHUTDOWN_UNKNOWN in cluster_ids:
            # POSIX honest floor: a shutdown was observed (journal span
            # ended) but no source can say HOW -- never guessed clean.
            cause = "unknown"
        else:
            cause = "clean"

        boots_after = [
            f.at_utc for f in ordered
            if f.event_id == EVENT_BOOT and f.at_utc > shut.at_utc
        ]
        boots_before_cluster = [
            f.at_utc for f in ordered
            if f.event_id == EVENT_BOOT and f.at_utc < cluster_start
        ]
        result.append(Epoch(
            shutdown_utc=shut.at_utc,
            cause=cause,
            boot_utc=min(boots_after) if boots_after else None,
            prev_fence_utc=(
                max(boots_before_cluster) if boots_before_cluster else None
            ),
        ))
        bound = cluster_start


def latest_epoch(fences: list[Fence]) -> Optional[Epoch]:
    """The most recent completed epoch, or None when no shutdown exists.

    A thin wrapper over :func:`enumerate_epochs` -- pinned equal to its
    first element by tests, so `last` and `last~0` can never drift.
    """
    epochs_list = enumerate_epochs(fences)
    return epochs_list[0] if epochs_list else None


# ── epoch-token addressing (R3 H2: last / last~N / bare dates) ───────────

_TILDE_TOKEN_RE = re.compile(r"^last~(\d+)$", re.IGNORECASE)
_DATE_TOKEN_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def classify_epoch_token(name: str) -> Optional[str]:
    """'last' | 'tilde' | 'date' | None -- pure shape classification.

    Mirrors (a subset of) session_sets.is_reserved_name: everything this
    recognizes was already reserved as a set name in R1, so wiring the
    behavior orphans nobody.
    """
    if name.lower() == "last":
        return "last"
    if _TILDE_TOKEN_RE.match(name):
        return "tilde"
    if _DATE_TOKEN_RE.match(name):
        return "date"
    return None


def resolve_epoch_token(token: str, epochs_list: list[Epoch],
                        tzinfo=None) -> list[tuple[int, Epoch]]:
    """Token -> [(history_index, Epoch)] matches, newest first.

    ``last`` == ``last~0``; ``last~N`` beyond recorded history returns
    [] (callers say how deep history goes); a bare date matches every
    epoch whose LOCAL-date span [epoch start, shutdown] intersects it --
    zero, one, or many (ambiguity is the CALLER'S to surface, never
    resolved silently here). ``tzinfo`` defaults to the system zone;
    tests pass fixed offsets.
    """
    kind = classify_epoch_token(token)
    if kind is None or not epochs_list:
        return []
    if kind == "last":
        return [(0, epochs_list[0])]
    if kind == "tilde":
        n = int(_TILDE_TOKEN_RE.match(token).group(1))
        return [(n, epochs_list[n])] if n < len(epochs_list) else []
    y, mo, d = map(int, _DATE_TOKEN_RE.match(token).groups())
    try:
        from datetime import date as _date

        target = _date(y, mo, d)
    except ValueError:
        return []
    tz = tzinfo or datetime.now().astimezone().tzinfo
    matches = []
    for i, ep in enumerate(epochs_list):
        hi_local = ep.shutdown_utc.astimezone(tz).date()
        lo = ep.prev_fence_utc or (
            ep.shutdown_utc - timedelta(hours=FALLBACK_WINDOW_HOURS))
        lo_local = lo.astimezone(tz).date()
        if lo_local <= target <= hi_local:
            matches.append((i, ep))
    return matches


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
        try:
            # Substance stat; tolerated absent so callers with narrower
            # row shapes (tests, older SELECTs) keep working.
            messages = row["message_count"]
        except (KeyError, IndexError):
            messages = None
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
                    "messages": messages,
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

"""The Live Session Registry: what is open right now (#60, #64).

One JSON file per session under ``<claude_dir>/csb-live/``, written by
csb's SessionStart hook and removed by its SessionEnd hook. The design
embraces the asymmetry the whole session-sets epic is built on:
**SessionEnd's unreliability is the signal, not the flaw.** A clean
close erases the entry; an unclean one leaves testimony. One comparison
against the current boot time classifies every entry:

* stamped after the current boot  -> currently open ("running" only when
  a live process verifies it; otherwise "no exit observed")
* stamped before the current boot -> was open when the machine went down
  -- simultaneously the exact open-at-shutdown record and crash
  evidence, because a clean close would have removed it

Design rules (see the live-session-registry DWP):

* **UUID-keyed, name-free.** The filename is the session UUID and the
  content carries NO session name -- names are resolved at query time
  through the index, so renames (which claude-session-logger performs
  routinely) cost the registry nothing.
* **csb's own files.** The logger's ``session-states/*.started`` flags
  look similar but are its run-counter state and are never removed;
  this registry is liveness state and must be removed. csb never reads
  or deletes the logger's files, and nothing here depends on the logger
  being installed.
* **One file per session.** Concurrent session starts never contend;
  create and unlink are atomic by filesystem semantics. No locks.
* **Paths via ClaudePaths** -- never a literal ``~/.claude``.

Cross-platform from day one: unlike shutdown fences (Windows event log),
the *current boot time* is trivial everywhere.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# One spelling, owned by ClaudePaths (the layout's single owner); the
# hook script mirrors it inline because it cannot import the package.
from .pathkit import ClaudePaths

LIVE_DIRNAME = ClaudePaths.LIVE_DIR
SNAPSHOT_FILENAME = "last-shutdown.json"
SNAPSHOT_VERSION = 1

# R3 (H3): boundary snapshots accumulate under a SUBDIRECTORY -- that is
# load-bearing, not cosmetic. Flat files would be read back as ghost
# sessions by read_entries' *.json glob, including by OLDER csb versions
# reading a git-synced store. last-shutdown.json stays the newest-alias
# so v0.8.x readers keep working.
BOUNDARY_DIRNAME = "boundaries"
BOUNDARY_RETENTION = 5
_SNAPSHOT_TOLERANCE_S = 600  # tick-counter vs event-log skew (~40s real)


def live_dir(claude_dir) -> Path:
    """The registry directory for a claude dir (not auto-created)."""
    return Path(claude_dir) / LIVE_DIRNAME


def is_safe_entry_id(session_id) -> bool:
    """True when ``session_id`` is safe to build a FILENAME from.

    An entry's session id reaches us from a JSON body that rides the
    backup store's git commits -- so it can arrive from another machine
    or a restored store, not only from a local hook. Anything that
    becomes part of a path and is later UNLINKED must therefore be
    treated as untrusted input: a crafted id like ``../../secrets``
    would otherwise escape ``csb-live/`` entirely.

    Deliberately strict rather than clever: a session id is a UUID-
    shaped token, so require a plain non-empty token with no path
    separators, no parent references, and no drive/UNC punctuation.
    """
    if not session_id or not isinstance(session_id, str):
        return False
    if len(session_id) > 128:
        return False
    if any(sep in session_id for sep in ("/", "\\", ":")):
        return False
    if ".." in session_id or session_id.startswith("."):
        return False
    return all(ch.isalnum() or ch in "-_" for ch in session_id)


def entry_path(claude_dir, session_id: str) -> Path:
    """The registry file for a session. Rejects unsafe ids (see above).

    Raises ValueError rather than returning a path outside the registry
    -- callers unlink what this returns, so failing loudly beats
    resolving somewhere unexpected.
    """
    if not is_safe_entry_id(session_id):
        raise ValueError(
            f"unsafe session id for a registry path: {session_id!r}")
    return live_dir(claude_dir) / f"{session_id}.json"


def snapshot_path(claude_dir) -> Path:
    return live_dir(claude_dir) / SNAPSHOT_FILENAME


def boundary_dir(claude_dir) -> Path:
    return live_dir(claude_dir) / BOUNDARY_DIRNAME


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_entry_ts(s: Optional[str]) -> Optional[datetime]:
    """Entry timestamp -> aware UTC datetime (3.10-safe), or None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── boot time (cross-platform, no event log needed) ──────────────────────

def current_boot_utc() -> Optional[datetime]:
    """When this machine last booted, as aware UTC. None when unknowable.

    The registry only needs ONE instant -- "when did the current boot
    start" -- which every platform exposes cheaply. This is deliberately
    not the event-log fence reader (Windows-only, needs history): the
    classification rule needs no history.
    """
    try:
        if sys.platform == "win32":
            # GetTickCount64: milliseconds since boot, monotone, no
            # subprocess. Subtracting from now() yields boot time with
            # ~seconds accuracy -- ample for a boundary that separates
            # boots that are hours apart.
            import ctypes

            ms = ctypes.windll.kernel32.GetTickCount64()
            return _utc_now() - _td_ms(ms)
        if sys.platform.startswith("linux"):
            with open("/proc/uptime", "r", encoding="ascii") as fh:
                seconds = float(fh.read().split()[0])
            return _utc_now() - _td_s(seconds)
        # macOS / BSD: kern.boottime via sysctl.
        import subprocess

        out = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
        # Format: "{ sec = 1722900000, usec = 0 } ..." (macOS) or a bare
        # epoch number on some BSDs.
        import re

        m = re.search(r"sec\s*=\s*(\d+)", out) or re.search(r"^(\d+)", out.strip())
        if m:
            return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
    except Exception:  # noqa: BLE001 -- liveness must never take csb down
        pass
    return None


def _td_ms(ms: float):
    from datetime import timedelta

    return timedelta(milliseconds=ms)


def _td_s(s: float):
    from datetime import timedelta

    return timedelta(seconds=s)


# ── hook-side operations (called by backup-hook.py; must never raise) ────

def record_session_start(claude_dir, session_id: str, source: str = "",
                         cwd: str = "", pid: Optional[int] = None) -> bool:
    """Write the entry for a starting session. Returns True if created.

    Write-if-missing for ``started_at``: a ``source=compact`` restart of
    the same session must not reset it -- the session has been open the
    whole time. The host ``pid`` (#72) is the opposite -- newest-wins:
    every SessionStart knows the *current* host, so an existing entry
    gets its pid refreshed in place (returns False; only a fresh entry
    returns True). An unparseable existing entry is left untouched --
    it is evidence, and destroying it costs more than a stale pid.

    Never raises (hook context): a failed write costs one observation,
    not a session.
    """
    if not session_id:
        return False
    try:
        path = entry_path(claude_dir, session_id)
        if path.exists():
            if pid is None:
                return False
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            if not isinstance(payload, dict) or payload.get("pid") == pid:
                return False
            payload["pid"] = int(pid)
            # pid_at: when this claim was stamped -- the arbitration key
            # when two entries claim one pid (an in-app switch strands
            # the old entry's stale pid; the freshest stamp wins).
            payload["pid_at"] = _iso(_utc_now())
            tmp = path.with_suffix(f".tmp-{os.getpid()}")
            tmp.write_text(json.dumps(payload, indent=2) + "\n",
                           encoding="utf-8")
            os.replace(tmp, path)
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session_id,
            "started_at": _iso(_utc_now()),
            "source": source or "",
            "cwd": cwd or "",
        }
        if pid is not None:
            payload["pid"] = int(pid)
            payload["pid_at"] = payload["started_at"]
        tmp = path.with_suffix(f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def record_session_end(claude_dir, session_id: str) -> bool:
    """Remove the entry for an ending session. Idempotent; never raises."""
    if not session_id:
        return False
    try:
        entry_path(claude_dir, session_id).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# ── reading the registry ─────────────────────────────────────────────────

def read_entries(claude_dir) -> list[dict]:
    """All registry entries, oldest-started first. Tolerates garbage.

    A file whose content cannot be parsed still represents *something*
    that started (the filename is the UUID); it is returned with a null
    ``started_at`` rather than dropped -- consistent with the epic's
    counted-never-dropped rule.

    Each entry carries ``_path``: the file it was ACTUALLY read from.
    FOR cleanup targeting only (#90) -- the sweep must unlink the file
    it read, not a path rebuilt from the body-claimed id, or an entry
    whose filename and body disagree is recorded forever and cleared
    never. NOT identity (that is ``session_id``, where a safe body
    claim wins -- content is the fact), and never written into records
    or JSON output: strip it at every exit.
    """
    directory = live_dir(claude_dir)
    if not directory.is_dir():
        return []
    entries: list[dict] = []
    for path in directory.glob("*.json"):
        if path.name == SNAPSHOT_FILENAME:
            continue
        session_id = path.stem
        started_at = None
        source = ""
        cwd = ""
        pid = None
        pid_at = None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                started_at = raw.get("started_at")
                source = raw.get("source") or ""
                cwd = raw.get("cwd") or ""
                # The BODY may name a different session than the
                # filename (historically it won outright). Honour it
                # only when it is a safe token: the id becomes a
                # filename elsewhere, and entries travel between
                # machines via the store's commits.
                claimed = raw.get("session_id")
                if claimed and is_safe_entry_id(claimed):
                    session_id = claimed
                try:
                    pid = int(raw.get("pid"))
                except (TypeError, ValueError):
                    pid = None
                pid_at = raw.get("pid_at") or None
        except (OSError, json.JSONDecodeError):
            pass
        entries.append({
            "session_id": session_id,
            "started_at": started_at,
            "source": source,
            "cwd": cwd,
            "pid": pid,
            "pid_at": pid_at,
            "_path": str(path),
        })
    entries.sort(key=lambda e: (e["started_at"] is None,
                                e["started_at"] or "", e["session_id"]))
    return entries


def seen_alive_after_boot(entry: dict, boot_utc: Optional[datetime]) -> bool:
    """True when csb CONFIRMED this session's host after ``boot_utc``.

    ``pid_at`` is the liveness field: the hook stamps it every fire with
    the moment it resolved the session's host process. ``started_at`` is
    the IDENTITY field -- when the session first opened -- and the hook
    deliberately preserves it across restarts so /compact cannot reset a
    session's open time. The two answer different questions, and a
    RESUMED session makes them disagree: pre-boot identity, post-boot
    liveness. Asking the identity field about liveness is what let the
    sweep delete a running session (2026-08-12).

    A timestamp, not a pid comparison -- so this is immune to pid reuse
    across the boot. The claim is only ever "csb saw this session's host
    alive at time T", which a reboot cannot forge.
    """
    if boot_utc is None:
        return False
    # A pid_at with no pid is not evidence: "csb saw the host alive at T"
    # is meaningless without knowing WHICH host, and nothing can ever
    # verify it. Such an entry would otherwise survive every sweep on a
    # liveness claim no verifier can confirm -- accumulating exactly the
    # graveyard the clearing step exists to prevent.
    if entry.get("pid") is None:
        return False
    seen = parse_entry_ts(entry.get("pid_at"))
    return seen is not None and seen >= boot_utc


def split_by_boot(entries: list[dict], boot_utc: Optional[datetime]
                  ) -> tuple[list[dict], list[dict]]:
    """(this_boot, pre_boot). Unknown boot time -> everything this_boot.

    This answers the LIVENESS question -- who is running now -- so a
    session whose host was confirmed after the boot is this_boot no
    matter how old its ``started_at`` is (see seen_alive_after_boot).
    The historical question ("whose entry predates the boot?") belongs
    to sweep_boundary and is deliberately NOT this partition.

    Erring toward this_boot when the boundary is unknowable is the safe
    direction: a pre-boot entry misfiled as current shows as "no exit
    observed" (true), while a current entry misfiled as pre-boot would
    be swept into the shutdown snapshot while its session is still open.
    Entries with unparseable timestamps stay this_boot for the same
    reason -- and an unparseable ``pid_at`` proves nothing, so it falls
    through to ``started_at`` rather than pinning an entry open.
    """
    if boot_utc is None:
        return list(entries), []
    this_boot: list[dict] = []
    pre_boot: list[dict] = []
    for entry in entries:
        if seen_alive_after_boot(entry, boot_utc):
            this_boot.append(entry)
            continue
        ts = parse_entry_ts(entry.get("started_at"))
        if ts is not None and ts < boot_utc:
            pre_boot.append(entry)
        else:
            this_boot.append(entry)
    return this_boot, pre_boot


# ── the boundary sweep ───────────────────────────────────────────────────

def _snapshot_entries(record) -> list[dict]:
    """The recoverable session entries in a boundary/snapshot record.

    FOR reading records defensively: returns the dict-shaped members of
    ``open_at_shutdown`` when it is a list, and ``[]`` for every other
    shape -- a top-level non-dict, a dict-valued field, a string --
    without ever raising. NOT for telling proven-zero from unknown
    (that is the caller's question; this answers only "what evidence
    can be read out of this record").

    Every reader shares this ONE tolerance policy. Before it existed
    each reader had its own: ``read_snapshot`` skipped malformed
    records entirely while the sweep's merge loop crashed on them --
    and the crash was swallowed by the hook-context handler into a
    permanent silent no-op for that boundary (found 2026-08-19,
    pre-release, by the v0.9.13 adversarial sweep).
    """
    if not isinstance(record, dict):
        return []
    entries = record.get("open_at_shutdown")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _entry_key(entry: dict):
    """Identity key for merging/deduplicating snapshot entries.

    FOR set-membership in the sweep's merge and no-op comparison:
    ``session_id`` when the entry has one, else the entry's canonical
    content. Two DISTINCT entries that both lack ``session_id`` must
    not collapse onto a shared ``None`` (counted-never-dropped, the
    same rule ``read_entries`` honours for unparseable files); two
    COPIES of one entry across duplicate records still must collapse.
    NOT a session identifier -- never use it to build a filename or
    resolve a path.
    """
    sid = entry.get("session_id")
    if sid is not None:
        return sid
    return json.dumps(entry, sort_keys=True)


_BOUNDARY_STAMP_RE = re.compile(r"^boundary-(\d{8}T\d{6}Z)\.json$")
_EPOCH_UTC = datetime.min.replace(tzinfo=timezone.utc)


def _boundary_filename(boot_at) -> Optional[str]:
    """The filename a boundary record SHOULD have, from its own boot_at.

    The record's content is the fact; the filename is a label derived
    from it. Deriving the name here (instead of inheriting whatever
    name an existing record carried) is what lets the sweep HEAL a
    hand-repaired or foreign record whose label lies about its content.
    """
    ts = parse_entry_ts(boot_at)
    if ts is None:
        return None
    stamp = ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"boundary-{stamp}.json"


def _record_instant(path: Path, data) -> Optional[datetime]:
    """When the boundary this record describes happened.

    FOR retention ordering only. The record's own ``boot_at`` (the
    fact) wins; the filename stamp (the label) is only a fallback for
    a record whose content lost its timestamp. NOT for addressing or
    boundary-identity -- ``covering_records`` answers "which records
    describe this boundary". Retention once ordered by filename alone
    and deleted a record it had just extended, because the record's
    label lied about its era (Finding A, 2026-08-19).
    """
    if isinstance(data, dict):
        at = parse_entry_ts(data.get("boot_at"))
        if at is not None:
            return at
    m = _BOUNDARY_STAMP_RE.match(path.name)
    if m:
        try:
            return datetime.strptime(
                m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def covering_records(claude_dir, boot_utc: datetime,
                     tolerance_s: int = _SNAPSHOT_TOLERANCE_S
                     ) -> list[tuple]:
    """Every retained boundary record describing THIS boundary, oldest first.

    Plural on purpose. The boundary key is a *derived* instant on Windows
    and Linux (`now - uptime`), and that derivation drifts -- ~6s over two
    days, measured. A drifted key mints a second record for one boot, and
    because each later sweep sees strictly less (entries it already
    cleared are gone), the newer record holds fewer sessions and the alias
    takes the smaller set.

    Returning only the FIRST match is the trap: on a machine that already
    holds duplicates it selects an arbitrary member, and merging with an
    arbitrary member of a duplicate set preserves nothing. A POC caught
    exactly that before it shipped.

    Returns [(path, parsed_record), ...] sorted by each record's own
    ``boot_at`` (filename as tiebreak), so callers can union across all
    of them and collapse to the oldest. Ordering by CONTENT matters:
    on well-formed records it equals filename order, but a record whose
    filename lies about its content (a hand repair, a foreign build)
    must not be "oldest" merely because its label says so.
    """
    out: list[tuple] = []
    bdir = boundary_dir(claude_dir)
    if boot_utc is None or not bdir.is_dir():
        return out
    for path in sorted(bdir.glob("boundary-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            # A bare JSON list/string/number is not a record. Skipping
            # (rather than raising into the hook-context handler, which
            # would silently abort the whole sweep) leaves it to age out
            # with the empties -- it holds no locatable evidence.
            continue
        at = parse_entry_ts(data.get("boot_at"))
        if at is not None and abs(
                (at - boot_utc).total_seconds()) <= tolerance_s:
            out.append((at, path, data))
    out.sort(key=lambda t: (t[0], t[1].name))
    return [(path, data) for _at, path, data in out]


_DETECT_BOOT = object()  # sentinel: "detect the boot time yourself"


def sweep_boundary(claude_dir, boot_utc=_DETECT_BOOT) -> int:
    """Freeze pre-boot entries into the shutdown snapshot and clear them.

    ``boot_utc`` defaults to self-detection. Passing ``None`` explicitly
    means "the boundary is unknowable" and sweeps nothing -- these are
    different situations and conflating them once caused a test to sweep
    against the REAL machine's boot time.

    Runs on the first hook fire after a new boot (piggybacking the
    SessionStart health check). Entries stamped before the current boot
    were open when the machine went down -- a clean close would have
    removed them -- so they become the exact "open at shutdown" record
    for the boundary just crossed, then their files are removed so the
    registry never accumulates like a flag graveyard. Clearing is also
    what keeps each boundary's evidence attributable to THAT boundary:
    an entry left behind would be re-recorded at the next shutdown,
    crediting a session's death to a reboot it did not attend.

    RECORDING and CLEARING are separate decisions, because a session
    RESUMED after the boot answers yes to both questions -- it was open
    at the shutdown (record it) and it is open right now (do NOT delete
    its registration). Deleting one cost a running session two days of
    invisibility on 2026-08-12; recording is keyed on ``started_at``
    (identity, historical) and deletion on ``pid_at`` (liveness).

    One snapshot, overwritten per boundary; deeper history is the
    deferred event log's job. Returns how many entries were RECORDED
    (which is >= the number deleted). Never raises (hook context).
    """
    try:
        if boot_utc is _DETECT_BOOT:
            boot_utc = current_boot_utc()
        if boot_utc is None:
            return 0  # cannot place the boundary; sweep nothing
        entries = read_entries(claude_dir)
        # The historical question -- deliberately NOT split_by_boot,
        # which answers liveness and would drop a resumed session out
        # of the record it belongs in.
        pre_boot = []
        for entry in entries:
            ts = parse_entry_ts(entry.get("started_at"))
            if ts is not None and ts < boot_utc:
                pre_boot.append(entry)
        if not live_dir(claude_dir).is_dir():
            # csb was never watching this machine -- recording "nobody was
            # open" would be an overclaim, not a measurement.
            return 0
        # NOTE: no `if not pre_boot: return 0`. A boundary csb swept and
        # found empty is EVIDENCE ("nobody was open"), and it must not
        # look like a boundary csb never examined. Writing the record
        # unconditionally is what lets the reader tell proven-zero from
        # unknown -- measured: before this, both returned None.
        # THE INVARIANT: a boundary record is SEALED except to ADD.
        #
        # It describes a moment that has already happened -- "was open when
        # the machine went down" -- and nothing observed later can falsify
        # it. So a sweep may add a session to the record; it may never
        # remove one, and never replace the record with a smaller set.
        #
        # This is not a merge for tidiness. Each sweep sees strictly LESS
        # than the one before it (entries it already cleared are gone from
        # disk), so an overwriting sweep walks the record down toward the
        # single session that happened to survive. Eight recorded sessions
        # became one that way, in the wild, one day after v0.9.12 shipped.
        #
        # Ideally the record would simply be written once and never
        # reopened. It is append-only rather than write-once ONLY so that
        # records already damaged in the wild can be repaired; once no
        # such record remains, sealing outright is the simpler design.
        #
        # SUNSET: 2027-02-19 (#92) -- the merge below is a REPAIR PATH and
        # is expected to be a no-op on every healthy machine. Do not read
        # its idleness as deadness: it exists because v0.9.12 shipped a
        # defect that shrank records on real installs, and removing it
        # before those installs have converged turns a recoverable state
        # into permanent evidence loss. Retire it once currency checks
        # (#94) show the fleet past v0.9.12, or on the date above,
        # whichever comes first -- and prefer sealing the record outright
        # at that point rather than leaving a merge nobody exercises.
        covering = covering_records(claude_dir, boot_utc)
        merged = list(pre_boot)
        seen = {_entry_key(e) for e in merged}
        boot_at = _iso(boot_utc)
        for _path, existing in covering:
            # _snapshot_entries never raises on a malformed record --
            # an AttributeError here once reached the hook-context
            # handler below and silently aborted the ENTIRE sweep,
            # permanently, for that boundary (Finding B, 2026-08-19).
            for entry in _snapshot_entries(existing):
                key = _entry_key(entry)
                if key not in seen:
                    merged.append(entry)
                    seen.add(key)
        if covering:
            # Adopt the OLDEST covering record's boot_at -- oldest by
            # CONTENT (covering_records orders by it): the first record
            # to describe a boundary names it. The filename is then
            # re-derived from the adopted boot_at below, so file and
            # content always agree even when the oldest covering record
            # arrived with a lying label.
            adopted = covering[0][1].get("boot_at")
            if adopted:
                boot_at = adopted
        # The filename the surviving record SHOULD have. boot_at is
        # parseable on both paths (covering_records only admits records
        # whose boot_at parses; the fallback is _iso(boot_utc)) -- the
        # `or` guard is belt-and-braces for the hook context.
        bname = _boundary_filename(boot_at) or (
            "boundary-" + boot_utc.astimezone(timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ") + ".json")
        # Already complete? Then LEAVE IT ALONE. The record describes a
        # settled moment, so re-writing identical contents is not merely
        # wasteful -- it churns `captured_at` (which should mean "when
        # csb froze this", not "when csb last looked") and dirties a file
        # that rides every backup commit. In steady state this is the
        # branch that runs, and the merge below never fires at all.
        # A record whose FILENAME disagrees with its own boot_at is not
        # complete -- it gets one healing rewrite, then no-ops forever.
        if len(covering) == 1 and covering[0][0].name == bname:
            existing_ids = {_entry_key(e)
                            for e in _snapshot_entries(covering[0][1])}
            if existing_ids == {_entry_key(e) for e in merged}:
                alias = read_snapshot(claude_dir)
                alias_ids = ({_entry_key(e)
                              for e in _snapshot_entries(alias)}
                             if alias else None)
                if alias_ids == existing_ids:
                    return len(merged)

        snapshot = {
            "version": SNAPSHOT_VERSION,
            "boot_at": boot_at,
            "captured_at": _iso(_utc_now()),
            # _path is cleanup targeting, not evidence -- a record rides
            # git-synced stores across machines, where a local path is
            # noise at best and a layout leak at worst.
            "open_at_shutdown": [
                {k: v for k, v in e.items() if k != "_path"}
                for e in merged],
        }
        payload = json.dumps(snapshot, indent=2) + "\n"
        path = snapshot_path(claude_dir)
        tmp = path.with_suffix(f".tmp-{os.getpid()}")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        # R3 (H3): the same snapshot also lands in boundaries/, keyed by
        # the boot instant, pruned to the newest BOUNDARY_RETENTION --
        # this is what lets `last~N --open` badge exactly within K.
        try:
            bdir = boundary_dir(claude_dir)
            bdir.mkdir(parents=True, exist_ok=True)
            # The FACT names the file: bname derives from the adopted
            # boot_at, and every covering duplicate -- including a
            # covering record whose old filename lied about its own
            # content -- COLLAPSES into it. (Reusing covering[0]'s
            # filename verbatim inherited hand-repair mismatches, and
            # filename-sorted retention then deleted the record in the
            # same call that extended it -- Finding A's amplifier.)
            bpath = bdir / bname
            for stale_path, _d in covering:
                if stale_path == bpath:
                    continue
                try:
                    stale_path.unlink()
                except OSError:
                    pass
            # Derive the temp name from bpath (one source of truth --
            # a branch-local variable referenced here once raised a
            # NameError that this function's hook-context `except
            # Exception` turned into a silent no-op).
            btmp = bpath.with_suffix(
                f".tmp-{os.getpid()}-{threading.get_ident()}")
            btmp.write_text(payload, encoding="utf-8")
            try:
                os.replace(btmp, bpath)
            except OSError:
                try:
                    btmp.unlink()
                except OSError:
                    pass
                # boundary history is best-effort; the loser's content
                # is equivalent to the winner's -- do not re-raise

            # Retention prefers EVIDENCE over recency. Now that empty
            # records are written, a purely chronological prune would
            # evict a boundary that still names open sessions in favour
            # of newer boundaries that name nobody -- measured, not
            # feared: a probe watched four empty records push a real one
            # out of the K slots. So: keep the newest K non-empty
            # records first, then fill any remaining slots with the
            # newest empty ones. An unreadable record carries no
            # recoverable evidence and is evicted with the empties.
            #
            # "Newest" comes from each record's own boot_at (falling
            # back to the filename stamp) -- the FACT, not the label.
            # Retention once sorted by filename and deleted a record
            # whose label lied about its era in the same call that had
            # just extended it (Finding A). A record with names but no
            # locatable instant classifies with the empties: evidence
            # that cannot be placed cannot be ranked.
            nonempty: list[tuple] = []
            empty: list[tuple] = []
            for p in sorted(bdir.glob("boundary-*.json")):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = None
                instant = _record_instant(p, data)
                names = _snapshot_entries(data)
                item = ((instant or _EPOCH_UTC, p.name), p)
                (nonempty if (names and instant is not None)
                 else empty).append(item)
            nonempty.sort(key=lambda t: t[0])
            empty.sort(key=lambda t: t[0])
            keep = {p for _k, p in nonempty[-BOUNDARY_RETENTION:]}
            if merged:
                # Never delete a record this call just extended. On
                # real machines the current boundary is content-newest
                # and already kept; this guard only matters for stores
                # holding future-dated records (hand edits, clock
                # jumps), where it may briefly hold K+1 -- losing the
                # freshest evidence to a rigid K would be the worse bug.
                keep.add(bpath)
            slots = BOUNDARY_RETENTION - len(keep)
            if slots > 0:
                keep.update(p for _k, p in empty[-slots:])
            for _k, stale in nonempty + empty:
                if stale in keep:
                    continue
                try:
                    stale.unlink()
                except OSError:
                    pass
        except OSError:
            pass  # boundary history is an extra; the alias already wrote
        # Clear the board -- EXCEPT for sessions csb confirmed alive on
        # this side of the boot. Their entry is now in the record above
        # (they were open at the shutdown) AND still on disk (they are
        # open now). Both statements are true; neither may evict the
        # other.
        ldir = live_dir(claude_dir).resolve()
        for entry in pre_boot:
            if seen_alive_after_boot(entry, boot_utc):
                continue
            # Unlink the file that was READ (#90). Rebuilding the path
            # from the body-claimed id unlinked a name that never
            # existed whenever filename and body disagreed -- silently
            # (FileNotFoundError is an OSError), so the entry was
            # re-recorded at every future boundary, forever. Identity
            # and location are separate questions: the body's safe id
            # remains the session's IDENTITY in the record above; the
            # path read is what cleanup targets. The containment
            # re-check stays -- entries ride git-synced stores, and a
            # path is deleted only if it still resolves inside the
            # registry directory.
            try:
                target = entry.get("_path")
                if target:
                    p = Path(target).resolve()
                    if p.parent == ldir:
                        p.unlink()
                else:  # entries from older callers without _path
                    entry_path(claude_dir, entry["session_id"]).unlink()
            except OSError:
                pass
        return len(merged)
    except Exception:  # noqa: BLE001 -- hook context; never break a session
        return 0


def open_at_shutdown(claude_dir, boot_utc: Optional[datetime] = None,
                     shutdown_utc: Optional[datetime] = None,
                     tolerance_s: int = _SNAPSHOT_TOLERANCE_S
                     ) -> Optional[list[dict]]:
    """Who was open at a boundary: the persisted snapshot, or derived.

    The snapshot is a DURABLE COPY of something the registry still knows
    until the sweep clears it. Between a reboot and the first sweep that
    copy does not exist, and a reader consulting only the snapshot shows
    a badge-less roster -- which is exactly how `csb set last` came to be
    silently wrong for 27 minutes after the 2026-08-12 restart, with the
    evidence sitting unread on disk the whole time.

    So: the snapshot when one covers the boundary; otherwise, when the
    boundary IS the current boot, the same answer derived from entries
    still on disk. **Read-only** -- deriving never writes, so a display
    path stays a display path and two concurrent readers cannot race.

    Only the CURRENT boot may be derived. An entry records when a session
    opened, never which boundary it died at, so deriving for an older
    epoch would credit every unswept entry to the wrong shutdown.

    Returns None when the answer is genuinely unknown -- callers must
    render that as ignorance, never as "nobody was open".
    """
    snapshot = read_snapshot(claude_dir, boot_utc=boot_utc,
                             shutdown_utc=shutdown_utc,
                             tolerance_s=tolerance_s)
    if snapshot is not None:
        # Tolerant read: display callers iterate these as dicts, and a
        # malformed member must degrade to absence, not a traceback.
        return _snapshot_entries(snapshot)
    if boot_utc is None:
        return None
    current = current_boot_utc()
    if current is None:
        return None
    if abs((boot_utc - current).total_seconds()) > tolerance_s:
        return None  # an older boundary -- only a snapshot can answer
    if not live_dir(claude_dir).is_dir():
        return None  # csb was never watching; claiming zero would lie
    derived: list[dict] = []
    for entry in read_entries(claude_dir):
        ts = parse_entry_ts(entry.get("started_at"))
        if ts is not None and ts < current:
            # _path never leaves the module (cleanup targeting only).
            derived.append({k: v for k, v in entry.items()
                            if k != "_path"})
    return derived or None


def read_snapshot(claude_dir, boot_utc: Optional[datetime] = None,
                  shutdown_utc: Optional[datetime] = None,
                  tolerance_s: int = _SNAPSHOT_TOLERANCE_S
                  ) -> Optional[dict]:
    """A boundary snapshot, or None. Corrupt -> None (advisory data).

    Without arguments: the newest snapshot (the legacy alias file) --
    v0.8.x behavior, unchanged. With ``boot_utc`` (R3): the snapshot
    whose ``boot_at`` sits within ``tolerance_s`` of that instant,
    searched across the alias AND the retained ``boundaries/`` history.

    With ``shutdown_utc``, a second pass covers CLAUDE-LESS REBOOT RUNS
    (H9): the sweep fires at the first hook AFTER a boot, so N Claude-
    less boots leave the testimony keyed to boot N+1 instead of the
    epoch that earned it. Read-time re-keying is sound: take the OLDEST
    snapshot newer than the epoch's shutdown whose entries ALL started
    before that shutdown -- had Claude run in any intermediate epoch,
    its hooks would have swept earlier (making THAT the oldest-newer
    snapshot) or contributed a younger entry (breaking the condition).
    Unparseable entry timestamps fail the condition -- honest absence
    over a guessed badge.
    """
    candidates: list = []
    for path in [snapshot_path(claude_dir)] + (
            sorted(boundary_dir(claude_dir).glob("boundary-*.json"),
                   reverse=True)
            if (boot_utc is not None or shutdown_utc is not None)
            else []):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict) or not isinstance(
                raw.get("open_at_shutdown"), list):
            continue
        if boot_utc is None and shutdown_utc is None:
            return raw
        candidates.append(raw)

    if boot_utc is not None:
        for raw in candidates:
            snap_boot = parse_entry_ts(raw.get("boot_at"))
            if snap_boot is not None and abs(
                    (snap_boot - boot_utc).total_seconds()) <= tolerance_s:
                return raw

    if shutdown_utc is not None:
        newer = []
        for raw in candidates:
            snap_boot = parse_entry_ts(raw.get("boot_at"))
            if snap_boot is not None and snap_boot > shutdown_utc:
                newer.append((snap_boot, raw))
        if newer:
            _boot, oldest_newer = min(newer, key=lambda t: t[0])
            starts = [parse_entry_ts(e.get("started_at"))
                      for e in _snapshot_entries(oldest_newer)]
            if starts and all(s is not None and s <= shutdown_utc
                              for s in starts):
                return oldest_newer
    return None

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
import sys
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


def live_dir(claude_dir) -> Path:
    """The registry directory for a claude dir (not auto-created)."""
    return Path(claude_dir) / LIVE_DIRNAME


def entry_path(claude_dir, session_id: str) -> Path:
    return live_dir(claude_dir) / f"{session_id}.json"


def snapshot_path(claude_dir) -> Path:
    return live_dir(claude_dir) / SNAPSHOT_FILENAME


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
                         cwd: str = "") -> bool:
    """Write the entry for a starting session. Returns True if written.

    Write-if-missing: a ``source=compact`` restart of the same session
    must not reset ``started_at`` -- the session has been open the whole
    time. Never raises (hook context): a failed write costs one
    observation, not a session.
    """
    if not session_id:
        return False
    try:
        path = entry_path(claude_dir, session_id)
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session_id,
            "started_at": _iso(_utc_now()),
            "source": source or "",
            "cwd": cwd or "",
        }
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
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                started_at = raw.get("started_at")
                source = raw.get("source") or ""
                cwd = raw.get("cwd") or ""
                session_id = raw.get("session_id") or session_id
        except (OSError, json.JSONDecodeError):
            pass
        entries.append({
            "session_id": session_id,
            "started_at": started_at,
            "source": source,
            "cwd": cwd,
        })
    entries.sort(key=lambda e: (e["started_at"] is None,
                                e["started_at"] or "", e["session_id"]))
    return entries


def split_by_boot(entries: list[dict], boot_utc: Optional[datetime]
                  ) -> tuple[list[dict], list[dict]]:
    """(this_boot, pre_boot). Unknown boot time -> everything this_boot.

    Erring toward this_boot when the boundary is unknowable is the safe
    direction: a pre-boot entry misfiled as current shows as "no exit
    observed" (true), while a current entry misfiled as pre-boot would
    be swept into the shutdown snapshot while its session is still open.
    Entries with unparseable timestamps stay this_boot for the same
    reason.
    """
    if boot_utc is None:
        return list(entries), []
    this_boot: list[dict] = []
    pre_boot: list[dict] = []
    for entry in entries:
        ts = parse_entry_ts(entry.get("started_at"))
        if ts is not None and ts < boot_utc:
            pre_boot.append(entry)
        else:
            this_boot.append(entry)
    return this_boot, pre_boot


# ── the boundary sweep ───────────────────────────────────────────────────

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
    registry never accumulates like a flag graveyard.

    One snapshot, overwritten per boundary; deeper history is the
    deferred event log's job. Returns how many entries were swept.
    Never raises (hook context).
    """
    try:
        if boot_utc is _DETECT_BOOT:
            boot_utc = current_boot_utc()
        if boot_utc is None:
            return 0  # cannot place the boundary; sweep nothing
        _, pre_boot = split_by_boot(read_entries(claude_dir), boot_utc)
        if not pre_boot:
            return 0
        snapshot = {
            "version": SNAPSHOT_VERSION,
            "boot_at": _iso(boot_utc),
            "captured_at": _iso(_utc_now()),
            "open_at_shutdown": pre_boot,
        }
        path = snapshot_path(claude_dir)
        tmp = path.with_suffix(f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(snapshot, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
        for entry in pre_boot:
            try:
                entry_path(claude_dir, entry["session_id"]).unlink()
            except OSError:
                pass
        return len(pre_boot)
    except Exception:  # noqa: BLE001 -- hook context; never break a session
        return 0


def read_snapshot(claude_dir) -> Optional[dict]:
    """The last-shutdown snapshot, or None. Corrupt -> None (advisory data)."""
    path = snapshot_path(claude_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not isinstance(
            raw.get("open_at_shutdown"), list):
        return None
    return raw

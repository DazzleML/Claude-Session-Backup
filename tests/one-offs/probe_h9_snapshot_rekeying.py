#!/usr/bin/env python3
"""Probe: H9 snapshot re-keying (read_snapshot(shutdown_utc=...)) exercised
directly against live_registry, independent of the pytest suite -- part of
the DELTA sweep verifying the H8/H9 review-round addenda (design:
2026-08-07__07-20-42__dev-workflow-process__r3-epoch-history-and-promotion.md).

Mirrors tests/test_epoch_history.py::TestSnapshotEpochReKeying (reattach,
younger-entry-block, boot-keyed-priority -- all pytest-green already) and
ADDS a two-snapshot case the pytest suite does not cover: two boundary
files both newer than an epoch's shutdown, verifying the OLDEST-newer one
wins re-attachment, not the newest.

Read-only against the real store; all state lives under a temp claude_dir
passed in by the caller. No csb CLI, no subprocess, no real ~/.claude
touched. Usage:  python tests/one-offs/probe_h9_snapshot_rekeying.py
"""

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import claude_session_backup.live_registry as lr  # noqa: E402

UUID_A = "eeee1111-bbbb-cccc-dddd-000000000001"
UUID_B = "eeee2222-bbbb-cccc-dddd-000000000002"
UUID_OLDER_NEWER = "eeee5555-bbbb-cccc-dddd-000000000005"
UUID_NEWEST = "eeee6666-bbbb-cccc-dddd-000000000006"

SHUTDOWN = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
LATE_BOOT = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)

results = []


def check(label, condition):
    results.append((label, bool(condition)))
    tag = "PASS" if condition else "FAIL"
    print(f"[{tag}] {label}")


def fresh_dir():
    d = Path(tempfile.mkdtemp(prefix="csb_h9_probe_"))
    return d


def sweep_late(claude_dir, sid, started_at, boot_utc=LATE_BOOT):
    lr.live_dir(claude_dir).mkdir(parents=True, exist_ok=True)
    lr.entry_path(claude_dir, sid).write_text(
        json.dumps({"session_id": sid, "started_at": lr._iso(started_at)}),
        encoding="utf-8",
    )
    swept = lr.sweep_boundary(claude_dir, boot_utc)
    assert swept == 1, f"expected 1 swept, got {swept}"


# ── Case 1: reattach -- boot-keyed lookup misses, shutdown-keyed finds it ──
def case_reattach():
    claude_dir = fresh_dir()
    try:
        sweep_late(claude_dir, UUID_A, SHUTDOWN - timedelta(hours=5))
        old_boot = SHUTDOWN + timedelta(seconds=40)
        boot_keyed = lr.read_snapshot(claude_dir, boot_utc=old_boot)
        check("case1: boot-keyed lookup at old epoch's own boot finds nothing",
              boot_keyed is None)
        snap = lr.read_snapshot(claude_dir, boot_utc=old_boot,
                                shutdown_utc=SHUTDOWN)
        check("case1: shutdown-keyed second pass reattaches the snapshot",
              snap is not None)
        if snap is not None:
            check("case1: reattached snapshot carries the right entry",
                  snap["open_at_shutdown"][0]["session_id"] == UUID_A)
    finally:
        shutil.rmtree(claude_dir, ignore_errors=True)


# ── Case 2: younger entry blocks reattachment ──────────────────────────────
def case_younger_blocks():
    claude_dir = fresh_dir()
    try:
        sweep_late(claude_dir, UUID_A, SHUTDOWN + timedelta(days=1))
        snap = lr.read_snapshot(
            claude_dir, boot_utc=SHUTDOWN + timedelta(seconds=40),
            shutdown_utc=SHUTDOWN)
        check("case2: entry started AFTER shutdown blocks reattachment "
              "(honest absence)", snap is None)
    finally:
        shutil.rmtree(claude_dir, ignore_errors=True)


# ── Case 3: boot-keyed match still wins over shutdown-keyed fallback ──────
def case_boot_keyed_wins():
    claude_dir = fresh_dir()
    try:
        lr.live_dir(claude_dir).mkdir(parents=True, exist_ok=True)
        lr.entry_path(claude_dir, UUID_B).write_text(json.dumps(
            {"session_id": UUID_B,
             "started_at": lr._iso(SHUTDOWN - timedelta(hours=2))}),
            encoding="utf-8")
        next_boot = SHUTDOWN + timedelta(seconds=45)
        swept = lr.sweep_boundary(claude_dir, next_boot)
        assert swept == 1
        snap = lr.read_snapshot(claude_dir, boot_utc=next_boot,
                                shutdown_utc=SHUTDOWN)
        check("case3: normal boot-keyed sweep still resolves",
              snap is not None)
        if snap is not None:
            check("case3: boot_at matches the NEXT boot exactly (not "
                  "re-keyed -- boot-keyed match wins first)",
                  snap["boot_at"] == lr._iso(next_boot))
    finally:
        shutil.rmtree(claude_dir, ignore_errors=True)


# ── Case 4 (NEW): two boundary files both newer than shutdown -- oldest
# of the two must win, not the newest. ─────────────────────────────────────
def case_two_snapshots_oldest_newer_wins():
    claude_dir = fresh_dir()
    try:
        # SNAP_A: the OLDER of the two "newer than shutdown" snapshots.
        boot_a = SHUTDOWN + timedelta(days=3)
        sweep_late(claude_dir, UUID_OLDER_NEWER, SHUTDOWN - timedelta(hours=2),
                  boot_utc=boot_a)
        # SNAP_B: the NEWEST snapshot -- also satisfies the all-started-
        # before-shutdown condition on its own, so a naive "pick newest"
        # implementation would return a plausible-looking WRONG answer
        # instead of failing loudly. This is the case that actually
        # discriminates oldest-newer-wins from newest-wins.
        boot_b = SHUTDOWN + timedelta(days=10)
        sweep_late(claude_dir, UUID_NEWEST, SHUTDOWN - timedelta(hours=1),
                  boot_utc=boot_b)

        files = sorted(lr.boundary_dir(claude_dir).glob("boundary-*.json"))
        check("case4 setup: two boundary files exist",
              len(files) == 2)

        snap = lr.read_snapshot(claude_dir, shutdown_utc=SHUTDOWN)
        check("case4: shutdown-keyed lookup with two newer candidates "
              "returns a snapshot", snap is not None)
        if snap is not None:
            check("case4: boot_at is the OLDER of the two newer boots "
                  "(not the newest)", snap["boot_at"] == lr._iso(boot_a))
            sids = {e["session_id"] for e in snap["open_at_shutdown"]}
            check("case4: reattached snapshot's entry is the OLDER-newer "
                  "one's session, not the newest one's",
                  sids == {UUID_OLDER_NEWER})
    finally:
        shutil.rmtree(claude_dir, ignore_errors=True)


def main():
    print("=" * 78)
    print("H9 probe: read_snapshot(shutdown_utc=...) re-keying, direct "
          "against live_registry")
    print("=" * 78)
    case_reattach()
    case_younger_blocks()
    case_boot_keyed_wins()
    case_two_snapshots_oldest_newer_wins()
    print("=" * 78)
    failed = [label for label, ok in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED:")
        for label in failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

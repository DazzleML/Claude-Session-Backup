"""Probe: does sweep_boundary DELETE a live session's registration?

The DWP (2026-08-14__16-57-27__...__boundary-sweep-liveness-and-trigger.md)
claims a resumed pre-boot session is destroyed by the sweep because
``split_by_boot`` keys on ``started_at`` and ignores ``pid_at``.

That claim rests on FORENSICS -- a boundary snapshot and two log lines from
the 2026-08-12 forced restart. Forensics are consistent with a hypothesis;
they do not prove a mechanism. This probe reproduces the state in a temp
fixture and runs the REAL code, so the claim becomes verified or dies.

Three entry shapes, one boot instant between them:

  A. pre-boot started_at, NO pid_at          -> genuinely dead; must be swept
  B. pre-boot started_at, PRE-boot pid_at    -> dead, pid seen before boot
  C. pre-boot started_at, POST-boot pid_at   -> THE VICTIM: csb saw its host
                                                alive AFTER the boot
  D. post-boot started_at                    -> normal live session

Expected if the DWP is right: C is swept and deleted exactly like A and B,
even though its own record proves it was alive on this side of the boundary.

Run:  python tests/one-offs/thinking/probe_boundary_sweep_eats_live_entry.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from claude_session_backup import live_registry as lr  # noqa: E402

BOOT = datetime(2026, 8, 12, 15, 56, 25, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(live: Path, sid: str, started: datetime, pid_at: datetime | None):
    payload = {
        "session_id": sid,
        "started_at": _iso(started),
        "source": "resume",
        "cwd": "C:\\code\\probe",
    }
    if pid_at is not None:
        payload["pid"] = 4242
        payload["pid_at"] = _iso(pid_at)
    (live / f"{sid}.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="csb-sweep-probe-"))
    live = tmp / "csb-live"
    live.mkdir(parents=True)

    cases = {
        # sid                     started_at        pid_at
        "aaaa-dead-no-pidat":    (BOOT - timedelta(days=1), None),
        "bbbb-dead-pre-pidat":   (BOOT - timedelta(days=1), BOOT - timedelta(hours=2)),
        "cccc-LIVE-post-pidat":  (BOOT - timedelta(days=1), BOOT + timedelta(minutes=27)),
        "dddd-normal-live":      (BOOT + timedelta(minutes=5), BOOT + timedelta(minutes=5)),
    }
    for sid, (started, pid_at) in cases.items():
        _write(live, sid, started, pid_at)

    print(f"fixture: {tmp}")
    print(f"boot   : {_iso(BOOT)}\n")

    # --- what does split_by_boot decide? ---------------------------------
    entries = lr.read_entries(tmp)
    this_boot, pre_boot = lr.split_by_boot(entries, BOOT)
    print("split_by_boot:")
    print("  this_boot:", sorted(e["session_id"] for e in this_boot))
    print("  pre_boot :", sorted(e["session_id"] for e in pre_boot))

    # --- run the real sweep ----------------------------------------------
    swept = lr.sweep_boundary(tmp, boot_utc=BOOT)
    print(f"\nsweep_boundary returned: {swept}")

    survivors = sorted(p.stem for p in live.glob("*.json")
                       if p.name != "last-shutdown.json")
    print("files surviving        :", survivors)

    snap = json.loads((live / "last-shutdown.json").read_text())
    recorded = sorted(e["session_id"] for e in snap["open_at_shutdown"])
    print("recorded in snapshot   :", recorded)

    # --- the verdict ------------------------------------------------------
    victim = "cccc-LIVE-post-pidat"
    deleted = victim not in survivors
    in_snap = victim in recorded
    print("\n" + "=" * 62)
    print(f"VICTIM {victim!r}")
    print(f"  file deleted by sweep : {deleted}")
    print(f"  recorded as dead      : {in_snap}")
    if deleted:
        print("\n  >>> BUG PRESENT: the sweep deletes a registration whose")
        print("      own pid_at proves it was alive AFTER the boot.")
        print("      (This is what the probe reported on 2026-08-16,")
        print("      against v0.9.11 -- it is how the DWP stopped being")
        print("      a hunch and became a reproduction.)")
    elif in_snap:
        print("\n  >>> FIXED: the live entry survived the sweep AND is")
        print("      still recorded as open at the shutdown. Both truths")
        print("      held -- neither evicted the other.")
    else:
        print("\n  >>> HALF-FIXED, REGRESSION: the entry survived but is")
        print("      NO LONGER recorded as open at shutdown. The delete")
        print("      was fixed by breaking the record -- exactly the")
        print("      trade Option C was rejected for. Re-open.")
    print("=" * 62)

    # control: the genuinely-dead ones must still be swept (no over-fix later)
    for ok in ("aaaa-dead-no-pidat", "bbbb-dead-pre-pidat"):
        assert ok not in survivors, f"control failed: {ok} should be swept"
        assert ok in recorded, f"control failed: {ok} should be recorded"
    assert "dddd-normal-live" in survivors, "control failed: live entry swept"
    print("controls OK: dead entries swept, post-boot entry untouched.")
    return 0 if deleted else 1


if __name__ == "__main__":
    raise SystemExit(main())

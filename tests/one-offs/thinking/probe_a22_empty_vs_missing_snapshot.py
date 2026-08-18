"""Probe A2.2: can csb tell "nobody was open" from "I never looked"?

F now warns when no snapshot covers an epoch. But TWO different worlds
produce "no snapshot":

  W1. csb never swept this boundary        -> genuinely UNKNOWN
  W2. csb swept and found nobody open      -> PROVEN ZERO

Today both look identical, because sweep_boundary returns early without
writing anything when nothing is pre-boot. A2.2 proposes writing the
record even when empty, so W2 becomes provable.

Before implementing, measure what actually happens now and what an
empty record would collide with:

  Q1  What does each layer return in W1 vs W2 today?
  Q2  Does open_at_shutdown() ALREADY distinguish them if a record
      exists? (it returns [] for empty, None for absent -- but does
      anything ever create the empty case?)
  Q3  What does commands.py's `snapshot_available = bool(snapshot_ids)`
      do with an empty record -- does it collapse W2 back into W1?
  Q4  RETENTION: if empty records consume the K=5 boundary slots, do
      they evict real evidence that `last~N --open` depends on?

Q4 is the one that could turn a clarity fix into evidence loss.

Run:  python tests/one-offs/thinking/probe_a22_empty_vs_missing_snapshot.py
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


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fixture(with_dir=True, entries=()):
    tmp = Path(tempfile.mkdtemp(prefix="csb-a22-"))
    if with_dir:
        live = lr.live_dir(tmp)
        live.mkdir(parents=True)
        for sid, started in entries:
            (live / f"{sid}.json").write_text(json.dumps({
                "session_id": sid, "started_at": started,
                "source": "resume", "cwd": "C:/w"}), encoding="utf-8")
    return tmp


def q1_q2_q3():
    print("=" * 70)
    print("Q1-Q3: what each layer says in W1 (never looked) vs W2 (zero)")
    print("=" * 70)
    worlds = {
        "W0 no registry dir at all": _fixture(with_dir=False),
        "W1 dir, never swept": _fixture(entries=[]),
        "W2 dir, swept, nobody open": _fixture(entries=[]),
        "W3 dir, swept, one was open": _fixture(
            entries=[("was-open", _iso(BOOT - timedelta(days=1)))]),
    }
    lr.sweep_boundary(worlds["W2 dir, swept, nobody open"], BOOT)
    lr.sweep_boundary(worlds["W3 dir, swept, one was open"], BOOT)

    print(f"{'world':<32} {'read_snapshot':<16} {'open_at_shutdown':<18}")
    print("-" * 70)
    for name, root in worlds.items():
        snap = lr.read_snapshot(root, boot_utc=BOOT)
        try:
            opened = lr.open_at_shutdown(root, boot_utc=BOOT)
        except Exception as e:  # noqa: BLE001
            opened = f"ERR {e}"
        snap_s = "None" if snap is None else \
            f"{len(snap.get('open_at_shutdown', []))} entries"
        open_s = ("None (unknown)" if opened is None
                  else f"[] (proven zero)" if opened == []
                  else f"{len(opened)} entries")
        print(f"{name:<32} {snap_s:<16} {open_s:<18}")

    print("\nQ3: commands.py computes `snapshot_available = bool(ids)`.")
    print("    An EMPTY record yields an empty id set -> False -> csb")
    print("    would print the 'UNKNOWN' warning for a boundary it")
    print("    actually proved was empty. A2.2 therefore REQUIRES")
    print("    changing that flag from bool(ids) to (entries is not None).")


def q4_retention():
    print("\n" + "=" * 70)
    print("Q4: would empty records evict real evidence? (K="
          f"{lr.BOUNDARY_RETENTION})")
    print("=" * 70)
    root = _fixture(entries=[])
    bdir = lr.boundary_dir(root)
    bdir.mkdir(parents=True, exist_ok=True)

    # One REAL boundary, oldest, then K empty ones after it.
    def write(stamp, payload_entries):
        (bdir / f"boundary-{stamp}.json").write_text(json.dumps({
            "version": lr.SNAPSHOT_VERSION,
            "boot_at": stamp, "captured_at": stamp,
            "open_at_shutdown": payload_entries}), encoding="utf-8")

    write("20260801T000000Z", [{"session_id": "IMPORTANT"}])
    for i in range(1, lr.BOUNDARY_RETENTION + 1):
        write(f"2026080{i+1}T000000Z", [])

    kept_before = sorted(p.name for p in bdir.glob("boundary-*.json"))
    print(f"before prune ({len(kept_before)} files): {kept_before}")

    # Trigger the existing retention path with a fresh sweep.
    live = lr.live_dir(root)
    (live / "dead.json").write_text(json.dumps({
        "session_id": "dead", "started_at": _iso(BOOT - timedelta(days=1)),
        "source": "resume", "cwd": "C:/w"}), encoding="utf-8")
    lr.sweep_boundary(root, BOOT)

    kept_after = sorted(p.name for p in bdir.glob("boundary-*.json"))
    print(f"after prune  ({len(kept_after)} files): {kept_after}")
    survived = any(
        "IMPORTANT" in (bdir / n).read_text(encoding="utf-8")
        for n in kept_after)
    print(f"\nreal evidence survived: {survived}")
    if not survived:
        print("  >>> CONFIRMED RISK: empty records evicted a boundary that")
        print("      still had real open-at-shutdown evidence in it.")
        print("      A2.2 MUST prune empty records before non-empty ones.")
    else:
        print("  >>> Real evidence survived this ordering -- but note the")
        print("      prune is filename-sorted (chronological), so the")
        print("      risk depends purely on age, not on content.")


if __name__ == "__main__":
    q1_q2_q3()
    q4_retention()

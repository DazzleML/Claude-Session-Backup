"""Adversarial probe (vector #8 x #5): a boundary record whose FILENAME
disagrees with its own `boot_at` content -- exactly the shape a hand
repair (or a foreign/older csb build) can produce, which is literally
what happened on the real machine per the dispatch prompt's DANGER
SURFACE section -- sitting among >= BOUNDARY_RETENTION other nonempty
records. Retention keeps `sorted(nonempty)[-BOUNDARY_RETENTION:]`, i.e.
newest BY FILENAME. If the mismatched record's filename sorts far
earlier than its true chronological position, does retention prune it
away even though it is the CURRENT boundary the sweep just extended?

SCRATCH ONLY. Never touches the real ~/.claude registry.

Confirmed (2026-08-19) that the retention-sort-by-filename root cause
predates v0.9.13: an equivalent standalone reproduction against the
v0.9.12-shipped ``live_registry.py`` (commit 8b88c771, no merge/
``covering_records`` logic at all) shows ordinary retention pressure
alone -- no merge involved -- prunes a pre-existing mismatched-filename
record just as readily. v0.9.13's merge step does not introduce this
gap; it inherits it. Write-up:
``private/claude/checklists/results/v0.9.13__boundary-keying__results__2026-08-19.md``.

RESOLVED (2026-08-19, same day): after the Finding A fix (retention
orders by content ``boot_at``; the sweep heals a lying filename), this
probe's expected verdict is ``REVIEW -- record renamed/collapsed
elsewhere, names preserved``: the mismatched FILENAME is gone by design
(healed to ``boundary-<boot_at>.json``) and all 9 names survive in the
healed record. ``LOST`` would mean regression. Pinned permanently in
``TestBoundaryKeyDriftAdversarial`` and ``TestRetentionContentOrdering``.

Run:  python tests/one-offs/thinking/probe_mismatched_filename_retention.py
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


def build_fixture():
    root = Path(tempfile.mkdtemp(prefix="csb-mismatch-"))
    live = lr.live_dir(root)
    live.mkdir(parents=True)
    bdir = lr.boundary_dir(root)
    bdir.mkdir(parents=True)

    # The mismatched record: filename claims a boundary from a year
    # earlier (as a hand-repair or foreign write might produce), but its
    # own `boot_at` content correctly names the CURRENT boundary (BOOT)
    # and already carries evidence -- like the real 8-session record.
    mismatched_name = "boundary-20250101T000000Z.json"
    (bdir / mismatched_name).write_text(json.dumps({
        "version": lr.SNAPSHOT_VERSION,
        "boot_at": _iso(BOOT),
        "captured_at": _iso(BOOT),
        "open_at_shutdown": [{"session_id": f"real-session-{i}"}
                             for i in range(8)],
    }), encoding="utf-8")

    # BOUNDARY_RETENTION (5) other nonempty records for DIFFERENT,
    # genuinely later boundaries, all self-consistently named -- an
    # ordinary week of subsequent restarts.
    for i in range(lr.BOUNDARY_RETENTION):
        other_boot = BOOT + timedelta(days=i + 1)
        stamp = other_boot.strftime("%Y%m%dT%H%M%SZ")
        (bdir / f"boundary-{stamp}.json").write_text(json.dumps({
            "version": lr.SNAPSHOT_VERSION,
            "boot_at": _iso(other_boot),
            "captured_at": _iso(other_boot),
            "open_at_shutdown": [{"session_id": f"other-{i}"}],
        }), encoding="utf-8")

    # A pre-boot entry still on disk for the CURRENT (mismatched-name)
    # boundary -- a session not yet folded into that record, so the
    # sweep has real work to do and must touch this record.
    lr.entry_path(root, "new-session-not-yet-recorded").write_text(
        json.dumps({
            "session_id": "new-session-not-yet-recorded",
            "started_at": _iso(BOOT - timedelta(hours=1)),
            "source": "startup", "cwd": "C:/w",
        }), encoding="utf-8")

    return root, mismatched_name


def main() -> int:
    root, mismatched_name = build_fixture()
    print(f"scratch root: {root}")
    before = sorted(p.name for p in lr.boundary_dir(root).glob("*.json"))
    print(f"before sweep ({len(before)} records):")
    for n in before:
        print(f"  {n}")

    n = lr.sweep_boundary(root, BOOT)
    print(f"\nsweep_boundary returned: {n}")

    after_paths = sorted(lr.boundary_dir(root).glob("*.json"))
    after = [p.name for p in after_paths]
    print(f"\nafter sweep ({len(after)} records):")
    for n in after:
        print(f"  {n}")

    survived = mismatched_name in after
    print(f"\nmismatched-name record survived retention: {survived}")

    if survived:
        data = json.loads((lr.boundary_dir(root) / mismatched_name)
                          .read_text(encoding="utf-8"))
        ids = sorted(e["session_id"] for e in data["open_at_shutdown"])
        expected = sorted([f"real-session-{i}" for i in range(8)]
                          + ["new-session-not-yet-recorded"])
        print(f"  names in it: {ids}")
        print(f"  expected:    {expected}")
        lost = set(expected) - set(ids)
        print(f"  lost names: {sorted(lost) if lost else 'NONE'}")
        verdict = "PASS -- record survived and lost nothing" if not lost \
            else "FAIL -- record survived but LOST NAMES"
    else:
        # Was it collapsed into another surviving record (fine) or
        # deleted outright with its evidence gone (a real defect)?
        found_elsewhere = False
        for p in after_paths:
            d = json.loads(p.read_text(encoding="utf-8"))
            ids = {e["session_id"] for e in d.get("open_at_shutdown", [])}
            if "real-session-0" in ids:
                found_elsewhere = True
                print(f"  8 real-session-* names found instead in: {p.name}")
                print(f"  ids: {sorted(ids)}")
        verdict = ("REVIEW -- record renamed/collapsed elsewhere, "
                   "names preserved" if found_elsewhere else
                   "FAIL -- record deleted by retention, 8+1 session "
                   "names LOST")

    print(f"\n>>> VERDICT: {verdict}")
    return 0 if verdict.startswith(("PASS", "REVIEW")) else 1


if __name__ == "__main__":
    raise SystemExit(main())

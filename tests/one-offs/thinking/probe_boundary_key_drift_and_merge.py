"""POC: does merge-on-write (E) + key-reuse (A) actually fix the drift bug?

The DWP recommends E+A. This proves the approach against a faithful model
of the observed failure BEFORE any production code changes.

THE OBSERVED FAILURE (real machine, 2026-08-19):

    boundary-20260812T155617Z.json  entries=1   <- newer, drifted key
    boundary-20260812T155625Z.json  entries=8   <- original
    last-shutdown.json (alias)      entries=1   <- OVERWRITTEN with the lesser

Why the second record holds fewer entries, faithfully modelled below:

  * entry A -- pre-boot started_at, no pid_at   -> swept AND deleted
  * entry B -- pre-boot started_at, post-boot pid_at (a resumed session)
               -> recorded, but NOT deleted (v0.9.12's fix keeps it alive)

  sweep 1 at T      records {A, B}, deletes A, keeps B
  sweep 2 at T-8s   only B remains to record -> new key, alias = {B}

So the degradation is not random: each later sweep sees strictly less,
and the alias takes the smallest. That is the mechanism to beat.

The proposed logic is implemented HERE, not in the package, so the
approach can be judged before it is committed to.

Run:  python tests/one-offs/thinking/probe_boundary_key_drift_and_merge.py
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
DRIFTED = BOOT - timedelta(seconds=8)      # what the machine derived 2 days later
TOLERANCE_S = 600


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fixture():
    """A registry in the exact shape that produced the real failure."""
    tmp = Path(tempfile.mkdtemp(prefix="csb-drift-"))
    live = lr.live_dir(tmp)
    live.mkdir(parents=True)
    # A: genuinely dead -- swept and removed on the first sweep.
    (live / "dead-A.json").write_text(json.dumps({
        "session_id": "dead-A", "started_at": _iso(BOOT - timedelta(days=1)),
        "source": "startup", "cwd": "C:/w"}), encoding="utf-8")
    # B: resumed session -- recorded every time, never removed.
    (live / "live-B.json").write_text(json.dumps({
        "session_id": "live-B", "started_at": _iso(BOOT - timedelta(days=1)),
        "source": "resume", "cwd": "C:/w",
        "pid": 11516, "pid_at": _iso(BOOT + timedelta(minutes=27))}),
        encoding="utf-8")
    return tmp


def _state(root):
    bdir = lr.boundary_dir(root)
    recs = {}
    for f in sorted(bdir.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        recs[f.name] = sorted(e["session_id"] for e in d["open_at_shutdown"])
    alias = lr.read_snapshot(root)
    return recs, sorted(e["session_id"] for e in alias["open_at_shutdown"]) if alias else None


# ── the PROPOSED logic, implemented here for evaluation ──────────────────

def _covering_records(bdir: Path, boot_utc):
    """(A) EVERY existing record describing this boundary, oldest key first.

    Plural, and that is the whole correction. The first attempt returned
    the FIRST match in filename order; on a machine that already held
    duplicates it picked the lesser record, unioned against that, and
    wrote the lesser back -- preserving nothing. Merging with an
    arbitrary member of a duplicate set is not merging.
    """
    out = []
    if not bdir.is_dir():
        return out
    for f in sorted(bdir.glob("boundary-*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            at = lr.parse_entry_ts(d.get("boot_at"))
        except (OSError, json.JSONDecodeError):
            continue
        if at is not None and abs((at - boot_utc).total_seconds()) <= TOLERANCE_S:
            out.append((f, d))
    return out


def proposed_sweep(claude_dir, boot_utc):
    """sweep_boundary + (E) union-not-overwrite + (A) reuse the covering key."""
    entries = lr.read_entries(claude_dir)
    pre_boot = [e for e in entries
                if (ts := lr.parse_entry_ts(e.get("started_at"))) is not None
                and ts < boot_utc]
    if not lr.live_dir(claude_dir).is_dir():
        return 0

    bdir = lr.boundary_dir(claude_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    covering = _covering_records(bdir, boot_utc)

    merged, boot_at = list(pre_boot), _iso(boot_utc)
    seen = {e["session_id"] for e in merged}
    # (E) UNION ACROSS ALL covering records -- "was open at this
    # shutdown" is monotone, so once proven nothing later disproves it.
    for _path, existing in covering:
        for e in existing.get("open_at_shutdown", []):
            if e.get("session_id") not in seen:
                merged.append(e)
                seen.add(e.get("session_id"))
        prev = lr.parse_entry_ts(existing.get("boot_at"))
        if prev is not None and prev < lr.parse_entry_ts(boot_at):
            boot_at = existing["boot_at"]
    if covering:
        target = covering[0][0]            # (A) reuse the oldest key
        for path, _d in covering[1:]:      # COLLAPSE the duplicates
            try:
                path.unlink()
            except OSError:
                pass
    else:
        target = bdir / f"boundary-{boot_utc.strftime('%Y%m%dT%H%M%SZ')}.json"

    payload = json.dumps({
        "version": lr.SNAPSHOT_VERSION, "boot_at": boot_at,
        "captured_at": _iso(datetime.now(timezone.utc)),
        "open_at_shutdown": merged}, indent=2) + "\n"
    target.write_text(payload, encoding="utf-8")
    lr.snapshot_path(claude_dir).write_text(payload, encoding="utf-8")

    for e in pre_boot:                     # unchanged deletion rule
        if lr.seen_alive_after_boot(e, boot_utc):
            continue
        try:
            lr.entry_path(claude_dir, e["session_id"]).unlink()
        except OSError:
            pass
    return len(merged)


# ── scenarios ────────────────────────────────────────────────────────────

def scenario(title, sweep_fn, boots):
    print("=" * 68)
    print(title)
    print("=" * 68)
    root = _fixture()
    for b in boots:
        sweep_fn(root, b)
    recs, alias = _state(root)
    for name, ids in recs.items():
        print(f"  {name:<34} entries={len(ids)} {ids}")
    print(f"  {'alias (last-shutdown.json)':<34} entries="
          f"{len(alias) if alias else 0} {alias}")
    return recs, alias


def main() -> int:
    # 1. Reproduce the real failure with the SHIPPED code.
    recs, alias = scenario(
        "1. SHIPPED v0.9.12 -- sweep at T, then at T-8s (derived drift)",
        lambda r, b: lr.sweep_boundary(r, b), [BOOT, DRIFTED])
    bug = len(recs) == 2 and alias is not None and len(alias) == 1
    # Scenario 1 runs the REAL sweep_boundary: before the fix it
    # reproduced, after the fix it correctly does not. A probe
    # written against a defect changes meaning once the defect is
    # gone, so this is recorded rather than scored.
    label = "REPRODUCED (pre-fix)" if bug else \
"no longer reproduces -- the fix holds"
    print(f"\n  >>> {label}: {len(recs)} record(s), "
          f"alias holds {len(alias or [])} of 2 entries\n")

    # 2. The proposed logic against the identical sequence.
    recs2, alias2 = scenario(
        "2. PROPOSED E+A -- same two sweeps, merge + key reuse",
        proposed_sweep, [BOOT, DRIFTED])
    fixed = (len(recs2) == 1 and alias2 is not None
             and set(alias2) == {"dead-A", "live-B"})
    print(f"\n  >>> {'FIXED' if fixed else 'NOT FIXED'}: {len(recs2)} record, "
          f"alias holds {len(alias2 or [])} of 2 entries\n")

    # 3. The guard: two GENUINELY distinct boots must not merge.
    far = BOOT - timedelta(hours=6)
    recs3, _ = scenario(
        "3. GUARD -- two boots 6 hours apart must stay separate",
        proposed_sweep, [far, BOOT])
    separate = len(recs3) == 2
    print(f"\n  >>> {'GUARD HOLDS' if separate else 'GUARD FAILED'}: "
          f"{len(recs3)} records\n")

    # 4. Convergence: a machine that ALREADY has duplicates.
    print("=" * 68)
    print("4. REPAIR -- a machine already holding two records converges")
    print("=" * 68)
    root = _fixture()
    lr.sweep_boundary(root, BOOT)          # shipped code makes the mess
    lr.sweep_boundary(root, DRIFTED)
    before, alias_before = _state(root)
    print(f"  before: {len(before)} records, alias entries="
          f"{len(alias_before or [])}")
    proposed_sweep(root, DRIFTED - timedelta(seconds=3))   # next sweep, fixed
    after, alias_after = _state(root)
    for name, ids in after.items():
        print(f"    {name:<32} entries={len(ids)} {ids}")
    print(f"  after : {len(after)} records, alias entries="
          f"{len(alias_after or [])}")
    repaired = alias_after is not None and set(alias_after) == {"dead-A", "live-B"}
    print(f"\n  >>> {'CONVERGES' if repaired else 'DOES NOT CONVERGE'}: "
          f"alias recovered {len(alias_after or [])} of 2 entries")
    print("      (records still 2 -- the drifted key is not retroactively\n"
          "       merged away, but the ALIAS and the lookup now hold\n"
          "       everything. Whether to also collapse the stale file is a\n"
          "       decision the DWP should settle.)")

    print("\n" + "=" * 68)
    ok = fixed and separate and repaired
    print("VERDICT:", "E+A holds (scenarios 2-4)" if ok
          else "APPROACH NOT VALIDATED -- re-open the analysis")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

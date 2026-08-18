"""Mutation baseline for the boundary sweep, run BEFORE the fix.

Question this answers: do our tests actually CONSTRAIN the sweep, or do
they merely pass? A test that cannot fail proves nothing, and the DWP's
credibility rests on the claim that we understand this code.

Method: apply one source mutation at a time to live_registry.py, run the
registry tests, record whether anything died. Two cohorts are scored
separately --

  PRE   = the 19 tests that existed before this investigation
  NEW   = TestResumedPreBootSessionIsNotDead (added today)

A mutant that PRE cannot kill but NEW can is a hole our work closed.
A mutant NOTHING kills is a hole still open -- report it, do not hide it.

SAFETY: the original source is held in memory and restored in a finally
block. A `dz git-snapshot` was taken before running this.

Run:  python tests/one-offs/thinking/probe_sweep_mutation_baseline.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TARGET = REPO / "claude_session_backup" / "live_registry.py"
COMMANDS = REPO / "claude_session_backup" / "commands.py"

# Mutants that live in commands.py rather than live_registry.py. Kept in
# a second table because the roster/display behaviour (F, A2.2) is only
# reachable through the CLI-level tests, not the registry unit tests.
CMD_TESTS = "tests/test_set_current.py tests/test_commands.py"
# (label, target file, find, replace) -- some of these mutate
# live_registry but must be scored against the COMMAND-level tests,
# because that is the seam the real defect crossed (hook -> cmd_check).
CMD_MUTANTS = [
    ("F: note gated behind --open again", COMMANDS,
     "    if not snapshot_available:",
     "    if open_only and not snapshot_available:"),
    ("A2.2: proven-zero collapses to unknown", COMMANDS,
     '            "snapshot_available": open_entries is not None,',
     '            "snapshot_available": bool(snapshot_ids),'),
    ("AC7: backup stops sweeping", COMMANDS,
     "            swept = _lr.sweep_boundary(claude_dir)",
     "            swept = 0"),
    ("AC4: cmd_check deletes the living again", TARGET,
     "            if seen_alive_after_boot(entry, boot_utc):\n"
     "                continue",
     "            if False:\n                continue"),
]

# PRE must deselect EVERY class added by this investigation, or the
# cohorts stop meaning what their names say. Adding the read-through
# class without updating this line made `readthru:` mutants report as
# "killed by PRE" -- i.e. killed by tests written ten minutes earlier.
_NEW_CLASSES = ("TestResumedPreBootSessionIsNotDead",
                "TestOpenAtShutdownReadThrough")
PRE = ("tests/test_live_registry.py " + " ".join(
    f"--deselect tests/test_live_registry.py::{c}" for c in _NEW_CLASSES))
NEW = " ".join(f"tests/test_live_registry.py::{c}" for c in _NEW_CLASSES)

# (label, find, replace) -- each anchor MUST appear exactly once.
#
# After the fix there are THREE textually-similar `ts < ...` comparisons
# (split_by_boot, sweep_boundary's record loop, open_at_shutdown's derive
# loop). Bare anchors matched several of them and the probe silently
# skipped 4 of 6 mutants while reporting "0 survived" -- a false green.
# Anchors now carry their indentation, which is what makes each unique.
MUTANTS = [
    # --- split_by_boot: the liveness question (8-space indent) ---------
    ("splitby: boundary-<=",
     "        if ts is not None and ts < boot_utc:",
     "        if ts is not None and ts <= boot_utc:"),
    ("splitby: inverted",
     "        if ts is not None and ts < boot_utc:",
     "        if ts is not None and ts > boot_utc:"),
    ("splitby: liveness-check-disabled",
     "        if seen_alive_after_boot(entry, boot_utc):",
     "        if False:"),
    # --- seen_alive_after_boot: the new predicate ----------------------
    ("pidat: never-alive",
     "    return seen is not None and seen >= boot_utc",
     "    return False"),
    ("pidat: boundary->",
     "    return seen is not None and seen >= boot_utc",
     "    return seen is not None and seen > boot_utc"),
    # --- sweep_boundary: record loop (12-space) + deletion -------------
    ("sweep: record-boundary-<=",
     "            if ts is not None and ts < boot_utc:",
     "            if ts is not None and ts <= boot_utc:"),
    ("sweep: deletes-the-living-again",
     "            if seen_alive_after_boot(entry, boot_utc):\n"
     "                continue",
     "            if False:\n                continue"),
    ("sweep: skip-the-unlink",
     '                entry_path(claude_dir, entry["session_id"]).unlink()',
     "                pass  # MUTANT: deletion removed"),
    # A2.2 removed `if not pre_boot: return 0` (an empty boundary is now
    # recorded as proven-zero). The gate that replaced it is the honesty
    # check: never claim "nobody was open" on a machine csb never watched.
    ("sweep: records-without-ever-watching",
     "        if not live_dir(claude_dir).is_dir():",
     "        if False:"),
    # --- open_at_shutdown: the read-through ----------------------------
    ("readthru: derive-boundary-<=",
     "        if ts is not None and ts < current:",
     "        if ts is not None and ts <= current:"),
    ("readthru: never-derives",
     "    derived: list[dict] = []",
     "    return None\n    derived: list[dict] = []"),
]


def run(selector: str) -> bool:
    """True == suite GREEN (mutant survived this cohort)."""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *selector.split(), "-q",
         "--no-header", "-x", "--timeout=60"],
        cwd=REPO, capture_output=True, text=True)
    return r.returncode == 0


def main() -> int:
    original = TARGET.read_text(encoding="utf-8")
    rows = []
    try:
        # sanity: both cohorts must be in a known state on clean source
        assert run(PRE), "PRE cohort must be green on unmutated source"
        assert run(NEW), ("NEW cohort must be green on unmutated source "
                          "(run this AFTER the fix; before it, NEW is red "
                          "by design and mutation scoring is meaningless)")
        print("clean source: PRE=green  NEW=green\n")

        for label, find, repl in MUTANTS:
            # Anchor on WHOLE LINES. Bare substring matching counted an
            # 8-space anchor as also matching the 12-space line that
            # contains it, reporting "2x" for anchors that were in fact
            # unique -- and skipping three real mutants on that basis.
            key, rep = "\n" + find + "\n", "\n" + repl + "\n"
            hits = original.count(key)
            if hits != 1:
                # NEVER score a skipped mutant as killed -- an ambiguous
                # anchor silently tested nothing and reported success.
                rows.append((label, "SKIP", "SKIP", f"anchor matched {hits}x"))
                continue
            TARGET.write_text(original.replace(key, rep), encoding="utf-8")
            pre_green = run(PRE)
            new_green = run(NEW)
            rows.append((label,
                         "SURVIVED" if pre_green else "killed",
                         "SURVIVED" if new_green else "killed", ""))
            TARGET.write_text(original, encoding="utf-8")
    finally:
        TARGET.write_text(original, encoding="utf-8")
        print(f"restored {TARGET.name} "
              f"({'clean' if TARGET.read_text(encoding='utf-8') == original else 'MISMATCH!'})\n")

    # ── commands.py cohort: F and A2.2 are display-layer behaviour ────
    pristine = {p: p.read_text(encoding="utf-8") for p in (COMMANDS, TARGET)}
    try:
        for label, target, find, repl in CMD_MUTANTS:
            src = pristine[target]
            key, rep = "\n" + find + "\n", "\n" + repl + "\n"
            hits = src.count(key)
            if hits != 1:
                rows.append((label, "SKIP", "SKIP", f"anchor matched {hits}x"))
                continue
            target.write_text(src.replace(key, rep), encoding="utf-8")
            rows.append((label, "n/a",
                         "SURVIVED" if run(CMD_TESTS) else "killed",
                         f"cmd-level / {target.name}"))
            target.write_text(src, encoding="utf-8")
    finally:
        for p, src in pristine.items():
            p.write_text(src, encoding="utf-8")
            ok = p.read_text(encoding="utf-8") == src
            print(f"restored {p.name} ({'clean' if ok else 'MISMATCH!'})")
        print()

    print(f"{'mutant':<34} {'PRE':<10} {'NEW':<10} note")
    print("-" * 74)
    survivors = skipped = 0
    for label, pre, new, note in rows:
        if pre == "SKIP":
            skipped += 1
        elif pre == "SURVIVED" and new == "SURVIVED":
            survivors += 1
        print(f"{label:<34} {pre:<10} {new:<10} {note}")
    print("-" * 74)
    scored = len(rows) - skipped
    print(f"{len(rows)} mutants: {scored} scored, {skipped} SKIPPED, "
          f"{survivors} survived BOTH cohorts.")
    if skipped:
        print("\n!! A SKIPPED mutant tested nothing. Do not read the")
        print("   survivor count as a pass until every anchor is unique.")
    if survivors:
        print("\nSurvivors are the honest finding: behaviour in those")
        print("directions is pinned by no test in either cohort.")
    return 1 if (skipped or survivors) else 0


if __name__ == "__main__":
    raise SystemExit(main())

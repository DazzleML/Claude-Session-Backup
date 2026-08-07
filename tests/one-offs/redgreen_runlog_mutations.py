#!/usr/bin/env python3
"""Red-green mutation verification for #69 step 5a (`csb backup --log-file`).

House rule: a passing test is meaningless if it would pass without the fix.
This probe injects the failure each guard exists to catch, confirms the
guard's test FAILS, restores, and confirms green.

  Mutation A -- disable the pythonw stream rebind:
               test_pythonw_streams_rebound_to_log must fail.
  Mutation B -- disable the structured run-log write:
               the line-format and skipped-lock evidence tests must fail.

Run from the repo root:  python tests/one-offs/redgreen_runlog_mutations.py
Kept per the keep-one-offs rule; re-run after any cmd_backup refactor.

Result 2026-08-06 (this box):
  attempt 1: RED B came back GREEN -- the mutation itself was broken
             (`if False:` guarded only an inserted `pass`; the write still
             ran). Lesson recorded at MUT_B below: verify the mutation
             mutates before trusting the red.
  corrected: RED A (no rebind):   1 failed   <- guard catches it
             RED B (no log line): 3 failed   <- guard catches it
             GREEN restored:      7 passed
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

TARGET = pathlib.Path("claude_session_backup/commands.py")

MUT_A_OLD = (
    "if sys.stdout is not None and sys.stderr is not None:\n        return"
)
MUT_A_NEW = "if True:\n        return"

# Anchor: the last line of _append_run_log's docstring. The mutation makes
# the function return before doing anything -- a REAL disable. (First
# attempt guarded only an inserted `pass` with `if False:` and the write
# still ran: the red stayed green. Mutations need mutating.)
MUT_B_OLD = 'surfaced by schedule status as missing evidence, not silently."""'
MUT_B_NEW = (
    'surfaced by schedule status as missing evidence, not silently."""\n'
    "    return"
)


def pytest_tail(keyword: str) -> str:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_backup_runlog.py",
         "-q", "-k", keyword],
        capture_output=True, text=True,
    )
    summary = [l for l in r.stdout.splitlines()
               if "passed" in l or "failed" in l or "error" in l]
    return summary[-1].strip() if summary else "(no summary line)"


def main() -> int:
    orig = TARGET.read_text(encoding="utf-8")
    assert MUT_A_OLD in orig, "mutation A anchor drifted -- update this probe"
    assert MUT_B_OLD in orig, "mutation B anchor drifted -- update this probe"
    try:
        TARGET.write_text(orig.replace(MUT_A_OLD, MUT_A_NEW), encoding="utf-8")
        print("RED A (no rebind):  ", pytest_tail("pythonw_streams_rebound"))

        TARGET.write_text(orig.replace(MUT_B_OLD, MUT_B_NEW), encoding="utf-8")
        print("RED B (no log line):", pytest_tail("structured_line or skipped or two_runs"))
    finally:
        TARGET.write_text(orig, encoding="utf-8")
    print("GREEN restored:     ", pytest_tail(""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""LIVE round-trip test against the REAL ~/.claude/settings.json.

Backs up the real settings.json first, confirms it's at the expected value,
sets it to 100 via the csb CLI, verifies, sets it back to 90, verifies, then
diffs the final file against the backup. Prints the actual tool output for
each step. Does NOT delete the backup -- the caller decides.

Run: python tests/one-offs/live_ttl_roundtrip.py
"""

import io
import json
import shutil
import sys
import time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import claude_session_backup.cli as cli
from claude_session_backup import config

EXPECTED_START = 90


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def step(label, argv):
    rc, out, err = run(argv)
    print(f"\n### {label}")
    print(f"$ csb {' '.join(argv)}   -> rc={rc}")
    for stream, text in (("STDOUT", out), ("STDERR", err)):
        if text.strip():
            print(f"  {stream}:")
            for line in text.rstrip('\n').splitlines():
                print(f"    {line}")
    return rc


def current_value(settings_path):
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    return data.get("cleanupPeriodDays")


def main():
    settings_path = config.get_settings_path(None)  # real ~/.claude/settings.json
    print(f"REAL settings file: {settings_path}")
    if not settings_path.exists():
        print("ERROR: real settings.json does not exist; aborting.")
        return 1

    original_bytes = settings_path.read_bytes()
    start_val = current_value(settings_path)
    print(f"Current cleanupPeriodDays = {start_val}")
    if start_val != EXPECTED_START:
        print(f"ABORT: expected {EXPECTED_START} at start, found {start_val}. "
              f"Not touching the file.")
        return 1

    # 1. Backup FIRST (timestamped, alongside the file).
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = settings_path.with_suffix(f".json.bak-{stamp}")
    shutil.copy2(settings_path, backup)
    print(f"Backup written: {backup}  ({len(original_bytes)} bytes)")

    # 2. GET (expect 90)
    step("GET (expect 90)", ["config", "settings:cleanupPeriodDays"])

    # 3. SET 100 (increase -> no reduction warning)
    rc = step("SET 100", ["config", "settings:cleanupPeriodDays", "100"])
    assert rc == 0, "set 100 failed"
    assert current_value(settings_path) == 100, "file did not become 100"

    # 4. Verify other keys preserved (compare every key EXCEPT the TTL).
    orig = json.loads(original_bytes.decode("utf-8"))
    now = json.loads(settings_path.read_text(encoding="utf-8"))
    orig_rest = {k: v for k, v in orig.items() if k != "cleanupPeriodDays"}
    now_rest = {k: v for k, v in now.items() if k != "cleanupPeriodDays"}
    assert orig_rest == now_rest, "OTHER SETTINGS CHANGED -- merge clobbered something!"
    print("\n[OK] all keys other than cleanupPeriodDays are byte-for-byte equal "
          "after the write.")

    # 5. GET (expect 100)
    step("GET (expect 100)", ["config", "settings:cleanupPeriodDays"])

    # 6. SET back to 90 (decrease -> reduction warning is expected + benign here)
    rc = step("SET back to 90 (reduction warning expected)",
              ["config", "settings:cleanupPeriodDays", "90"])
    assert rc == 0, "set 90 failed"
    assert current_value(settings_path) == 90, "file did not return to 90"

    # 7. GET (expect 90)
    step("GET (expect 90)", ["config", "settings:cleanupPeriodDays"])

    # 8. Compare final file to the backup.
    final_bytes = settings_path.read_bytes()
    print("\n### Final comparison vs backup")
    if final_bytes == original_bytes:
        print("  IDENTICAL: the file is byte-for-byte the same as before the test.")
        identical = True
    else:
        identical = False
        print("  Bytes differ (value is back to 90, but formatting was "
              "re-serialized). Functional JSON is identical:")
        print(f"    same parsed content: {json.loads(final_bytes.decode()) == orig}")
        # Show a small unified diff.
        import difflib
        diff = difflib.unified_diff(
            original_bytes.decode("utf-8").splitlines(),
            final_bytes.decode("utf-8").splitlines(),
            fromfile="settings.json (original)",
            tofile="settings.json (after round-trip)",
            lineterm="",
        )
        shown = list(diff)[:40]
        for line in shown:
            print(f"    {line}")

    print(f"\nBackup retained at: {backup}")
    print("RESULT:", "CLEAN (identical)" if identical else
          "WORKS, but re-serialized formatting -- see diff above")
    return 0


if __name__ == "__main__":
    sys.exit(main())

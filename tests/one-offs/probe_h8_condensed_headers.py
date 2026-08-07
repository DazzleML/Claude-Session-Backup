#!/usr/bin/env python3
"""Adversarial probe: H8 fallthrough header condensing at >3 skipped empty
epochs, NON-exhausted path (settles successfully on a working epoch after
skipping more than 3 empties).

Coverage gap check: neither tests/test_epoch_history.py nor the sibling
probes in this directory exercise this specific branch --
test_relative_address_walks_to_working_set skips exactly 1; the exhausted
probe (probe_h8_fallthrough_adversarial.py) exercises the DIFFERENT
"fallthrough_exhausted" summary branch, not the skipped[:3] + "(+K more)"
condensing branch in commands.py's `if skipped: more = ...` block. This
probe closes that gap directly. Design:
2026-08-07__07-20-42__dev-workflow-process__r3-epoch-history-and-promotion.md
(H8: "display condenses past 3 skipped headers").

In-process (imports cli/commands directly), temp claude_dir + temp SQLite
DB only. Nothing here touches the real ~/.claude store.

Usage:  python tests/one-offs/probe_h8_condensed_headers.py
"""

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import claude_session_backup.cli as cli  # noqa: E402
import claude_session_backup.commands as commands_module  # noqa: E402
from claude_session_backup.epochs import (  # noqa: E402
    EVENT_BOOT,
    EVENT_SHUTDOWN_CLEAN,
    Fence,
)
from claude_session_backup.index import init_schema, open_db  # noqa: E402

results = []


def check(label, condition):
    results.append((label, bool(condition)))
    tag = "PASS" if condition else "FAIL"
    print(f"[{tag}] {label}")


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _boot(dt):
    return Fence(at_utc=dt, event_id=EVENT_BOOT)


def _shut(dt):
    return Fence(at_utc=dt, event_id=EVENT_SHUTDOWN_CLEAN)


# Six epochs (indices 0..5, newest first). Only index 5 (deepest, DEEP)
# has index activity. Requesting last~1 must skip indices 1,2,3,4 (FOUR
# empties -- over the 3-header condensing threshold) before settling at
# index 5.
SIX_EPOCH_FENCES = [
    _boot(_utc(2026, 5, 1, 8, 0)),
    _shut(_utc(2026, 5, 10, 12, 0)), _boot(_utc(2026, 5, 10, 12, 5)),
    _shut(_utc(2026, 5, 20, 12, 0)), _boot(_utc(2026, 5, 20, 12, 5)),
    _shut(_utc(2026, 6, 1, 12, 0)), _boot(_utc(2026, 6, 1, 12, 5)),
    _shut(_utc(2026, 6, 10, 12, 0)), _boot(_utc(2026, 6, 10, 12, 5)),
    _shut(_utc(2026, 6, 20, 12, 0)), _boot(_utc(2026, 6, 20, 12, 5)),
    _shut(_utc(2026, 7, 1, 12, 0)), _boot(_utc(2026, 7, 1, 12, 5)),
]

UUID_DEEP = "eeee3333-bbbb-cccc-dddd-000000000003"
UUID_NEW = "eeee4444-bbbb-cccc-dddd-000000000004"


def _insert(conn, sid, name, last_active):
    conn.execute(
        "INSERT INTO sessions (session_id, session_name, project,"
        " start_folder, started_at, last_active_at, is_fork) VALUES"
        " (?, ?, 'C--code-test', 'C:\\code\\test',"
        " '2026-04-01T00:00:00Z', ?, 0)",
        (sid, name, last_active),
    )


def main():
    tmp = Path(tempfile.mkdtemp(prefix="csb_h8_condense_"))
    claude_dir = tmp / "claude"
    claude_dir.mkdir()
    db = tmp / "condense.db"
    try:
        conn = open_db(db)
        init_schema(conn, quiet=True)
        # NEW lives in epoch 0 (newest); DEEP lives in epoch 5 (deepest).
        # Epochs 1-4 have no matching activity at all.
        _insert(conn, UUID_NEW, "NEW__session", "2026-06-25T12:00:00Z")
        _insert(conn, UUID_DEEP, "DEEP__session", "2026-05-05T12:00:00Z")
        conn.commit()
        conn.close()

        orig_read_fences = commands_module.read_fences
        commands_module.read_fences = \
            lambda *a, **k: list(SIX_EPOCH_FENCES)
        try:
            buf_out = StringIO()
            orig_out = sys.stdout
            sys.stdout = buf_out
            try:
                rc = cli.main(["set", "show", "last~1", "--claude-dir",
                              str(claude_dir), "--db", str(db)])
            finally:
                sys.stdout = orig_out
            out = buf_out.getvalue()
            print(out)  # show it for the record

            check("condense: rc == 0", rc == 0)
            check("condense: settles on last~5 (DEEP)",
                  "Epoch 'last~5'" in out and "DEEP__session" in out)
            # Only the first 3 skipped headers print individually.
            check("condense: shows last~1 header (1st of 4 skipped)",
                  "Epoch 'last~1'" in out)
            check("condense: shows last~2 header (2nd of 4 skipped)",
                  "Epoch 'last~2'" in out)
            check("condense: shows last~3 header (3rd of 4 skipped)",
                  "Epoch 'last~3'" in out)
            check("condense: does NOT show last~4 header individually "
                  "(4th skipped -- condensed away)",
                  "Epoch 'last~4'" not in out)
            check("condense: names the condensed overflow count "
                  "('+1 more empty')", "+1 more empty" in out)

            buf_out2 = StringIO()
            sys.stdout = buf_out2
            try:
                rc2 = cli.main(["set", "show", "last~1", "--claude-dir",
                               str(claude_dir), "--db", str(db), "--json"])
            finally:
                sys.stdout = orig_out
            payload = json.loads(buf_out2.getvalue())
            check("condense JSON: rc == 0", rc2 == 0)
            check("condense JSON: skipped_empty carries ALL 4 skipped "
                  "tokens (JSON is not condensed, only the human display "
                  "is)",
                  [s["token"] for s in payload["skipped_empty"]] ==
                  ["last~1", "last~2", "last~3", "last~4"])
            check("condense JSON: settled name is last~5",
                  payload["name"] == "last~5")
        finally:
            commands_module.read_fences = orig_read_fences
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

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

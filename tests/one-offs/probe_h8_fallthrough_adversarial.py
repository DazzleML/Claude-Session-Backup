#!/usr/bin/env python3
"""Adversarial probe: H8 disclosed empty-epoch fallthrough, exhaustion case.

Part of the DELTA sweep verifying the H8/H9 review-round addenda (design:
2026-08-07__07-20-42__dev-workflow-process__r3-epoch-history-and-promotion.md).

pytest already covers this exact scenario
(tests/test_epoch_history.py::TestEmptyEpochFallthrough::
test_exhausted_walk_names_the_activity_floor) -- this probe re-derives it
independently, in-process (imports cli/commands directly, no subprocess),
against a temp claude_dir + temp SQLite DB. Nothing here touches the real
~/.claude store.

Usage:  python tests/one-offs/probe_h8_fallthrough_adversarial.py
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


# Four epochs, same shape as test_epoch_history's FOUR_EPOCH_FENCES, but
# for THIS probe every epoch is left empty except the newest -- so a walk
# starting at last~1 has nowhere with members to land, anywhere.
FOUR_EPOCH_FENCES_ALL_EMPTY_DEEP = [
    _boot(_utc(2026, 6, 1, 8, 0)),
    _shut(_utc(2026, 6, 10, 12, 0)), _boot(_utc(2026, 6, 10, 12, 5)),
    _shut(_utc(2026, 6, 20, 12, 0)), _boot(_utc(2026, 6, 20, 12, 5)),
    _shut(_utc(2026, 7, 1, 12, 0)), _boot(_utc(2026, 7, 1, 12, 5)),
    _shut(_utc(2026, 7, 20, 12, 0)), _boot(_utc(2026, 7, 20, 12, 5)),
]

UUID_NEW = "eeee4444-bbbb-cccc-dddd-000000000004"


def _insert(conn, sid, name, last_active):
    conn.execute(
        "INSERT INTO sessions (session_id, session_name, project,"
        " start_folder, started_at, last_active_at, is_fork) VALUES"
        " (?, ?, 'C--code-test', 'C:\\code\\test',"
        " '2026-06-01T00:00:00Z', ?, 0)",
        (sid, name, last_active),
    )


def main():
    tmp = Path(tempfile.mkdtemp(prefix="csb_h8_adversarial_"))
    claude_dir = tmp / "claude"
    claude_dir.mkdir()
    db = tmp / "floor.db"
    try:
        conn = open_db(db)
        init_schema(conn, quiet=True)
        # Only ONE session in the index, active in the NEWEST epoch (e0);
        # last~1, last~2, last~3 are all empty -- the exhaustion case.
        _insert(conn, UUID_NEW, "NEW__session", "2026-07-10T12:00:00Z")
        conn.commit()
        conn.close()

        orig_read_fences = commands_module.read_fences
        commands_module.read_fences = \
            lambda *a, **k: list(FOUR_EPOCH_FENCES_ALL_EMPTY_DEEP)
        try:
            buf_out, buf_err = StringIO(), StringIO()
            orig_out, orig_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = buf_out, buf_err
            try:
                rc = cli.main(["set", "show", "last~1", "--claude-dir",
                              str(claude_dir), "--db", str(db)])
            finally:
                sys.stdout, sys.stderr = orig_out, orig_err
            out = buf_out.getvalue()
            print(out)  # show it for the record

            check("exhausted: rc == 0 (honest empty answer, not an error)",
                  rc == 0)
            check("exhausted: settles BACK on the REQUESTED epoch "
                  "(last~1), not landing arbitrarily deep",
                  "Epoch 'last~1'" in out)
            check("exhausted: does NOT show last~3's header (condensed "
                  "tail, not every skipped epoch)",
                  "Epoch 'last~3'" not in out)
            check("exhausted: names how many deeper epochs were tried",
                  "2 deeper epochs" in out)
            check("exhausted: names the index's activity floor "
                  "(2026-07-10, the only session's last_active_at)",
                  "activity reaches back to 2026-07-10" in out)

            # JSON variant: activity_floor is SET (not null) when exhausted,
            # and requested_token == name (settled back on what was asked).
            buf_out2 = StringIO()
            sys.stdout = buf_out2
            try:
                rc2 = cli.main(["set", "show", "last~1", "--claude-dir",
                               str(claude_dir), "--db", str(db), "--json"])
            finally:
                sys.stdout = orig_out
            payload = json.loads(buf_out2.getvalue())
            check("exhausted JSON: rc == 0", rc2 == 0)
            check("exhausted JSON: activity_floor is SET (not null)",
                  payload["activity_floor"] is not None)
            check("exhausted JSON: activity_floor value is the session's "
                  "last_active_at date",
                  payload["activity_floor"].startswith("2026-07-10"))
            check("exhausted JSON: requested_token == name (settled on "
                  "the requested token itself)",
                  payload["requested_token"] == payload["name"] == "last~1")
            check("exhausted JSON: fallthrough_exhausted counts the "
                  "deeper epochs tried", payload["fallthrough_exhausted"] == 2)
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

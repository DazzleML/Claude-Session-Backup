"""Checklist probe: v0.8.5 Phase 6 R1 -- `boot` view, tier ladder, `--set`.

tests/checklists/v0.8.5__Feature__boot-view-and-set-flag.md

Independent cross-check of the automated pytest suite (tests/test_set_boot.py,
tests/test_resume_set.py): builds its OWN temp claude-dir + temp sqlite db
(never the real store), seeds sessions + live-registry entries, and drives
`cli.main()` directly -- the same in-process technique the pytest fixtures
use, but run standalone outside pytest so a bug shared between the fixture
and the source code (or a fixture typo) is less likely to hide behind a
green pytest run.

Safety: `subprocess.run` is ALWAYS monkeypatched before any `csb resume`
call in this script, as a defense-in-depth safety net -- no real `claude`
process can be spawned by this probe. Per the checklist's own "mock-free"
instruction for the passthrough item, the PASSTHROUGH VERIFICATION itself
never relies on the mock's absence-of-call: it is proven via error-path
output (a deliberately not-in-index roster member), so the finding would
hold even without the safety-net mock in place.

Run: python tests/one-offs/probe_v085_boot_set_checklist.py
Kept per repo convention -- one-offs retain standing reuse value.
"""

import contextlib
import io
import json
import shutil
import subprocess as subprocess_module
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import claude_session_backup.cli as cli  # noqa: E402
import claude_session_backup.epochs as epochs  # noqa: E402
import claude_session_backup.live_registry as lr  # noqa: E402
import claude_session_backup.liveness as lv  # noqa: E402
from claude_session_backup.index import init_schema, open_db  # noqa: E402
from claude_session_backup.session_sets import create_set  # noqa: E402

RESULTS = []
BASE = Path(tempfile.mkdtemp(prefix="csb_v085_probe_"))


def record(name, passed, detail=""):
    RESULTS.append((name, passed, detail))
    tag = "PASS" if passed else "FAIL"
    print(f"[{tag}] {name}" + (f"\n       {detail}" if detail else ""))


def run_cli(argv):
    """Call cli.main(argv); capture stdout/stderr/rc; catch SystemExit."""
    out, err = io.StringIO(), io.StringIO()
    rc = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def new_store(name):
    """A fresh claude-dir + db pair, isolated per scenario."""
    root = BASE / name
    claude_dir = root / "claude"
    claude_dir.mkdir(parents=True)
    db = root / "index.db"
    conn = open_db(db)
    init_schema(conn, quiet=True)
    return claude_dir, db, conn


def insert_session(conn, sid, name, last_active,
                    started_at="2026-07-01T00:00:00Z"):
    conn.execute(
        "INSERT INTO sessions (session_id, session_name, project,"
        " start_folder, started_at, last_active_at, is_fork) VALUES"
        " (?, ?, 'C--code-test', 'C:\\code\\test', ?, ?, 0)",
        (sid, name, started_at, last_active),
    )


def cflags(claude_dir, db):
    return ["--claude-dir", str(claude_dir), "--db", str(db)]


# ── SECTION A: boot view tier matrix (independent construction) ──────────

def section_a_tier_matrix():
    print("\n=== SECTION A: boot view tier matrix ===")
    BOOT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    claude_dir, db, conn = new_store("A_tiers")

    UUID_RUN = "a1111111-bbbb-cccc-dddd-000000000001"
    UUID_UNV = "a2222222-bbbb-cccc-dddd-000000000002"
    UUID_EXITED = "a4444444-bbbb-cccc-dddd-000000000004"
    UUID_OLD = "a5555555-bbbb-cccc-dddd-000000000005"
    UUID_NEW = "a3333333-bbbb-cccc-dddd-000000000003"

    insert_session(conn, UUID_RUN, "PROBE_RUNNING", "2026-08-01T14:00:00Z")
    insert_session(conn, UUID_UNV, "PROBE_UNVERIFIED", "2026-08-01T13:00:00Z")
    insert_session(conn, UUID_EXITED, "PROBE_EXITED", "2026-08-01T12:30:00Z")
    insert_session(conn, UUID_OLD, "PROBE_PREBOOT", "2026-07-20T00:00:00Z")
    conn.commit()
    conn.close()

    lr.live_dir(claude_dir).mkdir(parents=True, exist_ok=True)
    for sid, ts in ((UUID_RUN, "2026-08-01T12:10:00Z"),
                    (UUID_UNV, "2026-08-01T12:20:00Z"),
                    (UUID_NEW, "2026-08-01T12:40:00Z")):
        lr.entry_path(claude_dir, sid).write_text(json.dumps({
            "session_id": sid, "started_at": ts, "source": "startup",
            "cwd": "C:/code/probe",
        }), encoding="utf-8")

    run_mock = MagicMock(return_value=None)
    with patch.object(lr, "current_boot_utc", lambda: BOOT), \
         patch.object(lv, "scan",
                      lambda: lv.LiveScan(by_uuid={UUID_RUN.lower(): 9999},
                                          ok=True)), \
         patch.object(subprocess_module, "run", run_mock):
        rc, out, err = run_cli(["set", "show", "boot", "--json"] +
                               cflags(claude_dir, db))
        try:
            payload = json.loads(out)
            by_id = {m["session_id"]: m for m in payload["members"]}
            ok = (rc == 0
                  and payload["hooks_active"] is True
                  and by_id[UUID_RUN]["live_status"] == "running"
                  and by_id[UUID_RUN]["pid"] == 9999
                  and by_id[UUID_UNV]["live_status"] == "unverified"
                  and by_id[UUID_EXITED]["live_status"] == "exited"
                  and UUID_OLD not in by_id
                  and by_id[UUID_NEW]["in_index"] is False)
            record("A1: four-tier JSON payload (running/unverified/exited/"
                   "appendix, pre-boot excluded)", ok,
                   "" if ok else json.dumps(payload, indent=2))
        except Exception as exc:  # noqa: BLE001
            record("A1: four-tier JSON payload", False, f"{exc}\n{out}\n{err}")

        # Ordering: activity order, unindexed appendix members AFTER.
        try:
            ids = [m["session_id"] for m in payload["members"]]
            idxs = [m["index"] for m in payload["members"]]
            ok = (ids == [UUID_EXITED, UUID_UNV, UUID_RUN, UUID_NEW]
                  and idxs == [1, 2, 3, 4])
            record("A2: activity order + appendix ordering (unindexed"
                   " member never renumbers indexed rows)", ok,
                   "" if ok else f"ids={ids} idxs={idxs}")
        except Exception as exc:  # noqa: BLE001
            record("A2: appendix ordering", False, str(exc))

        rc2, out2, err2 = run_cli(["set", "show", "boot"] +
                                  cflags(claude_dir, db))
        ok = (rc2 == 0 and "Active this boot" in out2
              and "1 running" in out2 and "1 exited" in out2
              and "[exited]" in out2
              and "row numbers reflect this invocation" in out2
              and " ago" in out2 and "before shutdown" not in out2
              and "csb resume PROBE_RUNNING -- --fork-session" in out2)
        record("A3: human rendering (tier counts, [exited] tag, 'ago'"
               " gap label not 'before shutdown', stable fork hint)", ok,
               "" if ok else out2)


# ── SECTION B: hookless honesty ───────────────────────────────────────────

def section_b_hookless():
    print("\n=== SECTION B: hookless honesty ===")
    BOOT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    claude_dir, db, conn = new_store("B_hookless")
    UUID = "b1111111-bbbb-cccc-dddd-000000000001"
    insert_session(conn, UUID, "PROBE_HOOKLESS", "2026-08-01T12:30:00Z")
    conn.commit()
    conn.close()
    # No live_dir created at all -- no hook activity this boot, no snapshot.

    with patch.object(lr, "current_boot_utc", lambda: BOOT):
        rc, out, err = run_cli(["set", "show", "boot"] + cflags(claude_dir, db))
        ok = (rc == 0 and "[exited]" not in out
              and "exit detection unavailable" in out)
        record("B1: no hook evidence -> NO [exited] tag anywhere, footer"
               " discloses unavailability (red-green target)", ok,
               "" if ok else out)

        rc2, out2, err2 = run_cli(["set", "show", "boot", "--json"] +
                                  cflags(claude_dir, db))
        try:
            payload = json.loads(out2)
            ok = (payload["hooks_active"] is False
                  and payload["members"][0]["live_status"] is None)
            record("B2: JSON reports hooks_active=false, unadorned"
                   " live_status=null (never a guessed tag)", ok,
                   "" if ok else json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            record("B2: JSON hookless report", False, f"{exc}\n{out2}")


# ── SECTION C: Sections block ─────────────────────────────────────────────

def section_c_sections_block():
    print("\n=== SECTION C: checklist Sections block ===")
    BOOT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    claude_dir, db, conn = new_store("C_sections")
    UUID_A = "c1111111-bbbb-cccc-dddd-000000000001"
    UUID_B = "c2222222-bbbb-cccc-dddd-000000000002"
    insert_session(conn, UUID_A, "PROBE_C_A", "2026-08-01T13:00:00Z")
    insert_session(conn, UUID_B, "PROBE_C_B", "2026-08-01T09:00:00Z")  # old
    conn.commit()
    conn.close()
    lr.live_dir(claude_dir).mkdir(parents=True, exist_ok=True)
    lr.entry_path(claude_dir, UUID_A).write_text(json.dumps({
        "session_id": UUID_A, "started_at": "2026-08-01T12:50:00Z",
        "source": "startup", "cwd": "C:/code/probe",
    }), encoding="utf-8")

    with patch.object(lr, "current_boot_utc", lambda: BOOT), \
         patch.object(lv, "scan", lambda: lv.LiveScan(ok=True)):
        # --window on boot: gaps, "before now" header.
        rc, out, err = run_cli(["set", "show", "boot", "--window", "2"] +
                               cflags(claude_dir, db))
        ok = (rc == 0 and "before now" in out)
        record("C1: `set show boot --window 2` narrows + 'before now'"
               " header present", ok, "" if ok else out + err)

        # --open on boot: rc 2, names `set show last --open`.
        rc, out, err = run_cli(["set", "show", "boot", "--open"] +
                               cflags(claude_dir, db))
        ok = (rc == 2 and "epoch in progress" in err
              and "csb set show last --open" in err)
        record("C2: `set show boot --open` rc=2, names the last --open"
               " alternative", ok, "" if ok else f"rc={rc} err={err}")

    # Reserved shapes rejected vs near-misses accepted.
    reserved = ["boot", "BOOT", "last~1", "2026-8-9", "2026-08-09"]
    all_rejected = True
    detail = []
    for name in reserved:
        rc, out, err = run_cli(["set", "new", name, UUID_A[:8]] +
                               cflags(claude_dir, db))
        if not (rc == 2 and "reserved" in err):
            all_rejected = False
            detail.append(f"{name!r} -> rc={rc} err={err!r}")
    record("C3: reserved shapes rejected (boot/BOOT/last~1/bare dates)",
           all_rejected, "; ".join(detail))

    near_misses = ["2026-8-9__topic", "last-1"]
    all_legal = True
    detail = []
    for name in near_misses:
        rc, out, err = run_cli(["set", "new", name, UUID_A[:8]] +
                               cflags(claude_dir, db))
        if rc != 0:
            all_legal = False
            detail.append(f"{name!r} -> rc={rc} err={err!r}")
        else:
            run_cli(["set", "rm", name, "--yes"] + cflags(claude_dir, db))
    record("C4: near-misses accepted (2026-8-9__topic, last-1)", all_legal,
           "; ".join(detail))

    # csb resume --set NOPE 1 -- "No set named"
    with patch.object(subprocess_module, "run", MagicMock()):
        rc, out, err = run_cli(["resume", "--set", "NOPE", "1"] +
                               cflags(claude_dir, db))
        ok = (rc == 1 and "No set named" in err)
        record("C5: `csb resume --set NOPE 1` -> rc=1, 'No set named'", ok,
               "" if ok else f"rc={rc} err={err!r}")

        # bare csb resume -- rc 2 usage with the three forms
        rc, out, err = run_cli(["resume"] + cflags(claude_dir, db))
        ok = (rc == 2 and "--set" in err)
        record("C6: bare `csb resume` -> rc=2, usage mentions --set forms",
               ok, "" if ok else f"rc={rc} err={err!r}")

        # Old form `csb resume set 2` -- 'set' is now a plain query.
        rc, out, err = run_cli(["resume", "set", "2"]
                               + cflags(claude_dir, db))
        # argparse: "session_id"="set", extra positional "2" unrecognized
        # (selector nargs was removed) OR resolves 'set' as a query and
        # errors that '2' is unrecognized -- either way must NOT silently
        # dispatch old index-addressing, and error must be comprehensible.
        comprehensible = bool(err.strip()) and rc in (1, 2)
        record("C7: old form `csb resume set 2` -- no crash, 'set' is a"
               " query (not silently accepted as index form), error is"
               " comprehensible", comprehensible,
               f"rc={rc} out={out!r} err={err!r}")

        rc, out, err = run_cli(["resume", "set"] + cflags(claude_dir, db))
        ok = (rc == 1 and "which member" not in err)
        record("C7b: bare old form `csb resume set` resolves 'set' as an"
               " ordinary query (no such session -> rc=1), never a"
               " grammar token", ok, f"rc={rc} err={err!r}")


# ── SECTION D: --set grammar matrix ───────────────────────────────────────

FENCE_OUTPUT = (
    "2026-07-25T16:17:18.0000000Z|6005\n"
    "2026-07-25T16:16:32.0000000Z|6006\n"
    "2026-07-25T16:16:18.0000000Z|1074\n"
    "2026-07-15T08:18:17.0000000Z|6005\n"
    "2026-07-15T08:17:38.0000000Z|6006\n"
)


def section_d_set_grammar():
    print("\n=== SECTION D: --set grammar matrix ===")
    claude_dir, db, conn = new_store("D_grammar")
    UUID_1 = "d1111111-bbbb-cccc-dddd-000000000001"
    UUID_2 = "d2222222-bbbb-cccc-dddd-000000000002"
    UUID_3 = "d3333333-bbbb-cccc-dddd-000000000003"
    UUID_GONE = "d9999999-bbbb-cccc-dddd-000000000099"
    for sid, nm, active in (
        (UUID_1, "PROBE_D_ALPHA", "2026-07-18T00:00:00Z"),
        (UUID_2, "PROBE_D_BETA", "2026-07-20T00:00:00Z"),
        (UUID_3, "PROBE_D_GAMMA", "2026-07-22T00:00:00Z"),
    ):
        insert_session(conn, sid, nm, active)
    conn.commit()
    conn.close()
    create_set(claude_dir, "PROBE-STACK", [UUID_3, UUID_1])
    create_set(claude_dir, "PROBE-STALE", [UUID_GONE])

    fence_patches = [
        patch.object(epochs.sys, "platform", "win32"),
        patch.object(epochs, "_run_powershell", lambda *a, **k: FENCE_OUTPUT),
    ]
    run_mock_safety_net = MagicMock(
        side_effect=AssertionError(
            "SAFETY NET: subprocess.run was reached -- a real launch would"
            " have happened. This must never fire in section D."))

    with patch.object(subprocess_module, "run", run_mock_safety_net), \
         fence_patches[0], fence_patches[1]:
        # bare --set (const="last") -> reclaim menu of last epoch.
        rc, out, err = run_cli(["resume", "--set"] + cflags(claude_dir, db))
        ok = (rc == 0 and "available to reclaim" in out)
        record("D1: bare `--set` (no query) -> reclaim menu of 'last'", ok,
               f"rc={rc} out={out!r} err={err!r}")

        # --set NAME (bare) -> named-set reclaim menu.
        rc, out, err = run_cli(["resume", "--set", "PROBE-STACK"] +
                               cflags(claude_dir, db))
        ok = (rc == 0 and "PROBE-STACK" in out
              and "available to reclaim" in out)
        record("D2: `--set NAME` (bare) -> named-set reclaim menu", ok,
               f"rc={rc} out={out!r} err={err!r}")

        # --set NAME N -> resolves the right member deterministically;
        # verify via error path (member no longer in index) so the
        # subprocess safety net is never exercised.
        rc, out, err = run_cli(["resume", "--set", "PROBE-STALE", "1"] +
                               cflags(claude_dir, db))
        ok = (rc == 1 and "no longer in the index" in err)
        record("D3: `--set NAME N` resolves member N (proven via a stale"
               " member's clean pre-launch error, not a launch)", ok,
               f"rc={rc} err={err!r}")

        # --set N (bare number) against 'last' epoch: the seeded sessions
        # here are genuinely in-index and inside FENCE_OUTPUT's window, so
        # a full cli.main() run resolves to a REAL launchable member (the
        # safety net caught exactly this on the first pass -- see probe
        # run notes). Verify at the PARSER level instead: no command
        # logic executes, so there is zero launch risk regardless of what
        # `cmd_resume` would do with it.
        args = cli.build_parser().parse_args(["resume", "--set", "1"])
        ok = args.from_set == "1" and args.session_id is None
        record("D4: bare `--set N` parses to from_set='1', query=None"
               " (argparse level only -- cmd_resume's `from_set.isdigit()"
               " and query is None` then routes to index-into-'last',"
               " confirmed by code inspection, not executed here to"
               " avoid the real-launch path a full run reaches)", ok,
               f"args.from_set={args.from_set!r} args.session_id="
               f"{args.session_id!r}")

        # Passthrough: `--set NAME N -- --fork-session` (mock-free per the
        # checklist -- proven via the SAME clean pre-launch error, so the
        # finding stands even without the safety-net mock above).
        # NOTE: --claude-dir/--db MUST precede `--`, or argparse's `--`
        # split (cli._split_passthrough, which runs before argparse ever
        # sees argv) carries them into the passthrough tail instead of
        # parsing them as csb's own flags -- a real isolation leak onto
        # the actual ~/.claude store. test_resume_set.py's own `_run`
        # helper documents this exact trap.
        rc, out, err = run_cli(["resume", "--set", "PROBE-STALE", "1"] +
                               cflags(claude_dir, db) +
                               ["--", "--fork-session"])
        ok = (rc == 1 and "no longer in the index" in err)
        record("D5: `--set NAME N -- --fork-session` argv splits/parses"
               " correctly (reaches the SAME clean pre-launch resolution"
               " error as D3 -- proves passthrough didn't corrupt"
               " parsing); actual forwarding-into-launch is pytest-mock"
               " territory (test_forwards_passthrough_to_claude), not"
               " reproduced here per the no-real-launch rule", ok,
               f"rc={rc} err={err!r}")

        # NOPE with index still errors cleanly.
        rc, out, err = run_cli(["resume", "--set", "NOPE", "1"] +
                               cflags(claude_dir, db))
        ok = (rc == 1 and "No set named" in err)
        record("D6: `--set NOPE 1` -> rc=1 'No set named' (repeat, full"
               " matrix context)", ok, f"rc={rc} err={err!r}")


def section_e_output_hygiene():
    print("\n=== SECTION E: output hygiene (issue-number codes) ===")
    import re
    code_re = re.compile(r"#\d+")
    claude_dir, db, _conn = new_store("E_hygiene")

    help_targets = [
        ["--help"],
        ["set", "--help"],
        ["set", "show", "--help"],
        ["resume", "--help"],
    ]
    any_leak = False
    detail = []
    for argv in help_targets:
        rc, out, err = run_cli(argv)
        hits = code_re.findall(out) + code_re.findall(err)
        if hits:
            any_leak = True
            detail.append(f"{' '.join(argv)}: {hits}")
    record("E1: no issue-number codes (#NN) in --help output for"
           " csb/set/set show/resume", not any_leak, "; ".join(detail))

    # New-output-path scan for this release's own diff (the actual csb
    # commands this checklist exercises).
    with patch.object(subprocess_module, "run", MagicMock()):
        outputs = []
        rc, out, err = run_cli(["set", "show", "boot", "--claude-dir",
                                str(claude_dir), "--db", str(db)])
        outputs.append(out + err)
        rc, out, err = run_cli(["set", "list", "--claude-dir",
                                str(claude_dir), "--db", str(db)])
        outputs.append(out + err)
        rc, out, err = run_cli(["resume", "--set", "NOPE", "--claude-dir",
                                str(claude_dir), "--db", str(db)])
        outputs.append(out + err)
    combined = "\n".join(outputs)
    hits = code_re.findall(combined)
    record("E2: no issue-number codes in boot/set-list/resume-error"
           " command output", not hits, str(hits))


def main():
    section_a_tier_matrix()
    section_b_hookless()
    section_c_sections_block()
    section_d_set_grammar()
    section_e_output_hygiene()

    print("\n=== SUMMARY ===")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print(f"{passed} passed, {failed} failed, {len(RESULTS)} total")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name}\n    {detail}")
    print(f"\nSandbox root (not yet cleaned up): {BASE}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

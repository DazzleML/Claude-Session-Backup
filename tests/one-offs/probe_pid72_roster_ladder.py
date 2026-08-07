"""One-off probe: #72 pid-capture liveness -- roster ladder + degradation,
against a temp claude-dir/db, driven through the real public CLI
(``cli.main``) rather than pytest fixtures -- an independent cross-check
of tests/test_liveness_pid.py using the SAME technique
tests/one-offs/probe_v085_boot_set_checklist.py established (see that
script's precedent for why this is kept as a standalone script rather
than folded only into pytest).

Seeds a temp claude-dir + db with four registry-entry shapes and reads
``csb set show current --json`` against it:

  RUNNING   -- entry pid = a REAL live claude.exe pid (read-only
              discovered from the process table) -> expect "running"
  DEAD      -- entry pid = 4 (Windows System process; alive, but not
              claude-cli) -> expect "unverified" (registry-only tier)
  PIDLESS   -- no "pid" key at all -> argv-fallback tier (no live
              process names this made-up session, so still
              "unverified", but via the OTHER code path)
  STRIPPED  -- starts WITH a pid, then the pid key is hand-removed
              (mirrors checklist step 5's "hand-remove the pid key from
              one live entry") -> argv-fallback tier
  GARBAGE   -- entry pid = "not-a-pid" (garbage string) -> no crash,
              "unverified"

Never touches the real ~/.claude store: everything lives under a
tempfile.mkdtemp() tree, passed explicitly via --claude-dir/--db.
"""

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_session_backup.cli as cli  # noqa: E402
import claude_session_backup.live_registry as lr  # noqa: E402
from claude_session_backup.index import init_schema, open_db  # noqa: E402

REAL_LIVE_CLAUDE_PID = 35936  # read-only discovered from the real process
                              # table earlier in this run (hosts a real
                              # session, --resume 37cf7542-...)

UUID_RUNNING = "cccc1111-1111-1111-1111-000000000001"
UUID_DEAD = "cccc2222-1111-1111-1111-000000000002"
UUID_PIDLESS = "cccc3333-1111-1111-1111-000000000003"
UUID_STRIPPED = "cccc4444-1111-1111-1111-000000000004"
UUID_GARBAGE = "cccc5555-1111-1111-1111-000000000005"


def _insert(conn, sid, name):
    conn.execute(
        "INSERT INTO sessions (session_id, session_name, project,"
        " start_folder, started_at, last_active_at, is_fork) VALUES"
        " (?, ?, 'C--code-test', 'C:\\code\\test',"
        " '2026-08-01T00:00:00Z', '2026-08-01T13:00:00Z', 0)",
        (sid, name),
    )


def main():
    tmp = Path(tempfile.mkdtemp(prefix="csb_pid72_roster_"))
    claude_dir = tmp / "claude"
    db = tmp / "roster.db"

    conn = open_db(db)
    init_schema(conn, quiet=True)
    for sid, name in (
        (UUID_RUNNING, "RUNNING__session"),
        (UUID_DEAD, "DEAD__session"),
        (UUID_PIDLESS, "PIDLESS__session"),
        (UUID_STRIPPED, "STRIPPED__session"),
        (UUID_GARBAGE, "GARBAGE__session"),
    ):
        _insert(conn, sid, name)
    conn.commit()
    conn.close()

    lr.live_dir(claude_dir).mkdir(parents=True, exist_ok=True)

    def write_entry(sid, payload):
        lr.entry_path(claude_dir, sid).write_text(
            json.dumps(payload), encoding="utf-8")

    started = "2026-08-07T08:00:00Z"

    write_entry(UUID_RUNNING, {
        "session_id": UUID_RUNNING, "started_at": started,
        "source": "startup", "cwd": "C:/x", "pid": REAL_LIVE_CLAUDE_PID,
    })
    write_entry(UUID_DEAD, {
        "session_id": UUID_DEAD, "started_at": started,
        "source": "startup", "cwd": "C:/x", "pid": 4,
    })
    write_entry(UUID_PIDLESS, {
        "session_id": UUID_PIDLESS, "started_at": started,
        "source": "startup", "cwd": "C:/x",
    })
    # STRIPPED: written WITH a pid first, then the key is hand-removed --
    # mirrors the checklist's own step-5 wording exactly.
    write_entry(UUID_STRIPPED, {
        "session_id": UUID_STRIPPED, "started_at": started,
        "source": "startup", "cwd": "C:/x", "pid": 999999,
    })
    raw = json.loads(lr.entry_path(claude_dir, UUID_STRIPPED)
                     .read_text(encoding="utf-8"))
    del raw["pid"]
    write_entry(UUID_STRIPPED, raw)

    write_entry(UUID_GARBAGE, {
        "session_id": UUID_GARBAGE, "started_at": started,
        "source": "startup", "cwd": "C:/x", "pid": "not-a-pid",
    })

    argv = ["set", "show", "current", "--json",
            "--claude-dir", str(claude_dir), "--db", str(db)]
    print(f"claude_dir={claude_dir}")
    print(f"db={db}")
    print(f"argv={argv}")

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(argv)
    out = buf.getvalue()
    print("--- raw stdout ---")
    print(out)
    print(f"rc={rc}")

    payload = json.loads(out)
    by_id = {m["session_id"]: m for m in payload["members"]}

    results = []

    def check(label, cond, detail):
        results.append((label, bool(cond), detail))

    r = by_id.get(UUID_RUNNING, {})
    check("RUNNING is running",
         r.get("live_status") == "running" and r.get("pid") == REAL_LIVE_CLAUDE_PID,
         r)
    d = by_id.get(UUID_DEAD, {})
    check("DEAD (pid=4, non-claude) is unverified, not running",
         d.get("live_status") == "unverified" and d.get("pid") is None,
         d)
    p = by_id.get(UUID_PIDLESS, {})
    check("PIDLESS falls to argv tier, unverified",
         p.get("live_status") == "unverified" and p.get("pid") is None,
         p)
    s = by_id.get(UUID_STRIPPED, {})
    check("STRIPPED (pid hand-removed) falls to argv tier, unverified",
         s.get("live_status") == "unverified" and s.get("pid") is None,
         s)
    g = by_id.get(UUID_GARBAGE, {})
    check("GARBAGE pid string -- no crash, unverified",
         g.get("live_status") == "unverified" and g.get("pid") is None,
         g)

    print("\n=== RESULTS ===")
    all_pass = True
    for label, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"[{status}] {label}: {detail}")

    print(f"\nclaude_dir for cleanup: {claude_dir.parent}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())

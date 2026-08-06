#!/usr/bin/env python3
"""One-off probe: drives the v0.8.4 Live Session Registry checklist's
automatable scenarios end to end against a temp claude-dir, never the real
store.

Companion to the tester-unbounded sweep dated 2026-08-06
(tests/checklists/results/v0.8.4__results__2026-08-06.md). Builds a sandbox
claude-dir (git-initialized, hermetic), seeds a couple of indexed sessions,
then exercises: JSON validity, --window rejection on `set show current`,
reserved-name rejection, empty-registry message, the full hook lifecycle
(start->entry, end->removal, compact no-reset) via backup-hook.py with
CLAUDE_CONFIG_DIR set inline, a synthetic boundary sweep via `csb _check`,
`set new NAME --from current`, and the reclaim menu.

Run manually: python tests/one-offs/probe_v084_live_registry_checklist.py
Prints PASS/FAIL per scenario and a summary. Exits 1 if any scenario failed.
Leaves its sandbox under the OS temp dir for post-mortem inspection unless
--cleanup is passed (uses dz safedel when available).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = REPO / "hooks" / "scripts" / "backup-hook.py"

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" -- {detail}" if detail else ""))


def run_csb(args, claude_dir, db, env=None, input_text=None, cwd=None):
    cmd = ["csb", *args, "--claude-dir", str(claude_dir), "--db", str(db)]
    full_env = {**os.environ, **(env or {})}
    r = subprocess.run(
        cmd, capture_output=True, text=True, env=full_env,
        input=input_text, cwd=cwd, timeout=60,
    )
    return r


def run_hook(payload: dict, claude_dir: Path):
    """Fire the hook script with CLAUDE_CONFIG_DIR set inline (per sweep
    rules: env does not persist between subprocess calls, so it is passed
    explicitly on every invocation, and printf-equivalent (json.dumps via
    stdin=text) is used instead of shell echo)."""
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(claude_dir)}
    r = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, timeout=30,
    )
    return r


def build_sandbox(root: Path):
    claude = root / ".claude"
    projects = claude / "projects"
    projects.mkdir(parents=True)

    def make_session(sid, name, cwd_path, ts):
        pdir = projects / "C--sandbox-proj"
        pdir.mkdir(exist_ok=True)
        jsonl = pdir / f"{sid}.jsonl"
        events = [
            {"type": "custom-title", "customTitle": name, "sessionId": sid},
            {"type": "user", "timestamp": ts, "cwd": cwd_path,
             "sessionId": sid, "uuid": f"{sid}-u1"},
        ]
        jsonl.write_text("\n".join(json.dumps(e) for e in events) + "\n",
                         encoding="utf-8")
        return jsonl

    sid_a = "11111111-1111-1111-1111-111111111111"
    sid_b = "22222222-2222-2222-2222-222222222222"
    make_session(sid_a, "SANDBOX-ALPHA", "C:/sandbox/proj", "2026-08-06T10:00:00Z")
    make_session(sid_b, "SANDBOX-BETA", "C:/sandbox/proj", "2026-08-06T10:05:00Z")

    test_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "csb-test", "GIT_AUTHOR_EMAIL": "csb-test@test.local",
        "GIT_COMMITTER_NAME": "csb-test", "GIT_COMMITTER_EMAIL": "csb-test@test.local",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run(["git", "init", str(claude)], check=True, capture_output=True, env=test_env)
    subprocess.run(["git", "-C", str(claude), "config", "commit.gpgsign", "false"],
                   check=True, capture_output=True, env=test_env)
    subprocess.run(["git", "-C", str(claude), "add", "-A"], check=True, capture_output=True, env=test_env)
    subprocess.run(["git", "-C", str(claude), "commit", "--no-gpg-sign", "-m", "initial"],
                   check=True, capture_output=True, env=test_env)
    return claude, sid_a, sid_b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="csb_v090_probe_"))
    db = root / "index.db"
    claude, sid_a, sid_b = build_sandbox(root)
    print(f"Sandbox: {claude}")
    print(f"DB:      {db}")

    # Build the index from the two fixture sessions.
    r = run_csb(["update", "rebuild-index"], claude, db)
    record("sandbox: rebuild-index succeeds", r.returncode == 0, r.stderr.strip()[:200])

    # ---- Empty-registry message (fresh sandbox, no csb-live/ yet) -------
    r = run_csb(["set", "show", "current"], claude, db)
    ok = (r.returncode == 0
          and "no sessions for this boot" in r.stdout.lower()
          and "claude plugin update" in r.stderr)
    record("empty-registry message names `claude plugin update`", ok,
          f"rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")

    # ---- --window rejection on `set show current` (rc 2) ----------------
    r = run_csb(["set", "show", "current", "--window", "5"], claude, db)
    ok = r.returncode == 2 and "set show last" in r.stderr
    record("`set show current --window 5` rejected rc=2", ok,
          f"rc={r.returncode} stderr={r.stderr!r}")

    # ---- reserved-name rejection ----------------------------------------
    r = run_csb(["set", "new", "current", "x"], claude, db)
    ok = r.returncode == 2 and "reserved" in r.stderr.lower()
    record("`set new current x` rejected as reserved", ok,
          f"rc={r.returncode} stderr={r.stderr!r}")

    # ---- Hook lifecycle: SessionStart -> entry ---------------------------
    r = run_hook({"hook_event_name": "SessionStart", "source": "startup",
                 "session_id": sid_a, "cwd": "C:/sandbox/proj"}, claude)
    entry_a = claude / "csb-live" / f"{sid_a}.json"
    record("hook SessionStart writes registry entry", entry_a.exists(),
          f"hook rc={r.returncode} stderr={r.stderr[:200]!r}")
    started_at_1 = None
    if entry_a.exists():
        started_at_1 = json.loads(entry_a.read_text())["started_at"]

    # `set show current` should now show it as [no exit observed]
    # (no live process for this sandbox pid).
    r = run_csb(["set", "show", "current", "--json"], claude, db)
    ok = False
    try:
        payload = json.loads(r.stdout)
        members = payload.get("members", [])
        ok = (len(members) == 1 and members[0]["session_id"] == sid_a
             and members[0]["live_status"] == "unverified")
    except Exception as exc:  # noqa: BLE001
        ok = False
    record("`set show current` reflects the started entry", ok,
          f"rc={r.returncode} stdout={r.stdout[:300]!r}")

    # ---- source=compact must NOT reset started_at (write-if-missing) ----
    time.sleep(1.1)  # ensure a real clock delta would be observable
    r = run_hook({"hook_event_name": "SessionStart", "source": "compact",
                 "session_id": sid_a, "cwd": "C:/sandbox/proj"}, claude)
    started_at_2 = json.loads(entry_a.read_text())["started_at"] if entry_a.exists() else None
    record("compact SessionStart does not reset started_at",
          entry_a.exists() and started_at_1 == started_at_2,
          f"before={started_at_1} after={started_at_2}")

    # ---- SessionEnd -> removal (clean close) -----------------------------
    r = run_hook({"hook_event_name": "SessionEnd", "session_id": sid_a}, claude)
    record("hook SessionEnd removes registry entry", not entry_a.exists(),
          f"hook rc={r.returncode} stderr={r.stderr[:200]!r}")

    r = run_csb(["set", "show", "current"], claude, db)
    ok = r.returncode == 0 and "no sessions for this boot" in r.stdout.lower()
    record("`set show current` empty again after clean close", ok,
          f"rc={r.returncode} stdout={r.stdout!r}")

    # ---- JSON validity with a populated registry -------------------------
    run_hook({"hook_event_name": "SessionStart", "source": "startup",
             "session_id": sid_a, "cwd": "C:/sandbox/proj"}, claude)
    run_hook({"hook_event_name": "SessionStart", "source": "startup",
             "session_id": sid_b, "cwd": "C:/sandbox/proj"}, claude)
    r = run_csb(["set", "show", "current", "--json"], claude, db)
    json_ok = False
    try:
        payload = json.loads(r.stdout)
        json_ok = payload.get("kind") == "current" and len(payload["members"]) == 2
    except Exception:
        json_ok = False
    record("`set show current --json` parses (python -m json.tool equiv)",
          json_ok, r.stdout[:300])
    record("no issue-number codes in `set show current --json` output",
          "#" not in r.stdout, r.stdout[:300] if "#" in r.stdout else "")

    # human-mode too, grep for issue codes
    r_human = run_csb(["set", "show", "current"], claude, db)
    import re
    leaked = re.findall(r"#\d+", r_human.stdout) + re.findall(r"#\d+", r_human.stderr)
    record("no issue-number codes in `set show current` human output",
          not leaked, f"leaked={leaked}")

    # `set new NAME --from current` with 2 seeded registry entries ---------
    r = run_csb(["set", "new", "TONIGHT-PROBE", "--from", "current"], claude, db)
    ok = r.returncode == 0 and "2 session" in r.stdout
    record("`set new NAME --from current` promotes both live entries", ok,
          f"rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")

    r = run_csb(["set", "show", "TONIGHT-PROBE", "--json"], claude, db)
    try:
        payload = json.loads(r.stdout)
        ids = {m["session_id"] for m in payload["members"]}
        ok = ids == {sid_a, sid_b}
    except Exception:
        ok = False
    record("promoted set contains exactly the two live sessions", ok, r.stdout[:300])

    # ---- reclaim menu: close B, leave A open, check the menu -------------
    run_hook({"hook_event_name": "SessionEnd", "session_id": sid_b}, claude)
    r = run_csb(["resume", "set", "TONIGHT-PROBE"], claude, db)
    ok = (r.returncode == 0 and "SANDBOX-BETA" in r.stdout
         and "SANDBOX-ALPHA" not in r.stdout
         and "1 available to reclaim" in r.stdout
         and "1 currently open" in r.stdout)
    record("reclaim menu lists only the closed member, correct counts", ok,
          f"rc={r.returncode} stdout={r.stdout!r}")

    # ---- Boundary sweep: seed an old-timestamped entry, sweep via _check -
    live_dir = claude / "csb-live"
    old_sid = "33333333-3333-3333-3333-333333333333"
    old_entry = live_dir / f"{old_sid}.json"
    old_entry.write_text(json.dumps({
        "session_id": old_sid,
        "started_at": "2000-01-01T00:00:00Z",  # far in the past: pre-boot
        "source": "startup", "cwd": "C:/sandbox/proj",
    }), encoding="utf-8")
    # sid_a is still "open" (started this run, so its started_at is
    # "now" and will NOT sweep -- only old_sid should move).
    r = run_csb(["_check", "--quiet"], claude, db)
    # NOTE: _check with un-backed-up sessions present returns CHECK_GAP_EXIT
    # (10), not 0/1 -- the sandbox sessions were indexed via rebuild-index
    # but their jsonl mtimes are unrelated, so accept either 0 or 10 here;
    # what matters is the sweep side effect below.
    snapshot_path = live_dir / "last-shutdown.json"
    swept_ok = (not old_entry.exists()) and snapshot_path.exists()
    if swept_ok:
        snap = json.loads(snapshot_path.read_text())
        swept_ids = {e["session_id"] for e in snap.get("open_at_shutdown", [])}
        swept_ok = old_sid in swept_ids and sid_a not in swept_ids
    record("`_check` sweeps the pre-boot entry into last-shutdown.json",
          swept_ok, f"rc={r.returncode} old_entry_exists={old_entry.exists()}")

    r = run_csb(["set", "show", "current", "--json"], claude, db)
    try:
        payload = json.loads(r.stdout)
        ids = {m["session_id"] for m in payload["members"]}
        sweep_current_ok = old_sid not in ids and sid_a in ids
    except Exception:
        sweep_current_ok = False
    record("swept entry no longer appears in `set show current`",
          sweep_current_ok, r.stdout[:300])

    # ---- Latency probe: registry-first guard should not scan when the
    # resume target is NOT in the registry. Measured at the function level
    # (no real `claude` launch involved) by importing the package directly.
    sys.path.insert(0, str(REPO))
    from claude_session_backup import commands as _cmds  # noqa: E402

    config = {"claude_dir": str(claude), "index_path": str(db)}
    t0 = time.perf_counter()
    for _ in range(20):
        _cmds._live_pid_for(config, "99999999-9999-9999-9999-999999999999", None)
    t_not_in_registry = (time.perf_counter() - t0) / 20

    t0 = time.perf_counter()
    r_help = subprocess.run(["csb", "resume", "--help"], capture_output=True,
                            text=True, timeout=30)
    t_help = time.perf_counter() - t0

    record(
        "registry-first guard: not-in-registry lookup stays sub-scan-cost",
        t_not_in_registry < 0.05,
        f"avg={t_not_in_registry*1000:.2f}ms over 20 calls (no WMI scan "
        f"expected); `csb resume --help` baseline={t_help*1000:.1f}ms",
    )

    print()
    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"SUMMARY: {len(RESULTS) - n_fail}/{len(RESULTS)} passed, {n_fail} failed")
    print(f"Sandbox left at: {root}" if not args.cleanup else "")

    if args.cleanup:
        shutil.rmtree(root, ignore_errors=True)

    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())

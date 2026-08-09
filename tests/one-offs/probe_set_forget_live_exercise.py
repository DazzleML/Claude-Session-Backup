"""One-off: exercise `csb set forget` end to end via real subprocesses.

Builds a SCRATCH claude-dir under %TEMP% (never ~/.claude) with a
csb-live/ registry and a scratch sqlite index, then drives the real
`csb` console-script through every checklist scenario in
tests/checklists/v0.9.10__Feature__set-forget-registry-retraction.md
plus four adversarial probes (malformed entry JSON, an unlinkable
read-only entry, multi-target partial failure, and a path-traversal
attempt via a crafted session_id).

To fake "verifiably running" against the REAL (unmocked)
liveness.scan(), this spawns a copy of python.exe renamed to
claude.exe/claude and records ITS pid in a registry entry -- scan()'s
is_claude_cli() check matches on argv[0] shape, not on what actually
runs inside, so a renamed interpreter satisfies the real production
verification path without needing an actual Claude Code process. The
spawned process is one this script starts itself and kills itself at
the end (never touches a real session or foreign process).

Run: python tests/one-offs/probe_set_forget_live_exercise.py
"""
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

SCRATCH = Path(os.environ.get("TEMP", r"C:\Temp")) / "csb_forget_scratch"
CLAUDE_DIR = SCRATCH / "claude"
DB = SCRATCH / "cur.db"
LIVE_DIR = CLAUDE_DIR / "csb-live"

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" -- {detail}" if detail else ""))


def fresh_scratch():
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH, ignore_errors=True)
    CLAUDE_DIR.mkdir(parents=True)
    LIVE_DIR.mkdir(parents=True)


def init_db():
    from claude_session_backup.index import init_schema, open_db
    conn = open_db(DB)
    init_schema(conn, quiet=True)
    conn.close()


def insert_session(sid, name):
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT INTO sessions (session_id, session_name, project,"
        " start_folder, started_at, last_active_at, is_fork) VALUES"
        " (?, ?, 'C--code-test', 'C:\\code\\test',"
        " '2026-07-01T00:00:00Z', '2026-08-01T14:00:00Z', 0)",
        (sid, name),
    )
    conn.commit()
    conn.close()


def write_entry(sid, started_at, cwd, pid=None, raw_text=None, filename=None):
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = LIVE_DIR / (filename or f"{sid}.json")
    if raw_text is not None:
        path.write_text(raw_text, encoding="utf-8")
        return path
    body = {"session_id": sid, "started_at": started_at, "source": "startup",
            "cwd": cwd}
    if pid is not None:
        body["pid"] = pid
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def run_csb(*args):
    cp = subprocess.run(
        ["csb", "--claude-dir", str(CLAUDE_DIR), "--db", str(DB), *args],
        capture_output=True, text=True, timeout=30,
    )
    return cp.returncode, cp.stdout, cp.stderr


# ── fake "claude" process for real (unmocked) liveness verification ──────

def spawn_fake_claude():
    """Copy cmd.exe -> claude.exe, launch it idling, return (proc, pid, path).

    cmd.exe (unlike python.exe) has no dependent sibling DLLs, so a bare
    copy to a new directory starts cleanly -- an earlier attempt using a
    renamed python.exe silently crashed on startup (missing python3xx.dll
    beside the copy), leaving no live pid for scan() to find and
    invalidating the refusal/--force probes downstream.
    """
    src = Path(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"))
    fake = SCRATCH / "claude.exe"
    shutil.copy2(src, fake)
    proc = subprocess.Popen(
        [str(fake), "/c", "ping", "-n", "300", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    time.sleep(1.0)  # let it actually start before anyone scans for it
    return proc, proc.pid, fake


def main():
    fresh_scratch()
    init_db()

    UUID_STALE = "aaaaaaaa-0000-0000-0000-000000000001"
    UUID_LIVE = "bbbbbbbb-0000-0000-0000-000000000002"
    UUID_NOENTRY = "cccccccc-0000-0000-0000-000000000003"
    UUID_MALFORMED = "dddddddd-0000-0000-0000-000000000004"
    UUID_READONLY = "eeeeeeee-0000-0000-0000-000000000005"
    UUID_MULTI_A = "ffffffff-0000-0000-0000-000000000006"
    UUID_MULTI_B = "11111111-0000-0000-0000-000000000007"

    insert_session(UUID_STALE, "STALE__session")
    insert_session(UUID_LIVE, "LIVE__session")
    insert_session(UUID_NOENTRY, "NOENTRY__session")
    insert_session(UUID_MALFORMED, "MALFORMED__session")
    insert_session(UUID_READONLY, "READONLY__session")
    insert_session(UUID_MULTI_A, "MULTIA__session")
    insert_session(UUID_MULTI_B, "MULTIB__session")

    proc, fake_pid, fake_exe = spawn_fake_claude()
    try:
        # verify_entry's pid-reuse guard rejects a pid whose process
        # creation time is far newer than the entry's started_at (more
        # than _CREATION_SKEW_S=60s) -- correctly so, it is the guard
        # against a stale entry's pid having been reused by an unrelated
        # younger process. The fake process is created NOW, so its
        # entry's started_at must be a REAL recent timestamp, not a
        # fictional one, or the refusal path can never legitimately
        # trigger against it.
        from datetime import datetime, timedelta, timezone
        live_started = (datetime.now(timezone.utc) - timedelta(seconds=5)
                        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_entry(UUID_STALE, "2026-08-01T12:10:00Z", "C:/code/stale")
        write_entry(UUID_LIVE, live_started, "C:/code/live", pid=fake_pid)
        # UUID_NOENTRY: indexed, but deliberately NO registry file.
        write_entry(UUID_MALFORMED, None, None, raw_text="{not valid json::")
        write_entry(UUID_READONLY, "2026-08-01T12:40:00Z", "C:/code/ro")
        write_entry(UUID_MULTI_A, "2026-08-01T12:50:00Z", "C:/code/a")
        write_entry(UUID_MULTI_B, "2026-08-01T12:55:00Z", "C:/code/b")

        # sanity: real scan sees the fake claude.exe as a claude CLI proc
        rc, out, err = run_csb("set", "current", "--json")
        payload = json.loads(out) if rc == 0 else {}
        live_row = next((m for m in payload.get("members", [])
                          if m["session_id"] == UUID_LIVE), None)
        record("sanity: fake claude.exe verifies as running under REAL scan()",
               bool(live_row and live_row.get("live_status") == "running"
                    and live_row.get("pid") == fake_pid),
               f"rc={rc} live_row={live_row}")

        # ── Section 1: the refusal ──────────────────────────────────
        rc, out, err = run_csb("set", "forget", UUID_LIVE)
        record("checklist 1: refuses a verifiably-running session",
               rc == 1 and "RUNNING" in err and "--force" in err,
               f"rc={rc} err={err!r}")
        record("checklist 1: nothing removed on refusal",
               (LIVE_DIR / f"{UUID_LIVE}.json").exists())

        # ── Section 2: retract a stale entry ────────────────────────
        rc, out, err = run_csb("set", "forget", UUID_STALE)
        record("checklist 2: retracts a stale (unverified) entry",
               rc == 0 and "Retracted" in out,
               f"rc={rc} out={out!r}")
        record("checklist 2: file actually gone",
               not (LIVE_DIR / f"{UUID_STALE}.json").exists())

        # ── Section 3 (git-history claim) is store-dependent; the
        # scratch dir here is not a git repo, so this is a MANUAL item
        # against a real backup store per the checklist itself.

        # ── Section 4: row with no entry ────────────────────────────
        rc, out, err = run_csb("set", "forget", UUID_NOENTRY)
        record("checklist 4: no-entry row errors, does not succeed",
               rc == 1 and "nothing to retract" in err,
               f"rc={rc} err={err!r}")

        # ── Section 5: --force ───────────────────────────────────────
        rc, out, err = run_csb("set", "forget", UUID_LIVE, "--force")
        record("checklist 5: --force overrides the running refusal",
               rc == 0 and "Retracted" in out,
               f"rc={rc} out={out!r}")
        record("checklist 5: file gone despite process still running",
               not (LIVE_DIR / f"{UUID_LIVE}.json").exists())

        # ── Section 6: ordinary vocabulary + bare invocation ────────
        write_entry(UUID_LIVE, "2026-08-01T12:20:05Z", "C:/code/live2")
        rc, out, err = run_csb("set", "forget", "LIVE__session")
        record("checklist 6: resolves by session NAME",
               rc == 0 and "Retracted" in out, f"rc={rc} out={out!r}")

        write_entry(UUID_STALE, "2026-08-01T12:10:05Z", "C:/code/stale2")
        rc, out, err = run_csb("set", "forget", UUID_STALE[:8])
        record("checklist 6: resolves by UUID PREFIX",
               rc == 0 and "Retracted" in out, f"rc={rc} out={out!r}")

        rc, out, err = run_csb("set", "forget")
        record("checklist 6: bare invocation -> exit 2, teaches both forms",
               rc == 2 and "current:1" in err, f"rc={rc} err={err!r}")

        rc, out, err = run_csb("set", "rm", "current")
        record("checklist 6: `set rm current` still errors distinctly",
               rc == 1 and "No set named" in err, f"rc={rc} err={err!r}")

        # ── Adversarial (a): malformed entry JSON ───────────────────
        rc, out, err = run_csb("set", "forget", UUID_MALFORMED)
        record("adversarial (a): malformed JSON entry handled without crash",
               rc in (0, 1), f"rc={rc} out={out!r} err={err!r}")
        record("adversarial (a): malformed file actually removed on rc==0",
               (rc == 0) == (not (LIVE_DIR / f"{UUID_MALFORMED}.json").exists()),
               f"rc={rc} exists={(LIVE_DIR / f'{UUID_MALFORMED}.json').exists()}")

        # ── Adversarial (b): read-only entry file, unlink should fail ─
        ro_path = LIVE_DIR / f"{UUID_READONLY}.json"
        os.chmod(ro_path, stat.S_IREAD)
        rc, out, err = run_csb("set", "forget", UUID_READONLY)
        record("adversarial (b): read-only file -> non-crashing failure, exit 1",
               rc == 1 and "Could not retract" in err,
               f"rc={rc} out={out!r} err={err!r}")
        os.chmod(ro_path, stat.S_IWRITE)  # restore so cleanup can remove it
        record("adversarial (b): file survives the failed unlink",
               ro_path.exists())

        # ── Adversarial (c)-1: an UNRESOLVABLE token (typo/nonexistent
        # name) aborts at the shared _resolve_membership_args pre-pass,
        # BEFORE forget's own per-target loop ever runs -- this is a
        # different failure mode than a target that resolves but has no
        # registry entry (RV2), so both are worth recording separately.
        rc, out, err = run_csb("set", "forget", UUID_MULTI_A,
                                "does-not-exist-at-all", UUID_MULTI_B)
        a_gone = not (LIVE_DIR / f"{UUID_MULTI_A}.json").exists()
        b_gone = not (LIVE_DIR / f"{UUID_MULTI_B}.json").exists()
        record("adversarial (c)-1: unresolvable token aborts BEFORE any retraction "
               "(shared resolver's half-built-set rule, inherited from add/rm)",
               (not a_gone) and (not b_gone) and rc == 1,
               f"rc={rc} a_gone={a_gone} b_gone={b_gone} err={err!r}")

        # ── Adversarial (c)-2: the REAL question -- among targets that
        # all RESOLVE, does one target failing DURING the retraction
        # loop (no registry entry behind it, RV2) still let the others
        # be processed, and is the exit code right?
        rc, out, err = run_csb("set", "forget", UUID_MULTI_A,
                                UUID_NOENTRY, UUID_MULTI_B)
        a_gone = not (LIVE_DIR / f"{UUID_MULTI_A}.json").exists()
        b_gone = not (LIVE_DIR / f"{UUID_MULTI_B}.json").exists()
        record("adversarial (c)-2: mid-loop failure (no-entry target) still lets "
               "the OTHER resolvable targets retract, exit code reflects the miss",
               a_gone and b_gone and rc == 1 and "nothing to retract" in err,
               f"rc={rc} a_gone={a_gone} b_gone={b_gone} out={out!r} err={err!r}")

        # ── Adversarial (d): path traversal via crafted session_id ──
        canary = CLAUDE_DIR / "canary.json"
        canary.write_text('{"marker": "do not delete me"}', encoding="utf-8")
        traversal_file = LIVE_DIR / "evil.json"
        traversal_file.write_text(json.dumps({
            "session_id": "../canary", "started_at": "2026-08-01T13:00:00Z",
            "source": "startup", "cwd": "C:/nope",
        }), encoding="utf-8")
        rc, out, err = run_csb("set", "current", "--json")
        payload = json.loads(out) if rc == 0 else {}
        traversal_row = next(
            (m for m in payload.get("members", [])
             if m["session_id"] == "../canary"), None)
        record("adversarial (d)-1: crafted session_id surfaces as an addressable row",
               traversal_row is not None,
               f"row={traversal_row}")
        if traversal_row is not None:
            idx = traversal_row["index"]
            rc, out, err = run_csb("set", "forget", f"current:{idx}")
            record("adversarial (d)-2: forget on the crafted row -- canary survives?",
                   canary.exists(),
                   f"rc={rc} out={out!r} err={err!r} canary_exists={canary.exists()}")
    finally:
        # /T kills the whole tree (claude.exe's ping child too) -- a
        # bare proc.kill() only kills claude.exe itself and orphans ping.
        subprocess.run(["taskkill", "/PID", str(fake_pid), "/T", "/F"],
                       capture_output=True, check=False)
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        try:
            fake_exe.unlink()
        except OSError:
            pass

    print("\n=== SUMMARY ===")
    failed = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} probes passed")
    for name, ok, detail in failed:
        print(f"  FAIL: {name} -- {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

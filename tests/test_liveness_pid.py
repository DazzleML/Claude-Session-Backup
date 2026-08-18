"""Pid-capture liveness (#72): identity by lineage, not argv.

The hook records the hosting claude PID at SessionStart (run-hook.mjs
``process.ppid`` -> ``CSB_HOOK_HOST_PID`` -> the registry entry). An
entry that carries a pid verifies by THAT pid alone -- argv matching is
disabled for it, because a frozen argv can name a session its process
no longer hosts (the ghost), while the fork child's fresh UUID appears
in no argv at all (the false negative). Pid-less entries keep the argv
path -- the pre-upgrade contract is untouched.

Live repro that drove the design: 6 open sessions, 5 claude processes,
0 bare -- one ghost `[running]`, two running sessions unprovable, one
parent credited its fork's pid. See the pid-capture DWP.
"""

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import claude_session_backup.cli as cli
import claude_session_backup.live_registry as lr
import claude_session_backup.liveness as lv
from claude_session_backup.index import init_schema, open_db

BOOT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
STARTED = "2026-08-01T12:10:00Z"
STARTED_DT = datetime(2026, 8, 1, 12, 10, 0, tzinfo=timezone.utc)

UUID_GHOST = "aaaa1111-bbbb-cccc-dddd-000000000001"    # entry pid dead; argv still names it
UUID_SWITCHED = "aaaa2222-bbbb-cccc-dddd-000000000002"  # hosted by the process argv-naming GHOST
UUID_FPARENT = "aaaa3333-bbbb-cccc-dddd-000000000003"   # fork parent; own pid dead
UUID_FCHILD = "aaaa4444-bbbb-cccc-dddd-000000000004"    # fork child; argv names FPARENT

HOOK_PATH = (
    Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "backup-hook.py"
)


def _load_hook():
    spec = importlib.util.spec_from_file_location("csb_hook_pid", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _entry(session_id, pid=None, started_at=STARTED):
    payload = {"session_id": session_id, "started_at": started_at,
               "source": "startup", "cwd": "C:/code/x"}
    if pid is not None:
        payload["pid"] = pid
    return payload


# ── verify_entry: the D3 ladder, unit level ───────────────────────────


class TestVerifyEntry:
    def test_pid_bearing_entry_verifies_by_its_pid(self):
        scan = lv.LiveScan(by_pid={100: lv.ProcInfo(cmdline="claude")})
        assert lv.verify_entry(scan, _entry(UUID_SWITCHED, pid=100),
                               "NAME") == 100

    def test_ghost_dead_pid_ignores_argv_match(self):
        """THE ghost fix: the entry recorded its host; that host is gone.
        A live process whose frozen argv still names this session must
        NOT resurrect it -- that process is hosting something else."""
        scan = lv.LiveScan(
            by_pid={222: lv.ProcInfo(cmdline=f"claude --resume {UUID_GHOST}")},
            by_uuid={UUID_GHOST.lower(): 222},
        )
        assert lv.verify_entry(scan, _entry(UUID_GHOST, pid=111), None) is None

    def test_pidless_entry_keeps_argv_path(self):
        scan = lv.LiveScan(by_uuid={UUID_GHOST.lower(): 300})
        assert lv.verify_entry(scan, _entry(UUID_GHOST), None) == 300

    def test_pidless_entry_matches_by_name(self):
        scan = lv.LiveScan(by_name={"MY__session": 301})
        assert lv.verify_entry(scan, _entry(UUID_GHOST), "MY__session") == 301

    def test_pid_reuse_rejected_by_creation_time(self):
        """A younger claude on a recycled pid is not this session."""
        scan = lv.LiveScan(by_pid={100: lv.ProcInfo(
            cmdline="claude", created=STARTED_DT + timedelta(hours=1))})
        assert lv.verify_entry(scan, _entry(UUID_GHOST, pid=100), None) is None

    def test_creation_before_start_accepted(self):
        scan = lv.LiveScan(by_pid={100: lv.ProcInfo(
            cmdline="claude", created=STARTED_DT - timedelta(seconds=100))})
        assert lv.verify_entry(scan, _entry(UUID_GHOST, pid=100), None) == 100

    def test_creation_within_skew_accepted(self):
        scan = lv.LiveScan(by_pid={100: lv.ProcInfo(
            cmdline="claude", created=STARTED_DT + timedelta(seconds=30))})
        assert lv.verify_entry(scan, _entry(UUID_GHOST, pid=100), None) == 100

    def test_no_creation_time_degrades_to_no_guard(self):
        scan = lv.LiveScan(by_pid={100: lv.ProcInfo(cmdline="claude")})
        assert lv.verify_entry(scan, _entry(UUID_GHOST, pid=100), None) == 100

    # ── the resumed-session shape (2026-08-17) ────────────────────────
    # A session RESUMED after a reboot keeps its original started_at --
    # the hook preserves it so /compact cannot reset the open time -- but
    # its host process is BRAND NEW. Anchoring the reuse guard on
    # started_at therefore rejected a host that was merely younger than
    # the session, which every resumed session's host is. Measured live:
    # pid 11516, created 39.2h after started_at and 3s before pid_at,
    # reported `[no exit observed]` while csb ran inside it.
    #
    # pid_at is the pid's own provenance -- a process created AFTER csb
    # recorded that pid cannot be the process csb saw.
    def test_resumed_session_host_younger_than_session_verifies(self):
        entry = _entry(UUID_GHOST, pid=100)
        # Real ordering, as measured on 2026-08-17: the host process is
        # created, and the hook stamps pid_at seconds later (11516 was
        # born 3s before its pid_at). Both are ~2 days after started_at.
        entry["pid_at"] = "2026-08-03T12:15:00Z"
        scan = lv.LiveScan(by_pid={100: lv.ProcInfo(
            cmdline="claude",
            created=STARTED_DT + timedelta(days=2))})  # 2026-08-03T12:10Z
        assert lv.verify_entry(scan, entry, None) == 100

    def test_pid_reuse_after_pid_at_still_rejected(self):
        """The guard must keep working: a process born AFTER csb recorded
        the pid is a different process wearing a recycled number."""
        entry = _entry(UUID_GHOST, pid=100)
        entry["pid_at"] = "2026-08-03T12:00:00Z"
        scan = lv.LiveScan(by_pid={100: lv.ProcInfo(
            cmdline="claude",
            created=datetime(2026, 8, 3, 13, 0, 0, tzinfo=timezone.utc))})
        assert lv.verify_entry(scan, entry, None) is None

    def test_corrupt_early_pid_at_does_not_shadow_started_at(self):
        """A pid_at EARLIER than started_at is internally inconsistent --
        a session's pid is recorded at or after it opens. Letting the
        earlier stamp win permanently rejects a genuinely live host, and
        a false "not running" is the worse error here: hiding a live
        session is the failure the pid ladder exists to end.

        Regression introduced by v0.9.12's own anchor change and caught
        by the pre-ship sweep -- the anchor is the LATER of the two.
        """
        created = datetime(2026, 8, 12, 16, 23, 48, tzinfo=timezone.utc)
        scan = lv.LiveScan(by_pid={100: lv.ProcInfo(
            cmdline="claude", created=created)})
        # started_at POSTDATES the process -- the started_at anchor accepts.
        entry = _entry(UUID_GHOST, pid=100,
                       started_at="2026-08-13T00:00:00Z")
        assert lv.verify_entry(scan, dict(entry), None) == 100, "precondition"
        entry["pid_at"] = "2020-01-01T00:00:00Z"      # absurdly early
        assert lv.verify_entry(scan, entry, None) == 100

    def test_entry_without_pid_at_still_anchors_on_started_at(self):
        """Pre-#72 entries carry a pid but no pid_at -- the fallback keeps
        their guard behaviour exactly as it was."""
        scan = lv.LiveScan(by_pid={100: lv.ProcInfo(
            cmdline="claude", created=STARTED_DT + timedelta(hours=1))})
        assert lv.verify_entry(scan, _entry(UUID_GHOST, pid=100), None) is None

    def test_garbage_pid_is_no_proof(self):
        scan = lv.LiveScan(by_uuid={UUID_GHOST.lower(): 200})
        entry = _entry(UUID_GHOST)
        entry["pid"] = "not-a-pid"
        assert lv.verify_entry(scan, entry, None) is None


# ── the roster: all four #72 shapes through the public CLI ────────────


def _insert(conn, sid, name):
    conn.execute(
        "INSERT INTO sessions (session_id, session_name, project,"
        " start_folder, started_at, last_active_at, is_fork) VALUES"
        " (?, ?, 'C--code-test', 'C:\\code\\test',"
        " '2026-07-01T00:00:00Z', '2026-08-01T13:00:00Z', 0)",
        (sid, name),
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    db = tmp_path / "pid.db"
    conn = open_db(db)
    init_schema(conn, quiet=True)
    for sid, name in ((UUID_GHOST, "GHOST__session"),
                      (UUID_SWITCHED, "SWITCHED__session"),
                      (UUID_FPARENT, "FPARENT__session"),
                      (UUID_FCHILD, "FCHILD__session")):
        _insert(conn, sid, name)
    conn.commit()
    conn.close()

    lr.live_dir(claude_dir).mkdir(parents=True, exist_ok=True)
    for sid, pid in ((UUID_GHOST, 111), (UUID_SWITCHED, 222),
                     (UUID_FPARENT, 444), (UUID_FCHILD, 333)):
        lr.entry_path(claude_dir, sid).write_text(
            json.dumps(_entry(sid, pid=pid)), encoding="utf-8")

    monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
    # The live repro's process table: 222 argv-names GHOST but hosts
    # SWITCHED; 333 argv-names FPARENT but hosts the fork child.
    monkeypatch.setattr(lv, "scan", lambda: lv.LiveScan(
        by_pid={
            222: lv.ProcInfo(cmdline=f"claude --resume {UUID_GHOST}"),
            333: lv.ProcInfo(
                cmdline=f"claude --resume {UUID_FPARENT} --fork-session"),
        },
        by_uuid={UUID_GHOST.lower(): 222, UUID_FPARENT.lower(): 333},
    ))
    return SimpleNamespace(claude_dir=claude_dir, db=db)


def _current(env, capsys):
    assert cli.main(["set", "show", "current", "--json", "--claude-dir",
                     str(env.claude_dir), "--db", str(env.db)]) == 0
    payload = json.loads(capsys.readouterr().out)
    return {m["session_id"]: m for m in payload["members"]}


class TestCurrentRosterPidTruth:
    def test_ghost_is_not_running(self, env, capsys):
        """Red-green anchor: pre-#72 the argv match resurrects GHOST."""
        by_id = _current(env, capsys)
        assert by_id[UUID_GHOST]["live_status"] == "unverified"
        assert by_id[UUID_GHOST]["pid"] is None

    def test_switched_session_verifies_by_recorded_pid(self, env, capsys):
        by_id = _current(env, capsys)
        assert by_id[UUID_SWITCHED]["live_status"] == "running"
        assert by_id[UUID_SWITCHED]["pid"] == 222

    def test_fork_child_runs_and_parent_is_not_credited(self, env, capsys):
        by_id = _current(env, capsys)
        assert by_id[UUID_FCHILD]["live_status"] == "running"
        assert by_id[UUID_FCHILD]["pid"] == 333
        assert by_id[UUID_FPARENT]["live_status"] == "unverified"
        assert by_id[UUID_FPARENT]["pid"] is None


# ── pid-claim arbitration: one pid, one owner ─────────────────────────


def _member(sid, pid, status="running"):
    return {"session_id": sid, "live_status": status if pid else "unverified",
            "pid": pid}


class TestArbitration:
    def test_capture_beats_argv_the_legacy_ghost(self):
        """The live machine's exact scenario: an abandoned session's
        pid-LESS entry argv-matches the pid another session CAPTURED.
        The capture wins; the argv claim is the ghost."""
        switched = _member(UUID_SWITCHED, 222)
        ghost = _member(UUID_GHOST, 222)
        lv.arbitrate_pid_claims([
            (switched, _entry(UUID_SWITCHED, pid=222)),
            (ghost, _entry(UUID_GHOST)),          # pid-less -> argv claim
        ])
        assert switched["live_status"] == "running"
        assert switched["pid"] == 222
        assert ghost["live_status"] == "unverified"
        assert ghost["pid"] is None

    def test_freshest_capture_wins_between_two_stamps(self):
        """An in-app switch strands the old entry's stale captured pid;
        the new conversation's fresher stamp owns the host."""
        old = _member(UUID_GHOST, 222)
        new = _member(UUID_SWITCHED, 222)
        e_old = _entry(UUID_GHOST, pid=222)
        e_old["pid_at"] = "2026-08-01T12:00:00Z"
        e_new = _entry(UUID_SWITCHED, pid=222)
        e_new["pid_at"] = "2026-08-01T13:00:00Z"
        lv.arbitrate_pid_claims([(old, e_old), (new, e_new)])
        assert new["pid"] == 222
        assert old["live_status"] == "unverified" and old["pid"] is None

    def test_argv_only_claims_are_left_alone(self):
        """No capture in the dispute -> no stronger evidence to rule
        with; both rows keep their argv verdicts."""
        a = _member(UUID_GHOST, 300)
        b = _member(UUID_SWITCHED, 300)
        lv.arbitrate_pid_claims([
            (a, _entry(UUID_GHOST)), (b, _entry(UUID_SWITCHED)),
        ])
        assert a["pid"] == 300 and b["pid"] == 300

    def test_sole_claims_untouched(self):
        m = _member(UUID_GHOST, 100)
        lv.arbitrate_pid_claims([(m, _entry(UUID_GHOST, pid=100))])
        assert m["pid"] == 100 and m["live_status"] == "running"


class TestArbitrationThroughCli:
    def test_legacy_ghost_demoted_in_current_roster(self, tmp_path,
                                                    monkeypatch, capsys):
        """End to end: post-plugin-update this session captures its host
        pid; the abandoned session's legacy pid-less entry may no longer
        ride that pid's argv."""
        claude_dir = tmp_path / "claude"
        db = tmp_path / "arb.db"
        conn = open_db(db)
        init_schema(conn, quiet=True)
        _insert(conn, UUID_GHOST, "ABANDONED__session")
        _insert(conn, UUID_SWITCHED, "ACTIVE__session")
        conn.commit()
        conn.close()
        lr.live_dir(claude_dir).mkdir(parents=True, exist_ok=True)
        lr.entry_path(claude_dir, UUID_GHOST).write_text(
            json.dumps(_entry(UUID_GHOST)), encoding="utf-8")  # pid-less
        lr.entry_path(claude_dir, UUID_SWITCHED).write_text(
            json.dumps(_entry(UUID_SWITCHED, pid=222)), encoding="utf-8")
        monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
        monkeypatch.setattr(lv, "scan", lambda: lv.LiveScan(
            by_pid={222: lv.ProcInfo(
                cmdline=f"claude --resume {UUID_GHOST}")},
            by_uuid={UUID_GHOST.lower(): 222},
        ))
        assert cli.main(["set", "show", "current", "--json", "--claude-dir",
                         str(claude_dir), "--db", str(db)]) == 0
        by_id = {m["session_id"]: m
                 for m in json.loads(capsys.readouterr().out)["members"]}
        assert by_id[UUID_SWITCHED]["live_status"] == "running"
        assert by_id[UUID_SWITCHED]["pid"] == 222
        assert by_id[UUID_GHOST]["live_status"] == "unverified"
        assert by_id[UUID_GHOST]["pid"] is None


# ── registry semantics: newest-wins pid, write-if-missing started_at ──


class TestRecordSessionStart:
    def test_new_entry_records_pid(self, tmp_path):
        assert lr.record_session_start(tmp_path, UUID_GHOST,
                                       source="startup", pid=111) is True
        [entry] = lr.read_entries(tmp_path)
        assert entry["pid"] == 111
        assert entry["pid_at"] == entry["started_at"]  # one instant

    def test_refresh_updates_pid_at_stamp(self, tmp_path):
        lr.record_session_start(tmp_path, UUID_GHOST, pid=111)
        path = lr.entry_path(tmp_path, UUID_GHOST)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["pid_at"] = "2020-01-01T00:00:00Z"  # age the stamp
        path.write_text(json.dumps(payload), encoding="utf-8")
        lr.record_session_start(tmp_path, UUID_GHOST, source="compact",
                                pid=222)
        [entry] = lr.read_entries(tmp_path)
        assert entry["pid"] == 222
        assert entry["pid_at"] > "2020-01-01T00:00:00Z"

    def test_compact_heal_refreshes_pid_keeps_started_at(self, tmp_path):
        lr.record_session_start(tmp_path, UUID_GHOST, source="startup",
                                pid=111)
        before = json.loads(
            lr.entry_path(tmp_path, UUID_GHOST).read_text(encoding="utf-8"))
        assert lr.record_session_start(tmp_path, UUID_GHOST,
                                       source="compact", pid=222) is False
        after = json.loads(
            lr.entry_path(tmp_path, UUID_GHOST).read_text(encoding="utf-8"))
        assert after["pid"] == 222
        assert after["started_at"] == before["started_at"]
        assert after["source"] == "startup"  # first-open truth preserved

    def test_pidless_restart_leaves_entry_untouched(self, tmp_path):
        lr.record_session_start(tmp_path, UUID_GHOST, pid=111)
        lr.record_session_start(tmp_path, UUID_GHOST, source="compact")
        [entry] = lr.read_entries(tmp_path)
        assert entry["pid"] == 111

    def test_read_entries_tolerates_garbage_pid(self, tmp_path):
        lr.live_dir(tmp_path).mkdir(parents=True)
        lr.entry_path(tmp_path, UUID_GHOST).write_text(json.dumps(
            {"session_id": UUID_GHOST, "started_at": STARTED,
             "pid": "garbage"}), encoding="utf-8")
        [entry] = lr.read_entries(tmp_path)
        assert entry["pid"] is None


# ── the hook mirror (stdlib inline copy in backup-hook.py) ────────────


class TestHookMirror:
    @pytest.fixture
    def hook_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_DIR", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        bh = _load_hook()
        # These tests pin the ENV-channel semantics; the ancestry walk
        # (which would find pytest's real claude ancestor) has its own
        # class -- null its seam so the env fallback drives.
        monkeypatch.setattr(bh, "_process_table", lambda: None)
        return SimpleNamespace(bh=bh, dir=tmp_path, notes=[])

    def _note(self, hook_env):
        return hook_env.notes.append

    def test_start_stamps_host_pid_from_env(self, hook_env, monkeypatch):
        monkeypatch.setenv("CSB_HOOK_HOST_PID", "555")
        hook_env.bh._live_start(UUID_GHOST, "startup", "C:/x",
                                self._note(hook_env))
        raw = json.loads((hook_env.dir / "csb-live" /
                          f"{UUID_GHOST}.json").read_text(encoding="utf-8"))
        assert raw["pid"] == 555

    def test_refresh_updates_pid_preserves_started_at(self, hook_env,
                                                      monkeypatch):
        monkeypatch.setenv("CSB_HOOK_HOST_PID", "555")
        hook_env.bh._live_start(UUID_GHOST, "startup", "C:/x",
                                self._note(hook_env))
        path = hook_env.dir / "csb-live" / f"{UUID_GHOST}.json"
        before = json.loads(path.read_text(encoding="utf-8"))
        monkeypatch.setenv("CSB_HOOK_HOST_PID", "666")
        hook_env.bh._live_start(UUID_GHOST, "compact", "C:/x",
                                self._note(hook_env))
        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["pid"] == 666
        assert after["started_at"] == before["started_at"]

    def test_garbled_env_degrades_to_no_pid(self, hook_env, monkeypatch):
        monkeypatch.setenv("CSB_HOOK_HOST_PID", "not-a-pid")
        hook_env.bh._live_start(UUID_GHOST, "startup", "C:/x",
                                self._note(hook_env))
        raw = json.loads((hook_env.dir / "csb-live" /
                          f"{UUID_GHOST}.json").read_text(encoding="utf-8"))
        assert "pid" not in raw

    def test_missing_env_degrades_to_no_pid(self, hook_env, monkeypatch):
        monkeypatch.delenv("CSB_HOOK_HOST_PID", raising=False)
        monkeypatch.setattr(hook_env.bh, "_process_table", lambda: None)
        hook_env.bh._live_start(UUID_GHOST, "startup", "C:/x",
                                self._note(hook_env))
        raw = json.loads((hook_env.dir / "csb-live" /
                          f"{UUID_GHOST}.json").read_text(encoding="utf-8"))
        assert "pid" not in raw


class TestHostWalk:
    """#78: Claude Code runs hook commands through a shell, so node's
    ppid is a TRANSIENT cmd/sh that dies with the hook -- captured live
    twice (fork-birth and compact both stamped already-dead shells).
    The walk climbs to the real claude CLI ancestor."""

    TABLE = [
        (100, 90, "python.exe backup-hook.py"),
        (90, 80, "node run-hook.mjs"),
        (80, 70, "C:\\Windows\\system32\\cmd.exe /c node ..."),  # transient
        (70, 1, 'C:\\Users\\X\\.local\\bin\\claude.EXE --resume abc'),
        (1, 1, "wininit.exe"),
    ]

    def _bh(self, monkeypatch, table, env_pid=None):
        bh = _load_hook()
        monkeypatch.setattr(bh, "_process_table", lambda: table)
        if env_pid is not None:
            monkeypatch.setenv("CSB_HOOK_HOST_PID", str(env_pid))
        else:
            monkeypatch.delenv("CSB_HOOK_HOST_PID", raising=False)
        return bh

    def test_walk_climbs_past_the_transient_shell(self, monkeypatch):
        """RED-GREEN anchor: env ppid says 80 (the shell); the walk
        must return 70 (the claude CLI that outlives the hook)."""
        bh = self._bh(monkeypatch, self.TABLE, env_pid=80)
        assert bh._host_pid(start_pid=100) == 70

    def test_desktop_claude_ancestor_is_skipped(self, monkeypatch):
        table = [
            (100, 90, "python.exe backup-hook.py"),
            (90, 85, "node run-hook.mjs"),
            (85, 70, "Claude.exe --type=renderer"),  # Desktop, not CLI
            (70, 1, "claude --resume xyz"),
        ]
        bh = self._bh(monkeypatch, table)
        assert bh._host_pid(start_pid=100) == 70

    def test_no_claude_ancestor_falls_back_to_env(self, monkeypatch):
        table = [
            (100, 90, "python.exe backup-hook.py"),
            (90, 50, "node run-hook.mjs"),
            (50, 1, "bash"),
            (1, 1, "init"),
        ]
        bh = self._bh(monkeypatch, table, env_pid=90)
        assert bh._host_pid(start_pid=100) == 90

    def test_unreadable_table_falls_back_to_env(self, monkeypatch):
        bh = self._bh(monkeypatch, None, env_pid=42)
        assert bh._host_pid(start_pid=100) == 42


# ── scan(): by_pid construction and old-mock tolerance ────────────────


class TestScanShape:
    def test_by_pid_built_from_four_tuples(self, monkeypatch):
        created = datetime(2026, 8, 1, tzinfo=timezone.utc)
        monkeypatch.setattr(lv, "_enumerate_processes", lambda: [
            (100, "claude.exe", f"claude --resume {UUID_GHOST}", created),
            (200, "claude.exe", "claude", None),
            (300, "chrome.exe", "chrome --stuff", created),
        ])
        scan = lv.scan()
        assert set(scan.by_pid) == {100, 200}
        assert scan.by_pid[100].created == created
        assert scan.by_pid[200].created is None
        assert scan.by_uuid == {UUID_GHOST.lower(): 100}
        assert scan.bare_pids == (200,)

    def test_scan_tolerates_legacy_three_tuples(self, monkeypatch):
        """Old mocks of the seam stay valid: created defaults to None."""
        monkeypatch.setattr(lv, "_enumerate_processes", lambda: [
            ("42", "claude", "claude --resume SOME__name"),
        ])
        scan = lv.scan()
        assert scan.by_pid[42].created is None
        assert scan.by_name == {"SOME__name": "42"}


class TestElevatedSessionVerification:
    """An ELEVATED session hides its cmdline from a non-elevated scan.

    Caught live 2026-08-08: a running `claude.exe` under an admin
    Windows Terminal returned an empty CommandLine (and ExecutablePath)
    to a non-elevated Win32_Process query while `Name` still read fine.
    The cmdline-only gate dropped it from `by_pid`, so pid verification
    -- the mechanism #72 added precisely to stop depending on argv --
    failed, and a running session rendered `[no exit observed]`.
    """

    ENTRY = {
        "session_id": "eeee0001-bbbb-cccc-dddd-000000000001",
        "started_at": "2026-08-08T17:42:02Z",
        "pid": 17668,
        "pid_at": "2026-08-08T17:42:02Z",
    }

    def _scan_with_hidden_cmdline(self, monkeypatch):
        """The exact shape observed: name reads, cmdline does not."""
        monkeypatch.setattr(
            lv, "_enumerate_processes",
            lambda: [(17668, "claude.exe", "", None)])
        return lv.scan()

    def test_elevated_session_verifies_by_pid(self, monkeypatch):
        """RED-GREEN anchor: name-only identification must still admit
        the process to by_pid, so the captured pid verifies."""
        scan = self._scan_with_hidden_cmdline(monkeypatch)
        assert lv.verify_entry(scan, self.ENTRY, None) == 17668

    def test_hidden_cmdline_contributes_no_argv_identity(self, monkeypatch):
        """A nameless match carries no identifier: it must NOT land in
        the argv maps, or a cmdline-less process would become a wildcard
        for name/uuid matching."""
        scan = self._scan_with_hidden_cmdline(monkeypatch)
        assert scan.by_uuid == {}
        assert scan.by_name == {}
        assert 17668 in scan.by_pid

    def test_desktop_claude_still_excluded_when_cmdline_readable(
            self, monkeypatch):
        """The name gate must not resurrect Claude Desktop: with a
        READABLE desktop cmdline the existing exclusion still wins."""
        desktop = (r'"C:\Users\X\AppData\Local\AnthropicClaude'
                   r'\app-1.25927.0\claude.exe" --type=renderer')
        monkeypatch.setattr(
            lv, "_enumerate_processes",
            lambda: [(4242, "claude.exe", desktop, None)])
        scan = lv.scan()
        assert 4242 not in scan.by_pid

    def test_creation_guard_still_applies_to_name_only_matches(
            self, monkeypatch):
        """Name-only admission does not weaken the pid-reuse guard: a
        process created well AFTER the entry is still rejected."""
        from datetime import datetime, timedelta, timezone
        later = datetime(2026, 8, 8, 18, 30, tzinfo=timezone.utc)
        monkeypatch.setattr(
            lv, "_enumerate_processes",
            lambda: [(17668, "claude.exe", "", later)])
        scan = lv.scan()
        assert lv.verify_entry(scan, self.ENTRY, None) is None


class TestElevatedSessionAdversarial:
    """Probes for the v0.9.9 elevation fix, beyond the RED-GREEN anchor.

    ``is_claude_process_name`` is a WEAKER identifier than argv by its
    own docstring's admission. These tests pin exactly how weak, so a
    future change cannot silently widen or narrow the boundary without
    a test noticing.
    """

    def test_non_claude_process_named_claude_with_empty_cmdline_is_admitted(
            self, monkeypatch):
        """A process that is NOT actually the Claude CLI, but happens to
        be named 'claude.exe' with an unreadable cmdline, IS admitted to
        by_pid -- liveness.py has no way to rule it out from name alone.
        This is the accepted tradeoff the docstring names, not a bug:
        containment is that mere by_pid admission verifies nothing by
        itself (see the next test) -- only an EXACT recorded-pid match
        within the creation-time skew window would ever treat it as a
        live session, and that match is what the hook's own capture
        (not this scan) is responsible for being trustworthy."""
        monkeypatch.setattr(
            lv, "_enumerate_processes",
            lambda: [(31337, "claude.exe", "", None)])
        scan = lv.scan()
        assert 31337 in scan.by_pid
        assert scan.by_uuid == {} and scan.by_name == {}

    def test_desktop_shaped_process_admitted_when_cmdline_hidden_but_harmless(
            self, monkeypatch):
        """'Claude Desktop stays excluded' (checklist section 3) holds
        for a READABLE Desktop cmdline via DESKTOP_MARKERS -- see
        test_desktop_claude_still_excluded_when_cmdline_readable. It is
        NOT a structural exclusion at the by_pid level once cmdline is
        hidden: name alone cannot distinguish Desktop's claude.exe from
        the CLI's (the module's own docstring says so). The real
        containment argument is registry-level, not scan-level: no
        registry entry ever records Desktop's pid, so admission here
        must never cause an UNRELATED entry to verify against it."""
        monkeypatch.setattr(
            lv, "_enumerate_processes",
            lambda: [(5150, "Claude.exe", "", None)])  # Desktop-shaped, hidden
        scan = lv.scan()
        assert 5150 in scan.by_pid  # admitted -- name alone can't rule it out
        other_entry = {"session_id": UUID_GHOST, "started_at": STARTED,
                       "pid": 9001, "pid_at": STARTED}
        assert lv.verify_entry(scan, other_entry, None) is None

    def test_admission_precedes_creation_guard_rejection(self, monkeypatch):
        """Confirms the TWO-STEP sequence explicitly: a future-created,
        name-only match IS admitted to by_pid (scan()'s job) and is THEN
        rejected by the creation-time guard (verify_entry's job) --
        rather than simply being absent from by_pid altogether, which
        would be the pre-fix shape and would make a guard test pass for
        the wrong reason (nothing to guard against)."""
        later = datetime(2026, 8, 8, 18, 30, tzinfo=timezone.utc)
        monkeypatch.setattr(
            lv, "_enumerate_processes",
            lambda: [(17668, "claude.exe", "", later)])
        scan = lv.scan()
        assert 17668 in scan.by_pid, "must be admitted despite future creation"
        entry = {"session_id": "eeee0001-bbbb-cccc-dddd-000000000001",
                 "started_at": "2026-08-08T17:42:02Z", "pid": 17668,
                 "pid_at": "2026-08-08T17:42:02Z"}
        assert lv.verify_entry(scan, entry, None) is None, \
            "creation-time guard must still reject it"

    def test_two_distinct_elevated_sessions_dont_cross_verify(
            self, monkeypatch):
        """Two SEPARATE elevated sessions (each admitted via the
        name-only path, each with its own recorded pid) must verify
        independently through arbitrate_pid_claims -- broadening by_pid
        must not make arbitration see a false shared claim just because
        both rows took the same (name-only) admission path."""
        monkeypatch.setattr(
            lv, "_enumerate_processes",
            lambda: [(2001, "claude.exe", "", None),
                     (2002, "claude.exe", "", None)])
        scan = lv.scan()
        entry_a = {"session_id": UUID_GHOST, "started_at": STARTED,
                  "pid": 2001, "pid_at": STARTED}
        entry_b = {"session_id": UUID_SWITCHED, "started_at": STARTED,
                  "pid": 2002, "pid_at": STARTED}
        pid_a = lv.verify_entry(scan, entry_a, None)
        pid_b = lv.verify_entry(scan, entry_b, None)
        member_a = _member(UUID_GHOST, pid_a)
        member_b = _member(UUID_SWITCHED, pid_b)
        lv.arbitrate_pid_claims([(member_a, entry_a), (member_b, entry_b)])
        assert member_a["pid"] == 2001 and member_a["live_status"] == "running"
        assert member_b["pid"] == 2002 and member_b["live_status"] == "running"

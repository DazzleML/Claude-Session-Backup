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


# ── registry semantics: newest-wins pid, write-if-missing started_at ──


class TestRecordSessionStart:
    def test_new_entry_records_pid(self, tmp_path):
        assert lr.record_session_start(tmp_path, UUID_GHOST,
                                       source="startup", pid=111) is True
        [entry] = lr.read_entries(tmp_path)
        assert entry["pid"] == 111

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
        return SimpleNamespace(bh=_load_hook(), dir=tmp_path,
                               notes=[])

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
        hook_env.bh._live_start(UUID_GHOST, "startup", "C:/x",
                                self._note(hook_env))
        raw = json.loads((hook_env.dir / "csb-live" /
                          f"{UUID_GHOST}.json").read_text(encoding="utf-8"))
        assert "pid" not in raw


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

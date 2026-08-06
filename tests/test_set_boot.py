"""`csb set show boot`: the epoch in progress (#68 R1).

Everything active since this boot, with the full tier ladder -- including
`[exited]`, which only this view can show: an erased registry entry IS an
observed exit. Boot time and the scan are patched at their seams; the
registry is real files in a temp claude dir.
"""

import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import claude_session_backup.cli as cli
import claude_session_backup.commands as commands_module
import claude_session_backup.live_registry as lr
import claude_session_backup.liveness as lv
from claude_session_backup.index import init_schema, open_db
from claude_session_backup.session_sets import get_set

BOOT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

UUID_RUN = "11111111-bbbb-cccc-dddd-000000000001"   # registry + process
UUID_UNV = "22222222-bbbb-cccc-dddd-000000000002"   # registry only
UUID_EXITED = "44444444-bbbb-cccc-dddd-000000000004"  # activity, no entry
UUID_OLD = "55555555-bbbb-cccc-dddd-000000000005"   # pre-boot activity
UUID_NEW = "33333333-bbbb-cccc-dddd-000000000003"   # registry, unindexed


def _insert(conn, sid, name, last_active):
    conn.execute(
        "INSERT INTO sessions (session_id, session_name, project,"
        " start_folder, started_at, last_active_at, is_fork) VALUES"
        " (?, ?, 'C--code-test', 'C:\\code\\test',"
        " '2026-07-01T00:00:00Z', ?, 0)",
        (sid, name, last_active),
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    db = tmp_path / "boot.db"
    conn = open_db(db)
    init_schema(conn, quiet=True)
    _insert(conn, UUID_RUN, "RUNNING__session", "2026-08-01T14:00:00Z")
    _insert(conn, UUID_UNV, "UNVERIFIED__session", "2026-08-01T13:00:00Z")
    _insert(conn, UUID_EXITED, "EXITED__session", "2026-08-01T12:30:00Z")
    _insert(conn, UUID_OLD, "PREBOOT__session", "2026-07-20T00:00:00Z")
    conn.commit()
    conn.close()

    lr.live_dir(claude_dir).mkdir(parents=True, exist_ok=True)
    for sid, ts in ((UUID_RUN, "2026-08-01T12:10:00Z"),
                    (UUID_UNV, "2026-08-01T12:20:00Z"),
                    (UUID_NEW, "2026-08-01T12:40:00Z")):
        lr.entry_path(claude_dir, sid).write_text(json.dumps({
            "session_id": sid, "started_at": ts, "source": "startup",
            "cwd": "C:/code/fresh",
        }), encoding="utf-8")

    monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
    monkeypatch.setattr(
        lv, "scan",
        lambda: lv.LiveScan(by_uuid={UUID_RUN.lower(): 4242}, ok=True))

    run_mock = MagicMock(return_value=SimpleNamespace(returncode=0))
    import subprocess as subprocess_module
    monkeypatch.setattr(subprocess_module, "run", run_mock)
    return SimpleNamespace(claude_dir=claude_dir, db=db, run=run_mock)


def _run(env, *argv):
    return cli.main(list(argv) + ["--claude-dir", str(env.claude_dir),
                                  "--db", str(env.db)])


class TestTierMatrix:
    """The R1 DWP's D2 table, row by row."""

    def test_all_four_tiers(self, env, capsys):
        assert _run(env, "set", "show", "boot", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["kind"] == "boot"
        assert payload["hooks_active"] is True
        by_id = {m["session_id"]: m for m in payload["members"]}
        assert by_id[UUID_RUN]["live_status"] == "running"
        assert by_id[UUID_RUN]["pid"] == 4242
        assert by_id[UUID_UNV]["live_status"] == "unverified"
        assert by_id[UUID_EXITED]["live_status"] == "exited"
        assert UUID_OLD not in by_id           # pre-boot activity excluded
        assert by_id[UUID_NEW]["in_index"] is False   # appendix member

    def test_ordering_activity_then_registry_appendix(self, env, capsys):
        """Index rows in activity order; unindexed registry entries
        appended AFTER so their churn never renumbers indexed rows."""
        _run(env, "set", "show", "boot", "--json")
        payload = json.loads(capsys.readouterr().out)
        ids = [m["session_id"] for m in payload["members"]]
        assert ids == [UUID_EXITED, UUID_UNV, UUID_RUN, UUID_NEW]
        assert [m["index"] for m in payload["members"]] == [1, 2, 3, 4]

    def test_human_rendering(self, env, capsys):
        assert _run(env, "set", "show", "boot") == 0
        out = capsys.readouterr().out
        assert "Active this boot" in out
        assert "1 running" in out and "1 exited" in out
        assert "[exited]" in out
        assert "row numbers reflect this invocation" in out
        # The "when" column: activity recency relative to NOW, so the
        # roster carries its own timeline ("8d18h ago"), like last's
        # "before shutdown" gaps.
        assert " ago" in out
        assert "before shutdown" not in out
        # Running row: stable name-based fork hint, never a plain resume.
        assert "csb resume RUNNING__session -- --fork-session" in out


class TestHooklessDegradation:
    def test_no_exited_tag_without_hook_evidence(self, tmp_path, capsys,
                                                 monkeypatch):
        """Red-green target: without hook evidence, NO row may claim
        [exited] -- rows render unadorned plus one disclosure footer."""
        claude_dir = tmp_path / "hookless"
        claude_dir.mkdir()
        db = tmp_path / "hookless.db"
        conn = open_db(db)
        init_schema(conn, quiet=True)
        _insert(conn, UUID_EXITED, "SOME__session", "2026-08-01T12:30:00Z")
        conn.commit()
        conn.close()
        monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
        rc = cli.main(["set", "show", "boot", "--claude-dir",
                       str(claude_dir), "--db", str(db)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "[exited]" not in out
        assert "exit detection unavailable" in out

    def test_json_reports_hooks_inactive(self, tmp_path, capsys,
                                         monkeypatch):
        claude_dir = tmp_path / "hookless2"
        claude_dir.mkdir()
        db = tmp_path / "h2.db"
        conn = open_db(db)
        init_schema(conn, quiet=True)
        _insert(conn, UUID_EXITED, "SOME__session", "2026-08-01T12:30:00Z")
        conn.commit()
        conn.close()
        monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
        cli.main(["set", "show", "boot", "--json", "--claude-dir",
                  str(claude_dir), "--db", str(db)])
        payload = json.loads(capsys.readouterr().out)
        assert payload["hooks_active"] is False
        assert payload["members"][0]["live_status"] is None


class TestFlags:
    def test_window_narrows_with_canonical_indices(self, env, capsys,
                                                   monkeypatch):
        """--window on boot: reference instant is NOW; gaps, never
        renumbering."""
        from datetime import datetime as real_dt
        # Freeze "now" via the roster's as_of by narrowing to 1h: with
        # real now >> fixture times, everything falls outside the window.
        assert _run(env, "set", "show", "boot", "--window", "1",
                    "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["roster_size"] == 4
        assert payload["members"] == []
        assert payload["hidden_by_window"] >= 3

    def test_open_rejected_on_boot(self, env, capsys):
        rc = _run(env, "set", "show", "boot", "--open")
        assert rc == 2
        assert "epoch in progress" in capsys.readouterr().err

    def test_window_still_rejected_on_current(self, env, capsys):
        rc = _run(env, "set", "show", "current", "--window", "5")
        assert rc == 2

    def test_from_boot_promotion(self, env, capsys):
        assert _run(env, "set", "new", "BOOT-AUDIT", "--from", "boot") == 0
        entry = get_set(env.claude_dir, "BOOT-AUDIT")
        ids = [m["session_id"] for m in entry["members"]]
        assert set(ids) == {UUID_RUN, UUID_UNV, UUID_EXITED, UUID_NEW}

    def test_boot_unknown_errors_clearly(self, env, capsys, monkeypatch):
        monkeypatch.setattr(lr, "current_boot_utc", lambda: None)
        rc = _run(env, "set", "show", "boot")
        assert rc == 1
        assert "Cannot determine the current boot time" in \
            capsys.readouterr().err


class TestReservedGrammar:
    """Amendment 1: the FULL grammar is reserved from R1."""

    @pytest.mark.parametrize("name", ["boot", "BOOT", "last~1", "last~42",
                                      "2026-8-9", "2026-08-09"])
    def test_reserved_shapes_rejected(self, env, name, capsys):
        rc = _run(env, "set", "new", name, UUID_RUN[:8])
        assert rc == 2
        assert "reserved" in capsys.readouterr().err

    @pytest.mark.parametrize("name", ["last-1", "booted", "2026-8-9__topic",
                                      "last~x", "BOOT-AUDIT"])
    def test_near_misses_still_legal(self, env, name, capsys):
        rc = _run(env, "set", "new", name, UUID_RUN[:8])
        assert rc == 0, capsys.readouterr().err

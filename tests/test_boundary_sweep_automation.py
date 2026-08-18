"""CLI-level automation of the v0.9.12 human test checklist, Section 2 +
HV.3 + HV.6 (``tests/checklists/v0.9.12__Fix__boundary-sweep-liveness-and-
resumed-sessions.md``).

The underlying primitives (``sweep_boundary``, ``open_at_shutdown``,
``split_by_boot``) are exercised directly in ``test_live_registry.py``.
This file closes the CLI-level gap: does the checklist's claim hold true
when driven through ``cli.main()`` the way a human running the checklist
actually would -- ``csb set show last``, ``csb set show last~1``,
``csb set current`` -- rather than only through the library functions
those commands call.

Fixture pattern mirrors ``tests/test_set_current.py``'s ``env`` fixture:
a scratch ``--claude-dir``/``--db`` pair, ``current_boot_utc`` and the
Windows event-log fence reader patched at their module seams. Never
touches a real ``~/.claude``.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

import claude_session_backup.cli as cli
import claude_session_backup.live_registry as lr
from claude_session_backup.index import init_schema, open_db

BOOT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    db = tmp_path / "cur.db"
    conn = open_db(db)
    init_schema(conn, quiet=True)
    conn.close()
    monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
    from types import SimpleNamespace
    return SimpleNamespace(claude_dir=claude_dir, db=db)


def _run(env, *argv):
    flags = ["--claude-dir", str(env.claude_dir), "--db", str(env.db)]
    return cli.main(list(argv) + flags)


def _patch_two_epoch_history(monkeypatch):
    """Event log with TWO full, distinct epochs:

    * ``last``   -- shutdown 2026-07-25T16:16:32Z, boot 2026-08-01T12:00:30Z
      (a long-idle machine; matches the ``BOOT`` constant this module
      patches ``current_boot_utc`` to, within tolerance).
    * ``last~1`` -- shutdown 2026-07-15T08:16:00Z, boot 2026-07-15T08:18:17Z.

    ``enumerate_epochs`` attaches a boot fence to the epoch it ENDS (the
    nearest boot after that epoch's shutdown), so a boot between the two
    shutdowns resolves to ``last~1``'s boot, not ``last``'s -- verified
    against ``claude_session_backup.epochs.enumerate_epochs`` directly
    before trusting it in these tests (see the module docstring notes).
    """
    import claude_session_backup.epochs as epochs
    monkeypatch.setattr(epochs.sys, "platform", "win32")
    monkeypatch.setattr(epochs, "_run_powershell", lambda *a, **k: (
        "2026-08-01T12:00:30.0000000Z|6005\n"
        "2026-07-25T16:16:32.0000000Z|6006\n"
        "2026-07-15T08:18:17.0000000Z|6005\n"
        "2026-07-15T08:16:00.0000000Z|6006\n"
    ))


class TestSection2_1_ReadThroughOnlyAppliesToCurrentBoot:
    """2.1 -- `set last~1` (an OLDER epoch) must never derive badges from
    TODAY's live registry entries. Entries record when a session opened,
    never which shutdown it died at -- deriving for an older epoch would
    credit sessions to the wrong restart."""

    def test_older_epoch_does_not_derive_from_current_registry(
            self, env, capsys, monkeypatch):
        _patch_two_epoch_history(monkeypatch)
        # A registry entry that exists RIGHT NOW in the live directory,
        # whose started_at PREDATES last~1's own boot instant
        # (2026-07-15T08:18:17Z) -- i.e. it is exactly the shape that
        # WOULD satisfy the "pre-boot" derivation test if read-through
        # incorrectly applied the current-boot derivation math to an
        # older epoch. It must not: only current_boot_utc's own epoch
        # may ever be derived.
        sid = "99999999-bbbb-cccc-dddd-000000000009"
        lr.live_dir(env.claude_dir).mkdir(parents=True, exist_ok=True)
        lr.entry_path(env.claude_dir, sid).write_text(json.dumps({
            "session_id": sid, "started_at": "2026-07-10T00:00:00Z",
            "source": "startup", "cwd": "C:/code/older",
        }), encoding="utf-8")
        # No boundary snapshot exists for the last~1 epoch (boot=Jul 15
        # 08:18:17Z) -- this is the "csb hasn't proven it" case.

        rc = _run(env, "set", "show", "last~1", "--json")
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["snapshot_available"] is False, (
            "an older epoch with no matching snapshot must read as "
            "UNKNOWN, never derived from today's registry")
        # No member may carry a true open_at_shutdown badge -- there is
        # no snapshot to have proven it from.
        assert not any(m.get("open_at_shutdown") for m in payload["members"])

    def test_older_epoch_prints_unknown_note_not_a_derived_badge(
            self, env, capsys, monkeypatch):
        _patch_two_epoch_history(monkeypatch)
        rc = _run(env, "set", "show", "last~1")
        assert rc == 0
        err = capsys.readouterr().err
        assert "UNKNOWN, not none" in err

    def test_matching_boundary_record_for_the_older_epoch_still_works(
            self, env, capsys, monkeypatch):
        """A RETAINED boundary record for last~1 (not derived, not the
        current registry) is the one legitimate way an older epoch gets
        badges."""
        _patch_two_epoch_history(monkeypatch)
        sid = "88888888-bbbb-cccc-dddd-000000000008"
        older_boot = "2026-07-15T08:18:17Z"
        lr.live_dir(env.claude_dir).mkdir(parents=True, exist_ok=True)
        bdir = lr.boundary_dir(env.claude_dir)
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "boundary-20260715T081817Z.json").write_text(json.dumps({
            "version": lr.SNAPSHOT_VERSION, "boot_at": older_boot,
            "captured_at": older_boot,
            "open_at_shutdown": [{"session_id": sid,
                                  "started_at": "2026-07-20T00:00:00Z",
                                  "source": "startup", "cwd": "C:/x"}],
        }), encoding="utf-8")

        rc = _run(env, "set", "show", "last~1", "--json")
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["snapshot_available"] is True
        assert any(m["session_id"] == sid and m.get("open_at_shutdown")
                  for m in payload["members"])


class TestSection2_2_ReadingNeverWrites:
    """2.2 -- a display command leaves no trace: no new files, no
    entries removed, across repeated invocations."""

    def test_repeated_set_last_writes_nothing(self, env, capsys,
                                              monkeypatch):
        _patch_two_epoch_history(monkeypatch)
        sid = "77777777-bbbb-cccc-dddd-000000000007"
        lr.live_dir(env.claude_dir).mkdir(parents=True, exist_ok=True)
        lr.entry_path(env.claude_dir, sid).write_text(json.dumps({
            "session_id": sid, "started_at": "2026-07-30T10:00:00Z",
            "source": "startup", "cwd": "C:/w",
        }), encoding="utf-8")

        def _snapshot_state():
            live = lr.live_dir(env.claude_dir)
            return sorted(p.name for p in live.rglob("*")) if live.is_dir() \
                else []

        before = _snapshot_state()
        for _ in range(3):
            rc = _run(env, "set", "show", "last")
            assert rc == 0
            capsys.readouterr()
        after = _snapshot_state()
        assert before == after, (
            "a read-only display command changed the registry on disk: "
            f"before={before!r} after={after!r}")
        # The entry itself must survive untouched.
        assert lr.entry_path(env.claude_dir, sid).exists()

    def test_repeated_set_current_writes_nothing(self, env, capsys):
        sid = "66666666-bbbb-cccc-dddd-000000000006"
        lr.live_dir(env.claude_dir).mkdir(parents=True, exist_ok=True)
        lr.entry_path(env.claude_dir, sid).write_text(json.dumps({
            "session_id": sid, "started_at": "2026-08-01T12:10:00Z",
            "source": "startup", "cwd": "C:/w",
        }), encoding="utf-8")
        before = sorted(p.name for p in lr.live_dir(env.claude_dir).glob("*"))
        for _ in range(3):
            assert _run(env, "set", "current") == 0
            capsys.readouterr()
        after = sorted(p.name for p in lr.live_dir(env.claude_dir).glob("*"))
        assert before == after


class TestHV3_IgnoranceIsAnnounced:
    """HV.3 -- on a machine that never ran the csb hooks (csb-live/ never
    created at all), `csb set last` for the CURRENT boot's own epoch
    must say UNKNOWN, not silently show a badge-less roster."""

    def test_never_ran_hooks_notes_unknown_for_the_current_boot_epoch(
            self, env, capsys, monkeypatch):
        import claude_session_backup.epochs as epochs
        monkeypatch.setattr(epochs.sys, "platform", "win32")
        # A single-fence history where the epoch's boot EXACTLY matches
        # current_boot_utc (BOOT) -- so this is genuinely "the epoch
        # this machine just booted into", not an older, unrelated one.
        monkeypatch.setattr(epochs, "_run_powershell", lambda *a, **k: (
            "2026-08-01T12:00:00.0000000Z|6005\n"
            "2026-07-25T16:16:32.0000000Z|6006\n"
            "2026-07-15T08:18:17.0000000Z|6005\n"
        ))
        # Deliberately do NOT create claude_dir/csb-live at all -- the
        # literal HV.3 scenario, distinct from "csb-live/ exists but has
        # no snapshot yet".
        assert not lr.live_dir(env.claude_dir).exists()

        rc = _run(env, "set", "show", "last")
        assert rc == 0
        err = capsys.readouterr().err
        assert "UNKNOWN, not none" in err
        assert "csb backup" in err


class TestHV6_RowCountsAreInternallyConsistent:
    """HV.6 -- the header's `N sessions: X running, Y no exit observed`
    must match the tags actually rendered on the rows beneath it, and
    X + Y must equal N, across several liveness mixes (not just the
    trivially-true all-same-tier case)."""

    def _seed(self, env, sid, started, pid=None, verified_pid=None):
        lr.live_dir(env.claude_dir).mkdir(parents=True, exist_ok=True)
        payload = {"session_id": sid, "started_at": started,
                  "source": "startup", "cwd": "C:/w"}
        if pid is not None:
            payload["pid"] = pid
            payload["pid_at"] = started
        lr.entry_path(env.claude_dir, sid).write_text(
            json.dumps(payload), encoding="utf-8")

    def test_mixed_running_and_unverified_header_matches_rows(
            self, env, capsys, monkeypatch):
        import claude_session_backup.liveness as lv
        running_sid = "aaaaaaaa-0000-0000-0000-000000000001"
        unverified_sid = "bbbbbbbb-0000-0000-0000-000000000002"
        another_unverified = "cccccccc-0000-0000-0000-000000000003"
        self._seed(env, running_sid, "2026-08-01T12:05:00Z", pid=1234)
        self._seed(env, unverified_sid, "2026-08-01T12:06:00Z")
        self._seed(env, another_unverified, "2026-08-01T12:07:00Z")
        monkeypatch.setattr(
            lv, "scan",
            lambda: lv.LiveScan(
                by_pid={1234: lv.ProcInfo(cmdline="claude", created=None)},
                ok=True))

        rc = _run(env, "set", "current", "--json")
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        members = payload["members"]
        running_rows = [m for m in members if m["live_status"] == "running"]
        unverified_rows = [m for m in members
                          if m["live_status"] == "unverified"]
        assert len(running_rows) == 1
        assert len(unverified_rows) == 2
        assert len(running_rows) + len(unverified_rows) == len(members)
        assert running_rows[0]["session_id"] == running_sid

        # Cross-check against the human-rendered header text too.
        rc2 = _run(env, "set", "current")
        assert rc2 == 0
        out = capsys.readouterr().out
        assert "3 sessions: 1 running" in out
        assert "2 no exit observed" in out

    def test_all_unverified_header_matches_rows(self, env, capsys,
                                                monkeypatch):
        import claude_session_backup.liveness as lv
        monkeypatch.setattr(lv, "scan", lambda: lv.LiveScan(ok=True))
        for i in range(1, 4):
            self._seed(env, f"dddddddd-0000-0000-0000-00000000000{i}",
                      f"2026-08-01T12:0{i}:00Z")
        rc = _run(env, "set", "current")
        assert rc == 0
        out = capsys.readouterr().out
        assert "3 sessions: 0 running" in out
        assert "3 no exit observed" in out

"""Epoch history, addressing, retention, promotion (#68 P6c / R3).

The load-bearing red-greens:
  * the July-15 double-cycle must enumerate as ONE epoch (naive
    per-shutdown enumeration mints two, 77 seconds apart);
  * `latest_epoch` is pinned equal to `enumerate_epochs()[0]`;
  * date ambiguity lists-and-asks (rc 2), never newest-wins-silently;
  * boundary snapshots live in a SUBDIRECTORY -- flat files would read
    back as ghost registry entries, including under older csb versions;
  * promotion stamps provenance that survives the epoch's address
    drifting.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import claude_session_backup.cli as cli
import claude_session_backup.commands as commands_module
import claude_session_backup.live_registry as lr
from claude_session_backup.epochs import (
    EVENT_BOOT,
    EVENT_SHUTDOWN_CLEAN,
    Fence,
    classify_epoch_token,
    enumerate_epochs,
    latest_epoch,
    resolve_epoch_token,
)
from claude_session_backup.index import init_schema, open_db
from claude_session_backup.session_sets import get_set

UUID_A = "eeee1111-bbbb-cccc-dddd-000000000001"
UUID_B = "eeee2222-bbbb-cccc-dddd-000000000002"


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _boot(dt):
    return Fence(at_utc=dt, event_id=EVENT_BOOT)


def _shut(dt):
    return Fence(at_utc=dt, event_id=EVENT_SHUTDOWN_CLEAN)


# Three epochs; the middle shutdown and the next boot BOTH sit on July
# 15 (UTC), so that date is genuinely ambiguous between last and last~1.
THREE_EPOCH_FENCES = [
    _boot(_utc(2026, 7, 1, 8, 0)),
    _shut(_utc(2026, 7, 10, 20, 0)),
    _boot(_utc(2026, 7, 10, 20, 5)),
    _shut(_utc(2026, 7, 15, 4, 16, 21)),
    _boot(_utc(2026, 7, 15, 5, 0)),
    _shut(_utc(2026, 7, 25, 16, 16)),
    _boot(_utc(2026, 7, 25, 16, 20)),
]

# The real 2026-07-15 update log from the enumerate docstring: ONE
# restart double-cycling within 116 seconds.
DOUBLE_CYCLE = [
    _boot(_utc(2026, 7, 10, 20, 5)),
    _shut(_utc(2026, 7, 15, 4, 16, 21)),
    _boot(_utc(2026, 7, 15, 4, 17, 9)),
    _shut(_utc(2026, 7, 15, 4, 17, 38)),
    _boot(_utc(2026, 7, 15, 4, 18, 17)),
]


class TestEnumerate:
    def test_double_cycle_is_one_epoch(self):
        """RED-GREEN anchor: naive per-shutdown enumeration mints two
        epochs 77s apart out of one Windows-Update restart."""
        epochs_list = enumerate_epochs(DOUBLE_CYCLE)
        assert len(epochs_list) == 1
        assert epochs_list[0].shutdown_utc == _utc(2026, 7, 15, 4, 17, 38)
        # prev fence skips the mid-update boot: the epoch started Jul 10.
        assert epochs_list[0].prev_fence_utc == _utc(2026, 7, 10, 20, 5)

    def test_three_epochs_newest_first(self):
        epochs_list = enumerate_epochs(THREE_EPOCH_FENCES)
        assert [e.shutdown_utc for e in epochs_list] == [
            _utc(2026, 7, 25, 16, 16),
            _utc(2026, 7, 15, 4, 16, 21),
            _utc(2026, 7, 10, 20, 0),
        ]

    @pytest.mark.parametrize("fences", [
        [], DOUBLE_CYCLE, THREE_EPOCH_FENCES,
        [_shut(_utc(2026, 7, 1, 0, 0))],
    ])
    def test_latest_is_pinned_to_first(self, fences):
        """The wrapper contract: `last` and `last~0` can never drift."""
        epochs_list = enumerate_epochs(fences)
        expected = epochs_list[0] if epochs_list else None
        assert latest_epoch(fences) == expected


class TestTokenResolution:
    HISTORY = enumerate_epochs(THREE_EPOCH_FENCES)

    def test_classify(self):
        assert classify_epoch_token("last") == "last"
        assert classify_epoch_token("LAST~2") == "tilde"
        assert classify_epoch_token("2026-7-15") == "date"
        assert classify_epoch_token("CSB-STACK") is None
        assert classify_epoch_token("last-1") is None

    def test_last_equals_tilde_zero(self):
        assert resolve_epoch_token("last", self.HISTORY) == \
            resolve_epoch_token("last~0", self.HISTORY)

    def test_tilde_indexes_history(self):
        [(idx, ep)] = resolve_epoch_token("last~2", self.HISTORY)
        assert idx == 2
        assert ep.shutdown_utc == _utc(2026, 7, 10, 20, 0)

    def test_tilde_beyond_history_is_empty(self):
        assert resolve_epoch_token("last~9", self.HISTORY) == []

    def test_date_matches_span_in_local_zone(self):
        tz = timezone(timedelta(hours=-4))
        matches = resolve_epoch_token("2026-7-12", self.HISTORY, tzinfo=tz)
        assert [i for i, _ in matches] == [1]

    def test_invalid_date_is_empty(self):
        assert resolve_epoch_token("2026-13-40", self.HISTORY) == []


# ── CLI: addressing, ambiguity, implicit show ─────────────────────────


def _insert(conn, sid, name, last_active):
    conn.execute(
        "INSERT INTO sessions (session_id, session_name, project,"
        " start_folder, started_at, last_active_at, is_fork) VALUES"
        " (?, ?, 'C--code-test', 'C:\\code\\test',"
        " '2026-06-01T00:00:00Z', ?, 0)",
        (sid, name, last_active),
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    db = tmp_path / "hist.db"
    conn = open_db(db)
    init_schema(conn, quiet=True)
    # A lives in epoch last~1 (active Jul 14); B in last (active Jul 22).
    _insert(conn, UUID_A, "MIDDLE__session", "2026-07-14T12:00:00Z")
    _insert(conn, UUID_B, "NEWEST__session", "2026-07-22T12:00:00Z")
    conn.commit()
    conn.close()
    monkeypatch.setattr(commands_module, "read_fences",
                        lambda *a, **k: list(THREE_EPOCH_FENCES))
    import types

    return types.SimpleNamespace(claude_dir=claude_dir, db=db)


def _run(env, *argv):
    return cli.main(list(argv) + ["--claude-dir", str(env.claude_dir),
                                  "--db", str(env.db)])


class TestHistoryThroughCli:
    def test_show_last_tilde_one(self, env, capsys):
        assert _run(env, "set", "show", "last~1") == 0
        out = capsys.readouterr().out
        assert "Epoch 'last~1'" in out
        assert "MIDDLE__session" in out
        assert "NEWEST__session" not in out

    def test_show_json_names_the_token(self, env, capsys):
        assert _run(env, "set", "show", "last~1", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["name"] == "last~1"
        assert payload["history_index"] == 1

    def test_beyond_history_errors_with_depth(self, env, capsys):
        assert _run(env, "set", "show", "last~7") == 1
        err = capsys.readouterr().err
        assert "beyond recorded history" in err
        assert "3 completed epochs" in err

    def test_date_resolves_unambiguously(self, env, capsys):
        # Jul 22 falls only in the newest epoch's span.
        assert _run(env, "set", "show", "2026-7-22") == 0
        assert "NEWEST__session" in capsys.readouterr().out

    def test_date_ambiguity_lists_and_asks(self, env, capsys, monkeypatch):
        """RED-GREEN anchor: two epochs share Jul 15 (one ends 4:16 that
        day, the next begins 5:00). Silent newest-wins is forbidden.
        Resolution is pinned to UTC so the test is machine-tz-proof."""
        import claude_session_backup.epochs as ep
        monkeypatch.setattr(
            commands_module, "resolve_epoch_token",
            lambda token, hist, tzinfo=None: ep.resolve_epoch_token(
                token, hist, tzinfo=timezone.utc))
        assert _run(env, "set", "show", "2026-7-15") == 2
        err = capsys.readouterr().err
        assert "matches 2 epochs" in err
        assert "last~1" in err and "csb set show last" in err

    def test_resume_set_addresses_history(self, env, capsys, monkeypatch):
        """`csb resume --set last~1 1` resolves the SAME roster -- one
        materializer, both verbs."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import subprocess as subprocess_module
        run_mock = MagicMock(return_value=SimpleNamespace(returncode=0))
        monkeypatch.setattr(subprocess_module, "run", run_mock)
        assert _run(env, "resume", "--set", "last~1", "1") == 0
        launched = " ".join(str(a) for a in run_mock.call_args[0][0])
        assert UUID_A in launched

    def test_implicit_show_boot_token(self, env, capsys, monkeypatch):
        """`csb set last~1` == `csb set show last~1` (implicit show)."""
        assert _run(env, "set", "last~1") == 0
        assert "Epoch 'last~1'" in capsys.readouterr().out

    def test_implicit_show_unknown_name_errors_clearly(self, env, capsys):
        assert _run(env, "set", "NO-SUCH-SET") == 1
        assert "No set named 'NO-SUCH-SET'" in capsys.readouterr().err

    def test_set_list_epoch_table(self, env, capsys):
        assert _run(env, "set", "list") == 0
        out = capsys.readouterr().out
        assert "last~1" in out and "last~2" in out
        assert "addressable by date" in out

    def test_message_counts_render_on_rosters(self, env, capsys):
        """Substance at a glance: counts come from the index, so they
        survive even after the JSONL is purged (the triage case)."""
        conn = sqlite3.connect(env.db)
        conn.execute("UPDATE sessions SET message_count = 331 "
                     "WHERE session_id = ?", (UUID_A,))
        conn.commit()
        conn.close()
        assert _run(env, "set", "show", "last~1") == 0
        assert "331 msgs" in capsys.readouterr().out

    def test_set_list_command_column_aligns(self, env, capsys,
                                            monkeypatch):
        """boot's and the epochs' trailing commands share one column."""
        import claude_session_backup.live_registry as lr2
        monkeypatch.setattr(lr2, "current_boot_utc",
                            lambda: _utc(2026, 7, 25, 16, 20))
        assert _run(env, "set", "list") == 0
        out = capsys.readouterr().out
        cols = [line.index("csb set show") for line in out.splitlines()
                if "csb set show" in line
                and ("shutdown" in line or "in progress" in line)]
        assert len(cols) >= 2
        assert len(set(cols)) == 1

    def test_set_list_discloses_truncated_depth(self, env, capsys,
                                                monkeypatch):
        """No silent caps: 7 epochs -> 5 rows + an addressable-through
        line naming the true depth."""
        fences = []
        for day in range(1, 8):
            fences.append(_shut(_utc(2026, 7, day, 12, 0)))
            fences.append(_boot(_utc(2026, 7, day, 12, 5)))
        monkeypatch.setattr(commands_module, "read_fences",
                            lambda *a, **k: list(fences))
        assert _run(env, "set", "list") == 0
        out = capsys.readouterr().out
        assert "last~4" in out and "last~5" not in out.split("through")[0]
        assert "2 more addressable, through last~6" in out

    def test_set_list_json_epochs_array_and_legacy_key(self, env, capsys):
        assert _run(env, "set", "list", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["epoch"]["name"] == "last"  # legacy, byte-stable
        tokens = [e["token"] for e in payload["epochs"]]
        assert tokens == ["last", "last~1", "last~2"]


# ── disclosed empty-epoch fallthrough (H8, user review) ───────────────


# Four epochs; only e0 (NEW, Jul 10) and e2 (DEEP, Jun 15) have members.
FOUR_EPOCH_FENCES = [
    _boot(_utc(2026, 6, 1, 8, 0)),
    _shut(_utc(2026, 6, 10, 12, 0)), _boot(_utc(2026, 6, 10, 12, 5)),
    _shut(_utc(2026, 6, 20, 12, 0)), _boot(_utc(2026, 6, 20, 12, 5)),
    _shut(_utc(2026, 7, 1, 12, 0)), _boot(_utc(2026, 7, 1, 12, 5)),
    _shut(_utc(2026, 7, 20, 12, 0)), _boot(_utc(2026, 7, 20, 12, 5)),
]

UUID_DEEP = "eeee3333-bbbb-cccc-dddd-000000000003"
UUID_NEW = "eeee4444-bbbb-cccc-dddd-000000000004"


@pytest.fixture
def gap_env(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    db = tmp_path / "gap.db"
    conn = open_db(db)
    init_schema(conn, quiet=True)
    _insert(conn, UUID_DEEP, "DEEP__session", "2026-06-15T12:00:00Z")
    _insert(conn, UUID_NEW, "NEW__session", "2026-07-10T12:00:00Z")
    conn.commit()
    conn.close()
    monkeypatch.setattr(commands_module, "read_fences",
                        lambda *a, **k: list(FOUR_EPOCH_FENCES))
    import types

    return types.SimpleNamespace(claude_dir=claude_dir, db=db)


class TestEmptyEpochFallthrough:
    """A relative address means 'the last WORKING set' -- empty epochs
    are walked past, DISCLOSED, never silently redirected."""

    def test_relative_address_walks_to_working_set(self, gap_env, capsys):
        """H8 red-green anchor: last~1 is empty; the view shows its
        empty header AND settles on last~2's roster."""
        assert _run(gap_env, "set", "show", "last~1") == 0
        out = capsys.readouterr().out
        assert "Epoch 'last~1'" in out          # the empty period, shown
        assert "0 sessions" in out
        assert "nearest epoch with members" in out
        assert "Epoch 'last~2'" in out          # where it settled
        assert "DEEP__session" in out

    def test_json_discloses_requested_and_skipped(self, gap_env, capsys):
        assert _run(gap_env, "set", "show", "last~1", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["name"] == "last~2"
        assert payload["requested_token"] == "last~1"
        assert [s["token"] for s in payload["skipped_empty"]] == ["last~1"]

    def test_resume_number_matches_the_displayed_roster(self, gap_env,
                                                        capsys,
                                                        monkeypatch):
        """The read-a-number-get-that-number contract survives the
        fallthrough: resume --set last~1 1 opens what the show showed."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import subprocess as subprocess_module
        run_mock = MagicMock(return_value=SimpleNamespace(returncode=0))
        monkeypatch.setattr(subprocess_module, "run", run_mock)
        assert _run(gap_env, "resume", "--set", "last~1", "1") == 0
        launched = " ".join(str(a) for a in run_mock.call_args[0][0])
        assert UUID_DEEP in launched

    def test_date_addresses_stay_exact(self, gap_env, capsys):
        """A date is an absolute question; an empty answer IS the
        answer -- no fallthrough."""
        import claude_session_backup.epochs as ep
        assert _run(gap_env, "set", "show", "2026-6-25") == 0
        out = capsys.readouterr().out
        assert "nearest epoch with members" not in out
        assert "No sessions with activity" in out

    def test_deepest_empty_epoch_settles_back_with_pointer(self, gap_env,
                                                           capsys):
        """Nothing deeper to walk to: the requested epoch renders its
        own honest empty roster -- and POINTS at the nearest epoch that
        has members, so nobody binary-searches last~N by hand."""
        assert _run(gap_env, "set", "show", "last~3") == 0
        out = capsys.readouterr().out
        assert "Epoch 'last~3'" in out
        assert "Epoch 'last~2'" not in out       # hint, never a jump
        assert "nothing deeper exists" in out
        assert "nearest epoch with members: last~2" in out
        assert "csb set show last~2" in out
        assert "No sessions with activity" in out

    def test_nearest_working_in_json(self, gap_env, capsys):
        assert _run(gap_env, "set", "show", "last~3", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["fallthrough_failed"] is True
        assert payload["nearest_working"]["token"] == "last~2"
        assert payload["nearest_working"]["count"] == 1

    def test_promotion_stamps_the_settled_token(self, gap_env, capsys):
        """--from records what actually froze."""
        assert _run(gap_env, "set", "new", "FROZEN", "--from",
                    "last~1") == 0
        entry = get_set(gap_env.claude_dir, "FROZEN")
        assert entry["promoted_from"]["token"] == "last~2"

    def test_exhausted_walk_names_the_activity_floor(self, tmp_path,
                                                     capsys, monkeypatch):
        """All deeper epochs empty: the note condenses the whole tail
        and says WHY it can never succeed -- the index's activity
        reaches back only so far."""
        claude_dir = tmp_path / "claude"
        claude_dir.mkdir()
        db = tmp_path / "floor.db"
        conn = open_db(db)
        init_schema(conn, quiet=True)
        _insert(conn, UUID_NEW, "NEW__session", "2026-07-10T12:00:00Z")
        conn.commit()
        conn.close()
        monkeypatch.setattr(commands_module, "read_fences",
                            lambda *a, **k: list(FOUR_EPOCH_FENCES))
        rc = cli.main(["set", "show", "last~1", "--claude-dir",
                       str(claude_dir), "--db", str(db)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Epoch 'last~1'" in out          # settled BACK, not deep
        assert "Epoch 'last~3'" not in out
        assert "2 deeper epochs" in out
        assert "activity reaches back to 2026-07-10" in out
        # The pointer at the honest-to-god last working epoch.
        assert "nearest epoch with members: last" in out
        assert "csb set show last" in out


# ── promotion provenance (H5) ─────────────────────────────────────────


class TestPromotion:
    def test_from_epoch_token_stamps_provenance(self, env, capsys):
        assert _run(env, "set", "new", "JULY-MID", "--from", "last~1") == 0
        entry = get_set(env.claude_dir, "JULY-MID")
        assert [m["session_id"] for m in entry["members"]] == [UUID_A]
        assert entry["promoted_from"] == {
            "token": "last~1",
            "shutdown_at": "2026-07-15T04:16:21Z",
        }

    def test_provenance_renders_on_show(self, env, capsys):
        _run(env, "set", "new", "JULY-MID", "--from", "last~1")
        capsys.readouterr()
        assert _run(env, "set", "show", "JULY-MID") == 0
        out = capsys.readouterr().out
        assert "promoted from 'last~1'" in out

    def test_from_current_has_no_provenance(self, env, capsys, monkeypatch):
        import claude_session_backup.liveness as lv
        lr.live_dir(env.claude_dir).mkdir(parents=True, exist_ok=True)
        lr.entry_path(env.claude_dir, UUID_B).write_text(json.dumps(
            {"session_id": UUID_B, "started_at": "2026-07-26T00:00:00Z"}),
            encoding="utf-8")
        monkeypatch.setattr(lr, "current_boot_utc",
                            lambda: _utc(2026, 7, 25, 16, 20))
        monkeypatch.setattr(lv, "scan", lambda: lv.LiveScan())
        assert _run(env, "set", "new", "NOW-GROUP", "--from",
                    "current") == 0
        entry = get_set(env.claude_dir, "NOW-GROUP")
        assert "promoted_from" not in entry

    def test_from_garbage_view_rejected(self, env, capsys):
        assert _run(env, "set", "new", "X-GROUP", "--from",
                    "not-a-view") == 2
        assert "takes a view" in capsys.readouterr().err


# ── boundary retention (H3) ───────────────────────────────────────────


class TestBoundaryRetention:
    def _sweep(self, claude_dir, boot, sid):
        lr.live_dir(claude_dir).mkdir(parents=True, exist_ok=True)
        lr.entry_path(claude_dir, sid).write_text(json.dumps(
            {"session_id": sid,
             "started_at": lr._iso(boot - timedelta(hours=2))}),
            encoding="utf-8")
        assert lr.sweep_boundary(claude_dir, boot) == 1

    def test_boundaries_accumulate_and_prune_to_k(self, tmp_path):
        claude_dir = tmp_path / "claude"
        for i in range(7):
            boot = _utc(2026, 7, 1 + i, 8, 0)
            self._sweep(claude_dir, boot, f"{i}{UUID_A[1:]}")
        files = sorted(lr.boundary_dir(claude_dir).glob("boundary-*.json"))
        assert len(files) == lr.BOUNDARY_RETENTION
        # The newest K survive; the two oldest were pruned.
        assert files[0].name == "boundary-20260703T080000Z.json"
        # Legacy alias tracks the newest sweep.
        alias = lr.read_snapshot(claude_dir)
        assert alias["boot_at"] == "2026-07-07T08:00:00Z"

    def test_read_snapshot_finds_historical_boot(self, tmp_path):
        claude_dir = tmp_path / "claude"
        for i in range(3):
            self._sweep(claude_dir, _utc(2026, 7, 1 + i, 8, 0),
                        f"{i}{UUID_A[1:]}")
        snap = lr.read_snapshot(claude_dir,
                                boot_utc=_utc(2026, 7, 2, 8, 0, 30))
        assert snap is not None
        assert snap["boot_at"] == "2026-07-02T08:00:00Z"
        assert lr.read_snapshot(
            claude_dir, boot_utc=_utc(2026, 6, 1, 0, 0)) is None

    def test_boundary_files_never_read_as_registry_entries(self, tmp_path):
        """RED-GREEN contract: the subdirectory is load-bearing. A flat
        layout would return boundary snapshots as ghost sessions here --
        including under OLDER csb versions reading a synced store."""
        claude_dir = tmp_path / "claude"
        self._sweep(claude_dir, _utc(2026, 7, 1, 8, 0), UUID_A)
        assert lr.read_entries(claude_dir) == []


class TestSnapshotEpochReKeying:
    """H9: N Claude-less reboots leave the sweep's testimony keyed to
    boot N+1; read-time re-keying reattaches it to the epoch that
    earned it -- the shutdown its entries were actually open at."""

    SHUTDOWN = _utc(2026, 7, 15, 8, 0)   # the working epoch's end
    LATE_BOOT = _utc(2026, 7, 20, 9, 0)  # boot 6: Claude finally runs

    def _sweep_late(self, claude_dir, started_at):
        lr.live_dir(claude_dir).mkdir(parents=True, exist_ok=True)
        lr.entry_path(claude_dir, UUID_A).write_text(json.dumps(
            {"session_id": UUID_A, "started_at": lr._iso(started_at)}),
            encoding="utf-8")
        assert lr.sweep_boundary(claude_dir, self.LATE_BOOT) == 1

    def test_claude_less_reboot_run_reattaches(self, tmp_path):
        """RED-GREEN anchor: the snapshot is keyed to boot 6, but its
        entry started before the old shutdown -- the old epoch gets it."""
        claude_dir = tmp_path / "claude"
        self._sweep_late(claude_dir, self.SHUTDOWN - timedelta(hours=5))
        # Old epoch's own boot instant finds nothing by key...
        old_boot = self.SHUTDOWN + timedelta(seconds=40)
        assert lr.read_snapshot(claude_dir, boot_utc=old_boot) is None
        # ...but the shutdown-anchored second pass reattaches it.
        snap = lr.read_snapshot(claude_dir, boot_utc=old_boot,
                                shutdown_utc=self.SHUTDOWN)
        assert snap is not None
        assert snap["open_at_shutdown"][0]["session_id"] == UUID_A

    def test_entry_younger_than_shutdown_blocks_reattachment(self,
                                                             tmp_path):
        """An entry that STARTED after the epoch's shutdown proves the
        snapshot belongs to a later era -- honest absence."""
        claude_dir = tmp_path / "claude"
        self._sweep_late(claude_dir, self.SHUTDOWN + timedelta(days=1))
        assert lr.read_snapshot(
            claude_dir, boot_utc=self.SHUTDOWN + timedelta(seconds=40),
            shutdown_utc=self.SHUTDOWN) is None

    def test_boot_keyed_match_still_wins(self, tmp_path):
        """The normal case (sweep at the very next boot) is untouched."""
        claude_dir = tmp_path / "claude"
        lr.live_dir(claude_dir).mkdir(parents=True, exist_ok=True)
        lr.entry_path(claude_dir, UUID_B).write_text(json.dumps(
            {"session_id": UUID_B,
             "started_at": lr._iso(self.SHUTDOWN - timedelta(hours=2))}),
            encoding="utf-8")
        next_boot = self.SHUTDOWN + timedelta(seconds=45)
        assert lr.sweep_boundary(claude_dir, next_boot) == 1
        snap = lr.read_snapshot(claude_dir, boot_utc=next_boot,
                                shutdown_utc=self.SHUTDOWN)
        assert snap is not None
        assert snap["boot_at"] == lr._iso(next_boot)

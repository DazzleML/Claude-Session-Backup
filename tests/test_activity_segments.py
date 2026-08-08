"""Activity segments (#80, S1-S9): epoch membership from real timelines.

The index used to remember one timestamp per session (last_active_at),
so a session testified only for the epoch holding its FINAL activity --
reactivating MYProject in last~2 silently erased it from last~4's
roster. Segments record every sitting (contiguous activity split at
>ACTIVITY_GAP_MINUTES gaps), making membership evidence-complete:
a session belongs to every epoch a segment overlaps, and to nothing
its timeline provably skipped.

Design doc: s-epoch-membership-from-activity-segments DWP (S1-S9 +
addendum S2a-S2c/S3a).
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

import claude_session_backup.cli as cli
import claude_session_backup.commands as commands_module
from claude_session_backup.epochs import (
    EVENT_BOOT,
    EVENT_SHUTDOWN_CLEAN,
    Fence,
    build_roster,
    parse_index_ts,
)
from claude_session_backup.index import (
    fetch_activity_segments,
    init_schema,
    open_db,
    upsert_session,
)
from claude_session_backup.metadata import (
    ACTIVITY_GAP_MINUTES,
    SessionMetadata,
    _parse_jsonl_lines,
)


def _utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _line(ts, typ="user"):
    return json.dumps({"type": typ, "timestamp": ts, "cwd": "C:\\w"})


def _parse(lines):
    meta = SessionMetadata(session_id="seg-test")
    return _parse_jsonl_lines(lines, meta)


class TestFold:
    """The scan-time fold: sittings from event timestamps (S1, S2a)."""

    def test_close_events_form_one_segment(self):
        meta = _parse([
            _line("2026-08-01T10:00:00Z"),
            _line("2026-08-01T10:04:00Z", "assistant"),
            _line("2026-08-01T10:08:00Z"),
        ])
        assert meta.activity_extracted
        assert len(meta.activity_segments) == 1
        start, end, msgs = meta.activity_segments[0]
        assert parse_index_ts(start) == _utc(2026, 8, 1, 10, 0, 0)
        assert parse_index_ts(end) == _utc(2026, 8, 1, 10, 8, 0)
        assert msgs == 3

    def test_gap_over_threshold_splits(self):
        meta = _parse([
            _line("2026-08-01T10:00:00Z"),
            # 30 min > ACTIVITY_GAP_MINUTES -> new sitting
            _line("2026-08-01T10:30:01Z"),
        ])
        assert len(meta.activity_segments) == 2

    def test_gap_under_threshold_bridges(self):
        gap_min = ACTIVITY_GAP_MINUTES - 1
        meta = _parse([
            _line("2026-08-01T10:00:00Z"),
            _line(f"2026-08-01T10:{gap_min:02d}:00Z"),
        ])
        assert len(meta.activity_segments) == 1

    def test_out_of_order_timestamps_sorted_before_folding(self):
        """S2a: forked transcripts lead with inherited pre-fork events --
        the fold must sort, not assume stream order."""
        ordered = _parse([
            _line("2026-08-01T10:00:00Z"),
            _line("2026-08-01T10:05:00Z"),
            _line("2026-08-01T12:00:00Z"),
        ])
        shuffled = _parse([
            _line("2026-08-01T12:00:00Z"),
            _line("2026-08-01T10:00:00Z"),
            _line("2026-08-01T10:05:00Z"),
        ])
        assert shuffled.activity_segments == ordered.activity_segments
        assert len(shuffled.activity_segments) == 2

    def test_only_user_and_assistant_count_as_messages(self):
        meta = _parse([
            _line("2026-08-01T10:00:00Z", "user"),
            _line("2026-08-01T10:01:00Z", "progress"),
            _line("2026-08-01T10:02:00Z", "assistant"),
        ])
        assert len(meta.activity_segments) == 1
        assert meta.activity_segments[0][2] == 2  # progress spans, not counts

    def test_no_timestamps_no_segments(self):
        meta = _parse([json.dumps({"type": "user"})])
        assert meta.activity_extracted
        assert meta.activity_segments == []


class TestMigration:
    """House rule (the v0.7.0 lesson): a previous-version DB must open."""

    def test_opening_a_v8_database_upgrades_without_error(self, tmp_path):
        db = tmp_path / "v8.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        # A v8-era schema: everything current EXCEPT session_activity.
        conn.executescript(
            """
            CREATE TABLE schema_info (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, project TEXT NOT NULL,
                session_name TEXT, start_folder TEXT, started_at TEXT,
                last_active_at TEXT, last_user_at TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                claude_version TEXT, jsonl_path TEXT,
                jsonl_size INTEGER DEFAULT 0, jsonl_mtime REAL DEFAULT 0,
                last_scanned_at TEXT, deleted_at TEXT, last_git_commit TEXT,
                metadata_validated_at TEXT, parent_session_id TEXT,
                parent_message_uuid TEXT, is_fork INTEGER NOT NULL DEFAULT 0,
                forked_at TEXT, tool_paths_extracted_at TEXT
            );
            CREATE TABLE folder_usage (
                session_id TEXT NOT NULL, folder_path TEXT NOT NULL,
                usage_count INTEGER DEFAULT 1, is_start_folder INTEGER DEFAULT 0,
                path_exists INTEGER, path_verified_at TEXT, provenance TEXT,
                PRIMARY KEY (session_id, folder_path)
            );
            INSERT INTO schema_info VALUES ('schema_version', '8');
            INSERT INTO sessions (session_id, project)
                VALUES ('old-sess', 'proj');
            """
        )
        conn.commit()

        init_schema(conn, quiet=True)   # must not raise

        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "session_activity" in tables
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(session_activity)")}
        assert {"session_id", "seg_start", "seg_end", "messages"} <= cols
        # The pre-existing row survives, segment-less (S4 territory).
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        conn.close()


def _meta_with_segments(sid, segments, extracted=True):
    meta = SessionMetadata(session_id=sid, project="proj")
    meta.started_at = "2026-08-01T10:00:00Z"
    meta.last_active_at = "2026-08-01T12:00:00Z"
    meta.activity_segments = segments
    meta.activity_extracted = extracted
    return meta


class TestUpsert:
    """Segments ride upsert_session like folder_usage -- with the S2b
    guard: only a meta whose parse actually ran may replace them."""

    @pytest.fixture
    def conn(self, tmp_path):
        conn = open_db(str(tmp_path / "s.db"))
        init_schema(conn, quiet=True)
        yield conn
        conn.close()

    def test_segments_written_then_replaced_on_rescan(self, conn):
        upsert_session(conn, _meta_with_segments("s1", [
            ("2026-08-01T10:00:00Z", "2026-08-01T10:30:00Z", 5),
            ("2026-08-01T11:30:00Z", "2026-08-01T12:00:00Z", 7),
        ]))
        segs = fetch_activity_segments(conn)
        assert len(segs["s1"]) == 2

        upsert_session(conn, _meta_with_segments("s1", [
            ("2026-08-01T10:00:00Z", "2026-08-01T12:00:00Z", 12),
        ]))
        segs = fetch_activity_segments(conn)
        assert len(segs["s1"]) == 1
        assert segs["s1"][0][2] == 12

    def test_narrow_meta_never_wipes_indexed_segments(self, conn):
        """S2b red-green anchor: a meta built WITHOUT a real parse
        (activity_extracted False) must preserve existing segments."""
        upsert_session(conn, _meta_with_segments("s1", [
            ("2026-08-01T10:00:00Z", "2026-08-01T10:30:00Z", 5),
        ]))
        upsert_session(conn, _meta_with_segments("s1", [], extracted=False))
        segs = fetch_activity_segments(conn)
        assert len(segs.get("s1", [])) == 1

    def test_purged_session_keeps_segments(self, conn):
        """Purge sets deleted_at; the sessions row stays, so segments
        stay -- purged sessions remain segment-backed in old epochs."""
        from claude_session_backup.index import mark_deleted
        upsert_session(conn, _meta_with_segments("s1", [
            ("2026-08-01T10:00:00Z", "2026-08-01T10:30:00Z", 5),
        ]))
        mark_deleted(conn, "s1", "2026-08-05T00:00:00Z")
        assert len(fetch_activity_segments(conn).get("s1", [])) == 1


def _row(sid, last_active, messages=10):
    return {
        "session_id": sid, "session_name": sid, "project": "proj",
        "start_folder": "C:\\w", "started_at": "2026-07-01T00:00:00Z",
        "last_active_at": last_active, "jsonl_path": "", "jsonl_mtime": 0,
        "deleted_at": None, "is_fork": 0, "message_count": messages,
    }


# Three epoch windows, oldest->newest, separated by reboots:
#   A: Aug 1 00:00 - Aug 1 23:00
#   B: Aug 2 00:00 - Aug 2 23:00
#   C: Aug 3 00:00 - Aug 3 23:00
WIN_A = (_utc(2026, 8, 1), _utc(2026, 8, 1, 23))
WIN_B = (_utc(2026, 8, 2), _utc(2026, 8, 2, 23))
WIN_C = (_utc(2026, 8, 3), _utc(2026, 8, 3, 23))


class TestSegmentMembership:
    """build_roster with segments: the MYProject scenario and the
    honesty ladder (S2, S4)."""

    def test_myproject_appears_in_both_epochs(self):
        """THE founding scenario: created in A, reactivated in C --
        point membership loses A; segments keep both."""
        rows = [_row("myproject", "2026-08-03T12:00:00Z")]
        segments = {"myproject": [
            (_utc(2026, 8, 1, 10), _utc(2026, 8, 1, 11), 20),
            (_utc(2026, 8, 3, 11), _utc(2026, 8, 3, 12), 30),
        ]}
        members_a, _ = build_roster(rows, *WIN_A, segments=segments)
        members_c, _ = build_roster(rows, *WIN_C, segments=segments)
        assert [m["session_id"] for m in members_a] == ["myproject"]
        assert [m["session_id"] for m in members_c] == ["myproject"]

    def test_absent_from_epochs_the_timeline_skipped(self):
        """Anti-span honesty: activity in A and C, closed through B.
        Whole-span overlap would claim B; segments must not."""
        rows = [_row("myproject", "2026-08-03T12:00:00Z")]
        segments = {"myproject": [
            (_utc(2026, 8, 1, 10), _utc(2026, 8, 1, 11), 20),
            (_utc(2026, 8, 3, 11), _utc(2026, 8, 3, 12), 30),
        ]}
        members_b, _ = build_roster(rows, *WIN_B, segments=segments)
        assert members_b == []

    def test_segmentless_row_keeps_point_membership(self):
        """S4: no segments indexed -> exactly today's behavior."""
        rows = [_row("legacy", "2026-08-02T12:00:00Z")]
        with_segs = build_roster(rows, *WIN_B, segments={})
        without = build_roster(rows, *WIN_B)
        assert [m["session_id"] for m in with_segs[0]] == ["legacy"]
        assert with_segs[0][0]["last_active_at"] == \
            without[0][0]["last_active_at"]

    def test_per_epoch_last_active_and_messages(self):
        """S2/S2c: the roster row reports IN-WINDOW last activity and
        IN-WINDOW message count, not globals."""
        rows = [_row("myproject", "2026-08-03T12:00:00Z", messages=50)]
        segments = {"myproject": [
            (_utc(2026, 8, 1, 10), _utc(2026, 8, 1, 11), 20),
            (_utc(2026, 8, 3, 11), _utc(2026, 8, 3, 12), 30),
        ]}
        members_a, _ = build_roster(rows, *WIN_A, segments=segments)
        assert parse_index_ts(members_a[0]["last_active_at"]) == \
            _utc(2026, 8, 1, 11)
        assert members_a[0]["messages"] == 20

    def test_segment_straddling_window_end_clips_last_active(self):
        """A sitting running INTO the shutdown reports the shutdown
        instant as its in-window last activity, not a post-window time."""
        rows = [_row("s", "2026-08-02T01:00:00Z")]
        segments = {"s": [
            (_utc(2026, 8, 1, 22), _utc(2026, 8, 2, 1), 9),
        ]}
        members_a, _ = build_roster(rows, *WIN_A, segments=segments)
        assert parse_index_ts(members_a[0]["last_active_at"]) == WIN_A[1]

    def test_ordering_uses_in_window_activity(self):
        rows = [
            _row("early", "2026-08-03T12:00:00Z"),
            _row("late", "2026-08-01T20:00:00Z"),
        ]
        segments = {
            # 'early' worked in the morning of A, 'late' in the evening
            "early": [(_utc(2026, 8, 1, 9), _utc(2026, 8, 1, 10), 5),
                      (_utc(2026, 8, 3, 9), _utc(2026, 8, 3, 10), 5)],
            "late": [(_utc(2026, 8, 1, 19), _utc(2026, 8, 1, 20), 5)],
        }
        members_a, _ = build_roster(rows, *WIN_A, segments=segments)
        assert [m["session_id"] for m in members_a] == ["early", "late"]
        assert [m["index"] for m in members_a] == [1, 2]


class TestBackupIntegration:
    """AC: `csb backup` populates segments for scanned sessions -- the
    fold rides the scanner's existing stream, no separate pass."""

    def test_backup_indexes_segments(self, monkeypatch,
                                     mock_claude_dir_repoless, tmp_path,
                                     capsys):
        from types import SimpleNamespace
        from claude_session_backup.commands import cmd_backup
        import claude_session_backup.commands as commands_mod
        monkeypatch.setattr(
            commands_mod, "git_repo_state",
            lambda d: ("absent", "fatal: not a git repository"))
        db = tmp_path / "idx.db"
        rc = cmd_backup(SimpleNamespace(
            claude_dir=str(mock_claude_dir_repoless), db=str(db),
            no_commit=True, quiet=True))
        assert rc == 0
        conn = open_db(str(db))
        init_schema(conn, quiet=True)
        segs = fetch_activity_segments(conn)
        conn.close()
        # The fixture session: 6 timestamped user/assistant events over
        # ~2 minutes -> one sitting, 6 messages.
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert sid in segs
        assert len(segs[sid]) == 1
        assert segs[sid][0][2] == 6


class TestRebuildCarry:
    """S5: the deleted-session snapshot carries segments across a
    rebuild -- a purged session's JSONL is gone, so losing them here
    would silently demote it to point-membership."""

    def test_snapshot_and_restore_round_trip_segments(self, tmp_path):
        from claude_session_backup.index import (
            mark_deleted,
            restore_deleted_snapshot,
            snapshot_deleted_sessions,
        )
        conn = open_db(str(tmp_path / "a.db"))
        init_schema(conn, quiet=True)
        upsert_session(conn, _meta_with_segments("purged-1", [
            ("2026-08-01T10:00:00Z", "2026-08-01T10:30:00Z", 5),
            ("2026-08-02T09:00:00Z", "2026-08-02T09:45:00Z", 8),
        ]))
        mark_deleted(conn, "purged-1", "2026-08-05T00:00:00Z")
        snapshot = snapshot_deleted_sessions(conn)
        conn.close()

        # A fresh DB, as after the rebuild's rename-aside + rescan.
        conn2 = open_db(str(tmp_path / "b.db"))
        init_schema(conn2, quiet=True)
        assert restore_deleted_snapshot(conn2, snapshot) == 1
        segs = fetch_activity_segments(conn2)
        conn2.close()
        assert len(segs.get("purged-1", [])) == 2


# ── End to end: the materializer, fences and all ──────────────────────

UUID_MYPROJECT = "aaaa1111-2222-3333-4444-000000000001"
UUID_GHOST = "aaaa2222-2222-3333-4444-000000000002"

# Three epochs (mirrors test_epoch_history's shape):
#   last~2: [Jul 1 08:00, Jul 10 20:00]
#   last~1: [Jul 10 20:05, Jul 15 04:16]
#   last:   [Jul 15 05:00, Jul 25 16:16]
E2E_FENCES = [
    Fence(at_utc=_utc(2026, 7, 1, 8, 0), event_id=EVENT_BOOT),
    Fence(at_utc=_utc(2026, 7, 10, 20, 0), event_id=EVENT_SHUTDOWN_CLEAN),
    Fence(at_utc=_utc(2026, 7, 10, 20, 5), event_id=EVENT_BOOT),
    Fence(at_utc=_utc(2026, 7, 15, 4, 16, 21), event_id=EVENT_SHUTDOWN_CLEAN),
    Fence(at_utc=_utc(2026, 7, 15, 5, 0), event_id=EVENT_BOOT),
    Fence(at_utc=_utc(2026, 7, 25, 16, 16), event_id=EVENT_SHUTDOWN_CLEAN),
    Fence(at_utc=_utc(2026, 7, 25, 16, 20), event_id=EVENT_BOOT),
]


@pytest.fixture
def e2e(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    db = tmp_path / "segs.db"
    conn = open_db(str(db))
    init_schema(conn, quiet=True)
    # MYPROJECT: created during last~1, reactivated during last -- so its
    # last_active_at sits in `last` and point membership loses last~1.
    conn.execute(
        "INSERT INTO sessions (session_id, session_name, project,"
        " start_folder, started_at, last_active_at, is_fork, message_count)"
        " VALUES (?, 'MYPROJECT', 'C--code-my', 'C:\\code\\my',"
        " '2026-07-14T09:00:00Z', '2026-07-22T12:00:00Z', 0, 50)",
        (UUID_MYPROJECT,),
    )
    for seg_start, seg_end, msgs in [
        ("2026-07-14T09:00:00Z", "2026-07-14T11:00:00Z", 20),   # last~1
        ("2026-07-22T10:00:00Z", "2026-07-22T12:00:00Z", 30),   # last
    ]:
        conn.execute(
            "INSERT INTO session_activity (session_id, seg_start, seg_end,"
            " messages) VALUES (?, ?, ?, ?)",
            (UUID_MYPROJECT, seg_start, seg_end, msgs),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(commands_module, "read_fences",
                        lambda *a, **k: list(E2E_FENCES))
    import types
    return types.SimpleNamespace(claude_dir=claude_dir, db=db)


def _run(e2e_env, *argv):
    return cli.main(list(argv) + ["--claude-dir", str(e2e_env.claude_dir),
                                  "--db", str(e2e_env.db)])


class TestEndToEnd:
    def test_myproject_shows_in_both_epochs(self, e2e, capsys):
        """THE founding red-green, through the real materializer."""
        assert _run(e2e, "set", "show", "last") == 0
        assert "MYPROJECT" in capsys.readouterr().out
        assert _run(e2e, "set", "show", "last~1") == 0
        out = capsys.readouterr().out
        assert "MYPROJECT" in out          # was lost under point membership
        assert "20 msgs" in out            # IN-WINDOW count, not the 50

    def test_epoch_between_sittings_stays_empty_of_it(self, e2e, capsys):
        assert _run(e2e, "set", "show", "last~2") in (0, 1)
        out = capsys.readouterr().out
        assert "MYPROJECT" not in out.replace(
            "nearest epoch with members", "")

    def test_snapshot_union_row_appended_and_marked(self, e2e, capsys):
        """S3: a snapshot-proven session with no indexed activity in the
        window is APPENDED and disclosed -- never dropped."""
        bdir = e2e.claude_dir / "csb-live" / "boundaries"
        bdir.mkdir(parents=True)
        (bdir / "boundary-20260715T050000Z.json").write_text(json.dumps({
            "captured_at": "2026-07-15T05:01:00Z",
            "boot_at": "2026-07-15T05:00:00Z",
            "open_at_shutdown": [{
                "session_id": UUID_GHOST,
                "started_at": "2026-07-14T08:00:00Z",
                "cwd": "C:\\code\\ghost",
            }],
        }), encoding="utf-8")
        assert _run(e2e, "set", "show", "last~1") == 0
        out = capsys.readouterr().out
        assert UUID_GHOST in out
        assert "no indexed activity in this window" in out
        assert "2 sessions" in out         # counted: MYPROJECT + the ghost

    def test_snapshot_union_row_in_json(self, e2e, capsys):
        bdir = e2e.claude_dir / "csb-live" / "boundaries"
        bdir.mkdir(parents=True)
        (bdir / "boundary-20260715T050000Z.json").write_text(json.dumps({
            "captured_at": "2026-07-15T05:01:00Z",
            "boot_at": "2026-07-15T05:00:00Z",
            "open_at_shutdown": [{
                "session_id": UUID_GHOST,
                "started_at": "2026-07-14T08:00:00Z",
                "cwd": "C:\\code\\ghost",
            }],
        }), encoding="utf-8")
        assert _run(e2e, "set", "show", "last~1", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        by_id = {m["session_id"]: m for m in payload["members"]}
        ghost = by_id[UUID_GHOST]
        assert ghost["snapshot_only"] is True
        assert ghost["open_at_shutdown"] is True
        assert ghost["in_index"] is False
        assert by_id[UUID_MYPROJECT]["segment_backed"] is True
        assert by_id[UUID_MYPROJECT]["messages"] == 20
        # Union rows continue the numbering -- appended, never interleaved.
        assert ghost["index"] == len(payload["members"])

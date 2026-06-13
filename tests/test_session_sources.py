"""Tests for register_session_sources() in claude_session_backup.index."""

import pytest

from claude_session_backup.index import register_session_sources
from claude_session_backup.sesslog_scanner import SourceRow


def _make_session(conn, session_id, project="myproj"):
    """Insert a minimal session row so FK constraints are satisfied."""
    conn.execute(
        "INSERT INTO sessions (session_id, project) VALUES (?, ?)",
        (session_id, project),
    )
    conn.commit()


def _count_sources(conn, session_id):
    return conn.execute(
        "SELECT COUNT(*) FROM session_sources WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]


# ── Basic upsert ──────────────────────────────────────────────────────


def test_register_basic(mock_db):
    _make_session(mock_db, "sess-1")
    sources = [
        SourceRow("convo", "/x/.convo_a.log", 100, "2026-05-16T10:00:00+00:00"),
        SourceRow("sesslog", "/x/.sesslog_a.log", 200, "2026-05-16T10:00:00+00:00"),
        SourceRow("jsonl", "/y/sess-1.jsonl", 300, "2026-05-16T10:00:00+00:00"),
    ]
    added, removed = register_session_sources(
        mock_db, "sess-1", "myproj", sources, "2026-05-16T11:00:00+00:00"
    )
    assert added == 3
    assert removed == 0
    assert _count_sources(mock_db, "sess-1") == 3


def test_register_accepts_dict_sources(mock_db):
    """Caller can pass plain dicts as well as SourceRow dataclasses."""
    _make_session(mock_db, "sess-1")
    sources = [
        {
            "source_type": "jsonl",
            "source_path": "/y/sess-1.jsonl",
            "size_bytes": 100,
            "mtime": "2026-05-16T10:00:00+00:00",
        }
    ]
    added, removed = register_session_sources(
        mock_db, "sess-1", "myproj", sources, "2026-05-16T11:00:00+00:00"
    )
    assert added == 1
    assert removed == 0


def test_register_empty_source_list(mock_db):
    """Empty sources should clear out any existing rows but add none."""
    _make_session(mock_db, "sess-1")
    register_session_sources(
        mock_db, "sess-1", "myproj",
        [SourceRow("jsonl", "/y/a.jsonl", 1, "2026-05-16T10:00:00+00:00")],
        "2026-05-16T10:00:00+00:00",
    )
    assert _count_sources(mock_db, "sess-1") == 1

    added, removed = register_session_sources(
        mock_db, "sess-1", "myproj", [], "2026-05-16T11:00:00+00:00"
    )
    assert added == 0
    assert removed == 1
    assert _count_sources(mock_db, "sess-1") == 0


# ── Idempotency / replace semantics ───────────────────────────────────


def test_register_replaces_existing_rows(mock_db):
    _make_session(mock_db, "sess-1")
    register_session_sources(
        mock_db, "sess-1", "myproj",
        [
            SourceRow("convo", "/x/.convo_a.log", 100, "t1"),
            SourceRow("jsonl", "/y/old.jsonl", 200, "t1"),
        ],
        "t1",
    )
    assert _count_sources(mock_db, "sess-1") == 2

    # Replace with a different set
    added, removed = register_session_sources(
        mock_db, "sess-1", "myproj",
        [SourceRow("jsonl", "/y/new.jsonl", 300, "t2")],
        "t2",
    )
    assert added == 1
    assert removed == 2
    assert _count_sources(mock_db, "sess-1") == 1

    paths = [
        r["source_path"]
        for r in mock_db.execute(
            "SELECT source_path FROM session_sources WHERE session_id = 'sess-1'"
        ).fetchall()
    ]
    assert paths == ["/y/new.jsonl"]


def test_register_is_repeatable_no_growth(mock_db):
    """Calling register twice with the same sources doesn't accumulate rows."""
    _make_session(mock_db, "sess-1")
    sources = [
        SourceRow("convo", "/x/.convo_a.log", 100, "t1"),
        SourceRow("jsonl", "/y/sess-1.jsonl", 200, "t1"),
    ]

    register_session_sources(mock_db, "sess-1", "myproj", sources, "t1")
    register_session_sources(mock_db, "sess-1", "myproj", sources, "t2")

    assert _count_sources(mock_db, "sess-1") == 2


def test_register_isolates_per_session(mock_db):
    """Replacing one session's sources doesn't touch another's."""
    _make_session(mock_db, "sess-1")
    _make_session(mock_db, "sess-2", project="other")

    register_session_sources(
        mock_db, "sess-1", "myproj",
        [SourceRow("jsonl", "/y/1.jsonl", 1, "t1")], "t1",
    )
    register_session_sources(
        mock_db, "sess-2", "other",
        [SourceRow("jsonl", "/y/2.jsonl", 2, "t1")], "t1",
    )

    # Replace sess-1 only
    register_session_sources(
        mock_db, "sess-1", "myproj",
        [SourceRow("convo", "/x/.convo.log", 3, "t2")], "t2",
    )

    assert _count_sources(mock_db, "sess-1") == 1
    assert _count_sources(mock_db, "sess-2") == 1


# ── Schema invariants ─────────────────────────────────────────────────


def test_register_leaves_fts5_columns_null(mock_db):
    _make_session(mock_db, "sess-1")
    register_session_sources(
        mock_db, "sess-1", "myproj",
        [SourceRow("jsonl", "/y/a.jsonl", 1, "t1")], "t1",
    )

    row = mock_db.execute(
        "SELECT fts5_indexed_at, content_hash FROM session_sources "
        "WHERE session_id = 'sess-1'"
    ).fetchone()
    assert row["fts5_indexed_at"] is None
    assert row["content_hash"] is None


def test_register_stores_size_and_mtime(mock_db):
    _make_session(mock_db, "sess-1")
    register_session_sources(
        mock_db, "sess-1", "myproj",
        [SourceRow("convo", "/x/.convo.log", 4242, "2026-05-16T10:00:00+00:00")],
        "2026-05-16T11:00:00+00:00",
    )

    row = mock_db.execute(
        "SELECT size_bytes, mtime, last_seen FROM session_sources "
        "WHERE session_id = 'sess-1'"
    ).fetchone()
    assert row["size_bytes"] == 4242
    assert row["mtime"] == "2026-05-16T10:00:00+00:00"
    assert row["last_seen"] == "2026-05-16T11:00:00+00:00"


def test_register_stores_project_for_fts5_routing(mock_db):
    """The project column routes per-project FTS5 lookups."""
    _make_session(mock_db, "sess-1", project="amdead")
    register_session_sources(
        mock_db, "sess-1", "amdead",
        [SourceRow("jsonl", "/y/a.jsonl", 1, "t1")], "t1",
    )

    row = mock_db.execute(
        "SELECT project FROM session_sources WHERE session_id = 'sess-1'"
    ).fetchone()
    assert row["project"] == "amdead"


def test_register_cascade_on_session_delete(mock_db):
    """Deleting a session row also drops its session_sources rows (FK cascade)."""
    _make_session(mock_db, "sess-1")
    register_session_sources(
        mock_db, "sess-1", "myproj",
        [SourceRow("jsonl", "/y/a.jsonl", 1, "t1")], "t1",
    )
    assert _count_sources(mock_db, "sess-1") == 1

    mock_db.execute("DELETE FROM sessions WHERE session_id = 'sess-1'")
    mock_db.commit()

    assert _count_sources(mock_db, "sess-1") == 0


def test_register_handles_null_size_and_mtime(mock_db):
    """If the source file is missing, size/mtime are NULL but the row still inserts."""
    _make_session(mock_db, "sess-1")
    register_session_sources(
        mock_db, "sess-1", "myproj",
        [SourceRow("jsonl", "/y/missing.jsonl", None, None)],
        "t1",
    )

    row = mock_db.execute(
        "SELECT size_bytes, mtime FROM session_sources WHERE session_id = 'sess-1'"
    ).fetchone()
    assert row["size_bytes"] is None
    assert row["mtime"] is None

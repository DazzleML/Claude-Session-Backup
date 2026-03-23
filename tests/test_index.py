"""Tests for the SQLite index."""

from claude_session_backup.index import (
    upsert_session, list_sessions, get_session, mark_deleted,
    get_active_session_ids, get_stats, search_sessions,
)
from claude_session_backup.metadata import SessionMetadata


def _make_meta(session_id="test-id", name="test-session", project="C--code"):
    return SessionMetadata(
        session_id=session_id,
        session_name=name,
        project=project,
        start_folder="/home/user/code",
        started_at="2026-03-23T10:00:00Z",
        last_active_at="2026-03-23T10:30:00Z",
        message_count=42,
        tool_call_count=10,
        claude_version="2.1.81",
        folder_usage={
            "/home/user/code": 20,
            "/home/user/other": 15,
        },
    )


def test_upsert_and_get(mock_db):
    meta = _make_meta()
    upsert_session(mock_db, meta, "projects/C--code/test-id.jsonl", 1024, "2026-03-23T10:30:00Z")

    session = get_session(mock_db, "test-id")
    assert session is not None
    assert session["session_name"] == "test-session"
    assert session["message_count"] == 42
    assert len(session["folders"]) == 2


def test_upsert_updates_existing(mock_db):
    meta1 = _make_meta(name="original")
    upsert_session(mock_db, meta1, "path.jsonl", 100, "t1")

    meta2 = _make_meta(name="updated")
    meta2.message_count = 99
    upsert_session(mock_db, meta2, "path.jsonl", 200, "t2")

    session = get_session(mock_db, "test-id")
    assert session["session_name"] == "updated"
    assert session["message_count"] == 99


def test_mark_deleted(mock_db):
    meta = _make_meta()
    upsert_session(mock_db, meta, "path.jsonl", 100, "t1")

    mark_deleted(mock_db, "test-id", "2026-03-24T00:00:00Z")

    session = get_session(mock_db, "test-id")
    assert session["deleted_at"] == "2026-03-24T00:00:00Z"


def test_list_sessions_excludes_deleted(mock_db):
    meta1 = _make_meta("id1", "active")
    meta2 = _make_meta("id2", "deleted")

    upsert_session(mock_db, meta1, "p1.jsonl", 100, "t1")
    upsert_session(mock_db, meta2, "p2.jsonl", 100, "t1")
    mark_deleted(mock_db, "id2", "2026-03-24T00:00:00Z")

    active = list_sessions(mock_db, show_deleted=False)
    assert len(active) == 1
    assert active[0]["session_id"] == "id1"


def test_list_deleted_only(mock_db):
    meta1 = _make_meta("id1", "active")
    meta2 = _make_meta("id2", "deleted")

    upsert_session(mock_db, meta1, "p1.jsonl", 100, "t1")
    upsert_session(mock_db, meta2, "p2.jsonl", 100, "t1")
    mark_deleted(mock_db, "id2", "2026-03-24T00:00:00Z")

    deleted = list_sessions(mock_db, show_deleted=True)
    assert len(deleted) == 1
    assert deleted[0]["session_id"] == "id2"


def test_get_active_session_ids(mock_db):
    meta1 = _make_meta("id1", "one")
    meta2 = _make_meta("id2", "two")

    upsert_session(mock_db, meta1, "p1.jsonl", 100, "t1")
    upsert_session(mock_db, meta2, "p2.jsonl", 100, "t1")
    mark_deleted(mock_db, "id2", "2026-03-24T00:00:00Z")

    ids = get_active_session_ids(mock_db)
    assert ids == {"id1"}


def test_search(mock_db):
    meta = _make_meta(name="deploy-fix")
    upsert_session(mock_db, meta, "path.jsonl", 100, "t1")

    results = search_sessions(mock_db, "deploy")
    assert len(results) == 1
    assert results[0]["session_name"] == "deploy-fix"


def test_stats(mock_db):
    meta = _make_meta()
    upsert_session(mock_db, meta, "path.jsonl", 100, "t1")

    stats = get_stats(mock_db)
    assert stats["total_sessions"] == 1
    assert stats["active_sessions"] == 1
    assert stats["deleted_sessions"] == 0

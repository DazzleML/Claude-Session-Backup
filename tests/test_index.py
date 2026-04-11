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


# ── Sort tests ───────────────────────────────────────────────────────


def _make_meta_with(session_id, **overrides):
    """Build a SessionMetadata with custom field values for sort tests."""
    meta = _make_meta(session_id, f"sess-{session_id}")
    for key, value in overrides.items():
        setattr(meta, key, value)
    return meta


def test_list_sort_default_is_last_used(mock_db):
    # Two sessions with distinct last_active_at -- default sort should
    # return the newer one first (regression guard for backward compat).
    meta_old = _make_meta_with("id-old", last_active_at="2026-01-01T00:00:00Z")
    meta_new = _make_meta_with("id-new", last_active_at="2026-05-01T00:00:00Z")

    upsert_session(mock_db, meta_old, "p1.jsonl", 100, 1000.0, "t1")
    upsert_session(mock_db, meta_new, "p2.jsonl", 100, 2000.0, "t1")

    result = list_sessions(mock_db)
    assert [s["session_id"] for s in result] == ["id-new", "id-old"]


def test_list_sort_by_expiration(mock_db):
    # Oldest mtime = least days remaining = should appear first.
    meta_a = _make_meta_with("id-a")
    meta_b = _make_meta_with("id-b")
    meta_c = _make_meta_with("id-c")

    upsert_session(mock_db, meta_a, "a.jsonl", 100, 1000.0, "t1")  # oldest
    upsert_session(mock_db, meta_b, "b.jsonl", 100, 3000.0, "t1")  # newest
    upsert_session(mock_db, meta_c, "c.jsonl", 100, 2000.0, "t1")  # middle

    result = list_sessions(mock_db, sort_key="expiration")
    assert [s["session_id"] for s in result] == ["id-a", "id-c", "id-b"]


def test_list_sort_by_expiration_skips_zero_mtime(mock_db):
    # Zero mtime (never scanned) should sort to the bottom so real
    # expiring sessions are surfaced first.
    meta_real = _make_meta_with("id-real")
    meta_zero = _make_meta_with("id-zero")

    upsert_session(mock_db, meta_real, "real.jsonl", 100, 5000.0, "t1")
    upsert_session(mock_db, meta_zero, "zero.jsonl", 100, 0.0, "t1")

    result = list_sessions(mock_db, sort_key="expiration")
    assert result[0]["session_id"] == "id-real"
    assert result[-1]["session_id"] == "id-zero"


def test_list_sort_by_messages(mock_db):
    meta_small = _make_meta_with("id-small", message_count=5)
    meta_big = _make_meta_with("id-big", message_count=100)
    meta_mid = _make_meta_with("id-mid", message_count=20)

    upsert_session(mock_db, meta_small, "s.jsonl", 100, 1000.0, "t1")
    upsert_session(mock_db, meta_big, "b.jsonl", 100, 1000.0, "t1")
    upsert_session(mock_db, meta_mid, "m.jsonl", 100, 1000.0, "t1")

    result = list_sessions(mock_db, sort_key="messages")
    assert [s["session_id"] for s in result] == ["id-big", "id-mid", "id-small"]


def test_list_sort_by_size(mock_db):
    meta_a = _make_meta_with("id-a")
    meta_b = _make_meta_with("id-b")
    meta_c = _make_meta_with("id-c")

    upsert_session(mock_db, meta_a, "a.jsonl", 100, 1000.0, "t1")    # smallest
    upsert_session(mock_db, meta_b, "b.jsonl", 5000, 1000.0, "t1")   # biggest
    upsert_session(mock_db, meta_c, "c.jsonl", 800, 1000.0, "t1")    # middle

    result = list_sessions(mock_db, sort_key="size")
    assert [s["session_id"] for s in result] == ["id-b", "id-c", "id-a"]


def test_list_sort_by_started_and_oldest(mock_db):
    meta_old = _make_meta_with("id-old", started_at="2026-01-01T00:00:00Z")
    meta_new = _make_meta_with("id-new", started_at="2026-05-01T00:00:00Z")

    upsert_session(mock_db, meta_old, "o.jsonl", 100, 1000.0, "t1")
    upsert_session(mock_db, meta_new, "n.jsonl", 100, 1000.0, "t1")

    newest_first = list_sessions(mock_db, sort_key="started")
    assert [s["session_id"] for s in newest_first] == ["id-new", "id-old"]

    oldest_first = list_sessions(mock_db, sort_key="oldest")
    assert [s["session_id"] for s in oldest_first] == ["id-old", "id-new"]


def test_list_sort_invalid_key_raises(mock_db):
    import pytest
    with pytest.raises(ValueError, match="Unknown sort_key"):
        list_sessions(mock_db, sort_key="bogus")


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

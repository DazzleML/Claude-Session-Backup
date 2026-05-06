"""Tests for the SQLite index."""

from claude_session_backup.index import (
    upsert_session, list_sessions, get_session, mark_deleted,
    get_active_session_ids, get_stats, search_sessions,
    find_sessions_by_directory, find_sessions_by_term, escape_like_value,
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


# ── escape_like_value tests (#20) ──────────────────────────────────

def test_escape_like_passthrough():
    """Plain paths with no LIKE-special chars are unchanged."""
    assert escape_like_value("C:\\code\\amdead") == "C:\\code\\amdead"
    assert escape_like_value("/home/user/code") == "/home/user/code"


def test_escape_like_underscore():
    """Underscore in user input gets escaped."""
    assert escape_like_value("C:\\my_folder") == "C:\\my|_folder"


def test_escape_like_percent():
    """Percent in user input gets escaped."""
    assert escape_like_value("50%-share") == "50|%-share"


def test_escape_like_escape_char_doubled():
    """The escape char itself in user input gets doubled."""
    assert escape_like_value("|literal-pipe") == "||literal-pipe"


def test_escape_like_combined():
    """Multiple special chars in one path."""
    assert escape_like_value("a_b%c|d") == "a|_b|%c||d"


# ── find_sessions_by_directory tests (#20) ─────────────────────────

def _make_meta_with_folders(session_id, name, start_folder, folder_usage):
    """Helper for creating sessions with specific folder_usage patterns."""
    return SessionMetadata(
        session_id=session_id,
        session_name=name,
        project="C--code",
        start_folder=start_folder,
        started_at="2026-03-23T10:00:00Z",
        last_active_at="2026-03-23T10:30:00Z",
        message_count=42,
        tool_call_count=10,
        claude_version="2.1.81",
        folder_usage=folder_usage,
    )


def test_find_by_directory_exact_match_start_folder(mock_db):
    """-D <folder>: matches session whose start_folder equals exactly."""
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "amdead-session", "C:\\code\\amdead", {"C:\\code\\amdead": 100}),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s2", "fork-session", "C:\\code\\amdead-fork", {"C:\\code\\amdead-fork": 50}),
        "p.jsonl", 100, "t1",
    )

    # -D amdead: exact match on start_folder, no LIKE pattern
    results = find_sessions_by_directory(
        mock_db,
        exact_value="C:\\code\\amdead",
        like_match=None,
        like_exclude=None,
        top_n=3,
    )
    ids = {r["session_id"] for r in results}
    assert ids == {"s1"}  # s2 (sibling-prefix) excluded


def test_find_by_directory_descendants(mock_db):
    """-d <folder>: matches session whose folder_usage has descendants."""
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "in-amdead", "C:\\code\\amdead", {
            "C:\\code\\amdead\\subdir": 200,
            "C:\\code\\amdead": 50,
        }),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s2", "in-other", "C:\\code\\other", {"C:\\code\\other": 100}),
        "p.jsonl", 100, "t1",
    )

    # -d amdead: exact OR descendants
    results = find_sessions_by_directory(
        mock_db,
        exact_value="C:\\code\\amdead",
        like_match="C:\\code\\amdead\\%",
        like_exclude=None,
        top_n=3,
    )
    ids = {r["session_id"] for r in results}
    assert ids == {"s1"}


def test_find_by_directory_wildcard_with_descendants(mock_db):
    """-d amdead*: prefix-LIKE catches siblings + descendants."""
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "amdead", "C:\\code\\amdead", {"C:\\code\\amdead": 100}),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s2", "amdead-fork", "C:\\code\\amdead-fork", {"C:\\code\\amdead-fork": 50}),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s3", "amdead-sub", "C:\\code\\amdead\\sub", {"C:\\code\\amdead\\sub": 30}),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s4", "unrelated", "C:\\code\\other", {"C:\\code\\other": 20}),
        "p.jsonl", 100, "t1",
    )

    # -d amdead*: like_match = "C:\code\amdead%", no exclude
    results = find_sessions_by_directory(
        mock_db,
        exact_value=None,
        like_match="C:\\code\\amdead%",
        like_exclude=None,
        top_n=3,
    )
    ids = {r["session_id"] for r in results}
    assert ids == {"s1", "s2", "s3"}  # s4 (other) excluded


def test_find_by_directory_wildcard_no_descendants(mock_db):
    """-D amdead*: prefix LIKE with NOT LIKE descendant exclusion."""
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "amdead", "C:\\code\\amdead", {"C:\\code\\amdead": 100}),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s2", "amdead-fork", "C:\\code\\amdead-fork", {"C:\\code\\amdead-fork": 50}),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s3", "amdead-sub", "C:\\code\\amdead\\sub", {"C:\\code\\amdead\\sub": 30}),
        "p.jsonl", 100, "t1",
    )

    # -D amdead*: like_match = "C:\code\amdead%", like_exclude = "C:\code\amdead%\%"
    results = find_sessions_by_directory(
        mock_db,
        exact_value=None,
        like_match="C:\\code\\amdead%",
        like_exclude="C:\\code\\amdead%\\%",
        top_n=3,
    )
    ids = {r["session_id"] for r in results}
    assert ids == {"s1", "s2"}  # s3 (descendant) excluded


def test_find_by_directory_top_n_gates_folder_usage(mock_db):
    """folder_usage entries below top-N are NOT eligible for matching."""
    # Session whose start_folder is unrelated, but folder_usage has amdead at rank 5.
    # Top entries dominate, so rank-5 amdead should be filtered out at top_n=3.
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "elsewhere", "C:\\Users\\Bob", {
            "C:\\Users\\Bob\\proj1": 1000,
            "C:\\Users\\Bob\\proj2": 800,
            "C:\\Users\\Bob\\proj3": 600,
            "C:\\Users\\Bob\\proj4": 400,
            "C:\\code\\amdead": 5,  # rank 5 -- below top 3
        }),
        "p.jsonl", 100, "t1",
    )
    # Session with amdead in top-3
    upsert_session(
        mock_db,
        _make_meta_with_folders("s2", "in-amdead", "C:\\Users\\Bob", {
            "C:\\code\\amdead": 1000,  # rank 1
            "C:\\Users\\Bob\\other": 100,
        }),
        "p.jsonl", 100, "t1",
    )

    results = find_sessions_by_directory(
        mock_db,
        exact_value="C:\\code\\amdead",
        like_match=None,
        like_exclude=None,
        top_n=3,
    )
    ids = {r["session_id"] for r in results}
    assert ids == {"s2"}  # s1's rank-5 amdead is filtered out


def test_find_by_directory_top_n_none_includes_all(mock_db):
    """top_n=None means no gate -- a rank-5 folder_usage match is eligible."""
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "rank5", "C:\\Users\\Bob", {
            "C:\\Users\\Bob\\proj1": 1000,
            "C:\\Users\\Bob\\proj2": 800,
            "C:\\Users\\Bob\\proj3": 600,
            "C:\\Users\\Bob\\proj4": 400,
            "C:\\code\\amdead": 5,
        }),
        "p.jsonl", 100, "t1",
    )

    results = find_sessions_by_directory(
        mock_db,
        exact_value="C:\\code\\amdead",
        like_match=None,
        like_exclude=None,
        top_n=None,  # no gate
    )
    ids = {r["session_id"] for r in results}
    assert ids == {"s1"}


def test_find_by_directory_start_folder_always_eligible(mock_db):
    """start_folder matches even when no folder_usage entry would qualify."""
    # No folder_usage rows for this session -- only start_folder.
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "started-here", "C:\\code\\amdead", {}),
        "p.jsonl", 100, "t1",
    )

    results = find_sessions_by_directory(
        mock_db,
        exact_value="C:\\code\\amdead",
        like_match=None,
        like_exclude=None,
        top_n=3,
    )
    ids = {r["session_id"] for r in results}
    assert ids == {"s1"}


def test_find_by_directory_excludes_deleted(mock_db):
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "alive", "C:\\code\\amdead", {"C:\\code\\amdead": 100}),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s2", "dead", "C:\\code\\amdead", {"C:\\code\\amdead": 100}),
        "p.jsonl", 100, "t1",
    )
    mark_deleted(mock_db, "s2", "2026-03-24T00:00:00Z")

    results = find_sessions_by_directory(
        mock_db,
        exact_value="C:\\code\\amdead",
        like_match=None,
        like_exclude=None,
        top_n=3,
    )
    ids = {r["session_id"] for r in results}
    assert ids == {"s1"}


def test_find_by_directory_requires_match_criteria(mock_db):
    """Helper raises if neither exact_value nor like_match is provided."""
    import pytest
    with pytest.raises(ValueError):
        find_sessions_by_directory(
            mock_db,
            exact_value=None,
            like_match=None,
            like_exclude=None,
            top_n=3,
        )


def test_find_by_directory_start_folder_only_excludes_folder_usage_match(mock_db):
    """``start_folder_only=True`` skips folder_usage match entirely.

    Setup: a session whose start_folder is unrelated to 'amdead', but whose
    folder_usage contains 'C:\\code\\amdead' as a top-1 entry. Without the
    flag this would match (via folder_usage); with the flag it should NOT.
    """
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "elsewhere", "C:\\Users\\Bob", {
            "C:\\code\\amdead": 1000,
        }),
        "p.jsonl", 100, "t1",
    )

    # Without flag: matches via folder_usage
    results_default = find_sessions_by_directory(
        mock_db,
        exact_value="C:\\code\\amdead",
        like_match=None,
        like_exclude=None,
        top_n=3,
    )
    assert len(results_default) == 1

    # With flag: skips folder_usage check; start_folder doesn't match
    results_strict = find_sessions_by_directory(
        mock_db,
        exact_value="C:\\code\\amdead",
        like_match=None,
        like_exclude=None,
        top_n=3,
        start_folder_only=True,
    )
    assert len(results_strict) == 0


def test_find_by_directory_start_folder_only_matches_start_folder(mock_db):
    """When start_folder matches, ``start_folder_only=True`` still returns it."""
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "started-here", "C:\\code\\amdead", {
            "C:\\code\\amdead": 100,
            "C:\\Users\\Bob": 50,
        }),
        "p.jsonl", 100, "t1",
    )

    results = find_sessions_by_directory(
        mock_db,
        exact_value="C:\\code\\amdead",
        like_match=None,
        like_exclude=None,
        top_n=3,
        start_folder_only=True,
    )
    assert len(results) == 1
    assert results[0]["session_id"] == "s1"


def test_find_by_directory_start_folder_only_with_descendants(mock_db):
    """`-s amdead` (with descendants) matches sessions started in any subdir."""
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "exact", "C:\\code\\amdead", {}),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s2", "subdir", "C:\\code\\amdead\\sub", {}),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s3", "sibling", "C:\\code\\amdead-fork", {}),
        "p.jsonl", 100, "t1",
    )

    # -s amdead: exact OR descendants on start_folder
    results = find_sessions_by_directory(
        mock_db,
        exact_value="C:\\code\\amdead",
        like_match="C:\\code\\amdead\\%",
        like_exclude=None,
        top_n=None,
        start_folder_only=True,
    )
    ids = {r["session_id"] for r in results}
    assert ids == {"s1", "s2"}  # s3 (sibling-prefix) excluded


# ── find_sessions_by_term tests (#20) ──────────────────────────────

def test_find_by_term_matches_name(mock_db):
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "AMD_INTIGRITI-paper", "C:\\code", {}),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s2", "redditslack-bot", "C:\\code", {}),
        "p.jsonl", 100, "t1",
    )

    results = find_sessions_by_term(mock_db, "intigriti")
    ids = {r["session_id"] for r in results}
    assert ids == {"s1"}


def test_find_by_term_matches_start_folder(mock_db):
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "any", "C:\\code\\amdead", {}),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s2", "any", "C:\\code\\redditslack", {}),
        "p.jsonl", 100, "t1",
    )

    results = find_sessions_by_term(mock_db, "amdead")
    ids = {r["session_id"] for r in results}
    assert ids == {"s1"}


def test_find_by_term_matches_folder_usage_within_top_n(mock_db):
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "any", "C:\\Users", {
            "C:\\code\\amdead": 1000,  # rank 1 -- in top-3
            "C:\\code\\redditslack": 500,
        }),
        "p.jsonl", 100, "t1",
    )

    results = find_sessions_by_term(mock_db, "amdead", top_n=3)
    assert len(results) == 1
    assert results[0]["session_id"] == "s1"


def test_find_by_term_top_n_gates_low_rank_folder_usage(mock_db):
    """A folder_usage match at rank 50 with top_n=3 should NOT match."""
    folder_usage = {f"C:\\Users\\Bob\\proj{i}": 1000 - i for i in range(50)}
    folder_usage["C:\\code\\amdead"] = 1  # very last rank
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "elsewhere", "C:\\Users", folder_usage),
        "p.jsonl", 100, "t1",
    )

    results = find_sessions_by_term(mock_db, "amdead", top_n=3)
    assert len(results) == 0  # filtered out by top-N gate


def test_find_by_term_top_n_none_no_gate(mock_db):
    """top_n=None means no gate -- rank-50 folder_usage matches."""
    folder_usage = {f"C:\\Users\\Bob\\proj{i}": 1000 - i for i in range(50)}
    folder_usage["C:\\code\\amdead"] = 1
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "elsewhere", "C:\\Users", folder_usage),
        "p.jsonl", 100, "t1",
    )

    results = find_sessions_by_term(mock_db, "amdead", top_n=None)
    assert len(results) == 1
    assert results[0]["session_id"] == "s1"


def test_find_by_term_case_insensitive(mock_db):
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "amdead-paper", "C:\\code", {}),
        "p.jsonl", 100, "t1",
    )

    results_lower = find_sessions_by_term(mock_db, "amdead")
    results_upper = find_sessions_by_term(mock_db, "AMDEAD")
    results_mixed = find_sessions_by_term(mock_db, "AmDeAd")
    assert len(results_lower) == 1
    assert len(results_upper) == 1
    assert len(results_mixed) == 1


def test_find_by_term_excludes_deleted(mock_db):
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "amdead-alive", "C:\\code", {}),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s2", "amdead-dead", "C:\\code", {}),
        "p.jsonl", 100, "t1",
    )
    mark_deleted(mock_db, "s2", "2026-03-24T00:00:00Z")

    results = find_sessions_by_term(mock_db, "amdead")
    ids = {r["session_id"] for r in results}
    assert ids == {"s1"}


def test_find_by_term_escapes_user_wildcards(mock_db):
    """User-supplied % and _ should be literal, not LIKE wildcards."""
    upsert_session(
        mock_db,
        _make_meta_with_folders("s1", "literal_underscore", "C:\\code", {}),
        "p.jsonl", 100, "t1",
    )
    upsert_session(
        mock_db,
        _make_meta_with_folders("s2", "literalXunderscore", "C:\\code", {}),
        "p.jsonl", 100, "t1",
    )

    # Without escaping, "_" would match any single character, so s2 would also match.
    # With escaping, "_" is literal.
    results = find_sessions_by_term(mock_db, "literal_under")
    ids = {r["session_id"] for r in results}
    assert ids == {"s1"}

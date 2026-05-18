"""Tests for the per-project FTS5 database module (fts5_db.py)."""

import sqlite3
from pathlib import Path

import pytest

from claude_session_backup.fts5_db import (
    delete_session,
    escape_fts_query,
    fts5_available,
    init_fts5_schema,
    is_session_indexed,
    mark_session_indexed,
    open_fts5_db,
)


def _open_temp_db(tmp_path: Path) -> sqlite3.Connection:
    return open_fts5_db(tmp_path / "test.db")


# ── Schema creation + idempotency ────────────────────────────────────


def test_init_schema_creates_messages_table(tmp_path):
    conn = _open_temp_db(tmp_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    assert set(cols) >= {
        "id", "session_id", "uuid", "message_index",
        "role", "role_subtype", "content", "timestamp",
    }


def test_init_schema_creates_messages_fts_virtual_table(tmp_path):
    conn = _open_temp_db(tmp_path)
    # FTS5 virtual tables register with the sqlite_master schema
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'messages_fts'"
    ).fetchone()
    assert row is not None
    assert "fts5" in row[0].lower()
    assert "porter" in row[0].lower() and "unicode61" in row[0].lower()


def test_init_schema_creates_file_operations_table(tmp_path):
    conn = _open_temp_db(tmp_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(file_operations)").fetchall()]
    assert set(cols) >= {
        "id", "session_id", "message_index", "operation",
        "file_path", "timestamp",
    }


def test_init_schema_creates_indexed_sessions_table(tmp_path):
    conn = _open_temp_db(tmp_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(indexed_sessions)").fetchall()]
    assert set(cols) >= {
        "session_id", "last_jsonl_mtime", "last_content_hash", "indexed_at",
    }


def test_init_schema_idempotent(tmp_path):
    """Calling init_fts5_schema twice on the same DB doesn't error."""
    conn = _open_temp_db(tmp_path)
    init_fts5_schema(conn)  # second call
    init_fts5_schema(conn)  # third call -- still fine


def test_open_fts5_db_creates_parent_directory(tmp_path):
    """open_fts5_db creates the csb-fts/ directory if missing."""
    nested = tmp_path / "deeply" / "nested" / "csb-fts" / "test.db"
    assert not nested.parent.exists()
    conn = open_fts5_db(nested)
    assert nested.parent.exists()
    assert nested.exists()
    conn.close()


# ── Insert + FTS sync triggers ───────────────────────────────────────


def _insert(conn, **kwargs):
    """Test helper: insert one row with defaults."""
    defaults = {
        "session_id": "s1",
        "uuid": None,
        "message_index": 0,
        "role": "USER",
        "role_subtype": None,
        "content": "hello world",
        "timestamp": None,
    }
    defaults.update(kwargs)
    conn.execute(
        "INSERT INTO messages "
        "(session_id, uuid, message_index, role, role_subtype, content, timestamp) "
        "VALUES (:session_id, :uuid, :message_index, :role, :role_subtype, :content, :timestamp)",
        defaults,
    )
    conn.commit()


def test_insert_propagates_to_fts(tmp_path):
    """Trigger keeps messages_fts in sync on INSERT."""
    conn = _open_temp_db(tmp_path)
    _insert(conn, content="oauth callback handler")
    rows = conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'oauth'"
    ).fetchall()
    assert len(rows) == 1


def test_delete_propagates_to_fts(tmp_path):
    """Trigger removes from messages_fts on DELETE."""
    conn = _open_temp_db(tmp_path)
    _insert(conn, content="findable token")
    assert conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'findable'"
    ).fetchone()[0] == 1
    conn.execute("DELETE FROM messages")
    conn.commit()
    assert conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'findable'"
    ).fetchone()[0] == 0


def test_update_propagates_to_fts(tmp_path):
    """Trigger removes-then-inserts on UPDATE."""
    conn = _open_temp_db(tmp_path)
    _insert(conn, content="firstword here")
    conn.execute("UPDATE messages SET content = 'secondword here'")
    conn.commit()
    assert conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'firstword'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'secondword'"
    ).fetchone()[0] == 1


# ── Dedup uniqueness ─────────────────────────────────────────────────


def test_unique_session_message_index_constraint(tmp_path):
    """Two rows with same (session_id, message_index) -> IntegrityError."""
    conn = _open_temp_db(tmp_path)
    _insert(conn, session_id="s1", message_index=0)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, session_id="s1", message_index=0, content="dup")


def test_unique_uuid_constraint_for_non_null(tmp_path):
    """Two rows with same UUID -> IntegrityError. NULL uuid is allowed multiple times."""
    conn = _open_temp_db(tmp_path)
    _insert(conn, session_id="s1", message_index=0, uuid="abc")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, session_id="s2", message_index=0, uuid="abc")
    # NULL uuid: multiple rows OK
    _insert(conn, session_id="s3", message_index=0, uuid=None)
    _insert(conn, session_id="s4", message_index=0, uuid=None)


# ── is_session_indexed / mark_session_indexed ────────────────────────


def test_is_session_indexed_returns_false_for_unknown(tmp_path):
    conn = _open_temp_db(tmp_path)
    assert is_session_indexed(conn, "unknown-session", 1.0) is False


def test_is_session_indexed_returns_true_when_fresh(tmp_path):
    conn = _open_temp_db(tmp_path)
    mark_session_indexed(conn, "s1", 100.0, "hash", "2026-01-01T00:00:00Z")
    assert is_session_indexed(conn, "s1", 100.0) is True
    assert is_session_indexed(conn, "s1", 99.0) is True  # older request, still fresh


def test_is_session_indexed_returns_false_when_stale(tmp_path):
    """If jsonl_mtime > last_jsonl_mtime, session is stale."""
    conn = _open_temp_db(tmp_path)
    mark_session_indexed(conn, "s1", 100.0, "hash", "2026-01-01T00:00:00Z")
    assert is_session_indexed(conn, "s1", 200.0) is False


def test_mark_session_indexed_upsert(tmp_path):
    """Calling mark_session_indexed twice updates the row in place."""
    conn = _open_temp_db(tmp_path)
    mark_session_indexed(conn, "s1", 100.0, "h1", "2026-01-01T00:00:00Z")
    mark_session_indexed(conn, "s1", 200.0, "h2", "2026-02-01T00:00:00Z")
    rows = conn.execute(
        "SELECT * FROM indexed_sessions WHERE session_id = 's1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["last_jsonl_mtime"] == 200.0
    assert rows[0]["last_content_hash"] == "h2"


# ── delete_session cascades ──────────────────────────────────────────


def test_delete_session_purges_messages_and_metadata(tmp_path):
    conn = _open_temp_db(tmp_path)
    _insert(conn, session_id="s1", message_index=0, content="findme")
    _insert(conn, session_id="s1", message_index=1, content="alsoMe")
    mark_session_indexed(conn, "s1", 100.0, "h", "2026-01-01T00:00:00Z")
    conn.execute(
        "INSERT INTO file_operations "
        "(session_id, message_index, operation, file_path) "
        "VALUES ('s1', 0, 'read', '/x')"
    )
    conn.commit()

    n = delete_session(conn, "s1")
    assert n == 2  # messages deleted

    assert conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='s1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'findme'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM file_operations WHERE session_id='s1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM indexed_sessions WHERE session_id='s1'").fetchone()[0] == 0


# ── escape_fts_query ─────────────────────────────────────────────────


def test_escape_fts_query_wraps_tokens():
    assert escape_fts_query("oauth callback") == '"oauth" "callback"'


def test_escape_fts_query_strips_punctuation_in_tokens():
    """parens, commas, etc inside tokens are stripped (FTS5 would reject otherwise)."""
    out = escape_fts_query("oauth (v2)")
    assert "(" not in out and ")" not in out
    # The token 'v2' survives, and oauth is wrapped
    assert '"oauth"' in out
    assert '"v2"' in out


def test_escape_fts_query_empty_input():
    assert escape_fts_query("") == ""
    assert escape_fts_query("   ") == ""


def test_escape_fts_query_passes_through_explicit_operators():
    """If user types FTS5 syntax, don't wrap."""
    # User passes an explicit AND -- treat as already-FTS5
    raw = "oauth AND callback"
    out = escape_fts_query(raw)
    assert "AND" in out  # not mangled


# ── fts5_available ──────────────────────────────────────────────────


def test_fts5_available_on_modern_python():
    """Python 3.10+ ships with SQLite that has FTS5. This should be True."""
    assert fts5_available() is True

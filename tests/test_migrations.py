"""Tests for the schema migration framework.

Covers:
- Fresh DB fast-path (init_schema stamps SCHEMA_VERSION without running migrations)
- Upgrade path: v2 DB exists -> apply_pending runs _v3_add_session_sources
- Idempotency: re-running apply_pending after upgrade is a no-op
- Current-version short-circuit: starts at SCHEMA_VERSION -> no migrations run
- The migrated table actually exists and matches the expected schema
"""

import sqlite3

import pytest

from claude_session_backup import migrations
from claude_session_backup.index import SCHEMA_VERSION, open_db, init_schema


# ── Helpers ───────────────────────────────────────────────────────────


def _make_v2_db(db_path: str) -> sqlite3.Connection:
    """Create a database that looks like an old v2 install.

    Old v2 schema had: schema_info, sessions, folder_usage, scan_history,
    and three indexes -- but NO session_sources table.
    """
    conn = open_db(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_info (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            session_name TEXT,
            start_folder TEXT,
            started_at TEXT,
            last_active_at TEXT,
            last_user_at TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            claude_version TEXT,
            jsonl_path TEXT,
            jsonl_size INTEGER DEFAULT 0,
            jsonl_mtime REAL DEFAULT 0,
            last_scanned_at TEXT,
            deleted_at TEXT,
            last_git_commit TEXT
        );

        CREATE TABLE IF NOT EXISTS folder_usage (
            session_id TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            usage_count INTEGER DEFAULT 1,
            is_start_folder INTEGER DEFAULT 0,
            PRIMARY KEY (session_id, folder_path),
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS scan_history (
            scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TEXT NOT NULL,
            sessions_found INTEGER,
            sessions_new INTEGER,
            sessions_updated INTEGER,
            sessions_deleted INTEGER,
            git_commit TEXT
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_info (key, value) VALUES ('schema_version', '2')"
    )
    conn.commit()
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _has_index(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _get_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM schema_info WHERE key = 'schema_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


# ── Fast path: fresh DB ───────────────────────────────────────────────


def test_init_schema_on_fresh_db_stamps_current_version(tmp_path):
    conn = open_db(str(tmp_path / "fresh.db"))
    init_schema(conn)

    assert _get_version(conn) == SCHEMA_VERSION
    assert _has_table(conn, "session_sources")


def test_init_schema_on_fresh_db_does_not_run_migrations(tmp_path, capsys):
    """Fresh DBs take the fast path; no 'csb: migrated DB schema...' lines."""
    conn = open_db(str(tmp_path / "fresh.db"))
    init_schema(conn, quiet=False)

    captured = capsys.readouterr()
    assert "migrated DB schema" not in captured.out


# ── Upgrade path: v2 -> v3 ────────────────────────────────────────────


def test_apply_pending_from_v2_to_current(tmp_path):
    conn = _make_v2_db(str(tmp_path / "old.db"))
    assert _get_version(conn) == 2
    assert not _has_table(conn, "session_sources")

    new_version = migrations.apply_pending(conn, quiet=True)

    assert new_version == SCHEMA_VERSION
    assert _get_version(conn) == SCHEMA_VERSION
    # v3 migration: session_sources
    assert _has_table(conn, "session_sources")
    # v4 migration (v0.3.11): git_deleted_jsonls cache
    if SCHEMA_VERSION >= 4:
        assert _has_table(conn, "git_deleted_jsonls")


def test_apply_pending_from_v2_creates_indexes(tmp_path):
    conn = _make_v2_db(str(tmp_path / "old.db"))
    migrations.apply_pending(conn, quiet=True)

    assert _has_index(conn, "idx_session_sources_session")
    assert _has_index(conn, "idx_session_sources_project")
    assert _has_index(conn, "idx_session_sources_fts5")


def test_apply_pending_emits_notice_when_not_quiet(tmp_path, capsys):
    conn = _make_v2_db(str(tmp_path / "old.db"))
    migrations.apply_pending(conn, quiet=False)

    captured = capsys.readouterr()
    # Every applied migration should announce itself.
    assert "csb: migrated DB schema to v3" in captured.out
    if SCHEMA_VERSION >= 4:
        assert "csb: migrated DB schema to v4" in captured.out


def test_apply_pending_quiet_emits_nothing(tmp_path, capsys):
    conn = _make_v2_db(str(tmp_path / "old.db"))
    migrations.apply_pending(conn, quiet=True)

    captured = capsys.readouterr()
    assert captured.out == ""


# ── Idempotency ───────────────────────────────────────────────────────


def test_apply_pending_idempotent(tmp_path):
    conn = _make_v2_db(str(tmp_path / "old.db"))
    v_a = migrations.apply_pending(conn, quiet=True)
    v_b = migrations.apply_pending(conn, quiet=True)

    assert v_a == v_b == SCHEMA_VERSION
    assert _has_table(conn, "session_sources")
    if SCHEMA_VERSION >= 4:
        assert _has_table(conn, "git_deleted_jsonls")


def test_apply_pending_second_run_is_silent(tmp_path, capsys):
    conn = _make_v2_db(str(tmp_path / "old.db"))
    migrations.apply_pending(conn, quiet=True)
    capsys.readouterr()  # drain

    migrations.apply_pending(conn, quiet=False)
    captured = capsys.readouterr()
    assert "migrated DB schema" not in captured.out


# ── Short-circuit: already at target ──────────────────────────────────


def test_apply_pending_skips_when_already_current(tmp_path):
    """A DB already at SCHEMA_VERSION shouldn't trigger any DDL."""
    conn = open_db(str(tmp_path / "current.db"))
    init_schema(conn, quiet=True)  # stamps SCHEMA_VERSION via fast path

    new_version = migrations.apply_pending(conn, quiet=True)
    assert new_version == SCHEMA_VERSION


# ── Migrated schema sanity ────────────────────────────────────────────


def test_session_sources_columns_after_migration(tmp_path):
    """Verify every expected column is present and FTS5-ready."""
    conn = _make_v2_db(str(tmp_path / "old.db"))
    migrations.apply_pending(conn, quiet=True)

    cols = {
        row["name"]: row["type"]
        for row in conn.execute("PRAGMA table_info(session_sources)").fetchall()
    }
    expected = {
        "source_id": "INTEGER",
        "session_id": "TEXT",
        "project": "TEXT",
        "source_type": "TEXT",
        "source_path": "TEXT",
        "size_bytes": "INTEGER",
        "mtime": "TEXT",
        "last_seen": "TEXT",
        "fts5_indexed_at": "TEXT",
        "content_hash": "TEXT",
    }
    for col, typ in expected.items():
        assert col in cols, f"missing column {col}"
        assert cols[col] == typ, f"{col}: expected {typ}, got {cols[col]}"


def test_session_sources_foreign_key_cascade(tmp_path):
    """ON DELETE CASCADE: removing a session row removes its source rows."""
    conn = _make_v2_db(str(tmp_path / "old.db"))
    migrations.apply_pending(conn, quiet=True)
    conn.execute("PRAGMA foreign_keys=ON")

    conn.execute(
        "INSERT INTO sessions (session_id, project) VALUES (?, ?)",
        ("sess-1", "proj"),
    )
    conn.execute(
        "INSERT INTO session_sources "
        "(session_id, project, source_type, source_path) "
        "VALUES (?, ?, ?, ?)",
        ("sess-1", "proj", "jsonl", "/path/to/x.jsonl"),
    )
    conn.commit()

    assert conn.execute(
        "SELECT COUNT(*) FROM session_sources WHERE session_id = 'sess-1'"
    ).fetchone()[0] == 1

    conn.execute("DELETE FROM sessions WHERE session_id = 'sess-1'")
    conn.commit()

    assert conn.execute(
        "SELECT COUNT(*) FROM session_sources WHERE session_id = 'sess-1'"
    ).fetchone()[0] == 0


def test_session_sources_unique_constraint(tmp_path):
    """(session_id, source_path) is UNIQUE."""
    conn = _make_v2_db(str(tmp_path / "old.db"))
    migrations.apply_pending(conn, quiet=True)

    conn.execute(
        "INSERT INTO sessions (session_id, project) VALUES (?, ?)",
        ("sess-1", "proj"),
    )
    conn.execute(
        "INSERT INTO session_sources "
        "(session_id, project, source_type, source_path) VALUES (?, ?, ?, ?)",
        ("sess-1", "proj", "jsonl", "/x.jsonl"),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO session_sources "
            "(session_id, project, source_type, source_path) VALUES (?, ?, ?, ?)",
            ("sess-1", "proj", "convo", "/x.jsonl"),
        )


# ── v3 -> v4 (v0.3.11): git_deleted_jsonls cache ──────────────────────

def test_v4_adds_git_deleted_jsonls(tmp_path):
    """v3 -> v4 migration creates the git_deleted_jsonls cache table."""
    conn = _make_v2_db(str(tmp_path / "old.db"))
    migrations.apply_pending(conn, quiet=True)
    assert _has_table(conn, "git_deleted_jsonls")


def test_v4_table_has_expected_columns(tmp_path):
    """Schema-locked: future migrations may add columns, but the v4 baseline
    set must be present (callers depend on these names)."""
    conn = _make_v2_db(str(tmp_path / "old.db"))
    migrations.apply_pending(conn, quiet=True)
    cols = {row["name"] for row in
            conn.execute("PRAGMA table_info(git_deleted_jsonls)").fetchall()}
    expected = {
        "jsonl_path", "session_id", "last_commit", "deleted_commit",
        "deleted_at", "last_seen_size", "last_seen_mtime",
        "extracted_metadata", "last_refreshed_at",
    }
    assert expected.issubset(cols), f"missing columns: {expected - cols}"


def test_v4_indexes_present(tmp_path):
    conn = _make_v2_db(str(tmp_path / "old.db"))
    migrations.apply_pending(conn, quiet=True)
    assert _has_index(conn, "idx_git_deleted_jsonls_session")
    assert _has_index(conn, "idx_git_deleted_jsonls_extracted")


def test_v4_primary_key_on_jsonl_path(tmp_path):
    """Duplicate jsonl_path rows must fail at INSERT (PK constraint)."""
    conn = _make_v2_db(str(tmp_path / "old.db"))
    migrations.apply_pending(conn, quiet=True)
    conn.execute(
        "INSERT INTO git_deleted_jsonls (jsonl_path, session_id) VALUES (?, ?)",
        ("projects/foo/u.jsonl", "uuid-1"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO git_deleted_jsonls (jsonl_path, session_id) VALUES (?, ?)",
            ("projects/foo/u.jsonl", "uuid-2"),
        )

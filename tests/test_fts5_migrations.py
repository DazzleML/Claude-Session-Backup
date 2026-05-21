"""Tests for the per-project FTS5 schema migration framework."""

import sqlite3
from pathlib import Path

import pytest

from claude_session_backup.fts5_db import CURRENT_FTS_SCHEMA_VERSION, open_fts5_db
from claude_session_backup.fts5_migrations import (
    MIGRATIONS,
    _get_current_version,
    _set_current_version,
    _v2_add_strength_to_file_ops,
    apply_pending,
)


# ── Registry shape ────────────────────────────────────────────────────


def test_registry_contains_expected_versions():
    """MIGRATIONS keys list every target version we know how to produce."""
    # v1 is the initial state, not a migration target. Every key here
    # must be a function we ship.
    assert 2 in MIGRATIONS
    assert MIGRATIONS[2] is _v2_add_strength_to_file_ops


def test_registry_max_matches_current_schema_version():
    """The top of the registry should always equal CURRENT_FTS_SCHEMA_VERSION.

    Mismatch here = someone added a migration without bumping the public
    constant, OR bumped the constant without writing the migration.
    """
    assert max(MIGRATIONS.keys()) == CURRENT_FTS_SCHEMA_VERSION


# ── Helpers ───────────────────────────────────────────────────────────


def _make_v1_db(tmp_path: Path) -> Path:
    """Construct a pre-v0.3.1 per-project DB on disk: no strength column,
    no fts_schema_version table.

    Mirrors what a real v0.3.0 build would have produced.
    """
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            uuid TEXT, message_index INTEGER NOT NULL,
            role TEXT NOT NULL, role_subtype TEXT,
            content TEXT NOT NULL, timestamp TEXT,
            UNIQUE (session_id, message_index)
        );
        CREATE TABLE file_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            message_index INTEGER,
            operation TEXT NOT NULL,
            file_path TEXT NOT NULL,
            timestamp TEXT,
            UNIQUE (session_id, message_index, operation, file_path)
        );
        INSERT INTO file_operations
            (session_id, message_index, operation, file_path)
        VALUES
            ('s1', 0, 'wrote',         '/a.py'),
            ('s1', 1, 'edited',        '/b.py'),
            ('s1', 2, 'read',          '/c.py'),
            ('s1', 3, 'searched',      '/d/'),
            ('s1', 4, 'notebook_edit', '/e.ipynb');
    """)
    conn.commit()
    conn.close()
    return db_path


# ── _get_current_version / _set_current_version ──────────────────────


def test_get_current_version_empty_table_returns_one(tmp_path):
    """A fresh fts_schema_version table (no rows) = "v0.3.0 layout" = v1."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE fts_schema_version (version INTEGER NOT NULL)")
    assert _get_current_version(conn) == 1


def test_get_current_version_reads_row(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE fts_schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO fts_schema_version (version) VALUES (5)")
    assert _get_current_version(conn) == 5


def test_set_current_version_overwrites(tmp_path):
    """DELETE-then-INSERT pattern keeps exactly one row regardless of
    how many migrations have run."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE fts_schema_version (version INTEGER NOT NULL)")
    _set_current_version(conn, 2)
    _set_current_version(conn, 3)
    _set_current_version(conn, 4)
    rows = conn.execute("SELECT version FROM fts_schema_version").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 4


# ── apply_pending behavior ───────────────────────────────────────────


def test_apply_pending_runs_migration_on_v1_db(tmp_path):
    """End-to-end: a v1 DB gets migrated to CURRENT_FTS_SCHEMA_VERSION."""
    db_path = _make_v1_db(tmp_path)
    conn = open_fts5_db(db_path)  # triggers init_fts5_schema -> apply_pending

    # Version stamped
    version = conn.execute(
        "SELECT version FROM fts_schema_version"
    ).fetchone()[0]
    assert version == CURRENT_FTS_SCHEMA_VERSION

    # Strength column present + backfilled by operation kind
    rows = conn.execute(
        "SELECT operation, strength FROM file_operations ORDER BY message_index"
    ).fetchall()
    assert [(r["operation"], r["strength"]) for r in rows] == [
        ("wrote",         3),
        ("edited",        3),
        ("read",          2),
        ("searched",      1),
        ("notebook_edit", 3),
    ]


def test_apply_pending_noop_when_already_current(tmp_path):
    """Re-running apply_pending on a freshly-migrated DB doesn't repeat work."""
    db_path = _make_v1_db(tmp_path)
    conn = open_fts5_db(db_path)  # first migration

    # Spy: capture row count BEFORE second apply, then after
    before = conn.execute(
        "SELECT COUNT(*) FROM file_operations"
    ).fetchone()[0]
    new_version = apply_pending(conn)
    after = conn.execute(
        "SELECT COUNT(*) FROM file_operations"
    ).fetchone()[0]

    assert new_version == CURRENT_FTS_SCHEMA_VERSION
    assert before == after  # no rows added / removed by re-run


def test_apply_pending_returns_current_version_when_no_pending(tmp_path):
    """Return value matches current stamped version when there's nothing
    to do."""
    conn = open_fts5_db(tmp_path / "fresh.db")  # creates at CURRENT
    result = apply_pending(conn)
    assert result == CURRENT_FTS_SCHEMA_VERSION


# ── v2 migration body ────────────────────────────────────────────────


def test_v2_migration_idempotent_on_already_migrated_schema(tmp_path):
    """Running _v2_add_strength_to_file_ops twice on the same DB doesn't
    error and produces the same end state.

    This is what protects us from partial-migration crashes: if the
    ALTER succeeded but the UPDATE failed, the next open re-runs the
    migration body and reaches a correct end state.
    """
    db_path = _make_v1_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE fts_schema_version (version INTEGER NOT NULL)"
    )

    _v2_add_strength_to_file_ops(conn)
    _v2_add_strength_to_file_ops(conn)  # second run -- must not error

    rows = conn.execute(
        "SELECT operation, strength FROM file_operations ORDER BY message_index"
    ).fetchall()
    assert [(r["operation"], r["strength"]) for r in rows] == [
        ("wrote",         3),
        ("edited",        3),
        ("read",          2),
        ("searched",      1),
        ("notebook_edit", 3),
    ]


def test_v2_migration_self_heals_after_partial_alter(tmp_path):
    """Simulate the failure mode: ALTER TABLE succeeded (column exists)
    but UPDATE was interrupted (strength values still all DEFAULT 2).
    Re-running the migration restores correct strength values.
    """
    db_path = _make_v1_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE fts_schema_version (version INTEGER NOT NULL)"
    )

    # Simulate the partial state: ALTER ran, UPDATE didn't.
    conn.execute(
        "ALTER TABLE file_operations "
        "ADD COLUMN strength INTEGER NOT NULL DEFAULT 2"
    )
    # All rows now have strength=2, including 'wrote' / 'edited' /
    # 'notebook_edit' / 'searched' that should be 3 / 3 / 3 / 1.
    pre_repair = conn.execute(
        "SELECT operation, strength FROM file_operations ORDER BY message_index"
    ).fetchall()
    assert all(r["strength"] == 2 for r in pre_repair)

    # Re-running the migration body fixes the values.
    _v2_add_strength_to_file_ops(conn)
    post_repair = conn.execute(
        "SELECT operation, strength FROM file_operations ORDER BY message_index"
    ).fetchall()
    assert [(r["operation"], r["strength"]) for r in post_repair] == [
        ("wrote",         3),
        ("edited",        3),
        ("read",          2),
        ("searched",      1),
        ("notebook_edit", 3),
    ]


# ── User-visible auto-upgrade notice (v0.3.2) ────────────────────────


def test_apply_pending_prints_notice_per_migration(tmp_path, capsys):
    """Default (non-quiet) behavior prints one line per migration applied.

    Matches the user-visible audit-trail style the main DB uses via
    ``migrations.apply_pending``.
    """
    db_path = _make_v1_db(tmp_path)
    open_fts5_db(db_path)  # triggers init_fts5_schema -> apply_pending

    captured = capsys.readouterr()
    # Exactly one line per applied migration (currently just v2).
    assert "csb: per-project FTS5 schema migrated to v2" in captured.out


def test_apply_pending_silent_when_quiet(tmp_path, capsys):
    """quiet=True suppresses the auto-upgrade notice for callers that
    print their own progress (e.g. csb build-fts5 --quiet)."""
    db_path = _make_v1_db(tmp_path)
    open_fts5_db(db_path, quiet=True)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_apply_pending_no_notice_when_nothing_to_do(tmp_path, capsys):
    """Opening an already-current DB prints nothing -- no migration
    happened, no audit-trail noise."""
    db_path = _make_v1_db(tmp_path)
    open_fts5_db(db_path)  # first open migrates and prints
    capsys.readouterr()    # drain
    open_fts5_db(db_path)  # second open is a no-op
    captured = capsys.readouterr()
    assert captured.out == ""


def test_v2_migration_on_fresh_db_is_noop_for_alter(tmp_path):
    """Fresh DBs already have the strength column from _SCHEMA_SQL.
    Migration body should skip ALTER and just confirm backfill (which
    is also a no-op since the column was created with the right values
    written from the start via import_jsonl_to_db).
    """
    conn = open_fts5_db(tmp_path / "fresh.db")
    # No errors raised; rerun is safe.
    _v2_add_strength_to_file_ops(conn)
    # Column still exists, single fts_schema_version row.
    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(file_operations)"
    ).fetchall()]
    assert "strength" in cols
    n = conn.execute("SELECT COUNT(*) FROM fts_schema_version").fetchone()[0]
    assert n == 1

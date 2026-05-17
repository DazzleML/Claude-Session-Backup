"""
Schema migration framework.

Migrations are keyed by the *target* version they produce. Running
`apply_pending(conn)` reads the current ``schema_info.schema_version`` and
runs every migration whose target is higher, in ascending order, updating
``schema_info.schema_version`` after each one.

The framework is intentionally minimal:

- Each migration is a function ``def _vN_xxx(conn): ...`` that applies its
  DDL/DML directly. No down-migrations -- forward only.
- ``apply_pending`` is idempotent. Running it twice is a no-op the second
  time (current version >= target).
- Migrations run inside the caller's transaction context. ``apply_pending``
  commits after each migration so a partial failure leaves the DB at a
  well-defined intermediate version.

Hooked into ``init_schema()`` so every csb command path that opens the DB
auto-applies pending migrations on first use after an upgrade.
"""

import sqlite3
from typing import Callable


def _v3_add_session_sources(conn: sqlite3.Connection) -> None:
    """Add the session_sources table (Phase 1 of the transcript-search work).

    Records one row per searchable transcript file per session. FTS5-ready
    columns (``fts5_indexed_at``, ``content_hash``) are present from day one
    so the Phase 2 FTS5 work needs no schema migration.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS session_sources (
            source_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            project TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_path TEXT NOT NULL,
            size_bytes INTEGER,
            mtime TEXT,
            last_seen TEXT,
            fts5_indexed_at TEXT,
            content_hash TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
            UNIQUE (session_id, source_path)
        );

        CREATE INDEX IF NOT EXISTS idx_session_sources_session
            ON session_sources(session_id);
        CREATE INDEX IF NOT EXISTS idx_session_sources_project
            ON session_sources(project);
        CREATE INDEX IF NOT EXISTS idx_session_sources_fts5
            ON session_sources(fts5_indexed_at);
        """
    )


# Target version -> migration function.
# When adding a new migration, bump SCHEMA_VERSION in index.py to match.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    3: _v3_add_session_sources,
}


def _get_current_version(conn: sqlite3.Connection) -> int:
    """Read schema_info.schema_version. Defaults to 0 if the row is missing."""
    row = conn.execute(
        "SELECT value FROM schema_info WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0] if not isinstance(row, sqlite3.Row) else row["value"])
    except (TypeError, ValueError):
        return 0


def _set_current_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_info (key, value) VALUES ('schema_version', ?)",
        (str(version),),
    )


def apply_pending(conn: sqlite3.Connection, quiet: bool = False) -> int:
    """Apply every migration whose target is newer than the current version.

    Returns the new schema_version after migrations complete. If no
    migrations are pending, returns the existing version unchanged.

    Prints a one-line notice per migration applied unless ``quiet`` is True.
    """
    if not MIGRATIONS:
        return _get_current_version(conn)

    current = _get_current_version(conn)
    target = max(MIGRATIONS.keys())
    if current >= target:
        return current

    for version in sorted(MIGRATIONS.keys()):
        if version <= current:
            continue
        MIGRATIONS[version](conn)
        _set_current_version(conn, version)
        conn.commit()
        if not quiet:
            print(f"csb: migrated DB schema to v{version}")
        current = version

    return current

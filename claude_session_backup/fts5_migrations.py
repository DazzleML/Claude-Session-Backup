"""
Per-project FTS5 database schema migrations.

Parallel to :mod:`migrations` (which handles the main session-backup DB)
but operates against the per-project FTS5 DBs at
``~/.claude/csb-fts/<project>__<slug-hash>_<USER>.db``. Same registry
convention: a ``MIGRATIONS`` dict keyed by target version maps to the
function that produces that version from the previous one.

Migrations are forward-only. ``apply_pending(conn)`` reads the current
``fts_schema_version`` row, runs every migration whose target is higher,
in ascending order, and stamps the new version after each step. The
function is idempotent at every intermediate state -- a crash mid-migration
is recoverable on the next open (each migration body checks its own
preconditions before doing destructive work).

Hooked into :func:`fts5_db.init_fts5_schema` so every csb command path
that opens a per-project DB auto-applies pending migrations on first use
after an upgrade.

History:

- v1 (v0.3.0): initial schema (messages / messages_fts / file_operations
  / indexed_sessions). No ``fts_schema_version`` table -- absence of the
  table is how v0.3.1+ detects a v1 DB.
- v2 (v0.3.1): ``file_operations.strength`` column added with strength
  weights backfilled from operation kind. ``fts_schema_version`` table
  introduced.
"""

from __future__ import annotations

import sqlite3
from typing import Callable


# ── Individual migration steps ───────────────────────────────────────


def _v2_add_strength_to_file_ops(conn: sqlite3.Connection) -> None:
    """v0.3.1: add ``file_operations.strength`` INTEGER column with
    backfilled values from the operation kind.

    The CREATE TABLE in :data:`fts5_db._SCHEMA_SQL` already declares the
    strength column for FRESH DBs, so on a fresh open this step finds
    the column already present and skips the ALTER. For a v1 DB (built
    under v0.3.0) the column doesn't exist yet; we add it and backfill.

    Idempotent: re-running the body on an already-migrated DB is a no-op
    because the column-exists check skips ALTER and the UPDATE WHERE
    clauses match the same rows the same way.
    """
    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(file_operations)"
    ).fetchall()]
    if "strength" not in cols:
        conn.execute(
            "ALTER TABLE file_operations "
            "ADD COLUMN strength INTEGER NOT NULL DEFAULT 2"
        )
    # Backfill: default is 2 (correct for 'read'); promote active ops to
    # 3 and search probes to 1. Safe to re-run -- the WHERE conditions
    # match the same rows whether or not they've been set before.
    conn.execute(
        "UPDATE file_operations SET strength = 3 "
        "WHERE operation IN ('wrote', 'edited', 'notebook_edit')"
    )
    conn.execute(
        "UPDATE file_operations SET strength = 1 "
        "WHERE operation = 'searched'"
    )


# ── Registry ─────────────────────────────────────────────────────────


# Target version -> migration function. When adding a new migration:
#   1. Write `_vN_xxx(conn)` above.
#   2. Add `N: _vN_xxx` to MIGRATIONS below.
#   3. Update CURRENT_FTS_SCHEMA_VERSION in fts5_db.py to match.
#   4. Add a test under `tests/test_fts5_migrations.py` covering the
#      v(N-1) -> vN path.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _v2_add_strength_to_file_ops,
}


# ── Public API ───────────────────────────────────────────────────────


def _get_current_version(conn: sqlite3.Connection) -> int:
    """Read the recorded schema version, defaulting to 1 if the
    ``fts_schema_version`` table is empty.

    Absence of any row signals "this DB was created by v0.3.0" (v1
    layout), since v0.3.0 never populated that table. The table itself
    must exist -- it's created by the CREATE TABLE IF NOT EXISTS in
    :data:`fts5_db._SCHEMA_SQL` before any migration code runs.
    """
    row = conn.execute(
        "SELECT version FROM fts_schema_version LIMIT 1"
    ).fetchone()
    if row is None:
        return 1
    return int(row[0] if not isinstance(row, sqlite3.Row) else row["version"])


def _set_current_version(conn: sqlite3.Connection, version: int) -> None:
    """Stamp the per-project DB at the given version.

    We DELETE before INSERT (rather than UPSERT) so the table holds
    exactly one row regardless of how many migrations have run.
    """
    conn.execute("DELETE FROM fts_schema_version")
    conn.execute(
        "INSERT INTO fts_schema_version (version) VALUES (?)",
        (version,),
    )


def apply_pending(
    conn: sqlite3.Connection,
    quiet: bool = False,
) -> int:
    """Apply every migration whose target is newer than the current version.

    Returns the version after migrations complete. If no migrations are
    pending, returns the current version unchanged (no-op, no output).

    Prints a one-line notice per migration applied unless ``quiet`` is
    True. Mirrors the convention used by :func:`migrations.apply_pending`
    for the main DB so users get the same audit-trail style of feedback
    when csb auto-upgrades their per-project DBs on first use after an
    upgrade.

    Commits after each migration so a partial failure leaves the DB at
    a well-defined intermediate version -- the next open will resume
    from there (each migration body checks preconditions before doing
    destructive work, so partial-application self-heals).
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
            print(f"csb: per-project FTS5 schema migrated to v{version}")
        current = version

    return current

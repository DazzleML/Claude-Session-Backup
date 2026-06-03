"""
SQLite index -- rebuildable metadata cache for fast queries.

This is NOT the source of truth. The git repository is.
If this database is lost, `csb update rebuild-index` reconstructs it.
"""

import sqlite3
from pathlib import Path
from typing import Optional

from .metadata import SessionMetadata

SCHEMA_VERSION = 5

SCHEMA_SQL = """
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
    last_git_commit TEXT,
    metadata_validated_at TEXT
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

CREATE TABLE IF NOT EXISTS git_deleted_jsonls (
    jsonl_path TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    last_commit TEXT,
    deleted_commit TEXT,
    deleted_at TEXT,
    last_seen_size INTEGER,
    last_seen_mtime REAL,
    extracted_metadata INTEGER NOT NULL DEFAULT 0,
    last_refreshed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_last_active ON sessions(last_active_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_sessions_deleted ON sessions(deleted_at);
CREATE INDEX IF NOT EXISTS idx_session_sources_session ON session_sources(session_id);
CREATE INDEX IF NOT EXISTS idx_session_sources_project ON session_sources(project);
CREATE INDEX IF NOT EXISTS idx_session_sources_fts5 ON session_sources(fts5_indexed_at);
CREATE INDEX IF NOT EXISTS idx_git_deleted_jsonls_session ON git_deleted_jsonls(session_id);
CREATE INDEX IF NOT EXISTS idx_git_deleted_jsonls_extracted ON git_deleted_jsonls(extracted_metadata);
"""


def open_db(db_path: str) -> sqlite3.Connection:
    """Open (or create) the SQLite index database."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(conn: sqlite3.Connection, quiet: bool = False):
    """Initialize database schema if not already present.

    Runs the baseline DDL (idempotent via ``CREATE TABLE IF NOT EXISTS``),
    then applies any pending migrations registered in
    :mod:`claude_session_backup.migrations`. Fresh databases get fast-tracked
    to ``SCHEMA_VERSION`` without running migrations (the baseline DDL
    already covers their tables); existing databases at an older version
    get migrated incrementally.
    """
    from . import migrations

    conn.executescript(SCHEMA_SQL)

    # Fast path for fresh DBs: if schema_version isn't set yet, the baseline
    # DDL above already created every current table -- skip migrations and
    # stamp the current version directly. This avoids running migrations
    # whose DDL would race against the baseline CREATE TABLE IF NOT EXISTS.
    existing = conn.execute(
        "SELECT value FROM schema_info WHERE key = 'schema_version'"
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.commit()
        return

    # Existing DB -- run migrations from current version up to SCHEMA_VERSION.
    migrations.apply_pending(conn, quiet=quiet)


def upsert_session(conn: sqlite3.Connection, meta: SessionMetadata,
                   jsonl_path: str = "", jsonl_size: int = 0,
                   jsonl_mtime: float = 0.0, scanned_at: str = ""):
    """Insert or update a session in the index.

    Every successful upsert sets ``metadata_validated_at = scanned_at``
    because we just re-extracted metadata from source-of-truth (a live
    JSONL or a historical git blob). The display surfaces this as
    ``val: YY-MM-DD`` so users can tell verified-recent rows from stale.
    """
    conn.execute("""
        INSERT INTO sessions (
            session_id, project, session_name, start_folder,
            started_at, last_active_at, last_user_at, message_count, tool_call_count,
            claude_version, jsonl_path, jsonl_size, jsonl_mtime, last_scanned_at, deleted_at,
            metadata_validated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            session_name = COALESCE(excluded.session_name, sessions.session_name),
            start_folder = COALESCE(excluded.start_folder, sessions.start_folder),
            last_active_at = COALESCE(excluded.last_active_at, sessions.last_active_at),
            last_user_at = COALESCE(excluded.last_user_at, sessions.last_user_at),
            message_count = excluded.message_count,
            tool_call_count = excluded.tool_call_count,
            claude_version = COALESCE(excluded.claude_version, sessions.claude_version),
            jsonl_path = excluded.jsonl_path,
            jsonl_size = excluded.jsonl_size,
            jsonl_mtime = excluded.jsonl_mtime,
            last_scanned_at = excluded.last_scanned_at,
            deleted_at = NULL,
            metadata_validated_at = excluded.metadata_validated_at
    """, (
        meta.session_id, meta.project, meta.session_name, meta.start_folder,
        meta.started_at, meta.last_active_at, meta.last_user_at,
        meta.message_count, meta.tool_call_count,
        meta.claude_version, jsonl_path, jsonl_size, jsonl_mtime, scanned_at,
        scanned_at,  # metadata_validated_at == scanned_at: we just re-extracted
    ))

    # Update folder usage
    conn.execute("DELETE FROM folder_usage WHERE session_id = ?", (meta.session_id,))
    for folder_path, count in meta.folder_usage.items():
        is_start = 1 if folder_path == meta.start_folder else 0
        conn.execute(
            "INSERT INTO folder_usage (session_id, folder_path, usage_count, is_start_folder) "
            "VALUES (?, ?, ?, ?)",
            (meta.session_id, folder_path, count, is_start),
        )

    conn.commit()


def register_session_sources(
    conn: sqlite3.Connection,
    session_id: str,
    project: str,
    sources,
    scanned_at: str,
) -> tuple[int, int]:
    """Replace all ``session_sources`` rows for one session.

    Mirrors :func:`upsert_session`'s handling of ``folder_usage``: delete
    every existing row for the session, then insert the fresh set. This is
    transactional within the caller's connection and idempotent across
    repeated ``csb backup`` invocations.

    ``sources`` is an iterable of :class:`sesslog_scanner.SourceRow`
    instances (or any object/dict exposing ``source_type``, ``source_path``,
    ``size_bytes``, and ``mtime`` attributes/keys).

    The FTS5-readiness columns (``fts5_indexed_at``, ``content_hash``) are
    deliberately left NULL on insert -- Phase 2's FTS5 indexer is what
    eventually populates them.

    Returns ``(added_rows, removed_rows)`` for caller-side logging.
    """
    # How many rows did we have before?
    removed = conn.execute(
        "SELECT COUNT(*) FROM session_sources WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]

    conn.execute(
        "DELETE FROM session_sources WHERE session_id = ?",
        (session_id,),
    )

    added = 0
    for src in sources:
        # Support both dataclass-like attribute access and dict-style access.
        if hasattr(src, "source_type"):
            source_type = src.source_type
            source_path = src.source_path
            size_bytes = src.size_bytes
            mtime = src.mtime
        else:
            source_type = src["source_type"]
            source_path = src["source_path"]
            size_bytes = src.get("size_bytes")
            mtime = src.get("mtime")

        conn.execute(
            """
            INSERT INTO session_sources
                (session_id, project, source_type, source_path,
                 size_bytes, mtime, last_seen,
                 fts5_indexed_at, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (session_id, project, source_type, source_path,
             size_bytes, mtime, scanned_at),
        )
        added += 1

    conn.commit()
    return added, removed


def mark_deleted(conn: sqlite3.Connection, session_id: str, deleted_at: str):
    """Mark a session as deleted (no longer present on disk)."""
    conn.execute(
        "UPDATE sessions SET deleted_at = ? WHERE session_id = ? AND deleted_at IS NULL",
        (deleted_at, session_id),
    )
    conn.commit()


def snapshot_deleted_sessions(conn: sqlite3.Connection) -> list[dict]:
    """Return every deleted-session row plus its folder_usage rows.

    Used by ``cmd_rebuild_index`` to preserve deleted-session knowledge
    across a destructive rebuild: snapshot before the wipe, re-insert
    afterwards for any UUIDs the rebuild's live-FS scan didn't repopulate.

    Each output dict has the sessions-row columns AND a ``_folders`` key
    holding the matching folder_usage rows as dicts. The ``_folders`` key
    is consumed by ``restore_deleted_snapshot``; ignore it elsewhere.
    """
    rows = conn.execute(
        "SELECT * FROM sessions WHERE deleted_at IS NOT NULL"
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        folders = conn.execute(
            "SELECT folder_path, usage_count, is_start_folder "
            "FROM folder_usage WHERE session_id = ?",
            (r["session_id"],),
        ).fetchall()
        d["_folders"] = [dict(f) for f in folders]
        out.append(d)
    return out


def restore_deleted_snapshot(conn: sqlite3.Connection,
                              snapshot: list[dict]) -> int:
    """Re-insert snapshot rows for UUIDs not already in the live DB.

    Called by ``cmd_rebuild_index`` after the live-FS rescan. Sessions
    the rescan found (because their JSONL is on disk) are left alone --
    only genuinely-missing UUIDs from the snapshot get re-inserted. The
    INSERT uses the live schema's column list, so legacy snapshot rows
    with stale columns drop them gracefully.

    Returns count of rows actually re-inserted (excludes
    already-present skips).
    """
    if not snapshot:
        return 0

    # Probe live schema for column whitelist (handles schema drift across
    # csb versions: snapshot from old schema -> new schema may have added
    # or removed columns; only keep the intersection).
    live_cols = {row["name"] for row in
                 conn.execute("PRAGMA table_info(sessions)").fetchall()}

    restored = 0
    for d in snapshot:
        sid = d["session_id"]
        existing = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        if existing:
            continue

        folders = d.pop("_folders", [])

        # Build INSERT from columns the snapshot AND live schema both have
        cols = [k for k in d.keys()
                if not k.startswith("_") and k in live_cols]
        placeholders = ", ".join("?" for _ in cols)
        col_names = ", ".join(cols)
        conn.execute(
            f"INSERT INTO sessions ({col_names}) VALUES ({placeholders})",
            tuple(d[k] for k in cols),
        )

        for f in folders:
            conn.execute(
                "INSERT INTO folder_usage "
                "(session_id, folder_path, usage_count, is_start_folder) "
                "VALUES (?, ?, ?, ?)",
                (sid, f["folder_path"], f["usage_count"], f["is_start_folder"]),
            )
        restored += 1

    conn.commit()
    return restored


# Whitelist of allowed ORDER BY clauses for list_sessions().
# Keys MUST match the argparse choices in cli.py for `csb list --sort`.
# Values are fixed SQL fragments -- never interpolate user input here.
SORT_SQL = {
    "last-used":  "s.last_active_at DESC NULLS LAST",
    # NULLIF converts 0 (never-scanned sentinel) to NULL; NULLS LAST pushes
    # those rows to the bottom so real expiring sessions appear first.
    "expiration": "NULLIF(s.jsonl_mtime, 0) ASC NULLS LAST",
    "started":    "s.started_at DESC NULLS LAST",
    "oldest":     "s.started_at ASC NULLS LAST",
    "messages":   "s.message_count DESC",
    "size":       "s.jsonl_size DESC",
}


def list_sessions(conn: sqlite3.Connection, limit: int = 20,
                  show_deleted: bool = False, show_all: bool = False,
                  filter_keyword: str = None,
                  sort_key: str = "last-used") -> list[dict]:
    """
    List sessions with configurable ordering.

    sort_key must be a key in SORT_SQL (defaults to "last-used", which
    preserves historical behavior). Invalid keys raise ValueError; callers
    should rely on argparse `choices=` to prevent this in practice.

    If filter_keyword is provided, only return sessions where the keyword
    appears (case-insensitive) in session_name, project, start_folder,
    or any tracked folder_path. This filters on metadata, not rendered output.
    """
    params = []

    # Build WHERE conditions
    conditions = []
    if not show_all:
        if show_deleted:
            conditions.append("s.deleted_at IS NOT NULL")
        else:
            conditions.append("s.deleted_at IS NULL")

    if filter_keyword:
        # Match against session metadata fields OR any folder_usage path
        pattern = f"%{filter_keyword}%"
        conditions.append("""(
            s.session_name LIKE ? COLLATE NOCASE
            OR s.project LIKE ? COLLATE NOCASE
            OR s.start_folder LIKE ? COLLATE NOCASE
            OR s.session_id IN (
                SELECT fu.session_id FROM folder_usage fu
                WHERE fu.folder_path LIKE ? COLLATE NOCASE
            )
        )""")
        params.extend([pattern, pattern, pattern, pattern])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    try:
        order_by = SORT_SQL[sort_key]
    except KeyError:
        raise ValueError(
            f"Unknown sort_key {sort_key!r}; expected one of {sorted(SORT_SQL)}"
        )

    rows = conn.execute(f"""
        SELECT s.* FROM sessions s
        {where}
        ORDER BY {order_by}
        LIMIT ?
    """, params).fetchall()

    results = []
    for row in rows:
        session = dict(row)
        # Attach folder usage
        folders = conn.execute(
            "SELECT folder_path, usage_count, is_start_folder FROM folder_usage "
            "WHERE session_id = ? ORDER BY usage_count DESC",
            (session["session_id"],),
        ).fetchall()
        session["folders"] = [dict(f) for f in folders]
        results.append(session)

    return results


def count_deleted_with_filter(
    conn: sqlite3.Connection, filter_keyword: str | None = None
) -> int:
    """
    Count sessions matching ``filter_keyword`` (or all sessions) that are
    flagged ``deleted_at IS NOT NULL``.

    Reuses ``list_sessions``'s exact WHERE-predicate construction so the
    "N deleted hidden" footer in ``csb list`` reflects the same scope as
    the filter the user just typed.

    Args:
        conn: open SQLite connection.
        filter_keyword: if provided, count only deleted sessions whose
            session_name / project / start_folder / any folder_usage path
            contains the keyword (case-insensitive). If None, count all
            deleted sessions.

    Returns:
        Integer count. Zero when there are no matching deleted sessions
        (or the filter is too narrow to match any).
    """
    conditions = ["s.deleted_at IS NOT NULL"]
    params: list = []
    if filter_keyword:
        pattern = f"%{filter_keyword}%"
        conditions.append("""(
            s.session_name LIKE ? COLLATE NOCASE
            OR s.project LIKE ? COLLATE NOCASE
            OR s.start_folder LIKE ? COLLATE NOCASE
            OR s.session_id IN (
                SELECT fu.session_id FROM folder_usage fu
                WHERE fu.folder_path LIKE ? COLLATE NOCASE
            )
        )""")
        params.extend([pattern, pattern, pattern, pattern])

    where = " AND ".join(conditions)
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM sessions s WHERE {where}",
        params,
    ).fetchone()
    return int(row["c"] if row else 0)


def get_session(conn: sqlite3.Connection, session_id_prefix: str) -> Optional[dict]:
    """Get a session by ID prefix match."""
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id LIKE ?",
        (session_id_prefix + "%",),
    ).fetchone()

    if not row:
        return None

    session = dict(row)
    folders = conn.execute(
        "SELECT folder_path, usage_count, is_start_folder FROM folder_usage "
        "WHERE session_id = ? ORDER BY usage_count DESC",
        (session["session_id"],),
    ).fetchall()
    session["folders"] = [dict(f) for f in folders]
    return session


def get_indexed_mtime(conn: sqlite3.Connection, session_id: str) -> Optional[float]:
    """Return the JSONL mtime recorded for ``session_id`` at the last backup
    scan, or ``None`` if the session is not in the index.

    Exact-match (not prefix) -- callers pass a full session UUID. Used by
    ``csb check`` to decide whether a session has un-backed-up changes: a live
    JSONL mtime newer than this recorded value means the session changed since
    the last backup. A ``0`` recorded mtime (the column default) is returned
    as-is so the caller can treat it as "never really scanned".
    """
    row = conn.execute(
        "SELECT jsonl_mtime FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return row["jsonl_mtime"]


def escape_like_value(s: str, escape_char: str = "|") -> str:
    """
    Escape ``%`` / ``_`` / ``escape_char`` in a string for use as a LIKE pattern.

    Use together with ``ESCAPE '|'`` (or whichever escape_char) in the SQL.
    Without this, a user-supplied path containing ``_`` or ``%`` would be
    interpreted as a SQL LIKE wildcard. Backslashes (Windows path separators)
    are not special in LIKE without ESCAPE, so they pass through unmodified.

    Examples:
        escape_like_value("C:\\code\\my_folder") == "C:\\code\\my|_folder"
        escape_like_value("C:\\code\\50%-share") == "C:\\code\\50|%-share"
    """
    return (
        s.replace(escape_char, escape_char * 2)
        .replace("%", escape_char + "%")
        .replace("_", escape_char + "_")
    )


def _deleted_filter_clause(deleted_filter: str) -> str:
    """
    Build the WHERE fragment that restricts (or doesn't) on ``s.deleted_at``.

    Returns a SQL fragment that begins with ``s.deleted_at IS [NOT] NULL`` (no
    leading ``AND``) or the literal ``"1=1"`` for "no filter". Callers wrap it
    in their WHERE compositions as appropriate. Centralized so the three scan
    paths stay in sync.

    Allowed values:
      - ``"active"``  -> ``s.deleted_at IS NULL``  (default, preserves pre-#27 behavior)
      - ``"deleted"`` -> ``s.deleted_at IS NOT NULL``
      - ``"all"``     -> ``1=1`` (no restriction)
    """
    if deleted_filter == "active":
        return "s.deleted_at IS NULL"
    if deleted_filter == "deleted":
        return "s.deleted_at IS NOT NULL"
    if deleted_filter == "all":
        return "1=1"
    raise ValueError(
        f"unknown deleted_filter {deleted_filter!r}; expected 'active', 'deleted', or 'all'"
    )


def find_sessions_by_term(
    conn: sqlite3.Connection,
    term: str,
    top_n: int | None = None,
    limit: int = 50,
    *,
    deleted_filter: str = "active",
) -> list[dict]:
    """
    Broad metadata substring search across name, project, start_folder, folder_usage.

    Mirrors ``list_sessions(filter_keyword=...)`` but applies top-N gating
    to the folder_usage substring match (to keep scan results coherent
    with what the renderer displays).

    A session matches if any of these case-insensitively contain ``term``:
      - ``s.session_name``, ``s.project``, ``s.start_folder`` (always eligible)
      - One of its top-N folder_usage entries (gated when ``top_n`` is set)

    Args:
        conn: open SQLite connection.
        term: substring to search for.
        top_n: top-N gate for folder_usage matching, or None for no gate.
        limit: max sessions to return.
        deleted_filter: ``"active"`` (default; hide deleted), ``"deleted"`` (only
            deleted), or ``"all"`` (both). Mirrors the ``--deleted`` / ``--all``
            flags on ``csb scan``.
    """
    pattern = f"%{escape_like_value(term)}%"

    rnk_filter = ""
    rnk_params: list = []
    if top_n is not None:
        rnk_filter = "AND r.rnk <= ?"
        rnk_params = [top_n]

    del_clause = _deleted_filter_clause(deleted_filter)

    query = f"""
        WITH ranked AS (
            SELECT session_id, folder_path,
                   ROW_NUMBER() OVER (
                       PARTITION BY session_id
                       ORDER BY usage_count DESC, folder_path
                   ) AS rnk
            FROM folder_usage
        )
        SELECT DISTINCT s.* FROM sessions s
        WHERE {del_clause}
          AND (
            s.session_name LIKE ? ESCAPE '|' COLLATE NOCASE
            OR s.project LIKE ? ESCAPE '|' COLLATE NOCASE
            OR s.start_folder LIKE ? ESCAPE '|' COLLATE NOCASE
            OR EXISTS (
                SELECT 1 FROM ranked r
                WHERE r.session_id = s.session_id
                  {rnk_filter}
                  AND r.folder_path LIKE ? ESCAPE '|' COLLATE NOCASE
            )
          )
        ORDER BY s.last_active_at DESC
        LIMIT ?
    """
    params = [pattern, pattern, pattern] + rnk_params + [pattern, limit]
    rows = conn.execute(query, params).fetchall()

    results = []
    for row in rows:
        session = dict(row)
        folders = conn.execute(
            "SELECT folder_path, usage_count, is_start_folder FROM folder_usage "
            "WHERE session_id = ? ORDER BY usage_count DESC",
            (session["session_id"],),
        ).fetchall()
        session["folders"] = [dict(f) for f in folders]
        results.append(session)

    return results


def find_sessions_by_directory(
    conn: sqlite3.Connection,
    exact_value: str | None,
    like_match: str | None,
    like_exclude: str | None,
    top_n: int | None,
    *,
    start_folder_only: bool = False,
    limit: int = 50,
    deleted_filter: str = "active",
) -> list[dict]:
    """
    Find sessions whose start_folder OR top-N folder_usage paths match.

    A session matches if either:
      - ``s.start_folder`` matches the criteria (always eligible -- special slot), OR
      - One of its top-N folder_usage entries (ranked by usage_count desc)
        matches the criteria.

    When ``start_folder_only=True``, the folder_usage check is skipped
    entirely -- only ``s.start_folder`` is consulted. Useful for "what
    sessions originated here?" queries (the ``-s`` flag).

    Match criteria (caller pre-builds these):
      - ``exact_value``: path-equality match (e.g., ``"C:\\code\\amdead"``).
        Skipped when ``None``.
      - ``like_match``: SQL LIKE pattern (e.g., ``"C:\\code\\amdead\\%"`` for
        descendants, or ``"C:\\code\\amdead%"`` for wildcard prefix). Skipped
        when ``None``. Caller must pre-escape user-supplied ``%`` / ``_`` via
        ``escape_like_value`` -- the helper applies ``ESCAPE '|'``.
      - ``like_exclude``: SQL NOT LIKE pattern (e.g., for ``-D <pattern>*``,
        excludes descendants). Skipped when ``None``.

    Top-N gating:
      - ``top_n=None`` -> no gate (every folder_usage entry is eligible).
      - ``top_n=N`` -> only the top N most-used folder_usage entries per
        session are eligible. start_folder is always eligible regardless.
      - Has no effect when ``start_folder_only=True``.

    Args:
        conn: open SQLite connection.
        exact_value: literal-equality path, or None.
        like_match: LIKE pattern, or None.
        like_exclude: NOT LIKE pattern, or None.
        top_n: top-N gate for folder_usage matching, or None for no gate.
        start_folder_only: when True, skip folder_usage match entirely.
        limit: max sessions to return.

    Raises:
        ValueError: if both exact_value and like_match are None (no match
            criteria; callers must specify at least one).
    """
    if exact_value is None and like_match is None:
        raise ValueError(
            "find_sessions_by_directory requires at least one of "
            "exact_value or like_match"
        )

    def _build_match(field: str) -> tuple[str, list]:
        parts: list[str] = []
        params: list = []
        if exact_value is not None:
            parts.append(f"{field} = ? COLLATE NOCASE")
            params.append(exact_value)
        if like_match is not None:
            if like_exclude is not None:
                parts.append(
                    f"({field} LIKE ? ESCAPE '|' COLLATE NOCASE "
                    f"AND {field} NOT LIKE ? ESCAPE '|' COLLATE NOCASE)"
                )
                params.extend([like_match, like_exclude])
            else:
                parts.append(f"{field} LIKE ? ESCAPE '|' COLLATE NOCASE")
                params.append(like_match)
        return "(" + " OR ".join(parts) + ")", params

    start_clause, start_params = _build_match("s.start_folder")
    del_clause = _deleted_filter_clause(deleted_filter)

    if start_folder_only:
        # Skip folder_usage entirely -- match against start_folder only.
        query = f"""
            SELECT DISTINCT s.* FROM sessions s
            WHERE {del_clause}
              AND {start_clause}
            ORDER BY s.last_active_at DESC
            LIMIT ?
        """
        params = start_params + [limit]
    else:
        folder_clause, folder_params = _build_match("r.folder_path")

        rnk_filter = ""
        rnk_params: list = []
        if top_n is not None:
            rnk_filter = "AND r.rnk <= ?"
            rnk_params = [top_n]

        query = f"""
            WITH ranked AS (
                SELECT session_id, folder_path,
                       ROW_NUMBER() OVER (
                           PARTITION BY session_id
                           ORDER BY usage_count DESC, folder_path
                       ) AS rnk
                FROM folder_usage
            )
            SELECT DISTINCT s.* FROM sessions s
            WHERE {del_clause}
              AND (
                {start_clause}
                OR EXISTS (
                    SELECT 1 FROM ranked r
                    WHERE r.session_id = s.session_id
                      {rnk_filter}
                      AND {folder_clause}
                )
              )
            ORDER BY s.last_active_at DESC
            LIMIT ?
        """

        params = start_params + rnk_params + folder_params + [limit]
    rows = conn.execute(query, params).fetchall()

    results = []
    for row in rows:
        session = dict(row)
        folders = conn.execute(
            "SELECT folder_path, usage_count, is_start_folder FROM folder_usage "
            "WHERE session_id = ? ORDER BY usage_count DESC",
            (session["session_id"],),
        ).fetchall()
        session["folders"] = [dict(f) for f in folders]
        results.append(session)

    return results


def find_sessions_by_folder_usage(conn: sqlite3.Connection, path_prefix: str,
                                   limit: int = 50) -> list[dict]:
    """
    Find sessions where any folder_usage path starts with path_prefix.

    Matches:
      - Exact path: C:\\code\\chrome
      - Child paths: C:\\code\\chrome\\subfolder
      - Sibling-prefix paths: C:\\code\\chrome-extension (starts with prefix)

    This catches sessions started elsewhere but did real work in a
    directory matching the prefix. Case-insensitive.
    """
    # Normalize separators for matching
    prefix = path_prefix.replace("/", "\\")
    # Match anything that starts with the prefix (covers exact, children, and
    # sibling-prefix like chrome -> chrome-extension)
    pattern = prefix + "%"

    rows = conn.execute("""
        SELECT DISTINCT s.* FROM sessions s
        JOIN folder_usage fu ON s.session_id = fu.session_id
        WHERE fu.folder_path LIKE ? COLLATE NOCASE
          AND s.deleted_at IS NULL
        ORDER BY s.last_active_at DESC
        LIMIT ?
    """, (pattern, limit)).fetchall()

    results = []
    for row in rows:
        session = dict(row)
        folders = conn.execute(
            "SELECT folder_path, usage_count, is_start_folder FROM folder_usage "
            "WHERE session_id = ? ORDER BY usage_count DESC",
            (session["session_id"],),
        ).fetchall()
        session["folders"] = [dict(f) for f in folders]
        results.append(session)

    return results


def get_all_known_session_ids(conn: sqlite3.Connection) -> set[str]:
    """Return all session IDs we've ever tracked."""
    rows = conn.execute("SELECT session_id FROM sessions").fetchall()
    return {row["session_id"] for row in rows}


def get_active_session_ids(conn: sqlite3.Connection) -> set[str]:
    """Return session IDs that are not marked as deleted."""
    rows = conn.execute(
        "SELECT session_id FROM sessions WHERE deleted_at IS NULL"
    ).fetchall()
    return {row["session_id"] for row in rows}


def get_stats(conn: sqlite3.Connection) -> dict:
    """Return summary statistics."""
    total = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
    active = conn.execute(
        "SELECT COUNT(*) as c FROM sessions WHERE deleted_at IS NULL"
    ).fetchone()["c"]
    deleted = conn.execute(
        "SELECT COUNT(*) as c FROM sessions WHERE deleted_at IS NOT NULL"
    ).fetchone()["c"]
    projects = conn.execute(
        "SELECT COUNT(DISTINCT project) as c FROM sessions"
    ).fetchone()["c"]

    last_scan = conn.execute(
        "SELECT * FROM scan_history ORDER BY scan_id DESC LIMIT 1"
    ).fetchone()

    return {
        "total_sessions": total,
        "active_sessions": active,
        "deleted_sessions": deleted,
        "projects": projects,
        "last_scan": dict(last_scan) if last_scan else None,
    }


def record_scan(conn: sqlite3.Connection, scanned_at: str,
                found: int, new: int, updated: int, deleted: int,
                git_commit: str = ""):
    """Record a scan event in history."""
    conn.execute("""
        INSERT INTO scan_history (scanned_at, sessions_found, sessions_new,
                                  sessions_updated, sessions_deleted, git_commit)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (scanned_at, found, new, updated, deleted, git_commit))
    conn.commit()


def search_sessions(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    """Search sessions by name, project, or start_folder."""
    pattern = f"%{query}%"
    rows = conn.execute("""
        SELECT * FROM sessions
        WHERE session_name LIKE ? OR project LIKE ? OR start_folder LIKE ?
        ORDER BY last_active_at DESC
        LIMIT ?
    """, (pattern, pattern, pattern, limit)).fetchall()

    results = []
    for row in rows:
        session = dict(row)
        folders = conn.execute(
            "SELECT folder_path, usage_count, is_start_folder FROM folder_usage "
            "WHERE session_id = ? ORDER BY usage_count DESC",
            (session["session_id"],),
        ).fetchall()
        session["folders"] = [dict(f) for f in folders]
        results.append(session)

    return results


# ── git_deleted_jsonls cache (schema v4 / v0.3.11 backfill work) ───────

def upsert_git_deleted_jsonl(
    conn: sqlite3.Connection,
    jsonl_path: str,
    session_id: str,
    deleted_commit: Optional[str] = None,
    deleted_at: Optional[str] = None,
    last_commit: Optional[str] = None,
    last_seen_size: Optional[int] = None,
    last_seen_mtime: Optional[float] = None,
    last_refreshed_at: Optional[str] = None,
) -> bool:
    """Insert-or-update one row in the git_deleted_jsonls cache.

    Returns True if a new row was inserted, False if an existing row was
    updated. Callers use this to count new-vs-known deletions during a
    backfill refresh.

    Preserves the ``extracted_metadata`` flag across updates -- don't
    reset it to 0 if a row is already marked extracted (the historical
    blob didn't change just because we re-scanned git).
    """
    existing = conn.execute(
        "SELECT extracted_metadata FROM git_deleted_jsonls WHERE jsonl_path = ?",
        (jsonl_path,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO git_deleted_jsonls "
            "(jsonl_path, session_id, deleted_commit, deleted_at, "
            " last_commit, last_seen_size, last_seen_mtime, "
            " extracted_metadata, last_refreshed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (jsonl_path, session_id, deleted_commit, deleted_at,
             last_commit, last_seen_size, last_seen_mtime, last_refreshed_at),
        )
        conn.commit()
        return True

    # Update path: refresh the git-side data but keep extracted_metadata.
    conn.execute(
        "UPDATE git_deleted_jsonls SET "
        " session_id = ?, deleted_commit = ?, deleted_at = ?, "
        " last_commit = COALESCE(?, last_commit), "
        " last_seen_size = COALESCE(?, last_seen_size), "
        " last_seen_mtime = COALESCE(?, last_seen_mtime), "
        " last_refreshed_at = ? "
        "WHERE jsonl_path = ?",
        (session_id, deleted_commit, deleted_at,
         last_commit, last_seen_size, last_seen_mtime,
         last_refreshed_at, jsonl_path),
    )
    conn.commit()
    return False


def list_git_deleted_jsonls(
    conn: sqlite3.Connection,
    extracted: Optional[int] = None,
) -> list[dict]:
    """Return git_deleted_jsonls rows. Filter by extracted_metadata if set.

    ``extracted=0`` -> only rows still pending metadata extraction
    ``extracted=1`` -> only rows already extracted
    ``extracted=None`` (default) -> all rows
    """
    if extracted is None:
        rows = conn.execute(
            "SELECT * FROM git_deleted_jsonls ORDER BY jsonl_path"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM git_deleted_jsonls "
            "WHERE extracted_metadata = ? ORDER BY jsonl_path",
            (int(extracted),),
        ).fetchall()
    return [dict(r) for r in rows]


def count_git_deleted_jsonls(
    conn: sqlite3.Connection,
    extracted: Optional[int] = None,
) -> int:
    """Count git_deleted_jsonls rows. Filter by extracted_metadata if set."""
    if extracted is None:
        row = conn.execute(
            "SELECT COUNT(*) FROM git_deleted_jsonls"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM git_deleted_jsonls WHERE extracted_metadata = ?",
            (int(extracted),),
        ).fetchone()
    return int(row[0]) if row else 0


def mark_git_deleted_extracted(
    conn: sqlite3.Connection,
    jsonl_path: str,
) -> None:
    """Flip extracted_metadata to 1 for a given path. No-op if the row is missing."""
    conn.execute(
        "UPDATE git_deleted_jsonls SET extracted_metadata = 1 "
        "WHERE jsonl_path = ?",
        (jsonl_path,),
    )
    conn.commit()

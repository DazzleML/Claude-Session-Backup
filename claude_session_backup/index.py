"""
SQLite index -- rebuildable metadata cache for fast queries.

This is NOT the source of truth. The git repository is.
If this database is lost, `csb rebuild-index` reconstructs it.
"""

import sqlite3
from pathlib import Path
from typing import Optional

from .metadata import SessionMetadata

SCHEMA_VERSION = 2

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

CREATE INDEX IF NOT EXISTS idx_sessions_last_active ON sessions(last_active_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_sessions_deleted ON sessions(deleted_at);
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


def init_schema(conn: sqlite3.Connection):
    """Initialize database schema if not already present."""
    conn.executescript(SCHEMA_SQL)

    # Track schema version
    conn.execute(
        "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.commit()


def upsert_session(conn: sqlite3.Connection, meta: SessionMetadata,
                   jsonl_path: str = "", jsonl_size: int = 0,
                   jsonl_mtime: float = 0.0, scanned_at: str = ""):
    """Insert or update a session in the index."""
    conn.execute("""
        INSERT INTO sessions (
            session_id, project, session_name, start_folder,
            started_at, last_active_at, last_user_at, message_count, tool_call_count,
            claude_version, jsonl_path, jsonl_size, jsonl_mtime, last_scanned_at, deleted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
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
            deleted_at = NULL
    """, (
        meta.session_id, meta.project, meta.session_name, meta.start_folder,
        meta.started_at, meta.last_active_at, meta.last_user_at,
        meta.message_count, meta.tool_call_count,
        meta.claude_version, jsonl_path, jsonl_size, jsonl_mtime, scanned_at,
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


def mark_deleted(conn: sqlite3.Connection, session_id: str, deleted_at: str):
    """Mark a session as deleted (no longer present on disk)."""
    conn.execute(
        "UPDATE sessions SET deleted_at = ? WHERE session_id = ? AND deleted_at IS NULL",
        (deleted_at, session_id),
    )
    conn.commit()


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


def find_sessions_by_term(
    conn: sqlite3.Connection,
    term: str,
    top_n: int | None = None,
    limit: int = 50,
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
    """
    pattern = f"%{escape_like_value(term)}%"

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
        WHERE s.deleted_at IS NULL
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

    if start_folder_only:
        # Skip folder_usage entirely -- match against start_folder only.
        query = f"""
            SELECT DISTINCT s.* FROM sessions s
            WHERE s.deleted_at IS NULL
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
            WHERE s.deleted_at IS NULL
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

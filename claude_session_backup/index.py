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
                   scanned_at: str = ""):
    """Insert or update a session in the index."""
    conn.execute("""
        INSERT INTO sessions (
            session_id, project, session_name, start_folder,
            started_at, last_active_at, last_user_at, message_count, tool_call_count,
            claude_version, jsonl_path, jsonl_size, last_scanned_at, deleted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
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
            last_scanned_at = excluded.last_scanned_at,
            deleted_at = NULL
    """, (
        meta.session_id, meta.project, meta.session_name, meta.start_folder,
        meta.started_at, meta.last_active_at, meta.last_user_at,
        meta.message_count, meta.tool_call_count,
        meta.claude_version, jsonl_path, jsonl_size, scanned_at,
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


def list_sessions(conn: sqlite3.Connection, limit: int = 20,
                  show_deleted: bool = False, show_all: bool = False) -> list[dict]:
    """List sessions ordered by last_active_at descending."""
    if show_all:
        where = ""
    elif show_deleted:
        where = "WHERE deleted_at IS NOT NULL"
    else:
        where = "WHERE deleted_at IS NULL"

    rows = conn.execute(f"""
        SELECT * FROM sessions
        {where}
        ORDER BY last_active_at DESC NULLS LAST
        LIMIT ?
    """, (limit,)).fetchall()

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

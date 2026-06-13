"""
JSONL -> FTS5 ingest.

This module is now a thin shim over :mod:`transcript_walker`. The shared
walker yields ``ImportRow`` + ``FileOpRow`` instances; we insert them
into a per-project SQLite FTS5 DB and report the resulting row count +
content hash.

History: v0.3.0 had the walker + importer fused in one module. v0.3.1
split them so :mod:`search` can use the same walker against JSONL for
``--source jsonl`` searches -- closing a parity bug where Phase 1's own
JSONL parser missed Task-launched Agent content and file-op metadata.

Re-exports the walker's public symbols (``ImportRow``, ``FileOpRow``,
``iter_rows_from_jsonl``, plus the private helpers) so existing tests
and downstream code that imports from ``claude_session_backup.fts5_importer``
keep working without modification.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Re-export the walker's public + private symbols so existing imports
# keep working. The walker is the source of truth for these names.
from .transcript_walker import (  # noqa: F401
    FileOpRow,
    ImportRow,
    _AGENT_TOOL_NAMES,
    _FILE_OP_TOOLS,
    _extract_agent_tool_uses,
    _extract_file_ops,
    _find_matching_tool_result,
    _flatten_text_blocks,
    _flatten_tool_result_content,
    format_role_label,
    iter_rows_from_jsonl,
)


def _content_hash(jsonl_path: Path) -> str:
    """SHA-256 of the JSONL file -- 'has this file changed?' cheap check.

    Used as a secondary freshness signal in addition to mtime, for the
    rare case where mtime is preserved but content differs (e.g.
    rsync --times).
    """
    h = hashlib.sha256()
    try:
        with open(jsonl_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def import_jsonl_to_db(
    fts_conn: sqlite3.Connection,
    session_id: str,
    jsonl_path: Path,
    *,
    delete_existing: bool = True,
) -> tuple[int, str]:
    """Stream-import a session's JSONL into a per-project FTS5 DB.

    Returns ``(rows_inserted, content_hash)``. The caller is expected to
    update the main DB's ``session_sources.fts5_indexed_at`` and
    ``content_hash`` columns AND the per-project DB's
    ``indexed_sessions`` row (via ``fts5_db.mark_session_indexed``).

    When ``delete_existing=True`` (default), all rows for this
    ``session_id`` are removed before re-import. This is the only safe
    way to handle JSONL edits that remove or reorder events. Pass
    ``False`` only for additive append-only workloads (currently
    unused).
    """
    if delete_existing:
        fts_conn.execute(
            "DELETE FROM messages WHERE session_id = ?", (session_id,)
        )
        fts_conn.execute(
            "DELETE FROM file_operations WHERE session_id = ?", (session_id,)
        )

    msg_sql = (
        "INSERT OR REPLACE INTO messages "
        "(session_id, uuid, message_index, role, role_subtype, "
        " content, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    fop_sql = (
        "INSERT OR IGNORE INTO file_operations "
        "(session_id, message_index, operation, file_path, strength, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    rows_inserted = 0
    cursor = fts_conn.cursor()
    try:
        for row in iter_rows_from_jsonl(Path(jsonl_path), session_id):
            if isinstance(row, ImportRow):
                cursor.execute(msg_sql, row.as_tuple())
                rows_inserted += 1
            else:  # FileOpRow
                cursor.execute(fop_sql, row.as_tuple())
        fts_conn.commit()
    except Exception:
        fts_conn.rollback()
        raise

    return rows_inserted, _content_hash(Path(jsonl_path))


def now_iso() -> str:
    """ISO 8601 UTC timestamp for ``indexed_at`` columns."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

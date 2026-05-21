"""
FTS5 build orchestrator (Phase 2 of #3).

Walks the main DB's ``sessions`` + ``session_sources`` tables to
discover candidate sessions, opens the per-project FTS5 DB for each,
checks ``indexed_sessions`` freshness, and calls the JSONL importer
when work is needed. Updates the main DB's ``session_sources.
fts5_indexed_at`` / ``content_hash`` columns after each successful
session import.

The orchestrator is intentionally explicit -- no hidden background
indexing, no triggers in the main DB. ``csb backup`` does NOT call
this (would slow the hook). Users opt in via ``csb build-fts5`` or
the future ``csb backup --refresh-fts5`` flag.

Source of truth (per design doc):
  - Per-project FTS5 DB's ``indexed_sessions`` table: authoritative
    "is this session in the index, at what mtime"
  - Main DB's ``session_sources.fts5_indexed_at`` / ``content_hash``:
    HINT columns (kept in sync as a UX nicety; not consulted by the
    runtime search dispatcher in v0.3.1)
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from . import fts5_db
from . import fts5_importer
from . import fts_paths


@dataclass
class BuildStats:
    """Summary of a build_all run."""

    sessions_considered: int = 0
    sessions_indexed: int = 0
    sessions_skipped_fresh: int = 0
    sessions_skipped_missing_jsonl: int = 0
    sessions_skipped_no_project: int = 0
    rows_inserted: int = 0
    projects_touched: int = 0
    errors: int = 0


@dataclass
class _SessionCandidate:
    """One row from the sessions-to-consider enumeration."""

    session_id: str
    project: str
    encoded_slug: str
    jsonl_path: str
    jsonl_mtime: float


def _enumerate_sessions(
    main_conn: sqlite3.Connection,
    claude_dir: Path,
    project_filter: Optional[str] = None,
    session_filter: Optional[str] = None,
) -> Iterator[_SessionCandidate]:
    """Yield session candidates from the main DB.

    ``sessions.jsonl_path`` is stored as a RELATIVE path under
    ``claude_dir`` (e.g. ``"projects/<slug>/<uuid>.jsonl"``). This
    enumerator joins them against ``claude_dir`` to produce absolute
    paths for the importer + stat() calls.

    Sessions without a ``jsonl_path`` or whose file no longer exists
    are silently dropped. Deleted sessions (``deleted_at IS NOT NULL``)
    are excluded -- their JSONL has been purged from disk.

    ``project_filter`` matches ``sessions.project`` exactly (slug form).
    ``session_filter`` matches ``sessions.session_id`` exactly (caller
    resolves UUID prefix via :func:`ids.resolve_session_id` first).
    """
    where: list[str] = ["s.jsonl_path IS NOT NULL", "s.deleted_at IS NULL"]
    params: list = []
    if project_filter:
        where.append("s.project = ?")
        params.append(project_filter)
    if session_filter:
        where.append("s.session_id = ?")
        params.append(session_filter)

    sql = (
        "SELECT s.session_id, s.project, s.jsonl_path "
        "FROM sessions s "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY s.last_active_at DESC"
    )
    claude_dir = Path(claude_dir)
    for row in main_conn.execute(sql, params):
        rel_path = row["jsonl_path"]
        abs_path = claude_dir / rel_path
        try:
            live_mtime = abs_path.stat().st_mtime
        except OSError:
            # JSONL referenced but missing on disk -- skip silently.
            continue
        # encoded_slug = parent directory name (the original
        # ``~/.claude/projects/<slug>/`` folder).
        encoded_slug = abs_path.parent.name
        yield _SessionCandidate(
            session_id=row["session_id"],
            project=row["project"],
            encoded_slug=encoded_slug,
            jsonl_path=str(abs_path),
            jsonl_mtime=live_mtime,
        )


def _update_main_db_hint(
    main_conn: sqlite3.Connection,
    session_id: str,
    jsonl_path: str,
    indexed_at: str,
    content_hash: str,
) -> None:
    """Update session_sources.fts5_indexed_at + content_hash for the JSONL row.

    HINT-only: the per-project DB's ``indexed_sessions`` table is the
    source of truth; we keep this updated so ``csb scan``-style
    inspection or future dashboards have a quick read.
    """
    main_conn.execute(
        "UPDATE session_sources "
        "SET fts5_indexed_at = ?, content_hash = ? "
        "WHERE session_id = ? AND source_path = ?",
        (indexed_at, content_hash, session_id, jsonl_path),
    )
    main_conn.commit()


def build_all(
    main_conn: sqlite3.Connection,
    claude_dir: Path,
    *,
    project: Optional[str] = None,
    session_id: Optional[str] = None,
    force: bool = False,
    quiet: bool = False,
) -> BuildStats:
    """Build or refresh per-project FTS5 indices.

    Args:
        main_conn: open connection to ``session-backup.db``.
        claude_dir: ``~/.claude``-equivalent (where ``csb-fts/`` lives).
        project: optional project-slug filter. None = all projects.
        session_id: optional full UUID filter (caller resolves prefix
            via :func:`ids.resolve_session_id` before passing here).
        force: re-index every candidate even if the index is fresh.
        quiet: suppress per-session progress lines. Summary always
            printed at end unless caller explicitly wants silence.

    Returns:
        :class:`BuildStats` with the run counts.

    Side effects:
        - Creates ``<claude_dir>/csb-fts/`` if missing.
        - Creates / updates one or more per-project ``.db`` files.
        - Updates ``session_sources.fts5_indexed_at`` /
          ``content_hash`` on the main DB.
    """
    stats = BuildStats()
    open_fts_conns: dict[Path, sqlite3.Connection] = {}
    projects_touched: set[str] = set()

    try:
        for cand in _enumerate_sessions(
            main_conn, claude_dir,
            project_filter=project, session_filter=session_id,
        ):
            stats.sessions_considered += 1

            fts_path = fts_paths.fts5_db_path(
                claude_dir, cand.project, cand.encoded_slug,
            )
            if fts_path not in open_fts_conns:
                try:
                    open_fts_conns[fts_path] = fts5_db.open_fts5_db(
                        fts_path, quiet=quiet,
                    )
                except sqlite3.DatabaseError as e:
                    if not quiet:
                        print(
                            f"  [error] could not open {fts_path}: {e}",
                            file=sys.stderr,
                        )
                    stats.errors += 1
                    continue

            fts_conn = open_fts_conns[fts_path]

            # Freshness check (skip when force)
            if not force and fts5_db.is_session_indexed(
                fts_conn, cand.session_id, cand.jsonl_mtime,
            ):
                stats.sessions_skipped_fresh += 1
                continue

            # On force, purge first so re-import is clean
            if force:
                fts5_db.delete_session(fts_conn, cand.session_id)

            try:
                rows_added, content_hash = fts5_importer.import_jsonl_to_db(
                    fts_conn, cand.session_id, Path(cand.jsonl_path),
                )
            except Exception as e:  # noqa: BLE001
                if not quiet:
                    print(
                        f"  [error] import failed for {cand.session_id[:8]}: {e}",
                        file=sys.stderr,
                    )
                stats.errors += 1
                continue

            indexed_at = fts5_importer.now_iso()
            fts5_db.mark_session_indexed(
                fts_conn, cand.session_id, cand.jsonl_mtime,
                content_hash, indexed_at,
            )
            _update_main_db_hint(
                main_conn, cand.session_id, cand.jsonl_path,
                indexed_at, content_hash,
            )

            stats.sessions_indexed += 1
            stats.rows_inserted += rows_added
            projects_touched.add(cand.project)

            if not quiet:
                print(
                    f"  indexed {cand.session_id[:8]}  "
                    f"({cand.project}, {rows_added} msgs)"
                )

        stats.projects_touched = len(projects_touched)
    finally:
        for c in open_fts_conns.values():
            c.close()

    if not quiet:
        print(
            f"\nFTS5 build: {stats.sessions_indexed} indexed, "
            f"{stats.sessions_skipped_fresh} already fresh, "
            f"{stats.sessions_considered} considered, "
            f"{stats.rows_inserted} rows across "
            f"{stats.projects_touched} project(s)."
        )
        if stats.errors:
            print(f"  ({stats.errors} error(s) -- see stderr above)")

    return stats

"""
Per-project SQLite FTS5 content database (Phase 2 of #3).

One DB per project at ``~/.claude/csb-fts/<project>__<slug-hash>_<USER>.db``
(path convention locked in :mod:`fts_paths`). Each DB is self-contained --
no cross-DB joins, no shared schema with the main session-backup.db. The
main DB tracks WHICH sessions have been indexed via the already-reserved
``session_sources.fts5_indexed_at`` / ``content_hash`` columns; each
per-project FTS5 DB independently tracks the same fact via its own
``indexed_sessions`` table (source of truth for "did this row land?").

Why per-project (not one monolithic vault like claude-vault): smaller
files, faster targeted queries, per-project archive/move/delete, no
contention when multiple projects refresh in parallel. Locked at v0.2.5
in :mod:`fts_paths`.

Schema (mirrors claude-vault's pattern at ``db.rs``)::

    messages(id PK, session_id, uuid, message_index, role,
             role_subtype, content, timestamp,
             UNIQUE(session_id, message_index))
    messages_fts -- FTS5 virtual table with content='messages'
                    external content + porter unicode61 tokenizer
    indexed_sessions(session_id PK, last_jsonl_mtime, last_content_hash,
                     indexed_at)

The ``role`` + ``role_subtype`` split (vs storing ``"AGENT:explore"`` as
one role) lets queries say ``WHERE role = 'AGENT' AND role_subtype = ?``
cleanly. Renderers reconstruct the combined label.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path


# Schema is declared as a single executescript so it's transactionally
# applied even on a fresh file. All statements are IF NOT EXISTS so
# repeated calls to init_fts5_schema() are no-ops.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    uuid TEXT,
    message_index INTEGER NOT NULL,
    role TEXT NOT NULL,
    role_subtype TEXT,
    content TEXT NOT NULL,
    timestamp TEXT,
    UNIQUE (session_id, message_index)
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(role, role_subtype);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_uuid
    ON messages(uuid) WHERE uuid IS NOT NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content_rowid='id',
    content='messages',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS indexed_sessions (
    session_id TEXT PRIMARY KEY,
    last_jsonl_mtime REAL NOT NULL,
    last_content_hash TEXT,
    indexed_at TEXT NOT NULL
);

-- file_operations: discoverability layer (which conversations touched
-- which files). Populated from tool_use blocks during the SAME JSONL
-- walk that fills `messages`. We record the path + operation kind,
-- NOT the contents of changes (diffs live in git / on disk).
--
-- `strength` column (v0.3.1): per-row importance weight assigned at
-- import time. 3 = wrote/edited/notebook_edit (active modification),
-- 2 = read (passive view), 1 = searched (Grep probe). Used by future
-- ranking queries (`csb search -d` directory-scope mode). Default 2
-- so legacy rows pre-migration land on the median.
CREATE TABLE IF NOT EXISTS file_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    message_index INTEGER,           -- nullable: the originating msg row's index, if known
    operation TEXT NOT NULL,         -- 'read' | 'edited' | 'wrote' | 'searched' | 'notebook_edit'
    file_path TEXT NOT NULL,
    strength INTEGER NOT NULL DEFAULT 2,  -- v0.3.1: 3 (active) / 2 (read) / 1 (search)
    timestamp TEXT,
    UNIQUE (session_id, message_index, operation, file_path)
);

CREATE INDEX IF NOT EXISTS idx_file_ops_session
    ON file_operations(session_id);
CREATE INDEX IF NOT EXISTS idx_file_ops_path
    ON file_operations(file_path);
CREATE INDEX IF NOT EXISTS idx_file_ops_op
    ON file_operations(operation);

-- Per-project DB schema version. v1 = v0.3.0 (no strength column;
-- detected by ABSENCE of this table). v2 = v0.3.1 (strength column +
-- this version table). Migrations are applied in init_fts5_schema()
-- below before any further work touches the DB.
CREATE TABLE IF NOT EXISTS fts_schema_version (
    version INTEGER NOT NULL
);
"""

# Current per-project schema version. Bump when adding new columns /
# tables. v0.3.0 DBs in the wild are at v1 (no fts_schema_version row);
# init_fts5_schema migrates them in place on first open.
CURRENT_FTS_SCHEMA_VERSION = 2


def init_fts5_schema(
    conn: sqlite3.Connection,
    quiet: bool = False,
) -> None:
    """Create the messages / messages_fts / indexed_sessions schema and
    apply any pending per-project schema migrations.

    Idempotent -- safe to call on an already-initialized DB. All CREATE
    statements use IF NOT EXISTS.

    Migration logic lives in :mod:`fts5_migrations` (parallel to
    :mod:`migrations` for the main DB). Each open of a per-project DB
    consults the registry there and applies any pending steps in order.
    ``quiet`` is forwarded to suppress the per-migration notice line
    that ``apply_pending`` prints by default.
    """
    # Deferred import to keep the public surface of this module clean
    # and avoid a circular dep (fts5_migrations may eventually want to
    # reference fts5_db symbols for cross-version schema introspection).
    from . import fts5_migrations
    conn.executescript(_SCHEMA_SQL)
    fts5_migrations.apply_pending(conn, quiet=quiet)
    conn.commit()


def open_fts5_db(path: Path, quiet: bool = False) -> sqlite3.Connection:
    """Open (or create) a per-project FTS5 database at ``path``.

    Creates the parent directory if missing. Initializes schema and
    applies any pending migrations. Returns a connection with
    ``row_factory = sqlite3.Row`` so callers can read columns by name.

    ``quiet`` suppresses the per-migration auto-upgrade notice from
    ``fts5_migrations.apply_pending`` -- pass True from contexts that
    print their own progress (e.g. ``csb update build-fts5 --quiet``) and
    leave default False everywhere else so users see when their per-
    project DBs are silently auto-upgraded after an upgrade.

    Raises :class:`sqlite3.DatabaseError` (or subclass) if the file
    exists but isn't a valid SQLite DB. The caller's responsibility to
    decide whether to delete and retry; we never auto-clobber.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # Enforce foreign-key cascades for any future relations; cheap, idiomatic.
    conn.execute("PRAGMA foreign_keys = ON")
    init_fts5_schema(conn, quiet=quiet)
    return conn


def is_session_indexed(
    conn: sqlite3.Connection,
    session_id: str,
    jsonl_mtime: float,
) -> bool:
    """True if this session has been indexed AND the index is fresh.

    "Fresh" means the indexed row's ``last_jsonl_mtime`` >= the current
    ``jsonl_mtime``. Stale or missing rows return False, which signals
    the orchestrator to (re-)import.
    """
    row = conn.execute(
        "SELECT last_jsonl_mtime FROM indexed_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return False
    return row["last_jsonl_mtime"] >= jsonl_mtime


def delete_session(conn: sqlite3.Connection, session_id: str) -> int:
    """Purge all rows for ``session_id`` from messages, file_operations,
    and indexed_sessions.

    Returns the count of message rows deleted (the headline metric).
    Triggers handle FTS5 cleanup automatically. Used by ``--force``
    re-index and (future) by session-deletion handling.
    """
    cursor = conn.execute(
        "DELETE FROM messages WHERE session_id = ?", (session_id,)
    )
    deleted = cursor.rowcount
    conn.execute(
        "DELETE FROM file_operations WHERE session_id = ?", (session_id,)
    )
    conn.execute(
        "DELETE FROM indexed_sessions WHERE session_id = ?", (session_id,)
    )
    conn.commit()
    return deleted


def mark_session_indexed(
    conn: sqlite3.Connection,
    session_id: str,
    jsonl_mtime: float,
    content_hash: str | None,
    indexed_at: str,
) -> None:
    """Record (or update) the indexed_sessions row after a successful import."""
    conn.execute(
        "INSERT INTO indexed_sessions "
        "(session_id, last_jsonl_mtime, last_content_hash, indexed_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "last_jsonl_mtime = excluded.last_jsonl_mtime, "
        "last_content_hash = excluded.last_content_hash, "
        "indexed_at = excluded.indexed_at",
        (session_id, jsonl_mtime, content_hash, indexed_at),
    )
    conn.commit()


# ── FTS5 query escaping ──────────────────────────────────────────────


# A heuristic: if the user's pattern contains FTS5 syntax characters
# they probably know what they're doing. Otherwise we wrap each
# whitespace-separated token in double quotes (FTS5's literal-token
# syntax) so users can naturally type "oauth callback (v2)" without
# tripping on parens or punctuation.
_FTS5_OPERATOR_RE = re.compile(r'^[\s"]*[A-Z]+[\s"]+|[+*^]|"\s*[A-Z]+\s*"')


def escape_fts_query(pattern: str) -> str:
    """Quote a user pattern as a literal FTS5 phrase query.

    For a pattern like ``oauth callback``, returns
    ``"oauth" "callback"`` -- two FTS5 phrase terms (AND'd by default).
    Special chars inside individual tokens are stripped because FTS5
    rejects bare punctuation; this is intentionally conservative
    (false positives at the candidate-narrowing stage are fine -- the
    Python-side literal match in search.py validates accuracy).

    Heuristic: if the pattern already contains operators (AND / OR /
    NOT / NEAR, or ``+ * ^``), returns it verbatim so power users can
    write FTS5 queries directly via ``--regex`` / explicit strings.
    """
    if not pattern.strip():
        return ""
    # User-explicit FTS5 syntax: pass through unchanged.
    if _FTS5_OPERATOR_RE.search(pattern):
        return pattern
    # Split on whitespace, drop punctuation inside each token, wrap in
    # double quotes. Tokens with no alphanum content are dropped.
    out: list[str] = []
    for tok in pattern.split():
        cleaned = re.sub(r"[^\w]+", "", tok, flags=re.UNICODE)
        if cleaned:
            out.append(f'"{cleaned}"')
    return " ".join(out)


# ── FTS5 availability check ──────────────────────────────────────────


def fts5_available() -> bool:
    """True if the local SQLite was compiled with FTS5 support.

    Older SQLite (pre-3.9) lacks FTS5. Modern Python ships with a SQLite
    that does, but bundled distributions vary. Cheap to check once at
    startup; surfaces a clear error before we attempt CREATE VIRTUAL.
    """
    conn = sqlite3.connect(":memory:")
    try:
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE __probe USING fts5(x)"
            )
            return True
        except sqlite3.OperationalError:
            return False
    finally:
        conn.close()

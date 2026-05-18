"""
Transcript content search -- Phase 1 of #3 (parity-fixed in v0.3.1).

Walks the ``session_sources`` table (populated by ``csb backup``), parses
each source file for USER / AI / AGENT message blocks, and yields ``Hit``
records for every match. Literal substring is the default; regex is
opt-in via the caller's ``regex=True`` argument.

Three source channels are supported (per-session fallback):

- ``.convo_*.log`` (preferred) -- claude-session-logger's USER/AI/AGENT-only
  channel, multi-line block format: ``[[ts]] {ROLE: ... }``
- ``.sesslog_*.log`` -- same block format but also contains tool-call
  blocks (Bash, Read, Grep, ...). Tool blocks are filtered out at parse
  time; only USER/AI/AGENT survive.
- ``<uuid>.jsonl`` (authoritative fallback) -- raw Claude Code transcript.
  Delegates to :mod:`transcript_walker` for the actual JSONL walk, which
  also handles Task-launched Agent tool_use -> tool_result correlation
  (a v0.3.0 capability previously only available to the FTS5 importer).

The role-token grammar accepts ``USER``, ``AI``, ``AGENT``, and
``AGENT:<subtype>`` (e.g. ``AGENT:explore``, ``AGENT:senior-engineer``).
The subtype is preserved in the ``Hit.role`` field for downstream
filtering / display.

Context windowing is event-based (not character-based): ``-A N`` shows
the next N events after a match, ``-B N`` the previous N. For ``.convo`` /
``.sesslog`` files an "event" is a single block; for JSONL it's a single
``user``/``assistant`` message. The matched line is always yielded.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from . import fts5_db, fts_paths
from .transcript_walker import (
    ImportRow,
    format_role_label,
    iter_rows_from_jsonl,
)


# Opening of a USER / AI / AGENT[:subtype] block in .convo / .sesslog files.
# Captured groups: (timestamp, role-keyword-including-subtype, first-content)
# - The role keyword is one of USER, AI, or AGENT optionally followed by
#   ":<subtype>" where subtype is [A-Za-z0-9_-]+.
# - The first-content group captures whatever follows the role colon on the
#   opening line (empty when the block opens with just "{ROLE:" + newline).
_OPEN_RE = re.compile(
    r"^\[\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\] "
    r"\{(USER|AI|AGENT(?::[A-Za-z0-9_-]+)?):\s?(.*)$"
)


@dataclass
class Event:
    """One parsed message event from any source channel."""

    line_num: int  # 1-based line / event index within the source file
    role: str  # 'USER' | 'AI' | 'AGENT' | 'AGENT:<subtype>'
    timestamp: Optional[str]
    text: str


@dataclass
class Hit:
    """One match, ready for rendering."""

    session_id: str
    session_name: Optional[str]
    project: str
    last_active_at: Optional[str]
    source_type: str  # 'convo' | 'sesslog' | 'jsonl'
    source_path: str
    line_num: int
    role: str
    timestamp: Optional[str]
    matched_text: str
    start_folder: Optional[str] = None
    started_at: Optional[str] = None  # ISO 8601; used by --full-info renderer
    jsonl_mtime: float = 0.0  # epoch seconds; used for purge-countdown
    # --full-info 2 (level 2) additions: full folder list + meta fields.
    # Same row repeated across every Hit from this session (cheap ref).
    folders: list = field(default_factory=list)
    message_count: int = 0
    claude_version: Optional[str] = None
    context_above: list[Event] = field(default_factory=list)
    context_below: list[Event] = field(default_factory=list)


# ── Parsers ───────────────────────────────────────────────────────────


def parse_log_blocks(path: str) -> Iterator[Event]:
    """Yield Event for every USER/AI/AGENT block in a .convo or .sesslog file.

    Both channels use the same multi-line block format::

        [[YYYY-MM-DD HH:MM:SS]] {ROLE:
        ... content ...
        }

    Single-line form (closing ``}`` on the same line as the opener) is also
    supported. Non-USER/AI/AGENT tag blocks (Bash, Read, Skill, etc.) are
    skipped entirely -- their opening line doesn't match ``_OPEN_RE``.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip("\n")
        m = _OPEN_RE.match(line)
        if not m:
            i += 1
            continue

        ts, role, first = m.group(1), m.group(2), m.group(3)
        start_line = i + 1

        # Single-line case: "{ROLE: text}" all on one line
        if first.endswith("}"):
            yield Event(
                line_num=start_line,
                role=role,
                timestamp=ts,
                text=first[:-1].rstrip(),
            )
            i += 1
            continue

        # Multi-line: accumulate until a line that is exactly "}"
        chunks: list[str] = []
        if first:
            chunks.append(first)
        i += 1
        while i < n:
            ln = lines[i].rstrip("\n")
            if ln.rstrip() == "}":
                i += 1
                break
            chunks.append(ln)
            i += 1
        yield Event(
            line_num=start_line,
            role=role,
            timestamp=ts,
            text="\n".join(chunks),
        )


def parse_jsonl_events(
    path: str, session_id: Optional[str] = None
) -> Iterator[Event]:
    """Yield Event for every conversation row produced by the shared walker.

    Delegates to :func:`transcript_walker.iter_rows_from_jsonl` so the
    JSONL path produces the same role surface as the FTS5 importer:
    USER / AI / ``AGENT:<subtype>`` for both Skill-attributed AND
    Task-launched sub-agent output.

    The walker yields both ``ImportRow`` (conversation events) and
    ``FileOpRow`` (path-bearing tool_use blocks). For search rendering
    we only need the conversation events; FileOpRow rows are quietly
    dropped here. The (v0.3.x) ``--files`` / ``-d`` query paths will
    iterate the walker themselves and consume both kinds.

    ``session_id`` is plumbed for parity with the walker's signature
    but unused in the rendered Event (the caller already knows it).
    """
    sid = session_id or ""
    msg_index = 0
    for row in iter_rows_from_jsonl(Path(path), sid):
        if not isinstance(row, ImportRow):
            continue
        msg_index += 1
        yield Event(
            line_num=msg_index,  # 1-based event index, same shape as the old parser
            role=format_role_label(row.role, row.role_subtype),
            timestamp=row.timestamp,
            text=row.content,
        )


def parse_source(
    source_type: str, path: str, session_id: Optional[str] = None
) -> Iterator[Event]:
    """Dispatch to the correct parser for a source type.

    ``session_id`` is forwarded to the JSONL parser so the walker can
    label rows with the proper session id; ``.convo`` and ``.sesslog``
    parsers ignore it (they self-contain the role/timestamp grammar).
    """
    if source_type in ("convo", "sesslog"):
        yield from parse_log_blocks(path)
    elif source_type == "jsonl":
        yield from parse_jsonl_events(path, session_id)
    else:
        return


# ── Pattern matcher ───────────────────────────────────────────────────


def _build_matcher(pattern: str, regex: bool, case_sensitive: bool):
    """Return a callable ``match(text) -> bool``.

    Literal mode: case-insensitive substring by default; ``re.escape`` is
    used so regex metacharacters in a literal pattern are inert.
    """
    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"invalid regex {pattern!r}: {e}") from e
        return lambda text: bool(compiled.search(text))

    if case_sensitive:
        return lambda text: pattern in text
    needle = pattern.lower()
    return lambda text: needle in text.lower()


# ── Source resolution ─────────────────────────────────────────────────


# Sources are first-class peers in the dispatch order. The list defines
# the default ATTEMPT ORDER -- the search dispatcher walks it for each
# session and picks the first source that's available for that session.
#
# Order rationale:
#   1. fts5     -- fast indexed search when the per-project DB exists
#                  AND is fresh enough (last_jsonl_mtime >= session
#                  mtime). One sqlite query per session instead of a
#                  file walk; preserves AGENT:<subtype> labels via the
#                  walker-shared schema.
#   2. convo    -- claude-session-logger's USER/AI/AGENT-only multi-line
#                  block format. Live: hooks keep it current between
#                  builds. No tool-call noise.
#   3. sesslog  -- same block format, also contains tool-call blocks.
#                  Slightly bigger; same parser filters tool blocks out.
#   4. jsonl    -- raw Claude Code transcript. Source of truth. Always
#                  present (Claude Code writes it). Slowest because the
#                  walker has to extract AGENT correlation each call.
#
# Each source is INDEPENDENTLY OPTIONAL. A user without
# claude-session-logger sees `convo` / `sesslog` skipped automatically.
# A user who hasn't run `csb build-fts5` sees `fts5` skipped. A user
# who wants only one source uses `--source <name>` to pin the preference
# to a single entry. Future: per-user config can override the default
# preference list (e.g. logger-only users may want
# `["convo", "sesslog", "jsonl"]`).
_SOURCE_PREFERENCE = ("fts5", "convo", "sesslog", "jsonl")


def _resolve_preference(
    source_override: Optional[str],
    default_preference: tuple[str, ...] = _SOURCE_PREFERENCE,
) -> tuple[str, ...]:
    """Translate the user-facing ``--source`` value into a preference
    tuple the dispatcher can walk.

    ``"auto"`` / ``None`` -> ``default_preference`` (which the caller
    can adapt -- e.g. to drop ``convo`` / ``sesslog`` when no
    claude-session-logger output exists in the vault). ``"fts5"`` /
    ``"convo"`` / ``"sesslog"`` / ``"jsonl"`` -> a single-element
    tuple, pinning the user's choice so unavailable sources skip the
    session cleanly with no fallback.
    """
    if source_override in (None, "auto"):
        return default_preference
    return (source_override,)


def effective_default_preference(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Return the auto-detected source preference for this vault.

    Probes the main DB for evidence that ``claude-session-logger`` has
    ever written output (i.e. ``convo`` / ``sesslog`` rows in
    ``session_sources``). If absent, drops both from the preference
    so a logger-less user's default search naturally collapses to
    ``("fts5", "jsonl")`` -- no wasted lookups against sources that
    can't exist for them.

    User-explicit ``--source X`` always takes precedence over this
    default. This is purely for the "auto" path.
    """
    from .sesslog_parser import has_session_logger
    if has_session_logger(conn):
        return _SOURCE_PREFERENCE
    return tuple(s for s in _SOURCE_PREFERENCE if s not in ("convo", "sesslog"))


def _pick_source_for_session(
    session_row: sqlite3.Row,
    source_rows: list[sqlite3.Row],
    preference: tuple[str, ...],
    claude_dir: Optional[Path],
) -> tuple[Optional[str], object]:
    """Walk the preference list; return the first available source.

    Returns ``(source_type, handle)`` where ``handle`` is:
      - a :class:`pathlib.Path` to the per-project FTS5 DB (for ``fts5``)
      - the matching :class:`sqlite3.Row` from ``session_sources`` (for
        file-based sources)
      - ``None`` (paired with ``source_type=None``) when no source in
        the preference list is available for this session

    FTS5 availability requires the session to be in the per-project
    DB's ``indexed_sessions`` table. When the preference list has more
    than one entry (i.e. user took the default "auto" path), FTS5 also
    has to be FRESH (``last_jsonl_mtime >= session_jsonl_mtime``) so
    we don't return stale results when a perfectly good live source
    sits next in the preference. When the preference is a single
    explicit ``["fts5"]``, freshness is NOT required -- the user
    asked for FTS5 specifically and accepts possibly-stale data.

    File-based sources are available iff the corresponding row exists
    in ``session_sources``.
    """
    file_sources = {row["source_type"]: row for row in source_rows}
    explicit_choice = len(preference) == 1

    for source_type in preference:
        if source_type == "fts5":
            handle = _fts5_handle_for_session(
                session_row,
                claude_dir,
                require_fresh=not explicit_choice,
            )
            if handle is not None:
                return ("fts5", handle)
        elif source_type in file_sources:
            return (source_type, file_sources[source_type])
    return (None, None)


def _fts5_handle_for_session(
    session_row: sqlite3.Row,
    claude_dir: Optional[Path],
    *,
    require_fresh: bool,
) -> Optional[Path]:
    """Return the FTS5 DB path for this session if FTS5 is available
    (and, when ``require_fresh`` is True, fresh).

    Returns None when:
      - ``claude_dir`` is None (caller has no vault context; FTS5
        path resolution isn't possible)
      - the session row's ``jsonl_path`` is empty (we derive the
        encoded slug from it)
      - the per-project DB file doesn't exist
      - the session isn't in ``indexed_sessions``
      - ``require_fresh`` is True AND
        ``last_jsonl_mtime < session_jsonl_mtime``

    Wraps :func:`_fts5_path_if_indexed` so the dispatcher can keep
    its row-aware logic in one place.
    """
    if claude_dir is None:
        return None
    jsonl_rel = session_row["jsonl_path"]
    if not jsonl_rel:
        return None
    encoded_slug = Path(jsonl_rel).parent.name
    jsonl_mtime = (
        session_row["jsonl_mtime"] or 0.0 if require_fresh else None
    )
    return _fts5_path_if_indexed(
        claude_dir,
        session_row["project"],
        encoded_slug,
        session_row["session_id"],
        jsonl_mtime,
    )


# ── FTS5 dispatch helpers (v0.3.3) ────────────────────────────────────


def _fts5_path_if_indexed(
    claude_dir: Path,
    project: str,
    encoded_slug: str,
    session_id: str,
    jsonl_mtime: Optional[float] = None,
) -> Optional[Path]:
    """Return the FTS5 DB path IF this session has an indexed row.

    When ``jsonl_mtime`` is provided, also requires the index to be
    fresh: ``indexed_sessions.last_jsonl_mtime >= jsonl_mtime``. This
    is the "auto" smart-fallback contract -- stale -> grep fallback.

    When ``jsonl_mtime`` is None, returns the path as long as the
    session is in ``indexed_sessions`` at all -- regardless of staleness.
    This is the explicit ``--source fts5`` contract: the user asked
    for FTS5 specifically and accepts the possibility of stale data.

    Opens the per-project DB RAW (bypassing :func:`fts5_db.open_fts5_db`)
    so this read-only check doesn't trigger schema migrations or print
    the auto-upgrade notice during a search. A non-indexed DB just means
    "skip FTS5"; we never change DB state from inside the search path.

    Tolerates DB-open failures (corrupt file, ALTER mid-migration, etc.)
    by returning None -- search degrades gracefully to grep.
    """
    fts_path = fts_paths.fts5_db_path(claude_dir, project, encoded_slug)
    if not fts_path.exists():
        return None
    try:
        probe = sqlite3.connect(str(fts_path))
        probe.row_factory = sqlite3.Row
        row = probe.execute(
            "SELECT last_jsonl_mtime FROM indexed_sessions "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        probe.close()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    if jsonl_mtime is not None and row["last_jsonl_mtime"] < jsonl_mtime:
        return None
    return fts_path


def query_fts5_for_session(
    fts5_db_path: Path,
    session_id: str,
    pattern: str,
) -> Iterator[Event]:
    """Yield :class:`Event` for messages in this session's FTS5 DB.

    FTS5 ``MATCH`` does the candidate narrowing; the Python-side literal /
    regex matcher in :func:`search` handles the final correctness check.
    The porter stemmer + unicode61 tokenizer used by csb's FTS5 schema
    doesn't preserve exact-substring semantics, so we always validate
    Python-side after the FTS5 filter.

    If :func:`fts5_db.escape_fts_query` produces an empty pattern (all
    punctuation / whitespace), falls back to a full table scan of this
    session's messages so the Python matcher still gets to do its job.
    Matches Phase 1's "empty pattern = match all" semantics.
    """
    escaped = fts5_db.escape_fts_query(pattern)
    try:
        conn = sqlite3.connect(str(fts5_db_path))
        conn.row_factory = sqlite3.Row
    except sqlite3.DatabaseError:
        return
    try:
        if escaped:
            sql = (
                "SELECT m.message_index, m.role, m.role_subtype, "
                "  m.content, m.timestamp "
                "FROM messages m "
                "WHERE m.id IN (SELECT rowid FROM messages_fts "
                "  WHERE messages_fts MATCH ?) "
                "  AND m.session_id = ? "
                "ORDER BY m.message_index"
            )
            try:
                rows = conn.execute(sql, (escaped, session_id)).fetchall()
            except sqlite3.OperationalError:
                # FTS5 MATCH can raise on bizarre tokenizations; degrade
                # to full session scan rather than dropping all hits.
                rows = conn.execute(
                    "SELECT m.message_index, m.role, m.role_subtype, "
                    "  m.content, m.timestamp "
                    "FROM messages m WHERE m.session_id = ? "
                    "ORDER BY m.message_index",
                    (session_id,),
                ).fetchall()
        else:
            rows = conn.execute(
                "SELECT m.message_index, m.role, m.role_subtype, "
                "  m.content, m.timestamp "
                "FROM messages m WHERE m.session_id = ? "
                "ORDER BY m.message_index",
                (session_id,),
            ).fetchall()
        for row in rows:
            yield Event(
                line_num=(row["message_index"] or 0) + 1,
                role=format_role_label(row["role"], row["role_subtype"]),
                timestamp=row["timestamp"],
                text=row["content"],
            )
    finally:
        conn.close()


# ── Main search entry point ───────────────────────────────────────────


def search(
    conn: sqlite3.Connection,
    pattern: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    above: int = 0,
    below: int = 0,
    session_filter: Optional[str | list[str]] = None,
    source_override: Optional[str] = None,
    include_deleted: bool = False,
    only_deleted: bool = False,
    limit: int = 20,
    sort_key: str = "last-used",
    fetch_folders: bool = False,
    claude_dir: Optional[Path] = None,
) -> Iterator[Hit]:
    """Yield ``Hit`` for every match across all relevant sessions.

    Sessions are visited in the order chosen by ``sort_key`` (default
    ``last-used`` -- most-recently-active first). Valid keys match
    ``index.SORT_SQL``: ``last-used``, ``expiration``, ``started``,
    ``oldest``, ``messages``, ``size``. The iterator stops after ``limit``
    matches.

    ``session_filter`` accepts either a single UUID prefix (str) or a list
    of UUID prefixes. Multiple prefixes OR-match: ``session_id LIKE 'a%'
    OR session_id LIKE 'b%'``. Empty list / None means "all sessions".

    ``source_override`` translates to a per-session attempt-order:

      - ``None`` / ``"auto"`` -> walk :data:`_SOURCE_PREFERENCE` (fts5 ->
        convo -> sesslog -> jsonl), picking the first source available
        for each session. FTS5 must be FRESH to win at this level.
      - any single source name (``"fts5"``, ``"convo"``, ``"sesslog"``,
        ``"jsonl"``) -> pin the preference to that one source. Sessions
        where that source is unavailable are skipped; no fallback. For
        ``"fts5"`` specifically, freshness is NOT required -- the user
        asked for FTS5 and accepts possibly-stale results.

    Each source is independently optional. A user without
    claude-session-logger naturally skips ``convo`` / ``sesslog``. A
    user who hasn't run ``csb build-fts5`` naturally skips ``fts5``.
    A user with only the raw transcripts naturally falls all the way
    through to ``jsonl``. No "fallback" framing -- the dispatcher just
    walks the preference list in order.

    ``claude_dir`` is needed to resolve per-project FTS5 DB paths. When
    None, the dispatcher cannot evaluate the ``fts5`` source and treats
    it as unavailable for every session (the rest of the preference
    list runs normally).

    Raises ``ValueError`` if ``regex=True`` and the pattern doesn't compile,
    or if ``sort_key`` is not in ``SORT_SQL``.
    """
    # Defer import to avoid a circular dep (index.py imports nothing from
    # search.py, but pulling it in at module load time would couple the
    # two more tightly than they need to be).
    from .index import SORT_SQL
    if sort_key not in SORT_SQL:
        raise ValueError(
            f"Unknown sort_key {sort_key!r}; expected one of {sorted(SORT_SQL)}"
        )
    matcher = _build_matcher(pattern, regex, case_sensitive)
    # Auto-mode preference adapts to the user's actual vault: drops
    # convo / sesslog when no claude-session-logger output exists.
    # Explicit --source X overrides this entirely.
    default_pref = effective_default_preference(conn)
    preference = _resolve_preference(source_override, default_pref)

    # Build the session enumeration SQL. We could join sessions to
    # session_sources directly but a two-step approach (enumerate sessions,
    # then per-session fetch their sources) makes the source-preference
    # logic above easier to read and keeps the row count small.
    where = []
    params: list = []
    if only_deleted:
        where.append("s.deleted_at IS NOT NULL")
    elif not include_deleted:
        where.append("s.deleted_at IS NULL")

    # Normalize session_filter to a list of prefixes for uniform OR-match.
    prefixes: list[str] = []
    if isinstance(session_filter, str):
        prefixes = [session_filter] if session_filter else []
    elif session_filter:
        prefixes = [p for p in session_filter if p]
    if prefixes:
        ors = " OR ".join(["s.session_id LIKE ?"] * len(prefixes))
        where.append(f"({ors})")
        params.extend(f"{p}%" for p in prefixes)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT s.session_id, s.session_name, s.project, s.last_active_at, "
        "s.start_folder, s.started_at, s.jsonl_mtime, s.jsonl_path, "
        "s.message_count, s.claude_version "
        "FROM sessions s"
        f"{where_sql} "
        f"ORDER BY {SORT_SQL[sort_key]}"
    )

    hits_yielded = 0
    for session_row in conn.execute(sql, params):
        if hits_yielded >= limit:
            break

        # Fetch all session_sources rows for this session in one query.
        # The picker walks the preference list (FTS5 -> file sources)
        # and decides which one is available; file sources need a row
        # here, FTS5 looks at the per-project DB instead.
        source_rows = conn.execute(
            "SELECT source_type, source_path FROM session_sources "
            "WHERE session_id = ?",
            (session_row["session_id"],),
        ).fetchall()

        picked_type, picked_handle = _pick_source_for_session(
            session_row, source_rows, preference, claude_dir,
        )
        if picked_type is None:
            continue

        if picked_type == "fts5":
            events = list(query_fts5_for_session(
                picked_handle,  # Path to per-project FTS5 DB
                session_row["session_id"],
                pattern,
            ))
            picked_source_type = "fts5"
            picked_source_path = str(picked_handle)
        else:
            # File-based source: handle is the session_sources row.
            events = list(parse_source(
                picked_handle["source_type"],
                picked_handle["source_path"],
                session_row["session_id"],
            ))
            picked_source_type = picked_handle["source_type"]
            picked_source_path = picked_handle["source_path"]

        if not events:
            continue

        # Level-2 --full-info wants the full folder list. One query per
        # matching session; shared across this session's hits via the
        # `folders_for_session` list reference below.
        folders_for_session: list[dict] = []
        if fetch_folders:
            folder_rows = conn.execute(
                "SELECT folder_path, usage_count, is_start_folder "
                "FROM folder_usage WHERE session_id = ? "
                "ORDER BY usage_count DESC, is_start_folder DESC",
                (session_row["session_id"],),
            ).fetchall()
            folders_for_session = [dict(r) for r in folder_rows]

        for idx, ev in enumerate(events):
            if not matcher(ev.text):
                continue

            ctx_above = events[max(0, idx - above):idx] if above > 0 else []
            ctx_below = events[idx + 1:idx + 1 + below] if below > 0 else []

            yield Hit(
                session_id=session_row["session_id"],
                session_name=session_row["session_name"],
                project=session_row["project"],
                last_active_at=session_row["last_active_at"],
                source_type=picked_source_type,
                source_path=picked_source_path,
                line_num=ev.line_num,
                role=ev.role,
                timestamp=ev.timestamp,
                matched_text=ev.text,
                start_folder=session_row["start_folder"],
                started_at=session_row["started_at"],
                jsonl_mtime=session_row["jsonl_mtime"] or 0.0,
                folders=folders_for_session,
                message_count=session_row["message_count"] or 0,
                claude_version=session_row["claude_version"],
                context_above=ctx_above,
                context_below=ctx_below,
            )
            hits_yielded += 1
            if hits_yielded >= limit:
                break

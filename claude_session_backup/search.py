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


# Per-session source preference: most-readable first.
_SOURCE_PREFERENCE = ("convo", "sesslog", "jsonl")


def _pick_one_source(
    sources_for_session: list[sqlite3.Row],
    source_override: Optional[str],
) -> Optional[sqlite3.Row]:
    """Choose the single source row to search for a session.

    With no override, pick the highest-preference source available.
    With an override, return only a row of that type (or None).
    """
    by_type = {row["source_type"]: row for row in sources_for_session}
    if source_override and source_override != "auto":
        return by_type.get(source_override)
    for st in _SOURCE_PREFERENCE:
        if st in by_type:
            return by_type[st]
    return None


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

    ``source_override`` constrains the per-session source choice to a single
    channel; without it, ``.convo`` is preferred, then ``.sesslog``, then
    JSONL. A session with no matching source row is silently skipped.

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
        "s.start_folder, s.started_at, s.jsonl_mtime, "
        "s.message_count, s.claude_version "
        "FROM sessions s"
        f"{where_sql} "
        f"ORDER BY {SORT_SQL[sort_key]}"
    )

    hits_yielded = 0
    for session_row in conn.execute(sql, params):
        if hits_yielded >= limit:
            break

        # Fetch all source rows for this session in a single query
        source_rows = conn.execute(
            "SELECT source_type, source_path FROM session_sources "
            "WHERE session_id = ?",
            (session_row["session_id"],),
        ).fetchall()
        if not source_rows:
            continue

        picked = _pick_one_source(source_rows, source_override)
        if picked is None:
            continue

        # Materialize events so we can slice context windows
        events = list(parse_source(
            picked["source_type"],
            picked["source_path"],
            session_row["session_id"],
        ))
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
                source_type=picked["source_type"],
                source_path=picked["source_path"],
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

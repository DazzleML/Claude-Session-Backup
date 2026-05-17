"""
Transcript content search -- Phase 1 of #3.

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
  Event-level parser extracts ``type:user`` and ``type:assistant`` events
  only; assistant content blocks are flattened to plain text.

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

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional


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


def _flatten_assistant_content(content) -> str:
    """Concatenate the text from every ``type:'text'`` block in an assistant
    message's ``content`` array. Other block types (tool_use, thinking, etc.)
    are skipped -- we are searching conversation, not tool I/O.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def parse_jsonl_events(path: str) -> Iterator[Event]:
    """Yield Event for ``type:user`` and ``type:assistant`` events in a JSONL.

    User events: ``message.content`` is a plain string.
    Assistant events: ``message.content`` is a list of content blocks --
    flatten ``text`` blocks only.

    Malformed lines are skipped silently (matches the existing csb metadata
    parser's tolerance).
    """
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return

    try:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype not in ("user", "assistant"):
                continue
            ts = event.get("timestamp") or None
            msg = event.get("message", {}) or {}
            content = msg.get("content", "")
            text = _flatten_assistant_content(content) if etype == "assistant" else (
                content if isinstance(content, str) else _flatten_assistant_content(content)
            )
            if not text:
                continue
            yield Event(
                line_num=i,
                role="USER" if etype == "user" else "AI",
                timestamp=ts,
                text=text,
            )
    finally:
        f.close()


def parse_source(source_type: str, path: str) -> Iterator[Event]:
    """Dispatch to the correct parser for a source type."""
    if source_type in ("convo", "sesslog"):
        yield from parse_log_blocks(path)
    elif source_type == "jsonl":
        yield from parse_jsonl_events(path)
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
    session_filter: Optional[str] = None,
    source_override: Optional[str] = None,
    include_deleted: bool = False,
    only_deleted: bool = False,
    limit: int = 20,
) -> Iterator[Hit]:
    """Yield ``Hit`` for every match across all relevant sessions.

    Sessions are visited in ``last_active_at DESC`` order so the most-recent
    sessions surface first. The iterator stops after ``limit`` matches.

    ``source_override`` constrains the per-session source choice to a single
    channel; without it, ``.convo`` is preferred, then ``.sesslog``, then
    JSONL. A session with no matching source row is silently skipped.

    Raises ``ValueError`` if ``regex=True`` and the pattern doesn't compile.
    """
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

    if session_filter:
        where.append("s.session_id LIKE ?")
        params.append(f"{session_filter}%")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT s.session_id, s.session_name, s.project, s.last_active_at "
        "FROM sessions s"
        f"{where_sql} "
        "ORDER BY s.last_active_at DESC"
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
        events = list(parse_source(picked["source_type"], picked["source_path"]))
        if not events:
            continue

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
                context_above=ctx_above,
                context_below=ctx_below,
            )
            hits_yielded += 1
            if hits_yielded >= limit:
                break

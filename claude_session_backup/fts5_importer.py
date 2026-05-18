"""
JSONL → FTS5 ingest with role-aware classification (Phase 2 of #3).

Walks a session's ``<uuid>.jsonl`` and yields/inserts ``messages`` rows
labeled with ``role`` ('USER' | 'AI' | 'AGENT') and optional
``role_subtype`` (the agent / skill name when role is AGENT).

Two AGENT-detection paths cover the two sub-agent invocation styles
Claude Code uses:

1. **Skill-attributed** -- the top-level ``message.attributionSkill``
   field on a ``type:'assistant'`` event. Slash-command skills like
   ``/commit``, ``/dev-workflow-process``, ``/addendum`` set this. The
   assistant's reply text lives inside the same event's content blocks
   and is indexed directly.

2. **Agent tool-launched** -- the ``Agent`` tool_use block carries
   ``input.subagent_type`` (e.g. ``"Explore"``, ``"Plan"``,
   ``"senior-engineer"``). The sub-agent's actual response then arrives
   in a SUBSEQUENT ``tool_result`` block whose ``tool_use_id`` matches.
   The tool_result is itself nested inside a ``type:'user'`` event (the
   system framing the tool output back to the model). The importer
   tracks Agent ``tool_use.id`` -> subagent_type during the linear walk
   and labels the matching tool_result text as AGENT:<subagent_type>.

Both paths normalize the subtype to lowercase so display is consistent
regardless of which path produced the row ("Explore" and "explore"
become "explore").

Other content types are deliberately skipped:

  - ``thinking`` blocks (internal reasoning, not conversation)
  - ``tool_use`` blocks (except Agent, which is tracked for path #2)
  - ``tool_result`` blocks (except those matching an Agent tool_use)
  - ``type:'system'`` events, ``type:'summary'`` events
  - Empty / whitespace-only content

Content is preserved VERBATIM (no stripping of ``<system-reminder>``,
``<command-name>``, etc. -- csb policy diverges from claude-vault here
because we want search to faithfully reflect what was in the transcript).

Dedup is by ``(session_id, message_index)``: the same JSONL re-imported
yields the same rows.  ``import_jsonl_to_db`` uses ``INSERT OR REPLACE``
so a partial earlier import is upgraded in place on the next run.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


# Tool names whose tool_use blocks we track to find sub-agent identity.
# The single Agent tool is currently the only Task-style launcher.
_AGENT_TOOL_NAMES = ("Agent", "Task")


# Tool names -> file-operation kind. Each entry's tool_use.input gets
# the noted path-bearing field extracted and recorded as a
# file_operations row. Bash is intentionally absent: parsing arbitrary
# shell command lines for `cd` / `rm` / `cat` etc. is fragile and
# deferred (see CHANGELOG / task tracker).
_FILE_OP_TOOLS: dict[str, tuple[str, str]] = {
    # tool_name : (operation_kind, input_field)
    "Read":         ("read",        "file_path"),
    "Edit":         ("edited",      "file_path"),
    "Write":        ("wrote",       "file_path"),
    "Grep":         ("searched",    "path"),
    "NotebookEdit": ("notebook_edit", "notebook_path"),
}


class FileOpRow:
    """One row destined for the file_operations table."""

    __slots__ = ("session_id", "message_index", "operation",
                 "file_path", "timestamp")

    def __init__(self, *, session_id: str, message_index: Optional[int],
                 operation: str, file_path: str, timestamp: Optional[str]):
        self.session_id = session_id
        self.message_index = message_index
        self.operation = operation
        self.file_path = file_path
        self.timestamp = timestamp

    def as_tuple(self):
        return (
            self.session_id, self.message_index,
            self.operation, self.file_path, self.timestamp,
        )


def _extract_file_ops(content) -> Iterator[tuple[str, str]]:
    """Yield ``(operation, file_path)`` for each path-bearing tool_use.

    Walks an assistant event's content blocks. Skips tool_use blocks
    that aren't in ``_FILE_OP_TOOLS`` (Bash, Glob without paths, etc.).
    Empty / missing path fields are silently skipped (defensive --
    malformed tool_use shouldn't crash a session import).
    """
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        name = block.get("name")
        spec = _FILE_OP_TOOLS.get(name)
        if not spec:
            continue
        op_kind, input_field = spec
        inp = block.get("input") or {}
        path = inp.get(input_field)
        if isinstance(path, str) and path.strip():
            yield (op_kind, path)


def _flatten_text_blocks(content) -> str:
    """Concatenate text from all ``type:'text'`` blocks.

    Handles three shapes:
      - ``str`` (user messages are typically strings): returned as-is
      - ``list[dict]`` (assistant content blocks): joins ``type:'text'``
        block ``text`` fields with newlines; skips other block types
      - anything else: empty string

    NEVER strips, normalizes, or sanitizes. Verbatim by policy.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts)


def _flatten_tool_result_content(content) -> str:
    """Pull text from a tool_result block's ``content`` field.

    tool_result content can be:
      - a plain string
      - a list of ``{type:'text', text:...}`` blocks

    Returns concatenated text, never raises on malformed shapes.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("text")
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _extract_agent_tool_uses(content) -> Iterator[tuple[str, str]]:
    """Yield ``(tool_use_id, subagent_type_lower)`` for each Agent tool_use
    block in an assistant event's content.
    """
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        if block.get("name") not in _AGENT_TOOL_NAMES:
            continue
        tu_id = block.get("id")
        inp = block.get("input") or {}
        subtype = inp.get("subagent_type") or inp.get("agent_type")
        if tu_id and subtype:
            yield (tu_id, str(subtype).lower())


def _find_matching_tool_result(content, tracked_agents: dict[str, str]):
    """Look in a user event's content for a tool_result whose
    ``tool_use_id`` matches a tracked Agent tool_use.

    Yields ``(subagent_type, text)`` for each match. Most user events
    have at most one tool_result; lists are walked in order.
    """
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_result":
            continue
        tu_id = block.get("tool_use_id")
        if tu_id not in tracked_agents:
            continue
        text = _flatten_tool_result_content(block.get("content"))
        if text:
            yield (tracked_agents[tu_id], text)


# Records produced for each indexable event, before SQL INSERT.
class ImportRow:
    """One row destined for the messages table.

    Plain class (no dataclass) to keep the module import-time cheap
    for the importer hot path.
    """

    __slots__ = ("session_id", "uuid", "message_index", "role",
                 "role_subtype", "content", "timestamp")

    def __init__(self, *, session_id: str, uuid: Optional[str],
                 message_index: int, role: str,
                 role_subtype: Optional[str], content: str,
                 timestamp: Optional[str]):
        self.session_id = session_id
        self.uuid = uuid
        self.message_index = message_index
        self.role = role
        self.role_subtype = role_subtype
        self.content = content
        self.timestamp = timestamp

    def as_tuple(self):
        return (
            self.session_id, self.uuid, self.message_index,
            self.role, self.role_subtype, self.content, self.timestamp,
        )


def iter_rows_from_jsonl(
    jsonl_path: Path,
    session_id: str,
) -> Iterator[ImportRow | FileOpRow]:
    """Stream ``ImportRow`` and ``FileOpRow`` instances from a session's JSONL.

    Iterates linearly, tracking Agent ``tool_use.id`` -> subagent_type
    so subsequent ``tool_result`` blocks can be labeled correctly.
    Path-bearing tool_use blocks (Read / Edit / Write / Grep /
    NotebookEdit) also emit ``FileOpRow`` instances tied to the
    current ``message_index``.

    Skipped silently:
      - Malformed JSON lines
      - Events of type other than 'user' / 'assistant'
      - Empty content after flattening (for ImportRow only; file-op
        rows can still fire from a tool_use-only assistant event)
      - Assistant events containing only thinking blocks
    """
    tracked_agents: dict[str, str] = {}
    message_index = 0  # 0-based, advances per yielded ImportRow

    try:
        f = open(jsonl_path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return

    try:
        for line in f:
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

            evt_uuid = event.get("uuid")
            ts = event.get("timestamp")
            msg = event.get("message") or {}
            content = msg.get("content")

            # --- Path 1: track Agent tool_use blocks for later result matching
            if etype == "assistant":
                for tu_id, subtype in _extract_agent_tool_uses(content):
                    tracked_agents[tu_id] = subtype

            # --- File-op extraction: every path-bearing tool_use yields
            # a FileOpRow tied to the CURRENT message_index. We use
            # message_index BEFORE incrementing so the file op shares the
            # index with the assistant message that contained the tool
            # call (useful for join-back to context in future queries).
            if etype == "assistant":
                for op_kind, path in _extract_file_ops(content):
                    yield FileOpRow(
                        session_id=session_id,
                        message_index=message_index,
                        operation=op_kind,
                        file_path=path,
                        timestamp=ts,
                    )

            # --- Path 2: a user event MAY carry tool_result blocks for
            # previously-tracked Agent calls. Emit those as AGENT rows
            # BEFORE the user's own text (which is rarely present in
            # tool_result-carrying user events anyway).
            if etype == "user":
                for subtype, agent_text in _find_matching_tool_result(
                    content, tracked_agents
                ):
                    yield ImportRow(
                        session_id=session_id,
                        uuid=evt_uuid,  # OK to share with the user-event uuid
                        message_index=message_index,
                        role="AGENT",
                        role_subtype=subtype,
                        content=agent_text,
                        timestamp=ts,
                    )
                    message_index += 1

            # --- Emit the event's own text (USER for type:user, AI/AGENT
            # for type:assistant depending on attributionSkill).
            text = _flatten_text_blocks(content)
            if not text:
                # No conversation content -- e.g. an assistant event that's
                # only a tool_use, or a user event that's only a tool_result
                # (already handled above as AGENT rows).
                continue

            if etype == "user":
                role, role_subtype = "USER", None
            else:  # assistant
                attribution = event.get("attributionSkill")
                if attribution:
                    role, role_subtype = "AGENT", str(attribution).lower()
                else:
                    role, role_subtype = "AI", None

            yield ImportRow(
                session_id=session_id,
                uuid=evt_uuid,
                message_index=message_index,
                role=role,
                role_subtype=role_subtype,
                content=text,
                timestamp=ts,
            )
            message_index += 1
    finally:
        f.close()


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
        "(session_id, message_index, operation, file_path, timestamp) "
        "VALUES (?, ?, ?, ?, ?)"
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

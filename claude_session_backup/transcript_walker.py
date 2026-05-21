"""
JSONL transcript walker -- shared by FTS5 import and Phase 1 grep.

Pure extraction: walks a session's ``<uuid>.jsonl`` and yields
:class:`ImportRow` (one per conversation event) plus :class:`FileOpRow`
(one per path-bearing tool_use). No sqlite3, no FTS5 dependencies --
just dict / string operations over the JSONL event stream.

Two consumers:

- :mod:`fts5_importer` -- writes rows into per-project FTS5 DBs
- :mod:`search` -- yields the same rows to grep / regex matching when
  ``--source jsonl`` is used (this fixed the Phase 1 parity bug where
  ``--source jsonl`` missed Task-launched Agent content and file-ops)

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
   system framing the tool output back to the model). The walker
   tracks Agent ``tool_use.id`` -> subagent_type during the linear walk
   and labels the matching tool_result text as AGENT:<subagent_type>.

Both paths normalize the subtype to lowercase so display is consistent
regardless of which path produced the row ("Explore" and "explore"
become "explore").

File-op rows are emitted from path-bearing tool_use blocks in assistant
events. Each row carries a static ``strength`` weight (3 = wrote /
edited / notebook_edit, 2 = read, 1 = searched) so downstream queries
can rank "files this session was actually working on" vs "files glanced
at."

Content is preserved VERBATIM (no stripping of ``<system-reminder>``,
``<command-name>``, etc. -- csb policy diverges from claude-vault here
because we want search to faithfully reflect what was in the transcript).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Optional


# Tool names whose tool_use blocks we track to find sub-agent identity.
# The single Agent tool is currently the only Task-style launcher.
_AGENT_TOOL_NAMES = ("Agent", "Task")


# Tool names -> (operation_kind, input_field, strength). Each entry's
# tool_use.input gets the noted path-bearing field extracted and recorded
# as a file_operations row. Bash is intentionally absent: parsing
# arbitrary shell command lines for `cd` / `rm` / `cat` etc. is fragile
# and deferred (see CHANGELOG / task tracker).
#
# Strength taxonomy (signal-of-importance for "is this file part of the
# work being done"):
#   3 = active modification (wrote / edited / notebook_edit)
#   2 = passive read
#   1 = search probe (we know the path was on the user's mind but the
#       file may not even have been read after)
_FILE_OP_TOOLS: dict[str, tuple[str, str, int]] = {
    # tool_name : (operation_kind, input_field, strength)
    "Read":         ("read",          "file_path",     2),
    "Edit":         ("edited",        "file_path",     3),
    "Write":        ("wrote",         "file_path",     3),
    "Grep":         ("searched",      "path",          1),
    "NotebookEdit": ("notebook_edit", "notebook_path", 3),
}


class FileOpRow:
    """One row destined for the file_operations table."""

    __slots__ = ("session_id", "message_index", "operation",
                 "file_path", "strength", "timestamp")

    def __init__(self, *, session_id: str, message_index: Optional[int],
                 operation: str, file_path: str, strength: int,
                 timestamp: Optional[str]):
        self.session_id = session_id
        self.message_index = message_index
        self.operation = operation
        self.file_path = file_path
        self.strength = strength
        self.timestamp = timestamp

    def as_tuple(self):
        return (
            self.session_id, self.message_index,
            self.operation, self.file_path, self.strength, self.timestamp,
        )


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


def _extract_file_ops(content) -> Iterator[tuple[str, str, int]]:
    """Yield ``(operation, file_path, strength)`` for each path-bearing
    tool_use.

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
        op_kind, input_field, strength = spec
        inp = block.get("input") or {}
        path = inp.get(input_field)
        if isinstance(path, str) and path.strip():
            yield (op_kind, path, strength)


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


def format_role_label(role: str, role_subtype: Optional[str]) -> str:
    """Render a (role, role_subtype) pair as the single combined label
    Phase 1 search expects (e.g. "AGENT:explore", or plain "USER" / "AI").

    Used by ``search.parse_jsonl_events`` so the JSONL path produces the
    same role-token grammar as the ``.convo`` / ``.sesslog`` parsers.
    """
    if role == "AGENT" and role_subtype:
        return f"AGENT:{role_subtype}"
    return role


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
                for op_kind, path, strength in _extract_file_ops(content):
                    yield FileOpRow(
                        session_id=session_id,
                        message_index=message_index,
                        operation=op_kind,
                        file_path=path,
                        strength=strength,
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

"""Distilled chat-log rendering (#12, v0.4.0).

The distilled output is an optional READING layer over the preserved
JSONL -- never a replacement. csb is full-recovery-first: the bytes stay
in git; this module just renders a session the way a human wants to read
it -- an instant-messenger-style log (AIM / MSN / Discord):

    2026/06/11 18:02:02 <User>:
    message text, linebreaks preserved, visible as a block



    2026/06/11 18:02:41 <Claude>:
    response text...

        [Read] C:/code/proj/file.py:120-180
        [Bash] git status

Generous blank-line separation makes speaker turns obvious at a glance;
tool lines are one-liners with Vim-jumpable ``path:start-end`` locations
(never tool output, never verbose argument dumps). Readable as Markdown
in Typora, navigable in Vim.

Reuse, not reinvention: conversation events come from the SAME parsers
``csb search`` uses (built to read claude-session-logger's channels and
the raw JSONL alike), so sessions distill with or without the logger
installed.

Channel preference is **jsonl > sesslog > convo** -- the OPPOSITE of
search. Distill is csb's own reading of csb's own preserved record:
the JSONL is the complete verbatim source of truth, while the logger's
convo channel is itself a derived distillation (distilling a
distillation would compound logger-side filtering, and logger users
would get different output than standalone users). Search prefers
convo for speed and pre-filtered matching; distill prefers fidelity
and universality. ``--source`` overrides for either taste.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from .search import Event, parse_jsonl_events, parse_log_blocks
from .sesslog_parser import iter_file_ops_from_sesslog

# Tool names rendered as shell one-liners (first line of the command).
_SHELL_TOOLS = {"Bash", "PowerShell"}

# Display labels for FileOpRow.operation values (sesslog + jsonl walkers
# share the operation vocabulary).
_OP_LABELS = {
    "read": "Read",
    "edited": "Edit",
    "wrote": "Write",
    "notebook_edit": "NotebookEdit",
    "searched": "Grep",
}

# Opening of ANY logger block: ``[[ts]] {Tool: ...`` or ``[[ts]] {Tool|agent: ...``.
# Used for the sesslog shell-command extraction (file ops go through
# sesslog_parser's richer machinery instead).
_SHELL_BLOCK_RE = re.compile(
    r"^\[\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\] "
    r"\{(?P<tool>Bash|PowerShell)(?:\|[A-Za-z0-9_.-]+)?:\s*(?P<body>.*)$"
)

# The four-newline speaker separation from the spec: enough blank lines
# that "one person's messages stop and another begins" is obvious.
_TURN_SEPARATOR = "\n\n\n\n"

# Claude Code plumbing that the raw JSONL stores as user events but no
# human reading a conversation wants: slash-command envelopes and local
# command output capture. The logger's convo channel already excludes
# these; the jsonl channel gets the same readability here.
_PLUMBING_PREFIXES = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<command-name>",
    "<command-message>",
)


def _is_plumbing(text: str) -> bool:
    return text.lstrip().startswith(_PLUMBING_PREFIXES)


@dataclass
class ToolLine:
    """One rendered tool one-liner: ``[label] target``."""

    ts_sort: float
    label: str
    target: str


@dataclass
class ChatMessage:
    """One speaker turn, with any tool lines that followed it."""

    ts_sort: float
    ts_display: str
    identity: str
    text: str
    tools: list[ToolLine] = field(default_factory=list)


# ── timestamp + identity normalization ──────────────────────────────────


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    """Parse either ISO-8601 (JSONL events, usually Z-suffixed) or the
    logger's ``YYYY-MM-DD hh:mm:ss`` (already local). Returns aware-or-
    naive datetime, or None."""
    if not ts:
        return None
    try:
        if "T" in ts:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.astimezone() if dt.tzinfo else dt
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _ts_pair(ts: Optional[str], fallback: float) -> tuple[float, str]:
    """(sortable epoch, display string). Messages with no timestamp
    inherit the previous one (the ``fallback``) so the grid stays
    monotonic."""
    dt = _parse_ts(ts)
    if dt is None:
        if fallback:
            return fallback, datetime.fromtimestamp(fallback).strftime(
                "%Y/%m/%d %H:%M:%S")
        return 0.0, "????/??/?? ??:??:??"
    return dt.timestamp(), dt.strftime("%Y/%m/%d %H:%M:%S")


def _identity(role: str) -> str:
    """USER -> User, AI -> Claude, AGENT[:subtype] -> Agent[:subtype]."""
    if role == "USER":
        return "User"
    if role == "AI":
        return "Claude"
    if role.startswith("AGENT"):
        return "Agent" + role[len("AGENT"):]
    return role.title()


# ── tool-line extraction ─────────────────────────────────────────────────


def _fileop_tool_line(row) -> Optional[ToolLine]:
    """FileOpRow -> ToolLine with Vim-style path[:lines] target."""
    label = _OP_LABELS.get(row.operation, row.operation.title())
    target = row.file_path or ""
    if not target:
        return None
    dt = _parse_ts(row.timestamp)
    return ToolLine(ts_sort=dt.timestamp() if dt else 0.0,
                    label=label, target=target)


def iter_sesslog_shell_lines(path: str) -> Iterator[ToolLine]:
    """Yield Bash / PowerShell one-liners from a .sesslog channel file.

    Only the command's FIRST line is kept (the spec: basic tools, not
    walls; never output)."""
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            m = _SHELL_BLOCK_RE.match(line.rstrip("\n"))
            if not m:
                continue
            body = m.group("body").strip()
            # Strip surrounding quotes / trailing brace of one-line blocks.
            body = body.rstrip("}").strip().strip('"').strip()
            first = body.splitlines()[0].strip() if body else ""
            if not first:
                continue
            dt = _parse_ts(m.group("ts"))
            yield ToolLine(ts_sort=dt.timestamp() if dt else 0.0,
                           label=m.group("tool"), target=first)


def iter_jsonl_shell_lines(path: str) -> Iterator[ToolLine]:
    """Yield Bash / PowerShell command one-liners from a raw JSONL.

    The shared walker intentionally omits shell tools from file-ops
    (commands aren't file paths), so distill extracts them here: every
    assistant ``tool_use`` block whose name is a shell tool contributes
    the first line of its ``command`` input."""
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "assistant":
                continue
            content = (event.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            ts = event.get("timestamp")
            dt = _parse_ts(ts)
            ts_sort = dt.timestamp() if dt else 0.0
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                if block.get("name") not in _SHELL_TOOLS:
                    continue
                cmd = (block.get("input") or {}).get("command") or ""
                first = cmd.splitlines()[0].strip() if cmd else ""
                if first:
                    yield ToolLine(ts_sort=ts_sort,
                                   label=block["name"], target=first)


# ── channel selection + event assembly ───────────────────────────────────


def pick_channels(
    source_rows: list,
    jsonl_abs: Optional[Path],
    source_override: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], list[str]]:
    """Decide (convo_source_type, convo_path, tool_paths).

    Conversation preference is **jsonl > sesslog > convo** (see module
    docstring: distill renders csb's own preserved record; the JSONL is
    the verbatim source of truth, the logger channels are themselves
    derivations). A sesslog is only used for conversation when it
    actually carries conversation blocks (the #36 probe philosophy).
    The tool stream follows the same preference: jsonl when present,
    else the sesslog channel.
    """
    by_type: dict[str, str] = {}
    for row in source_rows:
        by_type.setdefault(row["source_type"], row["source_path"])
    jsonl_path = str(jsonl_abs) if jsonl_abs and jsonl_abs.is_file() else None

    if source_override:
        if source_override == "jsonl":
            return ("jsonl", jsonl_path, [p for p in [jsonl_path] if p])
        path = by_type.get(source_override)
        tool_paths = [p for p in (jsonl_path, by_type.get("sesslog")) if p][:1]
        return (source_override, path, tool_paths)

    convo_type = convo_path = None
    if jsonl_path:
        convo_type, convo_path = "jsonl", jsonl_path
    elif "sesslog" in by_type and _has_convo_blocks(by_type["sesslog"]):
        convo_type, convo_path = "sesslog", by_type["sesslog"]
    elif "convo" in by_type:
        convo_type, convo_path = "convo", by_type["convo"]

    tool_paths: list[str] = []
    if jsonl_path:
        tool_paths.append(jsonl_path)
    elif "sesslog" in by_type:
        tool_paths.append(by_type["sesslog"])
    return (convo_type, convo_path, tool_paths)


def _has_convo_blocks(path: str) -> bool:
    """Cheap probe: does this log carry any USER/AI/AGENT block?"""
    for _ in parse_log_blocks(path):
        return True
    return False


def build_chat_messages(
    *,
    convo_type: Optional[str],
    convo_path: Optional[str],
    tool_paths: list[str],
    session_id: str,
    mode: str = "both",
) -> list[ChatMessage]:
    """Assemble the chat-log model: speaker turns with attached tools.

    Tool lines are attached to the latest message whose timestamp is
    <= the tool's (uniform across channels; JSONL/sesslog/convo all
    carry per-event timestamps).
    """
    messages: list[ChatMessage] = []
    if mode in ("convo", "both") and convo_path and convo_type:
        events: Iterator[Event]
        if convo_type == "jsonl":
            events = parse_jsonl_events(convo_path, session_id)
        else:
            events = parse_log_blocks(convo_path)
        last_ts = 0.0
        for ev in events:
            if _is_plumbing(ev.text):
                continue
            ts_sort, ts_disp = _ts_pair(ev.timestamp, last_ts)
            last_ts = ts_sort or last_ts
            messages.append(ChatMessage(
                ts_sort=ts_sort, ts_display=ts_disp,
                identity=_identity(ev.role), text=ev.text.rstrip(),
            ))

    tools: list[ToolLine] = []
    if mode in ("tools", "both"):
        for tp in tool_paths:
            if tp.endswith(".jsonl"):
                for row in _iter_jsonl_fileops(tp, session_id):
                    tl = _fileop_tool_line(row)
                    if tl:
                        tools.append(tl)
                tools.extend(iter_jsonl_shell_lines(tp))
            else:
                for row in iter_file_ops_from_sesslog(Path(tp), session_id):
                    tl = _fileop_tool_line(row)
                    if tl:
                        tools.append(tl)
                tools.extend(iter_sesslog_shell_lines(tp))
        tools.sort(key=lambda t: t.ts_sort)

    if not messages:
        # tools-only mode (or no conversation found): synthesize turn
        # headers from the tool timeline so the log still reads
        # "who did what when".
        for tl in tools:
            dt_disp = (datetime.fromtimestamp(tl.ts_sort)
                       .strftime("%Y/%m/%d %H:%M:%S") if tl.ts_sort
                       else "????/??/?? ??:??:??")
            messages.append(ChatMessage(
                ts_sort=tl.ts_sort, ts_display=dt_disp,
                identity="Claude", text="", tools=[tl],
            ))
        return messages

    if tools:
        # Attach each tool to the latest message at-or-before its time.
        import bisect
        ordered = sorted(messages, key=lambda m: m.ts_sort)
        times = [m.ts_sort for m in ordered]
        for tl in tools:
            i = bisect.bisect_right(times, tl.ts_sort) - 1
            if i < 0:
                i = 0
            ordered[i].tools.append(tl)

    if mode == "tools":
        # Keep only turns that carry tools; drop the message text.
        kept = [m for m in messages if m.tools]
        for m in kept:
            m.text = ""
        return kept
    return messages


def _iter_jsonl_fileops(path: str, session_id: str):
    """FileOpRow stream from a raw JSONL (walker yields both kinds)."""
    from .transcript_walker import FileOpRow, iter_rows_from_jsonl
    for row in iter_rows_from_jsonl(Path(path), session_id):
        if isinstance(row, FileOpRow):
            yield row


# ── renderer ─────────────────────────────────────────────────────────────


def render_chat_log(
    messages: list[ChatMessage],
    *,
    session_name: str = "",
    session_id: str = "",
    source_label: str = "",
    mode: str = "both",
) -> Iterator[str]:
    """Yield the distilled log line-groups (streaming-friendly).

    The header is a tiny provenance block; everything after it is the
    IM-style log. Message text is emitted VERBATIM (it is markdown-
    bearing prose; Typora renders it, Vim reads it) -- the chat-log
    frame is whitespace-structural, not markdown-structural.
    """
    title = session_name or session_id
    yield f"# {title}\n"
    yield (f"<!-- csb distill | session: {session_id} | source: "
           f"{source_label} | filter: {mode} -->\n\n\n")
    for msg in messages:
        yield f"{msg.ts_display} <{msg.identity}>:\n"
        if msg.text:
            yield msg.text + "\n"
        if msg.tools:
            if msg.text:
                yield "\n"
            for tl in msg.tools:
                yield f"    [{tl.label}] {tl.target}\n"
        yield _TURN_SEPARATOR

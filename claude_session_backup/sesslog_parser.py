"""
claude-session-logger output -> FileOpRow extraction.

Parses the structured block format that ``claude-session-logger`` writes
to its ``.sesslog_*`` / ``.tools_*`` / ``.fileio_*`` channels and yields
:class:`transcript_walker.FileOpRow` records (same shape FTS5 import
produces from JSONL). This closes the v0.3.x logger-side parity gap:
file-operation metadata is now derivable from ANY of the three sources
(JSONL, FTS5, logger output) without having to walk the raw JSONL.

Block format (multi-line OR single-line)::

    [[YYYY-MM-DD HH:MM:SS]] {<Tool>[|<AgentName>]: <body> }

Where ``<Tool>`` is one of ``Read`` / ``Edit`` / ``Write`` / ``MultiEdit``
/ ``NotebookEdit`` (path-bearing) plus the search-style tools ``Grep``
/ ``Glob`` (also path-bearing via ``in "..."`` syntax). Other tools
(``Bash``, ``Skill``, ``Agent``, ``WebSearch``, ``Task``) appear in the
same format but aren't file-ops -- the parser skips them.

Body shapes the parser recognizes::

    {Read: "path/to/file" }
    {Read: "path/to/file:128-157" }       (line range -- stripped)
    {Read|Explore: "path" }                (agent-attributed)
    {Edit: "path:143" <- "snippet..." (-8/+18L) }
    {Write: "path" <- "preview..." (258L) }
    {Glob: **/*.py in "C:\\path\\to\\dir" }
    {Grep|oracle: pattern | "**/*.md" in "private/" }
    {NotebookEdit: "path.ipynb" <- ... }

Strength matches the JSONL walker's tier (3 = active modify,
2 = passive read, 1 = search probe).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from .transcript_walker import FileOpRow


# Outer-block regex. The body group is non-greedy to the closing brace;
# multi-line bodies are joined before matching (see iter_file_ops_*).
_BLOCK_RE = re.compile(
    r'^\[\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\] '
    r'\{(?P<tool>[A-Za-z]+)(?:\|(?P<agent>[A-Za-z0-9_.-]+))?:\s*'
    r'(?P<body>.*?)\s*\}\s*$',
    re.DOTALL,
)


# Tool name -> (operation_kind, strength, body_parser_kind).
#
# strength matches transcript_walker._FILE_OP_TOOLS exactly:
#   3 = active modification, 2 = passive read, 1 = search probe.
# MultiEdit is logger-only (JSONL emits it as multiple Edit blocks).
# Glob is logger-only (JSONL doesn't track Glob in the FTS5 importer).
_FILE_OP_TOOLS: dict[str, tuple[str, int, str]] = {
    "Read":         ("read",          2, "first_quoted"),
    "Edit":         ("edited",        3, "first_quoted"),
    "Write":        ("wrote",         3, "first_quoted"),
    "MultiEdit":    ("edited",        3, "first_quoted"),
    "NotebookEdit": ("notebook_edit", 3, "first_quoted"),
    "Grep":         ("searched",      1, "in_path"),
    "Glob":         ("searched",      1, "in_path"),
}


# First double-quoted token in the body (used for Read/Edit/Write).
_FIRST_QUOTED_RE = re.compile(r'"(?P<path>[^"]+)"')

# Strip trailing line-range suffix the logger appends to Read targets.
# ":143" or ":128-157" -- single-line and multi-line forms.
_LINE_RANGE_SUFFIX = re.compile(r':\d+(?:-\d+)?$')

# Path target inside `<...> in "<path>"` (used for Grep/Glob).
_IN_PATH_RE = re.compile(r'\sin\s+"(?P<path>[^"]+)"')


def _parse_first_quoted(body: str) -> str | None:
    """Return the first double-quoted string in ``body``, with any
    trailing ``:line`` / ``:line-range`` stripped from the path.

    Used by Read / Edit / Write / NotebookEdit / MultiEdit: the logger
    writes the file path as the first quoted token in the body.
    """
    m = _FIRST_QUOTED_RE.search(body)
    if not m:
        return None
    return _LINE_RANGE_SUFFIX.sub("", m.group("path"))


def _parse_in_path(body: str) -> str | None:
    """Return the path target from a ``... in "<path>"`` body.

    Used by Grep / Glob. If no ``in "..."`` clause is present (e.g.
    Grep with implicit cwd), falls back to the first quoted token --
    matches what the logger emits when the search has no explicit
    path filter.
    """
    m = _IN_PATH_RE.search(body)
    if m:
        return m.group("path")
    # Fallback: some Grep entries have just the pattern, no explicit
    # path. Return None rather than misinterpreting the pattern as a
    # path.
    return None


def iter_file_ops_from_sesslog(
    sesslog_path: Path,
    session_id: str,
) -> Iterator[FileOpRow]:
    """Stream FileOpRow records from a ``.sesslog_*`` / ``.tools_*`` /
    ``.fileio_*`` file.

    Handles both single-line and multi-line blocks (the logger uses
    multi-line for content previews with embedded newlines). Lines
    outside any recognized block (timestamps, session-start banners,
    blank lines, etc.) are silently skipped. Malformed blocks are
    skipped without raising.

    ``message_index`` is a 0-based per-source counter that increments
    every emitted row -- NOT a JSONL message index, so it's not
    join-comparable with the FTS5 importer's row numbers. The
    file_operations table's UNIQUE constraint
    (session_id, message_index, operation, file_path) means same-
    session imports from JSONL and sesslog won't conflict on identical
    paths because their indices live in different ranges; in practice
    only one source feeds the FTS5 DB at a time.
    """
    try:
        f = open(sesslog_path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return

    try:
        message_index = 0
        # Accumulator for multi-line blocks: when an opening `{Tool: `
        # line doesn't close with `}` on the same line, we keep
        # appending until we see a closing brace at column 0 or end.
        buffer: list[str] = []
        in_block = False

        for line in f:
            line = line.rstrip("\n").rstrip("\r")

            if not in_block:
                if line.startswith("[[") and "{" in line and "}" in line:
                    # Single-line block -- match directly.
                    row = _maybe_emit(line, session_id, message_index)
                    if row is not None:
                        yield row
                        message_index += 1
                elif line.startswith("[[") and "{" in line:
                    # Opening of a multi-line block.
                    buffer = [line]
                    in_block = True
                # else: ignore (banner, blank line, etc.)
            else:
                buffer.append(line)
                if line.rstrip() == "}" or line.endswith(" }"):
                    joined = "\n".join(buffer)
                    row = _maybe_emit(joined, session_id, message_index)
                    if row is not None:
                        yield row
                        message_index += 1
                    buffer = []
                    in_block = False
    finally:
        f.close()


def _maybe_emit(
    block_text: str,
    session_id: str,
    message_index: int,
) -> FileOpRow | None:
    """Parse a single (possibly multi-line) block and emit a FileOpRow
    if the tool is path-bearing AND a path was extractable.

    Returns None for non-file-op tools (Bash / Skill / Agent / etc.)
    and for malformed bodies where no path could be parsed.
    """
    m = _BLOCK_RE.match(block_text)
    if not m:
        return None
    tool = m.group("tool")
    spec = _FILE_OP_TOOLS.get(tool)
    if not spec:
        return None
    op_kind, strength, parser_kind = spec
    body = m.group("body") or ""
    if parser_kind == "first_quoted":
        path = _parse_first_quoted(body)
    else:  # "in_path"
        path = _parse_in_path(body)
    if not path:
        return None
    return FileOpRow(
        session_id=session_id,
        message_index=message_index,
        operation=op_kind,
        file_path=path,
        strength=strength,
        timestamp=m.group("ts"),
    )


# ── Sub-channel discovery ────────────────────────────────────────────


# Filename prefixes the logger writes that carry file-op metadata.
# Used by callers (search, csb files, etc.) to find all the channels
# a single session's logger output is spread across.
SESSLOG_FILEOP_CHANNELS = (
    ".sesslog_",   # kitchen-sink, includes file-ops in compact form
    ".tools_",     # AI activity, includes file-ops with longer previews
    ".fileio_",    # opt-in full file I/O channel
)


def find_fileop_channels_for_session(
    session_dir: Path,
) -> list[Path]:
    """List every logger output file in ``session_dir`` that carries
    file-op metadata.

    The logger names files as ``.<channel>_<subtype>__<session-tag>.log``
    where channel is one of :data:`SESSLOG_FILEOP_CHANNELS`. Each
    session typically has 1-3 such files depending on what tools the
    user ran.

    Returns paths sorted by name for deterministic iteration. Returns
    an empty list if the session directory doesn't exist or contains
    no logger output.
    """
    if not session_dir.is_dir():
        return []
    matches: list[Path] = []
    for entry in session_dir.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if any(name.startswith(p) for p in SESSLOG_FILEOP_CHANNELS):
            matches.append(entry)
    matches.sort(key=lambda p: p.name)
    return matches


# ── Logger presence detection ────────────────────────────────────────


def has_session_logger(main_conn) -> bool:
    """True if the main DB has ever recorded a logger-produced source
    (``convo`` or ``sesslog``).

    O(1) check via ``LIMIT 1``. The dispatcher uses this to decide
    whether to include logger sources in the default preference walk.
    Users without the logger (no ``.sesslog_*`` / ``.convo_*`` files
    on disk) get a leaner default preference (fts5 -> jsonl).
    """
    row = main_conn.execute(
        "SELECT 1 FROM session_sources "
        "WHERE source_type IN ('convo', 'sesslog') LIMIT 1"
    ).fetchone()
    return row is not None

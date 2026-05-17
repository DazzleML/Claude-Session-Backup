"""
Discover transcript source files for csb's content search.

Two responsibilities:

1. ``list_sesslog_folders(claude_dir)`` -- walk ``~/.claude/sesslogs/`` once
   and return ``{session_uuid: folder_path}``. Sesslog folder names follow
   the convention ``<SessionName>__<UUID>_<USER>`` defined by
   claude-session-logger; the UUID is captured by ``_UUID_SUFFIX_RE``.

2. ``list_session_sources(jsonl_path, sesslog_folder)`` -- for one session,
   enumerate every searchable transcript file: ``.convo_*.log``,
   ``.sesslog_*.log``, and the JSONL itself. Filters out tool-call
   sub-channels (``.sesslog-bash_``, ``.sesslog-grep_``, etc.) and
   ancillary files (``.shell_*``, ``.tasks_*``, ``.agents_*``,
   ``.overflow_*``).

Source rows are returned ready for ``register_session_sources()``.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Sesslog folder name pattern: <anything>__<uuid>_<user>
# The UUID is the canonical 8-4-4-4-12 hex format and is the only stable
# join key between a sesslog folder and a Claude Code session.
_UUID_SUFFIX_RE = re.compile(
    r"__([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_[^_]+$",
    re.IGNORECASE,
)


# File-name patterns we recognize as searchable USER/AI transcript channels.
# Note the trailing underscore distinguishes ``.sesslog_`` (the main channel
# that contains USER/AI tags) from ``.sesslog-bash_`` (a tool-call subchannel).
_CONVO_RE = re.compile(r"^\.convo_.+\.log$", re.IGNORECASE)
_SESSLOG_MAIN_RE = re.compile(r"^\.sesslog_.+\.log$", re.IGNORECASE)


@dataclass
class SourceRow:
    """One row destined for the ``session_sources`` table."""

    source_type: str  # 'convo' | 'sesslog' | 'jsonl'
    source_path: str  # absolute path on disk
    size_bytes: Optional[int]
    mtime: Optional[str]  # ISO 8601 UTC; None if file is missing


def _extract_uuid(folder_name: str) -> Optional[str]:
    """Pull the session UUID out of a sesslog folder name, or None."""
    m = _UUID_SUFFIX_RE.search(folder_name)
    return m.group(1).lower() if m else None


def list_sesslog_folders(claude_dir: Path) -> dict[str, Path]:
    """Return ``{session_uuid: folder_path}`` for every sesslog folder found.

    Walks ``<claude_dir>/sesslogs/`` non-recursively. Folders whose names
    don't match the ``__<UUID>_<USER>`` suffix pattern are skipped. If a
    UUID appears in multiple folder names (rare, can happen during a rename
    race), the lexicographically later folder wins -- the assumption is
    that the rename produced a newer name with the same UUID.
    """
    sesslogs_root = Path(claude_dir) / "sesslogs"
    if not sesslogs_root.is_dir():
        return {}

    out: dict[str, Path] = {}
    for entry in sorted(sesslogs_root.iterdir()):
        if not entry.is_dir():
            continue
        uuid = _extract_uuid(entry.name)
        if uuid is None:
            continue
        out[uuid] = entry
    return out


def _stat_source(path: Path, source_type: str) -> SourceRow:
    """Build a SourceRow from a path, capturing size/mtime if reachable."""
    try:
        st = path.stat()
        from datetime import datetime, timezone
        size = st.st_size
        mtime = (
            datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            .isoformat(timespec="seconds")
        )
    except OSError:
        size = None
        mtime = None
    return SourceRow(
        source_type=source_type,
        source_path=str(path),
        size_bytes=size,
        mtime=mtime,
    )


def list_session_sources(
    jsonl_path: Path,
    sesslog_folder: Optional[Path],
) -> list[SourceRow]:
    """Enumerate every searchable transcript file for one session.

    Returns a list of :class:`SourceRow` ready to insert into the
    ``session_sources`` table. The JSONL is always present in the result
    (whether or not the file currently exists -- the row records the path
    that the session *should* live at; existence is checked at search time).
    Sesslog-folder sources are added if a folder was provided and the file
    names match the recognized USER/AI channels.

    Notably skipped (tool-call subchannels and ancillary files):

    - ``.sesslog-bash_*.log``, ``.sesslog-grep_*.log``,
      ``.sesslog-glob_*.log``, ``.sesslog-powershell_*.log`` -- sub-channels
    - ``.shell_*``, ``.shell-*`` -- raw shell output channels
    - ``.tasks_*``, ``.agents_*`` -- task/agent ancillary channels
    - ``.overflow_*`` -- rotation/migration markers, not content
    """
    rows: list[SourceRow] = []

    if sesslog_folder is not None and sesslog_folder.is_dir():
        for entry in sorted(sesslog_folder.iterdir()):
            if not entry.is_file():
                continue
            name = entry.name
            if _CONVO_RE.match(name):
                rows.append(_stat_source(entry, "convo"))
            elif _SESSLOG_MAIN_RE.match(name):
                rows.append(_stat_source(entry, "sesslog"))
            # everything else is ignored on purpose

    # The JSONL is always recorded -- it's the authoritative fallback even
    # if the file is missing right now (search will surface that case).
    rows.append(_stat_source(Path(jsonl_path), "jsonl"))

    return rows

"""
Session scanner -- discovers Claude Code sessions in ~/.claude/projects/.

Walks the projects directory and identifies session JSONL files,
their associated directories (subagents, tool-results), and
session-state metadata files.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass
class SessionFiles:
    """All files associated with a single Claude Code session."""
    session_id: str
    project: str  # sanitized project path (e.g., "C--code-ext")
    jsonl_path: Path  # path to the .jsonl transcript
    session_dir: Optional[Path] = None  # path to session UUID directory (subagents, tool-results)
    state_file: Optional[Path] = None  # session-states/{uuid}.json
    name_cache: Optional[Path] = None  # session-states/{uuid}.name-cache
    file_history_dir: Optional[Path] = None  # file-history/{uuid}/
    jsonl_size: int = 0
    jsonl_mtime: float = 0.0
    related_files: list = field(default_factory=list)


def scan_projects(claude_dir: str) -> list[SessionFiles]:
    """
    Scan ~/.claude/projects/ for all session JSONL files.

    Returns a list of SessionFiles, one per discovered session.
    """
    claude_path = Path(claude_dir)
    projects_dir = claude_path / "projects"
    states_dir = claude_path / "session-states"
    history_dir = claude_path / "file-history"

    if not projects_dir.exists():
        return []

    sessions = []

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue

        project_name = project_dir.name

        for item in project_dir.iterdir():
            if not item.is_file() or item.suffix != ".jsonl":
                continue

            session_id = item.stem
            if not UUID_PATTERN.match(session_id):
                continue

            sf = SessionFiles(
                session_id=session_id,
                project=project_name,
                jsonl_path=item,
            )

            # Get file stats
            try:
                stat = item.stat()
                sf.jsonl_size = stat.st_size
                sf.jsonl_mtime = stat.st_mtime
            except OSError:
                pass

            # Check for session directory (subagents, tool-results)
            session_subdir = project_dir / session_id
            if session_subdir.is_dir():
                sf.session_dir = session_subdir

            # Check for session-states files
            state_file = states_dir / f"{session_id}.json"
            if state_file.exists():
                sf.state_file = state_file

            name_cache = states_dir / f"{session_id}.name-cache"
            if name_cache.exists():
                sf.name_cache = name_cache

            # Check for file-history
            hist_dir = history_dir / session_id
            if hist_dir.is_dir():
                sf.file_history_dir = hist_dir

            sessions.append(sf)

    return sessions


def scan_session_states(claude_dir: str) -> dict[str, Path]:
    """
    Scan ~/.claude/session-states/ for all known session IDs.

    Returns a dict of session_id -> state_file_path.
    This includes sessions that may no longer have JSONL files.
    """
    states_dir = Path(claude_dir) / "session-states"
    if not states_dir.exists():
        return {}

    known = {}
    for f in states_dir.iterdir():
        if f.suffix == ".json" and UUID_PATTERN.match(f.stem):
            known[f.stem] = f

    return known


def sanitize_path(path: str) -> str:
    """
    Convert a filesystem path to Claude Code's project folder name format.

    Claude Code stores projects in ~/.claude/projects/ using sanitized names:
      C:\\code         -> C--code
      C:\\code\\project -> C--code-project
      /home/user/code -> -home-user-code
    """
    p = str(Path(path).resolve())

    # Windows: C:\foo\bar -> C--foo-bar
    # The drive letter colon+backslash becomes drive_letter + "--"
    if len(p) >= 2 and p[1] == ":":
        drive = p[0]
        rest = p[2:]  # skip the ":"
        # Replace both / and \ with -
        rest = rest.replace("\\", "-").replace("/", "-")
        # Remove leading separator (the one right after colon)
        if rest.startswith("-"):
            rest = rest[1:]
        return f"{drive}--{rest}"

    # Unix: /home/user/code -> -home-user-code
    return p.replace("/", "-")


def scan_for_path(claude_dir: str, target_path: str) -> list[SessionFiles]:
    """
    Find all sessions in ~/.claude/projects/ whose project path starts with target_path.

    Uses Claude Code's sanitized project folder naming to do a fast prefix match
    against the projects directory -- no filesystem walking needed.

    Returns a list of SessionFiles sorted by JSONL modification time (newest first).
    """
    projects_dir = Path(claude_dir) / "projects"
    if not projects_dir.exists():
        return []

    # Sanitize the target path to match Claude Code's folder naming
    prefix = sanitize_path(target_path)

    # Find all project folders that match:
    #   - exact match (sessions started in this exact directory)
    #   - prefix + "-" (sessions started in child directories)
    all_sessions = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        name = project_dir.name
        if name == prefix or name.startswith(prefix + "-"):
            # Scan this project for sessions
            sessions = _scan_project_dir(claude_dir, project_dir)
            all_sessions.extend(sessions)

    # Sort by modification time, newest first
    all_sessions.sort(key=lambda s: s.jsonl_mtime, reverse=True)
    return all_sessions


def _scan_project_dir(claude_dir: str, project_dir: Path) -> list[SessionFiles]:
    """Scan a single project directory for session JSONL files."""
    claude_path = Path(claude_dir)
    states_dir = claude_path / "session-states"
    history_dir = claude_path / "file-history"
    project_name = project_dir.name
    sessions = []

    for item in project_dir.iterdir():
        if not item.is_file() or item.suffix != ".jsonl":
            continue

        session_id = item.stem
        if not UUID_PATTERN.match(session_id):
            continue

        sf = SessionFiles(
            session_id=session_id,
            project=project_name,
            jsonl_path=item,
        )

        try:
            stat = item.stat()
            sf.jsonl_size = stat.st_size
            sf.jsonl_mtime = stat.st_mtime
        except OSError:
            pass

        session_subdir = project_dir / session_id
        if session_subdir.is_dir():
            sf.session_dir = session_subdir

        state_file = states_dir / f"{session_id}.json"
        if state_file.exists():
            sf.state_file = state_file

        name_cache = states_dir / f"{session_id}.name-cache"
        if name_cache.exists():
            sf.name_cache = name_cache

        hist_dir = history_dir / session_id
        if hist_dir.is_dir():
            sf.file_history_dir = hist_dir

        sessions.append(sf)

    return sessions


def desanitize_project_path(sanitized: str) -> str:
    """
    Convert sanitized project name back to a filesystem path.

    'C--code-ext' -> 'C:\\code-ext' (Windows)
    '-home-user-project' -> '/home/user/project' (Unix)
    """
    # Handle Windows drive letter pattern: C-- -> C:\\
    if len(sanitized) >= 3 and sanitized[1:3] == "--":
        drive = sanitized[0].upper()
        rest = sanitized[3:].replace("-", os.sep)
        return f"{drive}:{os.sep}{rest}"

    # Unix paths start with -
    if sanitized.startswith("-"):
        return sanitized.replace("-", os.sep)

    return sanitized.replace("-", os.sep)

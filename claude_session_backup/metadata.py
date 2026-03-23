"""
Metadata extractor -- parses JSONL transcripts and session-state files
to extract session name, timestamps, working directory usage, and other metadata.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class SessionMetadata:
    """Extracted metadata for a single session."""
    session_id: str
    session_name: Optional[str] = None
    project: str = ""
    start_folder: Optional[str] = None
    started_at: Optional[str] = None  # ISO 8601 -- first event timestamp
    last_active_at: Optional[str] = None  # ISO 8601 -- last event timestamp (any type)
    last_user_at: Optional[str] = None  # ISO 8601 -- last user-type message timestamp
    message_count: int = 0
    tool_call_count: int = 0
    claude_version: Optional[str] = None
    folder_usage: dict[str, int] = field(default_factory=dict)  # path -> count


def extract_metadata(jsonl_path: Path, top_n_folders: int = 3) -> SessionMetadata:
    """
    Parse a session JSONL file and extract metadata.

    Streams the file line-by-line to handle large files efficiently.
    """
    meta = SessionMetadata(session_id=jsonl_path.stem)

    folder_counter = Counter()
    first_cwd = None
    first_ts = None
    last_ts = None
    last_user_ts = None
    msg_count = 0
    tool_count = 0
    version = None
    session_name = None

    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Extract session name from custom-title event
                if event.get("type") == "custom-title":
                    session_name = event.get("customTitle")

                # Track timestamps
                ts = event.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                # Track working directories
                cwd = event.get("cwd")
                if cwd:
                    if first_cwd is None:
                        first_cwd = cwd
                    folder_counter[cwd] += 1

                # Track Claude Code version
                v = event.get("version")
                if v and version is None:
                    version = v

                # Count messages and tool calls
                evt_type = event.get("type")
                if evt_type in ("user", "assistant"):
                    msg_count += 1
                if evt_type == "user" and ts:
                    last_user_ts = ts
                elif evt_type == "tool_use":
                    tool_count += 1

                # Also count tool calls embedded in message content
                if evt_type == "assistant":
                    content = event.get("message", {}).get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_count += 1

    except OSError:
        pass

    meta.session_name = session_name
    meta.start_folder = first_cwd
    meta.started_at = first_ts
    meta.last_active_at = last_ts
    meta.last_user_at = last_user_ts
    meta.message_count = msg_count
    meta.tool_call_count = tool_count
    meta.claude_version = version

    # Build folder usage: start_folder tracked separately,
    # plus top N other folders
    if first_cwd:
        meta.folder_usage[first_cwd] = folder_counter.get(first_cwd, 0)

    other_folders = {
        path: count
        for path, count in folder_counter.most_common()
        if path != first_cwd
    }
    for path, count in list(other_folders.items())[:top_n_folders]:
        meta.folder_usage[path] = count

    return meta


def read_session_state(state_path: Path) -> dict:
    """Read a session-states/{uuid}.json file and return its contents."""
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def read_name_cache(name_cache_path: Path) -> Optional[str]:
    """Read a session-states/{uuid}.name-cache file."""
    try:
        return name_cache_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def enrich_metadata(meta: SessionMetadata, state: dict, name_cache: Optional[str] = None):
    """
    Enrich metadata with info from session-states files.

    If session name wasn't found in JSONL (custom-title event),
    fall back to name-cache or session-state current_name.
    """
    if not meta.session_name:
        if name_cache:
            meta.session_name = name_cache
        elif state.get("current_name"):
            meta.session_name = state["current_name"]

    if not meta.start_folder and state.get("original_cwd"):
        meta.start_folder = state["original_cwd"]

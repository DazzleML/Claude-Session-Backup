"""
Metadata extractor -- parses JSONL transcripts and session-state files
to extract session name, timestamps, working directory usage, and other metadata.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import pathlevels, toolpaths
from .epochs import parse_index_ts

# Sitting split threshold (#80 S1): a gap longer than this between event
# timestamps ends one activity segment and starts the next. The value is
# COMPRESSION, not correctness -- membership truth is "any activity in
# the window", and segments blur only sub-threshold gaps. At 10 minutes,
# falsely bridging an entire epoch would require messaging within 10
# minutes on both sides of a full reboot. (User review tightened this
# from 60; row cost is one per sitting either way.)
ACTIVITY_GAP_MINUTES = 10

_SEGMENT_ISO = "%Y-%m-%dT%H:%M:%SZ"


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
    # path -> WORK UNITS (#56). One unit per tool call, credited to the
    # folder that call actually operated in -- NOT transcript events.
    # Counting events made the launch dir unbeatable (a transcript carries
    # ~5x more events than tool calls), which is why a session working in
    # another repo reported its launch folder as "primary" with a
    # four-digit count. A folder touched only as a call's SECONDARY is
    # present here with a count of 0: findable by `csb scan`, uncredited
    # for ranking.
    folder_usage: dict[str, int] = field(default_factory=dict)
    # Schema v8: how each folder entered the index -- `cd` (the shell
    # stood there), `structured`, `relative`, or `extracted`. Feeds the
    # path-exposure rung derivation; coldest-wins when a folder arrives
    # by several routes across the session.
    folder_provenance: dict[str, str] = field(default_factory=dict)
    # Set when tool-path harvesting ran. None means never harvested, which
    # consumers MUST render as "unmeasured" rather than "no folders".
    tool_paths_extracted: bool = False
    # Total successfully-parsed JSON events (any type). 0 means the file
    # parsed into nothing -- i.e. it's not a real transcript (e.g. a stub or
    # a symlink-target path string). Used by the restore-verify gate (v0.3.16)
    # to decide whether a reappeared JSONL is a genuine revival.
    event_count: int = 0
    # Fork lineage (v0.7.0, #22/#31). Populated from the first
    # ``compact_boundary`` event carrying a top-level ``forkedFrom`` --
    # the signature Claude Code writes when a session is forked
    # (``/branch``, ``/rewind``-continue, ``claude --fork-session -r``).
    # All None/0 for a root session.
    parent_session_id: Optional[str] = None
    parent_message_uuid: Optional[str] = None
    forked_at: Optional[str] = None  # the boundary event's own timestamp
    is_fork: bool = False
    # Activity segments (#80): (seg_start, seg_end, messages) ISO-Z
    # tuples, one per sitting, oldest-first. ``activity_extracted`` is
    # the S2b guard -- True only when the streaming parse actually ran,
    # so narrower meta paths can never wipe indexed segments.
    activity_segments: list[tuple[str, str, int]] = field(default_factory=list)
    activity_extracted: bool = False


def _parse_jsonl_lines(lines, meta: SessionMetadata) -> SessionMetadata:
    """Apply per-event JSONL parsing onto a ``SessionMetadata`` instance.

    Shared by ``extract_metadata`` (file-streaming) and
    ``extract_metadata_from_bytes`` (in-memory bytes from git show).
    Mutates and returns ``meta`` for chaining.
    """
    folder_counter: Counter = Counter()   # folder -> WORK UNITS (#56)
    touched: set = set()                  # folders touched at all (presence)
    provenance: dict = {}                 # folder -> coldest route (v8)
    seen_cwds: set = set()                # distinct session cwds observed
    activity_points: list = []            # (utc datetime, is_message) (#80)
    first_cwd = None
    first_ts = None
    last_ts = None
    last_user_ts = None
    msg_count = 0
    tool_count = 0
    event_count = 0
    version = None
    session_name = None
    fork_parent = None
    fork_message_uuid = None
    fork_at = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Count every successfully-parsed JSON event. event_count == 0 after
        # the loop means the file is not a real transcript (stub / garbage).
        event_count += 1

        # Extract session name from custom-title event
        if event.get("type") == "custom-title":
            session_name = event.get("customTitle")

        # Fork lineage (v0.7.0): a forked session's boundary row carries a
        # top-level forkedFrom pointer at its parent. Take the FIRST one --
        # it is the fork that minted THIS file. In practice it is line 1,
        # but we don't assume position: some sessions lead with
        # custom-title / agent-name rows. In-place compaction and
        # microcompact write boundary rows WITHOUT forkedFrom, and /clear
        # mints a fresh UUID with no boundary at all -- neither is a fork,
        # and both are correctly skipped by the forkedFrom requirement.
        if fork_parent is None and event.get("subtype") == "compact_boundary":
            forked = event.get("forkedFrom")
            if isinstance(forked, dict) and forked.get("sessionId"):
                fork_parent = forked.get("sessionId")
                fork_message_uuid = forked.get("messageUuid")
                fork_at = event.get("timestamp")

        # Track timestamps
        ts = event.get("timestamp")
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            # Activity fold input (#80): every timestamped event marks
            # the session ALIVE at that instant; user/assistant events
            # additionally count toward the sitting's message tally.
            ts_dt = parse_index_ts(ts)
            if ts_dt is not None:
                activity_points.append(
                    (ts_dt, event.get("type") in ("user", "assistant")))

        # Track the session's nominal working directory. This is the LAUNCH
        # directory (Claude Code only ever follows it into descendants), so
        # it identifies where the session STARTED -- it is no longer used as
        # a usage COUNT, because counting one tick per event made it
        # unbeatable regardless of where work happened (#56).
        cwd = event.get("cwd")
        if cwd:
            if first_cwd is None:
                first_cwd = cwd
            seen_cwds.add(cwd)

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

        # Also count tool calls embedded in message content -- and harvest
        # the folders each call worked in (#56). This is the ONLY source of
        # cross-repo work locations: `cwd` never leaves the launch tree, so
        # a session editing another repository is otherwise invisible to
        # `csb scan`.
        if evt_type == "assistant":
            content = event.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_count += 1
                        name = block.get("name") or ""
                        tool_input = block.get("input")
                        # PRESENCE: every folder the call touched, so scan
                        # can find the session (33% of folders are never
                        # any call's primary -- dropping them would
                        # recreate the bug this fixes). Provenance rides
                        # along (v8), coldest route winning across calls.
                        pairs = toolpaths.touched_with_provenance(
                            name, tool_input, cwd)
                        for folder, prov in pairs.items():
                            touched.add(folder)
                            provenance[folder] = pathlevels.merge_provenance(
                                provenance.get(folder), prov)
                        # WORK: exactly one unit per call, credited where
                        # the call actually operated.
                        primary = toolpaths.primary_folder(name, tool_input, cwd)
                        if primary:
                            folder_counter[primary] += 1

    meta.session_name = session_name
    meta.start_folder = first_cwd
    meta.started_at = first_ts
    meta.last_active_at = last_ts
    meta.last_user_at = last_user_ts
    meta.message_count = msg_count
    meta.tool_call_count = tool_count
    meta.event_count = event_count
    meta.claude_version = version
    meta.parent_session_id = fork_parent
    meta.parent_message_uuid = fork_message_uuid
    meta.forked_at = fork_at
    meta.is_fork = fork_parent is not None

    # The session cwd is a place work happened too -- calls that name no
    # path ran there -- so make sure it is representable even when the
    # transcript had no tool calls at all.
    for c in seen_cwds:
        key = toolpaths.normalize(c)
        if key and toolpaths.is_plausible_folder(key):
            touched.add(key)
            # The ambient cwd is a position fact: the shell stood there.
            provenance[key] = pathlevels.merge_provenance(
                provenance.get(key), "cd")

    # Work locations first (descending), so SQLite's ORDER BY matches the
    # in-memory ordering. Then PRESENCE-only rows at 0: touched but never
    # any call's primary. They rank nowhere and are found by `csb scan`.
    for path, count in folder_counter.most_common():
        meta.folder_usage[path] = count
    for path in sorted(touched):
        meta.folder_usage.setdefault(path, 0)
    for path in meta.folder_usage:
        meta.folder_provenance[path] = provenance.get(path, "extracted")

    meta.tool_paths_extracted = True

    # Fold activity points into sittings (#80 S1). SORT FIRST (S2a):
    # transcript order is only NEAR-chronological -- a forked session's
    # file leads with inherited pre-fork events -- and a streaming fold
    # over unsorted points would mint spurious or overlapping segments.
    activity_points.sort(key=lambda point: point[0])
    gap_seconds = ACTIVITY_GAP_MINUTES * 60
    segments: list = []  # [start_dt, end_dt, messages]
    for point_ts, is_message in activity_points:
        if segments and (point_ts - segments[-1][1]).total_seconds() \
                <= gap_seconds:
            segments[-1][1] = point_ts
            if is_message:
                segments[-1][2] += 1
        else:
            segments.append([point_ts, point_ts, 1 if is_message else 0])
    meta.activity_segments = [
        (seg_start.strftime(_SEGMENT_ISO), seg_end.strftime(_SEGMENT_ISO),
         seg_messages)
        for seg_start, seg_end, seg_messages in segments
    ]
    meta.activity_extracted = True

    return meta


def extract_metadata(jsonl_path: Path) -> SessionMetadata:
    """
    Parse a session JSONL file and extract metadata.

    Streams the file line-by-line to handle large files efficiently.

    Stores ALL distinct cwds seen in the JSONL events into ``folder_usage``
    (keyed by absolute path, valued by occurrence count). The renderer
    decides how many to display via ``--top N`` / ``--all-folders``;
    truncating at index time would make those flags unable to surface
    long-tail folders, see #21.
    """
    meta = SessionMetadata(session_id=jsonl_path.stem)

    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            _parse_jsonl_lines(f, meta)
    except OSError:
        pass

    return meta


def extract_metadata_from_bytes(blob: bytes, session_id: str,
                                 project: str = "") -> SessionMetadata:
    """
    Parse a session JSONL blob (in-memory bytes) and extract metadata.

    Same per-event parsing as ``extract_metadata`` but takes raw bytes
    rather than a file path. Used by ``cmd_backfill_deleted`` to
    reconstruct metadata for sessions whose JSONL has been culled from
    disk -- the bytes come from ``git show <commit>:<path>``.

    The session UUID is taken from the ``session_id`` parameter, NOT
    inferred from any filename or JSONL content (the JSONL itself
    doesn't reliably carry the session UUID -- it's always the filename
    in the live FS path). Caller supplies it from the path being read.

    Args:
      blob: raw JSONL bytes (UTF-8)
      session_id: the session UUID (from the historical jsonl_path)
      project: project slug (from the historical jsonl_path)

    Returns:
      A ``SessionMetadata`` with all observable fields populated.
      Malformed lines are silently skipped (matches ``extract_metadata``).
    """
    meta = SessionMetadata(session_id=session_id, project=project)
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        text = blob.decode("utf-8", errors="replace")
    _parse_jsonl_lines(text.splitlines(), meta)
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

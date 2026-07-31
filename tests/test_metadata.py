"""Tests for metadata extraction."""

import json
from pathlib import Path

from claude_session_backup.metadata import extract_metadata, enrich_metadata


def test_extract_metadata(mock_claude_dir):
    project_dir = mock_claude_dir / "projects" / "C--code-test"
    jsonl = project_dir / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"

    meta = extract_metadata(jsonl)

    assert meta.session_name == "test-session"
    assert meta.start_folder == "/home/user/code"
    assert meta.started_at == "2026-03-23T10:00:00Z"
    assert meta.last_active_at == "2026-03-23T10:02:05Z"
    assert meta.message_count == 6  # 3 user + 3 assistant
    assert meta.claude_version == "2.1.81"

    # Folder analysis. Since #56 the cwds a session sat in are recorded for
    # PRESENCE (so `csb scan` finds the session) but carry no work units of
    # their own -- work is credited per TOOL CALL, and this fixture has
    # none. Folder keys are normalized to the backslash form the index
    # matches on, so a POSIX cwd appears as `\home\user\code`.
    assert "\\home\\user\\code" in meta.folder_usage
    assert "\\home\\user\\other-project" in meta.folder_usage
    # No tool calls -> no work anywhere. The old assertion here was
    # `== 3`, an EVENT count: it made a folder's rank a function of how
    # many messages happened while sitting in it, which is why the launch
    # directory used to be unbeatable (#56).
    assert meta.folder_usage["\\home\\user\\other-project"] == 0


def test_extract_empty_file(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    meta = extract_metadata(empty)
    assert meta.session_name is None
    assert meta.message_count == 0


def test_extract_malformed_json(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n{invalid\n", encoding="utf-8")

    meta = extract_metadata(bad)
    assert meta.message_count == 0  # graceful degradation


def test_enrich_from_state():
    from claude_session_backup.metadata import SessionMetadata

    meta = SessionMetadata(session_id="test")
    state = {"current_name": "from-state", "original_cwd": "/original/path"}
    enrich_metadata(meta, state)

    assert meta.session_name == "from-state"
    assert meta.start_folder == "/original/path"


def test_enrich_preserves_existing_name():
    from claude_session_backup.metadata import SessionMetadata

    meta = SessionMetadata(session_id="test", session_name="from-jsonl")
    state = {"current_name": "from-state"}
    enrich_metadata(meta, state)

    assert meta.session_name == "from-jsonl"  # JSONL takes precedence


def test_extract_metadata_keeps_all_folders(tmp_path):
    """The indexer must persist every distinct cwd, not just the top-N -- the
    --top N / --all-folders renderer flags rely on the data being present."""
    jsonl = tmp_path / "test.jsonl"
    events = [
        # 6 distinct cwds, seen a differing number of times each
        *[{"type": "user", "timestamp": "2026-05-01T10:00:00Z", "cwd": "/home/a", "uuid": f"u{i}"} for i in range(5)],
        *[{"type": "user", "timestamp": "2026-05-01T10:00:00Z", "cwd": "/home/b", "uuid": f"v{i}"} for i in range(4)],
        *[{"type": "user", "timestamp": "2026-05-01T10:00:00Z", "cwd": "/home/c", "uuid": f"w{i}"} for i in range(3)],
        *[{"type": "user", "timestamp": "2026-05-01T10:00:00Z", "cwd": "/home/d", "uuid": f"x{i}"} for i in range(2)],
        {"type": "user", "timestamp": "2026-05-01T10:00:00Z", "cwd": "/home/e", "uuid": "y0"},
        {"type": "user", "timestamp": "2026-05-01T10:00:00Z", "cwd": "/home/f", "uuid": "z0"},
    ]
    jsonl.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    meta = extract_metadata(jsonl)

    # Every distinct cwd is retained (the #21 guarantee: no top-N truncation
    # at index time). Since #56 they are PRESENCE rows -- a cwd is where the
    # session sat, and this fixture makes no tool calls, so no work is
    # credited anywhere. Counting the sightings (5/4/3/2/1/1) is exactly the
    # event-based measure that made session length masquerade as folder
    # preference.
    assert set(meta.folder_usage) == {
        "\\home\\a", "\\home\\b", "\\home\\c",
        "\\home\\d", "\\home\\e", "\\home\\f",
    }
    assert set(meta.folder_usage.values()) == {0}


def test_tool_calls_credit_the_folder_they_worked_in(tmp_path):
    """#56 / AC7 -- the case the old fixtures could not express.

    A session with a CONSTANT cwd (what current Claude Code actually
    produces) whose tool calls operate in a different repository. Before
    the fix, folder_usage held one entry -- the launch dir -- and
    `csb scan other-repo` found nothing. This test FAILS on pre-fix code.
    """
    jsonl = tmp_path / "s.jsonl"
    launch = "/launch/dir"

    def call(tool, tool_input):
        return {
            "type": "assistant", "timestamp": "2026-05-01T10:00:00Z",
            "cwd": launch, "uuid": "a1",
            "message": {"content": [
                {"type": "tool_use", "name": tool, "input": tool_input},
            ]},
        }

    events = [
        {"type": "user", "timestamp": "2026-05-01T10:00:00Z", "cwd": launch, "uuid": "u1"},
        # Structured file tools -> the file's DIRECTORY is the work location
        call("Read", {"file_path": "/work/repo/pkg/mod.py"}),
        call("Edit", {"file_path": "/work/repo/pkg/mod.py"}),
        call("Write", {"file_path": "/work/repo/tests/test_mod.py"}),
        # A command naming an absolute path elsewhere
        call("Bash", {"command": "ls /work/repo/docs"}),
        # A command that names no path at all -> credited to the session cwd
        call("Bash", {"command": "git status"}),
    ]
    jsonl.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    meta = extract_metadata(jsonl)

    # The launch dir is NOT the top folder -- the repo the work happened in
    # is. This is the inversion #56 exists to correct.
    assert meta.folder_usage["\\work\\repo\\pkg"] == 2      # Read + Edit
    assert meta.folder_usage["\\work\\repo\\tests"] == 1
    assert meta.folder_usage["\\work\\repo\\docs"] == 1
    assert meta.folder_usage["\\launch\\dir"] == 1          # the path-less call

    top = max(meta.folder_usage.items(), key=lambda kv: kv[1])[0]
    assert top == "\\work\\repo\\pkg", "the launch dir must not win by default"

    # AC12: one unit per tool call, never more -- totals stay comparable to
    # the session's tool-call count.
    assert sum(meta.folder_usage.values()) <= meta.tool_call_count

    # AC10: harvesting ran, so a NULL marker downstream means "unmeasured",
    # never "this session touched nothing".
    assert meta.tool_paths_extracted is True


def test_secondary_touches_are_present_but_uncredited(tmp_path):
    """#56 / AC13 -- a folder that is never any call's PRIMARY is still
    recorded, so `csb scan` finds it. Measured on real transcripts, 33% of
    touched folders are only ever secondary; dropping them would recreate
    the bug."""
    jsonl = tmp_path / "s.jsonl"
    events = [{
        "type": "assistant", "timestamp": "2026-05-01T10:00:00Z",
        "cwd": "/launch", "uuid": "a1",
        "message": {"content": [{
            "type": "tool_use", "name": "Bash",
            # First absolute path wins primary; the second is a secondary touch.
            "input": {"command": "cp /src/repo/a.txt /work/repo/b.txt"},
        }]},
    }]
    jsonl.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    meta = extract_metadata(jsonl)

    assert meta.folder_usage["\\src\\repo"] == 1     # primary: work credited
    assert meta.folder_usage["\\work\\repo"] == 0    # secondary: present, uncredited


# ── event_count (restore-verify gate, v0.3.16) ──────────────────────────

def test_event_count_zero_for_garbage_stub(tmp_path):
    """A garbage/stub file (e.g. a symlink-target path string, not JSONL)
    parses into ZERO events -- the signal the restore-verify gate uses to
    refuse un-deleting a session from a non-transcript."""
    stub = tmp_path / "stub.jsonl"
    stub.write_bytes(b"C:/Users/x/.claude/projects/slug/uuid.jsonl")  # bare path
    meta = extract_metadata(stub)
    assert meta.event_count == 0
    assert meta.message_count == 0


def test_event_count_zero_for_empty_file(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert extract_metadata(empty).event_count == 0


def test_event_count_counts_all_parsed_events(tmp_path):
    """event_count is ALL parsed JSON events, not just user/assistant msgs --
    so a real-but-message-light transcript still reads as valid (>=1)."""
    jsonl = tmp_path / "real.jsonl"
    events = [
        {"type": "progress", "timestamp": "2026-05-01T10:00:00Z"},   # not a msg
        {"type": "system", "timestamp": "2026-05-01T10:00:01Z"},     # not a msg
        {"type": "user", "timestamp": "2026-05-01T10:00:02Z", "uuid": "u1"},
    ]
    jsonl.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    meta = extract_metadata(jsonl)
    assert meta.event_count == 3       # all three parsed
    assert meta.message_count == 1     # only the user msg

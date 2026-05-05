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

    # Folder analysis: start folder + other folders
    assert "/home/user/code" in meta.folder_usage
    assert "/home/user/other-project" in meta.folder_usage
    # other-project appears 3 times, code appears 4 times
    assert meta.folder_usage["/home/user/other-project"] == 3


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
        # 6 distinct cwds with descending counts: 5, 4, 3, 2, 1, 1
        *[{"type": "user", "timestamp": "2026-05-01T10:00:00Z", "cwd": "/a", "uuid": f"u{i}"} for i in range(5)],
        *[{"type": "user", "timestamp": "2026-05-01T10:00:00Z", "cwd": "/b", "uuid": f"v{i}"} for i in range(4)],
        *[{"type": "user", "timestamp": "2026-05-01T10:00:00Z", "cwd": "/c", "uuid": f"w{i}"} for i in range(3)],
        *[{"type": "user", "timestamp": "2026-05-01T10:00:00Z", "cwd": "/d", "uuid": f"x{i}"} for i in range(2)],
        {"type": "user", "timestamp": "2026-05-01T10:00:00Z", "cwd": "/e", "uuid": "y0"},
        {"type": "user", "timestamp": "2026-05-01T10:00:00Z", "cwd": "/f", "uuid": "z0"},
    ]
    jsonl.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    meta = extract_metadata(jsonl)

    # All six distinct cwds must be in folder_usage with their actual counts.
    # Pre-#21 behavior would have kept only first_cwd + top 3, dropping /e and /f.
    assert meta.folder_usage == {"/a": 5, "/b": 4, "/c": 3, "/d": 2, "/e": 1, "/f": 1}

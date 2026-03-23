"""Tests for the session scanner."""

import json
from pathlib import Path

from claude_session_backup.scanner import scan_projects, desanitize_project_path


def test_scan_finds_sessions(mock_claude_dir):
    sessions = scan_projects(str(mock_claude_dir))
    assert len(sessions) == 1

    sf = sessions[0]
    assert sf.session_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert sf.project == "C--code-test"
    assert sf.jsonl_path.exists()
    assert sf.jsonl_size > 0
    assert sf.state_file is not None
    assert sf.name_cache is not None


def test_scan_empty_dir(tmp_path):
    claude = tmp_path / ".claude" / "projects"
    claude.mkdir(parents=True)
    sessions = scan_projects(str(tmp_path / ".claude"))
    assert sessions == []


def test_scan_ignores_non_uuid_files(mock_claude_dir):
    """Non-UUID-named .jsonl files should be skipped."""
    project_dir = mock_claude_dir / "projects" / "C--code-test"
    (project_dir / "not-a-uuid.jsonl").write_text("{}", encoding="utf-8")
    sessions = scan_projects(str(mock_claude_dir))
    assert len(sessions) == 1  # Still just the one valid session


def test_desanitize_windows_path():
    assert desanitize_project_path("C--code-ext").startswith("C:")


def test_desanitize_unix_path():
    result = desanitize_project_path("-home-user-project")
    assert result.startswith("/") or result.startswith("\\")

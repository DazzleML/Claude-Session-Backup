"""Shared test fixtures for claude-session-backup."""

import json
import pytest
from pathlib import Path


@pytest.fixture
def mock_claude_dir(tmp_path):
    """Create a mock ~/.claude directory structure for testing."""
    claude = tmp_path / ".claude"
    projects = claude / "projects"
    states = claude / "session-states"

    # Create directories
    projects.mkdir(parents=True)
    states.mkdir(parents=True)

    # Create a sample project with a session
    project_dir = projects / "C--code-test"
    project_dir.mkdir()

    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    # Create JSONL transcript
    jsonl = project_dir / f"{session_id}.jsonl"
    events = [
        {"type": "custom-title", "customTitle": "test-session", "sessionId": session_id},
        {"type": "user", "timestamp": "2026-03-23T10:00:00Z", "cwd": "/home/user/code", "version": "2.1.81", "sessionId": session_id, "uuid": "u1"},
        {"type": "assistant", "timestamp": "2026-03-23T10:00:05Z", "cwd": "/home/user/code", "sessionId": session_id, "uuid": "u2"},
        {"type": "user", "timestamp": "2026-03-23T10:01:00Z", "cwd": "/home/user/other-project", "sessionId": session_id, "uuid": "u3"},
        {"type": "assistant", "timestamp": "2026-03-23T10:01:05Z", "cwd": "/home/user/other-project", "sessionId": session_id, "uuid": "u4"},
        {"type": "user", "timestamp": "2026-03-23T10:02:00Z", "cwd": "/home/user/other-project", "sessionId": session_id, "uuid": "u5"},
        {"type": "assistant", "timestamp": "2026-03-23T10:02:05Z", "cwd": "/home/user/code", "sessionId": session_id, "uuid": "u6"},
    ]
    jsonl.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    # Create session state
    state = states / f"{session_id}.json"
    state.write_text(json.dumps({
        "session_id": session_id,
        "transcript_path": str(jsonl),
        "original_cwd": "/home/user/code",
        "cwd": "/home/user/other-project",
        "current_name": "test-session",
    }), encoding="utf-8")

    # Create name cache
    name_cache = states / f"{session_id}.name-cache"
    name_cache.write_text("test-session", encoding="utf-8")

    # Initialize as git repo (no GPG signing in tests)
    import os
    import subprocess
    test_env = {
        **dict(os.environ),
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
    }
    subprocess.run(["git", "init", str(claude)], capture_output=True, check=True, env=test_env)
    subprocess.run(["git", "-C", str(claude), "config", "commit.gpgsign", "false"], capture_output=True, check=True, env=test_env)
    subprocess.run(["git", "-C", str(claude), "add", "-A"], capture_output=True, check=True, env=test_env)
    subprocess.run(
        ["git", "-C", str(claude), "commit", "--no-gpg-sign", "-m", "initial"],
        capture_output=True, check=True, env=test_env,
    )

    return claude


@pytest.fixture
def mock_db(tmp_path):
    """Create a temporary SQLite index database."""
    from claude_session_backup.index import open_db, init_schema
    db_path = str(tmp_path / "test.db")
    conn = open_db(db_path)
    init_schema(conn)
    return conn

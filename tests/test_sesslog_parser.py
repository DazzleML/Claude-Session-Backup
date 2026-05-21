"""Tests for claude_session_backup.sesslog_parser (logger output -> FileOpRow)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_session_backup.sesslog_parser import (
    SESSLOG_FILEOP_CHANNELS,
    find_fileop_channels_for_session,
    has_session_logger,
    iter_file_ops_from_sesslog,
)
from claude_session_backup.transcript_walker import FileOpRow


SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _write_log(tmp_path: Path, lines: list[str], name: str = "test.log") -> Path:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ── Body parsing per tool kind ───────────────────────────────────────


def test_parses_simple_read(tmp_path):
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {Read: "C:/code/test.py" }',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert len(rows) == 1
    assert rows[0].operation == "read"
    assert rows[0].file_path == "C:/code/test.py"
    assert rows[0].strength == 2
    assert rows[0].timestamp == "2026-05-18 10:00:00"


def test_strips_line_range_from_read_path(tmp_path):
    """Logger appends `:N` or `:N-M` to Read targets when the AI
    requested a specific line range; the canonical path doesn't
    include that suffix."""
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {Read: "C:/code/test.py:143" }',
        '[[2026-05-18 10:00:01]] {Read: "C:/code/test.py:128-157" }',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert [r.file_path for r in rows] == [
        "C:/code/test.py",
        "C:/code/test.py",
    ]


def test_parses_agent_attributed_tool(tmp_path):
    """Tools launched by a sub-agent are tagged `Tool|AgentName:`.
    Parser ignores the agent tag for file_operations -- the path
    is what matters."""
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {Read|Explore: "C:/code/foo.py" }',
        '[[2026-05-18 10:00:01]] {Edit|oracle: "C:/code/bar.py:10" ← "..." (-1/+2L) }',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert len(rows) == 2
    assert rows[0].operation == "read"
    assert rows[1].operation == "edited"
    assert rows[1].file_path == "C:/code/bar.py"


def test_parses_edit_with_diff_metadata(tmp_path):
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {Edit: "C:/code/x.py:42" ← "snippet..." (-3/+5L) }',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert rows[0].operation == "edited"
    assert rows[0].strength == 3
    assert rows[0].file_path == "C:/code/x.py"


def test_parses_write_with_content_preview(tmp_path):
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {Write: "C:/code/new.py" ← "first 20 chars..." (50L) }',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert rows[0].operation == "wrote"
    assert rows[0].strength == 3
    assert rows[0].file_path == "C:/code/new.py"


def test_parses_notebook_edit(tmp_path):
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {NotebookEdit: "C:/code/x.ipynb" ← "..." }',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert rows[0].operation == "notebook_edit"
    assert rows[0].strength == 3


def test_parses_multiedit_as_edited(tmp_path):
    """Logger-only path: JSONL emits MultiEdit as multiple Edit blocks
    in the importer, but the logger preserves it as one block. We
    map it to 'edited' for consistency."""
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {MultiEdit: "C:/code/x.py" ← "..." }',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert rows[0].operation == "edited"
    assert rows[0].strength == 3


def test_parses_glob_with_in_path(tmp_path):
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {Glob: **/*.py in "C:/code/src" }',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert rows[0].operation == "searched"
    assert rows[0].strength == 1
    assert rows[0].file_path == "C:/code/src"


def test_parses_grep_with_path_filter(tmp_path):
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {Grep: pattern | "**/*.md" in "private/" }',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert rows[0].operation == "searched"
    assert rows[0].file_path == "private/"


def test_grep_without_in_path_skipped(tmp_path):
    """Grep with no explicit path target -- the body is just the
    pattern. We don't misinterpret the pattern as a path."""
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {Grep: just-a-pattern }',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert rows == []


# ── Tools that should NOT emit a file-op row ─────────────────────────


def test_bash_is_not_a_file_op(tmp_path):
    """Bash commands aren't parsed (per csb policy -- shell parsing
    is fragile)."""
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {Bash: cd /tmp && rm foo.txt }',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert rows == []


def test_skill_agent_tasks_not_file_ops(tmp_path):
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {Skill: commit }',
        '[[2026-05-18 10:00:01]] {Agent: Find foo }',
        '[[2026-05-18 10:00:02]] {WebSearch: query }',
        '[[2026-05-18 10:00:03]] {Task: x }',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert rows == []


# ── Multi-line body handling ─────────────────────────────────────────


def test_handles_multiline_block(tmp_path):
    """Some Write/Edit blocks span multiple lines because the snippet
    preview contains newlines."""
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {Write: "C:/code/x.md" ← "line1',
        'line2',
        'line3..." (10L) }',
        '[[2026-05-18 10:00:01]] {Read: "C:/code/y.py" }',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert len(rows) == 2
    assert rows[0].operation == "wrote"
    assert rows[0].file_path == "C:/code/x.md"
    assert rows[1].operation == "read"
    assert rows[1].file_path == "C:/code/y.py"


# ── Skip / tolerance ─────────────────────────────────────────────────


def test_ignores_session_banner_and_blank_lines(tmp_path):
    p = _write_log(tmp_path, [
        '═' * 80,
        '═══ SESSION START  •  2026-05-18 10:00:00  •  Run #1  •  claude',
        '═' * 80,
        '',
        '[[2026-05-18 10:00:00]] {Read: "C:/code/foo.py" }',
        '',
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert len(rows) == 1


def test_missing_file_returns_empty(tmp_path):
    rows = list(iter_file_ops_from_sesslog(tmp_path / "absent.log", SID))
    assert rows == []


def test_malformed_block_silently_skipped(tmp_path):
    p = _write_log(tmp_path, [
        '[[no-timestamp]] {Read: "foo" }',          # bad timestamp
        '[[2026-05-18 10:00:00]] not-a-tag',         # no brace
        '[[2026-05-18 10:00:00]] {Read: }',          # no quoted path
        '[[2026-05-18 10:00:00]] {Read: "valid.py" }',  # good
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    # Only the last line produced a row.
    assert len(rows) == 1
    assert rows[0].file_path == "valid.py"


def test_message_index_increments_per_emitted_row(tmp_path):
    p = _write_log(tmp_path, [
        '[[2026-05-18 10:00:00]] {Bash: skipped }',         # skip
        '[[2026-05-18 10:00:01]] {Read: "a.py" }',          # index 0
        '[[2026-05-18 10:00:02]] {Skill: skipped }',        # skip
        '[[2026-05-18 10:00:03]] {Edit: "b.py:1" ← "x" }',  # index 1
    ])
    rows = list(iter_file_ops_from_sesslog(p, SID))
    assert [r.message_index for r in rows] == [0, 1]


# ── Sub-channel discovery ────────────────────────────────────────────


def test_find_fileop_channels_finds_all_expected_prefixes(tmp_path):
    sess = tmp_path / "session"
    sess.mkdir()
    # Files that SHOULD be picked up:
    (sess / ".sesslog_bash__sess.log").write_text("", encoding="utf-8")
    (sess / ".tools_bash__sess.log").write_text("", encoding="utf-8")
    (sess / ".fileio_bash__sess.log").write_text("", encoding="utf-8")
    # Files that should NOT be picked up:
    (sess / ".convo_bash__sess.log").write_text("", encoding="utf-8")  # no file-ops in .convo
    (sess / "transcript.jsonl").write_text("", encoding="utf-8")
    (sess / "README.md").write_text("", encoding="utf-8")

    found = find_fileop_channels_for_session(sess)
    names = sorted(p.name for p in found)
    assert names == [
        ".fileio_bash__sess.log",
        ".sesslog_bash__sess.log",
        ".tools_bash__sess.log",
    ]


def test_find_fileop_channels_handles_missing_dir(tmp_path):
    """Non-existent session dir returns empty list, no crash."""
    found = find_fileop_channels_for_session(tmp_path / "absent")
    assert found == []


def test_fileop_channel_constants_known():
    """Lock the public constant -- callers depend on this list."""
    assert SESSLOG_FILEOP_CHANNELS == (".sesslog_", ".tools_", ".fileio_")


# ── Logger presence detection ────────────────────────────────────────


def test_has_session_logger_true_when_convo_recorded(mock_db):
    mock_db.execute(
        "INSERT INTO sessions (session_id, session_name, project, last_active_at) "
        "VALUES (?, ?, ?, ?)",
        (SID, "test", "proj", "2026-05-18T10:00:00Z"),
    )
    mock_db.execute(
        "INSERT INTO session_sources "
        "(session_id, project, source_type, source_path, last_seen) "
        "VALUES (?, ?, 'convo', ?, ?)",
        (SID, "proj", "/tmp/.convo_x.log", "2026-05-18T10:00:00Z"),
    )
    mock_db.commit()
    assert has_session_logger(mock_db) is True


def test_has_session_logger_true_when_sesslog_recorded(mock_db):
    mock_db.execute(
        "INSERT INTO sessions (session_id, session_name, project, last_active_at) "
        "VALUES (?, ?, ?, ?)",
        (SID, "test", "proj", "2026-05-18T10:00:00Z"),
    )
    mock_db.execute(
        "INSERT INTO session_sources "
        "(session_id, project, source_type, source_path, last_seen) "
        "VALUES (?, ?, 'sesslog', ?, ?)",
        (SID, "proj", "/tmp/.sesslog_x.log", "2026-05-18T10:00:00Z"),
    )
    mock_db.commit()
    assert has_session_logger(mock_db) is True


def test_has_session_logger_false_when_only_jsonl(mock_db):
    """User who has the raw transcripts but no claude-session-logger."""
    mock_db.execute(
        "INSERT INTO sessions (session_id, session_name, project, last_active_at) "
        "VALUES (?, ?, ?, ?)",
        (SID, "test", "proj", "2026-05-18T10:00:00Z"),
    )
    mock_db.execute(
        "INSERT INTO session_sources "
        "(session_id, project, source_type, source_path, last_seen) "
        "VALUES (?, ?, 'jsonl', ?, ?)",
        (SID, "proj", "/tmp/x.jsonl", "2026-05-18T10:00:00Z"),
    )
    mock_db.commit()
    assert has_session_logger(mock_db) is False


def test_has_session_logger_false_when_no_sources(mock_db):
    """Fresh DB with no source rows at all."""
    assert has_session_logger(mock_db) is False

"""Tests for fts5_importer.py -- JSONL → ImportRow + FileOpRow stream."""

import json
from pathlib import Path

import pytest

from claude_session_backup.fts5_db import open_fts5_db
from claude_session_backup.fts5_importer import (
    FileOpRow,
    ImportRow,
    _content_hash,
    _extract_agent_tool_uses,
    _extract_file_ops,
    _find_matching_tool_result,
    _flatten_text_blocks,
    _flatten_tool_result_content,
    import_jsonl_to_db,
    iter_rows_from_jsonl,
)


SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _write(tmp_path: Path, events: list[dict]) -> Path:
    p = tmp_path / "session.jsonl"
    p.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    return p


# ── _flatten_text_blocks ─────────────────────────────────────────────


def test_flatten_text_string_passes_through():
    assert _flatten_text_blocks("hello") == "hello"


def test_flatten_text_blocks_list():
    out = _flatten_text_blocks([
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ])
    assert out == "first\nsecond"


def test_flatten_text_skips_non_text_blocks():
    out = _flatten_text_blocks([
        {"type": "text", "text": "keep"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
        {"type": "thinking", "text": "drop"},
    ])
    assert out == "keep"


def test_flatten_text_empty_or_invalid():
    assert _flatten_text_blocks(None) == ""
    assert _flatten_text_blocks({}) == ""
    assert _flatten_text_blocks([]) == ""
    assert _flatten_text_blocks([{"type": "text"}]) == ""  # missing text field


def test_flatten_text_preserves_system_reminder_tags():
    """csb policy: preserve verbatim, don't strip <system-reminder> etc."""
    out = _flatten_text_blocks([
        {"type": "text", "text": "<system-reminder>do X</system-reminder>"},
    ])
    assert "<system-reminder>" in out


# ── _flatten_tool_result_content ─────────────────────────────────────


def test_tool_result_string():
    assert _flatten_tool_result_content("tool says hi") == "tool says hi"


def test_tool_result_blocks_list():
    out = _flatten_tool_result_content([
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ])
    assert out == "first\nsecond"


def test_tool_result_unknown_shape():
    assert _flatten_tool_result_content(None) == ""
    assert _flatten_tool_result_content(42) == ""


# ── _extract_agent_tool_uses ─────────────────────────────────────────


def test_extract_agent_tool_uses_picks_agent_calls():
    content = [
        {"type": "tool_use", "id": "tu1", "name": "Agent",
         "input": {"subagent_type": "Explore", "prompt": "..."}},
        {"type": "tool_use", "id": "tu2", "name": "Read",
         "input": {"file_path": "/x"}},
    ]
    result = list(_extract_agent_tool_uses(content))
    assert result == [("tu1", "explore")]


def test_extract_agent_tool_uses_normalizes_lowercase():
    content = [
        {"type": "tool_use", "id": "x", "name": "Agent",
         "input": {"subagent_type": "Senior-Engineer"}},
    ]
    assert list(_extract_agent_tool_uses(content)) == [("x", "senior-engineer")]


def test_extract_agent_tool_uses_skips_missing_subtype():
    content = [
        {"type": "tool_use", "id": "x", "name": "Agent", "input": {}},
    ]
    assert list(_extract_agent_tool_uses(content)) == []


def test_extract_agent_tool_uses_handles_invalid_shape():
    assert list(_extract_agent_tool_uses(None)) == []
    assert list(_extract_agent_tool_uses("not a list")) == []


# ── _find_matching_tool_result ───────────────────────────────────────


def test_find_matching_tool_result_pairs_by_id():
    tracked = {"tu1": "explore", "tu2": "oracle"}
    content = [
        {"type": "tool_result", "tool_use_id": "tu1",
         "content": [{"type": "text", "text": "explore output"}]},
        {"type": "tool_result", "tool_use_id": "tuX",  # unknown -> skipped
         "content": "ignored"},
    ]
    result = list(_find_matching_tool_result(content, tracked))
    assert result == [("explore", "explore output")]


def test_find_matching_tool_result_string_content():
    tracked = {"tu1": "plan"}
    content = [
        {"type": "tool_result", "tool_use_id": "tu1", "content": "plain string output"},
    ]
    assert list(_find_matching_tool_result(content, tracked)) == [("plan", "plain string output")]


# ── _extract_file_ops ────────────────────────────────────────────────


def test_extract_file_ops_read_edit_write():
    content = [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/b.py"}},
        {"type": "tool_use", "name": "Write", "input": {"file_path": "/c.py"}},
    ]
    result = list(_extract_file_ops(content))
    # v0.3.1: each tuple is (operation, path, strength). 3 = active modify, 2 = read.
    assert result == [
        ("read", "/a.py", 2),
        ("edited", "/b.py", 3),
        ("wrote", "/c.py", 3),
    ]


def test_extract_file_ops_grep_uses_path_field():
    content = [
        {"type": "tool_use", "name": "Grep", "input": {"path": "src/", "pattern": "x"}},
    ]
    # Grep gets the lowest strength (1) -- search probe, not certain the file was read.
    assert list(_extract_file_ops(content)) == [("searched", "src/", 1)]


def test_extract_file_ops_notebook_edit():
    content = [
        {"type": "tool_use", "name": "NotebookEdit",
         "input": {"notebook_path": "/notebook.ipynb"}},
    ]
    # Notebook edit = active modification (3), same tier as Edit/Write.
    assert list(_extract_file_ops(content)) == [
        ("notebook_edit", "/notebook.ipynb", 3),
    ]


def test_extract_file_ops_bash_excluded():
    """Bash is intentionally not parsed -- too fragile."""
    content = [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "cat /etc/passwd"}},
    ]
    assert list(_extract_file_ops(content)) == []


def test_extract_file_ops_missing_path_silently_skipped():
    content = [
        {"type": "tool_use", "name": "Read", "input": {}},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": ""}},
    ]
    assert list(_extract_file_ops(content)) == []


# ── iter_rows_from_jsonl: end-to-end on synthetic JSONL ──────────────


def test_iter_rows_user_event(tmp_path):
    p = _write(tmp_path, [
        {"type": "user", "uuid": "u1", "timestamp": "t1",
         "message": {"content": "hello"}},
    ])
    rows = list(iter_rows_from_jsonl(p, SID))
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, ImportRow)
    assert r.role == "USER"
    assert r.role_subtype is None
    assert r.content == "hello"
    assert r.message_index == 0


def test_iter_rows_assistant_no_skill_is_AI(tmp_path):
    p = _write(tmp_path, [
        {"type": "assistant", "uuid": "u1", "timestamp": "t1",
         "message": {"content": [{"type": "text", "text": "I am the model"}]}},
    ])
    rows = list(iter_rows_from_jsonl(p, SID))
    assert len(rows) == 1
    assert rows[0].role == "AI"
    assert rows[0].role_subtype is None


def test_iter_rows_assistant_with_attribution_skill_is_AGENT(tmp_path):
    p = _write(tmp_path, [
        {"type": "assistant", "uuid": "u1", "timestamp": "t1",
         "attributionSkill": "fullpostmortem",
         "message": {"content": [{"type": "text", "text": "writing PM"}]}},
    ])
    rows = list(iter_rows_from_jsonl(p, SID))
    assert len(rows) == 1
    assert rows[0].role == "AGENT"
    assert rows[0].role_subtype == "fullpostmortem"


def test_iter_rows_agent_tool_chain_yields_agent_row(tmp_path):
    """The two-pass Agent detection: tool_use launches, tool_result reports."""
    p = _write(tmp_path, [
        # 1. Assistant launches Explore agent
        {"type": "assistant", "uuid": "u1", "timestamp": "t1",
         "message": {"content": [
             {"type": "tool_use", "id": "tu1", "name": "Agent",
              "input": {"subagent_type": "Explore"}},
         ]}},
        # 2. User event wrapping the agent's reply
        {"type": "user", "uuid": "u2", "timestamp": "t2",
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "tu1",
              "content": [{"type": "text", "text": "explore agent reply"}]},
         ]}},
    ])
    rows = [r for r in iter_rows_from_jsonl(p, SID) if isinstance(r, ImportRow)]
    # Exactly one row: the AGENT:explore row from the tool_result
    agent_rows = [r for r in rows if r.role == "AGENT"]
    assert len(agent_rows) == 1
    assert agent_rows[0].role_subtype == "explore"
    assert agent_rows[0].content == "explore agent reply"


def test_iter_rows_skips_non_user_assistant(tmp_path):
    p = _write(tmp_path, [
        {"type": "summary", "summary": "thing"},
        {"type": "system", "content": "noise"},
        {"type": "user", "uuid": "u1", "message": {"content": "real"}},
    ])
    rows = [r for r in iter_rows_from_jsonl(p, SID) if isinstance(r, ImportRow)]
    assert len(rows) == 1
    assert rows[0].content == "real"


def test_iter_rows_skips_empty_content(tmp_path):
    """Assistant event with only tool_use (no text) emits no ImportRow."""
    p = _write(tmp_path, [
        {"type": "assistant", "uuid": "u1",
         "message": {"content": [
             {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
         ]}},
    ])
    rows = [r for r in iter_rows_from_jsonl(p, SID) if isinstance(r, ImportRow)]
    assert len(rows) == 0  # no text content
    # But the file_op IS emitted
    fops = [r for r in iter_rows_from_jsonl(p, SID) if isinstance(r, FileOpRow)]
    assert len(fops) == 1
    assert fops[0].operation == "read"


def test_iter_rows_message_index_advances_per_import_row(tmp_path):
    p = _write(tmp_path, [
        {"type": "user", "uuid": "u1", "message": {"content": "a"}},
        {"type": "assistant", "uuid": "u2",
         "message": {"content": [{"type": "text", "text": "b"}]}},
        {"type": "user", "uuid": "u3", "message": {"content": "c"}},
    ])
    rows = [r for r in iter_rows_from_jsonl(p, SID) if isinstance(r, ImportRow)]
    assert [r.message_index for r in rows] == [0, 1, 2]


def test_iter_rows_tolerates_malformed_json_lines(tmp_path):
    p = tmp_path / "broken.jsonl"
    p.write_text(
        json.dumps({"type": "user", "uuid": "u1", "message": {"content": "good"}}) + "\n"
        "{this is not valid json}\n"
        + json.dumps({"type": "user", "uuid": "u2", "message": {"content": "also good"}}) + "\n",
        encoding="utf-8",
    )
    rows = list(iter_rows_from_jsonl(p, SID))
    import_rows = [r for r in rows if isinstance(r, ImportRow)]
    assert len(import_rows) == 2
    assert [r.content for r in import_rows] == ["good", "also good"]


def test_iter_rows_handles_missing_file(tmp_path):
    """Returns empty iterator (no crash) when file doesn't exist."""
    p = tmp_path / "nonexistent.jsonl"
    rows = list(iter_rows_from_jsonl(p, SID))
    assert rows == []


# ── import_jsonl_to_db: end-to-end ──────────────────────────────────


def test_import_inserts_rows_into_messages(tmp_path):
    p = _write(tmp_path, [
        {"type": "user", "uuid": "u1", "message": {"content": "hello"}},
        {"type": "assistant", "uuid": "u2",
         "message": {"content": [{"type": "text", "text": "hi back"}]}},
    ])
    conn = open_fts5_db(tmp_path / "fts.db")
    rows_added, content_hash = import_jsonl_to_db(conn, SID, p)
    assert rows_added == 2
    assert content_hash  # non-empty sha256

    db_rows = conn.execute(
        "SELECT role, content FROM messages ORDER BY message_index"
    ).fetchall()
    assert [(r["role"], r["content"]) for r in db_rows] == [
        ("USER", "hello"), ("AI", "hi back"),
    ]


def test_import_populates_file_operations(tmp_path):
    p = _write(tmp_path, [
        {"type": "assistant", "uuid": "u1",
         "message": {"content": [
             {"type": "text", "text": "reading file"},
             {"type": "tool_use", "name": "Read",
              "input": {"file_path": "/a.py"}},
         ]}},
    ])
    conn = open_fts5_db(tmp_path / "fts.db")
    import_jsonl_to_db(conn, SID, p)
    ops = conn.execute(
        "SELECT operation, file_path FROM file_operations"
    ).fetchall()
    assert [(o["operation"], o["file_path"]) for o in ops] == [("read", "/a.py")]


def test_import_dedupes_on_reimport(tmp_path):
    """Re-importing the same JSONL doesn't double the row count."""
    p = _write(tmp_path, [
        {"type": "user", "uuid": "u1", "message": {"content": "x"}},
    ])
    conn = open_fts5_db(tmp_path / "fts.db")
    import_jsonl_to_db(conn, SID, p)
    import_jsonl_to_db(conn, SID, p)
    n = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (SID,)).fetchone()[0]
    assert n == 1


def test_import_indexes_into_messages_fts(tmp_path):
    """After import, FTS5 MATCH works against the new rows."""
    p = _write(tmp_path, [
        {"type": "user", "uuid": "u1", "message": {"content": "find this oauth phrase"}},
    ])
    conn = open_fts5_db(tmp_path / "fts.db")
    import_jsonl_to_db(conn, SID, p)
    n = conn.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'oauth'"
    ).fetchone()[0]
    assert n == 1


def test_content_hash_changes_when_file_changes(tmp_path):
    p1 = _write(tmp_path, [{"type": "user", "uuid": "u1", "message": {"content": "x"}}])
    h1 = _content_hash(p1)
    # rewrite with different content
    p2 = _write(tmp_path, [{"type": "user", "uuid": "u1", "message": {"content": "y"}}])
    h2 = _content_hash(p2)
    assert h1 != h2
    assert h1 and h2

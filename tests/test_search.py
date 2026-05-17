"""Tests for the transcript content search engine (claude_session_backup.search)."""

import json
import re
from pathlib import Path

import pytest

from claude_session_backup.search import (
    Event,
    Hit,
    _build_matcher,
    _pick_one_source,
    parse_jsonl_events,
    parse_log_blocks,
    parse_source,
    search,
)


# ── Test data builders ────────────────────────────────────────────────


def _write_convo(tmp_path: Path, events: list[tuple[str, str, str]]) -> Path:
    """Write a .convo-style file. Each event is (timestamp, role, text)."""
    path = tmp_path / "x.convo.log"
    lines: list[str] = []
    for ts, role, text in events:
        if "\n" in text:
            lines.append(f"[[{ts}]] {{{role}:")
            lines.append(text)
            lines.append("}")
        else:
            lines.append(f"[[{ts}]] {{{role}: {text}}}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_jsonl(tmp_path: Path, events: list[dict]) -> Path:
    path = tmp_path / "x.jsonl"
    path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    return path


def _insert_session(conn, session_id, name, project, last_active="2026-05-16T10:00:00Z"):
    conn.execute(
        "INSERT INTO sessions (session_id, session_name, project, last_active_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, name, project, last_active),
    )


def _insert_source(conn, session_id, project, source_type, source_path):
    conn.execute(
        "INSERT INTO session_sources "
        "(session_id, project, source_type, source_path, last_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, project, source_type, source_path, "2026-05-16T10:00:00Z"),
    )


# ── parse_log_blocks ──────────────────────────────────────────────────


def test_parse_log_blocks_single_line(tmp_path):
    p = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "hello world")])
    events = list(parse_log_blocks(str(p)))
    assert len(events) == 1
    assert events[0].role == "USER"
    assert events[0].text == "hello world"
    assert events[0].timestamp == "2026-05-16 10:00:00"


def test_parse_log_blocks_multi_line(tmp_path):
    p = _write_convo(tmp_path, [("2026-05-16 10:00:00", "AI", "line1\nline2\nline3")])
    events = list(parse_log_blocks(str(p)))
    assert len(events) == 1
    assert events[0].role == "AI"
    assert events[0].text == "line1\nline2\nline3"


def test_parse_log_blocks_mixed_roles(tmp_path):
    p = _write_convo(tmp_path, [
        ("2026-05-16 10:00:00", "USER", "hi"),
        ("2026-05-16 10:00:05", "AI", "hello\nworld"),
        ("2026-05-16 10:00:10", "USER", "ok"),
    ])
    events = list(parse_log_blocks(str(p)))
    assert [e.role for e in events] == ["USER", "AI", "USER"]
    assert [e.text for e in events] == ["hi", "hello\nworld", "ok"]


def test_parse_log_blocks_skips_tool_tags(tmp_path):
    """Bash/Read/Skill blocks are not USER/AI/AGENT -- they should be skipped."""
    p = tmp_path / "mixed.log"
    p.write_text(
        "[[2026-05-16 10:00:00]] {USER: hi}\n"
        "[[2026-05-16 10:00:01]] {Bash: ls -la}\n"
        "[[2026-05-16 10:00:02]] {Read: /path/to/file}\n"
        "[[2026-05-16 10:00:03]] {AI: hello}\n",
        encoding="utf-8",
    )
    events = list(parse_log_blocks(str(p)))
    assert len(events) == 2
    assert [e.role for e in events] == ["USER", "AI"]


def test_parse_log_blocks_accepts_agent_bare(tmp_path):
    """{AGENT: text} without subtype should parse."""
    p = tmp_path / "agent.log"
    p.write_text(
        "[[2026-05-16 10:00:00]] {AGENT: agent did a thing}\n",
        encoding="utf-8",
    )
    events = list(parse_log_blocks(str(p)))
    assert len(events) == 1
    assert events[0].role == "AGENT"


def test_parse_log_blocks_accepts_agent_subtyped(tmp_path):
    """{AGENT:explore: text} should preserve the subtype in role."""
    p = tmp_path / "agent.log"
    p.write_text(
        "[[2026-05-16 10:00:00]] {AGENT:explore: searched files}\n"
        "[[2026-05-16 10:00:05]] {AGENT:senior-engineer: reviewed code}\n",
        encoding="utf-8",
    )
    events = list(parse_log_blocks(str(p)))
    assert len(events) == 2
    assert events[0].role == "AGENT:explore"
    assert events[1].role == "AGENT:senior-engineer"


def test_parse_log_blocks_handles_missing_file(tmp_path):
    events = list(parse_log_blocks(str(tmp_path / "absent.log")))
    assert events == []


def test_parse_log_blocks_line_numbers(tmp_path):
    """line_num records the OPENING line of the block."""
    p = tmp_path / "lines.log"
    p.write_text(
        "[[2026-05-16 10:00:00]] {USER:\n"
        "line a\n"
        "line b\n"
        "}\n"
        "[[2026-05-16 10:00:05]] {AI: short}\n",
        encoding="utf-8",
    )
    events = list(parse_log_blocks(str(p)))
    assert events[0].line_num == 1  # USER opens on line 1
    assert events[1].line_num == 5  # AI opens on line 5


# ── parse_jsonl_events ────────────────────────────────────────────────


def test_parse_jsonl_user_event(tmp_path):
    p = _write_jsonl(tmp_path, [
        {"type": "user", "timestamp": "2026-05-16T10:00:00Z",
         "message": {"role": "user", "content": "hello"}},
    ])
    events = list(parse_jsonl_events(str(p)))
    assert len(events) == 1
    assert events[0].role == "USER"
    assert events[0].text == "hello"


def test_parse_jsonl_assistant_event_with_content_blocks(tmp_path):
    """Assistant content is a list of blocks; flatten text blocks."""
    p = _write_jsonl(tmp_path, [
        {"type": "assistant", "timestamp": "2026-05-16T10:00:05Z", "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "part one"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                {"type": "text", "text": "part two"},
            ],
        }},
    ])
    events = list(parse_jsonl_events(str(p)))
    assert len(events) == 1
    assert events[0].role == "AI"
    assert "part one" in events[0].text
    assert "part two" in events[0].text
    assert "ls" not in events[0].text  # tool_use is filtered out


def test_parse_jsonl_skips_non_user_assistant(tmp_path):
    p = _write_jsonl(tmp_path, [
        {"type": "system", "message": {"content": "system msg"}},
        {"type": "user", "timestamp": "t", "message": {"content": "u"}},
        {"type": "summary", "message": {"content": "compaction"}},
    ])
    events = list(parse_jsonl_events(str(p)))
    assert len(events) == 1
    assert events[0].role == "USER"


def test_parse_jsonl_skips_empty_text(tmp_path):
    p = _write_jsonl(tmp_path, [
        {"type": "user", "message": {"content": ""}},
        {"type": "assistant", "message": {"content": []}},
    ])
    events = list(parse_jsonl_events(str(p)))
    assert events == []


def test_parse_jsonl_tolerates_corrupt_lines(tmp_path):
    p = tmp_path / "corrupt.jsonl"
    p.write_text(
        '{"type":"user","message":{"content":"good"}}\n'
        '{not valid json\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"also good"}]}}\n',
        encoding="utf-8",
    )
    events = list(parse_jsonl_events(str(p)))
    assert len(events) == 2
    assert events[0].text == "good"
    assert events[1].text == "also good"


def test_parse_jsonl_missing_file(tmp_path):
    events = list(parse_jsonl_events(str(tmp_path / "absent.jsonl")))
    assert events == []


# ── _build_matcher ────────────────────────────────────────────────────


def test_matcher_literal_default_case_insensitive():
    match = _build_matcher("oauth", regex=False, case_sensitive=False)
    assert match("OAuth Flow")
    assert match("learning about oauth")
    assert not match("nothing related")


def test_matcher_literal_case_sensitive():
    match = _build_matcher("OAuth", regex=False, case_sensitive=True)
    assert match("learning OAuth")
    assert not match("learning oauth")


def test_matcher_literal_does_not_interpret_regex_metachars():
    """A literal '.' must not match any character."""
    match = _build_matcher("a.b", regex=False, case_sensitive=False)
    assert match("a.b literal")
    assert not match("axb")  # would match if regex


def test_matcher_regex_case_insensitive_default():
    match = _build_matcher(r"oauth.*flow", regex=True, case_sensitive=False)
    assert match("OAuth Refresh Flow")
    assert not match("flow oauth")  # order matters


def test_matcher_regex_invalid_raises():
    with pytest.raises(ValueError):
        _build_matcher("(unclosed", regex=True, case_sensitive=False)


# ── _pick_one_source ──────────────────────────────────────────────────


def _row(source_type, source_path="/p"):
    """Tiny shim that mimics a sqlite3.Row for tests."""
    class R:
        def __init__(self, t, p):
            self._d = {"source_type": t, "source_path": p}

        def __getitem__(self, k):
            return self._d[k]

    return R(source_type, source_path)


def test_pick_source_prefers_convo():
    rows = [_row("jsonl"), _row("sesslog"), _row("convo")]
    picked = _pick_one_source(rows, source_override=None)
    assert picked["source_type"] == "convo"


def test_pick_source_falls_back_to_sesslog_then_jsonl():
    assert _pick_one_source([_row("jsonl"), _row("sesslog")], None)["source_type"] == "sesslog"
    assert _pick_one_source([_row("jsonl")], None)["source_type"] == "jsonl"


def test_pick_source_override_returns_only_that_type():
    rows = [_row("jsonl"), _row("convo")]
    assert _pick_one_source(rows, source_override="jsonl")["source_type"] == "jsonl"


def test_pick_source_override_missing_returns_none():
    rows = [_row("jsonl")]
    assert _pick_one_source(rows, source_override="convo") is None


def test_pick_source_auto_is_treated_as_no_override():
    rows = [_row("jsonl"), _row("convo")]
    assert _pick_one_source(rows, source_override="auto")["source_type"] == "convo"


# ── End-to-end search() against mock_db ───────────────────────────────


def test_search_finds_literal_in_convo(mock_db, tmp_path):
    convo = _write_convo(tmp_path, [
        ("2026-05-16 10:00:00", "USER", "find the oauth bug"),
        ("2026-05-16 10:00:05", "AI", "looking into auth"),
    ])
    _insert_session(mock_db, "sess-1", "test", "proj")
    _insert_source(mock_db, "sess-1", "proj", "convo", str(convo))
    mock_db.commit()

    hits = list(search(mock_db, "oauth"))
    assert len(hits) == 1
    assert hits[0].role == "USER"
    assert hits[0].source_type == "convo"
    assert "oauth" in hits[0].matched_text


def test_search_regex_mode(mock_db, tmp_path):
    convo = _write_convo(tmp_path, [
        ("2026-05-16 10:00:00", "AI", "I implemented the oauth callback flow"),
    ])
    _insert_session(mock_db, "sess-1", "test", "proj")
    _insert_source(mock_db, "sess-1", "proj", "convo", str(convo))
    mock_db.commit()

    hits = list(search(mock_db, r"oauth.*flow", regex=True))
    assert len(hits) == 1


def test_search_context_above_and_below(mock_db, tmp_path):
    convo = _write_convo(tmp_path, [
        ("2026-05-16 10:00:00", "USER", "context before"),
        ("2026-05-16 10:00:05", "AI", "context just before"),
        ("2026-05-16 10:00:10", "USER", "the MATCH line"),
        ("2026-05-16 10:00:15", "AI", "context just after"),
        ("2026-05-16 10:00:20", "USER", "context after that"),
    ])
    _insert_session(mock_db, "sess-1", "test", "proj")
    _insert_source(mock_db, "sess-1", "proj", "convo", str(convo))
    mock_db.commit()

    hits = list(search(mock_db, "MATCH", above=2, below=2))
    assert len(hits) == 1
    h = hits[0]
    assert len(h.context_above) == 2
    assert len(h.context_below) == 2
    assert [e.text for e in h.context_above] == ["context before", "context just before"]
    assert [e.text for e in h.context_below] == ["context just after", "context after that"]


def test_search_context_clamped_at_boundaries(mock_db, tmp_path):
    """Context request that goes past file start/end is silently clamped."""
    convo = _write_convo(tmp_path, [
        ("2026-05-16 10:00:00", "USER", "MATCH"),
        ("2026-05-16 10:00:05", "AI", "after"),
    ])
    _insert_session(mock_db, "sess-1", "test", "proj")
    _insert_source(mock_db, "sess-1", "proj", "convo", str(convo))
    mock_db.commit()

    hits = list(search(mock_db, "MATCH", above=10, below=10))
    assert len(hits) == 1
    assert hits[0].context_above == []
    assert len(hits[0].context_below) == 1


def test_search_session_filter_prefix(mock_db, tmp_path):
    convo1 = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "MATCH here")])
    convo2 = tmp_path / "convo2.log"
    convo2.write_text("[[2026-05-16 10:00:00]] {USER: MATCH there}\n", encoding="utf-8")

    _insert_session(mock_db, "abc1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "a", "p")
    _insert_session(mock_db, "xyz9-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "b", "p")
    _insert_source(mock_db, "abc1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "p", "convo", str(convo1))
    _insert_source(mock_db, "xyz9-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "p", "convo", str(convo2))
    mock_db.commit()

    hits = list(search(mock_db, "MATCH", session_filter="abc1"))
    assert len(hits) == 1
    assert hits[0].session_id.startswith("abc1")


def test_search_source_override_skips_sessions_without_that_source(mock_db, tmp_path):
    """A session with no .convo* row is skipped under --source convo."""
    jsonl_path = tmp_path / "x.jsonl"
    jsonl_path.write_text(
        '{"type":"user","message":{"content":"MATCH here"}}\n', encoding="utf-8"
    )
    _insert_session(mock_db, "sess-1", "test", "proj")
    _insert_source(mock_db, "sess-1", "proj", "jsonl", str(jsonl_path))
    mock_db.commit()

    # Default: finds it via jsonl fallback
    assert len(list(search(mock_db, "MATCH"))) == 1
    # With override="convo": no source -> no hit
    assert list(search(mock_db, "MATCH", source_override="convo")) == []


def test_search_jsonl_user_and_assistant_events(mock_db, tmp_path):
    jsonl_path = _write_jsonl(tmp_path, [
        {"type": "user", "timestamp": "t1",
         "message": {"role": "user", "content": "find the MATCH"}},
        {"type": "assistant", "timestamp": "t2",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "no marker here"}]}},
        {"type": "assistant", "timestamp": "t3",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "but MATCH appears here"}]}},
    ])
    _insert_session(mock_db, "sess-1", "test", "proj")
    _insert_source(mock_db, "sess-1", "proj", "jsonl", str(jsonl_path))
    mock_db.commit()

    hits = list(search(mock_db, "MATCH"))
    assert len(hits) == 2
    assert {h.role for h in hits} == {"USER", "AI"}


def test_search_limit_stops_iteration(mock_db, tmp_path):
    convo = _write_convo(tmp_path, [
        (f"2026-05-16 10:00:{i:02d}", "USER", "MATCH") for i in range(10)
    ])
    _insert_session(mock_db, "sess-1", "test", "proj")
    _insert_source(mock_db, "sess-1", "proj", "convo", str(convo))
    mock_db.commit()

    hits = list(search(mock_db, "MATCH", limit=3))
    assert len(hits) == 3


def test_search_excludes_deleted_by_default(mock_db, tmp_path):
    convo = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "MATCH")])
    _insert_session(mock_db, "sess-1", "a", "p")
    mock_db.execute(
        "UPDATE sessions SET deleted_at = ? WHERE session_id = ?",
        ("2026-05-16T11:00:00Z", "sess-1"),
    )
    _insert_source(mock_db, "sess-1", "p", "convo", str(convo))
    mock_db.commit()

    assert list(search(mock_db, "MATCH")) == []
    assert len(list(search(mock_db, "MATCH", include_deleted=True))) == 1
    assert len(list(search(mock_db, "MATCH", only_deleted=True))) == 1


def test_search_orders_by_last_active_desc(mock_db, tmp_path):
    older = tmp_path / "older.log"
    newer = tmp_path / "newer.log"
    older.write_text("[[2025-01-01 00:00:00]] {USER: MATCH old}\n", encoding="utf-8")
    newer.write_text("[[2026-05-16 10:00:00]] {USER: MATCH new}\n", encoding="utf-8")

    _insert_session(mock_db, "sess-old", "o", "p", last_active="2025-01-01T00:00:00Z")
    _insert_session(mock_db, "sess-new", "n", "p", last_active="2026-05-16T10:00:00Z")
    _insert_source(mock_db, "sess-old", "p", "convo", str(older))
    _insert_source(mock_db, "sess-new", "p", "convo", str(newer))
    mock_db.commit()

    hits = list(search(mock_db, "MATCH"))
    assert hits[0].session_id == "sess-new"
    assert hits[1].session_id == "sess-old"


def test_search_finds_agent_blocks(mock_db, tmp_path):
    """AGENT and AGENT:<subtype> blocks should be searchable."""
    p = tmp_path / "agent.log"
    p.write_text(
        "[[2026-05-16 10:00:00]] {AGENT: bare agent said MATCH here}\n"
        "[[2026-05-16 10:00:05]] {AGENT:explore: typed agent also MATCH here}\n",
        encoding="utf-8",
    )
    _insert_session(mock_db, "sess-1", "n", "p")
    _insert_source(mock_db, "sess-1", "p", "convo", str(p))
    mock_db.commit()

    hits = list(search(mock_db, "MATCH"))
    assert len(hits) == 2
    assert {h.role for h in hits} == {"AGENT", "AGENT:explore"}


def test_search_empty_pattern_no_hits(mock_db, tmp_path):
    """Edge: empty literal pattern matches everywhere. Confirm behavior is sane (no crash)."""
    convo = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "anything")])
    _insert_session(mock_db, "sess-1", "n", "p")
    _insert_source(mock_db, "sess-1", "p", "convo", str(convo))
    mock_db.commit()

    # Empty string is in every text -- caller's job to reject; we just don't crash
    hits = list(search(mock_db, "", limit=5))
    assert len(hits) == 1  # only 1 event in the file

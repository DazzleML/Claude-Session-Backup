"""Tests for the transcript content search engine (claude_session_backup.search)."""

import json
import re
from pathlib import Path

import pytest

from claude_session_backup.search import (
    Event,
    Hit,
    _SOURCE_PREFERENCE,
    _build_matcher,
    _pick_source_for_session,
    _resolve_preference,
    effective_default_preference,
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


# ── v0.3.1 parity: JSONL parser surfaces AGENT content + attribution ──


def test_parse_jsonl_skill_attributed_assistant_is_agent(tmp_path):
    """v0.3.1: attributionSkill on assistant event -> role 'AGENT:<skill>'.

    Previously, the Phase 1 parser labeled ALL assistant events as 'AI',
    silently dropping the attribution.
    """
    p = _write_jsonl(tmp_path, [
        {"type": "assistant", "timestamp": "t1",
         "attributionSkill": "fullpostmortem",
         "message": {"content": [{"type": "text", "text": "writing PM"}]}},
    ])
    events = list(parse_jsonl_events(str(p)))
    assert len(events) == 1
    assert events[0].role == "AGENT:fullpostmortem"
    assert events[0].text == "writing PM"


def test_parse_jsonl_task_launched_agent_surfaces(tmp_path):
    """v0.3.1 parity: Task-launched (Agent tool) sub-agent output now
    surfaces under --source jsonl.

    Previously the Phase 1 parser ignored tool_result blocks entirely,
    so Explore / oracle / Plan agent responses were invisible to
    `csb search --source jsonl`.
    """
    p = _write_jsonl(tmp_path, [
        # 1. Assistant launches an Explore agent
        {"type": "assistant", "timestamp": "t1", "message": {"content": [
            {"type": "tool_use", "id": "tu1", "name": "Agent",
             "input": {"subagent_type": "Explore", "prompt": "..."}},
        ]}},
        # 2. User event wrapping the agent's reply
        {"type": "user", "timestamp": "t2", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1",
             "content": [{"type": "text", "text": "explore agent reply"}]},
        ]}},
    ])
    events = list(parse_jsonl_events(str(p), session_id="sid"))
    agent_events = [e for e in events if e.role.startswith("AGENT")]
    assert len(agent_events) == 1
    assert agent_events[0].role == "AGENT:explore"
    assert agent_events[0].text == "explore agent reply"


def test_parse_jsonl_agent_role_label_format(tmp_path):
    """AGENT subtype is rendered exactly as 'AGENT:<lowercased subtype>'.

    Lowercasing happens in the walker so e.g. 'Senior-Engineer' becomes
    'AGENT:senior-engineer' regardless of casing in the source.
    """
    p = _write_jsonl(tmp_path, [
        {"type": "assistant", "timestamp": "t1", "message": {"content": [
            {"type": "tool_use", "id": "tu1", "name": "Agent",
             "input": {"subagent_type": "Senior-Engineer"}},
        ]}},
        {"type": "user", "timestamp": "t2", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1",
             "content": "engineer reply"},
        ]}},
    ])
    events = list(parse_jsonl_events(str(p)))
    agent = [e for e in events if e.role.startswith("AGENT")][0]
    assert agent.role == "AGENT:senior-engineer"


def test_parse_jsonl_signature_accepts_optional_session_id(tmp_path):
    """Calling with the new positional session_id arg works the same
    as the legacy single-arg form (Event payloads are identical)."""
    p = _write_jsonl(tmp_path, [
        {"type": "user", "timestamp": "t", "message": {"content": "x"}},
    ])
    a = list(parse_jsonl_events(str(p)))
    b = list(parse_jsonl_events(str(p), session_id="sid"))
    assert a[0].role == b[0].role == "USER"
    assert a[0].text == b[0].text == "x"


# ── v0.3.3: FTS5 dispatch helpers ─────────────────────────────────────


def _build_fake_fts5_db(claude_dir, project, encoded_slug, session_id,
                        messages, jsonl_mtime):
    """Build a real-on-disk per-project FTS5 DB pre-populated with messages.

    Returns the DB path. Schema matches what fts5_db.init_fts5_schema
    produces; messages are inserted via the FTS5 importer's row shape
    so triggers populate messages_fts.
    """
    from claude_session_backup import fts_paths
    from claude_session_backup.fts5_db import (
        open_fts5_db, mark_session_indexed,
    )
    from claude_session_backup.fts5_importer import now_iso

    db_path = fts_paths.fts5_db_path(claude_dir, project, encoded_slug)
    conn = open_fts5_db(db_path, quiet=True)
    cur = conn.cursor()
    for i, (role, role_subtype, content) in enumerate(messages):
        cur.execute(
            "INSERT INTO messages (session_id, uuid, message_index, "
            "  role, role_subtype, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, f"u{i}", i, role, role_subtype, content,
             f"2026-05-18T10:{i:02d}:00Z"),
        )
    mark_session_indexed(conn, session_id, jsonl_mtime, "deadbeef", now_iso())
    conn.commit()
    conn.close()
    return db_path


def _insert_session_with_jsonl(conn, session_id, project, jsonl_path,
                                jsonl_mtime):
    """Insert a sessions row including jsonl_path + jsonl_mtime
    (required for the FTS5 dispatch to resolve encoded_slug + freshness)."""
    conn.execute(
        "INSERT INTO sessions (session_id, session_name, project, "
        "  last_active_at, jsonl_path, jsonl_mtime) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, "test", project, "2026-05-18T10:00:00Z",
         jsonl_path, jsonl_mtime),
    )


def test_search_fts5_explicit_returns_hit_from_indexed_session(
    mock_db, tmp_path
):
    """--source fts5 finds content in an FTS5 DB that has the session
    indexed (regardless of staleness)."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    project = "C--code-test"
    encoded_slug = "C--code-test"
    sid = "fff11111-1111-1111-1111-111111111111"

    _build_fake_fts5_db(
        claude_dir, project, encoded_slug, sid,
        messages=[
            ("USER", None, "find the OAUTH phrase"),
            ("AI",   None, "no marker"),
        ],
        jsonl_mtime=1700000000.0,
    )
    # Session row points at a (non-existent) JSONL whose parent dir
    # name = encoded_slug. We never actually open the JSONL because
    # FTS5 dispatch short-circuits.
    jsonl_relpath = f"projects/{encoded_slug}/{sid}.jsonl"
    _insert_session_with_jsonl(
        mock_db, sid, project, jsonl_relpath, 1700000000.0,
    )
    mock_db.commit()

    hits = list(search(
        mock_db, "OAUTH", source_override="fts5", claude_dir=claude_dir,
    ))
    assert len(hits) == 1
    assert hits[0].source_type == "fts5"
    assert hits[0].role == "USER"
    assert "OAUTH" in hits[0].matched_text


def test_search_fts5_explicit_skips_unindexed_session(mock_db, tmp_path):
    """--source fts5 without an FTS5 DB on disk -> zero hits (NOT a
    fallback to grep). User explicitly asked for FTS5 only."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    sid = "fff22222-2222-2222-2222-222222222222"

    # Write a JSONL that *would* match if we walked it
    jsonl = _write_jsonl(tmp_path, [
        {"type": "user", "timestamp": "t",
         "message": {"role": "user", "content": "find the OAUTH phrase"}},
    ])
    _insert_session_with_jsonl(
        mock_db, sid, "proj", str(jsonl), 1700000000.0,
    )
    _insert_source(mock_db, sid, "proj", "jsonl", str(jsonl))
    mock_db.commit()

    # No FTS5 DB was built -> no hits
    hits = list(search(
        mock_db, "OAUTH", source_override="fts5", claude_dir=claude_dir,
    ))
    assert hits == []


def test_search_auto_uses_fts5_when_fresh(mock_db, tmp_path):
    """source_override=None + FTS5 fresh -> hit comes from FTS5,
    not from grep over the JSONL."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    project = "C--code-test"
    encoded_slug = "C--code-test"
    sid = "fff33333-3333-3333-3333-333333333333"

    # FTS5 contains "FROM_FTS5"; JSONL contains "FROM_JSONL" only.
    _build_fake_fts5_db(
        claude_dir, project, encoded_slug, sid,
        messages=[("USER", None, "search term FROM_FTS5 lives here")],
        jsonl_mtime=1700000000.0,
    )
    # Build a real on-disk JSONL with DIFFERENT content; if grep ran
    # against it we'd see FROM_JSONL, not FROM_FTS5.
    proj_dir = claude_dir / "projects" / encoded_slug
    proj_dir.mkdir(parents=True)
    jsonl_abs = proj_dir / f"{sid}.jsonl"
    jsonl_abs.write_text(
        '{"type":"user","timestamp":"t","message":{"content":"FROM_JSONL"}}\n',
        encoding="utf-8",
    )
    jsonl_relpath = f"projects/{encoded_slug}/{sid}.jsonl"
    _insert_session_with_jsonl(
        mock_db, sid, project, jsonl_relpath, 1700000000.0,
    )
    _insert_source(mock_db, sid, project, "jsonl", str(jsonl_abs))
    mock_db.commit()

    # Auto dispatch: FTS5 indexed with last_jsonl_mtime == session's
    # jsonl_mtime -> fresh -> use FTS5
    hits = list(search(mock_db, "FROM_FTS5", claude_dir=claude_dir))
    assert len(hits) == 1
    assert hits[0].source_type == "fts5"

    # And the grep-only term doesn't appear when FTS5 is picked
    hits = list(search(mock_db, "FROM_JSONL", claude_dir=claude_dir))
    assert hits == []


def test_search_auto_falls_through_to_grep_when_stale(mock_db, tmp_path):
    """source_override=None + FTS5 indexed at older mtime -> auto picks
    grep instead. The fresh-but-not-indexed content is found via JSONL
    walk."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    project = "C--code-test"
    encoded_slug = "C--code-test"
    sid = "fff44444-4444-4444-4444-444444444444"

    # FTS5 indexed at mtime=1000; session reports current mtime=2000.
    # -> Stale -> auto must fall through to grep.
    _build_fake_fts5_db(
        claude_dir, project, encoded_slug, sid,
        messages=[("USER", None, "STALE_TERM lives in FTS5")],
        jsonl_mtime=1000.0,  # what was indexed
    )
    proj_dir = claude_dir / "projects" / encoded_slug
    proj_dir.mkdir(parents=True)
    jsonl_abs = proj_dir / f"{sid}.jsonl"
    jsonl_abs.write_text(
        '{"type":"user","timestamp":"t","message":{"content":"FRESH_TERM lives in JSONL"}}\n',
        encoding="utf-8",
    )
    jsonl_relpath = f"projects/{encoded_slug}/{sid}.jsonl"
    _insert_session_with_jsonl(
        mock_db, sid, project, jsonl_relpath, 2000.0,  # current mtime
    )
    _insert_source(mock_db, sid, project, "jsonl", str(jsonl_abs))
    mock_db.commit()

    # FRESH_TERM is in JSONL only; grep finds it
    hits = list(search(mock_db, "FRESH_TERM", claude_dir=claude_dir))
    assert len(hits) == 1
    assert hits[0].source_type == "jsonl"

    # STALE_TERM is in FTS5 only; auto-dispatch fell through, so no hit
    hits = list(search(mock_db, "STALE_TERM", claude_dir=claude_dir))
    assert hits == []


def test_search_fts5_preserves_agent_role_label(mock_db, tmp_path):
    """FTS5 dispatch path renders AGENT:<subtype> roles correctly
    (parity with the JSONL walker's format)."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    project = "C--code-test"
    encoded_slug = "C--code-test"
    sid = "fff55555-5555-5555-5555-555555555555"

    _build_fake_fts5_db(
        claude_dir, project, encoded_slug, sid,
        messages=[
            ("AGENT", "explore", "OAUTH lives in an agent reply"),
            ("USER",  None,      "unrelated message"),
        ],
        jsonl_mtime=1700000000.0,
    )
    jsonl_relpath = f"projects/{encoded_slug}/{sid}.jsonl"
    _insert_session_with_jsonl(
        mock_db, sid, project, jsonl_relpath, 1700000000.0,
    )
    mock_db.commit()

    hits = list(search(
        mock_db, "OAUTH", source_override="fts5", claude_dir=claude_dir,
    ))
    assert len(hits) == 1
    assert hits[0].role == "AGENT:explore"


def test_search_no_claude_dir_disables_fts5(mock_db, tmp_path):
    """If the caller doesn't pass claude_dir, FTS5 is never consulted
    even when source_override is auto. Backwards-compatible default."""
    jsonl_path = _write_jsonl(tmp_path, [
        {"type": "user", "timestamp": "t",
         "message": {"role": "user", "content": "find ME"}},
    ])
    _insert_session_with_jsonl(
        mock_db, "sess-no-claude-dir", "proj", str(jsonl_path), 1700000000.0,
    )
    _insert_source(mock_db, "sess-no-claude-dir", "proj", "jsonl", str(jsonl_path))
    mock_db.commit()

    # No claude_dir -> FTS5 path entirely skipped; falls through to grep
    hits = list(search(mock_db, "ME"))  # default claude_dir=None
    assert len(hits) == 1
    assert hits[0].source_type == "jsonl"


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


# ── Source preference resolution + picker ────────────────────────────


def _row(source_type, source_path="/p"):
    """Tiny shim that mimics a sqlite3.Row for tests."""
    class R:
        def __init__(self, t, p):
            self._d = {"source_type": t, "source_path": p}

        def __getitem__(self, k):
            return self._d[k]

    return R(source_type, source_path)


def _session(session_id="sess-1", jsonl_path="", jsonl_mtime=0.0):
    """Mimic the sessions row shape the dispatcher reads."""
    class S:
        def __init__(self):
            self._d = {
                "session_id": session_id,
                "project": "proj",
                "jsonl_path": jsonl_path,
                "jsonl_mtime": jsonl_mtime,
            }

        def __getitem__(self, k):
            return self._d[k]

    return S()


# _resolve_preference: user-facing --source -> attempt order


def test_resolve_preference_auto_returns_full_default_order():
    assert _resolve_preference("auto") == _SOURCE_PREFERENCE
    assert _resolve_preference(None) == _SOURCE_PREFERENCE


def test_resolve_preference_default_lists_fts5_first():
    """v0.3.3 design: FTS5 is a first-class peer in the dispatch order,
    not a layer bolted on top. The default attempt order starts with
    FTS5 so an indexed-and-fresh session is served from the index."""
    assert _SOURCE_PREFERENCE[0] == "fts5"
    assert set(_SOURCE_PREFERENCE) == {"fts5", "convo", "sesslog", "jsonl"}


def test_resolve_preference_explicit_pins_single_source():
    assert _resolve_preference("fts5") == ("fts5",)
    assert _resolve_preference("convo") == ("convo",)
    assert _resolve_preference("jsonl") == ("jsonl",)


def test_resolve_preference_accepts_custom_default():
    """The default_preference arg lets the caller adapt to vault state
    (v0.3.4: dropping convo/sesslog when no logger present)."""
    custom = ("fts5", "jsonl")
    assert _resolve_preference(None, default_preference=custom) == custom
    assert _resolve_preference("auto", default_preference=custom) == custom
    # Explicit single source still wins, ignoring the custom default.
    assert _resolve_preference("convo", default_preference=custom) == ("convo",)


# v0.3.4: vault-aware default preference


def test_effective_default_with_logger_present_keeps_full_preference(mock_db):
    """If session_sources has at least one convo/sesslog row, the
    default preference stays at the full ("fts5", "convo", "sesslog",
    "jsonl") order."""
    mock_db.execute(
        "INSERT INTO sessions (session_id, session_name, project, last_active_at) "
        "VALUES (?, ?, ?, ?)",
        ("logger-sess", "x", "proj", "2026-05-18T10:00:00Z"),
    )
    mock_db.execute(
        "INSERT INTO session_sources "
        "(session_id, project, source_type, source_path, last_seen) "
        "VALUES (?, ?, 'convo', ?, ?)",
        ("logger-sess", "proj", "/tmp/x.convo", "2026-05-18T10:00:00Z"),
    )
    mock_db.commit()

    pref = effective_default_preference(mock_db)
    assert pref == _SOURCE_PREFERENCE


def test_effective_default_without_logger_drops_convo_sesslog(mock_db):
    """User with no claude-session-logger output -> preference collapses
    to ("fts5", "jsonl"). No wasted lookups against sources that
    can't exist for them."""
    mock_db.execute(
        "INSERT INTO sessions (session_id, session_name, project, last_active_at) "
        "VALUES (?, ?, ?, ?)",
        ("no-logger", "x", "proj", "2026-05-18T10:00:00Z"),
    )
    mock_db.execute(
        "INSERT INTO session_sources "
        "(session_id, project, source_type, source_path, last_seen) "
        "VALUES (?, ?, 'jsonl', ?, ?)",
        ("no-logger", "proj", "/tmp/x.jsonl", "2026-05-18T10:00:00Z"),
    )
    mock_db.commit()

    pref = effective_default_preference(mock_db)
    assert "convo" not in pref
    assert "sesslog" not in pref
    assert pref == ("fts5", "jsonl")


def test_effective_default_with_empty_db_drops_logger_sources(mock_db):
    """Fresh DB with no session_sources rows at all -> assume no logger
    until proven otherwise. (When sessions do get backed up, the
    logger detection updates naturally.)"""
    pref = effective_default_preference(mock_db)
    assert pref == ("fts5", "jsonl")


# _pick_source_for_session: walks preference, returns first available


def test_pick_source_prefers_first_available_in_preference():
    rows = [_row("jsonl"), _row("sesslog"), _row("convo")]
    sess = _session()
    # No claude_dir means fts5 is unavailable for this session; the
    # picker walks past it and lands on the first available file
    # source -- convo, per the default preference.
    src_type, handle = _pick_source_for_session(
        sess, rows, _SOURCE_PREFERENCE, claude_dir=None,
    )
    assert src_type == "convo"
    assert handle["source_type"] == "convo"


def test_pick_source_walks_through_unavailable_to_next():
    # Only jsonl exists; convo / sesslog rows missing. The picker
    # should still land on jsonl.
    sess = _session()
    src_type, handle = _pick_source_for_session(
        sess, [_row("jsonl")], _SOURCE_PREFERENCE, claude_dir=None,
    )
    assert src_type == "jsonl"


def test_pick_source_explicit_pin_only_that_source():
    rows = [_row("jsonl"), _row("convo")]
    sess = _session()
    src_type, handle = _pick_source_for_session(
        sess, rows, ("jsonl",), claude_dir=None,
    )
    assert src_type == "jsonl"


def test_pick_source_pinned_to_unavailable_returns_none():
    """A single-element preference pinned to a missing source returns
    (None, None) -- no fallback. Matches the v0.3.2 --source X
    behavior."""
    rows = [_row("jsonl")]
    sess = _session()
    src_type, handle = _pick_source_for_session(
        sess, rows, ("convo",), claude_dir=None,
    )
    assert src_type is None
    assert handle is None


def test_pick_source_empty_rows_no_fts5_returns_none():
    """No file sources AND no claude_dir for FTS5 -> nothing available
    in the entire preference list."""
    sess = _session()
    src_type, handle = _pick_source_for_session(
        sess, [], _SOURCE_PREFERENCE, claude_dir=None,
    )
    assert src_type is None


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


# ── session_filter: list-of-prefixes (v0.2.8) ────────────────────────


def test_search_session_filter_accepts_list_of_prefixes(mock_db, tmp_path):
    """Multi-prefix OR-match: hits from any matching session."""
    convo_a = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "MATCH a")])
    convo_b = tmp_path / "b.convo.log"
    convo_b.write_text("[[2026-05-16 10:00:00]] {USER: MATCH b}\n", encoding="utf-8")
    convo_c = tmp_path / "c.convo.log"
    convo_c.write_text("[[2026-05-16 10:00:00]] {USER: MATCH c}\n", encoding="utf-8")

    _insert_session(mock_db, "abc1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "a", "p")
    _insert_session(mock_db, "def2-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "b", "p")
    _insert_session(mock_db, "xyz9-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "c", "p")
    _insert_source(mock_db, "abc1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "p", "convo", str(convo_a))
    _insert_source(mock_db, "def2-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "p", "convo", str(convo_b))
    _insert_source(mock_db, "xyz9-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "p", "convo", str(convo_c))
    mock_db.commit()

    hits = list(search(mock_db, "MATCH", session_filter=["abc1", "def2"]))
    matched_prefixes = {h.session_id[:4] for h in hits}
    assert matched_prefixes == {"abc1", "def2"}


def test_search_session_filter_empty_list_returns_all(mock_db, tmp_path):
    """Empty list behaves like no filter -- all sessions visited."""
    convo = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "MATCH")])
    _insert_session(mock_db, "sess-1", "n", "p")
    _insert_source(mock_db, "sess-1", "p", "convo", str(convo))
    mock_db.commit()

    hits = list(search(mock_db, "MATCH", session_filter=[]))
    assert len(hits) == 1


def test_search_session_filter_single_string_still_works(mock_db, tmp_path):
    """Backward-compat: a bare str prefix still works (not just lists)."""
    convo = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "MATCH")])
    _insert_session(mock_db, "abc1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "n", "p")
    _insert_source(mock_db, "abc1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "p", "convo", str(convo))
    mock_db.commit()

    hits = list(search(mock_db, "MATCH", session_filter="abc1"))
    assert len(hits) == 1
    assert hits[0].session_id.startswith("abc1")


def test_search_session_filter_list_skips_empty_entries(mock_db, tmp_path):
    """Trailing/leading commas (passed as empty strings) are tolerated."""
    convo = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "MATCH")])
    _insert_session(mock_db, "abc1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "n", "p")
    _insert_source(mock_db, "abc1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "p", "convo", str(convo))
    mock_db.commit()

    hits = list(search(mock_db, "MATCH", session_filter=["abc1", "", "  "]))
    # Whitespace-only and empty entries are NOT filtered by search() -- the
    # CLI strip()s them before passing in. Here we just verify they don't
    # crash; a "  " prefix won't match anything in SQL LIKE.
    assert any(h.session_id.startswith("abc1") for h in hits)


# ── start_folder threaded into Hit (v0.2.8) ──────────────────────────


def test_search_hit_carries_start_folder(mock_db, tmp_path):
    """Hit.start_folder is populated from the sessions table for renderers."""
    convo = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "MATCH")])
    mock_db.execute(
        "INSERT INTO sessions (session_id, session_name, project, "
        "start_folder, last_active_at) VALUES (?, ?, ?, ?, ?)",
        ("sess-1", "name", "proj", "C:/code/x", "2026-05-16T10:00:00Z"),
    )
    _insert_source(mock_db, "sess-1", "proj", "convo", str(convo))
    mock_db.commit()

    hits = list(search(mock_db, "MATCH"))
    assert len(hits) == 1
    assert hits[0].start_folder == "C:/code/x"


# ── --sort: parity with csb list --sort (v0.3.0) ─────────────────────


def _seed_three_sessions_for_sort(mock_db, tmp_path):
    """Three sessions with distinct started_at / last_active_at / counts."""
    matches = []
    for sid, started, active, msgs, size in [
        ("aaa1", "2026-05-01T10:00:00Z", "2026-05-10T10:00:00Z", 100, 1000),  # oldest start, mid active, fewest msgs
        ("bbb2", "2026-05-05T10:00:00Z", "2026-05-15T10:00:00Z", 500, 5000),  # mid start, newest active, most msgs
        ("ccc3", "2026-05-10T10:00:00Z", "2026-05-05T10:00:00Z",  50,  500),  # newest start, oldest active, fewest size
    ]:
        full_id = f"{sid}-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        convo = tmp_path / f"{sid}.convo.log"
        convo.write_text(f"[[2026-05-16 10:00:00]] {{USER: MATCH-{sid}}}\n", encoding="utf-8")
        mock_db.execute(
            "INSERT INTO sessions (session_id, session_name, project, "
            "started_at, last_active_at, message_count, jsonl_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (full_id, sid, "p", started, active, msgs, size),
        )
        _insert_source(mock_db, full_id, "p", "convo", str(convo))
        matches.append(full_id)
    mock_db.commit()
    return matches


def test_search_sort_default_is_last_used(mock_db, tmp_path):
    """Default sort is last-used DESC (matches v0.2.6 implicit behavior)."""
    ids = _seed_three_sessions_for_sort(mock_db, tmp_path)
    hits = list(search(mock_db, "MATCH"))
    # bbb2 has newest last_active -> appears first
    assert hits[0].session_id == ids[1]


def test_search_sort_started_newest_first(mock_db, tmp_path):
    ids = _seed_three_sessions_for_sort(mock_db, tmp_path)
    hits = list(search(mock_db, "MATCH", sort_key="started"))
    # ccc3 has newest started_at -> first
    assert hits[0].session_id == ids[2]


def test_search_sort_oldest_first(mock_db, tmp_path):
    ids = _seed_three_sessions_for_sort(mock_db, tmp_path)
    hits = list(search(mock_db, "MATCH", sort_key="oldest"))
    # aaa1 has oldest started_at -> first
    assert hits[0].session_id == ids[0]


def test_search_sort_messages_most_first(mock_db, tmp_path):
    ids = _seed_three_sessions_for_sort(mock_db, tmp_path)
    hits = list(search(mock_db, "MATCH", sort_key="messages"))
    # bbb2 has most messages (500) -> first
    assert hits[0].session_id == ids[1]


def test_search_sort_size_largest_first(mock_db, tmp_path):
    ids = _seed_three_sessions_for_sort(mock_db, tmp_path)
    hits = list(search(mock_db, "MATCH", sort_key="size"))
    # bbb2 has largest jsonl_size (5000) -> first
    assert hits[0].session_id == ids[1]


def test_search_sort_unknown_key_raises_value_error(mock_db, tmp_path):
    convo = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "x")])
    _insert_session(mock_db, "s", "n", "p")
    _insert_source(mock_db, "s", "p", "convo", str(convo))
    mock_db.commit()
    with pytest.raises(ValueError, match="Unknown sort_key"):
        list(search(mock_db, "x", sort_key="bogus"))


# ── fetch_folders for --full-info level 2 (v0.2.10) ──────────────────


def test_search_fetch_folders_populates_hit_folders(mock_db, tmp_path):
    """fetch_folders=True -> Hit.folders contains rows from folder_usage."""
    convo = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "MATCH")])
    _insert_session(mock_db, "sess-1", "n", "p")
    _insert_source(mock_db, "sess-1", "p", "convo", str(convo))
    mock_db.execute(
        "INSERT INTO folder_usage (session_id, folder_path, usage_count, "
        "is_start_folder) VALUES (?, ?, ?, ?)",
        ("sess-1", "C:/code/x", 100, 1),
    )
    mock_db.execute(
        "INSERT INTO folder_usage (session_id, folder_path, usage_count, "
        "is_start_folder) VALUES (?, ?, ?, ?)",
        ("sess-1", "C:/code/y", 25, 0),
    )
    mock_db.commit()

    hits = list(search(mock_db, "MATCH", fetch_folders=True))
    assert len(hits) == 1
    folder_paths = {f["folder_path"] for f in hits[0].folders}
    assert folder_paths == {"C:/code/x", "C:/code/y"}


def test_search_fetch_folders_false_means_empty_folders(mock_db, tmp_path):
    """Default fetch_folders=False -> Hit.folders is empty list (no DB query)."""
    convo = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "MATCH")])
    _insert_session(mock_db, "sess-1", "n", "p")
    _insert_source(mock_db, "sess-1", "p", "convo", str(convo))
    mock_db.execute(
        "INSERT INTO folder_usage (session_id, folder_path, usage_count, "
        "is_start_folder) VALUES (?, ?, ?, ?)",
        ("sess-1", "C:/code/x", 100, 1),
    )
    mock_db.commit()

    hits = list(search(mock_db, "MATCH"))  # fetch_folders defaults to False
    assert len(hits) == 1
    assert hits[0].folders == []


def test_search_hit_carries_message_count_and_version(mock_db, tmp_path):
    """Hit.message_count and Hit.claude_version populated from sessions row."""
    convo = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "MATCH")])
    mock_db.execute(
        "INSERT INTO sessions (session_id, session_name, project, "
        "last_active_at, message_count, claude_version) VALUES "
        "(?, ?, ?, ?, ?, ?)",
        ("sess-1", "n", "p", "2026-05-16T10:00:00Z", 438, "2.1.50"),
    )
    _insert_source(mock_db, "sess-1", "p", "convo", str(convo))
    mock_db.commit()

    hits = list(search(mock_db, "MATCH"))
    assert hits[0].message_count == 438
    assert hits[0].claude_version == "2.1.50"

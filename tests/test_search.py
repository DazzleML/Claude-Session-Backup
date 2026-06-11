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
                        messages, jsonl_mtime, content_hash="deadbeef"):
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
    mark_session_indexed(conn, session_id, jsonl_mtime, content_hash, now_iso())
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


# ── v0.3.5: directory-scope mode (-d / -D + --min-strength) ───────────


def _build_fake_fts5_db_with_file_ops(
    claude_dir,
    project,
    encoded_slug,
    session_id,
    messages,
    file_ops,
    jsonl_mtime=1700000000.0,
):
    """Build a per-project FTS5 DB with both messages and file_operations.

    ``file_ops`` is a list of ``(operation, file_path, strength)`` tuples
    that get inserted into the file_operations table. ``messages`` uses
    the same shape as ``_build_fake_fts5_db``.

    Returns the DB path so tests can verify on-disk state.
    """
    import sqlite3 as _sqlite3
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
            # uuid namespaced by session_id so two sessions in one DB
            # don't collide on the unique index.
            (session_id, f"{session_id}-u{i}", i, role, role_subtype,
             content, f"2026-05-18T10:{i:02d}:00Z"),
        )
    for j, (operation, file_path, strength) in enumerate(file_ops):
        cur.execute(
            "INSERT INTO file_operations (session_id, message_index, "
            "  operation, file_path, strength, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, j, operation, file_path, strength,
             f"2026-05-18T10:{j:02d}:00Z"),
        )
    mark_session_indexed(conn, session_id, jsonl_mtime, "deadbeef", now_iso())
    conn.commit()
    conn.close()
    return db_path


def test_build_directory_globs_recursive():
    """``-d`` builds two GLOBs (one per separator), no excludes."""
    from claude_session_backup.search import _build_directory_globs
    include, exclude = _build_directory_globs(
        r"C:\code\foo", include_descendants=True,
    )
    assert include == [r"C:\code\foo\*", "C:/code/foo/*"]
    assert exclude is None


def test_build_directory_globs_folder_only():
    """``-D`` builds includes AND excludes (paths with extra separator)."""
    from claude_session_backup.search import _build_directory_globs
    include, exclude = _build_directory_globs(
        r"C:\code\foo", include_descendants=False,
    )
    assert include == [r"C:\code\foo\*", "C:/code/foo/*"]
    assert exclude == [r"C:\code\foo\*\*", "C:/code/foo/*/*"]


def test_build_directory_globs_strips_trailing_sep():
    """Trailing ``/`` or ``\\`` on input doesn't produce ``//`` / ``\\\\``."""
    from claude_session_backup.search import _build_directory_globs
    include, _ = _build_directory_globs(
        "C:/code/foo/", include_descendants=True,
    )
    assert include[1] == "C:/code/foo/*"  # no extra slash


def test_find_path_filtered_sessions_ranks_by_sum_strength(tmp_path):
    """Two sessions with different file-op profiles -- higher-strength
    session sorts first regardless of insertion order."""
    from claude_session_backup.search import (
        _build_directory_globs, find_path_filtered_sessions,
    )
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    # Build one DB containing two sessions touching the same dir.
    _build_fake_fts5_db_with_file_ops(
        claude_dir, "p", "p", "lightweight-sid",
        messages=[("USER", None, "x")],
        file_ops=[
            ("searched", "C:/code/foo/x.py", 1),
            ("read",     "C:/code/foo/y.py", 2),
        ],
    )
    db_path = _build_fake_fts5_db_with_file_ops(
        claude_dir, "p", "p", "heavyweight-sid",
        messages=[("USER", None, "y")],
        file_ops=[
            ("wrote",  "C:/code/foo/a.py", 3),
            ("edited", "C:/code/foo/b.py", 3),
            ("read",   "C:/code/foo/c.py", 2),
        ],
    )
    include, _ = _build_directory_globs(
        r"C:\code\foo", include_descendants=True,
    )
    rows = find_path_filtered_sessions(db_path, include)
    # Heavyweight (3+3+2 = 8) ranks before lightweight (1+2 = 3).
    assert [r[0] for r in rows] == ["heavyweight-sid", "lightweight-sid"]
    assert rows[0][1] == 8 and rows[0][2] == 3
    assert rows[1][1] == 3 and rows[1][2] == 2


def test_find_path_filtered_sessions_min_strength_filters_low_rows(tmp_path):
    """``min_strength=2`` excludes Grep/searched rows (strength=1)."""
    from claude_session_backup.search import (
        _build_directory_globs, find_path_filtered_sessions,
    )
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    db_path = _build_fake_fts5_db_with_file_ops(
        claude_dir, "p", "p", "sid-1",
        messages=[("USER", None, "x")],
        file_ops=[
            ("searched", "C:/code/foo/x.py", 1),
            ("read",     "C:/code/foo/y.py", 2),
        ],
    )
    include, _ = _build_directory_globs(
        r"C:\code\foo", include_descendants=True,
    )
    rows = find_path_filtered_sessions(db_path, include, min_strength=2)
    assert len(rows) == 1
    assert rows[0][0] == "sid-1"
    assert rows[0][1] == 2  # only the read counted
    assert rows[0][2] == 1  # one file_op survived


def test_find_path_filtered_sessions_folder_only_excludes_descendants(tmp_path):
    """``-D``-style exclude_descendants drops paths in subdirectories."""
    from claude_session_backup.search import (
        _build_directory_globs, find_path_filtered_sessions,
    )
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    db_path = _build_fake_fts5_db_with_file_ops(
        claude_dir, "p", "p", "sid-1",
        messages=[("USER", None, "x")],
        file_ops=[
            ("read", "C:/code/foo/top.py",       2),  # immediate child
            ("read", "C:/code/foo/sub/deep.py",  2),  # one level deeper
        ],
    )
    include, exclude = _build_directory_globs(
        r"C:\code\foo", include_descendants=False,
    )
    rows = find_path_filtered_sessions(db_path, include, exclude)
    # Only the immediate child survives the NOT (GLOB '*/*/*') exclude.
    assert rows[0][2] == 1


def test_find_path_filtered_sessions_missing_db_returns_empty(tmp_path):
    """Non-existent DB path returns [] (graceful skip)."""
    from claude_session_backup.search import find_path_filtered_sessions
    rows = find_path_filtered_sessions(
        tmp_path / "nope.db", [r"C:\code\foo\*"],
    )
    assert rows == []


def test_search_dir_scope_orders_sessions_by_strength(mock_db, tmp_path):
    """End-to-end: dir-scope dispatch returns hits in
    SUM(strength)-DESC order across multiple sessions."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()

    # heavy session: strength 3+3 = 6, indexed
    _build_fake_fts5_db_with_file_ops(
        claude_dir, "C--code-foo", "C--code-foo", "heavy-sid",
        messages=[("USER", None, "needle in heavy")],
        file_ops=[
            ("wrote",  "C:/code/foo/a.py", 3),
            ("edited", "C:/code/foo/b.py", 3),
        ],
    )
    # light session: strength 2, indexed in same DB
    _build_fake_fts5_db_with_file_ops(
        claude_dir, "C--code-foo", "C--code-foo", "light-sid",
        messages=[("USER", None, "needle in light")],
        file_ops=[("read", "C:/code/foo/x.py", 2)],
    )
    # Both sessions exist in the main DB (encoded_slug = parent of jsonl)
    _insert_session_with_jsonl(
        mock_db, "heavy-sid", "C--code-foo",
        "projects/C--code-foo/heavy-sid.jsonl", 1700000000.0,
    )
    _insert_session_with_jsonl(
        mock_db, "light-sid", "C--code-foo",
        "projects/C--code-foo/light-sid.jsonl", 1700000000.0,
    )
    mock_db.commit()

    hits = list(search(
        mock_db, "needle", claude_dir=claude_dir,
        dir_scope={
            "abs_path": r"C:\code\foo",
            "include_descendants": True,
            "min_strength": 1,
        },
    ))
    # Heavy session's hit should be first (higher SUM(strength)).
    assert [h.session_id for h in hits] == ["heavy-sid", "light-sid"]
    assert hits[0].strength_sum == 6 and hits[0].file_op_count == 2
    assert hits[1].strength_sum == 2 and hits[1].file_op_count == 1
    # All hits come from the FTS5 source.
    assert all(h.source_type == "fts5" for h in hits)


def test_search_dir_scope_empty_pattern_matches_all_in_ranked(mock_db, tmp_path):
    """Empty pattern + dir-scope -- every event in matched sessions
    surfaces (rest of search() honors empty = match-all semantics)."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    _build_fake_fts5_db_with_file_ops(
        claude_dir, "p", "p", "sid-1",
        messages=[
            ("USER", None, "anything"),
            ("AI",   None, "anything else"),
        ],
        file_ops=[("wrote", "C:/code/foo/a.py", 3)],
    )
    _insert_session_with_jsonl(
        mock_db, "sid-1", "p", "projects/p/sid-1.jsonl", 1700000000.0,
    )
    mock_db.commit()

    hits = list(search(
        mock_db, "", claude_dir=claude_dir,
        dir_scope={
            "abs_path": r"C:\code\foo",
            "include_descendants": True,
            "min_strength": 1,
        },
    ))
    assert len(hits) == 2  # both events surfaced


def test_search_dir_scope_min_strength_filters_low_signal(mock_db, tmp_path):
    """``min_strength=3`` keeps only edit/wrote/notebook_edit sessions."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    # session-A: only a Grep (strength=1) -- should be filtered out
    _build_fake_fts5_db_with_file_ops(
        claude_dir, "p", "p", "grep-only-sid",
        messages=[("USER", None, "needle")],
        file_ops=[("searched", "C:/code/foo/x.py", 1)],
    )
    # session-B: a wrote (strength=3) -- survives
    _build_fake_fts5_db_with_file_ops(
        claude_dir, "p", "p", "wrote-sid",
        messages=[("USER", None, "needle here")],
        file_ops=[("wrote", "C:/code/foo/y.py", 3)],
    )
    _insert_session_with_jsonl(
        mock_db, "grep-only-sid", "p",
        "projects/p/grep-only-sid.jsonl", 1700000000.0,
    )
    _insert_session_with_jsonl(
        mock_db, "wrote-sid", "p",
        "projects/p/wrote-sid.jsonl", 1700000000.0,
    )
    mock_db.commit()

    hits = list(search(
        mock_db, "needle", claude_dir=claude_dir,
        dir_scope={
            "abs_path": r"C:\code\foo",
            "include_descendants": True,
            "min_strength": 3,
        },
    ))
    # Only the wrote session passed the strength filter.
    assert [h.session_id for h in hits] == ["wrote-sid"]


def test_search_dir_scope_skips_session_not_in_main_db(mock_db, tmp_path):
    """Session present in FTS5 but missing from main sessions table is
    dropped quietly -- no orphan hit."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    _build_fake_fts5_db_with_file_ops(
        claude_dir, "p", "p", "orphan-sid",
        messages=[("USER", None, "x")],
        file_ops=[("wrote", "C:/code/foo/a.py", 3)],
    )
    # NOTE: no _insert_session_with_jsonl call -- orphan in main DB.
    mock_db.commit()

    hits = list(search(
        mock_db, "x", claude_dir=claude_dir,
        dir_scope={
            "abs_path": r"C:\code\foo",
            "include_descendants": True,
            "min_strength": 1,
        },
    ))
    assert hits == []


def test_search_dir_scope_no_fts_dir_returns_empty(mock_db, tmp_path):
    """Vault with no ``csb-fts/`` directory yet -> empty result, no
    crash."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    # No csb-fts directory created. dir_scope path should return cleanly.
    hits = list(search(
        mock_db, "x", claude_dir=claude_dir,
        dir_scope={
            "abs_path": r"C:\code\foo",
            "include_descendants": True,
            "min_strength": 1,
        },
    ))
    assert hits == []


def test_search_dir_scope_carries_strength_into_hit(mock_db, tmp_path):
    """Hit.strength_sum + Hit.file_op_count populated for dir-scope hits
    so renderers can show the ``[N file-ops, strength=S]`` suffix."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    _build_fake_fts5_db_with_file_ops(
        claude_dir, "p", "p", "sid-1",
        messages=[("USER", None, "FIND_ME")],
        file_ops=[
            ("wrote",  "C:/code/foo/a.py", 3),
            ("edited", "C:/code/foo/b.py", 3),
            ("read",   "C:/code/foo/c.py", 2),
        ],
    )
    _insert_session_with_jsonl(
        mock_db, "sid-1", "p", "projects/p/sid-1.jsonl", 1700000000.0,
    )
    mock_db.commit()

    hits = list(search(
        mock_db, "FIND_ME", claude_dir=claude_dir,
        dir_scope={
            "abs_path": r"C:\code\foo",
            "include_descendants": True,
            "min_strength": 1,
        },
    ))
    assert len(hits) == 1
    assert hits[0].strength_sum == 8
    assert hits[0].file_op_count == 3


def test_search_dir_scope_non_dir_hits_leave_strength_at_zero(mock_db, tmp_path):
    """A regular (non-dir-scope) search Hit keeps strength_sum=0 /
    file_op_count=0 so the renderer suppresses the suffix."""
    convo = _write_convo(tmp_path, [("2026-05-16 10:00:00", "USER", "needle")])
    _insert_session(mock_db, "sess-1", "test", "proj")
    _insert_source(mock_db, "sess-1", "proj", "convo", str(convo))
    mock_db.commit()

    hits = list(search(mock_db, "needle"))
    assert len(hits) == 1
    assert hits[0].strength_sum == 0
    assert hits[0].file_op_count == 0


# ── v0.3.5: transcript_path resolution ────────────────────────────────


def test_best_transcript_path_prefers_convo():
    """convo > sesslog > jsonl in the resolver preference."""
    from claude_session_backup.search import _best_transcript_path

    class _Row(dict):
        def keys(self): return super().keys()
        def __getitem__(self, k): return super().__getitem__(k)

    source_rows = [
        _Row(source_type="jsonl",   source_path="/path/x.jsonl"),
        _Row(source_type="convo",   source_path="/path/x.convo.log"),
        _Row(source_type="sesslog", source_path="/path/x.sesslog.log"),
    ]
    session_row = _Row(jsonl_path="projects/p/x.jsonl")
    assert _best_transcript_path(
        source_rows, session_row, claude_dir=None,
    ) == "/path/x.convo.log"


def test_best_transcript_path_falls_through_to_sesslog():
    """No convo row -> sesslog wins."""
    from claude_session_backup.search import _best_transcript_path

    class _Row(dict):
        def keys(self): return super().keys()
        def __getitem__(self, k): return super().__getitem__(k)

    source_rows = [
        _Row(source_type="sesslog", source_path="/path/x.sesslog.log"),
        _Row(source_type="jsonl",   source_path="/path/x.jsonl"),
    ]
    session_row = _Row(jsonl_path="projects/p/x.jsonl")
    assert _best_transcript_path(
        source_rows, session_row, claude_dir=None,
    ) == "/path/x.sesslog.log"


def test_best_transcript_path_falls_back_to_sessions_jsonl(tmp_path):
    """No session_sources rows -> resolve sessions.jsonl_path against
    claude_dir (the FTS5-only session case)."""
    from claude_session_backup.search import _best_transcript_path

    class _Row(dict):
        def keys(self): return super().keys()
        def __getitem__(self, k): return super().__getitem__(k)

    session_row = _Row(jsonl_path="projects/p/abc.jsonl")
    out = _best_transcript_path([], session_row, claude_dir=tmp_path)
    assert out == str(tmp_path / "projects/p/abc.jsonl")


def test_best_transcript_path_returns_none_when_nothing_available():
    """No source_rows, no claude_dir -> nothing to return."""
    from claude_session_backup.search import _best_transcript_path

    class _Row(dict):
        def keys(self): return super().keys()
        def __getitem__(self, k): return super().__getitem__(k)

    session_row = _Row(jsonl_path="projects/p/x.jsonl")
    assert _best_transcript_path(
        [], session_row, claude_dir=None,
    ) is None


def test_search_fts5_hit_populates_transcript_path_from_convo(
    mock_db, tmp_path
):
    """An FTS5 hit yields Hit.transcript_path pointing at the session's
    .convo log (or sesslog / jsonl, in preference order), NOT at the
    per-project FTS5 DB. Closes the v0.3.5 --files-only parity gap."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    sid = "fff44444-4444-4444-4444-444444444444"
    project = "C--code-test"
    encoded_slug = "C--code-test"

    _build_fake_fts5_db(
        claude_dir, project, encoded_slug, sid,
        messages=[("USER", None, "find the OAUTH phrase")],
        jsonl_mtime=1700000000.0,
    )
    jsonl_relpath = f"projects/{encoded_slug}/{sid}.jsonl"
    _insert_session_with_jsonl(
        mock_db, sid, project, jsonl_relpath, 1700000000.0,
    )
    # Convo file recorded -> transcript_path should point HERE, not at
    # the FTS5 DB.
    convo_path = str(tmp_path / "fake.convo.log")
    _insert_source(mock_db, sid, project, "convo", convo_path)
    _insert_source(mock_db, sid, project, "jsonl",
                   str(tmp_path / "fake.jsonl"))
    mock_db.commit()

    hits = list(search(
        mock_db, "OAUTH", source_override="fts5", claude_dir=claude_dir,
    ))
    assert len(hits) == 1
    assert hits[0].transcript_path == convo_path  # NOT the .db path
    assert hits[0].source_path.endswith(".db")    # source_path stays honest


def test_search_fts5_hit_falls_back_to_jsonl_when_no_convo(
    mock_db, tmp_path
):
    """No convo/sesslog rows -> transcript_path falls back to the
    sessions.jsonl_path resolved against claude_dir."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    sid = "fff55555-5555-5555-5555-555555555555"
    project = "C--code-test"
    encoded_slug = "C--code-test"

    _build_fake_fts5_db(
        claude_dir, project, encoded_slug, sid,
        messages=[("USER", None, "find the OAUTH phrase")],
        jsonl_mtime=1700000000.0,
    )
    jsonl_relpath = f"projects/{encoded_slug}/{sid}.jsonl"
    _insert_session_with_jsonl(
        mock_db, sid, project, jsonl_relpath, 1700000000.0,
    )
    # NO _insert_source calls -- pure FTS5-indexed session.
    mock_db.commit()

    hits = list(search(
        mock_db, "OAUTH", source_override="fts5", claude_dir=claude_dir,
    ))
    assert len(hits) == 1
    expected = str(claude_dir / jsonl_relpath)
    assert hits[0].transcript_path == expected


def test_search_file_based_hit_transcript_path_matches_source_path(
    mock_db, tmp_path
):
    """For convo/sesslog/jsonl dispatch hits, transcript_path should
    equal source_path -- the same file the dispatcher walked."""
    convo = _write_convo(tmp_path, [
        ("2026-05-16 10:00:00", "USER", "the oauth marker"),
    ])
    _insert_session(mock_db, "sess-1", "test", "proj")
    _insert_source(mock_db, "sess-1", "proj", "convo", str(convo))
    mock_db.commit()

    hits = list(search(mock_db, "oauth"))
    assert len(hits) == 1
    assert hits[0].transcript_path == hits[0].source_path
    assert hits[0].source_type == "convo"


def test_search_dir_scope_hit_populates_transcript_path(mock_db, tmp_path):
    """Dir-scope dispatcher resolves transcript_path the same way --
    --files-only with -d should return navigable files, not DB paths."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    _build_fake_fts5_db_with_file_ops(
        claude_dir, "p", "p", "sid-tp",
        messages=[("USER", None, "needle")],
        file_ops=[("wrote", "C:/code/foo/a.py", 3)],
    )
    _insert_session_with_jsonl(
        mock_db, "sid-tp", "p", "projects/p/sid-tp.jsonl", 1700000000.0,
    )
    sesslog_path = str(tmp_path / "fake.sesslog.log")
    _insert_source(mock_db, "sid-tp", "p", "sesslog", sesslog_path)
    mock_db.commit()

    hits = list(search(
        mock_db, "needle", claude_dir=claude_dir,
        dir_scope={
            "abs_path": r"C:\code\foo",
            "include_descendants": True,
            "min_strength": 1,
        },
    ))
    assert len(hits) == 1
    assert hits[0].transcript_path == sesslog_path
    # source_path is still the DB (honest about dispatch)
    assert hits[0].source_path.endswith(".db")


# ── v0.3.5: --only-aware limit cap ────────────────────────────────────


def test_cap_hits_by_sessions_keeps_all_hits_of_first_n_sessions():
    """With unit=sessions, --limit N keeps every hit from the first N
    distinct session_ids and drops the rest."""
    from claude_session_backup.search import cap_hits_by_output_unit
    hits = [
        Hit(session_id="A", session_name="a", project="p",
            last_active_at=None, source_type="convo",
            source_path="/a.log", line_num=1, role="USER",
            timestamp=None, matched_text="x"),
        Hit(session_id="A", session_name="a", project="p",
            last_active_at=None, source_type="convo",
            source_path="/a.log", line_num=2, role="AI",
            timestamp=None, matched_text="x"),
        Hit(session_id="B", session_name="b", project="p",
            last_active_at=None, source_type="convo",
            source_path="/b.log", line_num=1, role="USER",
            timestamp=None, matched_text="x"),
        Hit(session_id="C", session_name="c", project="p",
            last_active_at=None, source_type="convo",
            source_path="/c.log", line_num=1, role="USER",
            timestamp=None, matched_text="x"),
    ]
    capped = cap_hits_by_output_unit(hits, user_limit=2, unit="sessions")
    # Keeps both A hits and the B hit -- 2 distinct sessions.
    sids = [h.session_id for h in capped]
    assert sids == ["A", "A", "B"]


def test_cap_hits_by_files_keys_on_transcript_path():
    """With unit=files, the cap keys on Hit.transcript_path (falling back
    to source_path when transcript_path is None)."""
    from claude_session_backup.search import cap_hits_by_output_unit
    hits = [
        Hit(session_id="A", session_name="a", project="p",
            last_active_at=None, source_type="fts5",
            source_path="/a.db", line_num=1, role="USER",
            timestamp=None, matched_text="x",
            transcript_path="/a.convo.log"),
        Hit(session_id="B", session_name="b", project="p",
            last_active_at=None, source_type="fts5",
            source_path="/b.db", line_num=1, role="USER",
            timestamp=None, matched_text="x",
            transcript_path="/a.convo.log"),  # SAME transcript as A!
        Hit(session_id="C", session_name="c", project="p",
            last_active_at=None, source_type="fts5",
            source_path="/c.db", line_num=1, role="USER",
            timestamp=None, matched_text="x",
            transcript_path="/c.convo.log"),
    ]
    capped = cap_hits_by_output_unit(hits, user_limit=1, unit="files")
    # Only the unique transcript "/a.convo.log" survives -- both A and B
    # share it, but the cap=1 stops before adding C's distinct file.
    paths = [h.transcript_path for h in capped]
    assert paths == ["/a.convo.log", "/a.convo.log"]


def test_cap_hits_by_unit_invalid_unit_returns_unchanged():
    """An unrecognized unit returns the input list unchanged."""
    from claude_session_backup.search import cap_hits_by_output_unit
    hits = [
        Hit(session_id="A", session_name="a", project="p",
            last_active_at=None, source_type="convo",
            source_path="/a.log", line_num=1, role="USER",
            timestamp=None, matched_text="x"),
    ]
    assert cap_hits_by_output_unit(hits, 5, unit="bogus") == hits


def test_cap_hits_by_unit_limit_zero_keeps_nothing():
    """user_limit=0 means zero output rows -> empty list."""
    from claude_session_backup.search import cap_hits_by_output_unit
    hits = [
        Hit(session_id="A", session_name="a", project="p",
            last_active_at=None, source_type="convo",
            source_path="/a.log", line_num=1, role="USER",
            timestamp=None, matched_text="x"),
    ]
    assert cap_hits_by_output_unit(hits, 0, unit="sessions") == []


def test_cap_hits_by_unit_limit_exceeds_available():
    """user_limit larger than the number of distinct units returns all
    hits (no early break)."""
    from claude_session_backup.search import cap_hits_by_output_unit
    hits = [
        Hit(session_id="A", session_name="a", project="p",
            last_active_at=None, source_type="convo",
            source_path="/a.log", line_num=1, role="USER",
            timestamp=None, matched_text="x"),
        Hit(session_id="B", session_name="b", project="p",
            last_active_at=None, source_type="convo",
            source_path="/b.log", line_num=1, role="USER",
            timestamp=None, matched_text="x"),
    ]
    assert cap_hits_by_output_unit(hits, 99, unit="sessions") == hits


def test_search_fts5_regex_bypasses_match_and_finds_hits(mock_db, tmp_path):
    """Regression for the v0.3.3 bug exposed by v0.3.5 dir-scope:
    FTS5 MATCH doesn't understand regex syntax (``\\d``, ``|``, ``?``).
    With ``regex=True`` we must bypass MATCH and do a full-session scan
    so the Python regex matcher actually gets to filter, otherwise zero
    candidates ever surface and the user sees an empty result for what
    is clearly a matching pattern."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    sid = "fff66666-6666-6666-6666-666666666666"
    project = "C--code-test"
    encoded_slug = "C--code-test"

    _build_fake_fts5_db(
        claude_dir, project, encoded_slug, sid,
        messages=[
            ("USER", None, "talk about FTS5 indexing"),
            ("AI",   None, "yes the fts5 backend is fast"),
        ],
        jsonl_mtime=1700000000.0,
    )
    jsonl_relpath = f"projects/{encoded_slug}/{sid}.jsonl"
    _insert_session_with_jsonl(
        mock_db, sid, project, jsonl_relpath, 1700000000.0,
    )
    mock_db.commit()

    # regex='FTS\\d?|fts5' should match BOTH messages.
    hits = list(search(
        mock_db, r"FTS\d?|fts5",
        regex=True, source_override="fts5", claude_dir=claude_dir,
    ))
    assert len(hits) == 2
    # Same query with regex=False (literal) doesn't have regex syntax in
    # the text -- should only match the literal "FTS5" message
    hits_lit = list(search(
        mock_db, r"FTS\d?|fts5",
        regex=False, source_override="fts5", claude_dir=claude_dir,
    ))
    assert len(hits_lit) == 0  # literal pattern absent from corpus


def test_search_dir_scope_regex_bypasses_match(mock_db, tmp_path):
    """Same regex-bypass fix applies to the dir-scope FTS5 dispatch."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    _build_fake_fts5_db_with_file_ops(
        claude_dir, "p", "p", "rx-sid",
        messages=[("USER", None, "talk about FTS5 indexing")],
        file_ops=[("wrote", "C:/code/foo/a.py", 3)],
    )
    _insert_session_with_jsonl(
        mock_db, "rx-sid", "p",
        "projects/p/rx-sid.jsonl", 1700000000.0,
    )
    mock_db.commit()

    hits = list(search(
        mock_db, r"FTS\d?|fts5",
        regex=True, claude_dir=claude_dir,
        dir_scope={
            "abs_path": r"C:\code\foo",
            "include_descendants": True,
            "min_strength": 1,
        },
    ))
    assert len(hits) == 1
    assert "FTS5" in hits[0].matched_text


def test_search_dir_scope_excludes_descendants_with_folder_only(mock_db, tmp_path):
    """``include_descendants=False`` (-D) drops sessions whose only
    matches are inside subdirectories."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    # session-A only touches subdir files -- excluded under -D
    _build_fake_fts5_db_with_file_ops(
        claude_dir, "p", "p", "subdir-sid",
        messages=[("USER", None, "needle")],
        file_ops=[("wrote", "C:/code/foo/sub/deep.py", 3)],
    )
    # session-B touches an immediate-child file -- survives -D
    _build_fake_fts5_db_with_file_ops(
        claude_dir, "p", "p", "top-sid",
        messages=[("USER", None, "needle here")],
        file_ops=[("wrote", "C:/code/foo/top.py", 3)],
    )
    _insert_session_with_jsonl(
        mock_db, "subdir-sid", "p",
        "projects/p/subdir-sid.jsonl", 1700000000.0,
    )
    _insert_session_with_jsonl(
        mock_db, "top-sid", "p",
        "projects/p/top-sid.jsonl", 1700000000.0,
    )
    mock_db.commit()

    hits = list(search(
        mock_db, "needle", claude_dir=claude_dir,
        dir_scope={
            "abs_path": r"C:\code\foo",
            "include_descendants": False,
            "min_strength": 1,
        },
    ))
    assert [h.session_id for h in hits] == ["top-sid"]


# == #36: FTS5 content-hash freshness rescue + sesslog conversation probe ===


def test_search_auto_fts5_fresh_via_content_hash_when_mtime_stale(
    mock_db, tmp_path
):
    """#36 fix 1: mtime says stale but the stored content hash matches the
    CURRENT file bytes -> FTS5 is fresh (a restore / byte-identical rewrite
    only moved the mtime). Auto-dispatch uses FTS5, not grep."""
    from claude_session_backup.fts5_importer import _content_hash
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    project = "C--code-test"
    encoded_slug = "C--code-test"
    sid = "fff66666-6666-6666-6666-666666666666"

    proj_dir = claude_dir / "projects" / encoded_slug
    proj_dir.mkdir(parents=True)
    jsonl_abs = proj_dir / f"{sid}.jsonl"
    jsonl_abs.write_text(
        '{"type":"user","timestamp":"t","message":{"content":"FROM_JSONL"}}\n',
        encoding="utf-8",
    )
    # FTS5 indexed at OLD mtime but with the REAL hash of the current bytes.
    _build_fake_fts5_db(
        claude_dir, project, encoded_slug, sid,
        messages=[("USER", None, "RESCUED_TERM lives in FTS5")],
        jsonl_mtime=1000.0,
        content_hash=_content_hash(jsonl_abs),
    )
    jsonl_relpath = f"projects/{encoded_slug}/{sid}.jsonl"
    _insert_session_with_jsonl(
        mock_db, sid, project, jsonl_relpath, 2000.0,  # mtime moved forward
    )
    _insert_source(mock_db, sid, project, "jsonl", str(jsonl_abs))
    mock_db.commit()

    # The hash rescue keeps FTS5 fresh -> RESCUED_TERM found via fts5.
    hits = list(search(mock_db, "RESCUED_TERM", claude_dir=claude_dir))
    assert len(hits) == 1
    assert hits[0].source_type == "fts5"
    # And grep did NOT run (FROM_JSONL is on disk only).
    assert list(search(mock_db, "FROM_JSONL", claude_dir=claude_dir)) == []


def _write_shell_only_sesslog(path):
    """A .sesslog with shell commands/output but ZERO conversation blocks."""
    path.write_text(
        "$ git status\n"
        "On branch main\n"
        "nothing to commit, working tree clean\n"
        "$ python -m pytest -q\n"
        "880 passed\n",
        encoding="utf-8",
    )


def test_search_auto_shell_only_sesslog_falls_through_to_jsonl(
    mock_db, tmp_path
):
    """#36 fix 2 (the b6a4929f dead-end): a shell-only sesslog has no
    conversation blocks -> auto-dispatch must keep walking to the jsonl
    that HAS the content, instead of stopping at zero matches."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    project = "C--code-test"
    encoded_slug = "C--code-test"
    sid = "fff77777-7777-7777-7777-777777777777"

    sesslog = tmp_path / ".sesslog_bash.log"
    _write_shell_only_sesslog(sesslog)
    proj_dir = claude_dir / "projects" / encoded_slug
    proj_dir.mkdir(parents=True)
    jsonl_abs = proj_dir / f"{sid}.jsonl"
    jsonl_abs.write_text(
        '{"type":"user","timestamp":"t","message":{"content":"NEEDLE in jsonl"}}\n',
        encoding="utf-8",
    )
    _insert_session_with_jsonl(
        mock_db, sid, project, f"projects/{encoded_slug}/{sid}.jsonl", 2000.0,
    )
    _insert_source(mock_db, sid, project, "sesslog", str(sesslog))
    _insert_source(mock_db, sid, project, "jsonl", str(jsonl_abs))
    mock_db.commit()

    hits = list(search(mock_db, "NEEDLE", claude_dir=claude_dir))
    assert len(hits) == 1
    assert hits[0].source_type == "jsonl", (
        "dispatch dead-ended at the shell-only sesslog instead of "
        "falling through to the jsonl"
    )


def test_search_auto_sesslog_with_convo_blocks_still_used(mock_db, tmp_path):
    """#36 guard: the probe must NOT over-skip -- a sesslog that DOES carry
    conversation blocks keeps its place in the dispatch order."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    project = "C--code-test"
    sid = "fff88888-8888-8888-8888-888888888888"

    sesslog = tmp_path / ".sesslog_mixed.log"
    sesslog.write_text(
        "$ git status\n"
        "[[2026-05-18 10:00:00]] {USER: NEEDLE2 in the sesslog convo}\n",
        encoding="utf-8",
    )
    _insert_session(mock_db, sid, "mixed", project)
    _insert_source(mock_db, sid, project, "sesslog", str(sesslog))
    mock_db.commit()

    hits = list(search(mock_db, "NEEDLE2", claude_dir=claude_dir))
    assert len(hits) == 1
    assert hits[0].source_type == "sesslog"


def test_search_explicit_sesslog_shell_only_no_fallthrough(mock_db, tmp_path):
    """#36 contract pin: explicit --source sesslog on a shell-only log
    returns zero hits WITHOUT falling through -- the user asked for that
    channel specifically."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    project = "C--code-test"
    encoded_slug = "C--code-test"
    sid = "fff99999-9999-9999-9999-999999999999"

    sesslog = tmp_path / ".sesslog_shell.log"
    _write_shell_only_sesslog(sesslog)
    proj_dir = claude_dir / "projects" / encoded_slug
    proj_dir.mkdir(parents=True)
    jsonl_abs = proj_dir / f"{sid}.jsonl"
    jsonl_abs.write_text(
        '{"type":"user","timestamp":"t","message":{"content":"NEEDLE3 here"}}\n',
        encoding="utf-8",
    )
    _insert_session_with_jsonl(
        mock_db, sid, project, f"projects/{encoded_slug}/{sid}.jsonl", 2000.0,
    )
    _insert_source(mock_db, sid, project, "sesslog", str(sesslog))
    _insert_source(mock_db, sid, project, "jsonl", str(jsonl_abs))
    mock_db.commit()

    hits = list(search(
        mock_db, "NEEDLE3", source_override="sesslog", claude_dir=claude_dir,
    ))
    assert hits == []

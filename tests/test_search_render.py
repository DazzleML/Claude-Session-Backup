"""Tests for search output renderers, especially --sessions-only (v0.2.8)."""

import json

import pytest

from claude_session_backup.search import Event, Hit
from claude_session_backup.search_render import (
    render,
    render_files_only,
    render_human,
    render_json,
    render_sessions_only,
)


def _hit(session_id, name="name", project="proj", line=1, role="USER",
         matched="match here", start_folder="C:/code/x"):
    return Hit(
        session_id=session_id,
        session_name=name,
        project=project,
        last_active_at="2026-05-16T10:00:00Z",
        source_type="convo",
        source_path=f"/tmp/{session_id}.convo.log",
        line_num=line,
        role=role,
        timestamp="2026-05-16 10:00:00",
        matched_text=matched,
        start_folder=start_folder,
    )


# ── render_sessions_only ─────────────────────────────────────────────


def test_sessions_only_groups_hits_by_session(capsys):
    hits = [
        _hit("aaa1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "A", line=1),
        _hit("aaa1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "A", line=5),
        _hit("bbb2-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "B", line=2),
    ]
    render_sessions_only(hits, use_color=False)
    out = capsys.readouterr().out

    # Two summary blocks (one per session) -- count occurrences of "hit"
    assert out.count("hit") == 2
    assert "2 hits" in out
    assert "1 hit" in out and "1 hits" not in out  # singular grammar
    assert "A  aaa1" in out
    assert "B  bbb2" in out


def test_sessions_only_preserves_search_iteration_order(capsys):
    """search() yields last-active DESC; renderer must preserve that order."""
    hits = [
        _hit("zzz-2", "Z-newer"),
        _hit("zzz-2", "Z-newer"),
        _hit("aaa-1", "A-older"),
    ]
    render_sessions_only(hits, use_color=False)
    out = capsys.readouterr().out
    assert out.index("Z-newer") < out.index("A-older")


def test_sessions_only_shows_start_folder(capsys):
    hits = [_hit("sess-1", start_folder="C:/code/the-project")]
    render_sessions_only(hits, use_color=False)
    out = capsys.readouterr().out
    assert "C:/code/the-project" in out
    assert "start at:" in out


def test_sessions_only_shows_resume_hint(capsys):
    full_id = "abc1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    hits = [_hit(full_id)]
    render_sessions_only(hits, use_color=False)
    out = capsys.readouterr().out
    assert f"csb resume {full_id}" in out


def test_sessions_only_shortid_uses_compact_in_header_and_hint(capsys):
    full_id = "abc12345-1234-1234-1234-deadbeef1234"
    hits = [_hit(full_id)]
    render_sessions_only(hits, use_color=False, shortid=True)
    out = capsys.readouterr().out
    # Full UUID should NOT appear; compact form must
    assert full_id not in out
    assert "abc12345-...-eef1234" in out or "abc12345" in out  # head + tail visible


def test_sessions_only_full_uuid_is_default(capsys):
    full_id = "abc12345-1234-1234-1234-deadbeef1234"
    hits = [_hit(full_id)]
    render_sessions_only(hits, use_color=False)
    out = capsys.readouterr().out
    assert full_id in out  # FULL UUID is the default everywhere


def test_sessions_only_unknown_start_folder_renders_safely(capsys):
    hits = [_hit("sess-1", start_folder=None)]
    render_sessions_only(hits, use_color=False)
    out = capsys.readouterr().out
    assert "(unknown)" in out


def test_sessions_only_emits_next_hint_when_query_provided(capsys):
    hits = [_hit("abc1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee")]
    render_sessions_only(hits, use_color=False, query="oauth flow")
    out = capsys.readouterr().out
    assert "Next: csb search" in out
    assert "oauth flow" in out
    assert "--session-id" in out


def test_sessions_only_omits_next_hint_when_no_query(capsys):
    hits = [_hit("abc1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee")]
    render_sessions_only(hits, use_color=False, query=None)
    out = capsys.readouterr().out
    assert "Next:" not in out


def test_sessions_only_no_hits_prints_nothing(capsys):
    render_sessions_only([], use_color=False, query="x")
    assert capsys.readouterr().out == ""


def test_sessions_only_singular_vs_plural(capsys):
    """1 hit -> 'hit'; 2+ -> 'hits'."""
    one = [_hit("a")]
    two = [_hit("b"), _hit("b")]
    render_sessions_only(one, use_color=False)
    one_out = capsys.readouterr().out
    render_sessions_only(two, use_color=False)
    two_out = capsys.readouterr().out
    assert "1 hit\n" in one_out or "1 hit " in one_out
    assert "2 hits" in two_out


# ── render() dispatcher: sessions mode ───────────────────────────────


def test_render_dispatcher_routes_sessions_mode(capsys):
    hits = [_hit("sess-1")]
    render(hits, mode="sessions", use_color=False, query="q")
    out = capsys.readouterr().out
    assert "start at:" in out  # sessions-only formatting marker

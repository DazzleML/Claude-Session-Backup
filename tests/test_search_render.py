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
         matched="match here", start_folder="C:/code/x",
         started_at=None, jsonl_mtime=0.0,
         folders=None, message_count=0, claude_version=None):
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
        started_at=started_at,
        jsonl_mtime=jsonl_mtime,
        folders=folders or [],
        message_count=message_count,
        claude_version=claude_version,
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


# ── --full-info: extra 'started: <date> (purge in Nd)' line (v0.2.10) ──


def test_full_info_adds_started_line_in_default_mode(capsys):
    from claude_session_backup.search_render import render_human
    hits = [_hit("s1", started_at="2026-05-14T18:00:00Z")]
    render_human(hits, use_color=False, full_info=True, cleanup_days=90)
    out = capsys.readouterr().out
    # The label MUST be 'started:' (matches csb list vocabulary)
    assert "started:" in out


def test_full_info_omitted_when_flag_off(capsys):
    from claude_session_backup.search_render import render_human
    hits = [_hit("s1", started_at="2026-05-14T18:00:00Z")]
    render_human(hits, use_color=False, full_info=False, cleanup_days=90)
    out = capsys.readouterr().out
    assert "started:" not in out  # no second header line


def test_full_info_adds_purge_countdown_when_mtime_present(capsys):
    """jsonl_mtime + cleanup_days > 0 -> '(purge in Nd)' suffix."""
    from claude_session_backup.search_render import render_human
    import time
    recent_mtime = time.time() - 86400  # 1 day ago
    hits = [_hit("s1", started_at="2026-05-14T18:00:00Z", jsonl_mtime=recent_mtime)]
    render_human(hits, use_color=False, full_info=True, cleanup_days=90)
    out = capsys.readouterr().out
    assert "purge in" in out


def test_full_info_skips_purge_when_cleanup_days_zero(capsys):
    """cleanup_days=0 means user has no cleanup configured -- omit the countdown."""
    from claude_session_backup.search_render import render_human
    hits = [_hit("s1", started_at="2026-05-14T18:00:00Z", jsonl_mtime=1.7e9)]
    render_human(hits, use_color=False, full_info=True, cleanup_days=0)
    out = capsys.readouterr().out
    assert "purge in" not in out


def test_full_info_sessions_only_adds_started_line(capsys):
    """--full-info also enriches --sessions-only mode."""
    from claude_session_backup.search_render import render_sessions_only
    hits = [_hit("s1", started_at="2026-05-14T18:00:00Z")]
    render_sessions_only(hits, use_color=False, full_info=True, cleanup_days=90)
    out = capsys.readouterr().out
    assert "started:" in out
    assert "start at:" in out  # both lines present


def test_default_uses_human_readable_date_for_last_active(capsys):
    """The header always renders last-active as '<relative> (<human-date>)' --
    raw ISO is reserved for --json output and `csb show <uuid>`."""
    from claude_session_backup.search_render import render_human
    hits = [_hit("s1")]
    render_human(hits, use_color=False)  # NO --full-info
    out = capsys.readouterr().out
    # Raw ISO MUST NOT appear in the default header
    assert "last: 2026-05-16T10:00:00Z" not in out
    # Human-relative date MUST appear (some form, e.g. "today" / "N days ago")
    # We don't assert the exact word because relative_date() depends on
    # `now`; we assert the year fragment + parenthesized date format.
    assert "(2026-05-16" in out or "2026-05-16" in out


def test_default_handles_missing_last_active_gracefully(capsys):
    """If last_active_at is None, fall back to 'last: ?' (no crash)."""
    from claude_session_backup.search_render import render_human
    h = _hit("s1")
    h.last_active_at = None
    render_human([h], use_color=False)
    out = capsys.readouterr().out
    assert "last: ?" in out


# ── Visual separation between session blocks (v0.2.10) ──────────────


def test_render_human_blank_line_between_session_blocks(capsys):
    """Adjacent session blocks must be separated by a blank line."""
    from claude_session_backup.search_render import render_human
    hits = [
        _hit("aaa1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "A"),
        _hit("bbb2-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "B"),
    ]
    render_human(hits, use_color=False)
    out = capsys.readouterr().out
    # Between "A  aaa1..." and "B  bbb2..." headers there must be a blank line.
    a_idx = out.index("A  aaa1")
    b_idx = out.index("B  bbb2")
    between = out[a_idx:b_idx]
    assert "\n\n" in between, "expected blank line between session blocks"


def test_sessions_only_blank_line_between_blocks(capsys):
    from claude_session_backup.search_render import render_sessions_only
    hits = [
        _hit("aaa1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "A"),
        _hit("bbb2-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "B"),
    ]
    render_sessions_only(hits, use_color=False)
    out = capsys.readouterr().out
    a_idx = out.index("A  aaa1")
    b_idx = out.index("B  bbb2")
    between = out[a_idx:b_idx]
    assert "\n\n" in between


# ── Bold cyan session name (v0.2.10) ────────────────────────────────


def test_session_name_uses_bold_cyan_when_color_enabled(capsys):
    """Session header name carries the bold-cyan ANSI prefix (\\033[1;36m)."""
    from claude_session_backup.search_render import render_human
    hits = [_hit("aaa1-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "MYNAME")]
    render_human(hits, use_color=True)
    out = capsys.readouterr().out
    assert "\033[1;36m" in out
    assert "MYNAME" in out


# ── --full-info level 2 (-ff): folder list + meta line ──────────────


def _hit_with_folders():
    """Return a Hit with a realistic folders list + message_count + version."""
    return _hit(
        "s1",
        start_folder="C:/code/x",
        folders=[
            {"folder_path": "C:/code/x", "usage_count": 100, "is_start_folder": 1},
            {"folder_path": "C:/code/y", "usage_count": 50, "is_start_folder": 0},
            {"folder_path": "C:/code/z", "usage_count": 25, "is_start_folder": 0},
        ],
        message_count=438,
        claude_version="2.1.50",
    )


def test_full_info_level2_shows_start_folder_with_count(capsys):
    from claude_session_backup.search_render import render_human
    render_human([_hit_with_folders()], use_color=False, full_info=2)
    out = capsys.readouterr().out
    assert "start at: C:/code/x" in out
    assert "(100x)" in out


def test_full_info_level2_shows_top_other_folders(capsys):
    from claude_session_backup.search_render import render_human
    render_human([_hit_with_folders()], use_color=False, full_info=2)
    out = capsys.readouterr().out
    assert "C:/code/y" in out
    assert "(50x)" in out
    assert "C:/code/z" in out


def test_full_info_level2_shows_meta_line(capsys):
    from claude_session_backup.search_render import render_human
    render_human([_hit_with_folders()], use_color=False, full_info=2)
    out = capsys.readouterr().out
    assert "438 messages" in out
    assert "v2.1.50" in out


def test_full_info_level2_meta_uses_pipe_separator(capsys):
    from claude_session_backup.search_render import render_human
    render_human([_hit_with_folders()], use_color=False, full_info=2)
    out = capsys.readouterr().out
    assert "438 messages | v2.1.50" in out


def test_full_info_level2_omits_meta_when_no_fields(capsys):
    """When message_count=0 AND claude_version=None, no meta line."""
    from claude_session_backup.search_render import render_human
    h = _hit("s1", start_folder="C:/x",
             folders=[{"folder_path": "C:/x", "usage_count": 1, "is_start_folder": 1}])
    render_human([h], use_color=False, full_info=2)
    out = capsys.readouterr().out
    assert "messages" not in out
    assert " | v" not in out


def test_full_info_level2_sessions_only_skips_duplicate_start_at(capsys):
    """In --sessions-only mode, the level-2 helper must skip its own
    'start at:' line because the renderer already prints it with the
    resume hint."""
    from claude_session_backup.search_render import render_sessions_only
    render_sessions_only(
        [_hit_with_folders()], use_color=False, full_info=2,
    )
    out = capsys.readouterr().out
    # Exactly one 'start at:' line, not two
    assert out.count("start at:") == 1
    # But the other-folders + meta line still appear
    assert "C:/code/y" in out
    assert "438 messages | v2.1.50" in out


def test_full_info_level2_level1_lines_still_present(capsys):
    """Level 2 INCLUDES level 1's started/purge line (escalation, not replacement)."""
    from claude_session_backup.search_render import render_human
    h = _hit("s1", started_at="2026-05-14T18:00:00Z",
             folders=[{"folder_path": "C:/x", "usage_count": 1, "is_start_folder": 1}],
             message_count=10, claude_version="2.0")
    render_human([h], use_color=False, full_info=2, cleanup_days=90)
    out = capsys.readouterr().out
    assert "started:" in out  # level 1
    assert "C:/x" in out  # level 2 folder line
    assert "10 messages" in out  # level 2 meta


def test_full_info_level0_omits_all_extras(capsys):
    """Level 0 (default) = compact, no started:, no folders, no meta."""
    from claude_session_backup.search_render import render_human
    render_human([_hit_with_folders()], use_color=False, full_info=0)
    out = capsys.readouterr().out
    assert "started:" not in out
    assert "messages" not in out
    assert "C:/code/y" not in out  # other folder, not in default
    # Only the header line + the match excerpt
    assert "name" in out  # session name still there


def test_full_info_level1_stays_compact_no_separator(capsys):
    """Level 1 (single extra line) stays tight -- no blank line before
    the first hit. Level 1 is still grep-shaped output."""
    from claude_session_backup.search_render import render_human
    h = _hit("s1", started_at="2026-05-14T18:00:00Z")
    render_human([h], use_color=False, full_info=1, cleanup_days=90)
    out = capsys.readouterr().out
    started_idx = out.index("started:")
    hit_idx = out.index("L1 ")
    between = out[started_idx:hit_idx]
    assert "\n\n" not in between  # no separator at level 1


def test_full_info_level2_blank_line_before_first_hit(capsys):
    """Level 2 has 4+ extra lines -- the blank separator IS emitted so
    metadata block visually disconnects from the matches."""
    from claude_session_backup.search_render import render_human
    h = _hit_with_folders()
    render_human([h], use_color=False, full_info=2)
    out = capsys.readouterr().out
    # Find the meta-line-to-first-hit transition
    meta_idx = out.index("messages")
    hit_idx = out.index("L1 ")
    between = out[meta_idx:hit_idx]
    assert "\n\n" in between


def test_full_info_level0_no_blank_line_before_first_hit(capsys):
    """Level 0 keeps the tight one-line-header-then-hit layout."""
    from claude_session_backup.search_render import render_human
    h = _hit("s1")
    render_human([h], use_color=False, full_info=0)
    out = capsys.readouterr().out
    # Output should be: header line, then L1 line, then > line, then trailing \n
    # No blank line between header and L1.
    lines = out.split("\n")
    # First non-empty line is header; second is L1
    non_empty = [ln for ln in lines if ln.strip()]
    assert len(non_empty) >= 2
    assert "L1 " in non_empty[1]


# ── v0.3.5: query-match highlight helper ──────────────────────────────


def test_highlight_wraps_literal_match_in_bold_green():
    """Literal pattern occurrences get bold-green ANSI codes around them."""
    from claude_session_backup.search_render import _highlight
    out = _highlight(
        "find the oauth bug in oauth flow", "oauth",
        regex=False, case_sensitive=False, enabled=True,
    )
    assert out.count("\033[1;32m") == 2
    assert out.count("\033[0m") == 2
    assert "find the " in out and " bug in " in out and " flow" in out


def test_highlight_disabled_returns_text_unchanged():
    """``enabled=False`` is a no-op even with a valid pattern."""
    from claude_session_backup.search_render import _highlight
    plain = "find the oauth bug"
    assert _highlight(
        plain, "oauth", regex=False, case_sensitive=False, enabled=False,
    ) == plain


def test_highlight_empty_pattern_returns_text_unchanged():
    """Empty / None pattern is a no-op -- highlighting every char would
    be visual garbage when the user passed an empty query for
    'match all events in this session' semantics."""
    from claude_session_backup.search_render import _highlight
    plain = "find the oauth bug"
    assert _highlight(
        plain, "", regex=False, case_sensitive=False, enabled=True,
    ) == plain
    assert _highlight(
        plain, None, regex=False, case_sensitive=False, enabled=True,
    ) == plain


def test_highlight_case_sensitive_skips_wrong_case():
    """``case_sensitive=True`` makes uppercase pattern miss lowercase match."""
    from claude_session_backup.search_render import _highlight
    out = _highlight(
        "find the oauth bug", "OAUTH",
        regex=False, case_sensitive=True, enabled=True,
    )
    assert "\033[1;32m" not in out
    assert out == "find the oauth bug"


def test_highlight_regex_alternation_wraps_each_branch():
    """Regex mode supports alternation and other re features."""
    from claude_session_backup.search_render import _highlight
    out = _highlight(
        "alpha bravo charlie", "alpha|charlie",
        regex=True, case_sensitive=False, enabled=True,
    )
    assert out.count("\033[1;32m") == 2


def test_highlight_invalid_regex_falls_back_to_plain():
    """Malformed regex returns text plain instead of crashing the render."""
    from claude_session_backup.search_render import _highlight
    out = _highlight(
        "abc", "[",
        regex=True, case_sensitive=False, enabled=True,
    )
    assert out == "abc"


def test_highlight_literal_treats_metacharacters_as_literal():
    """Literal mode escapes regex metacharacters internally so
    ``oauth.flow`` only matches the literal sequence, not ``oauthXflow``."""
    from claude_session_backup.search_render import _highlight
    out = _highlight(
        "see oauth.flow and oauthXflow", "oauth.flow",
        regex=False, case_sensitive=False, enabled=True,
    )
    assert out.count("\033[1;32m") == 1


def test_render_human_highlights_query_in_matched_line(capsys):
    """End-to-end: render_human propagates query / regex / case_sensitive
    into in-line highlights inside the matched-text excerpt."""
    from claude_session_backup.search_render import render_human
    h = _hit("s1", matched="found the oauth marker")
    render_human(
        [h], use_color=True, query="oauth",
        regex=False, case_sensitive=False,
    )
    out = capsys.readouterr().out
    assert "\033[1;32moauth\033[0m" in out


def test_render_human_no_highlight_when_query_none(capsys):
    """No query -> no bold-green ANSI anywhere in the output. The leading
    ``>`` uses ``green`` (not ``bold_green``) so we check the bold variant."""
    from claude_session_backup.search_render import render_human
    h = _hit("s1", matched="found the oauth marker")
    render_human([h], use_color=True, query=None)
    out = capsys.readouterr().out
    assert "\033[1;32m" not in out

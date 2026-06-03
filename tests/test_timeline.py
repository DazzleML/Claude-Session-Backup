"""Tests for timeline formatting and purge countdown."""

import time

from claude_session_backup.timeline import (
    relative_date,
    format_timestamp,
    purge_countdown,
    _purge_style,
    format_session_line,
)


def test_relative_date_today():
    assert relative_date("2026-04-02T10:00:00Z") != "unknown"


def test_relative_date_unknown():
    assert relative_date(None) == "unknown"
    assert relative_date("") == "unknown"
    assert relative_date("not a date") == "unknown"


def test_format_timestamp():
    result = format_timestamp("2026-03-23T18:14:00Z")
    assert "2026-03-23" in result
    assert "18:14" in result


def test_format_timestamp_empty():
    assert format_timestamp(None) == ""
    assert format_timestamp("") == ""


def test_purge_countdown_positive():
    # File modified just now, 90-day cleanup -> ~90 days remaining
    days, text = purge_countdown(time.time(), 90)
    assert days >= 89
    assert "purge in" in text


def test_purge_countdown_overdue():
    # File modified 100 days ago, 90-day cleanup -> overdue
    old_mtime = time.time() - (100 * 86400)
    days, text = purge_countdown(old_mtime, 90)
    assert days < 0
    assert "OVERDUE" in text


def test_purge_countdown_purged_wording_when_deleted_at_set():
    """For already-deleted sessions, OVERDUE reads as 'should have been
    culled but wasn't' -- inaccurate when the cull already happened.
    Switch to PURGED Nd ago (past tense) when deleted_at is set."""
    old_mtime = time.time() - (100 * 86400)
    days, text = purge_countdown(old_mtime, 90, deleted_at="2026-05-31T00:00:00Z")
    assert days < 0
    assert "PURGED" in text
    assert "OVERDUE" not in text


def test_purge_countdown_overdue_when_deleted_at_none():
    """Regression: live sessions past the threshold still get OVERDUE."""
    old_mtime = time.time() - (100 * 86400)
    days, text = purge_countdown(old_mtime, 90, deleted_at=None)
    assert "OVERDUE" in text
    assert "PURGED" not in text


def test_purge_countdown_disabled():
    # cleanup_days <= 0 means disabled
    days, text = purge_countdown(time.time(), 0)
    assert days is None
    assert text == ""


def test_purge_countdown_no_mtime():
    days, text = purge_countdown(0, 90)
    assert days is None
    assert text == ""


def test_purge_style_green():
    assert "green" in _purge_style(60)


def test_purge_style_yellow():
    assert "yellow" in _purge_style(20)


def test_purge_style_orange():
    style = _purge_style(10)
    assert "255" in style or "orange" in style.lower()


def test_purge_style_red():
    assert "red" in _purge_style(3)


def test_purge_style_overdue():
    style = _purge_style(-5)
    assert "bold" in style
    assert "red" in style


def test_purge_style_none():
    assert _purge_style(None) == "dim"


def test_format_session_line_includes_val_when_metadata_validated():
    """val: YY-MM-DD appears in the footer when metadata_validated_at set."""
    session = {
        "session_name": "validated-session",
        "session_id": "u",
        "last_user_at": "2026-03-23T10:00:00Z",
        "started_at": "2026-03-23T09:00:00Z",
        "start_folder": "/x",
        "message_count": 5,
        "folders": [{"folder_path": "/x", "usage_count": 1, "is_start_folder": True}],
        "jsonl_mtime": time.time(),
        "metadata_validated_at": "2026-06-02T15:30:00Z",
    }
    line = format_session_line(session, 1, cleanup_days=90)
    # Leading zeros dropped on month/day to save horizontal space.
    assert "val: 26-6-2" in line
    assert "val: 26-06-02" not in line


def test_format_validated_date_keeps_double_digit_components():
    """Two-digit months/days don't get a leading-zero strip applied."""
    from claude_session_backup.timeline import _format_validated_date
    assert _format_validated_date("2026-12-15T00:00:00Z") == "26-12-15"
    assert _format_validated_date("2026-10-01T00:00:00Z") == "26-10-1"
    assert _format_validated_date("2026-01-25T00:00:00Z") == "26-1-25"
    assert _format_validated_date("2026-06-02T00:00:00Z") == "26-6-2"


def test_format_session_line_omits_val_when_metadata_validated_missing():
    """Backwards compat: rows without metadata_validated_at don't show val:."""
    session = {
        "session_name": "old-session",
        "session_id": "u",
        "last_user_at": "2026-03-23T10:00:00Z",
        "started_at": "2026-03-23T09:00:00Z",
        "start_folder": "/x",
        "message_count": 5,
        "folders": [{"folder_path": "/x", "usage_count": 1, "is_start_folder": True}],
        "jsonl_mtime": time.time(),
        # metadata_validated_at absent
    }
    line = format_session_line(session, 1, cleanup_days=90)
    assert "val:" not in line


def test_format_session_line_two_line_restore_layout_when_too_wide(monkeypatch):
    """Wide deleted-session footers split: restore command on its own line
    so the UUID stays double-click-copyable (no whitespace breaks selection)."""
    # Force a narrow console so the combined line definitely doesn't fit
    from claude_session_backup import timeline as tl
    monkeypatch.setattr(tl, "_console_width", lambda default=100: 80)

    session = {
        "session_name": "purged-session",
        "session_id": "abc12345-6789-0000-0000-000000000000",
        "last_user_at": "2026-03-01T10:00:00Z",
        "started_at": "2026-03-01T09:00:00Z",
        "start_folder": "/x",
        "message_count": 5,
        "claude_version": "2.1.99",
        "metadata_validated_at": "2026-06-02T00:00:00Z",
        "folders": [{"folder_path": "/x", "usage_count": 1, "is_start_folder": True}],
        "jsonl_mtime": time.time() - (100 * 86400),  # past cleanup
        "deleted_at": "2026-05-31T19:00:00Z",
    }
    line = format_session_line(session, 1, cleanup_days=90)
    assert "restore: claude --resume abc12345-6789-0000-0000-000000000000" in line
    footer_lines = [l for l in line.split("\n") if "id:" in l]
    assert len(footer_lines) == 1
    assert "restore:" not in footer_lines[0], (
        f"restore should be on its own line in narrow terminals; "
        f"footer: {footer_lines[0]!r}"
    )


def test_format_session_line_one_line_when_combined_fits(monkeypatch):
    """When the deleted-session footer + restore command fits the console
    width, keep them on one line (compact). Don't gratuitously split."""
    # Wide console so the short combined line fits easily.
    from claude_session_backup import timeline as tl
    monkeypatch.setattr(tl, "_console_width", lambda default=100: 200)

    # A SPARSE row (no msg_count, no version, no val:) -- the meta line
    # alone is short, the combined with restore should fit in 200 chars.
    session = {
        "session_name": "github-traffic-tracker",
        "session_id": "2e738436-fad5-4ae4-a929-17d9cc12e1de",
        # no last_user_at / started_at -> minimal display
        "start_folder": "C:\\code\\github-traffic-tracker",
        "folders": [{"folder_path": "C:\\code\\github-traffic-tracker",
                     "usage_count": 1, "is_start_folder": True}],
        "jsonl_mtime": time.time() - (100 * 86400),
        "deleted_at": "2026-05-31T19:00:00Z",
    }
    line = format_session_line(session, 18, cleanup_days=90)
    # Compact case: restore command IS pipe-joined with id on the same line.
    footer_lines = [l for l in line.split("\n") if "id:" in l]
    assert len(footer_lines) == 1
    assert "restore: claude --resume" in footer_lines[0], (
        f"sparse-row footer should keep restore inline when it fits; "
        f"footer: {footer_lines[0]!r}"
    )


def test_render_session_rich_passes_no_wrap_to_every_meta_print():
    """BUG regression: render_session_rich must pass no_wrap=True on every
    console.print() of the meta Text, so Rich's internal wrap-at-width
    can't undermine the explicit one-line-vs-two-line layout decision.

    Without no_wrap=True, in piped/subprocess/CI contexts where Rich
    detects width=79, the meta line wraps and emits a continuation line
    that looks like display corruption (e.g., 'val: 26-6-3' on its own
    unindented row).

    This test asserts the API contract directly by capturing every
    console.print() call and checking no_wrap=True was passed. Rendered-
    output assertions are brittle across Rich's force_terminal /
    file-type behavior modes; the contract is the load-bearing thing.

    See: tests/one-offs/probe_layout_wrap.py for the original reproduction.
    """
    from claude_session_backup.timeline import render_session_rich
    from rich.console import Console

    # Capture every console.print() call's kwargs
    captured_prints = []
    real_console = Console(file=__import__("io").StringIO(), width=79)
    real_print = real_console.print

    def capturing_print(*args, **kwargs):
        captured_prints.append({"args": args, "kwargs": kwargs})
        return real_print(*args, **kwargs)

    real_console.print = capturing_print

    session = {
        "session_name": "wide-row",
        "session_id": "4d7565f3-cef5-41b3-9acf-6a3bde0d418d",
        "last_user_at": "2026-03-01T10:00:00Z",
        "started_at": "2026-03-01T09:00:00Z",
        "start_folder": "/x",
        "message_count": 8788,
        "claude_version": "2.1.61",
        "metadata_validated_at": "2026-06-02T00:00:00Z",
        "folders": [{"folder_path": "/x", "usage_count": 1, "is_start_folder": True}],
        "jsonl_mtime": time.time() - (100 * 86400),
        "deleted_at": "2026-05-31T19:00:00Z",
    }
    render_session_rich(real_console, session, 1, cleanup_days=90)

    # Find the meta-line print: it has 'id:' in the Text's plain text.
    # Restore-line print: it has 'restore:' in the Text's plain text.
    # Both MUST have no_wrap=True.
    meta_prints = [
        c for c in captured_prints
        if c["args"] and hasattr(c["args"][0], "plain")
           and "id:" in c["args"][0].plain
    ]
    restore_prints = [
        c for c in captured_prints
        if c["args"] and hasattr(c["args"][0], "plain")
           and "restore:" in c["args"][0].plain
           and "id:" not in c["args"][0].plain  # exclude the inline one-line case
    ]

    assert len(meta_prints) >= 1, "should have printed the meta line"
    for p in meta_prints:
        assert p["kwargs"].get("no_wrap") is True, (
            f"meta print missing no_wrap=True: kwargs={p['kwargs']}"
        )
    for p in restore_prints:
        assert p["kwargs"].get("no_wrap") is True, (
            f"restore print missing no_wrap=True: kwargs={p['kwargs']}"
        )


def test_format_session_line_purged_wording_for_deleted_past_cleanup():
    """OVERDUE -> PURGED Nd ago wording when the session is already deleted."""
    session = {
        "session_name": "purged",
        "session_id": "u",
        "last_user_at": "2026-03-01T10:00:00Z",
        "started_at": "2026-03-01T09:00:00Z",
        "start_folder": "/x",
        "message_count": 5,
        "folders": [{"folder_path": "/x", "usage_count": 1, "is_start_folder": True}],
        "jsonl_mtime": time.time() - (100 * 86400),
        "deleted_at": "2026-05-31T19:00:00Z",
    }
    line = format_session_line(session, 1, cleanup_days=90)
    assert "PURGED" in line
    assert "OVERDUE" not in line


def test_format_session_line_basic():
    session = {
        "session_name": "test-session",
        "session_id": "aaaa-bbbb-cccc-dddd",
        "last_user_at": "2026-03-23T10:00:00Z",
        "started_at": "2026-03-23T09:00:00Z",
        "start_folder": "/home/user/code",
        "message_count": 42,
        "claude_version": "2.1.81",
        "folders": [
            {"folder_path": "/home/user/code", "usage_count": 20, "is_start_folder": True},
            {"folder_path": "/home/user/other", "usage_count": 15, "is_start_folder": False},
        ],
        "jsonl_mtime": time.time(),
    }
    line = format_session_line(session, 1, cleanup_days=90)
    assert "test-session" in line
    assert "aaaa-bbbb-cccc-dddd" in line
    assert "start at:" in line
    assert "/home/user/code" in line
    assert "(20x)" in line
    assert "42 messages" in line
    assert "purge in" in line


def _session_with_n_others(n: int) -> dict:
    """Build a fake session with `n` non-start folders for renderer tests."""
    folders = [
        {"folder_path": "/start", "usage_count": 100, "is_start_folder": True},
    ]
    for i in range(n):
        folders.append({
            "folder_path": f"/other-{i}",
            "usage_count": 10 - i,  # descending; storage ordering preserved
            "is_start_folder": False,
        })
    return {
        "session_name": "fixture",
        "session_id": "ffff-ffff-ffff-ffff",
        "last_user_at": "2026-03-23T10:00:00Z",
        "started_at": "2026-03-23T09:00:00Z",
        "start_folder": "/start",
        "message_count": 5,
        "folders": folders,
        "jsonl_mtime": time.time(),
    }


def test_format_session_line_default_top_3_caps_others_at_3():
    session = _session_with_n_others(7)
    line = format_session_line(session, 1, cleanup_days=0)
    # Default top_folders=3: only first 3 of 7 others appear
    for i in range(3):
        assert f"/other-{i}" in line
    for i in range(3, 7):
        assert f"/other-{i}" not in line
    # Meta line surfaces the remaining 4
    assert "4 other folders seen" in line


def test_format_session_line_top_5():
    session = _session_with_n_others(7)
    line = format_session_line(session, 1, cleanup_days=0, top_folders=5)
    for i in range(5):
        assert f"/other-{i}" in line
    for i in range(5, 7):
        assert f"/other-{i}" not in line
    assert "2 other folders seen" in line


def test_format_session_line_all_folders():
    session = _session_with_n_others(7)
    line = format_session_line(session, 1, cleanup_days=0, top_folders=None)
    # All 7 others must appear
    for i in range(7):
        assert f"/other-{i}" in line
    # And the "X other folders seen" tail-counter shouldn't appear
    assert "other folders seen" not in line


def test_format_session_line_top_zero():
    """`--top 0` is a degenerate-but-legal request: hide all others."""
    session = _session_with_n_others(7)
    line = format_session_line(session, 1, cleanup_days=0, top_folders=0)
    for i in range(7):
        assert f"/other-{i}" not in line
    assert "7 other folders seen" in line

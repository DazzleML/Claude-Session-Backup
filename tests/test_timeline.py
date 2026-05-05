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

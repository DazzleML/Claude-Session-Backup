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

"""Tests for claude_session_backup.schedule -- the #69 pure renderer core (P3).

Contract anchors: AC-3 (frozen absolute context, bare-`csb` tripwire),
AC-4 (marker ownership; foreign crontab lines byte-identical), AC-5
(Windows XML overrides + golden file + spaces-in-path fixture), AC-6
(StartCalendarInterval shapes), AC-11 (no 2>/dev/null; redirection into
the schedule log), Delta-3 (uniform interval grammar), Delta-11
(install-time-derived fire time; a hardcoded-midnight entry must FAIL).

Golden files live in tests/fixtures/schedule/. Structural round-trips
(ElementTree / plistlib) run alongside the byte goldens per Delta-2.
"""

from __future__ import annotations

import plistlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest

from claude_session_backup import schedule
from claude_session_backup.schedule import (
    CRON_BLOCK_BEGIN,
    CRON_BLOCK_END,
    LAUNCHD_LABEL,
    ScheduleError,
    ScheduleSpec,
    build_command_argv,
    cron_expression,
    derive_fire_time,
    fire_hours,
    merge_crontab,
    remove_from_crontab,
    render_crontab_block,
    render_plist,
    render_task_xml,
    validate_entry_argv,
    validate_interval,
)

FIXTURES = Path(__file__).parent / "fixtures" / "schedule"

# Deterministic specs -- the 14:37 install moment is the Delta-11 anchor
# used throughout (a real derived fire time, deliberately NOT midnight).
# The Windows interpreter path contains a space on purpose (Delta-2).

WIN_24H = ScheduleSpec(
    interval_minutes=1440, fire_hour=14, fire_minute=37,
    start_boundary="2026-08-06T14:37:00",
    python_exe=r"C:\Program Files\Python312\pythonw.exe",
    claude_dir=r"C:\Users\Extreme\.claude",
    log_file=r"C:\Users\Extreme\.claude\csb-logs\schedule.log",
)
WIN_15M = ScheduleSpec(
    interval_minutes=15, fire_hour=0, fire_minute=0,
    start_boundary="2026-08-06T14:37:00",
    python_exe=r"C:\Python312\pythonw.exe",
    claude_dir=r"C:\Users\Extreme\.claude",
    log_file=r"C:\Users\Extreme\.claude\csb-logs\schedule.log",
)
POSIX_24H = ScheduleSpec(
    interval_minutes=1440, fire_hour=14, fire_minute=37,
    start_boundary="2026-08-06T14:37:00",
    python_exe="/usr/bin/python3",
    claude_dir="/home/extreme/.claude",
    log_file="/home/extreme/.claude/csb-logs/schedule.log",
)
POSIX_12H = ScheduleSpec(
    interval_minutes=720, fire_hour=14, fire_minute=37,
    start_boundary="2026-08-06T14:37:00",
    python_exe="/usr/bin/python3",
    claude_dir="/home/extreme/.claude",
    log_file="/home/extreme/.claude/csb-logs/schedule.log",
)
POSIX_15M = ScheduleSpec(
    interval_minutes=15, fire_hour=0, fire_minute=0,
    start_boundary="2026-08-06T14:37:00",
    python_exe="/usr/bin/python3",
    claude_dir="/home/extreme/.claude",
    log_file="/home/extreme/.claude/csb-logs/schedule.log",
)


# ── Interval grammar (Delta-3: identical on every platform) ─────────────


@pytest.mark.parametrize("ok", [1, 5, 15, 20, 30, 60, 120, 480, 720, 1440])
def test_interval_grammar_accepts_uniform_set(ok):
    validate_interval(ok)


@pytest.mark.parametrize("bad", [0, -15, 7, 45, 90, 100, 300, 1000, 2880])
def test_interval_grammar_rejects_everything_else(bad):
    with pytest.raises(ScheduleError):
        validate_interval(bad)


def test_spec_construction_validates_interval_and_fire_time():
    with pytest.raises(ScheduleError):
        ScheduleSpec(
            interval_minutes=45, fire_hour=0, fire_minute=0,
            start_boundary="2026-08-06T14:37:00",
            python_exe="/usr/bin/python3", claude_dir="/h/.claude",
            log_file="/h/.claude/csb-logs/schedule.log",
        )
    with pytest.raises(ScheduleError):
        ScheduleSpec(
            interval_minutes=60, fire_hour=24, fire_minute=0,
            start_boundary="2026-08-06T14:37:00",
            python_exe="/usr/bin/python3", claude_dir="/h/.claude",
            log_file="/h/.claude/csb-logs/schedule.log",
        )


# ── Fire-time derivation (Delta-11: the load-bearing rule) ──────────────


def test_fire_time_derives_from_install_moment_not_midnight():
    # The Delta-11 red-green: installed at 14:37, a daily entry fires at
    # 14:37. If this ever returns (0, 0) for a daytime install, the
    # never-runs-on-9-to-5-desktops hazard is back.
    assert derive_fire_time(datetime(2026, 8, 6, 14, 37), 1440) == (14, 37)
    assert derive_fire_time(datetime(2026, 8, 6, 14, 37), 720) == (14, 37)
    assert derive_fire_time(datetime(2026, 8, 6, 14, 37), 1440) != (0, 0)


def test_fire_time_midnight_only_when_installed_at_midnight():
    assert derive_fire_time(datetime(2026, 8, 6, 0, 3), 1440) == (0, 3)


def test_fire_time_unused_for_subhour_intervals():
    assert derive_fire_time(datetime(2026, 8, 6, 14, 37), 15) == (0, 0)


def test_fire_hours_tiles_the_day_anchored_at_install_hour():
    assert fire_hours(POSIX_24H) == [14]
    assert fire_hours(POSIX_12H) == [2, 14]
    hourly = ScheduleSpec(
        interval_minutes=60, fire_hour=14, fire_minute=37,
        start_boundary="2026-08-06T14:37:00",
        python_exe="/usr/bin/python3", claude_dir="/h/.claude",
        log_file="/h/.claude/csb-logs/schedule.log",
    )
    assert fire_hours(hourly) == list(range(24))


# ── Command argv + the bare-name tripwire (AC-3) ────────────────────────


def test_command_argv_freezes_full_context():
    argv = build_command_argv(WIN_24H)
    assert argv[0] == r"C:\Program Files\Python312\pythonw.exe"
    assert argv[1:3] == ["-m", "claude_session_backup"]
    assert "backup" in argv and "--quiet" in argv
    assert argv[argv.index("--claude-dir") + 1] == r"C:\Users\Extreme\.claude"
    assert argv[argv.index("--log-file") + 1].endswith("schedule.log")
    assert "--db" not in argv  # default index path -> not baked


def test_command_argv_bakes_db_when_non_default():
    spec = ScheduleSpec(
        interval_minutes=1440, fire_hour=14, fire_minute=37,
        start_boundary="2026-08-06T14:37:00",
        python_exe="/usr/bin/python3", claude_dir="/h/.claude",
        log_file="/h/.claude/csb-logs/schedule.log",
        db_path="/elsewhere/custom.db",
    )
    argv = build_command_argv(spec)
    assert argv[argv.index("--db") + 1] == "/elsewhere/custom.db"


def test_validator_rejects_bare_csb():
    # The AC-3 red side: the exact entry the April 2026 sketch and the old
    # docs snippet would have generated must be refused.
    with pytest.raises(ScheduleError, match="bare"):
        validate_entry_argv(["csb", "backup", "--quiet",
                             "--claude-dir", "/h/.claude",
                             "--log-file", "/h/l.log"])


def test_validator_rejects_relative_interpreter():
    with pytest.raises(ScheduleError, match="absolute"):
        validate_entry_argv(["python", "-m", "claude_session_backup",
                             "backup", "--claude-dir", "/h/.claude",
                             "--log-file", "/h/l.log"])


def test_validator_rejects_missing_frozen_context():
    with pytest.raises(ScheduleError, match="--claude-dir"):
        validate_entry_argv(["/usr/bin/python3", "-m", "claude_session_backup",
                             "backup", "--log-file", "/h/l.log"])
    with pytest.raises(ScheduleError, match="--log-file"):
        validate_entry_argv(["/usr/bin/python3", "-m", "claude_session_backup",
                             "backup", "--claude-dir", "/h/.claude"])


def test_validator_accepts_full_path_interpreter_with_spaces():
    validate_entry_argv(build_command_argv(WIN_24H))


# ── Cron expression (anchored hour lists, never */N re-anchoring) ───────


def test_cron_expression_shapes():
    assert cron_expression(POSIX_15M) == "*/15 * * * *"
    assert cron_expression(POSIX_12H) == "37 2,14 * * *"
    assert cron_expression(POSIX_24H) == "37 14 * * *"


def test_cron_expression_never_silently_midnight():
    # Delta-11 in cron form: the daily expression for a 14:37 install
    # must contain hour 14, not hour 0.
    expr = cron_expression(POSIX_24H)
    assert " 14 " in expr and " 0 " not in f" {expr} "


# ── Crontab block + merge ownership (AC-4, AC-11, D5) ───────────────────


def test_crontab_block_golden():
    assert render_crontab_block(POSIX_24H) == (
        FIXTURES / "crontab_24h.txt").read_text(encoding="utf-8")


def test_crontab_block_is_marker_owned_and_logged():
    block = render_crontab_block(POSIX_24H)
    lines = block.splitlines()
    assert lines[0] == CRON_BLOCK_BEGIN
    assert lines[-1] == CRON_BLOCK_END
    assert ">> " in block and "2>&1" in block
    assert "2>/dev/null" not in block  # AC-11: the ban


def test_merge_preserves_foreign_lines_byte_identical():
    # THE AC-4 red-green (D5): a user-authored line CONTAINING the words
    # "csb backup" is not ours and must survive install + remove exactly.
    existing = (
        "PATH=/usr/local/bin\n"
        "*/5 * * * * /home/u/bin/wrapper-for-csb-backup.sh\n"
        "# reminder: csb backup runs from my own wrapper above\n"
    )
    merged = merge_crontab(existing, render_crontab_block(POSIX_24H))
    assert merged.startswith(existing)  # byte-identical prefix
    removed = remove_from_crontab(merged)
    assert removed == existing


def test_merge_is_idempotent():
    block = render_crontab_block(POSIX_24H)
    once = merge_crontab("MAILTO=x\n", block)
    twice = merge_crontab(once, block)
    assert once == twice
    assert twice.count(CRON_BLOCK_BEGIN) == 1


def test_merge_replaces_stale_own_block():
    old = merge_crontab("", render_crontab_block(POSIX_12H))
    new = merge_crontab(old, render_crontab_block(POSIX_24H))
    assert "37 14 * * *" in new
    assert "2,14" not in new
    assert new.count(CRON_BLOCK_BEGIN) == 1


def test_remove_from_empty_is_noop():
    assert remove_from_crontab("") == ""


# ── Windows task XML (AC-5, Delta-2) ────────────────────────────────────

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _parse_xml(spec):
    text = render_task_xml(spec)
    # ElementTree rejects the UTF-16 declaration on a str -- parse bytes.
    return text, ET.fromstring(text.encode("utf-16"))


def test_task_xml_golden_24h():
    assert render_task_xml(WIN_24H) == (
        FIXTURES / "task_24h.xml").read_text(encoding="utf-8")


def test_task_xml_structurally_valid_and_sets_ac5_overrides():
    _, root = _parse_xml(WIN_24H)
    settings = root.find("t:Settings", NS)
    get = lambda tag: settings.find(f"t:{tag}", NS).text
    assert get("DisallowStartIfOnBatteries") == "false"
    assert get("StopIfGoingOnBatteries") == "false"
    assert get("StartWhenAvailable") == "true"
    assert get("MultipleInstancesPolicy") == "IgnoreNew"
    assert get("ExecutionTimeLimit") == "PT1H"
    principal = root.find("t:Principals/t:Principal", NS)
    assert principal.find("t:LogonType", NS).text == "InteractiveToken"
    assert principal.find("t:RunLevel", NS).text == "LeastPrivilege"


def test_task_xml_spaces_in_interpreter_path():
    # Delta-2: <Command> carries the spaced path unquoted (the XML split
    # dodges cmd quoting); <Arguments> quotes its own spaced tokens.
    _, root = _parse_xml(WIN_24H)
    exec_el = root.find("t:Actions/t:Exec", NS)
    assert exec_el.find("t:Command", NS).text == (
        r"C:\Program Files\Python312\pythonw.exe")
    args = exec_el.find("t:Arguments", NS).text
    assert '"' not in args.split("--claude-dir")[0]  # unspaced tokens unquoted
    assert r"C:\Users\Extreme\.claude" in args


def test_task_xml_repetition_only_below_daily():
    text_24h, root_24h = _parse_xml(WIN_24H)
    assert root_24h.find("t:Triggers/t:CalendarTrigger/t:Repetition", NS) is None
    text_15m, root_15m = _parse_xml(WIN_15M)
    rep = root_15m.find("t:Triggers/t:CalendarTrigger/t:Repetition", NS)
    assert rep.find("t:Interval", NS).text == "PT15M"
    assert rep.find("t:Duration", NS).text == "P1D"


def test_task_xml_start_boundary_carries_install_moment():
    _, root = _parse_xml(WIN_24H)
    sb = root.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", NS)
    assert sb.text == "2026-08-06T14:37:00"


def test_task_xml_never_contains_bare_csb():
    text = render_task_xml(WIN_24H)
    assert not re.search(r"(?<![\w.\\/-])csb(?:\.exe)?(?![\w.-])",
                         text.replace("csb-scheduled-backup", "").replace(
                             "csb setup schedule", "").replace(
                             "csb scheduled backup", ""))


# ── launchd plist (AC-6, D1) ────────────────────────────────────────────


def test_plist_golden_24h():
    spec = ScheduleSpec(
        interval_minutes=1440, fire_hour=14, fire_minute=37,
        start_boundary="2026-08-06T14:37:00",
        python_exe="/usr/local/bin/python3",
        claude_dir="/Users/extreme/.claude",
        log_file="/Users/extreme/.claude/csb-logs/schedule.log",
    )
    assert render_plist(spec) == (
        FIXTURES / "agent_24h.plist").read_text(encoding="utf-8")


def test_plist_round_trips_and_uses_calendar_intervals():
    # Delta-2 structural check -- the only machine validation the plist
    # gets before real macOS hardware. D1: StartCalendarInterval, never
    # StartInterval (missed StartInterval firings are SKIPPED on sleep).
    data = plistlib.loads(render_plist(POSIX_24H).encode("utf-8"))
    assert data["Label"] == LAUNCHD_LABEL
    assert "StartInterval" not in data
    assert data["StartCalendarInterval"] == [{"Hour": 14, "Minute": 37}]
    assert data["ProgramArguments"] == build_command_argv(POSIX_24H)
    assert data["StandardOutPath"].endswith("schedule.log")
    assert data["StandardErrorPath"].endswith("schedule.log")


def test_plist_calendar_shapes_per_interval():
    twelve = plistlib.loads(render_plist(POSIX_12H).encode("utf-8"))
    assert twelve["StartCalendarInterval"] == [
        {"Hour": 2, "Minute": 37}, {"Hour": 14, "Minute": 37}]
    fifteen = plistlib.loads(render_plist(POSIX_15M).encode("utf-8"))
    assert fifteen["StartCalendarInterval"] == [
        {"Minute": 0}, {"Minute": 15}, {"Minute": 30}, {"Minute": 45}]

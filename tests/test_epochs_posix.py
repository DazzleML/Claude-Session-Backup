"""POSIX fence reading (#68 P6b / R2): journalctl -> wtmp, evidence-first.

Fixtures derive from a REAL capture (WSL2 Ubuntu 22.04, systemd 249 --
`wsl-ubuntu2204-systemd249_2026-08-07` in the private capture store) plus
spec-frozen shapes for rungs no local box can produce (systemd 254+ JSON,
BSD no-year `last`), which the R2 checklist flags for live validation.

The load-bearing red-greens:
  * F1 sniff -- systemd < 254 emits TEXT for `-o json` with rc 0; the
    JSON rung must fall through to the text parser on the SAME output.
  * The wtmp-rotation coverage guard -- silence in an UNCOVERED window
    is `unknown`, never a crash label.
  * The BSD no-year Dec->Jan rollover -- ordering guard, not just the
    future guard.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import claude_session_backup.epochs as epochs
from claude_session_backup.epochs import (
    EVENT_BOOT,
    EVENT_SHUTDOWN_CLEAN,
    EVENT_SHUTDOWN_UNEXPECTED,
    EVENT_SHUTDOWN_UNKNOWN,
    Fence,
    FenceUnavailableError,
    latest_epoch,
)

J_ARGV = ("journalctl", "--utc", "--list-boots", "-o", "json")
LF_ARGV = ("last", "-xF", "reboot", "shutdown")
LN_ARGV = ("last", "reboot", "shutdown")


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _usec(dt):
    return int(dt.timestamp() * 1_000_000)


# Verbatim shape from the WSL capture (--utc form, em-dash joined).
WSL_TEXT = """\
-2 28776786ece040c1b572f4e50224ca6e Thu 2026-08-06 19:24:56 UTC—Thu 2026-08-06 19:25:32 UTC
-1 8fad8e179da44c98836655cc80bc405b Fri 2026-08-07 03:03:15 UTC—Fri 2026-08-07 03:04:07 UTC
 0 a35a6865c6a840568e25b4f14045293f Fri 2026-08-07 10:32:10 UTC—Fri 2026-08-07 10:32:11 UTC
"""

JSON_BOOTS = json.dumps([
    {"index": -1, "boot_id": "8fad8e179da44c98836655cc80bc405b",
     "first_entry": _usec(_utc(2026, 8, 7, 3, 3, 15)),
     "last_entry": _usec(_utc(2026, 8, 7, 3, 4, 7))},
    {"index": 0, "boot_id": "a35a6865c6a840568e25b4f14045293f",
     "first_entry": str(_usec(_utc(2026, 8, 7, 10, 32, 10))),  # str variant
     "last_entry": _usec(_utc(2026, 8, 7, 10, 35, 0))},
])

# VPS-like wtmp: one clean shutdown record + coverage back to Aug 6 noon.
WTMP_FULL = """\
reboot   system boot  5.15.0-generic   Fri Aug  7 03:03:20 2026   still running
shutdown system down  5.15.0-generic   Fri Aug  7 03:04:05 2026 - Fri Aug  7 03:04:40 2026  (00:00)
reboot   system boot  5.15.0-generic   Thu Aug  6 19:25:00 2026 - Fri Aug  7 03:04:05 2026  (07:39)

wtmp begins Thu Aug  6 12:00:00 2026
"""


@pytest.fixture
def utc_tz_seam(monkeypatch):
    """Treat `last`'s naive local times as UTC for determinism."""
    monkeypatch.setattr(epochs, "_local_naive_to_utc",
                        lambda dt: dt.replace(tzinfo=timezone.utc))


def _canned(monkeypatch, responses):
    """Cann _run_command per argv tuple; missing -> FileNotFoundError."""
    def fake(argv, timeout=15.0):
        action = responses.get(tuple(argv))
        if action is None:
            raise FileNotFoundError(argv[0])
        if isinstance(action, Exception):
            raise action
        return action
    monkeypatch.setattr(epochs, "_run_command", fake)


# ── the journalctl rung ───────────────────────────────────────────────


class TestJournalJson:
    def test_spec_shape_parses_including_str_usec(self, monkeypatch,
                                                  utc_tz_seam):
        _canned(monkeypatch, {J_ARGV: (0, JSON_BOOTS),
                              LF_ARGV: (0, "")})
        fences = epochs._read_fences_posix()
        boots = [f for f in fences if f.event_id == EVENT_BOOT]
        shuts = [f for f in fences if f.event_id in
                 epochs.SHUTDOWN_EVENT_IDS]
        assert [b.at_utc for b in boots] == [
            _utc(2026, 8, 7, 10, 32, 10), _utc(2026, 8, 7, 3, 3, 15)]
        # Completed boot -1 ends at its last entry; no wtmp -> unknown.
        assert [(s.at_utc, s.event_id) for s in shuts] == [
            (_utc(2026, 8, 7, 3, 4, 7), EVENT_SHUTDOWN_UNKNOWN)]

    def test_f1_sniff_text_fallthrough(self, monkeypatch, utc_tz_seam):
        """RED-GREEN anchor: systemd 249 answers `-o json` with TEXT and
        rc 0. The rung must decode the SAME output as text, not trust
        the flag."""
        _canned(monkeypatch, {J_ARGV: (0, WSL_TEXT), LF_ARGV: (0, "")})
        fences = epochs._read_fences_posix()
        boots = [f.at_utc for f in fences if f.event_id == EVENT_BOOT]
        assert boots == [_utc(2026, 8, 7, 10, 32, 10),
                         _utc(2026, 8, 7, 3, 3, 15),
                         _utc(2026, 8, 6, 19, 24, 56)]
        shuts = [(f.at_utc, f.event_id) for f in fences
                 if f.event_id in epochs.SHUTDOWN_EVENT_IDS]
        assert shuts == [
            (_utc(2026, 8, 7, 3, 4, 7), EVENT_SHUTDOWN_UNKNOWN),
            (_utc(2026, 8, 6, 19, 25, 32), EVENT_SHUTDOWN_UNKNOWN)]


class TestJournalText:
    def test_wsl_capture_lines_parse(self):
        boots = epochs._parse_journalctl_text(WSL_TEXT)
        assert boots == [
            (-2, _utc(2026, 8, 6, 19, 24, 56), _utc(2026, 8, 6, 19, 25, 32)),
            (-1, _utc(2026, 8, 7, 3, 3, 15), _utc(2026, 8, 7, 3, 4, 7)),
            (0, _utc(2026, 8, 7, 10, 32, 10), _utc(2026, 8, 7, 10, 32, 11)),
        ]

    def test_header_line_is_skipped(self):
        headered = "IDX BOOT ID                          FIRST ENTRY" \
                   "                 LAST ENTRY\n" + WSL_TEXT
        assert len(epochs._parse_journalctl_text(headered)) == 3

    def test_local_mode_output_yields_nothing(self):
        """Only --utc output parses -- TZ-abbrev lines fail the UTC
        anchor rather than mis-parse."""
        local = WSL_TEXT.replace(" UTC", " EDT")
        assert epochs._parse_journalctl_text(local) == []


# ── the cause ladder (E4) ─────────────────────────────────────────────


class TestCorroboration:
    INSTANT = _utc(2026, 8, 7, 3, 4, 7)

    def test_matching_wtmp_record_is_clean(self):
        assert epochs._corroborate_shutdown(
            self.INSTANT, [self.INSTANT + timedelta(seconds=30)],
            self.INSTANT - timedelta(days=5)) == EVENT_SHUTDOWN_CLEAN

    def test_covered_silence_is_crash_by_absence(self):
        assert epochs._corroborate_shutdown(
            self.INSTANT, [], self.INSTANT - timedelta(days=5)
        ) == EVENT_SHUTDOWN_UNEXPECTED

    def test_rotated_wtmp_is_unknown_not_crash(self):
        """RED-GREEN anchor: `wtmp begins` AFTER the instant means wtmp
        cannot testify -- the crash label would be a lie."""
        assert epochs._corroborate_shutdown(
            self.INSTANT, [], self.INSTANT + timedelta(days=1)
        ) == EVENT_SHUTDOWN_UNKNOWN

    def test_absent_wtmp_is_unknown(self):
        assert epochs._corroborate_shutdown(
            self.INSTANT, [], None) == EVENT_SHUTDOWN_UNKNOWN


# ── the wtmp rungs ────────────────────────────────────────────────────


class TestWtmpFullYear:
    def test_records_and_begins_parse(self, utc_tz_seam):
        records, begins = epochs._parse_last_records(WTMP_FULL)
        assert ("reboot", _utc(2026, 8, 7, 3, 3, 20)) in records
        assert ("shutdown", _utc(2026, 8, 7, 3, 4, 5)) in records
        assert begins == _utc(2026, 8, 6, 12, 0, 0)

    def test_journal_plus_wtmp_corroborates_clean(self, monkeypatch,
                                                  utc_tz_seam):
        """The full chain: journal spans + a matching wtmp shutdown
        record upgrade `unknown` to `clean`."""
        _canned(monkeypatch, {J_ARGV: (0, WSL_TEXT),
                              LF_ARGV: (0, WTMP_FULL)})
        fences = epochs._read_fences_posix()
        by_time = {f.at_utc: f.event_id for f in fences}
        # Journal boot -1 ends 03:04:07; wtmp shutdown 03:04:05 -> clean.
        assert by_time[_utc(2026, 8, 7, 3, 4, 7)] == EVENT_SHUTDOWN_CLEAN
        # Boot -2 ends 19:25:32 on Aug 6; wtmp covers (begins 12:00)
        # but holds no record -> crash-by-absence.
        assert by_time[_utc(2026, 8, 6, 19, 25, 32)] == \
            EVENT_SHUTDOWN_UNEXPECTED

    def test_wtmp_only_synthesis_with_coverage_guard(self, monkeypatch,
                                                     utc_tz_seam):
        """No journal at all: shutdown records fence directly; a covered
        silent gap synthesizes a crash at boot-1s; an UNCOVERED gap
        (before `wtmp begins`) synthesizes nothing."""
        _canned(monkeypatch, {LF_ARGV: (0, WTMP_FULL)})
        fences = epochs._read_fences_posix()
        ids = [(f.at_utc, f.event_id) for f in fences]
        assert (_utc(2026, 8, 7, 3, 4, 5), EVENT_SHUTDOWN_CLEAN) in ids
        # Gap (19:25:00 boot .. 03:03:20 boot) holds the 03:04:05 record?
        # No: that record is AFTER the newer boot. The silent covered gap
        # before the 03:03:20 boot -> synthesized crash at boot-1s...
        # except the shutdown record at 03:04:05 sits after it; the
        # window checked is [prev_boot, boot): no record -> synthesis.
        assert (_utc(2026, 8, 7, 3, 3, 19), EVENT_SHUTDOWN_UNEXPECTED) in ids
        # The oldest boot's window starts at `wtmp begins` (covered) --
        # silent -> synthesis; nothing BEFORE begins is ever fenced.
        assert (_utc(2026, 8, 6, 19, 24, 59), EVENT_SHUTDOWN_UNEXPECTED) in ids


class TestBsdNoYear:
    def test_rollover_ordering_guard(self, utc_tz_seam):
        """RED-GREEN anchor: Dec 25 below a Dec 30 row is LAST year --
        the future guard alone cannot catch it."""
        out = ("reboot    ~                         Wed Dec 30 08:00\n"
               "reboot    ~                         Sat Jul  4 10:00\n"
               "reboot    ~                         Thu Dec 25 09:00\n")
        now_local = datetime(2026, 12, 30, 12, 0)
        placed, _ = epochs._parse_last_no_year(out, now_local)
        years = [at.year for _kind, at in placed]
        assert years == [2026, 2026, 2025]

    def test_future_guard(self, utc_tz_seam):
        out = "reboot    ~                         Mon Nov 30 08:00\n"
        placed, _ = epochs._parse_last_no_year(
            out, datetime(2026, 3, 1, 0, 0))
        assert placed[0][1].year == 2025


# ── chain control flow ────────────────────────────────────────────────


class TestChain:
    def test_volatile_single_boot_journal_falls_to_wtmp(self, monkeypatch,
                                                        utc_tz_seam):
        one_boot = json.dumps([{"index": 0, "boot_id": "a" * 32,
                                "first_entry": _usec(_utc(2026, 8, 7, 10, 32, 10)),
                                "last_entry": _usec(_utc(2026, 8, 7, 10, 35, 0))}])
        _canned(monkeypatch, {J_ARGV: (0, one_boot),
                              LF_ARGV: (0, WTMP_FULL)})
        fences = epochs._read_fences_posix()
        # wtmp's shutdown record made it in -- the 1-boot journal did
        # not swallow the answer.
        assert any(f.event_id == EVENT_SHUTDOWN_CLEAN for f in fences)

    def test_terminal_failure_names_every_rung(self, monkeypatch):
        _canned(monkeypatch, {})  # everything FileNotFoundError
        with pytest.raises(FenceUnavailableError) as exc:
            epochs._read_fences_posix()
        msg = str(exc.value)
        assert "journalctl" in msg and "last" in msg
        assert "still work" in msg

    def test_read_fences_dispatches_posix(self, monkeypatch, utc_tz_seam):
        monkeypatch.setattr(epochs.sys, "platform", "linux")
        _canned(monkeypatch, {J_ARGV: (0, WSL_TEXT), LF_ARGV: (0, "")})
        fences = epochs.read_fences()
        assert fences and fences[0].event_id in (
            EVENT_BOOT, EVENT_SHUTDOWN_UNKNOWN)

    def test_windows_path_untouched(self, monkeypatch):
        monkeypatch.setattr(epochs.sys, "platform", "win32")
        monkeypatch.setattr(epochs, "_run_powershell",
                            lambda script, timeout=30.0:
                            "2026-08-07T03:04:07.0000000Z|6006\n")
        fences = epochs.read_fences()
        assert fences[0].event_id == EVENT_SHUTDOWN_CLEAN


# ── the unknown cause through latest_epoch ────────────────────────────


class TestUnknownCause:
    def test_unknown_shutdown_yields_unknown_cause(self):
        fences = [
            Fence(_utc(2026, 8, 7, 10, 32, 10), EVENT_BOOT),
            Fence(_utc(2026, 8, 7, 3, 4, 7), EVENT_SHUTDOWN_UNKNOWN),
            Fence(_utc(2026, 8, 7, 3, 3, 15), EVENT_BOOT),
        ]
        epoch = latest_epoch(fences)
        assert epoch is not None
        assert epoch.cause == "unknown"
        assert epoch.shutdown_utc == _utc(2026, 8, 7, 3, 4, 7)

    def test_clean_evidence_in_cluster_beats_unknown(self):
        fences = [
            Fence(_utc(2026, 8, 7, 3, 4, 7), EVENT_SHUTDOWN_UNKNOWN),
            Fence(_utc(2026, 8, 7, 3, 4, 0), EVENT_SHUTDOWN_CLEAN),
        ]
        assert latest_epoch(fences).cause == "clean"

    def test_render_label(self):
        from claude_session_backup.set_render import _cause_label
        assert "unknown" in _cause_label("unknown")

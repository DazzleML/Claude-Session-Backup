"""epochs.py: fence parsing, epoch math, roster building (#60, #61).

Pure-logic tests -- no console, no database, no subprocess. The real
`Get-WinEvent` is never invoked here (the `_run_powershell` seam is
patched where needed); the human checklist covers a real reboot on a
real box. Timestamp assertions are TZ-agnostic: fences and index
timestamps are both constructed as explicit aware UTC datetimes.
"""

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import claude_session_backup.epochs as epochs
from claude_session_backup.epochs import (
    FALLBACK_WINDOW_HOURS,
    Fence,
    FenceUnavailableError,
    build_roster,
    epoch_window,
    format_gap,
    latest_epoch,
    parse_index_ts,
    read_fences,
    _parse_fence_lines,
)


def _utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


# ── timestamp normalization (the Python 3.10 floor) ──────────────────────

class TestParseIndexTs:
    def test_z_suffix(self):
        # 3.10's fromisoformat rejects a bare trailing Z -- must normalize.
        assert parse_index_ts("2026-07-25T16:16:32Z") == _utc(2026, 7, 25, 16, 16, 32)

    def test_powershell_seven_digit_fraction(self):
        # PowerShell '{0:o}' emits 7 fractional digits; 3.10 accepts only
        # 3 or 6. This is the exact shape Get-WinEvent produces.
        ts = parse_index_ts("2026-07-25T16:16:32.1234567Z")
        assert ts is not None
        assert ts.replace(microsecond=0) == _utc(2026, 7, 25, 16, 16, 32)

    def test_offset_form(self):
        assert parse_index_ts("2026-07-25T12:16:32-04:00") == _utc(2026, 7, 25, 16, 16, 32)

    def test_naive_assumed_utc(self):
        assert parse_index_ts("2026-07-25T16:16:32") == _utc(2026, 7, 25, 16, 16, 32)

    def test_index_millisecond_form(self):
        # The index stores JSONL-derived stamps like 2026-07-16T19:27:07.163Z
        assert parse_index_ts("2026-07-16T19:27:07.163Z") == datetime(
            2026, 7, 16, 19, 27, 7, 163000, tzinfo=timezone.utc
        )

    @pytest.mark.parametrize("bad", [None, "", "not-a-date", "2026-99-99T00:00:00Z"])
    def test_unparseable_returns_none(self, bad):
        assert parse_index_ts(bad) is None

    def test_normalization_at_string_level(self):
        # On 3.11+ fromisoformat parses Z/7-digit forms natively, so the
        # roundtrip tests above cannot prove the 3.10 hardening exists.
        # Assert the normalization itself: THIS is what 3.10 requires.
        from claude_session_backup.epochs import _normalize_iso
        assert _normalize_iso("2026-07-25T16:16:32Z") == "2026-07-25T16:16:32+00:00"
        assert (_normalize_iso("2026-07-25T16:16:32.1234567Z")
                == "2026-07-25T16:16:32.123456+00:00")
        # 3/6-digit fractions and offset forms pass through untouched.
        assert (_normalize_iso("2026-07-16T19:27:07.163Z")
                == "2026-07-16T19:27:07.163+00:00")
        assert (_normalize_iso("2026-07-25T12:16:32-04:00")
                == "2026-07-25T12:16:32-04:00")


# ── fence-line parsing ───────────────────────────────────────────────────

class TestParseFenceLines:
    def test_parses_and_sorts_newest_first(self):
        text = (
            "2026-07-15T08:18:17.0000000Z|6005\n"
            "2026-07-25T16:16:32.0000000Z|6006\n"
            "2026-07-25T16:16:18.0000000Z|1074\n"
        )
        fences = _parse_fence_lines(text)
        assert [f.event_id for f in fences] == [6006, 1074, 6005]
        assert fences[0].at_utc == _utc(2026, 7, 25, 16, 16, 32)

    def test_garbage_lines_skipped(self):
        text = (
            "WARNING: something powershell said\n"
            "\n"
            "not|a-timestamp\n"
            "2026-07-25T16:16:32Z|6006\n"
            "2026-07-25T16:16:32Z|notanid\n"
        )
        fences = _parse_fence_lines(text)
        assert len(fences) == 1
        assert fences[0].event_id == 6006

    def test_unknown_event_ids_dropped(self):
        assert _parse_fence_lines("2026-07-25T16:16:32Z|9999\n") == []

    def test_empty_input(self):
        assert _parse_fence_lines("") == []


# ── read_fences: platform gate + seam ────────────────────────────────────

class TestReadFences:
    def test_posix_dispatches_to_the_chain(self, monkeypatch):
        """R2 replaced the Windows-only refusal with the POSIX chain --
        deep coverage lives in test_epochs_posix.py; this pins the
        dispatch and the terminal error's helpfulness."""
        monkeypatch.setattr(epochs.sys, "platform", "linux")

        def nothing_runnable(argv, timeout=15.0):
            raise FileNotFoundError(argv[0])

        monkeypatch.setattr(epochs, "_run_command", nothing_runnable)
        with pytest.raises(FenceUnavailableError, match="still work"):
            read_fences()

    def test_seam_is_used_no_real_subprocess(self, monkeypatch):
        monkeypatch.setattr(epochs.sys, "platform", "win32")
        calls = []

        def fake_ps(script, timeout=30.0):
            calls.append(script)
            return "2026-07-25T16:16:32Z|6006\n"

        monkeypatch.setattr(epochs, "_run_powershell", fake_ps)
        fences = read_fences()
        assert len(calls) == 1
        assert "Get-WinEvent" in calls[0]
        assert fences == [Fence(_utc(2026, 7, 25, 16, 16, 32), 6006)]

    def test_missing_powershell_becomes_fence_unavailable(self, monkeypatch):
        monkeypatch.setattr(epochs.sys, "platform", "win32")

        def raise_fnf(script, timeout=30.0):
            raise FileNotFoundError("powershell")

        monkeypatch.setattr(epochs, "_run_powershell", raise_fnf)
        with pytest.raises(FenceUnavailableError, match="PowerShell"):
            read_fences()

    def test_timeout_becomes_fence_unavailable(self, monkeypatch):
        monkeypatch.setattr(epochs.sys, "platform", "win32")

        def raise_timeout(script, timeout=30.0):
            raise subprocess.TimeoutExpired(cmd="powershell", timeout=timeout)

        monkeypatch.setattr(epochs, "_run_powershell", raise_timeout)
        with pytest.raises(FenceUnavailableError, match="timed out"):
            read_fences()

    def test_no_events_is_empty_not_error(self, monkeypatch):
        monkeypatch.setattr(epochs.sys, "platform", "win32")
        monkeypatch.setattr(epochs, "_run_powershell", lambda *a, **k: "")
        assert read_fences() == []


# ── latest_epoch: collapse, cause labels, boundaries ─────────────────────

class TestLatestEpoch:
    def test_no_shutdown_fence_returns_none(self):
        assert latest_epoch([Fence(_utc(2026, 7, 25, 16, 17), 6005)]) is None
        assert latest_epoch([]) is None

    def test_basic_epoch_with_boot_and_prev_fence(self):
        fences = [
            Fence(_utc(2026, 7, 25, 16, 17, 18), 6005),  # boot after
            Fence(_utc(2026, 7, 25, 16, 16, 32), 6006),  # shutdown
            Fence(_utc(2026, 7, 15, 8, 18, 17), 6005),   # boot that started epoch
            Fence(_utc(2026, 7, 15, 8, 17, 38), 6006),   # previous shutdown
        ]
        ep = latest_epoch(fences)
        assert ep.shutdown_utc == _utc(2026, 7, 25, 16, 16, 32)
        assert ep.boot_utc == _utc(2026, 7, 25, 16, 17, 18)
        assert ep.prev_fence_utc == _utc(2026, 7, 15, 8, 18, 17)
        assert ep.cause == "clean"

    def test_cause_initiated_by_process(self):
        # The Windows Update signature: 1074 13s before the 6006.
        fences = [
            Fence(_utc(2026, 7, 25, 16, 16, 32), 6006),
            Fence(_utc(2026, 7, 25, 16, 16, 18), 1074),
        ]
        assert latest_epoch(fences).cause == "initiated-by-process"

    def test_cause_unexpected(self):
        assert latest_epoch([Fence(_utc(2026, 7, 25, 16, 16), 6008)]).cause == "unexpected"

    def test_far_away_1074_does_not_label(self):
        fences = [
            Fence(_utc(2026, 7, 25, 16, 16, 32), 6006),
            Fence(_utc(2026, 7, 25, 15, 0, 0), 1074),  # 76 min earlier
        ]
        assert latest_epoch(fences).cause == "clean"

    def test_double_cycle_prev_fence_skips_intermediate_boot(self):
        """The REAL 2026-07-15 update restart, verbatim from the event log.

        Update restarts double-cycle: shutdown/boot/shutdown/boot within
        ~2 minutes. prev_fence must be the boot that started the ENDED
        epoch (07-11), NOT the intermediate mid-update boot at 8:17:09 --
        that choice would collapse the default window to 29 seconds and
        render an empty roster after exactly the restart this feature
        exists for. (Red-green: the pre-cluster implementation fails
        this test with prev_fence == 8:17:09.)
        """
        fences = [
            Fence(_utc(2026, 7, 15, 8, 18, 17), 6005),  # final boot
            Fence(_utc(2026, 7, 15, 8, 17, 38), 6006),  # second shutdown
            Fence(_utc(2026, 7, 15, 8, 17, 38), 1074),
            Fence(_utc(2026, 7, 15, 8, 17, 9), 6005),   # INTERMEDIATE boot
            Fence(_utc(2026, 7, 15, 8, 16, 21), 6006),  # first shutdown
            Fence(_utc(2026, 7, 15, 8, 15, 6), 1074),
            Fence(_utc(2026, 7, 11, 7, 45, 29), 6005),  # boot that began the epoch
        ]
        ep = latest_epoch(fences)
        assert ep.shutdown_utc == _utc(2026, 7, 15, 8, 17, 38)
        assert ep.boot_utc == _utc(2026, 7, 15, 8, 18, 17)
        assert ep.prev_fence_utc == _utc(2026, 7, 11, 7, 45, 29)
        assert ep.cause == "initiated-by-process"

    def test_unexpected_shutdown_in_cluster_wins_cause(self):
        # A crash (6008) followed by an auto-restart cycle: the alarming
        # cause is the one to surface.
        fences = [
            Fence(_utc(2026, 7, 15, 8, 17, 38), 6006),
            Fence(_utc(2026, 7, 15, 8, 16, 21), 6008),
            Fence(_utc(2026, 7, 11, 7, 45, 29), 6005),
        ]
        assert latest_epoch(fences).cause == "unexpected"

    def test_distinct_shutdowns_do_not_collapse(self):
        fences = [
            Fence(_utc(2026, 7, 25, 16, 16), 6006),
            Fence(_utc(2026, 7, 15, 8, 17), 6006),
        ]
        ep = latest_epoch(fences)
        assert ep.shutdown_utc == _utc(2026, 7, 25, 16, 16)


# ── epoch_window ─────────────────────────────────────────────────────────

class TestEpochWindow:
    def _epoch(self, prev=None):
        return epochs.Epoch(
            shutdown_utc=_utc(2026, 7, 25, 16, 16, 32),
            cause="clean",
            boot_utc=_utc(2026, 7, 25, 16, 17, 18),
            prev_fence_utc=prev,
        )

    def test_default_is_previous_fence(self):
        prev = _utc(2026, 7, 15, 8, 18, 17)
        lo, hi, source = epoch_window(self._epoch(prev=prev))
        assert (lo, hi, source) == (prev, _utc(2026, 7, 25, 16, 16, 32), "previous-fence")

    def test_override_wins_over_previous_fence(self):
        lo, hi, source = epoch_window(self._epoch(prev=_utc(2026, 7, 15)), 24.0)
        assert source == "override"
        assert hi - lo == timedelta(hours=24)

    def test_fallback_when_previous_unknown(self):
        lo, hi, source = epoch_window(self._epoch(prev=None))
        assert source == "fallback-72h"
        assert hi - lo == timedelta(hours=FALLBACK_WINDOW_HOURS)


# ── build_roster ─────────────────────────────────────────────────────────

def _row(sid, last_active, name=None, deleted_at=None, is_fork=0):
    return {
        "session_id": sid,
        "session_name": name or f"session-{sid}",
        "project": "C--code-test",
        "start_folder": "C:\\code\\test",
        "started_at": "2026-07-01T00:00:00Z",
        "last_active_at": last_active,
        "jsonl_path": None,
        "jsonl_mtime": None,
        "deleted_at": deleted_at,
        "is_fork": is_fork,
    }


class TestBuildRoster:
    LO = _utc(2026, 7, 15, 8, 18, 17)
    HI = _utc(2026, 7, 25, 16, 16, 32)

    def test_membership_is_activity_in_window(self):
        rows = [
            _row("a" * 36, "2026-07-23T17:24:00Z"),          # in
            _row("b" * 36, "2026-07-14T00:00:00Z"),          # before lo
            _row("c" * 36, "2026-07-25T16:20:00Z"),          # after hi (post-boot)
        ]
        members, missing = build_roster(rows, self.LO, self.HI)
        assert [m["session_id"] for m in members] == ["a" * 36]
        assert missing == 0

    def test_boundaries_inclusive(self):
        rows = [
            _row("a" * 36, "2026-07-15T08:18:17Z"),  # == lo
            _row("b" * 36, "2026-07-25T16:16:32Z"),  # == hi
        ]
        members, _ = build_roster(rows, self.LO, self.HI)
        assert len(members) == 2

    def test_activity_order_ascending_with_stable_indices(self):
        rows = [
            _row("b" * 36, "2026-07-24T00:00:00Z"),
            _row("a" * 36, "2026-07-20T00:00:00Z"),
        ]
        members, _ = build_roster(rows, self.LO, self.HI)
        assert [m["session_id"][0] for m in members] == ["a", "b"]
        assert [m["index"] for m in members] == [1, 2]

    def test_tie_broken_by_session_id(self):
        rows = [
            _row("b" * 36, "2026-07-20T00:00:00Z"),
            _row("a" * 36, "2026-07-20T00:00:00Z"),
        ]
        members, _ = build_roster(rows, self.LO, self.HI)
        assert [m["session_id"][0] for m in members] == ["a", "b"]

    def test_missing_timestamps_counted_never_dropped_silently(self):
        rows = [
            _row("a" * 36, "2026-07-20T00:00:00Z"),
            _row("b" * 36, None),
            _row("c" * 36, "garbage"),
        ]
        members, missing = build_roster(rows, self.LO, self.HI)
        assert len(members) == 1
        assert missing == 2

    def test_purged_and_fork_flags_carried(self):
        rows = [_row("a" * 36, "2026-07-20T00:00:00Z",
                     deleted_at="2026-07-26T00:00:00Z", is_fork=1)]
        members, _ = build_roster(rows, self.LO, self.HI)
        assert members[0]["purged"] is True
        assert members[0]["is_fork"] is True

    def test_sqlite_rows_accepted(self):
        # build_roster must take sqlite3.Row directly (key access only).
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE sessions (session_id, session_name, project,"
            " start_folder, started_at, last_active_at, jsonl_path,"
            " jsonl_mtime, deleted_at, is_fork)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES ('a', 'n', 'p', 'f',"
            " '2026-07-01T00:00:00Z', '2026-07-20T00:00:00Z',"
            " NULL, NULL, NULL, 0)"
        )
        rows = conn.execute("SELECT * FROM sessions").fetchall()
        members, missing = build_roster(rows, self.LO, self.HI)
        assert len(members) == 1 and missing == 0
        conn.close()


# ── local-time fences vs UTC index timestamps (#61 AC) ───────────────────

class TestFenceTimezoneAgainstIndex:
    """Fences and index timestamps must land on one timeline.

    The event log reports LOCAL time; the query converts with
    ``.ToUniversalTime()`` so fences normally arrive UTC-suffixed. This
    pins the harder case -- a fence delivered in local-OFFSET form -- and
    proves membership still bounds Z-suffixed index timestamps exactly.
    Without correct offset handling every boundary here shifts by 4h and
    the roster silently gains or loses its edge members.
    """

    # 12:16:32-04:00 IS 16:16:32Z -- same instant, two spellings.
    FENCE_LOCAL = (
        "2026-07-25T12:17:18.0000000-04:00|6005\n"
        "2026-07-25T12:16:32.0000000-04:00|6006\n"
        "2026-07-15T04:18:17.0000000-04:00|6005\n"
    )

    def test_offset_fence_yields_utc_epoch(self):
        ep = latest_epoch(_parse_fence_lines(self.FENCE_LOCAL))
        assert ep.shutdown_utc == _utc(2026, 7, 25, 16, 16, 32)
        assert ep.prev_fence_utc == _utc(2026, 7, 15, 8, 18, 17)

    def test_membership_boundaries_survive_the_offset(self):
        ep = latest_epoch(_parse_fence_lines(self.FENCE_LOCAL))
        lo, hi, _ = epoch_window(ep)
        rows = [
            # 1s inside each edge, expressed the way the index stores them.
            _row("a" * 36, "2026-07-15T08:18:18Z"),
            _row("b" * 36, "2026-07-25T16:16:31Z"),
            # 1s outside each edge.
            _row("c" * 36, "2026-07-15T08:18:16Z"),
            _row("d" * 36, "2026-07-25T16:16:33Z"),
            # Inside only if the offset were mishandled (local read as UTC).
            _row("e" * 36, "2026-07-25T12:30:00Z"),
        ]
        members, _ = build_roster(rows, lo, hi)
        assert [m["session_id"][0] for m in members] == ["a", "e", "b"]

    def test_equivalent_spellings_agree(self):
        """Z-form and offset-form fence histories must be interchangeable."""
        fence_utc = (
            "2026-07-25T16:17:18Z|6005\n"
            "2026-07-25T16:16:32Z|6006\n"
            "2026-07-15T08:18:17Z|6005\n"
        )
        assert (epoch_window(latest_epoch(_parse_fence_lines(fence_utc)))
                == epoch_window(latest_epoch(_parse_fence_lines(self.FENCE_LOCAL))))


# ── format_gap ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("delta,expected", [
    (timedelta(days=1, hours=22, minutes=10), "1d22h"),
    (timedelta(hours=3, minutes=36), "3h36m"),
    (timedelta(minutes=5, seconds=30), "5m"),
    (timedelta(seconds=46), "46s"),
    (timedelta(seconds=0), "0s"),
    (timedelta(seconds=-5), "0s"),  # clock skew never renders negative
])
def test_format_gap(delta, expected):
    assert format_gap(delta) == expected

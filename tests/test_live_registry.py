"""Live Session Registry: entries, boundary classification, sweep (#64).

Pure filesystem tests -- no hooks, no console, no processes. The hook
integration (real stdin payloads through backup-hook.py) lives in
test_backup_hook.py; process verification in test_liveness.py.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import claude_session_backup.live_registry as lr


def _utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


BOOT = _utc(2026, 8, 1, 12, 0, 0)


class TestEntryLifecycle:
    def test_record_and_read(self, tmp_path):
        assert lr.record_session_start(tmp_path, "aaa-1", source="startup",
                                       cwd=r"C:\x") is True
        entries = lr.read_entries(tmp_path)
        assert len(entries) == 1
        assert entries[0]["session_id"] == "aaa-1"
        assert entries[0]["source"] == "startup"
        assert entries[0]["cwd"] == r"C:\x"
        assert lr.parse_entry_ts(entries[0]["started_at"]) is not None

    def test_write_if_missing_preserves_started_at(self, tmp_path):
        """A source=compact restart must not reset the open time."""
        lr.record_session_start(tmp_path, "aaa-1", source="startup")
        before = lr.read_entries(tmp_path)[0]["started_at"]
        assert lr.record_session_start(tmp_path, "aaa-1",
                                       source="compact") is False
        assert lr.read_entries(tmp_path)[0]["started_at"] == before

    def test_end_removes_and_is_idempotent(self, tmp_path):
        lr.record_session_start(tmp_path, "aaa-1")
        assert lr.record_session_end(tmp_path, "aaa-1") is True
        assert lr.record_session_end(tmp_path, "aaa-1") is False
        assert lr.read_entries(tmp_path) == []

    def test_empty_ids_are_noops(self, tmp_path):
        assert lr.record_session_start(tmp_path, "") is False
        assert lr.record_session_end(tmp_path, "") is False

    def test_no_name_field_ever_written(self, tmp_path):
        """UUID-keyed and name-free BY DESIGN: names are resolved at query
        time through the index, so the logger's renames cost nothing."""
        lr.record_session_start(tmp_path, "aaa-1", source="startup", cwd="x")
        raw = json.loads(
            lr.entry_path(tmp_path, "aaa-1").read_text(encoding="utf-8"))
        assert set(raw) == {"session_id", "started_at", "source", "cwd"}

    def test_open_order_is_started_at_order(self, tmp_path):
        for sid, ts in (("bbb-2", "2026-08-01T13:00:00Z"),
                        ("aaa-1", "2026-08-01T12:30:00Z")):
            lr.entry_path(tmp_path, sid).parent.mkdir(parents=True,
                                                      exist_ok=True)
            lr.entry_path(tmp_path, sid).write_text(json.dumps({
                "session_id": sid, "started_at": ts,
                "source": "", "cwd": "",
            }), encoding="utf-8")
        assert [e["session_id"] for e in lr.read_entries(tmp_path)] == \
            ["aaa-1", "bbb-2"]

    def test_garbage_entry_counted_not_dropped(self, tmp_path):
        """A corrupt file still testifies that SOMETHING started -- the
        filename is the UUID. Null timestamp, never silently dropped."""
        lr.live_dir(tmp_path).mkdir(parents=True)
        lr.entry_path(tmp_path, "ccc-3").write_text("{not json",
                                                    encoding="utf-8")
        entries = lr.read_entries(tmp_path)
        assert len(entries) == 1
        assert entries[0]["session_id"] == "ccc-3"
        assert entries[0]["started_at"] is None

    def test_snapshot_file_not_an_entry(self, tmp_path):
        lr.live_dir(tmp_path).mkdir(parents=True)
        lr.snapshot_path(tmp_path).write_text("{}", encoding="utf-8")
        assert lr.read_entries(tmp_path) == []


class TestBoundaryClassification:
    def _entry(self, sid, ts):
        return {"session_id": sid, "started_at": ts, "source": "", "cwd": ""}

    def test_split(self):
        entries = [
            self._entry("old-1", "2026-07-30T10:00:00Z"),
            self._entry("new-1", "2026-08-01T13:00:00Z"),
        ]
        this_boot, pre_boot = lr.split_by_boot(entries, BOOT)
        assert [e["session_id"] for e in this_boot] == ["new-1"]
        assert [e["session_id"] for e in pre_boot] == ["old-1"]

    def test_unknown_boot_errs_toward_current(self):
        """The safe direction: misfiled-as-current shows 'no exit
        observed' (true); misfiled-as-pre-boot would sweep a LIVE session
        into the shutdown snapshot."""
        entries = [self._entry("old-1", "2020-01-01T00:00:00Z")]
        this_boot, pre_boot = lr.split_by_boot(entries, None)
        assert len(this_boot) == 1 and pre_boot == []

    def test_unparseable_timestamp_errs_toward_current(self):
        entries = [self._entry("bad-1", None)]
        this_boot, pre_boot = lr.split_by_boot(entries, BOOT)
        assert len(this_boot) == 1 and pre_boot == []


class TestBoundarySweep:
    def _write(self, tmp_path, sid, ts):
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        lr.entry_path(tmp_path, sid).write_text(json.dumps({
            "session_id": sid, "started_at": ts, "source": "startup",
            "cwd": "C:/w",
        }), encoding="utf-8")

    def test_sweep_freezes_and_clears(self, tmp_path):
        self._write(tmp_path, "old-1", "2026-07-30T10:00:00Z")
        self._write(tmp_path, "old-2", "2026-07-31T10:00:00Z")
        self._write(tmp_path, "new-1", "2026-08-01T13:00:00Z")
        assert lr.sweep_boundary(tmp_path, BOOT) == 2
        snap = lr.read_snapshot(tmp_path)
        assert {e["session_id"] for e in snap["open_at_shutdown"]} == \
            {"old-1", "old-2"}
        assert snap["boot_at"] == "2026-08-01T12:00:00Z"
        # Live entries untouched; swept files gone.
        assert [e["session_id"] for e in lr.read_entries(tmp_path)] == \
            ["new-1"]

    def test_sweep_with_nothing_pre_boot_is_a_noop(self, tmp_path):
        self._write(tmp_path, "new-1", "2026-08-01T13:00:00Z")
        assert lr.sweep_boundary(tmp_path, BOOT) == 0
        assert lr.read_snapshot(tmp_path) is None

    def test_sweep_unknown_boot_sweeps_nothing(self, tmp_path):
        """Cannot place the boundary -> must not guess: a wrong guess
        would freeze a live session as 'open at shutdown'."""
        self._write(tmp_path, "old-1", "2020-01-01T00:00:00Z")
        assert lr.sweep_boundary(tmp_path, None) == 0
        assert len(lr.read_entries(tmp_path)) == 1

    def test_sweep_missing_dir_is_a_noop(self, tmp_path):
        assert lr.sweep_boundary(tmp_path, BOOT) == 0

    def test_corrupt_snapshot_reads_as_none(self, tmp_path):
        lr.live_dir(tmp_path).mkdir(parents=True)
        lr.snapshot_path(tmp_path).write_text("{broken", encoding="utf-8")
        assert lr.read_snapshot(tmp_path) is None


class TestBootTime:
    def test_windows_path(self, monkeypatch):
        if lr.sys.platform != "win32":
            pytest.skip("GetTickCount64 path")
        boot = lr.current_boot_utc()
        assert boot is not None
        # Sanity: in the past, and not absurdly far (a year).
        now = datetime.now(timezone.utc)
        assert timedelta(0) < now - boot < timedelta(days=366)

    def test_linux_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(lr.sys, "platform", "linux")
        uptime = tmp_path / "uptime"
        uptime.write_text("3600.00 7200.00\n", encoding="ascii")
        real_open = open

        def fake_open(path, *a, **k):
            if path == "/proc/uptime":
                return real_open(uptime, *a, **k)
            return real_open(path, *a, **k)

        monkeypatch.setattr("builtins.open", fake_open)
        boot = lr.current_boot_utc()
        assert boot is not None
        drift = abs((datetime.now(timezone.utc) - boot).total_seconds() - 3600)
        assert drift < 60

    def test_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(lr.sys, "platform", "linux")

        def raise_open(path, *a, **k):
            raise OSError("no /proc here")

        monkeypatch.setattr("builtins.open", raise_open)
        assert lr.current_boot_utc() is None

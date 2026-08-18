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

    def test_sweep_with_nothing_pre_boot_records_proven_zero(self, tmp_path):
        """Nothing to sweep is still a MEASUREMENT. The record is written
        with an empty roster so a reader can tell "csb looked and found
        nobody" from "csb never looked" -- before this they were both
        `None` and F had to report proven-zero as unknown."""
        self._write(tmp_path, "new-1", "2026-08-01T13:00:00Z")
        assert lr.sweep_boundary(tmp_path, BOOT) == 0
        snap = lr.read_snapshot(tmp_path)
        assert snap is not None
        assert snap["open_at_shutdown"] == []
        assert lr.open_at_shutdown(tmp_path, boot_utc=BOOT) == []

    def test_sweep_without_a_registry_dir_records_nothing(self, tmp_path):
        """csb was never watching -- claiming zero would overclaim."""
        assert lr.sweep_boundary(tmp_path, BOOT) == 0
        assert lr.read_snapshot(tmp_path) is None

    def test_retention_evicts_empty_records_before_evidence(self, tmp_path):
        """Retention prefers EVIDENCE over recency: K empty boundaries
        must not push out an older one that still names open sessions."""
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        bdir = lr.boundary_dir(tmp_path)
        bdir.mkdir(parents=True, exist_ok=True)

        def rec(stamp, names):
            (bdir / f"boundary-{stamp}.json").write_text(json.dumps({
                "version": lr.SNAPSHOT_VERSION, "boot_at": stamp,
                "captured_at": stamp,
                "open_at_shutdown": [{"session_id": n} for n in names],
            }), encoding="utf-8")

        rec("20260801T000000Z", ["IMPORTANT"])          # oldest, real
        for i in range(1, lr.BOUNDARY_RETENTION + 1):   # newer, all empty
            rec(f"2026080{i + 1}T000000Z", [])
        self._write(tmp_path, "dead-1", "2026-07-30T10:00:00Z")
        lr.sweep_boundary(tmp_path, BOOT)

        surviving = sorted(p.name for p in bdir.glob("boundary-*.json"))
        assert len(surviving) <= lr.BOUNDARY_RETENTION
        assert "boundary-20260801T000000Z.json" in surviving, (
            "an empty record evicted real open-at-shutdown evidence")

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


class TestResumedPreBootSessionIsNotDead:
    """A session RESUMED after a boot keeps its original ``started_at``
    (the hook's refresh path preserves it so /compact cannot reset the
    open time) while its ``pid_at`` is stamped fresh. The entry therefore
    reads pre-boot by identity and post-boot by liveness.

    ``pid_at`` is the liveness field: it records when csb last saw this
    session's host process. A pid_at AFTER the boot is proof the session
    is alive on THIS side of the boundary, and the sweep must not bury it.

    Observed live on 2026-08-12: a forced restart, a resume at 12:23:51
    that refreshed pid 11516, and a sweep one second later that deleted
    the entry -- leaving a running session invisible to `csb set current`
    for two days. See the boundary-sweep-liveness-and-trigger DWP.
    """

    def _write(self, tmp_path, sid, started, pid=None, pid_at=None):
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        payload = {"session_id": sid, "started_at": started,
                   "source": "resume", "cwd": "C:/w"}
        if pid_at is not None:
            payload["pid"] = pid if pid is not None else 4242
            payload["pid_at"] = pid_at
        lr.entry_path(tmp_path, sid).write_text(json.dumps(payload),
                                                encoding="utf-8")

    # ── the defect ────────────────────────────────────────────────────
    def test_post_boot_pid_at_classifies_as_this_boot(self, tmp_path):
        """AC1: liveness beats identity when the two disagree."""
        self._write(tmp_path, "resumed-1", "2026-07-30T10:00:00Z",
                    pid_at="2026-08-01T12:27:00Z")  # 27 min AFTER boot
        this_boot, pre_boot = lr.split_by_boot(
            lr.read_entries(tmp_path), BOOT)
        assert [e["session_id"] for e in this_boot] == ["resumed-1"]
        assert pre_boot == []

    def test_sweep_does_not_delete_a_proven_live_entry(self, tmp_path):
        """AC2a: the registration of a running session must survive."""
        self._write(tmp_path, "resumed-1", "2026-07-30T10:00:00Z",
                    pid_at="2026-08-01T12:27:00Z")
        lr.sweep_boundary(tmp_path, BOOT)
        assert [e["session_id"] for e in lr.read_entries(tmp_path)] == \
            ["resumed-1"]

    def test_sweep_still_records_it_as_open_at_shutdown(self, tmp_path):
        """AC2b: BOTH truths. It was open at the shutdown (historically
        true) AND it is open now (currently true). Recording must not
        cost the registration, and keeping the registration must not
        cost the epoch evidence."""
        self._write(tmp_path, "resumed-1", "2026-07-30T10:00:00Z",
                    pid_at="2026-08-01T12:27:00Z")
        lr.sweep_boundary(tmp_path, BOOT)
        snap = lr.read_snapshot(tmp_path)
        assert snap is not None
        assert "resumed-1" in {e["session_id"]
                               for e in snap["open_at_shutdown"]}

    def test_sweep_is_idempotent_for_a_live_entry(self, tmp_path):
        """AC8: re-sweeping must not thrash the snapshot or the file."""
        self._write(tmp_path, "resumed-1", "2026-07-30T10:00:00Z",
                    pid_at="2026-08-01T12:27:00Z")
        lr.sweep_boundary(tmp_path, BOOT)
        first = lr.snapshot_path(tmp_path).read_text(encoding="utf-8")
        lr.sweep_boundary(tmp_path, BOOT)
        assert lr.snapshot_path(tmp_path).read_text(encoding="utf-8") \
            == first
        assert lr.entry_path(tmp_path, "resumed-1").exists()

    # ── the regression boundary (these pass TODAY; pin them) ──────────
    def test_entry_with_no_pid_at_is_still_swept(self, tmp_path):
        """AC3a: no liveness evidence -> the old behaviour stands."""
        self._write(tmp_path, "dead-1", "2026-07-30T10:00:00Z")
        assert lr.sweep_boundary(tmp_path, BOOT) == 1
        assert lr.read_entries(tmp_path) == []
        assert {e["session_id"] for e in
                lr.read_snapshot(tmp_path)["open_at_shutdown"]} == {"dead-1"}

    def test_pre_boot_pid_at_is_still_swept(self, tmp_path):
        """AC3b: csb last saw the host BEFORE the boot -- that is not
        evidence of life, and the entry is still dead."""
        self._write(tmp_path, "dead-2", "2026-07-30T10:00:00Z",
                    pid_at="2026-08-01T09:00:00Z")  # 3h BEFORE boot
        assert lr.sweep_boundary(tmp_path, BOOT) == 1
        assert lr.read_entries(tmp_path) == []

    def test_unparseable_pid_at_does_not_rescue_a_dead_entry(self, tmp_path):
        """A garbage pid_at is not proof of anything; fall back to
        started_at rather than letting corruption pin an entry open."""
        self._write(tmp_path, "dead-3", "2026-07-30T10:00:00Z",
                    pid_at="not-a-timestamp")
        assert lr.sweep_boundary(tmp_path, BOOT) == 1
        assert lr.read_entries(tmp_path) == []

    # ── the boundary INSTANT itself (AC10) ────────────────────────────
    # Surfaced by mutation: `<` -> `<=` and `>=` -> `>` both went
    # undetected, so a session whose timestamp lands exactly on the boot
    # second was unconstrained in three separate comparisons.
    def test_started_exactly_at_boot_is_alive_not_swept(self, tmp_path):
        """A session that opened in the same second the machine booted
        is a session of THIS boot. `<` is the correct comparison and the
        suite must fail if it drifts to `<=`."""
        self._write(tmp_path, "edge-1", "2026-08-01T12:00:00Z")  # == BOOT
        this_boot, pre_boot = lr.split_by_boot(
            lr.read_entries(tmp_path), BOOT)
        assert [e["session_id"] for e in this_boot] == ["edge-1"]
        assert pre_boot == []
        assert lr.sweep_boundary(tmp_path, BOOT) == 0
        assert lr.entry_path(tmp_path, "edge-1").exists()

    def test_pid_at_without_a_pid_is_not_evidence(self, tmp_path):
        """A liveness claim with no host to check is unverifiable.

        `pid_at` says "csb saw the host alive at T" -- without `pid` there
        is no WHICH, so nothing can ever confirm it. Such an entry would
        otherwise survive every sweep forever on a claim no verifier can
        settle, which is precisely the graveyard the clearing step exists
        to prevent. Found by the pre-ship sweep against v0.9.12's own new
        predicate, not against old code.
        """
        self._write(tmp_path, "no-pid", "2026-07-30T10:00:00Z",
                    pid_at="2026-08-01T12:27:00Z")  # post-boot, but no pid
        entry = lr.read_entries(tmp_path)[0]
        del entry["pid"]  # _write only sets pid alongside pid_at; be explicit
        entry["pid"] = None
        assert lr.seen_alive_after_boot(entry, BOOT) is False

    def test_entry_with_pid_at_but_no_pid_is_swept(self, tmp_path):
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        lr.entry_path(tmp_path, "no-pid").write_text(json.dumps({
            "session_id": "no-pid", "started_at": "2026-07-30T10:00:00Z",
            "source": "resume", "cwd": "C:/w",
            "pid_at": "2026-08-01T12:27:00Z",   # no "pid" key at all
        }), encoding="utf-8")
        assert lr.sweep_boundary(tmp_path, BOOT) == 1
        assert lr.read_entries(tmp_path) == []

    def test_pid_at_exactly_at_boot_counts_as_alive(self, tmp_path):
        """`seen >= boot` -- a host confirmed in the boot second is on
        this side of the boundary. Pins `>=` against drifting to `>`."""
        self._write(tmp_path, "edge-2", "2026-07-30T10:00:00Z",
                    pid_at="2026-08-01T12:00:00Z")  # == BOOT
        assert lr.seen_alive_after_boot(
            lr.read_entries(tmp_path)[0], BOOT) is True
        lr.sweep_boundary(tmp_path, BOOT)
        assert lr.entry_path(tmp_path, "edge-2").exists()


class TestOpenAtShutdownReadThrough:
    """`open_at_shutdown` answers "who was open at this boundary" from
    the snapshot when one exists, and DERIVES it from unswept entries
    when it does not -- which is what makes `csb set last` correct in
    the window between a reboot and the first sweep. Read-only.
    """

    def _write(self, tmp_path, sid, started):
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        lr.entry_path(tmp_path, sid).write_text(json.dumps({
            "session_id": sid, "started_at": started, "source": "resume",
            "cwd": "C:/w"}), encoding="utf-8")

    def test_derives_from_unswept_entries_when_no_snapshot(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
        self._write(tmp_path, "was-open", "2026-07-30T10:00:00Z")
        self._write(tmp_path, "started-after", "2026-08-01T13:00:00Z")
        got = lr.open_at_shutdown(tmp_path, boot_utc=BOOT)
        assert [e["session_id"] for e in got] == ["was-open"]

    def test_deriving_never_writes(self, tmp_path, monkeypatch):
        """A display path must stay a display path: no snapshot appears
        and no entry is removed as a side effect of reading."""
        monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
        self._write(tmp_path, "was-open", "2026-07-30T10:00:00Z")
        lr.open_at_shutdown(tmp_path, boot_utc=BOOT)
        assert lr.read_snapshot(tmp_path) is None
        assert lr.entry_path(tmp_path, "was-open").exists()

    def test_snapshot_wins_when_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
        self._write(tmp_path, "recorded", "2026-07-30T10:00:00Z")
        lr.sweep_boundary(tmp_path, BOOT)
        self._write(tmp_path, "planted-later", "2026-07-29T10:00:00Z")
        got = lr.open_at_shutdown(tmp_path, boot_utc=BOOT)
        assert [e["session_id"] for e in got] == ["recorded"]

    def test_older_boundary_is_never_derived(self, tmp_path, monkeypatch):
        """Entries record when a session OPENED, never which shutdown it
        died at -- so deriving for an older epoch would credit every
        unswept entry to the wrong boundary. Unknown, not guessed."""
        monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
        self._write(tmp_path, "was-open", "2026-07-30T10:00:00Z")
        older = _utc(2026, 7, 20, 12, 0, 0)
        assert lr.open_at_shutdown(tmp_path, boot_utc=older) is None

    def test_started_exactly_at_boot_is_not_derived_as_open(
            self, tmp_path, monkeypatch):
        """AC10 in the read-through: same `<` boundary as the sweep."""
        monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
        self._write(tmp_path, "edge", "2026-08-01T12:00:00Z")  # == BOOT
        assert lr.open_at_shutdown(tmp_path, boot_utc=BOOT) is None

    def test_no_registry_dir_is_unknown_not_empty(self, tmp_path,
                                                  monkeypatch):
        """csb was never watching -- claiming "nobody was open" would
        overclaim (the hookless-honesty rule)."""
        monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
        assert lr.open_at_shutdown(tmp_path, boot_utc=BOOT) is None


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

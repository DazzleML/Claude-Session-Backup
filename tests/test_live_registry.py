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


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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


class TestBoundaryKeyDrift:
    """The boundary key is a DERIVED instant on Windows and Linux
    (``now - uptime``), and that derivation drifts -- ~6s over two days,
    measured on a real machine. A drifted key used to mint a second
    record for one boot, and because each later sweep sees strictly less
    (entries it already cleared are gone), the newer record held fewer
    sessions and the alias took the smaller set: eight recorded sessions
    became one, live, one day after v0.9.12 shipped.

    Recording is monotone -- "was open at this shutdown", once proven,
    is never disproven -- so a sweep merges into an existing record for
    the same boundary rather than replacing it.
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

    def _fixture(self, tmp_path):
        """The shape that produced the real failure: one entry the sweep
        clears, and one resumed session it records but keeps -- so a
        second sweep has strictly less left to record."""
        self._write(tmp_path, "dead-a", "2026-07-30T10:00:00Z")
        self._write(tmp_path, "live-b", "2026-07-30T10:00:00Z",
                    pid_at="2026-08-01T12:27:00Z")   # post-boot

    def _records(self, tmp_path):
        return sorted(lr.boundary_dir(tmp_path).glob("boundary-*.json"))

    def _ids(self, path):
        d = json.loads(path.read_text(encoding="utf-8"))
        return sorted(e["session_id"] for e in d["open_at_shutdown"])

    def test_drifted_key_does_not_mint_a_second_record(self, tmp_path):
        """AC1/AC9/AC10."""
        self._fixture(tmp_path)
        lr.sweep_boundary(tmp_path, BOOT)
        lr.sweep_boundary(tmp_path, BOOT - timedelta(seconds=8))
        recs = self._records(tmp_path)
        assert len(recs) == 1, "a drifted derivation minted a duplicate"
        assert self._ids(recs[0]) == ["dead-a", "live-b"]

    def test_the_alias_never_shrinks_for_one_boundary(self, tmp_path):
        """AC4 -- the observed symptom: 8 recorded sessions became 1."""
        self._fixture(tmp_path)
        lr.sweep_boundary(tmp_path, BOOT)
        lr.sweep_boundary(tmp_path, BOOT - timedelta(seconds=8))
        snap = lr.read_snapshot(tmp_path)
        assert sorted(e["session_id"] for e in snap["open_at_shutdown"])             == ["dead-a", "live-b"]

    def test_filename_and_boot_at_agree_and_do_not_wander(self, tmp_path):
        """AC11/AC11b -- the first record to describe a boundary names
        it, and repeated drift does not move that name."""
        self._fixture(tmp_path)
        lr.sweep_boundary(tmp_path, BOOT)
        first = self._records(tmp_path)[0].name
        for drift in (8, 20, 45):
            lr.sweep_boundary(tmp_path, BOOT - timedelta(seconds=drift))
        recs = self._records(tmp_path)
        assert len(recs) == 1 and recs[0].name == first
        stamp = json.loads(recs[0].read_text(encoding="utf-8"))["boot_at"]
        assert recs[0].name == "boundary-{}.json".format(
            stamp.replace("-", "").replace(":", ""))

    def test_a_complete_record_is_left_untouched(self, tmp_path):
        """AC14 -- when the record is already right, do nothing at all.

        The merge is a REPAIR path, not the normal case. In steady state
        it must not fire: rewriting identical contents churns
        ``captured_at`` (which means "when csb froze this", not "when csb
        last looked") and dirties a file that rides every backup commit.
        """
        self._fixture(tmp_path)
        lr.sweep_boundary(tmp_path, BOOT)
        rec = self._records(tmp_path)[0]
        before_mtime = rec.stat().st_mtime_ns
        before_text = rec.read_text(encoding="utf-8")
        alias_before = lr.snapshot_path(tmp_path).read_text(encoding="utf-8")

        for drift in (0, 6, 17):
            lr.sweep_boundary(tmp_path, BOOT - timedelta(seconds=drift))

        assert rec.read_text(encoding="utf-8") == before_text
        assert rec.stat().st_mtime_ns == before_mtime, (
            "a no-op sweep rewrote a record that was already complete")
        assert lr.snapshot_path(tmp_path).read_text(encoding="utf-8")             == alias_before

    def test_a_sweep_never_yields_a_smaller_record(self, tmp_path):
        """AC13 -- the INVARIANT, not a scenario.

        A boundary record is sealed except to add. Scenario tests prove
        the cases we thought of; this constrains the ones we did not.
        Whatever the registry looks like, a sweep must never leave a
        boundary holding fewer sessions than it already held.
        """
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        bdir = lr.boundary_dir(tmp_path)
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "boundary-20260801T120000Z.json").write_text(json.dumps({
            "version": lr.SNAPSHOT_VERSION,
            "boot_at": "2026-08-01T12:00:00Z",
            "captured_at": "2026-08-01T12:00:00Z",
            "open_at_shutdown": [{"session_id": s} for s in
                                 ("s-1", "s-2", "s-3", "s-4")],
        }), encoding="utf-8")

        def names():
            recs = sorted(bdir.glob("boundary-*.json"))
            assert len(recs) == 1
            d = json.loads(recs[0].read_text(encoding="utf-8"))
            return {e["session_id"] for e in d["open_at_shutdown"]}

        before = names()
        # Sweep repeatedly, with an EMPTY registry and a drifting key --
        # the worst case: nothing left on disk to re-observe.
        for drift in (0, 7, 19, 44, 120):
            lr.sweep_boundary(tmp_path, BOOT - timedelta(seconds=drift))
            after = names()
            assert after >= before, (
                f"a sweep shrank the record: {sorted(before)} -> "
                f"{sorted(after)}")
            before = after
        assert before == {"s-1", "s-2", "s-3", "s-4"}

    def test_two_genuine_boots_stay_separate(self, tmp_path):
        """AC5 -- tolerance must not merge boundaries hours apart."""
        self._fixture(tmp_path)
        lr.sweep_boundary(tmp_path, BOOT - timedelta(hours=6))
        lr.sweep_boundary(tmp_path, BOOT)
        assert len(self._records(tmp_path)) == 2

    def test_existing_duplicates_converge(self, tmp_path):
        """AC12 -- a machine that ALREADY holds duplicates is repaired by
        the next sweep, and the union takes from ALL covering records.

        Merging against only the FIRST match by filename is the trap: it
        selects an arbitrary member of the duplicate set and writes the
        lesser back. A POC caught that before it shipped.
        """
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        bdir = lr.boundary_dir(tmp_path)
        bdir.mkdir(parents=True, exist_ok=True)
        for stamp, ids in (("20260801T120000Z", ["dead-a", "live-b"]),
                           ("20260801T115952Z", ["live-b"])):
            (bdir / f"boundary-{stamp}.json").write_text(json.dumps({
                "version": lr.SNAPSHOT_VERSION,
                "boot_at": "2026-08-01T{}:{}:{}Z".format(
                    stamp[9:11], stamp[11:13], stamp[13:15]),
                "captured_at": "2026-08-01T12:00:00Z",
                "open_at_shutdown": [{"session_id": i} for i in ids],
            }), encoding="utf-8")

        lr.sweep_boundary(tmp_path, BOOT - timedelta(seconds=11))

        recs = self._records(tmp_path)
        assert len(recs) == 1, "duplicates were not collapsed"
        assert self._ids(recs[0]) == ["dead-a", "live-b"], (
            "the union took from only one covering record -- merging with "
            "an arbitrary member of a duplicate set preserves nothing")
        snap = lr.read_snapshot(tmp_path, boot_utc=BOOT)
        assert len(snap["open_at_shutdown"]) == 2, (
            "read_snapshot returned a covering record, but not the "
            "complete one")


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


class TestBoundaryKeyDriftAdversarial:
    """v0.9.13 tester dispatch: attack the append-only/repair-merge
    invariant beyond the sequences ``TestBoundaryKeyDrift`` already pins.
    Findings written up in
    ``private/claude/checklists/results/v0.9.13__boundary-keying__results__2026-08-19.md``.

    Every scenario here needs a boundary record that is ALREADY damaged
    in a way pure sweep_boundary() calls never produce on their own --
    a filename/``boot_at`` mismatch, a structurally malformed
    ``open_at_shutdown``, or an entry missing ``session_id``. That
    precondition is not hypothetical: the real machine's boundary
    history needed a HAND REPAIR on 2026-08-19 to merge three duplicate
    records back into one, and hand edits are exactly how a
    filename/content mismatch or a structural slip would be introduced.
    The repair-merge path's own docstring says it exists to tolerate
    "records already damaged in the wild" -- these tests probe whether
    it tolerates damage shapes *other* than the one it was built for.
    """

    def _boundary(self, tmp_path, filename, boot_at, open_at_shutdown):
        bdir = lr.boundary_dir(tmp_path)
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / filename).write_text(json.dumps({
            "version": lr.SNAPSHOT_VERSION, "boot_at": boot_at,
            "captured_at": boot_at,
            "open_at_shutdown": open_at_shutdown,
        }), encoding="utf-8")

    def _write_entry(self, tmp_path, sid, started):
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        lr.entry_path(tmp_path, sid).write_text(json.dumps({
            "session_id": sid, "started_at": started,
            "source": "startup", "cwd": "C:/w",
        }), encoding="utf-8")

    def test_mismatched_filename_record_is_healed_and_survives_retention(
            self, tmp_path):
        """Finding A, FIXED 2026-08-19 (strict xfail retired). Before the
        fix, this record -- extended to 9 names by the very same call --
        was deleted by retention in that call, because retention sorted
        candidates by FILENAME and this record's label lied about its
        era. Now retention orders by each record's own boot_at, the
        sweep never deletes a record it just extended, and the write
        HEALS the label: the surviving file is named by its content."""
        # A record whose filename claims a much older boundary than its
        # own boot_at content -- as a hand repair might produce.
        mismatched_name = "boundary-20250101T000000Z.json"
        self._boundary(tmp_path, mismatched_name, _iso(BOOT),
                       [{"session_id": f"real-session-{i}"}
                        for i in range(8)])
        # BOUNDARY_RETENTION other, self-consistently named records for
        # genuinely later boundaries -- an ordinary week of restarts.
        for i in range(lr.BOUNDARY_RETENTION):
            other_boot = BOOT + timedelta(days=i + 1)
            stamp = other_boot.strftime("%Y%m%dT%H%M%SZ")
            self._boundary(tmp_path, f"boundary-{stamp}.json",
                           _iso(other_boot), [{"session_id": f"other-{i}"}])
        # A pre-boot entry not yet folded into the mismatched record, so
        # THIS sweep has real work to do and must touch it.
        self._write_entry(tmp_path, "new-session-not-yet-recorded",
                          _iso(BOOT - timedelta(hours=1)))

        recorded = lr.sweep_boundary(tmp_path, BOOT)
        assert recorded == 9, "the merge itself computed the right count"

        after = {p.name for p in lr.boundary_dir(tmp_path).glob("*.json")}
        healed_name = "boundary-" + BOOT.strftime("%Y%m%dT%H%M%SZ") + ".json"
        assert healed_name in after, (
            "the record this call extended must survive retention, "
            "under a filename derived from its own boot_at")
        assert mismatched_name not in after, (
            "the lying label should be healed away, not left as a "
            "second copy")
        entries = json.loads(
            (lr.boundary_dir(tmp_path) / healed_name).read_text(
                encoding="utf-8"))["open_at_shutdown"]
        ids = {e["session_id"] for e in entries}
        assert ids == ({f"real-session-{i}" for i in range(8)}
                       | {"new-session-not-yet-recorded"}), (
            "all 9 names must be recoverable from the surviving record")

    def test_non_list_open_at_shutdown_does_not_silently_abort_the_sweep(
            self, tmp_path):
        """Finding B, FIXED 2026-08-19 (strict xfail retired). The merge
        once raised AttributeError on a malformed record, which the
        hook-context `except Exception: return 0` swallowed into a
        permanent silent no-op for that boundary. All readers now share
        `_snapshot_entries`, which never raises."""
        self._boundary(tmp_path, "boundary-20260801T120000Z.json",
                       _iso(BOOT), {"not": "a list"})
        self._write_entry(tmp_path, "new-real",
                          _iso(BOOT - timedelta(hours=1)))

        recorded = lr.sweep_boundary(tmp_path, BOOT)

        assert recorded > 0, (
            "sweep_boundary returned 0: the malformed covering record "
            "silently aborted the ENTIRE sweep, so 'new-real' -- a "
            "genuinely pre-boot session -- was never recorded anywhere")

    def test_two_entries_missing_session_id_are_not_collapsed(
            self, tmp_path):
        """Finding C, FIXED 2026-08-19 (strict xfail retired). The dedup
        set once keyed on `entry.get("session_id")`, collapsing two
        DISTINCT no-id entries onto a shared None. `_entry_key` now
        falls back to the entry's canonical content -- counted, never
        dropped, matching what read_entries does for unparseable files."""
        self._boundary(tmp_path, "boundary-20260801T120000Z.json",
                       _iso(BOOT), [{"cwd": "C:/x"}, {"cwd": "C:/y"},
                                    {"session_id": "named-c"}])
        self._write_entry(tmp_path, "new-real",
                          _iso(BOOT - timedelta(hours=1)))

        lr.sweep_boundary(tmp_path, BOOT)

        rec = sorted(lr.boundary_dir(tmp_path).glob("*.json"))[0]
        entries = json.loads(rec.read_text(encoding="utf-8"))[
            "open_at_shutdown"]
        no_id_count = sum(1 for e in entries if e.get("session_id") is None)
        assert no_id_count == 2, (
            f"expected both session_id-less entries to survive the "
            f"merge, only {no_id_count} did: {entries}")

    def test_largest_of_three_duplicates_not_first_by_filename_still_unions(
            self, tmp_path):
        """A shape ``test_existing_duplicates_converge`` (2 records) does
        not cover: 3+ covering records where the LARGEST is neither
        first nor last by filename. The union must not depend on which
        position holds the most names."""
        self._boundary(tmp_path, "boundary-20260801T120000Z.json",
                       "2026-08-01T12:00:00Z", [{"session_id": "a"}])
        self._boundary(tmp_path, "boundary-20260801T120020Z.json",
                       "2026-08-01T12:00:20Z",
                       [{"session_id": s} for s in "abcde"])
        self._boundary(tmp_path, "boundary-20260801T120040Z.json",
                       "2026-08-01T12:00:40Z",
                       [{"session_id": "a"}, {"session_id": "f"}])
        self._write_entry(tmp_path, "extra-new",
                          _iso(BOOT - timedelta(hours=1)))

        lr.sweep_boundary(tmp_path, BOOT)

        recs = sorted(lr.boundary_dir(tmp_path).glob("boundary-*.json"))
        assert len(recs) == 1, "duplicates were not collapsed"
        assert recs[0].name == "boundary-20260801T120000Z.json", (
            "the earliest filename should still name the survivor")
        ids = {e["session_id"] for e in
              json.loads(recs[0].read_text(encoding="utf-8"))[
                  "open_at_shutdown"]}
        assert ids == {"a", "b", "c", "d", "e", "f", "extra-new"}, (
            "the union lost a name from the record that was largest but "
            "not first in filename order")

    def test_forward_drift_does_not_mint_a_second_record(self, tmp_path):
        """The pinned drift tests only exercise BACKWARD drift (a boot
        time computed as later than the true instant, minus seconds).
        `current_boot_utc()` derives from now-minus-uptime, which can
        drift either direction depending on tick-counter behaviour."""
        self._write_entry(tmp_path, "dead-a",
                          _iso(BOOT - timedelta(days=1)))
        lr.sweep_boundary(tmp_path, BOOT)
        lr.sweep_boundary(tmp_path, BOOT + timedelta(seconds=8))
        recs = sorted(lr.boundary_dir(tmp_path).glob("boundary-*.json"))
        assert len(recs) == 1, "forward drift minted a duplicate record"

    def test_single_step_jump_past_tolerance_loses_nothing_within_records(
            self, tmp_path):
        """A single hop bigger than tolerance_s (601s) between sweeps of
        what is, in reality, the same boot -- plausible after a
        sleep/hibernate tick-counter glitch (an explicitly untested
        condition per the checklist's own gap list). By design,
        tolerance_s is a hard cutoff: two records are minted. This pins
        that the two-record split does not ALSO lose names within
        either record -- only that boundary-identity, not evidence, is
        what a >600s jump costs."""
        self._write_entry(tmp_path, "dead-a",
                          _iso(BOOT - timedelta(days=1)))
        self._write_entry(tmp_path, "live-b",
                          _iso(BOOT - timedelta(days=1)))
        lr.sweep_boundary(tmp_path, BOOT)
        lr.sweep_boundary(tmp_path, BOOT + timedelta(seconds=601))
        recs = sorted(lr.boundary_dir(tmp_path).glob("boundary-*.json"))
        assert len(recs) == 2, (
            "a >600s single-step jump should mint a second record "
            "(tolerance_s is a documented hard cutoff)")


class TestRetentionContentOrdering:
    """v0.9.13 Finding A: retention must order records by their own
    ``boot_at`` (the fact), not their filename (the label). Build order
    per the vault note: the well-formed regression pin lands FIRST and
    passes on the pre-change code, so the comparator change is proven
    to leave healthy machines' eviction untouched."""

    def _boundary(self, tmp_path, filename, boot_at, names):
        bdir = lr.boundary_dir(tmp_path)
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / filename).write_text(json.dumps({
            "version": lr.SNAPSHOT_VERSION, "boot_at": boot_at,
            "captured_at": boot_at,
            "open_at_shutdown": [{"session_id": s} for s in names],
        }), encoding="utf-8")

    def _consistent(self, tmp_path, boot, names):
        stamp = boot.strftime("%Y%m%dT%H%M%SZ")
        self._boundary(tmp_path, f"boundary-{stamp}.json", _iso(boot),
                       names)

    def _write_entry(self, tmp_path, sid, started):
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        lr.entry_path(tmp_path, sid).write_text(json.dumps({
            "session_id": sid, "started_at": started,
            "source": "startup", "cwd": "C:/w",
        }), encoding="utf-8")

    def test_well_formed_eviction_is_unchanged_by_the_comparator(
            self, tmp_path):
        """REGRESSION PIN (passes before AND after the ordering change):
        with K existing self-consistent records plus the record this
        sweep writes, the genuinely oldest record -- which on healthy
        data is ALSO the filename-oldest -- is the one evicted. On
        well-formed data filename order and boot_at order agree, so the
        comparator change must not move eviction on any healthy machine."""
        for i in range(lr.BOUNDARY_RETENTION):
            boot = BOOT - timedelta(days=lr.BOUNDARY_RETENTION - i)
            self._consistent(tmp_path, boot, [f"old-{i}"])
        self._write_entry(tmp_path, "dead-now",
                          _iso(BOOT - timedelta(hours=1)))

        lr.sweep_boundary(tmp_path, BOOT)

        names = {p.name
                 for p in lr.boundary_dir(tmp_path).glob("boundary-*.json")}
        oldest = (BOOT - timedelta(days=lr.BOUNDARY_RETENTION)).strftime(
            "%Y%m%dT%H%M%SZ")
        newest_kept = (BOOT - timedelta(days=1)).strftime("%Y%m%dT%H%M%SZ")
        boot_stamp = BOOT.strftime("%Y%m%dT%H%M%SZ")
        assert f"boundary-{oldest}.json" not in names, (
            "the genuinely oldest record should be the one evicted")
        assert f"boundary-{newest_kept}.json" in names
        assert f"boundary-{boot_stamp}.json" in names, (
            "the record for the boundary just swept must survive")
        assert len(names) == lr.BOUNDARY_RETENTION

    def test_content_order_beats_filename_order_for_eviction(self, tmp_path):
        """ANCHOR (Finding A's root, pinned directly): when a record's
        filename claims an ancient era but its content is recent,
        eviction must follow the content. Pre-fix, the filename-oldest
        record (the liar) was evicted despite holding the newest
        evidence of the five."""
        liar_name = "boundary-20250101T000000Z.json"
        self._boundary(tmp_path, liar_name,
                       _iso(BOOT - timedelta(days=1)), ["liar-content"])
        for i in range(2, 6):  # BOOT-2d .. BOOT-5d, self-consistent
            self._consistent(tmp_path, BOOT - timedelta(days=i),
                             [f"honest-{i}"])
        self._write_entry(tmp_path, "dead-now",
                          _iso(BOOT - timedelta(hours=1)))

        lr.sweep_boundary(tmp_path, BOOT)

        names = {p.name
                 for p in lr.boundary_dir(tmp_path).glob("boundary-*.json")}
        oldest_content = (BOOT - timedelta(days=5)).strftime(
            "%Y%m%dT%H%M%SZ")
        assert liar_name in names, (
            "the liar holds the newest content of the five and must be "
            "kept; evicting it means ordering followed the label")
        assert f"boundary-{oldest_content}.json" not in names, (
            "the genuinely oldest content should be the eviction")

    def test_unparseable_boot_at_falls_back_to_the_filename_stamp(
            self, tmp_path):
        """AC-7: a record whose content boot_at is garbage but whose
        filename stamp parses is ordered by the stamp -- evidence with
        a locatable label still ranks."""
        stamp = (BOOT - timedelta(days=1)).strftime("%Y%m%dT%H%M%SZ")
        self._boundary(tmp_path, f"boundary-{stamp}.json",
                       "not-a-date", ["fallback-record"])
        for i in range(2, 6):
            self._consistent(tmp_path, BOOT - timedelta(days=i),
                             [f"honest-{i}"])
        self._write_entry(tmp_path, "dead-now",
                          _iso(BOOT - timedelta(hours=1)))

        lr.sweep_boundary(tmp_path, BOOT)

        names = {p.name
                 for p in lr.boundary_dir(tmp_path).glob("boundary-*.json")}
        assert f"boundary-{stamp}.json" in names, (
            "content-newest of the five (via its filename stamp) -- "
            "must be kept")
        oldest = (BOOT - timedelta(days=5)).strftime("%Y%m%dT%H%M%SZ")
        assert f"boundary-{oldest}.json" not in names

    def test_record_with_no_locatable_instant_classifies_with_empties(
            self, tmp_path):
        """AC-7: names but NO parseable instant (content garbage AND a
        stampless filename) -- evidence that cannot be placed cannot be
        ranked; it fills leftover slots only. Pre-fix this file sorted
        LAST by filename (letters sort after digits) and was kept as
        'newest', evicting a real record."""
        self._boundary(tmp_path, "boundary-oops.json",
                       "not-a-date", ["unplaceable"])
        for i in range(1, 6):
            self._consistent(tmp_path, BOOT - timedelta(days=i),
                             [f"honest-{i}"])
        self._write_entry(tmp_path, "dead-now",
                          _iso(BOOT - timedelta(hours=1)))

        lr.sweep_boundary(tmp_path, BOOT)

        names = {p.name
                 for p in lr.boundary_dir(tmp_path).glob("boundary-*.json")}
        assert "boundary-oops.json" not in names, (
            "with the K slots full of placeable evidence, the "
            "unplaceable record is the one that goes")
        assert len(names) == lr.BOUNDARY_RETENTION


class TestMalformedRecordTolerance:
    """v0.9.13 Findings B/C follow-through: every reader of boundary
    records shares one tolerance policy (`_snapshot_entries`), the
    dedup key is total (`_entry_key`), and the write path heals a
    record whose filename lies about its content."""

    def _boundary(self, tmp_path, filename, boot_at, open_at_shutdown):
        bdir = lr.boundary_dir(tmp_path)
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / filename).write_text(json.dumps({
            "version": lr.SNAPSHOT_VERSION, "boot_at": boot_at,
            "captured_at": boot_at,
            "open_at_shutdown": open_at_shutdown,
        }), encoding="utf-8")

    def _write_entry(self, tmp_path, sid, started):
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        lr.entry_path(tmp_path, sid).write_text(json.dumps({
            "session_id": sid, "started_at": started,
            "source": "startup", "cwd": "C:/w",
        }), encoding="utf-8")

    def test_bare_list_boundary_file_does_not_abort_the_sweep(
            self, tmp_path):
        """ANCHOR (GT6, found in recon -- same class as Finding B one
        layer down): a boundary FILE whose JSON top level is a bare
        list raised AttributeError inside covering_records itself,
        which the hook-context handler swallowed into a silent abort."""
        bdir = lr.boundary_dir(tmp_path)
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = BOOT.strftime("%Y%m%dT%H%M%SZ")
        (bdir / f"boundary-{stamp}.json").write_text(
            json.dumps([1, 2, 3]), encoding="utf-8")
        self._write_entry(tmp_path, "new-real",
                          _iso(BOOT - timedelta(hours=1)))

        recorded = lr.sweep_boundary(tmp_path, BOOT)

        assert recorded == 1, (
            "the sweep must record the pre-boot session despite the "
            "malformed file sitting at the boundary's own filename")

    def test_malformed_covering_record_is_rewritten_once_then_noops(
            self, tmp_path):
        """AC-5: the healing write happens ONCE; after it the record is
        complete and the no-op guarantee (HV.3) takes over."""
        stamp = BOOT.strftime("%Y%m%dT%H%M%SZ")
        self._boundary(tmp_path, f"boundary-{stamp}.json",
                       _iso(BOOT), {"not": "a list"})
        self._write_entry(tmp_path, "new-real",
                          _iso(BOOT - timedelta(hours=1)))

        lr.sweep_boundary(tmp_path, BOOT)
        bpath = lr.boundary_dir(tmp_path) / f"boundary-{stamp}.json"
        first = bpath.read_bytes()
        entries = json.loads(first.decode("utf-8"))["open_at_shutdown"]
        assert isinstance(entries, list) and len(entries) == 1, (
            "the malformed field is rewritten as a well-formed list "
            "holding the recovered evidence")
        first_mtime = bpath.stat().st_mtime_ns

        lr.sweep_boundary(tmp_path, BOOT)
        assert bpath.read_bytes() == first
        assert bpath.stat().st_mtime_ns == first_mtime, (
            "a record healed once must not be rewritten again")

    def test_identical_no_id_entries_across_duplicates_still_dedupe(
            self, tmp_path):
        """AC-6, the other half of Finding C: distinct no-id entries
        both survive, but COPIES of one entry across two covering
        duplicates must still collapse to one."""
        s1 = BOOT.strftime("%Y%m%dT%H%M%SZ")
        s2 = (BOOT + timedelta(seconds=20)).strftime("%Y%m%dT%H%M%SZ")
        self._boundary(tmp_path, f"boundary-{s1}.json", _iso(BOOT),
                       [{"cwd": "C:/dup"}])
        self._boundary(tmp_path, f"boundary-{s2}.json",
                       _iso(BOOT + timedelta(seconds=20)),
                       [{"cwd": "C:/dup"}, {"cwd": "C:/uniq"}])
        self._write_entry(tmp_path, "new-real",
                          _iso(BOOT - timedelta(hours=1)))

        lr.sweep_boundary(tmp_path, BOOT)

        recs = sorted(lr.boundary_dir(tmp_path).glob("boundary-*.json"))
        assert len(recs) == 1, "covering duplicates must collapse"
        entries = json.loads(recs[0].read_text(encoding="utf-8"))[
            "open_at_shutdown"]
        no_id = [e for e in entries if e.get("session_id") is None]
        assert len(no_id) == 2, (
            f"expected the duplicate copy deduped and the distinct one "
            f"kept: {entries}")

    def test_heal_write_renames_a_lying_label_to_match_content(
            self, tmp_path):
        """AC-4, no retention pressure: after a sweep touches a
        mismatched covering record, exactly one record remains for the
        boundary, named by its own boot_at, holding every name."""
        self._boundary(tmp_path, "boundary-20250101T000000Z.json",
                       _iso(BOOT), [{"session_id": "a"}])
        self._write_entry(tmp_path, "new-real",
                          _iso(BOOT - timedelta(hours=1)))

        lr.sweep_boundary(tmp_path, BOOT)

        recs = sorted(lr.boundary_dir(tmp_path).glob("boundary-*.json"))
        assert len(recs) == 1
        healed = "boundary-" + BOOT.strftime("%Y%m%dT%H%M%SZ") + ".json"
        assert recs[0].name == healed
        data = json.loads(recs[0].read_text(encoding="utf-8"))
        assert data["boot_at"] == _iso(BOOT), (
            "filename and content must agree after the heal")
        ids = {e["session_id"] for e in data["open_at_shutdown"]}
        assert ids == {"a", "new-real"}

    # The five tests below each kill a survivor from the v0.9.13
    # mutation sweep (tests/mutation/v0.9.13__sweep__live-registry-bca.md)
    # -- written under the close-the-loop rule: a killable survivor's
    # killing test is written NOW, not filed for later.

    def test_corrupt_record_at_another_name_does_not_abort_the_sweep(
            self, tmp_path):
        """Kills M1 (_snapshot_entries non-dict guard). The bare-list
        test cannot: its malformed file sits at the CURRENT boundary's
        own filename, so the sweep overwrites it before retention reads
        it. Here the damage lives at a DIFFERENT boundary's name, so
        retention must read it while still malformed -- and tolerate
        both unparseable JSON (data=None) and a bare list."""
        bdir = lr.boundary_dir(tmp_path)
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "boundary-20250601T000000Z.json").write_text(
            "{not valid json", encoding="utf-8")
        (bdir / "boundary-20250602T000000Z.json").write_text(
            json.dumps([1, 2, 3]), encoding="utf-8")
        self._write_entry(tmp_path, "new-real",
                          _iso(BOOT - timedelta(hours=1)))

        recorded = lr.sweep_boundary(tmp_path, BOOT)

        assert recorded == 1, (
            "malformed records at OTHER boundaries' names must not "
            "abort recording for this one")

    def test_adoption_follows_content_order_not_filename_order(
            self, tmp_path):
        """Kills M4 (covering_records sort key). Two records cover one
        boundary and their filename order INVERTS their content order:
        the early-named file holds the newer boot_at. The adopted
        boot_at -- and therefore the healed filename -- must come from
        the oldest CONTENT, not the oldest label."""
        newer = BOOT + timedelta(seconds=20)
        # Early filename, NEWER content.
        self._boundary(tmp_path, "boundary-20250101T000000Z.json",
                       _iso(newer), [{"session_id": "a"}])
        # Late filename, OLDER content (the true first record).
        self._boundary(tmp_path, "boundary-20991231T000000Z.json",
                       _iso(BOOT), [{"session_id": "b"}])
        self._write_entry(tmp_path, "new-real",
                          _iso(BOOT - timedelta(hours=1)))

        lr.sweep_boundary(tmp_path, BOOT)

        recs = sorted(lr.boundary_dir(tmp_path).glob("boundary-*.json"))
        assert len(recs) == 1, "covering duplicates must collapse"
        data = json.loads(recs[0].read_text(encoding="utf-8"))
        assert data["boot_at"] == _iso(BOOT), (
            "the oldest CONTENT names the boundary; filename order "
            "picked the lying early label instead")
        assert recs[0].name == (
            "boundary-" + BOOT.strftime("%Y%m%dT%H%M%SZ") + ".json")

    def test_unparseable_started_at_is_never_swept(self, tmp_path):
        """Kills M5 (pre_boot condition flip). An entry whose
        started_at cannot be parsed proves nothing about the boundary;
        sweeping it would record AND DELETE a session that may be open
        right now. Erring toward this_boot is the documented rule."""
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        lr.entry_path(tmp_path, "garbled").write_text(json.dumps({
            "session_id": "garbled", "started_at": "not-a-timestamp",
            "source": "startup", "cwd": "C:/w",
        }), encoding="utf-8")

        recorded = lr.sweep_boundary(tmp_path, BOOT)

        assert recorded == 0, (
            "an unplaceable entry must not be recorded as pre-boot")
        assert lr.entry_path(tmp_path, "garbled").exists(), (
            "and above all its registration must not be deleted")

    def test_complete_but_mislabelled_record_still_gets_its_one_heal(
            self, tmp_path):
        """Kills M8 (no-op check's filename-agreement condition). A
        mismatched-name record whose CONTENT is already complete -- no
        new entries, alias in agreement -- must not short-circuit as a
        no-op: it gets exactly one healing rewrite, then no-ops."""
        self._boundary(tmp_path, "boundary-20250101T000000Z.json",
                       _iso(BOOT), [{"session_id": "a"}])
        # Alias agrees with the record, so ONLY the lying filename
        # distinguishes "complete" from "needs one heal".
        lr.snapshot_path(tmp_path).write_text(json.dumps({
            "version": lr.SNAPSHOT_VERSION, "boot_at": _iso(BOOT),
            "captured_at": _iso(BOOT),
            "open_at_shutdown": [{"session_id": "a"}],
        }), encoding="utf-8")

        lr.sweep_boundary(tmp_path, BOOT)

        healed = (lr.boundary_dir(tmp_path)
                  / ("boundary-" + BOOT.strftime("%Y%m%dT%H%M%SZ")
                     + ".json"))
        assert healed.exists(), (
            "the lying label was treated as complete and never healed")
        assert not (lr.boundary_dir(tmp_path)
                    / "boundary-20250101T000000Z.json").exists()
        # And healing happens ONCE: the next sweep is a byte-level no-op.
        before = healed.read_bytes()
        mtime = healed.stat().st_mtime_ns
        lr.sweep_boundary(tmp_path, BOOT)
        assert healed.read_bytes() == before
        assert healed.stat().st_mtime_ns == mtime

    def test_claude_less_reboots_credit_the_oldest_newer_snapshot(
            self, tmp_path):
        """Kills M12 (min -> max in read_snapshot's shutdown_utc pass).
        Two snapshots postdate the epoch's shutdown; the OLDEST is the
        one whose sweep observed that shutdown's leftovers. Picking the
        newest misattributes the testimony -- here it returns None
        because the newer snapshot names a session started later."""
        shutdown = BOOT - timedelta(days=1)
        bdir = lr.boundary_dir(tmp_path)
        bdir.mkdir(parents=True, exist_ok=True)
        first_boot = shutdown + timedelta(hours=1)
        second_boot = shutdown + timedelta(hours=2)
        for boot, sid, started in (
                (first_boot, "old-session", shutdown - timedelta(hours=3)),
                (second_boot, "later-session",
                 shutdown + timedelta(minutes=90))):
            stamp = boot.strftime("%Y%m%dT%H%M%SZ")
            (bdir / f"boundary-{stamp}.json").write_text(json.dumps({
                "version": lr.SNAPSHOT_VERSION, "boot_at": _iso(boot),
                "captured_at": _iso(boot),
                "open_at_shutdown": [{"session_id": sid,
                                      "started_at": _iso(started)}],
            }), encoding="utf-8")

        snap = lr.read_snapshot(tmp_path, shutdown_utc=shutdown)

        assert snap is not None, (
            "the oldest-newer snapshot qualifies and must be returned")
        assert snap["boot_at"] == _iso(first_boot), (
            "the OLDEST snapshot newer than the shutdown carries that "
            "epoch's testimony")

"""Adversarial edge-case probes for the v0.9.12 boundary-sweep/liveness fix.

Dispatched as an explicit exploration list rather than discovered ad hoc,
so each class below is headed with the scenario it probes -- alongside the
checklist automation, and beyond what
``tests/checklists/v0.9.12__Fix__boundary-sweep-liveness-and-resumed-sessions.md``
and its companion tests already cover. Findings are written up in
``tests/checklists/results/v0.9.12__boundary-sweep__results__2026-08-17.md``.

Two of them found real defects **in the fix itself**, not in old code:
an entry claiming liveness with no host recorded, and a corrupt liveness
stamp overriding a good identity stamp. Both were fixed before shipping,
and the tests here assert the CORRECTED property -- a test written
against a defect fails the moment the defect is fixed and reads like a
broken fix, which is the trap the v0.9.10 sweep already walked into once.
See ``TestPidAtWithoutPid`` and ``TestPidAtEarlierThanStartedAt``.
"""

import json
import threading
import time
from datetime import datetime, timezone

import pytest

import claude_session_backup.live_registry as lr
import claude_session_backup.liveness as lv

BOOT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def _write(claude_dir, sid, **fields):
    lr.live_dir(claude_dir).mkdir(parents=True, exist_ok=True)
    payload = {"session_id": sid}
    payload.update(fields)
    lr.entry_path(claude_dir, sid).write_text(json.dumps(payload),
                                              encoding="utf-8")


class TestPidAtWithoutPid:
    """Edge case 1a -- ``pid_at`` present, ``pid`` absent.

    Cannot arise from the hook's own write path (``record_session_start``
    always pairs them), but IS reachable from a corrupted file or an
    entry synced in from another machine via the backup store's git
    history -- the registry's own docstring names exactly that path
    ("an entry's session id... can arrive from another machine").

    Originally reported as REVIEW: ``seen_alive_after_boot`` trusted
    ``pid_at`` alone, so the sweep KEPT an entry that
    ``liveness.verify_entry`` could never mark ``[running]`` -- there is
    no ``pid`` to verify against. The two gates disagreed, and the entry
    lingered for one boundary cycle before the next restart cleared it.

    FIXED in v0.9.12: the retention gate now requires a ``pid`` as well.
    "csb saw the host alive at T" is not evidence without a WHICH, and
    an entry no verifier can ever settle must not buy itself a reprieve.
    The one-cycle bound made the old behaviour tolerable, not correct.
    """

    def test_is_swept_because_the_claim_is_unverifiable(self, tmp_path):
        """FIXED in v0.9.12 (this test previously characterised the
        defect). ``pid_at`` with no ``pid`` says "csb saw the host alive
        at T" without saying WHICH host -- nothing can ever confirm it,
        so it must not buy the entry a reprieve from the sweep. Asserting
        the CORRECT property, not the broken mechanism: a test written
        against a defect fails the moment the defect is fixed, and reads
        like a broken fix."""
        _write(tmp_path, "ghost-1",
              started_at="2026-07-30T10:00:00Z",  # pre-boot by identity
              source="resume", cwd="C:/w",
              pid_at="2026-08-01T12:27:00Z")       # post-boot, NO pid
        recorded = lr.sweep_boundary(tmp_path, BOOT)
        assert recorded == 1, "still recorded as open-at-shutdown evidence"
        assert not lr.entry_path(tmp_path, "ghost-1").exists(), (
            "an entry whose liveness claim no verifier can settle must "
            "not survive the sweep -- the two gates would disagree "
            "forever, which is the graveyard clearing exists to prevent")

    def test_but_never_verifies_as_running(self, tmp_path):
        """The display-layer gate correctly refuses to trust it -- the
        entry surviving the sweep does NOT mean `set current` shows it
        as `[running]`; it always reads unverified/no-exit-observed."""
        _write(tmp_path, "ghost-1",
              started_at="2026-07-30T10:00:00Z", source="resume",
              cwd="C:/w", pid_at="2026-08-01T12:27:00Z")
        entries = lr.read_entries(tmp_path)
        scan = lv.LiveScan(ok=True)  # no live processes at all
        assert lv.verify_entry(scan, entries[0], None) is None

    def test_cleared_on_the_first_cycle_not_the_second(self, tmp_path):
        """Before the fix this entry lingered for exactly one boundary
        cycle and self-corrected on the next restart. That bound made it
        tolerable, not correct -- and "tolerable" is not the standard for
        a board whose whole meaning is "these are open right now". It is
        now cleared on the FIRST sweep, so no second restart is needed."""
        _write(tmp_path, "ghost-1",
              started_at="2026-07-30T10:00:00Z", source="resume",
              cwd="C:/w", pid_at="2026-08-01T12:27:00Z")
        lr.sweep_boundary(tmp_path, BOOT)
        assert not lr.entry_path(tmp_path, "ghost-1").exists()

        boot2 = datetime(2026, 8, 5, 0, 0, 0, tzinfo=timezone.utc)
        lr.sweep_boundary(tmp_path, boot2)   # idempotent; nothing to do
        assert not lr.entry_path(tmp_path, "ghost-1").exists()


class TestPidWithoutPidAt:
    """Edge case 1b -- ``pid`` present, ``pid_at`` absent (the reverse).

    This IS a normal, reachable shape: any pre-#72 entry written by an
    older plugin. Falls back cleanly to ``started_at``-only
    classification -- the pre-existing behaviour, unchanged. No finding;
    included for completeness/symmetry with 1a."""

    def test_falls_back_to_started_at_classification(self, tmp_path):
        _write(tmp_path, "old-style", started_at="2026-07-30T10:00:00Z",
              source="resume", cwd="C:/w", pid=4242)  # no pid_at
        assert lr.seen_alive_after_boot(
            lr.read_entries(tmp_path)[0], BOOT) is False
        assert lr.sweep_boundary(tmp_path, BOOT) == 1
        assert not lr.entry_path(tmp_path, "old-style").exists()


class TestTimestampParsing:
    """Edge case 2 -- timezone-naive and offset-bearing timestamps in
    ``started_at``/``pid_at``. All handled correctly: naive assumed UTC
    (matches the hook's own ``_iso()`` writer, which always emits ``Z``),
    offset-bearing timestamps convert to the correct UTC instant, and
    garbage/empty/None all degrade to ``None`` without raising. No
    finding -- documented as a pinning regression test."""

    @pytest.mark.parametrize("raw,expected_hour", [
        ("2026-08-01T12:27:00Z", 12),
        ("2026-08-01T12:27:00", 12),           # naive -> assumed UTC
        ("2026-08-01T20:27:00+08:00", 12),      # offset -> converts to UTC
        ("2026-08-01T05:27:00-07:00", 12),      # negative offset
        ("2026-08-01T12:27:00.123456", 12),     # naive with microseconds
    ])
    def test_parses_to_the_correct_utc_instant(self, raw, expected_hour):
        parsed = lr.parse_entry_ts(raw)
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.astimezone(timezone.utc).hour == expected_hour

    @pytest.mark.parametrize("raw", ["", None, "not-a-timestamp", "   "])
    def test_garbage_degrades_to_none_without_raising(self, raw):
        assert lr.parse_entry_ts(raw) is None

    def test_sweep_tolerates_a_mixed_naive_and_offset_registry(
            self, tmp_path):
        """An entry using a naive timestamp and one using an explicit
        offset both classify correctly in the same sweep -- no crash,
        no silent misclassification of one relative to the other."""
        _write(tmp_path, "naive-1", started_at="2026-07-30T10:00:00",
              source="startup", cwd="C:/w")  # no tzinfo
        _write(tmp_path, "offset-1", started_at="2026-07-30T18:00:00+08:00",
              source="startup", cwd="C:/w")  # == 10:00 UTC
        recorded = lr.sweep_boundary(tmp_path, BOOT)
        assert recorded == 2
        assert not lr.entry_path(tmp_path, "naive-1").exists()
        assert not lr.entry_path(tmp_path, "offset-1").exists()


class TestPidAtEarlierThanStartedAt:
    """Edge case 3 -- ``pid_at`` earlier than ``started_at`` (nonsensical:
    liveness "proof" predating the session's own claimed birth), when
    ``started_at`` is itself post-boot.

    The sweep itself is unaffected (a post-boot ``started_at`` is never a
    sweep candidate regardless of ``pid_at`` -- confirmed below).

    The consequence was downstream, in ``liveness.verify_entry``'s
    pid-reuse guard: the anchor preferred ``pid_at`` over ``started_at``
    unconditionally. A ``pid_at`` corrupted to predate the real host's
    creation time by more than the 60s skew tolerance therefore rejected
    a COMPLETELY LEGITIMATE host (created a normal handful of seconds
    after its own session's ``started_at``) as "pid reused" -- reporting
    `[no exit observed]` for a session that was, in fact, running.

    FIXED in v0.9.12: the anchor is the LATER of the two stamps. A
    ``pid_at`` cannot legitimately precede ``started_at`` -- a pid is
    recorded at or after the session opens -- so the earlier stamp is
    the untrustworthy one, and a false "not running" is the worse error
    here: it hides a live session and disarms the guard that stops a
    second client joining one transcript.

    Unlike the pid_at-without-pid case, this does NOT self-correct on
    the next boundary (started_at was never pre-boot, so no sweep ever
    touches it) -- and the hook's own pid-refresh path
    (``record_session_start``) only re-stamps ``pid_at`` when the PID
    CHANGES, not on a same-pid re-observation, so a corrupted pid_at
    paired with an unchanged pid can persist for the life of the
    session. Reported as REVIEW (the precondition -- a backdated
    pid_at -- is not reachable from the hook's own write path either),
    with a concrete reproduction for the maintainer's judgment on
    whether the anchor should clamp to ``max(pid_at, started_at)``.
    """

    def test_sweep_ignores_it_started_at_is_post_boot(self, tmp_path):
        _write(tmp_path, "backwards-1", started_at="2026-08-01T12:10:00Z",
              source="startup", cwd="C:/w", pid=555,
              pid_at="2026-08-01T12:05:00Z")  # earlier than started_at
        recorded = lr.sweep_boundary(tmp_path, BOOT)
        assert recorded == 0
        assert lr.entry_path(tmp_path, "backwards-1").exists()

    def test_a_legitimate_host_is_not_rejected_as_pid_reuse(self, tmp_path):
        """FIXED in v0.9.12 (this test previously characterised the
        defect). A host created a normal 30s after its own session's
        started_at is by any reasonable measure the correct host, and a
        pid_at corrupted to predate started_at must not veto it: pid_at
        cannot legitimately precede started_at, so the earlier stamp is
        the untrustworthy one. The anchor is now the LATER of the two,
        because a false "not running" hides a live session -- the exact
        failure the pid ladder exists to end."""
        entry = {
            "session_id": "backwards-1",
            "started_at": "2026-08-01T12:10:00Z",
            "pid": 555,
            "pid_at": "2026-08-01T12:05:00Z",  # 5 min before started_at
        }
        real_creation = datetime(2026, 8, 1, 12, 10, 30,
                                 tzinfo=timezone.utc)  # 30s after started_at
        scan = lv.LiveScan(by_pid={
            555: lv.ProcInfo(cmdline="claude", created=real_creation)})
        assert lv.verify_entry(scan, entry, None) == 555

    def test_the_same_host_verifies_fine_without_the_corruption(
            self, tmp_path):
        """Control: with pid_at >= started_at (the normal shape), the
        identical real_creation instant verifies correctly -- isolates
        the corruption as the cause, not the 30s gap itself."""
        entry = {
            "session_id": "normal-1",
            "started_at": "2026-08-01T12:10:00Z",
            "pid": 555,
            "pid_at": "2026-08-01T12:10:05Z",  # normal: shortly AFTER
        }
        real_creation = datetime(2026, 8, 1, 12, 10, 30,
                                 tzinfo=timezone.utc)
        scan = lv.LiveScan(by_pid={
            555: lv.ProcInfo(cmdline="claude", created=real_creation)})
        assert lv.verify_entry(scan, entry, None) == 555


class TestBoundaryRecordOpenAtShutdownNotAList:
    """Edge case 4 -- a boundary record whose JSON is valid but whose
    ``open_at_shutdown`` is not a list (dict, string, null). Handled by
    the explicit ``isinstance(..., list)`` guard in ``read_snapshot`` --
    treated as no-valid-snapshot (None), which correctly propagates to
    "UNKNOWN, not none" rather than crashing or misreading. No finding."""

    @pytest.mark.parametrize("bad_value", [
        {"not": "a list"}, "not-even-a-container", None, 42, True,
    ])
    def test_malformed_open_at_shutdown_reads_as_no_snapshot(
            self, tmp_path, bad_value):
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        lr.snapshot_path(tmp_path).write_text(json.dumps({
            "version": 1, "boot_at": "2026-08-01T12:00:00Z",
            "captured_at": "2026-08-01T12:00:05Z",
            "open_at_shutdown": bad_value,
        }), encoding="utf-8")
        assert lr.read_snapshot(tmp_path) is None
        assert lr.open_at_shutdown(tmp_path, boot_utc=BOOT) is None

    def test_malformed_retained_boundary_record_same_treatment(
            self, tmp_path):
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        bdir = lr.boundary_dir(tmp_path)
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "boundary-20260801T120000Z.json").write_text(json.dumps({
            "version": 1, "boot_at": "2026-08-01T12:00:00Z",
            "captured_at": "2026-08-01T12:00:05Z",
            "open_at_shutdown": "not-a-list",
        }), encoding="utf-8")
        assert lr.read_snapshot(tmp_path, boot_utc=BOOT) is None


class TestConcurrentSweeps:
    """Edge case 5 -- multiple sweeps racing against one scratch registry
    at the same boot instant. Exercised with real OS threads (not just
    sequential calls) so genuine filesystem interleaving is possible.

    No corruption, no lost entries, no exceptions observed across 5
    trials x 8 threads x 20 pre-boot entries in manual probing. This
    test runs a smaller version (still real threads) to keep suite
    runtime reasonable while still exercising the race. No finding --
    ``os.replace`` is atomic on both platforms and the deletion loop's
    ``except OSError: pass`` absorbs the expected double-unlink race."""

    def test_racing_sweeps_do_not_corrupt_or_lose_entries(self, tmp_path):
        n_entries = 15
        for i in range(n_entries):
            _write(tmp_path, f"race-{i:03d}",
                  started_at="2026-07-30T10:00:00Z",
                  source="startup", cwd="C:/w")

        results = []
        errors = []

        def worker():
            try:
                results.append(lr.sweep_boundary(tmp_path, BOOT))
            except Exception as exc:  # noqa: BLE001 -- test wants to see it
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"sweep_boundary raised under concurrency: {errors}"
        remaining = [p for p in lr.live_dir(tmp_path).glob("*.json")
                    if p.name != lr.SNAPSHOT_FILENAME]
        assert remaining == [], (
            f"entries survived concurrent sweeps: {[p.name for p in remaining]}")
        snap = lr.read_snapshot(tmp_path)
        assert snap is not None
        assert isinstance(snap["open_at_shutdown"], list)
        assert len(snap["open_at_shutdown"]) == n_entries
        # No leftover atomic-write temp files from a torn concurrent write.
        leftovers = list(lr.live_dir(tmp_path).glob("*.tmp-*")) + \
            list(lr.boundary_dir(tmp_path).glob("*.tmp-*"))
        assert leftovers == []


class TestOpenAtShutdownBootUtcNoneAndEmptyBoundariesDir:
    """Edge case 6 -- ``open_at_shutdown()`` with ``boot_utc=None``, and
    with an EXISTING-but-EMPTY ``boundaries/`` directory. No finding."""

    def test_boot_utc_none_is_unknown_not_derived(self, tmp_path):
        _write(tmp_path, "x", started_at="2026-07-30T10:00:00Z")
        assert lr.open_at_shutdown(tmp_path, boot_utc=None) is None

    def test_empty_boundaries_dir_still_derives_from_live_entries(
            self, tmp_path, monkeypatch):
        """boundaries/ existing-but-empty must behave identically to
        boundaries/ not existing at all: fall through to deriving from
        the live registry, not silently treated as a proven-empty
        snapshot."""
        monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
        lr.boundary_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        _write(tmp_path, "y", started_at="2026-07-30T10:00:00Z")

        assert lr.read_snapshot(tmp_path, boot_utc=BOOT) is None
        derived = lr.open_at_shutdown(tmp_path, boot_utc=BOOT)
        assert derived is not None
        assert [e["session_id"] for e in derived] == ["y"]


class TestFilenameSessionIdMismatchDefeatsCleanup:
    """UNPLANNED FINDING (not one of the 7 dispatched edge cases) --
    discovered while building a real end-to-end subprocess smoke test.

    ``read_entries()``'s own docstring documents that the JSON BODY's
    ``session_id`` field wins over the entry file's own filename stem
    ("historically it won outright") -- i.e. an entry file need not be
    named ``<session_id>.json``; this is a supported, expected shape,
    not exotic corruption (the same duality that made the v0.9.10
    ``set forget`` path-traversal finding possible).

    ``sweep_boundary()``'s cleanup step does not go through that same
    resolution: it deletes via
    ``entry_path(claude_dir, entry["session_id"]).unlink()`` -- i.e. by
    the BODY's claimed id -- which silently misses (``FileNotFoundError``
    swallowed by the existing ``except OSError: pass``) whenever the
    actual file lives at a different name. Net effect: the entry is
    correctly RECORDED as open-at-shutdown evidence every time, but is
    NEVER actually removed from ``csb-live/`` -- confirmed PERSISTENT
    (not a one-cycle quirk like ``TestPidAtWithoutPid`` above) across
    two separate sweep boundaries in manual probing, and confirmed
    present in the code BEFORE this release's fix too (this is not a
    v0.9.12 regression; it is a pre-existing gap in the same function
    this release modified).

    This directly works against HV.5's "must not turn the registry into
    a graveyard of dead entries" guarantee: a mismatched-filename entry
    IS exactly such a graveyard resident, and it will be re-recorded
    into every future boundary snapshot too since it is never cleared.

    Reported as a genuine finding for the maintainer -- not fixed here
    (diagnose, not fix). This is an intentionally FAILING anchor test:
    it encodes the behaviour the sweep's own docstring promises
    ("clearing... is what keeps each boundary's evidence attributable
    to THAT boundary") and currently does not deliver for this shape.
    """

    def test_recorded_entry_is_actually_removed_from_disk(self, tmp_path):
        """#90, FIXED (strict xfail retired). The sweep now unlinks the
        file read_entries actually read (carried as ``_path``), not a
        path rebuilt from the body-claimed id. Identity and location
        are separate questions: the safe body claim still names the
        session in the record; the path read is what cleanup targets."""
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        # Filename deliberately does NOT match the body's session_id --
        # read_entries() honours the body per its own documented rule.
        mismatched_path = lr.live_dir(tmp_path) / "weird-filename.json"
        mismatched_path.write_text(json.dumps({
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-000000000001",
            "started_at": "2026-07-30T10:00:00Z",
            "source": "startup", "cwd": "C:/w",
        }), encoding="utf-8")

        recorded = lr.sweep_boundary(tmp_path, BOOT)
        assert recorded == 1, "still correctly recorded as evidence"
        assert not mismatched_path.exists(), (
            "sweep_boundary claims to CLEAR pre-boot entries after "
            "recording them, but this one -- whose filename does not "
            "match its body's session_id, a shape read_entries() itself "
            "documents as expected -- was silently left behind. The "
            "deletion loop unlinks entry_path(claude_dir, "
            "entry['session_id']), which resolves to a DIFFERENT path "
            "than the file actually read from.")


class TestLargeRegistries:
    """Edge case 7 -- 200+ entry registries and boundary histories.
    Measured linear (not quadratic) scaling for both `read_entries` and
    the retention scan's now-reads-every-file logic; timings recorded in
    the results report, not asserted here as hard thresholds (timing
    assertions are flaky across CI hosts) -- these tests assert
    CORRECTNESS at scale (right count swept, retention still caps at
    BOUNDARY_RETENTION) with a generous, non-flaky time ceiling as a
    smoke guard against an accidental quadratic regression."""

    def test_sweep_correctness_with_250_live_entries(self, tmp_path):
        n = 250
        for i in range(n):
            _write(tmp_path, f"bulk-{i:04d}",
                  started_at="2026-07-30T10:00:00Z",
                  source="startup", cwd="C:/w")
        t0 = time.perf_counter()
        recorded = lr.sweep_boundary(tmp_path, BOOT)
        elapsed = time.perf_counter() - t0
        assert recorded == n
        assert not list(lr.live_dir(tmp_path).glob("bulk-*.json"))
        # Generous smoke ceiling -- a real quadratic regression at this
        # N would blow well past this, a linear implementation will not.
        assert elapsed < 5.0, f"sweep of {n} entries took {elapsed:.2f}s"

    def test_retention_caps_with_300_preexisting_boundary_files(
            self, tmp_path):
        n_boundaries = 300
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        bdir = lr.boundary_dir(tmp_path)
        bdir.mkdir(parents=True, exist_ok=True)
        for i in range(n_boundaries):
            stamp = f"20200101T{i % 24:02d}{i % 60:02d}00Z"
            names = [{"session_id": f"s{i}"}] if i % 3 else []
            (bdir / f"boundary-{stamp}-{i:05d}.json").write_text(
                json.dumps({"version": 1, "boot_at": stamp,
                           "captured_at": stamp,
                           "open_at_shutdown": names}),
                encoding="utf-8")
        _write(tmp_path, "trigger", started_at="2026-07-30T10:00:00Z",
              source="startup", cwd="C:/w")

        t0 = time.perf_counter()
        lr.sweep_boundary(tmp_path, BOOT)
        elapsed = time.perf_counter() - t0

        remaining = list(bdir.glob("boundary-*.json"))
        assert len(remaining) <= lr.BOUNDARY_RETENTION
        assert elapsed < 5.0, (
            f"sweep with {n_boundaries} preexisting boundary files took "
            f"{elapsed:.2f}s -- check for quadratic behaviour in the "
            "retention scan")


class TestMismatchCleanupContract:
    """#90's remaining acceptance criteria beyond the retired anchor."""

    def _mismatched(self, tmp_path):
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        p = lr.live_dir(tmp_path) / "weird-filename.json"
        p.write_text(json.dumps({
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-000000000001",
            "started_at": "2026-07-30T10:00:00Z",
            "source": "startup", "cwd": "C:/w",
        }), encoding="utf-8")
        return p

    def test_resweep_does_not_rerecord(self, tmp_path):
        """AC: once cleared, the entry attends no further boundaries --
        the record holds it exactly once after any number of sweeps."""
        self._mismatched(tmp_path)
        lr.sweep_boundary(tmp_path, BOOT)
        lr.sweep_boundary(tmp_path, BOOT)
        snap = lr.read_snapshot(tmp_path)
        ids = [e.get("session_id") for e in snap["open_at_shutdown"]]
        assert ids == ["aaaaaaaa-bbbb-cccc-dddd-000000000001"]

    def test_containment_recheck_refuses_paths_outside_the_registry(
            self, tmp_path, monkeypatch):
        """AC: the deletion re-check stays. Even if an entry arrives
        claiming a _path outside csb-live/ (entries ride git-synced
        stores), the sweep must not unlink it."""
        victim = tmp_path / "precious.json"
        victim.write_text("{}", encoding="utf-8")
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        real = dict(session_id="aaaaaaaa-bbbb-cccc-dddd-000000000001",
                    started_at="2026-07-30T10:00:00Z", source="", cwd="",
                    pid=None, pid_at=None, _path=str(victim))
        monkeypatch.setattr(lr, "read_entries", lambda _cd: [real])
        lr.sweep_boundary(tmp_path, BOOT)
        assert victim.exists(), (
            "a _path outside the registry directory must never be "
            "unlinked, whatever the entry claims")

    def test_records_and_derived_answers_never_carry_path(self, tmp_path):
        """AC hygiene: _path is cleanup targeting, not evidence -- it
        must not appear in the written record."""
        self._mismatched(tmp_path)
        lr.sweep_boundary(tmp_path, BOOT)
        snap = lr.read_snapshot(tmp_path)
        assert all("_path" not in e for e in snap["open_at_shutdown"])


class TestFailedReplaceLeavesNoTemp:
    """CI 2026-08-20 (Windows, py3.11): a sharing-violation on
    os.replace during racing sweeps orphaned a boundary .tmp file.
    Deterministic pin: when the replace fails, the temp is cleaned."""

    def test_boundary_replace_failure_cleans_its_temp(
            self, tmp_path, monkeypatch):
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        _write(tmp_path, "dead-1", started_at="2026-07-30T10:00:00Z",
               source="startup", cwd="C:/w")
        real_replace = lr.os.replace

        def flaky(src, dst):
            if "boundaries" in str(dst):
                raise PermissionError("sharing violation")
            return real_replace(src, dst)

        monkeypatch.setattr(lr.os, "replace", flaky)
        lr.sweep_boundary(tmp_path, BOOT)
        leftovers = (list(lr.live_dir(tmp_path).glob("*.tmp-*"))
                     + list(lr.boundary_dir(tmp_path).glob("*.tmp-*")))
        assert leftovers == [], (
            f"a failed replace must not orphan its temp: {leftovers}")

    def test_alias_replace_failure_cleans_its_temp(
            self, tmp_path, monkeypatch):
        """CI 2026-08-22 (Windows, py3.13): the FIRST fix patched only
        the boundary-history site; the alias write still used a
        pid-only temp name and orphaned it on a sharing violation.
        Pin both sites independently so a half-fix cannot pass again."""
        lr.live_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        _write(tmp_path, "dead-1", started_at="2026-07-30T10:00:00Z",
               source="startup", cwd="C:/w")
        real_replace = lr.os.replace

        def flaky(src, dst):
            if str(dst).endswith(lr.SNAPSHOT_FILENAME):
                raise PermissionError("sharing violation")
            return real_replace(src, dst)

        monkeypatch.setattr(lr.os, "replace", flaky)
        lr.sweep_boundary(tmp_path, BOOT)  # never raises (hook context)
        leftovers = (list(lr.live_dir(tmp_path).glob("*.tmp-*"))
                     + list(lr.boundary_dir(tmp_path).glob("*.tmp-*")))
        assert leftovers == [], (
            f"a failed alias replace must not orphan its temp: {leftovers}")

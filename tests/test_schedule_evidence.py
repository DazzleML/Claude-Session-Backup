"""Tests for #69 step 5b: ensure_gitignore (AC-21), run-log rotation
(AC-10), and the three-layer evidence verdict (AC-14/D3).

The AC-21 case is the one contract-critical red-green in this batch
(per the sprint calibration): a store carrying the managed block must
survive a human's `git add -A` with ZERO csb-operational files staged
-- and the paired test proves that WITHOUT the block the same command
stages them, so the guard demonstrably can fail.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_session_backup.git_ops import (
    GITIGNORE_MARKER_BEGIN,
    ensure_gitignore,
    run_git,
)
from claude_session_backup.pathkit import ClaudePaths
from claude_session_backup.schedule_backends import (
    BackendStatus,
    evaluate_schedule_evidence,
)

TZ = timezone(timedelta(hours=-4))


def _stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _line(dt: datetime, outcome: str = "ok") -> str:
    return (f"{_stamp(dt)} outcome={outcome} rc=0 "
            f"duration_ms=100 claude_dir=x\n")


# ── ensure_gitignore: the managed block (AC-21) ─────────────────────────


def test_gitignore_created_with_block(tmp_path):
    assert ensure_gitignore(str(tmp_path)) is True
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert GITIGNORE_MARKER_BEGIN in text
    rules = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert ClaudePaths.CSB_LOGS + "/" in rules
    assert ClaudePaths.DEFAULT_DB in rules
    assert ClaudePaths.LOCK_FILE in rules
    assert ClaudePaths.FTS_DIR + "/" in rules
    # Data stays committed: never an ignore RULE for these (the comment
    # may mention them; a bare rule line may not exist).
    assert ClaudePaths.LIVE_DIR + "/" not in rules
    assert ClaudePaths.DISTILLED + "/" not in rules
    assert "backups/" not in rules


def test_gitignore_idempotent_and_preserving(tmp_path):
    (tmp_path / ".gitignore").write_text("# mine\nmy-secret-dir/\n",
                                         encoding="utf-8")
    assert ensure_gitignore(str(tmp_path)) is True
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith("# mine\nmy-secret-dir/\n")  # user content first, verbatim
    assert ensure_gitignore(str(tmp_path)) is False     # second call no-op
    assert text == (tmp_path / ".gitignore").read_text(encoding="utf-8")


def _seed_store(root: Path) -> None:
    """A store with operational artifacts AND data files."""
    run_git(str(root), "init", "-q")
    (root / ClaudePaths.CSB_LOGS).mkdir()
    (root / ClaudePaths.CSB_LOGS / "schedule.log").write_text("run\n")
    (root / ClaudePaths.DEFAULT_DB).write_text("sqlite")
    (root / ClaudePaths.LOCK_FILE).write_text("pid")
    (root / ClaudePaths.FTS_DIR).mkdir()
    (root / ClaudePaths.FTS_DIR / "proj.db").write_text("fts")
    (root / ClaudePaths.LIVE_DIR).mkdir()
    (root / ClaudePaths.LIVE_DIR / "u.json").write_text("{}")   # DATA
    (root / "projects" / "slug").mkdir(parents=True)
    (root / "projects" / "slug" / "u.jsonl").write_text("{}\n")  # DATA


def _staged_after_add_all(root: Path) -> set[str]:
    run_git(str(root), "add", "-A")
    r = run_git(str(root), "diff", "--cached", "--name-only")
    return {l for l in r.stdout.splitlines() if l.strip()}


OPERATIONAL = lambda: {  # noqa: E731
    f"{ClaudePaths.CSB_LOGS}/schedule.log",
    ClaudePaths.DEFAULT_DB,
    ClaudePaths.LOCK_FILE,
    f"{ClaudePaths.FTS_DIR}/proj.db",
}


def test_ac21_red_without_block_add_all_sweeps_operational(tmp_path):
    # The RED half: proves the guard is guarding something real.
    _seed_store(tmp_path)
    staged = _staged_after_add_all(tmp_path)
    assert OPERATIONAL() <= staged


def test_ac21_green_block_survives_git_add_all(tmp_path):
    _seed_store(tmp_path)
    ensure_gitignore(str(tmp_path))
    staged = _staged_after_add_all(tmp_path)
    assert not (OPERATIONAL() & staged), staged
    # Data still stages -- protection never hides user data.
    assert f"{ClaudePaths.LIVE_DIR}/u.json" in staged
    assert "projects/slug/u.jsonl" in staged


# ── Run-log rotation (AC-10) ────────────────────────────────────────────


def test_runlog_rotates_at_cap(tmp_path):
    from claude_session_backup.commands import _append_run_log
    import time as _t
    log = tmp_path / "schedule.log"
    log.write_text("x" * 1_100_000, encoding="utf-8")
    _append_run_log(log, "ok", 0, _t.monotonic(), str(tmp_path))
    rotated = log.with_suffix(log.suffix + ".1")
    assert rotated.exists() and rotated.stat().st_size > 1_000_000
    assert log.stat().st_size < 1_000
    assert "outcome=ok" in log.read_text(encoding="utf-8")


# ── The evidence verdict (AC-14) ────────────────────────────────────────


def _status(installed=True, running=False):
    return BackendStatus(installed=installed, running_now=running)


def test_verdict_not_installed_hints_setup(tmp_path):
    v = evaluate_schedule_evidence(_status(installed=False), 1440,
                                   tmp_path / "schedule.log")
    assert v.state == "not-installed" and "csb setup schedule" in v.detail


def test_verdict_pending_when_no_evidence(tmp_path):
    v = evaluate_schedule_evidence(_status(), 1440, tmp_path / "s.log")
    assert v.state == "pending" and "prime" in v.detail


def test_verdict_ok_within_two_intervals(tmp_path):
    now = datetime(2026, 8, 7, 12, 0, 0, tzinfo=TZ)
    log = tmp_path / "s.log"
    log.write_text(_line(now - timedelta(hours=20)), encoding="utf-8")
    v = evaluate_schedule_evidence(_status(), 1440, log, now=now)
    assert v.state == "ok"


def test_verdict_installed_not_running_past_threshold(tmp_path):
    now = datetime(2026, 8, 7, 12, 0, 0, tzinfo=TZ)
    log = tmp_path / "s.log"
    log.write_text(_line(now - timedelta(days=3)), encoding="utf-8")
    v = evaluate_schedule_evidence(_status(), 1440, log, now=now)
    assert v.state == "installed-not-running"
    assert "INSTALLED BUT NOT RUNNING" in v.detail


def test_verdict_reads_rotated_log_too(tmp_path):
    now = datetime(2026, 8, 7, 12, 0, 0, tzinfo=TZ)
    log = tmp_path / "s.log"
    log.write_text("garbage no timestamp\n", encoding="utf-8")
    log.with_suffix(log.suffix + ".1").write_text(
        _line(now - timedelta(hours=6)), encoding="utf-8")
    v = evaluate_schedule_evidence(_status(), 1440, log, now=now)
    assert v.state == "ok"


def test_verdict_running_now_softens_not_running(tmp_path):
    now = datetime(2026, 8, 7, 12, 0, 0, tzinfo=TZ)
    log = tmp_path / "s.log"
    log.write_text(_line(now - timedelta(days=5)), encoding="utf-8")
    v = evaluate_schedule_evidence(_status(running=True), 1440, log, now=now)
    assert v.state == "installed-not-running"
    assert "executing right now" in v.detail


def test_verdict_pending_softens_when_running_now(tmp_path):
    # Field-caught (2026-08-07 walkthrough): the user ran --status while
    # the prime was mid-execution (267009) and was told to consider
    # re-running setup. When the OS says a run is executing, say THAT.
    v = evaluate_schedule_evidence(_status(running=True), 1440,
                                   tmp_path / "s.log")
    assert v.state == "pending"
    assert "executing right now" in v.detail
    assert "re-run" not in v.detail


def test_verdict_ok_carries_error_outcome_warning(tmp_path):
    # Acceptance-check finding: a machine whose schedule fires but whose
    # backups FAIL (e.g. repo-less store) writes outcome=error lines --
    # execution evidence exists, so the state is ok, but the detail must
    # not read as all-clear.
    now = datetime(2026, 8, 7, 12, 0, 0, tzinfo=TZ)
    log = tmp_path / "s.log"
    log.write_text(_line(now - timedelta(hours=2), outcome="error"),
                   encoding="utf-8")
    v = evaluate_schedule_evidence(_status(), 1440, log, now=now)
    assert v.state == "ok"
    assert "ERROR" in v.detail


def test_verdict_ok_quiet_on_benign_outcomes(tmp_path):
    # skipped-lock is evidence the schedule fired while a hook run held
    # the lock -- benign by design, never a warning.
    now = datetime(2026, 8, 7, 12, 0, 0, tzinfo=TZ)
    log = tmp_path / "s.log"
    log.write_text(_line(now - timedelta(hours=2), outcome="skipped-lock"),
                   encoding="utf-8")
    v = evaluate_schedule_evidence(_status(), 1440, log, now=now)
    assert v.state == "ok"
    assert "ERROR" not in v.detail

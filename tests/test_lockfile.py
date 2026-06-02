"""Tests for the lock file mechanism, incl. PID-reuse staleness (v0.3.6)."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_session_backup import lockfile
from claude_session_backup.lockfile import backup_lock


# ── helpers ────────────────────────────────────────────────────────────


def _write_lock(path, pid, *, acquired_at=None, proc_name=None,
                start_time=None, host="testhost"):
    """Write a v0.3.6 JSON lock with the given identity fields."""
    path.write_text(json.dumps({
        "pid": pid,
        "acquired_at": acquired_at,
        "proc_name": proc_name,
        "start_time": start_time,
        "host": host,
    }), encoding="utf-8")


def _iso_ago(seconds):
    """ISO-UTC timestamp `seconds` in the past (for age-backstop tests)."""
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── basic acquire / release ────────────────────────────────────────────


def test_lock_acquire_and_release(tmp_path):
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is True
        assert lock_file.exists()
        data = json.loads(lock_file.read_text())
        assert data["pid"] == os.getpid()
        assert "acquired_at" in data
    # Cleaned up after the context exits.
    assert not lock_file.exists()


def test_lock_blocks_second_instance(tmp_path, monkeypatch):
    """A lock whose PID is alive AND identity matches AND is fresh blocks."""
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    # A live, fresh, identity-matching lock held by some other pid.
    _write_lock(lock_file, 4242, acquired_at=_iso_ago(5),
                proc_name="python.exe", start_time=1000.0)
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(lockfile, "_proc_name", lambda pid: "python.exe")
    monkeypatch.setattr(lockfile, "_proc_start_time", lambda pid: 1000.0)

    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is False


def test_lock_stale_dead_pid_reclaimed(tmp_path, monkeypatch):
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    _write_lock(lock_file, 999999, acquired_at=_iso_ago(5),
                proc_name="python.exe", start_time=1000.0)
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: False)

    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is True


def test_lock_corrupted_file(tmp_path):
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    lock_file.write_text("not a pid")  # non-JSON garbage
    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is True  # corrupted -> take over


def test_lock_bare_pid_treated_as_corrupted(tmp_path):
    """A bare integer (old format) is no longer special-cased -> take over."""
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    lock_file.write_text(str(os.getpid()))  # bare int, our live pid
    with backup_lock(lock_dir, quiet=True) as acquired:
        # Not parsed as a real lock -> treated as corrupted -> reclaimed.
        assert acquired is True


def test_lock_missing_dir(tmp_path):
    lock_dir = str(tmp_path / "nested")
    Path(lock_dir).mkdir()
    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is True


# ── PID-reuse staleness (the v0.3.6 fix) ───────────────────────────────


def test_reuse_by_different_name_reclaimed(tmp_path, monkeypatch):
    """THE bug: PID alive but it's a DIFFERENT program now -> reclaim."""
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    _write_lock(lock_file, 2932, acquired_at=_iso_ago(60),
                proc_name="python.exe", start_time=1000.0)
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(lockfile, "_proc_name", lambda pid: "WindowsTerminal.exe")
    monkeypatch.setattr(lockfile, "_proc_start_time", lambda pid: 9999.0)

    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is True  # reused by a different program -> stale


def test_reuse_by_same_name_newer_start_reclaimed(tmp_path, monkeypatch):
    """PID alive, same exe name, but a LATER start-time -> reuse -> reclaim."""
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    _write_lock(lock_file, 2932, acquired_at=_iso_ago(60),
                proc_name="python.exe", start_time=1000.0)
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(lockfile, "_proc_name", lambda pid: "python.exe")
    monkeypatch.setattr(lockfile, "_proc_start_time", lambda pid: 5000.0)

    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is True


def test_genuine_live_lock_not_reclaimed(tmp_path, monkeypatch):
    """Alive, identity matches within tolerance, fresh -> must NOT reclaim."""
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    _write_lock(lock_file, 2932, acquired_at=_iso_ago(10),
                proc_name="python.exe", start_time=1000.0)
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(lockfile, "_proc_name", lambda pid: "python.exe")
    # within _START_TIME_TOLERANCE_SECONDS
    monkeypatch.setattr(lockfile, "_proc_start_time", lambda pid: 1000.4)

    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is False


def test_hung_lock_under_threshold_not_reclaimed(tmp_path, monkeypatch):
    """Identity matches, age 5 min (< 30 min) -> still considered live."""
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    _write_lock(lock_file, 2932, acquired_at=_iso_ago(300),
                proc_name="python.exe", start_time=1000.0)
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(lockfile, "_proc_name", lambda pid: "python.exe")
    monkeypatch.setattr(lockfile, "_proc_start_time", lambda pid: 1000.0)

    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is False


def test_hung_lock_over_threshold_reclaimed(tmp_path, monkeypatch):
    """Identity matches but age exceeds threshold -> backstop reclaims."""
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    _write_lock(lock_file, 2932, acquired_at=_iso_ago(2400),  # 40 min
                proc_name="python.exe", start_time=1000.0)
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(lockfile, "_proc_name", lambda pid: "python.exe")
    monkeypatch.setattr(lockfile, "_proc_start_time", lambda pid: 1000.0)

    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is True


def test_identity_unavailable_falls_back_to_age(tmp_path, monkeypatch):
    """When name + start-time can't be read (None), only the age backstop
    decides. Fresh -> live; old -> reclaim."""
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(lockfile, "_proc_name", lambda pid: None)
    monkeypatch.setattr(lockfile, "_proc_start_time", lambda pid: None)

    # Fresh -> not reclaimed
    _write_lock(lock_file, 2932, acquired_at=_iso_ago(10),
                proc_name=None, start_time=None)
    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is False

    # Old -> reclaimed by the backstop
    _write_lock(lock_file, 2932, acquired_at=_iso_ago(2400),
                proc_name=None, start_time=None)
    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is True


def test_negative_age_treated_as_fresh(tmp_path, monkeypatch):
    """A future acquired_at (clock skew) must not be reaped on age alone."""
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    _write_lock(lock_file, 2932, acquired_at=future,
                proc_name="python.exe", start_time=1000.0)
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(lockfile, "_proc_name", lambda pid: "python.exe")
    monkeypatch.setattr(lockfile, "_proc_start_time", lambda pid: 1000.0)

    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is False  # negative age -> fresh, identity matches


# ── observability ──────────────────────────────────────────────────────


def test_reclaim_emits_notice(tmp_path, monkeypatch, capsys):
    """Reclaiming a stale lock prints a visible notice (the old failure was
    silent)."""
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    _write_lock(lock_file, 2932, acquired_at=_iso_ago(60),
                proc_name="python.exe", start_time=1000.0)
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(lockfile, "_proc_name", lambda pid: "WindowsTerminal.exe")
    monkeypatch.setattr(lockfile, "_proc_start_time", lambda pid: 9999.0)

    with backup_lock(lock_dir, quiet=False) as acquired:
        assert acquired is True
    err = capsys.readouterr().err
    assert "reclaimed stale backup lock" in err
    assert "WindowsTerminal.exe" in err


def test_skip_emits_informative_line(tmp_path, monkeypatch, capsys):
    """A genuine concurrent run prints PID + age, not a bare 'skipping'."""
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    _write_lock(lock_file, 4242, acquired_at=_iso_ago(180),
                proc_name="python.exe", start_time=1000.0)
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(lockfile, "_proc_name", lambda pid: "python.exe")
    monkeypatch.setattr(lockfile, "_proc_start_time", lambda pid: 1000.0)

    with backup_lock(lock_dir, quiet=False) as acquired:
        assert acquired is False
    err = capsys.readouterr().err
    assert "PID 4242" in err
    assert "started 3m ago" in err


def test_quiet_suppresses_messages(tmp_path, monkeypatch, capsys):
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"
    _write_lock(lock_file, 4242, acquired_at=_iso_ago(5),
                proc_name="python.exe", start_time=1000.0)
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(lockfile, "_proc_name", lambda pid: "python.exe")
    monkeypatch.setattr(lockfile, "_proc_start_time", lambda pid: 1000.0)

    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is False
    assert capsys.readouterr().err == ""


# ── concurrency: readback-verify ───────────────────────────────────────


def test_readback_verify_loser_yields_false(tmp_path, monkeypatch):
    """If another run wins the file between our write and readback, we yield
    False instead of proceeding (single-winner under race)."""
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"

    real_read = lockfile._read_lock
    calls = {"n": 0}

    def fake_read(path):
        calls["n"] += 1
        # 1st call: pre-acquire existence check -> no lock.
        if calls["n"] == 1:
            return None
        # 2nd call: readback after our write -> pretend a rival won.
        if calls["n"] == 2:
            return {"pid": os.getpid() + 1, "acquired_at": None,
                    "proc_name": None, "start_time": None}
        return real_read(path)

    monkeypatch.setattr(lockfile, "_read_lock", fake_read)

    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is False


# ── release ownership ──────────────────────────────────────────────────


def test_release_only_unlinks_when_owned(tmp_path, monkeypatch):
    """If the lock file gets overwritten by another pid during our run, we
    must NOT delete it on exit."""
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"

    with backup_lock(lock_dir, quiet=True) as acquired:
        assert acquired is True
        # Someone else stomps the lock with their pid mid-run.
        _write_lock(lock_file, os.getpid() + 1, acquired_at=_iso_ago(1),
                    proc_name="python.exe", start_time=2000.0)

    # We don't own it anymore -> we must leave it in place.
    assert lock_file.exists()
    assert json.loads(lock_file.read_text())["pid"] == os.getpid() + 1

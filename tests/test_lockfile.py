"""Tests for the lock file mechanism."""

import os
from pathlib import Path

from claude_session_backup.lockfile import backup_lock


def test_lock_acquire_and_release(tmp_path):
    lock_dir = str(tmp_path)
    with backup_lock(lock_dir) as acquired:
        assert acquired is True
        lock_file = tmp_path / ".csb-backup.lock"
        assert lock_file.exists()
        assert lock_file.read_text().strip() == str(os.getpid())

    # Lock file should be cleaned up after context exits
    assert not lock_file.exists()


def test_lock_blocks_second_instance(tmp_path):
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"

    # Simulate another running process by writing our own PID
    # (our PID is alive, so the lock should block)
    lock_file.write_text(str(os.getpid()))

    with backup_lock(lock_dir) as acquired:
        assert acquired is False  # Should not acquire


def test_lock_stale_detection(tmp_path):
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"

    # Write a PID that doesn't exist (very unlikely to be running)
    lock_file.write_text("999999999")

    with backup_lock(lock_dir) as acquired:
        assert acquired is True  # Stale lock should be taken over


def test_lock_corrupted_file(tmp_path):
    lock_dir = str(tmp_path)
    lock_file = tmp_path / ".csb-backup.lock"

    # Write garbage to the lock file
    lock_file.write_text("not a pid")

    with backup_lock(lock_dir) as acquired:
        assert acquired is True  # Corrupted lock should be taken over


def test_lock_missing_dir(tmp_path):
    lock_dir = str(tmp_path / "nonexistent")
    # Should not crash even if directory doesn't exist
    # (backup_lock writes to claude_dir which should exist)
    Path(lock_dir).mkdir()

    with backup_lock(lock_dir) as acquired:
        assert acquired is True

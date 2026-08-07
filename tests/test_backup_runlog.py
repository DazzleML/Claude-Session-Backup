"""Tests for `csb backup --log-file` -- #69 step 5a (AC-10, AC-13).

Covers: one structured line per run; the contended-lock loser still logs
(evidence the schedule fired); logging failure never fails the backup;
and the pythonw reality -- with sys.stdout/stderr None, the run rebinds
both to the log and stays diagnosable instead of dying silently.
"""

from __future__ import annotations

import re
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from claude_session_backup.commands import cmd_backup
from claude_session_backup.lockfile import backup_lock

LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} "
    r"outcome=(?P<outcome>[\w-]+) rc=(?P<rc>-?\d+) "
    r"duration_ms=(?P<ms>\d+) claude_dir=(?P<dir>.+)$"
)


def _args(mock_claude_dir, log_file=None, **over):
    base = dict(
        claude_dir=str(mock_claude_dir), db=None, quiet=True,
        no_commit=True, log_file=str(log_file) if log_file else None,
    )
    base.update(over)
    return Namespace(**base)


def _read_lines(path: Path) -> list[re.Match]:
    matches = [LINE_RE.match(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert all(matches), f"unparseable run-log line in {path.read_text(encoding='utf-8')!r}"
    return matches


def test_backup_appends_one_structured_line(mock_claude_dir, tmp_path):
    log = tmp_path / "csb-logs" / "schedule.log"  # parent must be auto-created
    rc = cmd_backup(_args(mock_claude_dir, log))
    assert rc == 0
    (m,) = _read_lines(log)
    assert m["outcome"] == "ok" and m["rc"] == "0"
    assert m["dir"] == str(mock_claude_dir)


def test_two_runs_two_lines_appended(mock_claude_dir, tmp_path):
    log = tmp_path / "schedule.log"
    assert cmd_backup(_args(mock_claude_dir, log)) == 0
    assert cmd_backup(_args(mock_claude_dir, log)) == 0
    assert len(_read_lines(log)) == 2


def test_no_log_file_means_no_log(mock_claude_dir, tmp_path):
    assert cmd_backup(_args(mock_claude_dir, None)) == 0
    assert not (tmp_path / "schedule.log").exists()


def test_contended_lock_loser_logs_skipped_and_exits_zero(mock_claude_dir, tmp_path):
    # AC-12/AC-13: while another instance holds the lock, the scheduled
    # run must exit 0 quietly -- but its log line is the EVIDENCE that
    # the schedule fired at all, so it must still be written.
    log = tmp_path / "schedule.log"
    with backup_lock(str(mock_claude_dir), quiet=True) as acquired:
        assert acquired
        rc = cmd_backup(_args(mock_claude_dir, log))
    assert rc == 0
    (m,) = _read_lines(log)
    assert m["outcome"] == "skipped-lock" and m["rc"] == "0"


def test_unwritable_log_never_fails_the_backup(mock_claude_dir, tmp_path):
    # AC-10: point the log under a FILE so mkdir/open must fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    bad_log = blocker / "nested" / "schedule.log"
    rc = cmd_backup(_args(mock_claude_dir, bad_log))
    assert rc == 0  # backup outcome unchanged; no exception escaped


def test_pythonw_streams_rebound_to_log(mock_claude_dir, tmp_path, monkeypatch):
    # Under pythonw both std streams are None: print() is a no-op and any
    # direct stream write raises. The rebind must make a NOISY run (not
    # --quiet) complete and land its output in the log file.
    log = tmp_path / "schedule.log"
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    rc = cmd_backup(_args(mock_claude_dir, log, quiet=False))
    assert rc == 0
    assert sys.stdout is not None and sys.stderr is not None  # rebound
    text = log.read_text(encoding="utf-8")
    assert "outcome=ok" in text
    # rebound stream handle must be flushed/usable -- the run's normal
    # human output preceded the structured line in the same file
    assert len(text.splitlines()) >= 1


def test_pythonw_without_log_file_does_not_crash(mock_claude_dir, monkeypatch):
    # No --log-file and no console: quiet mode must still survive
    # (print() to None is a documented no-op; nothing may .write()).
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    rc = cmd_backup(_args(mock_claude_dir, None))
    assert rc == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console semantics")
def test_git_spawns_suppress_child_console_windows(mock_claude_dir, monkeypatch):
    # Field-caught during the P1 probe (2026-08-06): under pythonw (no
    # parent console) every git child got its OWN conhost window -- a
    # visible flash per git call, several per backup. Every run_git spawn
    # must pass CREATE_NO_WINDOW so scheduled runs are truly windowless.
    import subprocess as sp
    from claude_session_backup import git_ops

    seen = []
    real_run = sp.run

    def spy(cmd, **kw):
        if cmd and cmd[0] == "git":
            seen.append(kw.get("creationflags", 0))
        return real_run(cmd, **kw)

    monkeypatch.setattr(sp, "run", spy)
    rc = cmd_backup(_args(mock_claude_dir, None, no_commit=False))
    assert rc == 0
    assert seen, "backup made no git calls?"
    assert all(f & sp.CREATE_NO_WINDOW for f in seen), (
        "a git spawn is missing CREATE_NO_WINDOW -- scheduled runs "
        "will flash conhost windows"
    )

"""Tests for #69 step 7: the `csb setup schedule` wiring.

Backends are faked at the `choose_backend` seam (the feature's single
platform gate), so every flow -- guided interval, dry-run, refusal,
WSL caveat, remove, status, prime reporting, config recording, the
`csb status` ambient line, and the setup-checklist row -- runs
identically on any development platform. The OS-talking layer beneath
has its own suite (test_schedule_backends.py, mocked at `_run`).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from claude_session_backup import commands
from claude_session_backup.cli import build_parser
from claude_session_backup.pathkit import ClaudePaths
from claude_session_backup.schedule import DEFAULT_INTERVAL_MINUTES
from claude_session_backup.schedule_backends import (
    SCHEDULE_REFUSED_EXIT,
    BackendStatus,
    DetectResult,
    InstallResult,
    RemoveResult,
)


class FakeBackend:
    """Protocol-shaped test double; records every call."""

    name = "crontab"

    def __init__(self, *, detect=None, install=None, remove=None,
                 status=None, prime=(True, "primed"), name=None):
        self._detect = detect or DetectResult(available=True)
        self._install = install or InstallResult(True, detail="fake entry")
        self._remove = remove or RemoveResult(True, was_installed=True,
                                              detail="fake removed")
        self._status = status or BackendStatus(installed=False)
        self._prime = prime
        if name:
            self.name = name
        self.calls = []
        self.installed_spec = None

    def detect(self):
        self.calls.append("detect")
        return self._detect

    def render(self, spec):
        self.calls.append("render")
        return f"<fake artifact interval={spec.interval_minutes}>"

    def install(self, spec):
        self.calls.append("install")
        self.installed_spec = spec
        return self._install

    def remove(self, claude_dir):
        self.calls.append("remove")
        return self._remove

    def status(self, spec=None):
        self.calls.append("status")
        return self._status

    def describe_entry(self):
        return "fake entry identity"

    def prime(self, spec):
        self.calls.append("prime")
        return self._prime


def _ns(claude_dir, **kw):
    base = dict(claude_dir=str(claude_dir), db=None, quiet=False,
                setup_action="schedule", interval=None, auto=False,
                dry_run=False, remove=False, status_only=False,
                print_systemd=False)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def fake_backend(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    monkeypatch.setattr(commands, "_interactive", lambda: False)
    return backend


def _config_file(claude_dir) -> dict:
    p = ClaudePaths.from_dir(claude_dir).config_file
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ── Parser wiring ───────────────────────────────────────────────────────


def test_parser_schedule_leaf_parses():
    args = build_parser().parse_args(
        ["setup", "schedule", "--interval", "720", "--dry-run"])
    assert args.command == "setup"
    assert getattr(args, "setup_action", None) == "schedule"
    assert args.interval == 720 and args.dry_run


def test_parser_bare_setup_has_no_schedule_action():
    args = build_parser().parse_args(["setup", "--auto"])
    assert getattr(args, "setup_action", None) is None
    assert args.auto


def test_parser_action_flags_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["setup", "schedule", "--dry-run", "--remove"])


def test_parser_common_flags_on_leaf():
    args = build_parser().parse_args(
        ["setup", "schedule", "--status", "--claude-dir", "X"])
    assert args.claude_dir == "X" and args.status_only


def test_cmd_setup_routes_schedule_action(tmp_path, monkeypatch, fake_backend):
    # Through cmd_setup (the cli.py dispatch target), not just the leaf fn.
    rc = commands.cmd_setup(_ns(tmp_path, status_only=True))
    assert rc == 0
    assert fake_backend.calls == ["status"]


# ── Consent (AC-17): bare setup never touches the scheduler ─────────────


def test_bare_setup_auto_never_calls_backend(tmp_path, monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    monkeypatch.setattr(commands, "_interactive", lambda: False)
    ns = _ns(tmp_path, setup_action=None, auto=True)
    ns.index_only = False
    commands.cmd_setup(ns)
    # The checklist row may probe status(); nothing may install or prime.
    assert "install" not in backend.calls and "prime" not in backend.calls


# ── Install flow ────────────────────────────────────────────────────────


def test_auto_installs_default_interval_and_primes(tmp_path, fake_backend, capsys):
    rc = commands.cmd_setup_schedule(_ns(tmp_path, auto=True))
    assert rc == 0
    assert "install" in fake_backend.calls and "prime" in fake_backend.calls
    spec = fake_backend.installed_spec
    assert spec.interval_minutes == DEFAULT_INTERVAL_MINUTES
    assert spec.claude_dir == str(ClaudePaths.from_dir(tmp_path).root)
    assert spec.log_file == str(ClaudePaths.from_dir(tmp_path).schedule_log)
    out = capsys.readouterr().out
    assert "Installed via crontab" in out and "--status" in out


def test_install_records_interval_in_config(tmp_path, fake_backend):
    commands.cmd_setup_schedule(_ns(tmp_path, auto=True, interval=720))
    cfg = _config_file(tmp_path)
    assert cfg["schedule_interval_minutes"] == 720
    assert cfg["schedule_installed_at"]


def test_install_writes_gitignore_block(tmp_path, fake_backend):
    commands.cmd_setup_schedule(_ns(tmp_path, auto=True))
    text = ClaudePaths.from_dir(tmp_path).gitignore.read_text(encoding="utf-8")
    assert ClaudePaths.CSB_LOGS + "/" in text


def test_db_baked_only_when_non_default(tmp_path, fake_backend):
    commands.cmd_setup_schedule(_ns(tmp_path, auto=True))
    assert fake_backend.installed_spec.db_path is None
    other = tmp_path / "elsewhere.db"
    commands.cmd_setup_schedule(_ns(tmp_path, auto=True, db=str(other)))
    assert fake_backend.installed_spec.db_path == str(other)


def test_invalid_interval_is_usage_error(tmp_path, fake_backend, capsys):
    rc = commands.cmd_setup_schedule(_ns(tmp_path, interval=90))
    assert rc == 2
    assert "install" not in fake_backend.calls
    assert "90" in capsys.readouterr().err


def test_non_interactive_without_flags_refuses_with_guidance(
        tmp_path, fake_backend, capsys):
    rc = commands.cmd_setup_schedule(_ns(tmp_path))
    assert rc == 2
    err = capsys.readouterr().err
    assert "--auto" in err and "--interval" in err
    assert "install" not in fake_backend.calls


def test_install_failure_is_error_exit(tmp_path, monkeypatch, capsys):
    backend = FakeBackend(install=InstallResult(False, detail="denied"))
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    rc = commands.cmd_setup_schedule(_ns(tmp_path, auto=True))
    assert rc == 1
    assert "denied" in capsys.readouterr().err
    assert "prime" not in backend.calls


def test_prime_failure_warns_but_install_stands(tmp_path, monkeypatch, capsys):
    backend = FakeBackend(prime=(False, "spawn error"))
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    rc = commands.cmd_setup_schedule(_ns(tmp_path, auto=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARNING" in out and "spawn error" in out


def test_deferred_install_skips_prime(tmp_path, monkeypatch, capsys):
    backend = FakeBackend(
        install=InstallResult(True, deferred=True,
                              detail="plist written; loads at next login"))
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    rc = commands.cmd_setup_schedule(_ns(tmp_path, auto=True))
    assert rc == 0
    assert "prime" not in backend.calls
    assert "next login" in capsys.readouterr().out


# ── The guided interval question (Delta-10) ─────────────────────────────


def _scripted_answers(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(commands, "_interactive", lambda: True)
    monkeypatch.setattr(commands, "_ask", lambda q: next(it))


def test_prompt_default_is_24h(tmp_path, fake_backend, monkeypatch):
    _scripted_answers(monkeypatch, [""])
    commands.cmd_setup_schedule(_ns(tmp_path))
    assert fake_backend.installed_spec.interval_minutes == 1440


def test_prompt_preset_choice(tmp_path, fake_backend, monkeypatch):
    _scripted_answers(monkeypatch, ["2"])
    commands.cmd_setup_schedule(_ns(tmp_path))
    assert fake_backend.installed_spec.interval_minutes == 720


def test_prompt_custom_reasks_on_invalid(tmp_path, fake_backend, monkeypatch,
                                         capsys):
    # "4" -> custom "90" (invalid: not whole hours dividing 24) -> loop
    # -> "4" -> "30" (valid divisor of 60).
    _scripted_answers(monkeypatch, ["4", "90", "4", "30"])
    commands.cmd_setup_schedule(_ns(tmp_path))
    assert fake_backend.installed_spec.interval_minutes == 30
    assert "Not a valid interval" in capsys.readouterr().out


def test_prompt_eof_aborts_cleanly(tmp_path, fake_backend, monkeypatch):
    monkeypatch.setattr(commands, "_interactive", lambda: True)
    monkeypatch.setattr(commands, "_ask", lambda q: None)
    rc = commands.cmd_setup_schedule(_ns(tmp_path))
    assert rc == 2
    assert "install" not in fake_backend.calls


# ── Refusal (AC-7) + WSL caveat (AC-8) ─────────────────────────────────


def test_refusal_prints_full_text_and_exit_11(tmp_path, monkeypatch, capsys):
    backend = FakeBackend(
        detect=DetectResult(False, reason="no running cron daemon"))
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    rc = commands.cmd_setup_schedule(_ns(tmp_path, auto=True))
    assert rc == SCHEDULE_REFUSED_EXIT == 11
    err = capsys.readouterr().err
    assert "Nothing was installed or modified." in err
    assert "sudo apt install cron" in err
    assert "--print-systemd" in err
    assert "install" not in backend.calls


def test_non_cron_refusal_is_short(tmp_path, monkeypatch, capsys):
    backend = FakeBackend(detect=DetectResult(False, reason="no launchctl"),
                          name="launchd")
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    rc = commands.cmd_setup_schedule(_ns(tmp_path, auto=True))
    assert rc == SCHEDULE_REFUSED_EXIT
    err = capsys.readouterr().err
    assert "no launchctl" in err and "apt install" not in err


def test_wsl_caveat_printed_but_install_proceeds(tmp_path, monkeypatch, capsys):
    backend = FakeBackend(detect=DetectResult(True, is_wsl=True))
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    rc = commands.cmd_setup_schedule(_ns(tmp_path, auto=True))
    assert rc == 0
    assert "WSL" in capsys.readouterr().out
    assert "install" in backend.calls


# ── Dry run ─────────────────────────────────────────────────────────────


def test_dry_run_renders_and_installs_nothing(tmp_path, fake_backend, capsys):
    rc = commands.cmd_setup_schedule(_ns(tmp_path, auto=True, dry_run=True,
                                         interval=720))
    assert rc == 0
    out = capsys.readouterr().out
    assert "<fake artifact interval=720>" in out
    assert "Nothing was installed" in out
    assert "-m claude_session_backup" in out       # the scheduled argv, visible
    assert "render" in fake_backend.calls
    assert "install" not in fake_backend.calls
    assert not _config_file(tmp_path)              # no config write either


# ── Remove ──────────────────────────────────────────────────────────────


def test_remove_reports_and_clears_config(tmp_path, fake_backend, capsys):
    commands.cmd_setup_schedule(_ns(tmp_path, auto=True, interval=720))
    rc = commands.cmd_setup_schedule(_ns(tmp_path, remove=True))
    assert rc == 0
    assert "Removed" in capsys.readouterr().out
    assert _config_file(tmp_path)["schedule_interval_minutes"] is None


def test_remove_nothing_installed_is_clean_success(tmp_path, monkeypatch,
                                                   capsys):
    backend = FakeBackend(remove=RemoveResult(True, was_installed=False,
                                              detail="no csb block in crontab"))
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    rc = commands.cmd_setup_schedule(_ns(tmp_path, remove=True))
    assert rc == 0
    assert "Nothing to remove" in capsys.readouterr().out


def test_remove_failure_is_error_exit(tmp_path, monkeypatch, capsys):
    backend = FakeBackend(remove=RemoveResult(False, was_installed=True,
                                              detail="crontab: permission denied"))
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    rc = commands.cmd_setup_schedule(_ns(tmp_path, remove=True))
    assert rc == 1
    assert "permission denied" in capsys.readouterr().err


# ── Status ──────────────────────────────────────────────────────────────


def _write_fresh_log(claude_dir, age=timedelta(minutes=5)):
    log = ClaudePaths.from_dir(claude_dir).schedule_log
    log.parent.mkdir(parents=True, exist_ok=True)
    stamp = (datetime.now().astimezone() - age).isoformat(timespec="seconds")
    log.write_text(f"{stamp} outcome=ok rc=0 duration_ms=100 "
                   f"claude_dir=x\n", encoding="utf-8")


def test_status_not_installed_hints_setup(tmp_path, fake_backend, capsys):
    rc = commands.cmd_setup_schedule(_ns(tmp_path, status_only=True))
    assert rc == 0
    assert "csb setup schedule" in capsys.readouterr().out


def test_status_ok_with_fresh_evidence(tmp_path, monkeypatch, capsys):
    backend = FakeBackend(status=BackendStatus(installed=True))
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    commands._write_config_file_keys(str(tmp_path),
                                     schedule_interval_minutes=1440)
    _write_fresh_log(tmp_path)
    rc = commands.cmd_setup_schedule(_ns(tmp_path, status_only=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Entry installed: yes" in out
    assert "fake entry identity" in out    # names WHAT to look for in the OS UI
    assert "last scheduled run" in out


def test_status_installed_not_running_verdict(tmp_path, monkeypatch, capsys):
    backend = FakeBackend(status=BackendStatus(installed=True))
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    commands._write_config_file_keys(str(tmp_path),
                                     schedule_interval_minutes=1440)
    _write_fresh_log(tmp_path, age=timedelta(days=4))
    commands.cmd_setup_schedule(_ns(tmp_path, status_only=True))
    assert "INSTALLED BUT NOT RUNNING" in capsys.readouterr().out


def test_status_survives_broken_backend(tmp_path, monkeypatch, capsys):
    class Exploding(FakeBackend):
        def status(self, spec=None):
            raise FileNotFoundError("crontab")
    monkeypatch.setattr(commands, "choose_backend", lambda: Exploding())
    rc = commands.cmd_setup_schedule(_ns(tmp_path, status_only=True))
    assert rc == 0
    assert "no scheduled backup entry" in capsys.readouterr().out


# ── --print-systemd (Delta-5) ───────────────────────────────────────────


def test_print_systemd_recipe(tmp_path, fake_backend, capsys):
    rc = commands.cmd_setup_schedule(_ns(tmp_path, print_systemd=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "systemd user timer" in out
    assert "-m claude_session_backup" in out
    assert "enable-linger" in out
    assert "install" not in fake_backend.calls


# ── Ambient surfaces: csb status line (AC-15) + checklist row (AC-18) ──


def test_cmd_status_carries_ambient_schedule_line(tmp_path, monkeypatch,
                                                  capsys):
    backend = FakeBackend()
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    rc = commands.cmd_status(argparse.Namespace(
        claude_dir=str(tmp_path), db=None, quiet=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Scheduled:" in out
    assert "csb setup schedule" in out


def test_setup_checklist_row_todo_then_done(tmp_path, monkeypatch, capsys):
    config = {"claude_dir": str(tmp_path),
              "index_path": str(tmp_path / "session-backup.db")}
    backend = FakeBackend()
    monkeypatch.setattr(commands, "choose_backend", lambda: backend)
    commands._setup_checklist(config, str(tmp_path), "repo note")
    out = capsys.readouterr().out
    assert "scheduled backup" in out and "csb setup schedule" in out

    backend._status = BackendStatus(installed=True)
    commands._setup_checklist(config, str(tmp_path), "repo note")
    out = capsys.readouterr().out
    assert "OS schedule installed" in out

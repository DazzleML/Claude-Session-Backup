"""`csb setup update` + plugin drift detection (#75, U1-U6).

The plugin delivers the hook scripts; staleness silently withholds
hook-side features (lived twice this week). Detection is filesystem-
only (U2); drift reports on explicit surfaces, never nags (U3); the
wrapper degrades honestly with exact manual commands (U4); the spec
incantation is defined exactly once (U5).
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import claude_session_backup.cli as cli
import claude_session_backup.commands as commands_module
from claude_session_backup._version import get_base_version
from claude_session_backup.commands import installed_plugin_version
from claude_session_backup.pathkit import ClaudePaths


def _cache(claude_dir, *versions):
    base = ClaudePaths.from_dir(claude_dir).plugin_cache
    for v in versions:
        (base / v).mkdir(parents=True, exist_ok=True)
    return base


class TestDetection:
    def test_missing_tree_is_none_not_a_warning(self, tmp_path):
        assert installed_plugin_version(tmp_path) is None

    def test_numeric_max_wins(self, tmp_path):
        """0.9.10 beats 0.9.2 -- numeric, not lexical."""
        _cache(tmp_path, "0.9.2", "0.9.10", "0.8.4")
        assert installed_plugin_version(tmp_path) == "0.9.10"

    def test_garbage_dirs_skipped(self, tmp_path):
        cache = _cache(tmp_path, "0.9.2")
        (cache / "not-a-version").mkdir()
        (cache / "0.9.x").mkdir()
        assert installed_plugin_version(tmp_path) == "0.9.2"


class TestStatusDriftLine:
    def _status(self, tmp_path, capsys):
        db = tmp_path / "s.db"
        assert cli.main(["status", "--claude-dir", str(tmp_path),
                         "--db", str(db)]) == 0
        return capsys.readouterr().out

    def test_stale_plugin_names_the_fix(self, tmp_path, capsys):
        """RED-GREEN anchor: the drift that sat invisible for three
        releases now has a status line."""
        _cache(tmp_path, "0.8.4")
        out = self._status(tmp_path, capsys)
        assert "0.8.4 installed" in out
        assert "csb setup update" in out

    def test_current_plugin_is_green_calm(self, tmp_path, capsys):
        _cache(tmp_path, get_base_version())
        out = self._status(tmp_path, capsys)
        assert f"{get_base_version()} (current)" in out
        assert "csb setup update" not in out

    def test_absent_plugin_is_calm_not_a_warning(self, tmp_path, capsys):
        out = self._status(tmp_path, capsys)
        assert "not installed (optional" in out
        assert "csb setup update" not in out


class TestWrapper:
    def _run(self, tmp_path, *argv):
        return cli.main(["setup", "update", "--claude-dir", str(tmp_path),
                         "--db", str(tmp_path / "s.db")] + list(argv))

    def test_not_installed_prints_both_install_commands(self, tmp_path,
                                                        capsys):
        assert self._run(tmp_path) == 3
        err = capsys.readouterr().err
        assert "claude plugin marketplace add" in err
        assert f"claude plugin install {ClaudePaths.PLUGIN_SPEC}" in err

    def test_no_claude_cli_prints_the_manual_command(self, tmp_path,
                                                     capsys, monkeypatch):
        _cache(tmp_path, "0.9.2")
        monkeypatch.setattr(commands_module.shutil, "which",
                            lambda name: None)
        assert self._run(tmp_path) == 3
        err = capsys.readouterr().err
        assert f"claude plugin update {ClaudePaths.PLUGIN_SPEC}" in err

    def test_success_invokes_exactly_the_spec(self, tmp_path, capsys,
                                              monkeypatch):
        _cache(tmp_path, "0.9.2")
        monkeypatch.setattr(commands_module.shutil, "which",
                            lambda name: "C:\\bin\\claude.exe")
        run_mock = MagicMock(return_value=SimpleNamespace(
            returncode=0, stdout="", stderr=""))
        monkeypatch.setattr(commands_module.subprocess, "run", run_mock)
        assert self._run(tmp_path) == 0
        assert run_mock.call_args[0][0] == [
            "C:\\bin\\claude.exe", "plugin", "update",
            ClaudePaths.PLUGIN_SPEC]
        out_err = capsys.readouterr()
        assert "Already current" in out_err.out  # cache unchanged by mock
        assert "No session restarts needed" in out_err.err

    def test_cli_failure_propagates_honestly(self, tmp_path, capsys,
                                             monkeypatch):
        _cache(tmp_path, "0.9.2")
        monkeypatch.setattr(commands_module.shutil, "which",
                            lambda name: "claude")
        monkeypatch.setattr(
            commands_module.subprocess, "run",
            MagicMock(return_value=SimpleNamespace(
                returncode=7, stdout="", stderr="")))
        assert self._run(tmp_path) == 7
        assert "exited 7" in capsys.readouterr().err


class TestSetupChecklistDriftRow:
    """The AC names TWO drift surfaces: `csb status` and the setup
    checklist. An installed-but-stale plugin withholds hook-side fixes
    just as surely as a missing one, so the checklist row goes [ ]."""

    def _registries(self, claude_dir):
        base = Path(claude_dir) / "plugins"
        base.mkdir(parents=True, exist_ok=True)
        (base / "known_marketplaces.json").write_text(json.dumps(
            {"dazzle-claude-session-backup":
                {"source": {"repo": "DazzleML/Claude-Session-Backup"}}}),
            encoding="utf-8")
        (base / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {
                "claude-session-backup@dazzle-claude-session-backup":
                    [{"scope": "user", "version": "0.6.0"}]}}),
            encoding="utf-8")

    def _checklist(self, tmp_path, monkeypatch, capsys):
        def _no_backend():
            raise RuntimeError("no scheduler probe in tests")
        monkeypatch.setattr(commands_module, "choose_backend", _no_backend)
        config = {"claude_dir": str(tmp_path),
                  "index_path": str(tmp_path / "s.db")}
        commands_module._setup_checklist(config, str(tmp_path), "repo note")
        return capsys.readouterr().out

    def test_stale_plugin_row_points_at_setup_update(self, tmp_path,
                                                     monkeypatch, capsys):
        self._registries(tmp_path)
        _cache(tmp_path, "0.8.4")
        out = self._checklist(tmp_path, monkeypatch, capsys)
        assert "[ ] auto-backup plugin" in out
        assert "0.8.4 installed" in out
        assert "csb setup update" in out

    def test_current_plugin_row_stays_done(self, tmp_path, monkeypatch,
                                           capsys):
        self._registries(tmp_path)
        _cache(tmp_path, get_base_version())
        out = self._checklist(tmp_path, monkeypatch, capsys)
        assert "[x] auto-backup plugin" in out
        assert "csb setup update" not in out

    def test_no_cache_tree_stays_done(self, tmp_path, monkeypatch, capsys):
        """Registered but no readable cache -> tolerant done row, never a
        false drift (detection is filesystem-only and best-effort, U2)."""
        self._registries(tmp_path)
        out = self._checklist(tmp_path, monkeypatch, capsys)
        assert "[x] auto-backup plugin" in out


class TestSpecSingleDefinition:
    def test_the_incantation_lives_in_pathkit_only(self):
        """U5: the spec is derived in ONE place; commands.py never
        hardcodes the marketplace string."""
        assert ClaudePaths.PLUGIN_SPEC == \
            "claude-session-backup@dazzle-claude-session-backup"
        import inspect

        source = inspect.getsource(commands_module)
        assert "dazzle-claude-session-backup" not in source

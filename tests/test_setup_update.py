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


def _write_registry(claude_dir, version, raw=None):
    """Claude Code's installed_plugins.json -- the authoritative record
    of which plugin version actually loads. ``raw`` writes arbitrary
    bytes instead (malformed / BOM cases)."""
    base = Path(claude_dir) / "plugins"
    base.mkdir(parents=True, exist_ok=True)
    path = base / "installed_plugins.json"
    if raw is not None:
        path.write_bytes(raw)
        return path
    path.write_text(json.dumps(
        {"version": 2, "plugins": {
            ClaudePaths.PLUGIN_SPEC: [
                {"scope": "user", "version": version}]}}),
        encoding="utf-8")
    return path


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

    def test_registry_version_wins_over_cache_max(self, tmp_path):
        """RED-GREEN anchor: what LOADS beats what was downloaded.

        The cache keeps every version ever fetched and prunes none, so
        a newer directory proves only that it was downloaded once.
        """
        _cache(tmp_path, "0.9.9", "0.9.10")
        _write_registry(tmp_path, "0.9.9")
        assert installed_plugin_version(tmp_path) == "0.9.9"

    def test_unreadable_registry_falls_back_to_the_cache(self, tmp_path):
        """The cache scan is retained as the fallback, not deleted --
        a malformed registry must not blind detection entirely."""
        _cache(tmp_path, "0.9.2")
        _write_registry(tmp_path, None, raw=b"{ this is not json")
        assert installed_plugin_version(tmp_path) == "0.9.2"

    def test_registry_without_a_version_falls_back(self, tmp_path):
        _cache(tmp_path, "0.9.2")
        _write_registry(tmp_path, None)
        assert installed_plugin_version(tmp_path) == "0.9.2"

    def test_multiple_records_take_the_first_with_a_version(self, tmp_path):
        """Pins the documented limitation rather than a guarantee.

        Claude Code's registry format permits several records per
        plugin (a user scope and a project scope). No multi-record
        entry has been observed in the wild, and Claude Code's own
        precedence between scopes is unknown -- so csb takes the first
        record carrying a version and does NOT invent a scope
        preference. This test exists so that changing that becomes a
        deliberate act rather than an accident.
        """
        base = Path(tmp_path) / "plugins"
        base.mkdir(parents=True, exist_ok=True)
        (base / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {ClaudePaths.PLUGIN_SPEC: [
                {"scope": "user", "version": "0.9.9"},
                {"scope": "project", "version": "0.9.10"}]}}),
            encoding="utf-8")
        assert installed_plugin_version(tmp_path) == "0.9.9"

    def test_registry_with_a_bom_is_still_read(self, tmp_path):
        """utf-8-sig tolerance (the #53 sin) applies to the version
        read too, not just the installed/not-installed bool."""
        payload = json.dumps(
            {"version": 2, "plugins": {
                ClaudePaths.PLUGIN_SPEC: [{"version": "0.9.9"}]}})
        _write_registry(tmp_path, None,
                        raw=b"\xef\xbb\xbf" + payload.encode("utf-8"))
        assert installed_plugin_version(tmp_path) == "0.9.9"


class TestShippedManifestVersions:
    """The plugin manifests must ship the same version as the CLI.

    `sync-versions.py` updates `_version.py` and nothing else, so the
    two `.claude-plugin` manifests are bumped by hand every release --
    a step that is easy to miss and silent when missed. The cost of
    missing it is specifically the drift this module exists to report:
    the marketplace would advertise the previous version, `csb setup
    update` would fetch nothing, and every user would sit in a
    permanent drift row that no command they run can clear.

    A script would only help someone who remembered to run it. This
    fails in CI whether they remembered or not.
    """

    MANIFESTS = ("marketplace.json", "plugin.json")

    def _repo_root(self):
        return Path(__file__).resolve().parent.parent

    def test_plugin_manifests_match_the_cli_version(self):
        root = self._repo_root() / ".claude-plugin"
        if not root.is_dir():                      # sdist / wheel install
            pytest.skip("no .claude-plugin directory in this tree")
        expected = get_base_version()
        for name in self.MANIFESTS:
            path = root / name
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            found = self._versions(data)
            assert found, f"{name}: no version field found"
            for value in found:
                assert value == expected, (
                    f"{name} declares {value!r} but the CLI is "
                    f"{expected!r} -- bump the manifests with the release, "
                    f"or the marketplace advertises a version csb will "
                    f"then report as drift no user can clear")

    def _versions(self, node):
        """Every "version" value at any depth (marketplace.json nests one
        per plugin alongside its own)."""
        out = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "version" and isinstance(value, str):
                    out.append(value)
                else:
                    out.extend(self._versions(value))
        elif isinstance(node, list):
            for item in node:
                out.extend(self._versions(item))
        return out


class TestVersionRanking:
    """`plugin_drift` ranking rules, exercised directly."""

    def test_equal_value_different_width_is_not_drift(self):
        """RED-GREEN anchor: "0.9" and "0.9.0" are one version written
        two ways. Raw tuple comparison ranks (0, 9, 0) above (0, 9),
        which reported drift -- and prescribed `pip install -U` -- for
        a version against itself. Found by a probe, not by review."""
        assert commands_module.plugin_drift("0.9.0", "0.9") == ("same", None)
        assert commands_module.plugin_drift("0.9", "0.9.0") == ("same", None)

    def test_ordering_is_numeric_not_lexical(self):
        state, remedy = commands_module.plugin_drift("0.9.9", "0.9.10")
        assert state == "behind"          # lexically "0.9.9" > "0.9.10"
        assert remedy == "csb setup update"

    def test_direction_reverses_with_the_pair(self):
        assert commands_module.plugin_drift("0.9.10", "0.9.9")[0] == "ahead"

    def test_unrankable_side_yields_unordered_and_no_remedy(self):
        assert commands_module.plugin_drift("0.9.10b1", "0.9.10") == (
            "unordered", None)
        assert commands_module.plugin_drift("0.9.10", "0.9.10b1") == (
            "unordered", None)

    def test_absent_is_its_own_state(self):
        assert commands_module.plugin_drift(None, "0.9.10") == ("absent", None)

    def test_empty_version_is_unordered_not_absent(self):
        """Mutation survivor M4, closed: `absent` and "unreadable" are
        different states and must not collapse.

        `None` means no plugin is installed -- `csb status` answers
        that with a calm dim "not installed (optional)". An empty
        version string means a plugin IS recorded but its version
        cannot be read, which is drift of unknown direction. Reporting
        the second as the first would tell someone with a broken
        install that they have no plugin at all.
        """
        assert commands_module.plugin_drift("", "0.9.10") == (
            "unordered", None)


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

    def test_matching_plugin_claims_agreement_not_currency(self, tmp_path,
                                                           capsys):
        """RED-GREEN anchor: plugin==CLI proves the two halves agree.
        It does NOT prove either is current with what exists upstream,
        and nothing on this path looked -- so the word "current" is an
        overclaim of exactly the kind the tier ladder exists to avoid.
        """
        _cache(tmp_path, get_base_version())
        out = self._status(tmp_path, capsys)
        assert f"{get_base_version()} (matches CLI)" in out
        assert "current" not in out
        assert "csb setup update" not in out

    def test_plugin_ahead_names_the_cli_upgrade(self, tmp_path, capsys):
        """RED-GREEN anchor: drift has two directions and only one is
        fixed by updating the plugin."""
        _write_registry(tmp_path, "99.0.0")
        out = self._status(tmp_path, capsys)
        assert "99.0.0 installed" in out
        assert "pip install -U claude-session-backup" in out
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
        # The mock leaves the cache at 0.9.2, so the update ran cleanly
        # and changed nothing while a gap remained -- rc 1, and the
        # marketplace is named as the stale link. "Already current"
        # here was the closed loop: a reassuring word and rc 0 while
        # the checklist stayed unticked.
        assert self._run(tmp_path) == 1
        assert run_mock.call_args[0][0] == [
            "C:\\bin\\claude.exe", "plugin", "update",
            ClaudePaths.PLUGIN_SPEC]
        out_err = capsys.readouterr()
        assert "No change: still 0.9.2" in out_err.out
        assert "current" not in out_err.out
        assert "claude plugin marketplace add" in out_err.err

    def test_update_that_lands_reports_the_transition(self, tmp_path,
                                                      capsys, monkeypatch):
        """The genuine success path: the cache gains the new version
        while `claude plugin update` runs, so csb names the move."""
        _cache(tmp_path, "0.9.2")
        monkeypatch.setattr(commands_module.shutil, "which",
                            lambda name: "claude")

        def _fake_update(*_a, **_kw):
            _cache(tmp_path, get_base_version())
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(commands_module.subprocess, "run", _fake_update)
        assert self._run(tmp_path) == 0
        out_err = capsys.readouterr()
        assert f"Updated: 0.9.2 -> {get_base_version()}" in out_err.out
        assert "No session restarts needed" in out_err.err

    def test_plugin_ahead_short_circuits_before_calling_claude(
            self, tmp_path, capsys, monkeypatch):
        """RED-GREEN anchor: with the plugin ahead, a plugin update is
        a no-op that would exit 0 and leave the row unticked. Refuse
        early and name the half that is actually stale."""
        _write_registry(tmp_path, "99.0.0")
        run_mock = MagicMock()
        monkeypatch.setattr(commands_module.shutil, "which",
                            lambda name: "claude")
        monkeypatch.setattr(commands_module.subprocess, "run", run_mock)
        assert self._run(tmp_path) == 3
        run_mock.assert_not_called()
        out_err = capsys.readouterr()
        assert "already ahead of this csb" in out_err.out
        assert "pip install -U claude-session-backup" in out_err.err

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

    def _registries(self, claude_dir, version="0.6.0"):
        """Claude Code's registries. ``version=None`` omits the recorded
        version, which is how a registry that cannot answer "what is
        loaded?" looks -- the U2 tolerance case."""
        base = Path(claude_dir) / "plugins"
        base.mkdir(parents=True, exist_ok=True)
        (base / "known_marketplaces.json").write_text(json.dumps(
            {"dazzle-claude-session-backup":
                {"source": {"repo": "DazzleML/Claude-Session-Backup"}}}),
            encoding="utf-8")
        record = {"scope": "user"}
        if version is not None:
            record["version"] = version
        (base / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {
                "claude-session-backup@dazzle-claude-session-backup":
                    [record]}}),
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
        self._registries(tmp_path, "0.8.4")
        _cache(tmp_path, "0.8.4")
        out = self._checklist(tmp_path, monkeypatch, capsys)
        assert "[ ] auto-backup plugin" in out
        assert "0.8.4 installed" in out
        assert "csb setup update" in out

    def test_current_plugin_row_stays_done(self, tmp_path, monkeypatch,
                                           capsys):
        self._registries(tmp_path, get_base_version())
        _cache(tmp_path, get_base_version())
        out = self._checklist(tmp_path, monkeypatch, capsys)
        assert "[x] auto-backup plugin" in out
        assert "csb setup update" not in out

    def test_neither_source_knows_stays_done(self, tmp_path, monkeypatch,
                                             capsys):
        """No cache tree AND no recorded version -> tolerant done row,
        never a false drift (detection is best-effort, U2).

        Renamed from test_no_cache_tree_stays_done: an absent cache no
        longer means absent knowledge, because the registry is now
        asked first. The principle is unchanged -- when NOTHING can
        answer, say nothing -- so the fixture withholds both sources.
        """
        self._registries(tmp_path, version=None)
        out = self._checklist(tmp_path, monkeypatch, capsys)
        assert "[x] auto-backup plugin" in out

    def test_registry_beats_a_newer_cache_directory(self, tmp_path,
                                                    monkeypatch, capsys):
        """RED-GREEN anchor: a newer version sitting in the cache does
        NOT make csb call the older ACTIVE plugin current.

        The cache accumulates every version ever fetched and prunes
        none, so its numeric max answers "what was downloaded". Claude
        Code loads what its registry names, and those hook scripts are
        what actually fire.
        """
        self._registries(tmp_path, "0.9.9")
        _cache(tmp_path, "0.9.9", get_base_version())
        out = self._checklist(tmp_path, monkeypatch, capsys)
        assert "[ ] auto-backup plugin" in out
        assert "0.9.9 installed" in out

    def test_plugin_ahead_points_at_the_cli_not_the_plugin(
            self, tmp_path, monkeypatch, capsys):
        """RED-GREEN anchor: when the CLI is the stale half, say so.

        `csb setup update` here is a closed loop -- the plugin update
        finds nothing, exits 0, and the row stays unticked forever.
        """
        self._registries(tmp_path, "99.0.0")
        out = self._checklist(tmp_path, monkeypatch, capsys)
        assert "[ ] auto-backup plugin" in out
        assert "pip install -U claude-session-backup" in out
        assert "csb setup update" not in out

    def test_unrankable_pair_names_no_remedy(self, tmp_path, monkeypatch,
                                             capsys):
        """A pre-release cannot be ordered against a stable release by
        the tuple rule, so csb reports the pair and guesses no
        direction -- "these differ" is provable, "yours is older" is
        not."""
        self._registries(tmp_path, "0.9.10b1")
        out = self._checklist(tmp_path, monkeypatch, capsys)
        assert "[ ] auto-backup plugin" in out
        assert "0.9.10b1 installed" in out
        assert "pip install -U" not in out
        assert "csb setup update" not in out


class TestSpecSingleDefinition:
    def test_the_incantation_lives_in_pathkit_only(self):
        """U5: the spec is derived in ONE place; commands.py never
        hardcodes the marketplace string."""
        assert ClaudePaths.PLUGIN_SPEC == \
            "claude-session-backup@dazzle-claude-session-backup"
        import inspect

        source = inspect.getsource(commands_module)
        assert "dazzle-claude-session-backup" not in source

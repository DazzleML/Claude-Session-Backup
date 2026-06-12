"""Tests for configuration handling -- csb's own config plus the Claude Code
``settings:`` passthrough that exposes the session purge TTL
(``cleanupPeriodDays``).

Two layers are covered:
  * config.py pure helpers (read/write/validate against settings.json)
  * the cmd_config dispatcher end-to-end via cli.main(), so the ``settings:``
    namespace routing, the --force guard, and exit codes are exercised the way
    a user hits them.
"""

import json
from pathlib import Path

import pytest

import claude_session_backup.cli as cli
from claude_session_backup import config


# ── helpers ─────────────────────────────────────────────────────────

def _claude_dir(tmp_path, settings=None):
    """Create a temp .claude dir, optionally seeding settings.json."""
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        (claude / "settings.json").write_text(
            json.dumps(settings, indent=2) + "\n", encoding="utf-8"
        )
    return claude


def _settings(claude_dir):
    p = Path(claude_dir) / "settings.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _run(argv, claude_dir):
    """Drive the real CLI; common --claude-dir is hoisted by main()."""
    return cli.main(list(argv) + ["--claude-dir", str(claude_dir)])


# ── get_settings_path ───────────────────────────────────────────────

def test_get_settings_path_points_at_settings_json(tmp_path):
    claude = _claude_dir(tmp_path)
    assert config.get_settings_path(str(claude)) == claude / "settings.json"


def test_get_settings_path_expands_user():
    p = config.get_settings_path("~/.claude")
    assert "~" not in str(p)
    assert p.name == "settings.json"


# ── read_claude_setting ─────────────────────────────────────────────

def test_read_claude_setting_present(tmp_path):
    claude = _claude_dir(tmp_path, {"cleanupPeriodDays": 90})
    value, present = config.read_claude_setting(str(claude), "cleanupPeriodDays")
    assert (value, present) == (90, True)


def test_read_claude_setting_absent_key(tmp_path):
    claude = _claude_dir(tmp_path, {"model": "opus"})
    value, present = config.read_claude_setting(str(claude), "cleanupPeriodDays")
    assert (value, present) == (None, False)


def test_read_claude_setting_missing_file(tmp_path):
    claude = _claude_dir(tmp_path)  # no settings.json
    assert config.read_claude_setting(str(claude), "cleanupPeriodDays") == (None, False)


def test_read_claude_setting_malformed_returns_absent(tmp_path):
    claude = _claude_dir(tmp_path)
    (claude / "settings.json").write_text("{ not valid json", encoding="utf-8")
    assert config.read_claude_setting(str(claude), "cleanupPeriodDays") == (None, False)


# ── write_claude_setting ────────────────────────────────────────────

def test_write_claude_setting_creates_new_file(tmp_path):
    claude = _claude_dir(tmp_path)
    path = config.write_claude_setting(str(claude), "cleanupPeriodDays", 365)
    assert Path(path).exists()
    assert _settings(claude) == {"cleanupPeriodDays": 365}


def test_write_claude_setting_preserves_other_keys(tmp_path):
    claude = _claude_dir(tmp_path, {
        "cleanupPeriodDays": 90,
        "permissions": {"allow": ["Read", "Glob"]},
        "model": "opus",
    })
    config.write_claude_setting(str(claude), "cleanupPeriodDays", 365)
    data = _settings(claude)
    assert data["cleanupPeriodDays"] == 365
    assert data["permissions"] == {"allow": ["Read", "Glob"]}
    assert data["model"] == "opus"


def test_write_claude_setting_trailing_newline_and_indent(tmp_path):
    claude = _claude_dir(tmp_path)
    config.write_claude_setting(str(claude), "cleanupPeriodDays", 42)
    raw = (claude / "settings.json").read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert "\n  " in raw  # 2-space indented


def test_write_claude_setting_uses_lf_not_crlf(tmp_path):
    """Claude Code writes settings.json with LF; csb must too. On Windows a
    text-mode write would translate \\n -> \\r\\n and rewrite every line,
    flooding the ~/.claude git repo with phantom CRLF diffs. Read raw BYTES
    (read_text would normalize and hide a regression)."""
    claude = _claude_dir(tmp_path)
    config.write_claude_setting(str(claude), "cleanupPeriodDays", 42)
    raw = (claude / "settings.json").read_bytes()
    assert b"\r\n" not in raw
    assert raw.count(b"\n") >= 2  # actually multi-line, not all on one line


def test_write_claude_setting_roundtrip_byte_identical(tmp_path):
    """A settings.json already in Claude Code's canonical form (LF, 2-space,
    trailing newline) must survive a value change + change-back byte-for-byte."""
    claude = _claude_dir(tmp_path)
    canonical = json.dumps(
        {"cleanupPeriodDays": 90, "permissions": {"allow": ["Read"]}}, indent=2
    ) + "\n"
    (claude / "settings.json").write_bytes(canonical.encode("utf-8"))
    original = (claude / "settings.json").read_bytes()
    config.write_claude_setting(str(claude), "cleanupPeriodDays", 100)
    config.write_claude_setting(str(claude), "cleanupPeriodDays", 90)
    assert (claude / "settings.json").read_bytes() == original


def test_write_claude_setting_refuses_malformed_existing(tmp_path):
    claude = _claude_dir(tmp_path)
    broken = "{ this is not json"
    (claude / "settings.json").write_text(broken, encoding="utf-8")
    with pytest.raises(ValueError):
        config.write_claude_setting(str(claude), "cleanupPeriodDays", 365)
    # The broken file must be left untouched, not clobbered.
    assert (claude / "settings.json").read_text(encoding="utf-8") == broken


def test_write_claude_setting_refuses_non_object_json(tmp_path):
    claude = _claude_dir(tmp_path)
    (claude / "settings.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        config.write_claude_setting(str(claude), "cleanupPeriodDays", 365)


# ── validate_cleanup_period ─────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("90", 90),
    (" 90 ", 90),
    ("0", 0),
    ("36500", 36500),
])
def test_validate_cleanup_period_valid(raw, expected):
    value, error = config.validate_cleanup_period(raw)
    assert error is None
    assert value == expected


@pytest.mark.parametrize("raw", ["-5", "abc", "1.5", "", "9e9"])
def test_validate_cleanup_period_invalid(raw):
    value, error = config.validate_cleanup_period(raw)
    assert value is None
    assert error


# ── read_cleanup_period (display convenience) ───────────────────────

def test_read_cleanup_period_present(tmp_path):
    claude = _claude_dir(tmp_path, {"cleanupPeriodDays": 90})
    assert config.read_cleanup_period(str(claude)) == 90


def test_read_cleanup_period_absent_defaults_30(tmp_path):
    claude = _claude_dir(tmp_path, {"model": "opus"})
    assert config.read_cleanup_period(str(claude)) == config.DEFAULT_CLEANUP_PERIOD_DAYS


def test_read_cleanup_period_zero_falls_back_to_default(tmp_path):
    # csb's display convenience treats a literal 0 as the 30-day default
    # (Claude Code itself would purge everything; there's nothing to count down).
    claude = _claude_dir(tmp_path, {"cleanupPeriodDays": 0})
    assert config.read_cleanup_period(str(claude)) == config.DEFAULT_CLEANUP_PERIOD_DAYS


def test_read_cleanup_period_roundtrip_with_write(tmp_path):
    claude = _claude_dir(tmp_path)
    config.write_claude_setting(str(claude), "cleanupPeriodDays", 365)
    assert config.read_cleanup_period(str(claude)) == 365


# ── cmd_config: settings: passthrough via cli.main ──────────────────

def test_config_get_present(tmp_path, capsys):
    claude = _claude_dir(tmp_path, {"cleanupPeriodDays": 90})
    rc = _run(["config", "settings:cleanupPeriodDays"], claude)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip().startswith("90")  # value is the leading token (parseable)


def test_config_get_absent_shows_default(tmp_path, capsys):
    claude = _claude_dir(tmp_path)  # no settings.json
    rc = _run(["config", "settings:cleanupPeriodDays"], claude)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip().startswith(str(config.DEFAULT_CLEANUP_PERIOD_DAYS))
    assert "default" in out


def test_config_set_valid_writes_and_preserves(tmp_path, capsys):
    claude = _claude_dir(tmp_path, {
        "cleanupPeriodDays": 90,
        "permissions": {"allow": ["Read"]},
    })
    rc = _run(["config", "settings:cleanupPeriodDays", "365"], claude)
    assert rc == 0
    data = _settings(claude)
    assert data["cleanupPeriodDays"] == 365
    assert data["permissions"] == {"allow": ["Read"]}


def test_config_set_zero_without_force_refuses(tmp_path, capsys):
    claude = _claude_dir(tmp_path, {"cleanupPeriodDays": 90})
    rc = _run(["config", "settings:cleanupPeriodDays", "0"], claude)
    err = capsys.readouterr().err
    assert rc == 2
    assert "--force" in err
    # File must be unchanged.
    assert _settings(claude)["cleanupPeriodDays"] == 90


def test_config_set_zero_with_force_writes(tmp_path, capsys):
    claude = _claude_dir(tmp_path, {"cleanupPeriodDays": 90})
    rc = _run(["config", "settings:cleanupPeriodDays", "0", "--force"], claude)
    assert rc == 0
    assert _settings(claude)["cleanupPeriodDays"] == 0


def test_config_set_negative_rejected(tmp_path, capsys):
    claude = _claude_dir(tmp_path, {"cleanupPeriodDays": 90})
    rc = _run(["config", "settings:cleanupPeriodDays", "-5"], claude)
    assert rc == 2
    assert _settings(claude)["cleanupPeriodDays"] == 90


def test_config_set_non_int_rejected(tmp_path, capsys):
    claude = _claude_dir(tmp_path, {"cleanupPeriodDays": 90})
    rc = _run(["config", "settings:cleanupPeriodDays", "abc"], claude)
    assert rc == 2


def test_config_set_refuses_malformed_settings(tmp_path, capsys):
    claude = _claude_dir(tmp_path)
    broken = "{ broken"
    (claude / "settings.json").write_text(broken, encoding="utf-8")
    rc = _run(["config", "settings:cleanupPeriodDays", "365"], claude)
    assert rc == 1
    assert (claude / "settings.json").read_text(encoding="utf-8") == broken


def test_config_bare_known_key_hints_namespace(tmp_path, capsys):
    claude = _claude_dir(tmp_path, {"cleanupPeriodDays": 90})
    rc = _run(["config", "cleanupPeriodDays"], claude)
    err = capsys.readouterr().err
    assert rc == 1
    assert "settings:cleanupPeriodDays" in err


def test_config_unknown_settings_key(tmp_path, capsys):
    claude = _claude_dir(tmp_path, {"cleanupPeriodDays": 90})
    rc = _run(["config", "settings:bogusKey"], claude)
    assert rc == 1


def test_config_csb_own_key_unaffected(tmp_path, capsys):
    """Setting a bare key writes csb's own config, never settings.json."""
    claude = _claude_dir(tmp_path, {"cleanupPeriodDays": 90})
    rc = _run(["config", "display_top_folders", "5"], claude)
    assert rc == 0
    own = json.loads((claude / "session-backup-config.json").read_text(encoding="utf-8"))
    assert own["display_top_folders"] == 5
    # csb's key must not leak into Claude Code's settings.json.
    assert "display_top_folders" not in _settings(claude)


def test_config_bare_dump_is_pure_json(tmp_path, capsys):
    """`csb config` (no key) emits valid JSON on stdout; the settings: hint
    goes to stderr so stdout stays pipeable."""
    claude = _claude_dir(tmp_path, {"cleanupPeriodDays": 90})
    rc = _run(["config"], claude)
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)  # raises if stdout isn't pure JSON
    assert "display_top_folders" in parsed
    assert "settings:cleanupPeriodDays" in captured.err



# == #45: CLAUDE_CONFIG_DIR -- follow Claude Code's own directory relocation ==


@pytest.fixture
def _no_dir_env(monkeypatch):
    """Baseline: neither csb's CLAUDE_DIR nor Claude Code's
    CLAUDE_CONFIG_DIR set (the developer machine may have either)."""
    monkeypatch.delenv("CLAUDE_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_BACKUP_DB", raising=False)


def test_claude_config_dir_alone_relocates_everything(_no_dir_env, monkeypatch, tmp_path):
    """#45: relocating via Claude Code's OWN env var is enough -- csb
    follows with zero configuration, including the DB default."""
    moved = tmp_path / "relocated-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(moved))
    cfg = config.load_config()
    assert cfg["claude_dir"] == str(moved)
    assert Path(cfg["index_path"]) == moved / "session-backup.db"


def test_claude_dir_beats_claude_config_dir(_no_dir_env, monkeypatch, tmp_path):
    """csb's own CLAUDE_DIR is the more specific override."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc-dir"))
    monkeypatch.setenv("CLAUDE_DIR", str(tmp_path / "csb-dir"))
    cfg = config.load_config()
    assert cfg["claude_dir"] == str(tmp_path / "csb-dir")


def test_cli_flag_beats_both_env_vars(_no_dir_env, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc-dir"))
    monkeypatch.setenv("CLAUDE_DIR", str(tmp_path / "csb-dir"))
    cfg = config.load_config(claude_dir=str(tmp_path / "flag-dir"))
    assert cfg["claude_dir"] == str(tmp_path / "flag-dir")
    assert Path(cfg["index_path"]) == tmp_path / "flag-dir" / "session-backup.db"


def test_explicit_db_env_not_overridden_by_relocation(_no_dir_env, monkeypatch, tmp_path):
    """An explicit DB override always wins over the follow-the-dir default."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc-dir"))
    monkeypatch.setenv("CLAUDE_SESSION_BACKUP_DB", str(tmp_path / "elsewhere.db"))
    cfg = config.load_config()
    assert cfg["index_path"] == str(tmp_path / "elsewhere.db")


def test_config_and_settings_paths_honor_claude_config_dir(_no_dir_env, monkeypatch, tmp_path):
    """The chicken-and-egg corner: csb's own config file AND Claude Code's
    settings.json must be read from the relocated dir."""
    moved = tmp_path / "relocated-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(moved))
    assert config.get_config_path() == moved / "session-backup-config.json"
    assert config.get_settings_path() == moved / "settings.json"


def test_default_unchanged_without_relocation(_no_dir_env):
    """Regression pin: no envs, no flag -> classic ~/.claude everywhere."""
    cfg = config.load_config()
    assert cfg["claude_dir"] == "~/.claude"
    assert Path(cfg["index_path"]).name == "session-backup.db"
    assert config.get_config_path() == Path.home() / ".claude" / "session-backup-config.json"

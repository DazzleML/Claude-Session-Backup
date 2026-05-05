"""
Configuration management for claude-session-backup.

Config file location: ~/.claude/session-backup-config.json
"""

import json
import os
from pathlib import Path

DEFAULT_CONFIG = {
    "claude_dir": "~/.claude",
    "index_path": "~/.claude/session-backup.db",
    "auto_commit": True,
    "commit_user_files": True,
    "idle_seconds_before_commit": 5,
}

# Environment variable overrides (CLI flag > env var > config file > default)
ENV_CLAUDE_DIR = "CLAUDE_DIR"
ENV_DB_PATH = "CLAUDE_SESSION_BACKUP_DB"

CONFIG_FILENAME = "session-backup-config.json"


def get_config_path(claude_dir=None):
    """Return the config file path."""
    base = Path(claude_dir).expanduser() if claude_dir else Path.home() / ".claude"
    return base / CONFIG_FILENAME


def load_config(claude_dir=None):
    """
    Load config with priority: CLI flag > env var > config file > default.

    The claude_dir parameter represents a CLI flag override.
    """
    config = dict(DEFAULT_CONFIG)

    # Apply env var overrides before config file (config file can still override)
    env_claude_dir = os.environ.get(ENV_CLAUDE_DIR)
    env_db = os.environ.get(ENV_DB_PATH)
    if env_claude_dir:
        config["claude_dir"] = env_claude_dir
    if env_db:
        config["index_path"] = env_db

    # Load config file (overrides env vars for non-path settings)
    config_path = get_config_path(claude_dir or config["claude_dir"])
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            config.update(user_config)
        except (json.JSONDecodeError, OSError):
            pass  # Use defaults on error

    # CLI flag takes highest priority (applied after config file)
    if claude_dir:
        config["claude_dir"] = claude_dir
    # Re-apply env vars for paths (they beat config file for paths)
    if env_claude_dir and not claude_dir:
        config["claude_dir"] = env_claude_dir
    if env_db:
        config["index_path"] = env_db

    return config


def save_config(config, claude_dir=None):
    """Save config to file."""
    config_path = get_config_path(claude_dir)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def resolve_paths(config):
    """Resolve ~ and relative paths in config to absolute paths."""
    resolved = dict(config)
    resolved["claude_dir"] = str(Path(config["claude_dir"]).expanduser().resolve())
    resolved["index_path"] = str(Path(config["index_path"]).expanduser().resolve())
    return resolved


DEFAULT_CLEANUP_PERIOD_DAYS = 30


def read_cleanup_period(claude_dir: str) -> int:
    """
    Read cleanupPeriodDays from Claude Code's settings.json.

    Returns the configured value, or DEFAULT_CLEANUP_PERIOD_DAYS (30) if not set.
    Returns 0 if cleanup is disabled (value <= 0).
    """
    settings_path = Path(claude_dir) / "settings.json"
    if not settings_path.exists():
        return DEFAULT_CLEANUP_PERIOD_DAYS

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        value = settings.get("cleanupPeriodDays", DEFAULT_CLEANUP_PERIOD_DAYS)
        return int(value) if value else DEFAULT_CLEANUP_PERIOD_DAYS
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return DEFAULT_CLEANUP_PERIOD_DAYS

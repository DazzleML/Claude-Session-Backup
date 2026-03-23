"""
Configuration management for claude-session-backup.

Config file location: ~/.claude/session-backup-config.json
"""

import json
from pathlib import Path

DEFAULT_CONFIG = {
    "claude_dir": "~/.claude",
    "index_path": "~/.claude/session-backup.db",
    "top_n_folders": 3,
    "auto_commit": True,
    "commit_user_files": True,
    "idle_seconds_before_commit": 5,
}

CONFIG_FILENAME = "session-backup-config.json"


def get_config_path(claude_dir=None):
    """Return the config file path."""
    base = Path(claude_dir).expanduser() if claude_dir else Path.home() / ".claude"
    return base / CONFIG_FILENAME


def load_config(claude_dir=None):
    """Load config from file, falling back to defaults."""
    config = dict(DEFAULT_CONFIG)
    config_path = get_config_path(claude_dir)

    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            config.update(user_config)
        except (json.JSONDecodeError, OSError):
            pass  # Use defaults on error

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

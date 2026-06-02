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
    # Renderer default for ``csb list`` / ``csb scan``: how many "other"
    # folder rows display beneath "start at". Override per-invocation
    # with ``--top N`` / ``--all-folders``. Stored as a string in JSON
    # config files but coerced to int on read; ``None`` / negative means
    # "show all" (equivalent to ``--all-folders``).
    "display_top_folders": 3,
    # ``csb status`` lists sessions with un-backed-up changes. When more than
    # this many are found, the list is capped and the rest collapse to
    # "+ N more not shown" -- past this count the actionable signal is "your
    # index is behind, run csb backup", not a wall of ids. Tweak in
    # session-backup-config.json.
    "status_unbacked_limit": 20,
}

# Environment variable overrides (CLI flag > env var > config file > default)
ENV_CLAUDE_DIR = "CLAUDE_DIR"
ENV_DB_PATH = "CLAUDE_SESSION_BACKUP_DB"

CONFIG_FILENAME = "session-backup-config.json"


def get_config_path(claude_dir=None):
    """Return the config file path."""
    base = Path(claude_dir).expanduser() if claude_dir else Path.home() / ".claude"
    return base / CONFIG_FILENAME


def get_settings_path(claude_dir=None):
    """Return the path to Claude Code's own ``settings.json``.

    This is a *different* file from csb's config (``session-backup-config.json``):
    it's where Claude Code keeps ``cleanupPeriodDays`` -- the session purge TTL --
    alongside permissions, hooks, model, and the rest. csb reads it for the
    purge countdown and (via the ``settings:`` namespace in ``csb config``)
    can edit the TTL through it.
    """
    base = Path(claude_dir).expanduser() if claude_dir else Path.home() / ".claude"
    return base / "settings.json"


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
    Read ``cleanupPeriodDays`` from Claude Code's settings.json.

    Returns the configured value, or ``DEFAULT_CLEANUP_PERIOD_DAYS`` (30) when
    the key is unset, zero, or the file is unreadable. (Claude Code itself
    treats a literal ``0`` as "disable session persistence / purge everything",
    but for csb's purge-countdown display we fall back to the 30-day default
    rather than 0.) A negative value is passed through as-is; downstream,
    ``timeline.purge_countdown`` suppresses the countdown for any value <= 0.

    This is a read-only convenience used by the list/scan/search renderers.
    To *view or change* the TTL, use ``csb config settings:cleanupPeriodDays``.
    """
    settings_path = get_settings_path(claude_dir)
    if not settings_path.exists():
        return DEFAULT_CLEANUP_PERIOD_DAYS

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        value = settings.get("cleanupPeriodDays", DEFAULT_CLEANUP_PERIOD_DAYS)
        return int(value) if value else DEFAULT_CLEANUP_PERIOD_DAYS
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return DEFAULT_CLEANUP_PERIOD_DAYS


# ── Claude Code settings passthrough ────────────────────────────────
#
# A few settings that materially affect csb live in Claude Code's OWN
# settings.json -- the purge TTL (``cleanupPeriodDays``) above all. So a config
# key can never be ambiguous between "csb's config" and "Claude Code's config",
# csb addresses the latter through a fully-qualified ``settings:`` namespace in
# ``csb config``:
#
#     csb config cleanupPeriodDays            -> bare key  -> csb's own config
#                                                (session-backup-config.json)
#     csb config settings:cleanupPeriodDays   -> namespaced -> Claude Code's
#                                                settings.json (read)
#     csb config settings:cleanupPeriodDays 365   -> write Claude Code's
#                                                settings.json
#
# A bare key ALWAYS addresses csb's own config; a ``settings:`` key ALWAYS
# addresses Claude Code's settings.json. No overlap, ever.

SETTINGS_NS = "settings:"

#: Claude Code settings.json keys csb knows how to read/write through the
#: ``settings:`` namespace, with the metadata needed to validate and explain
#: each one. Keyed by the bare (un-namespaced) key name.
CLAUDE_SETTINGS_KEYS = {
    "cleanupPeriodDays": {
        "default": DEFAULT_CLEANUP_PERIOD_DAYS,
        "summary": (
            "session purge TTL -- days Claude Code keeps a transcript after "
            "its last use before deleting it"
        ),
    },
}


def is_settings_key(key: str) -> bool:
    """True if ``key`` is namespaced (``settings:...``) for Claude Code's
    settings.json rather than csb's own config."""
    return key.startswith(SETTINGS_NS)


def settings_key_name(key: str) -> str:
    """Strip the ``settings:`` namespace, returning the bare settings.json key."""
    return key[len(SETTINGS_NS):]


def read_claude_setting(claude_dir, key):
    """
    Read a single key from Claude Code's settings.json.

    Returns ``(value, present)``. ``present`` is False when the file is
    missing/unreadable or the key is absent -- the caller supplies the default.
    """
    settings_path = get_settings_path(claude_dir)
    if not settings_path.exists():
        return None, False
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None, False
    if isinstance(data, dict) and key in data:
        return data[key], True
    return None, False


def write_claude_setting(claude_dir, key, value):
    """
    Merge ``{key: value}`` into Claude Code's settings.json, preserving every
    other key and 2-space formatting. Returns the settings.json path.

    Refuses (raises ``ValueError``) to overwrite an existing settings.json that
    is not a valid JSON object -- clobbering it would destroy the user's
    permissions, hooks, and other settings.
    """
    settings_path = get_settings_path(claude_dir)
    data = {}
    if settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise ValueError(
                f"{settings_path} is not valid JSON; refusing to overwrite it "
                f"({e}). Fix the file by hand first."
            )
        if not isinstance(data, dict):
            raise ValueError(
                f"{settings_path} does not contain a JSON object; refusing to "
                f"overwrite it."
            )
    data[key] = value
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" pins LF endings: Claude Code (a Node app) writes settings.json
    # with LF, so matching it keeps the round-trip byte-identical and avoids
    # rewriting every line to CRLF on Windows (which would flood the user's
    # ~/.claude git repo -- the very repo csb backs up -- with phantom diffs).
    with open(settings_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return settings_path


def validate_cleanup_period(raw):
    """
    Parse + range-check a ``cleanupPeriodDays`` value from CLI input.

    Returns ``(value, error)``: on success ``value`` is a non-negative int and
    ``error`` is None; on failure ``value`` is None and ``error`` is a message.

    Note ``0`` is *valid* here but dangerous -- Claude Code treats it as "disable
    session persistence" and deletes all transcripts at next startup -- so the
    caller is expected to gate ``0`` behind an explicit confirmation (--force).
    """
    try:
        value = int(str(raw).strip())
    except (ValueError, TypeError):
        return None, (
            f"cleanupPeriodDays must be a whole number of days (got {raw!r})."
        )
    if value < 0:
        return None, (
            "cleanupPeriodDays must be >= 0 (Claude Code rejects negatives). "
            "To effectively never purge, set a large number instead "
            "(e.g. 36500 = 100 years)."
        )
    return value, None

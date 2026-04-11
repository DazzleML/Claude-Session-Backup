# Contributing to Claude-Session-Backup

Thank you for considering contributing to Claude-Session-Backup!

## Development Setup

### Prerequisites

- **Python 3.10+**
- **Git**
- A `~/.claude/` directory (from Claude Code) initialized as a git repo

### Clone and Install

```bash
git clone https://github.com/DazzleML/Claude-Session-Backup.git
cd Claude-Session-Backup
python -m venv .venv
source .venv/bin/activate        # Linux/Mac
# or: .venv\Scripts\activate     # Windows
pip install -e ".[dev]"
```

### Run Tests

```bash
python -m pytest tests/ -v
```

All tests use `--no-gpg-sign` and disable GPG signing in test git repos to avoid key prompts.

## Project Structure

The project has two layers: the Python CLI (`csb`) and the Claude Code plugin that wraps it.

### CLI (Python package)

```
claude_session_backup/
  cli.py              # CLI entry point (argparse, common-flag hoisting)
  commands.py         # Command implementations
  scanner.py          # Session discovery (prefix match + sanitize_path)
  metadata.py         # JSONL metadata extraction (streaming parser)
  index.py            # SQLite index operations (rebuildable cache)
  git_ops.py          # Git operations (noise/user separation, unsigned commits)
  timeline.py         # Rich terminal formatting, purge countdown
  config.py           # Configuration + settings.json reader
  lockfile.py         # File-based lock for cron safety
  _version.py         # Version (PEP 440)
```

### Plugin (Claude Code marketplace entry)

```
.claude-plugin/
  plugin.json         # Plugin metadata
  marketplace.json    # Marketplace listing
hooks/
  hooks.json          # PreCompact + SessionEnd registrations
  scripts/
    run-hook.mjs      # Node bootstrapper (finds python, spawns the python script)
    backup-hook.py    # Runs `csb --quiet backup`
install.py            # Manual fallback installer
```

The plugin is installed via `claude plugin marketplace add ./` + `claude plugin install claude-session-backup@dazzle-claude-session-backup`. The CLI can also run standalone (cron, manual) without the plugin.

### Tests and scripts

```
tests/
  conftest.py         # Shared fixtures (mock_claude_dir, mock_db)
  test_*.py           # Test files (73 tests across cli, index, scanner, metadata, timeline, lockfile, hook)
scripts/
  sync-versions.py    # Version automation
  gh_issue_full.py    # GitHub issue viewer
```

## Key Design Principles

1. **Git is the source of truth** -- SQLite is a rebuildable cache
2. **Two-commit model** -- noise and user files are always separate commits
3. **No data loss** -- full JSONL preserved, nothing filtered or summarized
4. **Cross-platform** -- works on Windows, Linux, macOS, including hook execution (via Node.js bootstrapper)
5. **Cron-friendly** -- unsigned commits, quiet mode, lock file, proper exit codes
6. **Plugin + CLI separation** -- the CLI works standalone; the plugin wraps it for automatic Claude Code hook execution

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

```
claude_session_backup/
  cli.py              # CLI entry point (argparse)
  commands.py         # Command implementations
  scanner.py          # Session discovery
  metadata.py         # JSONL metadata extraction
  index.py            # SQLite index operations
  git_ops.py          # Git operations (noise/user separation)
  timeline.py         # Rich terminal formatting
  config.py           # Configuration management
  _version.py         # Version (PEP 440)
tests/
  conftest.py         # Shared fixtures (mock_claude_dir, mock_db)
  test_*.py           # Test files
scripts/
  sync-versions.py    # Version automation
  gh_issue_full.py    # GitHub issue viewer
```

## Key Design Principles

1. **Git is the source of truth** -- SQLite is a rebuildable cache
2. **Two-commit model** -- noise and user files are always separate commits
3. **No data loss** -- full JSONL preserved, nothing filtered or summarized
4. **Cross-platform** -- works on Windows, Linux, macOS
5. **Cron-friendly** -- unsigned commits, quiet mode, lock file, proper exit codes

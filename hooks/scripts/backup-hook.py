#!/usr/bin/env python3
"""
Claude Code hook script for claude-session-backup.

Runs `csb backup --quiet` when triggered by PreCompact or SessionEnd hooks.
Handles cross-platform PATH resolution:
  1. Try `csb` directly (if on PATH)
  2. Fall back to `python -m claude_session_backup` (works if pip-installed)

This script is invoked by Claude Code's hook system via hooks.json.
It should complete quickly and silently -- errors go to stderr only.
"""

import shutil
import subprocess
import sys


def find_csb():
    """Find the csb executable, with fallback to module invocation."""
    csb_path = shutil.which("csb")
    if csb_path:
        return [csb_path]

    # Fallback: invoke as Python module using the same Python that runs this script
    return [sys.executable, "-m", "claude_session_backup"]


def main():
    cmd = find_csb() + ["--quiet", "backup"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,  # 2 minute timeout to prevent hanging
        )
        if result.returncode != 0 and result.stderr:
            print(result.stderr, file=sys.stderr, end="")
    except FileNotFoundError:
        print(
            "csb not found. Install with: pip install claude-session-backup",
            file=sys.stderr,
        )
    except subprocess.TimeoutExpired:
        print("csb backup timed out after 120 seconds", file=sys.stderr)
    except Exception as e:
        print(f"csb backup hook error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

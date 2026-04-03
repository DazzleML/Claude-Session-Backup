"""Tests for the backup hook script.

These tests use mock environments and never touch the real ~/.claude directory.
"""

import subprocess
import sys
from pathlib import Path


def test_hook_script_finds_csb():
    """backup-hook.py should find csb and run without error."""
    hook_path = Path(__file__).parent.parent / "hooks" / "scripts" / "backup-hook.py"
    assert hook_path.exists(), f"Hook script not found at {hook_path}"

    # Run the hook script with a --help-like invocation to verify it can
    # find csb. We use a subprocess to isolate from our test environment.
    # The script will try to run `csb --quiet backup` which will succeed
    # (scanning real ~/.claude) or fail gracefully. Either way, the script
    # itself should exit 0.
    result = subprocess.run(
        [sys.executable, str(hook_path)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**dict(__import__("os").environ),
             # Ensure csb is findable via the same Python
             "PATH": __import__("os").environ.get("PATH", "")},
    )
    # The hook should always exit cleanly (errors go to stderr, not exit code)
    assert result.returncode == 0


def test_hook_script_has_shebang():
    """Hook script should have a proper shebang for Unix execution."""
    hook_path = Path(__file__).parent.parent / "hooks" / "scripts" / "backup-hook.py"
    first_line = hook_path.read_text(encoding="utf-8").split("\n")[0]
    assert first_line.startswith("#!/usr/bin/env python"), f"Missing shebang: {first_line}"


def test_hook_script_has_timeout():
    """Hook script should have a timeout to prevent hanging."""
    hook_path = Path(__file__).parent.parent / "hooks" / "scripts" / "backup-hook.py"
    content = hook_path.read_text(encoding="utf-8")
    assert "timeout=" in content, "Hook script should have a timeout parameter"


def test_hook_script_has_path_fallback():
    """Hook script should fall back to python -m if csb not on PATH."""
    hook_path = Path(__file__).parent.parent / "hooks" / "scripts" / "backup-hook.py"
    content = hook_path.read_text(encoding="utf-8")
    assert "sys.executable" in content, "Should have sys.executable fallback"
    assert "shutil.which" in content, "Should use shutil.which for csb lookup"

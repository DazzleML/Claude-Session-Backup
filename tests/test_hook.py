"""Tests for the backup hook script.

These tests use mock environments and never touch the real ~/.claude directory.
"""

import subprocess
import sys
from pathlib import Path


def test_hook_script_runs_cleanly():
    """backup-hook.py should execute end-to-end and exit 0.

    We feed a SessionStart/compact payload so the hook hits the SKIP path --
    this exercises stdin parsing + the decision logic + clean exit WITHOUT
    triggering a real background backup of ~/.claude. (Spawn behavior for
    non-skip inputs is covered with a mocked Popen in test_backup_hook.py.)
    """
    hook_path = Path(__file__).parent.parent / "hooks" / "scripts" / "backup-hook.py"
    assert hook_path.exists(), f"Hook script not found at {hook_path}"

    result = subprocess.run(
        [sys.executable, str(hook_path)],
        input='{"hook_event_name":"SessionStart","source":"compact"}',
        capture_output=True,
        text=True,
        timeout=30,
    )
    # The hook should always exit cleanly (errors go to stderr, not exit code)
    assert result.returncode == 0


def test_hook_script_has_shebang():
    """Hook script should have a proper shebang for Unix execution."""
    hook_path = Path(__file__).parent.parent / "hooks" / "scripts" / "backup-hook.py"
    first_line = hook_path.read_text(encoding="utf-8").split("\n")[0]
    assert first_line.startswith("#!/usr/bin/env python"), f"Missing shebang: {first_line}"


def test_hook_script_backgrounds_backup():
    """v0.3.7: the hook fires the backup in the background (Popen, no wait)
    so the session is never blocked -- it must NOT block on the backup."""
    hook_path = Path(__file__).parent.parent / "hooks" / "scripts" / "backup-hook.py"
    content = hook_path.read_text(encoding="utf-8")
    assert "subprocess.Popen" in content, "Hook should spawn the backup via Popen"
    assert "_should_run_backup" in content, "Hook should have the source-aware decision"
    # The old blocking `subprocess.run(... timeout=120)` wait is gone.
    assert "subprocess.run(" not in content, "Hook should not block on subprocess.run"


def test_hook_script_has_path_fallback():
    """Hook script should fall back to python -m if csb not on PATH."""
    hook_path = Path(__file__).parent.parent / "hooks" / "scripts" / "backup-hook.py"
    content = hook_path.read_text(encoding="utf-8")
    assert "sys.executable" in content, "Should have sys.executable fallback"
    assert "shutil.which" in content, "Should use shutil.which for csb lookup"

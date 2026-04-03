#!/usr/bin/env python3
"""
Manual install for claude-session-backup hooks.

Use this if the Claude Code plugin system isn't available.
Preferred method: claude plugin marketplace add "./"

Usage:
    python install.py           # Install hooks to ~/.claude/
    python install.py --check   # Show what would be installed
    python install.py --force   # Overwrite existing files
    python install.py --remove  # Remove installed hooks
"""

import argparse
import shutil
import sys
from pathlib import Path


def get_claude_dir() -> Path:
    """Get the ~/.claude directory path."""
    return Path.home() / ".claude"


def install(check_only: bool = False, force: bool = False) -> bool:
    """Install hook scripts to ~/.claude/hooks/"""
    script_dir = Path(__file__).parent
    claude_dir = get_claude_dir()

    files = [
        ("hooks/scripts/backup-hook.py", "hooks/backup-hook.py"),
    ]

    print(f"Claude directory: {claude_dir}")
    print()

    if check_only:
        print("Files to install:")
        for src_rel, dst_rel in files:
            dst = claude_dir / dst_rel
            exists = dst.exists()
            status = " (exists, will skip)" if exists and not force else ""
            status = " (exists, will overwrite)" if exists and force else status
            print(f"  {src_rel} -> {dst_rel}{status}")
        print()
        print("Run without --check to install.")
        print()
        print("After installing, add hooks to ~/.claude/settings.json:")
        _print_settings_snippet()
        return True

    # Create directories
    (claude_dir / "hooks").mkdir(parents=True, exist_ok=True)

    installed = 0
    skipped = 0

    for src_rel, dst_rel in files:
        src = script_dir / src_rel
        dst = claude_dir / dst_rel

        if not src.exists():
            print(f"  WARNING: source not found: {src_rel}")
            continue

        if dst.exists() and not force:
            print(f"  SKIP: {dst_rel} (already exists, use --force to overwrite)")
            skipped += 1
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  INSTALLED: {dst_rel}")
        installed += 1

    print()
    print(f"Installed: {installed}, Skipped: {skipped}")

    if installed > 0:
        print()
        print("Next steps: Add hooks to ~/.claude/settings.json:")
        _print_settings_snippet()

    return True


def remove() -> bool:
    """Remove installed hook scripts."""
    claude_dir = get_claude_dir()
    files = ["hooks/backup-hook.py"]

    removed = 0
    for rel in files:
        dst = claude_dir / rel
        if dst.exists():
            dst.unlink()
            print(f"  REMOVED: {rel}")
            removed += 1
        else:
            print(f"  NOT FOUND: {rel}")

    if removed > 0:
        print()
        print("Remember to remove the hook entries from ~/.claude/settings.json")

    return True


def _print_settings_snippet():
    """Print the settings.json hook configuration."""
    # Detect python command for this platform
    python_cmd = "python3" if sys.platform != "win32" else "python"
    hook_path = str(get_claude_dir() / "hooks" / "backup-hook.py")

    print(f"""
{{
  "hooks": {{
    "PreCompact": [
      {{
        "hooks": [
          {{
            "type": "command",
            "command": "{python_cmd} {hook_path}"
          }}
        ]
      }}
    ],
    "SessionEnd": [
      {{
        "hooks": [
          {{
            "type": "command",
            "command": "{python_cmd} {hook_path}"
          }}
        ]
      }}
    ]
  }}
}}""")


def main():
    parser = argparse.ArgumentParser(
        description="Install claude-session-backup hooks to ~/.claude/"
    )
    parser.add_argument("--check", action="store_true", help="Show what would be installed")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    parser.add_argument("--remove", action="store_true", help="Remove installed hooks")
    args = parser.parse_args()

    if args.remove:
        remove()
    else:
        install(check_only=args.check, force=args.force)


if __name__ == "__main__":
    main()

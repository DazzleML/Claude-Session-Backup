"""Allow running claude-session-backup as a module: python -m claude_session_backup"""

from .cli import main

if __name__ == "__main__":
    main()

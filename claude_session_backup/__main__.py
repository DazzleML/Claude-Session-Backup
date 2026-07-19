"""Allow running claude-session-backup as a module: python -m claude_session_backup"""

import sys

from .cli import main

if __name__ == "__main__":
    # Propagate the exit code (#52). A bare main() call always exited 0,
    # which silently broke every rc-sensitive caller of the module form --
    # notably the backup hook's `python -m claude_session_backup _check`
    # fallback, where rc 10 (gap) collapsing to 0 reads as "all backed up".
    sys.exit(main() or 0)

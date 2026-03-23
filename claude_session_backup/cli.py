"""
Command-line interface for claude-session-backup.

Git-backed Claude Code session backup with timeline view, folder analysis,
deletion detection, and session restore.

Usage:
    csb backup                           # scan, index, git commit
    csb list [-n 20] [--deleted]         # timeline view sorted by last-used
    csb status                           # summary of sessions, deletions, git state
    csb show <session-id>                # detailed session info with folder analysis
    csb restore <session-id>             # restore deleted session from git history
    csb search "query"                   # search session metadata
    csb rebuild-index                    # reconstruct SQLite from git history
    csb config [key] [value]             # view/edit configuration
"""

import argparse
import sys

from ._version import DISPLAY_VERSION


def build_parser():
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="csb",
        description="Git-backed Claude Code session backup tool.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {DISPLAY_VERSION}"
    )
    parser.add_argument(
        "--claude-dir",
        default=None,
        help="Path to Claude Code directory (default: ~/.claude)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite index database",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress non-error output (for cron)",
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # backup
    p_backup = sub.add_parser("backup", help="Scan sessions, update index, git commit")
    p_backup.add_argument(
        "--no-commit",
        action="store_true",
        help="Update index but skip git commit",
    )

    # list
    p_list = sub.add_parser("list", help="Timeline view sorted by last-used")
    p_list.add_argument("-n", type=int, default=20, help="Number of sessions to show")
    p_list.add_argument("--deleted", action="store_true", help="Show only deleted sessions")
    p_list.add_argument("--all", action="store_true", help="Show all sessions including deleted")
    p_list.add_argument("--json", action="store_true", help="Output as JSON")

    # status
    sub.add_parser("status", help="Summary of sessions, deletions, git state")

    # show
    p_show = sub.add_parser("show", help="Detailed session info with folder analysis")
    p_show.add_argument("session_id", help="Session ID (prefix match supported)")

    # restore
    p_restore = sub.add_parser("restore", help="Restore deleted session from git history")
    p_restore.add_argument("session_id", help="Session ID to restore")
    p_restore.add_argument("--dry-run", action="store_true", help="Show what would be restored")

    # search
    p_search = sub.add_parser("search", help="Search session metadata")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("-n", type=int, default=10, help="Max results")

    # rebuild-index
    sub.add_parser("rebuild-index", help="Reconstruct SQLite index from git history")

    # config
    p_config = sub.add_parser("config", help="View/edit configuration")
    p_config.add_argument("key", nargs="?", help="Config key to get/set")
    p_config.add_argument("value", nargs="?", help="Value to set")

    return parser


def main(argv=None):
    """Entry point for csb CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    # Import handlers lazily to keep startup fast
    if args.command == "backup":
        from .commands import cmd_backup
        return cmd_backup(args)
    elif args.command == "list":
        from .commands import cmd_list
        return cmd_list(args)
    elif args.command == "status":
        from .commands import cmd_status
        return cmd_status(args)
    elif args.command == "show":
        from .commands import cmd_show
        return cmd_show(args)
    elif args.command == "restore":
        from .commands import cmd_restore
        return cmd_restore(args)
    elif args.command == "search":
        from .commands import cmd_search
        return cmd_search(args)
    elif args.command == "rebuild-index":
        from .commands import cmd_rebuild_index
        return cmd_rebuild_index(args)
    elif args.command == "config":
        from .commands import cmd_config
        return cmd_config(args)

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

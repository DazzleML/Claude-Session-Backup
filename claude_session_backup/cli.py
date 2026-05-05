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
    csb resume <session-id>              # launch claude --resume with full UUID
    csb scan [path]                      # find sessions in current dir and children
    csb search "query"                   # search session metadata
    csb rebuild-index                    # reconstruct SQLite from git history
    csb config [key] [value]             # view/edit configuration
"""

import argparse
import sys

from ._version import DISPLAY_VERSION


# ── Common flags ────────────────────────────────────────────────────
# Flags like --quiet, --claude-dir, --db work in either position:
#   csb --quiet backup       (before subcommand)
#   csb backup --quiet       (after subcommand)
#
# Implementation: only define on subcommand parsers. In main(), do a
# pre-parse of the raw argv to extract any global-position flags and
# inject them into the subcommand's argv before full parsing.

_COMMON_FLAGS = {
    "--quiet": {"short": "-q", "action": "store_true", "default": False,
                "help": "Suppress non-error output (for cron)"},
    "--claude-dir": {"default": None,
                     "help": "Path to Claude Code directory (default: ~/.claude or $CLAUDE_DIR)"},
    "--db": {"default": None,
             "help": "Path to SQLite index database (default: ~/.claude/session-backup.db or $CLAUDE_SESSION_BACKUP_DB)"},
}

# All flag strings that are common (for pre-parse extraction)
_COMMON_FLAG_NAMES = set()
for flag, spec in _COMMON_FLAGS.items():
    _COMMON_FLAG_NAMES.add(flag)
    if "short" in spec:
        _COMMON_FLAG_NAMES.add(spec["short"])


def _add_common_flags(parser):
    """Add common flags to a subcommand parser."""
    for flag, spec in _COMMON_FLAGS.items():
        kwargs = {k: v for k, v in spec.items() if k != "short"}
        args = [flag]
        if "short" in spec:
            args.append(spec["short"])
        parser.add_argument(*args, **kwargs)


def _hoist_common_flags(argv):
    """
    Move common flags from before the subcommand to after it.

    Turns: ['--quiet', '--claude-dir', '/foo', 'backup', '--no-commit']
    Into:  ['backup', '--quiet', '--claude-dir', '/foo', '--no-commit']

    This lets argparse handle everything via subcommand parsers only.
    """
    if argv is None:
        return None

    hoisted = []
    remainder = []
    i = 0
    found_subcommand = False

    while i < len(argv):
        arg = argv[i]

        if found_subcommand:
            remainder.append(arg)
            i += 1
            continue

        if arg in _COMMON_FLAG_NAMES:
            # Check if this flag takes a value (not store_true)
            flag_key = arg if arg.startswith("--") else None
            if flag_key is None:
                # Short flag like -q -- find its long form
                for long_flag, spec in _COMMON_FLAGS.items():
                    if spec.get("short") == arg:
                        flag_key = long_flag
                        break

            takes_value = _COMMON_FLAGS.get(flag_key, {}).get("action") != "store_true"

            hoisted.append(arg)
            i += 1
            if takes_value and i < len(argv):
                hoisted.append(argv[i])
                i += 1
        elif not arg.startswith("-"):
            # This is the subcommand
            found_subcommand = True
            remainder.append(arg)
            i += 1
        else:
            # Unknown flag before subcommand (like --version)
            remainder.append(arg)
            i += 1

    return remainder + hoisted


def build_parser():
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="csb",
        description="Git-backed Claude Code session backup tool.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {DISPLAY_VERSION}"
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # backup
    p_backup = sub.add_parser("backup", help="Scan sessions, update index, git commit")
    _add_common_flags(p_backup)
    p_backup.add_argument(
        "--no-commit",
        action="store_true",
        help="Update index but skip git commit",
    )

    # list
    p_list = sub.add_parser("list", help="Timeline view (default sort: last-used)")
    _add_common_flags(p_list)
    p_list.add_argument("filter", nargs="?", default=None, help="Filter by keyword in session name, project, or folder paths (case-insensitive)")
    p_list.add_argument("-n", type=int, default=20, help="Number of sessions to show")
    p_list.add_argument(
        "--sort",
        choices=["last-used", "expiration", "started", "oldest", "messages", "size"],
        default="last-used",
        help="Sort order: last-used (default), expiration (soonest purge first), "
             "started (newest first), oldest (oldest first), messages, size",
    )
    p_list.add_argument("--deleted", action="store_true", help="Show only deleted sessions")
    p_list.add_argument("--all", action="store_true", help="Show all sessions including deleted")
    p_list.add_argument("--json", action="store_true", help="Output as JSON")
    p_list_folders = p_list.add_mutually_exclusive_group()
    p_list_folders.add_argument(
        "--top", type=int, metavar="N", default=None,
        help="Show top N other folders per session (default: 3). Use --all-folders for everything.",
    )
    p_list_folders.add_argument(
        "--all-folders", action="store_true",
        help="Show every tracked folder per session (no cap).",
    )

    # status
    p_status = sub.add_parser("status", help="Summary of sessions, deletions, git state")
    _add_common_flags(p_status)

    # show
    p_show = sub.add_parser("show", help="Detailed session info with folder analysis")
    _add_common_flags(p_show)
    p_show.add_argument("session_id", help="Session ID (prefix match supported)")

    # restore
    p_restore = sub.add_parser("restore", help="Restore deleted session from git history")
    _add_common_flags(p_restore)
    p_restore.add_argument("session_id", help="Session ID to restore")
    p_restore.add_argument("--dry-run", action="store_true", help="Show what would be restored")

    # resume
    p_resume = sub.add_parser("resume", help="Launch claude --resume with full UUID")
    _add_common_flags(p_resume)
    p_resume.add_argument("session_id", help="Session ID (prefix match supported)")

    # scan
    p_scan = sub.add_parser("scan", help="Find sessions in current directory and children")
    _add_common_flags(p_scan)
    p_scan.add_argument("path", nargs="?", default=".", help="Root path to scan (default: current directory)")
    p_scan.add_argument("-n", type=int, default=20, help="Number of sessions to show")
    p_scan.add_argument("--no-usage", "-NU", action="store_true",
                        help="Only match by project start folder, skip folder usage search")
    p_scan_folders = p_scan.add_mutually_exclusive_group()
    p_scan_folders.add_argument(
        "--top", type=int, metavar="N", default=None,
        help="Show top N other folders per session (default: 3). Use --all-folders for everything.",
    )
    p_scan_folders.add_argument(
        "--all-folders", action="store_true",
        help="Show every tracked folder per session (no cap).",
    )

    # search
    p_search = sub.add_parser("search", help="Search session metadata")
    _add_common_flags(p_search)
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("-n", type=int, default=10, help="Max results")

    # rebuild-index
    p_rebuild = sub.add_parser("rebuild-index", help="Reconstruct SQLite index from git history")
    _add_common_flags(p_rebuild)

    # config
    p_config = sub.add_parser("config", help="View/edit configuration")
    _add_common_flags(p_config)
    p_config.add_argument("key", nargs="?", help="Config key to get/set")
    p_config.add_argument("value", nargs="?", help="Value to set")

    return parser


def main(argv=None):
    """Entry point for csb CLI."""
    # Hoist common flags from before the subcommand to after it.
    # This makes `csb --quiet backup` work the same as `csb backup --quiet`.
    if argv is None:
        argv = sys.argv[1:]
    argv = _hoist_common_flags(argv)

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
    elif args.command == "resume":
        from .commands import cmd_resume
        return cmd_resume(args)
    elif args.command == "scan":
        from .commands import cmd_scan
        return cmd_scan(args)
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

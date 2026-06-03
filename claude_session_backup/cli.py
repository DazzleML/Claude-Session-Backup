"""
Command-line interface for claude-session-backup.

Git-backed Claude Code session backup with timeline view, folder analysis,
deletion detection, and session restore.

Usage:
    csb backup                           # scan, index, git commit
    csb list [-n 20] [--deleted [only|all]]  # timeline view sorted by last-used
    csb status                           # summary of sessions, deletions, git state
    csb show <session-id>                # detailed session info with folder analysis
    csb restore <session-id>             # restore deleted session from git history
    csb resume <session-id>              # launch claude --resume with full UUID
    csb scan [path]                      # find sessions in current dir and children
    csb search "query"                   # search session metadata
    csb update rebuild-index             # safely reconstruct SQLite (preserves deleted)
    csb update build-fts5                # build/refresh FTS5 content index
    csb update backfill-deleted          # discover culled sessions from git history
    csb config [key] [value]             # view/edit csb's own configuration
    csb config settings:cleanupPeriodDays [days]  # view/set Claude Code's purge TTL
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

    # metavar="<command>" keeps the usage line clean (a generic placeholder
    # instead of the full brace list) so internal subcommands registered with
    # help=SUPPRESS (e.g. `_check`) are hidden from `csb --help` entirely --
    # both the usage line and the command listing.
    sub = parser.add_subparsers(dest="command", metavar="<command>", help="Available commands")

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
    # ``--deleted`` is two-valued in v0.3.5: bare or ``only`` shows only
    # deleted; ``all`` shows live + deleted (replaces the old ``--all``).
    # Default (flag absent) -> live only, same as before.
    p_list.add_argument(
        "--deleted", nargs="?", choices=["only", "all"], const="only",
        default=None,
        help="Include deleted sessions. 'only' (bare or explicit) -- show "
             "deleted exclusively. 'all' -- show live AND deleted. "
             "Omit the flag for live-only (default).",
    )
    p_list.add_argument("--json", action="store_true", help="Output as JSON")
    p_list.add_argument(
        "--shortid", "-sid", action="store_true",
        help="Display compact UUID form (<head>-...-<tail>) instead of the full UUID. "
             "Full UUID is the default so users can paste into 'claude --resume <uuid>' "
             "(claude has no short-form resolver). csb commands accept either form.",
    )
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
    p_restore.add_argument(
        "--jsonl-only", action="store_true",
        help="Restore only the main JSONL transcript (pre-v0.3.12 behavior). "
             "Default is to also restore subagents/, tool-results/, session-states/, "
             "and sesslogs/ when present in git.",
    )
    p_restore.add_argument(
        "--force", action="store_true",
        help="Overwrite existing on-disk files. By default, restore refuses if any "
             "target file already exists for an alive (non-deleted) session, to avoid "
             "clobbering a live session in progress.",
    )

    # resume
    p_resume = sub.add_parser("resume", help="Launch claude --resume with full UUID")
    _add_common_flags(p_resume)
    p_resume.add_argument("session_id", help="Session ID (prefix match supported)")
    # Pruned-session handling (v0.3.14, #34): if the session has deleted_at
    # set, Claude Code can't resume it (JSONL missing). These flags control
    # whether we auto-restore from git before resuming. Default (no flag)
    # is interactive: prompt on TTY, refuse with hint on non-TTY.
    resume_pruned_group = p_resume.add_mutually_exclusive_group()
    resume_pruned_group.add_argument(
        "--restore-pruned",
        action="store_true", dest="restore_pruned",
        help="Auto-restore the session from git history before resuming, "
             "without prompting. Required for non-TTY use (cron, scripts).",
    )
    resume_pruned_group.add_argument(
        "--no-restore-pruned",
        action="store_true", dest="no_restore_pruned",
        help="Refuse to restore; exit with an error and a hint to run "
             "`csb restore` separately. Useful for scripts that want to "
             "detect pruned sessions instead of recovering them.",
    )

    # scan
    p_scan = sub.add_parser(
        "scan",
        help="Find sessions by term, location, or both",
        description=(
            "Find sessions by term, location, or both.\n\n"
            "  csb scan                  cwd path-prefix (today's default)\n"
            "  csb scan <term>           broad metadata substring (name, project, folder paths)\n"
            "  csb scan ./dirname        shortcut: same as -d dirname (no flag to remember)\n"
            "  csb scan -d <pattern>     path-strict: folder + descendants (start_folder OR top-N folder_usage)\n"
            "  csb scan -D <pattern>     path-strict: this folder only (no descendants)\n"
            "  csb scan -s <pattern>     start_folder only: 'what sessions originated here?' (skips folder_usage)\n"
            "  csb scan -d|-D|-s <pat> <term>   scope-then-filter combined\n\n"
            "Patterns accept a trailing '*' for sibling-prefix expansion (amdead* matches amdead, amdead-fork, ...)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_flags(p_scan)
    p_scan.add_argument(
        "term", nargs="?", default=None,
        help="Filter sessions whose name, project, or folder paths contain term (case-insensitive). "
             "If this starts with `./` or `.\\` (or is a bare `.`), it's auto-promoted to implicit -d.",
    )
    p_scan.add_argument(
        "term2", nargs="?", default=None,
        help="Optional second positional. Only valid when the first positional is a "
             "`./dirname` / `.\\dirname` shortcut -- in that case `term2` is the actual term "
             "filter (equivalent to `csb scan -d dirname term2`). Otherwise rejected.",
    )
    p_scan.add_argument("-n", type=int, default=20, help="Number of sessions to show")
    p_scan.add_argument(
        "--no-usage", "-NU", action="store_true",
        help="Skip folder_usage match; only consider session start_folder",
    )
    # -d / -D / -s mutually exclusive: path-strict modes
    p_scan_dir = p_scan.add_mutually_exclusive_group()
    p_scan_dir.add_argument(
        "-d", "--directories-below", metavar="PATTERN", default=None,
        help="Path-strict: match this folder + its descendants. Trailing '*' for sibling-prefix.",
    )
    p_scan_dir.add_argument(
        "-D", "--directory-only", metavar="PATTERN", default=None,
        help="Path-strict: match this folder only, no descendants. Trailing '*' for sibling-prefix.",
    )
    p_scan_dir.add_argument(
        "-s", "--start-dir-only", metavar="PATTERN", default=None,
        help="Path-strict: only match sessions whose start_folder is this folder + descendants. "
             "Skips folder_usage entirely. Useful for 'what sessions originated from here?'",
    )
    # --deleted / --all mutually exclusive: deletion-filter scope
    p_scan_del = p_scan.add_mutually_exclusive_group()
    p_scan_del.add_argument(
        "--deleted", action="store_true",
        help="Show only deleted sessions (DB-flagged via deleted_at). "
             "Combine with -d / --restore to discover and recover sessions "
             "purged from a specific folder.",
    )
    p_scan_del.add_argument(
        "--all", action="store_true",
        help="Show both active AND deleted sessions in the scoped folder.",
    )
    # --restore: bulk restoration of matching deleted sessions
    p_scan.add_argument(
        "--restore", action="store_true",
        help="After scanning, restore each matching deleted session from "
             "git history. Implies --deleted scope (active sessions are "
             "skipped). Confirms before restoring >1 file unless --yes "
             "is given. --dry-run previews only.",
    )
    p_scan.add_argument(
        "--dry-run", action="store_true",
        help="With --restore: preview what would be restored without writing.",
    )
    p_scan.add_argument(
        "--yes", "-y", action="store_true",
        help="With --restore: skip the >1-file confirmation prompt.",
    )
    p_scan.add_argument(
        "--force", action="store_true",
        help="With --restore: overwrite an existing on-disk file (default refuses).",
    )
    # --top / --all-folders mutually exclusive: display + folder_usage matching gate
    p_scan_folders = p_scan.add_mutually_exclusive_group()
    p_scan_folders.add_argument(
        "--top", type=int, metavar="N", default=None,
        help="Show top N other folders per session (default: 3). Also gates -d/-D folder_usage matching.",
    )
    p_scan_folders.add_argument(
        "--all-folders", action="store_true",
        help="Show every tracked folder per session (no cap). Also removes top-N gate from -d/-D matching.",
    )
    p_scan.add_argument(
        "--shortid", "-sid", action="store_true",
        help="Display compact UUID form (<head>-...-<tail>) instead of the full UUID. "
             "Full UUID is the default so users can paste into 'claude --resume <uuid>'.",
    )

    # search -- transcript content search (replaces v0.2.3's metadata LIKE)
    p_search = sub.add_parser(
        "search",
        help="Search transcript content (USER / AI / AGENT messages)",
        description=(
            "Search transcript content across every indexed session.\n\n"
            "By default, queries are case-insensitive literal substrings. "
            "Use -E for regex (Python re). Per-session source preference: "
            ".convo* -> .sesslog* -> JSONL (override with --source).\n\n"
            "For metadata search (session name / project / folder paths), "
            "use 'csb list <filter>' or 'csb scan <term>'."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_flags(p_search)
    p_search.add_argument("query", help="Search pattern (literal substring by default)")
    p_search.add_argument(
        "-E", "--regex", action="store_true",
        help="Treat query as a Python regex (re.IGNORECASE unless -s)",
    )
    p_search.add_argument(
        "-s", "--case-sensitive", action="store_true",
        help="Case-sensitive match (default: case-insensitive)",
    )
    p_search.add_argument(
        "-A", "--after", type=int, default=0, metavar="N",
        help="Show N events after each hit (mirrors grep -A)",
    )
    p_search.add_argument(
        "-B", "--before", type=int, default=0, metavar="N",
        help="Show N events before each hit (mirrors grep -B)",
    )
    p_search.add_argument(
        "-C", "--context", type=int, default=None, metavar="N",
        help="Show N events before AND after each hit (mirrors grep -C; overrides -A/-B)",
    )
    p_search.add_argument(
        "--session-id", default=None, metavar="UUID[,UUID...]",
        help="Constrain to one or more sessions by UUID prefix (>=4 hex chars). "
             "Comma-separated for multi-prefix OR-match.",
    )
    p_search.add_argument(
        "--source",
        choices=["auto", "fts5", "convo", "sesslog", "jsonl"], default="auto",
        help="Force a source channel (default: auto -- FTS5 when fresh, "
             "else .convo > .sesslog > jsonl). 'fts5' returns no hits for "
             "sessions not yet indexed by `csb update build-fts5`.",
    )
    p_search.add_argument(
        "--sort",
        choices=["last-used", "expiration", "started", "oldest", "messages", "size"],
        default="last-used",
        help="Session iteration order: last-used (default), expiration "
             "(soonest purge first), started (newest first), oldest (oldest "
             "first), messages, size. Matches 'csb list --sort' choices.",
    )
    # Same two-valued ``--deleted`` shape as ``csb list``. See p_list above.
    p_search.add_argument(
        "--deleted", nargs="?", choices=["only", "all"], const="only",
        default=None,
        help="Include deleted sessions. 'only' (bare or explicit) -- search "
             "only deleted. 'all' -- search live AND deleted. Omit the flag "
             "for live-only (default).",
    )
    p_search.add_argument(
        "--limit", type=int, default=20,
        help="Stop after N matches (default: 20)",
    )
    p_search.add_argument(
        "-F", "--full-match", action="store_true",
        help="Don't truncate long matched lines (default: 500 chars)",
    )
    p_search.add_argument(
        "-f", "--full-info", action="count", default=0,
        help="Add richer per-session header info. -f / --full-info for "
             "level 1 ('started: <date> (purge in Nd)'). -ff for level 2 "
             "(adds folder list + 'N messages | vX.Y.Z' meta line). "
             "Mirrors 'csb list' shape. Repeat to escalate (capped at 2).",
    )
    p_search.add_argument("--no-color", action="store_true", help="Disable ANSI color")
    # Output-mode mutex group: at most one of --json / --only.
    # Default mode (no flag): grouped human-readable hits with excerpts.
    p_search_mode = p_search.add_mutually_exclusive_group()
    p_search_mode.add_argument(
        "--json", action="store_true",
        help="NDJSON output -- one JSON object per hit (jq-friendly)",
    )
    p_search_mode.add_argument(
        "--only", choices=["files", "sessions"], default=None,
        metavar="{files,sessions}",
        help="Collapse output to a one-line-per-item summary. "
             "'files': unique transcript paths (convo > sesslog > jsonl). "
             "'sessions': per-session summary (name + UUID + project + "
             "start-at + hit count). Default (no flag): grouped excerpts.",
    )
    p_search.add_argument(
        "--shortid", "-sid", action="store_true",
        help="Display compact UUID form (<head>-...-<tail>) in session headers. "
             "Default is the full UUID so users can paste into 'claude --resume <uuid>'.",
    )
    # Directory-scope mutex (v0.3.5). Names mirror `csb scan`'s -d / -D:
    #   -d <path>  -- folder + descendants (recursive)
    #   -D <path>  -- folder only (no descendants)
    # Requires FTS5; rejects --source jsonl|convo|sesslog. See cmd_search.
    p_search_dir_scope = p_search.add_mutually_exclusive_group()
    p_search_dir_scope.add_argument(
        "-d", "--directories-below", metavar="PATH", default=None,
        help="Rank sessions by how heavily each worked on files under "
             "PATH (active edits weigh most; reads middle; Grep probes "
             "lightest), then narrow to ones whose transcripts match the "
             "query. Recurses into subdirectories -- answers 'what's "
             "been done in this folder, and who said what about it?'. "
             "Requires `csb update build-fts5` for affected projects.",
    )
    p_search_dir_scope.add_argument(
        "-D", "--directory-only", metavar="PATH", default=None,
        help="Same as -d but PATH only -- subdirectories excluded. Use "
             "when you care about work DIRECTLY in this folder, not in "
             "nested children. 'What's been done right here, not below?'",
    )
    p_search.add_argument(
        "--min-strength", type=int, choices=[1, 2, 3], default=1,
        metavar="N",
        help="Filter -d/-D file-ops by minimum strength. 1 (default) "
             "includes everything; 2 skips Grep/Glob probes; 3 keeps "
             "only active-modification ops (edited/wrote/notebook_edit). "
             "No effect outside -d/-D mode.",
    )

    # rebuild-index
    # ── csb update: umbrella for "reach in and refresh a representation" ops ──
    # Lives at the top level so all maintenance verbs group cleanly. Targets:
    #   rebuild-index     - reconstruct the SQLite session index
    #   build-fts5        - per-project FTS5 content index
    #   backfill-deleted  - discover deleted sessions from git history that
    #                       aren't in the live DB (v0.3.11)
    # Each target is independently safe to run on a live ~/.claude/ -- the
    # operation acquires the backup_lock and preserves deleted-session metadata.
    p_update = sub.add_parser(
        "update",
        help="Refresh / rebuild a csb representation (DB index, FTS5, deleted cache)",
        description=(
            "csb update <target> refreshes or rebuilds a specific csb "
            "representation. Each target is independently safe to run on a "
            "live ~/.claude/ -- the operation acquires the backup_lock and "
            "preserves all known deleted-session metadata. Targets: "
            "rebuild-index (SQLite session index), build-fts5 (per-project "
            "FTS5 content index), backfill-deleted (git-history backfill of "
            "culled-session metadata)."
        ),
    )
    update_sub = p_update.add_subparsers(dest="update_target", metavar="<target>")

    # csb update rebuild-index
    p_update_rebuild = update_sub.add_parser(
        "rebuild-index",
        help="Reconstruct SQLite index (preserves deleted-session metadata)",
        description=(
            "Reconstruct the SQLite session index. Acquires the backup_lock, "
            "moves the existing DB aside as .bak, runs the indexer against "
            "the live filesystem, then merges back any deleted-session rows "
            "(plus their folder_usage) that aren't in the rebuilt DB. The "
            ".bak is removed on success and restored on failure -- the "
            "rebuild is crash-safe."
        ),
    )
    _add_common_flags(p_update_rebuild)
    p_update_rebuild.add_argument(
        "--include-fts5", action="store_true",
        help=(
            "Also refresh the per-project FTS5 indexes (currently a no-op "
            "stub on this branch -- main wires the actual refresh in "
            "post-merge)."
        ),
    )
    p_update_rebuild.add_argument(
        "--include-backfill-deleted", action="store_true",
        help=(
            "Also run backfill-deleted after the rebuild -- discover "
            "culled-session metadata from git history in the same pass."
        ),
    )

    # csb update build-fts5
    p_update_build = update_sub.add_parser(
        "build-fts5",
        help="Build/refresh FTS5 content index (per-project DBs in ~/.claude/csb-fts/)",
        description=(
            "Index session transcripts into per-project SQLite FTS5 "
            "databases for fast content search. Idempotent -- skips "
            "sessions whose JSONL mtime hasn't changed since the last "
            "build. Use --force to re-index unconditionally."
        ),
    )
    _add_common_flags(p_update_build)
    p_update_build.add_argument(
        "--project", default=None, metavar="SLUG",
        help="Limit to one project (encoded slug form, e.g. 'C--code-myproj')",
    )
    p_update_build.add_argument(
        "--session-id", default=None, metavar="UUID",
        help="Limit to one session (UUID prefix; uses the shared resolver)",
    )
    p_update_build.add_argument(
        "--force", action="store_true",
        help="Re-index every candidate even if up-to-date",
    )

    # csb update backfill-deleted (NEW in v0.3.11)
    p_update_backfill = update_sub.add_parser(
        "backfill-deleted",
        help="Discover culled sessions from git history; synthesize DB rows",
        description=(
            "Discover deleted sessions that exist in git history but not in "
            "the live SQLite DB. For each such session, extract metadata "
            "from the historical JSONL blob and INSERT a deleted-flagged "
            "sessions row + folder_usage. Surfaces pre-csb-era deletions "
            "and sessions culled while csb wasn't running."
        ),
    )
    _add_common_flags(p_update_backfill)
    p_update_backfill.add_argument(
        "--full", action="store_true",
        help=(
            "Full re-scan of all git history. Default behavior is "
            "incremental -- walks only commits newer than the last refresh."
        ),
    )
    p_update_backfill.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be backfilled without writing any rows.",
    )

    # config
    p_config = sub.add_parser(
        "config",
        help="View/edit configuration",
        description=(
            "View/edit configuration. A bare key (e.g. 'display_top_folders') "
            "addresses csb's own config file. A 'settings:' key (e.g. "
            "'settings:cleanupPeriodDays') addresses Claude Code's own "
            "settings.json -- this is how you view or change the session purge "
            "TTL. Examples: 'csb config settings:cleanupPeriodDays' (view), "
            "'csb config settings:cleanupPeriodDays 365' (set)."
        ),
    )
    _add_common_flags(p_config)
    p_config.add_argument("key", nargs="?", help="Config key to get/set")
    p_config.add_argument("value", nargs="?", help="Value to set")
    p_config.add_argument(
        "--force", action="store_true",
        help="Confirm a dangerous settings write (e.g. "
             "settings:cleanupPeriodDays 0, which makes Claude Code delete all "
             "transcripts at next startup).",
    )

    # Internal: the SessionStart hook's gap detector. Hidden from `csb --help`
    # -- it's a hook mechanism, not a user-facing command. We OMIT help= (rather
    # than help=SUPPRESS, which argparse renders literally as "==SUPPRESS==" for
    # subparsers): with no help, argparse adds no entry to the command listing,
    # and the add_subparsers metavar="<command>" keeps it out of the usage line.
    # description= is independent of help= -- it's shown by `csb _check -h` only,
    # so a curious user who finds the command gets a real explanation rather
    # than a bare usage line. The hook reaches it via find_csb() the same way it
    # reaches `backup`, because the hook's Python may not import the package
    # directly. Invokable by hand for maintainers / post-crash triage.
    p_check = sub.add_parser(
        "_check",
        description=(
            "Internal health check used by the SessionStart backup hook. "
            "Reports sessions whose transcript is newer than the last backup "
            "scan (or were never indexed) -- i.e. sessions with un-backed-up "
            "changes. Exit code: 0 = all backed up, 10 = gap(s) found, "
            "1 = not a git repo. Hidden from `csb --help` because it's a hook "
            "mechanism, not a user command -- the user-facing view of the same "
            "data is the `Un-backed-up:` line in `csb status`."
        ),
    )
    _add_common_flags(p_check)
    p_check.add_argument(
        "--exclude",
        action="append",
        metavar="SESSION_ID",
        help="full session id to skip (repeatable); e.g. the currently-active "
             "session, whose JSONL is mid-write and always looks newer",
    )

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
    elif args.command == "update":
        from .commands import cmd_update
        return cmd_update(args)
    elif args.command == "config":
        from .commands import cmd_config
        return cmd_config(args)
    elif args.command == "_check":
        from .commands import cmd_check
        return cmd_check(args)

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

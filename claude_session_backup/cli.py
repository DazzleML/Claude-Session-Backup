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
    csb view [query]                     # open a session in Claude Code History Viewer
    csb distill <query>                  # render a session as a readable chat log
    csb scan [path]                      # find sessions in current dir and children
    csb search "query"                   # search session metadata
    csb set show last                    # what was active before the last shutdown
    csb update rebuild-index             # safely reconstruct SQLite (preserves deleted)
    csb update build-fts5                # build/refresh FTS5 content index
    csb update backfill-deleted          # discover culled sessions from git history
    csb config [key] [value]             # view/edit csb's own configuration
    csb config settings:cleanupPeriodDays [days]  # view/set Claude Code's purge TTL
"""

import argparse
import sys

from ._version import FULL_DISPLAY_VERSION


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

# Commands that launch a wrapped subtool and therefore accept `--` passthrough
# (#47): everything after a standalone `--` is forwarded verbatim to the child
# (resume -> claude, view -> the history viewer). Any other command rejects a
# passthrough rather than silently dropping it.
PASSTHROUGH_COMMANDS = {"resume", "view"}


def _add_common_flags(parser):
    """Add common flags to a subcommand parser."""
    for flag, spec in _COMMON_FLAGS.items():
        kwargs = {k: v for k, v in spec.items() if k != "short"}
        args = [flag]
        if "short" in spec:
            args.append(spec["short"])
        parser.add_argument(*args, **kwargs)


def add_deleted_flag(parser, verb: str, with_all_alias: bool = False):
    """Attach the canonical two-valued ``--deleted [only|all]`` flag (#41).

    One definition for every command that filters on deletion state
    (`list`, `search`, `scan`) -- the grammar `csb <cmd> --deleted [only|all]`
    is identical everywhere. ``verb`` parameterizes the help text ("show",
    "search", "scan"). ``with_all_alias`` additionally attaches a HIDDEN
    deprecated ``--all`` boolean (scan's pre-#41 spelling); it maps to
    ``--deleted all`` in ``commands.deleted_mode`` with a one-time warning,
    and is slated for removal at 0.4.
    """
    parser.add_argument(
        "--deleted", nargs="?", choices=["only", "all"], const="only",
        default=None,
        help=f"Include deleted sessions. 'only' (bare or explicit) -- {verb} "
             f"deleted exclusively. 'all' -- {verb} live AND deleted. "
             f"Omit the flag for live-only (default).",
    )
    if with_all_alias:
        parser.add_argument(
            "--all", action="store_true", help=argparse.SUPPRESS,
        )


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


def _nonneg_int(value: str) -> int:
    """Value parser for count-threshold filter keys.

    Rejects negatives outright: ``min-work=-5`` used to be silently
    accepted as a no-op, which is a quietly-ignored filter by another
    route.
    """
    v = int(value)
    if v < 0:
        raise argparse.ArgumentTypeError(
            f"expects a non-negative integer, got {v}"
        )
    return v


#: What each value parser expects, in words a user can act on. The generic
#: fallback in _filter_kv used the callable's __name__, which for the
#: custom validator leaked `_nonneg_int` into the error -- a private
#: Python identifier is not a user instruction (tester pass 3, Finding B).
_nonneg_int.expects = "a non-negative integer"


def _filter_kv(allowed: dict):
    """Build an argparse ``type=`` callable for ``--filter KEY=VALUE``.

    One vocabulary for narrowing, shared across verbs, instead of a new flag
    per predicate. The rule that keeps it coherent: **``--filter`` narrows the
    verb's RESULT SET, and the verb defines what its results are.** ``csb
    show`` has already chosen a session, so its results are folder rows;
    ``scan``/``list``/``tree`` results are sessions. Display trimming stays
    with ``--top`` / ``--all-folders`` -- keeping the 0.7.1 invariant that a
    display knob can never change what matches.

    The comparison direction lives in the KEY (``min-work=10``) rather than in
    an operator (``work>=10``) because ``>`` and ``<`` are redirection in
    cmd.exe and PowerShell and would be eaten before csb ever saw them.

    ``allowed`` maps key -> value parser. An unknown key is a hard error, never
    a silent no-op: a filter that is quietly ignored returns a wrong answer
    wearing a right answer's clothes.
    """
    keys = ", ".join(sorted(allowed))

    def parse(raw: str):
        if "=" not in raw:
            raise argparse.ArgumentTypeError(
                f"--filter expects KEY=VALUE, got {raw!r}. Valid keys: {keys}"
            )
        key, _, value = raw.partition("=")
        key = key.strip().lower()
        if key not in allowed:
            raise argparse.ArgumentTypeError(
                f"unknown filter key {key!r}. Valid keys: {keys}"
            )
        try:
            return key, allowed[key](value.strip())
        except (TypeError, ValueError):
            expects = getattr(
                allowed[key], "expects",
                getattr(allowed[key], "__name__", "a value"))
            raise argparse.ArgumentTypeError(
                f"--filter {key}= expects {expects}, got {value!r}"
            )

    return parse


def build_parser():
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="csb",
        description="Git-backed Claude Code session backup tool.",
    )
    parser.add_argument(
        "-V", "--version", action="version",
        version=f"%(prog)s {FULL_DISPLAY_VERSION}",
    )

    # metavar="<command>" keeps the usage line clean (a generic placeholder
    # instead of the full brace list) so internal subcommands registered with
    # help=SUPPRESS (e.g. `_check`) are hidden from `csb --help` entirely --
    # both the usage line and the command listing.
    sub = parser.add_subparsers(dest="command", metavar="<command>", help="Available commands")

    # setup (v0.6.0, #52): guided onboarding -- THE way to the protected state
    p_setup = sub.add_parser(
        "setup",
        help="Configure the git backup store (guided; --auto for no prompts)",
        description=(
            "Guided onboarding: checks the Claude dir, detects an existing git "
            "repo (including one rooted at an ancestor, e.g. a home-dir repo "
            "tracking .claude/), offers to initialize one when none exists, "
            "hardens .gitattributes, and offers the first backup. "
            "`--auto` performs all of that without prompts (for scripts and "
            "provisioning). `--index-only` is the EXPLICIT exception path: it "
            "records your sign-off that sessions are indexed but NOT backed "
            "up, which is the only way to silence the no-protection banner "
            "without a repo."
        ),
    )
    _add_common_flags(p_setup)
    p_setup.add_argument(
        "--auto",
        action="store_true",
        help="Non-interactive: initialize the repo (and run the first backup) "
             "without prompting",
    )
    p_setup.add_argument(
        "--index-only",
        action="store_true",
        help="Explicitly opt out of backup protection: record a sign-off that "
             "csb runs index-only (list/scan/search work; nothing is preserved)",
    )

    # backup
    p_backup = sub.add_parser("backup", help="Scan sessions, update index, git commit")
    _add_common_flags(p_backup)
    p_backup.add_argument(
        "--no-commit",
        action="store_true",
        help="Update index but skip git commit (works without a git repo: "
             "index-only, no backup protection)",
    )

    # list
    p_list = sub.add_parser("list", help="Timeline view (default sort: last-used)")
    _add_common_flags(p_list)
    p_list.add_argument("filter", nargs="?", default=None, help="Filter by keyword in session name, project, or folder paths (case-insensitive)")
    # Same grouping as `scan`: selection / display / limits. See the note
    # there on why the boundary between "what matches" and "what is shown"
    # is worth making visible.
    g_list_sel = p_list.add_argument_group(
        "selection -- which sessions match")
    g_list_disp = p_list.add_argument_group(
        "display -- how each matching session is shown")
    g_list_limit = p_list.add_argument_group(
        "limits and ordering")

    g_list_limit.add_argument(
        "-n", type=int, default=20, help="Number of sessions to show")
    g_list_limit.add_argument(
        "--sort",
        choices=["last-used", "expiration", "started", "oldest", "messages", "size"],
        default="last-used",
        help="Sort order: last-used (default), expiration (soonest purge first), "
             "started (newest first), oldest (oldest first), messages, size",
    )
    # ``--deleted`` is two-valued since v0.3.5; shared definition since #41.
    add_deleted_flag(g_list_sel, "show")
    g_list_disp.add_argument("--json", action="store_true", help="Output as JSON")
    g_list_disp.add_argument(
        "--shortid", "-sid", action="store_true",
        help="Display compact UUID form (<head>-...-<tail>) instead of the full UUID. "
             "Full UUID is the default so users can paste into 'claude --resume <uuid>' "
             "(claude has no short-form resolver). csb commands accept either form.",
    )
    p_list_folders = g_list_disp.add_mutually_exclusive_group()
    p_list_folders.add_argument(
        "--top", type=int, metavar="N", default=None,
        help="Show top N other folders per session (default: 3). Use --all-folders for everything.",
    )
    p_list_folders.add_argument(
        "--all-folders", action="store_true",
        help="Show every tracked folder per session (no cap).",
    )

    # tree (v0.7.0, #31): fork-lineage forest
    p_tree = sub.add_parser(
        "tree",
        help="Fork lineage: which session spawned which, as an indented tree",
        description=(
            "Render the fork forest -- every chain of sessions linked by "
            "/branch, /rewind-continue, or `claude --fork-session -r`. "
            "With no FILTER, the whole forest renders. With a FILTER, every "
            "family containing a match renders with the matches marked, so "
            "you see a session's ancestors AND descendants at once. "
            "PATH (or -d/-D) narrows to families that worked under a folder "
            "-- combining `csb list <filter>` with `csb scan <path>`."
        ),
    )
    _add_common_flags(p_tree)
    p_tree.add_argument(
        "filter", nargs="?", default=None,
        help="Filter by keyword in session name, project, id, or folder paths "
             "(same vocabulary as `csb list <filter>`). Trailing '*' anchors a "
             "prefix (NAME*); a bare '*' means everything; -E switches to "
             "regex. A path-shaped value here (e.g. `csb tree .`) is taken as "
             "the folder scope instead.",
    )
    p_tree.add_argument(
        "path", nargs="?", default=None,
        help="Optional folder scope -- must look like a path (absolute, "
             "contains a separator, or starts with ./). Equivalent to -d PATH.",
    )
    # Same grouping as `scan` and `list`. Tree carries the most flags of
    # any verb, so the split between "which families appear" and "how each
    # node is drawn" does the most work here.
    g_tree_sel = p_tree.add_argument_group(
        "selection -- which families appear")
    g_tree_disp = p_tree.add_argument_group(
        "display -- how each node is drawn")
    g_tree_limit = p_tree.add_argument_group(
        "limits and ordering")

    g_tree_sel.add_argument(
        "-E", "--regex", action="store_true",
        help="Interpret FILTER as a Python regular expression",
    )
    g_tree_sel.add_argument(
        "-s", "--case-sensitive", action="store_true",
        help="Case-sensitive FILTER match (default: case-insensitive)",
    )
    p_tree_dir = g_tree_sel.add_mutually_exclusive_group()
    p_tree_dir.add_argument(
        "-d", "--directories-below", metavar="PATH", default=None,
        help="Only families where some session worked under PATH "
             "(folder + descendants)",
    )
    p_tree_dir.add_argument(
        "-D", "--directory-only", metavar="PATH", default=None,
        help="Same as -d but PATH only -- subdirectories excluded",
    )
    g_tree_sel.add_argument(
        "--root", metavar="ID", default=None,
        help="Render only the family containing this session (UUID prefix, "
             "suffix, or name -- same resolver as `csb show`)",
    )
    g_tree_sel.add_argument(
        "--orphans", action="store_true",
        help="Only root sessions that never forked (no children)",
    )
    g_tree_sel.add_argument(
        "--lineage", action="store_true",
        help="Show only a match's own ancestors and descendants, omitting "
             "its siblings and cousins (default: the whole family)",
    )
    g_tree_limit.add_argument(
        "-n", type=int, default=None, metavar="N",
        help="Show at most N trees (families). Default: no limit.",
    )
    g_tree_limit.add_argument(
        "--max-nodes", type=int, default=None, metavar="N",
        help="Collapse a family larger than N sessions (default: 50). "
             "0 disables the cap.",
    )
    g_tree_limit.add_argument(
        "--sort",
        choices=["last-used", "expiration", "started", "oldest", "messages", "size"],
        default="last-used",
        help="Order of ROOTS (children always read in fork order)",
    )
    add_deleted_flag(g_tree_sel, "show")
    g_tree_disp.add_argument(
        "-f", "--full-info", action="count", default=0,
        help="Per-node detail: -f adds started + purge countdown, "
             "-ff adds folders and the message/version meta line",
    )
    g_tree_disp.add_argument(
        "-u", "--uuid", action="store_true",
        help="Show the full UUID beside each name (paste-ready for "
             "`claude --resume`). By default a NAMED session shows just its "
             "name, to keep the tree readable; unnamed sessions always show "
             "their UUID.",
    )
    g_tree_disp.add_argument(
        "--shortid", "-sid", action="store_true",
        help="Show the compact UUID form (<head>-...-<tail>) beside each name",
    )
    g_tree_disp.add_argument("--json", action="store_true",
                        help="Output nested JSON (one object per root)")
    g_tree_disp.add_argument("--ascii", action="store_true",
                        help="Force ASCII connectors (auto when the console "
                             "cannot encode box-drawing characters)")
    p_tree_folders = g_tree_disp.add_mutually_exclusive_group()
    p_tree_folders.add_argument(
        "--top", type=int, metavar="N", default=None,
        help="With -ff, show top N other folders per node (default: 3)",
    )
    p_tree_folders.add_argument(
        "--all-folders", action="store_true",
        help="With -ff, show every tracked folder per node",
    )

    # status
    p_status = sub.add_parser("status", help="Summary of sessions, deletions, git state")
    _add_common_flags(p_status)

    # show
    p_show = sub.add_parser(
        "show",
        help="Detailed session info with folder analysis",
        description=(
            "Detailed session info with folder analysis.\n"
            "\n"
            "The folder display is a fixed pipeline; each knob works one stage:\n"
            "  --paths <level>        which mentions count as places (a VIEW)\n"
            "  (scratch escape)       config: scratch_escape_min_work / _top_rank\n"
            "  --filter min-work=N    threshold on the surviving work rows\n"
            "  --all                  reveal the collapsed low-value tier\n"
            "\n"
            # Prose lines stay unwrapped -- RawDescriptionHelpFormatter
            # freezes any break we put here, while the options text below
            # flows to terminal width; mixed wrapping reads as a glitch.
            "Defaults: --paths suspected (everything stored; persist a choice with `csb config paths_level <level>`), no --filter (all work rows shown), low-value tier collapsed with an aggregate note.\n"
            "Order matters: --paths approximated MERGES folded counts BEFORE --filter thresholds them."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_flags(p_show)
    p_show.add_argument("session_id", help="Session ID (prefix match supported)")
    g_show_disp = p_show.add_argument_group(
        "folder display -- what appears, in pipeline order")
    # Two ORTHOGONAL knobs, deliberately not one flag: --filter hides by
    # COUNT (a long tail of one-off touches), --all reveals what is hidden
    # by KIND (scratch dirs, unresolved paths) regardless of count. Note
    # this is NOT `--all-folders`: that flag means "don't truncate to top-N"
    # on list/scan, and reusing the name for classification would give one
    # flag two meanings across commands.
    # Path-exposure rung (#56/H2): pick how much you trust, everything
    # colder comes with it. `choices` makes unknown levels fail loudly,
    # naming the valid ones. Selection is DISPLAY-only by design rule --
    # scan/search matching is rung-blind, structurally (the query layer
    # never imports pathlevels; a test pins it).
    from .pathlevels import SELECTABLE_LEVELS
    g_show_disp.add_argument(
        "--paths", metavar="LEVEL", default=None,
        choices=SELECTABLE_LEVELS,
        help="How much path detail to show, coldest-to-warmest: cd (only "
             "where the shell provably stood), verified (existing folders), "
             "approximated (unverifiable leaves folded up to real "
             "ancestors, marked (~)), suspected (everything stored -- the "
             "default), junk/raw (declared, not yet populated), all. "
             "Selecting a level shows it and everything colder. "
             "Persist with: csb config paths_level <level>.",
    )
    g_show_disp.add_argument(
        "--filter", action="append", dest="filters", metavar="KEY=VALUE",
        type=_filter_kv({"min-work": _nonneg_int}),
        help="Narrow which working directories are shown. Repeatable; "
             "multiple filters AND together. Keys: min-work=N (hide folders "
             "with fewer than N work units). Hidden folders are summarized "
             "in a count line and remain findable by `csb scan`.",
    )
    g_show_disp.add_argument(
        # dest is `show_all`, NOT `all_folders`. `--all-folders` on
        # list/scan/tree also writes to `all_folders` and means something
        # else ("don't truncate to top-N"), so sharing the dest would make
        # any shared render path read one flag as the other.
        "--all", action="store_true", dest="show_all",
        help="Reveal everything normally collapsed: scratch directories and "
             "paths that no longer resolve",
    )

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
    p_resume = sub.add_parser(
        "resume",
        help="Launch claude --resume with full UUID",
        description=(
            "Resolve a session, cd to its start folder, and launch\n"
            "`claude --resume <uuid>`. Forward extra args to claude after `--`:\n\n"
            "  csb resume <query>                    resume it\n"
            "  csb resume <query> -- --fork-session  resume + forward --fork-session to claude\n\n"
            "Everything after `--` is passed verbatim to claude (don't re-pass\n"
            "--resume / -r -- csb already supplies it)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_flags(p_resume)
    p_resume.add_argument(
        "session_id", metavar="query",
        help="Session UUID/prefix, exact session NAME, .jsonl path, folder, "
             "sesslog dir name, or keyword (every format csb view "
             "accepts; a superset of claude --resume's native surface). "
             "The literal word `set` switches to index addressing -- see "
             "below.",
    )
    # Index addressing (#63): `csb resume set <N>` reclaims member N of a
    # set by its roster number. A trailing nargs="*" rather than a
    # subparser, because argparse cannot host both a subparser and a free
    # positional query on one parser -- `set` is dispatched as a sentinel
    # in cmd_resume instead. Everything after `--` is split off before
    # parsing, so `csb resume set 2 -- --fork-session` still forwards.
    p_resume.add_argument(
        "selector", nargs="*", metavar="...", default=[],
        help="With query=`set`: [<set-name>] <N> -- the roster number from "
             "`csb set show`. `csb resume set 2` takes member 2 of the last "
             "boot epoch; `csb resume set CSB-STACK 2` of a named set. "
             "Numbers are stable positions, so they mean the same thing "
             "every time.",
    )
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
    # Live-session guard (#67): resuming a session that is already open
    # in another window makes two clients interleave into one transcript.
    # Advisory, never blocking; `-- --fork-session` is always exempt
    # (branching provably mints a new session id).
    resume_live_group = p_resume.add_mutually_exclusive_group()
    resume_live_group.add_argument(
        "--allow-live", action="store_true", dest="allow_live",
        help="Resume without prompting even when the session appears to "
             "be open already (a second client on one transcript).",
    )
    resume_live_group.add_argument(
        "--no-allow-live", action="store_true", dest="no_allow_live",
        help="Refuse to resume a session that appears to be open already; "
             "exit with a hint to branch via `-- --fork-session` instead.",
    )

    # view (#14): open a session in Claude Code History Viewer
    p_view = sub.add_parser(
        "view",
        help="Open a session in Claude Code History Viewer",
        description=(
            "Open a session's conversation in Claude Code History Viewer.\n\n"
            "  csb view <uuid-or-prefix>      open by session id\n"
            "  csb view <abs-path.jsonl>      open by transcript path\n"
            "  csb view <dir-or-.>            open the session that used a folder\n"
            "  csb view <sesslog-dir-name>    open by logger folder name\n"
            "  csb view <keyword>             open by name/project keyword\n"
            "  csb view                       list recent sessions\n\n"
            "Viewer discovery: $CLAUDEVIEW_BIN, then the `viewer_path` config\n"
            "key (csb config viewer_path <path>), then platform install\n"
            "locations. Without a viewer, prints the transcript path.\n"
            "Pruned sessions offer restore-from-git first (same flags as\n"
            "`csb resume`).\n\n"
            "Forward extra args to the viewer after `--`:\n"
            "  csb view <query> -- <viewer-args>"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_flags(p_view)
    p_view.add_argument(
        "query", nargs="?", default=None,
        help="Session UUID/prefix, .jsonl path, folder, sesslog dir name, "
             "or keyword. Omit to list recent sessions.",
    )
    view_pruned_group = p_view.add_mutually_exclusive_group()
    view_pruned_group.add_argument(
        "--restore-pruned",
        action="store_true", dest="restore_pruned",
        help="Auto-restore the session from git history before viewing, "
             "without prompting. Required for non-TTY use.",
    )
    view_pruned_group.add_argument(
        "--no-restore-pruned",
        action="store_true", dest="no_restore_pruned",
        help="Refuse to restore; exit with an error and a hint to run "
             "`csb restore` separately.",
    )

    # distill (#12): human-readable chat-log rendering
    p_distill = sub.add_parser(
        "distill",
        help="Render a session as a human-readable chat log",
        description=(
            "Render a session as an instant-messenger-style chat log --\n"
            "timestamped speaker turns with generous separation, plus\n"
            "one-line tool calls (never tool output). Markdown-friendly\n"
            "(Typora) and editor-friendly (Vim-jumpable file references).\n\n"
            "  csb distill <query>                 write ~/.claude/distilled/<slug>/<uuid>.md\n"
            "  csb distill <query> -o notes.md     write to a specific file\n"
            "  csb distill <query> --stdout        stream to stdout (pipe to less/glow)\n"
            "  csb distill <query> --filter convo  messages only (no tool lines)\n"
            "  csb distill <query> --filter tools  tool timeline only\n\n"
            "The distilled file is a READING layer -- the full JSONL stays\n"
            "preserved in git regardless. Config: distill_policy\n"
            "{always|on-demand|never} (default on-demand; 'always' makes\n"
            "csb backup regenerate stale files), distill_filter.\n"
            "Accepts every identifier csb view/resume accept."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_flags(p_distill)
    p_distill.add_argument(
        "query",
        help="Session UUID/prefix, name, .jsonl path, folder, sesslog dir "
             "name, or keyword",
    )
    p_distill.add_argument(
        "--filter", choices=["convo", "tools", "both"], default=None,
        help="Content filter (default: distill_filter config, 'both'): "
             "convo = messages only; tools = one-line tool calls only; "
             "both = interleaved.",
    )
    p_distill.add_argument(
        "--source", choices=["convo", "sesslog", "jsonl"], default=None,
        help="Force a conversation source channel (default: auto -- "
             "jsonl > sesslog > convo: distill renders csb's own "
             "preserved record, so the verbatim JSONL beats the "
             "logger's derived channels).",
    )
    distill_out_group = p_distill.add_mutually_exclusive_group()
    distill_out_group.add_argument(
        "-o", "--output", nargs="?", const="", default=None, metavar="PATH",
        help="Write to PATH. Bare -o (or omitting the flag entirely) writes "
             "the canonical ~/.claude/distilled/<slug>/<uuid>.md.",
    )
    distill_out_group.add_argument(
        "--stdout", action="store_true",
        help="Stream the chat log to stdout instead of writing a file "
             "(for piping into less / glow / redirects).",
    )
    distill_pruned_group = p_distill.add_mutually_exclusive_group()
    distill_pruned_group.add_argument(
        "--restore-pruned",
        action="store_true", dest="restore_pruned",
        help="Auto-restore a pruned session from git before distilling, "
             "without prompting.",
    )
    distill_pruned_group.add_argument(
        "--no-restore-pruned",
        action="store_true", dest="no_restore_pruned",
        help="Refuse to restore; exit with an error.",
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
            "  csb scan -d <pattern>     path-strict: folder + descendants (any recorded folder)\n"
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
    # Flags are grouped by WHICH QUESTION THEY ANSWER, because csb has three
    # kinds of narrowing that had been reading as one undifferentiated wall:
    # what MATCHES, how much of each match is SHOWN, and how many results
    # survive. Conflating the first two is what caused #56's match-gate bug
    # (a display cap silently decided which sessions existed), so making the
    # boundary visible in --help is part of the fix, not decoration.
    g_scan_sel = p_scan.add_argument_group(
        "selection -- which sessions match")
    g_scan_disp = p_scan.add_argument_group(
        "display -- how each matching session is shown (never changes what matches)")
    g_scan_limit = p_scan.add_argument_group(
        "limits -- how many results survive")
    g_scan_restore = p_scan.add_argument_group(
        "restore -- act on the matches (see --restore)")

    g_scan_limit.add_argument(
        "-n", type=int, default=20, help="Number of sessions to show")
    g_scan_disp.add_argument(
        "--json", action="store_true", help="Output as JSON")
    # Named for the MECHANISM, not the side effect (v0.7.1, replaces
    # --no-usage / -NU). What it does is bypass the SQLite index; what
    # falls OUT of that is "matches only by start folder", which is what
    # the old name described -- and which reads as a duplicate of -s.
    # The distinction became load-bearing in this release: while
    # folder_usage held one folder per session the flag barely changed
    # anything, and #56 made the index rich enough for it to matter.
    g_scan_sel.add_argument(
        "--no-index", "-NI", action="store_true", dest="no_index",
        help="Bypass the SQLite index: find sessions by walking "
             "~/.claude/projects/ and reading transcripts. Use when the "
             "index may be stale or incomplete. Note this cannot find "
             "DELETED sessions, which exist only in the index.",
    )
    # -d / -D / -s mutually exclusive: path-strict modes
    p_scan_dir = g_scan_sel.add_mutually_exclusive_group()
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
    # Deletion-filter scope: the canonical --deleted [only|all] grammar
    # (#41 -- scan finally matches list/search). The old boolean --all
    # remains as a hidden deprecated alias for --deleted all (removal: 0.4).
    add_deleted_flag(g_scan_sel, "scan", with_all_alias=True)
    # --restore: bulk restoration of matching deleted sessions
    g_scan_restore.add_argument(
        "--restore", action="store_true",
        help="After scanning, restore each matching deleted session from "
             "git history. Implies --deleted scope (active sessions are "
             "skipped). Confirms before restoring >1 file unless --yes "
             "is given. --dry-run previews only.",
    )
    g_scan_restore.add_argument(
        "--dry-run", action="store_true",
        help="With --restore: preview what would be restored without writing.",
    )
    g_scan_restore.add_argument(
        "--yes", "-y", action="store_true",
        help="With --restore: skip the >1-file confirmation prompt.",
    )
    g_scan_restore.add_argument(
        "--force", action="store_true",
        help="With --restore: overwrite an existing on-disk file (default refuses).",
    )
    # --top / --all-folders mutually exclusive: DISPLAY only.
    #
    # These used to double as a match gate on -d/-D, so `--top 3` silently
    # decided which sessions existed. That was removed (#56): how many
    # folders we print and whether a session matches are different
    # questions. Changing --top now changes only what you see.
    p_scan_folders = g_scan_disp.add_mutually_exclusive_group()
    p_scan_folders.add_argument(
        "--top", type=int, metavar="N", default=None,
        help="Show top N other folders per session (default: 3). Display only -- does not affect which sessions match.",
    )
    p_scan_folders.add_argument(
        "--all-folders", action="store_true",
        help="Show every tracked folder per session (no cap). Display only.",
    )
    g_scan_disp.add_argument(
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
    p_search.add_argument(
        "query", nargs="+", metavar="TERM",
        help="One or more search terms (literal substring by default). "
             "Multiple terms combine per --match.",
    )
    p_search.add_argument(
        "--match", choices=["all", "any"], default="all",
        help="How multiple TERMs combine, at the SESSION level: all (default, "
             "AND -- the session contains every term, in any order/message); "
             "any (OR -- at least one term). No effect with a single term.",
    )
    p_search.add_argument(
        "-E", "--regex", action="store_true",
        help="Treat each term as a Python regex (re.IGNORECASE unless -s)",
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
    add_deleted_flag(p_search, "search")
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
    # Directory-scope mutex (v0.3.5; source-agnostic since v0.5.1). Names
    # mirror `csb scan`'s -d / -D:
    #   -d <path>  -- folder + descendants (recursive)
    #   -D <path>  -- folder only (no descendants)
    # Works with any --source: default/auto/convo/sesslog/jsonl rank by
    # folder_usage (cwd activity); an explicit --source fts5 ranks by file-op
    # strength instead. See cmd_search.
    p_search_dir_scope = p_search.add_mutually_exclusive_group()
    p_search_dir_scope.add_argument(
        "-d", "--directories-below", metavar="PATH", default=None,
        help="Directory-scope: rank sessions by how much each was active "
             "under PATH, then narrow to ones whose transcripts match the "
             "query. Recurses into subdirectories. Source-agnostic -- finds "
             "every session that touched the folder; add `--source fts5` to "
             "rank by file-op strength instead (active edits weigh most). "
             "Answers 'what's been done in this folder, and who said what "
             "about it?'.",
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
        help="Filter -d/-D file-ops by minimum strength. Applies ONLY to "
             "--source fts5 (the file-op strength ranking); ignored (with a "
             "note) for other sources and outside -d/-D. 1 (default) includes "
             "everything; 2 skips Grep/Glob probes; 3 keeps only "
             "active-modification ops (edited/wrote/notebook_edit).",
    )

    # ── csb set: session sets -- what was active together (#60, #61) ──────
    # A "set" is a group of sessions that belong together. Epoch sets are
    # observed automatically (boot/shutdown fences from the OS event log +
    # index activity -- "what was I running before the restart?"); named
    # sets (#62) are curated. One verb for both kinds, one renderer.
    # Read-only: fences are read live, never stored; no hooks, no schema.
    p_set = sub.add_parser(
        "set",
        help="Session sets: what was active at the last shutdown (epochs); "
             "named sets",
        description=(
            "csb set <action> works with session sets -- groups of sessions "
            "that belong together. 'last' names the most recent boot epoch: "
            "the sessions active before the machine's last shutdown, "
            "reconstructed from the Windows event log and the existing "
            "index (read-only; nothing is stored). Named sets arrive "
            "with named sets."
        ),
    )
    set_sub = p_set.add_subparsers(dest="set_action", metavar="<action>")

    # csb set show
    p_set_show = set_sub.add_parser(
        "show",
        help="Numbered roster of a set ('last' = the most recent boot epoch)",
        description=(
            "Show a set's numbered roster. `csb set show last` answers "
            "'what was I running before the restart?': every session with "
            "activity in the window before the most recent shutdown fence, "
            "in activity order, each with a paste-able `csb resume` "
            "command. Honest by design: 'active within the window' is not "
            "proof a session was open -- exact open/close tracking arrives "
            "with live open/close tracking."
        ),
    )
    # Common flags on the LEAF only (mirrors `csb update`): argparse
    # subparsers re-apply their own defaults over values the parent
    # already parsed, so umbrella-level flags would be silently clobbered
    # (e.g. `csb set --claude-dir X show` losing X). The hoister makes
    # `csb --quiet set show last` work by appending flags after the leaf.
    _add_common_flags(p_set_show)
    p_set_show.add_argument(
        "set_name", metavar="<set>",
        help="'last' -- the most recent boot epoch (named sets arrive "
             "with #62)",
    )
    p_set_show.add_argument(
        "--window", type=float, default=None, metavar="HOURS",
        help="Activity window before the shutdown fence, in hours. "
             "Default: since the previous fence (the whole prior epoch) -- "
             "generous on purpose, because idle-but-open for days is normal "
             "and a tight window misses genuinely-open sessions.",
    )
    p_set_show.add_argument(
        "--json", action="store_true",
        help="Machine-readable roster (envelope with epoch metadata + "
             "members; emitted even when empty)",
    )
    p_set_show.add_argument(
        "--open", dest="open_only", action="store_true",
        help="Epoch view only: narrow to members that were provably open "
             "at the shutdown (from the live-registry boundary snapshot). "
             "A display filter -- indices keep their canonical values, so "
             "gaps are expected and `csb resume set <N>` still matches.",
    )

    # csb set new / list / add / rm -- named sets (#62)
    p_set_new = set_sub.add_parser(
        "new",
        help="Create a named set from one or more sessions",
        description=(
            "Create a named set -- a curated group of sessions that belong "
            "together, independent of any restart. Members are stored as "
            "full UUIDs (resolved now), so renaming a session later does "
            "not break the set. Names follow docs/naming.md: PURPOSE for a "
            "standing set (CSB-STACK), YYYY-M-D__topic for a snapshot."
        ),
    )
    _add_common_flags(p_set_new)
    p_set_new.add_argument("set_name", metavar="<name>", help="Set name")
    p_set_new.add_argument(
        "sessions", nargs="*", metavar="<session>",
        help="Sessions to include (UUID/prefix, name, path, or keyword). "
             "Optional when --from is given.",
    )
    p_set_new.add_argument(
        "--from", dest="from_view", choices=["current", "last"],
        default=None, metavar="{current,last}",
        help="Promote a whole view into the set: `current` freezes what is "
             "open right now (the live registry), `last` the previous boot "
             "epoch's roster. Curate afterwards by subtraction "
             "(`csb set rm <name> <session>`). Combinable with explicit "
             "sessions (union).",
    )

    p_set_list = set_sub.add_parser(
        "list",
        help="List named sets (and the most recent boot epoch)",
        description="List every named set with its member count, plus the "
                    "most recent boot epoch as the 'last' entry.",
    )
    _add_common_flags(p_set_list)
    p_set_list.add_argument(
        "--json", action="store_true", help="Machine-readable list",
    )

    p_set_add = set_sub.add_parser(
        "add",
        help="Add sessions to an existing named set",
        description="Add one or more sessions to a named set. Adding a "
                    "session that is already a member is a no-op.",
    )
    _add_common_flags(p_set_add)
    p_set_add.add_argument("set_name", metavar="<name>", help="Set name")
    p_set_add.add_argument(
        "sessions", nargs="+", metavar="<session>",
        help="Sessions to add (UUID/prefix, name, path, or keyword)",
    )

    p_set_rm = set_sub.add_parser(
        "rm",
        help="Remove sessions from a named set, or delete the whole set",
        description=(
            "Remove one or more sessions from a named set. With no "
            "sessions listed, deletes the entire set -- the sets file is "
            "committed in the user class, so a deletion is recoverable "
            "from your backup store's git history."
        ),
    )
    _add_common_flags(p_set_rm)
    p_set_rm.add_argument("set_name", metavar="<name>", help="Set name")
    p_set_rm.add_argument(
        "sessions", nargs="*", metavar="<session>",
        help="Sessions to remove; omit to delete the whole set",
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
            "Also force-rebuild the per-project FTS5 content indexes after "
            "the index rebuild. Fails soft -- an FTS5 problem warns but never "
            "fails the rebuild."
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
            "1 = error. Works without a git repo (index-only detection, #52). "
            "Hidden from `csb --help` because it's a hook "
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


def _split_passthrough(argv):
    """Split argv at the first standalone ``--`` token (#47).

    Returns ``(csb_argv, passthrough)`` -- everything before the ``--`` for
    csb's own parsing, everything after it forwarded verbatim to a wrapped
    subtool. No ``--`` -> ``(argv, [])``. Matches only the exact two-char
    token, never ``--db``/``--force``. Must run BEFORE ``_hoist_common_flags``
    so forwarded flags are never hoisted into csb's own options.
    """
    if "--" in argv:
        i = argv.index("--")
        return list(argv[:i]), list(argv[i + 1:])
    return list(argv), []


def _maybe_repoless_banner(args):
    """No-protection banner (v0.6.0, #52): unprotected is the EXCEPTION.

    Every interactive csb run against a claude dir with no git repo (and
    no recorded index-only sign-off) prints a loud stderr banner pointing
    at `csb setup`. It persists on EVERY run until one of the two
    resolved states is reached: a git repo exists (protection on), or the
    user explicitly ran `csb setup --index-only` (signed exception --
    banner silenced; `csb status` keeps reporting the state).

    Skipped for internal commands (`_check`), `--quiet` runs (the hooks
    fire `csb --quiet ...` in the background -- captured stderr is not
    user communication), and `setup` itself (it IS the fix).

    Cost: config load (already cheap) + one `git rev-parse` per
    interactive run while unresolved; the index-only ack short-circuits
    before the subprocess. Never raises -- the banner must not break a
    command.
    """
    try:
        command = getattr(args, "command", None) or ""
        if command.startswith("_") or command == "setup" or getattr(args, "quiet", False):
            return
        from pathlib import Path

        from .config import load_config, resolve_paths

        config = load_config(getattr(args, "claude_dir", None))
        if getattr(args, "claude_dir", None):
            config["claude_dir"] = args.claude_dir
        if str(config.get("backup_mode") or "full") == "index-only":
            return  # signed exception -- silenced
        claude_dir = resolve_paths(config)["claude_dir"]
        if not Path(claude_dir).is_dir():
            return

        from .git_ops import git_repo_state

        state, detail = git_repo_state(claude_dir)
        if state == "ok":
            return  # protected -- nothing to say

        bar = "=" * 68

        def _warn(text, style=None):
            # Red where rich + a real terminal allow it; plain otherwise
            # (pipes/captures/tests see the bare text either way).
            try:
                from rich.console import Console

                Console(file=sys.stderr, highlight=False, soft_wrap=True,
                        markup=False).print(text, style=style)
            except ImportError:
                print(text, file=sys.stderr)

        _warn(bar, "red")
        if state == "refused":
            # The repo EXISTS -- saying "no repository" here would be false
            # (and "nothing is preserved" doubly so: other contexts may be
            # committing fine). State the refusal and git's own words.
            _warn(f"csb: BACKUPS BLOCKED IN THIS SHELL -- a git repository "
                  f"EXISTS at {claude_dir}, but git refuses it here.",
                  "bold red")
            first = (detail or "").splitlines()[:1]
            if first:
                _warn(f"git said: {first[0]}")
            _warn("Your history is intact; do NOT re-initialize. Run `csb setup`")
            _warn("for diagnosis and fixes. Details: `csb status`.")
        elif state == "error":
            _warn(f"csb: BACKUP STATE UNKNOWN -- git itself failed: {detail}",
                  "bold red")
            _warn("Fix the git installation, then run `csb setup`.")
        else:
            _warn(f"csb: NO BACKUP PROTECTION -- {claude_dir} has no git repository.",
                  "bold red")
            _warn("Sessions are indexed at best; nothing is preserved or restorable.")
            _warn("Run `csb setup` for guided configuration (`csb setup --auto` for")
            _warn("no prompts). To stay unprotected ON PURPOSE, record your sign-off")
            _warn("with `csb setup --index-only` -- that is the only way to silence")
            _warn("this banner without a repo. Details: `csb status`.")
        _warn(bar, "red")
    except Exception:  # noqa: BLE001 -- the banner must never break a command
        pass


def main(argv=None):
    """Entry point for csb CLI."""
    # Hoist common flags from before the subcommand to after it.
    # This makes `csb --quiet backup` work the same as `csb backup --quiet`.
    if argv is None:
        argv = sys.argv[1:]

    # `--` passthrough (#47): everything after the first standalone `--` token
    # is forwarded verbatim to the wrapped subtool. Carve it off BEFORE
    # flag-hoisting so a forwarded flag (e.g. `csb resume x -- --db /other`)
    # is never mistaken for one of csb's own options. argparse never sees the
    # `--` -- the tail is reattached to the namespace as `args.passthrough`.
    argv, passthrough = _split_passthrough(argv)

    # `csb help` / `csb help <command>` -> argparse's --help forms. Users
    # type it constantly; "invalid choice: 'help'" is a hostile answer.
    if argv and argv[0] == "help":
        argv = argv[1:2] + ["--help"] if len(argv) > 1 else ["--help"]

    argv = _hoist_common_flags(argv)

    parser = build_parser()
    args = parser.parse_args(argv)
    args.passthrough = passthrough

    if args.command is None:
        parser.print_help()
        return 0

    # Only subtool-launchers can forward args; reject (never silently drop)
    # a passthrough given to any other command.
    if passthrough and args.command not in PASSTHROUGH_COMMANDS:
        capable = ", ".join(sorted(PASSTHROUGH_COMMANDS))
        print(
            f"csb {args.command}: `--` passthrough is only supported by: "
            f"{capable}. (Everything after `--` is forwarded to the wrapped "
            f"tool, which `{args.command}` does not launch.)",
            file=sys.stderr,
        )
        return 2

    # Onboarding enforcement: unprotected-and-unacknowledged cannot be
    # quiet -- banner every interactive run until resolved (v0.6.0, #52).
    _maybe_repoless_banner(args)

    # Import handlers lazily to keep startup fast
    if args.command == "setup":
        from .commands import cmd_setup
        return cmd_setup(args)
    elif args.command == "backup":
        from .commands import cmd_backup
        return cmd_backup(args)
    elif args.command == "list":
        from .commands import cmd_list
        return cmd_list(args)
    elif args.command == "tree":
        from .commands import cmd_tree
        return cmd_tree(args)
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
    elif args.command == "view":
        from .commands import cmd_view
        return cmd_view(args)
    elif args.command == "distill":
        from .commands import cmd_distill
        return cmd_distill(args)
    elif args.command == "scan":
        from .commands import cmd_scan
        return cmd_scan(args)
    elif args.command == "search":
        from .commands import cmd_search
        return cmd_search(args)
    elif args.command == "set":
        from .commands import cmd_set

        return cmd_set(args)
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

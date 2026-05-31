"""
Command implementations for the csb CLI.

Each cmd_* function receives parsed args and returns an exit code.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    load_config,
    resolve_paths,
    save_config,
    read_cleanup_period,
    DEFAULT_CLEANUP_PERIOD_DAYS,
    SETTINGS_NS,
    CLAUDE_SETTINGS_KEYS,
    is_settings_key,
    settings_key_name,
    get_settings_path,
    read_claude_setting,
    write_claude_setting,
    validate_cleanup_period,
)
from .git_ops import (
    git_commit_noise,
    git_commit_user,
    git_find_deleted_file,
    git_restore_file,
    git_status,
    is_git_repo,
)
from .lockfile import backup_lock
from .index import (
    get_active_session_ids,
    get_all_known_session_ids,
    get_indexed_mtime,
    get_session,
    get_stats,
    init_schema,
    list_sessions,
    mark_deleted,
    open_db,
    record_scan,
    register_session_sources,
    search_sessions,
    upsert_session,
)
from .sesslog_scanner import list_sesslog_folders, list_session_sources
from .metadata import (
    enrich_metadata,
    extract_metadata,
    read_name_cache,
    read_session_state,
)
from .scanner import scan_projects
from .timeline import format_session_line, format_timeline, render_timeline_rich, HAS_RICH


def _get_config(args):
    """Build resolved config from args + config file."""
    config = load_config(getattr(args, "claude_dir", None))
    if args.claude_dir:
        config["claude_dir"] = args.claude_dir
    if args.db:
        config["index_path"] = args.db
    return resolve_paths(config)


def _resolve_top_folders(args, config=None):
    """
    Decide the renderer's ``top_folders`` value from CLI args + config.

    Precedence (highest first):
      1. ``--all-folders``        -> None (show every folder)
      2. ``--top N``              -> N
      3. ``config["display_top_folders"]`` if set
      4. ``timeline.DEFAULT_TOP_FOLDERS`` (3)

    ``--all-folders`` wins defensively even if ``--top N`` is also set,
    in case a caller bypasses argparse's mutually exclusive group.

    The config value may be a non-positive int (treated as "show all",
    matching ``--all-folders``) or a malformed string (falls back to
    the module default rather than crashing the renderer).
    """
    from .timeline import DEFAULT_TOP_FOLDERS

    if getattr(args, "all_folders", False):
        return None  # show every folder
    top = getattr(args, "top", None)
    if top is not None:
        return top
    if config is not None and "display_top_folders" in config:
        try:
            cfg_top = int(config["display_top_folders"])
        except (TypeError, ValueError):
            return DEFAULT_TOP_FOLDERS
        if cfg_top < 0:
            return None  # negative = show all
        return cfg_top
    return DEFAULT_TOP_FOLDERS


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def cmd_backup(args) -> int:
    """Scan sessions, update index, optionally git commit."""
    config = _get_config(args)
    claude_dir = config["claude_dir"]
    quiet = getattr(args, "quiet", False)

    # Acquire lock (prevent concurrent cron runs). backup_lock now owns the
    # skip / stale-reclaim messaging (it has the lock's identity + age), so
    # we just honor the acquired flag here.
    with backup_lock(claude_dir, quiet=quiet) as acquired:
        if not acquired:
            return 0  # Not an error -- another instance is running
        return _cmd_backup_inner(args, config, claude_dir, quiet)


def _cmd_backup_inner(args, config, claude_dir, quiet) -> int:
    """Inner backup logic (runs under lock)."""

    # Verify git repo
    if not is_git_repo(claude_dir):
        print(f"Error: {claude_dir} is not a git repository.", file=sys.stderr)
        print("Initialize with: git -C ~/.claude init", file=sys.stderr)
        return 1

    # Open index
    conn = open_db(config["index_path"])
    init_schema(conn)

    # Scan for sessions
    sessions = scan_projects(claude_dir)
    now = _now_iso()

    # One-time discovery of sesslog folders -- maps session UUID to the
    # claude-session-logger folder that holds .convo*/.sesslog* transcripts.
    # Each per-session call below does an O(1) lookup against this map.
    sesslog_index = list_sesslog_folders(claude_dir)

    previously_known = get_active_session_ids(conn)
    found_ids = set()
    new_count = 0
    updated_count = 0
    sources_added_total = 0

    error_count = 0
    for i, sf in enumerate(sessions):
        found_ids.add(sf.session_id)
        is_new = sf.session_id not in previously_known

        try:
            # Extract metadata from JSONL
            meta = extract_metadata(sf.jsonl_path)
            meta.project = sf.project

            # Enrich with session-state info
            if sf.state_file:
                state = read_session_state(sf.state_file)
                name_cache = read_name_cache(sf.name_cache) if sf.name_cache else None
                enrich_metadata(meta, state, name_cache)

            # Upsert into index
            rel_path = str(sf.jsonl_path.relative_to(claude_dir))
            upsert_session(conn, meta, rel_path, sf.jsonl_size, sf.jsonl_mtime, now)

            # Register searchable transcript sources for this session
            # (Phase 1 of #3 content search). Fails-soft -- a per-session
            # scan error must not abort the whole backup.
            try:
                sesslog_folder = sesslog_index.get(sf.session_id)
                sources = list_session_sources(sf.jsonl_path, sesslog_folder)
                added, _removed = register_session_sources(
                    conn, sf.session_id, meta.project, sources, now,
                )
                sources_added_total += added
            except Exception as e:
                if not quiet:
                    print(
                        f"Warning: source registration failed for "
                        f"{sf.session_id}: {e}",
                        file=sys.stderr,
                    )

            if is_new:
                new_count += 1
            else:
                updated_count += 1
        except Exception as e:
            error_count += 1
            if not quiet:
                print(f"Warning: failed to process {sf.session_id}: {e}", file=sys.stderr)
            continue

        # Progress logging for large scans
        if not quiet and len(sessions) > 20 and (i + 1) % 20 == 0:
            print(f"  Progress: {i + 1}/{len(sessions)} sessions...", file=sys.stderr)

    # Detect deletions
    deleted_count = 0
    for missing_id in previously_known - found_ids:
        mark_deleted(conn, missing_id, now)
        deleted_count += 1

    # Git operations -- two separate commits: noise first, then user
    noise_hash = ""
    user_hash = ""
    if not args.no_commit and config["auto_commit"]:
        # Commit 1: noise (transient state, session data)
        noise_hash = git_commit_noise(claude_dir) or ""

        # Commit 2: user (configs, skills, agents, sesslogs)
        user_hash = git_commit_user(claude_dir) or ""

    # Use the most recent commit hash for the scan record
    commit_hash = user_hash or noise_hash

    # Record scan
    record_scan(conn, now, len(sessions), new_count, updated_count, deleted_count, commit_hash)
    conn.close()

    if not quiet:
        print(f"Scanned {len(sessions)} sessions: {new_count} new, {updated_count} updated, {deleted_count} deleted")
        if noise_hash:
            print(f"Noise commit:  {noise_hash[:8]}")
        if user_hash:
            print(f"User commit:   {user_hash[:8]}")
        if not noise_hash and not user_hash and not args.no_commit:
            print("No changes to commit.")

    return 0


def cmd_list(args) -> int:
    """Timeline view sorted by last-used."""
    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn)

    # v0.3.5: --deleted is two-valued. None / "only" / "all".
    deleted_mode = getattr(args, "deleted", None)
    sessions = list_sessions(
        conn,
        limit=args.n,
        show_deleted=(deleted_mode == "only"),
        show_all=(deleted_mode == "all"),
        filter_keyword=getattr(args, "filter", None),
        sort_key=getattr(args, "sort", "last-used"),
    )
    conn.close()

    cleanup_days = read_cleanup_period(config["claude_dir"])
    top_folders = _resolve_top_folders(args, config)

    if args.json:
        print(json.dumps(sessions, indent=2, default=str))
    elif HAS_RICH:
        render_timeline_rich(
            sessions, cleanup_days=cleanup_days, top_folders=top_folders,
            shortid=getattr(args, "shortid", False),
        )
    else:
        print(format_timeline(
            sessions, cleanup_days=cleanup_days, top_folders=top_folders,
            shortid=getattr(args, "shortid", False),
        ))

    return 0


def cmd_status(args) -> int:
    """Summary of sessions, deletions, git state."""
    config = _get_config(args)
    claude_dir = config["claude_dir"]

    conn = open_db(config["index_path"])
    init_schema(conn)
    stats = get_stats(conn)

    is_repo = is_git_repo(claude_dir)
    # Per-session "un-backed-up" detection (transcripts newer than the index).
    # Only meaningful inside a git repo (where backups commit); skip otherwise.
    unbacked = find_unbacked_sessions(conn, claude_dir) if is_repo else []
    conn.close()

    print(f"Claude Session Backup Status")
    print(f"  Claude dir:    {claude_dir}")
    print(f"  Git repo:      {'yes' if is_repo else 'NO'}")
    print(f"  Total sessions: {stats['total_sessions']}")
    print(f"  Active:         {stats['active_sessions']}")
    print(f"  Deleted:        {stats['deleted_sessions']}")
    print(f"  Projects:       {stats['projects']}")

    if stats["last_scan"]:
        scan = stats["last_scan"]
        print(f"  Last scan:      {scan['scanned_at']}")
        print(f"    Found: {scan['sessions_found']}, New: {scan['sessions_new']}, "
              f"Deleted: {scan['sessions_deleted']}")
        if scan.get("git_commit"):
            print(f"    Commit: {scan['git_commit'][:8]}")

    # Git status
    if is_repo:
        status = git_status(claude_dir)
        changed = len([l for l in status.split("\n") if l.strip()])
        if changed:
            print(f"  Uncommitted changes: {changed} files")
        else:
            print(f"  Working tree: clean")

        # Per-session backup freshness. Counts the live session honestly (its
        # transcript is mid-write) -> goes to 0 once all sessions close.
        if not unbacked:
            print(f"  Un-backed-up:   none")
        else:
            n = len(unbacked)
            print(f"  Un-backed-up:   {n} session"
                  f"{'s' if n != 1 else ''} (changed since last index -- run `csb backup`)")
            try:
                limit = int(config.get("status_unbacked_limit", 20))
            except (TypeError, ValueError):
                limit = 20
            if limit < 0:
                limit = n  # negative -> show all (matches display_top_folders)
            for sf, recorded in unbacked[:limit]:
                why = "never indexed" if recorded is None else "changed since last backup"
                name = ""
                if getattr(sf, "name_cache", None):
                    try:
                        name = (read_name_cache(sf.name_cache) or "").strip()
                    except Exception:
                        name = ""
                label = f"{name}  " if name else ""
                print(f"    {sf.session_id[:8]}  {label}({why})")
            if n > limit:
                print(f"    + {n - limit} more not shown")

    return 0


# Exit code for `_check` when un-backed-up sessions are found. Distinct from
# 1 (error) so callers (the SessionStart hook) can tell "gap" from "broke".
CHECK_GAP_EXIT = 10
# mtime slack (seconds) to absorb filesystem resolution / float jitter, so a
# freshly-backed-up session isn't flagged as stale by a sub-second difference.
_CHECK_MTIME_EPSILON = 1.0


def find_unbacked_sessions(conn, claude_dir, exclude=None):
    """Return [(SessionFile, recorded_mtime_or_None)] for sessions whose live
    JSONL is newer than the mtime recorded at the last backup scan (or that
    aren't in the index at all) -- i.e. sessions with un-backed-up changes.

    The single source of truth for "what isn't backed up", shared by the
    SessionStart hook's `_check` and (future) user-facing surfacing in
    ``csb status`` / ``csb list``. ``exclude`` is a set of full session ids to
    skip (e.g. the currently-active session, whose JSONL is mid-write).
    """
    exclude = set(exclude or [])
    stale = []
    for sf in scan_projects(claude_dir):
        if sf.session_id in exclude:
            continue
        recorded = get_indexed_mtime(conn, sf.session_id)
        if recorded is None or sf.jsonl_mtime > (recorded or 0) + _CHECK_MTIME_EPSILON:
            stale.append((sf, recorded))
    return stale


def cmd_check(args) -> int:
    """INTERNAL (`csb _check`): the SessionStart hook's gap detector. Reports
    sessions with un-backed-up changes; the hook uses the exit code to decide
    whether to warn + recover. Hidden from `csb --help`, but invokable by hand
    for maintainers / post-crash triage. ``--exclude <session-id>`` (repeatable)
    skips a session -- the hook excludes the currently-active one.

    Exit codes:
      0                -- clean: every session is backed up
      CHECK_GAP_EXIT   -- one or more sessions have un-backed-up changes
      1                -- error (not a git repo)
    """
    config = _get_config(args)
    claude_dir = config["claude_dir"]
    quiet = getattr(args, "quiet", False)
    exclude = getattr(args, "exclude", None)

    if not is_git_repo(claude_dir):
        print(f"Error: {claude_dir} is not a git repository.", file=sys.stderr)
        return 1

    conn = open_db(config["index_path"])
    init_schema(conn)
    stale = find_unbacked_sessions(conn, claude_dir, exclude)
    conn.close()

    if not stale:
        if not quiet:
            print("All sessions backed up.")
        return 0

    # Concise, user-facing summary (the hook puts this in a systemMessage).
    n = len(stale)
    print(f"csb: {n} session(s) with un-backed-up changes "
          f"(likely an unclean shutdown -- run `csb backup` to capture now):")
    for sf, recorded in stale[:5]:
        why = "never indexed" if recorded is None else "changed since last backup"
        print(f"  {sf.session_id[:8]}  ({why})")
    if n > 5:
        print(f"  ... and {n - 5} more")
    return CHECK_GAP_EXIT


def _resolve_session_or_exit(conn, query: str) -> tuple[str | None, int]:
    """Resolve a session-ID input via ``ids.resolve_session_id``.

    Returns ``(full_uuid, 0)`` on success. On any resolver failure, prints
    the appropriate error to stderr and returns ``(None, exit_code)`` --
    the caller closes the connection and propagates the exit code.

    Exit codes match standard conventions:
      - 1: no session found
      - 2: ambiguous match or invalid input
    """
    from .ids import (
        AmbiguousSessionID,
        InvalidSessionIDInput,
        NoSuchSessionID,
        format_ambiguous_error,
        resolve_session_id,
    )
    try:
        return resolve_session_id(conn, query), 0
    except AmbiguousSessionID as e:
        print(format_ambiguous_error(e), file=sys.stderr)
        return None, 2
    except NoSuchSessionID as e:
        print(f"No session found matching '{e.query}'", file=sys.stderr)
        return None, 1
    except InvalidSessionIDInput as e:
        print(f"Error: {e}", file=sys.stderr)
        return None, 2


def cmd_show(args) -> int:
    """Detailed session info with folder analysis."""
    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn)

    full_id, exit_code = _resolve_session_or_exit(conn, args.session_id)
    if full_id is None:
        conn.close()
        return exit_code

    session = get_session(conn, full_id)
    conn.close()

    if not session:
        # Resolver succeeded but get_session lost the row -- shouldn't happen
        # in normal use; keep the guard for paranoid safety.
        print(f"No session found matching '{args.session_id}'", file=sys.stderr)
        return 1

    _render_show(session)
    return 0


def _format_timestamp(iso_str: str | None) -> str:
    """Format an ISO 8601 UTC timestamp for human display.

    Returns "<local YYYY-MM-DD HH:MM:SS> (<tz>) [ <original ISO> ]". Keeps
    the original ISO string visible so users can grep the JSONL by exact
    timestamp without losing the local-time readability above it.

    Falls back to the raw input on parse failure (defensive: never throws).
    On Windows, strftime("%Z") often returns long names like "Eastern
    Daylight Time" -- we use the numeric UTC offset (e.g. "-04:00") in
    that case so the line stays compact.
    """
    if not iso_str:
        return "(unknown)"
    try:
        s = iso_str.replace("Z", "+00:00") if iso_str.endswith("Z") else iso_str
        dt_utc = datetime.fromisoformat(s)
        dt_local = dt_utc.astimezone()
        tz_label = dt_local.strftime("%Z")
        # Windows: replace long names with numeric offset
        if not tz_label or len(tz_label) > 5 or " " in tz_label:
            off = dt_local.strftime("%z")  # e.g. "-0400"
            tz_label = f"{off[:3]}:{off[3:]}" if len(off) == 5 else off
        local_part = dt_local.strftime("%Y-%m-%d %H:%M:%S")
        return f"{local_part} ({tz_label}) [ {iso_str} ]"
    except (ValueError, TypeError):
        return iso_str


def _render_show(session: dict) -> None:
    """Render the cmd_show output.

    Uses Rich colorization when available so the detail view matches the
    visual conventions of ``csb list`` and ``csb scan``. Falls back to
    plain text when Rich isn't installed.

    Color map (mirrors timeline.py):
      - Session name: bold cyan
      - Field labels: dim
      - Session ID: full UUID (deliberate -- detail view, copy-paste ready)
      - Start folder (most used): bold green
      - Other folders: grey70
      - Deleted marker: red
      - Resume hint: bold yellow
    """
    name = session["session_name"] or "(unnamed)"
    full_id = session["session_id"]
    folders = session.get("folders", []) or []

    if not HAS_RICH:
        # Plain-text fallback -- unchanged formatting
        print(f"Session: {name}")
        print(f"  ID:            {full_id}")
        print(f"  Project:       {session['project']}")
        print(f"  Start folder:  {session['start_folder'] or '(unknown)'}")
        print(f"  Started:       {_format_timestamp(session['started_at'])}")
        print(f"  Last active:   {_format_timestamp(session['last_active_at'])}")
        print(f"  Messages:      {session['message_count']}")
        print(f"  Tool calls:    {session['tool_call_count']}")
        print(f"  Claude ver:    {session['claude_version'] or '(unknown)'}")
        print(f"  JSONL path:    {session['jsonl_path']}")
        print(f"  JSONL size:    {session['jsonl_size']:,} bytes")
        if session.get("deleted_at"):
            print(f"  DELETED at:    {_format_timestamp(session['deleted_at'])}")
            print(f"  Restore with:  csb restore {full_id}")
        print(f"\n  Resume:        claude --resume {full_id}")
        if folders:
            print(f"\n  Working directories:")
            for f in folders:
                marker = " [start]" if f["is_start_folder"] else ""
                print(f"    {f['folder_path']}  ({f['usage_count']}x){marker}")
        return

    # Rich path
    from rich.console import Console
    from rich.text import Text
    console = Console()

    def field(label: str, value: str, value_style: str = "") -> None:
        line = Text()
        line.append(f"  {label:<14}", style="dim")
        line.append(value, style=value_style)
        console.print(line)

    title = Text()
    title.append("Session: ", style="dim")
    title.append(name, style="bold cyan")
    console.print(title)

    field("ID:", full_id, value_style="bold")
    field("Project:", session["project"], value_style="")
    field("Start folder:", session["start_folder"] or "(unknown)",
          value_style="bold green")
    field("Started:", _format_timestamp(session["started_at"]),
          value_style="dim")
    field("Last active:", _format_timestamp(session["last_active_at"]),
          value_style="dim")
    field("Messages:", str(session["message_count"]), value_style="")
    field("Tool calls:", str(session["tool_call_count"]), value_style="")
    field("Claude ver:", session["claude_version"] or "(unknown)",
          value_style="dim")
    field("JSONL path:", session["jsonl_path"], value_style="dim")
    field("JSONL size:", f"{session['jsonl_size']:,} bytes", value_style="dim")

    if session.get("deleted_at"):
        del_line = Text()
        del_line.append("  DELETED at:    ", style="red")
        del_line.append(_format_timestamp(session["deleted_at"]), style="red")
        console.print(del_line)
        restore_line = Text()
        restore_line.append("  Restore with:  ", style="dim")
        restore_line.append(f"csb restore {full_id}", style="bold yellow")
        console.print(restore_line)

    console.print()
    resume_line = Text()
    resume_line.append("  Resume:        ", style="dim")
    resume_line.append(f"claude --resume {full_id}", style="bold yellow")
    console.print(resume_line)

    if folders:
        console.print()
        console.print(Text("  Working directories:", style="dim"))
        # Identify the most-used folder for special styling
        max_count = max((f["usage_count"] for f in folders), default=0)
        for f in folders:
            is_start = bool(f["is_start_folder"])
            is_max = f["usage_count"] == max_count
            row = Text()
            row.append("    ")
            if is_start and is_max:
                row.append(f["folder_path"], style="bold green")
            elif is_max:
                row.append(f["folder_path"], style="bold green")
            elif is_start:
                row.append(f["folder_path"], style="white")
            else:
                row.append(f["folder_path"], style="grey70")
            row.append(f"  ({f['usage_count']}x)", style="dim")
            if is_start:
                row.append(" [start]", style="yellow")
            console.print(row)


def cmd_restore(args) -> int:
    """Restore deleted session from git history."""
    config = _get_config(args)
    claude_dir = config["claude_dir"]

    conn = open_db(config["index_path"])
    init_schema(conn)
    full_id, exit_code = _resolve_session_or_exit(conn, args.session_id)
    if full_id is None:
        conn.close()
        return exit_code

    session = get_session(conn, full_id)
    conn.close()

    if not session:
        print(f"No session found matching '{args.session_id}'", file=sys.stderr)
        return 1

    jsonl_path = session.get("jsonl_path")
    if not jsonl_path:
        print("No JSONL path recorded for this session.", file=sys.stderr)
        return 1

    # Check if file already exists
    full_path = Path(claude_dir) / jsonl_path
    if full_path.exists() and not session.get("deleted_at"):
        print(f"Session file already exists: {full_path}")
        print("This session doesn't appear to be deleted.")
        return 1

    # Find the file in git history
    commit = git_find_deleted_file(claude_dir, jsonl_path)
    if not commit:
        print(f"Could not find '{jsonl_path}' in git history.", file=sys.stderr)
        print("The file may never have been committed to git.", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"Would restore: {jsonl_path}")
        print(f"From commit:   {commit[:8]}")
        print(f"To:            {full_path}")
        return 0

    # Restore the file
    success = git_restore_file(claude_dir, commit, jsonl_path, str(full_path))
    if success:
        print(f"Restored: {jsonl_path}")
        print(f"From commit: {commit[:8]}")
        print(f"Session should now be visible in Claude Code.")
    else:
        print(f"Failed to restore '{jsonl_path}' from commit {commit[:8]}", file=sys.stderr)
        return 1

    return 0


def cmd_search(args) -> int:
    """Search transcript content (USER / AI / AGENT messages).

    Phase 1 of #3: walks ``session_sources``-recorded paths for every
    indexed session, parses USER / AI / AGENT[:subtype] blocks (in
    ``.convo`` / ``.sesslog``) or ``type:user`` / ``type:assistant``
    events (in JSONL), and prints hits with optional surrounding
    context. Replaces the v0.2.3 metadata-LIKE behavior (breaking
    change -- metadata search lives in ``csb list <filter>`` and
    ``csb scan <term>`` which already cover it).
    """
    from .search import search as run_search
    from .search_render import render

    # Force UTF-8 on stdout so cp1252 doesn't choke on em-dashes / smart
    # quotes that appear in transcripts. See CLAUDE.md's Windows
    # codepage section for the rationale.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn, quiet=getattr(args, "quiet", False))

    # Read cleanup_days once -- the --full-info renderer uses it for the
    # purge-countdown ("purge in 87d") field. Same source csb list uses.
    cleanup_days = read_cleanup_period(config["claude_dir"])

    # Resolve -C N into above/below
    above = args.before
    below = args.after
    if args.context is not None:
        above = below = args.context

    source_override = None if args.source == "auto" else args.source

    # v0.3.5: directory-scope (-d / -D) and --min-strength wiring. The
    # mutex on argparse guarantees at most one of -d/-D is set; we
    # additionally reject incompatible --source overrides (anything
    # other than auto or fts5) since dir-scope queries the per-project
    # FTS5 DBs exclusively -- there's no analog over .convo / .sesslog
    # / .jsonl yet.
    directories_below = getattr(args, "directories_below", None)
    directory_only = getattr(args, "directory_only", None)
    dir_path = directories_below or directory_only
    dir_scope: dict | None = None
    if dir_path is not None:
        if args.source not in ("auto", "fts5"):
            print(
                f"Error: -d/-D directory-scope is incompatible with "
                f"--source {args.source}. -d/-D requires FTS5; omit "
                f"--source (auto resolves to fts5) or pass --source fts5.",
                file=sys.stderr,
            )
            conn.close()
            return 2
        abs_path = str(Path(dir_path).resolve())
        dir_scope = {
            "abs_path": abs_path,
            "include_descendants": directory_only is None,  # -D excludes
            "min_strength": getattr(args, "min_strength", 1),
        }
        # The dispatcher always runs against FTS5 -- pin the source.
        source_override = "fts5"

    # Parse --session-id: comma-separated list of UUID prefixes. Empty
    # entries (e.g. trailing comma) and whitespace are tolerated.
    session_filter: list[str] = []
    raw = getattr(args, "session_id", None)
    if raw:
        session_filter = [p.strip() for p in raw.split(",") if p.strip()]

    # v0.3.5: --only {files,sessions} replaced the old --files-only /
    # --sessions-only pair. The output dispatcher below maps `only_mode`
    # straight to render()'s `mode` parameter.
    only_mode = getattr(args, "only", None)  # None | "files" | "sessions"

    # v0.3.5: --limit semantics flip when --only is set. Default mode
    # treats --limit as hits (one matched event = one output line).
    # --only sessions / --only files collapse hits into rows of a
    # higher-level unit, and the user's mental model is "give me N of
    # those" -- not "give me N raw hits, capped before they fill N
    # distinct rows." So when --only is set, we pull a generous
    # ceiling of raw hits from search() and cap by distinct units
    # below. ``user_limit`` is what the user actually typed.
    user_limit = args.limit
    effective_limit = args.limit
    if only_mode in ("sessions", "files"):
        effective_limit = 10_000  # generous ceiling; cap-by-unit below

    # --full-info is action="count": -f=1, -ff=2. Cap at 2 (current max level).
    full_info_level = min(getattr(args, "full_info", 0) or 0, 2)

    try:
        hits = list(run_search(
            conn,
            args.query,
            regex=args.regex,
            case_sensitive=args.case_sensitive,
            above=above,
            below=below,
            session_filter=session_filter or None,
            source_override=source_override,
            # v0.3.5: --deleted is two-valued. None / "only" / "all".
            include_deleted=(args.deleted == "all"),
            only_deleted=(args.deleted == "only"),
            limit=effective_limit,
            sort_key=getattr(args, "sort", "last-used"),
            fetch_folders=full_info_level >= 2,
            claude_dir=config["claude_dir"],
            dir_scope=dir_scope,
        ))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        conn.close()
        return 2
    conn.close()

    # v0.3.5: apply the unit-aware cap. In default mode this is a no-op.
    # In --only sessions / --only files, this turns the user's --limit
    # into "N output rows" (consistent with the rendered output unit).
    if only_mode in ("sessions", "files"):
        from .search import cap_hits_by_output_unit
        hits = cap_hits_by_output_unit(hits, user_limit, only_mode)

    if not hits:
        print(f"No content matches for {args.query!r}")
        print(
            "  Hint: for metadata search use 'csb list <filter>' or 'csb scan <term>'",
            file=sys.stderr,
        )
        return 0

    if args.json:
        mode = "json"
    elif only_mode == "files":
        mode = "files"
    elif only_mode == "sessions":
        mode = "sessions"
    else:
        mode = "human"

    use_color = None if not args.no_color else False
    # ``query`` is forwarded to render() for two purposes:
    #   - "sessions" mode uses it to compose the "Next: csb search ..." hint
    #   - "human" mode uses it (plus regex / case_sensitive) to highlight
    #     in-line matches with bold green
    # JSON / files modes ignore it.
    render(
        hits, mode=mode, use_color=use_color, full_match=args.full_match,
        shortid=getattr(args, "shortid", False),
        query=args.query,
        full_info=full_info_level,
        cleanup_days=cleanup_days,
        regex=args.regex,
        case_sensitive=args.case_sensitive,
    )

    return 0


def cmd_build_fts5(args) -> int:
    """Build / refresh per-project FTS5 content indices (Phase 2 of #3).

    Idempotent: by default only re-indexes sessions whose JSONL mtime
    has advanced past ``indexed_sessions.last_jsonl_mtime``. Use
    ``--force`` to rebuild unconditionally.

    Returns:
        0 on success (even if 0 sessions needed indexing)
        1 if FTS5 isn't available in the local SQLite build
        2 if --session-id was passed but doesn't resolve
    """
    from . import fts5_db, fts5_index

    # Bail early if the local SQLite lacks FTS5 (rare, but defensive).
    if not fts5_db.fts5_available():
        print(
            "Error: this Python's SQLite was built without FTS5 support. "
            "Try upgrading Python or installing a SQLite with FTS5 enabled.",
            file=sys.stderr,
        )
        return 1

    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn, quiet=getattr(args, "quiet", False))

    # Resolve --session-id (prefix) to a full UUID via the shared resolver.
    resolved_session: str | None = None
    raw_sid = getattr(args, "session_id", None)
    if raw_sid:
        full_id, exit_code = _resolve_session_or_exit(conn, raw_sid)
        if full_id is None:
            conn.close()
            return exit_code
        resolved_session = full_id

    claude_dir = Path(config["claude_dir"])
    quiet = getattr(args, "quiet", False)

    try:
        fts5_index.build_all(
            conn, claude_dir,
            project=getattr(args, "project", None),
            session_id=resolved_session,
            force=getattr(args, "force", False),
            quiet=quiet,
        )
    finally:
        conn.close()
    return 0


def cmd_rebuild_index(args) -> int:
    """Reconstruct SQLite index by re-scanning all sessions."""
    config = _get_config(args)
    db_path = config["index_path"]

    # Delete existing DB and rebuild
    db_file = Path(db_path)
    if db_file.exists():
        db_file.unlink()
        print(f"Removed existing index: {db_path}")

    # Re-run backup logic without git commit
    args.no_commit = True
    args.quiet = False
    result = cmd_backup(args)

    if result == 0:
        print("Index rebuilt successfully.")
    return result


def cmd_config(args) -> int:
    """View/edit configuration.

    A bare key addresses csb's own config (``session-backup-config.json``). A
    key in the ``settings:`` namespace addresses Claude Code's ``settings.json``
    -- chiefly ``settings:cleanupPeriodDays``, the session purge TTL. The two
    files never collide: bare -> ours, ``settings:`` -> Claude Code's.
    """
    config = load_config(getattr(args, "claude_dir", None))
    key = args.key

    # Route settings:* keys to Claude Code's settings.json.
    if key is not None and is_settings_key(key):
        return _config_claude_setting(args, config, key)

    if key is None:
        # Dump csb config as JSON on stdout (scriptable); surface the
        # settings: namespace on stderr so stdout stays pure JSON.
        print(json.dumps(config, indent=2))
        _print_settings_keys_hint(config)
        return 0

    # A bare key that names a known Claude Code setting is almost certainly a
    # mistake -- nudge toward the namespaced form rather than a dead end.
    if key in CLAUDE_SETTINGS_KEYS:
        print(
            f"'{key}' is a Claude Code setting (settings.json), not a csb "
            f"config key. Address it with the settings: namespace:\n"
            f"    csb config {SETTINGS_NS}{key}            # view\n"
            f"    csb config {SETTINGS_NS}{key} <value>    # change",
            file=sys.stderr,
        )
        return 1

    if args.value is None:
        # Show specific csb config key
        if key in config:
            val = config[key]
            print(json.dumps(val) if isinstance(val, (list, dict)) else str(val))
        else:
            print(f"Unknown config key: {key}", file=sys.stderr)
            return 1
        return 0

    # Set csb config value
    try:
        parsed = json.loads(args.value)
    except json.JSONDecodeError:
        parsed = args.value

    config[key] = parsed
    save_config(config, getattr(args, "claude_dir", None))
    print(f"Set {key} = {parsed}")
    return 0


def _config_claude_setting(args, config, namespaced_key) -> int:
    """Handle ``csb config settings:<key> [value]`` against Claude Code's
    settings.json (read on GET, read-merge-write on SET)."""
    bare = settings_key_name(namespaced_key)
    claude_dir = config["claude_dir"]
    settings_path = get_settings_path(claude_dir)

    if bare not in CLAUDE_SETTINGS_KEYS:
        known = ", ".join(SETTINGS_NS + k for k in CLAUDE_SETTINGS_KEYS)
        print(
            f"Unknown Claude Code setting: {namespaced_key}. "
            f"csb config can read/write: {known}.\n"
            f"(Other settings.json keys: edit {settings_path} directly.)",
            file=sys.stderr,
        )
        return 1

    meta = CLAUDE_SETTINGS_KEYS[bare]

    # GET
    if args.value is None:
        value, present = read_claude_setting(claude_dir, bare)
        if present:
            print(f"{value}  ({settings_path})")
        else:
            print(f"{meta['default']}  (default; not set in {settings_path})")
        if bare == "cleanupPeriodDays":
            _print_cleanup_guidance(value if present else meta["default"])
        return 0

    # SET -- validation is key-specific.
    if bare == "cleanupPeriodDays":
        return _set_cleanup_period(args, claude_dir)

    # No other writable keys yet (the registry is the allowlist).
    print(f"Setting {namespaced_key} is not writable via csb.", file=sys.stderr)
    return 1


def _set_cleanup_period(args, claude_dir) -> int:
    """Validate, guard, and write ``cleanupPeriodDays`` to settings.json."""
    value, error = validate_cleanup_period(args.value)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    # Guard the destructive value. 0 makes Claude Code stop writing transcripts
    # AND delete every existing one at next startup -- require explicit --force.
    if value == 0 and not getattr(args, "force", False):
        print(
            "Refusing to set cleanupPeriodDays = 0 without --force.\n"
            "  0 does NOT mean 'keep forever'. It disables session persistence: "
            "Claude Code stops writing transcripts and DELETES all existing "
            "ones at its next startup.\n"
            "  To effectively never purge, set a large number instead, e.g.:\n"
            f"      csb config {SETTINGS_NS}cleanupPeriodDays 36500\n"
            "  If you really do want to disable persistence, re-run with --force.",
            file=sys.stderr,
        )
        return 2

    current = read_cleanup_period(claude_dir)
    try:
        path = write_claude_setting(claude_dir, "cleanupPeriodDays", value)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Set cleanupPeriodDays = {value} in {path}")
    if value == 0:
        print(
            "  WARNING: session persistence is now OFF. Claude Code will delete "
            "existing transcripts at its next startup. Your csb git backups "
            "remain recoverable via 'csb restore'.",
            file=sys.stderr,
        )
    elif value < current:
        print(
            f"  Note: lowering the TTL from {current} to {value} days may purge "
            f"sessions older than {value} days at Claude Code's next start. "
            f"csb's git backups keep them recoverable ('csb restore').",
            file=sys.stderr,
        )
    print("  Takes effect the next time Claude Code starts.", file=sys.stderr)
    return 0


def _print_cleanup_guidance(value) -> None:
    """Explain what cleanupPeriodDays means + how to change it. Printed to
    stderr so a GET's stdout stays a clean, parseable value."""
    print(
        f"  Sessions are purged {value} day(s) after last use "
        f"(Claude Code default: {DEFAULT_CLEANUP_PERIOD_DAYS}).",
        file=sys.stderr,
    )
    print(
        f"  Change: csb config {SETTINGS_NS}cleanupPeriodDays <days>  (e.g. 365). "
        f"To effectively never purge, use a large number (e.g. 36500).",
        file=sys.stderr,
    )
    print(
        "  Caution: 0 disables session persistence -- Claude Code deletes all "
        "transcripts at next startup.",
        file=sys.stderr,
    )


def _print_settings_keys_hint(config) -> None:
    """On a bare ``csb config``, point at the settings: namespace (stderr, so
    the JSON dump on stdout stays clean and pipeable)."""
    claude_dir = config["claude_dir"]
    print("", file=sys.stderr)
    print(
        "Claude Code settings (separate file; address with the settings: "
        "namespace):",
        file=sys.stderr,
    )
    for bare, meta in CLAUDE_SETTINGS_KEYS.items():
        value, present = read_claude_setting(claude_dir, bare)
        shown = value if present else meta["default"]
        src = "" if present else " (default)"
        print(
            f"  {SETTINGS_NS}{bare} = {shown}{src}   # {meta['summary']}",
            file=sys.stderr,
        )


def cmd_resume(args) -> int:
    """Launch claude --resume with the full session UUID."""
    from .pathkit import derive_start_at

    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn)

    full_id, exit_code = _resolve_session_or_exit(conn, args.session_id)
    if full_id is None:
        conn.close()
        return exit_code

    session = get_session(conn, full_id)
    conn.close()

    if not session:
        print(f"No session found matching '{args.session_id}'", file=sys.stderr)
        return 1

    full_id = session["session_id"]
    name = session.get("session_name") or "(unnamed)"

    # Resolve cd target via pathkit (slug-decoded path = the only cwd whose
    # slug matches the JSONL's parent directory; per the upstream-source audit,
    # that's the only cwd from which `claude --resume <uuid>` will find the
    # file). Falls back to start_folder for sessions without a jsonl_path
    # (e.g., legacy index rows pre-#19).
    target = None
    jsonl_path = session.get("jsonl_path")
    if jsonl_path:
        first_cwd = session.get("start_folder")
        folders = session.get("folders") or []
        folder_usage = {f["folder_path"]: f.get("usage_count", 0) for f in folders}
        decoded = derive_start_at(jsonl_path, first_cwd=first_cwd, folder_usage=folder_usage)
        if decoded and not decoded.startswith("<"):
            target = decoded
    if target is None:
        target = session.get("start_folder")

    print(f"Resuming: {name}")
    print(f"  ID: {full_id}")
    if target:
        print(f"  cd {target}")
    print(f"  claude --resume {full_id}")
    print()

    # Launch claude --resume as a child process. We use subprocess.run with
    # cwd=target rather than os.chdir + os.execvp because Python's os.execvp
    # on Windows is _spawnv(P_OVERLAY, ...) -- the parent process exits and
    # spawns a child, but the controlling-TTY relationship doesn't transfer
    # cleanly. Symptom: claude TUI renders to stdout but stdin keystrokes
    # don't reach claude (they go into the void). subprocess.run inherits
    # the parent's stdin/stdout/stderr handles, which are still attached to
    # the user's terminal, so the TUI works correctly.
    #
    # Trade-off: the python process stays alive in memory while claude
    # runs (~30MB cost). When claude exits, its return code propagates.
    import subprocess
    try:
        result = subprocess.run(
            ["claude", "--resume", full_id],
            cwd=target if target else None,
            check=False,
        )
        return result.returncode
    except FileNotFoundError as e:
        # FileNotFoundError can fire from two places:
        #   (a) the cwd= path doesn't exist (target folder deleted)
        #   (b) the `claude` binary isn't in PATH
        # Disambiguate by checking whether the target itself is the issue.
        if target and not os.path.isdir(target):
            print(f"Error: cannot cd to {target}: {e}", file=sys.stderr)
            print("The folder may have been deleted. Run manually:", file=sys.stderr)
            print(f"  cd <correct-folder> && claude --resume {full_id}", file=sys.stderr)
            return 1
        print("Error: 'claude' command not found in PATH.", file=sys.stderr)
        print(f"Run manually: claude --resume {full_id}", file=sys.stderr)
        return 1
    except NotADirectoryError as e:
        # Edge case: target exists but isn't a directory (file with same name).
        print(f"Error: cannot cd to {target}: {e}", file=sys.stderr)
        return 1


def _resolve_directory_pattern(
    pattern: str,
    include_descendants: bool,
    cwd: Path | None = None,
) -> tuple[str, str | None, str | None, str | None]:
    """
    Resolve a user-supplied -d/-D PATTERN into SQL match criteria.

    Args:
        pattern: user input, e.g., ``"amdead"``, ``"amdead*"``, ``"C:\\code\\amdead"``.
            Trailing ``*`` is the only wildcard supported in v1.
        include_descendants: True for ``-d`` (folder + descendants), False for ``-D``.
        cwd: cwd for resolving relative patterns. Defaults to ``Path.cwd()``.

    Returns:
        ``(resolved_path, exact_value, like_match, like_exclude)`` where:
        - ``resolved_path``: human-readable absolute path for messages.
            For wildcard patterns, this is the parent + the literal prefix
            (with the wildcard stripped) -- not a real filesystem path.
        - ``exact_value``: pass to ``find_sessions_by_directory`` (or None).
        - ``like_match``: pass to ``find_sessions_by_directory`` (or None).
        - ``like_exclude``: pass to ``find_sessions_by_directory`` (or None).
    """
    from .index import escape_like_value

    if cwd is None:
        cwd = Path.cwd()
    cwd = Path(cwd)

    has_wildcard = pattern.endswith("*")
    bare = pattern[:-1] if has_wildcard else pattern

    # Resolve to an absolute path against the provided cwd (NOT process cwd
    # via .resolve(), so callers can control the resolution context for tests).
    bare_path = Path(bare) if bare else Path("")
    if bare_path.is_absolute():
        full = str(bare_path)
    else:
        joined = (cwd / bare_path) if bare else cwd
        full = str(joined)

    escaped_full = escape_like_value(full)
    sep = os.sep  # Platform separator

    if has_wildcard:
        if include_descendants:
            # -d amdead*: any path starting with the prefix (siblings + descendants)
            return full + "*", None, escaped_full + "%", None
        # -D amdead*: paths starting with prefix BUT no separator after
        return (
            full + "*",
            None,
            escaped_full + "%",
            escaped_full + "%" + sep + "%",
        )

    # No wildcard
    if include_descendants:
        # -d amdead: exact OR descendants
        descendants_pattern = escaped_full + sep + "%"
        return full, full, descendants_pattern, None
    # -D amdead: exact only
    return full, full, None, None


def _maybe_promote_dot_prefix(term: str | None) -> tuple[str | None, str | None]:
    """
    Auto-promote a ``./`` / ``.\\`` prefixed positional into an implicit ``-d`` pattern.

    Conventional shorthand: when a user types ``csb scan ./amdead`` or
    ``csb scan .\\amdead``, they are clearly indicating a path -- not a
    metadata search term. Same for a bare ``csb scan .`` (cwd).

    Returns ``(remaining_term, promoted_pattern)``:
      - If ``term`` had a path-prefix indicator -> promoted pattern (e.g.,
        ``"amdead"``, or ``"."`` for bare-dot inputs); remaining_term is None.
      - Otherwise -> term passes through; promoted_pattern is None.

    The caller decides whether to honor the promotion (it should be
    suppressed if the user already passed ``-d`` or ``-D`` explicitly).
    """
    if not term:
        return term, None
    if term in (".", "./", ".\\"):
        return None, "."
    if term.startswith("./") or term.startswith(".\\"):
        return None, term[2:]
    return term, None


def cmd_scan(args) -> int:
    """Find sessions by term, location, or both."""
    from .scanner import scan_for_path
    from .metadata import extract_metadata
    from .index import (
        find_sessions_by_directory,
        find_sessions_by_term,
        escape_like_value,
    )

    config = _get_config(args)
    quiet = getattr(args, "quiet", False)
    no_usage = getattr(args, "no_usage", False)
    top_n = _resolve_top_folders(args, config)

    # Resolve mode from argparse output
    directories_below = getattr(args, "directories_below", None)
    directory_only = getattr(args, "directory_only", None)
    start_dir_only = getattr(args, "start_dir_only", None)
    term = getattr(args, "term", None)
    term2 = getattr(args, "term2", None)

    # Two positionals are only valid when the FIRST is a `./` / `.\` / `.` shortcut.
    # In that case the second positional is the actual term filter (equivalent to
    # `csb scan -d <dirname> <term>`). Otherwise we reject -- a bare two-positional
    # form like `csb scan amdead my-paper` is ambiguous.
    if term2 is not None:
        first_is_dot_prefix = term in (".", "./", ".\\") or (
            term and (term.startswith("./") or term.startswith(".\\"))
        )
        if not first_is_dot_prefix:
            print(
                "Error: too many positional arguments. The two-positional form requires the "
                "first to be `./<dir>`, `.\\<dir>`, or bare `.` -- otherwise use "
                "`csb scan -d <dir> <term>` for the explicit form.",
                file=sys.stderr,
            )
            return 2

    # Auto-promote ./ or .\ prefixed positional to implicit -d
    # (only when -d/-D/-s are not already set explicitly).
    if directories_below is None and directory_only is None and start_dir_only is None:
        term, promoted = _maybe_promote_dot_prefix(term)
        if promoted is not None:
            directories_below = promoted
            # If the user gave two positionals (dot-prefix + term), the SECOND is
            # the actual term filter to apply within the path-strict scope.
            if term2 is not None:
                term = term2

    # Pattern + descendant flag (None pattern means: bare, treat as implicit "-d .")
    pattern: str | None = directories_below or directory_only or start_dir_only
    include_descendants = directory_only is None  # -D excludes descendants; -d/-s/bare include
    sql_start_folder_only = start_dir_only is not None

    is_path_mode = (pattern is not None) or (term is None)
    is_term_only = (pattern is None) and (term is not None)

    # ── Term-only mode: broad metadata search ──────────────────────
    if is_term_only:
        # Hint: if term coincides with a cwd subfolder, suggest -d
        cwd_match = (Path.cwd() / term)
        if cwd_match.is_dir():
            print(
                f"[info] '{term}' is also a folder under cwd. "
                f"Use 'csb scan -d {term}' for path-strict search.",
                file=sys.stderr,
            )

        try:
            conn = open_db(config["index_path"])
            init_schema(conn)
            results = find_sessions_by_term(conn, term, top_n=top_n)
            conn.close()
        except Exception:
            results = []

        return _render_scan_results(
            results, args, config,
            scope_label=f"matching '{term}'",
            quiet=quiet,
        )

    # ── Path-strict mode (or bare): -d / -D / no-args ──────────────
    if pattern is None:
        # Bare csb scan -> implicit -d .
        pattern_input = "."
        bare_mode = True
    else:
        pattern_input = pattern
        bare_mode = False

    resolved_path, exact_value, like_match, like_exclude = _resolve_directory_pattern(
        pattern_input, include_descendants
    )

    # Validate that the resolved path exists (warning, not blocker)
    has_wildcard = pattern_input.endswith("*")
    if not has_wildcard:
        if exact_value and not Path(exact_value).exists():
            print(
                f"[warning] '{pattern_input}' (resolved: {exact_value}) does not exist; "
                f"falling back to broad-term search if a term was provided.",
                file=sys.stderr,
            )
            if term is not None:
                # Fall back to broad term search
                try:
                    conn = open_db(config["index_path"])
                    init_schema(conn)
                    results = find_sessions_by_term(conn, term, top_n=top_n)
                    conn.close()
                except Exception:
                    results = []
                return _render_scan_results(
                    results, args, config,
                    scope_label=f"matching '{term}'",
                    quiet=quiet,
                )
            else:
                # No fallback term -> empty result set
                return _render_scan_results(
                    [], args, config,
                    scope_label=f"under {exact_value}",
                    quiet=quiet,
                )

    # Step 1: Filesystem scan (only when pattern resolves to a concrete path).
    # For wildcard patterns we skip the filesystem step -- scan_for_path doesn't
    # speak wildcards. SQLite covers these via the LIKE pattern.
    sessions_fs: list = []
    if not has_wildcard and exact_value:
        sessions_fs = scan_for_path(config["claude_dir"], exact_value)

    # Step 2: SQLite directory match (unless -NU)
    sql_results: list = []
    if not no_usage and (exact_value is not None or like_match is not None):
        try:
            conn = open_db(config["index_path"])
            init_schema(conn)
            sql_results = find_sessions_by_directory(
                conn, exact_value, like_match, like_exclude, top_n,
                start_folder_only=sql_start_folder_only,
            )
            conn.close()
        except Exception:
            pass  # Index may not exist yet -- graceful fallback

    # Merge: filesystem-scanned (with fresh metadata extraction) + SQLite-only
    seen_ids: set[str] = set()
    results: list = []

    for sf in sessions_fs:
        try:
            meta = extract_metadata(sf.jsonl_path)
            meta.project = sf.project
            results.append({
                "session_id": sf.session_id,
                "session_name": meta.session_name,
                "project": meta.project,
                "start_folder": meta.start_folder,
                "started_at": meta.started_at,
                "last_active_at": meta.last_active_at,
                "last_user_at": meta.last_user_at,
                "message_count": meta.message_count,
                "tool_call_count": meta.tool_call_count,
                "claude_version": meta.claude_version,
                "folders": [
                    {
                        "folder_path": path,
                        "usage_count": count,
                        "is_start_folder": path == meta.start_folder,
                    }
                    for path, count in meta.folder_usage.items()
                ],
                "jsonl_location": str(sf.jsonl_path),
                "jsonl_mtime": sf.jsonl_mtime,
            })
            seen_ids.add(sf.session_id)
        except Exception:
            continue

    for session in sql_results:
        if session["session_id"] not in seen_ids:
            seen_ids.add(session["session_id"])
            results.append(session)

    # Combined mode: also filter by term within the path-scoped results
    if term is not None:
        term_lower = term.lower()
        def _matches_term(s: dict) -> bool:
            for field in ("session_name", "project", "start_folder"):
                v = s.get(field) or ""
                if term_lower in str(v).lower():
                    return True
            for f in s.get("folders") or []:
                if term_lower in (f.get("folder_path") or "").lower():
                    return True
            return False
        results = [s for s in results if _matches_term(s)]

    # Build human-readable scope label
    if bare_mode:
        scope_label = f"under {resolved_path}"
    elif has_wildcard:
        scope_label = f"matching pattern {resolved_path}"
        if term:
            scope_label += f" filtered by '{term}'"
    else:
        scope_label = f"under {resolved_path}"
        if term:
            scope_label += f" filtered by '{term}'"

    return _render_scan_results(results, args, config, scope_label=scope_label, quiet=quiet)


def _render_scan_results(results, args, config, scope_label: str, quiet: bool) -> int:
    """Sort, trim, and render scan results. Shared by all scan modes."""
    no_usage = getattr(args, "no_usage", False)

    if not quiet:
        print(f"Scanning for sessions {scope_label}...\n")

    # Sort by last activity (most recent first)
    results.sort(
        key=lambda s: s.get("last_user_at") or s.get("last_active_at") or "",
        reverse=True,
    )

    total_found = len(results)
    results = results[:args.n]

    if not results:
        print("  No sessions found.")
        if no_usage:
            print("  Tip: try without -NU to also search by folder usage.")
        return 0

    cleanup_days = read_cleanup_period(config["claude_dir"])
    top_folders = _resolve_top_folders(args, config)

    if args.__dict__.get("json"):  # not all parsers have --json yet for scan
        print(json.dumps(results, indent=2, default=str))
        return 0

    print(f"Found {total_found} session(s) {scope_label}" +
          (f" (showing top {args.n}):" if total_found > args.n else ":"))
    print()

    if HAS_RICH:
        render_timeline_rich(
            results, cleanup_days=cleanup_days, top_folders=top_folders,
            shortid=getattr(args, "shortid", False),
        )
    else:
        print(format_timeline(
            results, cleanup_days=cleanup_days, top_folders=top_folders,
            shortid=getattr(args, "shortid", False),
        ))

    return 0

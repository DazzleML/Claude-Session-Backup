"""
Command implementations for the csb CLI.

Each cmd_* function receives parsed args and returns an exit code.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
    SESSION_HISTORY_SCOPES,
    categorize_path_for_uuid,
    ensure_gitattributes,
    git_commit_noise,
    git_commit_user,
    git_find_deleted_file,
    git_find_jsonl_by_uuid,
    git_last_commit_time,
    git_ls_tree_for_uuid,
    git_ls_tree_symlinks_for_uuid,
    git_restore_file,
    git_show_file_bytes,
    git_status,
    is_git_repo,
)
from .lockfile import backup_lock
from .index import (
    count_deleted_with_filter,
    find_sessions_by_folder_usage,
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
    restore_deleted_snapshot,
    search_sessions,
    snapshot_deleted_sessions,
    upsert_session,
)
from .sesslog_scanner import list_sesslog_folders, list_session_sources
from .metadata import (
    enrich_metadata,
    extract_metadata,
    read_name_cache,
    read_session_state,
)
from .pathkit import ClaudePaths
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

    # Defense in depth: ensure .gitattributes has the csb-managed block that
    # marks session JSONLs / sidecars as binary (no autocrlf, no eol filter).
    # Without this, a future commit on a host with `core.autocrlf=true` could
    # store CRLF-corrupted blobs that no amount of restore-side care can fix.
    # Idempotent -- only writes when the block is missing.
    ensure_gitattributes(claude_dir)

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

            # Upsert into index. Store rel_path with forward slashes so it
            # works directly with `git show <commit>:<path>` (which rejects
            # backslash separators on Windows). Path operations downstream
            # accept either separator. ClaudePaths.rel() also survives a
            # junction/symlink claude_dir, where a resolved-vs-raw prefix
            # mismatch made bare relative_to() raise ValueError (#46).
            rel_path = ClaudePaths.from_dir(claude_dir).rel(sf.jsonl_path)
            # Restore-verify gate (v0.3.16): only let a reappeared JSONL
            # clear an existing deleted_at if it's a genuine transcript
            # (>=1 parsed event). A stub / garbage file (event_count == 0,
            # e.g. left by a botched restore) preserves the deleted state
            # instead of silently un-deleting the session.
            upsert_session(
                conn, meta, rel_path, sf.jsonl_size, sf.jsonl_mtime, now,
                is_valid_transcript=(meta.event_count > 0),
            )

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

    # Distill-on-backup (#12): policy "always" regenerates stale canonical
    # chat-log files for the sessions just scanned. Runs BEFORE the git
    # commits so fresh distilled files ride the noise commit. Fails-soft.
    if str(config.get("distill_policy") or "on-demand") == "always":
        distilled_n = _refresh_distilled_files(conn, config, sessions, quiet)
        if distilled_n and not quiet:
            print(
                f"Distilled {distilled_n} session"
                f"{'s' if distilled_n != 1 else ''} -> "
                f"{Path(config['claude_dir']) / 'distilled'}"
            )

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


_warned_all_deprecated = False


def deleted_mode(args) -> str:
    """Normalize the shared ``--deleted [only|all]`` flag to a mode string:
    ``"live"`` (flag absent), ``"only"``, or ``"all"`` (#41).

    The single interpretation point for every command that filters on
    deletion state. Also absorbs scan's deprecated ``--all`` boolean alias
    (maps to ``"all"`` with a one-time deprecation warning; removal: 0.4).
    The companion attach-side helper is ``cli.add_deleted_flag`` -- the pair
    is split across modules only to avoid a cli->commands import cycle.
    """
    global _warned_all_deprecated
    if getattr(args, "all", False):
        if not _warned_all_deprecated:
            print(
                "Warning: --all is deprecated; use --deleted all "
                "(removal planned for 0.4).",
                file=sys.stderr,
            )
            _warned_all_deprecated = True
        return "all"
    return getattr(args, "deleted", None) or "live"


def cmd_list(args) -> int:
    """Timeline view sorted by last-used."""
    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn)

    mode = deleted_mode(args)
    filter_keyword = getattr(args, "filter", None)
    sessions = list_sessions(
        conn,
        limit=args.n,
        show_deleted=(mode == "only"),
        show_all=(mode == "all"),
        filter_keyword=filter_keyword,
        sort_key=getattr(args, "sort", "last-used"),
    )

    # Filter-aware "N deleted hidden" footer (Phase 3 / #27).
    # Only emit in default live-only mode. When --deleted only/all is
    # passed, the deleted rows are already on screen, so the footer would
    # be noise. Suppressed when count is zero -- don't say "0 deleted hidden".
    deleted_hidden_count = 0
    if mode == "live":
        deleted_hidden_count = count_deleted_with_filter(conn, filter_keyword)

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

    if deleted_hidden_count > 0:
        # Echo the user's filter back so the count's scope is unambiguous.
        # Example: `csb list amd` -> "(3 deleted sessions matching 'amd'
        # hidden -- run `csb list amd --deleted` to see, `csb restore <id>`
        # to recover)".
        word = "session" if deleted_hidden_count == 1 else "sessions"
        matching = f" matching '{filter_keyword}'" if filter_keyword else ""
        cmd_suffix = f" {filter_keyword}" if filter_keyword else ""
        print()
        print(
            f"({deleted_hidden_count} deleted {word}{matching} hidden -- "
            f"run `csb list{cmd_suffix} --deleted` to see, "
            f"`csb restore <id>` to recover)"
        )

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
    SessionStart hook's `_check` and ``csb status``'s un-backed-up section
    (and, eventually, ``csb list``). ``exclude`` is a set of full session ids
    to skip (e.g. the currently-active session, whose JSONL is mid-write).
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


def _resolve_session_or_exit(
    conn, query: str, miss_ok: bool = False
) -> tuple[str | None, int]:
    """Resolve a session-ID input via ``ids.resolve_session_id``.

    Returns ``(full_uuid, 0)`` on success. On any resolver failure, prints
    the appropriate error to stderr and returns ``(None, exit_code)`` --
    the caller closes the connection and propagates the exit code.

    ``miss_ok=True`` (#42): a plain no-match or a non-ID-shaped input
    returns ``(None, 0)`` SILENTLY so the caller can fall through to the
    multi-modal ``_resolve_session_query`` (names, paths, keywords).
    Ambiguous ID matches still print and return exit code 2 -- ambiguity
    is a real error in every mode.

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
        if miss_ok:
            return None, 0
        print(f"No session found matching '{e.query}'", file=sys.stderr)
        return None, 1
    except InvalidSessionIDInput as e:
        if miss_ok:
            return None, 0
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
    """Restore deleted session from git history.

    Lookup order:
      1. SQLite index -- if a session row exists, use its `jsonl_path`.
      2. Git history fallback (#28) -- if no DB row, walk
         `git log --all -- 'projects/*/<uuid>.jsonl'` to find a path.
         Multiple matches surface as a slug-collision error with the
         candidate list -- caller picks the right one and re-runs.

    The fallback path requires the FULL UUID (no prefix match against the
    DB is possible when the DB has no row). If a prefix is supplied and no
    DB row matches, the fallback can't help -- caller must supply the full
    UUID explicitly.
    """
    config = _get_config(args)
    claude_dir = config["claude_dir"]

    conn = open_db(config["index_path"])
    init_schema(conn)

    # Resolve the input (#44 -- restore was the last command without the
    # shared multi-modal resolver). Order:
    #   1. Strict ID resolver (prefix/suffix; ambiguity still exits 2).
    #      A plain miss falls through silently (miss_ok) -- the input may
    #      be a session NAME, path, folder, or keyword.
    #   2. Shared multi-modal resolver (same surface as resume/view/
    #      distill). Multi-match -> candidates timeline, exit 1.
    #   3. Still nothing + input is a full UUID: git-history fallback
    #      (#28) below -- the DB may be missing the row while git still
    #      has the JSONL. Names can't use the fallback (filenames carry
    #      UUIDs, not titles).
    full_id, exit_code = _resolve_session_or_exit(
        conn, args.session_id, miss_ok=True
    )
    if full_id is None and exit_code:
        conn.close()
        return exit_code
    session = get_session(conn, full_id) if full_id else None
    if session is None:
        result, method = _resolve_session_query(
            args.session_id, conn, claude_dir
        )
        if isinstance(result, list):
            label = method.split(":", 1)[1] if ":" in method else method
            _show_view_candidates(result, args.session_id, label)
            conn.close()
            return 1
        if result is not None:
            session = result
            full_id = session["session_id"]
    conn.close()

    # Resolve jsonl_path via DB row if present, otherwise via git history.
    jsonl_path: str | None = None
    if session:
        jsonl_path = session.get("jsonl_path")
        if not jsonl_path:
            print("No JSONL path recorded for this session.", file=sys.stderr)
            return 1
    else:
        # DB has no row. Fall back to git history (#28). Requires a full UUID
        # because git_find_jsonl_by_uuid does an exact filename match.
        if not _looks_like_full_uuid(args.session_id):
            print(
                f"No session found matching '{args.session_id}' in DB. "
                f"To search git history as a fallback, supply the full UUID "
                f"(36 chars, hyphenated).",
                file=sys.stderr,
            )
            return 1

        candidates = git_find_jsonl_by_uuid(claude_dir, args.session_id)
        if not candidates:
            print(
                f"No session found matching '{args.session_id}' in DB or git history.",
                file=sys.stderr,
            )
            return 1
        if len(candidates) > 1:
            print(
                f"Slug collision: session '{args.session_id}' was committed at "
                f"multiple paths over its lifetime.",
                file=sys.stderr,
            )
            print("Candidate paths:", file=sys.stderr)
            for c in candidates:
                print(f"  {c}", file=sys.stderr)
            print(
                "Manual restore required -- run `git -C <claude_dir> log --all -- '<path>'` "
                "for each candidate to identify the right one, then "
                "`git show <commit>:<path>` to recover.",
                file=sys.stderr,
            )
            return 1
        jsonl_path = candidates[0]

    # Find the commit to restore from. We use the parent of the deletion
    # commit, so the JSONL (and every sidecar that lived alongside it)
    # is present in that tree.
    commit = git_find_deleted_file(claude_dir, jsonl_path)
    if not commit:
        print(f"Could not find '{jsonl_path}' in git history.", file=sys.stderr)
        print("The file may never have been committed to git.", file=sys.stderr)
        return 1

    # Delegate the file-level restore work (slug extraction, scope
    # enumeration, per-file overwrite policy, lock acquisition, write
    # loop) to the shared `_restore_session` helper. cmd_restore is now
    # a thin wrapper that handles CLI arg parsing + DB/git resolution
    # (above) and output formatting (below). cmd_resume + cmd_view
    # call the same helper, so the restore policy stays consistent across
    # callers.
    resolved_uuid = full_id or args.session_id
    result = _restore_session(
        claude_dir=claude_dir,
        full_uuid=resolved_uuid,
        jsonl_path=jsonl_path,
        commit=commit,
        jsonl_only=getattr(args, "jsonl_only", False),
        force=getattr(args, "force", False),
        quiet=getattr(args, "quiet", False),
        dry_run=args.dry_run,
        db_mtime=(session.get("jsonl_mtime") or None) if session else None,
    )
    if result is None:
        return 1  # error already printed by helper

    if args.dry_run:
        nw = len(result.write_list)
        np_ = len(result.preserve_list)
        wplural = "s" if nw != 1 else ""
        pplural = "s" if np_ != 1 else ""
        print(f"Would restore {nw} file{wplural} from commit {result.commit_short}:")
        for p in result.write_list:
            print(f"  {p}")
        if result.preserve_list:
            print(f"Would preserve {np_} present file{pplural} (use --force to overwrite from git):")
            for p in result.preserve_list:
                print(f"  {p}")
        if result.recreated_symlinks:
            nr = len(result.recreated_symlinks)
            print(f"Would recreate {nr} symlink{'s' if nr != 1 else ''} "
                  f"(transcript.jsonl -> the restored transcript; "
                  f"others verbatim from git):")
            for p in result.recreated_symlinks:
                print(f"  {p}")
        if result.write_list:
            nt = len(result.write_list)
            line = (f"Would apply derived original timestamps to {nt} "
                    f"file{'s' if nt != 1 else ''} (mtime from index/"
                    f"transcript events/git history; Windows creation time "
                    f"from the first event).")
            try:
                db_mt = float(session.get("jsonl_mtime") or 0) if session else 0.0
            except (TypeError, ValueError):
                db_mt = 0.0
            if db_mt:
                iso = datetime.fromtimestamp(db_mt).strftime("%Y-%m-%d %H:%M:%S")
                line += f" Transcript mtime would be {iso} (from index)."
            print(line)
        if session is None:
            print("Source: git history (no DB row -- fallback mode)")
        return 0

    nw = len(result.write_list)
    np_ = len(result.preserve_list)
    wplural = "s" if nw != 1 else ""
    pplural = "s" if np_ != 1 else ""
    if result.failed:
        print(
            f"Restored {result.wrote}/{nw} file{wplural} from commit {result.commit_short}; "
            f"{len(result.failed)} failure{'s' if len(result.failed) != 1 else ''}:",
            file=sys.stderr,
        )
        for p in result.failed:
            print(f"  {p}", file=sys.stderr)
        return 1

    if result.wrote > 0:
        print(f"Restored {result.wrote} file{wplural} from commit {result.commit_short}.")
    elif result.preserve_list:
        print(
            f"Nothing to restore: all {np_} expected file{pplural} are "
            f"already on disk. Use --force to overwrite from git history "
            f"if you need to revert local changes."
        )
    else:
        print(f"Nothing to restore for commit {result.commit_short}.")
    if result.preserve_list and result.wrote > 0:
        print(
            f"Preserved {np_} present file{pplural} (kept on-disk content; "
            f"use --force to overwrite from git)."
        )
    if result.recreated_symlinks:
        nr = len(result.recreated_symlinks)
        print(
            f"Recreated {nr} symlink{'s' if nr != 1 else ''} "
            f"(transcript.jsonl -> the restored transcript; others verbatim)."
        )
    if result.skipped_symlinks:
        ns = len(result.skipped_symlinks)
        print(
            f"Skipped {ns} symlink{'s' if ns != 1 else ''} "
            f"(could not recreate -- no symlink privilege?)."
        )
    if result.times_applied:
        nt = result.times_applied
        print(
            f"Applied original timestamps to {nt} file{'s' if nt != 1 else ''} "
            f"(derived from index, transcript events, and git history)."
        )
    if session is None:
        print("(restored via git-history fallback -- DB had no row for this UUID)")
    if result.wrote > 1 and not getattr(args, "jsonl_only", False):
        slug = _extract_slug_from_jsonl_path(jsonl_path)
        cats = _categorize_restored_paths(result.write_list, slug, resolved_uuid)
        for label, count in cats:
            print(f"  {label}: {count}")
    # Restore-verify gate (v0.3.16): warn if the restored transcript is a stub.
    if result.transcript_valid is False:
        print(
            f"Warning: the restored transcript looks like a stub "
            f"({result.transcript_warning}). It may have been committed in a "
            f"degraded state; the conversation -- if it was ever captured -- "
            f"may only exist in claude-session-logger's sesslogs. "
            f"This session will stay marked deleted until a real transcript "
            f"is present.",
            file=sys.stderr,
        )
    print("Session should now be visible in Claude Code.")
    return 0


# ── Shared restore helper (v0.3.14 extraction) ─────────────────────────
#
# Single source of truth for the file-level restore policy. Used by:
#   - cmd_restore     -- the `csb restore <uuid>` command
#   - cmd_resume      -- prompts-to-restore when the session is pruned
#   - cmd_view        -- same when viewing a pruned session (#14 / #34)
#   - cmd_distill     -- same when distilling a pruned session (#12)
#
# Callers are responsible for resolving the UUID, finding `jsonl_path` (DB
# row OR git-history fallback), and finding `commit` (parent-of-deletion).
# The helper handles everything from there: slug extraction, SESSION-HISTORY
# scope enumeration, per-file overwrite policy, `backup_lock` acquisition,
# and the byte-pure write loop.

from dataclasses import dataclass, field


@dataclass
class RestoreResult:
    """Structured outcome of a single _restore_session call."""
    wrote: int = 0                    # number of files actually written
    failed: list[str] = field(default_factory=list)
    write_list: list[str] = field(default_factory=list)     # files that needed writing
    preserve_list: list[str] = field(default_factory=list)  # files preserved (already on disk)
    skipped_symlinks: list[str] = field(default_factory=list)  # git symlinks NOT restored (v0.3.15)
    recreated_symlinks: list[str] = field(default_factory=list)  # transcript.jsonl symlinks recreated (#38, v0.3.17)
    commit_short: str = ""            # short hash for output
    error: Optional[str] = None       # set on unrecoverable errors (e.g. bad slug)
    # Restore-verify gate (v0.3.16): after writing, did the main transcript
    # come out as a real JSONL? None = not checked (dry-run / nothing written);
    # True/False = checked. transcript_warning carries the reason when False.
    transcript_valid: Optional[bool] = None
    transcript_warning: str = ""
    # Timestamp fidelity (#40, v0.3.19): how many restored files got their
    # derived original timestamps reapplied (mtime always; Windows creation
    # time when pywin32 is available).
    times_applied: int = 0


def _restore_session(
    *,
    claude_dir: str,
    full_uuid: str,
    jsonl_path: str,
    commit: str,
    jsonl_only: bool = False,
    force: bool = False,
    quiet: bool = False,
    dry_run: bool = False,
    db_mtime: Optional[float] = None,
) -> Optional[RestoreResult]:
    """Core restore logic shared by cmd_restore / cmd_resume / cmd_view.

    Returns a RestoreResult, or None if the slug couldn't be extracted from
    jsonl_path (caller already printed an error and should return 1).

    Args:
        claude_dir: ~/.claude root (or whatever csb operates against).
        full_uuid: resolved full session UUID (not a prefix).
        jsonl_path: claude_dir-relative path to the main JSONL transcript,
            in `projects/<slug>/<uuid>.jsonl` shape. Caller resolves this
            from the DB row OR via `git_find_jsonl_by_uuid` fallback.
        commit: commit-ish to restore from. Caller obtains via
            `git_find_deleted_file(claude_dir, jsonl_path)`.
        jsonl_only: if True, restore only the main transcript JSONL
            (skip the session subtree and logger sidecars).
        force: if True, overwrite present on-disk files from git.
            Default behavior preserves on-disk content (idempotent;
            never clobbers local content with newer-than-git writes).
        quiet: passed through to `backup_lock` for quieter output.
        dry_run: if True, populate write_list/preserve_list but don't
            actually write anything (returns RestoreResult with wrote=0).
        db_mtime: the index's recorded ``jsonl_mtime`` for this session
            (survives deletion), used as the preferred mtime source for
            the main transcript (#40). None -> derive from content.

    The per-file overwrite policy (v0.3.12+): missing files always
    restored; present files preserved unless --force.
    """
    slug = _extract_slug_from_jsonl_path(jsonl_path)
    if not slug:
        print(
            f"Could not derive project slug from jsonl_path {jsonl_path!r}; "
            f"expected 'projects/<slug>/<uuid>.jsonl' shape.",
            file=sys.stderr,
        )
        return None

    # Discovery: enumerate every SESSION-HISTORY path at this commit via
    # the table-driven `git_ls_tree_for_uuid` (or just the JSONL when the
    # caller wants pre-v0.3.12 jsonl-only behavior).
    symlink_paths: set[str] = set()
    if jsonl_only:
        paths_to_restore = [jsonl_path]
    else:
        paths_to_restore = git_ls_tree_for_uuid(
            claude_dir, commit, slug, full_uuid
        )
        # Symlinks (git mode 120000) must NOT be restored: a symlink blob's
        # content is the link-target path, and writing it -- especially
        # through an existing on-disk link -- clobbers the target. The
        # logger regenerates its own transcript.jsonl symlink, so skipping
        # loses nothing. See the v0.3.15 symlink-clobber DWP.
        symlink_paths = git_ls_tree_symlinks_for_uuid(
            claude_dir, commit, slug, full_uuid
        )
        if not paths_to_restore:
            # Defensive: we found the JSONL above so it must be in the
            # tree, but just in case the helper returned nothing,
            # restore at least the JSONL.
            paths_to_restore = [jsonl_path]
        elif jsonl_path not in paths_to_restore:
            # Belt-and-braces: include the JSONL even if discovery missed it.
            paths_to_restore.append(jsonl_path)
        paths_to_restore = sorted(set(paths_to_restore))

    # Per-file overwrite policy:
    #   - Symlink entry        -> NEVER written byte-wise. ALL symlinks are
    #     RECREATED as real links after the write loop (#38 transcript-with-
    #     recomputed-target, #39 everything-else-verbatim); creation failure
    #     (no privilege) falls back to skip-and-report.
    #   - File missing on disk  -> restore from git (the whole point)
    #   - File present on disk  -> PRESERVE by default; --force opts in.
    write_list: list[str] = []
    preserve_list: list[str] = []
    symlink_candidates: list[str] = []
    for p in paths_to_restore:
        if p in symlink_paths:
            symlink_candidates.append(p)
            continue
        full = Path(claude_dir) / p
        # A dangling on-disk symlink reads as "missing" via exists() (which
        # follows the link to a non-existent target). Treat present-as-link
        # as present so the overwrite policy + write-guard apply.
        present = full.exists() or os.path.islink(full)
        if present:
            if force:
                write_list.append(p)
            else:
                preserve_list.append(p)
        else:
            write_list.append(p)

    result = RestoreResult(
        write_list=write_list,
        preserve_list=preserve_list,
        commit_short=commit[:8],
    )

    if dry_run:
        # Every symlink candidate would be recreated (#39): transcript links
        # with a recomputed target, anything else verbatim from its blob.
        # (Whether creation succeeds -- privilege -- is only knowable live.)
        result.recreated_symlinks.extend(symlink_candidates)
        return result

    # Real restore: acquire backup_lock for the whole multi-file write so
    # a concurrent `csb backup` doesn't snapshot a half-restored state.
    with backup_lock(claude_dir, quiet=quiet) as acquired:
        if not acquired:
            result.failed = list(write_list)
            return result
        for p in write_list:
            full = Path(claude_dir) / p
            if git_restore_file(claude_dir, commit, p, str(full)):
                result.wrote += 1
            else:
                result.failed.append(p)

        # Symlink handling (#38 + #39): we NEVER restore a symlink's blob
        # (that was the v0.3.15 clobber); every mode-120000 entry is
        # RECREATED as a real link via dazzle_filekit (cross-platform,
        # graceful no-privilege fallback). The logger's transcript.jsonl
        # gets a recomputed current-machine target (relocation-robust);
        # any other link is recreated verbatim from its blob target text.
        # Creation failure (no privilege) -> skip-and-report.
        for p in symlink_candidates:
            if _is_transcript_symlink(p, full_uuid):
                ok = _recreate_transcript_symlink(claude_dir, p, slug, full_uuid)
            else:
                ok = _recreate_symlink_verbatim(claude_dir, p, commit)
            if ok:
                result.recreated_symlinks.append(p)
            else:
                result.skipped_symlinks.append(p)

        # Timestamp fidelity (#40): a restore should be byte+METADATA-exact.
        # Reapply each written file's derived original times so the recovered
        # session is indistinguishable from never-deleted in any
        # filesystem-time view (and so the FTS5 mtime-freshness check doesn't
        # false-fire on recovery -- the #36 root cause). Derived sources only
        # (index mtime, transcript event timestamps, git commit dates) --
        # content-internal, so this works retroactively for all git history.
        result.times_applied = _apply_restored_times(
            claude_dir=claude_dir,
            written=[p for p in write_list if p not in result.failed],
            jsonl_path=jsonl_path,
            commit=commit,
            db_mtime=db_mtime,
        )

    # Restore-verify gate (v0.3.16): confirm the main transcript came out as
    # a real JSONL. If git only had a stub/garbage blob for it, the restore
    # "succeeded" mechanically but the user should be told the recovered
    # transcript isn't a real conversation (and the next backup will, per the
    # upsert guard, keep the session marked deleted rather than un-delete it
    # from a stub).
    jsonl_full = Path(claude_dir) / jsonl_path
    if jsonl_full.exists():
        ok, reason = _transcript_is_resumable(jsonl_full)
        result.transcript_valid = ok
        if not ok:
            result.transcript_warning = reason
    return result


def _is_transcript_symlink(rel_path: str, uuid: str) -> bool:
    """True if ``rel_path`` is the logger's per-session transcript.jsonl
    symlink for this UUID: ``sesslogs/<dir-containing-uuid>/transcript.jsonl``.

    The caller has already confirmed the path is an in-scope git symlink for
    this session; this just gates the recreate to the known transcript pattern
    (the only symlink the logger makes), leaving any other symlink to the
    conservative skip path.
    """
    norm = rel_path.replace("\\", "/")
    parts = norm.split("/")
    return (
        len(parts) == 3
        and parts[0] == "sesslogs"
        and parts[2] == "transcript.jsonl"
        and f"__{uuid}_" in parts[1]
    )


def _recreate_transcript_symlink(
    claude_dir: str, link_rel: str, slug: str, uuid: str
) -> bool:
    """Recreate the logger's transcript.jsonl symlink as a real filesystem
    link pointing at the restored transcript (#38).

    Target is an ABSOLUTE path on the CURRENT machine (not the foreign,
    possibly-stale path stored in the git symlink blob), so the link is valid
    here and matches the logger's own absolute form. Uses
    ``dazzle_filekit.create_symlink`` (os.symlink -> dazzlelink -> mklink with
    graceful fallback; ``force=True`` removes any blocking regular file or old
    link first -- which also heals the logger-blocked-stub state). Returns the
    library's success bool; ``False`` (e.g. Windows without symlink privilege)
    routes the caller to skip-and-report. Never raises, never writes a regular
    file -- so it can never reintroduce the v0.3.15 clobber.
    """
    try:
        from dazzle_filekit import create_symlink
    except ImportError:
        return False
    cp = ClaudePaths.from_dir(claude_dir)
    link_path = cp.abs_of(link_rel)
    # .resolve() stays: the is_symlink comparison below resolves the live
    # link's target, so both sides must share resolve semantics (#46).
    target_abs = cp.jsonl(slug, uuid).resolve()
    # Skip work if a correct symlink already exists (idempotent, no churn).
    try:
        if link_path.is_symlink() and Path(os.readlink(link_path)).resolve() == target_abs:
            return True
    except OSError:
        pass
    try:
        return bool(create_symlink(
            str(target_abs), str(link_path),
            force=True, target_is_directory=False,
        ))
    except Exception:
        # create_symlink is documented to return False rather than raise, but
        # guard anyway -- a symlink failure must never abort or corrupt a restore.
        return False


def _recreate_symlink_verbatim(claude_dir: str, link_rel: str, commit: str) -> bool:
    """Recreate a non-transcript git symlink entry as a real filesystem link
    using the VERBATIM target stored in the symlink blob (#39).

    A symlink blob's content IS the link-target path text. For links csb
    doesn't recognize (anything that isn't the logger's transcript.jsonl),
    that stored target is the best information available: on the same machine
    (the dominant restore case) it is exactly right; cross-machine it may
    dangle -- harmless, and strictly better than no link or a stub.

    Dir-vs-file: the blob doesn't record whether the target is a directory,
    so infer from the on-disk target when it exists (relative targets resolve
    against the link's parent, per symlink semantics); default to file.

    Same safety contract as the transcript recreate: never raises, never
    writes the target-path text as a regular file (the v0.3.15 clobber class
    stays closed). Returns False -> caller skips-and-reports.
    """
    try:
        from dazzle_filekit import create_symlink
    except ImportError:
        return False
    raw = git_show_file_bytes(claude_dir, commit, link_rel)
    if not raw:
        return False
    target = raw.decode("utf-8", errors="replace").strip()
    if not target:
        return False
    link_path = Path(claude_dir) / link_rel
    t = Path(target)
    probe = t if t.is_absolute() else (link_path.parent / t)
    try:
        is_dir = probe.is_dir()
    except OSError:
        is_dir = False
    # Idempotent: a link already pointing at this target is a no-op success.
    # Windows os.readlink returns absolute targets in extended-length form
    # (\\?\C:\...), so compare Path-normalized with the prefix stripped.
    try:
        if link_path.is_symlink():
            existing = os.readlink(str(link_path))
            if existing.startswith("\\\\?\\"):
                existing = existing[4:]
            if existing == target or Path(existing) == Path(target):
                return True
    except OSError:
        pass
    try:
        return bool(create_symlink(
            target, str(link_path),
            force=True, target_is_directory=is_dir,
        ))
    except Exception:
        return False


# ── Timestamp fidelity (#40): the restore metadata-apply layer ──────────────
#
# Restore should bring back WHEN, not just bytes. Git stores content + tree
# mode only -- no mtime/atime/creation-time -- so a naive restore stamps every
# recovered file with recovery time and the session floats to the top of any
# filesystem-time sort despite being logically old. These helpers derive each
# file's true times from data csb already holds and reapply them:
#
#   mtime ladder:  index jsonl_mtime (main transcript; survives deletion)
#                  -> last event timestamp in the JSONL content
#                  -> author date of the last git commit touching the path
#   birth (Win):   first event timestamp, via filekit SetFileTime
#   atime:         set alongside mtime (best-effort; modern OSes neuter atime)
#
# This is the extensible fidelity layer: future recorded-value sources (e.g.
# a preservelib manifest with exact mtimes/ACLs, Track C of the preservelib
# DWP) plug in as higher-priority rungs of the same ladder -- the apply
# plumbing does not change. Unix ctime is not settable (no OS API).


def _iso_to_epoch(ts: str) -> Optional[float]:
    """Parse a Claude Code event timestamp (ISO 8601, usually Z-suffixed)
    to an epoch float. Python 3.10's fromisoformat can't take 'Z'."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _line_event_time(line: bytes) -> Optional[float]:
    """Epoch time of one JSONL event line's ``timestamp`` field, or None."""
    s = line.strip()
    if not s or not s.startswith(b"{"):
        return None
    try:
        obj = json.loads(s)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    return _iso_to_epoch(obj.get("timestamp"))


def _jsonl_event_time_bounds(
    full: Path, head_lines: int = 50, tail_bytes: int = 65536
) -> tuple[Optional[float], Optional[float]]:
    """(first_event_time, last_event_time) of a JSONL transcript, derived
    cheaply: scan up to ``head_lines`` forward for the first timestamped
    event, and a ``tail_bytes`` block backward for the last. Avoids a full
    parse of 100MB+ transcripts; returns (None, None) on any failure."""
    first = last = None
    try:
        with open(full, "rb") as f:
            for i, line in enumerate(f):
                if i >= head_lines:
                    break
                ts = _line_event_time(line)
                if ts is not None:
                    first = ts
                    break
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            tail = f.read().splitlines()
        for line in reversed(tail):
            ts = _line_event_time(line)
            if ts is not None:
                last = ts
                break
    except OSError:
        return None, None
    return first, last


def _apply_restored_times(
    *,
    claude_dir: str,
    written: list[str],
    jsonl_path: str,
    commit: str,
    db_mtime: Optional[float] = None,
) -> int:
    """Reapply derived original timestamps to freshly-restored files (#40).

    Returns the number of files that received at least one timestamp.
    Never raises and never fails the restore -- a file whose times can't be
    derived or applied simply keeps recovery-time stamps.
    """
    # Defensive: the DB's jsonl_mtime SHOULD be a float (REAL column), but
    # SQLite's dynamic typing happily stores anything -- treat non-numeric
    # values as "no recorded mtime" rather than blowing up os.utime.
    try:
        db_mtime = float(db_mtime) if db_mtime else None
    except (TypeError, ValueError):
        db_mtime = None
    try:
        from dazzle_filekit.metadata import (
            is_win32_available,
            restore_windows_creation_time,
        )
        can_birth = is_win32_available()
    except ImportError:
        restore_windows_creation_time = None
        can_birth = False

    count = 0
    for p in written:
        full = Path(claude_dir) / p
        try:
            if not full.is_file():
                continue
        except OSError:
            continue
        mtime: Optional[float] = None
        birth: Optional[float] = None
        if p.replace("\\", "/").endswith(".jsonl"):
            birth, mtime = _jsonl_event_time_bounds(full)
        if p == jsonl_path and db_mtime:
            # The index's recorded filesystem mtime is exact -- prefer it
            # over the last-event approximation for the main transcript.
            mtime = db_mtime
        if mtime is None:
            mtime = git_last_commit_time(claude_dir, commit, p)
        if mtime is None and birth is None:
            continue
        applied = False
        # Creation time FIRST (SetFileTime), then utime -- so mtime/atime
        # land last and cannot be perturbed by the creation-time write.
        if birth is not None and can_birth and restore_windows_creation_time:
            try:
                applied = bool(
                    restore_windows_creation_time(str(full), birth)
                ) or applied
            except Exception:
                pass
        if mtime is not None:
            try:
                os.utime(str(full), (mtime, mtime))
                applied = True
            except OSError:
                pass
        if applied:
            count += 1
    return count


def _extract_slug_from_jsonl_path(jsonl_path: str) -> str:
    """Pull <slug> out of `projects/<slug>/<uuid>.jsonl`.

    Accepts both forward and back slashes (DB rows may store either).
    Returns "" when the path doesn't match the expected shape.
    """
    if not jsonl_path:
        return ""
    return ClaudePaths.parse_rel(jsonl_path).slug or ""


def _categorize_restored_paths(
    paths: list[str], slug: str, uuid: str,
) -> list[tuple[str, int]]:
    """Group restored paths by category for the user-facing summary.

    Categorization is driven by ``git_ops.SESSION_HISTORY_SCOPES`` -- the
    same table that drives discovery. Single source of truth means adding
    a new restore category gets a label here automatically.

    Order matches SESSION_HISTORY_SCOPES insertion order so the breakdown
    output is stable and readable.

    Returns ordered (label, count) pairs; only non-zero categories
    included. Paths that don't match any scope are aggregated under
    "other" (defensive -- shouldn't happen in normal restore flow).
    """
    # Use a dict to preserve scope-table order, init counts to 0
    cats: dict[str, int] = {spec.label: 0 for spec in SESSION_HISTORY_SCOPES}
    cats["other"] = 0
    for p in paths:
        label = categorize_path_for_uuid(p, slug, uuid)
        if label is None:
            cats["other"] += 1
        else:
            cats[label] = cats.get(label, 0) + 1
    return [(k, v) for k, v in cats.items() if v > 0]


def _looks_like_full_uuid(s: str) -> bool:
    """True if `s` is a 36-char hyphenated UUID string (case-insensitive)."""
    import re
    _UUID_RE = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        re.IGNORECASE,
    )
    return bool(_UUID_RE.match(s))


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
        # Multi-term boolean is not wired through the dir-scope ranker yet
        # (it ranks by file-op strength, a different path). Reject clearly
        # rather than silently searching only the first term.
        if len(args.query) > 1:
            print(
                "Error: multiple search terms are not supported with -d/-D "
                "directory-scope yet. Search one term with -d/-D, or drop "
                "-d/-D to combine terms.",
                file=sys.stderr,
            )
            conn.close()
            return 2

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
            args.query[0],
            extra_terms=tuple(args.query[1:]),
            match_mode=args.match,
            regex=args.regex,
            case_sensitive=args.case_sensitive,
            above=above,
            below=below,
            session_filter=session_filter or None,
            source_override=source_override,
            # Shared --deleted [only|all] grammar; one normalizer (#41).
            include_deleted=(deleted_mode(args) == "all"),
            only_deleted=(deleted_mode(args) == "only"),
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
        _terms_disp = " ".join(f'"{t}"' for t in args.query)
        _how = "" if len(args.query) == 1 else f" (--match {args.match})"
        print(f"No content matches for {_terms_disp}{_how}")
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
    """Build / refresh per-project FTS5 content indices.

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


def cmd_update(args) -> int:
    """Dispatcher for `csb update <target>`.

    Routes to the per-target implementation. With no target, prints the
    help so users learn what's updatable. Each target is a refresh /
    rebuild verb for a specific csb representation:

      rebuild-index    -- SQLite session index
      build-fts5       -- per-project FTS5 content indexes
      backfill-deleted -- git-history backfill of culled sessions (v0.3.11)
    """
    target = getattr(args, "update_target", None)
    if target is None:
        print(
            "csb update: pick a target.\n"
            "  csb update rebuild-index      -- reconstruct SQLite session index\n"
            "  csb update build-fts5         -- per-project FTS5 content index\n"
            "  csb update backfill-deleted   -- backfill culled-session metadata from git\n"
            "\n"
            "Run `csb update <target> -h` for per-target options.",
            file=sys.stderr,
        )
        return 2
    if target == "rebuild-index":
        return cmd_rebuild_index(args)
    if target == "build-fts5":
        return cmd_build_fts5(args)
    if target == "backfill-deleted":
        return cmd_backfill_deleted(args)
    # argparse's metavar restriction shouldn't let us reach here; defensive.
    print(f"Unknown update target: {target}", file=sys.stderr)
    return 2


def _maybe_refresh_fts5(args) -> None:
    """Wipe + rebuild the per-project FTS5 content indexes after a
    `csb update rebuild-index --include-fts5` (#3, the last open AC).

    Force-rebuild on purpose: rebuild-index is the nuclear
    reconstruct-everything verb, so the content indexes are rebuilt
    unconditionally too rather than mtime-gated (that incremental path
    is `csb update build-fts5` without --force).

    Fails SOFT in every case: by the time this seam runs the main index
    rebuild has already succeeded, so a missing-FTS5 SQLite build or an
    indexing error downgrades to a stderr warning with the manual
    command, never a non-zero rebuild exit.

    (Backup-time incremental FTS5 indexing -- the other half of the
    original #3 Phase 2 spec -- was REJECTED by design: it would add
    latency inside the PreCompact/SessionEnd hooks, and v0.3.22's
    search-time freshness rescue makes it unnecessary.)
    """
    from . import fts5_db, fts5_index

    quiet = getattr(args, "quiet", False)
    if not fts5_db.fts5_available():
        print(
            "Warning: this Python's SQLite lacks FTS5; skipped the "
            "--include-fts5 refresh (main index rebuild is intact).",
            file=sys.stderr,
        )
        return
    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn, quiet=quiet)
    try:
        fts5_index.build_all(
            conn, Path(config["claude_dir"]), force=True, quiet=quiet,
        )
    except Exception as e:  # noqa: BLE001 -- secondary refresh must not fail the rebuild
        print(
            f"Warning: FTS5 refresh failed ({e}); main index rebuild is "
            f"intact. Run `csb update build-fts5 --force` manually.",
            file=sys.stderr,
        )
    finally:
        conn.close()


def cmd_backfill_deleted(args) -> int:
    """Discover deleted sessions from git history; synthesize DB rows.

    Two-pass algorithm:

      1. Refresh the ``git_deleted_jsonls`` cache by walking
         ``git log --all --diff-filter=D --name-only -- 'projects/*.jsonl'``
         (via ``git_list_deleted_jsonls``). Inserts new rows; updates
         git-side fields on existing ones (the ``extracted_metadata``
         flag is preserved across re-upsert).

      2. For each cache row not yet marked ``extracted_metadata = 1``
         and not already in the live ``sessions`` table:
           - Resolve the commit BEFORE the deletion via
             ``git_find_deleted_file``.
           - Read the historical blob via ``git_show_file_bytes``.
           - Parse it with ``extract_metadata_from_bytes`` (the session
             UUID is supplied from the cached path -- not inferred from
             the blob, see Phase 0 reality-check report).
           - Insert a deleted-flagged ``sessions`` row + ``folder_usage``
             rows, then flip ``extracted_metadata = 1`` so subsequent
             passes skip this row.

    Flags:
      --dry-run -- preview without writing anything
      --full    -- accepted but not yet differentiated from the default
                   run (the last_refreshed_at marker is recorded but not
                   yet used as an incremental-skip gate)

    Plan ref: 2026-06-02__15-46-56__claude-plan__safe-update-umbrella-
    and-backfill-v0.3.11.md
    """
    from .index import (
        count_git_deleted_jsonls,
        list_git_deleted_jsonls,
        mark_git_deleted_extracted,
        upsert_git_deleted_jsonl,
    )
    from .git_ops import (
        git_list_deleted_jsonls,
        git_show_file_bytes,
    )
    from .metadata import extract_metadata_from_bytes

    config = _get_config(args)
    claude_dir = config["claude_dir"]
    quiet = getattr(args, "quiet", False)
    dry_run = getattr(args, "dry_run", False)

    if not is_git_repo(claude_dir):
        print(f"Error: {claude_dir} is not a git repository.", file=sys.stderr)
        return 1

    with backup_lock(claude_dir) as acquired:
        if not acquired:
            if not quiet:
                print("Another csb operation is in progress. Skipping.",
                      file=sys.stderr)
            return 0

        conn = open_db(config["index_path"])
        init_schema(conn)

        # 1. Refresh the cache from git log.
        now = _now_iso()
        deletions = git_list_deleted_jsonls(claude_dir)
        cache_new = 0
        for d in deletions:
            if dry_run:
                # Preview pass: count would-be-new rows without writing.
                exists = list_git_deleted_jsonls(conn)
                if not any(r["jsonl_path"] == d["jsonl_path"] for r in exists):
                    cache_new += 1
                continue
            inserted = upsert_git_deleted_jsonl(
                conn,
                jsonl_path=d["jsonl_path"],
                session_id=d["session_id"],
                deleted_commit=d.get("deleted_commit"),
                deleted_at=d.get("deleted_at"),
                last_refreshed_at=now,
            )
            if inserted:
                cache_new += 1

        if not quiet:
            print(f"git log: {len(deletions)} deleted JSONL path(s); "
                  f"{cache_new} new to cache")

        # 2. Synthesize new rows + auto-repair sparse existing ones.
        #
        # Auto-repair (Approach b from the v0.3.11 design discussion):
        # if the live sessions row exists AND its folder_usage has <=1
        # entries (the past-rebuild fingerprint), AND git has richer
        # metadata, refresh the row in-place from the historical blob.
        # No flag -- the heuristic is conservative enough that a row
        # already at >=2 folders is left alone.
        #
        # We walk ALL cache rows (not just extracted=0) so the repair
        # pass fires for rows from past backfill runs that already
        # marked themselves extracted. For extracted-and-intact rows
        # the per-row gates short-circuit before any git work.
        pending = list_git_deleted_jsonls(conn)
        synthesized = 0
        repaired = 0
        skipped_live_intact = 0
        skipped_no_blob = 0

        for d in pending:
            sid = d["session_id"]
            jp = d["jsonl_path"]

            live = get_session(conn, sid)

            # Fast-path: live row exists AND its folder_usage is already
            # non-sparse (>1 folder). No repair needed; no git work.
            # Saves 2 git ops per row -- significant on real DBs where
            # most rows are intact.
            if live:
                live_folder_count = conn.execute(
                    "SELECT COUNT(*) FROM folder_usage WHERE session_id = ?",
                    (sid,),
                ).fetchone()[0]
                if live_folder_count > 1:
                    skipped_live_intact += 1
                    if not dry_run:
                        mark_git_deleted_extracted(conn, jp)
                        # Stamp metadata_validated_at: the user invoked
                        # backfill-deleted explicitly, so they expect
                        # every cache-known row to reflect "I checked
                        # this just now." Intact-ness IS a finding even
                        # if it didn't trigger a git read.
                        conn.execute(
                            "UPDATE sessions SET metadata_validated_at = ? "
                            "WHERE session_id = ?",
                            (now, sid),
                        )
                        conn.commit()
                    continue
            else:
                live_folder_count = 0

            # Slow path: need git data. Find commit before deletion -> blob.
            parent_commit = git_find_deleted_file(claude_dir, jp)
            blob = None
            if parent_commit:
                blob = git_show_file_bytes(claude_dir, parent_commit, jp)

            if blob is None:
                # No usable git data. Mark extracted so we don't keep
                # retrying on every backfill pass.
                skipped_no_blob += 1
                if not dry_run:
                    mark_git_deleted_extracted(conn, jp)
                continue

            # Derive the project slug from the path: projects/<slug>/<uuid>.jsonl
            project = ClaudePaths.parse_rel(jp).slug or ""

            meta = extract_metadata_from_bytes(blob, session_id=sid, project=project)
            new_folder_count = len(meta.folder_usage)

            if live:
                # Auto-repair gate: refresh ONLY if git has strictly richer
                # folder data than the live (sparse) row.
                if new_folder_count > live_folder_count:
                    if dry_run:
                        if not quiet:
                            label = meta.session_name or live["session_name"] or "(unnamed)"
                            print(f"  [DRY] would repair: {sid[:8]}  {label!r}  "
                                  f"({live_folder_count} -> {new_folder_count} folders)")
                        repaired += 1
                        continue
                    # Repair: same upsert+mark_deleted as synthesize. The
                    # upsert's DELETE+INSERT on folder_usage refreshes the
                    # full folder list; mark_deleted re-applies deleted_at
                    # (upsert sets it to NULL on UPDATE by API contract).
                    upsert_session(
                        conn, meta, jp,
                        jsonl_size=int(d.get("last_seen_size") or 0),
                        jsonl_mtime=float(d.get("last_seen_mtime") or 0.0),
                        scanned_at=now,
                    )
                    mark_deleted(conn, sid, live.get("deleted_at") or d.get("deleted_at") or now)
                    mark_git_deleted_extracted(conn, jp)
                    repaired += 1
                else:
                    # Git has nothing better. Mark cache extracted, stamp
                    # metadata_validated_at on the row (we DID verify there's
                    # nothing more to recover), and move on.
                    skipped_live_intact += 1
                    if not dry_run:
                        mark_git_deleted_extracted(conn, jp)
                        conn.execute(
                            "UPDATE sessions SET metadata_validated_at = ? "
                            "WHERE session_id = ?",
                            (now, sid),
                        )
                        conn.commit()
                continue

            # No live row -- synthesize from the blob.
            if dry_run:
                if not quiet:
                    label = meta.session_name or "(unnamed)"
                    print(f"  [DRY] would synthesize: {sid[:8]}  {label!r}  "
                          f"({new_folder_count} folder(s))")
                synthesized += 1
                continue

            # Insert via normal upsert (sets deleted_at = NULL by API
            # contract), then mark_deleted to re-apply the cull timestamp.
            upsert_session(
                conn, meta, jp,
                jsonl_size=int(d.get("last_seen_size") or 0),
                jsonl_mtime=float(d.get("last_seen_mtime") or 0.0),
                scanned_at=now,
            )
            mark_deleted(conn, sid, d.get("deleted_at") or now)
            mark_git_deleted_extracted(conn, jp)
            synthesized += 1

        conn.close()

        if not quiet:
            verb_s = "would synthesize" if dry_run else "synthesized"
            verb_r = "would repair" if dry_run else "repaired"
            print(f"backfill-deleted: {verb_s} {synthesized} session(s), "
                  f"{verb_r} {repaired} sparse row(s) "
                  f"({skipped_live_intact} intact in live DB, "
                  f"{skipped_no_blob} unreadable from git)")
        return 0


def cmd_rebuild_index(args) -> int:
    """Reconstruct SQLite index; preserve deleted-session metadata across the rebuild.

    Replaces the v0.3.10 destructive ``unlink + cmd_backup`` flow that
    silently lost deleted-session rows (data-loss bug confirmed by
    ``tests/one-offs/rebuild_reality_check.py``).

    The safe rebuild:

      1. Acquires ``backup_lock`` for the whole operation -- concurrent
         ``csb backup`` cannot race the swap.
      2. Snapshots every deleted-session row (and its folder_usage rows)
         into memory via ``snapshot_deleted_sessions``.
      3. Moves the pre-rebuild DB aside to ``<db>.bak`` (does NOT delete
         it -- crash safety). Stale ``.bak`` from a prior failed rebuild
         is cleared first.
      4. Runs the indexer (``cmd_backup --no-commit``) against the live
         filesystem -- this rebuilds the active-session view.
      5. On rebuild failure, restores the ``.bak`` and propagates the
         error -- the user is never left with a corrupted-or-missing DB.
      6. On rebuild success, calls ``restore_deleted_snapshot`` to merge
         deleted-session rows back in (skipping any UUIDs the live
         rescan already repopulated, which would mean the JSONL came
         back somehow).
      7. Optionally runs ``_maybe_refresh_fts5`` (force wipe + rebuild
         of the per-project FTS5 DBs, fail-soft) if ``--include-fts5``.
      8. Optionally chains ``cmd_backfill_deleted`` if
         ``--include-backfill-deleted`` is set.
      9. Removes the ``.bak`` on full success.

    Plan ref: 2026-06-02__15-46-56__claude-plan__safe-update-umbrella-
    and-backfill-v0.3.11.md
    """
    config = _get_config(args)
    claude_dir = config["claude_dir"]
    db_path = config["index_path"]
    quiet = getattr(args, "quiet", False)

    with backup_lock(claude_dir) as acquired:
        if not acquired:
            if not quiet:
                print(
                    "Another csb backup or update is in progress. "
                    "Rebuild skipped.",
                    file=sys.stderr,
                )
            return 0  # not an error -- match cmd_backup's skipped-lock contract

        # 1. Snapshot deleted-session knowledge before the destructive part.
        snapshot: list[dict] = []
        bak_path: Optional[Path] = None
        db_file = Path(db_path)
        if db_file.exists():
            conn = open_db(db_path)
            init_schema(conn)
            snapshot = snapshot_deleted_sessions(conn)
            conn.close()

            bak_path = db_file.with_suffix(db_file.suffix + ".bak")
            if bak_path.exists():
                # A prior rebuild crashed mid-flight. Drop the stale .bak
                # (the live DB is fresher than it).
                bak_path.unlink()
                if not quiet:
                    print(f"Removed stale rebuild backup: {bak_path}",
                          file=sys.stderr)
            os.rename(str(db_file), str(bak_path))
            if not quiet:
                print(f"Moved old index aside: {bak_path}")

        # 2-3. Run the indexer's inner function directly (NOT cmd_backup,
        # which would try to re-acquire backup_lock and silently skip,
        # leaving us with just the snapshot's deleted rows and no live
        # sessions). On any failure, restore the .bak.
        args.no_commit = True
        try:
            result = _cmd_backup_inner(args, config, claude_dir, quiet)
        except Exception:
            if bak_path is not None and bak_path.exists():
                os.rename(str(bak_path), str(db_file))
                if not quiet:
                    print(f"Rebuild raised; restored: {db_path}",
                          file=sys.stderr)
            raise

        if result != 0:
            if bak_path is not None and bak_path.exists():
                os.rename(str(bak_path), str(db_file))
                if not quiet:
                    print(f"Rebuild returned non-zero ({result}); restored: "
                          f"{db_path}", file=sys.stderr)
            return result

        # 4. Merge the snapshot back in (skip UUIDs the rescan already has).
        if snapshot:
            conn = open_db(db_path)
            init_schema(conn)
            restored = restore_deleted_snapshot(conn, snapshot)
            conn.close()
            if not quiet:
                noun = "record" if restored == 1 else "records"
                print(f"Preserved {restored} deleted-session {noun} "
                      f"across rebuild")

        # 5. Optional --include-fts5: wipe + rebuild per-project FTS5 DBs
        # against the freshly rebuilt index (fails soft -- see the helper).
        if getattr(args, "include_fts5", False):
            _maybe_refresh_fts5(args)

        # 6. Optional --include-backfill-deleted -- chain cmd_backfill_deleted
        if getattr(args, "include_backfill_deleted", False):
            cmd_backfill_deleted(args)

        # 7. Cleanup .bak on success.
        if bak_path is not None and bak_path.exists():
            bak_path.unlink()

        if not quiet:
            print("Index rebuilt successfully.")
        return 0


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


def _resolve_pruned_decision(args, session: dict, name: str,
                             verb: str = "resume") -> str:
    """Decide what to do when `csb resume`/`csb view` targets a pruned session.

    ``verb`` parameterizes the wording ("resume" or "view"); the decision
    logic is identical for both callers (#34: one policy, two surfaces).

    Returns one of:
      - "restore" -- caller should invoke _restore_session, then proceed.
      - "abort"   -- caller should exit 0 (user declined or asked not to).
      - "error"   -- caller should exit 1 (non-interactive without flags;
                     printed a hint already).

    Decision precedence: explicit flag (--restore-pruned / --no-restore-pruned)
    > TTY-interactive prompt > non-TTY safe default (error with hint).
    """
    gerund = {
        "resume": "resuming", "view": "viewing", "distill": "distilling",
    }.get(verb, verb + "ing")
    if getattr(args, "no_restore_pruned", False):
        print(
            f"Session '{name}' is pruned (deleted_at set). "
            f"--no-restore-pruned set -- not {gerund}.",
            file=sys.stderr,
        )
        print(
            f"Run `csb restore {session['session_id']}` to recover, then "
            f"`csb {verb} {session['session_id']}` again.",
            file=sys.stderr,
        )
        return "error"
    if getattr(args, "restore_pruned", False):
        print(
            f"Session '{name}' is pruned (deleted_at: "
            f"{session['deleted_at']}). Restoring from git before {gerund}."
        )
        return "restore"
    # Interactive: prompt only on TTY. Non-TTY without a flag is an error
    # (hooks/cron shouldn't hang on a prompt).
    if not sys.stdin.isatty():
        print(
            f"Session '{name}' is pruned. Non-interactive (no TTY); "
            f"refusing to silently restore or abort.",
            file=sys.stderr,
        )
        print(
            f"Re-run with --restore-pruned to restore + {verb}, "
            f"or --no-restore-pruned to abort cleanly.",
            file=sys.stderr,
        )
        return "error"
    print(
        f"Session '{name}' is pruned (deleted_at: {session['deleted_at']})."
    )
    try:
        ans = input(
            f"Restore from git before {gerund}? [Y/n] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return "abort"
    if ans in ("", "y", "yes"):
        return "restore"
    return "abort"


# ── csb view (#14): resolver + launcher for Claude Code History Viewer ──────
#
# Repatriated from dazzlecmd's `dz claudeview` (which was written against
# csb's own API and shelled out to csb for everything but the launch).
# csb stays a discovery/backup/restore tool that LAUNCHES readers, never
# renders -- the viewer is the CCHV Tauri app; a readable text layer is
# #12's territory. Pruned sessions restore-in-place via _restore_session
# (#34) -- durable and byte+metadata-exact since v0.3.17/v0.3.18, which
# supersedes #34's "temporary resurrection" sandboxing ideas.

_VIEW_UUID_PREFIX_RE = re.compile(r"^[0-9a-fA-F-]{4,36}$")
_SESSLOG_UUID_RE = re.compile(
    r"__([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})_"
)


def _find_viewer(config) -> Optional[dict]:
    """Locate the Claude Code History Viewer binary or dev-mode project dir.

    Resolution order:
      1. $CLAUDEVIEW_BIN env var (explicit binary path; dz-claudeview compat)
      2. ``viewer_path`` config key (binary file OR dev-mode project dir)
      3. Platform install locations
      4. None -> caller prints the resolved transcript path instead.

    Returns {"mode": "binary"|"dev", "path": str} or None.
    """
    import platform as _platform

    def _classify(p: str) -> Optional[dict]:
        if os.path.isfile(p):
            return {"mode": "binary", "path": p}
        if (os.path.isdir(p)
                and os.path.isfile(os.path.join(p, "package.json"))
                and os.path.isdir(os.path.join(p, "src-tauri"))):
            return {"mode": "dev", "path": p}
        return None

    env_bin = os.environ.get("CLAUDEVIEW_BIN")
    if env_bin:
        found = _classify(env_bin)
        if found:
            return found

    cfg_path = config.get("viewer_path")
    if cfg_path:
        found = _classify(os.path.expanduser(str(cfg_path)))
        if found:
            return found

    candidates: list[str] = []
    system = _platform.system()
    if system == "Windows":
        # System-wide installer target FIRST -- this is what the standard
        # CCHV installer creates (C:\Program Files\CCHistoryViewer\) and
        # what average users will have.
        for pf_var in ("ProgramFiles", "ProgramFiles(x86)"):
            pf = os.environ.get(pf_var, "")
            if pf:
                candidates.append(os.path.join(
                    pf, "CCHistoryViewer", "claude-code-history-viewer.exe"))
                candidates.append(os.path.join(
                    pf, "Claude Code History Viewer",
                    "Claude Code History Viewer.exe"))
        # Per-user installer locations.
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if localappdata:
            for name in ("Claude Code History Viewer",
                         "dazzle-claude-code-history-viewer"):
                candidates.append(os.path.join(
                    localappdata, "Programs", name, f"{name}.exe"))
                candidates.append(os.path.join(
                    localappdata, "Programs", "Claude Code History Viewer",
                    "Claude Code History Viewer.exe"))
    elif system == "Darwin":
        candidates.append(
            "/Applications/Claude Code History Viewer.app"
            "/Contents/MacOS/claude-code-history-viewer")
        candidates.append(os.path.expanduser(
            "~/Applications/Claude Code History Viewer.app"
            "/Contents/MacOS/claude-code-history-viewer"))
    else:
        candidates.append("/usr/bin/claude-code-history-viewer")
        candidates.append(os.path.expanduser(
            "~/.local/bin/claude-code-history-viewer"))

    for p in candidates:
        if os.path.isfile(p):
            return {"mode": "binary", "path": p}
    return None


def _passthrough_args(args) -> list:
    """The args after a standalone `--` (#47), forwarded verbatim to the
    wrapped subtool. Empty list when none were given."""
    return list(getattr(args, "passthrough", None) or [])


def _launch_viewer(viewer: dict, session_value: str, passthrough: list = None) -> int:
    """Launch CCHV focused on ``session_value`` (a full session UUID).

    Binary mode launches DETACHED so the viewer outlives this shell.
    Dev mode runs ``pnpm tauri:dev`` in the foreground (build output
    visible; Ctrl-C stops it). ``passthrough`` (anything after `--`, #47)
    is appended verbatim to the viewer's argv.
    """
    import platform as _platform
    import subprocess

    extra = list(passthrough or [])
    mode, path = viewer["mode"], viewer["path"]
    if mode == "dev":
        cmd = ["pnpm", "tauri:dev", "--", "--", "--session", session_value] + extra
        print(f"Launching in dev mode from: {path}")
        print("  (Vite + cargo run -- Ctrl-C to stop)")
        try:
            return subprocess.run(cmd, cwd=path).returncode
        except (OSError, FileNotFoundError) as exc:
            print(f"Error launching dev mode: {exc}", file=sys.stderr)
            print("  Is pnpm installed?", file=sys.stderr)
            return 1

    cmd = [path, "--session", session_value] + extra
    try:
        if _platform.system() == "Windows":
            subprocess.Popen(
                cmd,
                creationflags=(subprocess.DETACHED_PROCESS
                               | subprocess.CREATE_NEW_PROCESS_GROUP),
                close_fds=True,
            )
        else:
            subprocess.Popen(cmd, start_new_session=True, close_fds=True)
        return 0
    except OSError as exc:
        print(f"Error launching viewer: {exc}", file=sys.stderr)
        return 1


def _resolve_session_query(query: str, conn, claude_dir: str):
    """Resolve a user query to a session row (multi-modal, #14).

    Resolution order (richest-match first):
      1. Directory path (incl ".") -> folder-usage lookup
      2. Absolute .jsonl path -> UUID from the filename stem
      3. UUID or UUID prefix -> shared get_session resolver
      4. Sesslog folder NAME with embedded UUID
      5. Free-text -> search_sessions (name/project/folder substring)

    Returns (session_dict, method) on a unique hit,
    (list_of_sessions, "candidates:<label>") on multi-match,
    or (None, reason) on no match.
    """
    resolved_path = query if os.path.isabs(query) else os.path.realpath(query)

    if os.path.isdir(resolved_path):
        m = _SESSLOG_UUID_RE.search(os.path.basename(resolved_path))
        if m:
            session = get_session(conn, m.group(1))
            if session:
                return session, "sesslog-dir"
        results = find_sessions_by_folder_usage(conn, resolved_path, limit=10)
        if len(results) == 1:
            return results[0], "folder"
        if len(results) > 1:
            return results, "candidates:folder"
        return None, f"no sessions found that used directory: {resolved_path}"

    if os.path.isabs(query) and os.path.exists(query):
        real = os.path.realpath(query)
        if real.endswith(".jsonl"):
            stem = os.path.splitext(os.path.basename(real))[0]
            session = get_session(conn, stem)
            if session:
                return session, "path"
        return None, f"path exists but no matching session found: {query}"

    if _VIEW_UUID_PREFIX_RE.match(query):
        session = get_session(conn, query)
        if session:
            return session, "uuid"
        # Fall through to free-text: short hex-ish strings can also be
        # legitimate name keywords; only give up after the search.

    m = _SESSLOG_UUID_RE.search(query)
    if m:
        session = get_session(conn, m.group(1))
        if session:
            return session, "sesslog-name"
        return None, (f"UUID extracted from folder name ({m.group(1)}) "
                      f"but no matching session")

    # Exact session-name match WINS over substring results (#42): this is
    # Claude Code's own /resume semantics (searchSessionsByCustomTitle with
    # exact:true), and csb's session_name IS the JSONL customTitle, so the
    # two agree by construction. Without this rung, a full session name
    # would substring-match itself AND its keyword cousins -> candidates
    # noise where Claude resumes directly.
    results = search_sessions(conn, query, limit=50)
    exact = [s for s in results if (s.get("session_name") or "") == query]
    if len(exact) == 1:
        return exact[0], "name-exact"
    if len(exact) > 1:
        return exact, "candidates:name-exact"
    results = results[:10]
    if len(results) == 1:
        return results[0], "search"
    if len(results) > 1:
        return results, "candidates:search"
    return None, f"no sessions match '{query}'"


def _show_view_candidates(sessions, query: str, label: str) -> None:
    """Display multi-match candidates via the timeline renderer."""
    print(f"\n{len(sessions)} sessions match '{query}' (via {label}):\n")
    if HAS_RICH:
        render_timeline_rich(sessions)
    else:
        print(format_timeline(sessions))
    print("\nRe-run with a UUID prefix to open a specific session.")


def cmd_view(args) -> int:
    """Open a session in Claude Code History Viewer (#14).

    Resolves the query against the index, restores pruned sessions first
    (same policy + flags as `csb resume`, #34), and launches the viewer
    detached. With no viewer installed, prints the resolved transcript
    path -- still the answer to "where is this conversation".
    """
    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn)

    query = getattr(args, "query", None)
    if not query:
        sessions = list_sessions(conn, limit=10)
        conn.close()
        if not sessions:
            print("No sessions indexed yet. Run `csb backup` first.")
            return 0
        print("Recent sessions -- pass a UUID, path, folder, or keyword "
              "to open one:\n")
        if HAS_RICH:
            render_timeline_rich(sessions)
        else:
            print(format_timeline(sessions))
        return 0

    result, method = _resolve_session_query(query, conn, config["claude_dir"])
    if result is None:
        print(f"Error: {method}", file=sys.stderr)
        conn.close()
        return 1
    if isinstance(result, list):
        label = method.split(":", 1)[1] if ":" in method else method
        _show_view_candidates(result, query, label)
        conn.close()
        return 1
    session = result
    full_id = session["session_id"]
    name = session.get("session_name") or "(unnamed)"

    # Pruned session (#34): restore-in-place first, same policy as resume.
    if session.get("deleted_at"):
        decision = _resolve_pruned_decision(args, session, name, verb="view")
        if decision == "abort":
            conn.close()
            return 0
        if decision == "error":
            conn.close()
            return 1
        jsonl_rel = session.get("jsonl_path")
        if not jsonl_rel:
            print(
                f"Session '{name}' is pruned but has no jsonl_path in the "
                f"DB row -- cannot auto-restore. Run `csb restore {full_id}` "
                f"manually.",
                file=sys.stderr,
            )
            conn.close()
            return 1
        commit = git_find_deleted_file(config["claude_dir"], jsonl_rel)
        if not commit:
            print(
                f"Couldn't find '{jsonl_rel}' in git history -- nothing to "
                f"restore.",
                file=sys.stderr,
            )
            conn.close()
            return 1
        restore_result = _restore_session(
            claude_dir=config["claude_dir"],
            full_uuid=full_id,
            jsonl_path=jsonl_rel,
            commit=commit,
            db_mtime=session.get("jsonl_mtime") or None,
            quiet=getattr(args, "quiet", False),
        )
        if restore_result is None:
            conn.close()
            return 1
        if restore_result.wrote == 0 and restore_result.failed:
            print(
                f"Restore failed for all {len(restore_result.failed)} "
                f"file(s); not launching the viewer.",
                file=sys.stderr,
            )
            conn.close()
            return 1
        print(
            f"Restored {restore_result.wrote} file"
            f"{'s' if restore_result.wrote != 1 else ''} from commit "
            f"{restore_result.commit_short}."
        )
    conn.close()

    # The transcript must exist on disk for the viewer to show anything.
    jsonl_rel = session.get("jsonl_path") or ""
    jsonl_full = Path(config["claude_dir"]) / jsonl_rel
    if not jsonl_rel or not jsonl_full.exists():
        print(
            f"Transcript not on disk: {jsonl_full}",
            file=sys.stderr,
        )
        print(
            f"The index may be stale (`csb backup`) or the session needs "
            f"`csb restore {full_id}`.",
            file=sys.stderr,
        )
        return 1

    display = f"{name} ({full_id[:8]}...)" if name != "(unnamed)" else full_id
    print(f"Opening: {display}")
    if method != "uuid":
        print(f"  Resolved via: {method}")
    print(f"  Path: {jsonl_full}")

    viewer = _find_viewer(config)
    if viewer is None:
        print("\nNo viewer found -- the transcript path above is the "
              "session's full conversation (JSONL).")
        print("To enable launching:")
        print("  - set $CLAUDEVIEW_BIN to the viewer binary, or")
        print("  - csb config viewer_path \"/path/to/viewer\", or")
        print("  - install: https://github.com/jhlee0409/claude-code-history-viewer")
        return 0

    return _launch_viewer(viewer, full_id, _passthrough_args(args))


# ── csb distill (#12): human-readable chat-log rendering ────────────────────
#
# The distilled output is an optional READING layer over the preserved
# JSONL -- never a replacement (full-recovery-first). Rendering lives in
# distill.py; this layer is resolution + policy + pruned handling + output
# routing, mirroring cmd_view's structure.


def _distill_canonical_path(claude_dir: str, session: dict) -> Path:
    """`~/.claude/distilled/<project-slug>/<uuid>.md` -- csb-owned dir
    (never inside the logger's sesslogs/), auto-backed-up by the noise
    commits, deterministic name -> idempotent regeneration."""
    jsonl_rel = session.get("jsonl_path") or ""
    slug = (Path(jsonl_rel).parent.name if jsonl_rel
            else (session.get("project") or "unknown"))
    return ClaudePaths.from_dir(claude_dir).distilled_md(slug, session["session_id"])


def _safe_stdout_write(text: str) -> None:
    """Write to stdout tolerating narrow console codepages (cp1252):
    unencodable characters degrade to replacement chars rather than
    crashing the render."""
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        sys.stdout.write(text.encode(enc, errors="replace").decode(enc))


def _render_session_distill(
    session: dict, src_rows, claude_dir: str, mode: str,
    source_override: "str | None" = None,
):
    """Shared assembly for cmd_distill + the backup `always` hook.

    Returns (chunk_iterator, source_label) or (None, reason).
    """
    from .distill import build_chat_messages, pick_channels, render_chat_log

    full_id = session["session_id"]
    jsonl_rel = session.get("jsonl_path") or ""
    jsonl_abs = ClaudePaths.from_dir(claude_dir).abs_of(jsonl_rel) if jsonl_rel else None
    convo_type, convo_path, tool_paths = pick_channels(
        src_rows, jsonl_abs, source_override,
    )
    if mode != "tools" and convo_path is None:
        return None, (
            "no readable conversation source on disk (transcript missing?). "
            f"Try `csb backup` to refresh the index, or `csb restore {full_id}`."
        )
    messages = build_chat_messages(
        convo_type=convo_type, convo_path=convo_path,
        tool_paths=tool_paths, session_id=full_id, mode=mode,
    )
    if not messages:
        return None, "nothing to distill (no conversation or tool events found)."
    name = session.get("session_name") or ""
    source_label = convo_type or "tools-only"
    chunks = render_chat_log(
        messages, session_name=name, session_id=full_id,
        source_label=source_label, mode=mode,
    )
    return chunks, source_label


def cmd_distill(args) -> int:
    """Render a session as an IM-style chat log (#12).

    Identifier surface matches view/resume (shared resolver). Default
    output is the canonical ``~/.claude/distilled/<slug>/<uuid>.md``
    (the log is a document, often large); ``-o PATH`` writes elsewhere;
    ``--stdout`` streams for piping. Policy ``never`` disables even the
    explicit command -- the user opted out entirely.
    """
    config = _get_config(args)
    policy = str(config.get("distill_policy") or "on-demand")
    if policy == "never":
        print(
            "distill_policy is 'never' -- distilling is disabled.",
            file=sys.stderr,
        )
        print(
            "Enable with: csb config distill_policy on-demand",
            file=sys.stderr,
        )
        return 1

    conn = open_db(config["index_path"])
    init_schema(conn)
    query = args.query
    result, method = _resolve_session_query(query, conn, config["claude_dir"])
    if result is None:
        print(f"Error: {method}", file=sys.stderr)
        conn.close()
        return 1
    if isinstance(result, list):
        label = method.split(":", 1)[1] if ":" in method else method
        _show_view_candidates(result, query, label)
        conn.close()
        return 1
    session = result
    full_id = session["session_id"]
    name = session.get("session_name") or "(unnamed)"

    # Pruned session: restore-in-place first, same policy as resume/view.
    if session.get("deleted_at"):
        decision = _resolve_pruned_decision(args, session, name, verb="distill")
        if decision == "abort":
            conn.close()
            return 0
        if decision == "error":
            conn.close()
            return 1
        jsonl_rel = session.get("jsonl_path")
        if not jsonl_rel:
            print(
                f"Session '{name}' is pruned but has no jsonl_path in the "
                f"DB row -- cannot auto-restore. Run `csb restore {full_id}` "
                f"manually.",
                file=sys.stderr,
            )
            conn.close()
            return 1
        commit = git_find_deleted_file(config["claude_dir"], jsonl_rel)
        if not commit:
            print(
                f"Couldn't find '{jsonl_rel}' in git history -- nothing to "
                f"restore.",
                file=sys.stderr,
            )
            conn.close()
            return 1
        restore_result = _restore_session(
            claude_dir=config["claude_dir"],
            full_uuid=full_id,
            jsonl_path=jsonl_rel,
            commit=commit,
            db_mtime=session.get("jsonl_mtime") or None,
            quiet=getattr(args, "quiet", False),
        )
        if restore_result is None or (
            restore_result.wrote == 0 and restore_result.failed
        ):
            print(
                "Restore did not complete cleanly; not distilling.",
                file=sys.stderr,
            )
            conn.close()
            return 1
        print(
            f"Restored {restore_result.wrote} file"
            f"{'s' if restore_result.wrote != 1 else ''} from commit "
            f"{restore_result.commit_short}."
        )

    src_rows = conn.execute(
        "SELECT source_type, source_path FROM session_sources "
        "WHERE session_id = ?",
        (full_id,),
    ).fetchall()
    conn.close()

    mode = getattr(args, "filter", None) or str(
        config.get("distill_filter") or "both"
    )
    chunks, label = _render_session_distill(
        session, src_rows, config["claude_dir"], mode,
        source_override=getattr(args, "source", None),
    )
    if chunks is None:
        print(f"Error: {label}", file=sys.stderr)
        return 1

    # Output routing: a distilled log is a DOCUMENT, often hundreds of KB,
    # so the default is the canonical file (path printed); stdout is the
    # explicit opt-in for piping (--stdout).
    if getattr(args, "stdout", False):
        for chunk in chunks:
            _safe_stdout_write(chunk)
        return 0
    output = getattr(args, "output", None)
    dest = (Path(output) if output
            else _distill_canonical_path(config["claude_dir"], session))
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        for chunk in chunks:
            f.write(chunk)
    print(f"Distilled ({mode}, source: {label}) -> {dest}")
    return 0


def _refresh_distilled_files(conn, config, session_files, quiet) -> int:
    """Backup-time `always` policy: regenerate the canonical distilled
    file for every scanned session whose file is missing or older than
    the live transcript. Fails-soft per session -- a render error never
    fails the backup. Returns the number of files (re)written."""
    claude_dir = config["claude_dir"]
    mode = str(config.get("distill_filter") or "both")
    written = 0
    for sf in session_files:
        try:
            session = get_session(conn, sf.session_id)
            if not session:
                continue
            dest = _distill_canonical_path(claude_dir, session)
            if dest.exists() and dest.stat().st_mtime >= (sf.jsonl_mtime or 0):
                continue
            src_rows = conn.execute(
                "SELECT source_type, source_path FROM session_sources "
                "WHERE session_id = ?",
                (sf.session_id,),
            ).fetchall()
            chunks, _label = _render_session_distill(
                session, src_rows, claude_dir, mode,
            )
            if chunks is None:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "w", encoding="utf-8", newline="\n") as f:
                for chunk in chunks:
                    f.write(chunk)
            written += 1
        except Exception as e:
            if not quiet:
                print(
                    f"Warning: distill failed for {sf.session_id}: {e}",
                    file=sys.stderr,
                )
    return written


def _transcript_is_resumable(jsonl_full_path: Path) -> tuple[bool, str]:
    """Preflight for `claude --resume`: confirm the on-disk JSONL looks like
    a real Claude Code transcript (first non-empty line is a JSON object),
    not garbage -- e.g. a symlink-target path string left by a broken restore,
    or a 0-byte / stub file from a session that was never JSONL-backed.

    Returns ``(ok, reason)``; ``reason`` is ``""`` when ok. Kept deliberately
    lenient: the JSON-object gate catches the real failure modes (bare path
    strings, truncated stubs) without rejecting minimal-but-valid transcripts.
    """
    if not jsonl_full_path.exists():
        return False, "transcript file is not on disk"
    try:
        if jsonl_full_path.stat().st_size == 0:
            return False, "transcript is empty (0 bytes)"
    except OSError as e:
        return False, f"cannot stat transcript ({e})"
    try:
        with open(jsonl_full_path, "r", encoding="utf-8", errors="replace") as f:
            first = ""
            for line in f:
                if line.strip():
                    first = line.strip()
                    break
    except OSError as e:
        return False, f"cannot read transcript ({e})"
    if not first:
        return False, "transcript has no content lines"
    try:
        obj = json.loads(first)
    except (json.JSONDecodeError, ValueError):
        return False, (
            "transcript's first line isn't valid JSON "
            "(looks like a stub or corrupt file)"
        )
    if not isinstance(obj, dict):
        return False, (
            "transcript's first line isn't a JSON object "
            "(not a Claude Code transcript)"
        )
    return True, ""


def cmd_resume(args) -> int:
    """Launch claude --resume with the full session UUID.

    Accepts every identifier `csb view` accepts (#42): UUID/prefix (the
    historical surface), exact session NAME (Claude Code's own /resume
    title semantics -- csb resolves it to the UUID, so Claude always
    receives the one format that is unconditionally direct), .jsonl path,
    directory, sesslog folder name, or free-text keyword.
    """
    from .pathkit import derive_start_at

    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn)

    query = args.session_id
    # Historical UUID/prefix resolver first (prefix/suffix matching,
    # ambiguity reporting with exit code 2). miss_ok: a plain no-match or
    # non-ID-shaped input (a session NAME, path, keyword) falls through to
    # the multi-modal resolver instead of erroring (#42).
    full_id, exit_code = _resolve_session_or_exit(conn, query, miss_ok=True)
    if full_id is None and exit_code:
        conn.close()
        return exit_code
    session = get_session(conn, full_id) if full_id else None
    if session is None:
        result, method = _resolve_session_query(
            query, conn, config["claude_dir"]
        )
        if result is None:
            print(f"Error: {method}", file=sys.stderr)
            conn.close()
            return 1
        if isinstance(result, list):
            label = method.split(":", 1)[1] if ":" in method else method
            _show_view_candidates(result, query, label)
            conn.close()
            return 1
        session = result
    conn.close()

    full_id = session["session_id"]
    name = session.get("session_name") or "(unnamed)"

    # v0.3.14 (#34): if the session is pruned (deleted_at set), Claude Code
    # can't resume it because the JSONL is gone. Offer to restore via git
    # first, then proceed with the resume.
    if session.get("deleted_at"):
        decision = _resolve_pruned_decision(args, session, name, verb="resume")
        if decision == "abort":
            return 0
        if decision == "error":
            return 1
        # decision == "restore" -- run the same _restore_session helper
        # that powers `csb restore`. No CLI flag duplication; this honors
        # the consolidation discipline #34 was filed against.
        jsonl_path_for_restore = session.get("jsonl_path")
        if not jsonl_path_for_restore:
            print(
                f"Session '{name}' is pruned but has no jsonl_path in the "
                f"DB row -- cannot auto-restore. Run `csb restore {full_id}` "
                f"manually (the fallback may find it via git history).",
                file=sys.stderr,
            )
            return 1
        commit_for_restore = git_find_deleted_file(
            config["claude_dir"], jsonl_path_for_restore
        )
        if not commit_for_restore:
            print(
                f"Couldn't find '{jsonl_path_for_restore}' in git history -- "
                f"nothing to restore.",
                file=sys.stderr,
            )
            return 1
        restore_result = _restore_session(
            claude_dir=config["claude_dir"],
            full_uuid=full_id,
            jsonl_path=jsonl_path_for_restore,
            commit=commit_for_restore,
            db_mtime=session.get("jsonl_mtime") or None,
            quiet=getattr(args, "quiet", False),
        )
        if restore_result is None or restore_result.failed:
            print(
                "Restore did not complete cleanly; refusing to invoke "
                "`claude --resume` against an incomplete restore. Run "
                "`csb restore` manually to see details.",
                file=sys.stderr,
            )
            return 1
        if restore_result.recreated_symlinks:
            nr = len(restore_result.recreated_symlinks)
            print(
                f"  (recreated {nr} symlink{'s' if nr != 1 else ''})"
            )
        if restore_result.skipped_symlinks:
            ns = len(restore_result.skipped_symlinks)
            print(
                f"  (skipped {ns} symlink{'s' if ns != 1 else ''} -- "
                f"could not recreate; no symlink privilege?)"
            )
        if restore_result.times_applied:
            nt = restore_result.times_applied
            print(
                f"  (applied original timestamps to {nt} "
                f"file{'s' if nt != 1 else ''})"
            )
        print(
            f"Restored {restore_result.wrote} file"
            f"{'s' if restore_result.wrote != 1 else ''} from commit "
            f"{restore_result.commit_short}. Proceeding with resume."
        )
        print()

    # Preflight (v0.3.15): Claude Code can only resume from a real JSONL
    # transcript. If the on-disk JSONL is empty/corrupt/a stub -- e.g. a
    # session that was never properly JSONL-backed, or one left broken by a
    # past restore -- refuse to launch `claude --resume` against it (which
    # would just print "No conversation found") and point the user at where
    # the conversation actually lives.
    resume_jsonl = session.get("jsonl_path")
    if resume_jsonl:
        ok, reason = _transcript_is_resumable(
            Path(config["claude_dir"]) / resume_jsonl
        )
        if not ok:
            print(f"Cannot resume '{name}': {reason}.", file=sys.stderr)
            print(
                "Claude Code resumes from the JSONL transcript, and this one "
                "isn't usable. If the session was logged by "
                "claude-session-logger, the conversation may still be readable:",
                file=sys.stderr,
            )
            print(f"  csb search <term> --session {full_id}", file=sys.stderr)
            print(
                f"  (or browse ~/.claude/sesslogs/ for a dir containing "
                f"{full_id})",
                file=sys.stderr,
            )
            return 1

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

    # Forward anything after `--` straight to claude (#47), e.g.
    # `csb resume <name> -- --fork-session`.
    claude_cmd = ["claude", "--resume", full_id] + _passthrough_args(args)
    launch_str = " ".join(claude_cmd)

    print(f"Resuming: {name}")
    print(f"  ID: {full_id}")
    if target:
        print(f"  cd {target}")
    print(f"  {launch_str}")
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
            claude_cmd,
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
            print(f"  cd <correct-folder> && {launch_str}", file=sys.stderr)
            return 1
        print("Error: 'claude' command not found in PATH.", file=sys.stderr)
        print(f"Run manually: {launch_str}", file=sys.stderr)
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

    # Deletion-filter scope (Phase 3 / #27; canonical grammar since #41).
    # Precedence: --restore implies deleted scope (restore only applies to
    # deleted sessions) and is applied AFTER normalization; default is
    # "active" (preserves pre-#27 behavior). The deprecated --all alias is
    # absorbed by deleted_mode().
    want_restore = bool(getattr(args, "restore", False))
    mode = deleted_mode(args)
    if want_restore or mode == "only":
        deleted_filter = "deleted"
    elif mode == "all":
        deleted_filter = "all"
    else:
        deleted_filter = "active"

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
            results = find_sessions_by_term(
                conn, term, top_n=top_n, deleted_filter=deleted_filter,
            )
            conn.close()
        except Exception:
            results = []

        return _render_scan_results(
            results, args, config,
            scope_label=_decorate_scope_label(f"matching '{term}'", deleted_filter),
            quiet=quiet,
            deleted_filter=deleted_filter,
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

    # Validate that the resolved path exists (warning, not blocker).
    # Skip this check when running in --deleted / --all mode: those are
    # exactly the queries where the scope path may no longer exist on disk
    # (e.g., user deleted the folder and wants to recover the sessions that
    # were in it). The SQL pass against the DB handles missing paths fine.
    has_wildcard = pattern_input.endswith("*")
    if not has_wildcard and deleted_filter == "active":
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
                    results = find_sessions_by_term(
                        conn, term, top_n=top_n, deleted_filter=deleted_filter,
                    )
                    conn.close()
                except Exception:
                    results = []
                return _render_scan_results(
                    results, args, config,
                    scope_label=_decorate_scope_label(f"matching '{term}'", deleted_filter),
                    quiet=quiet,
                    deleted_filter=deleted_filter,
                )
            else:
                # No fallback term -> empty result set
                return _render_scan_results(
                    [], args, config,
                    scope_label=_decorate_scope_label(f"under {exact_value}", deleted_filter),
                    quiet=quiet,
                    deleted_filter=deleted_filter,
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
                deleted_filter=deleted_filter,
            )
            conn.close()
        except Exception:
            pass  # Index may not exist yet -- graceful fallback

    # Merge: filesystem-scanned (with fresh metadata extraction) + SQLite-only.
    # The filesystem walk inherently only finds present-on-disk sessions, so it
    # cannot contribute deleted rows. When --deleted is set, skip the FS pass
    # entirely -- the SQL pass is authoritative for the "deleted" scope.
    seen_ids: set[str] = set()
    results: list = []

    if deleted_filter != "deleted":
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

    scope_label = _decorate_scope_label(scope_label, deleted_filter)

    return _render_scan_results(
        results, args, config, scope_label=scope_label, quiet=quiet,
        deleted_filter=deleted_filter,
    )


def _decorate_scope_label(label: str, deleted_filter: str) -> str:
    """Return ``label`` unchanged. The deleted-filter mode is rendered
    separately by ``_render_scan_results`` (it becomes the "session(s)"
    qualifier in the user-facing line). Kept as a hook so future scope
    decorations have one place to plug in."""
    return label


def _session_noun(deleted_filter: str, plural: bool = True) -> str:
    """Return the right noun for the filter mode.

    'active' -> 'session(s)'    (default; preserves pre-#27 wording)
    'deleted' -> 'deleted session(s)'
    'all' -> 'session(s) (active+deleted)'
    """
    if deleted_filter == "deleted":
        return "deleted sessions" if plural else "deleted session"
    if deleted_filter == "all":
        return "sessions (active+deleted)" if plural else "session (active or deleted)"
    return "sessions" if plural else "session"


def _render_scan_results(
    results, args, config, scope_label: str, quiet: bool,
    deleted_filter: str = "active",
) -> int:
    """Sort, trim, and render scan results. Shared by all scan modes.

    When ``--restore`` is set (Phase 3), delegate to the bulk-restore path
    instead of rendering. The scope/filter selection happens upstream --
    this function just dispatches.
    """
    no_usage = getattr(args, "no_usage", False)
    noun = _session_noun(deleted_filter)

    if not quiet:
        print(f"Scanning for {noun} {scope_label}...\n")

    # Sort by last activity (most recent first)
    results.sort(
        key=lambda s: s.get("last_user_at") or s.get("last_active_at") or "",
        reverse=True,
    )

    total_found = len(results)

    # Bulk restore path (Phase 3 / #27). Acts on the unrestricted result set
    # (not the -n trim) -- the user's intent is "restore everything matching
    # my scope", not "restore only what would fit on screen".
    if getattr(args, "restore", False):
        return _bulk_restore_jsonls(
            results, args, config, scope_label=scope_label, quiet=quiet,
        )

    results = results[:args.n]

    if not results:
        print(f"  No {noun} found.")
        if no_usage:
            print("  Tip: try without -NU to also search by folder usage.")
        return 0

    cleanup_days = read_cleanup_period(config["claude_dir"])
    top_folders = _resolve_top_folders(args, config)

    if args.__dict__.get("json"):  # not all parsers have --json yet for scan
        print(json.dumps(results, indent=2, default=str))
        return 0

    count_noun = _session_noun(deleted_filter)
    print(f"Found {total_found} {count_noun} {scope_label}" +
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


def _bulk_restore_jsonls(results, args, config, scope_label: str, quiet: bool) -> int:
    """
    Restore the JSONL for each session in ``results`` from git history.

    Driven by ``csb scan ... --restore``. Per the plan:

      - Requires at least one match. Empty results => print "Nothing to
        restore" and exit 0 (not an error -- the user's scope was just
        empty).
      - For one match: skip the confirmation prompt and proceed (the user
        already typed --restore; one file is unambiguous).
      - For >1 matches: confirm interactively unless ``--yes``. ``--dry-run``
        shows the preview without prompting.
      - For each result, skip if the on-disk file already exists unless
        ``--force``. Per-file status is printed.
      - Takes ``backup_lock`` for the file-write phase to avoid races with
        a concurrent ``csb backup`` (which would see the just-restored
        file as "new" with current mtime).

    Returns 0 on success (every file either restored, skipped-as-present,
    or dry-run-previewed). Returns 1 if any file failed to restore.
    """
    claude_dir = config["claude_dir"]
    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False))
    force = bool(getattr(args, "force", False))

    if not results:
        if not quiet:
            print(f"  Nothing to restore -- no deleted sessions {scope_label}.")
        return 0

    # For sessions in `results` that lack jsonl_path, we can't restore. Drop
    # them with a notice -- this shouldn't happen post-v0.2.4 but legacy
    # rows might exist.
    candidates: list[tuple[dict, str]] = []  # (session, jsonl_path)
    skipped_no_path = 0
    for s in results:
        p = s.get("jsonl_path")
        if not p:
            skipped_no_path += 1
            continue
        candidates.append((s, p))

    if not candidates:
        print(f"  No restorable rows (all {len(results)} matches lack jsonl_path).",
              file=sys.stderr)
        return 1

    n = len(candidates)
    if not quiet:
        verb = "Would restore" if dry_run else "Restore"
        print(f"{verb} {n} session JSONL(s) from git history:\n")
        for s, p in candidates:
            uuid = s["session_id"]
            name = s.get("session_name") or "(unnamed)"
            print(f"  {uuid[:8]}  {name}")
            print(f"            {p}")
        print()

    # Confirm-prompt guardrail: > 1 file and not --yes / not --dry-run.
    if n > 1 and not (yes or dry_run):
        try:
            reply = input(f"Proceed to restore {n} files? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            return 1
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 0

    if dry_run:
        if not quiet:
            print("(dry-run -- no files written)")
        return 0

    # Take backup_lock for the duration of the writes. Without this, a
    # concurrent `csb backup` would race: it might see the just-restored
    # file before its mtime stabilizes, mark it as a new session, then
    # commit stale metadata. The lock is short -- released as soon as
    # the loop completes.
    with backup_lock(claude_dir) as acquired:
        if not acquired:
            print(
                "Another csb backup is running. Wait for it to finish "
                "before restoring (avoids a race that could mark the "
                "restored file as 'new' with wrong metadata).",
                file=sys.stderr,
            )
            return 1

        restored = 0
        skipped = 0
        failed = 0
        for s, jsonl_rel in candidates:
            full_path = ClaudePaths.from_dir(claude_dir).abs_of(jsonl_rel)
            if full_path.exists() and not force:
                print(f"  SKIP  {s['session_id'][:8]}  {jsonl_rel} "
                      f"(already exists; use --force to overwrite)")
                skipped += 1
                continue
            commit = git_find_deleted_file(claude_dir, jsonl_rel)
            if not commit:
                print(f"  FAIL  {s['session_id'][:8]}  {jsonl_rel} "
                      f"(not in git history)", file=sys.stderr)
                failed += 1
                continue
            ok = git_restore_file(claude_dir, commit, jsonl_rel, full_path)
            if ok:
                print(f"  OK    {s['session_id'][:8]}  {jsonl_rel} "
                      f"(from {commit[:8]})")
                restored += 1
            else:
                print(f"  FAIL  {s['session_id'][:8]}  {jsonl_rel} "
                      f"(restore failed)", file=sys.stderr)
                failed += 1

    if not quiet:
        print()
        print(f"  Restored: {restored}    Skipped: {skipped}    Failed: {failed}")
        if skipped_no_path:
            print(f"  ({skipped_no_path} matches dropped -- no jsonl_path in DB row)")
    return 1 if failed else 0

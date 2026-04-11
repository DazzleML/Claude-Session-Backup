"""
Command implementations for the csb CLI.

Each cmd_* function receives parsed args and returns an exit code.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config, resolve_paths, save_config, read_cleanup_period
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
    get_session,
    get_stats,
    init_schema,
    list_sessions,
    mark_deleted,
    open_db,
    record_scan,
    search_sessions,
    upsert_session,
)
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


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def cmd_backup(args) -> int:
    """Scan sessions, update index, optionally git commit."""
    config = _get_config(args)
    claude_dir = config["claude_dir"]
    quiet = getattr(args, "quiet", False)

    # Acquire lock (prevent concurrent cron runs)
    with backup_lock(claude_dir) as acquired:
        if not acquired:
            if not quiet:
                print("Another csb backup is already running. Skipping.", file=sys.stderr)
            return 0  # Not an error -- just skip
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

    previously_known = get_active_session_ids(conn)
    found_ids = set()
    new_count = 0
    updated_count = 0

    error_count = 0
    for i, sf in enumerate(sessions):
        found_ids.add(sf.session_id)
        is_new = sf.session_id not in previously_known

        try:
            # Extract metadata from JSONL
            meta = extract_metadata(sf.jsonl_path, config["top_n_folders"])
            meta.project = sf.project

            # Enrich with session-state info
            if sf.state_file:
                state = read_session_state(sf.state_file)
                name_cache = read_name_cache(sf.name_cache) if sf.name_cache else None
                enrich_metadata(meta, state, name_cache)

            # Upsert into index
            rel_path = str(sf.jsonl_path.relative_to(claude_dir))
            upsert_session(conn, meta, rel_path, sf.jsonl_size, sf.jsonl_mtime, now)

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

    sessions = list_sessions(
        conn,
        limit=args.n,
        show_deleted=args.deleted,
        show_all=args.all,
        filter_keyword=getattr(args, "filter", None),
        sort_key=getattr(args, "sort", "last-used"),
    )
    conn.close()

    cleanup_days = read_cleanup_period(config["claude_dir"])

    if args.json:
        print(json.dumps(sessions, indent=2, default=str))
    elif HAS_RICH:
        render_timeline_rich(sessions, cleanup_days=cleanup_days)
    else:
        print(format_timeline(sessions, cleanup_days=cleanup_days))

    return 0


def cmd_status(args) -> int:
    """Summary of sessions, deletions, git state."""
    config = _get_config(args)
    claude_dir = config["claude_dir"]

    conn = open_db(config["index_path"])
    init_schema(conn)
    stats = get_stats(conn)
    conn.close()

    print(f"Claude Session Backup Status")
    print(f"  Claude dir:    {claude_dir}")
    print(f"  Git repo:      {'yes' if is_git_repo(claude_dir) else 'NO'}")
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
    if is_git_repo(claude_dir):
        status = git_status(claude_dir)
        changed = len([l for l in status.split("\n") if l.strip()])
        if changed:
            print(f"  Uncommitted changes: {changed} files")
        else:
            print(f"  Working tree: clean")

    return 0


def cmd_show(args) -> int:
    """Detailed session info with folder analysis."""
    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn)

    session = get_session(conn, args.session_id)
    conn.close()

    if not session:
        print(f"No session found matching '{args.session_id}'", file=sys.stderr)
        return 1

    print(f"Session: {session['session_name'] or '(unnamed)'}")
    print(f"  ID:            {session['session_id']}")
    print(f"  Project:       {session['project']}")
    print(f"  Start folder:  {session['start_folder'] or '(unknown)'}")
    print(f"  Started:       {session['started_at'] or '(unknown)'}")
    print(f"  Last active:   {session['last_active_at'] or '(unknown)'}")
    print(f"  Messages:      {session['message_count']}")
    print(f"  Tool calls:    {session['tool_call_count']}")
    print(f"  Claude ver:    {session['claude_version'] or '(unknown)'}")
    print(f"  JSONL path:    {session['jsonl_path']}")
    print(f"  JSONL size:    {session['jsonl_size']:,} bytes")

    if session.get("deleted_at"):
        print(f"  DELETED at:    {session['deleted_at']}")
        print(f"  Restore with:  csb restore {session['session_id']}")

    print(f"\n  Resume:        claude --resume {session['session_id']}")

    folders = session.get("folders", [])
    if folders:
        print(f"\n  Working directories:")
        for f in folders:
            marker = " [start]" if f["is_start_folder"] else ""
            print(f"    {f['folder_path']}  ({f['usage_count']}x){marker}")

    return 0


def cmd_restore(args) -> int:
    """Restore deleted session from git history."""
    config = _get_config(args)
    claude_dir = config["claude_dir"]

    conn = open_db(config["index_path"])
    init_schema(conn)
    session = get_session(conn, args.session_id)
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
    """Search session metadata."""
    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn)

    sessions = search_sessions(conn, args.query, args.n)
    conn.close()

    if not sessions:
        print(f"No sessions matching '{args.query}'")
        return 0

    print(f"Found {len(sessions)} session(s) matching '{args.query}':\n")
    for i, session in enumerate(sessions, 1):
        print(format_session_line(session, i))
        print()

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
    """View/edit configuration."""
    config = load_config(getattr(args, "claude_dir", None))

    if args.key is None:
        # Show all config
        print(json.dumps(config, indent=2))
        return 0

    if args.value is None:
        # Show specific key
        if args.key in config:
            val = config[args.key]
            print(json.dumps(val) if isinstance(val, (list, dict)) else str(val))
        else:
            print(f"Unknown config key: {args.key}", file=sys.stderr)
            return 1
        return 0

    # Set value
    try:
        parsed = json.loads(args.value)
    except json.JSONDecodeError:
        parsed = args.value

    config[args.key] = parsed
    save_config(config, getattr(args, "claude_dir", None))
    print(f"Set {args.key} = {parsed}")
    return 0


def cmd_resume(args) -> int:
    """Launch claude --resume with the full session UUID."""
    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn)

    session = get_session(conn, args.session_id)
    conn.close()

    if not session:
        print(f"No session found matching '{args.session_id}'", file=sys.stderr)
        return 1

    full_id = session["session_id"]
    start_folder = session.get("start_folder")
    name = session.get("session_name") or "(unnamed)"

    print(f"Resuming: {name}")
    print(f"  ID: {full_id}")
    if start_folder:
        print(f"  cd {start_folder}")
    print(f"  claude --resume {full_id}")
    print()

    # Execute claude --resume (replaces this process)
    try:
        os.execvp("claude", ["claude", "--resume", full_id])
    except FileNotFoundError:
        print("Error: 'claude' command not found in PATH.", file=sys.stderr)
        print(f"Run manually: claude --resume {full_id}", file=sys.stderr)
        return 1


def cmd_scan(args) -> int:
    """Find sessions whose project path matches the given directory."""
    from .scanner import scan_for_path
    from .metadata import extract_metadata
    from .index import find_sessions_by_folder_usage

    config = _get_config(args)
    root = Path(args.path).resolve()
    quiet = getattr(args, "quiet", False)
    no_usage = getattr(args, "no_usage", False)

    if not quiet:
        print(f"Scanning for sessions under {root}...\n")

    # Step 1: Prefix match on project folders (always)
    sessions = scan_for_path(config["claude_dir"], str(root))
    seen_ids = {sf.session_id for sf in sessions}

    # Step 2: Also search folder_usage in the index (unless --no-usage / -NU)
    usage_results = []
    if not no_usage:
        try:
            conn = open_db(config["index_path"])
            init_schema(conn)
            usage_results = find_sessions_by_folder_usage(conn, str(root))
            conn.close()
        except Exception:
            pass  # Index may not exist yet -- graceful fallback

    # Extract metadata from disk-scanned sessions
    results = []
    for sf in sessions:
        try:
            meta = extract_metadata(sf.jsonl_path, top_n_folders=3)
            meta.project = sf.project

            entry = {
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
            }
            results.append(entry)
        except Exception:
            continue

    # Merge in usage-matched sessions (from index, not disk)
    for session in usage_results:
        if session["session_id"] not in seen_ids:
            seen_ids.add(session["session_id"])
            results.append(session)

    # Sort all results by last activity (most recent first)
    results.sort(
        key=lambda s: s.get("last_user_at") or s.get("last_active_at") or "",
        reverse=True,
    )

    # Trim to requested count
    total_found = len(results)
    results = results[:args.n]

    if not results:
        print("  No sessions found.")
        if no_usage:
            print("  Tip: try without -NU to also search by folder usage.")
        return 0

    cleanup_days = read_cleanup_period(config["claude_dir"])

    print(f"Found {total_found} session(s) under {root}" +
          (f" (showing top {args.n}):" if total_found > args.n else ":"))
    print()

    if HAS_RICH:
        render_timeline_rich(results, cleanup_days=cleanup_days)
    else:
        print(format_timeline(results, cleanup_days=cleanup_days))

    return 0

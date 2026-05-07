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
    top_folders = _resolve_top_folders(args, config)

    if args.json:
        print(json.dumps(sessions, indent=2, default=str))
    elif HAS_RICH:
        render_timeline_rich(sessions, cleanup_days=cleanup_days, top_folders=top_folders)
    else:
        print(format_timeline(sessions, cleanup_days=cleanup_days, top_folders=top_folders))

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
    from .pathkit import derive_start_at

    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn)

    session = get_session(conn, args.session_id)
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
        render_timeline_rich(results, cleanup_days=cleanup_days, top_folders=top_folders)
    else:
        print(format_timeline(results, cleanup_days=cleanup_days, top_folders=top_folders))

    return 0

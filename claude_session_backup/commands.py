"""
Command implementations for the csb CLI.

Each cmd_* function receives parsed args and returns an exit code.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
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
    ensure_gitignore,
    git_repo_state,
    run_git,
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
)
from .lockfile import backup_lock
from .epochs import (
    FenceUnavailableError,
    build_roster,
    classify_epoch_token,
    enumerate_epochs,
    epoch_window,
    latest_epoch,
    parse_index_ts,
    read_fences,
    resolve_epoch_token,
)
from . import session_sets
from .set_render import epoch_header_segments, render_roster
from .index import (
    count_deleted_with_filter,
    fetch_activity_segments,
    find_sessions_by_folder_usage,
    get_active_session_ids,
    get_all_known_session_ids,
    get_indexed_mtime,
    get_session,
    get_stats,
    index_is_unbuilt,
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
from .scanner import count_session_jsonls, scan_projects
from .schedule import (
    DEFAULT_INTERVAL_MINUTES,
    INTERVAL_PRESETS,
    ScheduleError,
    ScheduleSpec,
    build_command_argv,
    derive_fire_time,
    validate_entry_argv,
    validate_interval,
)
from .schedule_backends import (
    REFUSAL_TEXT,
    SCHEDULE_REFUSED_EXIT,
    BackendStatus,
    choose_backend,
    evaluate_schedule_evidence,
    render_systemd_recipe,
)
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


def _rebind_streams_to_log(log_path: Path) -> None:
    """Point None std streams at the run log (#69 AC-10, pythonw reality).

    Under ``pythonw.exe`` both ``sys.stdout`` and ``sys.stderr`` are
    ``None``: bare ``print()`` is a silent no-op, direct stream writes
    raise, and an unhandled traceback has nowhere to go -- the process
    would die with only an exit code. Rebinding both to the log converts
    every one of those modes into captured, diagnosable output. Must run
    before ANY other output; failures degrade to leaving streams as-is
    (an unloggable run must still back up).
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle


def _append_run_log(log_path: Path, outcome: str, rc: int,
                    started_monotonic: float, claude_dir) -> None:
    """One structured line per backup run (#69 AC-10/AC-13).

    Wire format (stable, parsed later by schedule status -- 5b):
      ``<local ISO ts> outcome=<ok|skipped-lock|error> rc=<n> duration_ms=<n> claude_dir=<path>``

    A run that cannot write its line still succeeds (AC-10: logging
    failure never fails the backup) -- but note the dedicated filename is
    itself the scheduled-vs-hook source marker, so an unwritable log is
    surfaced by schedule status as missing evidence, not silently."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Dumb size-capped rotation, one generation (AC-10): at ~120
        # bytes/line even a 15-min cadence takes years to hit 1 MB, and
        # the .1 keeps enough history for any INSTALLED-BUT-NOT-RUNNING
        # forensics. Never let rotation failure block the line.
        try:
            if log_path.exists() and log_path.stat().st_size > 1_000_000:
                rotated = log_path.with_suffix(log_path.suffix + ".1")
                if rotated.exists():
                    rotated.unlink()
                log_path.rename(rotated)
        except OSError:
            pass
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(
                f"{stamp} outcome={outcome} rc={rc} "
                f"duration_ms={duration_ms} claude_dir={claude_dir}\n"
            )
    except OSError:
        pass


def cmd_backup(args) -> int:
    """Scan sessions, update index, optionally git commit."""
    log_file = getattr(args, "log_file", None)
    log_path = Path(log_file) if log_file else None
    if log_path is not None:
        _rebind_streams_to_log(log_path)  # before any output (pythonw)
    started = time.monotonic()

    config = _get_config(args)
    claude_dir = config["claude_dir"]
    quiet = getattr(args, "quiet", False)

    # Acquire lock (prevent concurrent cron runs). backup_lock now owns the
    # skip / stale-reclaim messaging (it has the lock's identity + age), so
    # we just honor the acquired flag here. The loser still logs its line:
    # "another instance won" is evidence the schedule fired (AC-13).
    with backup_lock(claude_dir, quiet=quiet) as acquired:
        if not acquired:
            if log_path is not None:
                _append_run_log(log_path, "skipped-lock", 0, started, claude_dir)
            return 0  # Not an error -- another instance is running
        try:
            rc = _cmd_backup_inner(args, config, claude_dir, quiet)
        except BaseException:
            if log_path is not None:
                _append_run_log(log_path, "error", -1, started, claude_dir)
            raise
    if log_path is not None:
        _append_run_log(log_path, "ok" if rc == 0 else "error",
                        rc, started, claude_dir)
    return rc


def _cmd_backup_inner(args, config, claude_dir, quiet) -> int:
    """Inner backup logic (runs under lock)."""

    # Verify git repo. Without an ACCEPTED one, `--no-commit` still
    # proceeds in index-only mode (#52): the index powers every read
    # command, and the recovery moment is exactly when setup is most
    # likely broken. Bare `backup` keeps the hard error -- but the error
    # must state what git ACTUALLY said: "refused" (repo exists, git
    # declines it -- ownership/safety) is not "absent", and prescribing
    # `git init` for a refusal is the wrong medicine (it reinforces the
    # loop: init "succeeds", nothing changes).
    repo_state, repo_detail = git_repo_state(claude_dir)
    repoless = repo_state != "ok"
    if repoless and not args.no_commit:
        if repo_state == "refused":
            print(f"Error: a git repository EXISTS at {claude_dir}, but git "
                  f"REFUSES it in this shell:", file=sys.stderr)
            for line in (repo_detail or "").splitlines():
                print(f"    {line}", file=sys.stderr)
            print("Run `csb setup` for diagnosis and fixes. Do NOT re-initialize "
                  "-- the repository and its history are intact.", file=sys.stderr)
        elif repo_state == "error":
            print(f"Error: git itself failed: {repo_detail}", file=sys.stderr)
        else:
            print(f"Error: {claude_dir} is not a git repository.", file=sys.stderr)
            print("Run `csb setup` for guided configuration "
                  "(or initialize directly: git -C ~/.claude init).", file=sys.stderr)
        print(
            "Or run `csb backup --no-commit` to build the search index "
            "without git (index-only: no backup protection).",
            file=sys.stderr,
        )
        return 1

    # Defense in depth: ensure .gitattributes has the csb-managed block that
    # marks session JSONLs / sidecars as binary (no autocrlf, no eol filter).
    # Without this, a future commit on a host with `core.autocrlf=true` could
    # store CRLF-corrupted blobs that no amount of restore-side care can fix.
    # Idempotent -- only writes when the block is missing.
    if not repoless:
        ensure_gitattributes(claude_dir)
        # Same idiom, opposite direction (#69 AC-21): keep csb's
        # OPERATIONAL artifacts (run logs, index + sidecars, lock, FTS)
        # out of a human's `git add -A` -- csb's own staging is
        # allowlist-only and never needed this, but the store deserves
        # protection from more than csb.
        ensure_gitignore(claude_dir)

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

    # Index-only warning (#52): emitted even under --quiet -- hook runs log
    # stderr, and "this was NOT a backup" must never be silent. The text
    # always pairs what happened (indexed) with what did not (protection),
    # and states the TRUE reason (absent vs refused vs git error).
    if repoless:
        if repo_state == "refused":
            why = (f"a repo EXISTS at {claude_dir} but git REFUSES it "
                   f"in this shell")
            fix = "Run `csb setup` for diagnosis -- do NOT re-initialize."
        elif repo_state == "error":
            why = f"git itself failed: {repo_detail}"
            fix = "Fix the git installation, then run `csb setup`."
        else:
            why = f"{claude_dir} is not a git repository"
            fix = f"Enable full backup with `csb setup` (or: git -C {claude_dir} init)"
        print(
            f"[index-only] Indexed {len(sessions)} session(s) -- NO backup "
            f"protection ({why}).",
            file=sys.stderr,
        )
        print(f"[index-only] {fix}", file=sys.stderr)

    return 0


# ── csb setup: guided onboarding (v0.6.0, #52) ──────────────────────


def _write_config_file_keys(claude_dir, **keys):
    """Update only the given keys in session-backup-config.json (creating
    the file if needed). Deliberately NOT ``save_config``: that dumps the
    whole merged config, freezing resolved defaults (like paths) into the
    file.
    """
    from .config import get_config_path

    path = get_config_path(claude_dir)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    data.update(keys)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _interactive() -> bool:
    """Whether prompts can be answered (stdin is a real terminal)."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:  # noqa: BLE001 -- treat unknowable stdin as non-tty
        return False


def _ask(question: str) -> str | None:
    """One prompt; None on EOF/interrupt (caller aborts cleanly)."""
    try:
        return input(question).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _clear_index_only_ack(config, claude_dir):
    """A repo now exists (or was just created): the signed index-only
    exception is stale -- clear it loudly so the config matches reality."""
    if str(config.get("backup_mode") or "full") == "index-only":
        _write_config_file_keys(
            claude_dir, backup_mode="full", index_only_ack_at=None,
        )
        print("[setup] Cleared the old index-only sign-off -- backups are the "
              "mode of record again.")


def _style_print(segments, err=False):
    """Print one line from (text, style) segments -- rich-styled when
    available (colors only reach real terminals; pipes/captures get plain
    text), plain print otherwise. Styles follow the timeline renderer's
    palette: green = done/protected, yellow = pending/exception, red =
    problem, cyan = a command to run, dim = detail.
    """
    stream = sys.stderr if err else sys.stdout
    if HAS_RICH:
        from rich.console import Console
        from rich.text import Text

        line = Text()
        for text, style in segments:
            line.append(text, style or "")
        # soft_wrap: never hard-wrap checklist rows at a fake 80-col width
        # when the stream isn't a terminal (pipes, tests).
        Console(file=stream, highlight=False, soft_wrap=True).print(line)
    else:
        print("".join(text for text, _ in segments), file=stream)


# Plugin identifiers in Claude Code's own registries -- used to detect
# (never install) companion tooling during `csb setup`. The csb plugin
# provides the auto-backup hooks; claude-session-logger is OPTIONAL but
# enhances csb (richer `csb search` channels via .convo/.sesslog
# transcripts, session-name enrichment) -- csb backs up and restores its
# files, so setup surfaces it as a considered choice, not a requirement.
_PLUGIN_KEY = ClaudePaths.PLUGIN_SPEC          # one home: pathkit (#75 U5)
_MARKETPLACE_KEY = ClaudePaths.PLUGIN_MARKETPLACE
_LOGGER_PLUGIN_KEY = "session-logger@dazzle-claude-plugins"
_LOGGER_MARKETPLACE_KEY = "dazzle-claude-plugins"
_LOGGER_MARKETPLACE_REPO = "DazzleML/claude-session-logger"


def _plugin_status(claude_dir, marketplace_key=_MARKETPLACE_KEY,
                   plugin_key=_PLUGIN_KEY):
    """(marketplace_added, plugin_installed) read from Claude Code's plugin
    registries (`plugins/known_marketplaces.json` / `installed_plugins.json`).
    Best-effort: absent or unreadable registries read as not-done -- setup
    then shows the install commands, which is the safe direction to be
    wrong in.
    """
    base = ClaudePaths.from_dir(claude_dir).plugins
    marketplace = False
    plugin = False

    def _read_json(path):
        # utf-8-sig: tolerate a BOM (e.g. a registry rewritten by
        # PowerShell 5.1 Set-Content), which plain utf-8 + json.loads
        # rejects -- and a rejected registry reads as "not installed",
        # re-instructing an already-done step (the #53 sin).
        return json.loads(path.read_text(encoding="utf-8-sig"))

    try:
        m = _read_json(base / "known_marketplaces.json")
        marketplace = marketplace_key in (m or {})
    except (OSError, ValueError):
        pass
    try:
        p = _read_json(base / "installed_plugins.json")
        plugin = bool((p or {}).get("plugins", {}).get(plugin_key))
    except (OSError, ValueError):
        pass
    return marketplace, plugin


def _last_backup_summary(config):
    """One-line summary of the last indexed scan, or None if never ran."""
    try:
        conn = open_db(config["index_path"])
        init_schema(conn)
        stats = get_stats(conn)
        conn.close()
        scan = stats.get("last_scan")
        if not scan:
            return None
        when = scan.get("scanned_at") or "?"
        try:
            # Compact local time for the one-line checklist row (the full
            # ISO form stays available via `csb status`).
            when = (datetime.fromisoformat(when.replace("Z", "+00:00"))
                    .astimezone().strftime("%Y-%m-%d %H:%M"))
        except (ValueError, TypeError):
            pass
        commit = (scan.get("git_commit") or "")[:8]
        return f"last scan {when}" + (f", commit {commit}" if commit else "")
    except Exception:  # noqa: BLE001 -- checklist is best-effort reporting
        return None


def _checklist_done(label, detail):
    _style_print([("  [x] ", "bold green"), (f"{label:<22}", "green"),
                  (f"({detail})", "dim")])


def _checklist_todo(label, note):
    _style_print([("  [ ] ", "bold yellow"), (f"{label:<22}", "yellow"),
                  (note, "dim")])


def _checklist_optional(label, note):
    """[~] -- an optional enhancement worth considering, NOT a pending
    requirement. Blue: the intermediate tone between green (done) and
    yellow (needs doing), so optional rows never read as obligations.
    """
    _style_print([("  [~] ", "bold blue"), (f"{label:<22}", "blue"),
                  (note, "dim")])


def _checklist_cmd(command):
    _style_print([("        -> ", "dim"), (command, "cyan")])


def _setup_checklist(config, claude_dir, repo_note):
    """State-aware closing checklist: [x] for what's already configured,
    [ ] with the exact command only for what's actually missing -- setup
    never tells a user to run something that's already done. Green = done,
    yellow = still to do, cyan = the command that does it.
    """
    print()
    _style_print([("Setup checklist:", "bold")])
    _checklist_done("git backup store", repo_note)

    backup = _last_backup_summary(config)
    if backup:
        _checklist_done("first backup", backup)
    else:
        _checklist_todo("first backup", "")
        _checklist_cmd("csb backup")

    marketplace, plugin = _plugin_status(claude_dir)
    if marketplace and plugin:
        # Drift surfaces here too (#75 U3): installed-but-stale withholds
        # hook-side fixes just as surely as not-installed withholds hooks.
        from ._version import get_base_version
        installed_ver = installed_plugin_version(claude_dir)
        if installed_ver is not None and installed_ver != get_base_version():
            _checklist_todo(
                "auto-backup plugin",
                f"({installed_ver} installed -- csb is {get_base_version()})")
            _checklist_cmd("csb setup update")
        else:
            _checklist_done("auto-backup plugin", f"{_PLUGIN_KEY} installed")
    else:
        _checklist_todo("auto-backup plugin", "(fires on PreCompact + SessionEnd)")
        if not marketplace:
            _checklist_cmd('claude plugin marketplace add "DazzleML/Claude-Session-Backup"')
        _checklist_cmd(f"claude plugin install {_PLUGIN_KEY}")

    # claude-session-logger (#53): OPTIONAL companion. Plugin presence is
    # what matters (a local-clone install registers the same plugin key,
    # possibly under a "./" marketplace) -- installed means done even if
    # the GitHub marketplace entry is absent.
    lg_marketplace, lg_plugin = _plugin_status(
        claude_dir, _LOGGER_MARKETPLACE_KEY, _LOGGER_PLUGIN_KEY,
    )
    if lg_plugin:
        _checklist_done("session logger", f"{_LOGGER_PLUGIN_KEY} installed")
    else:
        _checklist_optional(
            "session logger",
            "(optional -- richer csb search + session-name channels)",
        )
        if not lg_marketplace:
            _checklist_cmd(f'claude plugin marketplace add "{_LOGGER_MARKETPLACE_REPO}"')
        _checklist_cmd(f"claude plugin install {_LOGGER_PLUGIN_KEY}")

    # Scheduled backup (#69, AC-18): the only layer that runs when Claude
    # Code does NOT -- the hooks above can't protect a machine that sits
    # untouched while Claude Code's own cleanup counts down. Best-effort:
    # a probe that can't run never breaks setup's exit.
    try:
        st = _safe_backend_status(choose_backend())
        if st.installed:
            _checklist_done("scheduled backup", "OS schedule installed")
        else:
            _checklist_todo("scheduled backup",
                            "(runs backups even when Claude Code doesn't)")
            _checklist_cmd("csb setup schedule")
    except Exception:  # noqa: BLE001 -- checklist is best-effort reporting
        pass

    print()
    _style_print([("Anytime: ", "dim"), ("csb list", "cyan"), (" (timeline), ", "dim"),
                  ("csb status", "cyan"), (" (protection + index state)", "dim")])


def _setup_refusal(args, config, claude_dir, state, detail) -> int:
    """Setup's path for "git can see the repo but declines it" (refused)
    and "git itself is broken" (error). NEVER offers `git init` here --
    the repository and its history are intact; re-initializing is at best
    a no-op that restarts the loop, and telling users to do it teaches
    them to distrust every other csb message.
    """
    if state == "error":
        _style_print([(f"Git: ERROR -- {detail}", "bold red")])
        print("csb needs a working `git` on PATH. Fix the git installation, "
              "then re-run `csb setup`.")
        return 1

    _style_print([("Git repo: PRESENT -- but git REFUSES it in this shell.",
                   "bold red")])
    print("Your repository and backup history are intact. git said:")
    for line in (detail or "(no detail)").splitlines():
        _style_print([(f"    {line}", "dim")])
    print()
    print("The usual cause: directory ownership. If the Claude dir is owned by")
    print("Administrators/another account, git's safety check accepts it in")
    print("ELEVATED shells and refuses it in standard ones -- so csb can look")
    print("broken in one window and fine in another.")
    print()
    print("Fixes, most durable first:")
    print(f"  1. Make your own account the OWNER of {claude_dir}")
    print("     (elevated: Explorer > Properties > Security > Advanced > Owner,")
    print("      or icacls /setowner -- careful: the dir can contain symlinks/")
    print("      junctions; avoid blind recursive takeown).")
    safe_dir_value = str(claude_dir).replace("\\", "/")
    print("  2. Band-aid: tell git to trust the path as-is (applies wherever")
    print("     this git config is read; hides the ownership oddity):")
    _style_print([("       ", None),
                  (f'git config --global --add safe.directory "{safe_dir_value}"',
                   "cyan")])

    # Explained, DEFAULT-NO offer (decision ledger: never automatic -- it
    # weakens a git security control on csb's behalf). Interactive only;
    # --auto must not mutate the user's git config.
    if _interactive() and not getattr(args, "auto", False):
        answer = _ask("Run that safe.directory command now? [y/N] ")
        if answer is not None and answer.lower() in ("y", "yes"):
            r = run_git(claude_dir, "config", "--global", "--add",
                        "safe.directory", safe_dir_value, check=False)
            if r.returncode != 0:
                print(f"Error: git config failed: "
                      f"{(r.stderr or '').strip()}", file=sys.stderr)
                return 1
            new_state, _ = git_repo_state(claude_dir)
            if new_state == "ok":
                _style_print([("[setup] ", "bold green"),
                              ("git now accepts the repository -- backups are "
                               "enabled again.", "green")])
                ensure_gitattributes(claude_dir)
                ensure_gitignore(claude_dir)
                _clear_index_only_ack(config, claude_dir)
                _setup_checklist(config, claude_dir, f"repo at {claude_dir}")
                return 0
            print("git still refuses the repository -- the refusal is coming "
                  "from something else; see git's message above.")
            return 1
    print()
    print("Re-run `csb setup` after fixing -- it is safe to run repeatedly.")
    return 1


def _setup_index_only(args, config, claude_dir) -> int:
    """The EXPLICIT exception path: record a sign-off that csb runs
    index-only. Interactive runs must type the mode name back (informed
    consent); the flag itself is the consent in non-TTY/scripted runs.
    """
    state, _detail = git_repo_state(claude_dir)
    if state == "ok":
        _style_print([(f"A git repository already protects {claude_dir} -- "
                       f"index-only would be a downgrade for no gain.", "green")])
        print("Nothing recorded. (Backups continue to work.)")
        return 0
    if state == "refused":
        _style_print([("Note: a git repository EXISTS here -- git is refusing "
                       "it in this shell. `csb setup` can likely fix that; "
                       "signing off index-only would abandon working backups.",
                       "yellow")])

    _style_print([("Index-only mode means:", "bold")])
    print("  - sessions are INDEXED: `csb list` / `scan` / `search` / `resume` work")
    _style_print([("  - nothing is PRESERVED: no commits, no deletion history, "
                   "no `csb restore`", "red")])
    print("  - the no-protection banner is silenced -- this sign-off is the record")
    print("  - `csb setup` re-enables full protection at any time")
    if _interactive() and not getattr(args, "auto", False):
        answer = _ask('Type "index-only" to confirm you understand: ')
        if answer is None or answer.lower() != "index-only":
            print("Not confirmed -- nothing recorded.")
            return 2
    _write_config_file_keys(
        claude_dir,
        backup_mode="index-only",
        index_only_ack_at=_now_iso(),
    )
    _style_print([("[setup] ", "bold yellow"),
                  (f"Recorded index-only sign-off (backup_mode=index-only) "
                   f"for {claude_dir}.", "yellow")])
    _style_print([("[setup] ", "bold yellow"),
                  ("Reminder: NOTHING IS BACKED UP in this mode.", "red")])
    return 0


def _claude_dir_source(args) -> str:
    """Which mechanism selected the Claude dir, in load_config's precedence
    order (flag > CLAUDE_DIR > CLAUDE_CONFIG_DIR > config file > default).
    Shown in setup's header so relocated setups (docker, VMs, host-mounts,
    worktree isolation -- #45) can confirm the RIGHT dir is about to be
    configured before anything mutates.
    """
    from .config import ENV_CLAUDE_DIR, ENV_CLAUDE_CONFIG_DIR, get_config_path

    if getattr(args, "claude_dir", None):
        return "--claude-dir"
    if os.environ.get(ENV_CLAUDE_DIR):
        return f"{ENV_CLAUDE_DIR} env"
    if os.environ.get(ENV_CLAUDE_CONFIG_DIR):
        return f"{ENV_CLAUDE_CONFIG_DIR} env"
    try:
        cfg_path = get_config_path(None)
        if cfg_path.exists() and "claude_dir" in json.loads(
                cfg_path.read_text(encoding="utf-8")):
            return "config file"
    except (OSError, ValueError):
        pass
    return "default"


def cmd_setup(args) -> int:
    """Guided onboarding (v0.6.0, #52): configure the git backup store.

    The protected state is the point of csb; this command is THE way to
    reach it. Detects an existing repo (own or ancestor -- the home-repo
    layout counts), offers/executes `git init` (interactive Y/n, or
    `--auto` with no prompts), hardens .gitattributes, offers the first
    backup, and prints the plugin next-steps. `--index-only` is the
    explicit signed exception (see _setup_index_only).

    Exit codes: 0 configured (or already protected / ack recorded),
    1 error, 2 declined / not confirmed / non-interactive without flags.
    """
    # #69: `csb setup schedule` is its own flow. Bare `csb setup` (with or
    # without --auto) NEVER installs OS persistence -- the schedule always
    # takes this explicit extra word (AC-17).
    if getattr(args, "setup_action", None) == "schedule":
        return cmd_setup_schedule(args)
    if getattr(args, "setup_action", None) == "update":
        return cmd_setup_update(args)

    config = _get_config(args)
    claude_dir = config["claude_dir"]
    auto = getattr(args, "auto", False)
    quiet = getattr(args, "quiet", False)

    _style_print([("csb setup -- Claude dir: ", None), (claude_dir, "bold"),
                  (f"   (via {_claude_dir_source(args)})", "dim")])
    if not Path(claude_dir).is_dir():
        print(f"Error: {claude_dir} does not exist. Is Claude Code installed? "
              f"(A relocated dir needs --claude-dir / CLAUDE_CONFIG_DIR.)",
              file=sys.stderr)
        return 1

    if getattr(args, "index_only", False):
        return _setup_index_only(args, config, claude_dir)

    repo_state, repo_detail = git_repo_state(claude_dir)

    # ── Repo present but git declines it / git broken: NEVER offer init.
    if repo_state in ("refused", "error"):
        return _setup_refusal(args, config, claude_dir, repo_state, repo_detail)

    # ── Already protected? (the probe walks up: an ancestor repo counts)
    if repo_state == "ok":
        r = run_git(claude_dir, "rev-parse", "--show-toplevel", check=False)
        root = (r.stdout or "").strip()
        try:
            own_root = root and Path(root).resolve() == Path(claude_dir).resolve()
        except OSError:
            own_root = False
        where = "here" if own_root else f"rooted at {root} (an ancestor repo -- that works)"
        _style_print([(f"Git repo: yes, {where}. Backups are enabled -- "
                       f"nothing to set up.", "green")])
        ensure_gitattributes(claude_dir)
        ensure_gitignore(claude_dir)
        _clear_index_only_ack(config, claude_dir)
        repo_note = (f"repo at {claude_dir}" if own_root
                     else f"via ancestor repo {root}")
        _setup_checklist(config, claude_dir, repo_note)
        return 0

    # ── No repo anywhere up the chain
    _style_print([("Git repo: NO -- sessions are NOT protected.", "bold red")])
    if not auto:
        if not _interactive():
            print("Non-interactive input and no mode flag. Use one of:",
                  file=sys.stderr)
            print("  csb setup --auto          # initialize the repo, no prompts",
                  file=sys.stderr)
            print("  csb setup --index-only    # explicitly stay unprotected",
                  file=sys.stderr)
            return 2
        answer = _ask(f"Initialize a git repository at {claude_dir}? [Y/n] ")
        if answer is None:
            print("Aborted -- nothing changed.")
            return 2
        if answer.lower() in ("n", "no"):
            print("Declined -- nothing changed. To make unprotected operation "
                  "official (and silence the banner): csb setup --index-only")
            return 2

    r = run_git(claude_dir, "init", check=False)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip() or "git not available?"
        _style_print([(f"Error: git init failed: {detail}", "bold red")], err=True)
        return 1
    _style_print([("[setup] ", "bold green"),
                  (f"Initialized git repository at {claude_dir}", "green")])
    # Re-probe before proceeding: `git init` succeeds even where git then
    # REFUSES the resulting repo (ownership/safety -- init is exempt from
    # the check). Without this gate, setup walks straight into a failing
    # first backup and contradicts itself one line after "Initialized"
    # (the 2026-07-22 incident, verbatim).
    post_state, post_detail = git_repo_state(claude_dir)
    if post_state != "ok":
        print()
        print("The repository was created, but git immediately refuses it -- "
              "this is an environment problem (usually ownership), not csb:")
        return _setup_refusal(args, config, claude_dir, post_state, post_detail)
    ensure_gitattributes(claude_dir)
    ensure_gitignore(claude_dir)
    _style_print([("[setup] ", "bold green"),
                  ("Wrote .gitattributes (session files marked binary -- "
                   "no CRLF corruption).", None)])
    _clear_index_only_ack(config, claude_dir)

    # ── First backup (the repo is empty until one runs)
    run_backup = auto
    if not auto:
        answer = _ask("Run the first backup now (csb backup)? [Y/n] ")
        run_backup = answer is not None and answer.lower() not in ("n", "no")
    if run_backup:
        import argparse as _argparse

        rc = cmd_backup(_argparse.Namespace(
            claude_dir=getattr(args, "claude_dir", None),
            db=getattr(args, "db", None),
            no_commit=False, quiet=quiet,
        ))
        if rc != 0:
            print("Error: the first backup failed -- see above. The repo is "
                  "initialized; run `csb backup` again once resolved.",
                  file=sys.stderr)
            return 1
        _style_print([("[setup] ", "bold green"),
                      ("First backup complete -- sessions are protected.", "green")])
    else:
        _style_print([("[setup] ", "bold green"),
                      ("Repo ready; run ", None), ("csb backup", "cyan"),
                      (" when you want the first snapshot.", None)])
    _setup_checklist(config, claude_dir, f"repo at {claude_dir}")
    return 0


# ── csb setup schedule (#69): OS-scheduled backup ───────────────────────


def _resolve_scheduled_python() -> str:
    """The absolute interpreter a scheduled entry runs (AC-3/D4).

    On Windows the ``pythonw.exe`` sibling of the current interpreter --
    a console interpreter flashes a conhost window per run and gets the
    feature uninstalled. Everywhere else, ``sys.executable`` itself.
    """
    exe = Path(sys.executable)
    if sys.platform == "win32":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


def _build_schedule_spec(config, interval_minutes: int,
                         now: Optional[datetime] = None) -> ScheduleSpec:
    """Freeze ALL runtime context into the spec at install time (D4:
    schedulers do not source shell profiles). ``--db`` is baked only when
    the resolved index path differs from the store default, so a default
    setup stays portable across csb upgrades."""
    now = now or datetime.now()
    cp = ClaudePaths.from_dir(config["claude_dir"])
    fire_hour, fire_minute = derive_fire_time(now, interval_minutes)
    db_path = None
    try:
        if Path(config["index_path"]).resolve() != cp.default_db.resolve():
            db_path = str(config["index_path"])
    except (OSError, KeyError):
        pass
    spec = ScheduleSpec(
        interval_minutes=interval_minutes,
        fire_hour=fire_hour,
        fire_minute=fire_minute,
        start_boundary=now.strftime("%Y-%m-%dT%H:%M:%S"),
        python_exe=_resolve_scheduled_python(),
        claude_dir=str(cp.root),
        log_file=str(cp.schedule_log),
        db_path=db_path,
    )
    # Install-time self-check (AC-3): the same tripwire the golden tests
    # run -- a bare `csb`, a relative interpreter, or missing baked flags
    # must die HERE, not silently at hour 24.
    validate_entry_argv(build_command_argv(spec))
    return spec


def _human_minutes(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} minutes"
    if minutes < 1440:
        return f"{minutes // 60} hours"
    return "24 hours" if minutes == 1440 else f"{minutes // 1440} days"


def _choose_interval(args) -> Optional[int]:
    """The guided interval question (Delta-10). Returns minutes, or None
    for abort/usage-error (caller exits 2; a message was printed).

    --interval wins everywhere. --auto (or a non-TTY WITH --auto) takes
    the 24h default. Non-interactive without either is refused with
    guidance -- never a silent default, never a hang.
    """
    raw = getattr(args, "interval", None)
    if raw is not None:
        try:
            validate_interval(raw)
        except ScheduleError as e:
            print(f"Error: {e}", file=sys.stderr)
            return None
        return raw
    if getattr(args, "auto", False):
        return DEFAULT_INTERVAL_MINUTES
    if not _interactive():
        print("Non-interactive input and no interval. Use one of:",
              file=sys.stderr)
        print("  csb setup schedule --auto            # 24h default, no prompts",
              file=sys.stderr)
        print("  csb setup schedule --interval 720    # explicit cadence (minutes)",
              file=sys.stderr)
        return None

    m15, m12h, m24h = INTERVAL_PRESETS
    print("How often should the scheduled backup run?")
    print(f"  [1] every {m15} minutes   (near-continuous protection)")
    print(f"  [2] every {m12h // 60} hours")
    print(f"  [3] every {m24h // 60} hours      (default -- one snapshot a day)")
    print("  [4] custom")
    while True:
        answer = _ask("Choose [1-4, Enter=3]: ")
        if answer is None:
            print("Aborted -- nothing installed.")
            return None
        if answer in ("", "3"):
            return m24h
        if answer == "1":
            return m15
        if answer == "2":
            return m12h
        if answer == "4":
            custom = _ask("Interval in minutes (divisor of 60, or whole "
                          "hours dividing 24): ")
            if custom is None:
                print("Aborted -- nothing installed.")
                return None
            try:
                minutes = int(custom)
                validate_interval(minutes)
            except (ValueError, ScheduleError) as e:
                print(f"  Not a valid interval: {e}")
                continue
            return minutes
        print("  Please answer 1, 2, 3, or 4.")


def _safe_backend_status(backend) -> BackendStatus:
    """status() without letting a missing binary / odd environment throw
    (CrontabBackend.status spawns `crontab`, which may not exist)."""
    try:
        return backend.status()
    except Exception as e:  # noqa: BLE001 -- degrade to honest unknown
        return BackendStatus(installed=False, readback_available=False,
                             detail=f"status unavailable: {e}")


def _schedule_interval_from_config(config) -> tuple[int, bool]:
    """(interval, recorded) -- the installed cadence from the config file,
    or the default with recorded=False when nothing was recorded (e.g. an
    entry installed against another store)."""
    try:
        val = config.get("schedule_interval_minutes")
        if val:
            return int(val), True
    except (TypeError, ValueError):
        pass
    return DEFAULT_INTERVAL_MINUTES, False


def _schedule_status_report(backend, config) -> int:
    """--status: all three evidence layers, one honest verdict (AC-14)."""
    cp = ClaudePaths.from_dir(config["claude_dir"])
    interval, recorded = _schedule_interval_from_config(config)
    st = _safe_backend_status(backend)
    verdict = evaluate_schedule_evidence(st, interval, cp.schedule_log)

    print(f"Scheduled backup ({backend.name})")
    print(f"  Entry installed: {'yes' if st.installed else 'no'}")
    if st.installed:
        # Field-caught: name WHAT to look for in the OS UI (taskschd.msc
        # hunt) -- the backend knows its entry's identity.
        entry_desc = getattr(backend, "describe_entry", lambda: "")()
        if entry_desc:
            print(f"  Entry:           {entry_desc}")
        note = "" if recorded else " (not recorded in config; assumed)"
        print(f"  Expected cadence: every {_human_minutes(interval)}{note}")
        if st.readback_available and (st.last_run or st.last_result is not None):
            print(f"  OS readback:     last run {st.last_run or '(none)'}"
                  + (f", result {st.last_result}" if st.last_result is not None
                     else ""))
        elif st.detail:
            print(f"  OS readback:     {st.detail}")
        print(f"  Run log:         {cp.schedule_log}")
    state_style = {"ok": "green", "pending": "yellow",
                   "installed-not-running": "bold red",
                   "not-installed": "yellow"}.get(verdict.state)
    _style_print([("  Verdict:         ", None), (verdict.detail, state_style)])
    return 0


def installed_plugin_version(claude_dir):
    """Newest installed csb-plugin version from the cache layout, or None.

    Filesystem-only by design (#75 U2): drift detection must stay fast
    and never depend on the claude CLI. Tolerates a missing tree
    (CLI-only users are first-class -> None, never a warning),
    unparseable directory names (skipped), and multiple cached versions
    (numeric max wins).
    """
    cache = ClaudePaths.from_dir(claude_dir).plugin_cache
    best = None
    best_key = None
    try:
        entries = list(cache.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            key = tuple(int(part) for part in entry.name.split("."))
        except ValueError:
            continue
        if best_key is None or key > best_key:
            best_key, best = key, entry.name
    return best


def cmd_setup_update(args) -> int:
    """`csb setup update` -- refresh the installed plugin (#75).

    The plugin delivers the hook scripts, and this week proved staleness
    is not cosmetic (pid capture shipped twice via plugin updates the
    user had to incant by hand). Wrapper semantics (U4): honest
    degradations with the exact manual commands, the claude CLI's own
    output as the progress UI, and the one fact users doubt afterwards
    stated plainly (no restarts needed).
    """
    from ._version import get_base_version

    config = _get_config(args)
    claude_dir = config["claude_dir"]
    spec = ClaudePaths.PLUGIN_SPEC

    installed = installed_plugin_version(claude_dir)
    if installed is None:
        print("The csb plugin is not installed -- nothing to update.")
        print("  Install it (registers the backup + registry hooks):",
              file=sys.stderr)
        print('    claude plugin marketplace add '
              '"DazzleML/Claude-Session-Backup"', file=sys.stderr)
        print(f"    claude plugin install {spec}", file=sys.stderr)
        return 3

    claude_cli = shutil.which("claude")
    if claude_cli is None:
        print("The `claude` CLI is not on PATH -- run the update from a "
              "shell where it is:", file=sys.stderr)
        print(f"  claude plugin update {spec}", file=sys.stderr)
        return 3

    print(f"Installed plugin: {installed}  (csb is {get_base_version()})")
    try:
        result = subprocess.run([claude_cli, "plugin", "update", spec],
                                timeout=120, check=False)
    except subprocess.TimeoutExpired:
        print("`claude plugin update` timed out after 120s.",
              file=sys.stderr)
        return 1
    after = installed_plugin_version(claude_dir)
    if result.returncode == 0:
        if after != installed:
            print(f"Updated: {installed} -> {after}")
        else:
            print(f"Already current: {after}")
        print("  No session restarts needed -- hooks resolve the "
              "installed plugin at fire time.", file=sys.stderr)
        return 0
    print(f"`claude plugin update` exited {result.returncode} -- its "
          "output above has the detail.", file=sys.stderr)
    return result.returncode or 1


def cmd_setup_schedule(args) -> int:
    """`csb setup schedule` (#69): the backup layer for machines you are
    NOT using. Hooks fire on Claude Code activity; Claude Code's startup
    cleanup purges old transcripts either way -- an untouched machine
    needs the OS scheduler to run `csb backup` on its own.

    Exit codes: 0 done (install/remove/status/dry-run/print), 1 error,
    2 usage/declined, 11 refused (no usable scheduler -- SCHEDULE_REFUSED_EXIT).
    """
    config = _get_config(args)
    claude_dir = config["claude_dir"]
    backend = choose_backend()

    if getattr(args, "status_only", False):
        return _schedule_status_report(backend, config)

    if getattr(args, "print_systemd", False):
        interval = getattr(args, "interval", None) or DEFAULT_INTERVAL_MINUTES
        try:
            validate_interval(interval)
            spec = _build_schedule_spec(config, interval)
        except ScheduleError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
        print(render_systemd_recipe(spec))
        return 0

    if getattr(args, "remove", False):
        try:
            result = backend.remove(claude_dir)
        except Exception as e:  # noqa: BLE001 -- surface, don't traceback
            print(f"Error: schedule removal failed: {e}", file=sys.stderr)
            return 1
        if not result.ok:
            print(f"Error: schedule removal failed: {result.detail}",
                  file=sys.stderr)
            return 1
        if result.was_installed:
            _style_print([("[schedule] ", "bold green"),
                          (f"Removed ({result.detail}).", "green")])
            if result.backup_path:
                _style_print([("  Pre-removal snapshot: ", "dim"),
                              (result.backup_path, "dim")])
        else:
            print(f"Nothing to remove -- {result.detail}.")
        _write_config_file_keys(claude_dir, schedule_interval_minutes=None)
        return 0

    # ── Install path (and --dry-run, which stops at the rendering) ──
    try:
        detect = backend.detect()
    except Exception as e:  # noqa: BLE001
        detect = None
        detect_err = str(e)
    if detect is None or not detect.available:
        reason = detect.reason if detect is not None else detect_err
        if backend.name == "crontab":
            # AC-7: the full refusal -- what was checked, how to fix it,
            # and the systemd escape hatch. Verbatim, golden-tested text.
            print(REFUSAL_TEXT, file=sys.stderr)
        else:
            print(f"csb: cannot install a scheduled backup on this machine: "
                  f"{reason}.\nNothing was installed or modified.",
                  file=sys.stderr)
        return SCHEDULE_REFUSED_EXIT

    if detect.is_wsl:
        _style_print([("Note (WSL): ", "bold yellow"),
                      ("cron only runs while this WSL distro is running -- "
                       "a closed WSL means no scheduled backups. For "
                       "always-on protection of a Windows-side Claude dir, "
                       "run `csb setup schedule` from Windows instead.",
                       "yellow")])

    interval = _choose_interval(args)
    if interval is None:
        return 2

    try:
        spec = _build_schedule_spec(config, interval)
    except ScheduleError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    when = (f"every {_human_minutes(interval)}"
            + (f" at {spec.fire_hour:02d}:{spec.fire_minute:02d}"
               if interval >= 1440 else ""))

    if getattr(args, "dry_run", False):
        print(f"Would install via {backend.name}: {when}")
        print(f"  Command: {' '.join(build_command_argv(spec))}")
        print(f"  Run log: {spec.log_file}")
        print()
        print(backend.render(spec))
        print("Nothing was installed (--dry-run).")
        return 0

    try:
        result = backend.install(spec)
    except Exception as e:  # noqa: BLE001
        print(f"Error: schedule install failed: {e}", file=sys.stderr)
        return 1
    if not result.ok:
        print(f"Error: schedule install failed: {result.detail}",
              file=sys.stderr)
        return 1

    _style_print([("[schedule] ", "bold green"),
                  (f"Installed via {backend.name}: {when} ({result.detail}).",
                   "green")])
    if result.backup_path:
        _style_print([("  Pre-install snapshot: ", "dim"),
                      (result.backup_path, "dim")])
    _write_config_file_keys(claude_dir,
                            schedule_interval_minutes=interval,
                            schedule_installed_at=_now_iso())
    # The run log (and the index the runs touch) must never dirty the
    # user's own commits -- same guard the backup path applies (AC-21).
    ensure_gitignore(claude_dir)

    if result.deferred:
        # AC-6 honesty (launchd over SSH): the plist is written but not
        # loaded; a prime kickstart would fail against nothing.
        _style_print([("  Note: ", "bold yellow"),
                      ("the agent loads at next login; skipping the prime "
                       "run until then.", "yellow")])
        print(f"  Verify later with: csb setup schedule --status")
        return 0

    # Delta-14: prime run through the scheduler's own launcher, so a
    # broken entry fails NOW with the install output on screen -- not
    # silently at the first real fire.
    print("Dispatching prime run (proves the entry in the scheduler's own "
          "environment; a first backup can take a minute)...")
    try:
        ok, detail = backend.prime(spec)
    except Exception as e:  # noqa: BLE001
        ok, detail = False, str(e)
    if ok:
        _style_print([("  Prime: ", None), (detail, "green")])
    else:
        _style_print([("  WARNING: ", "bold red"),
                      (f"prime run failed: {detail}", "red")])
        print("  The schedule IS installed, but its first run could not be "
              "proven. Investigate now rather than at the next fire:")
    print(f"  Verify anytime with: csb setup schedule --status")
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


def _empty_state_guidance(config, path_mode: bool = False) -> list[str]:
    """Diagnostic lines for an empty result set (#52). Two exclusive cases:

    1. Index never built + transcripts exist on disk -> say so and name the
       command that fixes it (including the git-free `--no-commit` form --
       the post-crash recovery box is often the one without a repo).
    2. Index is fine, but a path-scoped scan matched nothing while sessions
       exist elsewhere -> redirect. Covers the "I scanned my checkout, but
       the session ran from another cwd" mental-model gap.

    Returns [] when neither diagnosis applies (or the DB is unreadable) --
    callers print nothing extra and the classic empty state stands alone.
    """
    try:
        conn = open_db(config["index_path"])
        init_schema(conn)
        unbuilt = index_is_unbuilt(conn)
        conn.close()
    except Exception:
        return []

    n = count_session_jsonls(config["claude_dir"])
    if n <= 0:
        return []
    noun = "session file" if n == 1 else "session files"
    if unbuilt:
        return [
            f"  Note: the csb index is empty -- {n} {noun} exist on disk "
            f"but nothing has been indexed yet.",
            "  Run `csb backup` to index + back up, or "
            "`csb backup --no-commit` to index without git.",
        ]
    if path_mode:
        noun = "session exists" if n == 1 else "sessions exist"
        return [
            f"  Tip: {n} {noun} under other folders -- "
            f"try `csb list -n 5` or `csb scan -d <parent-folder>`.",
        ]
    return []


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

    # Empty-index diagnosis (#52): "No sessions found." must never be a
    # dead end when transcripts exist on disk and the index was simply
    # never built (e.g. every backup failed silently on a repo-less box).
    if not sessions and not args.json:
        for line in _empty_state_guidance(config):
            print(line)

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


def _looks_like_path(value: str) -> bool:
    """True when a bare positional should be read as a folder, not a filter.

    Conservative on purpose (`csb tree FILTER PATH`): only an unambiguous
    path shape counts -- an explicit ``./`` / ``.\\`` prefix, a bare ``.``,
    an absolute path, or anything containing a separator. A plain word is
    NEVER guessed to be a path; the caller errors and names ``-d`` instead,
    so a mistyped filter can't silently become a folder scope that matches
    nothing.
    """
    if not value:
        return False
    if value in (".", "./", ".\\", "..") or value.startswith(("./", ".\\", "../", "..\\")):
        return True
    if "/" in value or "\\" in value:
        return True
    return bool(Path(value).is_absolute())


def _tree_scope_ids(conn, args) -> tuple[set[str] | None, str | None, int]:
    """Resolve ``csb tree``'s folder scope to a set of session ids.

    Reuses ``csb scan``'s pattern resolution and SQL matching, so PATH
    SYNTAX is identical across the two commands: trailing-``*`` wildcards,
    LIKE escaping, and descendants (``-d``) vs folder-only (``-D``).

    **ELIGIBILITY is now identical too.** Both pass ``top_n=None``. Tree
    always did: its unit is a FAMILY and its question is "did any member
    ever work here", not "is this folder prominent for this session", and
    wiring the display cap into scope would let ``--top 1`` silently
    change WHICH families appear.

    ``csb scan`` used to pass its display cap (``--top``, default 3) as
    ``top_n``, where it doubles as a match gate -- so a session whose
    4th-most-used folder was the target did not match. That was read as a
    deliberate divergence (this docstring said so) rather than as a bug,
    which is how it survived. It was invisible while sessions recorded
    ~1 folder; once #56 began harvesting every touched folder it hid
    roughly two thirds of real matches. How many folders we PRINT and
    whether a session MATCHES are different questions, and they are no
    longer answered by the same number.

    Returns ``(scope_ids, resolved_path, exit_code)``. ``scope_ids`` is None
    when no scope was requested.
    """
    from .index import find_sessions_by_directory

    below = getattr(args, "directories_below", None)
    only = getattr(args, "directory_only", None)
    positional = getattr(args, "path", None)

    pattern = below or only
    include_descendants = only is None

    # `csb tree .` / `csb tree ./sub` -- a path-shaped FIRST positional is
    # the path, not a filter. Same promotion `csb scan` does, so the muscle
    # memory carries over and no flag (or argument-order flag) is needed.
    # Only promote when the slot it would move into is free.
    if (positional is None and pattern is None
            and args.filter and _looks_like_path(args.filter)):
        positional = args.filter
        args.filter = None

    if positional is not None:
        if pattern is not None:
            print("Error: PATH given both positionally and via -d/-D. Use one.",
                  file=sys.stderr)
            return None, None, 2
        if not _looks_like_path(positional):
            print(
                f"Error: second positional {positional!r} does not look like a "
                f"path (expected an absolute path, one containing a separator, "
                f"or a ./ prefix). Use -d {positional!r} to force it as a "
                f"folder scope, or pass it as the FILTER instead.",
                file=sys.stderr,
            )
            return None, None, 2
        pattern = positional

    if pattern is None:
        return None, None, 0

    resolved, exact_value, like_match, like_exclude = _resolve_directory_pattern(
        pattern, include_descendants,
    )
    rows = find_sessions_by_directory(
        conn, exact_value, like_match, like_exclude, None,
        limit=100_000, deleted_filter="all",
    )
    return {r["session_id"] for r in rows}, resolved, 0


def cmd_tree(args) -> int:
    """Render the fork-lineage forest (#31).

    Builds the parent/child forest from ``sessions.parent_session_id`` and
    prints it as an indented tree. Selection composes ``csb list``'s keyword
    filter with ``csb scan``'s directory scope; per-node detail mirrors
    ``csb list -f/-ff``.
    """
    import json as _json
    import re as _re

    from .lineage import DEFAULT_MAX_NODES_PER_ROOT, build_forest
    from .lineage_render import forest_summary, print_forest, to_json

    # Session names can carry characters the console cannot encode. Relax
    # the error handler so those degrade to '?' instead of raising -- but do
    # NOT force the encoding to UTF-8: writing UTF-8 bytes to a cp437 /
    # cp1252 console turns the box-drawing connectors into mojibake
    # (Γö£ΓöÇ...). Keeping the console's own encoding lets the renderer's
    # probe pick the charset that will actually display correctly.
    console_encoding = getattr(sys.stdout, "encoding", None)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn, quiet=getattr(args, "quiet", False))

    scope_ids, resolved_path, rc = _tree_scope_ids(conn, args)
    if rc:
        conn.close()
        return rc

    root_id = None
    raw_root = getattr(args, "root", None)
    if raw_root:
        root_session, rc = _resolve_session_flexible(
            conn, raw_root, config["claude_dir"],
        )
        if root_session is None:
            conn.close()
            return rc
        root_id = root_session["session_id"]

    max_nodes = getattr(args, "max_nodes", None)
    if max_nodes is None:
        max_nodes = DEFAULT_MAX_NODES_PER_ROOT

    try:
        forest = build_forest(
            conn,
            filter_term=args.filter,
            regex=getattr(args, "regex", False),
            case_sensitive=getattr(args, "case_sensitive", False),
            scope_ids=scope_ids,
            deleted_filter=_tree_deleted_filter(args),
            root=root_id,
            orphans_only=getattr(args, "orphans", False),
            component_view=not getattr(args, "lineage", False),
            max_nodes_per_root=max_nodes,
        )
    except _re.error as e:
        print(f"Error: invalid regex {args.filter!r}: {e}", file=sys.stderr)
        conn.close()
        return 2

    # Roots order follows --sort; children always read in fork order.
    _sort_roots(forest, getattr(args, "sort", "last-used"))

    # Most sessions never fork, so an unfiltered forest is mostly one-node
    # "trees" that bury the real chains. Collapse those into a count (#31's
    # "(orphan root, no children: N sessions)") unless the user asked for
    # them with --orphans -- or unless the session MATCHED the filter, in
    # which case hiding it would silently answer the wrong question.
    solo_count = 0
    if not getattr(args, "orphans", False):
        solo_count = sum(
            1 for r in forest.roots if not r.children and not r.matched
        )
        forest.roots = [r for r in forest.roots if r.children or r.matched]
        forest.total_nodes = sum(1 for _ in forest.walk())

    limit = getattr(args, "n", None)
    if limit is not None and limit >= 0:
        forest.roots = forest.roots[:limit]
        forest.total_nodes = sum(1 for _ in forest.walk())

    cleanup_days = read_cleanup_period(config["claude_dir"])
    # Diagnostic signal for the empty states: an index that has NEVER
    # recorded a fork pointer is almost certainly one that predates v0.7.0
    # (lineage is extracted during indexing), not a vault without forks.
    any_lineage = conn.execute(
        "SELECT 1 FROM sessions WHERE parent_session_id IS NOT NULL LIMIT 1"
    ).fetchone() is not None
    conn.close()

    if getattr(args, "json", False):
        print(_json.dumps(to_json(forest), indent=2, default=str))
        return 0

    if not forest.roots:
        if solo_count:
            # Careful with the wording: these sessions matched but ended up
            # with no RENDERED relatives. That is not the same as "never
            # forked" -- under a narrowed scope (e.g. --deleted only) a real
            # fork partner can simply sit outside the population.
            scoped = _tree_deleted_filter(args) != "active" or bool(
                args.filter or resolved_path or getattr(args, "root", None))
            if scoped:
                print(f"No fork relationships within this scope -- "
                      f"{solo_count} session(s) matched, but any sessions they "
                      f"forked from or into fall outside it.")
            else:
                print(f"No fork lineage found -- {solo_count} session(s) "
                      f"matched but none of them forked.")
            if not any_lineage:
                _print_reindex_hint()
            elif scoped:
                print("  Widen the scope (e.g. --deleted all) to see partners "
                      "outside it, or --orphans to list the matches.",
                      file=sys.stderr)
            else:
                print("  Use --orphans to list them.", file=sys.stderr)
            return 0
        _tree_empty_message(args, resolved_path, any_lineage)
        return 0

    level = min(getattr(args, "full_info", 0) or 0, 2)
    top_folders = None if getattr(args, "all_folders", False) else (
        getattr(args, "top", None) or 3
    )
    print_forest(
        forest,
        level=level,
        shortid=getattr(args, "shortid", False),
        show_uuid=getattr(args, "uuid", False),
        cleanup_days=cleanup_days,
        top_folders=top_folders,
        charset="ascii" if getattr(args, "ascii", False) else None,
        encoding=console_encoding,
    )
    print()
    summary = forest_summary(forest)
    if solo_count:
        summary += (f" | {solo_count} never forked (--orphans to list)")
    print(summary)
    if forest.cycles_broken:
        print(
            f"Warning: broke {len(forest.cycles_broken)} parent-pointer cycle(s) "
            f"({', '.join(c[:8] for c in forest.cycles_broken)}) -- the index may "
            f"be corrupt; `csb update rebuild-index` re-derives lineage from "
            f"the transcripts.",
            file=sys.stderr,
        )
    if forest.phantom_parents:
        print(
            f"Note: {len(forest.phantom_parents)} parent session(s) are not in "
            f"the index (shown dimmed as '(unknown session)'). "
            f"`csb update backfill-deleted` may recover them from git history.",
            file=sys.stderr,
        )
    return 0


def _tree_deleted_filter(args) -> str:
    """Map the shared ``--deleted`` grammar onto the lineage vocabulary."""
    mode = deleted_mode(args)
    if mode == "only":
        return "deleted"
    if mode == "all":
        return "all"
    return "active"


#: Root-ordering vocabulary -- deliberately the same keys ``index.SORT_SQL``
#: accepts, so ``csb tree --sort`` and ``csb list --sort`` can never drift.
#: ``(field, descending)``; a missing value always sorts last, mirroring the
#: ``NULLS LAST`` in the SQL the other verbs use.
_TREE_SORT_FIELDS = {
    "last-used":  ("last_active_at", True),
    "expiration": ("jsonl_mtime", False),   # soonest purge first
    "started":    ("started_at", True),
    "oldest":     ("started_at", False),
    "messages":   ("message_count", True),
    "size":       ("jsonl_size", True),
}


def _sort_roots(forest, sort_key: str) -> None:
    """Order roots by the same vocabulary ``csb list --sort`` uses.

    Sorted in Python rather than SQL because the roots are already
    materialized objects. Rows missing the sort field are partitioned out
    and appended, which reproduces SQL's ``NULLS LAST`` without needing a
    sentinel value that would differ per column type.
    """
    field, descending = _TREE_SORT_FIELDS.get(
        sort_key, _TREE_SORT_FIELDS["last-used"],
    )

    def value(node):
        raw = node.session.get(field)
        return None if raw in (None, "", 0) else raw

    have = [n for n in forest.roots if value(n) is not None]
    missing = [n for n in forest.roots if value(n) is None]
    have.sort(key=value, reverse=descending)
    forest.roots = have + missing


def _print_reindex_hint() -> None:
    """Explain the one-time upgrade step for a pre-v0.7.0 index.

    Fork lineage is read from each transcript while indexing, so sessions
    indexed before v0.7.0 carry no pointer until they are re-scanned. An
    index with ZERO recorded pointers is far more likely to be in that
    state than to be a vault where nobody ever forked -- so say the useful
    thing instead of implying there is nothing to find.
    """
    print(
        "  Note: fork lineage is read from transcripts during indexing, and "
        "this index has none recorded yet -- sessions indexed before v0.7.0 "
        "need one pass to pick it up. Run `csb backup` (or "
        "`csb update rebuild-index`) and try again.",
        file=sys.stderr,
    )


def _tree_empty_message(args, resolved_path, any_lineage: bool = True) -> None:
    """Explain an empty forest the way list/scan explain empty results."""
    bits = []
    if args.filter:
        bits.append(f"filter {args.filter!r}")
    if resolved_path:
        bits.append(f"path {resolved_path}")
    if getattr(args, "root", None):
        bits.append(f"root {args.root!r}")
    if getattr(args, "orphans", False):
        bits.append("--orphans")
    scope = f" for {' + '.join(bits)}" if bits else ""
    print(f"No fork lineage found{scope}.")
    if not any_lineage:
        _print_reindex_hint()


def cmd_status(args) -> int:
    """Summary of sessions, deletions, git state."""
    config = _get_config(args)
    claude_dir = config["claude_dir"]

    conn = open_db(config["index_path"])
    init_schema(conn)
    stats = get_stats(conn)

    repo_state, repo_detail = git_repo_state(claude_dir)
    is_repo = repo_state == "ok"
    # Per-session "un-backed-up" detection (transcripts newer than the index).
    # Only meaningful inside a git repo (where backups commit); skip otherwise.
    unbacked = find_unbacked_sessions(conn, claude_dir) if is_repo else []
    conn.close()

    # One label column for every line (user report: three different
    # historical paddings read as raggedness). The 16-wide field
    # reproduces the pinned "Un-backed-up:   " spacing exactly.
    def _sline(label, value, style=None):
        _style_print([(f"  {label + ':':<16}", None), (str(value), style)])

    print(f"Claude Session Backup Status")
    _sline("Claude dir", claude_dir, "dim")
    if repo_state == "refused":
        _sline("Git repo", "REFUSED (a repo exists; git declines it "
               "in this shell)", "red")
        first = (repo_detail or "").splitlines()[:1]
        if first:
            print(f"    git said: {first[0]}")
        print(f"    (run `csb setup` for diagnosis and fixes -- do NOT "
              f"re-initialize)")
    elif repo_state == "error":
        _sline("Git repo", f"GIT ERROR ({repo_detail})", "red")
    else:
        _sline("Git repo", "yes" if is_repo else "NO",
               "green" if is_repo else "red")
    if repo_state == "absent":
        print(f"    (backups disabled -- run `csb setup` to configure, or "
              f"`git -C {claude_dir} init`;")
        print(f"     `csb backup --no-commit` builds the search index without git)")
    _sline("Total sessions", stats['total_sessions'])
    _sline("Active", stats['active_sessions'])
    _sline("Deleted", stats['deleted_sessions'], "dim")
    _sline("Projects", stats['projects'])

    if stats["last_scan"]:
        scan = stats["last_scan"]
        # Local wall time first (the human answer to "when?"), the
        # stored UTC beside it for searchability -- both, per user.
        scanned_dt = parse_index_ts(scan["scanned_at"])
        if scanned_dt is not None:
            from .set_render import format_local
            when = (f"{format_local(scanned_dt)}  "
                    f"({_iso_z(scanned_dt)})")
        else:
            when = scan["scanned_at"]
        _sline("Last scan", when)
        print(f"    Found: {scan['sessions_found']}, New: {scan['sessions_new']}, "
              f"Deleted: {scan['sessions_deleted']}")
        if scan.get("git_commit"):
            print(f"    Commit: {scan['git_commit'][:8]}")

    # Git status
    if is_repo:
        status = git_status(claude_dir)
        changed = len([l for l in status.split("\n") if l.strip()])
        if changed:
            _sline("Uncommitted", f"{changed} files", "yellow")
        else:
            _sline("Working tree", "clean", "green")

        # Per-session backup freshness. Counts the live session honestly (its
        # transcript is mid-write) -> goes to 0 once all sessions close.
        if not unbacked:
            _sline("Un-backed-up", "none", "green")
        else:
            n = len(unbacked)
            _sline("Un-backed-up",
                   f"{n} session{'s' if n != 1 else ''} (changed since "
                   "last index -- run `csb backup`)", "yellow")
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
                _style_print([
                    (f"    {sf.session_id[:8]}  ", "dim"),
                    (label, None), (f"({why})", "dim"),
                ])
            if n > limit:
                print(f"    + {n - limit} more not shown")

    # Plugin drift (#75 U3): report-surface only, never an ambient nag.
    # The plugin delivers the hook scripts, so staleness decides whether
    # liveness features actually arrive.
    try:
        from ._version import get_base_version

        installed = installed_plugin_version(claude_dir)
        base = get_base_version()
        if installed is None:
            _sline("Plugin", "not installed (optional -- hook automation "
                   "off)", "dim")
        elif installed == base:
            _sline("Plugin", f"{installed} (current)", "green")
        else:
            _sline("Plugin", f"{installed} installed -- csb is {base}; "
                   "run `csb setup update`", "yellow")
    except Exception:  # noqa: BLE001 -- ambient line is best-effort
        pass

    # Scheduled backup (#69 AC-15): one ambient line, the three-layer
    # verdict behind it. A probe failure never breaks `csb status`.
    try:
        interval, _recorded = _schedule_interval_from_config(config)
        verdict = evaluate_schedule_evidence(
            _safe_backend_status(choose_backend()), interval,
            ClaudePaths.from_dir(claude_dir).schedule_log)
        _sline("Scheduled", verdict.detail)
    except Exception:  # noqa: BLE001 -- ambient line is best-effort
        pass

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
      0                -- clean: every session is backed up (or indexed,
                          in repo-less index-only mode)
      CHECK_GAP_EXIT   -- one or more sessions have un-backed-up changes
      1                -- error

    Repo-less (#52): gap detection is DB-vs-disk only, so it runs fine
    without a git repo -- and MUST, because the silent-failure scenario
    (crashed box, no repo, hooks erroring into log files) is exactly when
    the SessionStart warning is most needed. The summary adapts: it
    recommends the git-free `csb backup --no-commit` and names the
    missing-repo condition instead of pretending a backup is possible.
    """
    config = _get_config(args)
    claude_dir = config["claude_dir"]
    quiet = getattr(args, "quiet", False)

    # Live Session Registry boundary sweep (#64): _check runs on every
    # SessionStart, i.e. reliably soon after any boot -- the right moment
    # to freeze pre-boot entries into the open-at-shutdown snapshot and
    # clear them. Silent and never fatal: the sweep is bookkeeping, the
    # gap check below is this command's actual job.
    from . import live_registry as _lr
    swept = _lr.sweep_boundary(claude_dir)
    if swept and not quiet:
        print(f"[live-registry] froze {swept} open-at-shutdown "
              f"entr{'y' if swept == 1 else 'ies'} into the boundary "
              "snapshot.", file=sys.stderr)
    exclude = getattr(args, "exclude", None)

    repo_state, _repo_detail = git_repo_state(claude_dir)
    repoless = repo_state != "ok"

    conn = open_db(config["index_path"])
    init_schema(conn)
    stale = find_unbacked_sessions(conn, claude_dir, exclude)
    conn.close()

    if not stale:
        if not quiet:
            if repo_state == "refused":
                print("All sessions indexed (git refuses the existing repo "
                      "in this context -- run `csb setup` for diagnosis).")
            elif repoless:
                print("All sessions indexed (index-only: no git repo, "
                      "no backup protection).")
            else:
                print("All sessions backed up.")
        return 0

    # Concise, user-facing summary (the hook puts this in a systemMessage).
    n = len(stale)
    if repo_state == "refused":
        print(f"csb: {n} session(s) with un-indexed changes -- a git repo "
              f"EXISTS at {claude_dir} but git REFUSES it in this context "
              f"(backups failing here; do NOT re-initialize). "
              f"Run `csb setup` for diagnosis and fixes:")
    elif repoless:
        print(f"csb: {n} session(s) with un-indexed changes, and "
              f"{claude_dir} is not a git repository (backups are failing). "
              f"Run `csb backup --no-commit` to index now; "
              f"`csb setup` enables real backups:")
    else:
        print(f"csb: {n} session(s) with un-backed-up changes "
              f"(likely an unclean shutdown -- run `csb backup` to capture now):")
    for sf, recorded in stale[:5]:
        why = "never indexed" if recorded is None else "changed since last backup"
        print(f"  {sf.session_id[:8]}  ({why})")
    if n > 5:
        print(f"  ... and {n - 5} more")
    return CHECK_GAP_EXIT


def _resolve_session_flexible(conn, query: str, claude_dir):
    """Resolve a session by UUID/prefix OR name / path / keyword.

    The two-step idiom established for `csb resume` by #42: try the
    historical hex resolver first (prefix / suffix matching with proper
    ambiguity reporting), and on a non-ID-shaped input fall through to the
    multi-modal :func:`_resolve_session_query`.

    Factored out so every command that takes "a session" accepts the same
    vocabulary. That matters more since v0.7.0: `csb tree` displays session
    NAMES by default, so the obvious next command (`csb show <that name>`)
    has to accept what the user just read off the screen.

    Returns ``(session_dict, 0)`` on success, or ``(None, exit_code)``
    after printing the appropriate error / candidate list.
    """
    full_id, exit_code = _resolve_session_or_exit(conn, query, miss_ok=True)
    if full_id is None and exit_code:
        return None, exit_code
    session = get_session(conn, full_id) if full_id else None
    if session is not None:
        return session, 0

    result, method = _resolve_session_query(query, conn, claude_dir)
    if result is None:
        print(f"Error: {method}", file=sys.stderr)
        return None, 1
    if isinstance(result, list):
        label = method.split(":", 1)[1] if ":" in method else method
        _show_view_candidates(result, query, label)
        return None, 1
    return result, 0


def _tier_folders(folders, min_work_escape: int = 10,
                  top_rank_escape: int = 10):
    """Split folder rows into display tiers (#56).

    The flat list conflated three genuinely different things and printed a
    meaningless ``(0x)`` for two of them:

      - **worked**    -- work units > 0. Where the session actually did
                         things, ranked. This is the answer to "where did I
                         spend my time".
      - **touched**   -- recorded because a call referenced the folder but
                         it was never that call's PRIMARY. Real and
                         findable by ``csb scan``; a count would be a lie,
                         so none is shown.
      - **low value** -- scratch space (temp dirs, caches) or a path that
                         does not resolve. Still stored and still
                         scannable -- collapsed here only to keep the
                         default view readable.

    Classification happens at DISPLAY time, never at index time, so
    retuning the heuristics costs nothing and never makes data
    unreachable.
    """
    from . import toolpaths as _tp

    worked, touched_only, low = [], [], []
    scratch_candidates = []
    for f in folders:
        path = f["folder_path"]
        # A resolvable-but-missing path is only "low value" when nothing
        # was worked there; a folder with real work that has since been
        # deleted is history worth showing.
        missing = _row_get(f, "path_exists") == 0
        if _tp.looks_like_scratch(path):
            if f["usage_count"]:
                scratch_candidates.append(f)
            else:
                low.append(f)
        elif missing and not f["usage_count"]:
            low.append(f)
        elif f["usage_count"]:
            worked.append(f)
        else:
            touched_only.append(f)

    # PROMINENT scratch escapes the collapse (F1, user-specified rule:
    # usage_count > 10 OR rank <= 10 over the session's worked rows).
    # The unconditional scratch->low rule misrepresented sessions whose
    # scratchpad IS the workplace -- measured, one real session's #1
    # folder (207x) was hidden behind "(N hidden)" -- and for purge-prone
    # Claude Code scratchpads, surfacing high-work scratch is a recovery
    # affordance: those dirs hold the session's probe scripts and tools,
    # and are reused across sessions as tool sheds. Promoted rows carry a
    # `(scratch)` tag so the display stays honest about what they are.
    if scratch_candidates:
        ranked = sorted(worked + scratch_candidates,
                        key=lambda f: -f["usage_count"])
        rank_of = {id(f): i + 1 for i, f in enumerate(ranked)}
        for f in scratch_candidates:
            if (f["usage_count"] > min_work_escape
                    or rank_of[id(f)] <= top_rank_escape):
                try:
                    f["_scratch_promoted"] = True
                except TypeError:  # sqlite3.Row -- tolerate, tag is cosmetic
                    pass
                worked.append(f)
            else:
                low.append(f)
        worked.sort(key=lambda f: -f["usage_count"])

    # Counts matter inside the collapse too ("at the very least we should
    # be counting"): order the hidden rows by work, so `--all` reads
    # most-material-first.
    low.sort(key=lambda f: (-f["usage_count"], f["folder_path"]))
    return worked, touched_only, low


def _aggregate_filters(pairs) -> dict:
    """Combine repeated ``--filter KEY=VALUE`` pairs: the STRICTEST wins.

    ``dict()`` on the pair list kept the LAST occurrence, so ``--filter
    min-work=50 --filter min-work=5`` silently discarded the stricter
    constraint -- a quietly-dropped filter, the failure class the loud
    unknown-key errors exist to prevent. Repeated filters AND together;
    for a count threshold, AND means ``max``.
    """
    out: dict = {}
    for key, value in pairs or []:
        out[key] = max(value, out[key]) if key in out else value
    return out


def _apply_work_filter(worked, min_work: int):
    """Split work rows by a user-chosen count threshold (`show --filter N`).

    Default 0 shows everything. The long tail of one-off folders is real
    data -- a single `Read` in another repo IS a touch -- so whether it is
    noise is a per-user judgement, not ours to make by default. Hidden
    rows stay in the index and stay findable by `csb scan`; only this view
    collapses them, and it always reports how many.

    Orthogonal to the low-value tier, which hides by KIND (scratch,
    unresolved) rather than by count and is revealed with `--all`.
    """
    if min_work <= 1:
        return worked, []
    keep = [f for f in worked if f["usage_count"] >= min_work]
    thin = [f for f in worked if f["usage_count"] < min_work]
    return keep, thin


def _thin_note(thin, min_work: int) -> str:
    """Always report the count AND the total of what was hidden.

    A filter that hides without saying so turns "I chose not to look" into
    "there was nothing there".
    """
    n = len(thin)
    total = sum(f["usage_count"] for f in thin)
    word = "folder" if n == 1 else "folders"
    # The hint must be executable: `--filter` takes KEY=VALUE, so a bare
    # `--filter 0` is now a parse error. Same trap as the old
    # `--all-folders` hint, which named a flag `show` does not accept.
    return (f"    ({n} more {word} below {min_work} work units, "
            f"{total} units total -- omit --filter to show)")


def _row_get(row, key, default=None):
    """sqlite3.Row has no .get(); tolerate pre-migration rows lacking a column."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _low_reason(f) -> str:
    from . import toolpaths as _tp
    if _tp.looks_like_scratch(f["folder_path"]):
        return "(scratch)"
    if _row_get(f, "path_exists") == 0:
        return "(not found)"
    return ""


def _print_low_value_note(low, show_all: bool) -> None:
    n = len(low)
    word = "path" if n == 1 else "paths"
    # Never hide the aggregate (F1, mirroring --filter's rule): a big
    # hidden work total should be visible AS a number before anyone runs
    # --all.
    total = sum(f["usage_count"] for f in low)
    units = f", {total} work units" if total else ""
    if show_all:
        print(f"\n  Low value ({n} {word}{units} -- scratch or unresolved):")
    else:
        # `--all`, NOT `--all-folders`: this hint is printed by `csb show`,
        # which has no --all-folders flag (that one lives on list/scan/tree
        # and means "don't truncate to top-N"). Naming it here sent users
        # straight into `unrecognized arguments: --all-folders`.
        print(f"\n  ({n} low-value {word} hidden{units} -- scratch dirs and "
              f"unresolved paths; --all to show)")


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

    # Accepts a UUID/prefix OR a session name / path / keyword -- the same
    # vocabulary `csb resume` takes, and the vocabulary `csb tree` prints.
    session, exit_code = _resolve_session_flexible(
        conn, args.session_id, config["claude_dir"],
    )
    if session is None:
        conn.close()
        return exit_code
    full_id = session["session_id"]

    if session:
        # Fork lineage (v0.7.0, #22 Phase 1) -- resolved before the
        # connection closes so the renderer stays DB-free.
        session["_lineage"] = _fetch_lineage(conn, full_id, session)
    conn.close()

    if not session:
        # Resolver succeeded but get_session lost the row -- shouldn't happen
        # in normal use; keep the guard for paranoid safety.
        print(f"No session found matching '{args.session_id}'", file=sys.stderr)
        return 1

    # Path-exposure rung: flag > config > neutral default (#56/H2).
    # The view runs BEFORE tiering, so scratch/thin machinery operates on
    # the post-view rows unchanged. Matching paths never see any of this.
    from . import pathlevels as _pl
    level = getattr(args, "paths", None)
    if level is None:
        level = config.get("paths_level") if config else None
        if level is not None and level not in _pl.SELECTABLE_LEVELS:
            print(
                f"[warning] config paths_level={level!r} is not a valid "
                f"level (valid: {', '.join(_pl.SELECTABLE_LEVELS)}); "
                f"using '{_pl.DEFAULT_LEVEL}'.",
                file=sys.stderr,
            )
            level = None
    if level is None:
        level = _pl.DEFAULT_LEVEL
    if level != _pl.DEFAULT_LEVEL or session.get("folders"):
        viewed, paths_note = _pl.apply_level(
            session.get("folders") or [], level)
        session["folders"] = viewed
        if paths_note:
            session["_paths_note"] = paths_note

    filters = _aggregate_filters(getattr(args, "filters", None))
    _render_show(session, show_all=getattr(args, "show_all", False),
                 min_work=filters.get("min-work", 0),
                 scratch_escape=_scratch_escape_config(config))
    return 0


def _scratch_escape_config(config) -> tuple[int, int]:
    """F1's two arms as user-definable config (H2): the junk boundary is
    a per-user judgment, so `>10 work units` and `top-10 rank` become
    two config lines. A malformed value warns naming the key and falls
    back -- corrupt config must not brick `csb show`, but silent
    reinterpretation is the failure class this release keeps hunting.
    """
    out = []
    for key, default in (("scratch_escape_min_work", 10),
                         ("scratch_escape_top_rank", 10)):
        val = (config or {}).get(key, default)
        try:
            # A bool is not a count -- int(True)=1 slips every numeric
            # check, so a fat-fingered `true` silently became threshold 1.
            if isinstance(val, bool):
                raise ValueError
            coerced = int(val)
            # int(3.5) never raises -- `csb config` stores JSON floats, so
            # 3.5 silently became 3: the exact silent-reinterpretation
            # class this code hunts (tester pass 1, Finding 3). LOSSY
            # coercion warns; 3.0 loses nothing and passes quietly.
            if float(val) != coerced or coerced < 0:
                raise ValueError
            val = coerced
        except (TypeError, ValueError, OverflowError):
            # OverflowError: int(float("inf")) -- a stored `Infinity` IS a
            # real float `csb config` can produce, and the round-1 lossy
            # fix added the int() path it reaches. Found by the round-2
            # adversarial pass AS a regression in round 1's own fix: the
            # patch written against silent reinterpretation introduced a
            # crash. A config value must never crash the tool.
            print(f"[warning] config {key}={val!r} is not a non-negative "
                  f"integer; using {default}.", file=sys.stderr)
            val = default
        out.append(val)
    return out[0], out[1]


def _fetch_lineage(conn, full_id: str, session: dict) -> dict:
    """Look up this session's parent and its direct forks.

    Returns ``{"parent": {...}|None, "parent_missing": <uuid>|None,
    "forks": [...]}``. ``parent_missing`` carries the UUID when the pointer
    names a session the index has never seen (pre-csb history, another
    machine) -- worth showing, because it still tells the user this session
    WAS forked from something.
    """
    out: dict = {"parent": None, "parent_missing": None, "forks": []}

    parent_id = session.get("parent_session_id")
    if parent_id:
        row = conn.execute(
            "SELECT session_id, session_name, deleted_at FROM sessions "
            "WHERE session_id = ?", (parent_id,),
        ).fetchone()
        if row is not None:
            out["parent"] = dict(row)
        else:
            out["parent_missing"] = parent_id

    out["forks"] = [
        dict(r) for r in conn.execute(
            "SELECT session_id, session_name, forked_at, deleted_at "
            "FROM sessions WHERE parent_session_id = ? "
            "ORDER BY forked_at, session_id", (full_id,),
        ).fetchall()
    ]
    return out


def _lineage_lines(session: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """Build ``(label, segments)`` rows for the show view's lineage block.

    ``segments`` is a list of ``(text, style)`` so each part can carry its
    own color, matching this view's existing conventions: names cyan, the
    purged marker RED (same as the `DELETED at:` line above), timestamps
    dim. Returning one flat string would force a single color on the whole
    row and paint `[purged]` the same as the name.

    Empty when the session neither was forked nor has forks -- the common
    case stays visually unchanged.
    """
    # Compact date form (timeline's, shared with `csb tree`) -- the verbose
    # local+tz+ISO form used by the fields above wraps these lines badly.
    from .timeline import format_timestamp as _compact_ts

    info = session.get("_lineage") or {}
    lines: list[tuple[str, list[tuple[str, str]]]] = []

    def row(uuid: str, name: str, deleted, stamp_iso):
        segs = [(uuid, ""), ("  ", ""), (name, "cyan")]
        if deleted:
            segs.append((" [purged]", "red"))
        if stamp_iso:
            segs.append((f"  (forked {_compact_ts(stamp_iso)})", "dim"))
        return segs

    forked_at = session.get("forked_at")

    parent = info.get("parent")
    if parent:
        lines.append(("Forked from:", row(
            parent["session_id"], parent.get("session_name") or "(unnamed)",
            parent.get("deleted_at"), forked_at)))
    elif info.get("parent_missing"):
        segs = [
            (info["parent_missing"], ""),
            ("  (not in index -- try csb update backfill-deleted)", "dim"),
        ]
        if forked_at:
            segs.append((f"  (forked {_compact_ts(forked_at)})", "dim"))
        lines.append(("Forked from:", segs))

    for i, fork in enumerate(info.get("forks") or []):
        lines.append((
            "Forks:" if i == 0 else "",
            row(fork["session_id"], fork.get("session_name") or "(unnamed)",
                fork.get("deleted_at"), fork.get("forked_at")),
        ))
    return lines


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


def _render_show(session: dict, show_all: bool = False,
                 min_work: int = 0,
                 scratch_escape: tuple = (10, 10)) -> None:
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
        lineage = _lineage_lines(session)
        if lineage:
            print()
            for label, segments in lineage:
                print(f"  {label:<14} {''.join(t for t, _ in segments)}")
            print(f"  {'':<14} (full tree: csb tree --root {full_id})")
        print(f"\n  Resume:        claude --resume {full_id}")
        if folders:
            worked, touched_only, low = _tier_folders(
                folders, *scratch_escape)
            worked, thin = _apply_work_filter(worked, min_work)
            print(f"\n  Working directories:")
            if session.get("_paths_note"):
                print(f"    ({session['_paths_note']})")
            for f in worked:
                marker = " [start]" if f["is_start_folder"] else ""
                if _row_get(f, "_approx_folded"):
                    marker += " (~)"
                if _row_get(f, "_scratch_promoted"):
                    marker += " (scratch)"
                print(f"    {f['folder_path']}  ({f['usage_count']}x){marker}")
            if thin:
                print(_thin_note(thin, min_work))
            if touched_only:
                print(f"\n  Also touched (no work credited):")
                for f in touched_only:
                    marker = " [start]" if f["is_start_folder"] else ""
                    # (~) here too -- a zero-work fold landing in this
                    # tier displayed with no indication a merge happened.
                    # The FOURTH two-render-paths miss of this effort.
                    if _row_get(f, "_approx_folded"):
                        marker += " (~)"
                    print(f"    {f['folder_path']}{marker}")
            if low:
                _print_low_value_note(low, show_all=show_all)
                if show_all:
                    for f in low:
                        count = (f"  ({f['usage_count']}x)"
                                 if f["usage_count"] else "")
                        # (~) in the low tier too -- a zero-work fold onto
                        # a scratch ancestor lands here, and this was the
                        # FIFTH render site with the marker convention
                        # silently inconsistent. One convention, all tiers.
                        fold = " (~)" if _row_get(f, "_approx_folded") else ""
                        print(f"    {f['folder_path']}{count}{fold}"
                              f"  {_low_reason(f)}")
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

    lineage = _lineage_lines(session)
    if lineage:
        console.print()
        for label, segments in lineage:
            line = Text()
            line.append(f"  {label:<14}", style="dim")
            for text, style in segments:
                line.append(text, style=style)
            console.print(line)
        hint = Text()
        hint.append(f"  {'':<14}", style="dim")
        hint.append(f"(full tree: csb tree --root {full_id})", style="dim")
        console.print(hint)

    console.print()
    resume_line = Text()
    resume_line.append("  Resume:        ", style="dim")
    resume_line.append(f"claude --resume {full_id}", style="bold yellow")
    console.print(resume_line)

    if folders:
        worked, touched_only, low = _tier_folders(
            folders, *scratch_escape)
        worked, thin = _apply_work_filter(worked, min_work)

        console.print()
        console.print(Text("  Working directories:", style="dim"))
        if session.get("_paths_note"):
            console.print(Text(f"    ({session['_paths_note']})",
                               style="dim"))
        # Rank by work units; the top one is where the session actually
        # spent itself -- frequently NOT the folder it launched from.
        max_count = max((f["usage_count"] for f in worked), default=0)
        for f in worked:
            is_start = bool(f["is_start_folder"])
            is_max = f["usage_count"] == max_count
            row = Text()
            row.append("    ")
            if is_max:
                row.append(f["folder_path"], style="bold green")
            elif is_start:
                row.append(f["folder_path"], style="white")
            else:
                row.append(f["folder_path"], style="grey70")
            row.append(f"  ({f['usage_count']}x)", style="dim")
            if is_start:
                row.append(" [start]", style="yellow")
            if _row_get(f, "_approx_folded"):
                row.append(" (~)", style="dim cyan")
            if _row_get(f, "_scratch_promoted"):
                row.append(" (scratch)", style="dim")
            if _row_get(f, "path_exists") == 0:
                row.append("  [not found]", style="dim yellow")
            console.print(row)

        if thin:
            console.print(Text(_thin_note(thin, min_work), style="dim"))

        if touched_only:
            console.print()
            console.print(Text("  Also touched (no work credited):", style="dim"))
            for f in touched_only:
                row = Text()
                row.append("    ")
                row.append(f["folder_path"], style="grey70")
                if f["is_start_folder"]:
                    row.append(" [start]", style="yellow")
                if _row_get(f, "_approx_folded"):
                    row.append(" (~)", style="dim cyan")
                console.print(row)

        if low:
            console.print()
            n = len(low)
            word = "path" if n == 1 else "paths"
            low_total = sum(f["usage_count"] for f in low)
            units = f", {low_total} work units" if low_total else ""
            if show_all:
                console.print(Text(
                    f"  Low value ({n} {word}{units} -- scratch or unresolved):",
                    style="dim"))
                for f in low:
                    row = Text()
                    row.append("    ")
                    row.append(f["folder_path"], style="grey50")
                    if f["usage_count"]:
                        row.append(f"  ({f['usage_count']}x)", style="dim")
                    if _row_get(f, "_approx_folded"):
                        row.append(" (~)", style="dim cyan")
                    row.append(f"  {_low_reason(f)}", style="dim")
                    console.print(row)
            else:
                # `--all`, not `--all-folders` -- see the plain-renderer note.
                console.print(Text(
                    f"  ({n} low-value {word} hidden{units} -- scratch dirs "
                    f"and unresolved paths; --all to show)", style="dim"))


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

    # v0.3.5 / v0.5.1: directory-scope (-d / -D) wiring. The argparse mutex
    # guarantees at most one of -d/-D is set. Directory-scope is now
    # source-agnostic AND multi-term. The source picks the folder signal:
    #   --source fts5 (explicit) -> file-op SUM(strength) ranking over the
    #       per-project FTS5 file_operations table (precise; --min-strength
    #       applies). Only FTS5-indexed sessions rank here.
    #   default / auto / convo / sesslog / jsonl -> folder_usage ranking
    #       (SUM(usage_count) under the path), so EVERY session that touched
    #       the folder is found and each is searched in its resolved source.
    # `_resolve_directory_pattern` builds the SQL match criteria the
    # folder_usage path needs (identical to `csb scan -d/-D`); `abs_path` +
    # `include_descendants` feed the FTS5 path. We no longer pin the source
    # to fts5 -- the resolved `source_override` drives the dispatch in
    # search(), and multiple terms are supported on both paths.
    directories_below = getattr(args, "directories_below", None)
    directory_only = getattr(args, "directory_only", None)
    dir_path = directories_below or directory_only
    dir_scope: dict | None = None
    if dir_path is not None:
        include_descendants = directory_only is None  # -D excludes descendants
        abs_path = str(Path(dir_path).resolve())
        _resolved, exact_value, like_match, like_exclude = (
            _resolve_directory_pattern(abs_path, include_descendants)
        )
        min_strength = getattr(args, "min_strength", 1)
        dir_scope = {
            "abs_path": abs_path,
            "include_descendants": include_descendants,
            "min_strength": min_strength,
            # SQL match criteria for the source-agnostic folder_usage path.
            "exact_value": exact_value,
            "like_match": like_match,
            "like_exclude": like_exclude,
        }
        # --min-strength only ranks the FTS5 file_operations table; it has no
        # analog for the folder_usage path. Note (don't fail) when the user
        # raised it under a non-fts5 source -- flags degrade gracefully.
        if min_strength > 1 and args.source != "fts5":
            print(
                "Note: --min-strength applies only to --source fts5 "
                f"(file-op strength ranking); ignored for --source {args.source}.",
                file=sys.stderr,
            )

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


# ── csb set: session sets -- boot epochs now, named sets with #62 ────────
#
# A "set" is a group of sessions that belong together. Two kinds share the
# verb (epic #60 decision): EPOCH sets are observed automatically ("what
# was active when the machine went down" -- zero user effort), NAMED sets
# are curated (#62). One noun, one renderer, one addressing scheme.

def cmd_set(args) -> int:
    """Dispatcher for `csb set <action>` (#60, #61).

    Actions:
      show <set>   -- numbered roster ('last' = the most recent boot epoch)

    Named-set CRUD (new / list / add / rm) arrives with #62.
    """
    action = getattr(args, "set_action", None)
    if action is None:
        print(
            "csb set: pick an action.\n"
            "  csb set show last            -- what was active at the last shutdown\n"
            "  csb set show <name>          -- a named set's roster\n"
            "  csb set new <name> <session>...   -- create a named set\n"
            "  csb set list                 -- every named set\n"
            "  csb set add <name> <session>...   -- extend a set\n"
            "  csb set rm <name> [<session>...]  -- remove members, or the set\n"
            "  csb set forget current:1     -- retract a stale live-registry entry\n"
            "\n"
            "Run `csb set <action> -h` for per-action options.",
            file=sys.stderr,
        )
        return 2
    if action == "show":
        return cmd_set_show(args)
    if action == "new":
        return cmd_set_new(args)
    if action == "list":
        return cmd_set_list(args)
    if action == "add":
        return cmd_set_add(args)
    if action == "rm":
        return cmd_set_rm(args)
    if action == "forget":
        return cmd_set_forget(args)
    # argparse's choices shouldn't let us reach here; defensive.
    print(f"Unknown set action: {action}", file=sys.stderr)
    return 2


def _set_warn(msg):
    """Warning sink for session_sets loaders -- stderr, never stdout."""
    print(f"Warning: {msg}", file=sys.stderr)


def _resolve_sessions_for_set(conn, queries, claude_dir):
    """Resolve CLI session queries to full UUIDs.

    Returns ``(uuids, exit_code)``. Resolution uses the same vocabulary as
    every other csb command (`_resolve_session_flexible`), and a single
    unresolvable query aborts the whole operation -- a half-built set is
    worse than none, because the user would have to work out which
    members landed.
    """
    uuids = []
    for query in queries:
        session, code = _resolve_session_flexible(conn, query, claude_dir)
        if session is None:
            return None, (code or 1)
        uuids.append(session["session_id"])
    return uuids, 0


def _resolve_membership_args(config, conn, queries):
    """Membership tokens: the view-index pre-pass, then the ordinary
    vocabulary (#76 -- the ONE seam `set new`/`add`/`rm` share).

    ``<view-or-set>:<N>`` grabs row N of that roster -- the number the
    user just read. Grammar (V1): split on the LAST colon; the suffix
    must be a bare integer AND the prefix must resolve as a view
    (current/boot/last/last~K/date) or an existing named set; anything
    else falls through UNCHANGED, so a session literally named
    ``notes:1`` keeps resolving by name. Each distinct prefix
    materializes ONCE through the shared materializer (V3), so epoch
    tokens inherit the empty-epoch fallthrough -- ``last~1:1`` grabs
    exactly what ``show last~1`` displayed -- and boot's registry
    appendix rows are addressable.

    Every grab is ECHOED (V5): live-view numbers shift as sessions open
    and close, so the race is disclosed, never prevented -- a mis-grab
    is visible immediately and ``set rm`` undoes it. Any miss aborts
    the WHOLE command (V4, the half-built-set rule). Tokens process in
    CLI order, so stored member order matches what was typed.
    Returns ``(uuids, exit_code)``.
    """
    resolved: list = []
    rosters: dict = {}
    for token in queries:
        prefix, sep, suffix = token.rpartition(":")
        is_view_index = bool(sep) and suffix.isdigit()
        # Name-first everywhere (the grammar unification): a session the
        # user literally named like a token stays addressable by that
        # name -- user-created names always beat promoted grammar in the
        # bare namespace. (Flips V1's edge, where a set named `notes`
        # outranked a session named `notes:1`; the per-grab echo makes
        # the resolution visible either way.)
        if is_view_index and _session_name_exists(conn, token):
            is_view_index = False
        if is_view_index and prefix not in rosters:
            if (prefix in ("current", "boot")
                    or classify_epoch_token(prefix) is not None
                    or session_sets.resolve_set_name(
                        config["claude_dir"], prefix) is not None):
                roster, code = _materialize_set_roster(config, prefix)
                if roster is None:
                    print(f"  (while resolving '{token}')",
                          file=sys.stderr)
                    return None, code
                rosters[prefix] = roster
            else:
                rosters[prefix] = None  # not a view -> ordinary query
        if is_view_index and rosters[prefix] is not None:
            roster = rosters[prefix]
            index = int(suffix)
            member = next((m for m in roster["members"]
                           if m["index"] == index), None)
            if member is None:
                size = len(roster["members"])
                noun = "row" if size == 1 else "rows"
                print(
                    f"'{token}': roster '{roster['name']}' has no row "
                    f"{index} ({size} {noun}). Nothing was changed.",
                    file=sys.stderr,
                )
                return None, 2
            sid = member["session_id"]
            from .ids import format_short_uuid

            name = member.get("session_name")
            label = (f"{name} ({format_short_uuid(sid)})" if name
                     else format_short_uuid(sid))
            print(f"  {token} -> {label}")
            if sid not in resolved:
                resolved.append(sid)
            continue
        single, code = _resolve_sessions_for_set(
            conn, [token], config["claude_dir"])
        if single is None:
            return None, code
        for sid in single:
            if sid not in resolved:
                resolved.append(sid)
    return resolved, 0


def _named_set_roster(conn, entry):
    """Materialize a named set's members into roster dicts.

    Members whose session is no longer in the index are rendered marked
    rather than dropped (#62 AC): a set that silently shrinks is a set
    that lies about what it holds. Indices are 1-based positions in the
    FULL member list -- the same stable-numbering contract the epoch
    roster established, which `csb resume last:<N>` relies on.
    """
    members = []
    for index, raw in enumerate(entry.get("members", []), start=1):
        sid = raw["session_id"]
        session = get_session(conn, sid)
        if session is None:
            members.append({
                "index": index,
                "session_id": sid,
                "session_name": None,
                "project": None,
                "start_folder": None,
                "started_at": None,
                "last_active_at": None,
                "jsonl_path": None,
                "jsonl_mtime": None,
                "purged": False,
                "is_fork": False,
                "in_index": False,
                "messages": None,
            })
            continue
        members.append({
            "index": index,
            "session_id": sid,
            "session_name": session.get("session_name"),
            "project": session.get("project"),
            "start_folder": session.get("start_folder"),
            "started_at": session.get("started_at"),
            "last_active_at": session.get("last_active_at"),
            "jsonl_path": session.get("jsonl_path"),
            "jsonl_mtime": session.get("jsonl_mtime"),
            "purged": bool(session.get("deleted_at")),
            "is_fork": bool(session.get("is_fork")),
            "in_index": True,
            "messages": session.get("message_count"),
        })
    return members


def _annotate_named_set_liveness(config, members) -> int:
    """Tag a NAMED set's members with live status. Returns the open count.

    `csb set list` already reports "(N open now)" per named set; this is
    what lets `csb set show <name>` say WHICH ones, in the same tiers the
    live views use.

    REGISTRY-FIRST, so a set with nothing open pays nothing: the entry
    glob is ~free and gates the 1-2s process scan entirely.

    Only TWO tiers are honest here. A named set is not boot-scoped, so a
    member absent from the registry usually just means "not open right
    now" -- the normal resting state of a curated group -- and rendering
    `boot`'s `[exited]` for it would claim an observed close that never
    happened. Absent members stay unadorned; the same rule the reclaim
    menu already applies (available = not in the registry).
    """
    from . import liveness

    entries = {e["session_id"]: e for e in _live_entries(config)}
    if not entries:
        return 0
    matched = [m for m in members if m["session_id"] in entries]
    if not matched:
        return 0

    scan = liveness.scan()
    pairs = []
    for m in matched:
        entry = entries[m["session_id"]]
        pid = liveness.verify_entry(scan, entry, m.get("session_name"))
        m["live_status"] = "running" if pid is not None else "unverified"
        m["pid"] = pid
        pairs.append((m, entry))
    # One pid belongs to one row (#72): a fork's frozen command line can
    # name its parent, and both may sit in this set.
    liveness.arbitrate_pid_claims(pairs)
    return sum(1 for m in matched if m.get("live_status") == "running")


def _materialize_set_roster(config, name):
    """The canonical roster for a set -- epoch (`last`) or named (#63).

    One path serves `csb set show` and `csb resume last:<N>`, so an index
    printed by the first always addresses the same session in the second.

    **This function takes no filter parameters, and that absence is the
    contract.** Row numbers are positions in the canonical roster -- for
    an epoch, every session active between the previous fence and the
    shutdown. Narrowing (``--window``) is a DISPLAY concern applied by
    the caller via :func:`_filter_roster_for_display`, which preserves
    these numbers and shows gaps.

    That split is load-bearing. An earlier version accepted the window
    here, so `csb set show last --window 60` renumbered from 1 over a
    subset while resume-by-index still addressed the full roster --
    two different sessions behind one number, in a workflow whose whole
    premise is "read the roster, walk away, run the next one". The
    docstring claimed this contract while the signature broke it.

    Returns ``(roster, 0)`` or ``(None, exit_code)`` with the error
    already printed. A roster is a dict with ``kind`` / ``name`` /
    ``epoch`` (None for named sets and when no fence exists) /
    ``members`` / ``missing_timestamps``; an empty ``members`` list is a
    valid roster, not an error -- callers decide how to present it.
    """
    window_hours = None  # canonical roster: never narrowed here
    if name == "current":
        return _materialize_current_roster(config), 0
    if name == "boot":
        return _materialize_boot_roster(config)
    token_kind = classify_epoch_token(name)
    if token_kind is not None:
        try:
            fences = read_fences()
        except FenceUnavailableError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return None, 1
        history = enumerate_epochs(fences)
        matches = resolve_epoch_token(name, history)
        if token_kind == "last" and not matches:
            return {
                "kind": "epoch", "name": "last", "epoch": None,
                "members": [], "missing_timestamps": 0,
                "window_hours": None, "window_source": None,
                "last_scan_at": None,
            }, 0
        if not matches:
            if token_kind == "tilde":
                depth = len(history)
                noun = "epoch" if depth == 1 else "epochs"
                print(
                    f"'{name}' is beyond recorded history -- {depth} "
                    f"completed {noun} known. `csb set list` shows the "
                    "addressable range.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"No completed epoch spans {name}. `csb set list` "
                    "shows recent epochs and their dates.",
                    file=sys.stderr,
                )
            return None, 1
        if len(matches) > 1:
            # Date ambiguity: list-and-ask, never newest-wins-silently
            # (R3 DWP) -- each row carries its exact token, so the
            # retry is one paste.
            print(f"'{name}' matches {len(matches)} epochs -- address "
                  "one exactly:", file=sys.stderr)
            for idx, ep in matches:
                token = "last" if idx == 0 else f"last~{idx}"
                print(f"  csb set show {token:8}  shutdown "
                      f"{_iso_z(ep.shutdown_utc)}  ({ep.cause})",
                      file=sys.stderr)
            return None, 2
        history_index, epoch = matches[0]
        requested = "last" if history_index == 0 else f"last~{history_index}"

        conn = open_db(config["index_path"])
        init_schema(conn)
        rows = conn.execute(
            "SELECT session_id, session_name, project, start_folder,"
            "       started_at, last_active_at, jsonl_path, jsonl_mtime,"
            "       deleted_at, is_fork, message_count"
            "  FROM sessions"
        ).fetchall()
        segments = _load_activity_segments(conn)
        last_scan_row = conn.execute(
            "SELECT scanned_at FROM scan_history ORDER BY scan_id DESC LIMIT 1"
        ).fetchone()
        conn.close()

        # Disclosed fallthrough (H8, user review): a RELATIVE address
        # usually means "the last WORKING set", and deep epochs thin out
        # as their sessions get resumed later (activity drift). Walk
        # deeper past empty epochs -- each skipped epoch is DISCLOSED in
        # the output, so nothing is silently redirected. Dates stay
        # exact: an absolute address is a question about that date.
        # Lives HERE in the shared materializer so `resume --set` and
        # `--from` land on the same roster the show displayed.
        candidates = [(history_index, epoch)]
        if token_kind in ("last", "tilde"):
            candidates += [(i, history[i])
                           for i in range(history_index + 1, len(history))]
        skipped_empty = []
        settled = None
        for idx, ep in candidates:
            lo_i, hi_i, ws_i = epoch_window(ep, window_hours)
            members_i, missing_i = build_roster(rows, lo_i, hi_i,
                                                segments=segments)
            if members_i:
                settled = (idx, ep, lo_i, hi_i, ws_i, members_i, missing_i)
                break
            skipped_empty.append({
                "token": "last" if idx == 0 else f"last~{idx}",
                "epoch": ep,
                "window_hours": (hi_i - lo_i).total_seconds() / 3600.0,
                "window_source": ws_i,
            })
        exhausted = 0
        activity_floor = None
        fallthrough_failed = False
        nearest_working = None
        if settled is None:
            # Nothing at-or-deeper has members: settle back on the
            # REQUESTED epoch (today's honest empty answer) rather than
            # landing arbitrarily deep. For relative tokens, explain
            # WHY it can never succeed (the index's activity floor) and
            # POINT at the nearest epoch that would work -- a hint,
            # never a jump: the user asked a deep question, and
            # silently answering a shallow one would be its own lie.
            if token_kind in ("last", "tilde"):
                fallthrough_failed = True
                exhausted = max(0, len(skipped_empty) - 1)
                floors = [parse_index_ts(r["last_active_at"]) for r in rows]
                floors = [f for f in floors if f is not None]
                activity_floor = min(floors) if floors else None
                for j in range(history_index - 1, -1, -1):
                    lo_j, hi_j, _ws_j = epoch_window(history[j],
                                                     window_hours)
                    members_j, _missing_j = build_roster(
                        rows, lo_j, hi_j, segments=segments)
                    if members_j:
                        nearest_working = {
                            "token": "last" if j == 0 else f"last~{j}",
                            "count": len(members_j),
                            "shutdown_at": history[j].shutdown_utc,
                        }
                        break
            lo, hi, window_source = epoch_window(epoch, window_hours)
            members, missing = build_roster(rows, lo, hi, segments=segments)
            skipped_empty = []
        else:
            history_index, epoch, lo, hi, window_source, members, missing \
                = settled

        # Snapshot badge + UNION (#80 S3, placed per S3a): a boundary
        # snapshot covering the settled epoch marks members provably open
        # at its shutdown -- and members it names that segment membership
        # DIDN'T surface (purged before indexing, pre-migration rows) are
        # APPENDED, marked, counted, never dropped. Lives here in the
        # shared materializer so `resume --set` and `--from` land on the
        # same roster `show` displays (the H8 lesson).
        from . import live_registry as _lr

        snapshot = _lr.read_snapshot(
            config["claude_dir"], boot_utc=epoch.boot_utc,
            shutdown_utc=epoch.shutdown_utc,
        )
        snapshot_ids: set = set()
        snap_entries: dict = {}
        if snapshot:
            for e in snapshot["open_at_shutdown"]:
                sid = e.get("session_id")
                if sid:
                    snapshot_ids.add(sid)
                    snap_entries[sid] = e
        for m in members:
            if m["session_id"] in snapshot_ids:
                m["open_at_shutdown"] = True
        rows_by_id = {r["session_id"]: r for r in rows}
        union_index = len(members)
        for sid in sorted(snapshot_ids - {m["session_id"] for m in members}):
            row = rows_by_id.get(sid)
            entry = snap_entries.get(sid, {})
            union_index += 1
            members.append({
                "index": union_index,
                "session_id": sid,
                "session_name": row["session_name"] if row is not None
                                else None,
                "project": row["project"] if row is not None else None,
                "start_folder": (row["start_folder"] if row is not None
                                 else None) or entry.get("cwd"),
                "started_at": (row["started_at"] if row is not None
                               else None) or entry.get("started_at"),
                "last_active_at": None,
                "jsonl_path": row["jsonl_path"] if row is not None else None,
                "jsonl_mtime": row["jsonl_mtime"] if row is not None else 0,
                "purged": bool(row["deleted_at"]) if row is not None
                          else False,
                "is_fork": bool(row["is_fork"]) if row is not None else False,
                "in_index": row is not None,
                "messages": None,
                "segment_backed": False,
                "snapshot_only": True,
                "open_at_shutdown": True,
            })

        canonical = "last" if history_index == 0 else f"last~{history_index}"
        return {
            "kind": "epoch", "name": canonical, "epoch": epoch,
            "history_index": history_index,
            "requested": requested,
            "skipped_empty": skipped_empty,
            "fallthrough_failed": fallthrough_failed,
            "fallthrough_exhausted": exhausted,
            "activity_floor": activity_floor,
            "nearest_working": nearest_working,
            "snapshot_available": bool(snapshot_ids),
            "members": members, "missing_timestamps": missing,
            "window_lo": lo, "window_hi": hi,
            "window_hours": (hi - lo).total_seconds() / 3600.0,
            "window_source": window_source,
            "last_scan_at": last_scan_row[0] if last_scan_row else None,
            "index_empty": not rows,
        }, 0

    try:
        entry = session_sets.get_set(config["claude_dir"], name,
                                     warn=_set_warn)
    except session_sets.SetError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None, 1
    if entry is None:
        print(
            f"No set named '{name}'. Use 'last' for the most recent boot "
            "epoch, or `csb set list` to see named sets.",
            file=sys.stderr,
        )
        return None, 1

    stored = session_sets.resolve_set_name(config["claude_dir"], name) or name
    conn = open_db(config["index_path"])
    init_schema(conn)
    members = _named_set_roster(conn, entry)
    conn.close()
    # Live tiers ride the materializer, so `resume --set NAME` and
    # `--from NAME` see the same liveness `show` displays.
    open_count = _annotate_named_set_liveness(config, members)
    return {
        "kind": "named", "name": stored, "epoch": None,
        "members": members, "missing_timestamps": 0,
        "open_count": open_count,
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "promoted_from": entry.get("promoted_from"),
        "window_hours": None, "window_source": None, "last_scan_at": None,
    }, 0


_INVOCATION_NOTE = ("(row numbers reflect this invocation -- they renumber "
                    "as sessions open and close; hints use stable names)")


def _load_activity_segments(conn):
    """session_id -> [(start_dt, end_dt, messages)] for roster builds (#80).

    One read + parse serves every ``build_roster`` call a command makes
    (the H8 fallthrough can probe several epochs). Unparseable rows are
    skipped -- a session whose every segment is garbage degrades to
    point-membership (S4), never to an error.
    """
    parsed: dict = {}
    for sid, seg_list in fetch_activity_segments(conn).items():
        out = []
        for seg_start, seg_end, seg_messages in seg_list:
            start_dt = parse_index_ts(seg_start)
            end_dt = parse_index_ts(seg_end)
            if start_dt is None or end_dt is None:
                continue
            out.append((start_dt, end_dt, seg_messages))
        if out:
            parsed[sid] = out
    return parsed


def _cmd_set_show_boot(args, json_mode) -> int:
    """`csb set show boot` -- everything active since this boot (R1)."""
    config = _get_config(args)
    roster, code = _materialize_set_roster(config, "boot")
    if roster is None:
        return code
    all_members = roster["members"]
    boot_utc = roster["boot_utc"]

    display_window = getattr(args, "window", None)
    members, hidden = _filter_roster_for_display(
        all_members, roster["as_of"], display_window)

    if json_mode:
        payload = {
            "kind": "boot",
            "name": "boot",
            "epoch": None,
            "boot_at": _iso_z(boot_utc),
            "as_of": _iso_z(roster["as_of"]),
            "hooks_active": roster["hooks_active"],
            "scan_ok": roster["scan_ok"],
            "bare_processes": roster["bare_processes"],
            "display_window_hours": display_window,
            "roster_size": len(all_members),
            "hidden_by_window": hidden,
            "members": [
                {k: m[k] for k in (
                    "index", "session_id", "session_name", "project",
                    "start_folder", "started_at", "last_active_at",
                    "purged", "is_fork", "in_index", "messages",
                    "live_status", "pid", "recorded_pid", "pid_at",
                )}
                for m in members
            ],
            "missing_timestamps": roster["missing_timestamps"],
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    from .set_render import render_roster as _render
    from .timeline import relative_date

    running = sum(1 for m in all_members
                  if m.get("live_status") == "running")
    unverified = sum(1 for m in all_members
                     if m.get("live_status") == "unverified")
    exited = sum(1 for m in all_members if m.get("live_status") == "exited")

    header = [[
        ("Active this boot", "bold"),
        (f" -- {_booted_label(boot_utc)}", "dim"),
        (" -- as of this invocation", "dim"),
    ]]
    noun = "session" if len(all_members) == 1 else "sessions"
    tier_line = [
        (f"{len(all_members)} {noun}: ", None),
        (f"{running} running", "green" if running else None),
        (f", {unverified} no exit observed",
         "yellow" if unverified else None),
        (f", {exited} exited", None),
    ]
    header.append(tier_line)
    header.append([(_INVOCATION_NOTE, "dim")])
    if hidden:
        header.append([
            (f"Showing {len(members)} of {len(all_members)} -- narrowed to "
             f"{display_window:g}h before now. ", None),
            ("Numbers stay canonical, so gaps are expected.", "dim"),
        ])

    if not all_members:
        for segs in header[:1]:
            _style_print(segs)
        print("No sessions with activity since this boot.")
        return 0

    # Stable hints on a live view: running rows get name-based fork hints.
    from .set_render import ambiguous_names_in, _resume_hint_target
    ambiguous = ambiguous_names_in(members)
    for m in members:
        if m.get("live_status") == "running":
            m["hint_override"] = (
                f"csb resume {_resume_hint_target(m, ambiguous)} "
                "-- --fork-session"
            )

    footers = []
    if not roster["hooks_active"]:
        footers.append(
            "exit detection unavailable (no hook activity observed this "
            "boot) -- rows show activity only."
        )
    if roster["missing_timestamps"]:
        n = roster["missing_timestamps"]
        noun2 = "session lacks" if n == 1 else "sessions lack"
        footers.append(f"Note: {n} {noun2} activity timestamps and cannot "
                       "be placed in any window.")
    if not members and hidden:
        for segs in header:
            _style_print(segs)
        print(f"No sessions active within {display_window:g}h of now.")
        return 0

    _render(members, header, footer_notes=tuple(footers),
            shutdown_utc=roster["as_of"], gap_label="ago")
    return 0


def _cmd_set_show_current(args, json_mode) -> int:
    """`csb set show current` -- what is open right now (#64)."""
    config = _get_config(args)
    roster, code = _materialize_set_roster(config, "current")
    if roster is None:
        return code
    members = roster["members"]
    boot_utc = roster["boot_utc"]

    if json_mode:
        payload = {
            "kind": "current",
            "name": "current",
            "epoch": None,
            "boot_at": _iso_z(boot_utc) if boot_utc else None,
            "scan_ok": roster["scan_ok"],
            "bare_processes": roster["bare_processes"],
            "members": [
                {k: m[k] for k in (
                    "index", "session_id", "session_name", "project",
                    "start_folder", "started_at", "last_active_at",
                    "purged", "is_fork", "in_index", "messages",
                    "source",
                    "live_status", "pid", "recorded_pid", "pid_at",
                )}
                for m in members
            ],
            "missing_timestamps": 0,
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    from .set_render import render_roster as _render
    from .timeline import relative_date

    if not members:
        print("The live registry has no sessions for this boot.")
        print(
            "  Note: live tracking needs this release's hooks -- if the csb "
            "plugin predates the registry, update it (`claude plugin "
            "update claude-session-backup`) and restart your sessions. "
            "Sessions already open register on their next start.",
            file=sys.stderr,
        )
        return 0

    running = sum(1 for m in members if m["live_status"] == "running")
    unverified = len(members) - running
    header = [[
        ("Currently open", "bold"),
        (" -- this boot", "dim"),
    ]]
    if boot_utc is not None:
        header[0].append((f" ({_booted_label(boot_utc)})", "dim"))
    tier_line = [
        (f"{len(members)} session{'s' if len(members) != 1 else ''}: ", None),
        (f"{running} running", "green" if running else None),
        (" (process-verified)", "dim"),
        (f", {unverified} no exit observed", "yellow" if unverified else None),
    ]
    header.append(tier_line)
    header.append([(_INVOCATION_NOTE, "dim")])
    if not roster["scan_ok"]:
        header.append([
            ("Process verification unavailable -- every row is registry-only.",
             "yellow"),
        ])

    footers = []
    if unverified and roster["bare_processes"]:
        noun = "process" if roster["bare_processes"] == 1 else "processes"
        footers.append(
            f"Note: {roster['bare_processes']} claude {noun} without a "
            "--resume identifier exist -- fresh sessions cannot be matched "
            "from argv, so an unverified row may well be one of those."
        )

    # Per-state hints (the C11/#64 rule): a RUNNING session must not get a
    # plain resume hint -- that would invite a second client onto one
    # transcript. Branch from it instead.
    from .set_render import ambiguous_names_in, _resume_hint_target
    ambiguous = ambiguous_names_in(members)
    for m in members:
        if m["live_status"] == "running":
            m["hint_override"] = (
                f"csb resume {_resume_hint_target(m, ambiguous)} "
                "-- --fork-session"
            )

    _render(members, header, footer_notes=tuple(footers), shutdown_utc=None)
    return 0


def _cmd_set_show_named(args, name, json_mode) -> int:
    """`csb set show <name>` for a NAMED set (#62).

    Shares the epoch roster's renderer and its stable 1-based numbering,
    so both kinds of set read identically and Phase 3's index addressing
    works against either.
    """
    config = _get_config(args)
    roster, code = _materialize_set_roster(config, name)
    if roster is None:
        return code
    stored = roster["name"]
    members = roster["members"]

    if json_mode:
        print(json.dumps({
            "kind": "named",
            "name": stored,
            "epoch": None,
            "created_at": roster.get("created_at"),
            "updated_at": roster.get("updated_at"),
            "promoted_from": roster.get("promoted_from"),
            "open_count": roster.get("open_count", 0),
            "members": [
                {**{k: m[k] for k in (
                    "index", "session_id", "session_name", "project",
                    "start_folder", "started_at", "last_active_at",
                    "purged", "is_fork", "in_index", "messages",
                )},
                 "live_status": m.get("live_status"),
                 "pid": m.get("pid")}
                for m in members
            ],
            "missing_timestamps": 0,
        }, indent=2, default=str))
        return 0

    count = len(members)
    noun = "member" if count == 1 else "members"
    header = [[
        (f"Set '{stored}'", "bold"),
        (f" -- {count} {noun}", "dim"),
    ]]
    open_count = roster.get("open_count", 0)
    if open_count:
        header[0].append((f" ({open_count} open now)", "green"))
    promoted = roster.get("promoted_from")
    if promoted:
        # Provenance (R3 H5): where the freeze came from -- the token at
        # freeze time plus the shutdown it named, which stays true as
        # that epoch's address drifts deeper into history.
        header.append([
            (f"promoted from '{promoted.get('token')}' "
             f"(shutdown {promoted.get('shutdown_at')})", "dim"),
        ])
    if not members:
        for segs in header:
            _style_print(segs)
        print()
        print("This set has no members.")
        print(f"  Add some: `csb set add {stored} <session>`", file=sys.stderr)
        return 0

    footers = []
    unresolved = sum(1 for m in members if not m["in_index"])
    if unresolved:
        noun = "member is" if unresolved == 1 else "members are"
        footers.append(
            f"Note: {unresolved} {noun} no longer in the index -- purged "
            "beyond recovery, or the index needs `csb update rebuild-index`."
        )
    # A running member's plain resume hint would invite a second client
    # onto one live transcript; branch instead (same rule as `current`).
    from .set_render import _resume_hint_target, ambiguous_names_in
    ambiguous = ambiguous_names_in(members)
    for m in members:
        if m.get("live_status") == "running":
            m["hint_override"] = (
                f"csb resume {_resume_hint_target(m, ambiguous)} "
                "-- --fork-session"
            )
    render_roster(members, header, footer_notes=tuple(footers),
                  shutdown_utc=None)
    return 0


def cmd_set_new(args) -> int:
    """`csb set new <name> [<session>...] [--from current|last]` (#62/#64).

    ``--from`` promotes a whole view -- freeze what is open right now
    (`--from current`) or the last epoch's roster (`--from last`) into a
    named set, then curate by subtraction (`csb set rm`). Explicit
    sessions may be given alongside it; the union is the set.
    """
    config = _get_config(args)
    name = args.set_name
    try:
        session_sets.validate_set_name(name)
    except session_sets.SetError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    from_view = getattr(args, "from_view", None)
    sessions_args = list(getattr(args, "sessions", []) or [])
    if not from_view and not sessions_args:
        print(
            "Nothing to add: name sessions, or use `--from current` / "
            "`--from last` to promote a whole view.",
            file=sys.stderr,
        )
        return 2

    uuids: list = []
    promoted_from = None
    if from_view:
        if (from_view not in ("current", "boot")
                and classify_epoch_token(from_view) is None):
            print(
                f"`--from` takes a view: current, boot, last, last~N, or "
                f"a date -- not '{from_view}'.",
                file=sys.stderr,
            )
            return 2
        roster, code = _materialize_set_roster(config, from_view)
        if roster is None:
            return code
        if not roster["members"]:
            print(f"View '{from_view}' has no members -- nothing to save.",
                  file=sys.stderr)
            return 1
        uuids.extend(m["session_id"] for m in roster["members"])
        if roster.get("kind") == "epoch" and roster.get("epoch") is not None:
            # Provenance (R3 H5): the CANONICAL token at freeze time plus
            # the shutdown instant -- stable even as `last~2` drifts to
            # `last~5` under new restarts.
            promoted_from = {
                "token": roster["name"],
                "shutdown_at": _iso_z(roster["epoch"].shutdown_utc),
            }

    if sessions_args:
        conn = open_db(config["index_path"])
        init_schema(conn)
        try:
            resolved, code = _resolve_membership_args(
                config, conn, sessions_args)
        finally:
            conn.close()
        if resolved is None:
            return code
        for sid in resolved:
            if sid not in uuids:
                uuids.append(sid)

    try:
        session_sets.create_set(config["claude_dir"], name, uuids,
                                promoted_from=promoted_from)
    except session_sets.SetError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    noun = "session" if len(uuids) == 1 else "sessions"
    origin = f" from '{from_view}'" if from_view else ""
    print(f"Created set '{name}' with {len(uuids)} {noun}{origin}.")
    print(f"  csb set show {name}", file=sys.stderr)
    return 0


def cmd_set_add(args) -> int:
    """`csb set add <name> <session>...` -- extend a named set (#62)."""
    config = _get_config(args)
    name = args.set_name
    stored = session_sets.resolve_set_name(config["claude_dir"], name,
                                           warn=_set_warn)
    if stored is None:
        # The intent is unambiguous -- the user typed real member tokens
        # at a name that doesn't exist yet. On a TTY, offer the create
        # (delegating to `set new`, which brings validation, resolution
        # echoes, and the summary). Scripts keep the deterministic
        # error + exact-retry hint -- never create implicitly there.
        if sys.stdin is not None and sys.stdin.isatty():
            try:
                answer = input(f"No set named '{name}'. Create it with "
                               "these members? [Y/n] ")
            except (EOFError, KeyboardInterrupt):
                print()
                return 1
            if answer.strip().lower() in ("", "y", "yes"):
                return cmd_set_new(args)
            return 1
        retry = " ".join([name] + list(args.sessions or []))
        print(f"No set named '{name}'. Create it with the same members:",
              file=sys.stderr)
        print(f"  csb set new {retry}", file=sys.stderr)
        return 1

    conn = open_db(config["index_path"])
    init_schema(conn)
    try:
        uuids, code = _resolve_membership_args(
            config, conn, args.sessions)
    finally:
        conn.close()
    if uuids is None:
        return code

    added = skipped = 0
    for sid in uuids:
        if session_sets.add_member(config["claude_dir"], stored, sid):
            added += 1
        else:
            skipped += 1
    if added:
        noun = "session" if added == 1 else "sessions"
        print(f"Added {added} {noun} to '{stored}'.")
    if skipped:
        noun = "session was" if skipped == 1 else "sessions were"
        print(f"{skipped} {noun} already a member of '{stored}' -- no change.")
    return 0


def cmd_set_forget(args) -> int:
    """`csb set forget <token-or-session>` -- retract a registry entry (RV1).

    The user testifying about what csb cannot know. A Ctrl+C'd session
    leaves its entry behind (the SessionEnd hook is cancelled before it
    can erase it), and a pid-less entry from an older plugin can never
    be resolved by evidence -- both then read `[no exit observed]`
    forever, indistinguishable from crash evidence, which is the one
    ambiguity the tier ladder exists to avoid. The witness who was
    there can settle it; csb cannot.

    Distinct from `csb set rm`, which edits membership YOU declared.
    This retracts evidence CSB recorded -- same gesture, opposite
    semantics, hence the separate verb (and the bare `csb forget`
    namespace is deliberately left for #77's index-level ladder).

    Guards:
      * REFUSES a verifiably-running session -- asserting "it is
        closed" about a process csb can see alive is a mistake, not an
        override. ``--force`` proceeds anyway, which is legitimate for
        PRIVACY (an entry naming a path you want gone) but never for
        state.
      * A roster row with NO registry entry behind it (an index-derived
        `boot` row) errors rather than silently succeeding (RV2) -- the
        row will still be there next invocation, and saying so teaches
        the difference between an observed entry and a derived row.

    Removal is a plain unlink (RV3): ``csb-live/`` rides the noise
    commits, so ``git log -- csb-live/`` retains the entry's whole life
    and death. The echo says so, keeping "recoverable" discoverable.
    """
    from . import live_registry, liveness
    from .ids import format_short_uuid

    config = _get_config(args)
    claude_dir = config["claude_dir"]
    queries = list(getattr(args, "sessions", []) or [])
    if not queries:
        print(
            "csb set forget needs a roster row or session to retract.\n"
            "  csb set forget current:1     the row you are looking at\n"
            "  csb set forget <session>     by name or UUID",
            file=sys.stderr,
        )
        return 2

    conn = open_db(config["index_path"])
    init_schema(conn)
    try:
        uuids, code = _resolve_membership_args(config, conn, queries)
    finally:
        conn.close()
    if uuids is None:
        return code

    entries = {e["session_id"]: e for e in live_registry.read_entries(claude_dir)}
    scan = None
    retracted = missing = refused = 0
    for sid in uuids:
        entry = entries.get(sid)
        if entry is None:
            # RV2: addressed a row with nothing behind it.
            print(f"No live-registry entry for {format_short_uuid(sid)} -- nothing "
                  "to retract (an index-derived row is not an observation).",
                  file=sys.stderr)
            missing += 1
            continue
        if not getattr(args, "force", False):
            if scan is None:
                scan = liveness.scan()
            if liveness.verify_entry(scan, entry, None) is not None:
                print(
                    f"{format_short_uuid(sid)} is verifiably RUNNING -- refusing to "
                    "retract an entry csb can see is true. Use --force if you "
                    "want it gone anyway (e.g. for privacy).",
                    file=sys.stderr,
                )
                refused += 1
                continue
        try:
            target = live_registry.entry_path(claude_dir, sid)
            # Belt-and-braces: entry_path already rejects unsafe ids,
            # but this is the only place csb DELETES from a path built
            # out of registry data, so confirm containment at the point
            # of no return rather than trusting a caller upstream.
            live = live_registry.live_dir(claude_dir).resolve()
            if live not in target.resolve().parents:
                raise ValueError(f"refusing to unlink outside {live}")
            target.unlink()
        except (OSError, ValueError) as exc:
            print(f"Could not retract {format_short_uuid(sid)}: {exc}",
                  file=sys.stderr)
            missing += 1
            continue
        name = entry.get("cwd") or ""
        print(f"Retracted {format_short_uuid(sid)}"
              + (f"  (started {entry.get('started_at')})"
                 if entry.get("started_at") else "")
              + (f"  {name}" if name else ""))
        retracted += 1

    if retracted:
        print("  The entry stays in your backup store's history "
              "(`git log -- csb-live/`).", file=sys.stderr)
    if refused or missing:
        return 1
    return 0


def cmd_set_rm(args) -> int:
    """`csb set rm <name> [<session>...]` -- remove members or the set (#62)."""
    config = _get_config(args)
    name = args.set_name
    stored = session_sets.resolve_set_name(config["claude_dir"], name,
                                           warn=_set_warn)
    if stored is None:
        print(f"No set named '{name}' -- `csb set list` shows what exists.",
              file=sys.stderr)
        return 1

    if not args.sessions:
        session_sets.delete_set(config["claude_dir"], stored)
        print(f"Deleted set '{stored}'.")
        print("  The sets file is committed in the user class -- recoverable "
              "from your backup store's git history.", file=sys.stderr)
        return 0

    conn = open_db(config["index_path"])
    init_schema(conn)
    try:
        uuids, code = _resolve_membership_args(
            config, conn, args.sessions)
    finally:
        conn.close()
    if uuids is None:
        return code

    removed = missing = 0
    for sid in uuids:
        if session_sets.remove_member(config["claude_dir"], stored, sid):
            removed += 1
        else:
            missing += 1
    if removed:
        noun = "session" if removed == 1 else "sessions"
        print(f"Removed {removed} {noun} from '{stored}'.")
    if missing:
        noun = "session was" if missing == 1 else "sessions were"
        print(f"{missing} {noun} not a member of '{stored}' -- nothing removed.")
    return 0


def cmd_set_list(args) -> int:
    """`csb set list` -- named sets plus the most recent boot epoch (#62)."""
    config = _get_config(args)
    json_mode = getattr(args, "json", False)
    try:
        sets = session_sets.list_sets(config["claude_dir"], warn=_set_warn)
    except session_sets.SetError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # The epoch section is best-effort: named sets are platform-independent
    # and must list even where fence reading cannot work (POSIX, no
    # PowerShell). Degrade to a note, never fail the whole command.
    # Fences are read ONCE and shared (R3 H4).
    history: list = []
    epoch = None
    epoch_note = None
    try:
        history = enumerate_epochs(read_fences())
        epoch = history[0] if history else None
        if epoch is None:
            epoch_note = "no shutdown fence found in the event log"
    except FenceUnavailableError as exc:
        epoch_note = str(exc)

    # The epoch in progress: fence-free (only needs the boot instant),
    # so it lists even where fence reading cannot work.
    from . import live_registry

    boot_utc = live_registry.current_boot_utc()

    # Live overlap: which of a set's members are open RIGHT NOW -- the
    # honest observable for "am I using this set" (csb cannot know
    # intent, only intersection). User question, 2026-08-07.
    try:
        live_ids = _live_session_ids(config)
    except Exception:  # noqa: BLE001 -- listing never fails on extras
        live_ids = set()

    if json_mode:
        payload = {
            "sets": [
                {
                    "name": name,
                    "members": len(entry.get("members", [])),
                    "open_members": len(
                        {m.get("session_id")
                         for m in entry.get("members", [])} & live_ids),
                    "created_at": entry.get("created_at"),
                    "updated_at": entry.get("updated_at"),
                }
                for name, entry in sets
            ],
            # Additive alongside the legacy "epoch" key (kept byte-stable
            # for existing consumers).
            "boot": (
                {"name": "boot", "boot_at": _iso_z(boot_utc)}
                if boot_utc is not None else None
            ),
            # R3: the addressable history, newest first. `exact` = a
            # retained boundary snapshot covers that epoch's boot, so
            # `--open` can badge it.
            "epochs": [
                {
                    "token": "last" if i == 0 else f"last~{i}",
                    "shutdown_at": _iso_z(ep.shutdown_utc),
                    "boot_at": _iso_z(ep.boot_utc) if ep.boot_utc else None,
                    "cause": ep.cause,
                    "exact": bool(live_registry.read_snapshot(
                        config["claude_dir"], boot_utc=ep.boot_utc,
                        shutdown_utc=ep.shutdown_utc)),
                }
                for i, ep in enumerate(history[:5])
            ],
            "epoch": (
                {
                    "name": "last",
                    "shutdown_at": _iso_z(epoch.shutdown_utc),
                    "cause": epoch.cause,
                }
                if epoch is not None else None
            ),
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    from .timeline import relative_date

    if sets:
        print("Named sets:")
        for name, entry in sets:
            member_ids = {m.get("session_id")
                          for m in entry.get("members", [])}
            count = len(member_ids)
            open_count = len(member_ids & live_ids)
            noun = "member" if count == 1 else "members"
            open_note = f" ({open_count} open now)" if open_count else ""
            updated = entry.get("updated_at")
            when = f"  updated {relative_date(updated)}" if updated else ""
            _style_print([
                ("  ", None), (name, "bold cyan"),
                (f"  {count} {noun}", None),
                (open_note, "green" if open_count else None),
                (when, "dim"),
            ])
    else:
        print("No named sets yet.")
        print("  Create one: `csb set new CSB-STACK <session> <session>`",
              file=sys.stderr)

    print()
    print("Live:")
    try:
        live_count = len(_live_session_ids(config))
    except Exception:  # noqa: BLE001 -- listing must never fail on the extras
        live_count = None
    if live_count:
        noun = "session" if live_count == 1 else "sessions"
        _style_print([
            ("  ", None), ("current", "bold cyan"),
            (f"  {live_count} {noun} open this boot", None),
            ("   csb set show current", "dim"),
        ])
    else:
        print("  current  (registry empty -- needs this release's hooks)")

    print()
    print("Boot epochs:")
    # Rows collect first so the trailing command column can align: the
    # middles vary ("in progress ..." vs "shutdown ... (cause)"), and a
    # ragged command column reads as noise (user request).
    table_rows = []
    if boot_utc is not None:
        table_rows.append(("boot", [
            (f" in progress ({_booted_label(boot_utc)})", None),
        ], "csb set show boot"))
    if history:
        from .set_render import format_local
        for i, ep in enumerate(history[:5]):
            token = "last" if i == 0 else f"last~{i}"
            table_rows.append((token, [
                (f" shutdown {format_local(ep.shutdown_utc)}", None),
                (f"  ({ep.cause})", "dim"),
            ], f"csb set show {token}"))
    if table_rows:
        mid_width = max(sum(len(text) for text, _style in mid)
                        for _token, mid, _cmd in table_rows)
        for token, mid, cmd in table_rows:
            pad = mid_width - sum(len(text) for text, _style in mid)
            _style_print(
                [("  ", None), (f"{token:7}", "bold cyan")] + mid
                + [(" " * pad + "   " + cmd, "dim")])
    if history:
        if len(history) > 5:
            # No silent caps: the table shows the 5 newest, but every
            # epoch the OS log still holds is addressable.
            deepest = len(history) - 1
            _style_print([
                (f"  ... {len(history) - 5} more addressable, through "
                 f"last~{deepest}", "dim"),
            ])
        _style_print([
            ("  epochs are also addressable by date, e.g. "
             f"`csb set show {history[0].shutdown_utc.astimezone().strftime('%Y-%m-%d')}`",
             "dim"),
        ])
    elif boot_utc is None:
        print(f"  (unavailable -- {epoch_note})")
    else:
        print(f"  last  (unavailable -- {epoch_note})")
    return 0


def _iso_z(dt) -> str:
    """Aware datetime -> the index's Z-suffixed ISO convention."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _booted_label(boot_utc) -> str:
    """Precise boot age: 'booted 12d19h ago', never a rounded phrase.

    relative_date's coarse buckets rendered a 12.8-day boot as
    "1 week ago" while roster rows said "12d17h ago" -- the mismatch
    made the window look wrong when it wasn't. Same format_gap
    vocabulary as the rows, so containment is visible at a glance.
    """
    from datetime import datetime as _dt

    from .epochs import format_gap
    return f"booted {format_gap(_dt.now(timezone.utc) - boot_utc)} ago"


def cmd_set_show(args) -> int:
    """`csb set show last` -- sessions active before the last shutdown (#61).

    Read-only reconstruction: boot/shutdown fences come from the OS event
    log live (never stored), membership from the existing index timestamps.
    The heuristic is honest about itself -- "active within the window",
    never "open" (activity is not liveness until #64 records real
    open/close events), and activity order, never open order.
    """
    # Session names are user text -- never let a cp1252/cp437 console
    # raise UnicodeEncodeError over one character (cmd_tree pattern; no
    # forced UTF-8, only tolerant errors).
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    name = getattr(args, "set_name", None)
    json_mode = getattr(args, "json", False)
    if name == "current":
        if getattr(args, "window", None) is not None:
            print(
                "`--window` narrows an epoch by activity; `current` is "
                "bounded by the boot, not a window. Use `csb set show last "
                "--window <hours>` for the epoch view.",
                file=sys.stderr,
            )
            return 2
        return _cmd_set_show_current(args, json_mode)
    if name == "boot":
        if getattr(args, "open_only", False):
            print(
                "`--open` reads the last-shutdown snapshot; `boot` is the "
                "epoch in progress -- nothing has shut down yet. Use "
                "`csb set show last --open`.",
                file=sys.stderr,
            )
            return 2
        return _cmd_set_show_boot(args, json_mode)
    if name != "last" and classify_epoch_token(name) is None:
        return _cmd_set_show_named(args, name, json_mode)

    config = _get_config(args)
    roster, code = _materialize_set_roster(config, name)
    if roster is None:
        return code

    epoch = roster["epoch"]
    if epoch is None:
        if json_mode:
            print(json.dumps(
                {"kind": "epoch", "name": "last", "epoch": None,
                 "members": [], "missing_timestamps": 0},
                indent=2,
            ))
            return 0
        print("No shutdown fence found in the event log -- nothing to "
              "reconstruct.")
        print("  Note: the System log may have rotated past the last restart.",
              file=sys.stderr)
        return 0

    # Open-at-shutdown badges + snapshot-union rows arrive FROM the
    # materializer (#80 S3a) -- `resume --set` and `--from` see the same
    # roster this command displays.
    snapshot_available = roster.get("snapshot_available", False)

    # The canonical roster is the epoch; --window (and --open) narrow what
    # is SHOWN while indices keep their canonical values (gaps, never
    # renumbering).
    all_members = roster["members"]
    display_window = getattr(args, "window", None)
    members, hidden = _filter_roster_for_display(
        all_members, epoch.shutdown_utc, display_window)
    if getattr(args, "open_only", False):
        if not snapshot_available:
            print(
                "Note: no boundary snapshot covers this epoch -- `--open` "
                "needs the live registry to have been active before the "
                "shutdown. Showing the full roster.",
                file=sys.stderr,
            )
        else:
            before = len(members)
            members = [m for m in members if m.get("open_at_shutdown")]
            hidden += before - len(members)
    missing = roster["missing_timestamps"]
    lo = roster["window_lo"]
    window_hours = roster["window_hours"]
    window_source = roster["window_source"]

    # Freshness advisory (#59 family): an index last updated before the
    # shutdown cannot contain the final pre-shutdown activity. Advisory,
    # never forced; stderr so --json stdout stays pure.
    last_scan_at = roster["last_scan_at"]
    last_scan = parse_index_ts(last_scan_at)
    if last_scan is not None and last_scan < epoch.shutdown_utc:
        from .timeline import relative_date
        print(
            f"Note: the index was last updated {relative_date(last_scan_at)} "
            "-- before this shutdown, so final pre-shutdown activity may be "
            "missing. Run `csb backup` to capture it.",
            file=sys.stderr,
        )

    if json_mode:
        payload = {
            "kind": "epoch",
            "name": roster["name"],
            "history_index": roster.get("history_index", 0),
            # H8 disclosure: what was asked vs where the fallthrough
            # settled, and every empty epoch passed on the way.
            "requested_token": roster.get("requested", roster["name"]),
            "skipped_empty": [
                {"token": sk["token"],
                 "shutdown_at": _iso_z(sk["epoch"].shutdown_utc),
                 "cause": sk["epoch"].cause}
                for sk in roster.get("skipped_empty", [])
            ],
            "fallthrough_failed": roster.get("fallthrough_failed", False),
            "fallthrough_exhausted": roster.get("fallthrough_exhausted", 0),
            "activity_floor": (
                _iso_z(roster["activity_floor"])
                if roster.get("activity_floor") is not None else None
            ),
            "nearest_working": (
                {"token": roster["nearest_working"]["token"],
                 "count": roster["nearest_working"]["count"],
                 "shutdown_at": _iso_z(
                     roster["nearest_working"]["shutdown_at"])}
                if roster.get("nearest_working") else None
            ),
            "epoch": {
                "shutdown_at": _iso_z(epoch.shutdown_utc),
                "boot_at": _iso_z(epoch.boot_utc) if epoch.boot_utc else None,
                "cause": epoch.cause,
                "window_start": _iso_z(lo),
                "window_hours": round(window_hours, 1),
                "window_source": window_source,
            },
            # `index` is the CANONICAL position -- what `csb resume last:<N>`
            # addresses -- so a narrowed view yields gaps, never a
            # renumbering. roster_size is the unfiltered total.
            "display_window_hours": display_window,
            "roster_size": len(all_members),
            "hidden_by_window": hidden,
            "snapshot_available": snapshot_available,
            "members": [
                {**{k: m[k] for k in (
                    "index", "session_id", "session_name", "project",
                    "start_folder", "started_at", "last_active_at",
                    "purged", "is_fork", "in_index", "messages",
                )},
                 "open_at_shutdown": bool(m.get("open_at_shutdown")),
                 "segment_backed": bool(m.get("segment_backed")),
                 "snapshot_only": bool(m.get("snapshot_only"))}
                for m in members
            ],
            "missing_timestamps": missing,
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    if roster.get("index_empty"):
        print("The index has no sessions -- nothing to place in the epoch.")
        guidance = _empty_state_guidance(config)
        if guidance:
            for line in guidance:
                print(line)
        else:
            print("  Run `csb backup` to index + back up, or `csb backup "
                  "--no-commit` to index without git.")
        return 0

    # H8 disclosure: every empty epoch the relative address walked past
    # renders its own header (count 0), so the user sees BOTH facts --
    # that period was empty, and here is the nearest working set.
    skipped = roster.get("skipped_empty", [])
    header = []
    for sk in skipped[:3]:
        header += epoch_header_segments(sk["epoch"], 0, sk["window_hours"],
                                        sk["window_source"],
                                        token=sk["token"])
    if skipped:
        more = f" (+{len(skipped) - 3} more empty)" if len(skipped) > 3 \
            else ""
        header.append([
            (f"-- empty{more}; showing the nearest epoch with members --",
             "dim"),
        ])
    header += epoch_header_segments(epoch, len(all_members), window_hours,
                                    window_source, token=roster["name"])
    if roster.get("fallthrough_failed"):
        n_deeper = roster.get("fallthrough_exhausted", 0)
        lead = (f"no members in any of the {n_deeper} deeper epochs either"
                if n_deeper else "nothing deeper exists")
        floor = roster.get("activity_floor")
        floor_note = (
            f" -- the index's activity reaches back to "
            f"{floor.strftime('%Y-%m-%d')}; older epochs have nothing "
            "to draw on" if floor is not None else ""
        )
        header.append([(f"({lead}{floor_note})", "dim")])
        nw = roster.get("nearest_working")
        if nw:
            noun = "session" if nw["count"] == 1 else "sessions"
            header.append([
                (f"(nearest epoch with members: {nw['token']} -- "
                 f"{nw['count']} {noun} -- csb set show {nw['token']})",
                 "dim"),
            ])
    if hidden:
        narrow_desc = (f"narrowed to {display_window:g}h before shutdown"
                       if display_window is not None else
                       "narrowed to open-at-shutdown members")
        header.append([
            (f"Showing {len(members)} of {len(all_members)} -- "
             f"{narrow_desc}. ", None),
            ("Numbers stay canonical, so gaps are expected and "
             "`csb resume last:<N>` numbers still match.", "dim"),
        ])
    if not members:
        for segs in header:
            _style_print(segs)
        print()
        if hidden:
            print(f"No sessions active within {display_window:g}h of the "
                  "shutdown.")
            print("  Tip: drop `--window` to see the whole epoch.",
                  file=sys.stderr)
        else:
            print("No sessions with activity in that window before the "
                  "shutdown.")
            print("  Tip: `--window <hours>` narrows the view.",
                  file=sys.stderr)
        return 0

    footers = []
    if missing:
        noun = "session lacks" if missing == 1 else "sessions lack"
        footers.append(
            f"Note: {missing} {noun} activity timestamps and cannot be "
            "placed in any window."
        )
    render_roster(members, header, footer_notes=tuple(footers),
                  shutdown_utc=epoch.shutdown_utc)
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

    bf_state, bf_detail = git_repo_state(claude_dir)
    if bf_state != "ok":
        if bf_state == "refused":
            print(f"Error: a git repository exists at {claude_dir} but git "
                  f"refuses it in this shell ({bf_detail.splitlines()[0] if bf_detail else 'no detail'}). "
                  f"Run `csb setup` for diagnosis.", file=sys.stderr)
        elif bf_state == "error":
            print(f"Error: git itself failed: {bf_detail}", file=sys.stderr)
        else:
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


def _split_set_token(config, token):
    """``<view-or-set>:<N>`` -> ``(prefix, index_str)``, else None.

    The ONE token grammar (#76 V1, promoted to resume): split on the
    LAST colon; the suffix must be a bare integer AND the prefix must
    resolve as a view (current/boot/last/last~K/date) or an existing
    named set. Exact tests only -- no scoring, no guessing.
    """
    prefix, sep, suffix = token.rpartition(":")
    if not (sep and suffix.isdigit() and prefix):
        return None
    if (prefix in ("current", "boot")
            or classify_epoch_token(prefix) is not None
            or session_sets.resolve_set_name(config["claude_dir"], prefix)
            is not None):
        return prefix, suffix
    return None


def _session_name_exists(conn, name: str) -> bool:
    """Exact session-name match in the index (purged rows included --
    a name the user gave ALWAYS wins the bare namespace)."""
    return conn.execute(
        "SELECT 1 FROM sessions WHERE session_name = ? LIMIT 1", (name,)
    ).fetchone() is not None


def cmd_resume(args) -> int:
    """Launch claude --resume with the full session UUID.

    Accepts every identifier `csb view` accepts (#42): UUID/prefix (the
    historical surface), exact session NAME (Claude Code's own /resume
    title semantics -- csb resolves it to the UUID, so Claude always
    receives the one format that is unconditionally direct), .jsonl path,
    directory, sesslog folder name, or free-text keyword -- plus, since
    the grammar unification, the SAME ``view-or-set:N`` tokens the
    membership verbs take: ``csb resume last:3`` is member 3 of the last
    epoch.

    Two lanes, one grammar (the promotion principle):

      bare    ``csb resume last:3``       names always win -- a session
              literally NAMED ``last:3`` resumes as itself (with a
              one-line pointer at the flag); otherwise the token fires.
      --set   ``csb resume --set last:3`` the fully-qualified lane:
              everything after the flag is set vocabulary, never a
              session name. ``--set NAME`` (no index) is the reclaim
              menu. The pre-unification forms (``--set 3``,
              ``--set NAME 3``, bare ``--set``) error with the exact
              token spelling -- bare integers were ambiguous (boot?
              last?) and are gone.
    """
    # getattr: pre-R1 call sites and test namespaces lack the attribute.
    from_set = getattr(args, "from_set", None)
    query = getattr(args, "session_id", None)
    if from_set is not None:
        config = _get_config(args)
        if from_set == "":
            print(
                "--set needs a set-family word:\n"
                "  csb resume --set last:3     member 3 of the last epoch\n"
                "  csb resume --set NAME       the reclaim menu",
                file=sys.stderr,
            )
            return 2
        if from_set.isdigit():
            print(
                f"`--set {from_set}` was ambiguous (boot? last?) and is "
                "retired -- name the view:\n"
                f"  csb resume --set last:{from_set}\n"
                f"  csb resume --set boot:{from_set}",
                file=sys.stderr,
            )
            return 2
        if query is not None:
            if query.isdigit():
                print(
                    f"`--set {from_set} {query}` is retired -- use the "
                    f"token form:\n  csb resume --set {from_set}:{query}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Unexpected argument '{query}' after --set "
                    f"{from_set}.",
                    file=sys.stderr,
                )
            return 2
        token = _split_set_token(config, from_set)
        if token is not None:
            return _cmd_resume_set(args, token[0], token[1])
        # Token-shaped but the prefix resolves to nothing: name the
        # PREFIX as the missing set, not the whole token (sweep R1).
        bad_prefix, bad_sep, bad_suffix = from_set.rpartition(":")
        if bad_sep and bad_suffix.isdigit() and bad_prefix:
            print(
                f"No set named '{bad_prefix}' (from token '{from_set}'). "
                "`csb set list` shows named sets and addressable epochs.",
                file=sys.stderr,
            )
            return 1
        return _cmd_resume_set(args, from_set, None)  # the reclaim menu
    if query is None:
        print(
            "csb resume needs a session query.\n"
            "  csb resume <query>          resume a session\n"
            "  csb resume last:3           member 3 of the last epoch\n"
            "  csb resume --set NAME       the reclaim menu",
            file=sys.stderr,
        )
        return 2
    # The flagless ladder: (1) an exact session name always wins;
    # (2) a set-vocabulary token; (3) the ordinary multi-modal search.
    token_config = _get_config(args)
    token = _split_set_token(token_config, query)
    if token is not None:
        conn = open_db(token_config["index_path"])
        init_schema(conn)
        shadowed = _session_name_exists(conn, query)
        conn.close()
        if not shadowed:
            return _cmd_resume_set(args, token[0], token[1])
        print(
            f"note: resolved as the session named '{query}'; for member "
            f"{token[1]} of '{token[0]}' use: csb resume --set {query}",
            file=sys.stderr,
        )
    return _resume_query(args, query)


def _materialize_current_roster(config) -> dict:
    """The `current` set: this boot's Live Session Registry entries (#64).

    True open order (started_at ASC -- the registry records real opens,
    not activity), canonical 1-based indices, and a two-tier liveness
    verdict per member:

      running        a live process provably belongs to this session
      no exit observed   registry says open; no process proof (a fresh
                         session argv cannot attribute, or a crash this
                         boot -- honest wording covers both)

    Never guesses from activity timestamps: the process-table probe
    measured a 25% false-negative rate for that on real data.
    """
    from . import live_registry, liveness

    claude_dir = config["claude_dir"]
    entries = live_registry.read_entries(claude_dir)
    boot_utc = live_registry.current_boot_utc()
    this_boot, _pre = live_registry.split_by_boot(entries, boot_utc)

    scan = liveness.scan() if this_boot else liveness.LiveScan(ok=True)

    conn = open_db(config["index_path"])
    init_schema(conn)
    members = []
    pairs = []  # (member, entry) for pid-claim arbitration (#72)
    for index, entry in enumerate(this_boot, start=1):
        sid = entry["session_id"]
        session = get_session(conn, sid) or {}
        session_name = session.get("session_name")
        pid = liveness.verify_entry(scan, entry, session_name)
        members.append({
            "index": index,
            "session_id": sid,
            "session_name": session_name,
            "project": session.get("project"),
            # A just-started session may not be indexed yet -- the
            # registry entry's cwd is then the only locator we have.
            "start_folder": session.get("start_folder") or entry.get("cwd"),
            "started_at": entry.get("started_at"),
            "last_active_at": session.get("last_active_at"),
            "jsonl_path": session.get("jsonl_path"),
            "jsonl_mtime": session.get("jsonl_mtime"),
            "purged": bool(session.get("deleted_at")),
            "is_fork": bool(session.get("is_fork")),
            "in_index": bool(session),
            "source": entry.get("source") or "",
            "messages": session.get("message_count"),
            "live_status": "running" if pid is not None else "unverified",
            "pid": pid,
            # RECORDED vs VERIFIED are different facts, and collapsing
            # them costs real diagnosis time: `pid: null` alone cannot
            # distinguish "the hook never captured a host" from
            # "captured one, but it no longer verifies" -- which point
            # at opposite bugs (capture path vs a stale/transient pid).
            "recorded_pid": entry.get("pid"),
            "pid_at": entry.get("pid_at"),
        })
        pairs.append((members[-1], entry))
    liveness.arbitrate_pid_claims(pairs)
    conn.close()
    return {
        "kind": "current",
        "name": "current",
        "epoch": None,
        "boot_utc": boot_utc,
        "members": members,
        "missing_timestamps": 0,
        "scan_ok": scan.ok,
        "bare_processes": len(scan.bare_pids),
        "window_hours": None, "window_source": None, "last_scan_at": None,
    }


def _materialize_boot_roster(config):
    """The `boot` set: everything active since this boot (Phase 6 R1).

    The epoch in progress -- the view between `current` (open right now)
    and `last` (the previous epoch). Cross-platform by construction: the
    only boundary it needs is the current boot instant, never fence
    history. Returns ``(roster, 0)`` or ``(None, exit_code)``.

    Tier inference per member (the R1 DWP's D2 table):
      in this-boot registry + process proof  -> running
      in this-boot registry, no proof        -> unverified
      absent from registry, hooks active     -> exited (its close ERASED
                                                the entry -- an observed
                                                exit, hence the tag)
      absent, hooks not provably active      -> None (unadorned row --
                                                never a guessed tag)
    """
    from datetime import datetime as _dt

    from . import live_registry, liveness

    claude_dir = config["claude_dir"]
    boot_utc = live_registry.current_boot_utc()
    if boot_utc is None:
        print(
            "Cannot determine the current boot time on this system -- the "
            "`boot` view is defined by it. (`current` and `last` may still "
            "work.)",
            file=sys.stderr,
        )
        return None, 1
    now_utc = _dt.now(timezone.utc)

    entries = live_registry.read_entries(claude_dir)
    this_boot, _pre = live_registry.split_by_boot(entries, boot_utc)
    registry_ids = {e["session_id"]: e for e in this_boot}
    snapshot = live_registry.read_snapshot(claude_dir)
    snap_captured = live_registry.parse_entry_ts(
        (snapshot or {}).get("captured_at"))
    hooks_active = bool(this_boot) or (
        snap_captured is not None and snap_captured >= boot_utc)

    scan = liveness.scan() if this_boot else liveness.LiveScan(ok=True)

    conn = open_db(config["index_path"])
    init_schema(conn)
    rows = conn.execute(
        "SELECT session_id, session_name, project, start_folder,"
        "       started_at, last_active_at, jsonl_path, jsonl_mtime,"
        "       deleted_at, is_fork, message_count"
        "  FROM sessions"
    ).fetchall()
    # Segment-overlap membership applies to the boot window identically
    # (#80 S7): [boot, now] is just an epoch still in progress.
    segments = _load_activity_segments(conn)
    members, missing = build_roster(rows, boot_utc, now_utc,
                                    segments=segments)

    indexed_ids = set()
    pairs = []  # (member, entry) for pid-claim arbitration (#72)
    for m in members:
        indexed_ids.add(m["session_id"])
        entry = registry_ids.get(m["session_id"])
        m["recorded_pid"] = entry.get("pid") if entry is not None else None
        m["pid_at"] = entry.get("pid_at") if entry is not None else None
        if entry is not None:
            pid = liveness.verify_entry(scan, entry, m["session_name"])
            m["live_status"] = "running" if pid is not None else "unverified"
            m["pid"] = pid
            pairs.append((m, entry))
        elif hooks_active:
            m["live_status"] = "exited"
            m["pid"] = None
        else:
            m["live_status"] = None
            m["pid"] = None

    # Registry-only appendix: open sessions csb has not indexed yet (same
    # union `current` shows), appended AFTER the activity-ordered members
    # so their churn never renumbers index-backed rows.
    index = len(members)
    for entry in this_boot:
        sid = entry["session_id"]
        if sid in indexed_ids:
            continue
        session = get_session(conn, sid) or {}
        pid = liveness.verify_entry(scan, entry, session.get("session_name"))
        index += 1
        members.append({
            "index": index,
            "session_id": sid,
            "session_name": session.get("session_name"),
            "project": session.get("project"),
            "start_folder": session.get("start_folder") or entry.get("cwd"),
            "started_at": entry.get("started_at"),
            "last_active_at": session.get("last_active_at"),
            "jsonl_path": session.get("jsonl_path"),
            "jsonl_mtime": session.get("jsonl_mtime"),
            "purged": bool(session.get("deleted_at")),
            "is_fork": bool(session.get("is_fork")),
            "in_index": bool(session),
            "messages": session.get("message_count"),
            "live_status": "running" if pid is not None else "unverified",
            "pid": pid,
            "recorded_pid": entry.get("pid"),
            "pid_at": entry.get("pid_at"),
        })
        pairs.append((members[-1], entry))
    liveness.arbitrate_pid_claims(pairs)
    conn.close()

    return {
        "kind": "boot",
        "name": "boot",
        "epoch": None,
        "boot_utc": boot_utc,
        "as_of": now_utc,
        "members": members,
        "missing_timestamps": missing,
        "hooks_active": hooks_active,
        "scan_ok": scan.ok,
        "bare_processes": len(scan.bare_pids),
        "window_hours": None, "window_source": None, "last_scan_at": None,
    }, 0


def _live_session_ids(config) -> set:
    """Session ids open THIS boot per the registry (the liveness rule)."""
    return {e["session_id"] for e in _live_entries(config)}


def _live_entries(config) -> list:
    """This boot's registry entries (full dicts -- pid included, #72)."""
    from . import live_registry

    entries = live_registry.read_entries(config["claude_dir"])
    boot_utc = live_registry.current_boot_utc()
    this_boot, _pre = live_registry.split_by_boot(entries, boot_utc)
    return this_boot


def _filter_roster_for_display(members, shutdown_utc, window_hours):
    """Narrow a roster for DISPLAY, preserving canonical index numbers.

    Returns ``(kept, hidden_count)``. Members keep the ``index`` they were
    assigned in the canonical roster, so a narrowed view shows gaps
    (``1, 3, 5``) rather than renumbering -- the gaps are the honest
    signal that rows were filtered out, and they keep
    `csb resume last:3` meaning what the reader just saw.

    A window wider than the epoch simply matches everything: there is
    nothing before the previous fence that belongs to *this* epoch.
    """
    if window_hours is None or shutdown_utc is None:
        return members, 0
    from datetime import timedelta

    cutoff = shutdown_utc - timedelta(hours=window_hours)
    kept = []
    for member in members:
        la = parse_index_ts(member["last_active_at"])
        if la is not None and la >= cutoff:
            kept.append(member)
    return kept, len(members) - len(kept)


def _cmd_resume_set(args, set_name: str, index_token) -> int:
    """Reclaim member N of a set -- `csb resume last:3` / `--set NAME:3`.

    Reclaiming a set is a loop the USER drives: read the roster, open a
    terminal where you want it, run this. csb supplies the addressing,
    never the window -- it cannot know which emulator, tab, or placement
    you meant, and guessing wrong is worse than not guessing, because
    then you have to close what you did not want.

    Indices are positions in the FULL roster (see
    :func:`_materialize_set_roster`), so a number means the same session
    today, tomorrow, and after the next restart. ``index_token=None`` is
    the RECLAIM MENU: what is still available to reopen (available = not
    currently open per the registry; exiting a session returns it,
    because a clean close removes its entry -- liveness, not progress).
    """
    if index_token is None:
        return _reclaim_menu(args, set_name)

    try:
        index = int(index_token)
    except ValueError:
        print(
            f"'{index_token}' is not a roster number. Use the number shown "
            f"by `csb set show {set_name}`, in the token form: "
            f"`csb resume --set {set_name}:<N>`.",
            file=sys.stderr,
        )
        return 2
    if index < 1:
        print("Roster numbers start at 1.", file=sys.stderr)
        return 2

    config = _get_config(args)
    roster, code = _materialize_set_roster(config, set_name)
    if roster is None:
        return code

    members = roster["members"]
    if not members:
        if roster["kind"] == "epoch" and roster["epoch"] is None:
            print("No shutdown fence found in the event log -- no epoch to "
                  "resume from.", file=sys.stderr)
        else:
            print(f"Set '{roster['name']}' has no members.", file=sys.stderr)
        return 1
    if index > len(members):
        noun = "member" if len(members) == 1 else "members"
        print(
            f"Set '{roster['name']}' has {len(members)} {noun} -- valid "
            f"indices are 1-{len(members)}.",
            file=sys.stderr,
        )
        return 1

    member = members[index - 1]
    if not member.get("in_index", True):
        if roster["kind"] == "current":
            # A live-registry member that csb has not indexed yet (started
            # after the last backup). claude --resume works regardless of
            # csb's index -- synthesize the minimal row and launch.
            return _resume_session_row(args, config, {
                "session_id": member["session_id"],
                "session_name": member.get("session_name"),
                "start_folder": member.get("start_folder"),
                "jsonl_path": None,
                "deleted_at": None,
            })
        print(
            f"Member {index} ({member['session_id']}) is no longer in the "
            "index -- purged beyond recovery, or the index needs "
            "`csb update rebuild-index`.",
            file=sys.stderr,
        )
        return 1

    conn = open_db(config["index_path"])
    init_schema(conn)
    session = get_session(conn, member["session_id"])
    conn.close()
    if session is None:
        print(f"Member {index} ({member['session_id']}) could not be loaded "
              "from the index.", file=sys.stderr)
        return 1
    return _resume_session_row(args, config, session)


def _reclaim_menu(args, set_name: str) -> int:
    """Bare `csb resume --set <name>`: what is still available to reopen.

    A member is AVAILABLE when it is not currently open per the Live
    Session Registry -- so exiting a session puts it back on this list,
    and resuming one (however: csb, bare `claude --resume`, anything
    that fires SessionStart) removes it. Indices stay canonical, so the
    gaps ARE the progress indicator and `csb resume --set NAME:N`
    means the same thing before and after.
    """
    config = _get_config(args)
    roster, code = _materialize_set_roster(config, set_name)
    if roster is None:
        return code
    members = roster["members"]
    if not members:
        print(f"Set '{roster['name']}' has no members.", file=sys.stderr)
        return 1

    live_ids = _live_session_ids(config)
    available = [m for m in members if m["session_id"] not in live_ids]
    open_count = len(members) - len(available)

    from .set_render import render_roster as _render

    noun = "member" if len(members) == 1 else "members"
    header = [[
        (f"Set '{roster['name']}'", "bold"),
        (f" -- {len(members)} {noun}: ", None),
        (f"{open_count} currently open", "green" if open_count else None),
        (", ", None),
        (f"{len(available)} available to reclaim", "bold" if available else None),
    ]]
    if not available:
        for segs in header:
            _style_print(segs)
        print()
        print(f"All {len(members)} {noun} are open -- nothing to reclaim.")
        return 0
    if open_count:
        header.append([
            (f"(open members keep their numbers -- gaps are progress; "
             f"`csb set show {roster['name']}` shows everyone)", "dim"),
        ])
    for m in available:
        m["hint_override"] = f"csb resume --set {roster['name']}:{m['index']}"
    _render(available, header, shutdown_utc=None)
    return 0


def _resume_query(args, query: str) -> int:
    """Resolve ``query`` to a session and hand off to the launcher."""
    config = _get_config(args)
    conn = open_db(config["index_path"])
    init_schema(conn)

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
    return _resume_session_row(args, config, session)


# Passthrough flags that provably produce a NEW session id, making a
# duplicate launch deliberate branching rather than an accidental second
# client on one transcript (#67). Keyed to session-ID divergence, NOT to
# passthrough presence: `--model opus` leaves the collision intact, and
# unknown future flags fail safe (still warn).
SAFE_DUPLICATE_FLAGS = {"--fork-session"}


def _resolve_live_decision(args, session: dict, name: str, pid) -> str:
    """Decide what to do when `csb resume` targets an ALREADY-LIVE session.

    Two clients appending to one transcript is the silent-interleaving
    hazard #67 documents (hit live on real data). Advisory, never
    blocking; mirrors _resolve_pruned_decision's precedence exactly:
    explicit flag (--allow-live / --no-allow-live) > TTY prompt >
    non-TTY warn-and-proceed (resume must stay scriptable -- warning to
    stderr, launch anyway).

    Returns "proceed" or "abort".
    """
    if any(flag in SAFE_DUPLICATE_FLAGS
           for flag in _passthrough_args(args)):
        return "proceed"  # forking a live session is the deliberate path
    where = f" (pid {pid})" if pid else ""
    if getattr(args, "no_allow_live", False):
        print(
            f"Session '{name}' appears to be open already{where}. "
            "--no-allow-live set -- not resuming.",
            file=sys.stderr,
        )
        print(
            "Branch from it instead: append `-- --fork-session`.",
            file=sys.stderr,
        )
        return "abort"
    if getattr(args, "allow_live", False):
        return "proceed"
    if not sys.stdin.isatty():
        print(
            f"Warning: session '{name}' appears to be open already{where} "
            "-- a second client would interleave into one transcript. "
            "Proceeding (non-interactive). Use `-- --fork-session` to "
            "branch, or --no-allow-live to refuse.",
            file=sys.stderr,
        )
        return "proceed"
    try:
        answer = input(
            f"Session '{name}' appears to be open already{where}. Resume "
            "a second client anyway? A branch (`-- --fork-session`) is "
            "usually what you want. [y/N] "
        )
    except (EOFError, KeyboardInterrupt):
        print()
        return "abort"
    return "proceed" if answer.strip().lower() in ("y", "yes") else "abort"


def _live_pid_for(config, session_id: str, session_name) -> Optional[int]:
    """PID (or 0) when this session is live; None when nothing says so.

    REGISTRY-FIRST, by design and by cost: the registry check is a file
    listing (~free) and gates everything; the process scan (a 1-2s WMI
    query on Windows) runs ONLY to verify an actual registry hit and
    fetch its PID. An early version scanned unconditionally -- taxing
    every resume with seconds of latency to cover only the
    hookless-install case, which degrades without the guard anyway
    ("hookless installs degrade honestly").

    A registry hit with no process proof returns 0 -- "registered open
    this boot, no process proof", covering both the fresh session argv
    cannot attribute and a crash this boot (the advisory wording covers
    both; forking a crashed session is harmless). Failures degrade to
    None -- liveness must never break a resume. Entries that recorded
    their host pid verify by that pid alone (#72).
    """
    try:
        entry = next((e for e in _live_entries(config)
                      if e["session_id"] == session_id), None)
        if entry is None:
            return None
        from . import liveness

        scan = liveness.scan()
        pid = liveness.verify_entry(scan, entry, session_name)
        return pid if pid is not None else 0
    except Exception:  # noqa: BLE001 -- advisory only
        return None


def _resume_session_row(args, config, session) -> int:
    """Launch ``claude --resume`` for an already-resolved session (#63).

    Extracted so `csb resume <query>` and `csb resume last:<N>` share one
    launcher rather than two. Everything that makes resume more than a
    subprocess call lives here -- the pruned-session restore offer, the
    live-session guard (#67), the transcript preflight, cwd derivation,
    and the passthrough forwarding -- so index addressing inherits all
    of it by construction instead of by remembering to reimplement it.

    csb NEVER spawns a terminal: this launches in the one it was invoked
    from, exactly as it always has.
    """
    from .pathkit import derive_start_at

    full_id = session["session_id"]
    name = session.get("session_name") or "(unnamed)"

    # #67: warn before inviting a second client onto one transcript.
    live_pid = _live_pid_for(config, full_id, session.get("session_name"))
    if live_pid is not None:
        if _resolve_live_decision(args, session, name, live_pid) == "abort":
            return 0

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
    #
    # Resolve the binary via PATH x PATHEXT BEFORE spawning (#55): a bare
    # name goes to Win32 CreateProcess, which searches PATH for .exe ONLY
    # -- an npm-shim install (claude.cmd, no claude.exe) is invisible to
    # it even though every shell resolves it via PATHEXT. shutil.which()
    # returns the shim's full path, which CreateProcess CAN run (a .cmd
    # by full path routes through %COMSPEC% automatically). On POSIX,
    # which() mirrors execvp's PATH walk -- identical outcome, resolved
    # a moment earlier. which() returning None is the one case where
    # "not found in PATH" is actually true.
    import subprocess
    claude_exe = shutil.which("claude")
    if claude_exe is None:
        print("Error: 'claude' command not found in PATH.", file=sys.stderr)
        print(f"Run manually: {launch_str}", file=sys.stderr)
        return 1
    claude_cmd[0] = claude_exe
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
        #   (b) the resolved binary vanished between which() and spawn (rare)
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
    no_index = getattr(args, "no_index", False)
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
    if (not has_wildcard and deleted_filter == "active"
            and exact_value and not Path(exact_value).exists()):
        # Warn -- but NEVER stop (0.7.2). This used to return [] (or fall
        # back to a BROAD term search, silently abandoning the path scope)
        # without ever querying the index. Existence is a confidence hint,
        # not a filter: the index is the only place a vanished folder's
        # history lives, and "the folder is gone" describes the exact
        # situation the recovery flow exists for. The SQL pass below
        # handles missing paths fine -- its own comment has said so all
        # along.
        print(
            f"[warning] '{pattern_input}' (resolved: {exact_value}) does not "
            f"exist on disk; searching the index anyway.",
            file=sys.stderr,
        )

    # Step 1: Filesystem scan (only when pattern resolves to a concrete path).
    # For wildcard patterns we skip the filesystem step -- scan_for_path doesn't
    # speak wildcards. SQLite covers these via the LIKE pattern.
    # `include_descendants` must be threaded through: scan_for_path's prefix
    # test on the sanitized project name treats `C--code-<child>` as a match
    # for `C:\code`, so calling it unconditionally made -D report sessions
    # whose only connection was a descendant start folder -- while the SQL
    # half of the same query correctly excluded them.
    sessions_fs: list = []
    if not has_wildcard and exact_value:
        sessions_fs = scan_for_path(config["claude_dir"], exact_value,
                                    include_descendants=include_descendants)

    # Step 2: SQLite directory match (unless -NI)
    sql_results: list = []
    if not no_index and (exact_value is not None or like_match is not None):
        try:
            conn = open_db(config["index_path"])
            init_schema(conn)
            # top_n=None: NO top-N gate on MATCHING (#56).
            #
            # `--top N` / `display_top_folders` govern how many folders are
            # DISPLAYED per session. Passing that same number here also
            # restricted which folders could make a session match, so a
            # session only qualified if the searched path was among its
            # busiest few. That was invisible while a session recorded ~1
            # folder; now that tool-call harvesting records every folder a
            # session touched (50-80 for an active session), the gate hid
            # the overwhelming majority of them -- `csb scan -d
            # C:\Users\Extreme` returned 4 sessions where 14 genuinely
            # matched. Whether a session *matches* a path and how many of
            # its folders we *print* are different questions.
            sql_results = find_sessions_by_directory(
                conn, exact_value, like_match, like_exclude, None,
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

    # Build human-readable scope label.
    #
    # "under X" means X and everything below it (-d, -s, bare). -D is a
    # different question -- "was this EXACT folder touched?" -- and saying
    # "under" for it misreports the search that ran, which matters most
    # when the two return different counts and the user is trying to work
    # out why.
    scope_word = "under" if include_descendants else "in exactly"
    if bare_mode:
        scope_label = f"under {resolved_path}"
    elif has_wildcard:
        scope_label = f"matching pattern {resolved_path}"
        if not include_descendants:
            scope_label += " (no descendants)"
        if term:
            scope_label += f" filtered by '{term}'"
    else:
        scope_label = f"{scope_word} {resolved_path}"
        if term:
            scope_label += f" filtered by '{term}'"

    scope_label = _decorate_scope_label(scope_label, deleted_filter)

    return _render_scan_results(
        results, args, config, scope_label=scope_label, quiet=quiet,
        deleted_filter=deleted_filter, path_mode=True,
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
    path_mode: bool = False,
) -> int:
    """Sort, trim, and render scan results. Shared by all scan modes.

    When ``--restore`` is set (Phase 3), delegate to the bulk-restore path
    instead of rendering. The scope/filter selection happens upstream --
    this function just dispatches.
    """
    no_index = getattr(args, "no_index", False)
    noun = _session_noun(deleted_filter)
    as_json = bool(args.__dict__.get("json"))

    if not quiet and not as_json:
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
        if as_json:
            print("[]")  # machine-readable empty -- no prose on stdout
            return 0
        print(f"  No {noun} found.")
        if no_index:
            # Be specific for the combination that CANNOT match rather than
            # offering a generic tip. `--deleted` looks only at the index,
            # and -NI is the flag that switches the index off, so the query
            # is structurally unmatchable -- not merely unlucky.
            if deleted_filter == "deleted":
                print("  Note: -NI cannot find deleted sessions -- they exist "
                      "only in the index, which -NI bypasses. Drop -NI.")
            else:
                print("  Tip: try without -NI to also search the index.")
        # Empty-state diagnosis + redirection (#52). Active scope only --
        # deleted-scope queries already imply the user knows what they're
        # looking for -- and suppressed for --quiet consumers.
        if deleted_filter == "active" and not quiet:
            for line in _empty_state_guidance(config, path_mode=path_mode):
                print(line)
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

"""
Git operations -- manages the ~/.claude git repository for backup.

Handles add, commit, restore from history, and status queries.
All operations shell out to the `git` CLI for maximum compatibility.

Commit separation follows the claude-cleanup model:
  - NOISE: auto-generated transient state (sessions, caches, telemetry)
  - USER: hand-edited configs and customizations (agents, skills, settings)
These are always committed separately with --no-gpg-sign for unattended use.

The restore path is byte-pure end to end:
  - git is invoked with -c core.autocrlf=false / core.eol=lf / core.safecrlf=false
    when reading bytes for restore (defeats per-file autocrlf smudge on checkout)
  - subprocess captures raw bytes (text=False) for restore reads
  - destination is written with write_bytes (no text-mode translation)
  - path arguments to `git show <commit>:<path>` are normalized to forward
    slashes -- git does not accept backslash separators in <path> on Windows
  - on backup, an idempotent .gitattributes block is maintained so future
    commits never receive autocrlf normalization regardless of host config
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Union

from .pathkit import ClaudePaths


# ── File classification ─────────────────────────────────────────────
# Mirrors dz claude-cleanup categories exactly.

NOISE_DIRS = [
    "debug",
    "file-history",
    "paste-cache",
    "projects",         # session JSONL transcripts -- the most important data to preserve
    "session-env",      # session environment data
    "session-states",
    "sessions",
    "shell-snapshots",
    "telemetry",
    "logs",
    "tasks",
]

NOISE_FILES = [
    "cache/changelog.md",
    ".credentials.json",
    "history.jsonl",
    "mcp-needs-auth-cache.json",
]

USER_DIRS = [
    "agents",
    "backups",
    "commands",
    "image-cache",
    "plans",
    "plugins",
    "private",
    "sesslogs",
    "skills",
    "todos",
]

USER_FILES = [
    "CLAUDE.md",
    "settings.json",
    "settings.local.json",
]

NOISE_COMMIT_MSG = "~/.claude noise: sync transient state files"
USER_COMMIT_MSG = "~/.claude user: sync configs, skills, session logs, and plugins"


# ── Core git helpers ────────────────────────────────────────────────

def run_git(claude_dir: str, *args, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in the claude_dir repository."""
    cmd = ["git", "-C", claude_dir] + list(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
    )


def is_git_repo(claude_dir: str) -> bool:
    """Check if claude_dir is a git repository."""
    result = run_git(claude_dir, "rev-parse", "--is-inside-work-tree", check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def git_status(claude_dir: str) -> str:
    """Return git status output."""
    result = run_git(claude_dir, "status", "--porcelain", check=False)
    return result.stdout


# ── Staging helpers ─────────────────────────────────────────────────

def _stage_paths(claude_dir: str, dirs: list[str], files: list[str]):
    """Stage specific directories and files within claude_dir."""
    for dirname in dirs:
        target = Path(claude_dir) / dirname
        if target.exists():
            run_git(claude_dir, "add", str(target), check=False)

    for filename in files:
        target = Path(claude_dir) / filename
        if target.exists():
            run_git(claude_dir, "add", str(target), check=False)


def git_stage_noise(claude_dir: str):
    """Stage noise-category files (transient state, session data)."""
    _stage_paths(claude_dir, NOISE_DIRS, NOISE_FILES)


def git_stage_user(claude_dir: str):
    """Stage user-category files (configs, skills, agents, sesslogs)."""
    _stage_paths(claude_dir, USER_DIRS, USER_FILES)


def git_add_session_files(claude_dir: str, paths: list[str]):
    """Stage specific session files for commit."""
    if not paths:
        return
    batch_size = 50
    for i in range(0, len(paths), batch_size):
        batch = paths[i:i + batch_size]
        run_git(claude_dir, "add", "--", *batch, check=False)


# ── Commit helpers ──────────────────────────────────────────────────

def _has_staged_changes(claude_dir: str) -> bool:
    """Check if there are staged changes ready to commit."""
    result = run_git(claude_dir, "diff", "--cached", "--quiet", check=False)
    return result.returncode != 0


def _get_head_hash(claude_dir: str) -> Optional[str]:
    """Get current HEAD commit hash."""
    result = run_git(claude_dir, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def git_commit(claude_dir: str, message: str) -> Optional[str]:
    """
    Commit staged changes unsigned. Returns commit hash, or None if nothing to commit.

    Always uses --no-gpg-sign for unattended operation (hooks, cron, Task Scheduler).
    """
    if not _has_staged_changes(claude_dir):
        return None

    result = run_git(
        claude_dir, "commit", "--no-gpg-sign", "-m", message,
        check=False,
    )
    if result.returncode != 0:
        return None

    return _get_head_hash(claude_dir)


def git_commit_noise(claude_dir: str, message: str = "") -> Optional[str]:
    """Stage and commit noise files. Returns commit hash or None."""
    git_stage_noise(claude_dir)
    return git_commit(claude_dir, message or NOISE_COMMIT_MSG)


def git_commit_user(claude_dir: str, message: str = "") -> Optional[str]:
    """Stage and commit user files. Returns commit hash or None."""
    git_stage_user(claude_dir)
    return git_commit(claude_dir, message or USER_COMMIT_MSG)


# ── History / restore helpers ───────────────────────────────────────

def git_log_for_file(claude_dir: str, file_path: str, limit: int = 5) -> list[dict]:
    """Get git log entries for a specific file."""
    result = run_git(
        claude_dir,
        "log", f"-{limit}",
        "--pretty=format:%H|%aI|%s",
        "--follow",
        "--", file_path,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []

    entries = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split("|", 2)
        if len(parts) == 3:
            entries.append({
                "commit": parts[0],
                "date": parts[1],
                "message": parts[2],
            })
    return entries


def _normalize_git_path(file_path: Union[str, Path]) -> str:
    """
    Normalize a path argument for use in ``git show <commit>:<path>``.

    Git does NOT accept backslash separators in the ``<path>`` half of the
    ``<commit>:<path>`` revision syntax -- on Windows, ``git show HEAD:foo\\bar``
    silently returns nothing. Always convert to forward slashes before passing
    to git. Strips leading separators too -- repo-relative paths are anchored
    at the worktree root and never start with ``/``.
    """
    s = str(file_path).replace("\\", "/")
    return s.lstrip("/")


# Cache: claude_dir -> "" (claude_dir IS repo root) or "some/prefix/" (claude_dir
# is a subdir at that prefix from the repo root). Keyed by realpath so we treat
# symlinks and junctions consistently. Populated lazily on first call.
_REPO_PREFIX_CACHE: dict = {}


def _claude_dir_prefix(claude_dir: str) -> str:
    """
    Return the repo-root-relative prefix for paths INSIDE claude_dir.

    When ``~/.claude/`` is the git repo root (the README's recommended
    setup), this returns ``""`` -- paths from git output need no
    translation. When the repo is one level up (e.g. ``~/`` with
    ``.claude/`` as a tracked subdir), this returns ``".claude/"`` so
    callers can strip it before passing paths back to git via
    ``-C claude_dir``.

    Cached per claude_dir realpath (one ``git rev-parse`` per process
    per repo). Returns ``""`` on any error -- safe degradation matches
    the original behavior.
    """
    import os
    key = os.path.realpath(claude_dir)
    if key in _REPO_PREFIX_CACHE:
        return _REPO_PREFIX_CACHE[key]
    try:
        result = run_git(claude_dir, "rev-parse", "--show-prefix", check=False)
        prefix = result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        prefix = ""
    # rev-parse --show-prefix returns "" if claude_dir is the repo root,
    # or e.g. ".claude/" if claude_dir is a subdir. Always trailing-slash
    # form on success (we trust git's output).
    _REPO_PREFIX_CACHE[key] = prefix
    return prefix


def _to_claude_dir_relative(claude_dir: str, repo_relative_path: str) -> str:
    """
    Translate a path returned by git (repo-root-relative) into a
    claude_dir-relative path ready to pass to a ``-C claude_dir``
    git pathspec (e.g. ``git log -- <path>``).

    When the prefix is empty (claude_dir IS the repo root), this is a
    no-op. When the prefix is non-empty and the path starts with it,
    strip it. When the path doesn't start with the prefix (e.g., it's
    outside claude_dir for some reason), pass it through unchanged --
    the caller's git invocation will fail loudly rather than silently
    look at the wrong path.
    """
    prefix = _claude_dir_prefix(claude_dir)
    if prefix and repo_relative_path.startswith(prefix):
        return repo_relative_path[len(prefix):]
    return repo_relative_path


def _to_repo_relative(claude_dir: str, claude_dir_relative_path: str) -> str:
    """
    Translate a claude_dir-relative path INTO a repo-root-relative one,
    suitable for ``git show <commit>:<path>``.

    The asymmetry: ``git log -- <pathspec>`` interprets pathspec
    relative to the working dir (so `-C claude_dir + projects/foo`
    works), but ``git show <commit>:<path>`` ALWAYS interprets path
    relative to the repo root (regardless of ``-C``). So callers of
    ``git show`` must prepend the claude_dir->repo prefix.

    When claude_dir IS the repo root, prefix is empty -> no-op. When
    the path already starts with the prefix (caller passed a repo-
    relative path by mistake), pass through unchanged (no double-
    prefix).
    """
    prefix = _claude_dir_prefix(claude_dir)
    if not prefix:
        return claude_dir_relative_path
    if claude_dir_relative_path.startswith(prefix):
        return claude_dir_relative_path
    return prefix + claude_dir_relative_path


def git_show_file(claude_dir: str, commit: str, file_path: Union[str, Path]) -> Optional[str]:
    """
    Retrieve file content from a specific git commit as a decoded string.

    Note: returns universal-newline-translated text. For byte-pure restore use
    ``git_show_file_bytes`` instead. This function is kept for non-restore
    callers that want the convenience of a string.
    """
    # `git show <commit>:<path>` always interprets <path> as REPO-relative
    # (regardless of `-C`). When claude_dir is a subdir of the repo (e.g.,
    # ~/.claude/ inside a ~/ git repo), prepend the repo-to-claude_dir
    # prefix so the path resolves correctly. No-op when claude_dir IS the
    # repo root.
    norm = _to_repo_relative(claude_dir, _normalize_git_path(file_path))
    result = run_git(
        claude_dir,
        "show", f"{commit}:{norm}",
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    return None


def git_show_file_bytes(
    claude_dir: str, commit: str, file_path: Union[str, Path]
) -> Optional[bytes]:
    """
    Retrieve file content from a specific git commit as raw bytes.

    Runs git with ``-c core.autocrlf=false -c core.eol=lf -c core.safecrlf=false``
    so that per-file autocrlf smudging on checkout cannot mutate the bytes,
    and captures stdout in binary mode (``text=False``) to bypass Python's
    universal-newline decoding. The result is the exact bytes of the blob
    stored in git.

    Returns None if the file is not present in the given commit (or git
    returns non-zero).
    """
    # See git_show_file: the <path> in `git show <commit>:<path>` is
    # REPO-relative, so prepend the claude_dir->repo prefix when needed.
    norm = _to_repo_relative(claude_dir, _normalize_git_path(file_path))
    result = subprocess.run(
        [
            "git", "-C", claude_dir,
            "-c", "core.autocrlf=false",
            "-c", "core.eol=lf",
            "-c", "core.safecrlf=false",
            "show", f"{commit}:{norm}",
        ],
        capture_output=True,
        text=False,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    return None


def git_last_commit_time(
    claude_dir: str, commit: str, file_path: Union[str, Path]
) -> Optional[float]:
    """Author time (epoch) of the last commit touching ``file_path`` at or
    before ``commit``.

    Used by the restore timestamp-fidelity layer (#40) as the mtime fallback
    for files with no internal timestamps (session-states, file-history,
    todos, logger text channels): the backup nearest the last modification
    is a principled stand-in for the file's true mtime, which git does not
    store.
    """
    norm = _to_repo_relative(claude_dir, _normalize_git_path(file_path))
    result = run_git(
        claude_dir,
        "log", "-1", "--format=%at", commit, "--", norm,
        check=False,
    )
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    try:
        return float(out.splitlines()[0].strip())
    except ValueError:
        return None


def git_find_jsonl_by_uuid(claude_dir: str, uuid: str) -> list[str]:
    """
    Find every distinct path under ``projects/*/<uuid>.jsonl`` that git has
    ever tracked, across all branches.

    Used as a fallback when csb's DB has no row for the session -- after
    ``csb update rebuild-index`` (which only sees current on-disk state), on a fresh
    machine, or for sessions committed by something other than csb. The git
    history is authoritative for "did this UUID ever exist as a session".

    The pathspec uses git's ``:(glob)`` magic so the ``*`` matches a single
    path component (the sanitized project-slug folder). Subagent JSONLs live
    at ``projects/<slug>/<session-uuid>/subagents/agent-*.jsonl`` and are
    intentionally excluded by the pattern shape -- we only return top-level
    session transcripts.

    Returns:
        Sorted list of distinct repo-relative paths (forward-slash). Empty
        list means the UUID was never tracked. More than one entry means a
        slug collision -- the same session JSONL was committed at different
        sanitized-folder names over its lifetime (rare; would happen if the
        original cwd was renamed between csb backups).
    """
    if not uuid:
        return []

    # `:(glob)` magic: ``*`` matches one path component. Without it, ``*``
    # is a literal character.
    pathspec = f":(glob){ClaudePaths.PROJECTS}/*/{uuid}.jsonl"
    result = run_git(
        claude_dir,
        "log", "--all", "--pretty=format:", "--name-only",
        "--", pathspec,
        check=False,
    )
    if result.returncode != 0:
        return []

    paths = set()
    for line in result.stdout.splitlines():
        s = line.strip()
        if s:
            # git emits paths relative to the REPO ROOT, which may be
            # a parent of claude_dir (e.g. ~/ with .claude/ as a subdir).
            # Strip the claude_dir prefix so downstream git calls (which
            # use -C claude_dir) interpret the path correctly.
            paths.add(_to_claude_dir_relative(
                claude_dir, _normalize_git_path(s)
            ))
    return sorted(paths)


# ── SESSION-HISTORY scope table (the source of truth for "what to restore") ──
#
# Adding a new SESSION-HISTORY category? Add ONE row to SESSION_HISTORY_SCOPES.
# That single row determines both DISCOVERY (which paths ls-tree returns) and
# CATEGORIZATION (the human-friendly label in `csb restore`'s summary output).
#
# Each spec has:
#   - label:       human-friendly category name (shown in restore summary)
#   - pathspec_fmt: format string for the `git ls-tree --` pathspec (broad
#                   directory scope; UUID/slug filtering happens in Python
#                   because `git ls-tree` doesn't support `:(glob)` magic)
#   - match_fmt:    Python-side predicate; tested against each ls-tree line
#                   after path normalization. Determines if the line is keyed
#                   to THIS uuid (vs. some sibling session's file in the same
#                   broad pathspec scope).
#
# Whitebox-verified against `c:/code-ext/claude-code/`:
#   - projects/<slug>/<uuid>{.jsonl,/...}  -- sessionStorage.ts (Claude Code)
#   - session-states/<uuid>.*               -- session_state.py (claude-session-logger)
#   - sesslogs/<dir-with-uuid>/...          -- reconciliation.py (logger)
#   - file-history/<uuid>/...               -- fileHistory.ts (Claude Code; /undo)
#   - tasks/<uuid>/...                      -- tasks.ts (Claude Code; task v2)
#   - session-env/<uuid>/...                -- sessionEnvironment.ts (Claude Code; shell env)
#
# Intentionally NOT restored (whitebox-confirmed ephemeral / non-session-history):
#   - debug/<uuid>.txt                      -- only read with --debug-file flag
#   - todos/<uuid>-agent-*.json             -- legacy v1; resume reads from JSONL
#   - telemetry/...<uuid>.*.json            -- retry queue, no resume read
#   - sesslogs/bak/<dir>/...                -- user-managed folder, not logger
#   - .session_cache.json under any project -- project-wide, not session-keyed


from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ScopeSpec:
    """One SESSION-HISTORY category for the restore-discovery scope table."""
    label: str
    pathspec_fmt: str  # formatted with .format(slug=..., uuid=...)
    match_fmt: Callable[[str, str, str], bool]  # (rel_path, slug, uuid) -> bool

    def pathspec(self, slug: str, uuid: str) -> str:
        return self.pathspec_fmt.format(slug=slug, uuid=uuid)

    def matches(self, rel_path: str, slug: str, uuid: str) -> bool:
        return self.match_fmt(rel_path, slug, uuid)


def _matches_sesslog_dir(rel: str, slug: str, uuid: str) -> bool:
    """sesslogs/<sanitized-name>__<uuid>_<user>/... -- match ONLY when the
    FIRST component under sesslogs/ contains `__<uuid>_`. This deliberately
    excludes `sesslogs/bak/` (user-managed; not logger). The logger's own
    nested `<sesslog-dir>/baks/` is included naturally because it lives
    under the matched first-component dir.
    """
    if not rel.startswith(f"{ClaudePaths.SESSLOGS}/"):
        return False
    parts = rel.split("/", 2)
    return len(parts) >= 2 and f"__{uuid}_" in parts[1]


# Composed from ClaudePaths' canonical name constants (#46): the same
# spelling that builds absolute paths builds these claude_dir-relative
# pathspec/match strings -- one owner, two representations.
SESSION_HISTORY_SCOPES: list[ScopeSpec] = [
    ScopeSpec(
        label="main transcript",
        pathspec_fmt=ClaudePaths.PROJECTS + "/{slug}",
        match_fmt=lambda rel, slug, uuid:
            rel == f"{ClaudePaths.PROJECTS}/{slug}/{uuid}.jsonl",
    ),
    ScopeSpec(
        label="session subtree",
        pathspec_fmt=ClaudePaths.PROJECTS + "/{slug}",
        match_fmt=lambda rel, slug, uuid:
            rel.startswith(f"{ClaudePaths.PROJECTS}/{slug}/{uuid}/"),
    ),
    ScopeSpec(
        label="session-states (logger)",
        pathspec_fmt=ClaudePaths.SESSION_STATES,
        match_fmt=lambda rel, slug, uuid:
            rel.startswith(f"{ClaudePaths.SESSION_STATES}/{uuid}."),
    ),
    ScopeSpec(
        label="sesslogs (logger)",
        pathspec_fmt=ClaudePaths.SESSLOGS,
        match_fmt=_matches_sesslog_dir,
    ),
    ScopeSpec(
        label="file-history (Claude Code /undo)",
        pathspec_fmt=ClaudePaths.FILE_HISTORY,
        match_fmt=lambda rel, slug, uuid:
            rel.startswith(f"{ClaudePaths.FILE_HISTORY}/{uuid}/"),
    ),
    ScopeSpec(
        label="tasks (Claude Code task v2)",
        pathspec_fmt=ClaudePaths.TASKS,
        match_fmt=lambda rel, slug, uuid:
            rel.startswith(f"{ClaudePaths.TASKS}/{uuid}/"),
    ),
    ScopeSpec(
        label="session-env (Claude Code shell env)",
        pathspec_fmt=ClaudePaths.SESSION_ENV,
        match_fmt=lambda rel, slug, uuid:
            rel.startswith(f"{ClaudePaths.SESSION_ENV}/{uuid}/"),
    ),
]


def categorize_path_for_uuid(rel_path: str, slug: str, uuid: str) -> Optional[str]:
    """Return the SESSION-HISTORY category label for a path keyed to ``uuid``,
    or None if the path is not in any SESSION-HISTORY scope.

    Used by `csb restore`'s summary output and by tests that need to verify
    which category a particular path falls into.
    """
    for spec in SESSION_HISTORY_SCOPES:
        if spec.matches(rel_path, slug, uuid):
            return spec.label
    return None


def git_ls_tree_for_uuid(
    claude_dir: str,
    commit: str,
    slug: str,
    uuid: str,
) -> list[str]:
    """
    Enumerate every SESSION-HISTORY path keyed to ``uuid`` at ``commit``.

    This is the discovery primitive that powers the v0.3.12+ full restore.
    It walks the git tree at a specific commit and returns the paths that
    should be restored to reconstruct the session as-it-was. The
    SESSION-HISTORY scope is defined by the table-driven ``SESSION_HISTORY_SCOPES``
    above -- adding a new category is one row.

    Scope at the time of writing (whitebox-verified against `c:/code-ext/claude-code/`
    and `c:/code/claude-projects/claude-session-logger/`):

      - Main transcript:          ``projects/<slug>/<uuid>.jsonl``
      - Session subtree:          ``projects/<slug>/<uuid>/...`` (subagents,
                                  tool-results, remote-agents)
      - Logger state files:       ``session-states/<uuid>.*``
      - Logger sesslog dir:       ``sesslogs/<sanitized-name>__<uuid>_<user>/...``
      - File-history (`/undo`):   ``file-history/<uuid>/...``
      - Tasks (v2):               ``tasks/<uuid>/...``
      - Session-env (shell env):  ``session-env/<uuid>/...``

    Explicitly excluded as EPHEMERAL or out-of-scope (whitebox-confirmed):
    ``debug/<uuid>.txt``, ``todos/<uuid>-agent-*.json``, ``telemetry/...<uuid>.json``,
    ``sesslogs/bak/`` (user-managed), and project-level ``.session_cache.json``.

    For populations without claude-session-logger, the session-states and
    sesslogs pathspecs match zero files and are silently absent from the
    result -- no error, no special-casing.

    The discipline this enforces: we ASK GIT what's there, we never
    construct paths and assume their existence.

    Args:
        claude_dir: absolute path to ``~/.claude`` (or the working dir csb
            operates against; may be a subdir of the actual git repo root).
        commit: commit-ish to inspect (e.g. parent-of-deletion).
        slug: the sanitized project-slug folder name under ``projects/``.
            Required to scope the projects/ pathspec; pass the slug
            resolved from the DB row's ``jsonl_path`` (or from
            ``git_find_jsonl_by_uuid`` for the DB-missing fallback).
        uuid: the session UUID.

    Returns:
        Sorted list of distinct claude_dir-relative paths (forward-slash).
        Empty list means either the commit is unknown or no matching
        SESSION-HISTORY files exist at that commit.
    """
    return [rel for rel, _is_symlink
            in _git_ls_tree_scoped_entries(claude_dir, commit, slug, uuid)]


# Git's symlink tree mode. A blob stored with this mode is a symlink whose
# *content* is the link target path -- restoring it as a regular file (or,
# worse, writing it THROUGH an existing on-disk link) corrupts data. See the
# v0.3.15 symlink-clobber DWP.
_GIT_SYMLINK_MODE = "120000"


def _git_ls_tree_scoped_entries(
    claude_dir: str,
    commit: str,
    slug: str,
    uuid: str,
) -> list[tuple[str, bool]]:
    """Core enumerator behind :func:`git_ls_tree_for_uuid` and
    :func:`git_ls_tree_symlinks_for_uuid`.

    Returns a sorted list of ``(claude_dir_relative_path, is_symlink)`` for
    every SESSION-HISTORY path keyed to ``uuid`` at ``commit``. One git call.

    Unlike the pre-v0.3.15 implementation, this parses the FULL ``git
    ls-tree`` output (``<mode> <type> <object>\\t<path>``) instead of
    ``--name-only`` so the symlink mode (120000) is available to callers.
    """
    if not uuid or not slug:
        return []

    # Collect unique pathspecs from the scope table (multiple specs may
    # share a pathspec -- e.g. main transcript and session subtree both
    # scope to projects/<slug>/). Deduplicate to keep the ls-tree command
    # short and the output minimal.
    pathspecs = sorted({spec.pathspec(slug, uuid) for spec in SESSION_HISTORY_SCOPES})

    result = run_git(
        claude_dir,
        "ls-tree", "-r", commit,   # NOTE: no --name-only; we need the mode column
        "--",
        *pathspecs,
        check=False,
    )
    if result.returncode != 0:
        return []

    entries: dict[str, bool] = {}
    for line in result.stdout.splitlines():
        # Format: "<mode> SP <type> SP <object> TAB <path>"
        if "\t" not in line:
            continue
        meta, raw_path = line.split("\t", 1)
        mode = meta.split(" ", 1)[0] if meta else ""
        normalized = _normalize_git_path(raw_path.strip())
        # Git emits repo-relative paths; translate to claude_dir-relative
        # so downstream operations (Path / git_show_file_bytes which
        # also uses -C claude_dir) interpret them correctly.
        rel = _to_claude_dir_relative(claude_dir, normalized)
        # Match against the scope table -- if ANY spec matches, include.
        if any(spec.matches(rel, slug, uuid) for spec in SESSION_HISTORY_SCOPES):
            entries[rel] = (mode == _GIT_SYMLINK_MODE)
    return sorted(entries.items())


def git_ls_tree_symlinks_for_uuid(
    claude_dir: str,
    commit: str,
    slug: str,
    uuid: str,
) -> set[str]:
    """Return the subset of in-scope paths that are git symlinks (mode 120000).

    These must NOT be restored as files: a symlink blob's content is the
    link-target path, and writing it (especially through an existing on-disk
    link) corrupts the target. claude-session-logger regenerates its own
    ``transcript.jsonl`` symlink on session activity, so skipping it loses
    nothing. See the v0.3.15 symlink-clobber DWP.
    """
    return {rel for rel, is_symlink
            in _git_ls_tree_scoped_entries(claude_dir, commit, slug, uuid)
            if is_symlink}


def git_find_deleted_file(claude_dir: str, file_path: Union[str, Path]) -> Optional[str]:
    """
    Find the last commit that contained a now-deleted file.

    Returns the commit hash, or None if file was never tracked.
    Path is normalized to forward slashes for git's pathspec.
    """
    norm = _normalize_git_path(file_path)
    result = run_git(
        claude_dir,
        "log", "-1",
        "--pretty=format:%H",
        "--diff-filter=D",
        "--", norm,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        delete_commit = result.stdout.strip()
        parent = run_git(
            claude_dir,
            "rev-parse", f"{delete_commit}~1",
            check=False,
        )
        if parent.returncode == 0:
            return parent.stdout.strip()

    # Fallback: find the last commit where the file existed
    result = run_git(
        claude_dir,
        "log", "-1",
        "--pretty=format:%H",
        "--all",
        "--", norm,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    return None


def git_restore_file(
    claude_dir: str,
    commit: str,
    file_path: Union[str, Path],
    dest_path: Union[str, Path],
) -> bool:
    """
    Restore a file from git history to a destination path, byte-for-byte.

    Does NOT use git checkout (which would modify working tree state and may
    apply autocrlf normalization). Instead reads raw bytes via
    ``git_show_file_bytes`` and writes them with ``write_bytes`` -- no decode,
    no re-encode, no text-mode translation. The resulting file is identical
    to the original at the time it was committed.

    Returns True on successful write, False if git_show_file_bytes returned
    None (file not in the given commit, or git error).
    """
    content = git_show_file_bytes(claude_dir, commit, file_path)
    if content is None:
        return False

    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Write-guard (S1c, v0.3.15): never write THROUGH an existing symlink or
    # junction. `Path.write_bytes` follows symlinks, so if `dest` is a link,
    # the bytes would land on the link TARGET -- the exact mechanism that
    # clobbered a restored 2 MB transcript via a dangling transcript.jsonl
    # symlink. Remove the link first so we write a regular file AT the path.
    # Defense-in-depth: discovery already skips git-tracked symlinks, but this
    # guard also protects against on-disk links that aren't in the restore set.
    if _is_link_or_junction(dest):
        try:
            dest.unlink()
        except OSError:
            # As a last resort, a directory junction may need os.rmdir; if we
            # can't remove it, refuse rather than write through.
            try:
                os.rmdir(dest)
            except OSError:
                return False
    dest.write_bytes(content)
    return True


def _is_link_or_junction(p: Path) -> bool:
    """True if `p` is a symlink (any OS) or a Windows directory junction.

    `os.path.islink` covers symlinks on all platforms. Windows junctions are
    not symlinks; `os.path.isjunction` detects them but only exists on Python
    3.12+. On older Pythons junctions fall through (rare for csb's file paths).
    """
    if os.path.islink(p):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        try:
            return isjunction(p)
        except OSError:
            return False
    return False


# ── .gitattributes self-maintenance ─────────────────────────────────

GITATTRIBUTES_MARKER_BEGIN = "# >>> csb-managed block (do not edit between markers)"
GITATTRIBUTES_MARKER_END = "# <<< end csb-managed block"

# These rules prevent git's autocrlf/eol filters from mutating session data
# bytes at commit OR checkout time. JSONL transcripts and JSON sidecars are
# treated as opaque byte streams -- exactly what the restore path expects.
GITATTRIBUTES_RULES = [
    "# Tell git that csb-managed files are binary -- no autocrlf, no eol normalization.",
    "# This guarantees byte-for-byte fidelity through backup -> commit -> restore.",
    "*.jsonl -text",
    "*.json -text",
    "*.name-cache -text",
]


def ensure_gitattributes(claude_dir: str) -> bool:
    """
    Idempotently maintain a csb-managed block in ``<claude_dir>/.gitattributes``.

    If the file is missing, creates it with just the csb block.
    If the file exists but lacks the csb block, appends the block.
    If the csb block already exists, no-op.
    Other contents (rules the user added by hand) are preserved verbatim.

    Returns True if the file was created or modified, False if nothing changed.
    Safe to call on every backup -- it's a cheap text read.
    """
    path = ClaudePaths.from_dir(claude_dir).gitattributes

    block_lines = [GITATTRIBUTES_MARKER_BEGIN, *GITATTRIBUTES_RULES, GITATTRIBUTES_MARKER_END]
    block = "\n".join(block_lines) + "\n"

    if not path.exists():
        path.write_text(block, encoding="utf-8", newline="\n")
        return True

    existing = path.read_text(encoding="utf-8")
    if GITATTRIBUTES_MARKER_BEGIN in existing and GITATTRIBUTES_MARKER_END in existing:
        # Block already present -- no-op. (We intentionally do NOT overwrite an
        # existing block in case a future csb release tweaks the rules: the
        # user may have edited them and we don't want to clobber. Document
        # block-update procedure separately if rules need to change.)
        return False

    # Append (with a separating blank line if the file didn't end in one)
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(existing + sep + block, encoding="utf-8", newline="\n")
    return True


# UUID extracted from path -- matches the standard 36-char hyphenated form.
_UUID_PATH_RE = re.compile(
    r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)


def git_list_deleted_jsonls(claude_dir: str,
                             since_commit: Optional[str] = None) -> list[dict]:
    """Enumerate every projects/*/<uuid>.jsonl that git has seen deleted.

    Runs ``git log --all --diff-filter=D --pretty=format:%H|%cI
    --name-only -- ':(glob)projects/*/*.jsonl'`` and parses the
    alternating commit-line / path-line output. Output is suitable for
    populating ``git_deleted_jsonls`` via the ``upsert_git_deleted_jsonl``
    helper (in ``index.py``).

    The ``:(glob)`` pathspec magic restricts ``*`` to one path component
    -- so ``projects/<slug>/<uuid>.jsonl`` matches but ``projects/<slug>/
    <session-uuid>/subagents/agent-*.jsonl`` doesn't. We only want
    top-level session transcripts.

    Args:
      claude_dir: path to the ~/.claude git repo
      since_commit: if set, restricts to ``<since_commit>..`` -- useful
        for incremental refresh (only walk new commits since last refresh)

    Returns:
      List of dicts with keys ``jsonl_path``, ``session_id``,
      ``deleted_commit``, ``deleted_at`` (ISO 8601 with timezone).
      Empty list if no deletions match.
    """
    range_arg = []
    if since_commit:
        range_arg = [f"{since_commit}.."]

    result = run_git(
        claude_dir,
        "log", "--all",
        *range_arg,
        "--pretty=format:%H|%cI",
        "--name-only",
        "--diff-filter=D",
        "--", f":(glob){ClaudePaths.PROJECTS}/*/*.jsonl",
        check=False,
    )
    if result.returncode != 0:
        return []

    out: list[dict] = []
    current_commit: Optional[str] = None
    current_iso: Optional[str] = None
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            # blank line separates commit blocks
            current_commit = None
            current_iso = None
            continue
        if current_commit is None and "|" in line:
            # commit line: "<hash>|<iso-timestamp>"
            current_commit, _, current_iso = line.partition("|")
            continue
        # path line -- git emits repo-root-relative; translate to
        # claude_dir-relative so downstream -C claude_dir calls work.
        norm = _to_claude_dir_relative(
            claude_dir, _normalize_git_path(line)
        )
        m = _UUID_PATH_RE.search(norm)
        if m:
            out.append({
                "jsonl_path": norm,
                "session_id": m.group(1).lower(),
                "deleted_commit": current_commit,
                "deleted_at": current_iso,
            })
    return out

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

import re
import subprocess
from pathlib import Path
from typing import Optional, Union


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
    pathspec = f":(glob)projects/*/{uuid}.jsonl"
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


def git_ls_tree_for_uuid(
    claude_dir: str,
    commit: str,
    slug: str,
    uuid: str,
) -> list[str]:
    """
    Enumerate every SESSION-HISTORY path keyed to ``uuid`` at ``commit``.

    This is the discovery primitive that powers the v0.3.12+ full restore
    (issues #32 + #33). It walks the git tree at a specific commit and
    returns the paths that should be restored to reconstruct the session
    as-it-was. Files outside this scope (debug/, telemetry/, file-history/,
    tasks/, todos/, session-env/) are intentionally NOT returned -- they
    are classified ephemeral per the 2026-06-03 design DWP.

    The four pathspecs we match:

      1. ``projects/<slug>/<uuid>.jsonl``                       (main transcript)
      2. ``projects/<slug>/<uuid>/**``                          (subagents/, tool-results/, remote-agents/, etc.)
      3. ``session-states/<uuid>.*``                            (logger state files; only present if user has claude-session-logger)
      4. ``sesslogs/*__<uuid>_*/**``                            (logger sesslog directory; same conditional)

    For populations without the logger, (3) and (4) match zero files and
    are silently absent from the result -- no error, no special-casing.

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
    if not uuid or not slug:
        return []

    # `git ls-tree` does NOT support `:(glob)` pathspec magic (unlike
    # `git log`), so we pass broad directory pathspecs and filter for
    # UUID-keyed paths in Python. The three pathspecs scope the walk:
    #
    #   - projects/<slug>/    -- contains the JSONL + the UUID subtree
    #   - session-states/     -- contains logger state files for many UUIDs
    #   - sesslogs/           -- contains many sesslog dirs for many UUIDs
    #
    # The Python filter below keeps only paths keyed to this UUID. This
    # naturally excludes the seven ephemeral categories (debug/,
    # telemetry/, file-history/, tasks/, todos/, session-env/, and the
    # project-level .session_cache.json) because they are not under
    # any of the three scoped pathspecs.
    result = run_git(
        claude_dir,
        "ls-tree", "-r", "--name-only", commit,
        "--",
        f"projects/{slug}",
        "session-states",
        "sesslogs",
        check=False,
    )
    if result.returncode != 0:
        return []

    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    jsonl_subtree_prefix = f"projects/{slug}/{uuid}/"
    session_state_prefix = f"session-states/{uuid}."
    sesslog_dir_match = f"__{uuid}_"

    paths = set()
    for line in result.stdout.splitlines():
        s = line.strip()
        if not s:
            continue
        normalized = _normalize_git_path(s)
        # Git emits repo-relative paths; translate to claude_dir-relative
        # so downstream operations (Path / git_show_file_bytes which
        # also uses -C claude_dir) interpret them correctly.
        rel = _to_claude_dir_relative(claude_dir, normalized)

        if rel == jsonl_path:
            paths.add(rel)
        elif rel.startswith(jsonl_subtree_prefix):
            paths.add(rel)
        elif rel.startswith(session_state_prefix):
            paths.add(rel)
        elif rel.startswith("sesslogs/"):
            # The logger writes its per-session dir DIRECTLY under
            # sesslogs/ as `sesslogs/<sanitized-name>__<uuid>_<user>/...`
            # (see claude-session-logger reconciliation.py:183-207). Only
            # match when the FIRST component after sesslogs/ contains
            # `__<uuid>_` -- nested intermediates like `sesslogs/bak/`
            # are NOT logger-managed (verified 2026-06-03 against the
            # logger source; `sesslogs/bak/` is a user-maintained folder
            # outside csb-restore's scope).
            #
            # The logger DOES write `baks/` (plural) INSIDE the per-session
            # sesslog dir for housekeeping recovery (file_io.py:408). Those
            # are naturally included because they're under the matched
            # first-component dir.
            parts = rel.split("/", 2)
            if len(parts) >= 2 and sesslog_dir_match in parts[1]:
                paths.add(rel)
    return sorted(paths)


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
    dest.write_bytes(content)
    return True


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
    path = Path(claude_dir) / ".gitattributes"

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
        "--", ":(glob)projects/*/*.jsonl",
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

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


def git_show_file(claude_dir: str, commit: str, file_path: Union[str, Path]) -> Optional[str]:
    """
    Retrieve file content from a specific git commit as a decoded string.

    Note: returns universal-newline-translated text. For byte-pure restore use
    ``git_show_file_bytes`` instead. This function is kept for non-restore
    callers that want the convenience of a string.
    """
    norm = _normalize_git_path(file_path)
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
    norm = _normalize_git_path(file_path)
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
    ``csb rebuild-index`` (which only sees current on-disk state), on a fresh
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
            paths.add(_normalize_git_path(s))
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

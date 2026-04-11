"""
Git operations -- manages the ~/.claude git repository for backup.

Handles add, commit, restore from history, and status queries.
All operations shell out to the `git` CLI for maximum compatibility.

Commit separation follows the claude-cleanup model:
  - NOISE: auto-generated transient state (sessions, caches, telemetry)
  - USER: hand-edited configs and customizations (agents, skills, settings)
These are always committed separately with --no-gpg-sign for unattended use.
"""

import subprocess
from pathlib import Path
from typing import Optional


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


def git_show_file(claude_dir: str, commit: str, file_path: str) -> Optional[str]:
    """Retrieve file content from a specific git commit."""
    result = run_git(
        claude_dir,
        "show", f"{commit}:{file_path}",
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    return None


def git_find_deleted_file(claude_dir: str, file_path: str) -> Optional[str]:
    """
    Find the last commit that contained a now-deleted file.

    Returns the commit hash, or None if file was never tracked.
    """
    result = run_git(
        claude_dir,
        "log", "-1",
        "--pretty=format:%H",
        "--diff-filter=D",
        "--", file_path,
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
        "--", file_path,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    return None


def git_restore_file(claude_dir: str, commit: str, file_path: str, dest_path: str) -> bool:
    """
    Restore a file from git history to a destination path.

    Does NOT use git checkout (which would modify the working tree state).
    Instead, reads content via git show and writes to dest_path.
    """
    content = git_show_file(claude_dir, commit, file_path)
    if content is None:
        return False

    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return True

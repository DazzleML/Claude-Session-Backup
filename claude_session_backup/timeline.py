"""
Timeline formatting -- renders session lists as human-readable timelines.

Provides relative date formatting ("today", "yesterday", "3 days ago")
and structured session display with folder analysis.

Uses rich for terminal formatting when available.
"""

import time
from datetime import datetime, timezone
from typing import Optional

try:
    from rich.console import Console
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


def relative_date(iso_timestamp: Optional[str], now: Optional[datetime] = None) -> str:
    """
    Format an ISO timestamp as a relative date string.

    Examples: "today", "yesterday", "3 days ago", "2 weeks ago"
    """
    if not iso_timestamp:
        return "unknown"

    try:
        # Handle both Z suffix and +00:00
        ts = iso_timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "unknown"

    if now is None:
        now = datetime.now(timezone.utc)

    delta = now - dt
    days = delta.days

    if days == 0:
        return "today"
    elif days == 1:
        return "yesterday"
    elif days < 7:
        return f"{days} days ago"
    elif days < 14:
        return "1 week ago"
    elif days < 30:
        weeks = days // 7
        return f"{weeks} weeks ago"
    elif days < 60:
        return "1 month ago"
    elif days < 365:
        months = days // 30
        return f"{months} months ago"
    else:
        years = days // 365
        return f"{years} year{'s' if years > 1 else ''} ago"


def format_timestamp(iso_timestamp: Optional[str]) -> str:
    """Format ISO timestamp as 'YYYY-MM-DD, at HH:MM'."""
    if not iso_timestamp:
        return ""

    try:
        ts = iso_timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%Y-%m-%d, at %H:%M")
    except (ValueError, TypeError):
        return ""


def _find_max_usage_folder(folders: list[dict]) -> Optional[str]:
    """Find the folder path with the highest usage count across all folders."""
    if not folders:
        return None
    max_folder = max(folders, key=lambda f: f.get("usage_count", 0))
    return max_folder.get("folder_path")


# ── Purge countdown ────────────────────────────────────────────────

def purge_countdown(jsonl_mtime: float, cleanup_days: int) -> tuple[Optional[int], str]:
    """
    Calculate days remaining before Claude Code would purge this session.

    Based on JSONL file modification time (active sessions refresh mtime
    on every interaction, so the countdown resets with use).

    Args:
        jsonl_mtime: File modification time as Unix timestamp
        cleanup_days: Claude Code's cleanupPeriodDays setting

    Returns:
        (days_remaining, purge_text)
        days_remaining is None if cleanup is disabled or mtime is unavailable.
    """
    if cleanup_days <= 0 or jsonl_mtime <= 0:
        return None, ""

    now = time.time()
    age_days = (now - jsonl_mtime) / 86400
    remaining = cleanup_days - age_days
    days = int(remaining)

    if days > 0:
        return days, f"(purge in {days}d)"
    else:
        overdue = abs(days)
        return days, f"(OVERDUE by {overdue}d)"


def _purge_style(days_remaining: Optional[int]) -> str:
    """Return a rich style string based on purge urgency."""
    if days_remaining is None:
        return "dim"
    if days_remaining > 30:
        return "dim green"
    elif days_remaining > 14:
        return "yellow"
    elif days_remaining > 6:
        return "rgb(255,165,0)"  # orange
    elif days_remaining > 0:
        return "red"
    else:
        return "bold red"


# ── Plain text formatting ──────────────────────────────────────────

def format_session_line(session: dict, index: int, cleanup_days: int = 0) -> str:
    """
    Format a single session for the timeline view (plain text fallback).
    """
    name = session.get("session_name") or "(unnamed)"
    last_user = session.get("last_user_at") or session.get("last_active_at")
    started = session.get("started_at")
    start_folder = session.get("start_folder") or "(unknown)"
    full_id = session.get("session_id", "")
    msg_count = session.get("message_count", 0)
    deleted = session.get("deleted_at")
    mtime = session.get("jsonl_mtime", 0)

    rel = relative_date(last_user)
    ts_user = format_timestamp(last_user)
    ts_started = format_timestamp(started)

    status = "[DELETED] " if deleted else ""

    lines = [f"  {index:>3}. {status}{name}  {rel} ({ts_user})"]
    if ts_started:
        purge_text = ""
        if cleanup_days > 0 and mtime > 0:
            _, purge_text = purge_countdown(mtime, cleanup_days)
            if purge_text:
                purge_text = f" {purge_text}"
        lines.append(f"       started: {ts_started}{purge_text}")

    folders = session.get("folders", [])
    start_count = 0
    other_folders = []
    total_folder_count = 0

    for f in folders:
        total_folder_count += 1
        if f.get("is_start_folder"):
            start_count = f["usage_count"]
        else:
            other_folders.append(f)

    lines.append(f"       start at: {start_folder} ({start_count}x)")

    displayed_others = other_folders[:5]
    for f in displayed_others:
        lines.append(f"       {f['folder_path']} ({f['usage_count']}x)")

    meta_parts = []
    remaining = total_folder_count - 1 - len(displayed_others)
    if remaining > 0:
        meta_parts.append(f"{remaining} other folder{'s' if remaining != 1 else ''} seen")

    meta_parts.append(f"id: {full_id}")
    if msg_count:
        meta_parts.append(f"{msg_count} messages")
    version = session.get("claude_version")
    if version:
        meta_parts.append(f"v{version}")
    if deleted:
        meta_parts.append(f"restore: claude --resume {full_id}")
    lines.append(f"       {' | '.join(meta_parts)}")

    return "\n".join(lines)


# ── Rich formatting ────────────────────────────────────────────────

def render_session_rich(console: Console, session: dict, index: int,
                        cleanup_days: int = 0):
    """
    Render a single session entry using rich formatting.

    Colors:
    - Session name: bold cyan
    - Relative date: yellow
    - Timestamp: dim
    - Purge countdown: green -> yellow -> orange -> red -> bold red
    - Most-used folder: bold green
    - Start folder (not max): white
    - Other folders (not max): grey70
    - Session ID: dim
    - Deleted: bold red
    """
    name = session.get("session_name") or "(unnamed)"
    last_user = session.get("last_user_at") or session.get("last_active_at")
    started = session.get("started_at")
    start_folder = session.get("start_folder") or "(unknown)"
    full_id = session.get("session_id", "")
    msg_count = session.get("message_count", 0)
    deleted = session.get("deleted_at")
    mtime = session.get("jsonl_mtime", 0)

    rel = relative_date(last_user)
    ts_user = format_timestamp(last_user)
    ts_started = format_timestamp(started)

    # Purge countdown
    days_remaining = None
    purge_text = ""
    if cleanup_days > 0 and mtime > 0:
        days_remaining, purge_text = purge_countdown(mtime, cleanup_days)
    purge_style = _purge_style(days_remaining)

    # Collect folder data
    folders = session.get("folders", [])
    start_count = 0
    other_folders = []
    total_folder_count = 0

    for f in folders:
        total_folder_count += 1
        if f.get("is_start_folder"):
            start_count = f["usage_count"]
        else:
            other_folders.append(f)

    max_path = _find_max_usage_folder(folders)

    # Header line: index + name + last user activity
    header = Text()
    header.append(f"  {index:>3}. ", style="dim")
    if deleted:
        header.append("[DELETED] ", style="bold red")
    header.append(name, style="bold cyan")
    header.append("  ")
    header.append(rel, style="yellow")
    header.append(f" ({ts_user})", style="dim")
    console.print(header)

    # Started date line with purge countdown
    if ts_started:
        started_line = Text("       ")
        started_line.append("started: ", style="dim blue")
        started_line.append(ts_started, style="dim")
        if purge_text:
            started_line.append(f" {purge_text}", style=purge_style)
        console.print(started_line)

    # Start folder line
    is_start_max = (start_folder == max_path)
    start_line = Text("       ")
    start_line.append("start at: ", style="blue")
    start_style = "bold green" if is_start_max else "white"
    start_line.append(start_folder, style=start_style)
    start_line.append(f" ({start_count}x)", style=start_style)
    console.print(start_line)

    # Other folders, each on own line
    displayed_others = other_folders[:5]
    for f in displayed_others:
        fpath = f["folder_path"]
        fcount = f["usage_count"]
        is_max = (fpath == max_path)
        fline = Text("       ")
        folder_style = "bold green" if is_max else "grey70"
        fline.append(fpath, style=folder_style)
        fline.append(f" ({fcount}x)", style=folder_style)
        console.print(fline)

    # Metadata line
    meta = Text("       ")
    remaining = total_folder_count - 1 - len(displayed_others)
    if remaining > 0:
        meta.append(f"{remaining} other folder{'s' if remaining != 1 else ''} seen", style="dim")
        meta.append(" | ", style="dim")

    meta.append(f"id: {full_id}", style="dim")
    if msg_count:
        meta.append(f" | {msg_count} messages", style="")
    version = session.get("claude_version")
    if version:
        meta.append(f" | v{version}", style="dim")
    if deleted:
        meta.append(f" | restore: ", style="dim")
        meta.append(f"claude --resume {full_id}", style="bold yellow")
    console.print(meta)


# ── Timeline renderers ─────────────────────────────────────────────

def format_timeline(sessions: list[dict], cleanup_days: int = 0) -> str:
    """Format a list of sessions as a plain text timeline (fallback)."""
    if not sessions:
        return "  No sessions found."

    lines = []
    for i, session in enumerate(sessions, 1):
        lines.append(format_session_line(session, i, cleanup_days=cleanup_days))
        lines.append("")

    return "\n".join(lines)


def render_timeline_rich(sessions: list[dict], console: Optional[Console] = None,
                         cleanup_days: int = 0):
    """Render a list of sessions using rich formatting."""
    if console is None:
        console = Console()

    if not sessions:
        console.print("  No sessions found.", style="dim")
        return

    for i, session in enumerate(sessions, 1):
        render_session_rich(console, session, i, cleanup_days=cleanup_days)
        console.print()  # blank line between entries

"""
Render :class:`claude_session_backup.search.Hit` records to a terminal.

Three output modes:

- **Default human-readable**: hits grouped by session; per-session header
  shows session name + UUID + project + last-active; each hit prints role
  tag, timestamp, line number, surrounding context (if requested), and
  the matched text. ANSI color is enabled by default (auto-disabled when
  stdout is not a TTY or ``--no-color`` is passed).
- **--files-only**: one line per unique source path, no excerpts.
- **--json**: one JSON object per hit, newline-delimited (jq-friendly).

Long matched lines truncate at 500 characters unless ``--full-match`` is
set; context lines truncate at 200. This keeps "broad query against a
huge AI response" from drowning the terminal while still showing what
matched.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Iterable

from .ids import format_short_uuid
from .search import Hit


# ── ANSI color helpers ────────────────────────────────────────────────


_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "magenta": "\033[35m",
    "blue": "\033[34m",
    "red": "\033[31m",
}


def _color_supported() -> bool:
    """True if stdout is a TTY and the platform supports ANSI."""
    if not getattr(sys.stdout, "isatty", lambda: False)():
        return False
    if os.name == "nt":
        # Windows Terminal / VS Code / modern PowerShell all support ANSI;
        # legacy cmd.exe handles it via VT processing if enabled. The Python
        # 3.6+ runtime on Win10+ enables this by default when stdout is a
        # console (see Python issue #30075). Trust it.
        return True
    return True


def _c(name: str, text: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{_ANSI[name]}{text}{_ANSI['reset']}"


# ── Truncation ────────────────────────────────────────────────────────


_MATCH_MAX = 500
_CONTEXT_MAX = 200


def _truncate(text: str, max_len: int) -> str:
    text = text.replace("\n", " ").replace("\r", "")
    if len(text) > max_len:
        return text[:max_len] + " ..."
    return text


# ── Renderers ─────────────────────────────────────────────────────────


def _role_color(role: str) -> str:
    """USER -> cyan, AI -> yellow, AGENT (any subtype) -> magenta."""
    if role == "USER":
        return "cyan"
    if role == "AI":
        return "yellow"
    if role.startswith("AGENT"):
        return "magenta"
    return "green"


def render_human(
    hits: list[Hit],
    *,
    use_color: bool = True,
    full_match: bool = False,
    shortid: bool = False,
) -> None:
    """Group-by-session human-readable output."""
    if not hits:
        return

    # Group consecutive hits by session_id so the iteration order from
    # search() (last_active DESC) is preserved.
    by_session: list[list[Hit]] = []
    for h in hits:
        if by_session and by_session[-1][0].session_id == h.session_id:
            by_session[-1].append(h)
        else:
            by_session.append([h])

    for group in by_session:
        first = group[0]
        name = first.session_name or "<unnamed>"
        id_display = (
            format_short_uuid(first.session_id) if shortid
            else first.session_id
        )
        hdr = (
            f"{_c('bold', name, use_color)}  "
            f"{_c('dim', id_display, use_color)}  "
            f"{_c('dim', '(' + first.project + ')', use_color)}  "
            f"last: {first.last_active_at or '?'}"
        )
        print(hdr)

        for h in group:
            role_label = _c(_role_color(h.role), f"[{h.role}]", use_color)
            ts = h.timestamp or "?"
            print(f"  {_c('dim', f'L{h.line_num}', use_color)} {role_label} {_c('dim', ts, use_color)}")

            # Context above
            for ev in h.context_above:
                sub_role = _c(_role_color(ev.role), f"[{ev.role}]", use_color)
                print(f"    {_c('dim', sub_role, use_color)} {_truncate(ev.text, _CONTEXT_MAX)}")

            # Matched line
            text = h.matched_text if full_match else _truncate(h.matched_text, _MATCH_MAX)
            print(f"    {_c('green', '>', use_color)} {text}")

            # Context below
            for ev in h.context_below:
                sub_role = _c(_role_color(ev.role), f"[{ev.role}]", use_color)
                print(f"    {_c('dim', sub_role, use_color)} {_truncate(ev.text, _CONTEXT_MAX)}")

            # Spacer between hits in the same session
            print()


def render_files_only(hits: list[Hit]) -> None:
    """One line per unique source path, sorted by appearance order."""
    seen: set[str] = set()
    for h in hits:
        if h.source_path in seen:
            continue
        seen.add(h.source_path)
        print(h.source_path)


def render_json(hits: Iterable[Hit]) -> None:
    """Newline-delimited JSON, one hit per line. Stable key set for jq."""
    for h in hits:
        obj = {
            "session_id": h.session_id,
            "session_name": h.session_name,
            "project": h.project,
            "last_active_at": h.last_active_at,
            "source_type": h.source_type,
            "source_path": h.source_path,
            "line_num": h.line_num,
            "role": h.role,
            "timestamp": h.timestamp,
            "matched_text": h.matched_text,
            "context_above": [
                {"line_num": e.line_num, "role": e.role,
                 "timestamp": e.timestamp, "text": e.text}
                for e in h.context_above
            ],
            "context_below": [
                {"line_num": e.line_num, "role": e.role,
                 "timestamp": e.timestamp, "text": e.text}
                for e in h.context_below
            ],
        }
        print(json.dumps(obj, ensure_ascii=False))


def render(
    hits: list[Hit],
    *,
    mode: str = "human",
    use_color: bool | None = None,
    full_match: bool = False,
    shortid: bool = False,
) -> None:
    """Top-level dispatcher used by ``cmd_search``."""
    if use_color is None:
        use_color = _color_supported()

    if mode == "json":
        render_json(hits)
    elif mode == "files":
        render_files_only(hits)
    else:
        render_human(
            hits, use_color=use_color, full_match=full_match, shortid=shortid,
        )

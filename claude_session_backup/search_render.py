"""
Render :class:`claude_session_backup.search.Hit` records to a terminal.

Three output modes:

- **Default human-readable**: hits grouped by session; per-session header
  shows session name + UUID + project + last-active; each hit prints role
  tag, timestamp, line number, surrounding context (if requested), and
  the matched text. ANSI color is enabled by default (auto-disabled when
  stdout is not a TTY or ``--no-color`` is passed). Occurrences of the
  user's query are highlighted in bold green inside the matched line
  (and context lines, when they happen to contain the query too).
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
import re
import sys
from typing import Iterable

from .ids import format_short_uuid
from .search import Hit
from .timeline import (
    DEFAULT_TOP_FOLDERS,
    _start_count_and_others,
    format_timestamp,
    purge_countdown,
    relative_date,
)


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
    # Session-name style: bold cyan to match csb list / csb scan's
    # session-name convention (timeline.py uses "bold cyan" via Rich).
    "bold_cyan": "\033[1;36m",
    # Query-match highlight: bold green. Used to mark every occurrence
    # of the user's pattern inside the matched line (and context lines
    # where it happens to appear).
    "bold_green": "\033[1;32m",
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


def _highlight(
    text: str,
    pattern: str | None,
    regex: bool,
    case_sensitive: bool,
    enabled: bool,
) -> str:
    """Wrap every occurrence of ``pattern`` in ``text`` with bold-green ANSI.

    Returns ``text`` unchanged when:
      - colors are disabled (``enabled=False``)
      - ``pattern`` is None or empty
      - the regex (in regex mode) fails to compile -- render plain
        rather than crash on a malformed pattern at render time

    Case sensitivity mirrors the search itself: literal mode is
    case-insensitive by default (``re.IGNORECASE``); ``-s`` flips it.
    Regex mode follows the same rule -- the pattern's own ``(?i)`` /
    ``(?-i)`` inline flags compose normally.

    Call AFTER any truncation, never before: ANSI codes embedded in a
    string change its byte length, so truncation would slice through
    escape sequences.
    """
    if not enabled or not pattern:
        return text
    flags = 0 if case_sensitive else re.IGNORECASE
    if regex:
        try:
            compiled = re.compile(pattern, flags)
        except re.error:
            return text
    else:
        compiled = re.compile(re.escape(pattern), flags)
    on = _ANSI["bold_green"]
    off = _ANSI["reset"]
    return compiled.sub(lambda m: f"{on}{m.group(0)}{off}", text)


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


def _full_info_level2_lines(
    hit: Hit,
    use_color: bool,
    top_folders: int = DEFAULT_TOP_FOLDERS,
    skip_start_folder_line: bool = False,
) -> list[str]:
    """Render the level-2 additions: folder list + 'N messages | vX.Y.Z' meta.

    Mirrors ``csb list`` output shape:
      ``  start at: <start_folder> (Nx)``  (suppressed when caller
      already prints a 'start at:' line, e.g. in --sessions-only mode)
      ``  <other_folder> (Nx)``  (up to ``top_folders``)
      ``  N messages | vX.Y.Z``  (only fields with content -- never
      ``  | | ``-style empty separators)
    """
    out: list[str] = []
    start_folder = hit.start_folder or "(unknown)"
    folders = hit.folders or []
    start_count, others = _start_count_and_others(folders, start_folder)

    if not skip_start_folder_line:
        if start_count is not None:
            line = (
                f"  {_c('dim', 'start at:', use_color)} "
                f"{_c('green', start_folder, use_color)} "
                f"{_c('dim', f'({start_count}x)', use_color)}"
            )
        else:
            line = (
                f"  {_c('dim', 'start at:', use_color)} "
                f"{_c('green', start_folder, use_color)}"
            )
        out.append(line)

    for f in others[:top_folders]:
        usage = f.get("usage_count", 0)
        out.append(
            f"  {f['folder_path']} "
            f"{_c('dim', f'({usage}x)', use_color)}"
        )

    # Meta line: 'N messages | vX.Y.Z'. We deliberately OMIT the session
    # ID from this line because csb search already shows it in the
    # first header line (csb list does the opposite -- includes id here
    # because list's first line doesn't have it).
    meta_parts: list[str] = []
    if hit.message_count:
        meta_parts.append(f"{hit.message_count} messages")
    if hit.claude_version:
        meta_parts.append(f"v{hit.claude_version}")
    if meta_parts:
        out.append("  " + _c("dim", " | ".join(meta_parts), use_color))

    return out


def _full_info_line(hit: Hit, cleanup_days: int, use_color: bool) -> str | None:
    """Format the optional second header line: 'started: <date> (purge in Nd)'.

    Reuses ``timeline.relative_date`` / ``format_timestamp`` / ``purge_countdown``
    so the wording matches ``csb list`` exactly -- one vocabulary across the CLI.
    Returns None if neither started_at nor a valid mtime is available.
    """
    started_human = format_timestamp(hit.started_at) if hit.started_at else ""
    purge_text = ""
    if cleanup_days > 0 and hit.jsonl_mtime > 0:
        _, purge_text = purge_countdown(hit.jsonl_mtime, cleanup_days)
        if purge_text:
            purge_text = " " + purge_text
    if not started_human and not purge_text:
        return None
    label = _c("dim", "  started:", use_color)
    body_parts: list[str] = []
    if started_human:
        body_parts.append(started_human)
    if purge_text:
        body_parts.append(_c("yellow", purge_text.strip(), use_color))
    return f"{label} {' '.join(body_parts)}"


def render_human(
    hits: list[Hit],
    *,
    use_color: bool = True,
    full_match: bool = False,
    shortid: bool = False,
    full_info: int = 0,
    cleanup_days: int = 0,
    query: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
) -> None:
    """Group-by-session human-readable output.

    ``full_info`` is an escalation int (0 / 1 / 2):

      0 -- one-line header only (default).
      1 -- adds 'started: <date> (purge in Nd)' (matches csb list shape).
      2 -- adds folder list (start_folder + top N others) + meta line
           'N messages | vX.Y.Z'. Hit.folders must be populated by
           ``search(..., fetch_folders=True)``.

    ``cleanup_days`` (from the user's Claude Code settings) feeds the
    purge countdown at level 1; pass 0 to suppress it cleanly.
    """
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

    for idx_group, group in enumerate(by_session):
        # Blank line BEFORE each session block (except the very first) so
        # adjacent sessions are visually separable. Inside a session,
        # hits already separate themselves with a trailing blank.
        if idx_group > 0:
            print()
        first = group[0]
        name = first.session_name or "<unnamed>"
        id_display = (
            format_short_uuid(first.session_id) if shortid
            else first.session_id
        )
        # Human-readable last-active date is the default (matches csb list).
        # The raw ISO is available via --json or `csb show <uuid>` for the
        # rare case where a user needs it to grep the JSONL.
        if first.last_active_at:
            last_label = (
                f"{relative_date(first.last_active_at)} "
                f"({format_timestamp(first.last_active_at)})"
            )
        else:
            last_label = "last: ?"
        hdr = (
            f"{_c('bold_cyan', name, use_color)}  "
            f"{_c('dim', id_display, use_color)}  "
            f"{_c('dim', '(' + first.project + ')', use_color)}  "
            f"{last_label}"
        )
        # v0.3.5: append the dir-scope ranking signal when present.
        # ``strength_sum > 0`` distinguishes a dir-scope hit (Hit was
        # produced by _search_dir_scope) from a normal hit (those rows
        # leave the fields at their default 0).
        if first.strength_sum > 0:
            ops_plural = "" if first.file_op_count == 1 else "s"
            hdr += "  " + _c(
                "dim",
                f"[{first.file_op_count} file-op{ops_plural}, "
                f"strength={first.strength_sum}]",
                use_color,
            )
        print(hdr)
        if full_info >= 1:
            extra = _full_info_line(first, cleanup_days, use_color)
            if extra:
                print(extra)
        if full_info >= 2:
            for ln in _full_info_level2_lines(first, use_color):
                print(ln)
            # Separator after the thick (4+ line) level-2 metadata block
            # so it doesn't run into the first hit. Level 1's single
            # extra line stays tight (still grep-shaped).
            print()

        for h in group:
            role_label = _c(_role_color(h.role), f"[{h.role}]", use_color)
            ts = h.timestamp or "?"
            print(f"  {_c('dim', f'L{h.line_num}', use_color)} {role_label} {_c('dim', ts, use_color)}")

            # Context above
            for ev in h.context_above:
                sub_role = _c(_role_color(ev.role), f"[{ev.role}]", use_color)
                ctx_text = _truncate(ev.text, _CONTEXT_MAX)
                ctx_text = _highlight(
                    ctx_text, query, regex, case_sensitive, use_color,
                )
                print(f"    {_c('dim', sub_role, use_color)} {ctx_text}")

            # Matched line
            text = h.matched_text if full_match else _truncate(h.matched_text, _MATCH_MAX)
            text = _highlight(text, query, regex, case_sensitive, use_color)
            print(f"    {_c('green', '>', use_color)} {text}")

            # Context below
            for ev in h.context_below:
                sub_role = _c(_role_color(ev.role), f"[{ev.role}]", use_color)
                ctx_text = _truncate(ev.text, _CONTEXT_MAX)
                ctx_text = _highlight(
                    ctx_text, query, regex, case_sensitive, use_color,
                )
                print(f"    {_c('dim', sub_role, use_color)} {ctx_text}")

            # Spacer between hits in the same session
            print()


def render_files_only(hits: list[Hit]) -> None:
    """One line per unique transcript file, sorted by appearance order.

    Prefers ``Hit.transcript_path`` (the human-navigable source resolved
    by ``search()`` -- convo > sesslog > jsonl, FTS5 DBs deliberately
    excluded). Falls back to ``Hit.source_path`` if for some reason
    transcript_path didn't get populated -- preserves the v0.3.4
    behavior for any hit shape that bypassed the v0.3.5 resolver.
    """
    seen: set[str] = set()
    for h in hits:
        path = h.transcript_path or h.source_path
        if path in seen:
            continue
        seen.add(path)
        print(path)


def render_sessions_only(
    hits: list[Hit],
    *,
    use_color: bool = True,
    shortid: bool = False,
    query: str | None = None,
    full_info: int = 0,
    cleanup_days: int = 0,
) -> None:
    """Per-session summary: one line per session containing matches.

    Output line:

        <name>  <UUID>  (<project>)  -- N hit(s)
          start at: <start_folder>    [csb resume <UUID>]

    With ``--shortid``, the UUID and resume hint use the compact form.
    A trailing "Next:" line hints at the drill-in command.
    """
    if not hits:
        return

    # Preserve search()'s last-active-DESC iteration order, but coalesce
    # all hits from the same session into a single summary row.
    order: list[str] = []
    counts: dict[str, int] = {}
    meta: dict[str, Hit] = {}
    for h in hits:
        if h.session_id not in counts:
            order.append(h.session_id)
            meta[h.session_id] = h
        counts[h.session_id] = counts.get(h.session_id, 0) + 1

    for idx_sess, sid in enumerate(order):
        # Blank line BEFORE each session block (except the very first) so
        # the summary is easier to scan.
        if idx_sess > 0:
            print()
        first = meta[sid]
        name = first.session_name or "<unnamed>"
        id_display = format_short_uuid(sid) if shortid else sid
        n = counts[sid]
        hit_word = "hit" if n == 1 else "hits"
        head_line = (
            f"{_c('bold_cyan', name, use_color)}  "
            f"{_c('dim', id_display, use_color)}  "
            f"{_c('dim', '(' + first.project + ')', use_color)}  "
            f"-- {_c('yellow', f'{n} {hit_word}', use_color)}"
        )
        # v0.3.5: dir-scope ranking signal -- mirror the suffix the
        # human-mode renderer prints so users can correlate the two.
        if first.strength_sum > 0:
            ops_plural = "" if first.file_op_count == 1 else "s"
            head_line += "  " + _c(
                "dim",
                f"[{first.file_op_count} file-op{ops_plural}, "
                f"strength={first.strength_sum}]",
                use_color,
            )
        print(head_line)
        if full_info >= 1:
            extra = _full_info_line(first, cleanup_days, use_color)
            if extra:
                print(extra)
        start_at = first.start_folder or "(unknown)"
        resume_hint = f"csb resume {id_display}"
        print(
            f"  {_c('dim', 'start at:', use_color)} "
            f"{_c('green', start_at, use_color)}    "
            f"{_c('dim', '[' + resume_hint + ']', use_color)}"
        )
        if full_info >= 2:
            # skip_start_folder_line=True: sessions-only already prints
            # the 'start at:' line above (with the resume hint), so the
            # level-2 helper only emits other-folder lines + meta.
            for ln in _full_info_level2_lines(
                first, use_color, skip_start_folder_line=True,
            ):
                print(ln)

    if query is not None and order:
        print()
        hint = (
            f'Next: csb search "{query}" --session-id '
            f"{format_short_uuid(order[0]) if shortid else order[0]} -A 3 -B 1"
        )
        print(_c("dim", hint, use_color))


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
        # v0.3.5: only emit dir-scope ranking fields when populated --
        # keeps the JSON shape stable for non-dir-scope callers.
        if h.strength_sum > 0:
            obj["strength_sum"] = h.strength_sum
            obj["file_op_count"] = h.file_op_count
        # v0.3.5: transcript_path is the navigable source file for this
        # session. Emitted whenever non-None (almost always) so JSON
        # consumers can read it directly without falling back to
        # source_path. For file-based dispatcher hits it equals
        # source_path; for FTS5 hits it resolves to the convo/sesslog/
        # jsonl that the user can actually open.
        if h.transcript_path is not None:
            obj["transcript_path"] = h.transcript_path
        print(json.dumps(obj, ensure_ascii=False))


def render(
    hits: list[Hit],
    *,
    mode: str = "human",
    use_color: bool | None = None,
    full_match: bool = False,
    shortid: bool = False,
    query: str | None = None,
    full_info: int = 0,
    cleanup_days: int = 0,
    regex: bool = False,
    case_sensitive: bool = False,
) -> None:
    """Top-level dispatcher used by ``cmd_search``.

    Modes:
      - "human" (default): grouped excerpts with role/timestamp/context.
        ``query`` + ``regex`` + ``case_sensitive`` drive in-line bold-green
        highlighting of every match inside excerpt lines.
      - "json": NDJSON, one hit per line (jq-friendly). No highlighting --
        downstream tools parse the matched_text verbatim.
      - "files": unique source paths only. No highlighting.
      - "sessions": per-session summary with hit counts (uses ``query`` to
        compose a drill-in hint at the bottom).

    ``full_info`` adds 'started: <date> (purge in Nd)' second header line
    to human and sessions modes. Has no effect on json / files modes.
    """
    if use_color is None:
        use_color = _color_supported()

    if mode == "json":
        render_json(hits)
    elif mode == "files":
        render_files_only(hits)
    elif mode == "sessions":
        render_sessions_only(
            hits, use_color=use_color, shortid=shortid, query=query,
            full_info=full_info, cleanup_days=cleanup_days,
        )
    else:
        render_human(
            hits, use_color=use_color, full_match=full_match, shortid=shortid,
            full_info=full_info, cleanup_days=cleanup_days,
            query=query, regex=regex, case_sensitive=case_sensitive,
        )

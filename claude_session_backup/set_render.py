"""Roster rendering for ``csb set`` (#60, #61).

THE one renderer for session-set rosters -- boot epochs now, named sets
when #62 lands -- so both kinds display identically (one display path was
an explicit epic decision). Line-based ``(text, style)`` segments in the
``_style_print`` idiom, NOT a Rich Table: tables re-wrap at fake widths
and the timeline renderer's ``no_wrap`` workaround (timeline.py) showed
how much care that costs. Lines are simpler and byte-comparable in tests.

Row shape follows ``search_render.render_sessions_only`` -- a name line
plus a ``start at: <folder>  [csb resume <hint>]`` hint line -- but in
Rich styles matching the timeline palette (name ``bold cyan``, detail
``dim``, purge marker ``red``, runnable hint ``bold yellow``), because
Rich is the repo's majority color system.

Honest-language rule (#61 AC): the header says "active within ... of
shutdown" and never claims sessions were *open* -- activity is not
liveness until the observation phase (#64) records real open/close
events. Ordering is by activity, and the header says so.
"""

from __future__ import annotations

import sys
from datetime import timezone
from typing import Optional

from .epochs import Epoch, format_gap, parse_index_ts
from .timeline import HAS_RICH, _resolve_start_at

# Marker for sessions Claude Code has purged (30-day TTL). The resume hint
# stays a plain `csb resume` -- the v0.3.14 pruned path offers the restore.
_MARK_PURGED = "[purged -- resume will offer restore-from-git]"

Segments = list[tuple[str, Optional[str]]]


def _emit(segments: Segments, stream=None, use_color: Optional[bool] = None) -> None:
    """Print one line of (text, style) segments; rich when it helps.

    Same idiom as commands._style_print (not imported -- commands imports
    this module, and the 10 lines are cheaper than a circular import).
    """
    stream = stream if stream is not None else sys.stdout
    if use_color is None:
        use_color = HAS_RICH and getattr(stream, "isatty", lambda: False)()
    if HAS_RICH and use_color:
        from rich.console import Console
        from rich.text import Text

        line = Text()
        for text, style in segments:
            line.append(text, style or "")
        Console(file=stream, highlight=False, soft_wrap=True).print(line)
    else:
        print("".join(text for text, _ in segments), file=stream)


def _fmt_utc(dt) -> str:
    """Aware datetime -> 'YYYY-MM-DD HH:MM UTC' (explicit, TZ-honest)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _cause_label(cause: str) -> str:
    return {
        "clean": "clean shutdown",
        "unexpected": "UNEXPECTED shutdown",
        "initiated-by-process": "restart initiated by a process (update/restart)",
    }.get(cause, cause)


def epoch_header_segments(
    epoch: Epoch, member_count: int, window_hours: float, window_source: str
) -> list[Segments]:
    """Header lines for an epoch roster, as segment lists."""
    cause_style = "red" if epoch.cause == "unexpected" else "yellow"
    line1: Segments = [
        ("Epoch 'last'", "bold"),
        (" -- shutdown ", "dim"),
        (_fmt_utc(epoch.shutdown_utc), None),
        ("  [", "dim"),
        (_cause_label(epoch.cause), cause_style),
        ("]", "dim"),
    ]
    if epoch.boot_utc is not None:
        gap = format_gap(epoch.boot_utc - epoch.shutdown_utc)
        line1.append((f"  boot +{gap}", "dim"))

    if window_source == "previous-fence":
        window_desc = (
            f"active since the previous fence ({window_hours:.1f}h before "
            "shutdown)"
        )
    elif window_source == "override":
        window_desc = f"active within {window_hours:g}h of shutdown (--window)"
    else:  # fallback-72h
        window_desc = (
            f"active within {window_hours:.0f}h of shutdown "
            "(previous fence unknown -- fallback)"
        )
    noun = "session" if member_count == 1 else "sessions"
    line2: Segments = [
        (f"{member_count} {noun} {window_desc} ", None),
        ("(activity order -- open order is not tracked)", "dim"),
    ]
    return [line1, line2]


def ambiguous_names_in(members: list[dict]) -> frozenset:
    """Session names appearing on more than one row of this roster."""
    seen, dupes = set(), set()
    for member in members:
        name = member.get("session_name")
        if not name:
            continue
        if name in seen:
            dupes.add(name)
        seen.add(name)
    return frozenset(dupes)


def member_row_segments(member: dict, shutdown_utc=None,
                        ambiguous_names=frozenset()) -> list[Segments]:
    """One roster member -> its display lines as segment lists.

    Line 1: `  N. <name>  <gap> before shutdown  [markers]`
    Line 2: `     start at: <folder>  [csb resume <hint>]`
    Always two lines -- a steady rhythm reads better than a jagged mix.
    (The width-collapse convention in timeline.py is for rows whose second
    line is OPTIONAL metadata; here the hint line is the payload.)

    ``shutdown_utc`` is None for named sets (#62) -- no gap column then.
    """
    name = member["session_name"] or member["session_id"]
    index = member["index"]
    line1: Segments = [
        (f"{index:>4}. ", None),
        (name, "bold cyan"),
    ]
    la = parse_index_ts(member["last_active_at"])
    if la is not None and shutdown_utc is not None:
        gap = format_gap(shutdown_utc - la)
        line1.append((f"  {gap} before shutdown", "yellow"))
    if member["is_fork"]:
        line1.append(("  [fork]", "dim"))
    if member["purged"]:
        line1.append(("  " + _MARK_PURGED, "red"))
    if not member.get("in_index", True):
        line1.append(("  [not in index]", "red"))

    hint_target = _resume_hint_target(member, ambiguous_names)
    start_at = _resolve_start_at(member)
    line2: Segments = [
        ("      start at: ", "dim"),
        (start_at, None),
        ("  [", "dim"),
        (f"csb resume {hint_target}", "bold yellow"),
        ("]", "dim"),
    ]
    return [line1, line2]


def _resume_hint_target(member: dict, ambiguous_names=frozenset()) -> str:
    """What the per-row `csb resume` hint should name.

    The session name when it exists, is shell-safe as one word, and is
    UNIQUE in this roster; the full UUID otherwise.

    The uniqueness check matters more than it looks. Session names
    collide readily -- a project branched across topics produces several
    rows with one name, differing only by folder -- and for those rows
    `csb resume <name>` cannot resolve. Printing it anyway would make the
    roster recommend a command guaranteed to fail on exactly the rows
    the index-addressing feature exists to disambiguate.
    """
    name = member["session_name"]
    if (name and " " not in name and not name.startswith("-")
            and name not in ambiguous_names):
        return name
    return member["session_id"]


def render_roster(
    members: list[dict],
    header_lines: list[Segments],
    *,
    footer_notes: tuple[str, ...] = (),
    shutdown_utc=None,
    stream=None,
    use_color: Optional[bool] = None,
) -> None:
    """Render a full roster: header, blank, rows, optional dim footers."""
    for segs in header_lines:
        _emit(segs, stream=stream, use_color=use_color)
    if members:
        _emit([("", None)], stream=stream, use_color=use_color)
    # Names that repeat in THIS roster fall back to UUIDs in their hints:
    # `csb resume <name>` cannot resolve a duplicate.
    ambiguous = ambiguous_names_in(members)
    for member in members:
        for segs in member_row_segments(member, shutdown_utc, ambiguous):
            _emit(segs, stream=stream, use_color=use_color)
    for note in footer_notes:
        _emit([("", None)], stream=stream, use_color=use_color)
        _emit([(note, "dim")], stream=stream, use_color=use_color)

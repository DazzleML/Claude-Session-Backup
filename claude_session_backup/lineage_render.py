"""
Render a fork forest as an indented CLI tree (DOS / Linux ``tree`` style).

Consumes the :class:`~claude_session_backup.lineage.LineageNode` graph and
emits plain text. Two charsets are supported -- Unicode box-drawing by
default and a pure-ASCII fallback for consoles that cannot encode it
(Windows cp437/cp1252, redirected pipes). Detection is automatic; ``--ascii``
forces the fallback.

**Per-node info levels mirror ``csb list`` / ``csb scan`` exactly** (#31),
and do so by calling the SAME helpers those commands use
(:mod:`timeline`'s ``relative_date`` / ``format_timestamp`` /
``purge_countdown`` / ``_id_for_display`` / ``_resolve_start_at``) rather
than reimplementing any of them:

    level 0 (default)  <id>  <name>  <relative-date> [forked <date>]
    level 1 (-f)       + started <ts> (purge in Nd)
    level 2 (-ff)      + start-at folder, other folders, N messages | vX.Y.Z

**Three visual tiers** (see :mod:`lineage`): matched nodes are marked and
bright, in-scope nodes render normally, and structural connectors -- purged
ancestors and phantom (never-indexed) parents -- render dim with a marker,
so a chain never appears decapitated but out-of-scope rows stay visually
subordinate. In no-color output the markers alone carry the distinction.
"""

from __future__ import annotations

import sys
from typing import Iterable, Optional

from .lineage import Forest, LineageNode
from .timeline import (
    HAS_RICH,
    _format_validated_date,
    _id_for_display,
    _resolve_start_at,
    _start_count_and_others,
    format_timestamp,
    purge_countdown,
    relative_date,
)

# Connector charsets. ``tee``/``elbow`` prefix a node; ``pipe``/``blank``
# continue (or close) the vertical rail for deeper levels.
_CHARSETS = {
    "unicode": {"tee": "├── ", "elbow": "└── ", "pipe": "│   ", "blank": "    "},
    "ascii": {"tee": "+-- ", "elbow": "\\-- ", "pipe": "|   ", "blank": "    "},
}

# Markers carry the tier when color is unavailable (pipes, --no-color,
# rich-less installs), so the tiering survives redirection.
_MARK_MATCH = "*"
_MARK_PURGED = "[purged]"
_MARK_PHANTOM = "[not indexed]"


def supports_unicode(stream=None) -> bool:
    """True when ``stream`` can encode the box-drawing characters.

    Probing the real encoding beats guessing from platform: PowerShell's
    cp1252 cannot represent ``├`` (writing it mojibakes or raises), while
    the classic DOS cp437 console renders it natively -- those glyphs are
    original IBM PC characters.

    NOTE for callers that relax stdout's error handling: capture the
    encoding BEFORE reconfiguring and pass it to :func:`render_forest`.
    Forcing stdout to UTF-8 first would make this probe always answer
    "yes" and defeat the fallback.
    """
    return _encoding_supports_unicode(None, stream)


def _encoding_supports_unicode(encoding: Optional[str], stream=None) -> bool:
    """Shared probe: can ``encoding`` (or ``stream``'s) carry box-drawing?"""
    if not encoding:
        encoding = getattr(stream or sys.stdout, "encoding", None)
    if not encoding:
        return False
    try:
        "├──└│".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _node_headline(node: LineageNode, *, shortid: bool, show_uuid: bool,
                   cleanup_days: int) -> list[tuple[str, Optional[str]]]:
    """Level-0 line as styled segments: identity, name, recency, fork moment.

    Returns ``[(text, style), ...]`` so the caller can colorize each part
    the way ``csb list`` / ``csb scan`` do (name bright, metadata dim)
    instead of painting the whole row one color. ``style`` is None for
    "inherit the row's tier style".

    **Identity is economical by default.** A named session shows just its
    name -- the UUID is 36 characters of noise in a tree that is already
    spending width on indentation. ``--uuid`` brings the full UUID back
    (paste-ready for ``claude --resume``), ``--shortid`` the compact
    head-tail form. An UNNAMED session always shows its UUID, because
    otherwise the row would not identify anything.
    """
    session = node.session
    if node.phantom:
        return [
            (_id_for_display(node.session_id, shortid), "dim"),
            ("  (unknown session)  ", None),
            (_MARK_PHANTOM, "dim"),
        ]

    name = session.get("session_name")
    sid = session.get("session_id", "")
    out: list[tuple[str, Optional[str]]] = []

    if show_uuid or shortid or not name:
        out.append((_id_for_display(sid, shortid), None))
        out.append(("  ", None))
    out.append((name or "(unnamed)", "bold cyan" if not node.matched else None))

    last = session.get("last_user_at") or session.get("last_active_at")
    rel = relative_date(last)
    if rel:
        out.append((f"  {rel}", "dim"))

    # forked_at is the boundary row's own timestamp -- the moment this
    # session split from its parent. Distinct from started_at (see the v6
    # migration note), so it is the only honest "forked <date>" source.
    forked = session.get("forked_at")
    if forked:
        out.append((f"  forked {format_timestamp(forked)}", "dim"))

    if session.get("deleted_at"):
        out.append((f"  {_MARK_PURGED}", "red" if not node.matched else None))
    if node.matched:
        out.append((f"  {_MARK_MATCH}", None))
    return out


def _node_detail_lines(node: LineageNode, *, level: int, cleanup_days: int,
                       top_folders: Optional[int]) -> list[str]:
    """Level 1 / 2 continuation lines for one node."""
    if node.phantom or level < 1:
        return []

    session = node.session
    out: list[str] = []

    started = format_timestamp(session.get("started_at"))
    if started:
        purge_text = ""
        mtime = session.get("jsonl_mtime") or 0
        if cleanup_days > 0 and mtime > 0:
            _, text = purge_countdown(
                mtime, cleanup_days, deleted_at=session.get("deleted_at"),
            )
            if text:
                purge_text = f" {text}"
        out.append(f"started: {started}{purge_text}")

    if level < 2:
        return out

    folders = session.get("folders") or []
    start_folder = _resolve_start_at(session)
    start_count, others = _start_count_and_others(folders, start_folder)
    if start_count is not None:
        out.append(f"start at: {start_folder} ({start_count}x)")
    else:
        out.append(f"start at: {start_folder}")

    shown = others if top_folders is None else others[:top_folders]
    for f in shown:
        out.append(f"{f['folder_path']} ({f['usage_count']}x)")

    meta: list[str] = []
    accounted = len(shown) + (1 if start_count is not None else 0)
    remaining = len(folders) - accounted
    if remaining > 0:
        meta.append(f"{remaining} other folder{'s' if remaining != 1 else ''} seen")
    if session.get("message_count"):
        meta.append(f"{session['message_count']} messages")
    if session.get("claude_version"):
        meta.append(f"v{session['claude_version']}")
    validated = _format_validated_date(session.get("metadata_validated_at"))
    if validated:
        meta.append(f"val: {validated}")
    if meta:
        out.append(" | ".join(meta))
    return out


def _style_for(node: LineageNode) -> Optional[str]:
    """Rich style for a node's tier, or None for the normal tier."""
    if node.matched:
        return "bold green"
    if node.phantom or not node.in_population:
        return "dim"
    return None


def render_forest(
    forest: Forest,
    *,
    level: int = 0,
    shortid: bool = False,
    show_uuid: bool = False,
    cleanup_days: int = 0,
    top_folders: Optional[int] = 3,
    charset: Optional[str] = None,
    encoding: Optional[str] = None,
    use_color: Optional[bool] = None,
    stream=None,
) -> list[tuple[str, list[tuple[str, Optional[str]]]]]:
    """Render ``forest`` to a list of ``(plain_text, segments)`` pairs.

    Returning data (rather than printing) keeps the renderer testable by
    byte-comparison and lets the caller decide between rich and plain
    emission. ``segments`` is a list of ``(text, style)`` covering the whole
    line, so each part can be colored independently -- the row's tier style
    applies wherever a segment's own style is None.

    ``charset`` is ``"unicode"`` / ``"ascii"``; None auto-detects, preferring
    an explicitly supplied ``encoding`` (the console's encoding captured
    BEFORE any reconfiguration) over probing ``stream``.
    """
    if charset is None:
        charset = ("unicode" if _encoding_supports_unicode(encoding, stream)
                   else "ascii")
    glyphs = _CHARSETS[charset]

    lines: list[tuple[str, list[tuple[str, Optional[str]]]]] = []

    def add(prefix: str, segments: list[tuple[str, Optional[str]]],
            tier: Optional[str]) -> None:
        segs: list[tuple[str, Optional[str]]] = []
        if prefix:
            segs.append((prefix, "dim"))
        for text, style in segments:
            segs.append((text, style or tier))
        lines.append(("".join(t for t, _ in segs), segs))

    def emit(node: LineageNode, prefix: str, is_last: bool, is_root: bool) -> None:
        if is_root:
            connector, child_prefix = "", ""
        else:
            connector = glyphs["elbow"] if is_last else glyphs["tee"]
            child_prefix = glyphs["blank"] if is_last else glyphs["pipe"]

        style = _style_for(node)
        add(f"{prefix}{connector}",
            _node_headline(node, shortid=shortid, show_uuid=show_uuid,
                           cleanup_days=cleanup_days),
            style)

        # Continuation lines hang under the node, inside its own rail.
        detail_prefix = f"{prefix}{child_prefix}" if not is_root else "  "
        detail_indent = "  " if is_root else "    "
        for detail in _node_detail_lines(
            node, level=level, cleanup_days=cleanup_days, top_folders=top_folders,
        ):
            add(f"{detail_prefix}{detail_indent}", [(detail, None)], style or "dim")

        kids = node.children
        next_prefix = prefix if is_root else f"{prefix}{child_prefix}"
        for i, child in enumerate(kids):
            emit(child, next_prefix, i == len(kids) - 1, False)

        if node.elided_children:
            hint = (f"... ({node.elided_children} more -- "
                    f"csb tree --root {node.session_id})")
            add(f"{next_prefix}{glyphs['elbow']}", [(hint, None)], "dim")

    for root in forest.roots:
        if lines:
            lines.append(("", []))
        emit(root, "", True, True)

    return lines


def print_forest(forest: Forest, **kwargs) -> None:
    """Print a rendered forest, using rich styling when a TTY is attached."""
    use_color = kwargs.pop("use_color", None)
    stream = kwargs.get("stream")
    rendered = render_forest(forest, **kwargs)

    if use_color is None:
        use_color = HAS_RICH and (stream or sys.stdout).isatty()

    if use_color and HAS_RICH:
        from rich.console import Console
        from rich.text import Text
        # soft_wrap keeps long session names on one logical line rather than
        # hard-wrapping them into the tree's indentation, which would break
        # the visual rails.
        console = Console(highlight=False, soft_wrap=True)
        for _text, segments in rendered:
            if not segments:
                console.print()
                continue
            line = Text()
            for text, style in segments:
                line.append(text, style=style or "")
            console.print(line)
        return

    for text, _segments in rendered:
        print(text)


def forest_summary(forest: Forest) -> str:
    """One-line footer: counts plus any structural warnings."""
    roots = len(forest.roots)
    bits = [f"{roots} tree{'s' if roots != 1 else ''}",
            f"{forest.total_nodes} session{'s' if forest.total_nodes != 1 else ''}"]
    if forest.elided_nodes:
        bits.append(f"{forest.elided_nodes} elided")
    if forest.phantom_parents:
        bits.append(f"{len(forest.phantom_parents)} unindexed parent"
                    f"{'s' if len(forest.phantom_parents) != 1 else ''}")
    return " | ".join(bits)


def to_json(forest: Forest) -> list[dict]:
    """Nested JSON payload: one object per root with ``children`` arrays."""
    return [root.to_dict() for root in forest.roots]

"""
Session ID input/output sugar -- shared resolver and display helper.

Every csb command that *accepts* a session-ID input goes through
``resolve_session_id`` so the prefix/suffix matching and collision UX is
identical across ``csb show``, ``csb resume``, ``csb restore``, and
``csb search --session-id``. Every renderer that *displays* a session ID
goes through ``format_short_uuid`` so the visual representation is the
same across ``csb list``, ``csb scan``, ``csb show``, ``csb search``, etc.

The design intent is friction-free typing: users see and type short forms
("abc12345" or "abc1...dead"), and the resolver maps that back to the
authoritative full UUID. Full UUIDs continue to work everywhere -- the
resolver accepts any length down to 4 chars.

**Collision handling.** If a prefix (or suffix) matches more than one
session row, the resolver raises :class:`AmbiguousSessionID` with the list
of candidate sessions. The CLI catches it, prints the list with names +
start-at paths, and exits 2 -- the user re-runs with a longer prefix or
the full UUID. No interactive prompt: stay scriptable.

**Why prefix AND suffix.** Some UUIDs share a leading nibble run by
accident, and a longer prefix may still collide. The suffix half of the
UUID is independent randomness; either half works equally well as a
disambiguator. Accepting both keeps the resolver maximally lenient
without weakening uniqueness.

**Minimum length.** 4 hex chars (~16 bits, ~65k addressable space). Below
that, a "match" is meaningless across 100+ sessions and we reject early
with a clear error rather than degrading to "guess from many candidates".
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Optional


_MIN_LENGTH = 4
_FULL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_HEX_FRAGMENT_RE = re.compile(r"^[0-9a-f-]+$", re.IGNORECASE)


@dataclass
class SessionCandidate:
    """One row returned alongside an ambiguous-match error."""

    session_id: str
    session_name: Optional[str]
    project: Optional[str]
    start_folder: Optional[str]


class SessionIDError(Exception):
    """Base class for resolver failures."""


class NoSuchSessionID(SessionIDError):
    """The query didn't match any session row.

    Attributes:
        query: The input that didn't resolve.
    """

    def __init__(self, query: str):
        super().__init__(f"No session found matching '{query}'")
        self.query = query


class AmbiguousSessionID(SessionIDError):
    """The query matched more than one session.

    Attributes:
        query: The input that matched too many sessions.
        candidates: The list of matching :class:`SessionCandidate` rows.
    """

    def __init__(self, query: str, candidates: list[SessionCandidate]):
        super().__init__(
            f"Ambiguous session ID '{query}' ({len(candidates)} matches)"
        )
        self.query = query
        self.candidates = candidates


class InvalidSessionIDInput(SessionIDError):
    """The input is too short, empty, or contains non-hex characters."""


# ── Display helper ────────────────────────────────────────────────────


def format_short_uuid(uuid: str, head: int = 8, tail: int = 8) -> str:
    """Return a compact display form of a UUID.

    Truncates a full 36-char UUID to ``<head>...<tail>``.  For short
    inputs (already <= head+tail+3 chars), returns the input unchanged.

    >>> format_short_uuid("abc12345-6789-abcd-ef01-234567890def")
    'abc12345-...-890def'
    >>> format_short_uuid("abc12345")
    'abc12345'
    """
    if not uuid:
        return ""
    if len(uuid) <= head + tail + 3:
        return uuid
    return f"{uuid[:head]}-...-{uuid[-tail:]}"


# ── Resolver ──────────────────────────────────────────────────────────


def _validate_query(query: str) -> str:
    """Normalize and sanity-check the user's input.

    Returns the lowercased query. Raises :class:`InvalidSessionIDInput` if
    the query is too short or contains non-hex characters (excluding the
    UUID separator ``-``).
    """
    if not query:
        raise InvalidSessionIDInput("Empty session ID input")
    q = query.strip().lower()
    if len(q) < _MIN_LENGTH:
        raise InvalidSessionIDInput(
            f"Session ID input '{query}' is too short "
            f"(need at least {_MIN_LENGTH} hex chars)"
        )
    if not _HEX_FRAGMENT_RE.fullmatch(q):
        raise InvalidSessionIDInput(
            f"Session ID input '{query}' contains non-hex characters"
        )
    return q


def _try_parse_head_tail(query: str) -> tuple[str, str] | None:
    """Detect the compact display form ``<head>-...-<tail>`` and split it.

    Returns ``(head, tail)`` if the input matches the display form (both
    halves hex, both >= 4 chars after stripping the surrounding dashes).
    Returns ``None`` if the input doesn't contain ``...`` -- caller falls
    through to the normal validation path.

    Raises :class:`InvalidSessionIDInput` if the input contains ``...`` but
    either half is too short or non-hex -- we want to fail loudly when
    users clearly meant to paste a compact form but typed it wrong.
    """
    q = query.strip().lower()
    if "..." not in q:
        return None
    parts = q.split("...")
    if len(parts) != 2:
        raise InvalidSessionIDInput(
            f"Compact session-ID input '{query}' has more than one '...' "
            f"separator -- expected '<head>-...-<tail>'"
        )
    head = parts[0].rstrip("-")
    tail = parts[1].lstrip("-")
    if len(head) < _MIN_LENGTH or len(tail) < _MIN_LENGTH:
        raise InvalidSessionIDInput(
            f"Compact session-ID input '{query}': both head and tail "
            f"need at least {_MIN_LENGTH} hex chars (got "
            f"head={len(head)}, tail={len(tail)})"
        )
    if not _HEX_FRAGMENT_RE.fullmatch(head):
        raise InvalidSessionIDInput(
            f"Compact session-ID input '{query}': head '{head}' "
            f"contains non-hex characters"
        )
    if not _HEX_FRAGMENT_RE.fullmatch(tail):
        raise InvalidSessionIDInput(
            f"Compact session-ID input '{query}': tail '{tail}' "
            f"contains non-hex characters"
        )
    return head, tail


def _make_candidate(row: sqlite3.Row) -> SessionCandidate:
    return SessionCandidate(
        session_id=row["session_id"],
        session_name=row["session_name"] if "session_name" in row.keys() else None,
        project=row["project"] if "project" in row.keys() else None,
        start_folder=row["start_folder"] if "start_folder" in row.keys() else None,
    )


def resolve_session_id(conn: sqlite3.Connection, query: str) -> str:
    """Resolve a user-supplied ID input to a single full UUID.

    Matching strategy (first hit wins):

    1. **Exact full UUID** -- the input matches the 8-4-4-4-12 canonical
       form; resolve to itself if a row exists.
    2. **Prefix match** -- session_id LIKE 'query%'.
    3. **Suffix match** -- session_id LIKE '%query'.

    Each tier short-circuits to the next only when its match set is
    empty. If any tier produces multiple rows, :class:`AmbiguousSessionID`
    fires with the candidates.

    Args:
        conn: open SQLite connection with the ``sessions`` table.
        query: user's input -- a full UUID, a prefix (>=4 chars), or a
            suffix (>=4 chars). Case-insensitive. The hyphens of the
            canonical form are accepted but not required.

    Returns:
        The full 36-character UUID of the unique matching session.

    Raises:
        InvalidSessionIDInput: query is empty, too short, or non-hex.
        NoSuchSessionID: no row matches the query.
        AmbiguousSessionID: more than one row matches the query at the
            first non-empty tier.

    Compact-display short-circuit: if ``query`` is the ``<head>-...-<tail>``
    form (what csb shows in ``csb list --shortid`` / ``csb search``),
    resolves directly via combined prefix+suffix match. This means users
    can copy what they SEE in csb output and paste it back as input.
    """
    # Tier 0: compact display form '<head>-...-<tail>'. If the input
    # contains '...', parse it and resolve via prefix-AND-suffix match.
    head_tail = _try_parse_head_tail(query)
    if head_tail is not None:
        head, tail = head_tail
        rows = conn.execute(
            "SELECT * FROM sessions WHERE session_id LIKE ? AND session_id LIKE ? "
            "ORDER BY session_id",
            (head + "%", "%" + tail),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["session_id"]
        if len(rows) > 1:
            raise AmbiguousSessionID(
                query, [_make_candidate(r) for r in rows]
            )
        raise NoSuchSessionID(query)

    q = _validate_query(query)

    # Tier 1: full UUID exact match
    if _FULL_UUID_RE.fullmatch(q):
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (q,),
        ).fetchone()
        if row:
            return row["session_id"]
        raise NoSuchSessionID(query)

    # Tier 2: prefix match
    prefix_rows = conn.execute(
        "SELECT * FROM sessions WHERE session_id LIKE ? ORDER BY session_id",
        (q + "%",),
    ).fetchall()
    if len(prefix_rows) == 1:
        return prefix_rows[0]["session_id"]
    if len(prefix_rows) > 1:
        raise AmbiguousSessionID(
            query, [_make_candidate(r) for r in prefix_rows]
        )

    # Tier 3: suffix match
    suffix_rows = conn.execute(
        "SELECT * FROM sessions WHERE session_id LIKE ? ORDER BY session_id",
        ("%" + q,),
    ).fetchall()
    if len(suffix_rows) == 1:
        return suffix_rows[0]["session_id"]
    if len(suffix_rows) > 1:
        raise AmbiguousSessionID(
            query, [_make_candidate(r) for r in suffix_rows]
        )

    raise NoSuchSessionID(query)


# ── CLI rendering of candidate lists (used by command error handlers) ──


def format_ambiguous_error(err: AmbiguousSessionID, *, max_show: int = 10) -> str:
    """Pretty-print an AmbiguousSessionID for terminal output.

    Returns a multi-line string. Caller writes to stderr.
    """
    lines = [
        f"Ambiguous session ID '{err.query}' ({len(err.candidates)} matches):"
    ]
    for c in err.candidates[:max_show]:
        short = format_short_uuid(c.session_id)
        name = c.session_name or "(unnamed)"
        proj = c.project or ""
        start = c.start_folder or ""
        lines.append(
            f"  {short}  {name}  ({proj})  started at: {start}"
        )
    if len(err.candidates) > max_show:
        lines.append(f"  ... and {len(err.candidates) - max_show} more")
    lines.append("Use a longer prefix or the full UUID.")
    return "\n".join(lines)

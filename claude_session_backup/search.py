"""
Transcript content search -- Phase 1 of #3 (parity-fixed in v0.3.1).

Walks the ``session_sources`` table (populated by ``csb backup``), parses
each source file for USER / AI / AGENT message blocks, and yields ``Hit``
records for every match. Literal substring is the default; regex is
opt-in via the caller's ``regex=True`` argument.

Three source channels are supported (per-session fallback):

- ``.convo_*.log`` (preferred) -- claude-session-logger's USER/AI/AGENT-only
  channel, multi-line block format: ``[[ts]] {ROLE: ... }``
- ``.sesslog_*.log`` -- same block format but also contains tool-call
  blocks (Bash, Read, Grep, ...). Tool blocks are filtered out at parse
  time; only USER/AI/AGENT survive.
- ``<uuid>.jsonl`` (authoritative fallback) -- raw Claude Code transcript.
  Delegates to :mod:`transcript_walker` for the actual JSONL walk, which
  also handles Task-launched Agent tool_use -> tool_result correlation
  (a v0.3.0 capability previously only available to the FTS5 importer).

The role-token grammar accepts ``USER``, ``AI``, ``AGENT``, and
``AGENT:<subtype>`` (e.g. ``AGENT:explore``, ``AGENT:senior-engineer``).
The subtype is preserved in the ``Hit.role`` field for downstream
filtering / display.

Context windowing is event-based (not character-based): ``-A N`` shows
the next N events after a match, ``-B N`` the previous N. For ``.convo`` /
``.sesslog`` files an "event" is a single block; for JSONL it's a single
``user``/``assistant`` message. The matched line is always yielded.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from . import fts5_db, fts_paths
from .transcript_walker import (
    ImportRow,
    format_role_label,
    iter_rows_from_jsonl,
)


# Opening of a USER / AI / AGENT[:subtype] block in .convo / .sesslog files.
# Captured groups: (timestamp, role-keyword-including-subtype, first-content)
# - The role keyword is one of USER, AI, or AGENT optionally followed by
#   ":<subtype>" where subtype is [A-Za-z0-9_-]+.
# - The first-content group captures whatever follows the role colon on the
#   opening line (empty when the block opens with just "{ROLE:" + newline).
_OPEN_RE = re.compile(
    r"^\[\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\] "
    r"\{(USER|AI|AGENT(?::[A-Za-z0-9_-]+)?):\s?(.*)$"
)


@dataclass
class Event:
    """One parsed message event from any source channel."""

    line_num: int  # 1-based line / event index within the source file
    role: str  # 'USER' | 'AI' | 'AGENT' | 'AGENT:<subtype>'
    timestamp: Optional[str]
    text: str


@dataclass
class Hit:
    """One match, ready for rendering."""

    session_id: str
    session_name: Optional[str]
    project: str
    last_active_at: Optional[str]
    source_type: str  # 'convo' | 'sesslog' | 'jsonl'
    source_path: str
    line_num: int
    role: str
    timestamp: Optional[str]
    matched_text: str
    start_folder: Optional[str] = None
    started_at: Optional[str] = None  # ISO 8601; used by --full-info renderer
    jsonl_mtime: float = 0.0  # epoch seconds; used for purge-countdown
    # --full-info 2 (level 2) additions: full folder list + meta fields.
    # Same row repeated across every Hit from this session (cheap ref).
    folders: list = field(default_factory=list)
    message_count: int = 0
    claude_version: Optional[str] = None
    context_above: list[Event] = field(default_factory=list)
    context_below: list[Event] = field(default_factory=list)
    # v0.3.5: populated only by directory-scope mode (-d / -D). Carry the
    # session's file-op summary forward so renderers can show the
    # "[N file-ops, strength=S]" suffix that motivates the ranking. Zero
    # in normal search means "not a dir-scope hit" -- renderers should
    # suppress the suffix in that case.
    strength_sum: int = 0
    file_op_count: int = 0
    # v0.3.5: best navigable transcript path for this session, independent
    # of the source the dispatcher actually walked. For file-based
    # sources this equals ``source_path``; for FTS5 we resolve the
    # session's best file-based source (convo > sesslog > jsonl) so
    # ``--files-only`` returns a file users can open instead of an
    # opaque per-project DB. None if no transcript could be resolved
    # (rare; means no session_sources rows AND no sessions.jsonl_path).
    transcript_path: str | None = None


# ── Parsers ───────────────────────────────────────────────────────────


def parse_log_blocks(path: str) -> Iterator[Event]:
    """Yield Event for every USER/AI/AGENT block in a .convo or .sesslog file.

    Both channels use the same multi-line block format::

        [[YYYY-MM-DD HH:MM:SS]] {ROLE:
        ... content ...
        }

    Single-line form (closing ``}`` on the same line as the opener) is also
    supported. Non-USER/AI/AGENT tag blocks (Bash, Read, Skill, etc.) are
    skipped entirely -- their opening line doesn't match ``_OPEN_RE``.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip("\n")
        m = _OPEN_RE.match(line)
        if not m:
            i += 1
            continue

        ts, role, first = m.group(1), m.group(2), m.group(3)
        start_line = i + 1

        # Single-line case: "{ROLE: text}" all on one line
        if first.endswith("}"):
            yield Event(
                line_num=start_line,
                role=role,
                timestamp=ts,
                text=first[:-1].rstrip(),
            )
            i += 1
            continue

        # Multi-line: accumulate until a line that is exactly "}"
        chunks: list[str] = []
        if first:
            chunks.append(first)
        i += 1
        while i < n:
            ln = lines[i].rstrip("\n")
            if ln.rstrip() == "}":
                i += 1
                break
            chunks.append(ln)
            i += 1
        yield Event(
            line_num=start_line,
            role=role,
            timestamp=ts,
            text="\n".join(chunks),
        )


def parse_jsonl_events(
    path: str, session_id: Optional[str] = None
) -> Iterator[Event]:
    """Yield Event for every conversation row produced by the shared walker.

    Delegates to :func:`transcript_walker.iter_rows_from_jsonl` so the
    JSONL path produces the same role surface as the FTS5 importer:
    USER / AI / ``AGENT:<subtype>`` for both Skill-attributed AND
    Task-launched sub-agent output.

    The walker yields both ``ImportRow`` (conversation events) and
    ``FileOpRow`` (path-bearing tool_use blocks). For search rendering
    we only need the conversation events; FileOpRow rows are quietly
    dropped here. The (v0.3.x) ``--files`` / ``-d`` query paths will
    iterate the walker themselves and consume both kinds.

    ``session_id`` is plumbed for parity with the walker's signature
    but unused in the rendered Event (the caller already knows it).
    """
    sid = session_id or ""
    msg_index = 0
    for row in iter_rows_from_jsonl(Path(path), sid):
        if not isinstance(row, ImportRow):
            continue
        msg_index += 1
        yield Event(
            line_num=msg_index,  # 1-based event index, same shape as the old parser
            role=format_role_label(row.role, row.role_subtype),
            timestamp=row.timestamp,
            text=row.content,
        )


def parse_source(
    source_type: str, path: str, session_id: Optional[str] = None
) -> Iterator[Event]:
    """Dispatch to the correct parser for a source type.

    ``session_id`` is forwarded to the JSONL parser so the walker can
    label rows with the proper session id; ``.convo`` and ``.sesslog``
    parsers ignore it (they self-contain the role/timestamp grammar).
    """
    if source_type in ("convo", "sesslog"):
        yield from parse_log_blocks(path)
    elif source_type == "jsonl":
        yield from parse_jsonl_events(path, session_id)
    else:
        return


# ── Pattern matcher ───────────────────────────────────────────────────


def _build_matcher(pattern: str, regex: bool, case_sensitive: bool):
    """Return a callable ``match(text) -> bool``.

    Literal mode: case-insensitive substring by default; ``re.escape`` is
    used so regex metacharacters in a literal pattern are inert.
    """
    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"invalid regex {pattern!r}: {e}") from e
        return lambda text: bool(compiled.search(text))

    if case_sensitive:
        return lambda text: pattern in text
    needle = pattern.lower()
    return lambda text: needle in text.lower()


# ── Source resolution ─────────────────────────────────────────────────


# Sources are first-class peers in the dispatch order. The list defines
# the default ATTEMPT ORDER -- the search dispatcher walks it for each
# session and picks the first source that's available for that session.
#
# Order rationale:
#   1. fts5     -- fast indexed search when the per-project DB exists
#                  AND is fresh enough (last_jsonl_mtime >= session
#                  mtime). One sqlite query per session instead of a
#                  file walk; preserves AGENT:<subtype> labels via the
#                  walker-shared schema.
#   2. convo    -- claude-session-logger's USER/AI/AGENT-only multi-line
#                  block format. Live: hooks keep it current between
#                  builds. No tool-call noise.
#   3. sesslog  -- same block format, also contains tool-call blocks.
#                  Slightly bigger; same parser filters tool blocks out.
#   4. jsonl    -- raw Claude Code transcript. Source of truth. Always
#                  present (Claude Code writes it). Slowest because the
#                  walker has to extract AGENT correlation each call.
#
# Each source is INDEPENDENTLY OPTIONAL. A user without
# claude-session-logger sees `convo` / `sesslog` skipped automatically.
# A user who hasn't run `csb update build-fts5` sees `fts5` skipped. A user
# who wants only one source uses `--source <name>` to pin the preference
# to a single entry. Future: per-user config can override the default
# preference list (e.g. logger-only users may want
# `["convo", "sesslog", "jsonl"]`).
_SOURCE_PREFERENCE = ("fts5", "convo", "sesslog", "jsonl")


def _resolve_preference(
    source_override: Optional[str],
    default_preference: tuple[str, ...] = _SOURCE_PREFERENCE,
) -> tuple[str, ...]:
    """Translate the user-facing ``--source`` value into a preference
    tuple the dispatcher can walk.

    ``"auto"`` / ``None`` -> ``default_preference`` (which the caller
    can adapt -- e.g. to drop ``convo`` / ``sesslog`` when no
    claude-session-logger output exists in the vault). ``"fts5"`` /
    ``"convo"`` / ``"sesslog"`` / ``"jsonl"`` -> a single-element
    tuple, pinning the user's choice so unavailable sources skip the
    session cleanly with no fallback.
    """
    if source_override in (None, "auto"):
        return default_preference
    return (source_override,)


def effective_default_preference(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Return the auto-detected source preference for this vault.

    Probes the main DB for evidence that ``claude-session-logger`` has
    ever written output (i.e. ``convo`` / ``sesslog`` rows in
    ``session_sources``). If absent, drops both from the preference
    so a logger-less user's default search naturally collapses to
    ``("fts5", "jsonl")`` -- no wasted lookups against sources that
    can't exist for them.

    User-explicit ``--source X`` always takes precedence over this
    default. This is purely for the "auto" path.
    """
    from .sesslog_parser import has_session_logger
    if has_session_logger(conn):
        return _SOURCE_PREFERENCE
    return tuple(s for s in _SOURCE_PREFERENCE if s not in ("convo", "sesslog"))


def _pick_source_for_session(
    session_row: sqlite3.Row,
    source_rows: list[sqlite3.Row],
    preference: tuple[str, ...],
    claude_dir: Optional[Path],
) -> tuple[Optional[str], object]:
    """Walk the preference list; return the first available source.

    Returns ``(source_type, handle)`` where ``handle`` is:
      - a :class:`pathlib.Path` to the per-project FTS5 DB (for ``fts5``)
      - the matching :class:`sqlite3.Row` from ``session_sources`` (for
        file-based sources)
      - ``None`` (paired with ``source_type=None``) when no source in
        the preference list is available for this session

    FTS5 availability requires the session to be in the per-project
    DB's ``indexed_sessions`` table. When the preference list has more
    than one entry (i.e. user took the default "auto" path), FTS5 also
    has to be FRESH (``last_jsonl_mtime >= session_jsonl_mtime``) so
    we don't return stale results when a perfectly good live source
    sits next in the preference. When the preference is a single
    explicit ``["fts5"]``, freshness is NOT required -- the user
    asked for FTS5 specifically and accepts possibly-stale data.

    File-based sources are available iff the corresponding row exists
    in ``session_sources``.
    """
    file_sources = {row["source_type"]: row for row in source_rows}
    explicit_choice = len(preference) == 1

    for source_type in preference:
        if source_type == "fts5":
            handle = _fts5_handle_for_session(
                session_row,
                claude_dir,
                require_fresh=not explicit_choice,
            )
            if handle is not None:
                return ("fts5", handle)
        elif source_type in file_sources:
            # Sesslog probe (#36): a shell-only .sesslog (commands +
            # output, ZERO conversation blocks) can never satisfy a
            # conversation search -- picking it would dead-end the
            # dispatch while the jsonl next in preference HAS the
            # content. Treat block-less sesslogs as unavailable and
            # keep walking. Explicit `--source sesslog` keeps today's
            # behavior (the user asked for that channel specifically).
            if source_type == "sesslog" and not explicit_choice:
                if not _sesslog_has_conversation_blocks(
                    file_sources[source_type]["source_path"]
                ):
                    continue
            return (source_type, file_sources[source_type])
    return (None, None)


def _sesslog_has_conversation_blocks(source_path: str) -> bool:
    """True if the .sesslog file contains at least one USER/AI/AGENT
    conversation block (#36).

    Early-exits on the first block opener, so convo-bearing sesslogs
    (the normal case) cost a few lines of scanning. The full-file scan
    only happens for the pathological shell-only logs this probe
    exists to skip. Unreadable/missing -> False (skip; the next source
    in preference gets its chance).
    """
    try:
        with open(source_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if _OPEN_RE.match(line.rstrip("\n")):
                    return True
    except OSError:
        return False
    return False


def _fts5_handle_for_session(
    session_row: sqlite3.Row,
    claude_dir: Optional[Path],
    *,
    require_fresh: bool,
) -> Optional[Path]:
    """Return the FTS5 DB path for this session if FTS5 is available
    (and, when ``require_fresh`` is True, fresh).

    Returns None when:
      - ``claude_dir`` is None (caller has no vault context; FTS5
        path resolution isn't possible)
      - the session row's ``jsonl_path`` is empty (we derive the
        encoded slug from it)
      - the per-project DB file doesn't exist
      - the session isn't in ``indexed_sessions``
      - ``require_fresh`` is True AND
        ``last_jsonl_mtime < session_jsonl_mtime``

    Wraps :func:`_fts5_path_if_indexed` so the dispatcher can keep
    its row-aware logic in one place.
    """
    if claude_dir is None:
        return None
    jsonl_rel = session_row["jsonl_path"]
    if not jsonl_rel:
        return None
    encoded_slug = Path(jsonl_rel).parent.name
    jsonl_mtime = (
        session_row["jsonl_mtime"] or 0.0 if require_fresh else None
    )
    return _fts5_path_if_indexed(
        claude_dir,
        session_row["project"],
        encoded_slug,
        session_row["session_id"],
        jsonl_mtime,
        jsonl_abs=Path(claude_dir) / jsonl_rel,
    )


# ── --only-aware output-row cap (v0.3.5) ──────────────────────────────


def cap_hits_by_output_unit(
    hits: list["Hit"],
    user_limit: int,
    unit: str,
) -> list["Hit"]:
    """Truncate ``hits`` so the renderer emits at most ``user_limit``
    distinct output rows.

    When ``--only`` collapses hits into sessions or files, the natural
    user expectation is ``--limit N`` = N output rows -- not N raw
    hits (a single noisy session can otherwise consume the whole
    limit and the rest of the ranked list is invisible). ``search()``
    yields hits one at a time, so the cap happens here after the
    fact: keep every hit belonging to the first N distinct units, and
    drop the rest.

    ``unit`` is either ``"sessions"`` (key on ``session_id``) or
    ``"files"`` (key on ``transcript_path`` with ``source_path`` as
    fallback). Any other value returns the input list unchanged --
    callers in non-``--only`` modes should not invoke this.

    Returns a new list; does not mutate the input.
    """
    if unit not in ("sessions", "files"):
        return list(hits)
    seen: list[str] = []
    capped: list["Hit"] = []
    for h in hits:
        if unit == "sessions":
            key = h.session_id
        else:
            key = h.transcript_path or h.source_path or ""
        if key not in seen:
            if len(seen) >= user_limit:
                break
            seen.append(key)
        capped.append(h)
    return capped


# ── Transcript-path resolution (v0.3.5) ───────────────────────────────


# File-based sources only, in user-navigability order: convo and sesslog
# are plain text (grep-friendly); jsonl is the last-resort authoritative
# fallback. FTS5 is deliberately excluded -- the per-project DB is what
# the dispatcher queries, not what a human would open.
_TRANSCRIPT_PREFERENCE = ("convo", "sesslog", "jsonl")


def _best_transcript_path(
    source_rows: list[sqlite3.Row],
    session_row: sqlite3.Row,
    claude_dir: Optional[Path],
) -> Optional[str]:
    """Pick the best human-navigable transcript file for a session.

    Walks :data:`_TRANSCRIPT_PREFERENCE` over the session's
    ``session_sources`` rows and returns the first available
    ``source_path`` (already absolute). If no file-based source rows
    exist (e.g. session indexed into FTS5 but never recorded by
    ``csb backup``), falls back to ``sessions.jsonl_path`` resolved
    against ``claude_dir``. Returns None if neither route yields a
    path (vanishingly rare in practice).

    Used to populate :attr:`Hit.transcript_path` so ``csb search
    --files-only`` returns the same kind of navigable file path
    regardless of whether the dispatcher walked the file or queried
    FTS5 -- parity with the pre-FTS5 behavior users had at v0.3.2.
    """
    by_type = {row["source_type"]: row["source_path"] for row in source_rows}
    for source_type in _TRANSCRIPT_PREFERENCE:
        if source_type in by_type and by_type[source_type]:
            return by_type[source_type]
    # Fallback: derive from sessions.jsonl_path (stored relative).
    jsonl_rel = session_row["jsonl_path"] if "jsonl_path" in session_row.keys() else None
    if jsonl_rel and claude_dir is not None:
        return str(Path(claude_dir) / jsonl_rel)
    return None


# ── FTS5 dispatch helpers (v0.3.3) ────────────────────────────────────


def _fts5_path_if_indexed(
    claude_dir: Path,
    project: str,
    encoded_slug: str,
    session_id: str,
    jsonl_mtime: Optional[float] = None,
    jsonl_abs: Optional[Path] = None,
) -> Optional[Path]:
    """Return the FTS5 DB path IF this session has an indexed row.

    When ``jsonl_mtime`` is provided, also requires the index to be
    fresh: ``indexed_sessions.last_jsonl_mtime >= jsonl_mtime``. This
    is the "auto" smart-fallback contract -- stale -> grep fallback.

    **Content-hash rescue (#36):** an mtime-stale verdict gets a second
    opinion when ``jsonl_abs`` is provided -- if the CURRENT file's
    SHA-256 equals ``indexed_sessions.last_content_hash``, the index IS
    fresh (the bytes it indexed are the bytes on disk; only the mtime
    moved -- e.g. a restore, ``rsync``, or any byte-identical rewrite).
    The hash is computed ONLY on the mtime-stale path, so the common
    fresh case stays cheap.

    When ``jsonl_mtime`` is None, returns the path as long as the
    session is in ``indexed_sessions`` at all -- regardless of staleness.
    This is the explicit ``--source fts5`` contract: the user asked
    for FTS5 specifically and accepts the possibility of stale data.

    Opens the per-project DB RAW (bypassing :func:`fts5_db.open_fts5_db`)
    so this read-only check doesn't trigger schema migrations or print
    the auto-upgrade notice during a search. A non-indexed DB just means
    "skip FTS5"; we never change DB state from inside the search path.

    Tolerates DB-open failures (corrupt file, ALTER mid-migration, etc.)
    by returning None -- search degrades gracefully to grep.
    """
    fts_path = fts_paths.fts5_db_path(claude_dir, project, encoded_slug)
    if not fts_path.exists():
        return None
    try:
        probe = sqlite3.connect(str(fts_path))
        probe.row_factory = sqlite3.Row
        row = probe.execute(
            "SELECT last_jsonl_mtime, last_content_hash "
            "FROM indexed_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        probe.close()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    if jsonl_mtime is not None and row["last_jsonl_mtime"] < jsonl_mtime:
        # mtime says stale -- second opinion via content hash (#36).
        stored_hash = row["last_content_hash"]
        if stored_hash and jsonl_abs is not None and jsonl_abs.is_file():
            from .fts5_importer import _content_hash
            if _content_hash(jsonl_abs) == stored_hash:
                return fts_path
        return None
    return fts_path


def query_fts5_for_session(
    fts5_db_path: Path,
    session_id: str,
    pattern: str,
    regex: bool = False,
) -> Iterator[Event]:
    """Yield :class:`Event` for messages in this session's FTS5 DB.

    FTS5 ``MATCH`` does the candidate narrowing; the Python-side literal /
    regex matcher in :func:`search` handles the final correctness check.
    The porter stemmer + unicode61 tokenizer used by csb's FTS5 schema
    doesn't preserve exact-substring semantics, so we always validate
    Python-side after the FTS5 filter.

    When ``regex=True``, FTS5 MATCH is bypassed entirely -- regex syntax
    (``\\d``, ``|``, ``?``, etc.) is not valid FTS5 query language, so
    feeding it to MATCH yields zero candidates and the Python regex
    filter never gets to run (the v0.3.3 bug this argument fixes).
    Full-session scan instead; the caller's matcher does all the work.

    If :func:`fts5_db.escape_fts_query` produces an empty pattern (all
    punctuation / whitespace), the same full-scan fallback kicks in.
    Matches Phase 1's "empty pattern = match all" semantics.
    """
    escaped = fts5_db.escape_fts_query(pattern) if not regex else ""
    try:
        conn = sqlite3.connect(str(fts5_db_path))
        conn.row_factory = sqlite3.Row
    except sqlite3.DatabaseError:
        return
    try:
        if escaped:
            sql = (
                "SELECT m.message_index, m.role, m.role_subtype, "
                "  m.content, m.timestamp "
                "FROM messages m "
                "WHERE m.id IN (SELECT rowid FROM messages_fts "
                "  WHERE messages_fts MATCH ?) "
                "  AND m.session_id = ? "
                "ORDER BY m.message_index"
            )
            try:
                rows = conn.execute(sql, (escaped, session_id)).fetchall()
            except sqlite3.OperationalError:
                # FTS5 MATCH can raise on bizarre tokenizations; degrade
                # to full session scan rather than dropping all hits.
                rows = conn.execute(
                    "SELECT m.message_index, m.role, m.role_subtype, "
                    "  m.content, m.timestamp "
                    "FROM messages m WHERE m.session_id = ? "
                    "ORDER BY m.message_index",
                    (session_id,),
                ).fetchall()
        else:
            rows = conn.execute(
                "SELECT m.message_index, m.role, m.role_subtype, "
                "  m.content, m.timestamp "
                "FROM messages m WHERE m.session_id = ? "
                "ORDER BY m.message_index",
                (session_id,),
            ).fetchall()
        for row in rows:
            yield Event(
                line_num=(row["message_index"] or 0) + 1,
                role=format_role_label(row["role"], row["role_subtype"]),
                timestamp=row["timestamp"],
                text=row["content"],
            )
    finally:
        conn.close()


# ── Directory-scope helpers (v0.3.5) ──────────────────────────────────


def _build_directory_globs(
    abs_path: str, include_descendants: bool
) -> tuple[list[str], list[str] | None]:
    """Build the GLOB pattern lists for ``-d`` / ``-D`` directory-scope.

    Returns ``(include_globs, exclude_globs_or_None)``:

      * ``include_globs`` -- always two patterns, one with ``\\`` separators
        and one with ``/`` separators. We OR them at query time so paths
        stored by Claude (forward-slash in most tool_use blocks; native
        backslash in some Bash output) both match without prior
        normalization.
      * ``exclude_globs`` -- only set for ``-D`` (folder-only). Two
        patterns ``<root>{sep}*{sep}*`` -- any path with at least one
        further separator past the root is excluded. The dispatcher
        applies these as ``NOT (file_path GLOB ? OR file_path GLOB ?)``.

    ``abs_path`` should already be resolved to an absolute path; this
    function only handles the separator-variant fan-out. Trailing
    separators are stripped so patterns never have a doubled ``\\\\`` /
    ``//`` at the boundary.
    """
    path_norm = abs_path.rstrip("/\\")
    back_root = path_norm.replace("/", "\\")
    fwd_root = path_norm.replace("\\", "/")
    include = [back_root + "\\*", fwd_root + "/*"]
    if include_descendants:
        return include, None
    exclude = [back_root + "\\*\\*", fwd_root + "/*/*"]
    return include, exclude


def find_path_filtered_sessions(
    fts5_db_path: Path,
    path_globs: list[str],
    exclude_descendant_globs: list[str] | None = None,
    min_strength: int = 1,
) -> list[tuple[str, int, int]]:
    """Step 1 of ``-d`` / ``-D`` ranking: sessions ordered by file-op strength.

    Runs a single grouped query against one per-project FTS5 DB's
    ``file_operations`` table and returns
    ``[(session_id, sum_strength, file_op_count), ...]`` sorted by
    ``sum_strength`` descending. Empty list if the file doesn't exist,
    fails to open, or has no matching rows.

    Pattern lists are OR'd together so callers can pass both separator
    variants without rewriting the SQL. ``min_strength`` adds
    ``AND strength >= ?`` when greater than 1; at 1 the clause is
    omitted so legacy rows (which all have strength >= 1 by default)
    are included without an unnecessary comparison.

    Opens the DB RAW (no schema init, no migrations). This is the same
    contract as the FTS5 freshness probe -- a read-only check should
    never mutate state on disk.
    """
    if not fts5_db_path.exists():
        return []
    if not path_globs:
        return []
    try:
        conn = sqlite3.connect(str(fts5_db_path))
    except sqlite3.DatabaseError:
        return []
    try:
        include_clause = " OR ".join(["file_path GLOB ?"] * len(path_globs))
        params: list = list(path_globs)
        sql_parts = [
            "SELECT session_id, SUM(strength) AS sum_strength, "
            "       COUNT(*) AS file_op_count",
            "FROM file_operations",
            f"WHERE ({include_clause})",
        ]
        if exclude_descendant_globs:
            exclude_clause = " OR ".join(
                ["file_path GLOB ?"] * len(exclude_descendant_globs)
            )
            sql_parts.append(f"  AND NOT ({exclude_clause})")
            params.extend(exclude_descendant_globs)
        if min_strength > 1:
            sql_parts.append("  AND strength >= ?")
            params.append(min_strength)
        sql_parts.append("GROUP BY session_id")
        sql_parts.append("ORDER BY sum_strength DESC")
        try:
            rows = conn.execute("\n".join(sql_parts), params).fetchall()
        except sqlite3.OperationalError:
            # Pre-strength DBs (v0.3.0) lack the column; rather than
            # silently misranking, treat as empty. The user gets no hits
            # from that DB and can run `csb update build-fts5` to migrate.
            return []
        return [(r[0], r[1], r[2]) for r in rows]
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


def _lookup_session_row(
    conn: sqlite3.Connection,
    session_id: str,
    session_filter_prefixes: list[str],
    include_deleted: bool,
    only_deleted: bool,
) -> Optional[sqlite3.Row]:
    """One-shot session lookup honoring search() filters.

    Returns the sessions row for ``session_id`` if it passes the same
    visibility filters the main path applies (deleted bits +
    ``--session-id`` prefix list). Returns None if the session doesn't
    exist or any filter rejects it.
    """
    where = ["s.session_id = ?"]
    params: list = [session_id]
    if only_deleted:
        where.append("s.deleted_at IS NOT NULL")
    elif not include_deleted:
        where.append("s.deleted_at IS NULL")
    if session_filter_prefixes:
        ors = " OR ".join(
            ["s.session_id LIKE ?"] * len(session_filter_prefixes)
        )
        where.append(f"({ors})")
        params.extend(f"{p}%" for p in session_filter_prefixes)
    sql = (
        "SELECT s.session_id, s.session_name, s.project, s.last_active_at, "
        "s.start_folder, s.started_at, s.jsonl_mtime, s.jsonl_path, "
        "s.message_count, s.claude_version "
        "FROM sessions s WHERE " + " AND ".join(where)
    )
    return conn.execute(sql, params).fetchone()


def _search_dir_scope(
    conn: sqlite3.Connection,
    pattern: str,
    *,
    abs_path: str,
    include_descendants: bool,
    min_strength: int,
    regex: bool,
    case_sensitive: bool,
    above: int,
    below: int,
    session_filter_prefixes: list[str],
    include_deleted: bool,
    only_deleted: bool,
    limit: int,
    fetch_folders: bool,
    claude_dir: Path,
) -> Iterator[Hit]:
    """Directory-scope dispatch: rank sessions by file-op strength under
    ``abs_path`` and yield hits in that order.

    Walks every per-project FTS5 DB in ``<claude_dir>/csb-fts/``, runs
    :func:`find_path_filtered_sessions` against each, merges the results,
    and sorts globally by ``sum_strength`` DESC. For each ranked session
    we look up the sessions row via :func:`_lookup_session_row`, run
    :func:`query_fts5_for_session` for the user's pattern, and yield
    :class:`Hit` with ``strength_sum`` / ``file_op_count`` populated so
    renderers can display the ranking signal.

    Sessions whose project hasn't been built into an FTS5 DB are skipped
    silently. Sessions filtered out (deleted bits, ``--session-id``
    prefix) likewise drop quietly. Empty pattern matches every event in
    the ranked sessions (mirrors the rest of search()'s "empty = match
    all" semantics).
    """
    matcher = _build_matcher(pattern, regex, case_sensitive)
    include_globs, exclude_globs = _build_directory_globs(
        abs_path, include_descendants,
    )

    fts_dir = fts_paths.fts5_db_dir(claude_dir)
    if not fts_dir.exists():
        return

    # Step 1: enumerate ranked (session_id, sum_strength, file_op_count,
    # fts_db_path) across every per-project FTS5 DB in the vault.
    ranked: list[tuple[str, int, int, Path]] = []
    for db_file in sorted(fts_dir.glob("*.db")):
        for sid, sum_strength, foc in find_path_filtered_sessions(
            db_file, include_globs, exclude_globs, min_strength,
        ):
            ranked.append((sid, sum_strength, foc, db_file))

    if not ranked:
        return

    ranked.sort(key=lambda r: r[1], reverse=True)

    hits_yielded = 0
    for session_id, sum_strength, file_op_count, fts_db_path in ranked:
        if hits_yielded >= limit:
            break

        session_row = _lookup_session_row(
            conn, session_id, session_filter_prefixes,
            include_deleted, only_deleted,
        )
        if session_row is None:
            continue

        # v0.3.5: resolve the best file-based transcript for this session
        # (parity with the non-dir-scope dispatch). One small query per
        # ranked session; cheap on indexes already in place.
        source_rows = conn.execute(
            "SELECT source_type, source_path FROM session_sources "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        transcript_path = _best_transcript_path(
            source_rows, session_row, claude_dir,
        )

        events = list(query_fts5_for_session(
            fts_db_path, session_id, pattern, regex=regex,
        ))
        if not events:
            continue

        folders_for_session: list[dict] = []
        if fetch_folders:
            folder_rows = conn.execute(
                "SELECT folder_path, usage_count, is_start_folder "
                "FROM folder_usage WHERE session_id = ? "
                "ORDER BY usage_count DESC, is_start_folder DESC",
                (session_id,),
            ).fetchall()
            folders_for_session = [dict(r) for r in folder_rows]

        for idx, ev in enumerate(events):
            if not matcher(ev.text):
                continue
            ctx_above = events[max(0, idx - above):idx] if above > 0 else []
            ctx_below = events[idx + 1:idx + 1 + below] if below > 0 else []
            yield Hit(
                session_id=session_id,
                session_name=session_row["session_name"],
                project=session_row["project"],
                last_active_at=session_row["last_active_at"],
                source_type="fts5",
                source_path=str(fts_db_path),
                line_num=ev.line_num,
                role=ev.role,
                timestamp=ev.timestamp,
                matched_text=ev.text,
                start_folder=session_row["start_folder"],
                started_at=session_row["started_at"],
                jsonl_mtime=session_row["jsonl_mtime"] or 0.0,
                folders=folders_for_session,
                message_count=session_row["message_count"] or 0,
                claude_version=session_row["claude_version"],
                context_above=ctx_above,
                context_below=ctx_below,
                strength_sum=sum_strength,
                file_op_count=file_op_count,
                transcript_path=transcript_path,
            )
            hits_yielded += 1
            if hits_yielded >= limit:
                break


# ── Main search entry point ───────────────────────────────────────────


def search(
    conn: sqlite3.Connection,
    pattern: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    above: int = 0,
    below: int = 0,
    session_filter: Optional[str | list[str]] = None,
    source_override: Optional[str] = None,
    include_deleted: bool = False,
    only_deleted: bool = False,
    limit: int = 20,
    sort_key: str = "last-used",
    fetch_folders: bool = False,
    claude_dir: Optional[Path] = None,
    dir_scope: Optional[dict] = None,
) -> Iterator[Hit]:
    """Yield ``Hit`` for every match across all relevant sessions.

    Sessions are visited in the order chosen by ``sort_key`` (default
    ``last-used`` -- most-recently-active first). Valid keys match
    ``index.SORT_SQL``: ``last-used``, ``expiration``, ``started``,
    ``oldest``, ``messages``, ``size``. The iterator stops after ``limit``
    matches.

    ``session_filter`` accepts either a single UUID prefix (str) or a list
    of UUID prefixes. Multiple prefixes OR-match: ``session_id LIKE 'a%'
    OR session_id LIKE 'b%'``. Empty list / None means "all sessions".

    ``source_override`` translates to a per-session attempt-order:

      - ``None`` / ``"auto"`` -> walk :data:`_SOURCE_PREFERENCE` (fts5 ->
        convo -> sesslog -> jsonl), picking the first source available
        for each session. FTS5 must be FRESH to win at this level.
      - any single source name (``"fts5"``, ``"convo"``, ``"sesslog"``,
        ``"jsonl"``) -> pin the preference to that one source. Sessions
        where that source is unavailable are skipped; no fallback. For
        ``"fts5"`` specifically, freshness is NOT required -- the user
        asked for FTS5 and accepts possibly-stale results.

    Each source is independently optional. A user without
    claude-session-logger naturally skips ``convo`` / ``sesslog``. A
    user who hasn't run ``csb update build-fts5`` naturally skips ``fts5``.
    A user with only the raw transcripts naturally falls all the way
    through to ``jsonl``. No "fallback" framing -- the dispatcher just
    walks the preference list in order.

    ``claude_dir`` is needed to resolve per-project FTS5 DB paths. When
    None, the dispatcher cannot evaluate the ``fts5`` source and treats
    it as unavailable for every session (the rest of the preference
    list runs normally).

    Raises ``ValueError`` if ``regex=True`` and the pattern doesn't compile,
    or if ``sort_key`` is not in ``SORT_SQL``.
    """
    # Defer import to avoid a circular dep (index.py imports nothing from
    # search.py, but pulling it in at module load time would couple the
    # two more tightly than they need to be).
    from .index import SORT_SQL
    if sort_key not in SORT_SQL:
        raise ValueError(
            f"Unknown sort_key {sort_key!r}; expected one of {sorted(SORT_SQL)}"
        )

    # Normalize session_filter to a list of prefixes for uniform OR-match.
    # We do this up here because dir-scope dispatch wants the same shape
    # without re-doing the work below.
    prefixes: list[str] = []
    if isinstance(session_filter, str):
        prefixes = [session_filter] if session_filter else []
    elif session_filter:
        prefixes = [p for p in session_filter if p]

    # v0.3.5: directory-scope mode (-d / -D). Hands off to a separate
    # dispatcher that ranks sessions by file-op strength under the
    # given path -- the rest of search()'s sort-key + preference-walk
    # machinery doesn't apply because the ordering is determined by
    # `SUM(strength)` across each session's file_operations rows.
    if dir_scope is not None:
        if claude_dir is None:
            return
        yield from _search_dir_scope(
            conn, pattern,
            abs_path=dir_scope["abs_path"],
            include_descendants=dir_scope["include_descendants"],
            min_strength=dir_scope.get("min_strength", 1),
            regex=regex,
            case_sensitive=case_sensitive,
            above=above,
            below=below,
            session_filter_prefixes=prefixes,
            include_deleted=include_deleted,
            only_deleted=only_deleted,
            limit=limit,
            fetch_folders=fetch_folders,
            claude_dir=claude_dir,
        )
        return

    matcher = _build_matcher(pattern, regex, case_sensitive)
    # Auto-mode preference adapts to the user's actual vault: drops
    # convo / sesslog when no claude-session-logger output exists.
    # Explicit --source X overrides this entirely.
    default_pref = effective_default_preference(conn)
    preference = _resolve_preference(source_override, default_pref)

    # Build the session enumeration SQL. We could join sessions to
    # session_sources directly but a two-step approach (enumerate sessions,
    # then per-session fetch their sources) makes the source-preference
    # logic above easier to read and keeps the row count small.
    where = []
    params: list = []
    if only_deleted:
        where.append("s.deleted_at IS NOT NULL")
    elif not include_deleted:
        where.append("s.deleted_at IS NULL")

    # ``prefixes`` was computed before the dir_scope branch above so both
    # dispatchers share the same normalization.
    if prefixes:
        ors = " OR ".join(["s.session_id LIKE ?"] * len(prefixes))
        where.append(f"({ors})")
        params.extend(f"{p}%" for p in prefixes)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT s.session_id, s.session_name, s.project, s.last_active_at, "
        "s.start_folder, s.started_at, s.jsonl_mtime, s.jsonl_path, "
        "s.message_count, s.claude_version "
        "FROM sessions s"
        f"{where_sql} "
        f"ORDER BY {SORT_SQL[sort_key]}"
    )

    hits_yielded = 0
    for session_row in conn.execute(sql, params):
        if hits_yielded >= limit:
            break

        # Fetch all session_sources rows for this session in one query.
        # The picker walks the preference list (FTS5 -> file sources)
        # and decides which one is available; file sources need a row
        # here, FTS5 looks at the per-project DB instead.
        source_rows = conn.execute(
            "SELECT source_type, source_path FROM session_sources "
            "WHERE session_id = ?",
            (session_row["session_id"],),
        ).fetchall()

        picked_type, picked_handle = _pick_source_for_session(
            session_row, source_rows, preference, claude_dir,
        )
        if picked_type is None:
            continue

        # v0.3.5: resolve the best file-based transcript for THIS session
        # once, regardless of which source the dispatcher picked. Used to
        # populate Hit.transcript_path so --files-only / JSON consumers
        # always see a navigable file (not a per-project FTS5 DB path).
        transcript_path = _best_transcript_path(
            source_rows, session_row, claude_dir,
        )

        if picked_type == "fts5":
            events = list(query_fts5_for_session(
                picked_handle,  # Path to per-project FTS5 DB
                session_row["session_id"],
                pattern,
                regex=regex,
            ))
            picked_source_type = "fts5"
            picked_source_path = str(picked_handle)
        else:
            # File-based source: handle is the session_sources row.
            events = list(parse_source(
                picked_handle["source_type"],
                picked_handle["source_path"],
                session_row["session_id"],
            ))
            picked_source_type = picked_handle["source_type"]
            picked_source_path = picked_handle["source_path"]

        if not events:
            continue

        # Level-2 --full-info wants the full folder list. One query per
        # matching session; shared across this session's hits via the
        # `folders_for_session` list reference below.
        folders_for_session: list[dict] = []
        if fetch_folders:
            folder_rows = conn.execute(
                "SELECT folder_path, usage_count, is_start_folder "
                "FROM folder_usage WHERE session_id = ? "
                "ORDER BY usage_count DESC, is_start_folder DESC",
                (session_row["session_id"],),
            ).fetchall()
            folders_for_session = [dict(r) for r in folder_rows]

        for idx, ev in enumerate(events):
            if not matcher(ev.text):
                continue

            ctx_above = events[max(0, idx - above):idx] if above > 0 else []
            ctx_below = events[idx + 1:idx + 1 + below] if below > 0 else []

            yield Hit(
                session_id=session_row["session_id"],
                session_name=session_row["session_name"],
                project=session_row["project"],
                last_active_at=session_row["last_active_at"],
                source_type=picked_source_type,
                source_path=picked_source_path,
                line_num=ev.line_num,
                role=ev.role,
                timestamp=ev.timestamp,
                matched_text=ev.text,
                start_folder=session_row["start_folder"],
                started_at=session_row["started_at"],
                jsonl_mtime=session_row["jsonl_mtime"] or 0.0,
                folders=folders_for_session,
                message_count=session_row["message_count"] or 0,
                claude_version=session_row["claude_version"],
                context_above=ctx_above,
                context_below=ctx_below,
                transcript_path=transcript_path,
            )
            hits_yielded += 1
            if hits_yielded >= limit:
                break

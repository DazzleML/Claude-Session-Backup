"""
pathkit -- filesystem-validated reverse decoding of Claude Code's project-dir slugs.

Claude Code stores session JSONLs at ``~/.claude/projects/<slug>/<uuid>.jsonl``
where ``<slug>`` is computed by ``sanitizePath`` in
``claude-code/utils/sessionStoragePortable.ts:311-319``::

    realpathSync(cwd).normalize('NFC').replace(/[^a-zA-Z0-9]/g, '-')

(verified upstream 2026-05-04, see notes/architecture/2026-05-04__22-32-42__
senior_sanitizePath-encoder-verification.md). The regex has no ``+``: each
individual non-alphanumeric character maps to exactly one ``-``. There is a
near-duplicate ``sanitizePath`` in ``utils/cachePaths.ts:13-19`` used only for
``env-paths`` cache logs -- not the project-dir slug -- so don't be misled
when grepping.

The encoding is lossy: path separators, colons, dots, hyphens-in-folder-names
all collapse to '-'. To answer "what cwd does ``claude --resume <uuid>`` need
in order to find this file?" we must REVERSE the encoding, which requires
filesystem validation -- try candidate decodings, keep the first that exists.

The "start at" reported by ``csb list`` and ``csb scan`` derives from this
function, not from JSONL content. See GH issue
https://github.com/DazzleML/Claude-Session-Backup/issues/19 for the full
rationale.

Multi-candidate disambiguation (#23): when a slug has more than one valid
on-disk decoding (e.g., a literal ``New--Project`` folder AND a sibling
``New\.Project`` folder), ``decode_project_slug(slug, first_cwd, folder_usage)``
picks the right one via a three-tier fallback (see the function docstring).
Callers without JSONL signals (``first_cwd=None, folder_usage=None``) get the
encoded-length heuristic -- preserving the original #19 behavior for which
``_decode_under`` is now a thin wrapper.

Known corner cases NOT yet handled (will return ``None`` rather than wrong):
  * Long slugs (>200 sanitized chars). Upstream truncates to a 200-char
    prefix + ``-`` + hash (``Bun.hash`` in CLI, ``djb2`` in Node SDK).
    No real-world cwd on the user's machine hits this today; longest
    observed is 94 chars.
  * UNC paths (``\\\\server\\share\\...``). These sanitize to a slug starting
    ``--server-share-...`` with no drive letter; the drive-letter guard
    rejects them. Mapped drive letters (``Z:\\``) work normally.

Mirrors the pattern in ``dz claude-session-metadata``'s ``decode_project_path``
(``C:\\code\\dazzlecmd\\github\\projects\\dazzletools\\claude-session-metadata\\
claude_session_metadata.py``). Kept in csb as a private module rather than a
shared dep to avoid a cross-repo coupling at the v1 stage. If a third
consumer appears, lift to a tiny shared package.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Union


# Mirrors claude-code/utils/sessionStoragePortable.ts:311-319 (the canonical
# encoder for the project-dir slug, verified 2026-05-04).
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")


def sanitize_path(name: str) -> str:
    """
    Encode a cwd to its project-dir slug -- mirror of claude-code's algorithm.

    >>> sanitize_path("C:\\\\code\\\\SYSDIAGNOSE")
    'C--code-SYSDIAGNOSE'
    >>> sanitize_path("C:\\\\")
    'C--'
    """
    return _NON_ALNUM.sub("-", name)


def _normalize_path(p: str) -> str:
    """
    Normalize a path for case/separator/trailing-slash-tolerant comparison.

    Uses stdlib helpers: ``os.path.normcase`` (case-fold on Windows, identity
    on POSIX) + ``os.path.normpath`` (collapse separators, drop trailing
    separator, eliminate ``..``). Does NOT resolve symlinks (no I/O).

    A ``dazzle_filekit.paths.normalize_path_no_resolve``-style helper would
    be a drop-in replacement here when that consolidation pass happens.
    """
    return os.path.normcase(os.path.normpath(p))


def _path_matches(p1: str, p2: str) -> bool:
    """
    True if ``p1`` equals ``p2`` exactly, OR ``p1`` is a descendant of ``p2``
    (``p1`` starts with ``p2`` followed by a separator). Both sides are
    normalized for case + separator + trailing-slash insensitivity.
    """
    if not p1 or not p2:
        return False
    n1 = _normalize_path(p1)
    n2 = _normalize_path(p2)
    if n1 == n2:
        return True
    return n1.startswith(n2 + os.sep)


def _collect_candidates(parent_dir: str, remaining: str) -> list[str]:
    """
    Find ALL paths under ``parent_dir`` whose sanitized-relative-form equals
    ``remaining``. Returns a list ordered by encoded-length descending (so the
    Tier 3 fallback in ``_disambiguate`` can pick the most-literal match by
    taking ``[0]``).

    See ``_decode_under`` for the slug-decoding semantics. The difference
    here: ``_collect_candidates`` does NOT short-circuit on the first match;
    it walks all eligible filesystem entries and accumulates every valid
    decoding. Empty list if no decoding resolves on disk.
    """
    if not remaining:
        return [parent_dir] if os.path.isdir(parent_dir) else []
    if not os.path.isdir(parent_dir):
        return []

    try:
        entries = os.listdir(parent_dir)
    except (OSError, PermissionError):
        return []

    encoded_entries = [(entry, sanitize_path(entry)) for entry in entries]
    encoded_entries.sort(key=lambda pair: len(pair[1]), reverse=True)

    found: list[str] = []
    for entry, encoded in encoded_entries:
        if not remaining.startswith(encoded):
            continue
        rest = remaining[len(encoded):]
        child = os.path.join(parent_dir, entry)
        if not rest:
            if os.path.isdir(child):
                found.append(child)
            continue
        if rest.startswith("-"):
            found.extend(_collect_candidates(child, rest[1:]))
    return found


def _disambiguate(
    candidates: list[str],
    first_cwd: Optional[str] = None,
    folder_usage: Optional[dict] = None,
) -> Optional[str]:
    """
    Pick the right candidate from a list of slug-decoded paths using JSONL signals.

    Three-tier fallback:
      Tier 1 (definitive, O(N)): if ``first_cwd`` matches any candidate
        (exact or prefix-with-separator after normalization), return that
        candidate. This is the canonical "session-open cwd" answer.
      Tier 2 (full histogram, O(N * M) where M = len(folder_usage)):
        if no candidate matches ``first_cwd`` but ``folder_usage`` is
        provided, find the candidate with the highest sum of matching
        cwd-counts (exact + prefix). Return that candidate.
      Tier 3 (no signal): fall back to ``candidates[0]``, which is the
        encoded-length-longest match per ``_collect_candidates``'s sort.
        Preserves #19's first-match behavior for callers without JSONL info.

    Empty ``candidates`` -> ``None``. Single-candidate -> that candidate
    (Tiers 1/2 are skipped; performance equivalent to #19 for the
    unambiguous case).
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Tier 1: first_cwd match
    if first_cwd:
        for c in candidates:
            if _path_matches(first_cwd, c):
                return c

    # Tier 2: folder_usage sum
    if folder_usage:
        best_candidate = None
        best_score = 0
        for c in candidates:
            score = sum(
                count for path, count in folder_usage.items()
                if _path_matches(path, c)
            )
            if score > best_score:
                best_score = score
                best_candidate = c
        if best_candidate is not None:
            return best_candidate

    # Tier 3: encoded-length heuristic (candidates already sorted)
    return candidates[0]


def _decode_under(parent_dir: str, remaining: str) -> Optional[str]:
    """
    Find a path under ``parent_dir`` whose sanitized-relative-form equals
    ``remaining``. Returns the absolute path or ``None``.

    Backward-compatible wrapper: collects all candidates and returns the
    encoded-length-longest one (Tier 3 behavior). Callers that have JSONL
    signals should call ``_collect_candidates`` + ``_disambiguate`` directly.

    See ``_collect_candidates`` for the slug-decoding semantics.
    """
    candidates = _collect_candidates(parent_dir, remaining)
    return _disambiguate(candidates, first_cwd=None, folder_usage=None)


def decode_project_slug(
    slug: str,
    first_cwd: Optional[str] = None,
    folder_usage: Optional[dict] = None,
) -> Optional[str]:
    """
    Reverse Claude Code's ``sanitizePath`` for a project-dir slug.

    Returns the cwd that, when sanitized, would produce this slug, or ``None``
    if no decoding resolves to an existing filesystem path.

    Examples (Windows; behavior mirrors on POSIX with appropriate roots):
        ``C--``                      -> ``C:\\``
        ``C--code-Foo``              -> ``C:\\code\\Foo``  (if exists)
        ``C--Users-Extreme--claude`` -> ``C:\\Users\\Extreme\\.claude`` (if exists)
        ``C--code-New--Project``     -> ``C:\\code\\New--Project`` if it exists,
                                        else ``C:\\code\\New\\.Project`` if THAT
                                        exists. Filesystem disambiguates.

    Args:
        slug: project-dir slug (e.g., ``C--code-amdead``).
        first_cwd: optional cwd from the JSONL's first event. When the slug
            decodes to multiple real folders, this is the Tier 1 oracle for
            picking the right one. Set to ``None`` for unambiguous slugs or
            callers without JSONL access.
        folder_usage: optional dict mapping cwd-paths to event counts (the
            JSONL's full cwd histogram). Used for Tier 2 disambiguation when
            ``first_cwd`` doesn't match any candidate.

    Returns:
        - The decoded cwd (string) if a candidate resolves on disk.
        - ``None`` if the slug doesn't match the drive-letter pattern OR no
          candidate decoding resolves.
    """
    if not slug or len(slug) < 3:
        return None

    # Drive-letter pattern: <letter>--<rest>
    if not (slug[0].isalpha() and slug[1] == "-" and slug[2] == "-"):
        return None

    drive = slug[0].upper() + ":"
    rest = slug[3:]
    drive_root = drive + os.sep

    # Bare drive root: ``C--`` -> ``C:\``
    if not rest:
        return drive_root if os.path.exists(drive_root) else None

    candidates = _collect_candidates(drive_root, rest)
    return _disambiguate(candidates, first_cwd=first_cwd, folder_usage=folder_usage)


def derive_start_at(
    jsonl_path: Union[str, Path],
    first_cwd: Optional[str] = None,
    folder_usage: Optional[dict] = None,
) -> str:
    """
    Compute the cwd from which ``claude --resume <uuid>`` will find this JSONL.

    Walks up to the JSONL's parent directory (the project-dir slug) and
    reverses Claude Code's encoding via filesystem-validated candidate
    decoding. When the slug decodes to multiple real folders, the ``first_cwd``
    and ``folder_usage`` arguments disambiguate via ``decode_project_slug``'s
    Tier 1/2/3 fallback chain.

    Args:
        jsonl_path: Absolute or relative path to the session JSONL file.
        first_cwd: cwd from the JSONL's first event (Tier 1 oracle). Optional.
        folder_usage: full cwd histogram from the JSONL (Tier 2 oracle).
            Optional. Pass when available to handle ambiguous slugs robustly.

    Returns:
        - The decoded cwd if filesystem validation succeeds.
        - ``"<unresolved:<slug>>"`` if the slug cannot be decoded (e.g., the
          original cwd has been deleted, or the slug pattern is unrecognized).
        - ``"<no-slug>"`` if the path has no parent directory.

    The "<unresolved:>" sentinel is intentionally a non-path string so that
    downstream rendering surfaces the slug verbatim rather than silently
    showing nothing -- maintainers debugging a stranded session need to see
    the slug.
    """
    if not jsonl_path:
        return "<no-slug>"
    p = Path(jsonl_path)
    slug = p.parent.name
    if not slug:
        return "<no-slug>"
    decoded = decode_project_slug(slug, first_cwd=first_cwd, folder_usage=folder_usage)
    if decoded is not None:
        return decoded
    return f"<unresolved:{slug}>"

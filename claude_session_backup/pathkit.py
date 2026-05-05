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


def _decode_under(parent_dir: str, remaining: str) -> Optional[str]:
    """
    Find a path under ``parent_dir`` whose sanitized-relative-form equals
    ``remaining``. Returns the absolute path or ``None``.

    The slug encoding is lossy: every non-alphanumeric character (path
    separator, ``:``, ``.``, ``-``, space, ...) collapses to ``-``. So a single
    ``-`` in the slug has multiple candidate decodings:

        ``New-Project``    <- sanitize("New-Project") = "New-Project"  (literal hyphen)
        ``New-Project``    <- sanitize("New.Project") = "New-Project"  (literal dot)
        ``New-Project``    <- sanitize("New Project") = "New-Project"  (space)

    And a ``--`` can decode as any pair of those, e.g.

        ``New--Project``   <- "New--Project"   (literal double-hyphen)
        ``New--Project``   <- "New\\.Project"  (separator + dotfile)
        ``New--Project``   <- "New-\\Project"  (trailing hyphen + separator)

    Disambiguation is purely contextual: enumerate actual filesystem entries
    under ``parent_dir`` and accept any entry whose ``sanitize_path`` form is
    a prefix of ``remaining``. If the remainder after that prefix is empty,
    we have a full match. If it begins with ``-`` (consume one as the
    next-level separator) we recurse. Longer-encoded entries are tried first
    so a literal ``New--Project`` folder beats the alternative ``New`` +
    nested ``.Project`` decoding when both exist.
    """
    if not remaining:
        return parent_dir if os.path.isdir(parent_dir) else None
    if not os.path.isdir(parent_dir):
        return None

    try:
        entries = os.listdir(parent_dir)
    except (OSError, PermissionError):
        return None

    # Sort by encoded-length descending so the most-literal interpretation
    # wins when the slug is ambiguous between a single longer name and a
    # shorter name plus subpath.
    encoded_entries = [(entry, sanitize_path(entry)) for entry in entries]
    encoded_entries.sort(key=lambda pair: len(pair[1]), reverse=True)

    for entry, encoded in encoded_entries:
        if not remaining.startswith(encoded):
            continue
        rest = remaining[len(encoded):]
        child = os.path.join(parent_dir, entry)
        if not rest:
            return child
        if rest.startswith("-"):
            # The dash represents the path separator between this entry
            # and the next level. Consume it and recurse.
            sub = _decode_under(child, rest[1:])
            if sub is not None:
                return sub
        # Otherwise: encoded was a prefix but next char isn't `-` -- the
        # match doesn't extend cleanly into a sub-path. Try the next entry.
    return None


def decode_project_slug(slug: str) -> Optional[str]:
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

    return _decode_under(drive_root, rest)


def derive_start_at(jsonl_path: Union[str, Path]) -> str:
    """
    Compute the cwd from which ``claude --resume <uuid>`` will find this JSONL.

    Walks up to the JSONL's parent directory (the project-dir slug) and
    reverses Claude Code's encoding via filesystem-validated candidate decoding.

    Args:
        jsonl_path: Absolute or relative path to the session JSONL file.

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
    decoded = decode_project_slug(slug)
    if decoded is not None:
        return decoded
    return f"<unresolved:{slug}>"

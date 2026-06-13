"""
Path conventions for per-project FTS5 databases.

``csb update build-fts5`` populates ``<claude_dir>/csb-fts/`` with one
SQLite database per project.

Naming follows claude-session-logger's ``<Name>__<UniqueID>_<USER>``
folder convention to satisfy four constraints:

1. **Per-project** -- one DB per project (not one monolithic vault).
   Deliberate deviation from claude-vault's design; rationale in the
   design doc.
2. **Multi-user safe** -- OS username in the filename so a shared
   ``~/.claude`` (rare but possible) doesn't collide.
3. **Recognizable by project name alone** -- the human-readable project
   basename appears as the first segment.
4. **Collision-free** -- same project name in different filesystem
   locations (e.g. ``C:\\code\\amdead`` vs ``C:\\code\\projects\\amdead``)
   produces distinct filenames via the slug hash.

Final pattern: ``<project>__<slug-hash>_<USER>.db``

Examples:
    amdead__a1b2c3d4_Extreme.db
    amdead__9c3d2e1b_Extreme.db    (different on-disk location)
    aichatnav__b2e7f1a9_Extreme.db

Why both ``project`` and ``encoded_slug`` parameters: ``project`` gives
human readability; ``encoded_slug`` (the ``~/.claude/projects/<slug>/``
folder name) is the uniqueness anchor. csb's ``sessions`` table records
both columns natively.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .pathkit import ClaudePaths


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_project_name(project: str) -> str:
    """Filesystem-safe project basename, max 60 chars.

    Any character outside ``[A-Za-z0-9._-]`` is replaced with a single
    hyphen. Leading / trailing hyphens are stripped. Empty input becomes
    ``"unnamed"`` so the resulting filename is always non-empty.
    """
    cleaned = _SAFE.sub("-", project).strip("-")[:60]
    return cleaned or "unnamed"


def _slug_hash(encoded_slug: str) -> str:
    """First 8 hex chars of ``sha256(encoded_slug)`` -- the collision anchor."""
    return hashlib.sha256(encoded_slug.encode("utf-8")).hexdigest()[:8]


def _current_user() -> str:
    """OS username; mirrors claude-session-logger's ``_<USER>`` suffix.

    Tries ``USERNAME`` (Windows) then ``USER`` (POSIX); falls back to
    ``"unknown"`` if neither is set.
    """
    return os.getenv("USERNAME") or os.getenv("USER") or "unknown"


def fts5_db_dir(claude_dir: Path | str) -> Path:
    """Return ``<claude_dir>/csb-fts/`` -- where per-project FTS5 DBs live."""
    return ClaudePaths.from_dir(claude_dir).fts_dir


def fts5_db_filename(
    project: str,
    encoded_slug: str,
    user: str | None = None,
) -> str:
    """Return ``"<project>__<slughash>_<user>.db"`` -- the filename only.

    Deterministic for any (project, encoded_slug, user) triple.
    """
    u = user or _current_user()
    return f"{_safe_project_name(project)}__{_slug_hash(encoded_slug)}_{u}.db"


def fts5_db_path(
    claude_dir: Path | str,
    project: str,
    encoded_slug: str,
    user: str | None = None,
) -> Path:
    """Return the absolute path to a per-project FTS5 database."""
    return fts5_db_dir(claude_dir) / fts5_db_filename(project, encoded_slug, user)


def list_fts_dbs(claude_dir: Path | str) -> dict[str, Path]:
    """Return ``{filename_stem: path}`` for every existing FTS5 DB.

    Empty dict until ``csb update build-fts5`` has built at least one
    per-project DB under ``<claude_dir>/csb-fts/``.
    """
    d = fts5_db_dir(claude_dir)
    if not d.exists():
        return {}
    return {p.stem: p for p in d.glob("*.db")}


def fts5_db_exists(
    claude_dir: Path | str,
    project: str,
    encoded_slug: str,
    user: str | None = None,
) -> bool:
    """True if a per-project FTS5 DB has been built for this triple.

    False until ``csb update build-fts5`` has indexed the project.
    """
    return fts5_db_path(claude_dir, project, encoded_slug, user).exists()

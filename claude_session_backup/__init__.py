"""
claude-session-backup - Git-backed Claude Code session backup tool.

Provides automated backup of Claude Code sessions with:
- Full session data preservation via git commits
- SQLite metadata index for fast timeline/search queries
- Deletion detection when Claude Code removes sessions
- Session restore from git history
- Working directory analysis per session

Usage:
    csb backup                    # scan, index, git commit
    csb list                      # timeline view sorted by last-used
    csb status                    # summary of sessions, deletions, git state
    csb show <session-id>         # detailed session info
    csb restore <session-id>      # restore deleted session from git
    csb search "query"            # search session metadata
    csb update rebuild-index      # safely reconstruct SQLite (preserves deleted)
    csb update build-fts5         # build/refresh FTS5 content index
    csb update backfill-deleted   # discover culled sessions from git history
"""

from ._version import __version__, get_version, get_base_version, VERSION, BASE_VERSION

__all__ = [
    "__version__",
    "get_version",
    "get_base_version",
    "VERSION",
    "BASE_VERSION",
]

# csb Documentation

The [project README](../README.md) is the quick tour; these documents are the full manual.

| Document | What it covers |
|----------|----------------|
| [commands.md](commands.md) | The complete command reference: every flag, plus deep dives on searching, distilling, purge-TTL management, and recovery (single + bulk) |
| [automation.md](automation.md) | Hands-off backups: the Claude Code plugin, manual hooks, cron, Task Scheduler, distill-on-backup |
| [maintenance.md](maintenance.md) | The `csb update` family (safe rebuild-index, build-fts5, backfill-deleted), restore semantics and guarantees, FTS5 freshness, deleted-session protection |
| [platforms.md](platforms.md) | Platform support notes (Windows / Linux / macOS / BSD) |

## Orientation in one paragraph

csb preserves every Claude Code session in your existing `~/.claude` git repository (git is the source of truth; SQLite is a rebuildable index), detects when Claude Code deletes sessions, and recovers them byte+metadata-exact -- transcript, subagents, tool-results, logger files, symlinks, and original timestamps. On top of that record sit the day-to-day verbs: `list`/`scan`/`show` for discovery, `search` for content (FTS5), `resume`/`view` to reopen a session in Claude Code or the History Viewer, and `distill` to read any session as a chat log.

## Other resources

- [CHANGELOG](../CHANGELOG.md) -- per-version detail
- [tests/checklists/](../tests/checklists/) -- the hand-runnable verification checklists each release ships with
- [GitHub issues](https://github.com/DazzleML/Claude-Session-Backup/issues) -- roadmap (#1) and brainstorm (#2) are evergreen

# csb Documentation

The [project README](../README.md) is the quick tour; these documents are the full manual.

| Document | What it covers |
|----------|----------------|
| [naming.md](naming.md) | **Start here.** Naming sessions so you can find them again: the `PROJECT__DATE__topic` convention, `/rename` vs `/renameAI`, the workflow that leaves a searchable trail, and naming forks so `csb tree` reads as a narrative |
| [commands.md](commands.md) | The complete command reference: every flag, plus deep dives on searching, distilling, purge-TTL management, and recovery (single + bulk) |
| [sets.md](sets.md) | Session sets and restart recovery: the three live/epoch views (`current`, `boot`, `last`/`last~N`/dates), activity-timeline membership, evidence tiers (`[running]`, `[open at shutdown]`, ...), named sets, promotion, and reclaiming a whole workspace after a reboot |
| [automation.md](automation.md) | Hands-off backups, both layers: the Claude Code plugin (hooks -- the machine you're using) and `csb setup schedule` (OS scheduler -- the machine you're not), plus manual hooks, interval guidance, `csb setup update` for plugin drift, and distill-on-backup |
| [maintenance.md](maintenance.md) | The `csb update` family (safe rebuild-index, build-fts5, backfill-deleted), restore semantics and guarantees, FTS5 freshness, deleted-session protection |
| [platforms.md](platforms.md) | Platform support notes (Windows / Linux / macOS / BSD) |

## Orientation in one paragraph

csb preserves every Claude Code session in your existing `~/.claude` git repository (git is the source of truth; SQLite is a rebuildable index), detects when Claude Code deletes sessions, and recovers them byte+metadata-exact -- transcript, subagents, tool-results, logger files, symlinks, and original timestamps. On top of that record sit the day-to-day verbs: `list`/`scan`/`show` for discovery, `tree` for fork lineage, `search` for content (FTS5), `resume`/`view` to reopen a session in Claude Code or the History Viewer, and `distill` to read any session as a chat log. And on top of *those* sits restart recovery: `csb set` reconstructs which sessions were active in any epoch -- before the last shutdown or forty restarts ago -- from each session's real activity timeline, knows what is open right now, and hands you a resume command per row.

Those discovery verbs all match on the session **name**, so how you name sessions decides how findable they are -- [naming.md](naming.md) is the shortest path to getting real value out of the rest.

## Other resources

- [CHANGELOG](../CHANGELOG.md) -- per-version detail
- [tests/checklists/](../tests/checklists/) -- the hand-runnable verification checklists each release ships with
- [GitHub issues](https://github.com/DazzleML/Claude-Session-Backup/issues) -- roadmap (#1) and brainstorm (#2) are evergreen

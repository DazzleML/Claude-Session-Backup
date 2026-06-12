# csb Command Reference

The complete command surface. The [README](../README.md) shows highlights; this is the full manual. Maintenance verbs (`csb update *`) have their own deep-dive in [maintenance.md](maintenance.md); automation setups (plugin, hooks, cron) live in [automation.md](automation.md).

## Full command list

```bash
csb backup                            # Scan, index, git commit (noise + user)
csb backup --no-commit                # Scan and index only
csb list [-n 20]                      # Timeline view (default sort: last-used)
csb list [keyword]                    # Filter by keyword in name/project/folders
csb list --sort expiration            # Sort by soonest-to-purge first
csb list --sort {last-used|expiration|started|oldest|messages|size}
csb list --deleted                    # Show deleted sessions
csb scan                              # Find sessions touching cwd (path-prefix)
csb scan <term>                       # Filter by term: name, project, folder paths
csb scan ./<dirname>                  # Shortcut: same as -d <dirname> (no flag to remember)
csb scan -d <pattern>                 # Path-strict: folder + descendants
csb scan -D <pattern>                 # Path-strict: this folder only, no descendants
csb scan -s <pattern>                 # start_folder only ("what sessions originated here?")
csb scan -d <pattern> <term>          # Scope-then-filter combined
csb scan -d <pattern>* / -D <pattern>* / -s <pattern>*  # Trailing-* wildcard
csb scan ... -NU                      # Skip folder-usage search (start_folder only)
csb status                            # Summary stats
csb show <session-id>                 # Detailed session info with folder analysis
csb search "query"                    # Search transcript content (USER/AI/AGENT messages)
csb search -E "regex.*pattern"        # Regex mode (Python re)
csb search "X" -C 3                   # Show 3 events of context before AND after each hit
csb search "X" -A 5 -B 2              # Asymmetric context (5 after, 2 before)
csb search "X" --source convo         # Force a source channel; auto = convo > sesslog > jsonl
csb search "X" --session <uuid>       # Constrain to one session by UUID prefix
csb search "X" --json                 # NDJSON output for piping into jq
csb restore <session-id>              # Restore deleted session from git history
csb resume <query>                    # Launch claude --resume (UUID/prefix, session NAME,
                                      #   path, folder, sesslog name, or keyword)
csb view [query]                      # Open a session in Claude Code History Viewer
                                      #   (UUID/prefix, .jsonl path, folder, sesslog name, or keyword)
csb distill <query>                   # Render a session -> ~/.claude/distilled/<slug>/<uuid>.md
csb distill <query> --stdout          # Stream the chat log to stdout (pipe to less/glow)
csb distill <query> --filter convo    # Messages only; also: tools | both (default)
csb update rebuild-index              # Safely reconstruct SQLite (preserves deleted-session metadata)
csb update build-fts5                 # Build / refresh per-project FTS5 content index
csb update backfill-deleted           # Discover culled sessions from git history; auto-repair sparse rows
csb config [key] [value]              # View/edit csb's own configuration
csb config settings:cleanupPeriodDays         # View Claude Code's session purge TTL
csb config settings:cleanupPeriodDays 365     # Set the TTL (writes ~/.claude/settings.json)
```

Common flags (`--quiet`, `--claude-dir`, `--db`) work before OR after the subcommand.

## Searching conversations

Use `csb search` to find old sessions by **what was discussed**, not just by folder or name. The query is a case-insensitive literal substring by default; `-E` switches to Python regex.

Under the hood `csb search` consults per-project **FTS5** indexes (SQLite's built-in [full-text search engine](https://sqlite.org/fts5.html), the same one that powers many IDE/Mail search bars). Run `csb update build-fts5` once to build them; after that, searching tens of thousands of messages is sub-second because FTS5 is an inverted-index lookup, not a `LIKE '%word%'` linear scan. **What's indexed**: every USER prompt, AI/assistant response, and subagent (AGENT) sidechain transcript -- plus tool calls and outputs when the raw `<uuid>.jsonl` is the source (the `.convo*` / `.sesslog*` sources from [claude-session-logger](https://github.com/DazzleML/claude-session-logger) are USER/AI/AGENT-only by design). csb stores one FTS5 database per project (`~/.claude/csb-fts/<project>__<hash>_<user>.db`) so search stays fast even when individual projects accumulate years of history.

```bash
# Find every session where you talked about OAuth callbacks
csb search "oauth callback"

# Regex with context (3 events above and below each hit)
csb search -E "refresh.*token" -C 3

# Constrain to one session and one source channel
csb search "auth flow" --session 916441e6 --source convo

# Pipe results into another tool
csb search "rate limit" --json | jq -r '.session_id' | sort -u
```

Per-session source preference is `.convo*` (preferred, USER/AI/AGENT-only) -> `.sesslog*` (filtered to USER/AI/AGENT) -> `<uuid>.jsonl` (authoritative fallback). New sessions logged by [claude-session-logger](https://github.com/DazzleML/claude-session-logger) get the cleanest `.convo*` source; older sessions fall through to JSONL automatically. Hits are sorted by session last-used time, so the most recent matches surface first. Freshness is two-tier (mtime fast-path + content-hash rescue) -- see [maintenance.md](maintenance.md#fts5-freshness-semantics-v0322-36).

For metadata search (folder paths, project, session name), use `csb list <filter>` or `csb scan <term>` -- those are the right tools for "find sessions in this folder" rather than "find sessions about this topic."

## Reading conversations (distill)

`csb search` finds the needle; `csb distill` lets you read the haystack comfortably -- an instant-messenger-style log with timestamped speaker turns (`<User>`, `<Claude>`, `<Agent:explore>`), generous separation, and one-line tool calls (`[Read] path`, `[Bash] command`) instead of walls of tool output. Markdown-friendly (Typora) and editor-friendly (Vim-jumpable file references).

```bash
csb distill <anything-that-identifies-a-session>     # writes ~/.claude/distilled/<slug>/<uuid>.md
csb distill <query> --stdout                         # stream instead; pipe to less/glow
csb distill <query> -o notes.md                      # write to a specific file
csb distill <query> --filter convo                   # messages only; also: tools | both (default)
csb distill <query> --source convo                   # force a channel (default: jsonl > sesslog > convo)
```

The distilled file is a *reading layer* -- the full JSONL remains the preserved record. Channel preference is `jsonl > sesslog > convo` (deliberately the opposite of search): distill renders csb's own preserved record, so output is identical whether or not you run the logger.

Config: `distill_policy {always|on-demand|never}` (default `on-demand`; `always` makes `csb backup` keep canonical files fresh; `never` disables distilling entirely) and `distill_filter`. See [maintenance.md](maintenance.md#csb-distill-v040-12) for policy details.

## Finding sessions at risk of purge

Claude Code auto-deletes sessions after `cleanupPeriodDays` (default 30). To see which of your sessions are closest to being purged:

```bash
csb list --sort expiration -n 20
```

Sessions are sorted by the JSONL file's modification time, so active sessions (which refresh their mtime on every interaction) stay safe while dormant sessions surface to the top of the expiration list.

To **view or change the TTL itself** without hand-editing `settings.json`:

```bash
csb config settings:cleanupPeriodDays         # show current value + source + guidance
csb config settings:cleanupPeriodDays 365     # keep transcripts for a year
csb config settings:cleanupPeriodDays 36500   # effectively never purge (~100 years)
```

The `settings:` prefix is a fully-qualified namespace: a bare key (e.g. `csb config display_top_folders`) addresses csb's own config, while a `settings:` key addresses Claude Code's `~/.claude/settings.json` -- the two never collide. The write is a read-merge-write that preserves your other settings and refuses to touch a malformed file.

> [!CAUTION]
> `cleanupPeriodDays` of **`0` does not mean "keep forever"** -- Claude Code treats it as *disable session persistence* and deletes all transcripts at its next startup. csb refuses to write `0` without `--force`. For "never purge", set a large number instead.

## Recovery

When Claude Code purges a session you wanted to keep, csb can recover it from your `~/.claude` git history. The restore is **byte+metadata-exact** on every platform csb supports: the full session footprint comes back (transcript, subagents, tool-results, logger sesslogs), symlinks are recreated as real links, and original timestamps are reapplied (mtime everywhere; creation time on Windows) -- a recovered session is indistinguishable from one that was never deleted, and sorts correctly in any file manager.

### Finding what was deleted

```bash
csb list --deleted                  # Every session csb has flagged deleted, all projects
csb list amd --deleted              # Filtered: only deleted sessions matching "amd"
csb scan --deleted                  # Deleted sessions touching cwd (or any folder)
csb scan --deleted all              # Live AND deleted together (same grammar as list/search)
csb scan -d /path/to/proj --deleted # Scoped to a specific folder (folder + descendants)
csb scan --deleted --all-folders    # Don't truncate the per-session folder list
```

The default `csb list` and `csb scan` hide deleted sessions (active-only view); the bottom of `csb list` shows a one-line footer when there are deleted sessions matching your filter so you don't have to remember to check.

### Recovering one session

```bash
csb restore <session-uuid>          # Full UUID required when DB has no row for it
csb restore <prefix>                # Prefix works when the session IS in csb's DB
csb restore <uuid> --dry-run        # Preview writes/preserves/symlinks/timestamps without writing
csb restore <uuid> --force          # Overwrite present on-disk files from git
csb restore <uuid> --jsonl-only     # Restore only the main transcript
```

If csb's DB doesn't have a row for the session (e.g., on a fresh machine), `csb restore` falls back to walking `git log --all` for `projects/*/<uuid>.jsonl`. It needs the full UUID for the fallback path. To discover deleted sessions from git that aren't in the live DB, use `csb update backfill-deleted` (see [maintenance.md](maintenance.md)).

`csb resume`, `csb view`, and `csb distill` all detect pruned sessions and offer this restore inline (`--restore-pruned` / `--no-restore-pruned` for non-interactive use).

### Recovering many sessions at once

```bash
csb scan -d <pattern> --deleted --restore --dry-run    # Preview the whole set
csb scan -d <pattern> --deleted --restore              # Confirm prompt for >1 file
csb scan -d <pattern> --deleted --restore --yes       # Skip the prompt
csb scan -d <pattern> --deleted --restore --force     # Overwrite existing on-disk files
```

Bulk restore takes the same `backup_lock` as `csb backup`, so it won't race a concurrent backup. Per-file status (`OK` / `SKIP` / `FAIL`) is printed; the final line summarizes counts.

### Restore guarantees and limits

- Present on-disk files are **preserved by default** (never clobbered); `--force` opts into overwriting from git.
- A restore-verify gate confirms the recovered transcript is genuine -- a stub or garbage blob can never silently un-delete a session.
- Symlinks are **recreated as real links, never written through** (the logger's `transcript.jsonl` gets a current-machine target; others are recreated verbatim).
- Original timestamps are derived from csb's own records (index mtime, transcript event times, git history) -- retroactively, for every session ever backed up. Unix ctime is the one thing no OS API can restore.
- Hardlinks, Windows junctions, and NTFS alternate data streams cannot be represented by git at all -- out of scope for git-based restore by construction.

> [!IMPORTANT]
> **Restored sessions can be RE-purged by Claude Code.** Its cleanup (which runs at startup) deletes transcripts whose file mtime is older than `cleanupPeriodDays` -- and because csb faithfully restores the *original* mtime, a session that was purged for age comes back still purge-eligible. csb never re-deletes anything, and nothing is ever lost (git retains it; csb re-marks it deleted at the next backup; restore again anytime), but if you want a restored old session to STAY on disk: `csb resume` it (activity refreshes the mtime), raise the TTL (`csb config settings:cleanupPeriodDays 365`), or read it via `csb distill` -- the `~/.claude/distilled/` output is outside the purge's reach entirely.

Deep dives: [maintenance.md](maintenance.md) covers the `csb update` family, restore semantics, FTS5 freshness, and the deleted-session protection guarantees.

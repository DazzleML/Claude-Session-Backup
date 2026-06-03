# csb maintenance: `csb update` and the deleted-session story

This guide covers the maintenance verbs introduced or refined in v0.3.11:

- `csb update rebuild-index` -- safely reconstruct the SQLite index
- `csb update build-fts5` -- per-project FTS5 content index
- `csb update backfill-deleted` -- discover + repair deleted-session metadata from git
- The `val:` field shown by `csb list` / `csb scan`
- `PURGED Nd ago` vs `OVERDUE by Nd` -- what each says about a session

If you came here from "why doesn't `csb update rebuild-index` lose my deleted sessions anymore," see [Safe rebuild](#csb-update-rebuild-index) below.

---

## The `csb update` umbrella

All "reach in and refresh a representation" verbs live under `csb update`:

```bash
csb update rebuild-index           # reconstruct the SQLite session index
csb update build-fts5              # build/refresh per-project FTS5 indexes
csb update backfill-deleted        # discover deleted sessions from git history
```

Each target acquires the same `backup_lock` as `csb backup` and is safe to run on a live `~/.claude/` -- concurrent backups queue rather than race. No target deletes data on success; failures are recoverable (see each section).

## `csb update rebuild-index`

Reconstruct the SQLite index without losing **deleted-session knowledge**.

The pre-v0.3.11 implementation was destructive: it deleted the entire index file then ran `csb backup`, which re-discovered only the sessions whose JSONL was still on disk. Sessions whose JSONL had been culled by Claude Code (the `cleanupPeriodDays` purge) lost their entire DB row -- name, message count, `folder_usage`, `deleted_at` flag, the `jsonl_path` pointer -- gone. Restore via `csb restore <uuid>` still worked because git history was intact, but you no longer knew the UUID existed to ask about it.

v0.3.11 makes the rebuild safe via a snapshot-then-merge dance:

1. Take `backup_lock`.
2. Snapshot every deleted-session row (plus its `folder_usage` rows) to memory.
3. Move the existing DB aside as `<db>.bak` -- never `unlink` it.
4. Run the indexer (`cmd_backup --no-commit`) against the live filesystem to rebuild the active-session view.
5. If the rebuild fails, restore `<db>.bak` and propagate the error. The user never ends up with a missing-or-corrupt DB.
6. On success, merge the snapshot back in for UUIDs the rescan didn't find (the actually-deleted ones). UUIDs the rescan re-discovered are left alone (their data is fresher).
7. Delete `<db>.bak`.

### Flags

```bash
csb update rebuild-index --include-fts5
```

Stub flag on this branch -- reserved as the integration seam for `csb backup --refresh-fts5` work landing in a follow-on release. Currently a no-op; the flag is accepted so callers can already wire it.

```bash
csb update rebuild-index --include-backfill-deleted
```

Chain a backfill pass after the rebuild. Equivalent to running `csb update rebuild-index && csb update backfill-deleted` but in one acquired-lock window.

### Crash recovery

If `csb` crashes mid-rebuild (power loss, killed mid-write, etc.), the next invocation sees a stale `<db>.bak` next to the live DB and clears it before starting fresh. The live DB itself is whatever git's `--diff-filter=D` recipe + the prior snapshot reconstruction produced -- never empty.

## `csb update build-fts5`

> **What's FTS5?** SQLite's built-in **F**ull-**T**ext **S**earch engine, version 5 ([sqlite.org/fts5.html](https://sqlite.org/fts5.html)). Instead of `LIKE '%word%'` linear scans, FTS5 builds an inverted-index of every token in your session transcripts so `csb search "oauth callback"` is sub-second across tens of thousands of messages. It's the same engine that powers Apple Mail / iMessage / many IDEs' search. csb stores one FTS5 database per project (`~/.claude/csb-fts/<project>__<hash>_<user>.db`) so search remains scoped + fast even when individual projects grow large. **What gets indexed**: every USER prompt, AI/assistant response, and subagent (AGENT) sidechain -- plus tool calls and outputs when the raw `<uuid>.jsonl` is the source. (claude-session-logger's `.convo*` and `.sesslog*` sources are USER/AI/AGENT-only by design and exclude tool noise; FTS5 uses whichever source the per-session preference selects.)

Build or refresh the per-project FTS5 content indexes that `csb search` consults. Each project (`~/.claude/projects/<slug>/`) gets its own `~/.claude/csb-fts/<project>__<hash>_<user>.db`. Idempotent: skips sessions whose JSONL `mtime` hasn't changed since the last indexer pass.

### Flags

```bash
csb update build-fts5 --project C--code-myproj    # limit to one project
csb update build-fts5 --session-id <uuid-prefix>  # limit to one session
csb update build-fts5 --force                      # re-index even if up-to-date
```

## `csb update backfill-deleted`

Discover deleted sessions from git history that the live DB doesn't know about, and synthesize sessions rows from the historical JSONL blobs. Also auto-repair existing rows whose `folder_usage` was corrupted by a past destructive rebuild (when git has richer data).

### Three things this does

For each JSONL git has ever seen deleted under `projects/*/<uuid>.jsonl`:

| Live DB state | What backfill does |
|---|---|
| No row for this UUID | **Synthesize**: extract metadata from the historical git blob, INSERT a deleted-flagged sessions row + folder_usage. |
| Row exists, `folder_usage` is intact (>1 folder rows) | **Fast-path skip**: don't read git. Stamp `metadata_validated_at` on the row (we checked the cache, the row is fine). |
| Row exists, `folder_usage` is sparse (<=1 row) AND git's blob has more folders | **Auto-repair**: re-extract metadata from git, refresh the row in place (preserves `deleted_at`, refreshes `folder_usage`). |
| Row exists, sparse, but git has nothing better | **Skip and record**: mark `metadata_validated_at` so the user knows we checked. |

### Flags

```bash
csb update backfill-deleted --dry-run    # preview, write nothing
csb update backfill-deleted --full       # full re-scan (default is incremental in future releases)
```

### Output

```
git log: 26 deleted JSONL path(s); 26 new to cache
backfill-deleted: synthesized 2 session(s), repaired 8 sparse row(s) (16 intact in live DB, 0 unreadable from git)
```

| Count | Meaning |
|---|---|
| `synthesized N` | New sessions rows created from historical blobs (UUIDs not in live DB). |
| `repaired N` | Existing sparse rows whose `folder_usage` was refreshed from git. |
| `intact in live DB` | Rows where git had nothing better (or fast-path: row already non-sparse). |
| `unreadable from git` | Git knows of the deletion but couldn't read the blob -- gitignore-window casualties (see #9). |

### The `git_deleted_jsonls` cache

Backfill populates a `git_deleted_jsonls` cache table (schema v4) recording every JSONL path git has seen deleted. Subsequent backfill passes skip rows already marked `extracted_metadata = 1`. The cache speeds up repeat runs and doesn't normally need user attention -- it's a private implementation detail.

## The `val:` field

`csb list` and `csb scan` show a `val: YY-M-D` field in the per-session footer when `metadata_validated_at` is populated:

```
id: 7db6c156-4f58-456f-b64a-e959dd29df1a | 55 messages | v2.1.63 | val: 26-6-2
restore: claude --resume 7db6c156-4f58-456f-b64a-e959dd29df1a
```

It means: **csb has cross-checked this row against source-of-truth on the given date.** Specifically, one of:

- `csb backup` re-extracted metadata from the live JSONL (active sessions, every backup pass).
- `csb update backfill-deleted` checked the historical git blob and either repaired the row or confirmed git had nothing better.

A missing `val:` field means "we haven't validated this row" -- either it's older than v0.3.11, or it was never touched by a code path that stamps the field. Run `csb backup` (for live sessions) or `csb update backfill-deleted` (for deleted sessions tracked by git) to populate it.

### What `val:` does NOT cover

- Pre-csb-era deletions that were never tracked by git or csb: no row exists to validate.
- Sessions deleted while csb's deletion-detection was broken: `last_active_at` and `message_count` may have been captured at the last good backup, but no further validation can recover what wasn't committed.

## `PURGED Nd ago` vs `OVERDUE by Nd`

Both indicate a session is past the `cleanupPeriodDays` threshold. The wording distinguishes intent:

| Wording | Meaning |
|---|---|
| `(purge in Nd)` | Active session, Claude Code will cull it in N days unless interaction refreshes the mtime. |
| `(OVERDUE by Nd)` | Live JSONL still on disk, but past the threshold. Claude Code's purger SHOULD have culled it; either it hasn't run yet or there's something keeping the file alive. **Back this up if you care.** |
| `(PURGED Nd ago)` | Session has `deleted_at` set -- the cull already happened. The number is "how long ago." |

The distinction matters because `OVERDUE` is a call to action ("we expected this to be gone but it isn't"), while `PURGED` is a past-tense record.

## Width-aware row layout

For deleted sessions, the footer carries an extra action line:

```
restore: claude --resume <uuid>
```

When the combined `id: ... | val: ... | restore: ...` line fits the console width (default 120 cols, auto-detected via `shutil.get_terminal_size`), it stays on one line. When it would overflow, the restore command splits onto its own line so the UUID stays unbroken (double-click-to-copy friendly).

If your terminal is narrower than 120 cols, expect the two-line layout even on short rows. If wider, expect the compact one-line for everything that fits.

## `csb restore` and the recovery story

`csb restore <uuid>` recovers a deleted session as completely as git lets it -- not just the main transcript, but every sidecar git has for that UUID. This is the v0.3.12+ behavior; the v0.3.11 behavior (JSONL only) is preserved behind `--jsonl-only`.

### What gets restored by default

Discovery is **table-driven** by `git_ops.SESSION_HISTORY_SCOPES` -- the single source of truth for what counts as session-history. Each row defines a `git ls-tree` pathspec scope, a UUID-keying predicate, and a category label. Adding a new category = adding one row.

| Source | Path pattern | Whose? | Why restored (whitebox citation) |
|---|---|---|---|
| Main transcript | `projects/<slug>/<uuid>.jsonl` | Claude Code | The session itself |
| Subagent sidechain | `projects/<slug>/<uuid>/subagents/agent-*.{jsonl,meta.json}` | Claude Code | Read on subagent resume to route to correct system prompt (`sessionStorage.ts:283-289`) |
| Tool-result spillover | `projects/<slug>/<uuid>/tool-results/*.{txt,json}` | Claude Code | Referenced by `<persisted-output>` tag in transcript; re-applied on resume (`toolResultStorage.ts:104-117`) |
| Remote-agent metadata | `projects/<slug>/<uuid>/remote-agents/*.meta.json` | Claude Code (CCR) | Read on resume to reconnect to still-running CCR tasks (`sessionStorage.ts:373-398`) |
| Logger state pointer | `session-states/<uuid>.{json,name-cache,source,run,started,...}` | claude-session-logger | `/renameAI` and `/sessioninfo` read `.json` directly with no fallback (`rename_session.py:206`) |
| Logger sesslog dir | `sesslogs/<sanitized-name>__<uuid>_<user>/` (recursive, including the logger's own per-session `baks/`) | claude-session-logger | The transcript-to-sesslog mapping for tool calls / shell / agent logs |
| **File-history snapshots** (v0.3.13+) | `file-history/<uuid>/<content-hash>@v<N>` | Claude Code | `/undo` reads from here on resume; without these, `/undo` shows snapshots but rewind fails at `restoreBackup()` (`fileHistory.ts:733-741`) |
| **Tasks v2 state** (v0.3.13+) | `tasks/<uuid>/{<N>.json,.highwatermark,.lock}` | Claude Code | Task-v2 reads tasks directly from disk on resume when `isTodoV2Enabled()` is true (`tasks.ts:221-227`); without these, task list silently regenerates empty + ID counter resets |
| **Session-env hooks** (v0.3.13+) | `session-env/<uuid>/*-hook-{N}.sh` | Claude Code | Read by shell-execution path to restore venv/conda activation across subshells (`sessionEnvironment.ts:15-23`); without these, env state lost between commands |

Users without `claude-session-logger` installed see no logger files attempted -- git enumeration handles both populations naturally; csb has no concept of "is the logger installed."

### What is NOT auto-restored

Intentionally excluded (whitebox-verified EPHEMERAL):

- `debug/<uuid>.txt` -- only read with `--debug-file` flag (`utils/debug.ts:232-234`); standard flows never read it.
- `todos/<uuid>-agent-*.json` -- legacy v1 storage; current Claude Code extracts todos from the JSONL transcript on resume (`sessionRestore.ts:77-93`), so the file is effectively write-only.
- `telemetry/...<uuid>.json` -- append-only retry queue for failed telemetry events; analytics gap if missing but no user-visible feature break.
- `sesslogs/bak/` (singular, sibling of per-session sesslog dirs) -- user-managed manual backup folder, NOT written by claude-session-logger. csb stays out of user-managed structures. The logger DOES write `<sesslog-dir>/baks/` (plural, nested) for housekeeping; those ARE restored because they're under the matched per-session dir.
- Project-level files like `projects/<slug>/.session_cache.json` -- not session-keyed.

If you discover one of these is needed for your workflow, file an issue -- the classification is whitebox-evidence-based but not final.

### Overwrite policy (non-destructive default)

Per-file:

| File state on disk | Default | `--force` |
|---|---|---|
| Missing | restore from git | restore from git |
| Present (bytes match git) | skip (silent) | skip (already correct) |
| Present (bytes differ from git) | **PRESERVE on-disk** | overwrite with git bytes |

The preserve-present default means re-running `csb restore` is naturally idempotent (safe to call twice; nothing changes) and local-newer logger content (e.g. sesslog appends that landed after the last `csb backup`) is never clobbered. The summary output reports the restore count and the preserve count so the operation is transparent:

```
Restored 8 files from commit ed3bdf7f.
Preserved 20 present files (kept on-disk content; use --force to overwrite from git).
  main transcript: 1
  session-states (logger): 3
  sesslogs (logger): 4
Session should now be visible in Claude Code.
```

If everything is already present (idempotent re-run), the output is explicit:

```
Nothing to restore: all 28 expected files are already on disk. Use --force to overwrite from git history if you need to revert local changes.
```

### Flags

- `--jsonl-only` -- pre-v0.3.12 behavior. Restores only `projects/<slug>/<uuid>.jsonl`. Useful for piping to a viewer.
- `--force` -- overwrite present files from git. Default behavior preserves on-disk content. Use when local files are stale or corrupted and git is authoritative.
- `--dry-run` -- show what would be restored AND what would be preserved without writing anything.

### Concurrency

`csb restore` acquires `backup_lock` for the duration of the multi-file write. A concurrent `csb backup` cannot snapshot a half-restored state. The same lock is used by `csb update *` so all maintenance / recovery operations are mutually exclusive.

### Restore-then-resume ordering

If you're going to `claude --resume <uuid>` after restore, run restore FIRST. The logger's `reconcile_session_directory` finds the restored sesslog dir by GUID-in-dirname scan; if the dir is missing when resume fires, the logger creates a fresh empty one at the same path and appends new tool calls there. Restoring the old dir AFTER that point leaves you with two parallel dirs and no automatic merge. Ordering: restore -> resume.

### Related verbs

- `csb scan -d <path> --deleted --restore` -- bulk-restores every deleted session whose original folder was under `<path>`. Uses the same per-file overwrite policy as `csb restore`.
- `csb update backfill-deleted` -- the metadata-side complement: doesn't restore files (that's `csb restore`); makes sure the DB knows about every UUID git has seen.
- **`csb resume <pruned-uuid>`** (v0.3.14+) -- detects pruned sessions and offers to restore first. On TTY: prompts "Restore from git before resuming? [Y/n]". Non-TTY: requires `--restore-pruned` (auto-yes) or `--no-restore-pruned` (abort with hint to use `csb restore`). The two flags are mutually exclusive; default (no flag) is interactive. Calls the same `_restore_session` helper as `csb restore`, so the policy stays consistent (preserve-present default; `--force` still overrides for `csb restore` itself but `csb resume`'s auto-restore step does not pass `--force`).

## Schema versions

v0.3.11 ships schema migrations through v5:

| Version | Migration | Added by |
|---|---|---|
| v3 | `session_sources` table (FTS5 source manifest) | v0.2.5 |
| v4 | `git_deleted_jsonls` cache table | v0.3.11 |
| v5 | `sessions.metadata_validated_at` column | v0.3.11 |

Run `csb update rebuild-index` (or any other csb command that opens the DB) to apply pending migrations. The framework prints `csb: migrated DB schema to vN` once per applied migration; subsequent runs are silent.

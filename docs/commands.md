# csb Command Reference

The complete command surface. The [README](../README.md) shows highlights; this is the full manual. Maintenance verbs (`csb update *`) have their own deep-dive in [maintenance.md](maintenance.md); automation setups (plugin, hooks, cron) live in [automation.md](automation.md).

## Common workflows

The day-to-day patterns, expanded from the README's quick list:

| You're thinking... | Run | Notes |
|---|---|---|
| "What sessions touched THIS project?" | `cd <project>` then `csb scan .` | The `.` shortcut scopes to the cwd (folder + descendants) with no flags to remember; add a term (`csb scan . auth`) to filter within the folder, or `-s .` for "originated here" only |
| "What was I working on before the reboot?" | `csb set show last` | The boot epoch: every session active before the machine's last shutdown, in activity order, each with a paste-able resume command. `csb list -n 5` is the manual fallback |
| "Which session discussed X?" | `csb search "X"` -> `csb distill <hit>` | Search finds it, distill makes it readable |
| "Pick up where I left off" | `csb resume <name-or-prefix-or-keyword>` | Accepts everything `claude --resume` does and more |
| "Something got purged" | `csb list --deleted` -> `csb restore <id>` | Or bulk: `csb scan -d <path> --deleted --restore` |
| "Show it to me in a GUI" | `csb view <query>` | Launches Claude Code History Viewer, detached |

## Full command list

```bash
csb setup                             # Guided onboarding: detect/init the git store, first backup
csb setup --auto                      # Same, no prompts (scripts / provisioning)
csb setup --index-only                # EXPLICIT opt-out of protection (records your sign-off)
csb backup                            # Scan, index, git commit (noise + user)
csb backup --no-commit                # Scan and index only (works without a git repo)
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
csb scan ... -NI                      # Bypass the SQLite index; read transcripts from disk
                                      #   (--no-index; cannot find DELETED sessions)
csb scan ... --json                   # Output as JSON (pure stdout; empty scope prints [])
csb tree                              # Fork lineage: which session spawned which
csb tree <filter>                     # Families containing a match (ancestors + descendants)
csb tree <filter> <path>              # ...also scoped to a folder (list + scan in one)
csb tree --root <id>                  # Just one family
csb tree --orphans                    # Sessions that never forked
csb tree -f / -ff                     # Per-node detail (same levels as list/scan)
csb set show last                     # What was active before the last shutdown (boot epoch)
csb set show last --window 24         # Same, fixed activity window in hours
csb set show last --json              # Machine-readable roster envelope
csb set new <name> <session>...       # Create a named set (curated group)
csb set show <name>                   # A named set's roster
csb set list                          # Every named set, plus the 'last' epoch
csb set add <name> <session>...       # Extend a named set
csb set rm <name> [<session>...]      # Remove members, or delete the set
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

Common flags (`--quiet`, `--claude-dir`, `--db`) work before OR after the subcommand. `csb -V` (or `--version`) prints the version with full build details when stamped (`csb BETA 0.6.1 (0.6.1_main_62-20260719-9e2c684e)`); `csb help [command]` is an alias for `--help`.

**No git repo yet? Protection is the rule; index-only is the signed exception (#52, v0.6.0).** `csb setup` is the way in: it detects an existing repo (including one rooted at an ancestor, e.g. a home-dir repo tracking `.claude/`), offers to `git init` when none exists (`--auto` skips the prompts), hardens `.gitattributes`, and runs your first backup. It closes with a state-aware checklist -- `[x]` done, `[ ]` pending (with the exact command), `[~]` optional -- covering the git store, the first backup, the auto-backup plugin, and the optional [claude-session-logger](https://github.com/DazzleML/claude-session-logger) companion (richer `csb search` + session-name channels; enhances csb but never required). Already-configured items are never re-instructed. Until the dir is protected -- or you record an explicit sign-off with `csb setup --index-only` -- **every interactive csb run prints a NO BACKUP PROTECTION banner**. A third state exists besides yes/no: **git can REFUSE a repository it can see** (the post-2.35.2 "dubious ownership" safety check -- on Windows it is elevation-dependent for Administrators-owned dirs, so the same repo can work in one shell and be refused in another). csb reports that state as what it is: the banner says `BACKUPS BLOCKED IN THIS SHELL` with git's own message, `csb status` shows `Git repo: REFUSED`, and `csb setup` diagnoses it (ownership repair first; git's `safe.directory` band-aid as an explained, default-No offer) -- and will never suggest re-initializing over an existing repo. Meanwhile the tool still functions: `csb backup --no-commit` scans and indexes every session even without a usable repo (so `list`, `scan`, `search`, `resume`, `distill` all work), saying loudly that this is `[index-only] ... NO backup protection`; this exists for the recovery moment -- a crashed or freshly rebuilt box where you need to find your sessions *before* setup is fixed. Bare `csb backup` still requires the repo and points at `csb setup`. The automation hooks fall back to index-only runs on a repo-less box, so the index stays fresh either way. Running `csb setup` after signing off index-only re-enables protection and clears the sign-off.

**Relocated `~/.claude`?** csb follows Claude Code's `CLAUDE_CONFIG_DIR` automatically (container / host-mount / worktree-isolated setups need zero csb configuration). Precedence: `--claude-dir` > `CLAUDE_DIR` > `CLAUDE_CONFIG_DIR` > `claude_dir` config key > `~/.claude`; the default DB location follows the relocation too.

**csb as a wrapper (`--` passthrough).** For the commands that launch a subtool -- `csb resume` (-> `claude`) and `csb view` (-> the history viewer) -- everything after a standalone `--` is forwarded verbatim to that tool. csb does the session resolution + `cd`; the tool gets your extra flags:

```bash
csb resume MY-PROJECT__2025-5-25__redesign -- --fork-session
#  -> resolve the name -> cd to its start folder -> claude --resume <uuid> --fork-session
csb view <query> -- <viewer-args>
```

The split happens before csb parses its own options, so a forwarded flag is never mistaken for one of csb's (`csb resume x -- --db /other` sends `--db /other` to claude, not to csb). Don't re-pass `--resume` / `-r` in the passthrough -- csb already supplies it. A command that doesn't launch a subtool (e.g. `csb list -- foo`) rejects the `--` rather than silently dropping it.

## How `csb scan`'s scope flags relate

`csb scan` answers three different questions, and the flags that narrow it fall into three groups. `csb scan --help` now prints them under those headings; this section explains why the grouping matters.

**Which sessions match** -- `-d` / `-D` / `-s` / `-NI` / `--deleted` / the positional term.
**How each match is shown** -- `--top` / `--all-folders` / `--shortid` / `--json`.
**How many survive** -- `-n`.

The distinction is not cosmetic. Through v0.7.0, `--top N` was *also* passed as a match gate, so a display setting silently decided which sessions existed. That is fixed (v0.7.1): **`--top` changes what you see; it can never change what matches.**

### The three path flags

| Flag | Matches a session when... |
|---|---|
| `-d PATH` | it touched `PATH` **or anything below it** |
| `-D PATH` | it touched **exactly** `PATH` |
| `-s PATH` | its **start folder** is `PATH` or below -- "what originated here?" |

`-d` is always a superset of `-D`. If you ever see `-d` return fewer results than `-D` on the same path, that is a bug worth reporting.

### `-NI` is a different kind of flag

`-d` / `-D` / `-s` choose **what to match against**. `-NI` (`--no-index`) chooses **where results come from**: it bypasses the SQLite index entirely, finding sessions by walking `~/.claude/projects/` and reading their transcripts. Reach for it when the index may be stale, half-built, or wrong -- it is the escape hatch that trusts the files over the database.

(Through v0.7.0 this flag was spelled `--no-usage` / `-NU`, a name that described what *falls out* of the bypass -- "only start_folder is considered" -- rather than the bypass itself. That reading made it look like a duplicate of `-s`.)

Because that walk identifies sessions by their project directory -- which is derived from the start folder -- **`-NI` on its own already behaves much like `-s`**. Two flags, two routes, nearly one destination. The difference that matters: `-s` asks the index a narrower question, while `-NI` does not ask the index at all.

### Caveats when combining them

- **`-s` and `-NI` together is redundant.** `-NI` disables the index query, which is the only place `-s`'s start-folder restriction is applied. So `csb scan -s PATH -NI` behaves exactly like `csb scan -d PATH -NI`. Use one or the other.
- **`-NI` with `--deleted` (bare, i.e. `only`) can never match.** Deleted sessions exist only in the index -- a filesystem walk cannot find a file that is gone -- so the `deleted` scope depends on the index, and `-NI` is precisely the flag that bypasses it. csb says so explicitly rather than returning a bare empty result: *"-NI cannot find deleted sessions -- they exist only in the index, which -NI bypasses. Drop -NI."* (`--deleted all` is unaffected -- the filesystem pass still runs for the live half.)
- **`-d` / `-D` / `-s` are mutually exclusive** with each other; `-NI` is not, which is why the combinations above are reachable at all.

## Choosing how much path detail you see (`csb show --paths`)

The harvester records path mentions at different confidence levels, and no single cut answers every question. `csb show --paths <level>` picks a rung on one ladder -- **selecting a level shows it and everything colder** (more trusted):

| level | shows | notes |
|---|---|---|
| `cd` | only folders the shell provably stood in | the hyper-condensed view |
| `verified` | + folders the existence probe confirmed | |
| `approximated` | + unverifiable leaves **folded upward** into their nearest existing ancestor, counts merged, marked `(~)` | "someone wrote to something LIKE `X\a`; `X` exists; count `X`" |
| `suspected` | everything stored, as extracted | **the default** -- identical to not passing the flag |
| `junk` / `raw` / `all` | declared rungs with no stored population yet | selecting one says so honestly and shows `suspected` |

This is the canonical statement of the ladder; other documents reference it. Persist a default with `csb config paths_level <level>`. The scratch-prominence boundary is also yours to tune: `csb config scratch_escape_min_work N` and `csb config scratch_escape_top_rank N`.

**Display only, permanently.** `scan` and `search` MATCH against everything stored at every rung setting -- `csb scan -D <exact-folder>` keeps finding exact-folder history no matter how condensed anyone's display is. This is a design rule with its own tests, not a current implementation detail.

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

### Multiple terms and directory-scope

Pass several terms to find sessions containing them **all** (or **any**), evaluated at the session level -- a session qualifies when its transcript holds the terms anywhere, in any order, across the same or different messages. `--match all` (default) is AND; `--match any` is OR.

```bash
csb search "oauth" "refresh token" --match all      # sessions mentioning BOTH
csb search "SC:N" "SI:N" "SA:N" -d . --match all     # ...that were also active in this folder
```

`-d <path>` / `-D <path>` scope the search to sessions that worked under a folder (`-d` recurses into subdirectories; `-D` is that folder only). Directory-scope is **source-agnostic**: the default finds *every* session that touched the folder (ranked by folder activity) and searches each in its best available source. Add `--source fts5` to rank instead by file-op strength -- active edits weigh most, reads middle, Grep/Glob probes lightest -- over sessions built into the FTS5 index (`--min-strength {1,2,3}` filters those file-ops; it applies only under `--source fts5`).

For metadata search (folder paths, project, session name), use `csb list <filter>` or `csb scan <term>` -- those are the right tools for "find sessions in this folder" rather than "find sessions about this topic."

**If you find yourself reaching for `csb search` to locate a project rather than a conversation, the names are the problem.** Metadata queries are instant and content search isn't; a session named `PROJECT__DATE__topic` is findable with `csb list PROJECT`. See [naming.md](naming.md) for the convention and how `/rename` / `/renameAI` apply it.

## Fork lineage (csb tree)

Forking a session -- `/branch`, continuing a `/rewind`, or `claude --fork-session -r` -- mints a **new** session that inherits the old one's history. `csb tree` shows how those sessions relate:

```bash
csb tree                              # the whole forest
csb tree search                       # families containing a match (match marked *)
csb tree "CLAUDE-SESSION-BACKUP*"     # trailing-* anchors a prefix
csb tree -E "^v\d-release"            # regex filter
csb tree .                            # families that worked in the current folder
csb tree search "C:\code\myproject"   # ...a filter AND a folder, in one command
csb tree --root 722c24c2              # one family
csb tree --orphans                    # sessions that never forked
csb tree -u                           # show full UUIDs (paste into claude --resume)
csb tree -ff --shortid                # per-node detail, compact UUIDs
```

```
CLAUDE-SESSION-BACKUP__adding-transcript-search  2 months ago
└── CLAUDE-SESSION-BACKUP__phase-1-grep-first  2 months ago  forked 2026-05-18, at 09:12
    ├── CLAUDE-SESSION-BACKUP__phase-2-fts5-index  6 weeks ago  forked 2026-05-24, at 14:03
    │   └── CLAUDE-SESSION-BACKUP__fts5-escaping-fix  1 month ago  forked 2026-06-21, at 17:11
    └── CLAUDE-SESSION-BACKUP__multi-term-boolean  1 month ago  forked 2026-06-21, at 19:03  *

DAZZLECMD__mode-symlinks  4 weeks ago
└── DAZZLECMD__2026-6-18__Groupables  1 month ago  forked 2026-06-18, at 01:21
    └── DAZZLECMD__2026-6-21__committing-tools  3 weeks ago  forked 2026-06-21, at 15:31

2 trees | 8 sessions | 96 never forked (--orphans to list)
```

Selection works like the rest of csb: the FILTER uses the same vocabulary as `csb list <filter>` (session name, project, UUID, folder paths) with trailing-`*` and `-E` regex added, and the folder scope behaves like `csb scan -d/-D`. A filter renders the whole **family** around each match so you see ancestors *and* descendants at once; `--lineage` narrows that to the match's own line of descent, dropping cousins.

**Folder scope is slightly wider than `csb scan`'s, on purpose.** `csb scan` treats its `--top N` display cap (default 3) as a match gate, so a session whose 4th-most-used folder is the target doesn't match. `csb tree` applies no such gate — its unit is a *family*, and the question is "did any member ever work here", so gating would hide a real chain. `csb tree <path>` can therefore surface a session `csb scan <path>` omits; never the reverse.

**Positionals are order-forgiving.** A path-shaped first argument is read as the folder scope, so `csb tree .` means "families that worked here" — the same `./`-promotion `csb scan` does. A bare `*` in the filter slot means "everything", so `csb tree * .` works too if you prefer being explicit. Anything that isn't obviously a path stays a filter, and a non-path *second* positional is rejected with a message naming `-d` rather than silently scoping to nothing.

**Identity is economical.** Named sessions show just their name — a UUID per row costs 36 columns in a view already spending width on indentation. Add `-u`/`--uuid` for full paste-ready UUIDs, or `--shortid` for the compact `<head>-...-<tail>` form; unnamed sessions always show their UUID. When a terminal is attached, names render bright and the dates/fork stamps dim, matching `csb list` and `csb scan`.

**Reading the display.** Matched sessions carry `*`. Sessions outside the current scope render dimmed as *structural connectors* -- a purged ancestor still appears (marked `[purged]`) so a chain never looks headless, and a parent csb has never indexed appears as `[not indexed]` so its children still group as siblings (try `csb update backfill-deleted` to recover it from git history). Otherwise the deleted scope matches `csb list`: active-only by default, `--deleted all` / `--deleted only` to widen or flip. Roots follow `--sort`; children always read in fork order. A family larger than `--max-nodes` (default 50) collapses with a `csb tree --root <id>` hint. Box-drawing characters are used when the console can encode them, ASCII otherwise (`--ascii` forces it).

`csb show <id>` shows the same relationships for a single session as `Forked from:` / `Forks:` lines.

**Lineage needs one backup to appear.** It is read from each transcript during indexing, so after upgrading to v0.7.0 run `csb backup` (or `csb update rebuild-index`) once for already-indexed sessions.

## Restart recovery (csb set)

A Windows Update restart destroys the one thing `csb backup` cannot preserve on its own: the knowledge of **which sessions were open together** when the machine went down. Every transcript survives; the arrangement lives in your head. `csb set show last` reconstructs it:

```bash
csb set show last                     # the boot epoch: what was active before the last shutdown
csb set show last --window 24        # fixed window instead of the default
csb set show last --json             # machine-readable envelope
```

```
Epoch 'last' -- shutdown 2026-07-25 16:16 UTC  [restart initiated by a process (update/restart)]  boot +45s
9 sessions active since the previous fence (248.0h before shutdown) (activity order -- open order is not tracked)

   1. CLAUDE-SESSION-BACKUP__2026-7-15__fixing-multiterm-search  8d20h before shutdown
      start at: C:\code\claude-projects\Claude-Session-Backup  [csb resume CLAUDE-SESSION-BACKUP__2026-7-15__fixing-multiterm-search]
   2. DAZZLECMD__2026-7-5__fiber-nuance-with-FQCN  5d19h before shutdown
      start at: C:\code\dazzlecmd\github  [csb resume DAZZLECMD__2026-7-5__fiber-nuance-with-FQCN]
   ...
```

Open your preferred terminal, position the tab, paste the row's `csb resume` command; repeat. csb never spawns windows -- which terminal, which tab, and where they go is yours.

**How it works.** The shutdown *fence* is read live from the Windows System event log (boot 6005 / clean shutdown 6006 / unexpected 6008 / restart-initiated 1074 -- the Windows Update signature), and membership comes from the activity timestamps already in the index. Read-only: nothing is stored, no hooks fire, and an update restart's double shutdown/boot cycle is collapsed into one fence.

**The default window is the whole prior epoch** (everything since the previous fence), on purpose: sessions routinely sit open-but-idle for days, and a tight window would silently miss exactly the windows you most want back. `--window <hours>` overrides it when you want just the recent tail.

**Honest by design.** "Active within the window" is evidence, not proof: a session idle longer than the window is missed, and one exited shortly before the shutdown is included. The roster therefore says *active*, never *open*, and orders by activity -- exact open/close tracking is the observation phase of the session-sets epic. Purged members appear marked, and their `csb resume` hint restores them from git on the way (same flow as any pruned resume).

**Freshness.** If the index was last updated before the shutdown, the roster warns on stderr -- the final pre-shutdown activity may not have been indexed yet. `csb backup` catches it up.

### Named sets

An epoch is observed; a **named set** is curated. It answers the other question — "which sessions do I reload *together*" — and needs no restart, no fences, and no foresight beyond deciding the group:

```bash
csb set new CSB-STACK <session> <session>   # create from any session queries
csb set show CSB-STACK                      # same numbered roster as an epoch
csb set add CSB-STACK <session>             # extend
csb set rm  CSB-STACK <session>             # remove a member
csb set rm  CSB-STACK                       # delete the whole set
csb set list                                # every named set, plus the 'last' epoch
```

Members resolve with csb's usual vocabulary (UUID or prefix, session name, path, keyword) and are **stored as full UUIDs**, so renaming a session later never breaks a set. Naming conventions and the three reserved names live in [naming.md](naming.md#naming-sets).

**Where they live, and why it matters.** Sets are stored in `csb-sets.json` in your backup store and committed in the *user* class alongside settings and skills. That is deliberate: set membership is not derivable from transcripts, so an index table would be silently erased by `csb update rebuild-index` — a command csb actively recommends. As user data in git, sets survive rebuilds by construction, ride existing backups, and a bad edit is one `git checkout` away. A corrupt sets file is therefore reported rather than silently reset.

A member that has left the index (purged beyond recovery, or an index needing a rebuild) is shown **marked, not dropped** — a set that quietly shrinks is a set that lies about what it holds.

Index addressing (`csb resume set <N>`) is the next phase of the same epic.

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
csb list auth --deleted             # Filtered: only deleted sessions matching "auth"
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
csb restore <session-name>          # Names, paths, folders, keywords -- same surface as resume/view/distill
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

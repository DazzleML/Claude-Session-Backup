# Session Sets

Claude Code preserves *sessions*; nothing preserves the **arrangement** -- which sessions were open together, which group served one task, what a forced restart scattered across fifteen windows. csb's session sets capture that arrangement three ways:

- **Observed sets** (zero effort): what is open right now (`current`); everything active since startup (`boot`); what was active before any restart in recorded history (`last`, `last~1`, dates).
- **Named sets** (curated): the groups you declare, extend, and reload.
- **The bridge**: freeze any observed view into a named set with one command, then reclaim it window by window -- csb supplies the addressing, never the windows.

**Scope, stated plainly: only Claude Code sessions are tracked.** Your editors, browsers, and other terminal windows are invisible to csb -- a roster answers "which *Claude sessions* were open", never "what was on my screen".

## The views

| View | Question it answers | Source |
|---|---|---|
| `csb set show current` | what is open right now? | the Live Session Registry (hooks) + process verification |
| `csb set show boot` | what has run since startup? | boot instant + activity index + registry |
| `csb set show last` | what was active before the restart? | boot/shutdown fences + activity index |
| `csb set show last~2` | ...and before the restart before that? | the same, addressed into history |
| `csb set show 2026-7-15` | what was I running that Tuesday? | date-addressed epochs (ambiguity listed, never guessed) |
| `csb set show <name>` | a group I declared | `csb-sets.json` (rides the user commit) |

`show` is implicit: `csb set boot`, `csb set last~2`, and `csb set MY-GROUP` all work bare. `csb set list` is the overview -- named sets, the live row, and the epoch table (up to five addressable epochs with local times and causes).

## Evidence tiers

Every roster row says only what can be shown, never more:

| Tag | Means | Proof |
|---|---|---|
| `[running]` | provably open | a live process owns the session -- verified by the exact host pid the SessionStart hook recorded, which survives forks and in-app session switching |
| `[no exit observed]` | open per the registry, unproven | registry entry; no process proof |
| `[exited]` | opened this boot, closed cleanly | its close *erased* the registry entry |
| `[open at shutdown]` | was open when the machine went down | the boundary snapshot for that epoch |

The asymmetry is the design: a clean close *erases*; an unclean one *testifies*. Leftover registry entries with no process behind them are crash evidence, not garbage. One pid belongs to one row: when claims collide (a fork's frozen command line naming its parent; a switched session's stale identity), captured evidence beats command-line matching and the freshest capture wins.

## Restart recovery, end to end

```bash
csb set list                      # which epochs are addressable?
csb set show last                 # the roster: who was active before the restart
csb resume --set 2                # reclaim member 2, in THIS terminal
csb resume --set last 2 -- --fork-session    # or branch it instead
```

csb never spawns a terminal, window, or tab -- you choose where each session lands. Roster numbers are canonical: `--window` narrows what is *shown* (gaps, never renumbering), so the number you read always addresses the session you meant. `--open` narrows to members a boundary snapshot proves were open at the shutdown -- exact within the retained history (five boundaries), honestly absent beyond it.

## Named sets and promotion

```bash
csb set new CSB-STACK <session> <session>   # declare
csb set new CSB-STACK boot:1 boot:8 boot:15 # ...or grab roster rows by number
csb set add CSB-STACK <session>             # extend
csb set new TUESDAY --from 2026-7-15        # freeze a whole epoch
csb set new NOW --from current              # freeze what is open right now
csb resume --set CSB-STACK                  # the reclaim menu: what is NOT open yet
csb resume --set CSB-STACK 3                # open member 3 here
```

A set promoted from an epoch remembers its source: `promoted from 'last~2' (shutdown ...)` -- provenance that stays true as that epoch's address drifts deeper into history. The reclaim menu is driven by liveness, not stored progress: exiting a session puts it back on the list, because its clean close erased its registry entry.

## Naming

Set names follow the same conventions as session names (see [naming.md](naming.md)). The grammar reserves `last`, `current`, `boot`, `set`, bare integers, `last~N`, and bare-date shapes (`2026-8-9`) -- suffixed forms like `2026-8-9__release-day` stay legal.

## Retention notes

- Boundary snapshots keep the newest **5** restarts under `csb-live/boundaries/` (~1KB each); `last-shutdown.json` remains the newest-alias for older readers.
- Epoch *rosters* come from the activity index, which is only as deep as your session files. Claude Code prunes transcripts on a timer (`cleanupPeriodDays`); **90 days is a comfortable setting** that keeps a full quarter of epochs reconstructable -- csb's git store preserves the data regardless, but the live index is what rosters read.
- The registry itself is versioned by the backup store's noise commits -- `git log -- csb-live/` is a free open/close event history, which is why a dedicated event log stays on the roadmap rather than in the code.

## Platform notes

`current`, `boot`, and named sets are pure cross-platform. Epoch views read OS fence sources -- the Windows event log, or the journalctl/wtmp chain on POSIX (including the honest `unknown` cause where sources cannot testify). WSL's "boots" are VM starts, not machine restarts. Details: [platforms.md](platforms.md).

## Where it's headed

Parked ideas live in the epic's issue tracker: grouping suggestions (which sessions *travel together*), cached epoch summaries, and the dedicated event log noted above.

## See also

- [commands.md](commands.md) -- the authoritative command reference
- [naming.md](naming.md) -- naming conventions and reserved names
- [platforms.md](platforms.md) -- what works where

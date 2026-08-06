# Session Sets

> **Status: this document is a scaffold.** The featureset is mid-build (epic
> is in its sixth phase); sections marked *(planned)* describe direction, not
> shipped behavior. The full write-up lands once the phase completes.
> [commands.md](commands.md) is the accurate per-command reference today.

## Intent

Claude Code preserves *sessions*; nothing preserves the **arrangement** -- which
sessions were open together, which group served one task, what a forced restart
scattered. csb's session sets capture that arrangement three ways:

- **Observed sets** (zero effort): what is open right now; what this boot has
  touched; what was active before any previous shutdown.
- **Named sets** (curated): the groups you declare and reload.
- **The bridge**: freeze any observed view into a named set with one command,
  then reclaim it window by window -- csb supplies the addressing, never the
  windows.

## The views

| View | Question | Source | Since |
|---|---|---|---|
| `csb set show current` | what is open right now? | the Live Session Registry (hooks) + process verification | v0.8.4 |
| `csb set show boot` | what has run since startup? | boot instant + activity index + registry | v0.8.5 |
| `csb set show last` | what was active before the restart? | boot/shutdown fences + activity index | v0.8.0 |
| `csb set show last~N` / a date | ...and before earlier restarts? | fence history + boundary snapshots | *(planned)* |
| `csb set show <name>` | a group I declared | `csb-sets.json` (user commit class) | v0.8.1 |

## Evidence tiers

Every roster row says only what can be shown, never more:

| Tag | Means | Proof |
|---|---|---|
| `[running]` | provably open | a live process owns the session |
| `[no exit observed]` | open per the registry, unproven | registry entry, no process match |
| `[exited]` | opened this boot, closed cleanly | its close erased the registry entry |
| `[open at shutdown]` | was open when the machine went down | the boundary snapshot |

The asymmetry is the design: a clean close *erases*; an unclean one *testifies*.

## Use cases

- **The Windows Update special**: reboot kills 15 windows -> `csb set show last`
  -> reopen what mattered, one terminal at a time.
- **The standing group**: `csb set new CSB-STACK --from current`, then
  `csb resume --set CSB-STACK` any morning to see what's left to open.
- **Crash forensics**: leftovers in `current` that no process backs are the
  sessions that died without a goodbye.
- **The audit trail** *(planned)*: what did this machine work on last Tuesday?
  -- address the epoch by date.

## Where it's headed

Platform parity (`last` on Linux/macOS/BSD -- `current`, `boot`, and named
sets are already cross-platform), epoch history with `last~N` and date
addressing, richer promotion. Parked ideas (grouping suggestions,
cached summaries) live in the epic's issue tracker. Deeper history has a
free foundation already: the backup store's noise commits version the
registry, so every open/close is in `git log` even before a dedicated
event log exists.

## See also

- [commands.md](commands.md) -- the authoritative command reference
- [naming.md](naming.md) -- set naming conventions and reserved names
- [platforms.md](platforms.md) -- what works where

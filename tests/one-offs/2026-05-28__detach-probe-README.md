# Detach-survival probe — read this after restart

**Goal:** find out whether a process spawned by the csb SessionEnd hook **survives Claude Code session teardown** on Windows. This decides the v0.3.7 "durable on-exit backup" design (detach-primary vs deferral-floor). See design doc:
`2026-05-28__10-56-54__durable-on-exit-backup-detached-vs-deferred.md`

## RESULT (2026-05-28) — RAN, CONCLUSIVE

- **Detach FAILED.** Live exit/restart logged `hook start` → `spawned detached child mode=breakaway` → `hook end`, but **no `detached-survived`** (child confirmed DEAD). `CREATE_BREAKAWAY_FROM_JOB` was *permitted* (no spawn error), yet the child still died → Claude Code **tree-kills descendants on exit** (not job-kill-on-close); subprocess detachment can't escape it.
- **Active hook = the live REPO copy** (`script=C:\code\...\Claude-Session-Backup\hooks\scripts\backup-hook.py`), NOT the 0.2.0 cache — directory-source marketplace runs in-place.
- **Scheduler handoff PASSED** (follow-up probe, `sched_probe_target.py`): a `schtasks` one-shot ran under `svchost`/`Schedule` (ppid outside Claude Code's tree) → survives teardown. **This is the chosen mechanism.**
- Evidence + reusable code preserved in `private/snippets/detach_probe.py` and `private/snippets/scheduler_handoff_probe.py`.
- Probe instrumentation has been reverted from both hook copies.

## What was instrumented (TEMPORARY — revert after reading results)

`_detach_probe()` added + called at top of `main()` in BOTH:
1. **Active hook (cache copy):** `~/.claude/plugins/cache/dazzle-claude-session-backup/claude-session-backup/0.2.0/hooks/scripts/backup-hook.py`
2. **Repo copy (insurance):** `hooks/scripts/backup-hook.py`

On SessionEnd the probe: logs `hook start`, spawns a **detached** child (Windows: `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP|CREATE_BREAKAWAY_FROM_JOB`; POSIX: `start_new_session`), logs `hook end`, then the child **sleeps 8s** (past teardown) and appends `detached-survived`. The real `csb backup` still runs.

Probe log: `~/.claude/csb-logs/detach-probe.log` (cleared before the test).

## How to read the result

```
cat ~/.claude/csb-logs/detach-probe.log
```

| Log contents | Meaning | Design implication |
|---|---|---|
| `hook start` + `spawned ... mode=breakaway` + `detached-survived` (~8s later) | Detach WORKS; breakaway permitted; child outlived teardown | **Detach-primary (Path A) viable** — build it |
| `breakaway spawn FAILED ...` then `detached-survived` | Job forbids breakaway, but a plain-detached child still survived | Detach works without breakaway; fine |
| `breakaway spawn FAILED ...` + NO `detached-survived` | Job forbids breakaway AND kills the child | **Windows kill-on-close job → detach impossible → deferral floor (Path F-floor)** |
| `hook start` + `hook end` + NO `detached-survived` | Hook ran fully but the detached child was killed during its sleep | kill-on-close job → deferral floor |
| only `hook start` (no `hook end`) | Hook itself killed near-instantly | aggressive teardown; deferral floor |
| `script=...cache...0.2.0...` vs `script=...code\Claude-Session-Backup...` | tells which hook copy is the ACTIVE one (CLAUDE_PLUGIN_ROOT) | informs whether to fix the stale 0.2.0 plugin |

Also confirms the broader diagnosis: a `hook start` with no `detached-survived` is direct proof the teardown kills hook children (the root cause of the stale-index + leaked-lock incident).

## Revert (after reading results)

```
# repo copy — git revert:
git -C C:/code/claude-projects/Claude-Session-Backup checkout hooks/scripts/backup-hook.py
# cache copy — remove _detach_probe() func + the 4-line call in main(),
# OR (cleaner) refresh from the marketplace, which also fixes the stale 0.2.0 plugin:
#   claude plugin update claude-session-backup@dazzle-claude-session-backup   (restart required)
# child script artifact:
rm ~/.claude/csb-logs/_detach_child.py   # truly throwaway
```

## Then

Resume the v0.3.7 design from the doc above, now with the empirical detach verdict. Note the user's refinement: keep the git **commit on exit**, but as a **separate job** from the index update (two jobs, not one monolithic backup) so a slow/killed commit can't take down the fast index.

# Automating csb backups

csb is designed for unattended operation (`--quiet`, `--no-gpg-sign` commits, a PID-aware lock file). There are two automation layers, and they protect against different things -- most setups want **both**. They coexist safely because every entry point takes the same `backup_lock`.

| Layer | Fires when | Protects |
|-------|-----------|----------|
| Claude Code plugin (hooks) | you actually use Claude Code (PreCompact, SessionEnd) | the session you are working in right now |
| Scheduled backup (`csb setup schedule`) | on a timer, via the OS scheduler | machines you are NOT using |

## Why the schedule matters: the idle-machine gap

Every csb hook fires on user action -- compacting, ending a session, starting Claude Code. A machine that just sits there fires none of them. Meanwhile Claude Code's own startup cleanup deletes transcripts older than its retention period (`cleanupPeriodDays`, default 30 days) whether or not they were ever backed up. On a laptop you set aside for six weeks, or a desktop you only open Claude Code on occasionally, the purge countdown runs and the hooks never do. The scheduled backup is the layer that closes that gap: the OS runs `csb backup` on a timer, no Claude Code required.

## Scheduled backup: `csb setup schedule`

```bash
csb setup schedule              # guided: asks for an interval, installs, prime-runs
csb setup schedule --auto      # no prompts, 24h interval
csb setup schedule --interval 720   # explicit cadence in minutes
csb setup schedule --dry-run   # print exactly what would be installed
csb setup schedule --status    # health check: is it installed AND actually running?
csb setup schedule --remove    # remove the csb entry (only the csb entry)
```

One command, one behavior, four platforms: Windows Task Scheduler (task XML via `schtasks`), cron (Linux, BSD, WSL), launchd (macOS). The installed entry carries fully-baked context -- absolute interpreter, `--claude-dir`, `--db` when non-default, `--log-file` -- because scheduler environments do not source your shell profile; an entry that says just `csb` works in your terminal and silently fails in the scheduler.

Install also runs an immediate **prime backup** through the scheduler's own launcher, so a broken entry fails on your screen at install time instead of silently at the first real fire. Every scheduled run appends one line to `<claude-dir>/csb-logs/schedule.log`; `csb setup schedule --status` (and the `Scheduled:` line in `csb status`) reads that evidence and will say **INSTALLED BUT NOT RUNNING** if the entry exists but nothing has fired within twice the expected interval -- the failure mode hand-rolled schedules never report.

### Choosing an interval

The guided setup offers 15 minutes, 12 hours, and 24 hours (the default), plus custom values (a divisor of 60 minutes, or whole hours dividing 24).

- **24 hours (default)** -- one snapshot a day is enough to beat a 30-day purge window with a wide margin, at near-zero cost. Intervals of an hour or more fire at the time of day you ran setup (a machine in use *now* is likely in use at this hour on other days) -- never a hardcoded midnight, which on a machine that is off overnight would simply never run.
- **12 hours** -- halves the worst-case data-at-risk window for active multi-machine setups.
- **15 minutes** -- near-continuous protection. Worth knowing: a backup is not free once your store grows. A no-op run (nothing changed) on a ~270-session store measures around 55 seconds, most of it git work; at 15-minute cadence that is a background git workload every quarter hour. Fine on a fast desktop, noticeable on a laptop on battery. If in doubt, start at 24h -- the hooks already cover the machine you are actively using.

The run log records `duration_ms` per run, so if backups start taking long enough to matter you will be able to see it in `<claude-dir>/csb-logs/schedule.log`.

### Platform notes

- **Windows**: the task is created non-elevated for the current user, runs `pythonw` (no console window), starts on battery, and uses `StartWhenAvailable` so a fire missed while the machine slept runs at wake. If you previously created the hand-rolled "Claude Session Backup" task from an older version of this page, `csb setup schedule` will detect it and print the removal command -- the old task double-runs backups and its bare `csb` breaks whenever PATH changes.
- **Linux/BSD**: installs a marker-fenced block in your user crontab; everything outside the block is never touched. Setup first verifies a cron *daemon is actually running* -- a crontab entry with no daemon looks installed and never fires, so csb refuses to install into that state and prints per-distro install instructions instead (`csb setup schedule` exits with code 11). Minimal container images frequently ship no cron at all; this refusal is a first-class path, not an edge case.
- **No cron and can't install it?** `csb setup schedule --print-systemd` prints a ready-to-paste systemd user timer recipe. csb does not install or manage systemd units -- note the recipe's lingering caveat: user timers stop when your last login session ends unless `loginctl enable-linger` is set.
- **WSL**: cron inside WSL only runs while the distro is running. For an always-on schedule protecting a Windows-side Claude dir, run `csb setup schedule` from Windows instead. Setup detects WSL and says so.
- **macOS**: a LaunchAgent with `StartCalendarInterval` (fires catch up after sleep at the next matching time). Over SSH with no GUI session the agent may not load until next login -- install reports exactly that instead of claiming success.

## Claude Code Plugin (hooks)

The repository ships as a Claude Code plugin that registers PreCompact and SessionEnd hooks automatically. You can install it straight from GitHub -- no clone required:

```bash
# Add the DazzleML marketplace (one-time)
claude plugin marketplace add "DazzleML/Claude-Session-Backup"

# Install the plugin
claude plugin install claude-session-backup@dazzle-claude-session-backup
```

Alternatively, if you already have a clone for development:

```bash
# From a clone of this repo
claude plugin marketplace add ./
claude plugin install claude-session-backup@dazzle-claude-session-backup
```

The plugin uses a Node.js bootstrapper (`run-hook.mjs`) to find the correct Python binary on each platform, so it works reliably on Windows, Linux, and macOS without any shell quoting concerns. PreCompact fires synchronously before `/compact` to preserve full conversation detail; SessionEnd fires on exit to catch any remaining changes.

The hook writes its own diagnostics to `<claude-dir>/csb-logs/`. Relocated setups are followed via `CLAUDE_DIR` or `CLAUDE_CONFIG_DIR` (the same env vars the csb CLI honors); a relocation expressed only through `--claude-dir` or the config file does not reach the hook -- set the env var for hook-driven setups.

### Keeping the plugin current: `csb setup update`

The plugin is the delivery vehicle for the hook *scripts*, so hook-side improvements only reach you when the installed plugin updates -- `pip install -U` alone is not enough. The converse is equally true: `csb setup update` alone is not enough either, since it syncs the plugin and never touches the CLI. Keeping csb healthy means keeping *both* halves moving.

- **`csb status`** shows a `Plugin:` line: `<version> (matches CLI)` in green when the two agree, a yellow drift line when they do not, and a calm "not installed (optional)" for CLI-only users. The version it names is the one Claude Code actually loads, read from Claude Code's own plugin registry -- not merely the newest one sitting in the plugin cache, which keeps every version ever fetched and prunes none.
- **Drift has two directions with different fixes.** When the plugin trails the CLI, the fix is `csb setup update`. When the CLI trails the plugin, the fix is `pip install -U claude-session-backup` -- updating the plugin again would change nothing. csb names whichever one applies. When the two versions cannot be ranked against each other (a pre-release, say), it reports the pair and deliberately names no fix rather than guessing a direction.
- **`csb setup update`** runs the update for you (the exact `claude plugin update <name>@<marketplace>` incantation is easy to forget) and reports the before/after versions. No session restarts are needed afterwards: hooks resolve the installed plugin at fire time. If it completes cleanly without fetching anything while a gap remains, the marketplace is the stale link -- re-add it with `claude plugin marketplace add "DazzleML/Claude-Session-Backup"` and retry.

Note what none of this checks: whether a newer csb exists at all. `(matches CLI)` means the two halves agree with each other -- not that either is current with what has been released.

## Manual hook installation

If you prefer to manage hooks yourself, add this to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreCompact": [{"hooks": [{"type": "command", "command": "csb backup --quiet"}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "csb backup --quiet &"}]}]
  }
}
```

Or use `python install.py` in the repo to copy the hook script and print the snippet.

(Bare `csb` is fine *here* -- hooks run in your login environment where PATH is yours. It is scheduler entries that must never rely on it.)

## Migrating from a hand-rolled schedule

Older versions of this page suggested raw `crontab` and `schtasks` one-liners. If you set one up, replace it -- the hand-rolled entries invoke bare `csb`, which silently stops working when PATH differs or changes, and nothing ever tells you:

```bash
# Linux/macOS: remove your old line from `crontab -e`, then:
csb setup schedule
```

```powershell
# Windows: remove the old task, then install the managed one
schtasks /delete /tn "Claude Session Backup" /f
csb setup schedule
```

## Distill-on-backup

With `csb config distill_policy always`, every backup also regenerates stale human-readable chat logs under `~/.claude/distilled/` (committed with the noise commit). See [commands.md](commands.md#reading-conversations-distill).

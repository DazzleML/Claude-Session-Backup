# Automating csb backups

csb is designed for unattended operation (`--quiet`, `--no-gpg-sign` commits, a PID-aware lock file). Pick one or more of the setups below -- they coexist safely because every entry point takes the same `backup_lock`.

## Claude Code Plugin (recommended)

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

## Cron (Linux/Mac)

Belt-and-suspenders periodic backup as a safety net:

```bash
*/15 * * * * /usr/local/bin/csb backup --quiet 2>/dev/null
```

## Task Scheduler (Windows)

```powershell
schtasks /create /tn "Claude Session Backup" /tr "csb backup --quiet" /sc minute /mo 15
```

## Distill-on-backup

With `csb config distill_policy always`, every backup also regenerates stale human-readable chat logs under `~/.claude/distilled/` (committed with the noise commit). See [commands.md](commands.md#reading-conversations-distill).

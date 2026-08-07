# Platform Support

`csb` is designed to work on Windows, Linux, macOS, and BSD. This page captures what's verified vs. designed-for-but-not-yet-verified, and the platform-specific notes you should know about.

## Verification matrix

| Platform | CLI | Git backup | Plugin / hooks | Scheduled backup | Notes |
|----------|-----|------------|----------------|------------------|-------|
| **Windows 11** | ✅ verified | ✅ verified | ✅ verified | ✅ verified | Primary dev platform. Tested under `cmd.exe`, PowerShell, and Git Bash. Task registration + prime run proven live (non-elevated `schtasks /create /xml`). |
| **Linux** | ✅ designed for | ✅ designed for | ✅ designed for | ✅ designed for | Node.js bootstrapper handles `python` vs `python3` differences. cron backend's daemon detection verified against real minimal-image census (most ship no cron; csb refuses honestly). Not yet end-to-end verified by the maintainer on native hardware. |
| **macOS (Intel + Apple Silicon)** | ✅ designed for | ✅ designed for | ✅ designed for | ⚠️ unverified on hardware | Same bootstrapper pattern as Linux. The launchd backend (LaunchAgent, `StartCalendarInterval`) is built from documented behavior and a field specimen, but the maintainer has no Mac -- verification reports welcome. |
| **BSD (FreeBSD / OpenBSD)** | ⚠️ likely works | ⚠️ likely works | ⚠️ untested | ⚠️ unverified on hardware | Python 3.10+ and Git are the only hard requirements. The cron backend deliberately uses `ps -axo` (BSD-safe; `-e` means *environment* there) and cron ships in the BSD base systems, but no BSD hardware run has happened yet. |

"Designed for" means the code paths exist and have been reviewed for cross-platform safety (path handling, subprocess invocation, hook bootstrapper), but the maintainer hasn't yet run the full flow on that OS. Community verification and issue reports are welcome.

## Requirements (all platforms)

- **Python 3.10+** — `csb` uses `match`/`case` and other 3.10+ features.
- **Git** — for backup storage. `~/.claude/` must be initialized as a git repository (`git -C ~/.claude init`).
- **Node.js** (plugin users only) — Claude Code is itself a Node.js app, so this is already present if you're using Claude Code.

## Platform-specific notes

### Windows

- Tested on Windows 10 and Windows 11.
- The plugin bootstrapper (`run-hook.mjs`) auto-detects `py`, `python`, and `python3` on the PATH and uses whichever is present.
- File paths are handled via `pathlib.Path` throughout — Windows backslashes and UNC paths should work; if you find a path that doesn't, please file an issue.
- PowerShell 5.1 vs PowerShell 7: both work. Git Bash and MSYS2 also work.
- `csb setup schedule` registers a non-elevated per-user task via `schtasks /create /xml` running `pythonw` (no console window). The task starts on battery and uses `StartWhenAvailable`, so a fire missed while asleep runs at wake. Git subprocesses spawned by the scheduled run are console-window-suppressed too.
- `dz safedel` integration (when available) is respected for file deletions.
- **Boot epochs (`csb set show last`, `last~N`, dates)** read the Windows System event log (IDs 6005/6006/6008/1074) via PowerShell — the original and most detailed fence source (it distinguishes clean vs unexpected vs process-initiated restarts natively).

### Linux

- The Node.js bootstrapper is specifically designed to handle distros where `python` is not on the PATH (Ubuntu 20.04+, most modern Debian derivatives).
- Tested on WSL2 (Ubuntu) as part of cross-platform development; full native-Linux verification is pending.
- `csb setup schedule` installs a marker-fenced block in your user crontab and refuses (exit 11, with per-distro fix instructions) when no cron daemon is running — a spool entry nothing reads would look installed and never fire. Minimal container images frequently ship no cron at all. `--print-systemd` prints a user-timer recipe for cron-less systems (mind the `enable-linger` caveat). See [automation.md](automation.md).
- **WSL**: cron only runs while the distro is running; for an always-on schedule protecting a Windows-side Claude dir, run `csb setup schedule` from Windows. Setup detects WSL and says so.

#### Boot epochs on Linux (`csb set show last` and friends)

The POSIX fence chain, in order:

1. **journalctl** (`--utc --list-boots -o json`) — boot spans define both boot and shutdown instants. On systemd older than ~254, `-o json` is *silently ignored* (text comes back with exit 0); csb sniffs and parses whichever arrives. Verified against real systemd 249 output.
2. **wtmp** (`last -xF reboot shutdown`) — corroborates *causes*: a shutdown record within 10 minutes of a journal span's end means `clean`; covered silence means `unexpected` (crash-by-absence); a wtmp rotated past the instant means `unknown` — the honest floor, never a guessed crash. Stock logrotate keeps one wtmp file, so `unknown` is common on long-running boxes and is correct.
3. On systems with no journal at all, wtmp records fence directly (with the same coverage guard).

**WSL boot semantics**: a WSL "boot" is a *VM start* — the lightweight VM launches on first use and auto-terminates when idle — not a machine restart. `csb set show last` inside WSL therefore answers "what was active before my last WSL cold-start", which can be hours inside one Windows boot, and its causes read `unexpected` because WSL terminations never write wtmp shutdown records. Both behaviors are honest descriptions of how WSL actually works. For *machine* restart recovery on a WSL-using box, run csb from the Windows side.

### macOS

- Not yet verified end-to-end but no known blockers.
- `csb setup schedule` writes a LaunchAgent plist (`~/Library/LaunchAgents/com.dazzleml.csb-backup.plist`) using `StartCalendarInterval`, so a fire missed during sleep runs at the next matching time. Over SSH with no GUI session, `launchctl bootstrap` can fail — install reports "plist written; loads at next login" rather than claiming success.
- `dirs.user_data_dir` resolution via the `dirs` crate uses `~/Library/Application Support/...` conventions; `csb` does not depend on Mac-specific paths.

### BSD

- Python 3.10+ and Git are widely available on FreeBSD and OpenBSD via `pkg` / `ports`.
- Plugin installation requires Node.js (`npm` + `node`), which is available in both ecosystems.
- Boot epochs use the wtmp chain (`last -xF`, falling back to the no-year `last` form with a year-rollover walk for date placement). Built from documented output shapes; a real-BSD verification run is welcome — the parsers degrade to the honest `unknown` cause rather than guessing when a shape surprises them.
- `csb setup schedule` uses the same crontab backend as Linux; cron ships in the BSD base systems (if the daemon isn't running it was disabled — re-enable via `rc.conf` on FreeBSD/NetBSD or `rcctl` on OpenBSD). The daemon pre-flight deliberately uses `ps -axo` because BSD `ps -e` means *show the environment*, not *every process*.
- Not tested; bug reports welcome.

## Reporting platform issues

If you hit a platform-specific problem, please include the following when filing an issue:

- OS + version (e.g. `Windows 11 23H2`, `Ubuntu 22.04 LTS`, `macOS 14.4 on Apple Silicon`).
- Python version (`python --version`).
- Git version (`git --version`).
- Node.js version if you're using the plugin (`node --version`).
- The exact command you ran and the error / unexpected output.

Issues: <https://github.com/DazzleML/Claude-Session-Backup/issues>

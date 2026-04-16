# Platform Support

`csb` is designed to work on Windows, Linux, macOS, and BSD. This page captures what's verified vs. designed-for-but-not-yet-verified, and the platform-specific notes you should know about.

## Verification matrix

| Platform | CLI | Git backup | Plugin / hooks | Notes |
|----------|-----|------------|----------------|-------|
| **Windows 11** | ✅ verified | ✅ verified | ✅ verified | Primary dev platform. Tested under `cmd.exe`, PowerShell, and Git Bash. |
| **Linux** | ✅ designed for | ✅ designed for | ✅ designed for | Node.js bootstrapper handles `python` vs `python3` differences. Not yet end-to-end verified by the maintainer. |
| **macOS (Intel + Apple Silicon)** | ✅ designed for | ✅ designed for | ✅ designed for | Same bootstrapper pattern as Linux. Not yet end-to-end verified by the maintainer. |
| **BSD (FreeBSD / OpenBSD)** | ⚠️ likely works | ⚠️ likely works | ⚠️ untested | Python 3.10+ and Git are the only hard requirements. Plugin bootstrapper assumes Node.js is installed (same as Claude Code). |

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
- `dz safedel` integration (when available) is respected for file deletions.

### Linux

- The Node.js bootstrapper is specifically designed to handle distros where `python` is not on the PATH (Ubuntu 20.04+, most modern Debian derivatives).
- Tested on WSL2 (Ubuntu) as part of cross-platform development; full native-Linux verification is pending.
- Cron is a supported automation path alongside the plugin — see the [README's Automation section](../README.md#automation).

### macOS

- Not yet verified end-to-end but no known blockers.
- `launchd` can be used for periodic backup as an alternative to the plugin.
- `dirs.user_data_dir` resolution via the `dirs` crate uses `~/Library/Application Support/...` conventions; `csb` does not depend on Mac-specific paths.

### BSD

- Python 3.10+ and Git are widely available on FreeBSD and OpenBSD via `pkg` / `ports`.
- Plugin installation requires Node.js (`npm` + `node`), which is available in both ecosystems.
- Not tested; bug reports welcome.

## Reporting platform issues

If you hit a platform-specific problem, please include the following when filing an issue:

- OS + version (e.g. `Windows 11 23H2`, `Ubuntu 22.04 LTS`, `macOS 14.4 on Apple Silicon`).
- Python version (`python --version`).
- Git version (`git --version`).
- Node.js version if you're using the plugin (`node --version`).
- The exact command you ran and the error / unexpected output.

Issues: <https://github.com/DazzleML/Claude-Session-Backup/issues>

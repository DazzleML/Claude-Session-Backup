# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to a PEP 440 versioning scheme (see `_version.py`).

Status: **prealpha**. Until the first alpha release, breaking changes may land between patch versions. Each entry that changes observable behavior is flagged accordingly.

## [Unreleased]

### Fixed
- **`csb list` / `csb scan` "start at" line now reports the cwd that lets `claude --resume` find the session** -- previously derived from a JSONL `cwd` histogram (most-common cwd across all events), which silently misled users when Claude Code was launched from a parent dir and `cd`-ed into a subdir afterwards. Now derived from the project-dir slug (`~/.claude/projects/<slug>/`) via filesystem-validated reverse decoding in the new `claude_session_backup/pathkit.py` module. Mirrors the upstream encoder at `claude-code/utils/sessionStoragePortable.ts:311-319` (`replace(/[^a-zA-Z0-9]/g, '-')`); the inverse uses `os.listdir` per directory level and the longest-encoded-entry-first heuristic to disambiguate slugs that have multiple valid filesystem decodings (e.g., a folder literally named `New--Project` vs. `New\.Project`). Returns the sentinel `<unresolved:slug>` when no candidate decodes (e.g., the original cwd has been deleted) so maintainers can still see the slug. Closes #19. (#19, prealpha-blocker)
- **Indexer no longer truncates folder usage to top-N; `csb list --top N` / `--all-folders` reveal the long tail** -- `metadata.py` previously kept only `1 + top_n_folders` rows in the SQLite `folder_usage` table at index time, which made the long tail of cwds invisible regardless of any renderer flag. The slug-decoded "start at" cwd was a frequent casualty (e.g., AMD_INTIGRITI's `C:\` ranked 5th by JSONL count and got dropped), surfacing as a missing `(Nx)` count next to the "start at" line. The indexer now persists every distinct cwd from the JSONL events; the renderer truncates at display time using the new flags. Closes #21.

### Added
- **`csb list --top N` and `csb list --all-folders` (also on `csb scan`)** -- control how many "other" folder rows display beneath the "start at" line. `--top N` shows the top N most-used cwds; `--all-folders` shows everything. Default unchanged (top 3). Mutually exclusive flags. Acceptance criteria from #21 are met. (#21)
- **44 new tests** -- `test_pathkit.py` (33 covering the encoder ground truth, including the lossy `\.` separator-plus-dotfile and `-\` literal-hyphen-plus-separator cases that produce identical 2-dash slugs from genuinely different source paths, filesystem-driven decoding, ambiguity handling, drive-root and unresolvable cases, and `<unresolved:>` / `<no-slug>` sentinel paths); 7 in `test_cli.py` covering `--top` / `--all-folders` / mutex behavior; 4 renderer tests in `test_timeline.py`; 1 indexer test in `test_metadata.py` confirming all folder rows persist. Total: 118/118 pass.

### Changed
- **`extract_metadata` no longer accepts `top_n_folders`** -- the indexer always stores all rows. Callers updated. Removed the `top_n_folders` config key (was dead config now that the indexer is single-policy).
- **Existing index rows are still truncated until re-indexed.** Run `csb rebuild-index` to backfill the long tail across all sessions; new sessions and any session touched by `csb backup` will pick up the new behavior on the next pass without a full rebuild.

## [0.2.2] -- 2026-04-15

### Fixed
- **Stats dashboard showed "unable to load statistics"** -- `docs/stats/index.html` and `docs/stats/README.md` had orphan gist IDs hardcoded by a stray `ghtraf --configure` run. Replaced with the authoritative badge (`7aa669e4...`) and archive (`6ee098fb...`) gist IDs that `.ghtraf.json` and the repo variables point at.

## [0.2.1] -- 2026-04-15

First release with the repository public. Focus: make the install path work today (GitHub-based `pip install` and URL-based Claude Code plugin install), fix broken or inaccurate pieces of the published surface, and set up repo infrastructure for later automation.

### Added
- **Prealpha warning banner** in README so users understand the maturity level before installing (#12, #13, #14 listed as the alpha gates).
- **`docs/platforms.md`** — verification matrix, per-platform notes, and a reporting template for platform-specific issues. Fixes the 404 that the Platform badge was pointing at.
- **Traffic tracking infrastructure** — `.github/workflows/traffic-badges.yml` and `docs/stats/` dashboard deployed via `ghtraf`, backed by csb-specific badge and archive gists. Scheduled nightly workflow will populate the Installs badge once the workflow runs.
- **`.ghtraf.json`** — persisted ghtraf config (badge/archive gist IDs, dashboard dir) so future ghtraf runs can reuse the existing gists rather than creating new ones.
- **Claude Code plugin install via marketplace URL** — Automation section now leads with `claude plugin marketplace add "DazzleML/Claude-Session-Backup"` so end users don't need to clone first. Clone-based install preserved as a development alternative.

### Changed
- **README Quick Start and Installation** now lead with `pip install git+https://github.com/DazzleML/Claude-Session-Backup.git` since the PyPI release is pending. The `pip install claude-session-backup` line is kept as "once published" so we can flip it back when the first PyPI publish succeeds.
- **Quick Start is now a numbered 5-step flow** with an explicit "install the Claude Code plugin" step 2. Without the plugin, the PreCompact hook isn't registered and compactions happen silently -- exactly the failure mode csb is meant to prevent.
- **Version bump** `0.2.0` → `0.2.1`. Stays PEP 440-compatible (no `a1` suffix) so `pip install` picks up updates without `--pre`. Project phase remains `prealpha` and is communicated via the README banner and the `Development Status :: 2 - Pre-Alpha` classifier.

### Fixed
- **Acknowledgement of the upstream Claude Code History Viewer author** — was incorrectly credited to `@pinkpixel-dev`; corrected to [@jhlee0409](https://github.com/jhlee0409) ([jhlee0409/claude-code-history-viewer](https://github.com/jhlee0409/claude-code-history-viewer)).
- **Installs badge URL** was pointing at `DazzleTools/dazzlecmd`'s traffic gist (copy-paste artifact); now points at the csb-specific gist `7aa669e4...` owned by this project.

### Documentation
- Full README restructure so the "install → set up hooks → start using it" flow is linear and obvious to a first-time reader.

## [0.2.0] -- 2026-04-11 (prealpha)

First public release. `csb list --sort`, `csb scan` with folder-usage search, cross-platform Claude Code plugin with Node.js bootstrapper, two-commit backup model, timeline view with purge countdown, session resume and restore. 73/73 tests pass. See the [v0.2.0 release notes](https://github.com/DazzleML/Claude-Session-Backup/releases/tag/v0.2.0) for the full highlight list.

[Unreleased]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/DazzleML/Claude-Session-Backup/releases/tag/v0.2.0

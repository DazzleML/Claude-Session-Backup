# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to a PEP 440 versioning scheme (see `_version.py`).

Status: **prealpha**. Until the first alpha release, breaking changes may land between patch versions. Each entry that changes observable behavior is flagged accordingly.

## [Unreleased]

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

[Unreleased]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/DazzleML/Claude-Session-Backup/releases/tag/v0.2.0

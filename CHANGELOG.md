# Changelog

All notable changes to comfydbg will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-03-22

### Fixed
- `detect` command crashes with FileNotFoundError when run outside a ComfyUI directory (removed unused `parse_requirements` call)

### Changed
- Minor help text alignment in epilog examples

## [0.1.0-alpha] - 2026-03-21

### Added
- **Startup management**: `skip`, `full`, `force` commands for targeted pip control
- **Package install with rollback**: `-CN` (from current), `-HN` (from head), `-1/-2/-3` shortcuts
- **Multi-source install**: PyPI (default), GitHub git (`-G`), GitHub wheel (`-W`)
- **Build recipe system**: Auto-build wheels from GitHub releases for non-standard packages
- **Version comparison**: `version` command shows installed vs required with mismatch flags
- **Workflow fingerprinting**: `detect` extracts version info from PNG/WebP images and JSON files
- **Custom node bisect**: Binary search for broken nodes using `--whitelist-custom-nodes`
- **Bisect exclude/restore**: Move crashing nodes aside without losing them
- **Known package mappings**: PyPI-to-GitHub mappings for common ComfyUI packages
- **begin.cmd replacement**: Drop-in replacement for ComfyUI startup scripts

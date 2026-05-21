# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to a PEP 440 versioning scheme (see `_version.py`).

Status: **prealpha**. Until the first alpha release, breaking changes may land between patch versions. Each entry that changes observable behavior is flagged accordingly.

## [Unreleased]

## [0.3.5] -- 2026-05-21 (prealpha)

Directory-scope search: `csb search -d <path>` (and `-D` for folder-only) ranks every indexed session that touched files under PATH by SUM(strength) of those file-operations, then runs FTS5 MATCH for the user's pattern within each ranked session. The killer-use-case feature of the v0.3.x track -- "I'm in folder X, which past sessions actually worked on it, and what did they say about Y?" -- finally lands. New `--min-strength {1,2,3}` filter trims out low-signal rows (Grep probes, read-only). Two small ergonomic refinements ride along: matched query terms now render in bold green inside excerpt lines, and the `--all` / `--deleted` flag pair is unified into a single `--deleted {only,all}` argument. 571/571 tests pass (was 547 at v0.3.4; +24 net new).

### Added
- **`csb search -d PATH` / `csb search -D PATH`** -- directory-scope mode. Mirrors `csb scan`'s `-d` / `-D` semantics: `-d` is recursive (folder + all subdirectories), `-D` is folder-only (no descendants). Session iteration order ignores `--sort` and uses `SUM(strength) DESC` instead, so the session that most heavily worked on PATH sorts first. Mutually exclusive flags at the argparse layer; mutex with `--source jsonl|convo|sesslog` enforced at runtime with a clear error message (the feature queries per-project FTS5 DBs exclusively).
- **`--min-strength {1,2,3}`** for `csb search` -- filters file-operations by minimum strength when in `-d/-D` mode. `1` (default) includes everything; `2` skips Grep/Glob probes (strength=1); `3` keeps only active-modification ops (`edited` / `wrote` / `notebook_edit`, strength=3). No effect outside dir-scope mode.
- **Per-session strength suffix in renderers** -- human mode and `--sessions-only` mode append `[N file-ops, strength=S]` to the session header when the hit came from dir-scope dispatch. JSON mode emits `strength_sum` and `file_op_count` fields. Non-dir-scope hits keep the fields at their default 0 so the suffix stays suppressed.
- **`find_path_filtered_sessions(fts5_db_path, path_globs, exclude_descendant_globs=None, min_strength=1)`** in `search.py` -- the step-1 helper that runs `SELECT session_id, SUM(strength), COUNT(*) FROM file_operations WHERE file_path GLOB ? [...] GROUP BY session_id ORDER BY sum_strength DESC` against one per-project DB. Accepts an OR'd list of GLOBs (`-d` builds both `\\` and `/` separator variants so paths captured with either style match without prior normalization). `OperationalError` on pre-strength DBs degrades to empty; the user can rebuild via `csb build-fts5` if they want hits from that project.
- **`_build_directory_globs(abs_path, include_descendants)`** -- builds the include and exclude GLOB lists for `-d` / `-D` semantics. `-D`'s exclude pattern (`<root>{sep}*{sep}*`) catches paths with at least one further separator past the root, dropping subdirectory matches cleanly.
- **`_search_dir_scope(...)` + `_lookup_session_row(...)`** in `search.py` -- the directory-scope dispatcher. Walks every `<claude_dir>/csb-fts/*.db`, aggregates the per-DB ranked lists, sorts globally by `sum_strength DESC`, then for each ranked session looks up the main DB sessions row (respecting `--all` / `--deleted` / `--session-id` filters) and runs `query_fts5_for_session` for the user's pattern. Sessions missing from the main DB or filtered out drop quietly -- no orphan hits.
- **15 new dir-scope tests** in `tests/test_search.py` covering GLOB-builder shape (`-d`, `-D`, trailing separator), `find_path_filtered_sessions` (ranking, `min_strength` filtering, `-D` exclude, missing DB tolerance), and end-to-end `search()` against `dir_scope` (strength ordering across multiple sessions, empty-pattern matches all, `--min-strength 3` filters Grep-only sessions, orphan session skip, no-csb-fts-dir tolerance, `Hit.strength_sum` / `Hit.file_op_count` plumbed, non-dir-scope hits leave strength at 0, `-D` excludes descendants while `-d` keeps them).
- **In-line query highlighting in `csb search` human-mode output** -- every occurrence of the user's pattern inside the matched-text excerpt (and context lines that happen to contain the pattern) is wrapped in bold green ANSI. Auto-disabled when stdout is not a TTY or `--no-color` is passed. Empty / invalid-regex patterns degrade to plain rendering rather than crashing.
- **`_highlight(text, pattern, regex, case_sensitive, enabled)`** in `search_render.py` -- the helper. Literal mode internally escapes regex metacharacters so `oauth.flow` matches the literal sequence and not `oauthXflow`. Regex mode honors `-s` (case-sensitive) and inline `(?i)` flags compose normally. Called AFTER truncation so the embedded ANSI codes don't corrupt slice math.
- **9 new highlight tests** in `tests/test_search_render.py` covering: literal match (case-insensitive default) wraps in `\033[1;32m...\033[0m`, `enabled=False` is a no-op, empty / None pattern is a no-op (mirrors search()'s empty=match-all semantics), case-sensitive miss returns plain, regex alternation wraps each branch, invalid regex falls back to plain, literal metacharacters stay literal, `render_human(query=...)` ends-to-end emits the ANSI in the matched-line output, `query=None` produces no bold-green ANSI anywhere.

### Changed
- **`Hit` dataclass** gained `strength_sum: int = 0` and `file_op_count: int = 0`. Default zero on every non-dir-scope hit, so existing renderers don't print the suffix for normal searches.
- **`search()` signature** gained `dir_scope: Optional[dict] = None`. The dict carries `{"abs_path", "include_descendants", "min_strength"}` and triggers the dir-scope dispatcher. Backwards-compatible -- existing callers that don't pass the argument run the same code path they did at v0.3.4.
- **`render_human()` and `render()`** signatures gained `query`, `regex`, `case_sensitive` kwargs so `cmd_search` can plumb the in-line highlight rules into the renderer. Defaults preserve the v0.3.4 behavior (no highlight) for any caller that doesn't pass them.

### Breaking (CLI surface, prealpha)
- **`csb list` and `csb search` `--all` flag removed; `--deleted` now takes an optional `{only,all}` argument.** Old behavior maps directly: old `--deleted` -> new `--deleted only` (bare `--deleted` defaults to `only`); old `--all` -> new `--deleted all`; omitting the flag still means "live only" as before. Simpler flag surface, clearer relationship between the two modes, no orphan `--all` that hid the fact it was actually a deleted-related option. Pre-alpha breaking change; documented for any shell scripts.
- **`csb search` `--files-only` and `--sessions-only` flags removed; `--only {files,sessions}` replaces them.** Same semantics, smaller flag surface, mutually exclusive with `--json` as before. Old `--files-only` -> new `--only files`; old `--sessions-only` -> new `--only sessions`. Default (no flag) still produces grouped excerpts. Pre-alpha breaking change. See #30 for the deferred decision on extending `--only` to `csb list` and `csb scan`.
- **`csb search --files-only` (now `--only files`) returns transcript paths instead of internal FTS5 DB paths.** New `Hit.transcript_path` field, resolved via convo > sesslog > jsonl preference per session. The v0.3.3 FTS5 dispatch was returning the per-project `.db` path through `Hit.source_path` -- technically honest but unhelpful, because users running `--files-only` want a file they can `cat` / `grep` / open. Now uniform across all dispatch paths. JSON output gains an explicit `transcript_path` key when populated; `source_path` keeps its "what the dispatcher walked" meaning.
- **`csb search --limit N` semantics are now contextual to `--only`.** Default mode (no `--only`) treats `--limit N` as N raw hits (current behavior, preserved). `--only sessions` treats it as N distinct sessions; `--only files` treats it as N unique transcript paths. The implicit "one hit = one output line" relationship from the default mode is preserved when the output unit changes. Fixes the surprising behavior where `csb search "" -d <path> --only sessions --limit 3` would show only ONE session (the highest-strength session ate all 3 hits before the iterator could visit other sessions). Internal: new `cap_hits_by_output_unit(hits, user_limit, unit)` helper applied in `cmd_search` after `run_search` returns.

### NO CHANGE (with rationale)
- **`fts5_db.py`** schema -- the `file_operations.strength` column landed in v0.3.1; v0.3.5 only consumes it. No new tables or columns; no migration.
- **`fts5_importer.py`** -- still writes the same `FileOpRow` shape into `file_operations`. `-d` / `-D` queries that table at read time; the importer doesn't know about the new feature.
- **`transcript_walker.py`** -- still yields `FileOpRow` with `(operation, path, strength)`. The strength weights set in v0.3.1 (`3/2/1` for active / read / search) are the same weights the dir-scope ranker uses, so importer and query agree by construction.
- **`fts_paths.py`** -- per-project DB resolution and `list_fts_dbs(claude_dir)` already existed for v0.3.3's dispatcher. v0.3.5's dir-scope code iterates the same `csb-fts/*.db` glob.

### Verified live
- `csb search "FTS5" -d <vault>` against this project's vault: the session that built the FTS5 feature sorts first with `[149 file-ops, strength=377]` in its header, followed by older sessions in strength-descending order.
- `--min-strength 3` against the same scope drops that to `[93 file-ops, strength=279]` (the search/read entries removed), as expected from the strength tiering.

### Carried over from v0.3.4
- The auto-mode default preference (`effective_default_preference`) still adapts to vault state for non-dir-scope searches. Dir-scope mode pins source to `fts5` regardless because the ranking SQL only exists in the per-project FTS5 DB.

## [0.3.4] -- 2026-05-18 (prealpha)

Logger-side parity: csb can now derive file-operation metadata from `claude-session-logger`'s output channels (`.sesslog_*`, `.tools_*`, `.fileio_*`) without walking the raw JSONL or building the FTS5 index. The search dispatcher's default preference adapts to the vault -- users without the logger see `("fts5", "jsonl")` as their auto-mode preference instead of the full list. 547/547 tests pass (was 519 at v0.3.3; +28 net).

### Added
- **`claude_session_backup/sesslog_parser.py`** (new module) -- parses the structured block format claude-session-logger emits to `.sesslog_*` / `.tools_*` / `.fileio_*` channels and yields `transcript_walker.FileOpRow` records (same shape FTS5 import produces from JSONL). Closes the logger-side half of the v0.3.x parity story: file-operation metadata is now derivable from ANY of the three sources (JSONL, FTS5, logger output).
- **`iter_file_ops_from_sesslog(path, session_id)`** -- streams `FileOpRow` from one logger file. Handles single-line + multi-line blocks; recognizes `Read` / `Edit` / `Write` / `MultiEdit` / `NotebookEdit` (path-bearing, first-quoted parser) plus `Grep` / `Glob` (path target via `... in "..."` body). Skips `Bash` / `Skill` / `Agent` / `WebSearch` / `Task` blocks (not file-ops). Strips `:LINE` / `:LINE-RANGE` suffixes from Read paths. Strength matches the JSONL walker: 3 = active modify, 2 = passive read, 1 = search probe.
- **`find_fileop_channels_for_session(session_dir)`** -- lists every logger output file in a session's directory that carries file-op metadata (any name starting with `.sesslog_`, `.tools_`, or `.fileio_`). Useful for future code that wants to merge file-op data from multiple channels of the same session.
- **`has_session_logger(main_conn)`** -- O(1) probe of `session_sources` for any `convo` / `sesslog` row. Used by the search dispatcher to decide whether the default preference should include those sources.
- **`effective_default_preference(conn)`** in `search.py` -- returns the auto-detected default preference for the vault. Logger present -> full `("fts5", "convo", "sesslog", "jsonl")` list. Logger absent -> collapsed `("fts5", "jsonl")` -- no wasted lookups against sources that can't exist. User-explicit `--source X` still wins.
- **28 new tests** -- 24 in `tests/test_sesslog_parser.py` (every tool kind, line-range stripping, agent-attributed tags, multi-line blocks, banner/blank skipping, malformed tolerance, message_index increment per-emitted-row only, sub-channel discovery, logger presence in 4 vault states); 4 in `tests/test_search.py` (`_resolve_preference` accepts custom `default_preference`; `effective_default_preference` returns full list when logger present, collapsed list when absent, collapsed list for empty DB).

### Changed
- **`_resolve_preference(source_override, default_preference=_SOURCE_PREFERENCE)`** gained an optional `default_preference` parameter so the caller can adapt the auto-mode preference to vault state. Backwards-compatible: existing callers that don't pass the new arg get the module-level constant they had before.
- **`csb search`** in auto mode now adapts to vault state via `effective_default_preference`. A user who has never run claude-session-logger sees their `csb search` walk `fts5 -> jsonl` only -- previously the dispatcher would have probed `convo` / `sesslog` for every session before falling through to JSONL. Performance win and conceptual cleanliness for the "no logger" configuration.

### Verified live
- Parsed 74 file-op rows from a real `.tools_*` log (39 read / 11 wrote / 9 edited / 15 searched), all with correct strength values matching the JSONL importer's tiering.

### Carried over from v0.3.3
- Search dispatcher logic unchanged; the new `default_preference` parameter is the only signature change. Existing `--source fts5` / `--source convo` / etc. behavior identical.

## [0.3.3] -- 2026-05-18 (prealpha)

The keystone v0.3.x deliverable: `csb search` now actually queries FTS5. Designed source-agnostic from the start -- FTS5 is a first-class peer in the preference list alongside `.convo` / `.sesslog` / `.jsonl`, not a layer bolted on top. A single uniform dispatcher walks the preference order per session and returns the first source that's available for that session. Each source is independently optional: a user without claude-session-logger automatically skips `.convo` / `.sesslog`; a user who hasn't run `csb build-fts5` skips `fts5`; a user with only raw transcripts falls through to `jsonl`. New `--source fts5` choice for users who want FTS5-only semantics. 519/519 tests pass (was 510 at v0.3.2; +9 net).

### Added
- **`--source fts5`** as a `csb search` choice. Returns hits only from sessions present in the per-project `indexed_sessions` table; sessions not yet built (via `csb build-fts5`) are skipped silently with no fallback. The user-explicit "I want FTS5 results, even possibly stale" knob -- pair with `csb build-fts5 --force` if you want the freshest index.
- **Source-agnostic dispatcher** in `search.py`. `_SOURCE_PREFERENCE = ("fts5", "convo", "sesslog", "jsonl")` defines the project's default attempt order. `_resolve_preference(--source)` translates the user choice to a preference tuple (auto -> the full default; explicit single source -> a one-element tuple). `_pick_source_for_session(...)` walks the tuple in order and returns the first source whose availability check passes -- one loop, no FTS5 special-casing.
- **`query_fts5_for_session(fts5_db_path, session_id, pattern)`** in `search.py`. Yields `Event` records for matches in a single session's per-project FTS5 DB. Uses FTS5 `MATCH` for fast candidate narrowing (porter unicode61 tokenizer) followed by the same Python-side literal / regex matcher the rest of `csb search` uses -- preserves csb's literal-substring semantics even though the tokenizer would otherwise expand "run" -> "running".
- **`_fts5_path_if_indexed(claude_dir, project, encoded_slug, session_id, jsonl_mtime=None)`** in `search.py`. Read-only freshness probe; opens the per-project DB RAW (bypasses `open_fts5_db`) so the search path never triggers migrations or prints the migration-notice line. `jsonl_mtime=None` -> any indexed session counts (`--source fts5` contract). `jsonl_mtime=N` -> `last_jsonl_mtime >= N` required (`--source auto` contract).
- **`claude_dir` parameter on `search.search()`** -- forwarded by `cmd_search` from the active config. Required for resolving per-project FTS5 DB paths; when None, the dispatcher cannot evaluate the `fts5` source and treats it as unavailable for every session (the rest of the preference list runs normally).
- **9 new / reframed tests** in `tests/test_search.py`: preference resolution (auto -> full default, single name -> singleton tuple), default order has fts5 first plus all four expected names, picker walks preference and returns first available, picker walks past unavailable sources to the next, pinned source returns only itself, pinned to a missing source returns `(None, None)`, plus 5 end-to-end search() tests: `--source fts5` returns indexed hit, `--source fts5` skips unindexed, auto picks FTS5 when fresh, auto walks past FTS5 when stale, FTS5 path preserves `AGENT:<subtype>` role label.

### Changed
- **`_SOURCE_PREFERENCE`** now lists `"fts5"` first. The old constant `("convo", "sesslog", "jsonl")` is gone; FTS5 takes its rightful place at the top of the attempt order.
- **`csb search` SQL** in the session-enumeration loop now selects `s.jsonl_path` so the dispatcher can derive each session's encoded slug for FTS5 DB path resolution.

### Removed (internal)
- **`_pick_one_source(sources, source_override)`** -- replaced by the source-agnostic `_pick_source_for_session` walker. The old function only knew about file-based sources and required the search() loop to special-case FTS5 dispatch before calling it; the new picker handles all four sources uniformly in a single loop.

### Performance
- For sessions whose FTS5 index is fresh, `csb search` now returns hits in roughly the time of a SQLite `MATCH` query (sub-100ms on test vaults of ~200 sessions) instead of walking the `.convo` / `.sesslog` / JSONL file every time. The cross-project case still iterates per project (no cross-DB join yet) -- one query per per-project DB.

### Carried over from v0.3.2
- Migration framework + visible auto-upgrade notice unchanged. `csb search` opens FTS5 DBs RAW for its freshness check and the actual MATCH query, so it never trips the migration runner -- migrations only happen via `csb build-fts5` (which is the right place).

## [0.3.2] -- 2026-05-18 (prealpha)

Maintenance step ahead of the v0.3.3 search dispatcher work: refactor the per-project FTS5 DB migration logic from an inline conditional in `fts5_db.py` into a registry-pattern module matching the main DB's `migrations.py`. Same shape, same convention -- adding future per-project schema versions is now "write a function, register it" instead of editing a branch. Per-project migrations now also print a user-visible notice on auto-upgrade (matching the main DB's existing audit-trail style). 510/510 tests pass (was 496 at v0.3.1; +14 net).

### Added
- **`claude_session_backup/fts5_migrations.py`** -- new module parallel to `migrations.py`. Exports:
  - `MIGRATIONS: dict[int, Callable]` registry keyed by target version
  - `apply_pending(conn, quiet=False)` -- forward-only runner; reads `fts_schema_version`, applies every pending migration in order, commits after each step (partial failure leaves the DB at a well-defined intermediate version)
  - `_v2_add_strength_to_file_ops(conn)` -- the single existing migration, extracted from the previous inline conditional
- **User-visible auto-upgrade notice** -- when a per-project FTS5 DB is opened and migrations run, csb now prints `csb: per-project FTS5 schema migrated to v{version}` per applied step (matching the format the main DB uses via `migrations.apply_pending`). Suppressible via `csb build-fts5 --quiet` (the `quiet` flag threads through `open_fts5_db` → `init_fts5_schema` → `apply_pending`). No-op opens print nothing.
- **14 new tests** in `tests/test_fts5_migrations.py`: registry shape sanity, version getter/setter behavior, fresh-DB vs v1-DB apply_pending paths, idempotency on already-current DBs, **partial-migration self-healing** (ALTER succeeded but UPDATE was interrupted → re-running the body restores correct strength values), no-op on fresh DBs where the column already exists from `_SCHEMA_SQL`, and the new visibility behavior (default prints notice, `quiet=True` suppresses, no-op opens are silent).

### Changed
- **`fts5_db.init_fts5_schema`** now delegates to `fts5_migrations.apply_pending` instead of running an inline conditional. Behavior identical; structure parallel to main DB.
- **`fts5_db.open_fts5_db(path, quiet=False)`** and **`init_fts5_schema(conn, quiet=False)`** gained a `quiet` parameter that forwards through to the migration runner.
- **`fts5_index.build_all`** passes its `quiet` flag through to `open_fts5_db` so `csb build-fts5 --quiet` cleanly suppresses migration notices alongside its own per-session progress.

### Removed (internal)
- **`fts5_db._migrate_per_project_schema`** -- inlined into `_v2_add_strength_to_file_ops` in the new `fts5_migrations` module. No external callers; entirely internal.

### Carried over from v0.3.1
- All v0.3.1 behavior is preserved bit-for-bit: real DBs that migrated to v2 under v0.3.1 stay at v2 under v0.3.2 with no rework; fresh DBs created under v0.3.2 still land at v2 with strength column populated at INSERT time.

### Why a separate release
Per the project's per-commit-version-bump convention. This is a pure refactor with one new module + framework tests; bundling it with the v0.3.3 dispatcher work would mix unrelated concerns and make either step harder to revert. The framework lands first so v0.3.3 can write its dispatcher tests against a stable migration foundation.

## [0.3.1] -- 2026-05-17 (prealpha)

Foundation step for the v0.3.x parity story (issue #3): extract the JSONL walker into a shared module that both the FTS5 importer AND Phase 1 grep search consume, add a `strength` weighting to the `file_operations` table for future ranking, and close a long-standing parity bug where `csb search --source jsonl` silently missed Task-launched sub-agent content. 496/496 tests pass (was 487 at v0.3.0). FTS5 still NOT wired into the search dispatcher -- that ships in v0.3.2.

### Added
- **`claude_session_backup/transcript_walker.py`** (new module) -- single source of truth for the JSONL → `ImportRow` + `FileOpRow` extraction. Both the FTS5 importer and the Phase 1 JSONL search use it, so the two paths now produce the same role surface (USER / AI / `AGENT:<subtype>`) and the same file-op metadata.
- **`file_operations.strength`** column on each per-project FTS5 DB -- INTEGER, NOT NULL, DEFAULT 2. Assigned at import time per operation kind: 3 = active modification (`wrote`, `edited`, `notebook_edit`), 2 = passive `read`, 1 = `searched` (Grep probe). Enables future ranking queries like "files this session was actually working on" without a Python post-pass.
- **Per-project DB schema versioning** -- new `fts_schema_version` table inside each per-project FTS5 DB. v0.3.0 DBs (no version table, no strength column) are detected as v1 and migrated in place on first open: `ALTER TABLE` adds the strength column, then values are backfilled from the operation kind, then the version is stamped to 2. Verified against 49 real DBs on disk.
- **`transcript_walker.format_role_label(role, role_subtype)`** -- shared helper that renders the (role, role_subtype) tuple as `"AGENT:explore"` / `"USER"` / `"AI"`, matching the role-token grammar Phase 1 `.convo` / `.sesslog` parsers already produce.
- **9 new tests** -- 5 in `test_fts5_db.py` (strength column present + correct type/default, `fts_schema_version` table, v1→v2 in-place migration with backfill, migration idempotency, end-to-end strength write at import time); 4 in `test_search.py` (`--source jsonl` surfaces skill-attributed assistant events as `AGENT:<skill>`, Task-launched Agent tool_result blocks surface as `AGENT:<subtype>`, role subtype is lowercased consistently, the new optional `session_id` arg is back-compatible with the single-arg call form). Total 496/496 (was 487).

### Changed
- **`csb search --source jsonl` now sees Task-launched sub-agent content.** Pre-v0.3.1 the Phase 1 JSONL parser only looked at user/assistant text blocks and silently dropped `tool_result` blocks, so output from `/commit`, `/dev-workflow-process`, `Explore`, `oracle`, `Plan`, `senior-engineer`, etc. was invisible to `csb search` when the source was JSONL. The shared walker tracks `Agent` tool_use → tool_result correlation during the linear walk and labels the matching tool_result text as `AGENT:<subagent_type>`.
- **`csb search --source jsonl` now respects `attributionSkill`.** Skill-launched assistant events (the path used by `/commit`, `/fullpostmortem`, etc.) are labeled `AGENT:<skill>` instead of the previous generic `AI`. Matches what FTS5 import has done since v0.3.0.
- **`parse_jsonl_events(path)` gained an optional `session_id` parameter** -- forwarded to the walker for parity with the FTS5 importer's signature, ignored by the rendered Event. Calls without the arg keep working unchanged.

### Refactored (no behavior change for the FTS5 importer)
- **`fts5_importer.py` is now a thin shim** (~120 LOC down from ~430). The walker functions (`iter_rows_from_jsonl`, `ImportRow`, `FileOpRow`, `_extract_file_ops`, `_extract_agent_tool_uses`, `_find_matching_tool_result`, `_flatten_text_blocks`, `_flatten_tool_result_content`) moved to `transcript_walker.py` and are re-exported from `fts5_importer` so downstream callers / existing tests keep importing from the old location.
- **`_extract_file_ops` now yields `(operation, path, strength)` 3-tuples** (was 2-tuples) so the strength weight follows the row to the importer's INSERT statement.
- **`_FILE_OP_TOOLS` constant** gains the strength field per entry.

### Carried over from v0.3.0
- `csb build-fts5` and per-project FTS5 DB convention unchanged. Existing v0.3.0 DBs get migrated to v2 on next open (transparently); a `csb build-fts5 --force` will produce identical row content with the new strength column populated.

## [0.3.0] -- 2026-05-17 (prealpha)

Phase 2 infrastructure for issue #3: per-project SQLite FTS5 content indices. Adds the `csb build-fts5` command that imports each session's JSONL transcript into `~/.claude/csb-fts/<project>__<slug-hash>_<USER>.db` with role-aware classification, sub-agent attribution, and file-operation metadata. `csb search` behavior is **unchanged in v0.3.0** -- this is the data-layer foundation; v0.3.1 will wire it into the search dispatcher. 487/487 tests pass.

### Added
- **`csb build-fts5`** -- new subcommand that builds / refreshes the FTS5 indices. Flags: `--project <slug>` (limit to one), `--session-id <uuid-prefix>` (limit to one via the shared resolver), `--force` (rebuild unconditionally), `--quiet`. Idempotent: skips sessions whose JSONL mtime hasn't advanced past `indexed_sessions.last_jsonl_mtime`. The per-project DB convention (`<project>__<slug-hash>_<USER>.db`) was locked at v0.2.5 in `fts_paths.py`; v0.3.0 fills in the actual DB schema + ingest.
- **Per-project FTS5 schema** -- `messages` base table (id, session_id, uuid, message_index, role, role_subtype, content, timestamp) + `messages_fts` virtual table with `content='messages'` external content + `porter unicode61` tokenizer. Sync triggers (INSERT/UPDATE/DELETE → FTS) mirror the claude-vault production pattern. `indexed_sessions` table inside each per-project DB is the authoritative "is this session indexed" tracker.
- **Two AGENT-attribution paths** in the JSONL importer:
  - **Skill-attributed** (slash-command skills like `/commit`, `/dev-workflow-process`): `message.attributionSkill` on `type:'assistant'` events → `role='AGENT'`, `role_subtype=<skill>`.
  - **Task-launched** (Agent-tool sub-agents like `Explore`, `Plan`, `oracle`, `senior-engineer`): tracked via `tool_use.id` → `subagent_type`, then the matching `tool_result` block in the next user event is labeled `AGENT:<subagent_type>`. This is the path that puts sub-agent output into the search corpus -- previously invisible to grep.
- **File-operation metadata** (`file_operations` table inside each per-project DB) -- discoverability layer for "which conversations touched which files." Populated from path-bearing tool_use blocks during the same JSONL walk: `Read` → `op='read'`, `Edit` → `'edited'`, `Write` → `'wrote'`, `Grep` → `'searched'`, `NotebookEdit` → `'notebook_edit'`. Bash command parsing deliberately deferred. The search-side UX (`csb files <pattern>` / `csb search --files <glob>`) ships in a future patch -- v0.3.0 just captures the data so users build the index once.
- **`fts5_db.py` / `fts5_importer.py` / `fts5_index.py`** -- three new modules:
  - `fts5_db.py` (~190 LOC) -- schema, `open_fts5_db`, `init_fts5_schema`, `is_session_indexed`, `mark_session_indexed`, `delete_session`, `escape_fts_query`, `fts5_available` probe
  - `fts5_importer.py` (~330 LOC) -- `iter_rows_from_jsonl` (streams `ImportRow` + `FileOpRow` with both AGENT paths), `import_jsonl_to_db`, content-hash helper
  - `fts5_index.py` (~200 LOC) -- `build_all` orchestrator with project / session filters, freshness check, force re-index, per-session error tolerance
- **64 new tests** -- 21 in `test_fts5_db.py` (schema, triggers, dedup, freshness, escape), 33 in `test_fts5_importer.py` (every role classification path including Agent tool chain, file-op extraction, dedup-on-reimport, malformed JSON tolerance, system-reminder verbatim preservation), 10 in `test_fts5_index.py` (orchestrator: single + multi-project, idempotency, mtime change → re-index, force, project / session filters, deleted-skip, missing-file-skip, session_sources hint update). Total 487/487 (was 423 at v0.2.10).

### Behavior unchanged
- `csb search` still walks `.convo` / `.sesslog` / JSONL files. The FTS5 backend exists but is not yet consulted -- v0.3.1 will add the smart-fallback dispatcher.

### Notes
- Content cleaning policy: **no stripping** of `<system-reminder>`, `<command-name>`, etc. (csb preserves verbatim, diverges from claude-vault which cleans).
- Tokenizer choice: `porter unicode61` (same as claude-vault's production setup).
- Per-project DBs (not one monolithic vault): smaller files, faster targeted queries, per-project archive/move/delete, multi-user safety via the `_<USER>` filename suffix.
- Schema migration: **NO** `schema_version=4` bump on the main DB. Per-project DBs are self-contained; main DB stays at v3. The reserved slot remains available for a future cross-DB linker table if one is ever needed.

## [0.2.10] -- 2026-05-17 (prealpha)

`csb search` polish pass to bring it to parity with `csb list` and `csb scan`: per-session sort order, escalating richer-info levels (`-f` / `-ff`), readable date format by default, visual block separation. 420/420 tests pass.

### Added
- **`csb search --sort {last-used,expiration,started,oldest,messages,size}`** -- mirrors `csb list --sort` exactly. Default `last-used` matches the v0.2.6 implicit behavior, so adding the flag is non-breaking. The chosen sort drives the session enumeration order in `search()` and therefore which sessions surface first under `--limit`, what `--sessions-only` lists at the top, and which session the `Next:` hint points at. ORDER BY clauses are reused from `index.SORT_SQL` (the same whitelist `csb list` consumes) -- one source of truth for sort vocabulary.
- **`csb search -f` / `--full-info`** (level 1) -- adds `started: <date> (purge in Nd)` second header line per session, matching the format `csb list` shows. Reuses `timeline.relative_date` / `format_timestamp` / `purge_countdown` so wording stays consistent across the CLI. `cleanup_days=0` (no purge configured) suppresses the countdown half cleanly.
- **`csb search -ff`** (level 2) -- escalates further to add the folder list (start_folder + top N other folders with usage counts) and a `N messages | vX.Y.Z` meta line. Brings search output to parity with `csb list` / `csb scan` per-session detail. In `--sessions-only` mode the helper suppresses its own `start at:` line (the renderer already prints one with the inline `[csb resume ...]` hint) to avoid duplication.
- **argparse `action="count"`** on `--full-info` -- standard verbosity-style escalation (`-f`, `-ff`, `-fff...`). `cmd_search` caps the effective level at 2.
- **`Hit.started_at`, `Hit.jsonl_mtime`, `Hit.folders`, `Hit.message_count`, `Hit.claude_version`** -- new fields on the search Hit so renderers don't need extra DB round-trips. `folders` only populated when `search(..., fetch_folders=True)` is requested by the caller (level 2 path).

### Changed
- **Default `csb search` header now shows human-readable last-active**: `<relative> (<human-date>)` (e.g. `today (2026-05-17, at 10:27)`) replaces the raw ISO `last: 2026-05-17T10:27:00.123Z`. The relative+human form is far easier to scan at a glance and takes about the same column width. Raw ISO is preserved in `--json` output and `csb show <uuid>` for the rare case where exact timestamps are needed for grep'ing the JSONL.
- **Session names render in bold cyan**, matching `csb list` and `csb scan` conventions. Previously plain bold, which competed with hit content above it for visual prominence.
- **Blank line between session blocks** in both default and `--sessions-only` modes. Adjacent session headers were running together; the separator makes it obvious where one session's hits end and the next session begins.

### Notes
- 36 new tests across `test_search.py` (6 for --sort + 3 for fetch_folders/Hit fields), `test_cli.py` (6 for --sort + 6 for -f / -ff levels), and `test_search_render.py` (12 for --full-info level 1 / human-readable default / bold-cyan ANSI / blank-line separator + 8 for level-2 folder list / meta line / sessions-only no-duplicate-start-at / level-0-omits-all). Total 420/420 pass (was 384 at v0.2.9).

## [0.2.9] -- 2026-05-17 (prealpha)

`csb search --sessions-only` for "which sessions mention X" summary queries, plus a small CLI cleanup: `--session` is renamed to `--session-id` and now accepts comma-separated UUID prefixes for multi-session OR-match. The three output modes (`--json`, `--files-only`, `--sessions-only`) are now wrapped in an argparse mutex group so accidental combinations fail loud at parse time. 384/384 tests pass.

### Added
- **`--sessions-only`** -- per-session summary mode for `csb search`. Output shows one block per matching session: name + UUID + project on the first line, `start at: <folder>    [csb resume <uuid>]` on the second, plus a `Next: csb search "<query>" --session-id <first-uuid> -A 3 -B 1` hint to drill into excerpts. When the user doesn't pass `--limit`, sessions-only mode auto-raises the per-call limit (effective 10,000) so a single noisy session can't crowd out other sessions from the summary. Combines cleanly with `--shortid` for compact UUID display.
- **`--session-id` accepts comma-separated UUID prefixes** -- e.g. `csb search foo --session-id abc1,def2,xyz9` OR-matches across three sessions. Empty entries (trailing/leading commas, whitespace-only) are tolerated and stripped before the SQL query. Prefix minimum is the resolver's standard 4 hex chars. (Name-based multi-match isn't supported in the comma form: names contain underscores and hyphens that conflict with the splitter; use `csb scan <name>` for name-based discovery.)
- **`Hit.start_folder`** -- new field on `search.Hit` populated from `sessions.start_folder` so renderers don't need to query the DB. Used by `--sessions-only` for the start-at line.
- **23 new tests** -- 12 in `test_search_render.py` (sessions-only grouping, ordering, shortid threading, full-UUID default, singular/plural grammar, unknown-folder fallback, Next-hint with/without query, empty-hits no-op, dispatcher routing), 5 in `test_search.py` (list-of-prefixes, empty-list-is-all, bare-string-still-works, tolerates-empty-entries, Hit.start_folder populated), 6 in `test_cli.py` (--session-id accepts, --session rejected, three mutex combinations, --sessions-only parses). Total 384/384 pass.

### Changed (BREAKING)
- **`--session` -> `--session-id`** -- the `csb search` flag is renamed for clarity. `--session` always took a UUID prefix (never a session name); the new name makes that unambiguous and aligns with the `<session_id>` vocabulary used by `csb show` / `csb resume` / `csb restore`. No deprecation alias -- the flag is hours old in real-world terms, very unlikely anyone scripted it yet.

### Notes
- `--files-only`, `--json`, and `--sessions-only` are now mutually exclusive via argparse's `add_mutually_exclusive_group()`. Combining them fails at parse time with a clear error. The default (no flag) remains the grouped human-readable excerpt mode.
- Why `--sessions-only` auto-raises the limit: with `--limit 20` (the search default), a single session with 25+ matches would yield all 20 hits before the iterator visited the second session. Sessions-only mode's semantic is "show me ALL sessions that mention this," not "show me 20 hits' worth of sessions." If the user explicitly passes `--limit N`, that value is respected.

## [0.2.8] -- 2026-05-17 (prealpha)

Phase 3 of the restore shoring-up plan. Closes #27 by surfacing deleted sessions in `csb scan` and `csb list`, adding a filter-aware "N deleted hidden" footer to `csb list`, and adding bulk-restore via `csb scan --deleted --restore`. Test count 304 → 322 (+18 Phase 3 tests). README now has a Recovery section (closes the deferred README AC from #29). Version renumbered from 0.2.7 to 0.2.8 to clear the slot for main's `v0.2.7` (short-UUID sugar) work, which merged in concurrently.

### Added
- **`csb scan --deleted`** / **`csb scan --all`** -- mutually exclusive flags that change which sessions a scan returns. Default behavior unchanged (active-only). `--deleted` returns only DB-flagged deleted sessions in the scoped folder; `--all` returns both active and deleted. Works in every scan mode: bare-cwd, `-d` / `-D` / `-s` path-strict, broad-term, and combined (`-d <pattern> <term>`). When `--deleted` is set, csb skips the filesystem-existence check on the scope path so users can still query for sessions whose original folder was deleted.
- **`csb scan --restore`** + **`--dry-run`** / **`--yes`** / **`--force`** -- bulk restore of every deleted session matching the scope. Implies `--deleted`. Confirms before restoring >1 file unless `--yes` is given. `--dry-run` previews per-file. Default refuses to overwrite an existing on-disk file (use `--force` to override). Takes `backup_lock` for the file-write phase so a concurrent `csb backup` can't race the restore and mark the just-restored file as "new" with current mtime.
- **Filter-aware footer for `csb list`** -- when running in default active-only mode and the DB has matching deleted sessions, csb prints a one-line footer with the count and the correct suggested command. The count and the suggested command both respect the user's filter keyword: `csb list` prints `"3 deleted sessions hidden -- run \`csb list --deleted\` to see"`; `csb list amd` prints `"2 deleted sessions matching 'amd' hidden -- run \`csb list amd --deleted\` to see"`. Suppressed when count is zero, when `--deleted` is set (user is already looking at them), or when `--all` is set. New `count_deleted_with_filter()` helper in `index.py` reuses `list_sessions`'s exact WHERE-predicate construction so the count's scope matches the filter exactly.
- **`README.md` "Recovery" section** -- documents the full deleted-session discovery + recovery story end-to-end: find, restore single, restore many, what restore does NOT do (mtime, subdirs, other state files). Closes the `Document in README "Recovery" section: csb restore is byte-exact regardless of host git's autocrlf settings` AC from #29 that was deferred to this phase per the plan.
- **18 new tests in `tests/test_restore.py`**: SQL deleted-filter for `find_sessions_by_directory` / `find_sessions_by_term` (5 including unknown-filter ValueError), `count_deleted_with_filter` (2 unfiltered + keyword), filter-aware footer in `cmd_list` (4 including the no-footer-when-zero / no-footer-when-deleted-shown cases), `csb scan --deleted` / `--all` (2), bulk restore (5: dry-run, yes-skips-prompt, refuse-existing-without-force, force-overwrites, empty-scope-says-nothing-to-restore).

### Changed
- **`find_sessions_by_directory` and `find_sessions_by_term`** in `index.py` -- new keyword-only `deleted_filter` parameter (default `"active"` preserves pre-#27 behavior). Accepts `"active"` / `"deleted"` / `"all"`. Centralized via a new `_deleted_filter_clause` helper so the three scan paths stay in sync. Unknown values raise `ValueError`.
- **`cmd_scan` skips the scope-path filesystem-existence warning** when running in `--deleted` or `--all` mode. The warning was correct for the active-only use case but misleading for "find sessions in this folder that I deleted" -- the folder may also be gone. The SQL pass against the DB handles missing scope paths fine.
- **`csb scan` user-facing wording** -- the noun in the "Scanning for ..." / "Found N ..." line now reflects the deleted-filter mode: `"sessions"` / `"deleted sessions"` / `"sessions (active+deleted)"`. Helper `_session_noun()` keeps this in one place.

### Plan reference
Phase 3 of the four-phase restore shoring-up plan (`2026-05-16__16-30-43__claude-plan__shore-up-csb-restore-subsystem.md`). Phase 4 (end-to-end hand-runnable checklist, closes #13) ships next and converts the Phase 0 `restore_reality_check.py` evidence into a permanent procedure including `claude --resume` validation.

## [0.2.7] -- 2026-05-17 (prealpha)

Short-UUID sugar: type `csb show 7250ddce` (prefix), `csb resume c6793d73adaf` (suffix), or even `csb show 916441e6-...-1d090ef5` (the compact display form copied from `csb list --shortid` / `csb search`) instead of typing the full 36-char UUID. One shared resolver and one shared display helper, used by every csb command that takes or shows a session ID. `csb show` output is now Rich-colorized to match the `csb list` / `csb scan` visual style, and `Started:` / `Last active:` / `DELETED at:` timestamps now show local time alongside the original ISO 8601 UTC string for easier reading without losing exact searchability. 361/361 tests pass.

### Added
- **`claude_session_backup/ids.py`** -- shared session-ID resolver and display helper. `resolve_session_id(conn, query)` accepts: full UUIDs, prefixes (>=4 hex chars), suffixes (>=4 hex chars), or the compact display form `<head>-...-<tail>`. Four-tier matching: compact-form > exact full-UUID > prefix > suffix. On ambiguous match: raises `AmbiguousSessionID` with the candidate list (name, project, start-at path); the CLI prints them and exits 2, the user re-runs with a longer prefix. No interactive prompts -- stays scriptable. `format_short_uuid(uuid, head=8, tail=8)` returns the compact display form (e.g. `7250ddce-...-3d73adaf`).
- **`_resolve_session_or_exit()` helper** in `commands.py` -- standard error-to-exit-code mapping (1 for no-match, 2 for ambiguous / invalid input). Used by `cmd_show`, `cmd_resume`, `cmd_restore`. Any csb command that takes a session-ID input goes through this single path.
- **`--shortid` / `-sid` flag on `csb list`, `csb scan`, and `csb search`** -- opt-in to the compact UUID display form (`<head>-...-<tail>`). Default everywhere is the full UUID so users can paste directly into `claude --resume <uuid>` (the native claude binary has no short-form resolver). The compact form, when chosen, round-trips back through csb's resolver if pasted as input -- so copy-from-output is always safe within csb-land.
- **`csb show` output is now Rich-colorized**: session name in bold cyan, start folder in bold green, deleted markers in red, the `Resume:` and `Restore with:` hints in bold yellow, dim styling for labels and metadata. Plain-text fallback kept for environments without Rich.
- **Human-readable timestamps in `csb show`** -- `Started:`, `Last active:`, and `DELETED at:` now render as `<local YYYY-MM-DD HH:MM:SS> (<tz>) [ <original ISO> ]`. The local-time prefix makes scanning easy; the bracketed ISO string is kept so users can grep the JSONL by exact timestamp. Falls back to numeric UTC offset (e.g. `-04:00`) on Windows where `strftime("%Z")` returns long names like "Eastern Daylight Time".
- **34 new tests** -- 30 in `test_ids.py` covering all 4 resolver tiers (input validation, full-UUID exact match, prefix-unique / prefix-ambiguous / longer-prefix-disambiguates, suffix-unique / suffix-ambiguous / suffix-fallback-after-prefix-miss, no-match, compact-form resolve / ambiguous / no-match / rejects too-short or non-hex halves, format_short_uuid round-trip, format_ambiguous_error truncation and null-metadata tolerance), plus 4 in `test_commands.py` for `_format_timestamp` (none, ISO retains original, TZ label present, unparseable falls back). Total: 361/361 pass (was 327 at v0.2.6; +34 net).

### Changed
- **`csb show <prefix>`, `csb resume <prefix>`, `csb restore <prefix>`** now accept any unambiguous prefix or suffix (>=4 chars) instead of requiring the full UUID. Backward-compatible -- full UUIDs continue to work. On collision, the CLI lists the candidates and the user re-runs with a longer prefix.
- **All csb commands display the FULL UUID by default** -- `csb list`, `csb scan`, `csb search`, and `csb show` are uniform: full UUID is the visible default everywhere. This keeps the copy-paste-into-`claude --resume` workflow friction-free across the entire CLI. Use `--shortid` / `-sid` to opt into the compact display when readability matters more than paste-into-native-tools.

### Notes
- The compact display form ``<head>-...-<tail>`` is round-trip-safe: csb commands that take a session ID input accept the same string that csb displayed. Pasting from `csb search` or `csb list --shortid` works directly. Native `claude --resume` still needs the FULL UUID since claude has no resolver -- which is why csb's list/scan defaults to showing the full form.
- 4 chars is the minimum length per half (head or tail). Below that, matching is meaningless across 100+ sessions; we reject early with a clear error rather than degrade to "guess from many candidates".
- Collision UX is non-interactive: print candidates, exit 2. Scripts can detect ambiguity by exit code and resolve programmatically.

## [0.2.6] -- 2026-05-16 (prealpha)

`csb search` now searches transcript content. Phase 1 of #3 (FTS5 epic) is complete -- the breaking change to `csb search`'s semantics is live, and `cmd_search` walks the `session_sources` paths populated by 0.2.5's backup integration. Metadata search (which `csb search` used to do) lives in `csb list <filter>` and `csb scan <term>`. 327/327 tests pass.

### Changed (BREAKING)
- **`csb search <query>` now searches transcript content** -- previously it ran a SQL `LIKE` against session name / project / start folder. It now walks every indexed session's `.convo_*.log` (preferred) or `.sesslog_*.log` (USER/AI/AGENT filter) or `<uuid>.jsonl` (authoritative fallback) for matches in conversation text. Hit output groups results by session, shows role tag + timestamp + line number + optional context window, and truncates long matches at 500 chars. Empty-result output emits a stderr hint pointing at `csb list <filter>` / `csb scan <term>` for metadata search. (Phase 1 of #3.)
- **`csb search` flag set rewritten** -- new flags: `-E/--regex` (Python re), `-s/--case-sensitive`, `-A N` / `-B N` / `-C N` (grep-style context, in events not lines), `--session UUID` (constrain to one session by UUID prefix), `--source {auto,convo,sesslog,jsonl}` (force a source channel), `--all` (include deleted), `--deleted` (only deleted), `--limit N` (default 20), `--full-match`, `--no-color`, `--json` (NDJSON), `--files-only`. The old `-n N` (max results) is replaced by `--limit N`.

### Added
- **`claude_session_backup/search.py`** -- the content search engine. `search()` is a generator yielding `Hit` dataclasses; `parse_log_blocks()` handles `.convo` and `.sesslog` multi-line block format (`[[ts]] {ROLE: ... }` with closing `}` on its own line) and accepts USER, AI, AGENT, and AGENT:`<subtype>` role tags (the subtype is preserved for filtering / display); `parse_jsonl_events()` walks `type:user` / `type:assistant` events and flattens assistant content blocks to text. Per-session source preference is `.convo > .sesslog > jsonl` with override; `_build_matcher()` switches between literal substring and Python regex.
- **`claude_session_backup/search_render.py`** -- three output modes: `human` (default, ANSI-colored, session-grouped), `files-only` (one source path per line), and `json` (NDJSON, one hit per line). Long matched text truncates at 500 chars; context lines at 200. ANSI auto-disables when stdout is not a TTY or `--no-color` is set. UTF-8 stdout reconfiguration in `cmd_search` handles em-dashes / smart quotes from transcripts on Windows cp1252 terminals.
- **`claude_session_backup/fts_paths.py`** -- Phase 2 scaffolding. Per-project FTS5 databases will live at `<claude_dir>/csb-fts/<project>__<slug-hash>_<USER>.db`. The naming pattern mirrors claude-session-logger's `<Name>__<UniqueID>_<USER>` convention to satisfy four constraints: per-project (deliberate deviation from claude-vault's monolithic vault), multi-user safe, recognizable by project name, and collision-free across same-named projects in different on-disk locations (slug-hash differentiates). Phase 1 ships only the contract -- `fts5_db_exists()` always returns False until Phase 2.
- **60 new tests** -- 36 in `test_search.py` (block parser including AGENT and AGENT:subtype, JSONL parser including content-block flattening, matcher cases, source preference, end-to-end search with context windowing, session filter, source override, deleted-inclusion modes, limit, ordering), 24 in `test_fts_paths.py` (project-name sanitization, slug-hash determinism + collision resistance, current-user fallback, filename format regex, list_fts_dbs surfacing existing DBs). Total: 327/327 pass (was 267 at v0.2.5).

### Notes
- "Events" are the natural unit of context: one block in `.convo` / `.sesslog`, one user/assistant message in JSONL. `-A 3` shows the next 3 events after the matched event, not 3 lines.
- The breaking flag-set change is intentional and authorized -- prealpha, few users, redesigned for clarity. Users who scripted the old `csb search -n N <query>` style need to update to `csb search <query> --limit N` (and accept that the meaning shifted from metadata to content).
- AGENT tag support is forward-compatible: when claude-session-logger emits `{AGENT:explore: ...}` blocks, `csb search` will find their content. Bare `{AGENT: ...}` also works.
- Phase 2 (FTS5 + Porter stemming over per-project DBs) closes #3 fully and ships in a follow-up; the `fts_paths.py` contract is locked so Phase 2 only adds the indexer + a small `messages_fts_meta` linker table.

## [0.2.6] -- 2026-05-16 (prealpha, restore Phase 2 / #28 -- parallel work, same version slot as the search rewrite above)

Phase 2 of the restore shoring-up plan. Closes #28 by letting `csb restore` fall back to git history when the DB has no row for the requested session. Affects users post-`rebuild-index`, on a fresh machine (DB lost / never built), or restoring sessions committed by something other than csb. Test count: 250 → 304 (+12 Phase 2 tests on top of v0.2.5's +41).

> **Note**: This entry and the `[0.2.6]` entry above (`csb search` content rewrite) were two parallel commits both targeted at `v0.2.6` on different feature branches, both merged into `main`. Both are real, both shipped. Future releases will not double-claim a version.

### Added
- **`git_find_jsonl_by_uuid(claude_dir, uuid)`** in `git_ops.py` -- walks `git log --all` for any `projects/*/<uuid>.jsonl` path. Uses git's `:(glob)` pathspec magic so the `*` matches exactly one path component (excludes subagent JSONLs under `projects/<slug>/<uuid>/subagents/...`). Returns a sorted list of distinct repo-relative paths. Empty list = never tracked. Multi-path list = slug-collision (rare; happens if the original cwd was renamed between csb backups).
- **`csb restore <full-uuid>` git-history fallback** -- when `get_session()` returns None (DB row missing), `cmd_restore` calls `git_find_jsonl_by_uuid` to recover the path from git history, then proceeds through the existing `git_find_deleted_file` + `git_restore_file` pipeline. Output includes a `(restored via git-history fallback -- DB had no row for this UUID)` notice so users can see which path was taken. The fallback requires the **full UUID** (36 chars, hyphenated) because the git-side lookup is an exact filename match; prefix-only IDs get a clear error directing the user to supply the full UUID.
- **Slug-collision handling** -- when `git_find_jsonl_by_uuid` returns multiple paths, `cmd_restore` exits 1 with the candidate list and instructions for manual recovery via `git log` / `git show`. No auto-pick (the right choice depends on which slug the user actually wants the session under).
- **12 new tests in `tests/test_restore.py`** -- `git_find_jsonl_by_uuid` (6 tests: single-path, deleted-file recovery, unknown UUID, subagent exclusion, slug collision, empty input, glob-pathspec depth check); `cmd_restore` fallback path (6 tests: DB-missing-but-git-has-it, neither, full-UUID requirement, refuse-to-overwrite-existing, DB-row regression, fallback `--dry-run`).

### Changed
- **`cmd_restore` now refuses to overwrite an existing on-disk file when the DB has no row** -- previously the only overwrite guard checked `session.deleted_at`; if the DB had no row at all, the file could be silently replaced. New logic: when reaching the fallback path AND the target file already exists on disk, exit 1 with "Refusing to overwrite without explicit need." Users who genuinely want a re-restore can delete the file first, which is what the deleted-at flow assumes anyway.

### Plan reference
Phase 2 of the four-phase restore shoring-up plan (`2026-05-16__16-30-43__claude-plan__shore-up-csb-restore-subsystem.md`). Phase 3 (`csb scan --deleted` + bulk restore + filter-aware "N deleted hidden" footer, #27) ships next. Phase 4 (end-to-end hand-runnable checklist closing #13) ships after Phase 3.

## [0.2.5] -- 2026-05-16 (prealpha)

Phase 1 infrastructure for transcript content search (#3). This release lands the schema and data-collection layer that the upcoming `csb search` rewrite will read from -- the user-facing CLI surface change ships in a later commit. Existing behavior is unchanged; the only observable difference is a one-line `csb: migrated DB schema to v3` notice on the first invocation after upgrade and a small extra cost during `csb backup` to register transcript source paths. 267/267 tests pass.

Coordinates with the parallel `claude-session-backup__restore-shoring-up` branch (planned 0.2.4) by jumping to 0.2.5; either ordering of merges is valid.

### Added
- **Schema migration framework** (`claude_session_backup/migrations.py`) -- versioned migrations keyed off the existing `schema_info.schema_version`. Runs automatically on first `init_schema()` after upgrade with a one-line `csb: migrated DB schema to vN` notice; silent on subsequent invocations and on fresh databases. Designed to absorb future schema changes (Phase 2 FTS5, etc.) without per-PR plumbing.
- **`session_sources` table** (schema v3) -- one row per searchable transcript file per session. Columns: `session_id` (FK with ON DELETE CASCADE), `project`, `source_type` (`convo` / `sesslog` / `jsonl`), `source_path`, `size_bytes`, `mtime`, `last_seen`. Plus FTS5-ready columns `fts5_indexed_at` and `content_hash` that Phase 2 will populate; left NULL in Phase 1 so the schema doesn't change between phases. UNIQUE on `(session_id, source_path)`. Indexes on `session_id`, `project`, and `fts5_indexed_at`.
- **`claude_session_backup/sesslog_scanner.py`** -- new module that discovers transcript sources for a session. `list_sesslog_folders(claude_dir)` walks `~/.claude/sesslogs/` once and returns `{session_uuid: folder_path}` by parsing claude-session-logger's `<SessionName>__<UUID>_<USER>` folder convention. `list_session_sources(jsonl_path, sesslog_folder)` enumerates `.convo_*.log`, `.sesslog_*.log`, and the JSONL itself for a session, filtering out tool-call subchannels (`.sesslog-bash_`, `-grep_`, `-glob_`, `-powershell_`) and ancillary channels (`.shell_*`, `.tasks_*`, `.agents_*`, `.overflow_*`). Returns `SourceRow` dataclasses with size/mtime captured at scan time. Missing-file rows (JSONL deleted, etc.) still emit a row with NULL size/mtime so search can report "this session's content is no longer on disk."
- **`register_session_sources()`** in `claude_session_backup/index.py` -- mirrors the delete-then-insert pattern `upsert_session()` uses for `folder_usage`. Replaces all `session_sources` rows for a session in a single transaction; idempotent across repeated `csb backup` runs. Accepts both `SourceRow` instances and plain dicts. Returns `(added, removed)` counts for caller-side logging.
- **`cmd_backup` populates `session_sources` automatically** -- after each `upsert_session()` call, `cmd_backup` now calls `list_session_sources()` + `register_session_sources()` to keep the table current. The sesslog folder lookup happens once before the loop (one filesystem walk per backup, not per session). Wrapped in a per-session try/except so a source-registration failure for one session never aborts the whole backup. The existing `csb backup` output is unchanged; verbose-mode summary of source rows registered is deferred to a follow-up.
- **41 new tests** -- 12 in `test_migrations.py` (fast path for fresh DBs, v2->v3 upgrade, idempotency, quiet mode, column schema, FK cascade, UNIQUE constraint), 18 in `test_sesslog_scanner.py` (UUID extraction with case-folding, folder enumeration, file-pattern filtering, ancillary-channel rejection, missing-file fallback, size/mtime capture), and 11 in `test_session_sources.py` (basic upsert, dict-vs-dataclass input, empty-source clearing, replace semantics, per-session isolation, FK cascade, FTS5 columns left NULL, project denormalization for FTS5 routing). Total: 267/267 pass (was 226 at v0.2.3).

### Notes
- This is **infrastructure only** -- no CLI surface changes. The `csb search` command still does its v0.2.3 metadata-LIKE behavior. The user-visible content-search experience ships in a separate commit (and the breaking change to `csb search` semantics ships in 0.3.0).
- The FTS5-ready columns (`fts5_indexed_at`, `content_hash`) are intentional dead weight in Phase 1 so Phase 2's per-project FTS5 indexer can populate them without a schema migration.
- Per-project FTS5 databases (Phase 2) will live at `~/.claude/csb-fts/<project>__<slug-hash>_<USER>.db`. The path convention is locked in the plan doc; `fts_paths.py` and the FTS5 indexer ship in a later commit.

## [0.2.4] -- 2026-05-16 (prealpha)

Phase 1 of the restore shoring-up plan. Closes #29 (alpha-blocker) by making `csb restore` byte-pure on Windows. Adds the first 24 automated tests against the restore code path (previously zero coverage). Phase 0 reality-check (`tests/one-offs/restore_reality_check.py`) confirmed two stacked bugs in `csb restore`; this release fixes both.

### Fixed
- **`csb restore` was functionally broken on Windows due to backslash paths passed to `git show`** -- `commands.py:_cmd_backup_inner` stored `jsonl_path` in the DB as `str(Path.relative_to())` which produces backslashes on Windows. `cmd_restore` then passed that string to `git_show_file`, which embedded it into `git show <commit>:<path>` -- and git silently returns nothing when `<path>` has backslash separators. Restore reported "Failed to restore..." for every Windows user, regardless of CRLF. Fixed in two places: (a) `commands.py` now stores `rel_path` via `.as_posix()` so the DB has forward slashes going forward; (b) `git_ops.py` introduces `_normalize_git_path` which the boundary functions (`git_show_file`, `git_show_file_bytes`, `git_restore_file`, `git_find_deleted_file`) call internally -- so legacy backslash-stored DB rows are also handled transparently without a schema migration. Closes #29.
- **`csb restore` could mutate JSONL bytes on Windows even after fixing the path bug** -- `git_show_file` used `subprocess.run(text=True)` which decodes git's stdout via Python's universal-newline mode (`\r\n` → `\n`), then `Path.write_text(content, encoding="utf-8")` re-encoded on the way out (text mode on Windows: `\n` → `\r\n`). Combined with git's `core.autocrlf` smudge filter, the restored bytes could differ from the committed blob in three independent ways. The restore path now uses the new `git_show_file_bytes` helper (subprocess with `text=False` + git invoked with `-c core.autocrlf=false -c core.eol=lf -c core.safecrlf=false`) and writes via `dest.write_bytes()`. The round trip is now byte-exact regardless of host git config. Verified by `tests/test_restore.py::test_git_restore_file_byte_pure_roundtrip_lf` and `::test_git_restore_file_preserves_embedded_cr`. Closes #29.

### Added
- **`csb backup` now maintains a `.gitattributes` block in `~/.claude/`** -- defense in depth against future commits storing CRLF-translated blobs. The new `ensure_gitattributes(claude_dir)` helper (in `git_ops.py`) idempotently writes a marker-delimited block containing `*.jsonl -text`, `*.json -text`, `*.name-cache -text`. Called once per backup. If the file already has the csb-managed block, no-op. If the user has their own rules, they're preserved verbatim. The marker pair (`# >>> csb-managed block (do not edit between markers)` / `# <<< end csb-managed block`) lets future csb releases update the block precisely without clobbering user content. (#29 defense-in-depth)
- **`git_show_file_bytes(claude_dir, commit, file_path)`** -- new byte-pure helper in `git_ops.py`. Runs git with autocrlf/eol/safecrlf disabled and captures stdout in binary mode. Returns raw `bytes` (or `None` if the file isn't in that commit). Used by `git_restore_file`. Available to future callers that need byte fidelity (e.g., Phase 2 git-history fallback).
- **`_normalize_git_path(file_path)`** -- new helper in `git_ops.py`. Converts backslash to forward slash and strips leading separators. Applied internally by all git-boundary functions. Accepts `str` or `Path`.
- **`tests/test_restore.py`** (NEW FILE, 24 tests) -- first test coverage for restore. Sections: path normalization (5), `git_show_file_bytes` (5 including empty/10MB/missing/backslash), `git_restore_file` round trips (7 including embedded CR, empty, 10MB, backslash, Pathlib dest, missing), `git_find_deleted_file` backslash regression (1), `ensure_gitattributes` (4 including idempotent + user-block preservation + already-present detection), end-to-end backup-then-restore (1), `git status` no-spurious-diff (1), POSIX regression (1, skipped on Windows).
- **`tests/one-offs/restore_reality_check.py`** -- Phase 0 sandboxed reality-check script. Builds a temp git repo (auto-cleaned via `TemporaryDirectory`), runs 7 scenarios, emits a markdown report tabulating (Scenario × Layer × Hypothesis × Observed × Verdict). Never touches the real `~/.claude/`. Useful as a manual regression check on any new host.

### Changed
- **`git_show_file` now decodes via text mode but normalizes the path first** -- kept for non-restore callers that want the convenience of a string. Restore-side callers must use `git_show_file_bytes`. Signature widened from `str` to `Union[str, Path]` for `file_path`.
- **`git_restore_file` signature widened**: `file_path` and `dest_path` accept `str` or `Path` (previously `str` only). Now writes via `write_bytes` instead of `write_text`. Internal helper `git_show_file_bytes` does the byte-pure read.
- **`git_find_deleted_file` accepts backslash paths** -- internally normalizes via `_normalize_git_path`. Same return value semantics.
- **Test count: 226 → 250** (no regressions, +24 net new tests in `test_restore.py`).

### Plan reference
Phase 1 of the four-phase restore shoring-up plan (`2026-05-16__16-30-43__claude-plan__shore-up-csb-restore-subsystem.md`, design analysis at `2026-05-16__14-57-24__csb-deleted-session-discovery-and-recovery.md`), tracked across issues #27 / #28 / #29 as sub-issues of #13. Phase 2 (git-history fallback for missing DB rows, #28) and Phase 3 (`csb scan --deleted` + bulk restore, #27) land in subsequent releases.

## [0.2.3] -- 2026-05-06 (prealpha)

Closes the v0.2.3 epic bundle: pathkit `start at` semantics (#19), folder-usage long tail with `--top N` / `--all-folders` (#21), `display_top_folders` config (#21 follow-up), `csb scan` term-vs-folder disambiguation with `-d` / `-D` / `-s` flags (#20), pathkit multi-candidate slug disambiguation (#23), and `csb resume` cd + Windows TTY-handoff fix (#24). The release is grounded in a senior-eng upstream-source audit (#25, closed) that ruled out the file-relocation hypothesis and confirmed the slug encoder behavior. 226/226 tests pass.

### Fixed
- **`csb list` / `csb scan` "start at" line now reports the cwd that lets `claude --resume` find the session** -- previously derived from a JSONL `cwd` histogram (most-common cwd across all events), which silently misled users when Claude Code was launched from a parent dir and `cd`-ed into a subdir afterwards. Now derived from the project-dir slug (`~/.claude/projects/<slug>/`) via filesystem-validated reverse decoding in the new `claude_session_backup/pathkit.py` module. Mirrors the upstream encoder at `claude-code/utils/sessionStoragePortable.ts:311-319` (`replace(/[^a-zA-Z0-9]/g, '-')`); the inverse uses `os.listdir` per directory level and the longest-encoded-entry-first heuristic to disambiguate slugs that have multiple valid filesystem decodings (e.g., a folder literally named `New--Project` vs. `New\.Project`). Returns the sentinel `<unresolved:slug>` when no candidate decodes (e.g., the original cwd has been deleted) so maintainers can still see the slug. Closes #19. (#19, prealpha-blocker)
- **Indexer no longer truncates folder usage to top-N; `csb list --top N` / `--all-folders` reveal the long tail** -- `metadata.py` previously kept only `1 + top_n_folders` rows in the SQLite `folder_usage` table at index time, which made the long tail of cwds invisible regardless of any renderer flag. The slug-decoded "start at" cwd was a frequent casualty (e.g., AMD_INTIGRITI's `C:\` ranked 5th by JSONL count and got dropped), surfacing as a missing `(Nx)` count next to the "start at" line. The indexer now persists every distinct cwd from the JSONL events; the renderer truncates at display time using the new flags. Closes #21.
- **`csb scan` no longer auto-resolves the positional argument as a path against cwd** -- previously, `csb scan amdead` silently treated `amdead` as `<cwd>\amdead` even when the user meant a metadata-search term. The bare positional now means "filter sessions whose name, project, or folder paths contain this term"; explicit path-strict mode is reached via the new `-d` / `-D` flags. Closes #20.
- **`csb resume` now actually launches claude in the correct cwd, with working stdin** -- previously `cmd_resume` printed `cd <start_folder>` as informational text and then called `os.execvp("claude", ...)` from the user's terminal cwd, which made `claude --resume <uuid>` fail with "No conversation found" whenever the user wasn't already in the right directory. The fix has two layers and an implementation note: (a) target is derived from `pathkit.derive_start_at(jsonl_path)` -- the slug-decoded path is the only cwd whose slug matches the JSONL's parent directory, per the upstream-source audit -- with fallback to `start_folder` for legacy session rows that lack `jsonl_path`; (b) the launch uses `subprocess.run(["claude", "--resume", uuid], cwd=target)` rather than `os.chdir + os.execvp`. The subprocess approach is required on Windows because Python's `os.execvp` there is `_spawnv(P_OVERLAY, ...)` -- the parent process exits and a child spawns, but the controlling-TTY relationship doesn't transfer cleanly (claude TUI renders to stdout but stdin keystrokes go into the void). `subprocess.run` inherits the parent's stdin/stdout/stderr handles, so the TUI works correctly. The python parent process stays alive (~30MB) while claude runs and propagates claude's exit code. `FileNotFoundError` from `subprocess.run` disambiguates between "target folder deleted" and "`claude` not in PATH" via `os.path.isdir(target)`. Closes #24.
- **Pathkit slug decoding now disambiguates ambiguous on-disk decodings via JSONL signals** -- when the slug `C--code-New--Project` happens to decode to two real folders (e.g., a literal `New--Project` AND a `New\.Project` sibling), `pathkit.decode_project_slug(slug, first_cwd, folder_usage)` picks the correct one via three-tier fallback: Tier 1 if `first_cwd` matches a candidate exactly or as a prefix-with-separator; Tier 2 if the JSONL's `folder_usage` histogram weights one candidate higher; Tier 3 (encoded-length heuristic) when neither signal matches -- preserving #19's behavior for callers without JSONL access. Path comparisons use case+separator+trailing-slash normalization (stdlib `os.path.normcase`/`normpath`) so Windows variants (`C:\Code\...` vs `c:/code/.../`) compare equal. The `derive_start_at(jsonl_path, first_cwd, folder_usage)` signature mirrors this, and `timeline._resolve_start_at` threads the signals from the existing session dict (no new indexer fields needed). Closes #23.

### Added
- **`csb list --top N` and `csb list --all-folders` (also on `csb scan`)** -- control how many "other" folder rows display beneath the "start at" line. `--top N` shows the top N most-used cwds; `--all-folders` shows everything. Default unchanged (top 3). Mutually exclusive flags. Acceptance criteria from #21 are met. (#21)
- **`display_top_folders` config key** -- persistent renderer default for `csb list` / `csb scan`. Set in `~/.claude/session-backup-config.json` (e.g., `"display_top_folders": 5`) to make 5 the default without typing `--top 5` every time. Negative values mean "show all" (equivalent to `--all-folders`). Resolution precedence: `--all-folders` > `--top N` > config > module default (3). The issue's design considerations explicitly punted this as "separable", but it landed as a small follow-up since the helper plumbing was already in place. (#21 follow-up)
- **`csb scan -d PATTERN` / `--directories-below`** -- path-strict scan: match sessions whose `start_folder` OR top-N `folder_usage` paths equal `<pattern>` or are descendants. Trailing `*` for sibling-prefix expansion (`-d amdead*` matches `amdead`, `amdead-fork`, etc., plus their descendants). Path is resolved cwd-relative if not absolute. (#20)
- **`csb scan -D PATTERN` / `--directory-only`** -- like `-d` but excludes descendants (only this folder, no subdirectories). With wildcard `-D amdead*`, matches sibling basenames at the same directory level only. Mutually exclusive with `-d`. (#20)
- **`csb scan -s PATTERN` / `--start-dir-only`** -- path-strict on `start_folder` only; skips `folder_usage` entirely. Answers "what sessions originated here?" -- useful in a directory to ask "is there anything I can resume that started in this folder?" Trailing `*` for sibling-prefix expansion. Mutually exclusive with `-d` / `-D`. (#20)
- **`csb scan <term>` (positional)** -- broad metadata substring search across session name, project, start_folder, and top-N folder_usage paths. Same vocabulary as `csb list <filter>` but with top-N gating to keep results coherent with what the renderer displays. Combinable with `-d` / `-D` / `-s` for "scope-then-filter" semantics. Emits an `[info]` hint to stderr if the term coincides with a cwd subfolder, suggesting `-d <term>` for path-strict search. (#20)
- **`./dirname` and `.\dirname` shortcut** -- when the positional starts with `./` or `.\` (the conventional shell indicator for a relative path), it's auto-promoted to `-d <dirname>` path-strict mode. So `csb scan ./amdead` is equivalent to `csb scan -d amdead`, no flag-name to remember. Bare `csb scan .` is also promoted (equivalent to `csb scan -d .`). Suppressed if the user already passed `-d` or `-D` explicitly. The shortcut also composes with a term filter: `csb scan ./amdead my-paper` parses as two positionals and is equivalent to `csb scan -d amdead my-paper`. A bare two-positional form without the dot-prefix (`csb scan amdead my-paper`) is rejected with a clear error suggesting the explicit `-d` form. (#20)
- **`csb scan` info / warning hints (stderr-routed)** -- `[info]` when a term coincides with a cwd subfolder; `[warning]` when `-d <pattern>` resolves to a path that doesn't exist (with graceful fallback to broad-term search if a term was provided). Stderr routing keeps `--json` stdout clean for tooling consumers. (#20)
- **`find_sessions_by_directory` and `find_sessions_by_term` SQL helpers** in `claude_session_backup/index.py` with SQLite window-function-based top-N gating (`ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY usage_count DESC)`). Uses `ESCAPE '|'` clause for safe LIKE-pattern composition with user-supplied paths containing `%` / `_`. Requires SQLite >= 3.25 (2018; modern Python ships 3.31+). (#20)
- **73 new tests** -- 25 in `test_index.py` (window-function correctness, top-N gating, escape semantics, deleted-session exclusion, `start_folder_only=True` branch); 27 in `test_cli.py` covering all forms of the new scan grammar + mutex enforcement (including `-s` short/long, `-s/-d` and `-s/-D` mutex); 21 in `test_commands.py` for `_resolve_directory_pattern` (relative + absolute paths, wildcard variants, special character escaping) and `_maybe_promote_dot_prefix` (./ and .\ shortcut auto-promotion). Total: 192/192 pass.
- **34 new tests for #23 + #24 + 2-positional fix** -- 20 in `test_pathkit.py` (`_collect_candidates` returns ALL on-disk decodings; `_disambiguate` Tier 1 / Tier 2 / Tier 3 fallback chain; path-comparison normalization for case + separator + trailing-slash; backward-compat signature for `decode_project_slug` and `derive_start_at` accepting the new `first_cwd` / `folder_usage` kwargs); 9 in `test_commands.py` for `cmd_resume` covering subprocess.run launch with `cwd=target`, returncode propagation, missing-target FileNotFoundError vs missing-claude FileNotFoundError disambiguation, session-not-found, Layer 2 slug-decoded path preference, and fallback to `start_folder` on `<unresolved:>` sentinel or missing `jsonl_path`; 1 in `test_commands.py` for `cmd_scan`'s 2-positional rejection without dot-prefix; 4 in `test_cli.py` for the new `term2` parser positional (dot-prefix + term combo, single-positional case, three-positional rejection, etc.). Total: 226/226 pass.

### Changed
- **`extract_metadata` no longer accepts `top_n_folders`** -- the indexer always stores all rows. Callers updated. Removed the `top_n_folders` config key (was dead config now that the indexer is single-policy).
- **Existing index rows are still truncated until re-indexed.** Run `csb rebuild-index` to backfill the long tail across all sessions; new sessions and any session touched by `csb backup` will pick up the new behavior on the next pass without a full rebuild.
- **Breaking: `csb scan <positional>` now means "filter by term", not "treat as path"**. To preserve pre-v0.2.3 semantics, switch to `csb scan -d <positional>`. The bare `csb scan` (no positional) still defaults to cwd path-prefix; this is the only invocation pattern that is fully backwards-compatible. `csb scan` is prealpha; this break is documented and intentional. (#20)

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

[Unreleased]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.3.5...HEAD
[0.3.5]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.10...v0.3.0
[0.2.10]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.9...v0.2.10
[0.2.9]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.8...v0.2.9
[0.2.8]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/DazzleML/Claude-Session-Backup/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/DazzleML/Claude-Session-Backup/releases/tag/v0.2.0

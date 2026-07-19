"""Tests for command handlers (commands.py).

Focused on pure helpers and dispatcher logic that can be unit-tested without
filesystem fixtures or rendering. End-to-end CLI behavior is covered by the
human test checklist at ``tests/checklists/v0.2.3__Feature__csb-scan-disambiguation.md``.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import claude_session_backup.commands as commands_module
from claude_session_backup.commands import (
    _resolve_directory_pattern,
    _maybe_promote_dot_prefix,
    _format_timestamp,
    cmd_resume,
    cmd_search,
)


# ── _resolve_directory_pattern: -d (with descendants) ──────────────

def test_resolve_d_relative_no_wildcard():
    """`-d amdead` from /work -> resolves to /work/amdead with exact + descendants."""
    cwd = Path("/work")
    full, exact, like_m, like_x = _resolve_directory_pattern(
        "amdead", include_descendants=True, cwd=cwd
    )
    expected_full = str(cwd / "amdead")
    assert full == expected_full
    assert exact == expected_full
    assert like_m == expected_full + os.sep + "%"
    assert like_x is None


def test_resolve_d_absolute_no_wildcard():
    """`-d /code/amdead` (absolute) -> uses path as-is."""
    full, exact, like_m, like_x = _resolve_directory_pattern(
        "/code/amdead", include_descendants=True, cwd=Path("/anywhere")
    )
    expected_full = str(Path("/code/amdead"))
    assert full == expected_full
    assert exact == expected_full
    assert like_m == expected_full + os.sep + "%"


def test_resolve_d_wildcard():
    """`-d amdead*` -> like-prefix only (siblings + descendants), no exact."""
    cwd = Path("/work")
    full, exact, like_m, like_x = _resolve_directory_pattern(
        "amdead*", include_descendants=True, cwd=cwd
    )
    expected_prefix = str(cwd / "amdead")
    assert full == expected_prefix + "*"
    assert exact is None
    assert like_m == expected_prefix + "%"
    assert like_x is None


def test_resolve_d_wildcard_absolute():
    """`-d /code/amdead*` (absolute with wildcard)."""
    full, exact, like_m, like_x = _resolve_directory_pattern(
        "/code/amdead*", include_descendants=True, cwd=Path("/anywhere")
    )
    expected_prefix = str(Path("/code/amdead"))
    assert full == expected_prefix + "*"
    assert exact is None
    assert like_m == expected_prefix + "%"


# ── _resolve_directory_pattern: -D (no descendants) ────────────────

def test_resolve_D_relative_no_wildcard():
    """`-D amdead` -> exact match only, no descendants."""
    cwd = Path("/work")
    full, exact, like_m, like_x = _resolve_directory_pattern(
        "amdead", include_descendants=False, cwd=cwd
    )
    expected_full = str(cwd / "amdead")
    assert full == expected_full
    assert exact == expected_full
    assert like_m is None
    assert like_x is None


def test_resolve_D_wildcard():
    """`-D amdead*` -> prefix LIKE + descendant exclusion."""
    cwd = Path("/work")
    full, exact, like_m, like_x = _resolve_directory_pattern(
        "amdead*", include_descendants=False, cwd=cwd
    )
    expected_prefix = str(cwd / "amdead")
    assert full == expected_prefix + "*"
    assert exact is None
    assert like_m == expected_prefix + "%"
    # Descendants exclusion: prefix + % + sep + %
    assert like_x == expected_prefix + "%" + os.sep + "%"


# ── Special cases ──────────────────────────────────────────────────

def test_resolve_bare_dot():
    """`-d .` -> resolves to cwd itself, exact + descendants."""
    cwd = Path("/work")
    full, exact, like_m, like_x = _resolve_directory_pattern(
        ".", include_descendants=True, cwd=cwd
    )
    # Path("/work") / Path(".") = Path("/work")  (the . is normalized away by Path)
    # This passes through to str() which may keep the dot or not depending on Path semantics.
    # Just verify match criteria are sensible:
    assert exact == full
    assert like_m == full + os.sep + "%"


def test_resolve_user_underscores_escaped_in_like():
    """User input with `_` gets escaped in the LIKE pattern (not in exact)."""
    cwd = Path("/code")
    full, exact, like_m, like_x = _resolve_directory_pattern(
        "my_folder", include_descendants=True, cwd=cwd
    )
    # exact preserves the underscore literally
    assert "_" in exact
    # like_m has the underscore escaped with the | escape char
    assert "|_" in like_m
    # And the trailing "%" wildcard is still our SQL wildcard, not escaped
    assert like_m.endswith(os.sep + "%")


def test_resolve_user_percent_escaped_in_like():
    """User input with `%` gets escaped in the LIKE pattern."""
    cwd = Path("/code")
    full, exact, like_m, like_x = _resolve_directory_pattern(
        "50%-share", include_descendants=True, cwd=cwd
    )
    assert "%" in exact  # exact preserves
    assert "|%" in like_m  # like escapes
    assert like_m.endswith(os.sep + "%")  # trailing wildcard preserved


def test_resolve_returns_strings_not_paths():
    """All returned values are str (or None), not Path objects."""
    full, exact, like_m, like_x = _resolve_directory_pattern(
        "amdead", include_descendants=True, cwd=Path("/work")
    )
    assert isinstance(full, str)
    assert isinstance(exact, str)
    assert isinstance(like_m, str)
    assert like_x is None


# ── _maybe_promote_dot_prefix: ./ and .\ shorthand ─────────────────

def test_promote_dot_slash_prefix():
    """./amdead -> implicit -d amdead."""
    term, promoted = _maybe_promote_dot_prefix("./amdead")
    assert term is None
    assert promoted == "amdead"


def test_promote_dot_backslash_prefix():
    """.\\amdead (Windows) -> implicit -d amdead."""
    term, promoted = _maybe_promote_dot_prefix(".\\amdead")
    assert term is None
    assert promoted == "amdead"


def test_promote_bare_dot():
    """`.` alone -> implicit -d ."""
    term, promoted = _maybe_promote_dot_prefix(".")
    assert term is None
    assert promoted == "."


def test_promote_dot_slash_alone():
    """`./` (just the prefix, no dirname) -> implicit -d ."""
    term, promoted = _maybe_promote_dot_prefix("./")
    assert term is None
    assert promoted == "."


def test_promote_dot_backslash_alone():
    term, promoted = _maybe_promote_dot_prefix(".\\")
    assert term is None
    assert promoted == "."


def test_promote_no_change_for_plain_term():
    """A plain term (no path-prefix) passes through unchanged."""
    term, promoted = _maybe_promote_dot_prefix("amdead")
    assert term == "amdead"
    assert promoted is None


def test_promote_no_change_for_absolute_path():
    """Absolute paths are NOT auto-promoted (user must use -d explicitly).

    The dot-prefix shortcut is for the common 'I'm typing a relative path I see
    in front of me' case. Absolute paths could legitimately be either a literal
    term-as-path (unusual) or a directory; we don't guess.
    """
    term, promoted = _maybe_promote_dot_prefix("/code/amdead")
    assert term == "/code/amdead"
    assert promoted is None

    term, promoted = _maybe_promote_dot_prefix("C:\\code\\amdead")
    assert term == "C:\\code\\amdead"
    assert promoted is None


def test_promote_no_change_for_dotfile_no_prefix():
    """`.gitignore` is a dotfile name, NOT a path prefix -- term-mode."""
    term, promoted = _maybe_promote_dot_prefix(".gitignore")
    assert term == ".gitignore"
    assert promoted is None


def test_promote_no_change_for_double_dot():
    """`..` and `../path` are not auto-promoted (could be either; unusual case).

    If users want to scan a parent directory, they can use -d ../foo explicitly.
    """
    term, promoted = _maybe_promote_dot_prefix("..")
    assert term == ".."
    assert promoted is None

    term, promoted = _maybe_promote_dot_prefix("../foo")
    assert term == "../foo"
    assert promoted is None


def test_promote_handles_none():
    """None passes through (no positional given)."""
    term, promoted = _maybe_promote_dot_prefix(None)
    assert term is None
    assert promoted is None


def test_promote_handles_empty():
    """Empty string passes through (argparse should not give us this normally)."""
    term, promoted = _maybe_promote_dot_prefix("")
    assert term == ""
    assert promoted is None


# ── cmd_resume: subprocess-based launch (#24) ─────────────────────────
#
# cmd_resume must launch `claude --resume <uuid>` with cwd set to the
# slug-decoded path so that claude finds the JSONL. We use subprocess.run
# (not os.execvp) because Python's os.execvp on Windows is _spawnv with
# P_OVERLAY -- the parent exits and a child spawns, but the controlling
# TTY relationship doesn't transfer cleanly (claude TUI renders but stdin
# keystrokes go into the void). subprocess.run inherits the parent's
# stdin/stdout/stderr handles so the TUI works.
#
# Path resolution: target is derived from pathkit.derive_start_at (slug-
# decoded path) when the session row has jsonl_path, else falls back to
# session['start_folder'] for legacy rows.


@pytest.fixture
def mock_resume_env(monkeypatch):
    """Set up cmd_resume's environment with mocks for subprocess and DB access."""
    # subprocess.run is imported INSIDE cmd_resume (`import subprocess`), so
    # patch the subprocess module attribute directly -- the inline import
    # picks up the patched version.
    import subprocess as subprocess_module
    run_mock = MagicMock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(subprocess_module, "run", run_mock)

    # Mock DB layer so the test doesn't need a real SQLite file.
    monkeypatch.setattr(commands_module, "open_db", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(commands_module, "init_schema", MagicMock())

    # Mock the shared session-ID resolver to succeed by default. Tests that
    # need a "not found" or "ambiguous" outcome override this mock directly.
    resolver_mock = MagicMock(return_value=("full-uuid-123", 0))
    monkeypatch.setattr(
        commands_module, "_resolve_session_or_exit", resolver_mock,
    )

    # Bypass the v0.3.15 transcript preflight by default: these tests use
    # fake/non-existent jsonl_paths to exercise cd-target resolution and the
    # launch mechanics, NOT transcript validity (which has dedicated tests in
    # test_restore.py). Tests that want to exercise the preflight override this.
    preflight_mock = MagicMock(return_value=(True, ""))
    monkeypatch.setattr(
        commands_module, "_transcript_is_resumable", preflight_mock,
    )

    return SimpleNamespace(run=run_mock, resolver=resolver_mock,
                           preflight=preflight_mock)


def _make_args(session_id="abcd1234", **kwargs):
    """Build a fake argparse namespace for cmd_resume."""
    defaults = {"session_id": session_id, "claude_dir": None, "db": None, "quiet": False}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_session(session_id="abcd1234-full-uuid", start_folder="/work/amdead", name="test-session"):
    """Build a fake session row dict (matches what get_session returns)."""
    return {
        "session_id": session_id,
        "start_folder": start_folder,
        "session_name": name,
    }


def test_resume_calls_subprocess_with_target_cwd(monkeypatch, mock_resume_env):
    """The fix: subprocess.run gets cwd=target so claude inherits the right cwd.

    Replaces the prior os.chdir + os.execvp pattern. cwd= is preferred because
    it (a) doesn't mutate the parent's cwd and (b) doesn't trigger Windows'
    broken P_OVERLAY TTY handoff.
    """
    session = _make_session(session_id="full-uuid-123", start_folder="/work/amdead")
    monkeypatch.setattr(commands_module, "get_session", MagicMock(return_value=session))

    rc = cmd_resume(_make_args())

    mock_resume_env.run.assert_called_once_with(
        ["claude", "--resume", "full-uuid-123"],
        cwd="/work/amdead",
        check=False,
    )
    assert rc == 0  # default mock returncode


def test_resume_returncode_propagates(monkeypatch, mock_resume_env):
    """Whatever returncode claude exits with must be the rc that csb returns."""
    session = _make_session(start_folder="/work/amdead")
    monkeypatch.setattr(commands_module, "get_session", MagicMock(return_value=session))
    mock_resume_env.run.return_value = SimpleNamespace(returncode=42)

    rc = cmd_resume(_make_args())

    assert rc == 42


def test_resume_no_target_passes_cwd_none(monkeypatch, mock_resume_env):
    """If session has no start_folder AND no jsonl_path, target is None -> cwd=None."""
    session = _make_session(start_folder=None)
    monkeypatch.setattr(commands_module, "get_session", MagicMock(return_value=session))

    cmd_resume(_make_args())

    mock_resume_env.run.assert_called_once_with(
        ["claude", "--resume", "abcd1234-full-uuid"],
        cwd=None,
        check=False,
    )


def test_resume_filenotfound_for_missing_target_returns_1(monkeypatch, mock_resume_env):
    """FileNotFoundError when the target folder doesn't exist (subprocess.run cwd= check)."""
    session = _make_session(start_folder="/no/such/folder")
    monkeypatch.setattr(commands_module, "get_session", MagicMock(return_value=session))
    monkeypatch.setattr(commands_module.os.path, "isdir", MagicMock(return_value=False))
    mock_resume_env.run.side_effect = FileNotFoundError(
        "[WinError 2] The system cannot find the file specified"
    )

    rc = cmd_resume(_make_args())

    assert rc == 1


def test_resume_filenotfound_for_missing_claude_returns_1(monkeypatch, mock_resume_env, tmp_path):
    """FileNotFoundError when `claude` itself isn't in PATH (target dir exists)."""
    # Use a real tmp_path as the target so os.path.isdir(target) is True.
    session = _make_session(start_folder=str(tmp_path))
    monkeypatch.setattr(commands_module, "get_session", MagicMock(return_value=session))
    mock_resume_env.run.side_effect = FileNotFoundError("claude not in PATH")

    rc = cmd_resume(_make_args())

    assert rc == 1


def test_resume_session_not_found_returns_1_no_subprocess(monkeypatch, mock_resume_env):
    """If the resolver reports no match, return 1 without spawning anything."""
    # Override the default-success resolver mock to simulate no-match.
    mock_resume_env.resolver.return_value = (None, 1)

    rc = cmd_resume(_make_args(session_id="nonexistent"))

    assert rc == 1
    mock_resume_env.run.assert_not_called()


# ── cmd_resume Layer 2: target via pathkit.derive_start_at ───────────
#
# When the session row has a jsonl_path, cmd_resume must derive its cd target
# from pathkit (slug-decoded path) rather than from start_folder. Per the
# upstream-source audit, the slug-decoded path is the only cwd whose slug
# matches the JSONL's parent directory -- and that's the only cwd from which
# `claude --resume <uuid>` will find the file.


def test_resume_layer2_uses_slug_decoded_path_not_start_folder(monkeypatch, mock_resume_env):
    """Layer 2: cwd = derive_start_at(jsonl_path), not session['start_folder']."""
    session = {
        "session_id": "full-uuid-123",
        "session_name": "test",
        "start_folder": "/some/other/path",  # different from slug-decoded
        "jsonl_path": "/fake/jsonl/path.jsonl",
        "folders": [{"folder_path": "/work/amdead", "usage_count": 100}],
    }
    monkeypatch.setattr(commands_module, "get_session", MagicMock(return_value=session))

    # Mock pathkit.derive_start_at to return a known slug-decoded path.
    derive_mock = MagicMock(return_value="/slug/decoded/cwd")
    monkeypatch.setattr("claude_session_backup.pathkit.derive_start_at", derive_mock)

    cmd_resume(_make_args())

    derive_mock.assert_called_once_with(
        "/fake/jsonl/path.jsonl",
        first_cwd="/some/other/path",
        folder_usage={"/work/amdead": 100},
    )
    # Verify subprocess.run cwd= is the slug-decoded path, not start_folder
    mock_resume_env.run.assert_called_once_with(
        ["claude", "--resume", "full-uuid-123"],
        cwd="/slug/decoded/cwd",
        check=False,
    )


def test_resume_layer2_falls_back_to_start_folder_on_unresolved_sentinel(monkeypatch, mock_resume_env):
    """If derive_start_at returns a `<unresolved:slug>` sentinel, fall back to start_folder."""
    session = {
        "session_id": "full-uuid-123",
        "session_name": "test",
        "start_folder": "/work/amdead",
        "jsonl_path": "/fake/jsonl/path.jsonl",
        "folders": [],
    }
    monkeypatch.setattr(commands_module, "get_session", MagicMock(return_value=session))
    monkeypatch.setattr(
        "claude_session_backup.pathkit.derive_start_at",
        MagicMock(return_value="<unresolved:Z--zzzz-deleted>"),
    )

    cmd_resume(_make_args())

    mock_resume_env.run.assert_called_once_with(
        ["claude", "--resume", "full-uuid-123"],
        cwd="/work/amdead",
        check=False,
    )


def test_resume_layer2_no_jsonl_path_uses_start_folder(monkeypatch, mock_resume_env):
    """Legacy session row (pre-#19) with no jsonl_path: skip pathkit, use start_folder."""
    session = {
        "session_id": "full-uuid-123",
        "session_name": "test",
        "start_folder": "/work/amdead",
        # NO jsonl_path
    }
    monkeypatch.setattr(commands_module, "get_session", MagicMock(return_value=session))
    derive_mock = MagicMock()
    monkeypatch.setattr("claude_session_backup.pathkit.derive_start_at", derive_mock)

    cmd_resume(_make_args())

    derive_mock.assert_not_called()
    mock_resume_env.run.assert_called_once_with(
        ["claude", "--resume", "full-uuid-123"],
        cwd="/work/amdead",
        check=False,
    )


# ── cmd_scan: two-positional form (./dirname + term) ─────────────────
#
# Regression for the v0.2.3 checklist case 2a.4: `csb scan ./amdead my-paper`
# must work as `csb scan -d amdead my-paper`. The fix added a second optional
# positional `term2` to the parser; cmd_scan validates that the first is a
# dot-prefix when both are present, and uses the second as the actual term.
#
# These tests exercise cmd_scan's positional handling without invoking the
# downstream SQL/index machinery (which is covered by test_index.py).


def test_scan_rejects_two_positionals_when_first_not_dot_prefix(monkeypatch, capsys):
    """Two positionals where the first is a plain term (not ./...) must be rejected."""
    from claude_session_backup.commands import cmd_scan

    # Stub out everything cmd_scan touches AFTER the two-positional check.
    monkeypatch.setattr(commands_module, "open_db", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(commands_module, "init_schema", MagicMock())
    monkeypatch.setattr(commands_module, "_get_config", MagicMock(return_value={}))
    monkeypatch.setattr(commands_module, "_resolve_top_folders", MagicMock(return_value=3))

    args = SimpleNamespace(
        term="amdead",
        term2="my-paper",
        directories_below=None,
        directory_only=None,
        start_dir_only=None,
        no_usage=False,
        n=20,
        json=False,
        quiet=False,
        claude_dir=None,
        db=None,
    )
    rc = cmd_scan(args)

    assert rc == 2
    captured = capsys.readouterr()
    assert "too many positional arguments" in captured.err.lower()


# ── _format_timestamp: ISO → local + ISO display ─────────────────────

def test_format_timestamp_none_returns_unknown():
    assert _format_timestamp(None) == "(unknown)"
    assert _format_timestamp("") == "(unknown)"


def test_format_timestamp_iso_z_format_retains_original():
    """The original ISO string MUST appear in brackets so users can
    grep the JSONL by exact timestamp."""
    result = _format_timestamp("2026-03-23T18:14:14.520Z")
    assert "[ 2026-03-23T18:14:14.520Z ]" in result
    # Local portion has space, not T, and no Z suffix
    assert "2026-03-23T" not in result.split("[")[0]


def test_format_timestamp_includes_tz_label():
    """Output must include a parenthesized timezone label -- either a
    short name (e.g. EDT) or a numeric offset (e.g. -04:00)."""
    result = _format_timestamp("2026-03-23T18:14:14.520Z")
    # Local-time portion is in form "YYYY-MM-DD HH:MM:SS (tz)"
    local_part = result.split("[")[0].strip()
    assert "(" in local_part and ")" in local_part


def test_format_timestamp_unparseable_falls_back_to_input():
    """Defensive: never throw on bad input -- return as-is."""
    assert _format_timestamp("not-a-timestamp") == "not-a-timestamp"


# ── cmd_check: un-backed-up session detection (v0.3.9) ──────────────
#
# `csb check` compares each session's live JSONL mtime against the mtime
# recorded in the index at the last backup. Newer (or never indexed) -> a
# gap (exit CHECK_GAP_EXIT). The SessionStart hook uses this to decide
# whether to warn + recover.

from claude_session_backup.commands import cmd_check, CHECK_GAP_EXIT
from claude_session_backup.index import open_db, init_schema, upsert_session
from claude_session_backup.scanner import scan_projects
from claude_session_backup.metadata import extract_metadata

_CONFTEST_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _check_args(claude_dir, db, exclude=None, quiet=True):
    return SimpleNamespace(
        claude_dir=str(claude_dir), db=str(db), quiet=quiet, exclude=exclude,
    )


def _index_sessions_at_live_mtime(claude_dir, db):
    """Index every scanned session at its CURRENT live mtime -> 'backed up'."""
    conn = open_db(str(db))
    init_schema(conn)
    for sf in scan_projects(claude_dir):
        meta = extract_metadata(sf.jsonl_path)
        meta.project = sf.project
        # .as_posix() matches the production write seam (commands.py upsert):
        # the DB contract is forward-slash POSIX -- str() emitted backslashes
        # on Windows, the drift that taught readers backslash tolerance (#46).
        rel = sf.jsonl_path.relative_to(claude_dir).as_posix()
        upsert_session(conn, meta, rel, sf.jsonl_size, sf.jsonl_mtime, "2026-01-01T00:00:00Z")
    conn.commit()
    conn.close()


def test_check_gap_when_session_unindexed(mock_claude_dir, tmp_path, capsys):
    """A session present on disk but absent from the index is a gap."""
    db = tmp_path / "check.db"
    rc = cmd_check(_check_args(mock_claude_dir, db))
    assert rc == CHECK_GAP_EXIT
    assert "un-backed-up" in capsys.readouterr().out


def test_check_clean_when_indexed_at_live_mtime(mock_claude_dir, tmp_path):
    """Once the session is indexed at its live mtime, check reports clean."""
    db = tmp_path / "check.db"
    _index_sessions_at_live_mtime(mock_claude_dir, db)
    rc = cmd_check(_check_args(mock_claude_dir, db))
    assert rc == 0


def test_check_exclude_skips_session(mock_claude_dir, tmp_path):
    """--exclude <sid> drops the (only) session -> clean even though unindexed."""
    db = tmp_path / "check.db"
    rc = cmd_check(_check_args(mock_claude_dir, db, exclude=[_CONFTEST_SID]))
    assert rc == 0


def test_check_not_git_repo_runs_detection_anyway(monkeypatch, tmp_path, capsys):
    """#52: a non-git claude dir is no longer a hard error -- gap detection
    is DB-vs-disk only, so check proceeds in index-only mode. An empty
    projects dir -> clean (rc 0) with the index-only caveat. (is_git_repo
    is mocked False -- on a dev box the temp dir can sit inside the home
    git repo, so a tmp path isn't reliably repo-free.)"""
    monkeypatch.setattr(commands_module, "is_git_repo", MagicMock(return_value=False))
    plain = tmp_path / "not-claude"
    (plain / "projects").mkdir(parents=True)
    db = tmp_path / "check.db"
    rc = cmd_check(_check_args(plain, db, quiet=False))
    assert rc == 0
    assert "index-only" in capsys.readouterr().out


# ── csb status: un-backed-up surfacing (v0.3.9) ────────────────────

from claude_session_backup.commands import cmd_status


def test_status_unbacked_none_when_indexed(mock_claude_dir, tmp_path, capsys):
    """Index the session at its live mtime -> status reports 'none'."""
    db = tmp_path / "status.db"
    _index_sessions_at_live_mtime(mock_claude_dir, db)
    cmd_status(SimpleNamespace(claude_dir=str(mock_claude_dir), db=str(db)))
    assert "Un-backed-up:   none" in capsys.readouterr().out


def test_status_unbacked_lists_gap_with_id(mock_claude_dir, tmp_path, capsys):
    """A fresh index -> the on-disk session shows as un-backed-up, by short id."""
    db = tmp_path / "status.db"
    cmd_status(SimpleNamespace(claude_dir=str(mock_claude_dir), db=str(db)))
    out = capsys.readouterr().out
    assert "Un-backed-up:   1 session " in out
    assert "never indexed" in out
    assert _CONFTEST_SID[:8] in out


def test_status_unbacked_respects_config_limit(mock_claude_dir, tmp_path, capsys):
    """status_unbacked_limit caps the list; the rest collapse to '+ N more'."""
    import json as _json
    (mock_claude_dir / "session-backup-config.json").write_text(
        _json.dumps({"status_unbacked_limit": 0}), encoding="utf-8",
    )
    db = tmp_path / "status.db"
    cmd_status(SimpleNamespace(claude_dir=str(mock_claude_dir), db=str(db)))
    out = capsys.readouterr().out
    assert "+ 1 more not shown" in out
    assert _CONFTEST_SID[:8] not in out  # nothing listed at limit 0


# ── v0.5.1: cmd_search dir-scope wiring (guards removed, source-agnostic) ──
#
# These mock the heavy deps (config/DB/search/render) and assert cmd_search's
# WIRING: multi-term + -d no longer errors, -d accepts any --source, the
# resolved source_override drives dispatch (no forced fts5 pin), the dir_scope
# dict carries the folder_usage SQL criteria, and --min-strength degrades to a
# note (not a failure) under a non-fts5 source.


def _make_search_args(**kwargs):
    """Fake argparse namespace for cmd_search (search()/render() are mocked)."""
    defaults = dict(
        query=["needle"], match="all", source="auto",
        before=0, after=0, context=None, regex=False, case_sensitive=False,
        directories_below=None, directory_only=None, min_strength=1,
        session_id=None, only=None, limit=20, full_info=0,
        sort="last-used", shortid=False, json=False, no_color=True,
        full_match=False, quiet=True, deleted=None, all=False,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture
def mock_search_env(monkeypatch, tmp_path):
    """Patch cmd_search's deps; return the search() mock (empty hits -> rc 0)."""
    import claude_session_backup.search as search_module
    search_mock = MagicMock(return_value=[])
    # cmd_search does `from .search import search as run_search` at call time,
    # so patching the module attribute is picked up by the inline import.
    monkeypatch.setattr(search_module, "search", search_mock)
    monkeypatch.setattr(
        commands_module, "_get_config",
        lambda args: {"index_path": str(tmp_path / "i.db"), "claude_dir": tmp_path},
    )
    monkeypatch.setattr(commands_module, "open_db", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(commands_module, "init_schema", MagicMock())
    monkeypatch.setattr(commands_module, "read_cleanup_period", MagicMock(return_value=30))
    return search_mock


def test_cmd_search_multiterm_dir_scope_no_longer_errors(mock_search_env):
    """The user's bug: multiple terms + -d used to return 2. Now it runs, and
    search() receives every term plus a source-agnostic dir_scope."""
    args = _make_search_args(
        query=["SC:N", "SI:N", "SA:N"], match="all",
        directories_below=".", source="auto",
    )
    rc = cmd_search(args)
    assert rc == 0  # not 2 (multi-term guard removed)
    _, kwargs = mock_search_env.call_args
    assert kwargs["extra_terms"] == ("SI:N", "SA:N")
    assert kwargs["match_mode"] == "all"
    ds = kwargs["dir_scope"]
    assert ds is not None
    assert ds["exact_value"] is not None      # folder_usage SQL criteria present
    assert "like_match" in ds and "like_exclude" in ds
    # Default (auto) is NOT force-pinned to fts5 anymore -> folder_usage path.
    assert kwargs["source_override"] is None


def test_cmd_search_dir_scope_accepts_non_fts5_source(mock_search_env):
    """`-d` with `--source jsonl` used to return 2 (incompatible). Now allowed;
    the resolved source flows through untouched."""
    args = _make_search_args(
        query=["needle"], directories_below=".", source="jsonl",
    )
    rc = cmd_search(args)
    assert rc == 0
    _, kwargs = mock_search_env.call_args
    assert kwargs["source_override"] == "jsonl"
    assert kwargs["dir_scope"]["exact_value"] is not None


def test_cmd_search_dir_scope_explicit_fts5_keeps_source(mock_search_env):
    """Explicit `--source fts5 -d` still routes to the strength path (dispatch
    keys on source_override == 'fts5' inside search())."""
    args = _make_search_args(
        query=["needle"], directories_below=".", source="fts5",
    )
    rc = cmd_search(args)
    assert rc == 0
    _, kwargs = mock_search_env.call_args
    assert kwargs["source_override"] == "fts5"


def test_cmd_search_directory_only_sets_exclude(mock_search_env):
    """`-D` (folder-only) builds a like_exclude so descendants are dropped;
    `-d` does not."""
    d_args = _make_search_args(query=["needle"], directory_only=".", source="auto")
    cmd_search(d_args)
    _, d_kwargs = mock_search_env.call_args
    assert d_kwargs["dir_scope"]["include_descendants"] is False


def test_cmd_search_min_strength_note_for_non_fts5(mock_search_env, capsys):
    """`--min-strength` raised under a non-fts5 source prints a note (no fail)."""
    args = _make_search_args(
        query=["needle"], directories_below=".", source="auto", min_strength=2,
    )
    rc = cmd_search(args)
    assert rc == 0
    err = capsys.readouterr().err
    assert "--min-strength applies only to --source fts5" in err


def test_cmd_search_no_min_strength_note_for_fts5(mock_search_env, capsys):
    """No note when `--min-strength` is used with `--source fts5` (it applies)."""
    args = _make_search_args(
        query=["needle"], directories_below=".", source="fts5", min_strength=2,
    )
    cmd_search(args)
    err = capsys.readouterr().err
    assert "--min-strength applies only" not in err


def test_cmd_search_no_min_strength_note_at_default(mock_search_env, capsys):
    """Default --min-strength (1) never notes, even under a non-fts5 source."""
    args = _make_search_args(
        query=["needle"], directories_below=".", source="auto", min_strength=1,
    )
    cmd_search(args)
    err = capsys.readouterr().err
    assert "--min-strength applies only" not in err


# ── #52: repo-less index-only mode + empty-state diagnosis ─────────────
#
# The crash-recovery scenario: ~/.claude with sessions on disk but no git
# repo. `csb backup --no-commit` must index anyway (the read stack depends
# on it), bare `csb backup` must keep failing loudly (a backup that cannot
# commit protects nothing), and empty `list`/`scan` results must diagnose
# an unbuilt index instead of dead-ending. is_git_repo is mocked in the
# repo-less tests -- on a dev box tmp_path can sit inside an ancestor repo.

from claude_session_backup.commands import (
    cmd_backup,
    cmd_list,
    _empty_state_guidance,
)
from claude_session_backup.index import index_is_unbuilt
from claude_session_backup.scanner import count_session_jsonls


def _backup_args(claude_dir, db, no_commit=False, quiet=False):
    return SimpleNamespace(
        claude_dir=str(claude_dir), db=str(db),
        no_commit=no_commit, quiet=quiet,
    )


def _list_args(claude_dir, db, **kw):
    defaults = dict(
        claude_dir=str(claude_dir), db=str(db),
        n=20, json=False, filter=None, sort="last-used",
        deleted=None, all=False, top=None, all_folders=False,
        shortid=False, quiet=False,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _mock_repoless(monkeypatch):
    monkeypatch.setattr(commands_module, "is_git_repo", MagicMock(return_value=False))


def test_backup_no_commit_repoless_indexes(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """AC1: --no-commit without a git repo indexes the sessions (rc 0) and
    warns [index-only] instead of dying at the git precondition."""
    _mock_repoless(monkeypatch)
    db = tmp_path / "idx.db"
    rc = cmd_backup(_backup_args(mock_claude_dir_repoless, db, no_commit=True))
    captured = capsys.readouterr()
    assert rc == 0
    assert "[index-only]" in captured.err
    assert "NO backup protection" in captured.err
    conn = open_db(str(db))
    init_schema(conn)
    assert not index_is_unbuilt(conn)
    conn.close()


def test_backup_bare_repoless_still_errors_and_mentions_no_commit(
    monkeypatch, mock_claude_dir_repoless, tmp_path, capsys,
):
    """AC5: bare `csb backup` without a repo keeps the hard error, and the
    error now names the --no-commit escape hatch. Nothing gets indexed."""
    _mock_repoless(monkeypatch)
    db = tmp_path / "idx.db"
    rc = cmd_backup(_backup_args(mock_claude_dir_repoless, db, no_commit=False))
    captured = capsys.readouterr()
    assert rc == 1
    assert "not a git repository" in captured.err
    assert "--no-commit" in captured.err
    conn = open_db(str(db))
    init_schema(conn)
    assert index_is_unbuilt(conn)
    conn.close()


def test_backup_no_commit_repoless_quiet_still_warns(
    monkeypatch, mock_claude_dir_repoless, tmp_path, capsys,
):
    """The [index-only] warning survives --quiet -- hook runs log stderr,
    and 'this was NOT a backup' must never be silent."""
    _mock_repoless(monkeypatch)
    db = tmp_path / "idx.db"
    rc = cmd_backup(_backup_args(mock_claude_dir_repoless, db, no_commit=True, quiet=True))
    assert rc == 0
    assert "[index-only]" in capsys.readouterr().err


def test_list_empty_index_with_files_hints_backup(
    monkeypatch, mock_claude_dir_repoless, tmp_path, capsys,
):
    """AC3: empty index + transcripts on disk -> `csb list` explains the
    index was never built and names both backup forms."""
    db = tmp_path / "idx.db"
    rc = cmd_list(_list_args(mock_claude_dir_repoless, db))
    captured = capsys.readouterr()
    assert rc == 0
    assert "No sessions found." in captured.out
    assert "index is empty" in captured.out
    assert "csb backup --no-commit" in captured.out


def test_list_empty_index_no_files_no_hint(tmp_path, capsys):
    """AC3: empty index + genuinely no session files -> classic empty state,
    no misleading hint."""
    claude = tmp_path / ".claude"
    (claude / "projects").mkdir(parents=True)
    db = tmp_path / "idx.db"
    rc = cmd_list(_list_args(claude, db))
    captured = capsys.readouterr()
    assert rc == 0
    assert "No sessions found." in captured.out
    assert "index is empty" not in captured.out


def test_list_json_empty_index_no_hint(mock_claude_dir_repoless, tmp_path, capsys):
    """AC7: --json output stays machine-readable -- no hint text."""
    db = tmp_path / "idx.db"
    rc = cmd_list(_list_args(mock_claude_dir_repoless, db, json=True))
    captured = capsys.readouterr()
    assert rc == 0
    assert "index is empty" not in captured.out
    import json as _json
    assert _json.loads(captured.out) == []


def test_guidance_unbuilt_index_with_files(mock_claude_dir_repoless, tmp_path):
    config = {
        "claude_dir": str(mock_claude_dir_repoless),
        "index_path": str(tmp_path / "idx.db"),
    }
    lines = _empty_state_guidance(config)
    joined = "\n".join(lines)
    assert "index is empty" in joined
    assert "csb backup --no-commit" in joined


def test_guidance_built_index_path_mode_redirects(mock_claude_dir, tmp_path):
    """AC4: index built, path-scoped scan found nothing, sessions exist
    elsewhere -> redirection tip (the 'scanned my checkout, session ran
    from another cwd' mental-model gap)."""
    db = tmp_path / "idx.db"
    _index_sessions_at_live_mtime(mock_claude_dir, db)
    config = {"claude_dir": str(mock_claude_dir), "index_path": str(db)}
    lines = _empty_state_guidance(config, path_mode=True)
    joined = "\n".join(lines)
    assert "under other folders" in joined
    assert "csb list -n 5" in joined


def test_guidance_built_index_non_path_mode_silent(mock_claude_dir, tmp_path):
    """A filtered `csb list` miss on a healthy index gets no guidance --
    the redirection tip is path-scan-only."""
    db = tmp_path / "idx.db"
    _index_sessions_at_live_mtime(mock_claude_dir, db)
    config = {"claude_dir": str(mock_claude_dir), "index_path": str(db)}
    assert _empty_state_guidance(config, path_mode=False) == []


def test_count_session_jsonls(mock_claude_dir_repoless, tmp_path):
    assert count_session_jsonls(str(mock_claude_dir_repoless)) == 1
    empty = tmp_path / "empty-claude"
    (empty / "projects").mkdir(parents=True)
    assert count_session_jsonls(str(empty)) == 0
    assert count_session_jsonls(str(tmp_path / "nonexistent")) == 0


def test_check_repoless_gap_mentions_no_commit(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """Repo-less check runs gap detection anyway (the hook's systemMessage
    path depends on it) and recommends the git-free indexing command."""
    _mock_repoless(monkeypatch)
    db = tmp_path / "check.db"
    rc = cmd_check(_check_args(mock_claude_dir_repoless, db))
    captured = capsys.readouterr()
    assert rc == CHECK_GAP_EXIT
    assert "not a git repository" in captured.out
    assert "csb backup --no-commit" in captured.out


def test_check_repoless_clean_when_indexed(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """Repo-less + fully indexed -> rc 0, message flags index-only mode."""
    _mock_repoless(monkeypatch)
    db = tmp_path / "check.db"
    _index_sessions_at_live_mtime(mock_claude_dir_repoless, db)
    rc = cmd_check(_check_args(mock_claude_dir_repoless, db, quiet=False))
    assert rc == 0
    assert "index-only" in capsys.readouterr().out


def test_status_repoless_shows_advice(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """`csb status` pairs `Git repo: NO` with the way out."""
    _mock_repoless(monkeypatch)
    db = tmp_path / "status.db"
    cmd_status(SimpleNamespace(claude_dir=str(mock_claude_dir_repoless), db=str(db)))
    out = capsys.readouterr().out
    assert "Git repo:      NO" in out
    assert "backups disabled" in out
    assert "csb backup --no-commit" in out


# ── v0.6.0 (#52): cmd_setup -- guided onboarding ───────────────────────
#
# is_git_repo is patched to a plain `.git`-exists check: deterministic on
# dev boxes whose tmp dir may sit inside an ancestor repo, while still
# flipping to True after cmd_setup's real `git init` runs.

from claude_session_backup.commands import cmd_setup


def _setup_args(claude_dir, db, **kw):
    defaults = dict(
        claude_dir=str(claude_dir), db=str(db),
        auto=False, index_only=False, quiet=False,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _patch_dotgit_check(monkeypatch):
    monkeypatch.setattr(
        commands_module, "is_git_repo",
        lambda d: (Path(d) / ".git").exists(),
    )


def _read_cfg(claude_dir):
    import json as _json
    p = Path(claude_dir) / "session-backup-config.json"
    return _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def test_setup_auto_initializes_and_backs_up(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """AC-S2: --auto on a repo-less dir -> git repo created, gitattributes
    written, first backup committed, exit 0, zero prompts."""
    _patch_dotgit_check(monkeypatch)
    db = tmp_path / "setup.db"
    rc = cmd_setup(_setup_args(mock_claude_dir_repoless, db, auto=True))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert (mock_claude_dir_repoless / ".git").exists()
    assert (mock_claude_dir_repoless / ".gitattributes").exists()
    assert "Initialized git repository" in out
    assert "First backup complete" in out
    conn = open_db(str(db))
    init_schema(conn)
    assert not index_is_unbuilt(conn)   # the first backup indexed the session
    conn.close()


def test_setup_interactive_accept_defaults(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """AC-S1: pressing Enter at both prompts (defaults) initializes and
    backs up."""
    _patch_dotgit_check(monkeypatch)
    monkeypatch.setattr(commands_module, "_interactive", lambda: True)
    answers = iter(["", ""])   # init? -> default Y; first backup? -> default Y
    monkeypatch.setattr(commands_module, "_ask", lambda q: next(answers))
    rc = cmd_setup(_setup_args(mock_claude_dir_repoless, tmp_path / "s.db"))
    assert rc == 0
    assert (mock_claude_dir_repoless / ".git").exists()


def test_setup_interactive_decline_leaves_untouched(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    _patch_dotgit_check(monkeypatch)
    monkeypatch.setattr(commands_module, "_interactive", lambda: True)
    monkeypatch.setattr(commands_module, "_ask", lambda q: "n")
    rc = cmd_setup(_setup_args(mock_claude_dir_repoless, tmp_path / "s.db"))
    out = capsys.readouterr().out
    assert rc == 2
    assert not (mock_claude_dir_repoless / ".git").exists()
    assert "--index-only" in out   # the decline path names the official opt-out


def test_setup_interactive_eof_aborts_clean(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    _patch_dotgit_check(monkeypatch)
    monkeypatch.setattr(commands_module, "_interactive", lambda: True)
    monkeypatch.setattr(commands_module, "_ask", lambda q: None)   # EOF/Ctrl-C
    rc = cmd_setup(_setup_args(mock_claude_dir_repoless, tmp_path / "s.db"))
    assert rc == 2
    assert not (mock_claude_dir_repoless / ".git").exists()


def test_setup_noninteractive_without_flags_exits_2(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """AC-S6: piped/CI `csb setup` with no mode flag -> exit 2 with
    instructions, no mutation, no hang."""
    _patch_dotgit_check(monkeypatch)
    monkeypatch.setattr(commands_module, "_interactive", lambda: False)
    rc = cmd_setup(_setup_args(mock_claude_dir_repoless, tmp_path / "s.db"))
    err = capsys.readouterr().err
    assert rc == 2
    assert "--auto" in err and "--index-only" in err
    assert not (mock_claude_dir_repoless / ".git").exists()


def test_setup_index_only_flag_records_signoff(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """AC-S3: the flag is the consent in non-TTY runs; the sign-off (mode +
    timestamp) lands in the config file and the output states the cost."""
    _patch_dotgit_check(monkeypatch)
    monkeypatch.setattr(commands_module, "_interactive", lambda: False)
    rc = cmd_setup(_setup_args(mock_claude_dir_repoless, tmp_path / "s.db", index_only=True))
    out = capsys.readouterr().out
    assert rc == 0
    cfg = _read_cfg(mock_claude_dir_repoless)
    assert cfg["backup_mode"] == "index-only"
    assert cfg["index_only_ack_at"]
    assert "NOTHING IS BACKED UP" in out


def test_setup_index_only_interactive_requires_typed_confirm(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    _patch_dotgit_check(monkeypatch)
    monkeypatch.setattr(commands_module, "_interactive", lambda: True)

    # Wrong confirmation -> nothing recorded.
    monkeypatch.setattr(commands_module, "_ask", lambda q: "yes")
    rc = cmd_setup(_setup_args(mock_claude_dir_repoless, tmp_path / "s.db", index_only=True))
    assert rc == 2
    assert _read_cfg(mock_claude_dir_repoless).get("backup_mode") is None

    # Typing the mode name back -> recorded.
    monkeypatch.setattr(commands_module, "_ask", lambda q: "index-only")
    rc = cmd_setup(_setup_args(mock_claude_dir_repoless, tmp_path / "s.db", index_only=True))
    assert rc == 0
    assert _read_cfg(mock_claude_dir_repoless)["backup_mode"] == "index-only"


def test_setup_with_repo_reports_protected_and_clears_stale_ack(monkeypatch, mock_claude_dir, tmp_path, capsys):
    """AC-S5-adjacent: repo already present -> 'nothing to set up'; a stale
    index-only sign-off is cleared loudly so config matches reality."""
    import json as _json
    (mock_claude_dir / "session-backup-config.json").write_text(
        _json.dumps({"backup_mode": "index-only", "index_only_ack_at": "2026-01-01"}),
        encoding="utf-8",
    )
    rc = cmd_setup(_setup_args(mock_claude_dir, tmp_path / "s.db"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing to set up" in out
    assert "Cleared the old index-only sign-off" in out
    assert _read_cfg(mock_claude_dir)["backup_mode"] == "full"


def test_setup_index_only_with_repo_refuses_downgrade(mock_claude_dir, tmp_path, capsys):
    rc = cmd_setup(_setup_args(mock_claude_dir, tmp_path / "s.db", index_only=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "downgrade" in out
    assert _read_cfg(mock_claude_dir).get("backup_mode") is None  # nothing recorded


def test_setup_missing_dir_errors(tmp_path, capsys):
    rc = cmd_setup(_setup_args(tmp_path / "nope", tmp_path / "s.db"))
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err


# ── v0.6.0: setup closing checklist is state-aware ([x]/[ ]) ───────────
#
# `csb setup` must never instruct a user to run something already done:
# each checklist item probes real state (git repo, last scan in the DB,
# Claude Code's plugin registries) and shows the command only when the
# item is actually missing.


def _write_plugin_registries(claude_dir, marketplace=True, plugin=True):
    import json as _json
    base = Path(claude_dir) / "plugins"
    base.mkdir(parents=True, exist_ok=True)
    if marketplace:
        (base / "known_marketplaces.json").write_text(_json.dumps({
            "dazzle-claude-session-backup": {"source": {"repo": "DazzleML/Claude-Session-Backup"}},
        }), encoding="utf-8")
    if plugin:
        (base / "installed_plugins.json").write_text(_json.dumps({
            "version": 2,
            "plugins": {
                "claude-session-backup@dazzle-claude-session-backup": [
                    {"scope": "user", "version": "0.6.0"},
                ],
            },
        }), encoding="utf-8")


def test_setup_checklist_all_done_shows_no_commands(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """Everything configured -> three [x] rows and NO `claude plugin` /
    `csb backup` instructions (the user's complaint: don't tell people to
    run what's already done)."""
    _patch_dotgit_check(monkeypatch)
    _write_plugin_registries(mock_claude_dir_repoless)
    db = tmp_path / "s.db"
    rc = cmd_setup(_setup_args(mock_claude_dir_repoless, db, auto=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("[x]") == 3
    assert "[ ]" not in out
    assert "claude plugin marketplace add" not in out
    assert "claude plugin install" not in out


def test_setup_checklist_missing_plugin_shows_only_missing_commands(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """Marketplace added but plugin not installed -> [ ] plugin row with
    ONLY the install command (no marketplace-add re-instruction)."""
    _patch_dotgit_check(monkeypatch)
    _write_plugin_registries(mock_claude_dir_repoless, marketplace=True, plugin=False)
    rc = cmd_setup(_setup_args(mock_claude_dir_repoless, tmp_path / "s.db", auto=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "[ ] auto-backup plugin" in out
    assert "claude plugin install" in out
    assert "claude plugin marketplace add" not in out
    assert "[x] first backup" in out           # --auto ran it


def test_setup_checklist_backup_declined_left_unchecked(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """Init accepted, first backup declined -> backup row is [ ] with the
    csb backup pointer."""
    _patch_dotgit_check(monkeypatch)
    monkeypatch.setattr(commands_module, "_interactive", lambda: True)
    answers = iter(["", "n"])   # init? -> Y (default); first backup? -> no
    monkeypatch.setattr(commands_module, "_ask", lambda q: next(answers))
    rc = cmd_setup(_setup_args(mock_claude_dir_repoless, tmp_path / "s.db"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "[x] git backup store" in out
    assert "[ ] first backup" in out
    assert "-> csb backup" in out


# ── v0.6.0: setup header names the claude-dir source (relocated setups) ─


def test_claude_dir_source_precedence(monkeypatch, tmp_path):
    """Flag > CLAUDE_DIR > CLAUDE_CONFIG_DIR > default -- mirrors
    load_config so docker/VM relocations (#45) see which mechanism won."""
    from claude_session_backup.commands import _claude_dir_source
    monkeypatch.delenv("CLAUDE_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # Keep the config-file probe away from the real ~/.claude config.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    flag = SimpleNamespace(claude_dir="C:/x")
    noflag = SimpleNamespace(claude_dir=None)

    assert _claude_dir_source(flag) == "--claude-dir"
    assert _claude_dir_source(noflag) == "default"

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "C:/relocated")
    assert _claude_dir_source(noflag) == "CLAUDE_CONFIG_DIR env"

    monkeypatch.setenv("CLAUDE_DIR", "C:/more-specific")
    assert _claude_dir_source(noflag) == "CLAUDE_DIR env"

    assert _claude_dir_source(flag) == "--claude-dir"  # flag still wins


def test_setup_header_shows_source(monkeypatch, mock_claude_dir, tmp_path, capsys):
    rc = cmd_setup(_setup_args(mock_claude_dir, tmp_path / "s.db"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "(via --claude-dir)" in out


# ── v0.6.0 checklist FV.2c fix: csb scan --json ────────────────────────
#
# The render path always supported json output, but the scan parser never
# registered the flag (found by the tester run). Pin: flag parses, stdout
# is PURE json in both empty and non-empty cases (no "Scanning for..."
# preamble, no empty-state prose).


def _cli(argv):
    from claude_session_backup.cli import main as cli_main
    return cli_main(argv)


def test_scan_json_empty_is_pure_json(monkeypatch, mock_claude_dir, tmp_path, capsys):
    import json as _json
    scope = tmp_path / "nothing-here"
    scope.mkdir()
    rc = _cli(["scan", "-d", str(scope), "--json",
               "--claude-dir", str(mock_claude_dir), "--db", str(tmp_path / "s.db")])
    captured = capsys.readouterr()
    assert rc == 0
    assert _json.loads(captured.out) == []
    assert "Scanning for" not in captured.out


def test_scan_json_results_are_pure_json(tmp_path, capsys):
    import json as _json
    # A session whose cwd is a REAL local dir, so the live filesystem scan
    # matches it (the conftest fixture's POSIX cwds don't resolve on
    # Windows).
    from claude_session_backup.scanner import sanitize_path
    work = tmp_path / "work"
    work.mkdir()
    claude = tmp_path / ".claude"
    proj = claude / "projects" / sanitize_path(str(work))
    proj.mkdir(parents=True)
    sid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    (proj / f"{sid}.jsonl").write_text(
        _json.dumps({"type": "user", "timestamp": "2026-07-18T10:00:00Z",
                     "cwd": str(work), "sessionId": sid, "uuid": "u1"}) + "\n",
        encoding="utf-8",
    )
    rc = _cli(["scan", "-d", str(work), "--json",
               "--claude-dir", str(claude), "--db", str(tmp_path / "s.db")])
    captured = capsys.readouterr()
    assert rc == 0
    data = _json.loads(captured.out)
    assert isinstance(data, list) and len(data) == 1
    assert data[0]["session_id"] == sid
    assert "Scanning for" not in captured.out

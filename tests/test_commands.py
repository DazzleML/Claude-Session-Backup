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


def test_check_not_git_repo_returns_1(monkeypatch, tmp_path):
    """A non-git claude dir is an error (rc 1), not a gap. (is_git_repo is
    mocked False -- on a dev box the temp dir can sit inside the home git
    repo, so a tmp path isn't reliably repo-free.)"""
    monkeypatch.setattr(commands_module, "is_git_repo", MagicMock(return_value=False))
    plain = tmp_path / "not-claude"
    (plain / "projects").mkdir(parents=True)
    db = tmp_path / "check.db"
    rc = cmd_check(_check_args(plain, db))
    assert rc == 1


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

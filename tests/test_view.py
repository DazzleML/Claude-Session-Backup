"""Tests for `csb view` (#14): viewer discovery, multi-modal session
resolution, and the cmd_view launch/fallback/pruned flows.

The viewer itself (CCHV, a Tauri GUI) is never launched here -- launch is
mocked; the human test checklist covers the real GUI."""

import argparse
import os
from pathlib import Path

import pytest

from claude_session_backup.commands import (
    _find_viewer,
    _launch_viewer,
    _resolve_session_query,
    cmd_view,
)
from claude_session_backup.index import init_schema, open_db, upsert_session
from claude_session_backup.metadata import SessionMetadata


UUID_A = "aaaa0001-1111-2222-3333-444444444444"
UUID_B = "bbbb0002-1111-2222-3333-444444444444"


def _make_view_env(tmp_path, with_jsonl=True):
    """A throwaway claude-dir + DB with two indexed sessions (no git needed)."""
    claude = tmp_path / "claude"
    slug = "C--code-viewproj"
    jsonl_rel_a = f"projects/{slug}/{UUID_A}.jsonl"
    if with_jsonl:
        full = claude / jsonl_rel_a
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b'{"type":"user","timestamp":"2026-05-01T10:00:00Z"}\n')
    db = tmp_path / "view.db"
    conn = open_db(db)
    init_schema(conn)
    upsert_session(
        conn,
        SessionMetadata(
            session_id=UUID_A, session_name="alpha-build-session",
            project=slug, start_folder="C:\\code\\viewproj",
            folder_usage={str(tmp_path / "workdir"): 5},
        ),
        jsonl_path=jsonl_rel_a, jsonl_size=10, jsonl_mtime=1700000000.0,
        scanned_at="2026-05-01T10:00:00Z",
    )
    upsert_session(
        conn,
        SessionMetadata(
            session_id=UUID_B, session_name="beta-debug-session",
            project=slug, start_folder="C:\\code\\other",
        ),
        jsonl_path=f"projects/{slug}/{UUID_B}.jsonl",
        jsonl_size=10, jsonl_mtime=1700000001.0,
        scanned_at="2026-05-01T10:00:00Z",
    )
    return claude, db, conn


def _view_args(tmp_path, db, claude, **kw):
    defaults = dict(
        query=None, claude_dir=str(claude), db=str(db), quiet=True,
        restore_pruned=False, no_restore_pruned=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ── _find_viewer ────────────────────────────────────────────────────────


@pytest.fixture
def isolated_viewer_env(monkeypatch, tmp_path):
    """Make platform auto-discovery come up empty deterministically.

    Redirects every env var the Windows candidate list reads (a real
    CCHV install in Program Files would otherwise leak into tests)."""
    monkeypatch.delenv("CLAUDEVIEW_BIN", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-lad"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "empty-pf"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "empty-pf86"))
    return tmp_path


def test_find_viewer_env_var_wins(isolated_viewer_env, monkeypatch, tmp_path):
    exe = tmp_path / "viewer.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("CLAUDEVIEW_BIN", str(exe))
    found = _find_viewer({"viewer_path": None})
    assert found == {"mode": "binary", "path": str(exe)}


def test_find_viewer_config_key_binary(isolated_viewer_env, tmp_path):
    exe = tmp_path / "cfg-viewer.exe"
    exe.write_bytes(b"MZ")
    found = _find_viewer({"viewer_path": str(exe)})
    assert found == {"mode": "binary", "path": str(exe)}


def test_find_viewer_config_key_dev_dir(isolated_viewer_env, tmp_path):
    dev = tmp_path / "cchv-dev"
    (dev / "src-tauri").mkdir(parents=True)
    (dev / "package.json").write_text("{}")
    found = _find_viewer({"viewer_path": str(dev)})
    assert found == {"mode": "dev", "path": str(dev)}


def test_find_viewer_none_when_nothing_installed(isolated_viewer_env):
    assert _find_viewer({"viewer_path": None}) is None


def test_find_viewer_bad_env_falls_through_to_config(
    isolated_viewer_env, monkeypatch, tmp_path
):
    monkeypatch.setenv("CLAUDEVIEW_BIN", str(tmp_path / "nonexistent.exe"))
    exe = tmp_path / "real.exe"
    exe.write_bytes(b"MZ")
    found = _find_viewer({"viewer_path": str(exe)})
    assert found == {"mode": "binary", "path": str(exe)}


@pytest.mark.skipif(os.name != "nt", reason="Windows install-location test")
def test_find_viewer_program_files_standard_install(
    isolated_viewer_env, monkeypatch, tmp_path
):
    """The standard CCHV installer target (Program Files\\CCHistoryViewer)
    is auto-discovered -- the average user's zero-config path."""
    pf = tmp_path / "pf"
    exe = pf / "CCHistoryViewer" / "claude-code-history-viewer.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("ProgramFiles", str(pf))
    found = _find_viewer({"viewer_path": None})
    assert found == {"mode": "binary", "path": str(exe)}


# ── _resolve_session_query ─────────────────────────────────────────────────


def test_resolve_uuid_prefix(tmp_path):
    claude, db, conn = _make_view_env(tmp_path)
    session, method = _resolve_session_query("aaaa0001", conn, str(claude))
    assert method == "uuid"
    assert session["session_id"] == UUID_A
    conn.close()


def test_resolve_absolute_jsonl_path(tmp_path):
    claude, db, conn = _make_view_env(tmp_path)
    p = claude / f"projects/C--code-viewproj/{UUID_A}.jsonl"
    session, method = _resolve_session_query(str(p), conn, str(claude))
    assert method == "path"
    assert session["session_id"] == UUID_A
    conn.close()


def test_resolve_sesslog_folder_name(tmp_path):
    claude, db, conn = _make_view_env(tmp_path)
    name = f"VIEWPROJ__some-title__{UUID_A}_Extreme"
    session, method = _resolve_session_query(name, conn, str(claude))
    assert method == "sesslog-name"
    assert session["session_id"] == UUID_A
    conn.close()


def test_resolve_free_text_unique(tmp_path):
    claude, db, conn = _make_view_env(tmp_path)
    session, method = _resolve_session_query("alpha-build", conn, str(claude))
    assert method == "search"
    assert session["session_id"] == UUID_A
    conn.close()


def test_resolve_free_text_multi_returns_candidates(tmp_path):
    claude, db, conn = _make_view_env(tmp_path)
    result, method = _resolve_session_query("session", conn, str(claude))
    assert isinstance(result, list) and len(result) == 2
    assert method == "candidates:search"
    conn.close()


def test_resolve_directory_by_folder_usage(tmp_path):
    claude, db, conn = _make_view_env(tmp_path)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    session, method = _resolve_session_query(str(workdir), conn, str(claude))
    assert method == "folder"
    assert session["session_id"] == UUID_A
    conn.close()


def test_resolve_no_match(tmp_path):
    claude, db, conn = _make_view_env(tmp_path)
    result, reason = _resolve_session_query("zzz-no-such-thing", conn, str(claude))
    assert result is None
    assert "no sessions match" in reason
    conn.close()


def test_resolve_exact_name_match(tmp_path):
    """#42: an exact session-name query resolves directly (Claude Code's
    own /resume exact-title semantics)."""
    claude, db, conn = _make_view_env(tmp_path)
    session, method = _resolve_session_query(
        "alpha-build-session", conn, str(claude)
    )
    assert method == "name-exact"
    assert session["session_id"] == UUID_A
    conn.close()


def test_resolve_exact_name_beats_substring_matches(tmp_path):
    """#42: the exact name wins even when it ALSO substring-matches other
    sessions -- direct resolve, not a candidates list."""
    from claude_session_backup.index import upsert_session as _upsert
    claude, db, conn = _make_view_env(tmp_path)
    # A third session whose name CONTAINS the full name of session A,
    # so a pure substring search would return 2 candidates.
    uuid_c = "cccc0003-1111-2222-3333-444444444444"
    _upsert(
        conn,
        SessionMetadata(
            session_id=uuid_c,
            session_name="alpha-build-session-redux-part-two",
            project="C--code-viewproj",
        ),
        jsonl_path=f"projects/C--code-viewproj/{uuid_c}.jsonl",
        jsonl_size=10, jsonl_mtime=1700000002.0,
        scanned_at="2026-05-01T10:00:00Z",
    )
    session, method = _resolve_session_query(
        "alpha-build-session", conn, str(claude)
    )
    assert method == "name-exact", (
        "exact name must resolve directly, not return candidates"
    )
    assert session["session_id"] == UUID_A
    conn.close()


# ── cmd_view flows ──────────────────────────────────────────────────────


def test_cmd_view_no_query_lists_recent(tmp_path, capsys):
    claude, db, conn = _make_view_env(tmp_path)
    conn.close()
    rc = cmd_view(_view_args(tmp_path, db, claude))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Recent sessions" in out


def test_cmd_view_unresolved_query_errors(tmp_path, capsys):
    claude, db, conn = _make_view_env(tmp_path)
    conn.close()
    rc = cmd_view(_view_args(tmp_path, db, claude, query="zzz-nothing"))
    assert rc == 1
    assert "no sessions match" in capsys.readouterr().err


def test_cmd_view_multi_candidate_lists_and_exits_1(tmp_path, capsys):
    claude, db, conn = _make_view_env(tmp_path)
    conn.close()
    rc = cmd_view(_view_args(tmp_path, db, claude, query="session"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "2 sessions match" in out


def test_cmd_view_launches_with_full_uuid(
    tmp_path, capsys, monkeypatch, isolated_viewer_env
):
    claude, db, conn = _make_view_env(tmp_path)
    conn.close()
    launched = {}
    monkeypatch.setattr(
        "claude_session_backup.commands._find_viewer",
        lambda config: {"mode": "binary", "path": "X"},
    )
    monkeypatch.setattr(
        "claude_session_backup.commands._launch_viewer",
        lambda viewer, value: launched.update(viewer=viewer, value=value) or 0,
    )
    rc = cmd_view(_view_args(tmp_path, db, claude, query="aaaa0001"))
    out = capsys.readouterr().out
    assert rc == 0
    assert launched["value"] == UUID_A  # full UUID, never the prefix
    assert "Opening: alpha-build-session" in out


def test_cmd_view_no_viewer_prints_path_rc0(
    tmp_path, capsys, isolated_viewer_env
):
    claude, db, conn = _make_view_env(tmp_path)
    conn.close()
    rc = cmd_view(_view_args(tmp_path, db, claude, query="aaaa0001"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "No viewer found" in out
    assert f"{UUID_A}.jsonl" in out  # the path IS the useful output
    assert "CLAUDEVIEW_BIN" in out and "viewer_path" in out


def test_cmd_view_transcript_missing_on_disk_errors(
    tmp_path, capsys, isolated_viewer_env
):
    claude, db, conn = _make_view_env(tmp_path, with_jsonl=False)
    conn.close()
    rc = cmd_view(_view_args(tmp_path, db, claude, query="aaaa0001"))
    err = capsys.readouterr().err
    assert rc == 1
    assert "Transcript not on disk" in err
    assert "csb backup" in err or "csb restore" in err


def test_cmd_view_pruned_no_restore_pruned_errors(
    tmp_path, capsys, isolated_viewer_env
):
    from claude_session_backup.index import mark_deleted
    claude, db, conn = _make_view_env(tmp_path)
    mark_deleted(conn, UUID_A, "2026-05-31T00:00:00Z")
    conn.close()
    rc = cmd_view(_view_args(
        tmp_path, db, claude, query="aaaa0001", no_restore_pruned=True,
    ))
    err = capsys.readouterr().err
    assert rc == 1
    assert "not viewing" in err
    assert f"csb view {UUID_A}" in err  # the retry hint names view, not resume


def test_cmd_view_pruned_restore_pruned_restores_then_launches(
    tmp_path, capsys, monkeypatch, isolated_viewer_env
):
    from claude_session_backup.commands import RestoreResult
    from claude_session_backup.index import mark_deleted
    claude, db, conn = _make_view_env(tmp_path)
    mark_deleted(conn, UUID_A, "2026-05-31T00:00:00Z")
    conn.close()

    restored = {}
    monkeypatch.setattr(
        "claude_session_backup.commands.git_find_deleted_file",
        lambda cd, p: "deadbeefcafe",
    )
    monkeypatch.setattr(
        "claude_session_backup.commands._restore_session",
        lambda **kw: restored.update(kw) or RestoreResult(
            wrote=3, commit_short="deadbeef"),
    )
    monkeypatch.setattr(
        "claude_session_backup.commands._find_viewer",
        lambda config: {"mode": "binary", "path": "X"},
    )
    launched = {}
    monkeypatch.setattr(
        "claude_session_backup.commands._launch_viewer",
        lambda viewer, value: launched.update(value=value) or 0,
    )
    rc = cmd_view(_view_args(
        tmp_path, db, claude, query="aaaa0001", restore_pruned=True,
    ))
    out = capsys.readouterr().out
    assert rc == 0
    assert restored["full_uuid"] == UUID_A
    assert restored["db_mtime"] == 1700000000.0  # #40 fidelity rides along
    assert "Restoring from git before viewing" in out
    assert "Restored 3 files" in out
    assert launched["value"] == UUID_A


def test_cmd_view_pruned_restore_failure_aborts_launch(
    tmp_path, capsys, monkeypatch, isolated_viewer_env
):
    """#34 AC6 mirror for view: a restore that fails for every file must
    not launch the viewer. rc 1, clear message, no launch attempt."""
    from claude_session_backup.commands import RestoreResult
    from claude_session_backup.index import mark_deleted
    claude, db, conn = _make_view_env(tmp_path)
    mark_deleted(conn, UUID_A, "2026-05-31T00:00:00Z")
    conn.close()

    monkeypatch.setattr(
        "claude_session_backup.commands.git_find_deleted_file",
        lambda cd, p: "deadbeefcafe",
    )
    monkeypatch.setattr(
        "claude_session_backup.commands._restore_session",
        lambda **kw: RestoreResult(
            wrote=0, failed=["projects/x/a.jsonl"], commit_short="deadbeef",
        ),
    )
    launched = []
    monkeypatch.setattr(
        "claude_session_backup.commands._launch_viewer",
        lambda viewer, value: launched.append(value) or 0,
    )
    rc = cmd_view(_view_args(
        tmp_path, db, claude, query="aaaa0001", restore_pruned=True,
    ))
    err = capsys.readouterr().err
    assert rc == 1
    assert "not launching the viewer" in err
    assert launched == []


def test_launch_viewer_binary_detached(monkeypatch):
    """Binary mode uses Popen (detached), passes --session <value>."""
    calls = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls["cmd"] = cmd
            calls["kw"] = kw

    monkeypatch.setattr("subprocess.Popen", FakePopen)
    rc = _launch_viewer({"mode": "binary", "path": "viewer.exe"}, UUID_A)
    assert rc == 0
    assert calls["cmd"] == ["viewer.exe", "--session", UUID_A]
    if os.name == "nt":
        import subprocess
        assert calls["kw"]["creationflags"] & subprocess.DETACHED_PROCESS

# == #42: csb resume accepts every identifier csb view accepts ==============
#
# Claude Code's native surface (verified in resume.tsx): full UUID, or
# exact custom-title match. csb resolves names/paths/keywords to the UUID
# via the index and always hands claude --resume the full UUID.


def _resume_args(db, claude, **kw):
    defaults = dict(
        session_id=None, claude_dir=str(claude), db=str(db), quiet=True,
        restore_pruned=False, no_restore_pruned=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _intercept_claude(monkeypatch, launched):
    import subprocess as subprocess_module
    real_run = subprocess_module.run

    def _mock_run(*a, **kw):
        cmd = a[0] if a else kw.get("args", [])
        if cmd and cmd[0] == "claude":
            launched.append(cmd)

            class _R:
                returncode = 0
            return _R()
        return real_run(*a, **kw)

    monkeypatch.setattr(subprocess_module, "run", _mock_run)


def test_cmd_resume_by_exact_session_name(tmp_path, capsys, monkeypatch):
    """#42 headline: `csb resume <exact-session-name>` resolves via the
    index and launches `claude --resume <full-uuid>`."""
    from claude_session_backup.commands import cmd_resume
    claude, db, conn = _make_view_env(tmp_path)
    conn.close()
    launched = []
    _intercept_claude(monkeypatch, launched)

    rc = cmd_resume(_resume_args(
        db, claude, session_id="alpha-build-session",
    ))
    captured = capsys.readouterr()
    assert rc == 0, "stderr: " + captured.err
    assert len(launched) == 1
    assert launched[0][:3] == ["claude", "--resume", UUID_A], (
        "claude must receive the FULL UUID, never the name"
    )


def test_cmd_resume_keyword_multi_match_shows_candidates(
    tmp_path, capsys, monkeypatch
):
    """#42: an ambiguous keyword lists candidates (like csb view) and
    never launches claude."""
    from claude_session_backup.commands import cmd_resume
    claude, db, conn = _make_view_env(tmp_path)
    conn.close()
    launched = []
    _intercept_claude(monkeypatch, launched)

    rc = cmd_resume(_resume_args(db, claude, session_id="session"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "2 sessions match" in out
    assert launched == []


def test_cmd_resume_uuid_prefix_regression(tmp_path, capsys, monkeypatch):
    """#42 regression pin: the historical UUID-prefix surface is unchanged."""
    from claude_session_backup.commands import cmd_resume
    claude, db, conn = _make_view_env(tmp_path)
    conn.close()
    launched = []
    _intercept_claude(monkeypatch, launched)

    rc = cmd_resume(_resume_args(db, claude, session_id="aaaa0001"))
    captured = capsys.readouterr()
    assert rc == 0, "stderr: " + captured.err
    assert launched and launched[0][:3] == ["claude", "--resume", UUID_A]


def test_cmd_resume_nonsense_query_errors_cleanly(
    tmp_path, capsys, monkeypatch
):
    """#42: a query matching nothing errors with the resolver's message."""
    from claude_session_backup.commands import cmd_resume
    claude, db, conn = _make_view_env(tmp_path)
    conn.close()
    launched = []
    _intercept_claude(monkeypatch, launched)

    rc = cmd_resume(_resume_args(db, claude, session_id="zzz-nothing-here"))
    err = capsys.readouterr().err
    assert rc == 1
    assert "no sessions match" in err
    assert launched == []

"""
Tests for restore correctness (Phase 1 of the restore shoring-up plan).

Closes acceptance criteria for #29 (byte fidelity) and partial coverage for #13
(end-to-end restore verification). Adds the FIRST automated tests against the
restore code path, which previously had zero coverage.

Test surface:
  - git_show_file_bytes: byte-pure read from any commit
  - git_restore_file: byte-pure write to the target location
  - path normalization: backslash paths from legacy DB rows
  - ensure_gitattributes: idempotent block management
  - regression: Linux still works (no Windows-only assumptions)
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_session_backup.git_ops import (
    GITATTRIBUTES_MARKER_BEGIN,
    GITATTRIBUTES_MARKER_END,
    GITATTRIBUTES_RULES,
    _normalize_git_path,
    ensure_gitattributes,
    git_find_deleted_file,
    git_find_jsonl_by_uuid,
    git_ls_tree_for_uuid,
    git_ls_tree_symlinks_for_uuid,
    git_restore_file,
    git_show_file,
    git_show_file_bytes,
)


# ── Helpers ─────────────────────────────────────────────────────────────

def _git_env() -> dict:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
    }


def _git(claude: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(claude), *args],
        capture_output=True, text=True, env=_git_env(), check=False,
    )


def _commit_file(claude: Path, rel_path: str, content: bytes, message: str = "add") -> str:
    """Write a file (byte-exact), git add + commit, return commit hash."""
    full = claude / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content)
    _git(claude, "add", str(full))
    _git(claude, "commit", "--no-gpg-sign", "-m", message)
    head = _git(claude, "rev-parse", "HEAD")
    return head.stdout.strip()


def _commit_symlink(claude: Path, link_rel: str, target: str,
                    message: str = "add symlink") -> str:
    """Stage a git symlink ENTRY (tree mode 120000) whose blob content is
    ``target``, WITHOUT needing real filesystem symlink privileges.

    This is the fixture trick from the v0.3.15 DWP: hash the target-path
    text as a blob, then `git update-index --cacheinfo 120000,...` to add
    it to the index with symlink mode, then commit. The TREE carries mode
    120000 regardless of whether the OS materializes it as a real link on
    checkout -- which is exactly what we need to exercise the discovery /
    skip / write-guard paths deterministically on any platform.

    Returns the commit hash.
    """
    import subprocess as _sp
    r = _sp.run(
        ["git", "-C", str(claude), "hash-object", "-w", "--stdin"],
        input=target.encode("utf-8"),
        capture_output=True, env=_git_env(), check=True,
    )
    blob = r.stdout.decode().strip()
    _git(claude, "update-index", "--add", "--cacheinfo", f"120000,{blob},{link_rel}")
    _git(claude, "commit", "--no-gpg-sign", "-m", message)
    return _git(claude, "rev-parse", "HEAD").stdout.strip()


def _can_make_symlink(tmp: Path) -> bool:
    """True if this OS/user can create real filesystem symlinks (Windows
    needs Developer Mode or admin). Used to skip the write-through-guard
    tests that genuinely need an on-disk symlink at the destination."""
    probe = tmp / "_symlink_probe_link"
    target = tmp / "_symlink_probe_target"
    try:
        target.write_text("x")
        os.symlink(target, probe)
        probe.unlink()
        target.unlink()
        return True
    except (OSError, NotImplementedError):
        # clean up partial state
        try:
            if probe.exists() or probe.is_symlink():
                probe.unlink()
        except OSError:
            pass
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        return False


# ── _normalize_git_path ─────────────────────────────────────────────────

def test_normalize_git_path_backslash_to_forward():
    assert _normalize_git_path("projects\\C--code-foo\\uuid.jsonl") == "projects/C--code-foo/uuid.jsonl"


def test_normalize_git_path_forward_unchanged():
    assert _normalize_git_path("projects/C--code-foo/uuid.jsonl") == "projects/C--code-foo/uuid.jsonl"


def test_normalize_git_path_mixed():
    assert _normalize_git_path("projects/C--code-foo\\uuid.jsonl") == "projects/C--code-foo/uuid.jsonl"


def test_normalize_git_path_strips_leading_separator():
    assert _normalize_git_path("/projects/foo.jsonl") == "projects/foo.jsonl"
    assert _normalize_git_path("\\projects\\foo.jsonl") == "projects/foo.jsonl"


def test_normalize_git_path_accepts_pathlib():
    assert _normalize_git_path(Path("projects/foo.jsonl")) == "projects/foo.jsonl"


# ── git_show_file_bytes ─────────────────────────────────────────────────

def test_git_show_file_bytes_returns_exact_bytes(mock_claude_dir):
    # LF-only content with embedded carriage return inside a JSON string value
    content = b'{"a":1}\n{"msg":"line with \\r literal"}\n'
    commit = _commit_file(mock_claude_dir, "projects/test/sample.jsonl", content, "add lf-only")

    result = git_show_file_bytes(str(mock_claude_dir), commit, "projects/test/sample.jsonl")
    assert result == content, "byte-pure read must return exact bytes from the blob"


def test_git_show_file_bytes_empty_file(mock_claude_dir):
    commit = _commit_file(mock_claude_dir, "projects/test/empty.jsonl", b"", "empty")
    result = git_show_file_bytes(str(mock_claude_dir), commit, "projects/test/empty.jsonl")
    assert result == b""


def test_git_show_file_bytes_large_file(mock_claude_dir):
    # 10MB synthetic JSONL (one big line)
    big_content = (b'{"x":' + b"0" * (10 * 1024 * 1024 - 7) + b"}\n")
    assert len(big_content) >= 10 * 1024 * 1024
    commit = _commit_file(mock_claude_dir, "projects/test/big.jsonl", big_content, "big")
    result = git_show_file_bytes(str(mock_claude_dir), commit, "projects/test/big.jsonl")
    assert result == big_content
    assert len(result) == len(big_content)


def test_git_show_file_bytes_missing_returns_none(mock_claude_dir):
    head = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()
    result = git_show_file_bytes(str(mock_claude_dir), head, "projects/test/does-not-exist.jsonl")
    assert result is None


def test_git_show_file_bytes_normalizes_backslash_path(mock_claude_dir):
    """Path with backslash separators must work the same as forward slashes."""
    content = b'{"slug":"test"}\n'
    commit = _commit_file(mock_claude_dir, "projects/test/back.jsonl", content, "back")
    # Both forms must succeed and return identical bytes
    result_fwd = git_show_file_bytes(str(mock_claude_dir), commit, "projects/test/back.jsonl")
    result_bs = git_show_file_bytes(str(mock_claude_dir), commit, "projects\\test\\back.jsonl")
    assert result_fwd == content
    assert result_bs == content, "backslash path must normalize before reaching git"


# ── git_restore_file (full byte-pure round trip) ────────────────────────

def test_git_restore_file_byte_pure_roundtrip_lf(mock_claude_dir, tmp_path):
    """The canonical Phase 1 acceptance test: LF stays LF on Windows."""
    content = b'{"line":1}\n{"line":2}\n{"line":3}\n'
    commit = _commit_file(mock_claude_dir, "projects/test/lf.jsonl", content, "lf")

    # Delete on disk, restore from commit
    src = mock_claude_dir / "projects/test/lf.jsonl"
    src.unlink()
    assert not src.exists()

    success = git_restore_file(str(mock_claude_dir), commit, "projects/test/lf.jsonl", str(src))
    assert success
    assert src.read_bytes() == content, (
        f"byte-pure restore failed: {src.read_bytes()!r} != {content!r}"
    )


def test_git_restore_file_preserves_embedded_cr(mock_claude_dir):
    """JSON string values with literal \\r must survive the round trip."""
    content = b'{"x":"a\rb\rc"}\n{"y":"normal"}\n'
    commit = _commit_file(mock_claude_dir, "projects/test/cr.jsonl", content, "cr")
    src = mock_claude_dir / "projects/test/cr.jsonl"
    src.unlink()
    assert git_restore_file(str(mock_claude_dir), commit, "projects/test/cr.jsonl", str(src))
    assert src.read_bytes() == content


def test_git_restore_file_empty_file(mock_claude_dir):
    commit = _commit_file(mock_claude_dir, "projects/test/empty.jsonl", b"", "empty")
    src = mock_claude_dir / "projects/test/empty.jsonl"
    src.unlink()
    assert git_restore_file(str(mock_claude_dir), commit, "projects/test/empty.jsonl", str(src))
    assert src.read_bytes() == b""
    assert src.stat().st_size == 0


def test_git_restore_file_large_file(mock_claude_dir):
    big_content = (b'{"x":' + b"0" * (10 * 1024 * 1024 - 7) + b"}\n")
    commit = _commit_file(mock_claude_dir, "projects/test/big.jsonl", big_content, "big")
    src = mock_claude_dir / "projects/test/big.jsonl"
    src.unlink()
    assert git_restore_file(str(mock_claude_dir), commit, "projects/test/big.jsonl", str(src))
    assert src.read_bytes() == big_content


def test_git_restore_file_normalizes_backslash_path(mock_claude_dir):
    """Phase 0 finding: the production code passed backslash paths to git show
    on Windows, which silently failed. After Phase 1, backslash must work."""
    content = b'{"slug":"backslash-test"}\n'
    commit = _commit_file(mock_claude_dir, "projects/test/bs.jsonl", content, "bs")
    src = mock_claude_dir / "projects/test/bs.jsonl"
    src.unlink()
    # Pass the path with backslash -- like a legacy DB row on Windows
    success = git_restore_file(
        str(mock_claude_dir), commit, "projects\\test\\bs.jsonl", str(src)
    )
    assert success, "backslash path must be normalized internally"
    assert src.read_bytes() == content


def test_git_restore_file_accepts_pathlib_dest(mock_claude_dir):
    content = b'{"x":1}\n'
    commit = _commit_file(mock_claude_dir, "projects/test/plib.jsonl", content, "p")
    src = mock_claude_dir / "projects/test/plib.jsonl"
    src.unlink()
    # dest_path as Path, not str
    assert git_restore_file(str(mock_claude_dir), commit, "projects/test/plib.jsonl", src)
    assert src.read_bytes() == content


def test_git_restore_file_missing_returns_false(mock_claude_dir, tmp_path):
    head = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()
    target = tmp_path / "should-not-exist.jsonl"
    result = git_restore_file(
        str(mock_claude_dir), head, "projects/nope/missing.jsonl", str(target)
    )
    assert result is False
    assert not target.exists()


# ── git_find_deleted_file (regression after path normalization) ─────────

def test_git_find_deleted_file_works_with_backslash_path(mock_claude_dir):
    content = b'{"test":true}\n'
    commit = _commit_file(mock_claude_dir, "projects/test/find.jsonl", content, "fwd")
    # Delete on disk -- not committing the deletion (matches csb's pattern)
    (mock_claude_dir / "projects/test/find.jsonl").unlink()
    # Find via backslash path: must locate the same commit
    found = git_find_deleted_file(str(mock_claude_dir), "projects\\test\\find.jsonl")
    assert found == commit


# ── ensure_gitattributes ─────────────────────────────────────────────────

def test_ensure_gitattributes_creates_file_when_missing(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    gitattr = claude / ".gitattributes"
    assert not gitattr.exists()

    changed = ensure_gitattributes(str(claude))
    assert changed is True
    assert gitattr.exists()

    content = gitattr.read_text(encoding="utf-8")
    assert GITATTRIBUTES_MARKER_BEGIN in content
    assert GITATTRIBUTES_MARKER_END in content
    for rule in GITATTRIBUTES_RULES:
        assert rule in content


def test_ensure_gitattributes_idempotent(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    # Call twice -- second call must be a no-op
    first = ensure_gitattributes(str(claude))
    second = ensure_gitattributes(str(claude))
    assert first is True
    assert second is False, "second call must not modify the file"

    # Marker block must appear exactly once
    content = (claude / ".gitattributes").read_text(encoding="utf-8")
    assert content.count(GITATTRIBUTES_MARKER_BEGIN) == 1
    assert content.count(GITATTRIBUTES_MARKER_END) == 1


def test_ensure_gitattributes_preserves_user_block(tmp_path):
    """Pre-existing user rules must be left intact when we append the csb block."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    user_rules = "# my own rules\n*.log -text\n*.bin binary\n"
    (claude / ".gitattributes").write_text(user_rules, encoding="utf-8")

    changed = ensure_gitattributes(str(claude))
    assert changed is True

    final = (claude / ".gitattributes").read_text(encoding="utf-8")
    # User's existing content untouched
    assert "# my own rules" in final
    assert "*.log -text" in final
    assert "*.bin binary" in final
    # csb block now also present
    assert GITATTRIBUTES_MARKER_BEGIN in final
    assert "*.jsonl -text" in final


def test_ensure_gitattributes_does_not_duplicate_when_user_already_added_csb_block(tmp_path):
    """If the user (or a prior csb run) added the block manually, no-op."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    # Simulate a pre-existing csb block (with the markers)
    existing = "\n".join([
        GITATTRIBUTES_MARKER_BEGIN,
        "*.jsonl -text",
        GITATTRIBUTES_MARKER_END,
        "",
    ])
    (claude / ".gitattributes").write_text(existing, encoding="utf-8")
    pre = (claude / ".gitattributes").read_text(encoding="utf-8")

    changed = ensure_gitattributes(str(claude))
    assert changed is False
    post = (claude / ".gitattributes").read_text(encoding="utf-8")
    assert pre == post


# ── End-to-end: backup-then-restore using ensure_gitattributes ──────────

def test_end_to_end_backup_and_restore_byte_pure(mock_claude_dir):
    """Full simulation of csb's backup-then-restore flow with the .gitattributes
    safety net in place. The restored bytes must equal the original bytes."""
    # Set up: drop a JSONL with mixed content, ensure_gitattributes, commit.
    ensure_gitattributes(str(mock_claude_dir))
    _git(mock_claude_dir, "add", str(mock_claude_dir / ".gitattributes"))
    _git(mock_claude_dir, "commit", "--no-gpg-sign", "-m", "csb gitattributes")

    content = b'{"event":"start"}\n{"msg":"contains\\rliteral CR"}\n{"event":"end"}\n'
    rel = "projects/end2end/transcript.jsonl"
    commit = _commit_file(mock_claude_dir, rel, content, "transcript")

    # Capture for later comparison
    original = (mock_claude_dir / rel).read_bytes()
    assert original == content

    # Delete and restore via the real production path
    (mock_claude_dir / rel).unlink()
    assert git_restore_file(str(mock_claude_dir), commit, rel, str(mock_claude_dir / rel))

    restored = (mock_claude_dir / rel).read_bytes()
    assert restored == original, (
        f"end-to-end byte fidelity failed: orig={len(original)} bytes, "
        f"restored={len(restored)} bytes"
    )


# ── git status after restore shows no spurious diff ────────────────────

def test_git_status_after_restore_has_no_spurious_diff(mock_claude_dir):
    """Per #29 AC: after restore, `git status` should show the file identical
    to HEAD -- no autocrlf-normalization diff. Direct evidence that the
    bytes-on-disk match the bytes-in-the-tree."""
    content = b'{"event":"a"}\n{"event":"b"}\n{"event":"c"}\n'
    commit = _commit_file(mock_claude_dir, "projects/test/diff.jsonl", content, "diff")
    src = mock_claude_dir / "projects/test/diff.jsonl"
    src.unlink()
    assert git_restore_file(str(mock_claude_dir), commit, "projects/test/diff.jsonl", str(src))

    # `git status --porcelain` on the just-restored file should show nothing --
    # meaning the working-tree bytes are byte-identical to the HEAD blob.
    status = _git(mock_claude_dir, "status", "--porcelain", "projects/test/diff.jsonl")
    assert status.stdout.strip() == "", (
        f"unexpected spurious diff after restore -- git sees: {status.stdout!r}"
    )


# ── Phase 2: git_find_jsonl_by_uuid (lookup fallback for missing DB row) ──

def _make_session_jsonl(claude_dir: Path, project_slug: str, uuid: str,
                        content: bytes = b'{"x":1}\n', message: str = "add") -> str:
    """Commit a session JSONL under projects/<slug>/<uuid>.jsonl. Returns rel path."""
    rel = f"projects/{project_slug}/{uuid}.jsonl"
    _commit_file(claude_dir, rel, content, message)
    return rel


def test_git_find_jsonl_by_uuid_returns_single_path(mock_claude_dir):
    uuid = "11111111-2222-3333-4444-555555555555"
    rel = _make_session_jsonl(mock_claude_dir, "C--code-proj", uuid)
    paths = git_find_jsonl_by_uuid(str(mock_claude_dir), uuid)
    assert paths == [rel], f"expected [{rel!r}], got {paths!r}"


def test_git_find_jsonl_by_uuid_finds_deleted_file_in_history(mock_claude_dir):
    """Even after the JSONL is deleted from working tree, git history has it."""
    uuid = "22222222-2222-2222-2222-222222222222"
    rel = _make_session_jsonl(mock_claude_dir, "C--code-proj", uuid)
    (mock_claude_dir / rel).unlink()  # delete from disk but not git
    paths = git_find_jsonl_by_uuid(str(mock_claude_dir), uuid)
    assert paths == [rel]


def test_git_find_jsonl_by_uuid_unknown_uuid_returns_empty(mock_claude_dir):
    paths = git_find_jsonl_by_uuid(str(mock_claude_dir), "00000000-dead-beef-cafe-000000000000")
    assert paths == []


def test_git_find_jsonl_by_uuid_excludes_subagent_jsonls(mock_claude_dir):
    """Subagent JSONLs (`projects/<slug>/<session-uuid>/subagents/agent-*.jsonl`)
    have a different path shape and must NOT be returned. We only want top-level
    session transcripts."""
    session_uuid = "33333333-4444-5555-6666-777777777777"
    # Commit a session JSONL
    session_rel = _make_session_jsonl(mock_claude_dir, "C--proj", session_uuid)
    # Also commit a subagent JSONL whose name contains the session uuid string
    subagent_rel = f"projects/C--proj/{session_uuid}/subagents/agent-12345.jsonl"
    _commit_file(mock_claude_dir, subagent_rel, b'{"agent":"a"}\n', "add subagent")

    paths = git_find_jsonl_by_uuid(str(mock_claude_dir), session_uuid)
    assert paths == [session_rel], f"subagent should not appear, got {paths}"


def test_git_find_jsonl_by_uuid_multi_path_slug_collision(mock_claude_dir):
    """Same UUID committed under TWO different project slugs over its lifetime
    (rare -- happens if a parent dir got renamed). Both paths must surface so
    the caller can decide."""
    uuid = "88888888-9999-aaaa-bbbb-cccccccccccc"
    rel_a = _make_session_jsonl(mock_claude_dir, "C--proj-old", uuid, message="under old slug")
    rel_b = _make_session_jsonl(mock_claude_dir, "C--proj-new", uuid,
                                content=b'{"newer":true}\n', message="under new slug")
    paths = git_find_jsonl_by_uuid(str(mock_claude_dir), uuid)
    assert set(paths) == {rel_a, rel_b}, f"expected both paths, got {paths}"
    assert paths == sorted(paths), "results must be sorted for deterministic output"


def test_git_find_jsonl_by_uuid_empty_uuid_returns_empty(mock_claude_dir):
    assert git_find_jsonl_by_uuid(str(mock_claude_dir), "") == []


def test_git_find_jsonl_by_uuid_uses_glob_pathspec_correctly(mock_claude_dir):
    """The `:(glob)` magic prefix matches ONE path component. We test that a
    UUID in a deeper path doesn't false-match."""
    uuid = "44444444-5555-6666-7777-888888888888"
    # This is a deeper-nested path (3 levels under projects/) that includes the UUID
    deep_rel = f"projects/C--proj/sub/{uuid}.jsonl"
    _commit_file(mock_claude_dir, deep_rel, b'{"deep":true}\n', "add deep")
    # And one at the correct depth
    legit_rel = _make_session_jsonl(mock_claude_dir, "C--legit", uuid,
                                     content=b'{"legit":true}\n', message="add legit")

    paths = git_find_jsonl_by_uuid(str(mock_claude_dir), uuid)
    # The deeper path SHOULD match too since git's :(glob) `*` only matches one
    # component but the pattern is `projects/*/UUID.jsonl` -- so the 3-level
    # path won't match. Only the 2-level (correct) one should match.
    assert legit_rel in paths
    assert deep_rel not in paths, (
        f"deep-nested path should not match the projects/*/UUID.jsonl pattern: {paths}"
    )


# ── Phase 2: cmd_restore end-to-end with fallback ───────────────────────

def _make_args_namespace(**kwargs):
    """Build a Namespace mimicking argparse output for cmd_restore."""
    import argparse
    defaults = {
        "session_id": None,
        "dry_run": False,
        "quiet": False,
        "claude_dir": None,
        "db": None,
        # v0.3.12: full-restore flags
        "jsonl_only": False,
        "force": False,
        # v0.3.14 (#34): resume-pruned flags
        "restore_pruned": False,
        "no_restore_pruned": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_cmd_restore_falls_back_to_git_when_db_row_missing(mock_claude_dir, tmp_path, capsys):
    """The headline Phase 2 case: rebuild-index wiped the DB row, but git still
    has the JSONL. cmd_restore must find it via git_find_jsonl_by_uuid."""
    from claude_session_backup.commands import cmd_restore
    from claude_session_backup.index import open_db, init_schema

    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    # Match conftest's session UUID -- already committed in the initial commit
    src = mock_claude_dir / "projects/C--code-test" / f"{uuid}.jsonl"
    assert src.exists(), "fixture should have this session"
    # The blob in git is the source of truth (autocrlf may have normalized
    # the on-disk CRLF to LF at commit time). Phase 1's byte-pure restore
    # reproduces the BLOB, not whatever was on disk. So that's what we
    # compare against.
    rel = f"projects/C--code-test/{uuid}.jsonl"
    expected = git_show_file_bytes(str(mock_claude_dir), "HEAD", rel)
    assert expected is not None
    src.unlink()
    # v0.3.12: full-restore also walks session-states/<uuid>.* -- the
    # conftest fixture commits these, so they're conflicts unless removed.
    # Test intent is the JSONL fallback only; delete the sidecars so we
    # observe a clean restore.
    for sidecar in (mock_claude_dir / "session-states").glob(f"{uuid}.*"):
        sidecar.unlink()

    # Build a brand-new (empty) DB -- simulates post-rebuild-index state
    fresh_db = tmp_path / "fresh.db"
    conn = open_db(str(fresh_db))
    init_schema(conn)
    conn.close()

    args = _make_args_namespace(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
    )
    rc = cmd_restore(args)
    captured = capsys.readouterr()

    assert rc == 0, f"expected success, got rc={rc}; stderr={captured.err}"
    assert "fallback" in captured.out.lower(), (
        f"expected fallback notice in output, got: {captured.out!r}"
    )
    assert src.exists(), "JSONL should be restored to disk"
    assert src.read_bytes() == expected, "restored bytes must match the git blob"


def test_cmd_restore_errors_when_neither_db_nor_git_has_session(mock_claude_dir, tmp_path, capsys):
    from claude_session_backup.commands import cmd_restore
    from claude_session_backup.index import open_db, init_schema

    fresh_db = tmp_path / "fresh.db"
    conn = open_db(str(fresh_db))
    init_schema(conn)
    conn.close()

    args = _make_args_namespace(
        session_id="99999999-9999-9999-9999-999999999999",
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
    )
    rc = cmd_restore(args)
    captured = capsys.readouterr()

    assert rc == 1
    assert "no session found" in captured.err.lower()
    assert "git history" in captured.err.lower()


def test_cmd_restore_fallback_requires_full_uuid(mock_claude_dir, tmp_path, capsys):
    """The git-history fallback can't match prefixes (filename comparison).
    A prefix should get a clear error message."""
    from claude_session_backup.commands import cmd_restore
    from claude_session_backup.index import open_db, init_schema

    fresh_db = tmp_path / "fresh.db"
    conn = open_db(str(fresh_db))
    init_schema(conn)
    conn.close()

    args = _make_args_namespace(
        session_id="aaaaaaaa",  # prefix only -- not a full UUID
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
    )
    rc = cmd_restore(args)
    captured = capsys.readouterr()

    assert rc == 1
    assert "full uuid" in captured.err.lower()


def test_cmd_restore_dry_run_against_fallback_path(mock_claude_dir, tmp_path, capsys):
    """`--dry-run` must work even when we reach the JSONL via git-history
    fallback. Should preview without writing anything."""
    from claude_session_backup.commands import cmd_restore
    from claude_session_backup.index import open_db, init_schema

    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    src = mock_claude_dir / "projects/C--code-test" / f"{uuid}.jsonl"
    assert src.exists()
    src.unlink()
    assert not src.exists()
    # v0.3.12 full-restore also discovers session-states/ sidecars; remove
    # them so the dry-run shows what'd be restored without a conflict.
    for sidecar in (mock_claude_dir / "session-states").glob(f"{uuid}.*"):
        sidecar.unlink()

    fresh_db = tmp_path / "fresh.db"
    conn = open_db(str(fresh_db))
    init_schema(conn)
    conn.close()

    args = _make_args_namespace(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
        dry_run=True,
    )
    rc = cmd_restore(args)
    captured = capsys.readouterr()

    assert rc == 0, f"dry-run should succeed, got rc={rc}; stderr={captured.err}"
    assert "would restore" in captured.out.lower()
    assert "fallback" in captured.out.lower(), (
        "dry-run output should indicate this is the fallback path"
    )
    assert not src.exists(), "dry-run must NOT write the file"


def test_cmd_restore_fallback_preserves_existing_file(mock_claude_dir, tmp_path, capsys):
    """When the file IS on disk and we're in fallback mode (no DB row),
    v0.3.12 PRESERVES the on-disk content rather than refusing. The fallback
    is identical to the normal path: missing files restored, present files
    preserved. rc=0 with a clear preserve-count message.
    """
    from claude_session_backup.commands import cmd_restore
    from claude_session_backup.index import open_db, init_schema

    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    src = mock_claude_dir / "projects/C--code-test" / f"{uuid}.jsonl"
    assert src.exists()
    original_content = src.read_bytes()

    fresh_db = tmp_path / "fresh.db"
    conn = open_db(str(fresh_db))
    init_schema(conn)
    conn.close()

    args = _make_args_namespace(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
    )
    rc = cmd_restore(args)
    captured = capsys.readouterr()

    assert rc == 0, "fallback with all present should succeed (no-op); stderr=" + captured.err
    # The on-disk content must be byte-untouched.
    assert src.read_bytes() == original_content, "file must not have been touched"
    # Output should indicate preservation (no overwrite).
    combined = (captured.out + captured.err).lower()
    assert "already on disk" in combined or "preserved" in combined or "nothing to restore" in combined


def test_cmd_restore_with_db_row_unchanged_regression(mock_claude_dir, tmp_path, capsys):
    """When the DB has a row (normal path), behavior must be unchanged from v0.2.4."""
    from claude_session_backup.commands import cmd_restore
    from claude_session_backup.index import open_db, init_schema, upsert_session, mark_deleted
    from claude_session_backup.metadata import SessionMetadata

    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    src = mock_claude_dir / "projects/C--code-test" / f"{uuid}.jsonl"
    rel = f"projects/C--code-test/{uuid}.jsonl"
    # Source of truth is the blob, not the on-disk file (which conftest wrote
    # via write_text -- subject to autocrlf at commit time).
    expected = git_show_file_bytes(str(mock_claude_dir), "HEAD", rel)
    assert expected is not None
    src.unlink()

    # Build a DB with a deleted-marked session row
    db_path = tmp_path / "with-row.db"
    conn = open_db(str(db_path))
    init_schema(conn)
    meta = SessionMetadata(session_id=uuid, project="C--code-test")
    upsert_session(conn, meta, rel, 0, 0.0, "2026-05-16T20:00:00Z")
    mark_deleted(conn, uuid, "2026-05-16T20:00:01Z")
    conn.commit()
    conn.close()

    args = _make_args_namespace(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(db_path),
    )
    rc = cmd_restore(args)
    captured = capsys.readouterr()

    assert rc == 0, f"stderr: {captured.err}"
    assert "fallback" not in captured.out.lower(), (
        "DB-row path should NOT mention fallback in user-visible output"
    )
    assert src.exists()
    assert src.read_bytes() == expected, "restored bytes must match the git blob"


# ── Phase 3: csb scan --deleted / --all / --restore + filter-aware footer ──

@pytest.fixture
def populated_db_and_repo(mock_claude_dir, tmp_path):
    """A claude dir + DB with three sessions, one of them marked deleted on disk.

    Returns (claude_dir, db_path, sessions_dict) where sessions_dict has keys
    'active1', 'active2', 'deleted1' mapping to UUIDs.
    """
    from claude_session_backup.index import (
        open_db, init_schema, upsert_session, mark_deleted,
    )
    from claude_session_backup.metadata import SessionMetadata

    # Three distinct UUIDs
    active1 = "11111111-2222-3333-4444-555555555551"
    active2 = "11111111-2222-3333-4444-555555555552"
    deleted1 = "11111111-2222-3333-4444-555555555553"

    db_path = tmp_path / "phase3.db"
    conn = open_db(str(db_path))
    init_schema(conn)

    # Commit JSONLs to mock_claude_dir for each. deleted1 we'll also unlink
    # from disk to simulate Claude Code purging it.
    for uuid in (active1, active2, deleted1):
        rel = f"projects/C--code-proj/{uuid}.jsonl"
        _commit_file(mock_claude_dir, rel, b'{"x":1}\n', f"add {uuid[:8]}")

        meta = SessionMetadata(session_id=uuid, project="C--code-proj")
        meta.start_folder = "C:\\code\\proj"
        meta.folder_usage = {"C:\\code\\proj": 1}
        upsert_session(conn, meta, rel, 0, 0.0, "2026-05-16T20:00:00Z")

    # Mark deleted1 as deleted (don't actually need to remove the file --
    # but doing so matches a realistic state).
    mark_deleted(conn, deleted1, "2026-05-16T20:00:01Z")
    (mock_claude_dir / f"projects/C--code-proj/{deleted1}.jsonl").unlink()
    conn.commit()
    conn.close()

    return mock_claude_dir, db_path, {
        "active1": active1, "active2": active2, "deleted1": deleted1,
    }


def test_find_sessions_by_directory_default_excludes_deleted(populated_db_and_repo):
    """Regression: default deleted_filter='active' preserves pre-#27 behavior."""
    from claude_session_backup.index import open_db, init_schema, find_sessions_by_directory

    claude, db, ids = populated_db_and_repo
    conn = open_db(str(db))
    init_schema(conn)
    rows = find_sessions_by_directory(
        conn, "C:\\code\\proj", None, None, top_n=None,
    )
    conn.close()
    found = {r["session_id"] for r in rows}
    assert ids["active1"] in found and ids["active2"] in found
    assert ids["deleted1"] not in found, "default mode must exclude deleted"


def test_find_sessions_by_directory_deleted_only(populated_db_and_repo):
    from claude_session_backup.index import open_db, init_schema, find_sessions_by_directory

    claude, db, ids = populated_db_and_repo
    conn = open_db(str(db))
    init_schema(conn)
    rows = find_sessions_by_directory(
        conn, "C:\\code\\proj", None, None, top_n=None, deleted_filter="deleted",
    )
    conn.close()
    found = {r["session_id"] for r in rows}
    assert found == {ids["deleted1"]}, f"expected only deleted1, got {found}"


def test_find_sessions_by_directory_all_returns_both(populated_db_and_repo):
    from claude_session_backup.index import open_db, init_schema, find_sessions_by_directory

    claude, db, ids = populated_db_and_repo
    conn = open_db(str(db))
    init_schema(conn)
    rows = find_sessions_by_directory(
        conn, "C:\\code\\proj", None, None, top_n=None, deleted_filter="all",
    )
    conn.close()
    found = {r["session_id"] for r in rows}
    assert found == set(ids.values())


def test_find_sessions_by_directory_unknown_filter_raises(populated_db_and_repo):
    from claude_session_backup.index import open_db, init_schema, find_sessions_by_directory

    claude, db, _ids = populated_db_and_repo
    conn = open_db(str(db))
    init_schema(conn)
    with pytest.raises(ValueError, match="unknown deleted_filter"):
        find_sessions_by_directory(
            conn, "C:\\code\\proj", None, None, top_n=None,
            deleted_filter="nonsense",
        )
    conn.close()


def test_find_sessions_by_term_deleted_filter(populated_db_and_repo):
    from claude_session_backup.index import open_db, init_schema, find_sessions_by_term

    claude, db, ids = populated_db_and_repo
    conn = open_db(str(db))
    init_schema(conn)
    rows = find_sessions_by_term(conn, "proj", deleted_filter="deleted")
    conn.close()
    found = {r["session_id"] for r in rows}
    assert found == {ids["deleted1"]}


def test_count_deleted_with_filter_unfiltered(populated_db_and_repo):
    from claude_session_backup.index import open_db, init_schema, count_deleted_with_filter

    _claude, db, _ids = populated_db_and_repo
    conn = open_db(str(db))
    init_schema(conn)
    assert count_deleted_with_filter(conn) == 1
    conn.close()


def test_count_deleted_with_filter_matches_keyword(populated_db_and_repo):
    from claude_session_backup.index import open_db, init_schema, count_deleted_with_filter

    _claude, db, _ids = populated_db_and_repo
    conn = open_db(str(db))
    init_schema(conn)
    assert count_deleted_with_filter(conn, "proj") == 1
    assert count_deleted_with_filter(conn, "totally-not-a-match") == 0
    conn.close()


def test_cmd_list_footer_appears_when_active_only_with_deleted_present(
    populated_db_and_repo, capsys,
):
    from claude_session_backup.commands import cmd_list

    claude, db, ids = populated_db_and_repo
    # v0.3.5: --deleted is two-valued (None / "only" / "all"). None == live-only.
    args = _make_args_namespace(
        n=20, deleted=None, json=False,
        filter=None, sort="last-used", top=None, all_folders=False,
        claude_dir=str(claude), db=str(db),
    )
    rc = cmd_list(args)
    captured = capsys.readouterr()
    assert rc == 0
    # Footer should appear exactly once, with the unfiltered phrasing.
    assert "1 deleted session" in captured.out
    assert "matching" not in captured.out, "unfiltered list must NOT echo a keyword"
    assert "csb list --deleted" in captured.out


def test_cmd_list_footer_echoes_filter_keyword(populated_db_and_repo, capsys):
    from claude_session_backup.commands import cmd_list

    claude, db, ids = populated_db_and_repo
    args = _make_args_namespace(
        n=20, deleted=None, json=False,
        filter="proj", sort="last-used", top=None, all_folders=False,
        claude_dir=str(claude), db=str(db),
    )
    rc = cmd_list(args)
    captured = capsys.readouterr()
    assert rc == 0
    assert "1 deleted session matching 'proj'" in captured.out, captured.out
    assert "csb list proj --deleted" in captured.out


def test_cmd_list_no_footer_when_deleted_shown(populated_db_and_repo, capsys):
    from claude_session_backup.commands import cmd_list

    claude, db, ids = populated_db_and_repo
    # v0.3.5: --deleted="only" replaces the old boolean --deleted=True.
    args = _make_args_namespace(
        n=20, deleted="only", json=False,
        filter=None, sort="last-used", top=None, all_folders=False,
        claude_dir=str(claude), db=str(db),
    )
    rc = cmd_list(args)
    captured = capsys.readouterr()
    assert rc == 0
    assert "hidden" not in captured.out, "no footer when --deleted is set"


def test_cmd_list_no_footer_when_zero_deleted(mock_claude_dir, tmp_path, capsys):
    """When there are no deleted sessions, the footer must NOT print."""
    from claude_session_backup.commands import cmd_list
    from claude_session_backup.index import open_db, init_schema, upsert_session
    from claude_session_backup.metadata import SessionMetadata

    db = tmp_path / "zero_deleted.db"
    conn = open_db(str(db))
    init_schema(conn)
    meta = SessionMetadata(session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", project="C--proj")
    upsert_session(conn, meta, "projects/C--proj/x.jsonl", 0, 0.0, "2026-05-16T20:00:00Z")
    conn.commit()
    conn.close()

    # v0.3.5: deleted=None == live-only mode.
    args = _make_args_namespace(
        n=20, deleted=None, json=False,
        filter=None, sort="last-used", top=None, all_folders=False,
        claude_dir=str(mock_claude_dir), db=str(db),
    )
    rc = cmd_list(args)
    captured = capsys.readouterr()
    assert rc == 0
    assert "hidden" not in captured.out


# ── csb scan --deleted / --all / --restore (cmd_scan integration) ──────

def _make_scan_args(**kwargs):
    """Build a Namespace matching argparse output for cmd_scan."""
    import argparse
    defaults = {
        # path-strict mode flags
        "directories_below": None, "directory_only": None, "start_dir_only": None,
        # positionals
        "term": None, "term2": None,
        # display / behavior
        "n": 20, "no_usage": False, "json": False,
        "top": None, "all_folders": False,
        # phase 3 flags
        "deleted": False, "all": False, "restore": False, "dry_run": False,
        "yes": False, "force": False,
        # common flags
        "quiet": False, "claude_dir": None, "db": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_cmd_scan_deleted_returns_only_deleted(populated_db_and_repo, capsys):
    from claude_session_backup.commands import cmd_scan

    claude, db, ids = populated_db_and_repo
    # #41: scan uses the canonical two-valued grammar -- "only", not True.
    args = _make_scan_args(
        directories_below="C:\\code\\proj",
        deleted="only",
        claude_dir=str(claude), db=str(db),
    )
    rc = cmd_scan(args)
    captured = capsys.readouterr()
    assert rc == 0
    # Match on the FULL UUID since my fixture UUIDs share the first 30 chars.
    assert ids["deleted1"] in captured.out, captured.out
    assert ids["active1"] not in captured.out
    assert ids["active2"] not in captured.out
    # Scope label should mention deleted
    assert "deleted sessions" in captured.out.lower()


def test_cmd_scan_all_returns_both(populated_db_and_repo, capsys):
    from claude_session_backup.commands import cmd_scan

    claude, db, ids = populated_db_and_repo
    args = _make_scan_args(
        directories_below="C:\\code\\proj",
        all=True,
        claude_dir=str(claude), db=str(db),
    )
    rc = cmd_scan(args)
    captured = capsys.readouterr()
    assert rc == 0
    # All three should appear when --all is set
    for k in ("active1", "active2", "deleted1"):
        # Either as UUID prefix in output or as part of the "Found N sessions" line
        pass  # actual presence checked via the "Found N" count below
    assert "active+deleted" in captured.out or "3" in captured.out


def test_cmd_scan_restore_dry_run_no_writes(populated_db_and_repo, capsys):
    from claude_session_backup.commands import cmd_scan

    claude, db, ids = populated_db_and_repo
    deleted_path = claude / f"projects/C--code-proj/{ids['deleted1']}.jsonl"
    assert not deleted_path.exists(), "fixture should have deleted this from disk"

    args = _make_scan_args(
        directories_below="C:\\code\\proj",
        restore=True, dry_run=True, yes=True,
        claude_dir=str(claude), db=str(db),
    )
    rc = cmd_scan(args)
    captured = capsys.readouterr()
    assert rc == 0
    assert "would restore" in captured.out.lower()
    assert "dry-run" in captured.out.lower()
    assert not deleted_path.exists(), "dry-run must not write files"


def test_cmd_scan_restore_yes_skips_prompt_and_restores(populated_db_and_repo, capsys):
    from claude_session_backup.commands import cmd_scan

    claude, db, ids = populated_db_and_repo
    deleted_path = claude / f"projects/C--code-proj/{ids['deleted1']}.jsonl"
    assert not deleted_path.exists()

    args = _make_scan_args(
        directories_below="C:\\code\\proj",
        restore=True, yes=True,
        claude_dir=str(claude), db=str(db),
    )
    rc = cmd_scan(args)
    captured = capsys.readouterr()
    assert rc == 0, f"unexpected failure; stderr: {captured.err}"
    assert deleted_path.exists(), "file should be restored"
    assert b'{"x":1}\n' == deleted_path.read_bytes(), "bytes must match the blob"
    assert "Restored: 1" in captured.out


def test_cmd_scan_restore_refuses_existing_without_force(populated_db_and_repo, capsys):
    """If the on-disk file exists, restore must skip unless --force."""
    from claude_session_backup.commands import cmd_scan

    claude, db, ids = populated_db_and_repo
    # Put the file back on disk (as if user un-deleted it manually)
    deleted_path = claude / f"projects/C--code-proj/{ids['deleted1']}.jsonl"
    deleted_path.write_bytes(b'{"modified":true}\n')
    assert deleted_path.exists()

    args = _make_scan_args(
        directories_below="C:\\code\\proj",
        restore=True, yes=True,
        claude_dir=str(claude), db=str(db),
    )
    rc = cmd_scan(args)
    captured = capsys.readouterr()
    assert rc == 0
    assert "SKIP" in captured.out
    assert "use --force" in captured.out
    # File NOT overwritten
    assert deleted_path.read_bytes() == b'{"modified":true}\n'


def test_cmd_scan_restore_force_overwrites_existing(populated_db_and_repo, capsys):
    from claude_session_backup.commands import cmd_scan

    claude, db, ids = populated_db_and_repo
    deleted_path = claude / f"projects/C--code-proj/{ids['deleted1']}.jsonl"
    deleted_path.write_bytes(b'{"modified":true}\n')

    args = _make_scan_args(
        directories_below="C:\\code\\proj",
        restore=True, yes=True, force=True,
        claude_dir=str(claude), db=str(db),
    )
    rc = cmd_scan(args)
    captured = capsys.readouterr()
    assert rc == 0
    assert "OK" in captured.out
    # Now overwritten with the git-blob bytes
    assert deleted_path.read_bytes() == b'{"x":1}\n'


def test_cmd_scan_restore_empty_scope_says_nothing_to_restore(mock_claude_dir, tmp_path, capsys):
    """When no deleted sessions match the scope, exit 0 with a clear notice."""
    from claude_session_backup.commands import cmd_scan
    from claude_session_backup.index import open_db, init_schema

    db = tmp_path / "empty.db"
    conn = open_db(str(db))
    init_schema(conn)
    conn.close()

    args = _make_scan_args(
        directories_below=".",
        restore=True, yes=True,
        claude_dir=str(mock_claude_dir), db=str(db),
    )
    rc = cmd_scan(args)
    captured = capsys.readouterr()
    assert rc == 0
    assert "nothing to restore" in captured.out.lower()


# ── Phase 2 (v0.3.11): safe csb update rebuild-index ───────────────────

def _make_rebuild_args(**kwargs):
    """Build a Namespace mimicking argparse output for cmd_rebuild_index."""
    import argparse
    defaults = {
        "claude_dir": None,
        "db": None,
        "quiet": True,
        "no_commit": True,
        "include_fts5": False,
        "include_backfill_deleted": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_cmd_rebuild_index_preserves_deleted_rows(populated_db_and_repo, capsys):
    """The headline Phase 2 case: rebuild must not lose deleted-session rows.

    Note: mock_claude_dir has its own pre-existing session committed via the
    conftest fixture, which a live-FS rescan will discover. So the rebuild's
    post-state includes BOTH populated_db_and_repo's rows AND that fixture
    session. The test asserts on the load-bearing invariant -- the deleted
    row survives, not absolute counts.
    """
    from claude_session_backup.commands import cmd_rebuild_index
    from claude_session_backup.index import open_db

    claude, db, ids = populated_db_and_repo
    conn = open_db(str(db))
    pre_deleted = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE deleted_at IS NOT NULL"
    ).fetchone()[0]
    pre_culled_present = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (ids["deleted1"],)
    ).fetchone() is not None
    conn.close()
    assert pre_deleted == 1
    assert pre_culled_present

    args = _make_rebuild_args(claude_dir=str(claude), db=str(db))
    rc = cmd_rebuild_index(args)
    assert rc == 0

    # Post-rebuild: the deleted row must survive (the load-bearing invariant).
    conn = open_db(str(db))
    post_deleted = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE deleted_at IS NOT NULL"
    ).fetchone()[0]
    post_culled_row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (ids["deleted1"],)
    ).fetchone()
    conn.close()
    assert post_deleted >= 1, "deleted_at flag lost across rebuild"
    assert post_culled_row is not None, "deleted-session row gone after rebuild"
    assert post_culled_row["deleted_at"], \
        "deleted_at lost its value across rebuild"


def test_cmd_rebuild_index_preserves_folder_usage(populated_db_and_repo):
    """The FK-CASCADE risk: dropping a deleted session row also drops its
    folder_usage rows. The snapshot must preserve them too."""
    from claude_session_backup.commands import cmd_rebuild_index
    from claude_session_backup.index import open_db

    claude, db, ids = populated_db_and_repo
    conn = open_db(str(db))
    pre_folders = conn.execute(
        "SELECT folder_path FROM folder_usage WHERE session_id = ?",
        (ids["deleted1"],),
    ).fetchall()
    conn.close()
    assert len(pre_folders) >= 1, "fixture should have folder_usage for the deleted session"

    args = _make_rebuild_args(claude_dir=str(claude), db=str(db))
    rc = cmd_rebuild_index(args)
    assert rc == 0

    conn = open_db(str(db))
    post_folders = conn.execute(
        "SELECT folder_path FROM folder_usage WHERE session_id = ?",
        (ids["deleted1"],),
    ).fetchall()
    conn.close()
    assert {row["folder_path"] for row in post_folders} == {row["folder_path"] for row in pre_folders}, \
        "folder_usage paths for deleted session lost across rebuild"


def test_cmd_rebuild_index_empty_pre_rebuild_db(mock_claude_dir, tmp_path):
    """Degenerate case: no pre-rebuild DB. Rebuild should succeed cleanly."""
    from claude_session_backup.commands import cmd_rebuild_index

    db_path = tmp_path / "fresh.db"
    assert not db_path.exists(), "test setup error: DB should NOT exist"

    args = _make_rebuild_args(claude_dir=str(mock_claude_dir), db=str(db_path))
    rc = cmd_rebuild_index(args)
    assert rc == 0
    assert db_path.exists(), "rebuild on fresh state should create the DB"


def test_cmd_rebuild_index_clears_bak_on_success(populated_db_and_repo):
    """Successful rebuild must clean up the .bak file."""
    from claude_session_backup.commands import cmd_rebuild_index
    from pathlib import Path

    claude, db, ids = populated_db_and_repo
    bak = Path(str(db) + ".bak")
    assert not bak.exists(), "no leftover .bak before rebuild"

    args = _make_rebuild_args(claude_dir=str(claude), db=str(db))
    rc = cmd_rebuild_index(args)
    assert rc == 0
    assert not bak.exists(), ".bak must be cleaned up on success"


def test_cmd_rebuild_index_clears_stale_bak_from_prior_failure(populated_db_and_repo):
    """A pre-existing .bak from a crashed prior rebuild must not block the next one."""
    from claude_session_backup.commands import cmd_rebuild_index
    from pathlib import Path

    claude, db, ids = populated_db_and_repo
    # Simulate a stale .bak left from a crashed prior rebuild
    bak = Path(str(db) + ".bak")
    bak.write_bytes(b"stale -- should be cleared by next rebuild")
    assert bak.exists()

    args = _make_rebuild_args(claude_dir=str(claude), db=str(db))
    rc = cmd_rebuild_index(args)
    assert rc == 0
    assert not bak.exists(), "stale .bak must be cleared then re-created+removed cleanly"


def test_cmd_rebuild_index_restores_bak_on_failure(populated_db_and_repo, monkeypatch):
    """Inner failure must restore the .bak so the user isn't left without a DB."""
    from claude_session_backup.commands import cmd_rebuild_index
    from claude_session_backup import commands as cmds
    from pathlib import Path

    claude, db, ids = populated_db_and_repo
    pre_size = Path(str(db)).stat().st_size
    pre_bytes = Path(str(db)).read_bytes()

    # Force the indexer's inner function to raise
    def boom(*a, **kw):
        raise RuntimeError("simulated inner failure")
    monkeypatch.setattr(cmds, "_cmd_backup_inner", boom)

    args = _make_rebuild_args(claude_dir=str(claude), db=str(db))
    try:
        cmd_rebuild_index(args)
        assert False, "expected RuntimeError to propagate"
    except RuntimeError:
        pass

    # DB must still exist and match its pre-rebuild contents
    assert Path(str(db)).exists(), "DB must be restored from .bak on failure"
    assert Path(str(db)).read_bytes() == pre_bytes, \
        "DB contents must round-trip across the failed rebuild"
    assert not Path(str(db) + ".bak").exists(), \
        ".bak should have been renamed back into place"


def test_cmd_rebuild_index_include_fts5_flag_calls_stub(populated_db_and_repo, monkeypatch):
    """--include-fts5 must invoke the _maybe_refresh_fts5 stub (main's seam)."""
    from claude_session_backup.commands import cmd_rebuild_index
    from claude_session_backup import commands as cmds

    called = {"n": 0}

    def fake_refresh(args):
        called["n"] += 1

    monkeypatch.setattr(cmds, "_maybe_refresh_fts5", fake_refresh)

    claude, db, ids = populated_db_and_repo
    args = _make_rebuild_args(claude_dir=str(claude), db=str(db), include_fts5=True)
    rc = cmd_rebuild_index(args)
    assert rc == 0
    assert called["n"] == 1, "stub seam should fire exactly once when --include-fts5 set"


def test_cmd_rebuild_index_skips_fts5_stub_when_flag_absent(populated_db_and_repo, monkeypatch):
    """Default rebuild (no --include-fts5) must NOT call the FTS5 seam."""
    from claude_session_backup.commands import cmd_rebuild_index
    from claude_session_backup import commands as cmds

    called = {"n": 0}

    def fake_refresh(args):
        called["n"] += 1

    monkeypatch.setattr(cmds, "_maybe_refresh_fts5", fake_refresh)

    claude, db, ids = populated_db_and_repo
    args = _make_rebuild_args(claude_dir=str(claude), db=str(db), include_fts5=False)
    rc = cmd_rebuild_index(args)
    assert rc == 0
    assert called["n"] == 0


# ── snapshot_deleted_sessions / restore_deleted_snapshot (unit-level) ───

def test_snapshot_deleted_sessions_captures_folders(populated_db_and_repo):
    """Snapshot must include folder_usage rows under the _folders key."""
    from claude_session_backup.index import open_db, snapshot_deleted_sessions

    claude, db, ids = populated_db_and_repo
    conn = open_db(str(db))
    snap = snapshot_deleted_sessions(conn)
    conn.close()

    assert len(snap) == 1, "fixture has exactly one deleted session"
    row = snap[0]
    assert row["session_id"] == ids["deleted1"]
    assert "_folders" in row
    assert len(row["_folders"]) >= 1
    assert row["_folders"][0]["folder_path"] == "C:\\code\\proj"


def test_restore_deleted_snapshot_skips_already_present(populated_db_and_repo):
    """If the live rescan already re-discovered a snapshot UUID, don't double-insert."""
    from claude_session_backup.index import (
        open_db, snapshot_deleted_sessions, restore_deleted_snapshot,
    )

    claude, db, ids = populated_db_and_repo
    conn = open_db(str(db))
    snap = snapshot_deleted_sessions(conn)
    # The deleted session is still in the DB (we haven't done a rebuild here).
    # restore_deleted_snapshot should skip it as already-present.
    restored = restore_deleted_snapshot(conn, snap)
    conn.close()
    assert restored == 0, "should skip rows that already exist in the live DB"


# ── Phase 3 (v0.3.11): git_deleted_jsonls cache + git_list_deleted_jsonls ──

def test_git_list_deleted_jsonls_finds_culled_jsonl(mock_claude_dir):
    """git_list_deleted_jsonls enumerates JSONL deletions from git log."""
    from claude_session_backup.git_ops import git_list_deleted_jsonls

    uuid = "cccccccc-dddd-eeee-ffff-000000000001"
    rel = f"projects/test-proj/{uuid}.jsonl"
    # Commit a file then delete + commit deletion
    _commit_file(mock_claude_dir, rel, b'{"x":1}\n', f"add {uuid[:8]}")
    (mock_claude_dir / rel).unlink()
    _git(mock_claude_dir, "add", "-A")
    _git(mock_claude_dir, "commit", "--no-gpg-sign", "-m", f"cull {uuid[:8]}")

    deletions = git_list_deleted_jsonls(str(mock_claude_dir))
    matching = [d for d in deletions if d["session_id"] == uuid]
    assert len(matching) == 1
    assert matching[0]["jsonl_path"] == rel
    assert matching[0]["deleted_commit"], "should have a commit hash"
    assert matching[0]["deleted_at"], "should have an ISO timestamp"


def test_to_claude_dir_relative_strips_prefix_when_subdir(tmp_path):
    """Unit test for the helper that does the actual path translation.

    Direct verification: when claude_dir is a subdir of the git repo,
    `_to_claude_dir_relative` must strip the repo-to-claude_dir prefix
    so downstream `git -C claude_dir` calls see the path correctly.
    """
    from claude_session_backup.git_ops import (
        _to_claude_dir_relative, _REPO_PREFIX_CACHE,
    )

    # Build the same subdir-in-repo layout as the integration test
    repo_root = tmp_path / "home"
    claude_dir = repo_root / ".claude"
    claude_dir.mkdir(parents=True)

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.local",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.local",
    }
    subprocess.run(["git", "init", str(repo_root)], env=env, check=True,
                   capture_output=True)

    # Clear cache so the test isn't influenced by other tests' cache entries
    _REPO_PREFIX_CACHE.clear()
    try:
        # Case 1: repo-relative path with the prefix -> stripped
        assert _to_claude_dir_relative(
            str(claude_dir), ".claude/projects/foo/u.jsonl"
        ) == "projects/foo/u.jsonl"

        # Case 2: path without the prefix -> passes through (defensive)
        assert _to_claude_dir_relative(
            str(claude_dir), "other/path"
        ) == "other/path"
    finally:
        _REPO_PREFIX_CACHE.clear()


def test_to_repo_relative_prepends_prefix_when_subdir(tmp_path):
    """Symmetric companion: git show <commit>:<path> needs REPO-relative
    paths even when invoked via `-C claude_dir`. The helper must prepend
    the prefix in the subdir case."""
    from claude_session_backup.git_ops import (
        _to_repo_relative, _REPO_PREFIX_CACHE,
    )

    repo_root = tmp_path / "home"
    claude_dir = repo_root / ".claude"
    claude_dir.mkdir(parents=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.local",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.local",
    }
    subprocess.run(["git", "init", str(repo_root)], env=env, check=True,
                   capture_output=True)

    _REPO_PREFIX_CACHE.clear()
    try:
        # claude_dir-relative -> repo-relative (prefix added)
        assert _to_repo_relative(
            str(claude_dir), "projects/foo/u.jsonl"
        ) == ".claude/projects/foo/u.jsonl"

        # If caller already passed the repo-relative form, don't double-prefix
        assert _to_repo_relative(
            str(claude_dir), ".claude/projects/foo/u.jsonl"
        ) == ".claude/projects/foo/u.jsonl"
    finally:
        _REPO_PREFIX_CACHE.clear()


def test_to_claude_dir_relative_noop_when_claude_dir_is_repo_root(tmp_path):
    """When claude_dir IS the repo root, prefix is empty and the helper
    must be a no-op (otherwise it'd corrupt every path)."""
    from claude_session_backup.git_ops import (
        _to_claude_dir_relative, _REPO_PREFIX_CACHE,
    )

    # claude_dir IS the repo root (the README's recommended setup)
    claude_dir = tmp_path / "dot-claude"
    claude_dir.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.local",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.local",
    }
    subprocess.run(["git", "init", str(claude_dir)], env=env, check=True,
                   capture_output=True)

    _REPO_PREFIX_CACHE.clear()
    try:
        assert _to_claude_dir_relative(
            str(claude_dir), "projects/foo/u.jsonl"
        ) == "projects/foo/u.jsonl"
    finally:
        _REPO_PREFIX_CACHE.clear()


def test_git_list_deleted_jsonls_strips_repo_prefix_when_claude_dir_is_subdir(tmp_path):
    """Bug repro: when ~/.claude/ is a subdir of the git repo (repo at ~/),
    git emits paths like '.claude/projects/...' relative to repo root, but
    csb's downstream calls pass them back to git via `-C claude_dir`,
    expecting 'projects/...'. The translation must strip the repo prefix.

    Discovered when csb update backfill-deleted reported "24 unreadable
    from git" against a real ~/ -based repo: the cache had 26 paths with
    .claude/projects/... that subsequent git ops couldn't find.
    """
    from claude_session_backup.git_ops import git_list_deleted_jsonls

    # Build a sandboxed repo at the parent level (mirrors ~/ setup).
    # ~/                          <- repo root
    #   .claude/                  <- claude_dir (subdir, NOT repo root)
    #     projects/<slug>/<uuid>.jsonl
    repo_root = tmp_path / "home"
    claude_dir = repo_root / ".claude"
    proj_dir = claude_dir / "projects" / "subdir-test"
    proj_dir.mkdir(parents=True)

    uuid = "ffffffff-1111-2222-3333-444444444444"
    rel_in_claude_dir = f"projects/subdir-test/{uuid}.jsonl"
    abs_path = claude_dir / "projects" / "subdir-test" / f"{uuid}.jsonl"
    abs_path.write_bytes(b'{"x":1}\n')

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.local",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.local",
    }
    # git init at the REPO ROOT (parent of claude_dir, not claude_dir itself)
    subprocess.run(["git", "init", str(repo_root)], env=env, check=True,
                   capture_output=True)
    for k, v in [("commit.gpgsign", "false"), ("user.name", "test"),
                 ("user.email", "t@t.local")]:
        subprocess.run(["git", "-C", str(repo_root), "config", k, v],
                       env=env, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_root), "add", "-A"],
                   env=env, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_root), "commit",
                    "--no-gpg-sign", "-m", "initial"],
                   env=env, check=True, capture_output=True)
    # Cull + commit deletion
    abs_path.unlink()
    subprocess.run(["git", "-C", str(repo_root), "add", "-A"],
                   env=env, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_root), "commit",
                    "--no-gpg-sign", "-m", "cull"],
                   env=env, check=True, capture_output=True)

    deletions = git_list_deleted_jsonls(str(claude_dir))
    matching = [d for d in deletions if d["session_id"] == uuid]
    assert len(matching) == 1, "should find the culled JSONL"
    # KEY ASSERTION: the cached path must be claude_dir-relative, NOT
    # repo-relative. Before the fix this came back as '.claude/projects/...'
    assert matching[0]["jsonl_path"] == rel_in_claude_dir, (
        f"path must be claude_dir-relative ('{rel_in_claude_dir}'), got "
        f"'{matching[0]['jsonl_path']}' (the repo-root-relative form). "
        f"This is the bug: downstream git_show_file_bytes(claude_dir, "
        f"commit, path) cannot find the file when 'path' has the .claude/ "
        f"prefix."
    )


def test_git_list_deleted_jsonls_excludes_subagent_jsonls(mock_claude_dir):
    """The :(glob) pathspec must not match nested subagent JSONLs."""
    from claude_session_backup.git_ops import git_list_deleted_jsonls

    parent_uuid = "11111111-aaaa-bbbb-cccc-000000000001"
    subagent_uuid = "22222222-aaaa-bbbb-cccc-000000000002"
    parent_rel = f"projects/foo/{parent_uuid}.jsonl"
    subagent_rel = f"projects/foo/{parent_uuid}/subagents/{subagent_uuid}.jsonl"

    _commit_file(mock_claude_dir, parent_rel, b'{"x":1}\n', "add parent")
    _commit_file(mock_claude_dir, subagent_rel, b'{"y":2}\n', "add subagent")
    # Delete both
    (mock_claude_dir / parent_rel).unlink()
    (mock_claude_dir / subagent_rel).unlink()
    _git(mock_claude_dir, "add", "-A")
    _git(mock_claude_dir, "commit", "--no-gpg-sign", "-m", "cull both")

    deletions = git_list_deleted_jsonls(str(mock_claude_dir))
    uuids = {d["session_id"] for d in deletions}
    assert parent_uuid in uuids
    assert subagent_uuid not in uuids, "subagent JSONLs must be excluded by :(glob) pattern"


def test_upsert_git_deleted_jsonl_insert_returns_true(tmp_path):
    from claude_session_backup.index import open_db, init_schema, upsert_git_deleted_jsonl

    conn = open_db(str(tmp_path / "cache.db"))
    init_schema(conn)
    inserted = upsert_git_deleted_jsonl(
        conn, jsonl_path="projects/x/u.jsonl", session_id="u",
        deleted_commit="abc123", deleted_at="2026-06-01T00:00:00+00:00",
    )
    conn.close()
    assert inserted is True


def test_upsert_git_deleted_jsonl_update_returns_false(tmp_path):
    """Second upsert of the same path is an UPDATE, returns False."""
    from claude_session_backup.index import open_db, init_schema, upsert_git_deleted_jsonl

    conn = open_db(str(tmp_path / "cache.db"))
    init_schema(conn)
    upsert_git_deleted_jsonl(conn, "projects/x/u.jsonl", "u", "abc123")
    second = upsert_git_deleted_jsonl(conn, "projects/x/u.jsonl", "u", "def456")
    conn.close()
    assert second is False


def test_upsert_git_deleted_jsonl_preserves_extracted_flag(tmp_path):
    """Re-upserting a row must NOT reset extracted_metadata to 0."""
    from claude_session_backup.index import (
        open_db, init_schema,
        upsert_git_deleted_jsonl, mark_git_deleted_extracted,
        list_git_deleted_jsonls,
    )

    conn = open_db(str(tmp_path / "cache.db"))
    init_schema(conn)
    upsert_git_deleted_jsonl(conn, "projects/x/u.jsonl", "u", "abc123")
    mark_git_deleted_extracted(conn, "projects/x/u.jsonl")
    # Re-upsert (simulating a later refresh that re-sees the same deletion)
    upsert_git_deleted_jsonl(conn, "projects/x/u.jsonl", "u", "abc123",
                              last_refreshed_at="2026-06-02T00:00:00Z")
    rows = list_git_deleted_jsonls(conn)
    conn.close()
    assert len(rows) == 1
    assert rows[0]["extracted_metadata"] == 1, "extracted flag must survive re-upsert"


# ── Phase 4 (v0.3.11): cmd_backfill_deleted ───────────────────────────

def test_extract_metadata_from_bytes_takes_session_id_from_param(mock_claude_dir):
    """Phase 0 finding: extract_metadata reads session_id from filename.
    The from_bytes variant must take session_id explicitly (not from blob)."""
    from claude_session_backup.metadata import extract_metadata_from_bytes

    blob = b'{"type":"custom-title","customTitle":"test","sessionId":"u-from-json"}\n'
    meta = extract_metadata_from_bytes(blob, session_id="from-param", project="proj")
    assert meta.session_id == "from-param", "must use parameter, not infer from blob"
    assert meta.project == "proj"
    assert meta.session_name == "test"


def test_extract_metadata_from_bytes_parses_events(mock_claude_dir):
    """The bytes variant must produce the same metadata as the file variant
    for equivalent input."""
    from claude_session_backup.metadata import (
        extract_metadata, extract_metadata_from_bytes,
    )
    import tempfile

    blob = (b'{"type":"custom-title","customTitle":"hello"}\n'
            b'{"type":"user","timestamp":"2026-06-01T10:00:00Z",'
            b'"cwd":"/c/proj","message":{"content":"hi"}}\n'
            b'{"type":"assistant","timestamp":"2026-06-01T10:00:05Z",'
            b'"cwd":"/c/proj"}\n')

    # File-based reference
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        tf.write(blob)
        tf_path = Path(tf.name)
    try:
        file_meta = extract_metadata(tf_path)
    finally:
        tf_path.unlink(missing_ok=True)

    # Bytes-based variant (with explicit session_id since the file path is gone)
    bytes_meta = extract_metadata_from_bytes(blob, session_id=file_meta.session_id)

    # Modulo session_id (driven by filename in the file variant), the
    # parsed fields must match.
    assert bytes_meta.session_name == file_meta.session_name == "hello"
    assert bytes_meta.start_folder == file_meta.start_folder == "/c/proj"
    assert bytes_meta.message_count == file_meta.message_count == 2
    assert bytes_meta.folder_usage == file_meta.folder_usage


@pytest.fixture
def repo_with_culled_session(mock_claude_dir, tmp_path):
    """A claude dir whose git history has one CULLED session (JSONL deleted
    on disk + commit) plus an empty DB. The set-up for cmd_backfill_deleted."""
    uuid = "abcdef00-1111-2222-3333-444444444444"
    rel = f"projects/test-cull-proj/{uuid}.jsonl"
    content = (b'{"type":"custom-title","customTitle":"culled-session"}\n'
               b'{"type":"user","timestamp":"2026-06-01T10:00:00Z",'
               b'"cwd":"/c/cull-test"}\n')
    _commit_file(mock_claude_dir, rel, content, f"add {uuid[:8]}")
    (mock_claude_dir / rel).unlink()
    _git(mock_claude_dir, "add", "-A")
    _git(mock_claude_dir, "commit", "--no-gpg-sign", "-m", f"cull {uuid[:8]}")

    db = tmp_path / "backfill.db"
    return mock_claude_dir, db, uuid, rel


def _make_backfill_args(**kwargs):
    import argparse
    defaults = {
        "claude_dir": None, "db": None, "quiet": True,
        "dry_run": False, "full": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_cmd_backfill_deleted_synthesizes_row_for_culled_session(repo_with_culled_session):
    """Headline Phase 4 case: a session culled from disk is now in git
    history. backfill-deleted must synthesize a deleted-flagged row from
    the historical blob."""
    from claude_session_backup.commands import cmd_backfill_deleted
    from claude_session_backup.index import open_db, init_schema, get_session

    claude, db, uuid, rel = repo_with_culled_session
    args = _make_backfill_args(claude_dir=str(claude), db=str(db))

    rc = cmd_backfill_deleted(args)
    assert rc == 0

    conn = open_db(str(db))
    init_schema(conn)
    row = get_session(conn, uuid)
    conn.close()

    assert row is not None, "synthesized row missing for culled session"
    assert row["session_id"] == uuid
    assert row["session_name"] == "culled-session"
    assert row["deleted_at"], "synthesized row must have deleted_at set"
    assert row["start_folder"] == "/c/cull-test"


def test_cmd_backfill_deleted_dry_run_writes_nothing(repo_with_culled_session):
    """--dry-run must NOT insert any rows."""
    from claude_session_backup.commands import cmd_backfill_deleted
    from claude_session_backup.index import open_db, init_schema, get_session, count_git_deleted_jsonls

    claude, db, uuid, rel = repo_with_culled_session
    args = _make_backfill_args(claude_dir=str(claude), db=str(db), dry_run=True)

    rc = cmd_backfill_deleted(args)
    assert rc == 0

    conn = open_db(str(db))
    init_schema(conn)
    # No sessions row
    assert get_session(conn, uuid) is None
    # No cache row either (dry run doesn't even write to git_deleted_jsonls)
    assert count_git_deleted_jsonls(conn) == 0
    conn.close()


def test_cmd_backfill_deleted_skips_already_in_live_db(repo_with_culled_session):
    """If the live sessions table already has a row for the UUID, just
    flag the cache row as extracted and skip the re-import."""
    from claude_session_backup.commands import cmd_backfill_deleted
    from claude_session_backup.index import (
        open_db, init_schema, upsert_session, count_git_deleted_jsonls,
        list_git_deleted_jsonls,
    )
    from claude_session_backup.metadata import SessionMetadata

    claude, db, uuid, rel = repo_with_culled_session

    # Pre-populate the live DB with a row for this UUID
    conn = open_db(str(db))
    init_schema(conn)
    meta = SessionMetadata(session_id=uuid, project="test-cull-proj")
    meta.session_name = "pre-existing"
    upsert_session(conn, meta, rel, 0, 0.0, "2026-06-01T00:00:00Z")
    conn.close()

    args = _make_backfill_args(claude_dir=str(claude), db=str(db))
    rc = cmd_backfill_deleted(args)
    assert rc == 0

    # The cache row should exist but be marked extracted (since the live
    # row was already there).
    conn = open_db(str(db))
    extracted = list_git_deleted_jsonls(conn, extracted=1)
    pending = list_git_deleted_jsonls(conn, extracted=0)
    conn.close()
    assert len(extracted) == 1
    assert len(pending) == 0
    assert extracted[0]["session_id"] == uuid


def test_cmd_backfill_deleted_idempotent(repo_with_culled_session):
    """Running backfill twice must not double-insert."""
    from claude_session_backup.commands import cmd_backfill_deleted
    from claude_session_backup.index import open_db, init_schema

    claude, db, uuid, rel = repo_with_culled_session
    args = _make_backfill_args(claude_dir=str(claude), db=str(db))

    cmd_backfill_deleted(args)
    cmd_backfill_deleted(args)  # second pass

    conn = open_db(str(db))
    n = conn.execute("SELECT COUNT(*) FROM sessions WHERE session_id = ?",
                     (uuid,)).fetchone()[0]
    conn.close()
    assert n == 1, "second backfill pass must not create a duplicate"


def test_cmd_update_backfill_deleted_via_cli_dispatcher(repo_with_culled_session):
    """End-to-end: csb update backfill-deleted via the cli.main() entrypoint."""
    from claude_session_backup.cli import main as cli_main

    claude, db, uuid, rel = repo_with_culled_session
    rc = cli_main([
        "update", "backfill-deleted",
        "--claude-dir", str(claude),
        "--db", str(db),
        "--quiet",
    ])
    assert rc == 0


def test_cmd_backfill_deleted_auto_repairs_sparse_folder_usage(repo_with_culled_session):
    """The 'past-rebuild fingerprint' case: a deleted-session live row exists
    but its folder_usage was wiped by an old destructive rebuild. backfill
    should auto-detect (sparse folder_usage + git has richer data) and
    refresh the row in place."""
    from claude_session_backup.commands import cmd_backfill_deleted
    from claude_session_backup.index import (
        open_db, init_schema, upsert_session, mark_deleted,
    )
    from claude_session_backup.metadata import SessionMetadata

    claude, db, uuid, rel = repo_with_culled_session

    # Pre-populate a SPARSE live row -- only 1 folder (the start_folder).
    # This is what `csb list --deleted only` shows after a past
    # destructive rebuild cascade-deleted the rich folder_usage.
    conn = open_db(str(db))
    init_schema(conn)
    sparse_meta = SessionMetadata(session_id=uuid, project="test-cull-proj")
    sparse_meta.session_name = "culled-session"  # name we know is in the blob
    sparse_meta.start_folder = "/c/cull-test"
    sparse_meta.folder_usage = {"/c/cull-test": 1}  # sparse: just the one
    upsert_session(conn, sparse_meta, rel, 0, 0.0, "2026-06-01T00:00:00Z")
    mark_deleted(conn, uuid, "2026-06-01T12:00:00Z")
    pre_folder_count = conn.execute(
        "SELECT COUNT(*) FROM folder_usage WHERE session_id = ?", (uuid,)
    ).fetchone()[0]
    conn.close()
    assert pre_folder_count == 1, "fixture setup: row should be sparse"

    # Add a second folder to the git blob so it has STRICTLY MORE data
    # than the live sparse row. We commit a richer JSONL that replaces
    # what was culled, then re-cull it.
    richer = (b'{"type":"custom-title","customTitle":"culled-session"}\n'
              b'{"type":"user","timestamp":"2026-06-01T10:00:00Z",'
              b'"cwd":"/c/cull-test"}\n'
              b'{"type":"assistant","timestamp":"2026-06-01T10:00:05Z",'
              b'"cwd":"/c/cull-test/subdir"}\n'
              b'{"type":"user","timestamp":"2026-06-01T10:01:00Z",'
              b'"cwd":"/c/cull-test/subdir"}\n')
    _commit_file(claude, rel, richer, "re-add with richer cwds")
    (claude / rel).unlink()
    _git(claude, "add", "-A")
    _git(claude, "commit", "--no-gpg-sign", "-m", "re-cull richer version")

    args = _make_backfill_args(claude_dir=str(claude), db=str(db))
    rc = cmd_backfill_deleted(args)
    assert rc == 0

    # The sparse row should now have its folder_usage refreshed from git.
    conn = open_db(str(db))
    post_folder_count = conn.execute(
        "SELECT COUNT(*) FROM folder_usage WHERE session_id = ?", (uuid,)
    ).fetchone()[0]
    post_row = conn.execute(
        "SELECT deleted_at FROM sessions WHERE session_id = ?", (uuid,)
    ).fetchone()
    conn.close()
    assert post_folder_count > pre_folder_count, "folder_usage should have been refreshed from git"
    assert post_row["deleted_at"], "deleted_at must survive the auto-repair"


def test_cmd_backfill_deleted_leaves_intact_rows_alone(repo_with_culled_session):
    """Rows whose folder_usage already has >=2 entries are NOT auto-repaired
    (the gate is sparse-only). Avoids unnecessary rewrites."""
    from claude_session_backup.commands import cmd_backfill_deleted
    from claude_session_backup.index import (
        open_db, init_schema, upsert_session, mark_deleted,
    )
    from claude_session_backup.metadata import SessionMetadata

    claude, db, uuid, rel = repo_with_culled_session

    # Pre-populate an INTACT live row -- 2+ folders already.
    conn = open_db(str(db))
    init_schema(conn)
    rich_meta = SessionMetadata(session_id=uuid, project="test-cull-proj")
    rich_meta.session_name = "culled-session"
    rich_meta.start_folder = "/c/cull-test"
    # 3 folders: above the sparse gate
    rich_meta.folder_usage = {
        "/c/cull-test": 100, "/c/cull-test/a": 50, "/c/cull-test/b": 20,
    }
    upsert_session(conn, rich_meta, rel, 999, 12345.0, "2026-06-01T00:00:00Z")
    mark_deleted(conn, uuid, "2026-06-01T12:00:00Z")
    pre_size = conn.execute(
        "SELECT jsonl_size FROM sessions WHERE session_id = ?", (uuid,)
    ).fetchone()[0]
    pre_folder_count = conn.execute(
        "SELECT COUNT(*) FROM folder_usage WHERE session_id = ?", (uuid,)
    ).fetchone()[0]
    conn.close()
    assert pre_folder_count == 3

    args = _make_backfill_args(claude_dir=str(claude), db=str(db))
    rc = cmd_backfill_deleted(args)
    assert rc == 0

    # The intact row should be untouched -- same size, same folder count.
    conn = open_db(str(db))
    post_size = conn.execute(
        "SELECT jsonl_size FROM sessions WHERE session_id = ?", (uuid,)
    ).fetchone()[0]
    post_folder_count = conn.execute(
        "SELECT COUNT(*) FROM folder_usage WHERE session_id = ?", (uuid,)
    ).fetchone()[0]
    conn.close()
    assert post_folder_count == pre_folder_count == 3, \
        "intact row's folder_usage should NOT have been touched"
    assert post_size == pre_size == 999, "intact row's jsonl_size should NOT have been overwritten"


def test_list_git_deleted_jsonls_filters_by_extracted(tmp_path):
    from claude_session_backup.index import (
        open_db, init_schema,
        upsert_git_deleted_jsonl, mark_git_deleted_extracted,
        list_git_deleted_jsonls, count_git_deleted_jsonls,
    )

    conn = open_db(str(tmp_path / "cache.db"))
    init_schema(conn)
    upsert_git_deleted_jsonl(conn, "projects/a/u1.jsonl", "u1")
    upsert_git_deleted_jsonl(conn, "projects/a/u2.jsonl", "u2")
    upsert_git_deleted_jsonl(conn, "projects/a/u3.jsonl", "u3")
    mark_git_deleted_extracted(conn, "projects/a/u2.jsonl")

    assert count_git_deleted_jsonls(conn) == 3
    assert count_git_deleted_jsonls(conn, extracted=0) == 2
    assert count_git_deleted_jsonls(conn, extracted=1) == 1

    pending = list_git_deleted_jsonls(conn, extracted=0)
    assert {r["jsonl_path"] for r in pending} == {
        "projects/a/u1.jsonl", "projects/a/u3.jsonl",
    }
    done = list_git_deleted_jsonls(conn, extracted=1)
    assert {r["jsonl_path"] for r in done} == {"projects/a/u2.jsonl"}
    conn.close()


# ── Linux regression -- the byte-pure path must also work on POSIX ──────

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX regression check")
def test_byte_pure_restore_works_on_posix(mock_claude_dir):
    """The text-mode pipeline coincidentally worked on Linux because no
    autocrlf translation kicked in. The byte-mode pipeline must also work
    (no behavior regression)."""
    content = b'{"x":1}\n{"y":2}\n'
    commit = _commit_file(mock_claude_dir, "projects/test/posix.jsonl", content, "posix")
    src = mock_claude_dir / "projects/test/posix.jsonl"
    src.unlink()
    assert git_restore_file(str(mock_claude_dir), commit, "projects/test/posix.jsonl", str(src))
    assert src.read_bytes() == content


# ── git_ls_tree_for_uuid (#32 + #33 full-restore discovery) ─────────────

def test_git_ls_tree_for_uuid_returns_jsonl_only_when_nothing_else_committed(mock_claude_dir):
    """Minimal session: only the top-level JSONL was committed.

    Result must contain the JSONL only -- no false positives from sibling
    files in unrelated directories.
    """
    uuid = "aaaaaaaa-1111-2222-3333-444444444444"
    slug = "C--proj-minimal"
    rel = _make_session_jsonl(mock_claude_dir, slug, uuid)
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    paths = git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid)
    assert paths == [rel]


def test_git_ls_tree_for_uuid_includes_subagents_and_tool_results(mock_claude_dir):
    """Full session subtree: subagents/, tool-results/, remote-agents/ all present."""
    uuid = "bbbbbbbb-1111-2222-3333-444444444444"
    slug = "C--proj-full"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid)
    sub_rel = f"projects/{slug}/{uuid}/subagents/agent-abc123.jsonl"
    sub_meta_rel = f"projects/{slug}/{uuid}/subagents/agent-abc123.meta.json"
    tool_rel = f"projects/{slug}/{uuid}/tool-results/bx9k2.txt"
    remote_rel = f"projects/{slug}/{uuid}/remote-agents/remote-agent-tk1.meta.json"
    _commit_file(mock_claude_dir, sub_rel, b'{"a":1}\n', "subagent")
    _commit_file(mock_claude_dir, sub_meta_rel, b'{"agentType":"explore"}\n', "submeta")
    _commit_file(mock_claude_dir, tool_rel, b'big tool output\n', "toolres")
    _commit_file(mock_claude_dir, remote_rel, b'{"sessionId":"abc"}\n', "remote")
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    paths = set(git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid))
    assert paths == {jsonl_rel, sub_rel, sub_meta_rel, tool_rel, remote_rel}


def test_git_ls_tree_for_uuid_includes_session_states(mock_claude_dir):
    """Logger state files (session-states/<uuid>.*) must be discovered."""
    uuid = "cccccccc-1111-2222-3333-444444444444"
    slug = "C--proj-logger"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid)
    state_rel = f"session-states/{uuid}.json"
    cache_rel = f"session-states/{uuid}.name-cache"
    src_rel = f"session-states/{uuid}.source"
    _commit_file(mock_claude_dir, state_rel, b'{"session_id":"c"}\n', "state")
    _commit_file(mock_claude_dir, cache_rel, b"name-here", "namecache")
    _commit_file(mock_claude_dir, src_rel, b"x", "source")
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    paths = set(git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid))
    assert paths == {jsonl_rel, state_rel, cache_rel, src_rel}


def test_git_ls_tree_for_uuid_includes_sesslogs_dir(mock_claude_dir):
    """Sesslog directory at sesslogs/<sanitized-name>__<uuid>_<user>/ must be discovered (recursive)."""
    uuid = "dddddddd-1111-2222-3333-444444444444"
    slug = "C--proj-sesslog"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid)
    log1_rel = f"sesslogs/MY-PROJECT__some-name__{uuid}_Alice/.sesslog_bash.log"
    log2_rel = f"sesslogs/MY-PROJECT__some-name__{uuid}_Alice/.shell_bash.log"
    overflow_rel = f"sesslogs/MY-PROJECT__some-name__{uuid}_Alice/.sesslog_bash.log.overflow.1"
    _commit_file(mock_claude_dir, log1_rel, b"log line 1\n", "log1")
    _commit_file(mock_claude_dir, log2_rel, b"shell line 1\n", "log2")
    _commit_file(mock_claude_dir, overflow_rel, b"overflow\n", "overflow")
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    paths = set(git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid))
    assert paths == {jsonl_rel, log1_rel, log2_rel, overflow_rel}


def test_git_ls_tree_for_uuid_excludes_sesslogs_bak_subdir(mock_claude_dir):
    """`sesslogs/bak/` (singular, as a sibling of per-session sesslog dirs)
    is NOT logger-managed -- verified 2026-06-03 against the claude-session-logger
    source. It's a user-maintained folder, outside csb-restore's scope.

    The logger DOES write `baks/` (plural) INSIDE per-session sesslog dirs
    for housekeeping. Those ARE in scope -- see the companion
    test_git_ls_tree_for_uuid_includes_per_session_baks_subdir test.
    """
    uuid = "ddbbddbb-1111-2222-3333-444444444444"
    slug = "C--proj-bak"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid)
    main_log_rel = f"sesslogs/MY-PROJ__main__{uuid}_Alice/.sesslog_bash.log"
    # User-managed bak/: parts[1] = "bak", UUID is in parts[2]. MUST NOT match.
    user_bak_log_rel = f"sesslogs/bak/MY-PROJ__main__{uuid}_Alice/.sesslog_bash.log.overflow.1"
    _commit_file(mock_claude_dir, main_log_rel, b"main\n", "main")
    _commit_file(mock_claude_dir, user_bak_log_rel, b"user-bak\n", "user-bak")
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    paths = set(git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid))
    assert paths == {jsonl_rel, main_log_rel}, (
        "sesslogs/bak/ should be excluded as a user-maintained folder: " + repr(paths)
    )


def test_git_ls_tree_for_uuid_includes_per_session_baks_subdir(mock_claude_dir):
    """Logger DOES write `<sesslog-dir>/baks/` (plural) for housekeeping
    recovery (file_io.py:408). Those nested files MUST be matched -- they're
    inside the per-session sesslog dir whose name contains the UUID."""
    uuid = "ccddccdd-1111-2222-3333-444444444444"
    slug = "C--proj-perbaks"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid)
    main_log_rel = f"sesslogs/MY-PROJ__main__{uuid}_Alice/.sesslog_bash.log"
    # Logger-managed baks/ inside per-session dir
    nested_bak_rel = f"sesslogs/MY-PROJ__main__{uuid}_Alice/baks/old-name__abc.log"
    _commit_file(mock_claude_dir, main_log_rel, b"main\n", "main")
    _commit_file(mock_claude_dir, nested_bak_rel, b"old\n", "nested-bak")
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    paths = set(git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid))
    assert paths == {jsonl_rel, main_log_rel, nested_bak_rel}, (
        "Per-session baks/ must be matched (it's under the UUID-keyed dir): " + repr(paths)
    )


def test_git_ls_tree_for_uuid_excludes_ephemeral_paths(mock_claude_dir):
    """Paths classified EPHEMERAL by v0.3.13 (whitebox-verified against
    Claude Code source) MUST NOT appear in the result. The whitelist of
    EPHEMERAL categories under ~/.claude/ is:

      - debug/<uuid>.txt          -- only read with --debug-file
      - telemetry/...<uuid>.json  -- retry queue, no resume read
      - todos/<uuid>-agent-*.json -- legacy v1; resume reads from JSONL

    Categories that USED TO BE deferred-EPHEMERAL in v0.3.12 but are now
    SESSION-HISTORY in v0.3.13 (file-history, tasks, session-env) are
    covered by their own dedicated inclusion tests below; this test
    exists to ensure we never *accidentally* restore the categories that
    are genuinely ephemeral.
    """
    uuid = "eeeeeeee-1111-2222-3333-444444444444"
    slug = "C--proj-noise"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid)
    debug_rel = f"debug/{uuid}.txt"
    telem_rel = f"telemetry/1p_failed_events.{uuid}.abc.json"
    todos_rel = f"todos/{uuid}-agent-{uuid}.json"
    for rel, body in [
        (debug_rel, b"debug log\n"),
        (telem_rel, b'{"event":"x"}\n'),
        (todos_rel, b"[]\n"),
    ]:
        _commit_file(mock_claude_dir, rel, body, "add " + rel)
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    paths = set(git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid))
    assert paths == {jsonl_rel}, "ephemeral paths leaked: " + repr(paths)


def test_git_ls_tree_for_uuid_includes_file_history(mock_claude_dir):
    """v0.3.13: file-history/<uuid>/* is SESSION-HISTORY -- Claude Code's
    /undo feature reads from it on resume (whitebox: fileHistory.ts:733-741).
    Without these, /undo on a recovered session fails at restoreBackup().
    """
    uuid = "11ee11ee-1111-2222-3333-444444444444"
    slug = "C--proj-fhist"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid)
    fhist1 = f"file-history/{uuid}/032ce4a81e82662a@v1"
    fhist2 = f"file-history/{uuid}/032ce4a81e82662a@v2"
    fhist3 = f"file-history/{uuid}/aabbccdd11223344@v1"
    for rel, body in [
        (fhist1, b"snapshot v1\n"),
        (fhist2, b"snapshot v2\n"),
        (fhist3, b"snapshot other\n"),
    ]:
        _commit_file(mock_claude_dir, rel, body, "fhist " + rel)
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    paths = set(git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid))
    assert paths == {jsonl_rel, fhist1, fhist2, fhist3}


def test_git_ls_tree_for_uuid_includes_tasks(mock_claude_dir):
    """v0.3.13: tasks/<uuid>/* is SESSION-HISTORY (when task v2 enabled) --
    Claude Code reads tasks directly from disk on resume in that mode
    (whitebox: tasks.ts:221-227, sessionRestore.ts:55). Without these the
    task list silently regenerates empty + ID counter resets.
    """
    uuid = "22ee22ee-1111-2222-3333-444444444444"
    slug = "C--proj-tasks"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid)
    task1 = f"tasks/{uuid}/1.json"
    task2 = f"tasks/{uuid}/2.json"
    hwm = f"tasks/{uuid}/.highwatermark"
    for rel, body in [
        (task1, b'{"task":1}\n'),
        (task2, b'{"task":2}\n'),
        (hwm, b"3"),
    ]:
        _commit_file(mock_claude_dir, rel, body, "tasks " + rel)
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    paths = set(git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid))
    assert paths == {jsonl_rel, task1, task2, hwm}


def test_git_ls_tree_for_uuid_includes_session_env(mock_claude_dir):
    """v0.3.13: session-env/<uuid>/* is SESSION-HISTORY -- read by Claude
    Code's shell-execution path on every subshell to restore venv/conda
    activation (whitebox: sessionEnvironment.ts:15-23). Without these the
    resumed session loses its shell environment state.
    """
    uuid = "33ee33ee-1111-2222-3333-444444444444"
    slug = "C--proj-senv"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid)
    env1 = f"session-env/{uuid}/SessionStart-hook-1.sh"
    env2 = f"session-env/{uuid}/SetupCwdChange-hook-2.sh"
    for rel, body in [
        (env1, b"export VENV=/path/to/venv\n"),
        (env2, b"cd /new/cwd\n"),
    ]:
        _commit_file(mock_claude_dir, rel, body, "senv " + rel)
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    paths = set(git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid))
    assert paths == {jsonl_rel, env1, env2}


def test_categorize_path_for_uuid_returns_correct_labels(mock_claude_dir):
    """The categorize helper should return the correct ScopeSpec label for
    each path category, and None for paths outside the SESSION-HISTORY scope.

    Drives `csb restore`'s summary output AND the table-driven discovery
    invariant: every path returned by git_ls_tree_for_uuid must categorize
    to a non-None label (round-trip property).
    """
    from claude_session_backup.git_ops import categorize_path_for_uuid
    uuid = "44ee44ee-1111-2222-3333-444444444444"
    slug = "C--proj-cat"

    in_scope = [
        (f"projects/{slug}/{uuid}.jsonl", "main transcript"),
        (f"projects/{slug}/{uuid}/subagents/agent-x.jsonl", "session subtree"),
        (f"projects/{slug}/{uuid}/tool-results/x.txt", "session subtree"),
        (f"projects/{slug}/{uuid}/remote-agents/x.meta.json", "session subtree"),
        (f"session-states/{uuid}.json", "session-states (logger)"),
        (f"session-states/{uuid}.name-cache", "session-states (logger)"),
        (f"session-states/{uuid}.run", "session-states (logger)"),
        (f"sesslogs/MY__name__{uuid}_Alice/.sesslog.log", "sesslogs (logger)"),
        (f"file-history/{uuid}/abc@v1", "file-history (Claude Code /undo)"),
        (f"tasks/{uuid}/1.json", "tasks (Claude Code task v2)"),
        (f"session-env/{uuid}/SessionStart-hook-1.sh", "session-env (Claude Code shell env)"),
    ]
    for path, expected_label in in_scope:
        actual = categorize_path_for_uuid(path, slug, uuid)
        assert actual == expected_label, (
            "category mismatch for " + path +
            ": expected " + repr(expected_label) + " got " + repr(actual)
        )

    out_of_scope = [
        f"debug/{uuid}.txt",
        f"telemetry/1p_failed_events.{uuid}.abc.json",
        f"todos/{uuid}-agent-{uuid}.json",
        f"sesslogs/bak/MY__name__{uuid}_Alice/.log",
        f"projects/other-slug/{uuid}.jsonl",  # wrong slug
        f"projects/{slug}/different-uuid.jsonl",  # wrong uuid
    ]
    for path in out_of_scope:
        assert categorize_path_for_uuid(path, slug, uuid) is None, (
            "out-of-scope path miscategorized: " + path
        )


def test_git_ls_tree_for_uuid_does_not_match_other_uuid(mock_claude_dir):
    """A second session committed under the same slug must not bleed into the
    first session's results."""
    uuid_a = "ffffffff-1111-2222-3333-444444444444"
    uuid_b = "ffffffff-9999-8888-7777-666666666666"
    slug = "C--proj-shared"
    rel_a = _make_session_jsonl(mock_claude_dir, slug, uuid_a)
    rel_b = _make_session_jsonl(mock_claude_dir, slug, uuid_b,
                                content=b'{"b":1}\n', message="add b")
    sub_b_rel = "projects/" + slug + "/" + uuid_b + "/subagents/agent-zzz.jsonl"
    _commit_file(mock_claude_dir, sub_b_rel, b'{"agent":"b"}\n', "sub b")
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    paths_a = set(git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid_a))
    paths_b = set(git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid_b))
    assert paths_a == {rel_a}
    assert paths_b == {rel_b, sub_b_rel}


def test_git_ls_tree_for_uuid_full_isolation_across_all_pathspec_scopes(mock_claude_dir):
    """Strong adversarial isolation: at a single commit, set up files for
    THREE UUIDs across all four SESSION-HISTORY categories (jsonl + subtree,
    session-states, sesslogs) AND two slugs. Then verify that requesting
    each UUID returns ONLY that UUID's files -- no leakage from siblings.

    This is the load-bearing safety test for "csb restore won't bring back
    unrelated files just because they're in the same commit."
    """
    target = "aaaaaaaa-1234-5678-9abc-deffacedface"
    other1 = "bbbbbbbb-1234-5678-9abc-deffacedface"
    other2 = "cccccccc-1234-5678-9abc-deffacedface"
    slug_target = "C--proj-target"
    slug_other = "C--proj-other"

    # TARGET: everything we want restored
    target_jsonl = f"projects/{slug_target}/{target}.jsonl"
    target_sub = f"projects/{slug_target}/{target}/subagents/agent-tgt.jsonl"
    target_tool = f"projects/{slug_target}/{target}/tool-results/btgt.txt"
    target_state = f"session-states/{target}.json"
    target_cache = f"session-states/{target}.name-cache"
    target_sesslog = f"sesslogs/PROJ__name__{target}_Alice/.sesslog_bash.log"

    # OTHER1: same slug as target, different UUID, full footprint
    other1_jsonl = f"projects/{slug_target}/{other1}.jsonl"
    other1_sub = f"projects/{slug_target}/{other1}/subagents/agent-other1.jsonl"
    other1_state = f"session-states/{other1}.json"
    other1_sesslog = f"sesslogs/PROJ__name__{other1}_Bob/.sesslog_bash.log"

    # OTHER2: different slug, different UUID, full footprint
    other2_jsonl = f"projects/{slug_other}/{other2}.jsonl"
    other2_sub = f"projects/{slug_other}/{other2}/subagents/agent-other2.jsonl"
    other2_state = f"session-states/{other2}.json"
    other2_cache = f"session-states/{other2}.name-cache"
    other2_sesslog = f"sesslogs/OTHERPROJ__name__{other2}_Carol/.sesslog_bash.log"

    all_files = [
        # TARGET
        (target_jsonl, b'{"target-jsonl":true}\n'),
        (target_sub, b'{"target-sub":true}\n'),
        (target_tool, b"target tool result\n"),
        (target_state, b'{"session_id":"target"}\n'),
        (target_cache, b"target-name"),
        (target_sesslog, b"target log\n"),
        # OTHER1
        (other1_jsonl, b'{"other1":true}\n'),
        (other1_sub, b'{"other1-sub":true}\n'),
        (other1_state, b'{"session_id":"other1"}\n'),
        (other1_sesslog, b"other1 log\n"),
        # OTHER2
        (other2_jsonl, b'{"other2":true}\n'),
        (other2_sub, b'{"other2-sub":true}\n'),
        (other2_state, b'{"session_id":"other2"}\n'),
        (other2_cache, b"other2-name"),
        (other2_sesslog, b"other2 log\n"),
    ]
    for rel, body in all_files:
        _commit_file(mock_claude_dir, rel, body, "add " + rel)
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    # Resolve TARGET -- must get exactly its 6 files, nothing else
    target_paths = set(git_ls_tree_for_uuid(
        str(mock_claude_dir), commit, slug_target, target
    ))
    expected_target = {
        target_jsonl, target_sub, target_tool,
        target_state, target_cache, target_sesslog,
    }
    assert target_paths == expected_target, (
        "TARGET leak/miss: extra=" + repr(target_paths - expected_target) +
        " missing=" + repr(expected_target - target_paths)
    )

    # Resolve OTHER1 -- must get exactly its 4 files, nothing from target/other2
    other1_paths = set(git_ls_tree_for_uuid(
        str(mock_claude_dir), commit, slug_target, other1
    ))
    expected_other1 = {other1_jsonl, other1_sub, other1_state, other1_sesslog}
    assert other1_paths == expected_other1

    # Resolve OTHER2 -- must get exactly its 5 files, in its own slug only
    other2_paths = set(git_ls_tree_for_uuid(
        str(mock_claude_dir), commit, slug_other, other2
    ))
    expected_other2 = {
        other2_jsonl, other2_sub, other2_state, other2_cache, other2_sesslog
    }
    assert other2_paths == expected_other2

    # Cross-check: NO file is in more than one result set (zero overlap)
    assert not (target_paths & other1_paths), "target and other1 overlap"
    assert not (target_paths & other2_paths), "target and other2 overlap"
    assert not (other1_paths & other2_paths), "other1 and other2 overlap"


def test_git_ls_tree_for_uuid_does_not_match_uuid_inside_filename(mock_claude_dir):
    """Adversarial: a file whose NAME contains the UUID-as-substring but whose
    PATH structure doesn't fit any SESSION-HISTORY pattern MUST NOT be matched.

    Examples: a debug log named after the UUID, a backup with the UUID in
    the suffix, a foreign tool's file that happens to embed the UUID.
    """
    uuid = "deadbeef-1234-5678-9abc-deffacedface"
    slug = "C--proj-adv"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid)

    # Adversarial paths that contain the UUID as substring but are NOT
    # SESSION-HISTORY:
    foreign = [
        # UUID as substring in a different session-states key (should not match)
        # session_state_prefix = "session-states/<uuid>." -- the trailing dot
        # matters; an "<uuid>_backup.json" entry must NOT match.
        f"session-states/{uuid}_backup.json",
        # UUID in a file at a deeper-than-expected projects/ depth
        f"projects/{slug}/sub/{uuid}.jsonl",  # 3-component path under slug, not 2
        # UUID in a sesslog filename but the parent dir name doesn't contain it
        f"sesslogs/unrelated-dir/some-{uuid}-suffix.log",
        # UUID in a totally unrelated top-level (debug-like; we don't scope this)
        # not testable here -- debug/ isn't in any pathspec so it can't even be seen
    ]
    for rel in foreign:
        _commit_file(mock_claude_dir, rel, b"adversarial\n", "add " + rel)
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    paths = set(git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid))
    # The jsonl is legitimate -- it's the only thing that should match.
    assert paths == {jsonl_rel}, (
        "adversarial UUID-substring paths leaked into results: " +
        repr(paths - {jsonl_rel})
    )


def test_git_ls_tree_for_uuid_returns_state_at_specified_commit(mock_claude_dir):
    """Specifying an earlier commit returns that commit's tree, not HEAD's.
    Validates the recover-from-deletion use case."""
    uuid = "abababab-1111-2222-3333-444444444444"
    slug = "C--proj-time"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid)
    sub_rel = "projects/" + slug + "/" + uuid + "/subagents/agent-time.jsonl"
    _commit_file(mock_claude_dir, sub_rel, b'{"agent":"t"}\n', "add sub")
    pre_delete_commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    (mock_claude_dir / jsonl_rel).unlink()
    (mock_claude_dir / sub_rel).unlink()
    _git(mock_claude_dir, "add", "-A")
    _git(mock_claude_dir, "commit", "--no-gpg-sign", "-m", "delete session")
    head_commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()

    paths_at_head = git_ls_tree_for_uuid(str(mock_claude_dir), head_commit, slug, uuid)
    assert paths_at_head == []

    paths_at_pre = set(git_ls_tree_for_uuid(
        str(mock_claude_dir), pre_delete_commit, slug, uuid
    ))
    assert paths_at_pre == {jsonl_rel, sub_rel}


def test_git_ls_tree_for_uuid_unknown_commit_returns_empty(mock_claude_dir):
    """A bogus commit-ish must return empty list, not raise."""
    uuid = "bcbcbcbc-1111-2222-3333-444444444444"
    slug = "C--proj-bogus"
    _make_session_jsonl(mock_claude_dir, slug, uuid)
    paths = git_ls_tree_for_uuid(str(mock_claude_dir), "deadbeef" * 5, slug, uuid)
    assert paths == []


def test_git_ls_tree_for_uuid_empty_uuid_returns_empty(mock_claude_dir):
    assert git_ls_tree_for_uuid(str(mock_claude_dir), "HEAD", "C--proj", "") == []


def test_git_ls_tree_for_uuid_empty_slug_returns_empty(mock_claude_dir):
    """Empty slug means we can't construct the projects/<slug>/ pathspec safely."""
    uuid = "cdcdcdcd-1111-2222-3333-444444444444"
    _make_session_jsonl(mock_claude_dir, "some-slug", uuid)
    assert git_ls_tree_for_uuid(str(mock_claude_dir), "HEAD", "", uuid) == []


# ── symlink-clobber fix (v0.3.15 -- DWP 2026-06-10) ─────────────────────

def test_git_ls_tree_symlinks_for_uuid_detects_symlink(mock_claude_dir):
    """The discovery helper must identify git symlink entries (mode 120000)
    in the SESSION-HISTORY scope. This is the signal that drives the
    skip-don't-restore policy. Pure git-tree test -- no filesystem symlink
    privilege needed (uses the cacheinfo fixture trick)."""
    uuid = "5117ec00-1111-2222-3333-444444444444"
    slug = "C--proj-symlink"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid)
    # The logger writes sesslogs/<dir>/transcript.jsonl as a SYMLINK whose
    # target is the projects JSONL -- this is the exact incident shape.
    link_rel = f"sesslogs/PROJ__name__{uuid}_Alice/transcript.jsonl"
    target = f"/c/Users/x/.claude/projects/{slug}/{uuid}.jsonl"
    # also a normal log file so the dir is a real session sesslog dir
    log_rel = f"sesslogs/PROJ__name__{uuid}_Alice/.sesslog_bash.log"
    _commit_file(mock_claude_dir, log_rel, b"shell log\n", "log")
    commit = _commit_symlink(mock_claude_dir, link_rel, target, "transcript symlink")

    symlinks = git_ls_tree_symlinks_for_uuid(str(mock_claude_dir), commit, slug, uuid)
    assert link_rel in symlinks, f"symlink not detected: {symlinks}"
    # the regular files must NOT be flagged as symlinks
    assert jsonl_rel not in symlinks
    assert log_rel not in symlinks


def test_git_ls_tree_for_uuid_still_lists_symlink_path(mock_claude_dir):
    """Back-compat: git_ls_tree_for_uuid keeps returning ALL in-scope paths
    (including symlinks) as a flat list. The skip decision happens in the
    restore layer, not here -- this function stays a pure enumerator."""
    uuid = "5117ec01-1111-2222-3333-444444444444"
    slug = "C--proj-symlink2"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid)
    link_rel = f"sesslogs/PROJ__name__{uuid}_Alice/transcript.jsonl"
    commit = _commit_symlink(mock_claude_dir, link_rel,
                             f"projects/{slug}/{uuid}.jsonl", "tlink")

    paths = set(git_ls_tree_for_uuid(str(mock_claude_dir), commit, slug, uuid))
    assert jsonl_rel in paths
    assert link_rel in paths  # listed, even though restore will skip it


def test_restore_session_recreates_or_skips_transcript_symlink_never_writes_blob(
    mock_claude_dir, tmp_path
):
    """`_restore_session` must NEVER restore a symlink's BLOB content (that was
    the v0.3.15 clobber). For the transcript.jsonl symlink it instead RECREATES
    a real link (#38, v0.3.17) when the OS permits, or skips-and-reports when it
    doesn't. Either way the path is never a regular file holding the blob's
    target-path string, and the symlink is never in write_list."""
    from claude_session_backup.commands import _restore_session
    uuid = "5117ec02-1111-2222-3333-444444444444"
    slug = "C--proj-symlink3"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid,
                                    content=b'{"real":"transcript"}\n')
    log_rel = f"sesslogs/PROJ__name__{uuid}_Alice/.sesslog_bash.log"
    _commit_file(mock_claude_dir, log_rel, b"shell log\n", "log")
    link_rel = f"sesslogs/PROJ__name__{uuid}_Alice/transcript.jsonl"
    commit = _commit_symlink(mock_claude_dir, link_rel,
                             f"projects/{slug}/{uuid}.jsonl", "tlink")
    for rel in (jsonl_rel, log_rel, link_rel):
        full = mock_claude_dir / rel
        if full.is_symlink() or full.exists():
            full.unlink()

    result = _restore_session(
        claude_dir=str(mock_claude_dir),
        full_uuid=uuid,
        jsonl_path=jsonl_rel,
        commit=commit,
    )
    assert result is not None
    assert link_rel not in result.write_list  # never byte-restored
    # It's handled exactly one way: recreated OR skipped (privilege-dependent).
    assert link_rel in (result.recreated_symlinks + result.skipped_symlinks)
    link_full = mock_claude_dir / link_rel
    if link_rel in result.recreated_symlinks:
        # Recreated as a real symlink resolving to the restored transcript.
        assert link_full.is_symlink()
        assert link_full.resolve() == (mock_claude_dir / jsonl_rel).resolve()
    else:
        # Skipped: nothing materialized (NOT a regular file with the blob text).
        assert not link_full.exists() and not link_full.is_symlink()
    # The real files were restored regardless.
    assert (mock_claude_dir / jsonl_rel).read_bytes() == b'{"real":"transcript"}\n'
    assert (mock_claude_dir / log_rel).exists()


def test_is_transcript_symlink_recognizes_pattern():
    from claude_session_backup.commands import _is_transcript_symlink
    uuid = "abc12345-1111-2222-3333-444444444444"
    assert _is_transcript_symlink(f"sesslogs/PROJ__name__{uuid}_Alice/transcript.jsonl", uuid)
    # wrong basename
    assert not _is_transcript_symlink(f"sesslogs/PROJ__{uuid}_Alice/other.jsonl", uuid)
    # wrong depth (nested baks/)
    assert not _is_transcript_symlink(f"sesslogs/PROJ__{uuid}_Alice/baks/transcript.jsonl", uuid)
    # wrong uuid in dir
    assert not _is_transcript_symlink("sesslogs/PROJ__other-uuid_Alice/transcript.jsonl", uuid)
    # not under sesslogs
    assert not _is_transcript_symlink(f"projects/slug/{uuid}.jsonl", uuid)


def test_restore_session_falls_back_to_skip_when_symlink_creation_fails(
    mock_claude_dir, tmp_path, monkeypatch
):
    """When create_symlink returns False (e.g. Windows without symlink
    privilege), the transcript.jsonl link is SKIPPED -- never materialized as a
    regular file holding the blob's target-path string, never fails the restore."""
    from claude_session_backup import commands as cmds
    from claude_session_backup.commands import _restore_session
    uuid = "57c00001-1111-2222-3333-444444444444"
    slug = "C--proj-nopriv"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid, content=b'{"x":1}\n')
    link_rel = f"sesslogs/PROJ__name__{uuid}_Alice/transcript.jsonl"
    commit = _commit_symlink(mock_claude_dir, link_rel,
                             f"projects/{slug}/{uuid}.jsonl", "tlink")
    for rel in (jsonl_rel, link_rel):
        full = mock_claude_dir / rel
        if full.is_symlink() or full.exists():
            full.unlink()

    # Simulate "no symlink privilege": create_symlink returns False.
    monkeypatch.setattr("dazzle_filekit.create_symlink", lambda *a, **k: False)

    result = _restore_session(
        claude_dir=str(mock_claude_dir), full_uuid=uuid,
        jsonl_path=jsonl_rel, commit=commit,
    )
    assert link_rel in result.skipped_symlinks
    assert link_rel not in result.recreated_symlinks
    link_full = mock_claude_dir / link_rel
    # CRITICAL: no regular file (the v0.3.15 bug) and no link materialized.
    assert not link_full.exists() and not link_full.is_symlink()
    # Restore otherwise succeeded.
    assert (mock_claude_dir / jsonl_rel).read_bytes() == b'{"x":1}\n'


def test_restore_session_recreate_heals_blocking_regular_file(
    mock_claude_dir, tmp_path
):
    """A 107-byte-style regular file sitting where the transcript.jsonl symlink
    should be BLOCKS the logger from ever recreating the link. Restore must
    replace it with a real symlink (privileged) -- healing that dead state."""
    from claude_session_backup.commands import _restore_session
    if not _can_make_symlink(tmp_path):
        pytest.skip("symlink creation not permitted")
    uuid = "57c00002-1111-2222-3333-444444444444"
    slug = "C--proj-heal"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid, content=b'{"x":1}\n')
    link_rel = f"sesslogs/PROJ__name__{uuid}_Alice/transcript.jsonl"
    commit = _commit_symlink(mock_claude_dir, link_rel,
                             f"projects/{slug}/{uuid}.jsonl", "tlink")
    # Pre-place a blocking REGULAR FILE at the link path (the leftover stub).
    link_full = mock_claude_dir / link_rel
    link_full.parent.mkdir(parents=True, exist_ok=True)
    if link_full.is_symlink() or link_full.exists():
        link_full.unlink()
    link_full.write_bytes(b"C:/some/old/path/transcript.jsonl")  # blocking stub

    result = _restore_session(
        claude_dir=str(mock_claude_dir), full_uuid=uuid,
        jsonl_path=jsonl_rel, commit=commit,
    )
    assert link_rel in result.recreated_symlinks
    assert link_full.is_symlink()  # the stub was healed into a real link
    assert link_full.resolve() == (mock_claude_dir / jsonl_rel).resolve()


def test_restore_session_recreate_is_idempotent(mock_claude_dir, tmp_path):
    """If the correct transcript.jsonl symlink already exists, recreate is a
    no-op success (no churn)."""
    from claude_session_backup.commands import _restore_session
    if not _can_make_symlink(tmp_path):
        pytest.skip("symlink creation not permitted")
    uuid = "57c00003-1111-2222-3333-444444444444"
    slug = "C--proj-idem"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid, content=b'{"x":1}\n')
    link_rel = f"sesslogs/PROJ__name__{uuid}_Alice/transcript.jsonl"
    commit = _commit_symlink(mock_claude_dir, link_rel,
                             f"projects/{slug}/{uuid}.jsonl", "tlink")
    # Pre-create the CORRECT symlink already.
    link_full = mock_claude_dir / link_rel
    link_full.parent.mkdir(parents=True, exist_ok=True)
    if link_full.is_symlink() or link_full.exists():
        link_full.unlink()
    target_abs = (mock_claude_dir / jsonl_rel).resolve()
    os.symlink(target_abs, link_full)
    mtime_before = link_full.lstat().st_mtime

    result = _restore_session(
        claude_dir=str(mock_claude_dir), full_uuid=uuid,
        jsonl_path=jsonl_rel, commit=commit,
    )
    assert link_rel in result.recreated_symlinks
    assert link_full.is_symlink()
    assert link_full.resolve() == target_abs


def test_restore_session_non_transcript_symlink_recreated_verbatim(
    mock_claude_dir, tmp_path
):
    """#39: a non-transcript symlink in scope is recreated VERBATIM from its
    blob target text (recreated when privileged; skipped without privilege --
    NEVER materialized as a regular file holding the target path)."""
    from claude_session_backup.commands import _restore_session
    uuid = "57c00004-1111-2222-3333-444444444444"
    slug = "C--proj-other"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid, content=b'{"x":1}\n')
    # A symlink that is NOT transcript.jsonl, with a relative target that
    # exists on disk (so the link resolves after recreate).
    other_link = f"sesslogs/PROJ__name__{uuid}_Alice/weird-link.log"
    real_target = mock_claude_dir / f"sesslogs/PROJ__name__{uuid}_Alice/real.log"
    real_target.parent.mkdir(parents=True, exist_ok=True)
    real_target.write_bytes(b"real log content\n")
    commit = _commit_symlink(mock_claude_dir, other_link, "real.log", "olink")
    link_full = mock_claude_dir / other_link
    if link_full.is_symlink() or link_full.exists():
        link_full.unlink()

    result = _restore_session(
        claude_dir=str(mock_claude_dir), full_uuid=uuid,
        jsonl_path=jsonl_rel, commit=commit,
    )
    if _can_make_symlink(tmp_path):
        assert other_link in result.recreated_symlinks
        assert link_full.is_symlink()
        assert os.readlink(str(link_full)) == "real.log"
        # The link resolves to the real on-disk target's content.
        assert link_full.read_bytes() == b"real log content\n"
    else:
        assert other_link in result.skipped_symlinks
        # CRITICAL: never a regular file holding the target-path text.
        assert not link_full.exists() or link_full.is_symlink()


def test_restore_session_verbatim_recreate_dangling_target(
    mock_claude_dir, tmp_path
):
    """#39: an unrecognized symlink whose blob target doesn't exist on this
    machine is still recreated (dangling link -- harmless, and strictly
    better than nothing). Privilege-aware."""
    from claude_session_backup.commands import _restore_session
    if not _can_make_symlink(tmp_path):
        pytest.skip("symlink creation not permitted")
    uuid = "57c00005-1111-2222-3333-444444444444"
    slug = "C--proj-dangle"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid, content=b'{"x":1}\n')
    other_link = f"sesslogs/PROJ__name__{uuid}_Alice/foreign-link.log"
    foreign_target = "C:/nonexistent/foreign/machine/path.log"
    commit = _commit_symlink(mock_claude_dir, other_link, foreign_target, "flink")
    link_full = mock_claude_dir / other_link
    if link_full.is_symlink() or link_full.exists():
        link_full.unlink()

    result = _restore_session(
        claude_dir=str(mock_claude_dir), full_uuid=uuid,
        jsonl_path=jsonl_rel, commit=commit,
    )
    assert other_link in result.recreated_symlinks
    assert link_full.is_symlink()
    # Windows os.readlink returns absolute targets in extended-length form
    # (\\?\C:\...); strip the prefix and compare Path-normalized.
    raw = os.readlink(str(link_full))
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    assert Path(raw) == Path(foreign_target)
    assert not link_full.exists()  # dangling: link present, target absent


def test_restore_session_verbatim_dir_symlink_inference(
    mock_claude_dir, tmp_path
):
    """#39: when the blob target is an existing DIRECTORY, recreation passes
    target_is_directory so Windows gets a proper directory symlink."""
    from claude_session_backup.commands import _restore_session
    if not _can_make_symlink(tmp_path):
        pytest.skip("symlink creation not permitted")
    uuid = "57c00006-1111-2222-3333-444444444444"
    slug = "C--proj-dirlink"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid, content=b'{"x":1}\n')
    dir_link = f"sesslogs/PROJ__name__{uuid}_Alice/data-dir"
    real_dir = mock_claude_dir / f"sesslogs/PROJ__name__{uuid}_Alice/actual-dir"
    real_dir.mkdir(parents=True, exist_ok=True)
    (real_dir / "inside.txt").write_bytes(b"inside\n")
    commit = _commit_symlink(mock_claude_dir, dir_link, "actual-dir", "dlink")
    link_full = mock_claude_dir / dir_link
    if link_full.is_symlink() or link_full.exists():
        link_full.unlink()

    result = _restore_session(
        claude_dir=str(mock_claude_dir), full_uuid=uuid,
        jsonl_path=jsonl_rel, commit=commit,
    )
    assert dir_link in result.recreated_symlinks
    assert link_full.is_symlink()
    # Resolves as a directory; contents reachable through the link.
    assert (link_full / "inside.txt").read_bytes() == b"inside\n"


def test_restore_session_verbatim_falls_back_when_create_fails(
    mock_claude_dir, tmp_path, monkeypatch
):
    """#39: when create_symlink returns False for an unrecognized link, it is
    skipped-and-reported -- never materialized as a regular file (the v0.3.15
    clobber class stays closed for the verbatim path too)."""
    from claude_session_backup.commands import _restore_session
    uuid = "57c00007-1111-2222-3333-444444444444"
    slug = "C--proj-vfail"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid, content=b'{"x":1}\n')
    other_link = f"sesslogs/PROJ__name__{uuid}_Alice/cant-make.log"
    commit = _commit_symlink(mock_claude_dir, other_link, "/somewhere/else.log", "clink")
    link_full = mock_claude_dir / other_link
    if link_full.is_symlink() or link_full.exists():
        link_full.unlink()

    monkeypatch.setattr("dazzle_filekit.create_symlink", lambda *a, **k: False)

    result = _restore_session(
        claude_dir=str(mock_claude_dir), full_uuid=uuid,
        jsonl_path=jsonl_rel, commit=commit,
    )
    assert other_link in result.skipped_symlinks
    assert other_link not in result.recreated_symlinks
    assert not link_full.exists() and not link_full.is_symlink()
    # Restore otherwise succeeded.
    assert (mock_claude_dir / jsonl_rel).read_bytes() == b'{"x":1}\n'


def test_git_restore_file_does_not_write_through_symlink(mock_claude_dir, tmp_path):
    """Defense-in-depth (S1c): even if a symlink reaches git_restore_file, it
    must NOT write through the link onto the target. It removes the link and
    writes a regular file at the path instead. Needs a real on-disk symlink."""
    if not _can_make_symlink(tmp_path):
        pytest.skip("filesystem symlink creation not permitted on this OS/user")
    # The 'target' is a precious file we must NOT clobber
    target = tmp_path / "precious_transcript.jsonl"
    target.write_bytes(b"PRECIOUS 2MB-analog real transcript content\n")
    # 'dest' is a symlink pointing at the precious target
    dest = tmp_path / "link_to_precious.jsonl"
    os.symlink(target, dest)
    # Commit some small "garbage" blob to restore through the link
    commit = _commit_file(mock_claude_dir, "projects/x/garbage.jsonl",
                          b"garbage 111\n", "garbage")

    ok = git_restore_file(str(mock_claude_dir), commit,
                          "projects/x/garbage.jsonl", str(dest))
    assert ok
    # The precious target MUST be untouched
    assert target.read_bytes() == b"PRECIOUS 2MB-analog real transcript content\n", (
        "write-guard failed -- wrote THROUGH the symlink and clobbered the target"
    )
    # dest is now a regular file with the garbage (link was removed first)
    assert not dest.is_symlink()
    assert dest.read_bytes() == b"garbage 111\n"


def test_restore_session_full_does_not_clobber_via_dangling_symlink(
    mock_claude_dir, tmp_path
):
    """The EXACT incident (b6a4929f): restore a session whose scope contains
    the real 2MB-analog transcript AND a transcript.jsonl symlink pointing at
    it, with the symlink present-but-dangling on disk (target purged). The
    restore must end with the real transcript intact, NOT clobbered by the
    symlink's 111-byte target-path content.

    Needs a real on-disk symlink to reproduce the write-through path."""
    from claude_session_backup.commands import _restore_session
    if not _can_make_symlink(tmp_path):
        pytest.skip("filesystem symlink creation not permitted on this OS/user")
    uuid = "5117ec03-1111-2222-3333-444444444444"
    slug = "C--proj-clobber"
    real_content = b'{"event":1}\n' * 500  # multi-line "big" transcript analog
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid, content=real_content)
    log_rel = f"sesslogs/PROJ__name__{uuid}_Alice/.sesslog_bash.log"
    _commit_file(mock_claude_dir, log_rel, b"shell\n", "log")
    link_rel = f"sesslogs/PROJ__name__{uuid}_Alice/transcript.jsonl"
    # the symlink blob content is the (claude-dir-relative) path of the jsonl
    link_target_text = str((mock_claude_dir / jsonl_rel))
    commit = _commit_symlink(mock_claude_dir, link_rel, link_target_text, "tlink")

    # Simulate the purge: jsonl removed from disk; the on-disk symlink remains
    # (dangling). This is the real-world pruned-session shape.
    (mock_claude_dir / jsonl_rel).unlink()
    link_full = mock_claude_dir / link_rel
    link_full.parent.mkdir(parents=True, exist_ok=True)
    if link_full.is_symlink() or link_full.exists():
        link_full.unlink()
    os.symlink(mock_claude_dir / jsonl_rel, link_full)  # dangling (target gone)

    result = _restore_session(
        claude_dir=str(mock_claude_dir),
        full_uuid=uuid,
        jsonl_path=jsonl_rel,
        commit=commit,
    )
    assert result is not None
    # THE load-bearing assertion: the real transcript is intact, not 111-byte garbage
    assert (mock_claude_dir / jsonl_rel).read_bytes() == real_content, (
        "restore clobbered the transcript through the dangling symlink"
    )
    # v0.3.17: the transcript.jsonl symlink is recreated (privileged) or
    # skipped (unprivileged) -- never byte-restored, never clobbering.
    assert link_rel in (result.recreated_symlinks + result.skipped_symlinks)
    # If recreated, the link must point at the (intact) restored transcript.
    link_full = mock_claude_dir / link_rel
    if link_rel in result.recreated_symlinks:
        assert link_full.is_symlink()
        assert link_full.resolve() == (mock_claude_dir / jsonl_rel).resolve()


# ── cmd_restore full-restore (v0.3.12 -- #32 + #33) ─────────────────────

def _setup_full_session(claude_dir, slug, uuid, with_logger=True):
    """Commit a session with subagents, tool-results, and optionally logger files.
    Then delete from disk to simulate a purged session. Returns dict of relative
    paths -> expected bytes."""
    paths = {
        f"projects/{slug}/{uuid}.jsonl": b'{"transcript":true}\n',
        f"projects/{slug}/{uuid}/subagents/agent-fa1.jsonl": b'{"agent":"fa1"}\n',
        f"projects/{slug}/{uuid}/subagents/agent-fa1.meta.json": b'{"agentType":"explore"}\n',
        f"projects/{slug}/{uuid}/tool-results/bzx9.txt": b"large tool output\n",
    }
    if with_logger:
        paths.update({
            f"session-states/{uuid}.json": b'{"session_id":"x"}\n',
            f"session-states/{uuid}.name-cache": b"session-name",
            f"sesslogs/PROJ__session-name__" + uuid + "_Alice/.sesslog_bash.log": b"tool call 1\n",
            f"sesslogs/PROJ__session-name__" + uuid + "_Alice/.shell_bash.log": b"$ ls\n",
        })
    for rel, body in paths.items():
        _commit_file(claude_dir, rel, body, "add " + rel)
    # Now delete all the files to simulate purge (commit the deletion too)
    for rel in paths:
        full = claude_dir / rel
        if full.exists():
            full.unlink()
    _git(claude_dir, "add", "-A")
    _git(claude_dir, "commit", "--no-gpg-sign", "-m", "purge session")
    return paths


def _populate_db_with_session(db_path, slug, uuid, jsonl_path, deleted_at="2026-03-24T00:00:00Z"):
    """Seed the SQLite DB with a session row marked deleted."""
    from claude_session_backup.index import open_db, init_schema, upsert_session, mark_deleted
    from claude_session_backup.metadata import SessionMetadata
    conn = open_db(str(db_path))
    init_schema(conn)
    meta = SessionMetadata(
        session_id=uuid,
        session_name="session-name",
        project=slug,
        start_folder="C:\\code\\test",
        started_at="2026-03-23T10:00:00Z",
        last_active_at="2026-03-23T10:30:00Z",
        message_count=10,
        tool_call_count=2,
        claude_version="2.1.81",
        folder_usage={"C:\\code\\test": 5},
    )
    # NOTE: positional arg 5 is jsonl_mtime (float); scanned_at is keyword.
    # (This fixture used to pass the ISO string into jsonl_mtime by mistake,
    # which #40's numeric consumption surfaced.)
    upsert_session(conn, meta, jsonl_path, 100, 0.0,
                   scanned_at="2026-03-23T10:30:00Z")
    if deleted_at:
        mark_deleted(conn, uuid, deleted_at)
    conn.close()


def test_cmd_restore_default_restores_full_session_subtree(mock_claude_dir, tmp_path, capsys):
    """Default restore brings back EVERYTHING in the SESSION-HISTORY scope:
    the JSONL, subagents/, tool-results/, session-states/, sesslogs/."""
    from claude_session_backup.commands import cmd_restore
    slug = "C--code-fullsess"
    uuid = "11111111-aaaa-bbbb-cccc-222222222222"
    expected = _setup_full_session(mock_claude_dir, slug, uuid, with_logger=True)
    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    fresh_db = tmp_path / "fresh.db"
    _populate_db_with_session(fresh_db, slug, uuid, jsonl_path)

    args = _make_args_namespace(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
    )
    rc = cmd_restore(args)
    captured = capsys.readouterr()
    assert rc == 0, "stderr: " + captured.err

    # Every file we committed should now be back on disk byte-for-byte
    for rel, body in expected.items():
        full = mock_claude_dir / rel
        assert full.exists(), "missing after restore: " + rel + "\nstdout: " + captured.out
        assert full.read_bytes() == body, "bytes mismatch: " + rel


def test_cmd_restore_jsonl_only_preserves_v0311_behavior(mock_claude_dir, tmp_path):
    """--jsonl-only restores only the main transcript, leaving sidecars in git."""
    from claude_session_backup.commands import cmd_restore
    slug = "C--code-jsonlonly"
    uuid = "22222222-aaaa-bbbb-cccc-333333333333"
    expected = _setup_full_session(mock_claude_dir, slug, uuid, with_logger=True)
    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    fresh_db = tmp_path / "fresh.db"
    _populate_db_with_session(fresh_db, slug, uuid, jsonl_path)

    args = _make_args_namespace(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
        jsonl_only=True,
    )
    rc = cmd_restore(args)
    assert rc == 0

    # ONLY the jsonl should be back
    assert (mock_claude_dir / jsonl_path).exists()
    for rel in expected:
        if rel == jsonl_path:
            continue
        assert not (mock_claude_dir / rel).exists(), "should not be restored under --jsonl-only: " + rel


def test_cmd_restore_no_logger_files_silently_no_op_for_those(mock_claude_dir, tmp_path):
    """When the session was committed WITHOUT logger files, restore brings
    back the Claude Code subtree only. No error, no warning, just absence."""
    from claude_session_backup.commands import cmd_restore
    slug = "C--code-nologgger"
    uuid = "33333333-aaaa-bbbb-cccc-444444444444"
    expected = _setup_full_session(mock_claude_dir, slug, uuid, with_logger=False)
    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    fresh_db = tmp_path / "fresh.db"
    _populate_db_with_session(fresh_db, slug, uuid, jsonl_path)

    args = _make_args_namespace(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
    )
    rc = cmd_restore(args)
    assert rc == 0

    for rel in expected:
        assert (mock_claude_dir / rel).exists(), "missing: " + rel
    # No logger files were committed; none restored
    assert not (mock_claude_dir / f"session-states/{uuid}.json").exists()
    assert not list((mock_claude_dir / "sesslogs").glob("*" + uuid + "*")) if (mock_claude_dir / "sesslogs").exists() else True


def test_cmd_restore_preserves_present_files_restores_missing(mock_claude_dir, tmp_path, capsys):
    """v0.3.12 policy: default restore is 'preserve present, restore missing'.

    If some files exist on disk (alive or partially-recovered session) and
    some are missing, restore brings back only the missing ones and leaves
    the present ones byte-untouched -- even if their content differs from
    git. This is the safe default: never clobber what's on disk.
    """
    from claude_session_backup.commands import cmd_restore
    slug = "C--code-partial"
    uuid = "44444444-aaaa-bbbb-cccc-555555555555"
    expected = _setup_full_session(mock_claude_dir, slug, uuid, with_logger=True)
    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    # Simulate: the JSONL is back (e.g. from a prior restore), and one
    # sesslog file has NEWER content than git (logger kept writing after
    # the git snapshot). The session-states + subagents + other files
    # are still missing on disk.
    (mock_claude_dir / jsonl_path).parent.mkdir(parents=True, exist_ok=True)
    (mock_claude_dir / jsonl_path).write_bytes(b'{"local-newer":true}\n')
    sesslog_path = f"sesslogs/PROJ__session-name__{uuid}_Alice/.sesslog_bash.log"
    (mock_claude_dir / sesslog_path).parent.mkdir(parents=True, exist_ok=True)
    (mock_claude_dir / sesslog_path).write_bytes(b"tool call 1\nNEWER LINE the logger wrote after git snapshot\n")

    fresh_db = tmp_path / "fresh.db"
    _populate_db_with_session(fresh_db, slug, uuid, jsonl_path)

    args = _make_args_namespace(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
    )
    rc = cmd_restore(args)
    captured = capsys.readouterr()

    assert rc == 0, "restore should succeed (preserves present, restores missing); stderr=" + captured.err
    # JSONL present + different from git: PRESERVED. Local "newer" content kept.
    assert (mock_claude_dir / jsonl_path).read_bytes() == b'{"local-newer":true}\n', (
        "present file MUST NOT be overwritten by default"
    )
    # Sesslog present + has more data than git: PRESERVED.
    assert b"NEWER LINE" in (mock_claude_dir / sesslog_path).read_bytes(), (
        "local sesslog with more content MUST be preserved"
    )
    # All previously-missing files: restored byte-for-byte from git.
    for rel, body in expected.items():
        if rel in (jsonl_path, sesslog_path):
            continue
        assert (mock_claude_dir / rel).exists(), "missing file not restored: " + rel
        assert (mock_claude_dir / rel).read_bytes() == body, "bytes mismatch on restored: " + rel


def test_cmd_restore_force_overwrites_even_when_local_differs(mock_claude_dir, tmp_path):
    """--force explicitly opts into clobbering present files with git bytes.
    Use case: local file is corrupted / stale and user wants git's version."""
    from claude_session_backup.commands import cmd_restore
    slug = "C--code-forcediff"
    uuid = "44ff44ff-aaaa-bbbb-cccc-555555555555"
    expected = _setup_full_session(mock_claude_dir, slug, uuid, with_logger=False)
    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    (mock_claude_dir / jsonl_path).parent.mkdir(parents=True, exist_ok=True)
    (mock_claude_dir / jsonl_path).write_bytes(b'{"stale-or-corrupt":true}\n')
    fresh_db = tmp_path / "fresh.db"
    _populate_db_with_session(fresh_db, slug, uuid, jsonl_path)

    args = _make_args_namespace(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
        force=True,
    )
    rc = cmd_restore(args)
    assert rc == 0
    assert (mock_claude_dir / jsonl_path).read_bytes() == expected[jsonl_path], (
        "--force MUST overwrite local with git bytes"
    )


def test_cmd_restore_force_overwrites_existing_files(mock_claude_dir, tmp_path):
    """--force allows restore to proceed even when files exist on disk."""
    from claude_session_backup.commands import cmd_restore
    slug = "C--code-force"
    uuid = "55555555-aaaa-bbbb-cccc-666666666666"
    expected = _setup_full_session(mock_claude_dir, slug, uuid, with_logger=False)
    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    (mock_claude_dir / jsonl_path).write_bytes(b'{"stale":true}\n')
    fresh_db = tmp_path / "fresh.db"
    _populate_db_with_session(fresh_db, slug, uuid, jsonl_path, deleted_at=None)

    args = _make_args_namespace(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
        force=True,
    )
    rc = cmd_restore(args)
    assert rc == 0
    # The stale file should be overwritten with the git blob bytes
    assert (mock_claude_dir / jsonl_path).read_bytes() == expected[jsonl_path]


def test_cmd_restore_idempotent_on_deleted_session(mock_claude_dir, tmp_path):
    """Re-running restore on an already-restored deleted session is fine:
    we write the same bytes; no conflict (because session is deleted)."""
    from claude_session_backup.commands import cmd_restore
    slug = "C--code-idemp"
    uuid = "66666666-aaaa-bbbb-cccc-777777777777"
    expected = _setup_full_session(mock_claude_dir, slug, uuid, with_logger=False)
    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    fresh_db = tmp_path / "fresh.db"
    _populate_db_with_session(fresh_db, slug, uuid, jsonl_path)

    args = _make_args_namespace(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
    )
    rc1 = cmd_restore(args)
    rc2 = cmd_restore(args)
    assert rc1 == 0 and rc2 == 0
    for rel, body in expected.items():
        assert (mock_claude_dir / rel).read_bytes() == body


def test_cmd_restore_idempotent_no_op_touches_no_files(mock_claude_dir, tmp_path):
    """Strong 'no sneaky writes' assertion: when everything is present, the
    no-op restore must not touch ANY file's mtime, size, or bytes.

    Implementation strategy: do the restore, then pin every restored file's
    mtime to a specific past timestamp (2026-01-01). Run restore again
    (should be no-op). Verify every mtime is still pinned -- i.e. no write
    happened. Robust against filesystem timestamp resolution since we use
    an explicit past time well outside any subsecond fuzz window.
    """
    import os
    from claude_session_backup.commands import cmd_restore
    slug = "C--code-no-sneaky-writes"
    uuid = "deadbeef-1111-2222-3333-444444444444"
    expected = _setup_full_session(mock_claude_dir, slug, uuid, with_logger=True)
    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    fresh_db = tmp_path / "fresh.db"
    _populate_db_with_session(fresh_db, slug, uuid, jsonl_path)

    args = _make_args_namespace(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
    )
    # First call: restores everything
    assert cmd_restore(args) == 0

    # Pin every restored file's mtime to a known past timestamp
    pinned_atime = 1735689600  # 2025-01-01T00:00:00 UTC
    pinned_mtime = 1767225600  # 2026-01-01T00:00:00 UTC
    restored_paths = list(expected.keys())
    for rel in restored_paths:
        full = mock_claude_dir / rel
        assert full.exists(), "expected restored: " + rel
        os.utime(full, (pinned_atime, pinned_mtime))

    # Snapshot byte content + size + mtime BEFORE the second restore
    before = {}
    for rel in restored_paths:
        full = mock_claude_dir / rel
        st = full.stat()
        before[rel] = (st.st_mtime, st.st_size, full.read_bytes())

    # Second call: must be a no-op
    assert cmd_restore(args) == 0

    # Snapshot AFTER and assert nothing moved
    for rel in restored_paths:
        full = mock_claude_dir / rel
        st = full.stat()
        b_mtime, b_size, b_bytes = before[rel]
        assert st.st_mtime == b_mtime, (
            "mtime CHANGED on no-op restore: " + rel +
            " before=" + str(b_mtime) + " after=" + str(st.st_mtime)
        )
        assert st.st_size == b_size, "size changed on no-op restore: " + rel
        assert full.read_bytes() == b_bytes, "bytes changed on no-op restore: " + rel


def test_cmd_restore_dry_run_reports_full_count(mock_claude_dir, tmp_path, capsys):
    """--dry-run on full restore enumerates every path it would write,
    not just the JSONL."""
    from claude_session_backup.commands import cmd_restore
    slug = "C--code-dryrun"
    uuid = "77777777-aaaa-bbbb-cccc-888888888888"
    expected = _setup_full_session(mock_claude_dir, slug, uuid, with_logger=True)
    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    fresh_db = tmp_path / "fresh.db"
    _populate_db_with_session(fresh_db, slug, uuid, jsonl_path)

    args = _make_args_namespace(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(fresh_db),
        dry_run=True,
    )
    rc = cmd_restore(args)
    captured = capsys.readouterr()
    assert rc == 0
    # Every expected path should be mentioned in the dry-run output
    for rel in expected:
        assert rel in captured.out, "dry-run output missing path: " + rel + "\nGot:\n" + captured.out
    # And nothing should actually be written
    for rel in expected:
        assert not (mock_claude_dir / rel).exists(), "dry-run wrote: " + rel


# ── cmd_resume on pruned UUID (v0.3.14, #34) ────────────────────────────

def _setup_pruned_session_in_db(db_path, claude_dir, slug, uuid):
    """Commit a full session to git, delete its files, mark deleted in DB.
    Mirrors the lifecycle: session existed -> Claude Code purged -> DB
    knows it's pruned but git has the bytes."""
    expected = _setup_full_session(claude_dir, slug, uuid, with_logger=False)
    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    _populate_db_with_session(db_path, slug, uuid, jsonl_path,
                              deleted_at="2026-05-15T00:00:00Z")
    return expected, jsonl_path


def _make_resume_args(**kwargs):
    """Namespace mimicking argparse for cmd_resume (mirrors CLI flags)."""
    import argparse
    defaults = {
        "session_id": None,
        "quiet": False,
        "claude_dir": None,
        "db": None,
        "restore_pruned": False,
        "no_restore_pruned": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_cmd_resume_pruned_with_no_restore_flag_exits_with_hint(
    mock_claude_dir, tmp_path, capsys, monkeypatch
):
    """`csb resume <uuid> --no-restore-pruned` on a pruned session must
    exit 1 with a clear hint to use `csb restore` instead. No prompt,
    no restore."""
    from claude_session_backup.commands import cmd_resume
    slug = "C--code-pruned-norestore"
    uuid = "abcd1111-aaaa-bbbb-cccc-222222222222"
    _setup_pruned_session_in_db(tmp_path / "fresh.db", mock_claude_dir, slug, uuid)
    # Ensure we never accidentally launch a real claude subprocess
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: pytest.fail("should not invoke claude"))

    args = _make_resume_args(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(tmp_path / "fresh.db"),
        no_restore_pruned=True,
    )
    rc = cmd_resume(args)
    captured = capsys.readouterr()
    assert rc == 1
    combined = (captured.out + captured.err).lower()
    assert "pruned" in combined
    assert "csb restore" in combined  # hint to the user


def test_cmd_resume_pruned_non_tty_without_flag_exits_with_hint(
    mock_claude_dir, tmp_path, capsys, monkeypatch
):
    """Non-TTY (cron/script) without --restore-pruned or --no-restore must
    NOT hang on a prompt. Exit 1 with a hint to pass one of the flags."""
    from claude_session_backup.commands import cmd_resume
    slug = "C--code-pruned-notty"
    uuid = "abcd2222-aaaa-bbbb-cccc-333333333333"
    _setup_pruned_session_in_db(tmp_path / "fresh.db", mock_claude_dir, slug, uuid)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: pytest.fail("should not invoke claude"))
    # Force non-TTY
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    args = _make_resume_args(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(tmp_path / "fresh.db"),
    )
    rc = cmd_resume(args)
    captured = capsys.readouterr()
    assert rc == 1
    combined = (captured.out + captured.err).lower()
    assert "non-interactive" in combined or "no tty" in combined
    assert "--restore-pruned" in combined and "--no-restore-pruned" in combined


def test_cmd_resume_pruned_with_restore_pruned_flag_restores_then_attempts_resume(
    mock_claude_dir, tmp_path, capsys, monkeypatch
):
    """--restore-pruned auto-restores from git, then proceeds to launch
    claude --resume. We surgically intercept ONLY `claude` invocations --
    other subprocess.run calls (git operations) must pass through."""
    from claude_session_backup.commands import cmd_resume
    slug = "C--code-pruned-yes"
    uuid = "abcd3333-aaaa-bbbb-cccc-444444444444"
    expected, jsonl_path = _setup_pruned_session_in_db(
        tmp_path / "fresh.db", mock_claude_dir, slug, uuid
    )
    # Confirm baseline: files are NOT on disk before resume call
    for rel in expected:
        assert not (mock_claude_dir / rel).exists()

    # Surgically intercept ONLY `claude` invocations -- other subprocess.run
    # calls (git operations during restore) must pass through.
    captured_runs = []
    real_run = subprocess.run

    class _MockResult:
        returncode = 0

    def _mock_run(*a, **kw):
        cmd = a[0] if a else kw.get("args", [])
        if cmd and cmd[0] == "claude":
            captured_runs.append((a, kw))
            return _MockResult()
        return real_run(*a, **kw)

    monkeypatch.setattr("subprocess.run", _mock_run)

    args = _make_resume_args(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(tmp_path / "fresh.db"),
        restore_pruned=True,
    )
    rc = cmd_resume(args)
    captured = capsys.readouterr()
    assert rc == 0, "stderr: " + captured.err

    # Files were restored
    for rel, body in expected.items():
        assert (mock_claude_dir / rel).exists(), "missing after restore: " + rel
        assert (mock_claude_dir / rel).read_bytes() == body

    # claude --resume was invoked
    assert len(captured_runs) == 1
    args_passed, _ = captured_runs[0]
    cmd = args_passed[0]
    assert cmd[0] == "claude"
    assert cmd[1] == "--resume"
    assert cmd[2] == uuid


def test_cmd_resume_pruned_restore_failure_does_not_launch_claude(
    mock_claude_dir, tmp_path, capsys, monkeypatch
):
    """#34 AC6 (the long-open 'restore failure mid-resume' gap): when the
    auto-restore does not complete cleanly, cmd_resume refuses to invoke
    `claude --resume` against the incomplete restore. rc 1, no launch."""
    from claude_session_backup.commands import RestoreResult, cmd_resume
    slug = "C--code-pruned-fail"
    uuid = "abcd7777-aaaa-bbbb-cccc-888888888888"
    _setup_pruned_session_in_db(
        tmp_path / "fresh.db", mock_claude_dir, slug, uuid
    )

    monkeypatch.setattr(
        "claude_session_backup.commands._restore_session",
        lambda **kw: RestoreResult(
            wrote=0, failed=["projects/x/a.jsonl", "projects/x/b.json"],
            commit_short="deadbeef",
        ),
    )
    launched = []
    real_run = subprocess.run

    def _mock_run(*a, **kw):
        cmd = a[0] if a else kw.get("args", [])
        if cmd and cmd[0] == "claude":
            launched.append(cmd)
            class _R:
                returncode = 0
            return _R()
        return real_run(*a, **kw)

    monkeypatch.setattr("subprocess.run", _mock_run)

    args = _make_resume_args(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(tmp_path / "fresh.db"),
        restore_pruned=True,
    )
    rc = cmd_resume(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "did not complete cleanly" in captured.err
    assert launched == [], "claude --resume must NOT run after a failed restore"


def test_cmd_resume_pruned_tty_prompt_yes(mock_claude_dir, tmp_path, capsys, monkeypatch):
    """TTY + user types 'Y' (or empty) at the prompt -> restore + resume."""
    from claude_session_backup.commands import cmd_resume
    slug = "C--code-pruned-prompt-y"
    uuid = "abcd4444-aaaa-bbbb-cccc-555555555555"
    expected, _ = _setup_pruned_session_in_db(tmp_path / "fresh.db", mock_claude_dir, slug, uuid)

    # Force TTY + canned answer
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    # Same selective mock as the --restore-pruned test: only catch `claude`
    real_run = subprocess.run

    class _MockResult:
        returncode = 0

    def _mock_run(*a, **kw):
        cmd = a[0] if a else kw.get("args", [])
        if cmd and cmd[0] == "claude":
            return _MockResult()
        return real_run(*a, **kw)

    monkeypatch.setattr("subprocess.run", _mock_run)

    args = _make_resume_args(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(tmp_path / "fresh.db"),
    )
    rc = cmd_resume(args)
    assert rc == 0
    # Restore happened
    for rel in expected:
        assert (mock_claude_dir / rel).exists()


def test_cmd_resume_pruned_tty_prompt_no_aborts_cleanly(
    mock_claude_dir, tmp_path, capsys, monkeypatch
):
    """TTY + user types 'n' -> abort with rc=0 (cleanly aborted, not error)
    and no restore + no resume invoked."""
    from claude_session_backup.commands import cmd_resume
    slug = "C--code-pruned-prompt-n"
    uuid = "abcd5555-aaaa-bbbb-cccc-666666666666"
    expected, _ = _setup_pruned_session_in_db(tmp_path / "fresh.db", mock_claude_dir, slug, uuid)

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: pytest.fail("should not invoke claude"))

    args = _make_resume_args(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(tmp_path / "fresh.db"),
    )
    rc = cmd_resume(args)
    assert rc == 0
    # No restore happened
    for rel in expected:
        assert not (mock_claude_dir / rel).exists()


def test_cmd_resume_alive_session_unchanged_regression(
    mock_claude_dir, tmp_path, monkeypatch
):
    """Alive (non-pruned) session: cmd_resume should NOT prompt, NOT restore,
    just launch claude --resume immediately. v0.3.13 behavior preserved."""
    from claude_session_backup.commands import cmd_resume
    slug = "C--code-alive-resume"
    uuid = "abcd6666-aaaa-bbbb-cccc-777777777777"
    _make_session_jsonl(mock_claude_dir, slug, uuid)
    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    # NOT marked deleted
    _populate_db_with_session(tmp_path / "fresh.db", slug, uuid, jsonl_path,
                              deleted_at=None)

    captured_runs = []
    real_run = subprocess.run

    class _MockResult:
        returncode = 0

    def _mock_run(*a, **kw):
        cmd = a[0] if a else kw.get("args", [])
        if cmd and cmd[0] == "claude":
            captured_runs.append((a, kw))
            return _MockResult()
        return real_run(*a, **kw)

    monkeypatch.setattr("subprocess.run", _mock_run)
    # Prompt MUST NOT fire for alive sessions
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("alive session should not prompt"))

    args = _make_resume_args(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(tmp_path / "fresh.db"),
    )
    rc = cmd_resume(args)
    assert rc == 0
    assert len(captured_runs) == 1


# ── resume preflight (v0.3.15 -- don't launch claude against garbage) ───

def test_transcript_is_resumable_accepts_valid_jsonl(mock_claude_dir):
    from claude_session_backup.commands import _transcript_is_resumable
    p = mock_claude_dir / "projects/x/good.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'{"type":"user","sessionId":"abc"}\n{"type":"assistant"}\n')
    ok, reason = _transcript_is_resumable(p)
    assert ok and reason == ""


def test_transcript_is_resumable_rejects_symlink_target_garbage(mock_claude_dir):
    """The exact b6a4929f failure: the JSONL is a bare path string (the
    content of a symlink blob), not JSON. Must be flagged not-resumable."""
    from claude_session_backup.commands import _transcript_is_resumable
    p = mock_claude_dir / "projects/x/garbage.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"C:/Users/x/.claude/projects/slug/uuid.jsonl")  # bare path, no newline
    ok, reason = _transcript_is_resumable(p)
    assert not ok
    assert "json" in reason.lower()


def test_transcript_is_resumable_rejects_empty_and_missing(mock_claude_dir):
    from claude_session_backup.commands import _transcript_is_resumable
    empty = mock_claude_dir / "projects/x/empty.jsonl"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_bytes(b"")
    ok, reason = _transcript_is_resumable(empty)
    assert not ok and "empty" in reason.lower()

    missing = mock_claude_dir / "projects/x/nope.jsonl"
    ok2, reason2 = _transcript_is_resumable(missing)
    assert not ok2 and "not on disk" in reason2.lower()


def test_cmd_resume_refuses_garbage_transcript_does_not_launch_claude(
    mock_claude_dir, tmp_path, capsys, monkeypatch
):
    """The headline v0.3.15 preflight: an alive session whose on-disk JSONL is
    garbage (a symlink-target path string) must NOT invoke `claude --resume`
    -- it should exit with an honest message pointing at csb search."""
    from claude_session_backup.commands import cmd_resume
    slug = "C--code-garbage-resume"
    uuid = "abcd7777-aaaa-bbbb-cccc-888888888888"
    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    # Write garbage at the transcript path (the 111-byte symlink-target shape)
    full = mock_claude_dir / jsonl_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(b"C:/Users/x/.claude/projects/" + slug.encode() + b"/" + uuid.encode() + b".jsonl")
    _populate_db_with_session(tmp_path / "fresh.db", slug, uuid, jsonl_path,
                              deleted_at=None)

    # claude must NEVER be invoked
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: pytest.fail("claude --resume should not be launched against garbage"),
    )

    args = _make_resume_args(
        session_id=uuid,
        claude_dir=str(mock_claude_dir),
        db=str(tmp_path / "fresh.db"),
    )
    rc = cmd_resume(args)
    captured = capsys.readouterr()
    assert rc == 1
    combined = (captured.out + captured.err).lower()
    assert "cannot resume" in combined
    assert "csb search" in combined


# ── _restore_session helper (v0.3.14 extraction smoke test) ────────────

def test_restore_session_helper_returns_structured_result(mock_claude_dir, tmp_path):
    """Direct call to the extracted helper: confirms it can be invoked
    independently of cmd_restore's CLI shell, with structured return."""
    from claude_session_backup.commands import _restore_session, RestoreResult
    slug = "C--code-helper-test"
    uuid = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
    expected = _setup_full_session(mock_claude_dir, slug, uuid, with_logger=False)
    jsonl_path = f"projects/{slug}/{uuid}.jsonl"
    commit = _git(mock_claude_dir, "log", "--all", "-1", "--diff-filter=D",
                  "--pretty=format:%H", "--", jsonl_path).stdout.strip()
    parent = _git(mock_claude_dir, "rev-parse", commit + "~1").stdout.strip()

    result = _restore_session(
        claude_dir=str(mock_claude_dir),
        full_uuid=uuid,
        jsonl_path=jsonl_path,
        commit=parent,
    )
    assert isinstance(result, RestoreResult)
    assert result.wrote == len(expected)
    assert result.failed == []
    assert result.commit_short == parent[:8]
    # Files actually on disk byte-for-byte
    for rel, body in expected.items():
        assert (mock_claude_dir / rel).read_bytes() == body


# ── restore-verify gate (v0.3.16): warn on stub + preserve deleted_at ───

def test_restore_session_flags_invalid_transcript(mock_claude_dir, tmp_path):
    """When git only has a STUB blob for the main transcript, _restore_session
    restores it but flags transcript_valid=False so the caller can warn."""
    from claude_session_backup.commands import _restore_session
    slug = "C--code-stub"
    uuid = "57b00001-1111-2222-3333-444444444444"
    jsonl_rel = f"projects/{slug}/{uuid}.jsonl"
    # Commit a GARBAGE stub (a bare path string, the symlink-target shape)
    _commit_file(mock_claude_dir, jsonl_rel,
                 b"C:/Users/x/.claude/projects/" + slug.encode() + b"/" + uuid.encode() + b".jsonl",
                 "stub")
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()
    (mock_claude_dir / jsonl_rel).unlink()  # wipe so restore writes it

    result = _restore_session(
        claude_dir=str(mock_claude_dir),
        full_uuid=uuid,
        jsonl_path=jsonl_rel,
        commit=commit,
        jsonl_only=True,
    )
    assert result is not None
    assert result.wrote == 1
    assert result.transcript_valid is False
    assert result.transcript_warning  # non-empty reason


def test_restore_session_marks_valid_transcript_valid(mock_claude_dir, tmp_path):
    """A real transcript blob restores with transcript_valid=True."""
    from claude_session_backup.commands import _restore_session
    slug = "C--code-realtx"
    uuid = "57b00002-1111-2222-3333-444444444444"
    jsonl_rel = _make_session_jsonl(mock_claude_dir, slug, uuid,
                                    content=b'{"type":"user","sessionId":"x"}\n')
    commit = _git(mock_claude_dir, "rev-parse", "HEAD").stdout.strip()
    (mock_claude_dir / jsonl_rel).unlink()

    result = _restore_session(
        claude_dir=str(mock_claude_dir),
        full_uuid=uuid,
        jsonl_path=jsonl_rel,
        commit=commit,
        jsonl_only=True,
    )
    assert result.transcript_valid is True
    assert result.transcript_warning == ""


def test_cmd_backup_keeps_deleted_session_deleted_when_jsonl_is_garbage(
    mock_claude_dir, tmp_path, monkeypatch
):
    """END-TO-END incident guard: a deleted session whose on-disk JSONL is
    garbage must STAY deleted after `csb backup` re-indexes it -- the upsert
    guard prevents the silent un-delete that broke b6a4929f."""
    from claude_session_backup.commands import cmd_backup
    from claude_session_backup.index import open_db, init_schema, get_session, mark_deleted
    slug = "C--code-staydeleted"
    uuid = "57b00003-1111-2222-3333-444444444444"
    jsonl_rel = f"projects/{slug}/{uuid}.jsonl"
    # The on-disk JSONL is garbage (event_count will be 0)
    full = mock_claude_dir / jsonl_rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(b"C:/Users/x/.claude/projects/slug/uuid.jsonl")

    db = tmp_path / "fresh.db"
    conn = open_db(str(db)); init_schema(conn)
    # Seed the session as KNOWN-deleted
    from claude_session_backup.metadata import SessionMetadata
    from claude_session_backup.index import upsert_session
    meta = SessionMetadata(session_id=uuid, project=slug, session_name="staydeleted",
                           start_folder="C:/x", started_at="2026-03-01T00:00:00Z",
                           last_active_at="2026-03-01T01:00:00Z")
    upsert_session(conn, meta, jsonl_rel, 100, 0.0, "t0")
    mark_deleted(conn, uuid, "2026-05-31T00:00:00Z")
    conn.close()

    args = _make_args_namespace(
        claude_dir=str(mock_claude_dir), db=str(db),
    )
    # cmd_backup needs a no_commit flag etc. -- use getattr defaults; add what's needed
    args.no_commit = True
    args.no_folder_search = False
    cmd_backup(args)

    conn = open_db(str(db)); init_schema(conn)
    row = get_session(conn, uuid)
    conn.close()
    # The session must STILL be marked deleted (garbage didn't revive it)
    assert row is not None
    assert row["deleted_at"] == "2026-05-31T00:00:00Z", (
        "garbage JSONL silently un-deleted the session (the b6a4929f cascade)"
    )


# ── #40: content-derived timestamp restoration (byte+metadata-exact) ────────
#
# Restore must bring back WHEN, not just bytes: mtime from the index /
# transcript events / git history, Windows creation time from the first
# event. Sources are content-internal, so restoration is retroactive for
# everything already in git history.

_TS_FIRST_ISO = "2026-05-01T10:00:00.000Z"
_TS_LAST_ISO = "2026-05-02T18:30:00.000Z"
_TS_JSONL = (
    b'{"type":"user","timestamp":"2026-05-01T10:00:00.000Z",'
    b'"message":{"role":"user","content":"hi"}}\n'
    b'{"type":"assistant","timestamp":"2026-05-02T18:30:00.000Z",'
    b'"message":{"role":"assistant","content":"yo"}}\n'
)


def _iso_epoch(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def test_restore_applies_db_mtime_to_transcript(mock_claude_dir):
    """#40: the index's recorded jsonl_mtime (exact filesystem mtime at last
    scan, survives deletion) is the preferred mtime source for the main
    transcript."""
    from claude_session_backup.commands import _restore_session
    uuid = "70400001-1111-2222-3333-444444444444"
    slug = "C--proj-ts-db"
    jsonl_rel = f"projects/{slug}/{uuid}.jsonl"
    commit = _commit_file(mock_claude_dir, jsonl_rel, _TS_JSONL, "add ts jsonl")
    (mock_claude_dir / jsonl_rel).unlink()
    db_mtime = 1700000000.0  # a known, clearly-old mtime

    result = _restore_session(
        claude_dir=str(mock_claude_dir), full_uuid=uuid,
        jsonl_path=jsonl_rel, commit=commit, db_mtime=db_mtime,
    )
    assert result.times_applied >= 1
    assert abs(os.path.getmtime(mock_claude_dir / jsonl_rel) - db_mtime) < 2.0, (
        "restored transcript did not get the index-recorded mtime back"
    )


def test_restore_derives_mtime_from_last_event_when_no_db_mtime(mock_claude_dir):
    """#40: without an index mtime (e.g. git-history fallback restore), the
    transcript's LAST EVENT timestamp is the mtime -- a transcript's mtime IS
    its last write IS its last event. Retroactive for all git history."""
    from claude_session_backup.commands import _restore_session
    uuid = "70400002-1111-2222-3333-444444444444"
    slug = "C--proj-ts-ev"
    jsonl_rel = f"projects/{slug}/{uuid}.jsonl"
    commit = _commit_file(mock_claude_dir, jsonl_rel, _TS_JSONL, "add ts jsonl")
    (mock_claude_dir / jsonl_rel).unlink()

    result = _restore_session(
        claude_dir=str(mock_claude_dir), full_uuid=uuid,
        jsonl_path=jsonl_rel, commit=commit, db_mtime=None,
    )
    assert result.times_applied >= 1
    expected = _iso_epoch(_TS_LAST_ISO)
    assert abs(os.path.getmtime(mock_claude_dir / jsonl_rel) - expected) < 2.0


def test_restore_non_jsonl_file_gets_commit_date(mock_claude_dir):
    """#40: footprint files with no internal timestamps (session-states,
    file-history, logger text channels) get the author date of the last git
    commit touching them -- the backup nearest the last modification."""
    from claude_session_backup.commands import _restore_session
    uuid = "70400003-1111-2222-3333-444444444444"
    slug = "C--proj-ts-git"
    jsonl_rel = f"projects/{slug}/{uuid}.jsonl"
    _commit_file(mock_claude_dir, jsonl_rel, _TS_JSONL, "add ts jsonl")
    state_rel = f"session-states/{uuid}.json"
    commit = _commit_file(
        mock_claude_dir, state_rel, b'{"session_id":"x"}\n', "add state"
    )
    expected = float(_git(
        mock_claude_dir, "log", "-1", "--format=%at", commit, "--", state_rel
    ).stdout.strip())
    for rel in (jsonl_rel, state_rel):
        (mock_claude_dir / rel).unlink()

    result = _restore_session(
        claude_dir=str(mock_claude_dir), full_uuid=uuid,
        jsonl_path=jsonl_rel, commit=commit,
    )
    assert result.times_applied >= 2  # transcript AND the state file
    assert abs(os.path.getmtime(mock_claude_dir / state_rel) - expected) < 2.0


def test_restore_sets_windows_birth_time(mock_claude_dir):
    """#40: Windows creation (birth) time is set from the FIRST event
    timestamp via filekit SetFileTime. Windows + pywin32 only."""
    if sys.platform != "win32":
        pytest.skip("Windows-only: creation-time restore")
    try:
        from dazzle_filekit.metadata import is_win32_available
    except ImportError:
        pytest.skip("dazzle_filekit not installed")
    if not is_win32_available():
        pytest.skip("pywin32 not available")
    from claude_session_backup.commands import _restore_session
    uuid = "70400004-1111-2222-3333-444444444444"
    slug = "C--proj-ts-birth"
    jsonl_rel = f"projects/{slug}/{uuid}.jsonl"
    commit = _commit_file(mock_claude_dir, jsonl_rel, _TS_JSONL, "add ts jsonl")
    (mock_claude_dir / jsonl_rel).unlink()

    result = _restore_session(
        claude_dir=str(mock_claude_dir), full_uuid=uuid,
        jsonl_path=jsonl_rel, commit=commit,
    )
    assert result.times_applied >= 1
    st = os.stat(mock_claude_dir / jsonl_rel)
    expected = _iso_epoch(_TS_FIRST_ISO)
    assert abs(st.st_ctime - expected) < 2.0, (
        "restored transcript did not get its birth time back "
        f"(st_ctime={st.st_ctime}, expected~{expected})"
    )


def test_restore_preserved_files_keep_their_times(mock_claude_dir):
    """#40: present files are preserved (not rewritten) by default -- their
    LIVE timestamps must not be touched by the fidelity layer."""
    import time
    from claude_session_backup.commands import _restore_session
    uuid = "70400005-1111-2222-3333-444444444444"
    slug = "C--proj-ts-keep"
    jsonl_rel = f"projects/{slug}/{uuid}.jsonl"
    commit = _commit_file(mock_claude_dir, jsonl_rel, _TS_JSONL, "add ts jsonl")
    # File stays ON DISK with a distinctive live mtime.
    live_mtime = time.time() - 12345
    os.utime(mock_claude_dir / jsonl_rel, (live_mtime, live_mtime))

    result = _restore_session(
        claude_dir=str(mock_claude_dir), full_uuid=uuid,
        jsonl_path=jsonl_rel, commit=commit, db_mtime=1700000000.0,
    )
    assert jsonl_rel in result.preserve_list
    assert result.times_applied == 0
    assert abs(os.path.getmtime(mock_claude_dir / jsonl_rel) - live_mtime) < 2.0


def test_find_unbacked_not_fooled_by_restored_old_mtime(mock_claude_dir, tmp_path):
    """#40 interaction: after a timestamp-faithful restore, the file's mtime
    EQUALS the index-recorded mtime, so changed-detection does NOT flag the
    session as having un-backed-up changes. (Without #40 the recovery-time
    mtime would false-flag it.) Also pins the expiration-display source:
    the recorded jsonl_mtime stays the truthful old value."""
    import time
    from claude_session_backup.commands import find_unbacked_sessions
    from claude_session_backup.index import (
        get_indexed_mtime, init_schema, open_db, upsert_session,
    )
    from claude_session_backup.metadata import SessionMetadata
    uuid = "70400006-1111-2222-3333-444444444444"
    slug = "C--proj-ts-scan"
    jsonl_rel = f"projects/{slug}/{uuid}.jsonl"
    full = mock_claude_dir / jsonl_rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(_TS_JSONL)
    t_old = _iso_epoch(_TS_LAST_ISO)
    os.utime(full, (t_old, t_old))  # the #40-restored state: old mtime back

    conn = open_db(tmp_path / "scan.db")
    init_schema(conn)
    upsert_session(
        conn, SessionMetadata(session_id=uuid),
        jsonl_path=jsonl_rel, jsonl_size=len(_TS_JSONL),
        jsonl_mtime=t_old, scanned_at="2026-05-02T18:30:00Z",
    )
    # Restored-old mtime == recorded mtime -> NOT flagged.
    stale = find_unbacked_sessions(conn, str(mock_claude_dir))
    assert [s for s, _ in stale if s.session_id == uuid] == []
    # Expiration source stays truthful: recorded mtime is the old value.
    assert abs(get_indexed_mtime(conn, uuid) - t_old) < 2.0
    # Sanity contrast: a recovery-time mtime WOULD be flagged.
    now = time.time()
    os.utime(full, (now, now))
    stale = find_unbacked_sessions(conn, str(mock_claude_dir))
    assert [s for s, _ in stale if s.session_id == uuid] != []
    conn.close()


def test_fts5_freshness_with_restored_old_mtime(tmp_path):
    """#40 interaction (the #36 root-cause scenario): a restored-old mtime
    satisfies the FTS5 freshness check, so content search does not falsely
    consider the session stale after a faithful restore."""
    import time
    from claude_session_backup.fts5_db import (
        is_session_indexed, mark_session_indexed, open_fts5_db,
    )
    uuid = "70400007-1111-2222-3333-444444444444"
    t_old = _iso_epoch(_TS_LAST_ISO)
    conn = open_fts5_db(tmp_path / "fts.db", quiet=True)
    mark_session_indexed(conn, uuid, t_old, "hash123", "2026-05-02T18:30:00Z")
    # Faithful restore: on-disk mtime == indexed mtime -> fresh.
    assert is_session_indexed(conn, uuid, t_old) is True
    # Without #40: recovery-time mtime -> falsely stale (the #36 trigger).
    assert is_session_indexed(conn, uuid, time.time()) is False
    conn.close()

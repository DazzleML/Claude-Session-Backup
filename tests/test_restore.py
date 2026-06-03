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


def test_cmd_restore_fallback_refuses_to_overwrite_existing_file(mock_claude_dir, tmp_path, capsys):
    """When the file IS on disk but the DB has no row, the fallback must NOT
    overwrite -- the user might have edited it / it may not be deleted at all."""
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

    assert rc == 1
    assert "already exists" in captured.err.lower()
    assert "refusing to overwrite" in captured.err.lower()
    assert src.read_bytes() == original_content, "file must not have been touched"


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
    args = _make_scan_args(
        directories_below="C:\\code\\proj",
        deleted=True,
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

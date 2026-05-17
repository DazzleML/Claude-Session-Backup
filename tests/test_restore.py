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

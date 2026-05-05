"""
Tests for pathkit -- filesystem-validated reverse decoding of project-dir slugs.

The encoder (sanitize_path) is exact (regex-based). The decoder is
filesystem-driven: it walks tmp_path-style directory trees so the tests
work on any platform without depending on the user's real filesystem.

decode_project_slug is hardcoded to drive-letter slugs (Claude Code's
own format on Windows), so its tests are Windows-only and skip on POSIX.
"""

import os
from pathlib import Path

import pytest

from claude_session_backup.pathkit import (
    _decode_under,
    decode_project_slug,
    derive_start_at,
    sanitize_path,
)


# -- sanitize_path (the encoder; round-trip ground truth) -----------

def test_sanitize_path_drive_root():
    assert sanitize_path("C:\\") == "C--"


def test_sanitize_path_simple_subdir():
    assert sanitize_path("C:\\code\\SYSDIAGNOSE") == "C--code-SYSDIAGNOSE"


def test_sanitize_path_dotfile():
    assert sanitize_path("C:\\Users\\Extreme\\.claude") == "C--Users-Extreme--claude"


def test_sanitize_path_literal_double_dash_in_name():
    # Folder named with literal `--` -- both dashes survive sanitization,
    # producing the same `--` sequence as a `\.` (separator + dotfile).
    assert sanitize_path("C:\\code\\New--Project") == "C--code-New--Project"


def test_sanitize_path_dot_in_middle():
    assert sanitize_path("C:\\code\\New.Project") == "C--code-New-Project"


def test_sanitize_path_space():
    assert sanitize_path("C:\\Program Files") == "C--Program-Files"


# These six cases lock in the upstream-verified behavior (each non-alphanum
# char maps to one '-' independently; runs produce runs). See
# notes/architecture/2026-05-04__22-32-42__senior_sanitizePath-encoder-
# verification.md for the source citations confirming all six against
# claude-code/utils/sessionStoragePortable.ts:311-319.

def test_sanitize_path_separator_plus_dotfile():
    """`\\.` is two chars -> two dashes, not one."""
    assert sanitize_path("C:\\code\\New\\.Project") == "C--code-New--Project"


def test_sanitize_path_trailing_hyphen_plus_separator():
    """`-\\` is two chars -> two dashes (not three, not one)."""
    assert sanitize_path("C:\\code\\New-\\Project") == "C--code-New--Project"


def test_sanitize_path_consecutive_separators():
    """Two separators in a row -> two dashes (no run-collapsing)."""
    assert sanitize_path("C:\\code\\New\\\\Project") == "C--code-New--Project"


def test_sanitize_path_triple_hyphen_in_name_survives():
    """`---` literal in a folder name survives as `---` (each '-' is itself
    non-alphanumeric and maps to '-')."""
    assert sanitize_path("C:\\code\\foo---bar") == "C--code-foo---bar"


def test_sanitize_path_no_run_collapse():
    """Verifies the regex has no `+`: the four sources below all encode to
    distinct slugs -- they are NOT collapsed to a single dash."""
    # 1 dash, 2 dashes, 3 dashes -- all preserved
    assert sanitize_path("a.b") == "a-b"        # 1 char  -> 1 dash
    assert sanitize_path("a..b") == "a--b"      # 2 chars -> 2 dashes
    assert sanitize_path("a...b") == "a---b"    # 3 chars -> 3 dashes


def test_sanitize_path_unc_starts_with_double_dash():
    """UNC paths sanitize to slugs starting `--`, not a drive letter."""
    assert sanitize_path("\\\\server\\share\\foo") == "--server-share-foo"


# -- _decode_under: tmp_path tests, platform-independent -----------

def test_decode_under_simple(tmp_path):
    (tmp_path / "code").mkdir()
    result = _decode_under(str(tmp_path), "code")
    assert result == str(tmp_path / "code")


def test_decode_under_nested(tmp_path):
    (tmp_path / "Users" / "Extreme").mkdir(parents=True)
    result = _decode_under(str(tmp_path), "Users-Extreme")
    assert result == str(tmp_path / "Users" / "Extreme")


def test_decode_under_dotfile(tmp_path):
    (tmp_path / "Users" / "Extreme" / ".claude").mkdir(parents=True)
    result = _decode_under(str(tmp_path), "Users-Extreme--claude")
    assert result == str(tmp_path / "Users" / "Extreme" / ".claude")


def test_decode_under_literal_double_dash(tmp_path):
    """Folder 'New--Project' must decode to itself, not 'New/.Project'."""
    (tmp_path / "code" / "New--Project").mkdir(parents=True)
    result = _decode_under(str(tmp_path), "code-New--Project")
    assert result == str(tmp_path / "code" / "New--Project")


def test_decode_under_ambiguity_prefers_literal(tmp_path):
    """When both 'New--Project' (literal) and 'New/.Project' exist, literal wins."""
    (tmp_path / "code" / "New--Project").mkdir(parents=True)
    (tmp_path / "code" / "New" / ".Project").mkdir(parents=True)
    result = _decode_under(str(tmp_path), "code-New--Project")
    # Longer-encoded match (the literal `New--Project` directory) is chosen.
    assert result == str(tmp_path / "code" / "New--Project")


def test_decode_under_falls_back_to_dotfile_interpretation(tmp_path):
    """When only the 'New/.Project' interpretation exists, that's what we get."""
    (tmp_path / "code" / "New" / ".Project").mkdir(parents=True)
    result = _decode_under(str(tmp_path), "code-New--Project")
    assert result == str(tmp_path / "code" / "New" / ".Project")


def test_decode_under_dot_in_filename(tmp_path):
    """Folder 'New.Project' decodes from the slug 'New-Project'."""
    (tmp_path / "code" / "New.Project").mkdir(parents=True)
    result = _decode_under(str(tmp_path), "code-New-Project")
    assert result == str(tmp_path / "code" / "New.Project")


def test_decode_under_hyphen_in_filename(tmp_path):
    """Folder 'foo-bar' (literal hyphen) decodes to itself, not nested foo/bar."""
    (tmp_path / "code" / "foo-bar").mkdir(parents=True)
    result = _decode_under(str(tmp_path), "code-foo-bar")
    assert result == str(tmp_path / "code" / "foo-bar")


def test_decode_under_falls_back_to_nested_when_literal_hyphen_absent(tmp_path):
    """When 'foo-bar' folder doesn't exist but 'foo/bar' does, the latter wins."""
    (tmp_path / "code" / "foo" / "bar").mkdir(parents=True)
    result = _decode_under(str(tmp_path), "code-foo-bar")
    assert result == str(tmp_path / "code" / "foo" / "bar")


def test_decode_under_no_match_returns_none(tmp_path):
    (tmp_path / "code").mkdir()
    result = _decode_under(str(tmp_path), "nonexistent-foo")
    assert result is None


def test_decode_under_partial_match_returns_none(tmp_path):
    """Slug has a trailing component nothing matches; whole decoding fails."""
    (tmp_path / "code" / "foo").mkdir(parents=True)
    result = _decode_under(str(tmp_path), "code-foo-bar")
    assert result is None


def test_decode_under_empty_remaining_returns_parent(tmp_path):
    result = _decode_under(str(tmp_path), "")
    assert result == str(tmp_path)


def test_decode_under_nonexistent_parent_returns_none(tmp_path):
    fake = tmp_path / "does-not-exist"
    result = _decode_under(str(fake), "anything")
    assert result is None


# -- decode_project_slug (Windows drive-letter slugs) --------------

def test_decode_project_slug_invalid_pattern():
    assert decode_project_slug("") is None
    assert decode_project_slug(None) is None
    assert decode_project_slug("C") is None
    # No double-dash after the drive letter:
    assert decode_project_slug("Cabc") is None
    assert decode_project_slug("C-abc") is None


def test_decode_project_slug_unresolvable_drive_path():
    """Slug points at a path that doesn't exist -> None."""
    assert decode_project_slug("Z--definitely-does-not-exist-12345-zzz") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific drive-root semantics")
def test_decode_project_slug_bare_drive_root():
    # `C--` decodes to `C:\` (assuming Windows boot drive exists)
    assert decode_project_slug("C--") == "C:\\"


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific real path")
def test_decode_project_slug_real_users_dir():
    # `C:\Users` exists on every Windows install
    assert decode_project_slug("C--Users") == "C:\\Users"


# -- derive_start_at (the public entry) ---------------------------

def test_derive_start_at_empty():
    assert derive_start_at("") == "<no-slug>"


def test_derive_start_at_none():
    assert derive_start_at(None) == "<no-slug>"


def test_derive_start_at_unresolved_slug():
    """JSONL under a slug that can't decode -> <unresolved:slug> sentinel."""
    fake = "/some/place/Z--definitely-not-a-real-cwd-zzz/uuid.jsonl"
    result = derive_start_at(fake)
    assert result.startswith("<unresolved:")
    assert "Z--definitely-not-a-real-cwd-zzz" in result


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific")
def test_derive_start_at_real_drive_root():
    # Synthetic JSONL path inside a real ~/.claude/projects/C-- dir.
    # Even if no such file exists, the parent slug 'C--' decodes to C:\.
    fake_jsonl = "C:\\Users\\Extreme\\.claude\\projects\\C--\\fake-uuid.jsonl"
    result = derive_start_at(fake_jsonl)
    assert result == "C:\\"

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


# ── Multi-candidate disambiguation (#23) ──────────────────────────────
#
# `_collect_candidates` and `_disambiguate` are new helpers that surface ALL
# real filesystem decodings of an ambiguous slug, then pick a winner using
# JSONL signals (first_cwd, folder_usage). When the slug is unambiguous (a
# single decoding resolves), behavior matches #19. When ambiguous, the
# Tier 1 -> Tier 2 -> Tier 3 fallback chain disambiguates.

from claude_session_backup import pathkit as _pathkit


@pytest.fixture
def ambiguous_fs(tmp_path):
    """
    Filesystem state where slug `code-New--Project` has TWO real decodings:

      <tmp_path>/code/New--Project          (literal `--`)
      <tmp_path>/code/New/.Project          (separator + dotfile)

    Both sanitize to `code-New--Project`. Tests use this to exercise
    multi-candidate disambiguation paths.
    """
    code_dir = tmp_path / "code"
    code_dir.mkdir()
    literal = code_dir / "New--Project"
    literal.mkdir()
    (literal / "marker_literal").write_text("L")
    nested_parent = code_dir / "New"
    nested_parent.mkdir()
    nested = nested_parent / ".Project"
    nested.mkdir()
    (nested / "marker_nested").write_text("N")

    class _NS:
        pass
    ns = _NS()
    ns.root = str(tmp_path)
    ns.code = str(code_dir)
    ns.literal = str(literal)
    ns.nested = str(nested)
    ns.nested_parent = str(nested_parent)
    return ns


# -- _collect_candidates --

def test_collect_candidates_returns_both_for_ambiguous_slug(ambiguous_fs):
    """`_collect_candidates` returns ALL real folders that decode validly."""
    candidates = _pathkit._collect_candidates(ambiguous_fs.code, "New--Project")
    assert sorted(candidates) == sorted([ambiguous_fs.literal, ambiguous_fs.nested])


def test_collect_candidates_single_for_unambiguous(tmp_path):
    """When only one decoding exists on disk, returns a one-element list."""
    code = tmp_path / "code"
    code.mkdir()
    only = code / "amdead"
    only.mkdir()
    candidates = _pathkit._collect_candidates(str(code), "amdead")
    assert candidates == [str(only)]


def test_collect_candidates_empty_when_no_match(tmp_path):
    """When no entry matches, returns empty list."""
    code = tmp_path / "code"
    code.mkdir()
    (code / "different").mkdir()
    candidates = _pathkit._collect_candidates(str(code), "amdead")
    assert candidates == []


def test_collect_candidates_nonexistent_parent(tmp_path):
    """Non-existent parent returns empty list (defensive)."""
    candidates = _pathkit._collect_candidates(str(tmp_path / "nope"), "x")
    assert candidates == []


# -- Tier 1: first_cwd matches one candidate --

def test_tier1_first_cwd_exact_match_picks_that_candidate(ambiguous_fs):
    """If first_cwd equals one candidate exactly, that candidate wins."""
    candidates = _pathkit._collect_candidates(ambiguous_fs.code, "New--Project")
    chosen = _pathkit._disambiguate(
        candidates,
        first_cwd=ambiguous_fs.literal,
        folder_usage=None,
    )
    assert chosen == ambiguous_fs.literal


def test_tier1_first_cwd_subdirectory_match_picks_ancestor(ambiguous_fs):
    """If first_cwd is a SUBDIRECTORY of one candidate, that candidate wins (prefix match)."""
    deep = os.path.join(ambiguous_fs.literal, "deep", "subdir")
    candidates = _pathkit._collect_candidates(ambiguous_fs.code, "New--Project")
    chosen = _pathkit._disambiguate(candidates, first_cwd=deep, folder_usage=None)
    assert chosen == ambiguous_fs.literal


def test_tier1_first_cwd_picks_other_direction(ambiguous_fs):
    """Tier 1 must work in the OTHER direction too (nested candidate wins)."""
    candidates = _pathkit._collect_candidates(ambiguous_fs.code, "New--Project")
    chosen = _pathkit._disambiguate(
        candidates,
        first_cwd=ambiguous_fs.nested,
        folder_usage=None,
    )
    assert chosen == ambiguous_fs.nested


def test_tier1_first_cwd_no_match_falls_through(ambiguous_fs):
    """If first_cwd matches NEITHER candidate, fall through to Tier 2/3."""
    candidates = _pathkit._collect_candidates(ambiguous_fs.code, "New--Project")
    chosen = _pathkit._disambiguate(
        candidates,
        first_cwd="/completely/unrelated/path",
        folder_usage=None,
    )
    # Tier 3: encoded-length heuristic. Literal `New--Project` (encoded len 13)
    # beats `New` (encoded len 3) for the first dir-walk step.
    assert chosen == ambiguous_fs.literal


# -- Tier 2: folder_usage histogram --

def test_tier2_folder_usage_picks_highest_count(ambiguous_fs):
    """When first_cwd misses, the candidate with the highest folder_usage sum wins."""
    candidates = _pathkit._collect_candidates(ambiguous_fs.code, "New--Project")
    chosen = _pathkit._disambiguate(
        candidates,
        first_cwd=None,
        folder_usage={ambiguous_fs.nested: 10, ambiguous_fs.literal: 50},
    )
    assert chosen == ambiguous_fs.literal


def test_tier2_folder_usage_includes_subdirectory_counts(ambiguous_fs):
    """folder_usage entries that are subdirectories of a candidate count toward that candidate."""
    candidates = _pathkit._collect_candidates(ambiguous_fs.code, "New--Project")
    nested_subdir = os.path.join(ambiguous_fs.nested, "deeper")
    chosen = _pathkit._disambiguate(
        candidates,
        first_cwd=None,
        folder_usage={
            ambiguous_fs.literal: 5,
            ambiguous_fs.nested: 20,
            nested_subdir: 100,  # rolls up to nested
        },
    )
    assert chosen == ambiguous_fs.nested


def test_tier2_no_matching_folder_usage_falls_through(ambiguous_fs):
    """If folder_usage has no entries matching either candidate, fall through to Tier 3."""
    candidates = _pathkit._collect_candidates(ambiguous_fs.code, "New--Project")
    chosen = _pathkit._disambiguate(
        candidates,
        first_cwd=None,
        folder_usage={"/totally/unrelated": 100},
    )
    assert chosen == ambiguous_fs.literal  # Tier 3 fallback


# -- Tier 3: encoded-length heuristic (no JSONL signals) --

def test_tier3_no_signals_uses_encoded_length(ambiguous_fs):
    """No first_cwd, no folder_usage -> encoded-length heuristic picks longest match."""
    candidates = _pathkit._collect_candidates(ambiguous_fs.code, "New--Project")
    chosen = _pathkit._disambiguate(candidates, first_cwd=None, folder_usage=None)
    assert chosen == ambiguous_fs.literal


def test_tier3_single_candidate_short_circuits(tmp_path):
    """Single-candidate list returns directly without consulting JSONL info."""
    code = tmp_path / "code"
    code.mkdir()
    only = code / "amdead"
    only.mkdir()
    candidates = _pathkit._collect_candidates(str(code), "amdead")
    chosen = _pathkit._disambiguate(candidates, first_cwd=None, folder_usage=None)
    assert chosen == str(only)


def test_disambiguate_empty_candidates_returns_none():
    assert _pathkit._disambiguate([], first_cwd=None, folder_usage=None) is None


# -- Path-comparison normalization --

def test_path_matches_case_insensitive_on_windows(ambiguous_fs):
    """Windows paths compare case-insensitively (POSIX is strict; skip there)."""
    if os.name != "nt":
        pytest.skip("case-insensitivity is Windows-specific")
    candidates = _pathkit._collect_candidates(ambiguous_fs.code, "New--Project")
    upper = ambiguous_fs.literal.upper()
    chosen = _pathkit._disambiguate(candidates, first_cwd=upper, folder_usage=None)
    assert chosen == ambiguous_fs.literal


def test_path_matches_mixed_separators(ambiguous_fs):
    """Forward and backward slashes compare equal after normalization."""
    candidates = _pathkit._collect_candidates(ambiguous_fs.code, "New--Project")
    forward = ambiguous_fs.literal.replace(os.sep, "/")
    chosen = _pathkit._disambiguate(candidates, first_cwd=forward, folder_usage=None)
    assert chosen == ambiguous_fs.literal


def test_path_matches_trailing_separator(ambiguous_fs):
    """Trailing slashes don't break equality."""
    candidates = _pathkit._collect_candidates(ambiguous_fs.code, "New--Project")
    with_trail = ambiguous_fs.literal + os.sep
    chosen = _pathkit._disambiguate(candidates, first_cwd=with_trail, folder_usage=None)
    assert chosen == ambiguous_fs.literal


# -- decode_project_slug + derive_start_at: signature compat with new kwargs --

def test_decode_project_slug_accepts_new_kwargs():
    """decode_project_slug must accept first_cwd and folder_usage as keyword args."""
    result = decode_project_slug(
        "ZZZZZ-not-a-real-slug",
        first_cwd="/fake",
        folder_usage={"/fake": 1},
    )
    assert result is None


def test_derive_start_at_accepts_new_kwargs(tmp_path):
    """derive_start_at must accept first_cwd and folder_usage as keyword args."""
    slug_dir = tmp_path / "Z--zzzz-fake"
    slug_dir.mkdir()
    jsonl = slug_dir / "abcd.jsonl"
    jsonl.write_text("")
    result = derive_start_at(
        str(jsonl),
        first_cwd="/somewhere",
        folder_usage={"/somewhere": 5},
    )
    # Z:\ unlikely to exist; expect <unresolved:> sentinel
    assert isinstance(result, str)
    assert result.startswith("<unresolved:") or result == "<no-slug>"


def test_derive_start_at_no_kwargs_preserves_existing_behavior(tmp_path):
    """Calling without new kwargs works exactly as before -- backward compat."""
    slug_dir = tmp_path / "Z--zzzz-fake"
    slug_dir.mkdir()
    jsonl = slug_dir / "abcd.jsonl"
    jsonl.write_text("")
    result = derive_start_at(str(jsonl))
    assert isinstance(result, str)


# -- End-to-end: decode_project_slug uses Tier 1 when called with first_cwd --
#
# These tests skip on POSIX because decode_project_slug is hard-wired to drive-letter
# slugs (Claude Code's Windows-specific convention). The disambiguation logic is fully
# exercised by the unit tests above against tmp_path; these end-to-end tests are
# additional smoke coverage on Windows.

@pytest.mark.skipif(os.name != "nt", reason="decode_project_slug is Windows-specific")
def test_e2e_drive_root_with_first_cwd_argument():
    """Passing first_cwd to decode_project_slug doesn't break the unambiguous case."""
    result = decode_project_slug(
        "C--",
        first_cwd="C:\\Users\\Extreme",
        folder_usage={"C:\\Users\\Extreme": 100},
    )
    assert result == "C:\\"

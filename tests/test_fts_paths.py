"""Tests for claude_session_backup.fts_paths -- Phase 2 path scaffolding.

Phase 1 ships only the naming contract: no FTS5 DBs are created yet.
These tests pin the contract so Phase 2's FTS5 indexer has a stable
foundation to build on.
"""

import os
import re

import pytest

from claude_session_backup.fts_paths import (
    _safe_project_name,
    _slug_hash,
    fts5_db_dir,
    fts5_db_exists,
    fts5_db_filename,
    fts5_db_path,
    list_fts_dbs,
)


# ── _safe_project_name ────────────────────────────────────────────────


def test_safe_project_name_passes_through_alphanumerics():
    assert _safe_project_name("amdead") == "amdead"


def test_safe_project_name_keeps_dots_dashes_underscores():
    assert _safe_project_name("foo.bar_baz-qux") == "foo.bar_baz-qux"


def test_safe_project_name_replaces_path_separators():
    assert _safe_project_name("foo/bar baz\\qux") == "foo-bar-baz-qux"


def test_safe_project_name_strips_leading_trailing_hyphens():
    assert _safe_project_name("///foo///") == "foo"


def test_safe_project_name_truncates_at_60():
    long_name = "a" * 100
    out = _safe_project_name(long_name)
    assert len(out) == 60
    assert out == "a" * 60


def test_safe_project_name_empty_becomes_unnamed():
    assert _safe_project_name("") == "unnamed"
    assert _safe_project_name("///") == "unnamed"


# ── _slug_hash ────────────────────────────────────────────────────────


def test_slug_hash_is_8_hex_chars():
    h = _slug_hash("C--code-amdead-local")
    assert len(h) == 8
    assert re.fullmatch(r"[0-9a-f]{8}", h)


def test_slug_hash_deterministic():
    assert _slug_hash("X") == _slug_hash("X")


def test_slug_hash_differs_for_different_inputs():
    a = _slug_hash("C--code-amdead-local")
    b = _slug_hash("C--code-projects-amdead-local")
    assert a != b


# ── fts5_db_filename ──────────────────────────────────────────────────


def test_filename_contains_project_name():
    fn = fts5_db_filename("amdead", "C--code-amdead-local", user="Extreme")
    assert fn.startswith("amdead__")


def test_filename_contains_user_suffix():
    fn = fts5_db_filename("amdead", "C--code-amdead-local", user="Extreme")
    assert fn.endswith("_Extreme.db")


def test_filename_uses_current_user_when_omitted(monkeypatch):
    monkeypatch.setenv("USERNAME", "TestUser")
    monkeypatch.delenv("USER", raising=False)
    fn = fts5_db_filename("amdead", "C--code-amdead-local")
    assert "_TestUser.db" in fn


def test_filename_falls_back_to_unknown_user(monkeypatch):
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    fn = fts5_db_filename("amdead", "C--code-amdead-local")
    assert fn.endswith("_unknown.db")


def test_filename_deterministic_for_same_inputs():
    a = fts5_db_filename("amdead", "C--code-amdead-local", user="Extreme")
    b = fts5_db_filename("amdead", "C--code-amdead-local", user="Extreme")
    assert a == b


def test_filename_collision_resistance_same_name_different_slug():
    """The critical property: same project name in different locations
    produces DIFFERENT filenames thanks to the slug hash."""
    a = fts5_db_filename("amdead", "C--code-amdead-local", user="u")
    b = fts5_db_filename("amdead", "C--code-projects-amdead-local", user="u")
    assert a != b
    # Both still start with the human-readable project name
    assert a.startswith("amdead__")
    assert b.startswith("amdead__")


def test_filename_sanitizes_unsafe_project_chars():
    fn = fts5_db_filename("foo/bar baz", "any-slug", user="u")
    assert fn.startswith("foo-bar-baz__")
    # No raw slashes or spaces in the filename
    assert "/" not in fn
    assert " " not in fn


def test_filename_format_matches_full_pattern():
    """End-to-end regex match against the documented pattern."""
    fn = fts5_db_filename("amdead", "C--code-amdead-local", user="Extreme")
    assert re.fullmatch(r"amdead__[0-9a-f]{8}_Extreme\.db", fn)


# ── fts5_db_dir / fts5_db_path ────────────────────────────────────────


def test_db_dir_under_claude_dir(tmp_path):
    assert fts5_db_dir(tmp_path) == tmp_path / "csb-fts"


def test_db_path_combines_dir_and_filename(tmp_path):
    path = fts5_db_path(tmp_path, "amdead", "C--code-amdead-local", user="u")
    assert path.parent == tmp_path / "csb-fts"
    assert path.name.startswith("amdead__")
    assert path.name.endswith("_u.db")


# ── Phase 1 invariants: no DBs exist yet ──────────────────────────────


def test_list_fts_dbs_empty_when_dir_missing(tmp_path):
    """Phase 1: csb-fts/ directory hasn't been created yet."""
    assert list_fts_dbs(tmp_path) == {}


def test_list_fts_dbs_empty_when_dir_present_but_empty(tmp_path):
    (tmp_path / "csb-fts").mkdir()
    assert list_fts_dbs(tmp_path) == {}


def test_list_fts_dbs_finds_existing(tmp_path):
    """Forward-compat: when Phase 2 creates DBs, list_fts_dbs surfaces them."""
    d = tmp_path / "csb-fts"
    d.mkdir()
    (d / "amdead__a1b2c3d4_Extreme.db").write_text("")
    (d / "other__b2e7f1a9_Extreme.db").write_text("")

    result = list_fts_dbs(tmp_path)
    assert set(result.keys()) == {"amdead__a1b2c3d4_Extreme", "other__b2e7f1a9_Extreme"}


def test_fts5_db_exists_false_in_phase1(tmp_path):
    """Phase 1 invariant: no FTS5 DB is ever built, so this is always False."""
    assert not fts5_db_exists(tmp_path, "amdead", "C--code-amdead-local", user="u")


def test_fts5_db_exists_true_after_manual_create(tmp_path):
    """Forward-compat: Phase 2 creates the DB, fts5_db_exists returns True."""
    d = tmp_path / "csb-fts"
    d.mkdir()
    fn = fts5_db_filename("amdead", "C--code-amdead-local", user="u")
    (d / fn).write_text("")
    assert fts5_db_exists(tmp_path, "amdead", "C--code-amdead-local", user="u")

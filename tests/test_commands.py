"""Tests for command handlers (commands.py).

Focused on pure helpers and dispatcher logic that can be unit-tested without
filesystem fixtures or rendering. End-to-end CLI behavior is covered by the
human test checklist at ``tests/checklists/v0.2.3__Feature__csb-scan-disambiguation.md``.
"""

import os
from pathlib import Path

import pytest

from claude_session_backup.commands import (
    _resolve_directory_pattern,
    _maybe_promote_dot_prefix,
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

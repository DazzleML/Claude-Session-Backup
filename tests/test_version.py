"""Tests for version module."""

from claude_session_backup._version import (
    MAJOR, MINOR, PATCH, PHASE, PROJECT_PHASE,
    get_version, get_base_version, get_display_version, get_pip_version,
    __app_name__,
)


def test_app_name():
    assert __app_name__ == "claude-session-backup"


def test_version_components():
    assert isinstance(MAJOR, int)
    assert isinstance(MINOR, int)
    assert isinstance(PATCH, int)


def test_phase_valid():
    """PHASE is empty string (stable release) or a string like 'alpha', 'beta', 'rc1'."""
    assert isinstance(PHASE, str)


def test_get_version_returns_string():
    v = get_version()
    assert isinstance(v, str)
    assert len(v) > 0


def test_base_version_format():
    base = get_base_version()
    assert base.startswith(f"{MAJOR}.{MINOR}.{PATCH}")


def test_display_version_includes_project_phase():
    display = get_display_version()
    if PROJECT_PHASE and PROJECT_PHASE != "stable":
        assert PROJECT_PHASE.upper() in display
    else:
        assert display == get_base_version()


def test_pip_version_pep440():
    pip_v = get_pip_version()
    assert "-" not in pip_v
    # Structural PEP 440 check. The shapes we emit: X.Y.Z (main, no
    # phase), X.Y.Z{a|b|rc}N (phased), X.Y.Z[...]devN (branch builds --
    # dev releases are LEGAL PEP 440). The old digits-only else-branch
    # falsely failed on the first-ever feature-branch build stamp
    # (0.8.5.dev82, #69 checkpoint 3): it had only ever run against
    # main-branch stamps, where the dev segment never appears.
    import re
    assert re.fullmatch(
        r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.dev\d+)?", pip_v
    ), pip_v
    if PHASE:
        assert re.search(r"(?:a|b|rc)\d+", pip_v), pip_v


# ── v0.6.1: full build details in `csb -V` ─────────────────────────────


def test_full_display_version_includes_build_string(monkeypatch):
    """With build metadata baked in, -V shows display + the full string
    (branch, build#, date, commit) -- distinguishes a GitHub clone's build
    from a pip release carrying the same number."""
    import claude_session_backup._version as v
    monkeypatch.setattr(v, "__version__", "0.6.1_main_62-20260719-9e2c684e")
    full = v.get_full_display_version()
    assert full.endswith("(0.6.1_main_62-20260719-9e2c684e)")
    assert full.startswith(v.get_display_version())


def test_full_display_version_plain_without_metadata(monkeypatch):
    """No build metadata (fresh checkout, no stamping hooks) -> no empty
    or redundant parenthetical."""
    import claude_session_backup._version as v
    monkeypatch.setattr(v, "__version__", "0.6.1")
    assert v.get_full_display_version() == v.get_display_version()
    assert "(" not in v.get_full_display_version()

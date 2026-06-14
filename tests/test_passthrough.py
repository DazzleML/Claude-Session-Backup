"""Tests for `--` passthrough -- csb as a transparent wrapper (#47).

Everything after a standalone `--` is forwarded verbatim to the wrapped
subtool (resume -> claude, view -> viewer). The split happens BEFORE
flag-hoisting so forwarded flags are never mistaken for csb's own.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from claude_session_backup import cli
from claude_session_backup.cli import _split_passthrough, _hoist_common_flags
from claude_session_backup import commands as commands_module
from claude_session_backup.commands import _passthrough_args, _launch_viewer, cmd_resume


# ---- _split_passthrough --------------------------------------------------


def test_split_basic():
    assert _split_passthrough(["resume", "q", "--", "--fork-session"]) == (
        ["resume", "q"], ["--fork-session"])


def test_split_no_separator():
    assert _split_passthrough(["resume", "q"]) == (["resume", "q"], [])


def test_split_empty_passthrough():
    assert _split_passthrough(["resume", "q", "--"]) == (["resume", "q"], [])


def test_split_only_first_dashdash_splits():
    # subsequent `--` tokens are forwarded verbatim
    assert _split_passthrough(["resume", "q", "--", "--a", "--", "--b"]) == (
        ["resume", "q"], ["--a", "--", "--b"])


def test_split_does_not_match_long_flags():
    # "--db" is NOT the standalone "--" separator
    assert _split_passthrough(["resume", "q", "--db", "/x"]) == (
        ["resume", "q", "--db", "/x"], [])


# ---- isolation: split must precede hoisting (the load-bearing guarantee) --


def test_passthrough_flags_are_not_hoisted_into_csb():
    """`csb resume x -- --db /evil`: --db must reach the child, never csb.

    Splitting first then hoisting keeps --db out of csb's argv. If the split
    were skipped, _hoist_common_flags would pull --db into csb's options.
    """
    csb_argv, passthrough = _split_passthrough(["resume", "x", "--", "--db", "/evil"])
    hoisted = _hoist_common_flags(csb_argv)
    assert "--db" not in hoisted and "/evil" not in hoisted
    assert passthrough == ["--db", "/evil"]


# ---- _passthrough_args ---------------------------------------------------


def test_passthrough_args_present():
    assert _passthrough_args(SimpleNamespace(passthrough=["--x", "y"])) == ["--x", "y"]


def test_passthrough_args_absent_or_none():
    assert _passthrough_args(SimpleNamespace()) == []
    assert _passthrough_args(SimpleNamespace(passthrough=None)) == []


# ---- main() capability gate ----------------------------------------------


def test_gate_rejects_passthrough_on_noncapable_command(capsys):
    rc = cli.main(["list", "--", "foo"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "passthrough is only supported by" in err
    assert "resume" in err and "view" in err


# ---- resume forwards to claude -------------------------------------------


@pytest.fixture
def mock_resume_env(monkeypatch):
    """Mock cmd_resume's subprocess + DB + resolver + preflight.

    Self-contained mirror of test_commands.py's fixture so this file stands
    alone (subprocess.run is imported inside cmd_resume, so patch the module).
    """
    import subprocess as subprocess_module
    run_mock = MagicMock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(subprocess_module, "run", run_mock)
    monkeypatch.setattr(commands_module, "open_db", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(commands_module, "init_schema", MagicMock())
    monkeypatch.setattr(commands_module, "_resolve_session_or_exit",
                        MagicMock(return_value=("full-uuid-123", 0)))
    monkeypatch.setattr(commands_module, "_transcript_is_resumable",
                        MagicMock(return_value=(True, "")))
    return SimpleNamespace(run=run_mock)


def _make_args(session_id="abcd1234", **kw):
    d = {"session_id": session_id, "claude_dir": None, "db": None, "quiet": False}
    d.update(kw)
    return SimpleNamespace(**d)


def test_resume_forwards_passthrough_to_claude(monkeypatch, mock_resume_env):
    session = {"session_id": "full-uuid-123", "start_folder": "/work/p",
               "session_name": "s"}
    monkeypatch.setattr(commands_module, "get_session", MagicMock(return_value=session))

    cmd_resume(_make_args(passthrough=["--fork-session"]))

    mock_resume_env.run.assert_called_once_with(
        ["claude", "--resume", "full-uuid-123", "--fork-session"],
        cwd="/work/p",
        check=False,
    )


def test_resume_without_passthrough_unchanged(monkeypatch, mock_resume_env):
    """Backward-compat: no passthrough -> exact prior command."""
    session = {"session_id": "u9", "start_folder": "/w", "session_name": "s"}
    monkeypatch.setattr(commands_module, "get_session", MagicMock(return_value=session))

    cmd_resume(_make_args())  # no passthrough attr at all

    mock_resume_env.run.assert_called_once_with(
        ["claude", "--resume", "u9"], cwd="/w", check=False)


# ---- view forwards to the viewer -----------------------------------------


def test_launch_viewer_appends_passthrough(monkeypatch):
    calls = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls["cmd"] = cmd

    # Pin platform.system() BEFORE patching subprocess.Popen: on a cache-cold
    # interpreter, _launch_viewer's `_platform.system()` shells out via
    # subprocess (`ver` on Windows), which would otherwise hit our faked
    # Popen. Forcing a value keeps the test deterministic + OS-independent;
    # both branches build the same cmd, so we just check the appended args.
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("subprocess.Popen", FakePopen)
    rc = _launch_viewer({"mode": "binary", "path": "viewer.exe"}, "uuid-x",
                        ["--theme", "dark"])
    assert rc == 0
    assert calls["cmd"] == ["viewer.exe", "--session", "uuid-x", "--theme", "dark"]

"""Tests for the SessionStart/PreCompact/SessionEnd backup hook (v0.3.7).

The hook is a standalone script (hooks/scripts/backup-hook.py), not part of
the installable package, so we load it via importlib and exercise its pure
decision + parse functions plus main()'s spawn behavior (with subprocess.Popen
monkeypatched so no real backup runs).
"""

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

HOOK_PATH = (
    Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "backup-hook.py"
)


def _load_hook():
    spec = importlib.util.spec_from_file_location("csb_backup_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bh = _load_hook()


class _FakeStdin:
    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)

    def isatty(self):
        return False


def _set_stdin(monkeypatch, text: str):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(text.encode("utf-8")))


# ── _should_run_backup: the trigger matrix ────────────────────────────


@pytest.mark.parametrize(
    "event,source,expected",
    [
        ("SessionStart", "startup", True),
        ("SessionStart", "resume", True),   # /fork, /branch, /rewind-continue
        ("SessionStart", "clear", True),
        ("SessionStart", "compact", False),  # PreCompact already covered it
        ("SessionStart", "", True),          # unknown source -> run (safe)
        ("PreCompact", "", True),
        ("SessionEnd", "", True),
        ("", "", True),                      # manual invocation
    ],
)
def test_should_run_backup(event, source, expected):
    assert bh._should_run_backup(event, source) is expected


# ── _read_hook_input: stdin parsing tolerance ──────────────────────────


def test_read_hook_input_valid(monkeypatch):
    _set_stdin(monkeypatch, json.dumps(
        {"hook_event_name": "SessionStart", "source": "resume",
         "session_id": "abc123", "extra": 1}
    ))
    assert bh._read_hook_input() == ("SessionStart", "resume", "abc123")


def test_read_hook_input_precompact_no_source(monkeypatch):
    _set_stdin(monkeypatch, json.dumps({"hook_event_name": "PreCompact"}))
    assert bh._read_hook_input() == ("PreCompact", "", "")


def test_read_hook_input_empty(monkeypatch):
    _set_stdin(monkeypatch, "")
    assert bh._read_hook_input() == ("", "", "")


def test_read_hook_input_garbage(monkeypatch):
    _set_stdin(monkeypatch, "not json at all")
    assert bh._read_hook_input() == ("", "", "")


def test_read_hook_input_non_dict(monkeypatch):
    _set_stdin(monkeypatch, "[1, 2, 3]")
    assert bh._read_hook_input() == ("", "", "")


def test_read_hook_input_tty_returns_empty(monkeypatch):
    class _Tty:
        buffer = io.BytesIO(b'{"hook_event_name":"SessionStart"}')
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdin", _Tty())
    # TTY guard: never block on read; treat as manual -> ("", "", "")
    assert bh._read_hook_input() == ("", "", "")


# ── main(): spawn behavior (Popen monkeypatched -> no real backup) ─────


class _FakePopen:
    """Records construction; intentionally has NO wait() -- if main() ever
    waited on the backup, calling it would AttributeError and fail the test,
    proving the fire-and-don't-wait contract."""
    calls: list = []

    def __init__(self, cmd, **kwargs):
        type(self).calls.append((cmd, kwargs))


@pytest.fixture
def captured_popen(monkeypatch, tmp_path):
    _FakePopen.calls = []
    monkeypatch.setattr(bh.subprocess, "Popen", _FakePopen)
    # keep logs out of the real ~/.claude
    monkeypatch.setattr(bh.Path, "home", lambda: tmp_path)
    return _FakePopen


def test_main_skips_compact_no_spawn(monkeypatch, captured_popen):
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionStart", "compact", "s1"))
    bh.main()
    assert captured_popen.calls == []  # compaction SessionStart never backs up


def test_main_sessionstart_clean_no_spawn(monkeypatch, captured_popen, capsys):
    """SessionStart with no gap (v0.3.9): detector says clean -> NO backup,
    NO warning. SessionEnd is the durable trigger, not SessionStart."""
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionStart", "resume", "s1"))
    monkeypatch.setattr(bh, "_run_check", lambda sid: ("clean", ""))
    bh.main()
    assert captured_popen.calls == []          # no recovery backup
    assert "systemMessage" not in capsys.readouterr().out  # no warning


def test_main_sessionstart_gap_warns_and_spawns(monkeypatch, captured_popen, capsys):
    """A detected gap -> emit a systemMessage warning AND spawn a recovery
    backup (the 'warn + recover' behavior)."""
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionStart", "startup", "s1"))
    monkeypatch.setattr(bh, "_run_check", lambda sid: ("gap", "csb: 1 session(s) un-backed-up"))
    bh.main()
    assert len(captured_popen.calls) == 1      # recovery backup spawned
    out = capsys.readouterr().out
    assert "systemMessage" in out
    assert "un-backed-up" in out


def test_main_sessionstart_passes_session_id_to_check(monkeypatch, captured_popen):
    """The current session_id is forwarded to the check so it can be excluded
    (its JSONL is mid-write and would always look stale)."""
    seen = {}

    def fake_check(sid):
        seen["sid"] = sid
        return ("clean", "")

    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionStart", "startup", "cur-sid"))
    monkeypatch.setattr(bh, "_run_check", fake_check)
    bh.main()
    assert seen["sid"] == "cur-sid"


def test_main_sessionstart_check_error_defensive_backup(monkeypatch, captured_popen, capsys):
    """If the detector itself errors, back up defensively but DON'T warn (no
    confirmed gap to report)."""
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionStart", "resume", "s1"))
    monkeypatch.setattr(bh, "_run_check", lambda sid: ("error", "boom"))
    bh.main()
    assert len(captured_popen.calls) == 1      # defensive backup
    assert "systemMessage" not in capsys.readouterr().out  # no false warning


def test_main_spawns_on_sessionend(monkeypatch, captured_popen):
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionEnd", "", "s1"))
    bh.main()
    assert len(captured_popen.calls) == 1


def test_main_spawns_on_manual(monkeypatch, captured_popen):
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("", "", ""))
    bh.main()
    assert len(captured_popen.calls) == 1


def test_main_does_not_wait(monkeypatch, captured_popen):
    """_FakePopen has no .wait(); main() completing proves it never waits."""
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("PreCompact", "", "s1"))
    bh.main()  # would raise AttributeError if main() called .wait()
    assert len(captured_popen.calls) == 1


# ── _detach_kwargs + detached spawn (v0.3.8: survive teardown, no window) ──


def test_detach_kwargs_windows(monkeypatch):
    monkeypatch.setattr(bh.sys, "platform", "win32")
    kw = bh._detach_kwargs()
    # CREATE_NO_WINDOW (0x08000000) | CREATE_NEW_PROCESS_GROUP (0x200).
    # CREATE_NO_WINDOW => hidden console inherited by git children => no popups
    # (DETACHED_PROCESS would leave csb consoleless -> each git child pops one).
    # CREATE_NEW_PROCESS_GROUP => shielded from the teardown Ctrl-C/Break.
    assert kw == {"creationflags": 0x08000000 | 0x00000200}
    assert "start_new_session" not in kw


def test_detach_kwargs_posix(monkeypatch):
    monkeypatch.setattr(bh.sys, "platform", "linux")
    kw = bh._detach_kwargs()
    assert kw == {"start_new_session": True}
    assert "creationflags" not in kw


def test_main_spawns_detached(monkeypatch, captured_popen):
    """The spawn must carry detach kwargs so the backup is decoupled from the
    session's process tree (survives SessionEnd teardown) -- the v0.3.8 fix."""
    monkeypatch.setattr(bh.sys, "platform", "win32")
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionEnd", "", "s1"))
    bh.main()
    assert len(captured_popen.calls) == 1
    _cmd, kwargs = captured_popen.calls[0]
    assert kwargs.get("creationflags") == 0x08000000 | 0x00000200

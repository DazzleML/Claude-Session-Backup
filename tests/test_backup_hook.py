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
    assert bh._read_hook_input() == ("SessionStart", "resume", "abc123", "")


def test_read_hook_input_precompact_no_source(monkeypatch):
    _set_stdin(monkeypatch, json.dumps({"hook_event_name": "PreCompact"}))
    assert bh._read_hook_input() == ("PreCompact", "", "", "")


def test_read_hook_input_empty(monkeypatch):
    _set_stdin(monkeypatch, "")
    assert bh._read_hook_input() == ("", "", "", "")


def test_read_hook_input_garbage(monkeypatch):
    _set_stdin(monkeypatch, "not json at all")
    assert bh._read_hook_input() == ("", "", "", "")


def test_read_hook_input_non_dict(monkeypatch):
    _set_stdin(monkeypatch, "[1, 2, 3]")
    assert bh._read_hook_input() == ("", "", "", "")


def test_read_hook_input_tty_returns_empty(monkeypatch):
    class _Tty:
        buffer = io.BytesIO(b'{"hook_event_name":"SessionStart"}')
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdin", _Tty())
    # TTY guard: never block on read; treat as manual -> ("", "", "")
    assert bh._read_hook_input() == ("", "", "", "")


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
    # Repo present by default -- the probe shells out via subprocess.run,
    # which would otherwise hit the fake Popen. Repo-less tests override.
    monkeypatch.setattr(bh, "_git_repo_state", lambda d: "ok")
    # The #78 ancestry walk also rides subprocess.run -> the fake Popen
    # would count it as a spawn; these tests count BACKUP spawns. The
    # walk has its own tests (TestHostWalk in test_liveness_pid.py).
    monkeypatch.setattr(bh, "_process_table", lambda: None)
    # keep logs out of the real ~/.claude
    monkeypatch.setattr(bh.Path, "home", lambda: tmp_path)
    return _FakePopen


def test_main_skips_compact_no_spawn(monkeypatch, captured_popen):
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionStart", "compact", "s1", ""))
    bh.main()
    assert captured_popen.calls == []  # compaction SessionStart never backs up


def test_main_sessionstart_clean_no_spawn(monkeypatch, captured_popen, capsys):
    """SessionStart with no gap (v0.3.9): detector says clean -> NO backup,
    NO warning. SessionEnd is the durable trigger, not SessionStart."""
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionStart", "resume", "s1", ""))
    monkeypatch.setattr(bh, "_run_check", lambda sid: ("clean", ""))
    bh.main()
    assert captured_popen.calls == []          # no recovery backup
    assert "systemMessage" not in capsys.readouterr().out  # no warning


def test_main_sessionstart_gap_warns_and_spawns(monkeypatch, captured_popen, capsys):
    """A detected gap -> emit a systemMessage warning AND spawn a recovery
    backup (the 'warn + recover' behavior)."""
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionStart", "startup", "s1", ""))
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

    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionStart", "startup", "cur-sid", ""))
    monkeypatch.setattr(bh, "_run_check", fake_check)
    bh.main()
    assert seen["sid"] == "cur-sid"


def test_main_sessionstart_check_error_defensive_backup(monkeypatch, captured_popen, capsys):
    """If the detector itself errors, back up defensively but DON'T warn (no
    confirmed gap to report)."""
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionStart", "resume", "s1", ""))
    monkeypatch.setattr(bh, "_run_check", lambda sid: ("error", "boom"))
    bh.main()
    assert len(captured_popen.calls) == 1      # defensive backup
    assert "systemMessage" not in capsys.readouterr().out  # no false warning


def test_main_spawns_on_sessionend(monkeypatch, captured_popen):
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionEnd", "", "s1", ""))
    bh.main()
    assert len(captured_popen.calls) == 1


def test_main_spawns_on_manual(monkeypatch, captured_popen):
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("", "", "", ""))
    bh.main()
    assert len(captured_popen.calls) == 1


def test_main_repoless_falls_back_to_no_commit(monkeypatch, captured_popen):
    """Repo-less box (#52): the spawned backup carries --no-commit so the
    index stays fresh instead of the run dying at the git precondition."""
    monkeypatch.setattr(bh, "_git_repo_state", lambda d: "absent")
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionEnd", "", "s1", ""))
    bh.main()
    assert len(captured_popen.calls) == 1
    cmd, _kwargs = captured_popen.calls[0]
    assert "--no-commit" in cmd


def test_main_with_repo_spawns_plain_backup(monkeypatch, captured_popen):
    """With a git repo, the spawn is the classic full backup -- no
    --no-commit sneaking in."""
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionEnd", "", "s1", ""))
    bh.main()
    cmd, _kwargs = captured_popen.calls[0]
    assert "--no-commit" not in cmd


def test_main_repoless_gap_still_warns(monkeypatch, captured_popen, capsys):
    """Repo-less + gap: the systemMessage warning still fires AND the
    recovery spawn is index-only (#52) -- the crash scenario that used to
    fail silently."""
    monkeypatch.setattr(bh, "_git_repo_state", lambda d: "absent")
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionStart", "startup", "s1", ""))
    monkeypatch.setattr(bh, "_run_check", lambda sid: ("gap", "csb: 2 session(s) with un-indexed changes"))
    bh.main()
    assert len(captured_popen.calls) == 1
    cmd, _kwargs = captured_popen.calls[0]
    assert "--no-commit" in cmd
    assert "systemMessage" in capsys.readouterr().out


def test_main_does_not_wait(monkeypatch, captured_popen):
    """_FakePopen has no .wait(); main() completing proves it never waits."""
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("PreCompact", "", "s1", ""))
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
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionEnd", "", "s1", ""))
    bh.main()
    assert len(captured_popen.calls) == 1
    _cmd, kwargs = captured_popen.calls[0]
    assert kwargs.get("creationflags") == 0x08000000 | 0x00000200


def test_read_hook_input_strips_powershell_bom(monkeypatch):
    """PowerShell's `|` pipe prepends a UTF-8 BOM; the parse must survive it
    (a BOM'd payload used to fall back to ("", "", "") -- mis-classifying
    the event as manual)."""
    payload = "﻿" + json.dumps(
        {"hook_event_name": "SessionEnd", "source": "", "session_id": "s9"}
    )
    _set_stdin(monkeypatch, payload)
    assert bh._read_hook_input() == ("SessionEnd", "", "s9", "")


def test_main_writes_fallback_note_before_opening_child_log(monkeypatch, tmp_path):
    """Regression for the v0.6.0 checklist FV.5 finding: the [index-only]
    fallback note must be flushed to the log BEFORE the child's output
    handle is opened -- on Windows the inherited handle writes from the
    position captured at open time, clobbering later-appended notes. We
    pin the ordering: every note() written by main() must happen before
    open_out()'s handle exists."""
    order = []

    class _OrderedPopen:
        def __init__(self, cmd, **kwargs):
            order.append("spawn")

    real_note_target = {}

    monkeypatch.setattr(bh.subprocess, "Popen", _OrderedPopen)
    monkeypatch.setattr(bh, "_git_repo_state", lambda d: "absent")
    monkeypatch.setattr(bh.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(bh, "_read_hook_input", lambda: ("SessionEnd", "", "s1", ""))

    # Wrap open() on the run log to record when the child's handle opens.
    real_open = bh.Path.open

    def tracking_open(self, *a, **k):
        mode = a[0] if a else k.get("mode", "r")
        if self.name.startswith("backup-") and str(mode).startswith("a"):
            order.append(f"open:{mode}")
        return real_open(self, *a, **k)

    monkeypatch.setattr(bh.Path, "open", tracking_open)
    bh.main()

    log = tmp_path / ".claude" / "csb-logs" / "backup-SessionEnd.log"
    content = log.read_text(encoding="utf-8")
    assert "[index-only] no git repo" in content   # the note survived
    # The fallback note's open must precede the child-output handle's open
    # that feeds the spawn. Opens are all append-mode; the LAST open before
    # "spawn" is the child handle, and the note text is already on disk by
    # then (asserted above, plus ordering below).
    assert order[-1] == "spawn"
    assert order.count("spawn") == 1


def test_backup_extra_args_refused_wording(monkeypatch, tmp_path):
    """Hook log under refusal says REFUSES (repo intact), not "no git
    repo" -- the mislabel that muddied the 2026-07-22 incident."""
    monkeypatch.setattr(bh, "_git_repo_state", lambda d: "refused")
    notes = []
    extra = bh._backup_extra_args(notes.append)
    assert extra == ["--no-commit"]
    assert len(notes) == 1
    assert "REFUSES" in notes[0]
    assert "do NOT re-init" in notes[0]
    assert "no git repo" not in notes[0]


def test_backup_extra_args_absent_wording(monkeypatch, tmp_path):
    monkeypatch.setattr(bh, "_git_repo_state", lambda d: "absent")
    notes = []
    extra = bh._backup_extra_args(notes.append)
    assert extra == ["--no-commit"]
    assert "no git repo" in notes[0]


# ── Live Session Registry wiring (#64) ─────────────────────────────────


@pytest.fixture
def registry_env(monkeypatch, tmp_path):
    """Isolate _claude_dir and neutralize spawn/check for registry tests."""
    monkeypatch.setattr(bh, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(bh, "_run_check", lambda sid: ("clean", ""))
    monkeypatch.setattr(bh, "_spawn_backup",
                        lambda out, note, extra=None: None)
    return tmp_path


def _entry(tmp_path, sid):
    return tmp_path / "csb-live" / f"{sid}.json"


def test_sessionstart_writes_registry_entry(registry_env, monkeypatch):
    monkeypatch.setattr(bh, "_read_hook_input",
                        lambda: ("SessionStart", "startup", "reg-1", "C:/w"))
    bh.main()
    raw = json.loads(_entry(registry_env, "reg-1").read_text(encoding="utf-8"))
    assert raw["session_id"] == "reg-1"
    assert raw["source"] == "startup"
    assert raw["cwd"] == "C:/w"
    assert "session_name" not in raw  # name-free by design (#64)


def test_sessionend_removes_registry_entry(registry_env, monkeypatch):
    monkeypatch.setattr(bh, "_read_hook_input",
                        lambda: ("SessionStart", "startup", "reg-2", ""))
    bh.main()
    assert _entry(registry_env, "reg-2").exists()
    monkeypatch.setattr(bh, "_read_hook_input",
                        lambda: ("SessionEnd", "", "reg-2", ""))
    bh.main()
    assert not _entry(registry_env, "reg-2").exists()


def test_compact_sessionstart_registers_but_never_resets(registry_env,
                                                         monkeypatch):
    """The compact-skip gate must not skip registration -- but a compact
    of an already-tracked session must not reset started_at either."""
    monkeypatch.setattr(bh, "_read_hook_input",
                        lambda: ("SessionStart", "startup", "reg-3", ""))
    bh.main()
    before = json.loads(
        _entry(registry_env, "reg-3").read_text(encoding="utf-8"))["started_at"]
    monkeypatch.setattr(bh, "_read_hook_input",
                        lambda: ("SessionStart", "compact", "reg-3", ""))
    bh.main()
    after = json.loads(
        _entry(registry_env, "reg-3").read_text(encoding="utf-8"))["started_at"]
    assert after == before
    # And a compact fire for an UNTRACKED session (hook installed
    # mid-session) still registers it.
    monkeypatch.setattr(bh, "_read_hook_input",
                        lambda: ("SessionStart", "compact", "reg-4", ""))
    bh.main()
    assert _entry(registry_env, "reg-4").exists()


def test_sessionend_without_entry_is_silent(registry_env, monkeypatch):
    monkeypatch.setattr(bh, "_read_hook_input",
                        lambda: ("SessionEnd", "", "never-seen", ""))
    bh.main()  # must not raise
    assert not _entry(registry_env, "never-seen").exists()


def test_empty_session_id_writes_nothing(registry_env, monkeypatch):
    monkeypatch.setattr(bh, "_read_hook_input",
                        lambda: ("SessionStart", "startup", "", ""))
    bh.main()
    assert not (registry_env / "csb-live").exists()

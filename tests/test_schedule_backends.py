"""Tests for schedule_backends -- the OS shells around the #69 renderers.

Everything is mocked at the module's own `_run` seam; the real OS
behavior these mocks encode was measured first (P1/P2/P4 probe results
in tests/one-offs/). Covers: the single platform gate (AC-2), Windows
install/remove/status incl. 267009=running and remove-nothing=ok
(AC-4), backup-before-mutate snapshots (Delta-1), crontab empty-spool
vs denial handling (AC-7), daemon pre-flight incl. the busybox path,
launchd bootstrap-deferred honesty (AC-6), the verbatim refusal text +
distinct exit code (Delta-5/Delta-7), and the systemd recipe printer.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_session_backup import schedule_backends as sb
from claude_session_backup.schedule import (
    CRON_BLOCK_BEGIN,
    ScheduleSpec,
    render_crontab_block,
)

SPEC = ScheduleSpec(
    interval_minutes=1440, fire_hour=14, fire_minute=37,
    start_boundary="2026-08-06T14:37:00",
    python_exe="/usr/bin/python3" if sys.platform != "win32"
    else r"C:\Python312\pythonw.exe",
    claude_dir="",  # set per-test to tmp_path
    log_file="/tmp/schedule.log" if sys.platform != "win32"
    else r"C:\t\schedule.log",
)


def spec_for(tmp_path: Path) -> ScheduleSpec:
    return ScheduleSpec(
        interval_minutes=SPEC.interval_minutes, fire_hour=SPEC.fire_hour,
        fire_minute=SPEC.fire_minute, start_boundary=SPEC.start_boundary,
        python_exe=SPEC.python_exe, claude_dir=str(tmp_path),
        log_file=SPEC.log_file,
    )


class FakeRun:
    """Programmable `_run` stand-in: route by (argv0, subcommand-ish)."""

    def __init__(self):
        self.routes: list[tuple] = []   # (predicate, rc, out, err)
        self.calls: list[list[str]] = []
        self.stdins: list = []

    def route(self, pred, rc=0, out="", err=""):
        self.routes.append((pred, rc, out, err))

    def __call__(self, args, stdin_text=None, timeout=60):
        self.calls.append(list(args))
        self.stdins.append(stdin_text)
        for pred, rc, out, err in self.routes:
            if pred(args):
                return subprocess.CompletedProcess(args, rc, out, err)
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def fake(monkeypatch):
    f = FakeRun()
    monkeypatch.setattr(sb, "_run", f)
    return f


# ── The single platform gate (AC-2) ─────────────────────────────────────


def test_choose_backend_is_the_only_platform_gate(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(sb.choose_backend(), sb.WindowsTaskBackend)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(sb.choose_backend(), sb.LaunchdBackend)
    for posix in ("linux", "freebsd14", "openbsd7", "netbsd"):
        monkeypatch.setattr(sys, "platform", posix)
        assert isinstance(sb.choose_backend(), sb.CrontabBackend)


# ── Windows backend (P1-proven semantics) ───────────────────────────────


def _is(cmd, *frag):
    return lambda a: a[0] == cmd and all(f in a for f in frag)


def test_win_install_fresh_no_snapshot(fake, tmp_path):
    fake.route(_is("schtasks", "/query", "/xml"), rc=1, err="not found")
    fake.route(_is("schtasks", "/create"), rc=0, out="SUCCESS")
    r = sb.WindowsTaskBackend().install(spec_for(tmp_path))
    assert r.ok and r.backup_path is None
    create = [c for c in fake.calls if "/create" in c][0]
    xml_file = create[create.index("/xml") + 1]
    assert "/f" in create  # idempotent overwrite form


def test_win_install_snapshots_existing_task(fake, tmp_path):
    fake.route(_is("schtasks", "/query", "/xml"), rc=0, out="<Task>old</Task>")
    fake.route(_is("schtasks", "/create"), rc=0)
    r = sb.WindowsTaskBackend().install(spec_for(tmp_path))
    assert r.ok and r.backup_path is not None
    saved = Path(r.backup_path)
    assert saved.parent == tmp_path / "backups" / "csb-schedule"  # Delta-1
    assert saved.read_text(encoding="utf-8") == "<Task>old</Task>"


def test_win_install_failure_surfaces_stderr(fake, tmp_path):
    fake.route(_is("schtasks", "/query", "/xml"), rc=1)
    fake.route(_is("schtasks", "/create"), rc=1, err="ERROR: Access is denied.")
    r = sb.WindowsTaskBackend().install(spec_for(tmp_path))
    assert not r.ok and "denied" in r.detail


def test_win_install_notes_legacy_handrolled_task(fake, tmp_path):
    # The pre-#69 docs snippet registered "Claude Session Backup" by hand;
    # both tasks running is lock-safe but wasteful, and the legacy entry's
    # bare `csb` is PATH-fragile -- install points at the removal command.
    fake.route(_is("schtasks", "/query", "/xml"), rc=1)
    fake.route(_is("schtasks", "/create"), rc=0)
    fake.route(lambda a: "Claude Session Backup" in a, rc=0, out="present")
    r = sb.WindowsTaskBackend().install(spec_for(tmp_path))
    assert r.ok
    assert "legacy" in r.detail
    assert '/delete /tn "Claude Session Backup"' in r.detail


def test_win_install_without_legacy_task_stays_quiet(fake, tmp_path):
    fake.route(_is("schtasks", "/query", "/xml"), rc=1)
    fake.route(_is("schtasks", "/create"), rc=0)
    fake.route(lambda a: "Claude Session Backup" in a, rc=1, err="not found")
    r = sb.WindowsTaskBackend().install(spec_for(tmp_path))
    assert r.ok and "legacy" not in r.detail


def test_win_remove_nothing_is_success(fake, tmp_path):
    # AC-4 + P1: /delete of a missing task rc=1 -> clean success.
    fake.route(_is("schtasks", "/query", "/xml"), rc=1)
    fake.route(_is("schtasks", "/delete"), rc=1,
               err="ERROR: The system cannot find the file specified.")
    r = sb.WindowsTaskBackend().remove(str(tmp_path))
    assert r.ok and not r.was_installed


def test_win_remove_installed_snapshots_first(fake, tmp_path):
    fake.route(_is("schtasks", "/query", "/xml"), rc=0, out="<Task>x</Task>")
    fake.route(_is("schtasks", "/delete"), rc=0, out="SUCCESS")
    r = sb.WindowsTaskBackend().remove(str(tmp_path))
    assert r.ok and r.was_installed and r.backup_path


def test_win_status_running_is_a_state_not_a_failure(fake):
    # P1 observed 267009 (SCHED_S_TASK_RUNNING) mid-pythonw-startup.
    fake.route(_is("schtasks", "/query", "/xml"), rc=0, out="<Task/>")
    fake.route(_is("schtasks", "/v"), rc=0, out=(
        "Last Run Time:                        8/7/2026 12:01:54 AM\n"
        "Last Result:                          267009\n"))
    s = sb.WindowsTaskBackend().status()
    assert s.installed and s.running_now and s.last_result == 267009


def test_win_status_localized_labels_degrade_honestly(fake):
    fake.route(_is("schtasks", "/query", "/xml"), rc=0, out="<Task/>")
    fake.route(_is("schtasks", "/v"), rc=0,
               out="Letzte Laufzeit:  07.08.2026\nLetztes Ergebnis: 0\n")
    s = sb.WindowsTaskBackend().status()
    assert s.installed and not s.readback_available


def test_win_status_absent(fake):
    fake.route(_is("schtasks", "/query", "/xml"), rc=1)
    assert not sb.WindowsTaskBackend().status().installed


# ── Crontab backend (AC-4/AC-7, P4-informed) ────────────────────────────


def test_crontab_empty_spool_is_not_an_error(fake):
    fake.route(_is("crontab", "-l"), rc=1, err="no crontab for user")
    text, err = sb._read_crontab()
    assert text == "" and err is None


def test_crontab_denial_surfaces_verbatim(fake):
    fake.route(_is("crontab", "-l"), rc=1,
               err="You (user) are not allowed to use this program (crontab)")
    _, err = sb._read_crontab()
    assert err and "not allowed" in err


def test_crontab_install_pipes_merged_text(fake, tmp_path):
    fake.route(_is("crontab", "-l"), rc=1, err="no crontab for user")
    fake.route(lambda a: a == ["crontab", "-"], rc=0)
    spec = spec_for(tmp_path)
    r = sb.CrontabBackend().install(spec)
    assert r.ok and r.backup_path is None  # empty spool -> nothing to save
    written = fake.stdins[-1]
    assert written == sb.merge_crontab("", render_crontab_block(spec))
    assert CRON_BLOCK_BEGIN in written


def test_crontab_install_snapshots_nonempty_spool(fake, tmp_path):
    fake.route(_is("crontab", "-l"), rc=0, out="MAILTO=me\n")
    fake.route(lambda a: a == ["crontab", "-"], rc=0)
    r = sb.CrontabBackend().install(spec_for(tmp_path))
    assert r.ok and r.backup_path
    assert Path(r.backup_path).read_text(encoding="utf-8") == "MAILTO=me\n"
    assert fake.stdins[-1].startswith("MAILTO=me\n")  # foreign line survives


def test_crontab_remove_nothing_is_success(fake, tmp_path):
    fake.route(_is("crontab", "-l"), rc=0, out="MAILTO=me\n")
    r = sb.CrontabBackend().remove(str(tmp_path))
    assert r.ok and not r.was_installed
    assert not any(a == ["crontab", "-"] for a in fake.calls)  # no rewrite


def test_crontab_remove_strips_only_our_block(fake, tmp_path):
    spec = spec_for(tmp_path)
    current = sb.merge_crontab("MAILTO=me\n", render_crontab_block(spec))
    fake.route(_is("crontab", "-l"), rc=0, out=current)
    fake.route(lambda a: a == ["crontab", "-"], rc=0)
    r = sb.CrontabBackend().remove(str(tmp_path))
    assert r.ok and r.was_installed and r.backup_path
    assert fake.stdins[-1] == "MAILTO=me\n"


def test_daemon_preflight_accepts_named_daemons(fake):
    fake.route(_is("ps", "-axo", "comm="), rc=0, out="bash\ncron\nsshd\n")
    assert sb._cron_daemon_running()


def test_daemon_preflight_busybox_applet(fake):
    fake.route(_is("ps", "-axo", "comm="), rc=0, out="sh\nbusybox\n")
    fake.route(_is("ps", "-axo", "args="), rc=0,
               out="/bin/sh\nbusybox crond -f -l 8\n")
    assert sb._cron_daemon_running()


def test_daemon_preflight_spool_tool_alone_is_not_enough(fake):
    # THE silent-failure gate: crontab binary present, nothing running.
    fake.route(_is("ps", "-axo", "comm="), rc=0, out="bash\nsshd\n")
    assert not sb._cron_daemon_running()


def test_crontab_detect_refuses_without_daemon(fake, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/crontab")
    fake.route(_is("ps", "-axo", "comm="), rc=0, out="bash\n")
    d = sb.CrontabBackend().detect()
    assert not d.available and "daemon" in d.reason


# ── launchd backend (AC-6; hardware-unverified, logic-tested) ───────────


@pytest.fixture
def macish(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_launchd_install_bootstrap_ok(fake, macish, tmp_path):
    fake.route(_is("launchctl", "bootstrap"), rc=0)
    r = sb.LaunchdBackend().install(spec_for(tmp_path))
    assert r.ok and not r.deferred
    assert (macish / "Library" / "LaunchAgents"
            / f"{sb.LAUNCHD_LABEL}.plist").exists()


def test_launchd_bootstrap_failure_reports_deferred_never_running(
        fake, macish, tmp_path):
    # AC-6: SSH/no-GUI bootstrap failure -> "written; loads at next
    # login", explicitly NOT installed-and-running.
    fake.route(_is("launchctl", "bootstrap"), rc=5, err="Bootstrap failed")
    r = sb.LaunchdBackend().install(spec_for(tmp_path))
    assert r.ok and r.deferred and "next login" in r.detail


def test_launchd_remove_nothing_is_success(fake, macish, tmp_path):
    r = sb.LaunchdBackend().remove(str(tmp_path))
    assert r.ok and not r.was_installed


# ── Refusal text + recipe (Delta-5/Delta-7) ─────────────────────────────


def test_refusal_text_golden_properties():
    t = sb.REFUSAL_TEXT
    assert t.isascii()  # codepage rule
    for needle in ("running cron daemon", "sudo apt install cron",
                   "rcctl", "--print-systemd", "enable-linger",
                   "Nothing was installed or modified",
                   "csb setup schedule --dry-run"):
        assert needle in t, needle
    assert "re-enable" in t  # BSD: refused-vs-absent, never "install"


def test_refused_exit_code_is_distinct():
    from claude_session_backup.commands import CHECK_GAP_EXIT
    assert sb.SCHEDULE_REFUSED_EXIT == 11
    assert sb.SCHEDULE_REFUSED_EXIT != CHECK_GAP_EXIT
    assert sb.SCHEDULE_REFUSED_EXIT not in (0, 1, 2)


def test_systemd_recipe_carries_full_context(tmp_path):
    spec = spec_for(tmp_path)
    r = sb.render_systemd_recipe(spec)
    assert "--claude-dir" in r and "--log-file" in r
    assert spec.python_exe in r
    assert "Persistent=true" in r
    assert "enable-linger" in r          # the warning ships inline
    assert "not managed" in r            # D2 boundary: printed, never owned
    assert "14:37:00" in r               # Delta-11 fire time survives


def test_systemd_recipe_notes_windows_rendered_paths(tmp_path):
    # Tester finding #3: rendered on Windows, the recipe baked
    # C:\...\pythonw.exe into ExecStart -- meaningless on the Linux
    # target. The recipe must say it is a shape preview, not paste-ready.
    win = ScheduleSpec(
        interval_minutes=1440, fire_hour=14, fire_minute=37,
        start_boundary="2026-08-06T14:37:00",
        python_exe=r"C:\Python312\pythonw.exe",
        claude_dir=str(tmp_path), log_file=r"C:\t\schedule.log")
    assert "generated on Windows" in sb.render_systemd_recipe(win)
    posix = ScheduleSpec(
        interval_minutes=1440, fire_hour=14, fire_minute=37,
        start_boundary="2026-08-06T14:37:00",
        python_exe="/usr/bin/python3",
        claude_dir=str(tmp_path), log_file="/tmp/schedule.log")
    assert "generated on Windows" not in sb.render_systemd_recipe(posix)


# ── Tester sweep findings (2026-08-07): busybox ps, docker WSL,
#    remove-guard for the wrong store ─────────────────────────────────────


def test_daemon_preflight_falls_back_to_proc_when_ps_lacks_axo(
        fake, monkeypatch):
    # Tester finding #2 (stock Alpine): BusyBox ps rejects -axo (rc 1,
    # "unrecognized option: x") while crond genuinely runs. The pre-flight
    # must fall back to /proc/<pid>/comm instead of refusing.
    fake.route(lambda a: a[0] == "ps", rc=1, err="ps: unrecognized option: x")
    monkeypatch.setattr(sb, "_proc_comm_names",
                        lambda: {"sh", "crond"}, raising=False)
    assert sb._cron_daemon_running() is True


def test_daemon_preflight_honest_false_when_no_ps_and_no_proc(
        fake, monkeypatch):
    fake.route(lambda a: a[0] == "ps", rc=1, err="ps: unrecognized option: x")
    monkeypatch.setattr(sb, "_proc_comm_names", lambda: None, raising=False)
    assert sb._cron_daemon_running() is False


def test_is_wsl_false_inside_docker_container(monkeypatch):
    # Tester finding #4: on Docker-Desktop-for-Windows every container's
    # /proc/version says "microsoft" -- a container is not a WSL user
    # session, and the WSL caveat there is just confusing.
    monkeypatch.setattr(sb, "_in_container", lambda: True, raising=False)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert sb._is_wsl() is False


def _fake_task_xml(claude_dir_str: str) -> str:
    return ("<Task><Actions><Exec><Command>C:\\P\\pythonw.exe</Command>"
            "<Arguments>-m claude_session_backup backup --quiet "
            f"--claude-dir {claude_dir_str} --log-file "
            f"{claude_dir_str}\\csb-logs\\schedule.log"
            "</Arguments></Exec></Actions></Task>")


def test_win_remove_refuses_entry_for_different_store(fake, tmp_path):
    # Tester finding #5 -- the near-miss: cleanup's remove would have
    # silently deleted a production entry installed for ANOTHER store.
    fake.route(_is("schtasks", "/query", "/xml"), rc=0,
               out=_fake_task_xml(r"C:\Other\store"))
    r = sb.WindowsTaskBackend().remove(str(tmp_path))
    assert not r.ok and r.was_installed
    assert "different store" in r.detail
    assert not any("/delete" in c for c in fake.calls)


def test_win_remove_proceeds_when_store_matches(fake, tmp_path):
    from claude_session_backup.pathkit import ClaudePaths
    resolved = str(ClaudePaths.from_dir(tmp_path).root)
    fake.route(_is("schtasks", "/query", "/xml"), rc=0,
               out=_fake_task_xml(resolved))
    fake.route(_is("schtasks", "/delete"), rc=0)
    r = sb.WindowsTaskBackend().remove(str(tmp_path))
    assert r.ok and r.was_installed


def test_win_remove_legacy_entry_without_claude_dir_still_removes(
        fake, tmp_path):
    # No --claude-dir marker in the artifact -> nothing to mismatch;
    # the snapshot is the safety net (existing behavior preserved).
    fake.route(_is("schtasks", "/query", "/xml"), rc=0, out="<Task>old</Task>")
    fake.route(_is("schtasks", "/delete"), rc=0)
    r = sb.WindowsTaskBackend().remove(str(tmp_path))
    assert r.ok and r.was_installed


def test_cron_remove_refuses_block_for_different_store(fake, tmp_path):
    other = ScheduleSpec(
        interval_minutes=1440, fire_hour=14, fire_minute=37,
        start_boundary="2026-08-06T14:37:00",
        python_exe="/usr/bin/python3",
        claude_dir="/other/store", log_file="/other/store/csb-logs/s.log")
    current = sb.merge_crontab("MAILTO=me\n", render_crontab_block(other))
    fake.route(_is("crontab", "-l"), rc=0, out=current)
    r = sb.CrontabBackend().remove(str(tmp_path))
    assert not r.ok and r.was_installed
    assert "different store" in r.detail
    assert not any(a == ["crontab", "-"] for a in fake.calls)


def test_cron_remove_guard_scopes_to_the_block_not_user_lines(
        fake, tmp_path):
    # A user line mentioning the target store must not fool the guard
    # when the BLOCK belongs to another store.
    other = ScheduleSpec(
        interval_minutes=1440, fire_hour=14, fire_minute=37,
        start_boundary="2026-08-06T14:37:00",
        python_exe="/usr/bin/python3",
        claude_dir="/other/store", log_file="/other/store/csb-logs/s.log")
    from claude_session_backup.pathkit import ClaudePaths
    resolved = str(ClaudePaths.from_dir(tmp_path).root)
    user_lines = f"# my notes about {resolved}\nMAILTO=me\n"
    current = sb.merge_crontab(user_lines, render_crontab_block(other))
    fake.route(_is("crontab", "-l"), rc=0, out=current)
    r = sb.CrontabBackend().remove(str(tmp_path))
    assert not r.ok and "different store" in r.detail


def test_win_install_notes_replacing_other_stores_entry(fake, tmp_path):
    fake.route(_is("schtasks", "/query", "/xml"), rc=0,
               out=_fake_task_xml(r"C:\Other\store"))
    fake.route(_is("schtasks", "/create"), rc=0)
    fake.route(lambda a: "Claude Session Backup" in a, rc=1)
    r = sb.WindowsTaskBackend().install(spec_for(tmp_path))
    assert r.ok and r.backup_path
    assert "DIFFERENT store" in r.detail


def test_backends_describe_their_entry():
    # Field-caught (taskschd.msc hunt): --status must NAME the entry so
    # users know what to look for in their scheduler UI.
    assert sb.WINDOWS_TASK_NAME in sb.WindowsTaskBackend().describe_entry()
    assert "crontab" in sb.CrontabBackend().describe_entry()
    assert sb.LAUNCHD_LABEL in sb.LaunchdBackend().describe_entry()
"""OS scheduler backends for ``csb setup schedule`` (#69).

``schedule.py`` renders (pure text, golden-tested); THIS module talks to
the operating system. One backend per scheduler, all behind the same
small surface, and :func:`choose_backend` is the ONLY platform gate in
the feature (the ``liveness.py`` contract -- no ``platform`` branching
in command logic).

Every mechanism here was probed before it was code (results in the
probe docstrings, `tests/one-offs/`):

  Windows  P1: non-elevated ``schtasks /create /xml`` works; UTF-8 XML
           accepted; ``/query /v`` yields Last Run/Result readback;
           transient ``267009`` (SCHED_S_TASK_RUNNING) means RUNNING,
           never failure; ``/delete`` of a missing task rc=1 -> mapped
           to success (AC-4 remove-nothing).
  POSIX    P4: 3/4 minimal images ship NO cron -- detection is a
           first-class path, not an edge case. The pre-flight checks
           for a RUNNING daemon (the binary works against the spool
           with no daemon alive -- the worst silent failure).
  All      P2: the scheduler environment is NOT the shell (all CLAUDE*
           vars absent, PATH differs) -- which is why entries carry
           fully-baked context and the backends never rely on env.

Backup-before-mutate (Delta-1): every install/remove snapshots what it
is about to replace under ``<claude_dir>/backups/csb-schedule/`` --
deliberately inside ``backups/`` (USER_DIRS) so snapshots ride the user
commit: a user's crontab is THEIR data.

Locale note (Windows status readback): ``schtasks /query /v /fo LIST``
labels are localized; v1 parses the English labels and degrades to
"os readback unavailable" elsewhere -- the other evidence layers
(run log, index/git) are locale-independent. Tracked as a polish item.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Protocol

from .pathkit import ClaudePaths
from .schedule import (
    CRON_BLOCK_BEGIN,
    LAUNCHD_LABEL,
    WINDOWS_TASK_NAME,
    ScheduleSpec,
    build_command_argv,
    merge_crontab,
    remove_from_crontab,
    render_crontab_block,
    render_plist,
    render_task_xml,
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

#: SCHED_S_TASK_RUNNING -- the task is currently executing (P1 observed
#: this querying during pythonw startup). A state, not an error.
SCHTASKS_RUNNING = 267009

#: Daemon names accepted by the POSIX pre-flight (AC-7 detection list).
CRON_DAEMONS = ("cron", "crond", "fcron")


def _run(args: list[str], stdin_text: Optional[str] = None,
         timeout: int = 60) -> subprocess.CompletedProcess:
    """One spawn helper for every backend: captured, windowless, bounded."""
    return subprocess.run(
        args, input=stdin_text, capture_output=True, text=True,
        timeout=timeout, creationflags=_NO_WINDOW,
    )


# ── Results ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DetectResult:
    available: bool
    reason: str = ""            # human line when unavailable
    is_wsl: bool = False        # AC-8: install proceeds with a caveat


@dataclass(frozen=True)
class InstallResult:
    ok: bool
    detail: str = ""
    backup_path: Optional[str] = None   # Delta-1 snapshot, when one was taken
    deferred: bool = False              # AC-6: written but loads at next login


@dataclass(frozen=True)
class RemoveResult:
    ok: bool
    was_installed: bool
    detail: str = ""
    backup_path: Optional[str] = None


@dataclass(frozen=True)
class BackendStatus:
    installed: bool
    running_now: bool = False           # Windows 267009
    last_run: str = ""                  # OS readback, "" when unavailable
    last_result: Optional[int] = None
    readback_available: bool = True     # False -> locale/api gap, not absence
    detail: str = ""


def _snapshot(claude_dir: str, name: str, content: str) -> Optional[str]:
    """Delta-1 backup-before-mutate; failure never blocks the operation."""
    try:
        d = ClaudePaths.from_dir(claude_dir).schedule_backups
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        p = d / f"{stamp}-{name}"
        p.write_text(content, encoding="utf-8")
        return str(p)
    except OSError:
        return None


# ── The protocol ────────────────────────────────────────────────────────


class SchedulerBackend(Protocol):
    name: str

    def detect(self) -> DetectResult: ...
    def install(self, spec: ScheduleSpec) -> InstallResult: ...
    def remove(self, claude_dir: str) -> RemoveResult: ...
    def status(self, spec: Optional[ScheduleSpec] = None) -> BackendStatus: ...
    def prime(self, spec: ScheduleSpec) -> tuple[bool, str]: ...


# ── Windows: Task Scheduler via task XML (AC-5, P1-proven) ──────────────


class WindowsTaskBackend:
    name = "windows-task-scheduler"

    def detect(self) -> DetectResult:
        # schtasks.exe ships in System32 on every supported Windows.
        return DetectResult(available=True)

    def _query_xml(self) -> Optional[str]:
        r = _run(["schtasks", "/query", "/tn", WINDOWS_TASK_NAME, "/xml"])
        return r.stdout if r.returncode == 0 else None

    def install(self, spec: ScheduleSpec) -> InstallResult:
        backup = None
        existing = self._query_xml()
        if existing is not None:
            backup = _snapshot(spec.claude_dir, "task.xml", existing)
        xml = render_task_xml(spec)
        # P1: UTF-8 file accepted (tried first, succeeded). Write without
        # BOM; schtasks sniffs content.
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".xml", delete=False
        ) as f:
            f.write(xml)
            tmp = f.name
        try:
            r = _run(["schtasks", "/create", "/tn", WINDOWS_TASK_NAME,
                      "/xml", tmp, "/f"])
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if r.returncode != 0:
            return InstallResult(False, (r.stderr or r.stdout).strip(),
                                 backup_path=backup)
        return InstallResult(True, f"task {WINDOWS_TASK_NAME} registered",
                             backup_path=backup)

    def remove(self, claude_dir: str) -> RemoveResult:
        backup = None
        existing = self._query_xml()
        if existing is not None:
            backup = _snapshot(claude_dir, "task.xml", existing)
        r = _run(["schtasks", "/delete", "/tn", WINDOWS_TASK_NAME, "/f"])
        if r.returncode == 0:
            return RemoveResult(True, was_installed=True,
                                detail="task removed", backup_path=backup)
        # P1: rc=1 "cannot find the file" when absent -- remove-nothing
        # exits clean (AC-4).
        return RemoveResult(True, was_installed=False,
                            detail="no task was installed")

    def status(self, spec: Optional[ScheduleSpec] = None) -> BackendStatus:
        if self._query_xml() is None:
            return BackendStatus(installed=False)
        v = _run(["schtasks", "/query", "/tn", WINDOWS_TASK_NAME,
                  "/v", "/fo", "LIST"])
        last_run, last_result = "", None
        for line in v.stdout.splitlines():
            if line.startswith("Last Run Time:"):
                last_run = line.split(":", 1)[1].strip()
            elif line.startswith("Last Result:"):
                try:
                    last_result = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        if last_result is None and not last_run:
            # Localized labels (see module docstring) -- readback gap,
            # not absence.
            return BackendStatus(installed=True, readback_available=False,
                                 detail="os readback unavailable (locale)")
        return BackendStatus(
            installed=True,
            running_now=(last_result == SCHTASKS_RUNNING),
            last_run=last_run,
            last_result=last_result,
        )

    def prime(self, spec: ScheduleSpec) -> tuple[bool, str]:
        # Delta-14: through the scheduler's own launcher, so PATH/env
        # failures surface NOW, not at hour 24.
        r = _run(["schtasks", "/run", "/tn", WINDOWS_TASK_NAME])
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()
        return True, "prime run dispatched via Task Scheduler"


# ── POSIX: crontab (Linux / BSD / WSL -- AC-4/AC-7, P4-informed) ───────


def _read_crontab() -> tuple[str, Optional[str]]:
    """(current crontab text, error) -- empty-spool rc=1 is NOT an error."""
    r = _run(["crontab", "-l"])
    if r.returncode == 0:
        return r.stdout, None
    # Every mainstream cron prints a no-crontab notice to stderr with
    # rc 1 for an empty spool; treat as empty. Real denials
    # (cron.allow/deny) also rc!=0 -- surface stderr verbatim (AC-7)
    # so the caller can tell the difference.
    if "no crontab" in (r.stderr or "").lower():
        return "", None
    return "", (r.stderr or r.stdout).strip() or "crontab -l failed"


class CrontabBackend:
    name = "crontab"

    def detect(self) -> DetectResult:
        import shutil
        if shutil.which("crontab") is None:
            return DetectResult(False, reason="no crontab binary",
                                is_wsl=_is_wsl())
        if not _cron_daemon_running():
            # The worst silent failure: spool writable, nothing reads it.
            return DetectResult(False, reason="no running cron daemon",
                                is_wsl=_is_wsl())
        return DetectResult(True, is_wsl=_is_wsl())

    def install(self, spec: ScheduleSpec) -> InstallResult:
        current, err = _read_crontab()
        if err:
            return InstallResult(False, f"cannot read crontab: {err}")
        backup = _snapshot(spec.claude_dir, "crontab.txt", current) \
            if current.strip() else None
        merged = merge_crontab(current, render_crontab_block(spec))
        r = _run(["crontab", "-"], stdin_text=merged)
        if r.returncode != 0:
            return InstallResult(False,
                                 (r.stderr or r.stdout).strip(),
                                 backup_path=backup)
        return InstallResult(True, "crontab block installed",
                             backup_path=backup)

    def remove(self, claude_dir: str) -> RemoveResult:
        current, err = _read_crontab()
        if err:
            return RemoveResult(False, was_installed=False,
                                detail=f"cannot read crontab: {err}")
        if CRON_BLOCK_BEGIN not in current:
            return RemoveResult(True, was_installed=False,
                                detail="no csb block in crontab")
        backup = _snapshot(claude_dir, "crontab.txt", current)
        r = _run(["crontab", "-"], stdin_text=remove_from_crontab(current))
        if r.returncode != 0:
            return RemoveResult(False, was_installed=True,
                                detail=(r.stderr or r.stdout).strip(),
                                backup_path=backup)
        return RemoveResult(True, was_installed=True,
                            detail="csb block removed", backup_path=backup)

    def status(self, spec: Optional[ScheduleSpec] = None) -> BackendStatus:
        current, err = _read_crontab()
        if err:
            return BackendStatus(installed=False, readback_available=False,
                                 detail=err)
        installed = CRON_BLOCK_BEGIN in current
        # cron has NO per-job execution readback (Rnd2 D3) -- the run
        # log and index/git are the evidence layers here.
        return BackendStatus(installed=installed, readback_available=False,
                             detail="cron provides no execution readback; "
                                    "see run log")

    def prime(self, spec: ScheduleSpec) -> tuple[bool, str]:
        # No "fire this crontab line now" API exists: run the exact
        # rendered command directly. Honest limitation (Rnd4 Addendum 2):
        # proves the COMMAND, not the daemon's delivery of it.
        r = _run(build_command_argv(spec), timeout=600)
        if r.returncode != 0:
            return False, f"prime run rc={r.returncode}"
        return True, "prime run ok (command proven; daemon delivery " \
                     "proven at first real fire)"


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(
            encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def _cron_daemon_running() -> bool:
    """Process-table pre-flight (AC-7). `-axo`, never `-eo` (FreeBSD:
    `-e` means environment -- the liveness.py lesson)."""
    r = _run(["ps", "-axo", "comm="])
    if r.returncode != 0:
        return False
    names = {line.strip().rsplit("/", 1)[-1]
             for line in r.stdout.splitlines() if line.strip()}
    if names & set(CRON_DAEMONS):
        return True
    # busybox crond shows as "crond" (covered) or "busybox" -- check argv.
    if "busybox" in names:
        r2 = _run(["ps", "-axo", "args="])
        return any("crond" in ln for ln in r2.stdout.splitlines()
                   if "busybox" in ln)
    return False


# ── macOS: launchd LaunchAgent (AC-6; UNVERIFIED-ON-HARDWARE) ──────────


class LaunchdBackend:
    name = "launchd"

    def _plist_path(self) -> Path:
        return (Path.home() / "Library" / "LaunchAgents"
                / f"{LAUNCHD_LABEL}.plist")

    def detect(self) -> DetectResult:
        import shutil
        if shutil.which("launchctl") is None:
            return DetectResult(False, reason="no launchctl")
        return DetectResult(True)

    def install(self, spec: ScheduleSpec) -> InstallResult:
        plist = self._plist_path()
        backup = None
        if plist.exists():
            backup = _snapshot(spec.claude_dir, "agent.plist",
                               plist.read_text(encoding="utf-8"))
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(render_plist(spec), encoding="utf-8")
        uid = os.getuid()  # POSIX-only path; never reached on Windows
        r = _run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)])
        if r.returncode != 0:
            # AC-6 honesty: over SSH with no GUI session bootstrap can
            # fail -- written-but-deferred is reported as exactly that,
            # never as installed-and-running.
            return InstallResult(True, deferred=True, backup_path=backup,
                                 detail="plist written; loads at next login "
                                        f"(bootstrap: {(r.stderr or '').strip()})")
        return InstallResult(True, "agent bootstrapped", backup_path=backup)

    def remove(self, claude_dir: str) -> RemoveResult:
        plist = self._plist_path()
        if not plist.exists():
            return RemoveResult(True, was_installed=False,
                                detail="no agent was installed")
        backup = _snapshot(claude_dir, "agent.plist",
                           plist.read_text(encoding="utf-8"))
        uid = os.getuid()
        _run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"])
        try:
            plist.unlink()
        except OSError as e:
            return RemoveResult(False, was_installed=True, detail=str(e),
                                backup_path=backup)
        return RemoveResult(True, was_installed=True,
                            detail="agent removed", backup_path=backup)

    def status(self, spec: Optional[ScheduleSpec] = None) -> BackendStatus:
        if not self._plist_path().exists():
            return BackendStatus(installed=False)
        uid = os.getuid()
        r = _run(["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"])
        return BackendStatus(installed=True,
                             readback_available=(r.returncode == 0),
                             detail="loaded" if r.returncode == 0
                                    else "plist present; not loaded "
                                         "(loads at next login)")

    def prime(self, spec: ScheduleSpec) -> tuple[bool, str]:
        uid = os.getuid()
        r = _run(["launchctl", "kickstart",
                  f"gui/{uid}/{LAUNCHD_LABEL}"])
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()
        return True, "prime run dispatched via launchd"


# ── The single platform gate ────────────────────────────────────────────


def choose_backend() -> SchedulerBackend:
    """THE only platform branch in the scheduled-backup feature (AC-2)."""
    if sys.platform == "win32":
        return WindowsTaskBackend()
    if sys.platform == "darwin":
        return LaunchdBackend()
    return CrontabBackend()


# ── AC-7 refusal text (verbatim from the consultation, golden-tested) ──

REFUSAL_TEXT = """\
csb: cannot install a scheduled backup on this machine.

  A user crontab needs a running cron daemon, and none was found
  (checked the process table for: cron, crond, fcron, busybox crond).
  Writing a crontab entry anyway would LOOK installed while nothing
  ever runs it -- a backup that silently never fires is worse than
  a clear no.

To fix it, pick one:

  [1] Install cron (recommended -- runs your jobs even when you are
      not logged in):
        Debian/Ubuntu:   sudo apt install cron
        RHEL/Fedora:     sudo dnf install cronie
        Alpine:          doas apk add dcron
        FreeBSD/OpenBSD/NetBSD: cron ships in the base system; if it
          is not running it was disabled -- re-enable it via rc.conf
          (FreeBSD/NetBSD) or rcctl (OpenBSD).
      Then re-run:  csb setup schedule

  [2] Use a systemd user timer instead (no cron needed):
        csb setup schedule --print-systemd
      prints ready-to-paste unit + timer files and the commands to
      enable them.
      WARNING: user timers stop when your last login session ends,
      unless lingering is enabled:  loginctl enable-linger $USER
      (may require root). Without lingering, a machine you are not
      logged in to -- the exact machine this feature protects --
      gets NO scheduled runs.

Nothing was installed or modified.
`csb setup schedule --dry-run` shows what an install would write.
"""

#: Distinct exit code for the refused state (Delta-7; precedent:
#: CHECK_GAP_EXIT = 10 in commands.py).
SCHEDULE_REFUSED_EXIT = 11


def render_systemd_recipe(spec: ScheduleSpec) -> str:
    """`--print-systemd` (Delta-5): a pure-text recipe, never owned.

    We print a systemd user timer; we do not install, enable, or manage
    it -- D2's no-systemd-backend boundary holds. UNVERIFIED-ON-HARDWARE
    like the macOS path; the recipe states the linger caveat inline.
    """
    argv = " ".join(build_command_argv(spec))
    n = spec.interval_minutes
    if n >= 60:
        cal = f"*-*-* {'/'.join([f'{spec.fire_hour:02d}'])}:{spec.fire_minute:02d}:00" \
            if n == 1440 else f"*-*-* 00/{n // 60}:{spec.fire_minute:02d}:00"
    else:
        cal = f"*-*-* *:00/{n}:00"
    return f"""\
# csb scheduled backup via systemd user timer (printed by csb, not managed)
# 1. Save as ~/.config/systemd/user/csb-backup.service
[Unit]
Description=csb scheduled backup

[Service]
Type=oneshot
ExecStart={argv}

# 2. Save as ~/.config/systemd/user/csb-backup.timer
[Unit]
Description=csb scheduled backup timer

[Timer]
OnCalendar={cal}
Persistent=true

[Install]
WantedBy=timers.target

# 3. Enable:
#      systemctl --user daemon-reload
#      systemctl --user enable --now csb-backup.timer
# 4. IMPORTANT -- if you log out, user timers stop unless lingering
#    is on:  loginctl enable-linger $USER   (may require root)
"""

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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Protocol

from .pathkit import ClaudePaths
from .schedule import (
    CRON_BLOCK_BEGIN,
    CRON_BLOCK_END,
    LAUNCHD_LABEL,
    WINDOWS_LEGACY_TASK_NAME,
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


def _entry_targets_other_store(entry_text: str, claude_dir: str) -> bool:
    """True when the installed entry's baked ``--claude-dir`` is NOT the
    store this command was pointed at (tester finding #5 -- the remove
    near-miss: one fixed entry name per user means a remove aimed at
    store A can land on an entry protecting store B; deleting it would
    silently kill B's backups).

    Substring check on the resolved root, deliberately: both install and
    remove normalize through ``ClaudePaths.from_dir`` so same-store
    spellings converge, and the baked ``--log-file`` path shares the root
    prefix, so a same-store entry always contains it. Entries carrying no
    ``--claude-dir`` marker can't mismatch (pre-marker/legacy artifacts;
    the pre-mutate snapshot remains the net there).
    """
    if "--claude-dir" not in entry_text:
        return False
    try:
        resolved = str(ClaudePaths.from_dir(claude_dir).root)
    except OSError:
        return False
    return resolved not in entry_text


_OTHER_STORE_REFUSAL = (
    "the installed entry backs up a different store (its baked --claude-dir "
    "is not {target}); refusing to remove it -- re-run with --claude-dir "
    "pointing at the store that entry protects"
)


# ── The protocol ────────────────────────────────────────────────────────


class SchedulerBackend(Protocol):
    name: str

    def detect(self) -> DetectResult: ...
    def render(self, spec: ScheduleSpec) -> str: ...   # --dry-run artifact
    def describe_entry(self) -> str: ...               # what to look for in the OS UI
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

    def render(self, spec: ScheduleSpec) -> str:
        return render_task_xml(spec)

    def describe_entry(self) -> str:
        # Field-caught: a user in taskschd.msc has no way to know the name.
        return (f'task "{WINDOWS_TASK_NAME}" at the root of the '
                f'Task Scheduler Library (taskschd.msc)')

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
        detail = f"task {WINDOWS_TASK_NAME} registered"
        if existing is not None and _entry_targets_other_store(
                existing, spec.claude_dir):
            # The overwrite was legal (one entry name per user), but the
            # user should know they just repointed the machine's ONLY
            # scheduled backup at a different store.
            detail += ("; note: the replaced entry backed up a DIFFERENT "
                       "store (its snapshot was saved)")
        # Legacy hand-rolled task from the pre-#69 docs snippet: harmless
        # (the backup lock serializes), but it double-runs backups and its
        # bare `csb` breaks silently on PATH changes -- tell the user.
        legacy = _run(["schtasks", "/query", "/tn", WINDOWS_LEGACY_TASK_NAME])
        if legacy.returncode == 0:
            detail += (
                f"; note: a legacy '{WINDOWS_LEGACY_TASK_NAME}' task from the "
                f"old hand-rolled setup also exists -- remove it with: "
                f'schtasks /delete /tn "{WINDOWS_LEGACY_TASK_NAME}" /f'
            )
        return InstallResult(True, detail, backup_path=backup)

    def remove(self, claude_dir: str) -> RemoveResult:
        existing = self._query_xml()
        if existing is not None and _entry_targets_other_store(
                existing, claude_dir):
            return RemoveResult(
                False, was_installed=True,
                detail=_OTHER_STORE_REFUSAL.format(target=claude_dir))
        backup = None
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


def _extract_cron_block(text: str) -> str:
    """Just the lines INSIDE csb's marker fence (guard scope for the
    other-store check -- user lines outside the fence are not evidence
    of what the block targets)."""
    inside, out = False, []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == CRON_BLOCK_BEGIN:
            inside = True
            continue
        if stripped == CRON_BLOCK_END:
            inside = False
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


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

    def render(self, spec: ScheduleSpec) -> str:
        return render_crontab_block(spec)

    def describe_entry(self) -> str:
        return ('fenced "csb scheduled backup" block in your user crontab '
                '(crontab -l)')

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
        detail = "crontab block installed"
        old_block = _extract_cron_block(current)
        if old_block and _entry_targets_other_store(old_block,
                                                    spec.claude_dir):
            detail += ("; note: the replaced block backed up a DIFFERENT "
                       "store (your previous crontab was snapshotted)")
        return InstallResult(True, detail, backup_path=backup)

    def remove(self, claude_dir: str) -> RemoveResult:
        current, err = _read_crontab()
        if err:
            return RemoveResult(False, was_installed=False,
                                detail=f"cannot read crontab: {err}")
        if CRON_BLOCK_BEGIN not in current:
            return RemoveResult(True, was_installed=False,
                                detail="no csb block in crontab")
        # Guard scope is the BLOCK alone -- a user line that merely
        # mentions the target path must not defeat the mismatch check.
        if _entry_targets_other_store(_extract_cron_block(current),
                                      claude_dir):
            return RemoveResult(
                False, was_installed=True,
                detail=_OTHER_STORE_REFUSAL.format(target=claude_dir))
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


def _in_container() -> bool:
    """Docker/podman detection. Needed because Docker Desktop's WSL2
    backend stamps "microsoft" into EVERY container's /proc/version
    (tester finding #4) -- a container is not a WSL user session, and
    the WSL caveat inside one is just noise."""
    try:
        return (Path("/.dockerenv").exists()
                or Path("/run/.containerenv").exists())
    except OSError:
        return False


def _is_wsl() -> bool:
    if _in_container():
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(
            encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def _proc_comm_names() -> Optional[set]:
    """Process names via /proc/<pid>/comm -- the fallback for Linuxes
    whose ``ps`` can't do ``-axo`` (stock Alpine BusyBox, tester finding
    #2) or that ship no ``ps`` at all. None when /proc isn't available
    (non-Linux), so the caller can tell "no processes" from "no view"."""
    proc = Path("/proc")
    try:
        if not proc.is_dir():
            return None
        names = set()
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                names.add((entry / "comm").read_text(
                    encoding="utf-8", errors="replace").strip())
            except OSError:
                continue  # process exited mid-scan
        return names or None
    except OSError:
        return None


def _cron_daemon_running() -> bool:
    """Process-table pre-flight (AC-7). `-axo`, never `-eo` (FreeBSD:
    `-e` means environment -- the liveness.py lesson). When ps itself
    is missing or rejects -axo (BusyBox), fall back to /proc scanning
    rather than refusing a genuinely-running daemon."""
    try:
        r = _run(["ps", "-axo", "comm="])
    except OSError:
        r = None
    if r is not None and r.returncode == 0:
        names = {line.strip().rsplit("/", 1)[-1]
                 for line in r.stdout.splitlines() if line.strip()}
        if names & set(CRON_DAEMONS):
            return True
        # busybox crond shows as "crond" (covered) or "busybox" -- check
        # argv (safe here: -axo just worked once).
        if "busybox" in names:
            r2 = _run(["ps", "-axo", "args="])
            return any("crond" in ln for ln in r2.stdout.splitlines()
                       if "busybox" in ln)
        return False
    names = _proc_comm_names()
    if names is None:
        return False  # no ps AND no /proc: honestly unknowable -> refuse
    return bool(names & set(CRON_DAEMONS))


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

    def render(self, spec: ScheduleSpec) -> str:
        return render_plist(spec)

    def describe_entry(self) -> str:
        return f"LaunchAgent plist at {self._plist_path()}"

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
        plist_text = plist.read_text(encoding="utf-8")
        if _entry_targets_other_store(plist_text, claude_dir):
            return RemoveResult(
                False, was_installed=True,
                detail=_OTHER_STORE_REFUSAL.format(target=claude_dir))
        backup = _snapshot(claude_dir, "agent.plist", plist_text)
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


# ── The three-layer evidence verdict (AC-14, D3) ────────────────────────

import re as _re

_RUNLOG_TS_RE = _re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}) outcome=([\w-]+)")


@dataclass(frozen=True)
class EvidenceVerdict:
    """What `csb status` renders. States:

    ``not-installed``          no entry registered (+ hint)
    ``ok``                     evidence of a run within 2x interval
    ``pending``                installed, no evidence yet -- the Delta-14
                               prime run should have produced some, so
                               this is itself a soft warning
    ``installed-not-running``  entry present, newest evidence older than
                               2x interval -- THE verdict this feature
                               exists to be able to say (prior art's
                               field detection latency was indefinite)
    """
    state: str
    detail: str
    newest_evidence: Optional[datetime] = None


def _newest_runlog_entry(log_path: Path) -> Optional[tuple[datetime, str]]:
    """Newest (timestamp, outcome) across schedule.log and its .1 rotation."""
    newest: Optional[tuple[datetime, str]] = None
    for p in (log_path, log_path.with_suffix(log_path.suffix + ".1")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in reversed(text.splitlines()):
            m = _RUNLOG_TS_RE.match(line)
            if m:
                try:
                    ts = datetime.fromisoformat(m.group(1))
                except ValueError:
                    continue
                if newest is None or ts > newest[0]:
                    newest = (ts, m.group(2))
                break
    return newest


def evaluate_schedule_evidence(
    backend_status: BackendStatus,
    interval_minutes: int,
    log_path: Path,
    now: Optional[datetime] = None,
) -> EvidenceVerdict:
    """Pure aggregation of the evidence layers into one honest verdict.

    Layer 1 (OS readback) is corroboration only -- cron has none, and
    Windows labels are locale-fragile. Layer 2, csb's OWN run log, is
    the portable execution evidence and decides the verdict. (Layer 3,
    last-backup-from-index/git, arrives with the `csb status` wiring --
    it cannot distinguish scheduled from hook runs, so it can only ever
    soften, not decide.) The 2x-interval threshold ships annotated as
    uncalibrated-but-strictly-better-than-prior-art (Delta-6).
    """
    if not backend_status.installed:
        return EvidenceVerdict(
            "not-installed",
            "no scheduled backup entry -- install with: csb setup schedule")
    entry = _newest_runlog_entry(log_path)
    if entry is None:
        if backend_status.running_now:
            # Field-caught: --status during the install-time prime read
            # as an alarm ("re-run setup") while everything was working.
            # When the OS says a run is executing, say THAT.
            return EvidenceVerdict(
                "pending",
                "installed; a run is executing right now -- check again "
                "in a minute")
        return EvidenceVerdict(
            "pending",
            "installed, but no run evidence yet -- the install-time prime "
            "run should have written a log line; re-run `csb setup "
            "schedule` if this persists")
    newest, outcome = entry
    now = now or datetime.now().astimezone()
    age = now - newest
    threshold = timedelta(minutes=2 * interval_minutes)
    if age <= threshold:
        detail = (f"last scheduled run {_human_age(age)} ago (expected "
                  f"every {_human_interval(interval_minutes)})")
        if outcome == "error":
            # The schedule fires; the BACKUP fails (e.g. repo-less
            # store). Execution evidence alone must not read as
            # all-clear.
            detail += (" -- the last run reported an ERROR; see the run "
                       "log")
        return EvidenceVerdict("ok", detail, newest_evidence=newest)
    detail = (f"INSTALLED BUT NOT RUNNING -- last evidence "
              f"{_human_age(age)} ago, expected every "
              f"{_human_interval(interval_minutes)}")
    if backend_status.running_now:
        detail += " (a run is executing right now)"
    return EvidenceVerdict("installed-not-running", detail,
                           newest_evidence=newest)


def _human_age(delta: "timedelta") -> str:
    s = int(delta.total_seconds())
    if s < 90 * 60:
        return f"{max(s // 60, 0)}m"
    if s < 48 * 3600:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _human_interval(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 1440:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"


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
    note = ""
    if ":" in spec.python_exe[:3] or "\\" in spec.python_exe:
        # Tester finding #3: rendered on Windows, ExecStart would carry
        # C:\...\pythonw.exe -- meaningless on the Linux target. Say so.
        note = (
            "# NOTE: this recipe was generated on Windows -- the absolute\n"
            "# paths below are THIS machine's and will not exist on the\n"
            "# Linux target. Run `csb setup schedule --print-systemd` on\n"
            "# the target machine itself for correct paths; treat this\n"
            "# output as a shape preview only.\n\n")
    return note + f"""\
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

"""Scheduled-backup entry generation (#69, Phase P3: the pure core).

This module is the renderer half of ``csb setup schedule``: a frozen
:class:`ScheduleSpec` describing ONE scheduled ``csb backup`` run, and
three pure-text renderers that turn it into the platform artifact:

  - Windows Task Scheduler task XML (registered via ``schtasks /create /xml``)
  - macOS launchd LaunchAgent plist (``StartCalendarInterval`` -- wake-coalescing)
  - a marker-owned crontab block (Linux / BSD / WSL share this renderer)

Everything here is deliberately side-effect free so the correctness-critical
text is golden-file testable (AC-5) before any code touches an OS scheduler.
The install/remove/status shells and backend selection live elsewhere; the
ONLY platform knowledge in this file is what each artifact format requires.

Contract anchors (see the #69 design trail, DISCUSS_Rnd2/Rnd3 round files):

  AC-3   entries carry an absolute interpreter + ``-m claude_session_backup``,
         baked ``--claude-dir`` (and ``--db`` when non-default), ``--log-file``;
         never a bare ``csb``. :func:`validate_entry_argv` enforces it.
  AC-4   crontab ownership is a marker block; :func:`merge_crontab` replaces
         only csb's own block and leaves every foreign line byte-identical.
  AC-5   Windows XML sets the battery/missed-start/instances overrides the
         schtasks flag surface cannot express (defaults verified 2026-08-06:
         DisallowStartIfOnBatteries=True etc. -- the shipped defaults would
         silently skip laptops on battery).
  AC-11  POSIX entries append shell redirection into the schedule log; the
         csb ``--log-file`` line is the second evidence layer (two altitudes:
         redirection catches "python never started", --log-file catches
         everything after).
  Delta-11 (interval decision addendum) for intervals >= 60 min the fire time is
         DERIVED FROM INSTALL TIME, never a hardcoded midnight -- a daily
         cron job at 00:00 on a 9-to-5 desktop never runs (cron has no
         catch-up); firing at the hour the user demonstrably uses the
         machine is what makes a 24h default honest.
  Delta-3   the interval grammar is identical on every platform (parity by
         subtraction -- the reddit-slack lesson): a divisor of 60 minutes,
         or a whole number of hours that divides 24.
"""

from __future__ import annotations

import re
import shlex
import plistlib
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from xml.sax.saxutils import escape as _xml_escape

# ── Fixed identities (idempotency anchors -- AC-4) ──────────────────────
# One name per platform artifact. Install replaces its own artifact by
# name/marker; remove deletes only these. Version-to-version renames are
# forbidden (name drift leaves two entries running).

WINDOWS_TASK_NAME = "csb-scheduled-backup"
#: The old hand-install snippet's name (docs/automation.md); install offers
#: to detect/warn so a manual task doesn't silently double-run (AC-19).
WINDOWS_LEGACY_TASK_NAME = "Claude Session Backup"

LAUNCHD_LABEL = "com.dazzleml.csb-backup"

CRON_BLOCK_BEGIN = "# >>> csb scheduled backup -- managed by `csb setup schedule`; do not hand-edit this block >>>"
CRON_BLOCK_END = "# <<< csb scheduled backup <<<"

#: Interval presets surfaced by guided setup ({15 min, 12 hr, 24 hr, custom},
#: 24h preselected -- the user's decision, Rnd4 Addendum 1).
INTERVAL_PRESETS = (15, 720, 1440)
DEFAULT_INTERVAL_MINUTES = 1440


class ScheduleError(ValueError):
    """A spec or rendered entry violates the #69 contract."""


# ── The spec ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScheduleSpec:
    """Everything needed to render one scheduled ``csb backup`` entry.

    All context is resolved at INSTALL time and frozen in (the D4 lesson:
    schedulers do not source shell profiles, so nothing may depend on the
    runtime environment -- not the interpreter, not the claude dir, not
    the DB path, not the log path).

    ``python_exe`` is the absolute interpreter -- ``pythonw.exe`` on
    Windows (a console interpreter flashes a conhost window per run and
    gets the feature uninstalled). ``start_boundary`` is the install
    moment as a naive local ISO string; it seeds the Windows trigger and
    is supplied by the caller so rendering stays deterministic.
    """

    interval_minutes: int
    fire_hour: int
    fire_minute: int
    start_boundary: str          # "YYYY-MM-DDTHH:MM:SS", local time
    python_exe: str
    claude_dir: str
    log_file: str
    db_path: Optional[str] = None

    def __post_init__(self) -> None:
        validate_interval(self.interval_minutes)
        if not (0 <= self.fire_hour <= 23 and 0 <= self.fire_minute <= 59):
            raise ScheduleError(
                f"fire time {self.fire_hour:02d}:{self.fire_minute:02d} "
                f"out of range"
            )


def validate_interval(minutes: int) -> None:
    """Enforce the uniform interval grammar (Delta-3 -- parity by subtraction).

    Legal: a divisor of 60 (sub-hour cadence: any uptime of ``minutes``
    guarantees a run), or a whole number of hours that divides 24 (so the
    fire-hour list tiles the day exactly). Everything else is refused on
    EVERY platform rather than becoming a per-platform partial feature.
    """
    if not isinstance(minutes, int) or minutes <= 0:
        raise ScheduleError(f"interval must be a positive integer of minutes, got {minutes!r}")
    if minutes < 60:
        if 60 % minutes != 0:
            raise ScheduleError(
                f"interval {minutes}m is not a divisor of 60 -- "
                f"use one of: 1,2,3,4,5,6,10,12,15,20,30 minutes"
            )
    else:
        if minutes % 60 != 0 or (24 * 60) % minutes != 0:
            raise ScheduleError(
                f"interval {minutes}m must be a whole number of hours that "
                f"divides 24 (60, 120, 180, 240, 360, 480, 720, 1440)"
            )


def derive_fire_time(install_moment: datetime, interval_minutes: int) -> tuple[int, int]:
    """Fire time from the install moment (Delta-11 -- the load-bearing rule).

    For intervals >= 60 min the entry fires anchored at the INSTALL hour
    and minute: the user ran setup then, so the machine is demonstrably in
    use at that time on days they use it. Never a hardcoded midnight --
    on cron (no catch-up) a 00:00 daily job on a 9-to-5 desktop simply
    never runs, while LOOKING installed.

    Sub-hour intervals do not use an anchor (``*/N`` covers every phase);
    (0, 0) is returned and ignored by the renderers.
    """
    if interval_minutes < 60:
        return (0, 0)
    return (install_moment.hour, install_moment.minute)


def fire_hours(spec: ScheduleSpec) -> list[int]:
    """The hour-of-day list an interval >= 60 tiles, anchored at fire_hour.

    1440 -> [H]; 720 -> [H, (H+12) % 24]; 60 -> all 24 hours; etc.
    Sorted ascending because cron hour lists and plist arrays should be
    stable for golden comparison.
    """
    step_hours = spec.interval_minutes // 60
    if step_hours == 0:
        raise ScheduleError("fire_hours() is meaningless for sub-hour intervals")
    count = 24 // step_hours
    return sorted((spec.fire_hour + k * step_hours) % 24 for k in range(count))


# ── Command construction + the bare-name guard (AC-3) ───────────────────


def build_command_argv(spec: ScheduleSpec) -> list[str]:
    """The exact argv every backend schedules.

    ``python -m claude_session_backup`` rather than the ``csb`` console
    script: pip regenerates script wrappers on upgrade, and Windows has no
    ``csbw`` twin for ``pythonw``. All paths absolute; all context frozen.
    """
    argv = [
        spec.python_exe,
        "-m", "claude_session_backup",
        "backup", "--quiet",
        "--claude-dir", spec.claude_dir,
    ]
    if spec.db_path:
        argv += ["--db", spec.db_path]
    argv += ["--log-file", spec.log_file]
    return argv


#: A token that is exactly `csb`/`csb.exe` (not part of a path or a longer
#: word). Matching ANY such token is the red-green tripwire for AC-3.
_BARE_CSB_RE = re.compile(r"(?<![\w.\\/-])csb(?:\.exe)?(?![\w.-])")


def validate_entry_argv(argv: list[str]) -> None:
    """Refuse an entry that would silently fail in a scheduler environment.

    Checks (AC-3): no bare ``csb`` token anywhere; an absolute-looking
    interpreter in argv[0]; ``--claude-dir`` and ``--log-file`` present.
    Used by the golden tests and, later, as an install-time self-check.
    """
    joined = " ".join(argv)
    if _BARE_CSB_RE.search(joined):
        raise ScheduleError(
            "entry contains a bare `csb` -- schedulers run with a minimal "
            "PATH; use an absolute interpreter + -m (AC-3)"
        )
    head = argv[0] if argv else ""
    if not (":" in head[:3] or head.startswith(("/", "\\\\"))):
        raise ScheduleError(f"interpreter {head!r} is not an absolute path (AC-3)")
    for required in ("--claude-dir", "--log-file"):
        if required not in argv:
            raise ScheduleError(f"entry is missing {required} (AC-3/D4)")


# ── Renderer: Windows Task Scheduler XML (AC-5) ─────────────────────────


def _win_quote(token: str) -> str:
    """Quote one token for the XML <Arguments> string (spaces only --
    the <Command>/<Arguments> split dodges the rest of cmd quoting hell)."""
    return f'"{token}"' if " " in token else token


def _iso_interval(minutes: int) -> str:
    return f"PT{minutes}M" if minutes < 60 else f"PT{minutes // 60}H"


def render_task_xml(spec: ScheduleSpec) -> str:
    """Windows task XML -- the ONLY schtasks path that can express AC-5.

    The legacy ``/create`` flag surface has zero battery-related flags
    (verified on real defaults 2026-08-06), so the four overrides below
    are exactly why we generate XML and register with ``/create /xml``.
    ``pythonw.exe`` + InteractiveToken avoids both the conhost flash and
    any credential ceremony (field precedent: Opera's non-elevated task,
    identical settings block).

    Returned as ``str``; the install layer owns encoding (the historical
    UTF-16 preference is P1's explicit check before that writer freezes).
    """
    argv = build_command_argv(spec)
    validate_entry_argv(argv)
    arguments = " ".join(_win_quote(t) for t in argv[1:])

    if spec.interval_minutes < 1440:
        repetition = (
            "        <Repetition>\n"
            f"          <Interval>{_iso_interval(spec.interval_minutes)}</Interval>\n"
            "          <Duration>P1D</Duration>\n"
            "        </Repetition>\n"
        )
    else:
        repetition = ""

    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>csb scheduled backup (issue #69) -- commits Claude Code session\n"
        "work to the backup store even when no session event fires. Remove with:\n"
        "csb setup schedule --remove</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <CalendarTrigger>\n"
        f"      <StartBoundary>{_xml_escape(spec.start_boundary)}</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <ScheduleByDay>\n"
        "        <DaysInterval>1</DaysInterval>\n"
        "      </ScheduleByDay>\n"
        + repetition +
        "    </CalendarTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>\n"
        "    <Enabled>true</Enabled>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{_xml_escape(spec.python_exe)}</Command>\n"
        f"      <Arguments>{_xml_escape(arguments)}</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


# ── Renderer: macOS launchd LaunchAgent plist (AC-6) ────────────────────


def render_plist(spec: ScheduleSpec) -> str:
    """launchd LaunchAgent with ``StartCalendarInterval``.

    Calendar intervals (NOT ``StartInterval``) because launchd coalesces
    missed calendar events and fires once on wake -- ``StartInterval``
    firings missed during sleep are skipped (the D1 correction; the
    original DWP had this inverted).

    ``ProgramArguments`` is a real argv array, so there is no quoting
    problem on this platform at all. stdout/stderr go to the schedule log
    (the pre-interpreter evidence layer, AC-11's launchd analog).
    """
    argv = build_command_argv(spec)
    validate_entry_argv(argv)

    if spec.interval_minutes < 60:
        calendar: list[dict] = [
            {"Minute": m} for m in range(0, 60, spec.interval_minutes)
        ]
    else:
        calendar = [
            {"Hour": h, "Minute": spec.fire_minute} for h in fire_hours(spec)
        ]

    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": argv,
        "StartCalendarInterval": calendar,
        "StandardOutPath": spec.log_file,
        "StandardErrorPath": spec.log_file,
    }
    return plistlib.dumps(payload, sort_keys=True).decode("utf-8")


# ── Renderer: crontab block (Linux / BSD / WSL -- AC-4, AC-11) ──────────


def cron_expression(spec: ScheduleSpec) -> str:
    """The five-field cron time spec for this interval + derived anchor.

    Hour lists are explicit and anchored at the derived fire hour --
    ``*/12`` in the hour field would silently re-anchor at midnight and
    resurrect the exact never-runs hazard Delta-11 exists to prevent.
    """
    n = spec.interval_minutes
    if n < 60:
        return f"*/{n} * * * *"
    if n == 60:
        return f"{spec.fire_minute} * * * *"
    hours = ",".join(str(h) for h in fire_hours(spec))
    return f"{spec.fire_minute} {hours} * * *"


def render_crontab_block(spec: ScheduleSpec) -> str:
    """The marker-owned block ``merge_crontab`` installs.

    - tokens are ``shlex.quote``d; ``%`` is escaped (cron treats a bare
      ``%`` as a command/stdin separator)
    - stderr+stdout append into the schedule log -- NEVER ``2>/dev/null``
      (the old docs snippet discarded exactly the errors that reveal every
      known failure mode; AC-11 bans it from generated entries)
    """
    argv = build_command_argv(spec)
    validate_entry_argv(argv)
    command = " ".join(shlex.quote(t) for t in argv).replace("%", r"\%")
    log = shlex.quote(spec.log_file)
    return (
        f"{CRON_BLOCK_BEGIN}\n"
        f"{cron_expression(spec)} {command} >> {log} 2>&1\n"
        f"{CRON_BLOCK_END}\n"
    )


def merge_crontab(existing: str, block: str) -> str:
    """Return ``existing`` with csb's marker block replaced-or-appended.

    Ownership is the MARKER, never a substring match: the April 2026
    sketch's ``grep -v "csb backup"`` would have eaten a user's own
    hand-written line containing those words (D5). Foreign lines pass
    through byte-identical -- that guarantee is red-green tested.
    """
    lines = existing.splitlines(keepends=True)
    kept: list[str] = []
    inside = False
    for line in lines:
        stripped = line.rstrip("\n")
        if stripped == CRON_BLOCK_BEGIN:
            inside = True
            continue
        if stripped == CRON_BLOCK_END:
            inside = False
            continue
        if not inside:
            kept.append(line)
    result = "".join(kept)
    if result and not result.endswith("\n"):
        result += "\n"
    return result + block


def remove_from_crontab(existing: str) -> str:
    """``existing`` minus csb's marker block; foreign lines untouched."""
    without = merge_crontab(existing, "")
    return without

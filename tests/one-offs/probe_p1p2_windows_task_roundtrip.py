#!/usr/bin/env python3
"""P1+P2: non-elevated Windows Task Scheduler round-trip with the REAL entry.

User-approved single-task probe (#69). Registers `csb-scheduled-backup`
(the production task name), exercises it, and removes it. Everything
targets a SCRATCH claude-dir -- the live store is never referenced.

P1 answers:
  1. Does non-elevated `schtasks /create /xml` registration work at all?
  2. Which XML file ENCODING does schtasks accept (UTF-8 vs UTF-16)?
     -- the writer-encoding question flagged in round 3 (i)
  3. Does the REAL rendered entry (pythonw -m ... backup --quiet
     --claude-dir ... --log-file ...) actually execute via the scheduler
     and land its structured run-log line?
  4. Query-back: /query /XML round-trip + /query /v Last Run/Result
     (AC-14's Windows evidence layer)
  5. Re-register with /f (idempotent overwrite), /delete, verify gone,
     second /delete (remove-nothing behavior, AC-4 evidence)

P2 answers (via the extended sched_probe_target.py): what environment do
scheduled processes REALLY get -- sys.executable, cwd, and the environ
diff vs the interactive shell (the D4 divergence, measured).

Probe-only deviation from production XML, documented: a <WorkingDirectory>
element is injected so `-m claude_session_backup` resolves THE WORKTREE
package (which has --log-file); the installed package is v0.8.5 and does
not. Production entries need no WorkingDirectory (all paths absolute,
installed package on sys.path).

Run from the worktree root:
  python tests/one-offs/probe_p1p2_windows_task_roundtrip.py

Results 2026-08-06/07 (three runs, this box, non-elevated):

  P1.1  /create /xml non-elevated: WORKS (registered in \ root, Limited)
  P1.2  encoding: UTF-8 file ACCEPTED despite the UTF-16 declaration
        (tried first, succeeded -- UTF-16 therefore never exercised;
        backend writer decision recorded in the Rnd4 assessment)
  P1.3  real entry executed by the scheduler: outcome=ok rc=0 in 172ms
        (and 156ms/796ms on earlier runs); run-log line lands as designed
  P1.4  evidence readback: Last Result 0 / Last Run Time correct.
        Transient 267009 (SCHED_S_TASK_RUNNING) observed when querying
        during pythonw startup -- status parsing must treat it as
        "running", not failure
  P1.5  /f overwrite works; delete + verify-gone works; re-delete rc=1
        "cannot find the file" -- install layer maps this to exit 0 (AC-4)
  P2    scheduled env vs interactive: 53 vs 76 vars -- ALL CLAUDE* vars
        absent, GIT_ASKPASS/GIT_EDITOR/GIT_TERMINAL_PROMPT absent,
        PYTHONIOENCODING absent, PATH differs. D4 (bake all context)
        empirically CONFIRMED. cwd honored via WorkingDirectory.

  FIELD CATCH (run 2): scheduled git children each opened a conhost
  window under pythonw -- fixed with CREATE_NO_WINDOW in git_ops
  (_NO_WINDOW), regression-tested in test_backup_runlog.py. USER-
  CONFIRMED 2026-08-07: no flashes on the post-fix runs ("I didn't see
  any flashes after the initial ones"). Probe's own bugs fixed along
  the way: stale-content wait; cross-call XML splice.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.getcwd())
from claude_session_backup import schedule  # noqa: E402  (worktree import)

WORKTREE = Path(os.getcwd())
SCRATCH = Path(tempfile.gettempdir()) / "csb-p1-scratch-claude"
TASK = schedule.WINDOWS_TASK_NAME
PYTHONW = Path(sys.executable).with_name("pythonw.exe")


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kw)


def schtasks(*args: str) -> subprocess.CompletedProcess:
    return run(["schtasks", *args])


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def make_scratch() -> None:
    (SCRATCH / "projects").mkdir(parents=True, exist_ok=True)
    if not (SCRATCH / ".git").exists():
        run(["git", "init", "-q", str(SCRATCH)])
        run(["git", "-C", str(SCRATCH), "config", "user.email", "probe@local"])
        run(["git", "-C", str(SCRATCH), "config", "user.name", "p1 probe"])


def build_xml(arguments: str | None = None) -> str:
    """Real rendered XML; optionally swap the <Arguments> payload (P2).

    Lesson from run 2 of this probe: deriving the P2 variant by string-
    matching across SEPARATE build calls silently no-ops, because each
    call stamps a different start_boundary second. Swap within ONE
    rendered string only.
    """
    now = datetime.now()
    h, m = schedule.derive_fire_time(now, 1440)
    spec = schedule.ScheduleSpec(
        interval_minutes=1440, fire_hour=h, fire_minute=m,
        start_boundary=now.strftime("%Y-%m-%dT%H:%M:%S"),
        python_exe=str(PYTHONW),
        claude_dir=str(SCRATCH),
        log_file=str(SCRATCH / "csb-logs" / "schedule.log"),
    )
    xml = schedule.render_task_xml(spec)
    if arguments is not None:
        from xml.sax.saxutils import escape
        head, _, rest = xml.partition("<Arguments>")
        _, _, tail = rest.partition("</Arguments>")
        xml = head + "<Arguments>" + escape(arguments) + "</Arguments>" + tail
    # Probe-only: point -m at the worktree package (see module docstring).
    return xml.replace(
        "    </Exec>",
        f"      <WorkingDirectory>{WORKTREE}</WorkingDirectory>\n    </Exec>",
    )


def register(xml: str) -> str:
    """Try UTF-8 then UTF-16; return which encoding schtasks accepted."""
    for enc, label in (("utf-8", "UTF-8"), ("utf-16", "UTF-16 LE + BOM")):
        with tempfile.NamedTemporaryFile(
            "w", encoding=enc, suffix=".xml", delete=False
        ) as f:
            f.write(xml)
            path = f.name
        r = schtasks("/create", "/tn", TASK, "/xml", path, "/f")
        Path(path).unlink(missing_ok=True)
        print(f"  /create with {label}: rc={r.returncode} "
              f"{(r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr).strip() else ''}")
        if r.returncode == 0:
            return label
    raise SystemExit("registration failed with both encodings")


def wait_for(path: Path, seconds: int, needle: str = "") -> bool:
    """Wait for the file to exist AND contain a completed line.

    First run raced: the pythonw stream-rebind CREATES the log file at
    process start, but the structured line lands at run END -- existence
    is not completion. Poll for content instead.
    """
    for _ in range(seconds * 2):
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if (needle in text if needle else text.strip()):
                return True
        time.sleep(0.5)
    return False


def main() -> int:
    print(f"task={TASK}  scratch={SCRATCH}  pythonw={PYTHONW}")
    if not PYTHONW.exists():
        print("pythonw.exe not found beside interpreter"); return 1
    make_scratch()

    section("P1.1-1.2 register (non-elevated) + encoding check")
    accepted = register(build_xml())

    section("P1.4a query-back")
    q = schtasks("/query", "/tn", TASK, "/xml")
    print(f"  /query /xml rc={q.returncode}, "
          f"{len(q.stdout.splitlines())} lines returned")
    battery_ok = "<DisallowStartIfOnBatteries>false" in q.stdout
    print(f"  AC-5 override survived registration: {battery_ok}")

    section("P1.3 run the REAL entry via the scheduler")
    log = SCRATCH / "csb-logs" / "schedule.log"
    if log.exists():
        log.unlink()  # stale-content lesson: wait on FRESH evidence only
    r = schtasks("/run", "/tn", TASK)
    print(f"  /run rc={r.returncode}")
    if wait_for(log, 60, "outcome="):
        line = log.read_text(encoding="utf-8").strip().splitlines()[-1]
        print(f"  run-log line: {line}")
    else:
        print("  TIMEOUT: no run-log line within 60s")

    section("P1.4b execution evidence readback")
    v = schtasks("/query", "/tn", TASK, "/v", "/fo", "LIST")
    for want in ("Last Run Time", "Last Result", "Logon Mode"):
        for ln in v.stdout.splitlines():
            if ln.startswith(want):
                print(f"  {ln.strip()}")

    section("P2 env capture (re-register same name with /f = overwrite test)")
    dump = SCRATCH / "env-dump.json"
    dump.unlink(missing_ok=True)
    target = WORKTREE / "tests" / "one-offs" / "sched_probe_target.py"
    accepted2 = register(build_xml(arguments=f'"{target}" "{dump}"'))
    r = schtasks("/run", "/tn", TASK)
    print(f"  /run rc={r.returncode}")
    if wait_for(dump, 30):
        data = json.loads(dump.read_text(encoding="utf-8"))
        sched_env = data["environ"]
        here = dict(os.environ)
        missing = sorted(set(here) - set(sched_env))
        print(f"  executable={data['executable']}")
        print(f"  cwd={data['cwd']}")
        print(f"  env vars: interactive={len(here)} scheduled={len(sched_env)} "
              f"missing-in-scheduled={len(missing)}")
        interesting = [k for k in missing if k.upper().startswith(
            ("CLAUDE", "VIRTUAL", "CONDA", "PYTHON", "SSH", "GIT"))]
        print(f"  notable missing: {interesting or '(none of the usual suspects)'}")
        pi, ps = here.get("PATH", ""), sched_env.get("PATH", "")
        print(f"  PATH length: interactive={len(pi)} scheduled={len(ps)} "
              f"({'DIFFERENT' if pi != ps else 'identical'})")
    else:
        print("  TIMEOUT: no env dump within 30s")

    section("P1.5 delete, verify, re-delete")
    d1 = schtasks("/delete", "/tn", TASK, "/f")
    print(f"  /delete rc={d1.returncode}")
    q2 = schtasks("/query", "/tn", TASK)
    print(f"  /query after delete rc={q2.returncode} (nonzero = gone)")
    d2 = schtasks("/delete", "/tn", TASK, "/f")
    print(f"  second /delete rc={d2.returncode} "
          f"({(d2.stderr or d2.stdout).strip().splitlines()[-1] if (d2.stderr or d2.stdout).strip() else ''})")

    print(f"\nencodings accepted: run1={accepted} run2={accepted2}")
    print("scratch retained for inspection:", SCRATCH)
    return 0


if __name__ == "__main__":
    sys.exit(main())

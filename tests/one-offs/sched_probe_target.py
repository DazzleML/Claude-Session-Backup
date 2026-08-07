"""One-off: target script launched by Windows Task Scheduler.

Original purpose (durable-backup design era): prove a scheduled task runs as
a process OWNED BY THE SCHEDULER SERVICE -- decoupled from (and unkillable
by) the Claude Code session tree. Logs pid + ppid so the parent can be
confirmed as svchost/taskeng, not bash/node/csb.

Extended for #69 P2 (the env-capture probe): when given an output path
argument, ALSO dump ``sys.executable``, ``os.getcwd()`` and the full
``os.environ`` as JSON -- the measurement that turns the D4 environment-
divergence argument (schedulers don't source shell profiles; CLAUDE_DIR-
style vars are absent) into recorded fact. Without the argument it behaves
exactly as before. Kept per the keep-one-offs rule.
"""
import json
import os
import sys
import time
import pathlib

if len(sys.argv) > 1:
    # P2 mode: full environment capture to the given path (scratch dir --
    # never the real ~/.claude).
    out = pathlib.Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "environ": dict(os.environ),
    }
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
else:
    # Original mode: append pid/ppid proof line.
    p = pathlib.Path.home() / ".claude" / "csb-logs" / "sched-probe.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(
            time.strftime("%Y-%m-%d %H:%M:%S ")
            + f"sched-ran pid={os.getpid()} ppid={os.getppid()}\n"
        )

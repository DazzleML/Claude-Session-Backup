"""One-off: target script launched by Windows Task Scheduler to prove that a
scheduled task runs as a process OWNED BY THE SCHEDULER SERVICE -- decoupled
from (and unkillable by) the Claude Code session tree. Logs its own pid + ppid
so we can confirm the parent is svchost/taskeng (scheduler), not bash/node/csb.
Throwaway probe for the durable-backup design. Safe to delete."""
import os
import time
import pathlib

p = pathlib.Path.home() / ".claude" / "csb-logs" / "sched-probe.log"
p.parent.mkdir(parents=True, exist_ok=True)
with p.open("a", encoding="utf-8") as f:
    f.write(
        time.strftime("%Y-%m-%d %H:%M:%S ")
        + f"sched-ran pid={os.getpid()} ppid={os.getppid()}\n"
    )

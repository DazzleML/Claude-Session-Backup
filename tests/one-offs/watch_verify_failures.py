"""Watchdog: catch a pid-bearing registry entry failing verification.

A conditional breakpoint that can wait. The failure (a LIVE session
rendering `[no exit observed]` although its entry holds the correct
host pid) is intermittent and self-heals, so interactive stepping only
works if you happen to be present. This polls instead, and on the FIRST
failure freezes a complete forensic snapshot.

The discriminator that matters -- and the thing no surviving artifact
records -- is WHERE the pid went missing:

  * pid present in the raw enumeration but absent from `by_pid`
    -> the is_claude_cli() FILTER dropped it (cmdline-gate theory)
  * pid absent from the raw enumeration entirely
    -> the process-table QUERY missed it (partial/rate-limited scan)

Both produce an identical `[no exit observed]` row today. Also records
the creation-time guard's inputs, since that is verify_entry's only
other path to None.

Read-only: touches no registry entry, writes only its own report.

Usage:  python watch_verify_failures.py [interval_seconds] [--forever]
"""

import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_session_backup import live_registry as lr  # noqa: E402
from claude_session_backup import liveness as lv  # noqa: E402

INTERVAL = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1][0].isdigit() else 20.0
FOREVER = "--forever" in sys.argv
def _report_path():
    """Where the forensic dump lands -- deliberately NOT the repo.

    The dump carries session names, working directories and pids: real
    project layout, mildly disclosive, and useless to anyone else since
    it is per-machine runtime output regenerable by re-running. Prefer
    the project's local-only private/ tree when this script sits in a
    checkout; fall back to the system temp dir anywhere else (e.g. when
    copied to another machine to run standalone).
    """
    root = Path(__file__).resolve().parents[2]
    if (root / "private" / "claude").is_dir():
        out = root / "private" / "claude" / "diagnostics"
        try:
            out.mkdir(parents=True, exist_ok=True)
            return out / "verify_failure_report.txt"
        except OSError:
            pass
    return Path(tempfile.gettempdir()) / "csb_verify_failure_report.txt"


REPORT = _report_path()
CLAUDE = Path.home() / ".claude"


def _emit(lines):
    text = "\n".join(lines)
    print(text)
    with REPORT.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n" + "=" * 70 + "\n")


def check_once(n):
    raw = lv._enumerate_processes()
    scan = lv.scan()
    entries = lr.read_entries(CLAUDE)
    boot = lr.current_boot_utc()
    this_boot, _pre = lr.split_by_boot(entries, boot)

    raw_pids = {}
    for row in (raw or []):
        try:
            raw_pids[int(row[0])] = row
        except (TypeError, ValueError):
            continue

    failures = []
    dead = []
    for e in this_boot:
        pid = e.get("pid")
        if pid is None:
            continue                      # pid-less: argv path, not our case
        pid = int(pid)
        if lv.verify_entry(scan, e, None) is not None:
            continue                      # verified fine
        # A captured entry failed. Is its process actually alive?
        row = raw_pids.get(pid)
        if row is None:
            # Process genuinely gone -> `[no exit observed]` is CORRECT
            # (an unclean close). Not the bug; counted, never alerted --
            # otherwise a known ghost buries the real catch at every poll.
            dead.append(e.get("session_id", "?")[:8])
        else:
            name, cmdline = row[1] or "", row[2] or ""
            in_by_pid = pid in scan.by_pid
            verdict = (
                f"PROCESS PRESENT in raw table (name={name!r}) but "
                f"in_by_pid={in_by_pid} -- "
                + ("FILTER DROPPED IT (is_claude_cli said no)"
                   if not in_by_pid else
                   "in by_pid, so the CREATION-TIME GUARD rejected it")
            )
            failures.append((e, pid, verdict, name, cmdline,
                             scan.by_pid.get(pid)))

    if not failures:
        print(f"[{n}] ok -- {len(this_boot)} entries, "
              f"{len(raw_pids)} procs, by_pid={len(scan.by_pid)}, "
              f"scan.ok={scan.ok}, correctly-dead={len(dead)}{' ' + ','.join(dead) if dead else ''}")
        return False

    now = datetime.now(timezone.utc).isoformat()
    out = [f"!!! VERIFY FAILURE CAUGHT at {now} (poll #{n})",
           f"scan.ok={scan.ok} raw_rows={len(raw_pids)} "
           f"by_pid={len(scan.by_pid)} bare={len(scan.bare_pids)} "
           f"entries_this_boot={len(this_boot)}"]
    for e, pid, verdict, name, cmdline, info in failures:
        out += [
            f"  session={e.get('session_id')}",
            f"    entry: pid={pid} pid_at={e.get('pid_at')} "
            f"started_at={e.get('started_at')} source={e.get('source')}",
            f"    verdict: {verdict}",
            f"    proc name={name!r}",
            f"    proc cmdline={cmdline[:160]!r}",
            f"    by_pid info={info!r}",
        ]
    out.append("  --- full by_pid membership ---")
    out.append("  " + ", ".join(str(p) for p in sorted(scan.by_pid)))
    out.append("  --- every registry entry ---")
    for e in this_boot:
        out.append("  " + json.dumps(e, default=str))
    _emit(out)
    return True


def main():
    print(f"watching every {INTERVAL}s -- report -> {REPORT}")
    n = 0
    while True:
        n += 1
        try:
            caught = check_once(n)
        except Exception as exc:  # noqa: BLE001 -- a watchdog must not die
            print(f"[{n}] probe error: {exc!r}")
            caught = False
        if caught and not FOREVER:
            print("caught one -- stopping (pass --forever to keep going)")
            return 0
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())

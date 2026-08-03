#!/usr/bin/env python3
"""Probe: can we reconstruct "what was open at the last restart" from data
csb ALREADY has -- no hooks, no new storage?

Context (#60, session-sets epic): the MVP is a read-only query that answers
"which sessions were active when the machine went down." This probe is the
MVP's dry run: it joins the Windows boot-fence history (event log IDs
6005/6006/6008/1074) against the existing `sessions` table timestamps
(started_at / last_active_at) and prints the candidate roster for the most
recent restart, at several activity-window thresholds.

Validation is human: the user looks at the roster for the 2026-07-25 restart
and says whether those were, in fact, their windows.

Known approximations (by design -- this is the heuristic version):
  * A session open-but-idle for longer than the window is MISSED.
  * A session closed shortly before shutdown is WRONGLY INCLUDED.
  * Close events don't exist historically; only activity does.
The probe's job is to measure whether those approximations matter in
practice, and to pick the default window.

Timezone note: event-log times are LOCAL; index timestamps are ISO UTC
("Z"). We convert the fence to UTC via the local zone offset.

Read-only. Usage:  python tests/one-offs/probe_epoch_reconstruction.py
"""

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

DB = os.path.expanduser("~/.claude/session-backup.db")

# Activity windows to try: "session counts as open-at-shutdown if it had
# activity within this long before the fence."
WINDOWS_H = [2, 6, 24, 72]


def get_fences(max_events: int = 40):
    """Read boot/shutdown events from the System log via PowerShell.

    Returns a list of (utc_datetime, event_id) newest-first.
    6005=boot  6006=clean shutdown  6008=unexpected  1074=initiated-by-process
    """
    ps = (
        "Get-WinEvent -FilterHashtable @{LogName='System'; ID=6005,6006,6008,1074} "
        f"-MaxEvents {max_events} -ErrorAction SilentlyContinue | "
        "ForEach-Object { '{0:o}|{1}' -f $_.TimeCreated.ToUniversalTime(), $_.Id }"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, timeout=30,
    ).stdout
    fences = []
    for line in out.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        ts_s, eid = line.rsplit("|", 1)
        ts = datetime.fromisoformat(ts_s).replace(tzinfo=timezone.utc) \
            if datetime.fromisoformat(ts_s).tzinfo is None \
            else datetime.fromisoformat(ts_s).astimezone(timezone.utc)
        fences.append((ts, int(eid)))
    return fences


def parse_ts(s):
    """Index timestamps -> aware UTC datetime (tolerate Z / offset / naive)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main():
    fences = get_fences(max_events=60)
    # SHUTDOWN fences (6006 clean or 6008 unexpected), newest first.
    # Collapse fences <10min apart (double shutdown/boot cycles during updates).
    shutdowns = []
    for t, e in ((t, e) for t, e in fences if e in (6006, 6008)):
        if not shutdowns or (shutdowns[-1][0] - t).total_seconds() > 600:
            shutdowns.append((t, e))
    if not shutdowns:
        print("No shutdown fence found in the event log window.")
        return 1

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    all_rows = conn.execute(
        "SELECT session_id, session_name, project, start_folder,"
        "       started_at, last_active_at, deleted_at, is_fork "
        "FROM sessions"
    ).fetchall()

    for shut_utc, shut_kind in shutdowns[:4]:
        report_fence(fences, all_rows, shut_utc, shut_kind)
    return 0


def report_fence(fences, rows, shut_utc, shut_kind):
    cause = "clean (6006)" if shut_kind == 6006 else "UNEXPECTED (6008)"
    initiated = any(
        e == 1074 and abs((t - shut_utc).total_seconds()) < 300 for t, e in fences
    )
    boots_after = [t for t, e in fences if e == 6005 and t > shut_utc]
    boot_utc = min(boots_after) if boots_after else None

    print("=" * 78)
    print(f"Shutdown fence: {shut_utc:%Y-%m-%d %H:%M:%S} UTC  [{cause}"
          f"{', initiated by process (1074) -- update/restart' if initiated else ''}]")
    if boot_utc:
        print(f"Followed by boot: {boot_utc:%Y-%m-%d %H:%M:%S} UTC")
    print()

    sessions = []
    for r in rows:
        la = parse_ts(r["last_active_at"])
        st = parse_ts(r["started_at"])
        if la is None or st is None:
            continue
        sessions.append((r, st, la))

    for wh in WINDOWS_H:
        lo = shut_utc - timedelta(hours=wh)
        hits = [
            (r, st, la) for (r, st, la) in sessions
            if lo <= la <= shut_utc and st <= shut_utc
        ]
        hits.sort(key=lambda x: x[2])  # by last activity, oldest first
        print(f"--- window: activity within {wh}h before shutdown "
              f"-> {len(hits)} session(s) ---")
        for r, st, la in hits:
            name = r["session_name"] or r["session_id"][:8]
            gap = shut_utc - la
            gap_s = (f"{gap.days}d{gap.seconds // 3600}h" if gap.days
                     else f"{gap.seconds // 3600}h{(gap.seconds % 3600) // 60:02d}m")
            tags = []
            if r["deleted_at"]:
                tags.append("purged")
            if r["is_fork"]:
                tags.append("fork")
            tag_s = f"  [{','.join(tags)}]" if tags else ""
            print(f"  {la:%m-%d %H:%M} UTC (-{gap_s:<7}) {name:<62.62}"
                  f" {r['project'] or '':<28.28}{tag_s}")
        print()


if __name__ == "__main__":
    sys.exit(main())

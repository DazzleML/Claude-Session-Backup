"""One-off repair: stamp proven host pids into pid-less registry entries.

Context (#72, v0.9.2): pid capture ships with the plugin's hooks, so
sessions opened UNDER THE OLD PLUGIN have pid-less entries until their
next hook fire (compact/reopen). This machine's five open sessions all
have hosts proven by parent-chain walks and argv evidence recorded in
the pid-capture DWP -- this script writes exactly what each session's
own hook will re-confirm, via the canonical writer (refresh path:
pid + pid_at, started_at untouched).

Evidence (process table re-verified immediately before running):
  35936  --resume 37cf7542 (stale argv)     HOSTS 71eacb1a  (parent-chain walk x2)
  41636  --resume "AKJR-UPDATES__..."        HOSTS 5a23998a  (name resume, plain)
  45672  --resume cd69fce9                   HOSTS cd69fce9  (uuid resume, no fork)
  60424  --resume "...Oracle-search"         HOSTS 37f09835  (name resume, plain)
  68512  --resume 37f09835 --fork-session    HOSTS 3b7e178d  (the fork child)

37cf7542 (add-tree) is deliberately NOT stamped: nothing hosts it; once
35936 is captured by 71eacb1a's entry, arbitration demotes its stale
argv claim (capture beats argv).
"""

from pathlib import Path

from claude_session_backup import live_registry

CLAUDE_DIR = Path.home() / ".claude"

STAMPS = {
    "71eacb1a-60b1-45e6-bc52-be42a4f717a4": 35936,  # creating-session-sets
    "5a23998a-67a0-4610-a9dd-ba83604398bd": 41636,  # AKJR-UPDATES
    "cd69fce9-f7ae-4269-b61c-417752df3115": 45672,  # DAZZLECMD oracle-info
    "37f09835-4c0a-4bdb-9049-b379ea6b5771": 60424,  # Oracle-search
    "3b7e178d-8b43-4e68-abb5-4922e679c7f6": 68512,  # bug-69 (fork child)
}


def main():
    before = {e["session_id"]: e.get("pid")
              for e in live_registry.read_entries(CLAUDE_DIR)}
    for sid, pid in STAMPS.items():
        if sid not in before:
            print(f"SKIP {sid[:8]}: no registry entry (not open this boot?)")
            continue
        live_registry.record_session_start(CLAUDE_DIR, sid, pid=pid)
    after = {e["session_id"]: (e.get("pid"), e.get("pid_at"))
             for e in live_registry.read_entries(CLAUDE_DIR)}
    for sid in STAMPS:
        if sid in after:
            pid, pid_at = after[sid]
            print(f"{sid[:8]}: pid {before.get(sid)} -> {pid} (pid_at {pid_at})")


if __name__ == "__main__":
    main()

"""Probe: what do REAL Live Session Registry entries look like?

Ground-truth reconnaissance for the registry-hygiene DWP. Classifies
every entry in `csb-live/` by the evidence it carries (pid vs pid-less)
and by what a process scan says about its host RIGHT NOW, and reports
what the retained boundary snapshots hold.

Read-only. Run: python tests/one-offs/probe_registry_entry_shapes.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_session_backup import live_registry as lr  # noqa: E402
from claude_session_backup import liveness as lv  # noqa: E402

claude_dir = Path.home() / ".claude"
entries = lr.read_entries(claude_dir)
boot_utc = lr.current_boot_utc()
this_boot, pre_boot = lr.split_by_boot(entries, boot_utc)

print(f"boot_utc = {boot_utc}")
print(f"entries: {len(entries)} total -- {len(this_boot)} this boot, "
      f"{len(pre_boot)} pre-boot")

scan = lv.scan()
print(f"scan ok={scan.ok}  processes with cmdline={len(scan.by_pid)}  "
      f"bare={len(scan.bare_pids)}")
print()

buckets = {"pid+alive": 0, "pid+gone": 0, "pidless+argv": 0,
           "pidless+unverifiable": 0}
for e in this_boot:
    sid = e.get("session_id", "?")[:8]
    pid = e.get("pid")
    verdict = lv.verify_entry(scan, e, None)
    if pid is not None:
        alive = scan.by_pid.get(int(pid)) is not None
        bucket = "pid+alive" if alive else "pid+gone"
        extra = f"pid={pid} alive={alive}"
    else:
        bucket = "pidless+argv" if verdict else "pidless+unverifiable"
        extra = "no pid"
    buckets[bucket] += 1
    print(f"  {sid}  {bucket:22} {extra:20} "
          f"source={e.get('source', '-'):9} verify->{verdict}")

print()
print("population:", json.dumps(buckets))

bdir = lr.boundary_dir(claude_dir)
snaps = sorted(bdir.glob("boundary-*.json")) if bdir.is_dir() else []
print(f"\nretained boundary snapshots: {len(snaps)}")
for p in snaps:
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"  {p.name}: unreadable")
        continue
    members = raw.get("open_at_shutdown", [])
    withpid = sum(1 for m in members if m.get("pid") is not None)
    print(f"  {p.name}: {len(members)} members "
          f"({withpid} carry a pid, {len(members) - withpid} pid-less)")

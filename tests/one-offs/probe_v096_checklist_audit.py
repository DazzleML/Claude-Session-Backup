#!/usr/bin/env python
"""Read-only audit of the real ~/.claude store for the v0.10.0
activity-segments checklist (#80). Written during the tester-unbounded
sweep of tests/checklists/v0.10.0__Feature__activity-segments-epoch-membership.md

Calls the SAME production code the CLI uses (_get_config /
_materialize_set_roster -- the exact function `csb set show` and
`csb resume --set` both call), just in one process instead of spawning
`csb` once per epoch. No writes: init_schema() is a no-op once the DB
is already at the latest schema version (confirmed separately: v9).

Safe to re-run at any time -- it only performs SELECTs against the
real store, identical to what `csb set show <epoch> --json` does per
epoch, batched across the whole addressable history.
"""
import json
from types import SimpleNamespace

import claude_session_backup.commands as commands
from claude_session_backup.epochs import enumerate_epochs, read_fences

args = SimpleNamespace(claude_dir=None, db=None)
config = commands._get_config(args)

fences = read_fences()
history = enumerate_epochs(fences)
print(f"Total completed epochs in history: {len(history)}")

# token -> roster dict (only for tokens that resolve without fallthrough
# ambiguity; last~N for N in range is always unambiguous)
tokens = ["last"] + [f"last~{i}" for i in range(1, len(history) + 1)]

rosters = {}
fallthrough_count = 0
resolved_count = 0
for tok in tokens:
    roster, code = commands._materialize_set_roster(config, tok)
    if roster is None:
        continue
    rosters[tok] = roster
    resolved_count += 1

# session_id -> list of (token, messages, purged, segment_backed,
#                          snapshot_only, open_at_shutdown, session_name)
membership = {}
for tok, roster in rosters.items():
    for m in roster.get("members", []):
        membership.setdefault(m["session_id"], []).append((
            tok, m.get("messages"), m.get("purged"),
            m.get("segment_backed"), m.get("snapshot_only"),
            m.get("open_at_shutdown"), m.get("session_name"),
        ))

print(f"\nDistinct sessions appearing in >=1 epoch roster: {len(membership)}")

multi = {sid: rows for sid, rows in membership.items() if len(rows) >= 2}
print(f"Sessions appearing in >=2 epoch rosters: {len(multi)}")

# ---- Absence-proof candidates: appears in epoch index i and j (j > i+1)
# with a NON-empty roster in between that does not contain this session.
def epoch_depth(tok):
    return 0 if tok == "last" else int(tok.split("~")[1])

print("\n--- Absence-proof candidates (gap epoch has members, excludes "
      "the session) ---")
found_absence = 0
for sid, rows in multi.items():
    depths = sorted(epoch_depth(t) for t, *_ in rows)
    for a, b in zip(depths, depths[1:]):
        if b - a >= 2:
            # at least one epoch strictly between a and b
            gap_ok = True
            for d in range(a + 1, b):
                gap_tok = "last" if d == 0 else f"last~{d}"
                gap_roster = rosters.get(gap_tok)
                if gap_roster is None:
                    gap_ok = False
                    break
                gap_members = {m["session_id"]
                                for m in gap_roster.get("members", [])}
                if not gap_members or sid in gap_members:
                    gap_ok = False
                    break
            if gap_ok:
                name = rows[0][6]
                found_absence += 1
                print(f"  {name} ({sid[:8]}): present at depth {a} and "
                      f"{b}, absent (proven, non-empty gap roster) at "
                      f"depth(s) {list(range(a + 1, b))}")
                if found_absence >= 5:
                    break
    if found_absence >= 5:
        break
if found_absence == 0:
    print("  none found in this history shape")

# ---- Purged-row candidates: any historical epoch member with purged=True
print("\n--- Purged-row candidates (old-epoch archaeology) ---")
purged_hits = 0
for tok, roster in rosters.items():
    for m in roster.get("members", []):
        if m.get("purged"):
            purged_hits += 1
            print(f"  {tok}: {m.get('session_name')} ({m['session_id'][:8]}) "
                  f"purged=True")
            if purged_hits >= 8:
                break
    if purged_hits >= 8:
        break
print(f"  total purged rows seen across all epochs: {purged_hits}")

# ---- Snapshot-union candidates
print("\n--- Snapshot-only / open-at-shutdown candidates ---")
snap_hits = 0
for tok, roster in rosters.items():
    for m in roster.get("members", []):
        if m.get("snapshot_only") or m.get("open_at_shutdown"):
            snap_hits += 1
            print(f"  {tok}: {m.get('session_name')} "
                  f"({m['session_id'][:8]}) snapshot_only="
                  f"{m.get('snapshot_only')} open_at_shutdown="
                  f"{m.get('open_at_shutdown')}")
    if roster.get("snapshot_available"):
        print(f"  {tok}: snapshot_available=True")
print(f"  total snapshot/open rows seen: {snap_hits}")

# ---- Fallthrough frequency
print("\n--- Fallthrough frequency (requested_token != name) ---")
ft = 0
for tok, roster in rosters.items():
    req = roster.get("requested_token")
    name = roster.get("name")
    if req is not None and req != name:
        ft += 1
        skipped = roster.get("skipped_empty", [])
        print(f"  requested {req!r} -> settled {name!r} "
              f"(skipped: {skipped})")
print(f"  {ft} fallthroughs out of {resolved_count} resolved tokens "
      f"({tokens[1:] and tokens[0] and 'last + last~1..N'})")

# ---- Partition-property spot audit (sum of in-window == global count)
print("\n--- Partition property audit (sample of multi-epoch sessions) ---")
import sqlite3
conn = sqlite3.connect(config["index_path"])
checked = 0
mismatches = 0
for sid, rows in list(multi.items())[:15]:
    global_row = conn.execute(
        "SELECT message_count FROM sessions WHERE session_id=?", (sid,)
    ).fetchone()
    if global_row is None:
        continue
    global_count = global_row[0]
    in_window_sum = sum(r[1] for r in rows if r[1] is not None)
    checked += 1
    ok = in_window_sum == global_count
    if not ok:
        mismatches += 1
    print(f"  {rows[0][6]}: in-window sum={in_window_sum} global="
          f"{global_count} {'OK' if ok else 'MISMATCH'} "
          f"(epochs: {[r[0] for r in rows]})")
conn.close()
print(f"\n  checked={checked} mismatches={mismatches}")

print("\n--- boot epoch check ---")
boot_roster, code = commands._materialize_set_roster(config, "boot")
print(f"  boot roster members: {len(boot_roster.get('members', []))}, "
      f"kind={boot_roster.get('kind')}")

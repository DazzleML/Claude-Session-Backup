"""Bench: what did the #80 activity-segment fold cost the scanner?

Times ``extract_metadata`` (the full streaming parse) over the N largest
real transcripts in ~/.claude/projects. Run it twice -- once on current
code, once with metadata.py stashed to the pre-fold version -- to get an
honest before/after on identical inputs.

Also times the query side: ``_load_activity_segments`` + a
``build_roster`` overlap pass over the real index, which is the entire
per-command cost a `csb set show` pays for segments.

Usage:  python tests/one-offs/bench_segments_scan.py [N_FILES]

Results (2026-08-07, PLZWORK, 275-session store, warm cache):
  scan side, 8 largest transcripts = 850.2 MB total:
    pre-fold  11.93s (71.3 MB/s)
    post-fold 12.87s (66.1 MB/s)   -> +0.94s, ~+8% (run variance ~15%,
                                      a first post-fold run hit 10.10s)
  query side: 126 sessions with segments, 3317 segment rows:
    load+parse 12.2 ms; build_roster overlap 0.4 ms  -> ~13 ms per show
  context: `csb backup` re-parses EVERY live transcript each run (no
  changed-file gate on the parse) -- so the +8% lands on each backup
  (~+1-2s on this store), and one backup after upgrading segment-backed
  all 126 active sessions at once. This measurement is also the case
  for #82 (incremental byte-offset parsing).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_session_backup.metadata import extract_metadata  # noqa: E402


def bench_scan(n_files: int) -> None:
    projects = Path.home() / ".claude" / "projects"
    jsonls = sorted(projects.rglob("*.jsonl"),
                    key=lambda p: p.stat().st_size, reverse=True)[:n_files]
    total_bytes = 0
    total_s = 0.0
    print(f"-- scan side: extract_metadata over the {len(jsonls)} largest "
          "transcripts --")
    for path in jsonls:
        size = path.stat().st_size
        t0 = time.perf_counter()
        meta = extract_metadata(path)
        dt = time.perf_counter() - t0
        total_bytes += size
        total_s += dt
        segs = getattr(meta, "activity_segments", None)
        seg_note = f"{len(segs)} segments" if segs is not None else "pre-fold"
        print(f"  {size/1e6:7.1f} MB  {dt*1000:8.1f} ms  "
              f"{meta.event_count:7d} events  {seg_note}  {path.name[:16]}")
    if total_s:
        print(f"  TOTAL {total_bytes/1e6:.1f} MB in {total_s:.2f}s "
              f"({total_bytes/1e6/total_s:.1f} MB/s)")


def bench_query() -> None:
    try:
        from claude_session_backup.commands import _load_activity_segments
        from claude_session_backup.epochs import build_roster
        from claude_session_backup.index import init_schema, open_db
    except ImportError:
        print("-- query side: skipped (pre-fold code) --")
        return
    from datetime import datetime, timedelta, timezone

    db = Path.home() / ".claude" / "session-backup.db"
    if not db.exists():
        print(f"-- query side: no index at {db} --")
        return
    conn = open_db(str(db))
    init_schema(conn, quiet=True)
    t0 = time.perf_counter()
    segments = _load_activity_segments(conn)
    t_load = time.perf_counter() - t0
    rows = conn.execute("SELECT * FROM sessions").fetchall()
    hi = datetime.now(timezone.utc)
    lo = hi - timedelta(days=7)
    t0 = time.perf_counter()
    members, _missing = build_roster(rows, lo, hi, segments=segments)
    t_roster = time.perf_counter() - t0
    conn.close()
    n_segs = sum(len(v) for v in segments.values())
    print(f"-- query side (real index): {len(rows)} sessions, "
          f"{len(segments)} with segments, {n_segs} segment rows --")
    print(f"  load+parse segments: {t_load*1000:6.1f} ms")
    print(f"  build_roster (7d window, {len(members)} members): "
          f"{t_roster*1000:6.1f} ms")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    bench_scan(n)
    bench_query()

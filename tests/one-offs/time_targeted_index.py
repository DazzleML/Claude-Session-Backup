"""One-off: measure the cold cost of a TARGETED single-session index (no git
commit) -- the work a SessionEnd hook would do under Claude Code's 1.5s
SessionEnd-hook timeout. If this fits well under 1.5s, a synchronous targeted
index on SessionEnd completes (search fresh, lock released cleanly, no stale
lock) without needing OS-scheduler handoff.

Run cold via subprocess so python startup + imports are included:
    python tests/one-offs/time_targeted_index.py <jsonl_path>

Prints: import time, per-step times, and total. Does NOT git commit and does
NOT mutate the real index destructively (writes to a throwaway temp DB)."""
import sys
import time
import tempfile
from pathlib import Path

_t0 = time.perf_counter()
from claude_session_backup.index import open_db, init_schema, upsert_session
from claude_session_backup.metadata import extract_metadata
from claude_session_backup.sesslog_scanner import list_session_sources, list_sesslog_folders
from claude_session_backup.index import register_session_sources
_t_import = time.perf_counter() - _t0


def main():
    jsonl = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if not jsonl or not jsonl.exists():
        print(f"usage: time_targeted_index.py <existing jsonl>  (got {jsonl})")
        return
    now = "2026-05-28T00:00:00Z"
    steps = {}

    t = time.perf_counter()
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    conn = open_db(db)
    init_schema(conn, quiet=True)
    steps["open+init temp DB"] = time.perf_counter() - t

    t = time.perf_counter()
    meta = extract_metadata(jsonl)
    steps["extract_metadata (1 session)"] = time.perf_counter() - t

    t = time.perf_counter()
    upsert_session(conn, meta, str(jsonl), jsonl.stat().st_size, jsonl.stat().st_mtime, now)
    steps["upsert_session"] = time.perf_counter() - t

    t = time.perf_counter()
    try:
        folders = list_sesslog_folders(Path.home() / ".claude")
        sources = list_session_sources(jsonl, folders.get(meta.session_id) if isinstance(folders, dict) else None)
        register_session_sources(conn, meta.session_id, meta.project or "p", sources, now)
        steps["register_session_sources"] = time.perf_counter() - t
    except Exception as e:
        steps["register_session_sources"] = f"(skipped: {e!r})"

    conn.close()
    Path(db).unlink(missing_ok=True)

    print(f"  import claude_session_backup: {_t_import:.3f}s")
    for k, v in steps.items():
        print(f"  {k}: {v if isinstance(v, str) else f'{v:.3f}s'}")
    work = sum(v for v in steps.values() if isinstance(v, float))
    print(f"  --- work total: {work:.3f}s | import+work: {_t_import + work:.3f}s ---")
    print(f"  JSONL size: {jsonl.stat().st_size/1024:.0f} KB, messages: {meta.message_count}")


if __name__ == "__main__":
    main()

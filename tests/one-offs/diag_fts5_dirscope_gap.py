"""Diagnose why a session appears in folder_usage dir-scope but not fts5 dir-scope.

Context (2026-07-16, v0.5.1): a session that the default (folder_usage)
``csb search -d .`` surfaced was absent from ``--source fts5 -d .`` on the
same folder. Two hypotheses:
    A) semantic difference -- the session was ACTIVE in the folder (cwd) but
       worked no FILES there, so file_operations-strength ranking correctly
       skips it;
    B) FTS5 staleness -- the session/project isn't (freshly) indexed.

RESULT of the original run: hypothesis B -- the session was NOT indexed in
ANY per-project FTS5 DB; the vault's csb-fts DBs were last built ~2 months
prior (before the session existed). Resolved by backing up ``csb-fts/`` and
running ``csb update build-fts5``. The folder_usage default finding the
session anyway is the v0.5.1 coverage win working as designed.

READ-ONLY: opens the main index + per-project FTS5 DBs with sqlite3 and
only SELECTs. Usage:
    python tests/one-offs/diag_fts5_dirscope_gap.py <session_prefix> [scope_dir]
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_session_backup.config import load_config, resolve_paths  # noqa: E402
from claude_session_backup import fts_paths  # noqa: E402

if len(sys.argv) < 2:
    print(__doc__)
    raise SystemExit("usage: diag_fts5_dirscope_gap.py <session_prefix> [scope_dir]")
SID_PREFIX = sys.argv[1]
SCOPE = (sys.argv[2] if len(sys.argv) > 2 else r"C:\code").lower()


def main() -> int:
    cfg = resolve_paths(load_config())
    claude_dir = Path(cfg["claude_dir"])
    conn = sqlite3.connect(cfg["index_path"])
    conn.row_factory = sqlite3.Row

    row = conn.execute(
        "SELECT session_id, project, jsonl_path, jsonl_mtime FROM sessions "
        "WHERE session_id LIKE ?", (SID_PREFIX + "%",),
    ).fetchone()
    if row is None:
        print(f"no session matching {SID_PREFIX!r}")
        return 1
    sid, project = row["session_id"], row["project"]
    print(f"session : {sid}")
    print(f"project : {project!r}   jsonl_mtime: {row['jsonl_mtime']}")

    fu = conn.execute(
        "SELECT folder_path, usage_count FROM folder_usage "
        "WHERE session_id = ? ORDER BY usage_count DESC", (sid,),
    ).fetchall()
    in_scope = [r for r in fu if r["folder_path"].lower().startswith(SCOPE)]
    print(f"\nfolder_usage rows: {len(fu)} total, {len(in_scope)} under {SCOPE}:")
    for r in in_scope:
        print(f"  {r['folder_path']}  ({r['usage_count']}x)")

    fts_dir = fts_paths.fts5_db_dir(claude_dir)
    print(f"\nFTS5 dir: {fts_dir}")
    found_in = []
    for db_file in sorted(fts_dir.glob("*.db")):
        try:
            f = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            f.row_factory = sqlite3.Row
            idx = f.execute(
                "SELECT last_jsonl_mtime FROM indexed_sessions WHERE session_id = ?",
                (sid,),
            ).fetchone()
            if idx is None:
                f.close()
                continue
            found_in.append(db_file.name)
            fresh = idx["last_jsonl_mtime"] >= (row["jsonl_mtime"] or 0)
            print(f"\nindexed in {db_file.name}")
            print(f"  indexed mtime {idx['last_jsonl_mtime']} vs session {row['jsonl_mtime']}"
                  f"  -> {'FRESH' if fresh else 'STALE'}")
            ops = f.execute(
                "SELECT file_path, strength FROM file_operations WHERE session_id = ?",
                (sid,),
            ).fetchall()
            in_scope_ops = [o for o in ops if o["file_path"].lower().replace("/", "\\")
                            .startswith(SCOPE)]
            print(f"  file_operations: {len(ops)} total, {len(in_scope_ops)} under {SCOPE}")
            for o in in_scope_ops[:10]:
                print(f"    [{o['strength']}] {o['file_path']}")
            folders = {}
            for o in ops:
                top = "\\".join(o["file_path"].replace("/", "\\").split("\\")[:3])
                folders[top] = folders.get(top, 0) + 1
            print("  file-op top-level folders:")
            for k, v in sorted(folders.items(), key=lambda kv: -kv[1])[:8]:
                print(f"    {v:4d}x {k}")
            f.close()
        except sqlite3.Error as e:
            print(f"  ({db_file.name}: {e})")
    if not found_in:
        print(f"\nNOT indexed in ANY FTS5 DB -> hypothesis B (missing index)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

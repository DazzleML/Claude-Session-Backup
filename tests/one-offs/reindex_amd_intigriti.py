"""Re-extract metadata for the AMD_INTIGRITI session only.

Used to verify the #21 indexer change (store all folder rows, not just top-N)
without paying the cost of a full `csb rebuild-index`. The pre-#21 row had
only 4 folder_usage entries; after this script runs the row should have all
distinct cwds from the JSONL.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from claude_session_backup.config import load_config, resolve_paths
from claude_session_backup.index import init_schema, open_db, upsert_session
from claude_session_backup.metadata import extract_metadata

UUID = "f2d0d074-f06c-435c-94c3-46606a91d32c"

config = resolve_paths(load_config())
conn = open_db(config["index_path"])
init_schema(conn)

jsonl = Path(config["claude_dir"]) / "projects" / "C--" / f"{UUID}.jsonl"
print(f"Re-extracting from {jsonl} ({jsonl.stat().st_size:,} bytes)...")

meta = extract_metadata(jsonl)
meta.project = "C--"

rel_path = str(jsonl.relative_to(config["claude_dir"]))
size = jsonl.stat().st_size
mtime = jsonl.stat().st_mtime
now = datetime.now(timezone.utc).isoformat()

upsert_session(conn, meta, rel_path, size, mtime, now)
conn.commit()

print(f"\nfolder_usage rows for {UUID}:")
print(f"{'folder_path':<70} {'count':>6}")
print("-" * 78)
for row in conn.execute(
    "SELECT folder_path, usage_count FROM folder_usage "
    "WHERE session_id = ? ORDER BY usage_count DESC",
    (UUID,),
):
    print(f"{row['folder_path']:<70} {row['usage_count']:>6}")

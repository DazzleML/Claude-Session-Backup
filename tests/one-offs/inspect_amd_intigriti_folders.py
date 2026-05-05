"""Inspect the folder_usage rows csb stored for the AMD_INTIGRITI session."""

import sqlite3
from pathlib import Path

db = Path.home() / ".claude" / "session-backup.db"
conn = sqlite3.connect(str(db))
rows = conn.execute(
    "SELECT folder_path, usage_count, is_start_folder FROM folder_usage "
    "WHERE session_id='f2d0d074-f06c-435c-94c3-46606a91d32c' "
    "ORDER BY usage_count DESC LIMIT 20"
).fetchall()
print("folder_path                                                       usage  is_start")
print("-" * 90)
for r in rows:
    print(f"{repr(r[0]):<65} {r[1]:>5}  {r[2]}")

# Also check the start_folder field on the session
sess = conn.execute(
    "SELECT start_folder, jsonl_path FROM sessions "
    "WHERE session_id='f2d0d074-f06c-435c-94c3-46606a91d32c'"
).fetchone()
print()
print(f"sessions.start_folder = {sess[0]!r}")
print(f"sessions.jsonl_path   = {sess[1]!r}")

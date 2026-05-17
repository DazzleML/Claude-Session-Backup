"""Phase-1 search prototype -- validates the design end-to-end.

Reads session_sources from the real DB, walks every source path for USER/AI
messages, prints hits with surrounding context. Literal substring match,
case-insensitive. The point is to prove the foundation works before we
formalize Step 6 (claude_session_backup/search.py).

Usage:
    python tests/one-offs/proto_search.py <query>
    python tests/one-offs/proto_search.py <query> --limit 5
    python tests/one-offs/proto_search.py <query> --source convo
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

# Force UTF-8 on stdout so cp1252 doesn't choke on transcript snippets
# that contain em-dashes, smart quotes, etc.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = Path.home() / ".claude" / "session-backup.db"

# Opening of a USER/AI block: [[ts]] {ROLE:  (and possibly content on same line)
OPEN_RE = re.compile(
    r"^\[\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\] \{(USER|AI):\s?(.*)$"
)


def parse_log_lines(path):
    """Yield (start_line, role, ts, text) for USER/AI blocks.

    Both .convo and .sesslog channels use block format:
      [[ts]] {ROLE:
      ...multi-line content...
      }
    Single-line variant is also supported when the closing } sits on the
    same line as the opener. Non-USER/non-AI tag blocks (Bash, Read, etc.)
    are skipped entirely.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip("\n")
        m = OPEN_RE.match(line)
        if not m:
            # Bash:, Read:, Skill:, etc. -- also use [[ts]] {Tag: ...}
            # but we only care about USER/AI here. Skip.
            i += 1
            continue

        ts, role, first = m.group(1), m.group(2), m.group(3)
        start_line = i + 1

        # Single-line case: "{USER: text}" on one line
        if first.endswith("}"):
            yield start_line, role, ts, first[:-1].rstrip()
            i += 1
            continue

        # Multi-line: collect until standalone "}" closer
        chunks = []
        if first:
            chunks.append(first)
        i += 1
        while i < n:
            ln = lines[i].rstrip("\n")
            if ln.rstrip() == "}":
                i += 1
                break
            chunks.append(ln)
            i += 1
        yield start_line, role, ts, "\n".join(chunks)


def parse_jsonl_events(path):
    """Yield (event_num, role, ts, text) for type:user/assistant events."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = e.get("type")
                if t not in ("user", "assistant"):
                    continue
                ts = e.get("timestamp", "")
                role = "USER" if t == "user" else "AI"
                msg = e.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
                    text = " ".join(parts)
                else:
                    text = ""
                if text:
                    yield i, role, ts, text
    except OSError:
        return


def search(query, source_filter=None, limit=20):
    q = query.lower()
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    sql = """
        SELECT s.session_id, s.session_name, s.project, s.last_active_at,
               ss.source_type, ss.source_path
        FROM session_sources ss
        JOIN sessions s ON ss.session_id = s.session_id
        WHERE s.deleted_at IS NULL
    """
    params = []
    if source_filter:
        sql += " AND ss.source_type = ?"
        params.append(source_filter)
    sql += " ORDER BY s.last_active_at DESC"

    hits = 0
    for row in conn.execute(sql, params):
        if hits >= limit:
            break
        path = row["source_path"]
        if row["source_type"] in ("convo", "sesslog"):
            events = parse_log_lines(path)
        else:
            events = parse_jsonl_events(path)

        for ev_num, role, ts, text in events:
            if q in text.lower():
                hits += 1
                snippet = text[:200].replace("\\n", " ")
                if len(text) > 200:
                    snippet += " ..."
                name = (row["session_name"] or "<unnamed>")[:40]
                src = row["source_type"]
                print(f"[{hits}] {name:40s} {row['session_id'][:8]}  ({src})")
                print(f"    L{ev_num} [{role}] {ts}")
                print(f"    {snippet}")
                print()
                if hits >= limit:
                    break

    conn.close()
    if hits == 0:
        print(f"No hits for {query!r}")
    else:
        print(f"--- {hits} hit(s) shown (--limit to adjust) ---")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--source", choices=["convo", "sesslog", "jsonl"], default=None)
    args = ap.parse_args()
    search(args.query, args.source, args.limit)


if __name__ == "__main__":
    main()

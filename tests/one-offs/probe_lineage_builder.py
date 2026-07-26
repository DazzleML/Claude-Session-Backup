"""Probe the lineage builder against the real fork topology (synthetic DB).

Builds an in-memory index seeded with the ACTUAL parent->child edges
observed on this machine (2026-07-26 census: 32 forks; the C-- project's
21-fork multi-branch web plus dazzlecmd's 5-deep linear chain), then
exercises build_forest() across the scope/filter/tier combinations.

Run: python tests/one-offs/probe_lineage_builder.py
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_session_backup.index import init_schema  # noqa: E402
from claude_session_backup.lineage import build_forest  # noqa: E402

# (child, parent) -- real edges from the census.
EDGES = [
    ("0909224f", "fa61ce25"), ("1a9f53db", "1feba631"), ("23f58cd1", "e0679878"),
    ("2462d165", "2d177aa2"), ("2d177aa2", "0909224f"), ("305402a7", "722c24c2"),
    ("4df30efe", "a1ebd18d"), ("65e9a8af", "6dd9f33b"), ("722c24c2", "f2d0d074"),
    ("89583e08", "0909224f"), ("9c0e8818", "23f58cd1"), ("a1ebd18d", "722c24c2"),
    ("ac1b5df5", "2d177aa2"), ("bb9481ea", "1feba631"), ("c6a5e384", "c6d20739"),
    ("c6d20739", "bb9481ea"), ("d0bb911d", "0909224f"), ("d91c1599", "9c0e8818"),
    ("dac78227", "f2d0d074"), ("f2d0d074", "23f58cd1"), ("fa61ce25", "d91c1599"),
    ("6a6575fe", "aa7bafc2"),
    ("08c0764e", "6230f734"), ("6230f734", "8ace3e9d"),
    ("748d8f62", "feb87e16"), ("feb87e16", "08c0764e"),
    ("4c4429f9", "562ac10b"),
]
ROOTS_ONLY = ["e0679878", "6dd9f33b", "1feba631", "aa7bafc2", "8ace3e9d", "562ac10b",
              "lonely01", "lonely02",
              # never forked, no parent -> a true --orphans candidate
              "isolated1",
              # deleted LEAF with no in-scope descendants: must stay hidden
              # by default (it is not a structural connector)
              "deadleaf1"]


def seed(conn, deleted=(), phantom_parent=None):
    ids = set(ROOTS_ONLY)
    for c, p in EDGES:
        ids.add(c); ids.add(p)
    for i, sid in enumerate(sorted(ids)):
        conn.execute(
            "INSERT INTO sessions (session_id, project, session_name, start_folder, "
            "started_at, last_active_at, message_count, jsonl_path, deleted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, "C--", f"SESSION__{sid}", r"C:\code\proj",
             f"2026-05-{(i % 28) + 1:02d}T10:00:00Z",
             f"2026-06-{(i % 28) + 1:02d}T10:00:00Z",
             100 + i, f"projects/C--/{sid}.jsonl",
             "2026-07-01T00:00:00Z" if sid in deleted else None),
        )
        conn.execute(
            "INSERT INTO folder_usage (session_id, folder_path, usage_count, is_start_folder) "
            "VALUES (?,?,?,?)", (sid, r"C:\code\proj", 10, 1))
    for n, (c, p) in enumerate(EDGES):
        conn.execute(
            "UPDATE sessions SET parent_session_id=?, is_fork=1, forked_at=? "
            "WHERE session_id=?", (p, f"2026-06-{(n % 28) + 1:02d}T12:00:00Z", c))
    if phantom_parent:
        conn.execute("UPDATE sessions SET parent_session_id=?, is_fork=1, forked_at=? "
                     "WHERE session_id=?",
                     (phantom_parent, "2026-06-01T12:00:00Z", "lonely01"))
        conn.execute("UPDATE sessions SET parent_session_id=?, is_fork=1, forked_at=? "
                     "WHERE session_id=?",
                     (phantom_parent, "2026-06-02T12:00:00Z", "lonely02"))
    conn.commit()


def show(label, forest):
    print(f"\n=== {label} ===")
    print(f"roots={len(forest.roots)} nodes={forest.total_nodes} "
          f"elided={forest.elided_nodes} cycles={forest.cycles_broken} "
          f"phantoms={forest.phantom_parents}")
    for r in forest.roots:
        for n in r.walk():
            tier = ("MATCH" if n.matched else
                    "norm" if n.in_population else
                    "PHANTOM" if n.phantom else "dim")
            print(f"  {'  ' * n.depth}{n.session_id} [{tier}]"
                  f"{f' (+{n.elided_children} elided)' if n.elided_children else ''}")


def main():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn, quiet=True)
    seed(conn, deleted={"23f58cd1", "9c0e8818", "deadleaf1"}, phantom_parent="ffffffff")

    f = build_forest(conn)
    show("full forest, active-only (default)", f)

    f = build_forest(conn, filter_term="SESSION__dac78227")
    show("filter=dac78227 -> COMPONENT view (whole family, default)", f)

    f = build_forest(conn, filter_term="SESSION__dac78227", component_view=False)
    show("filter=dac78227 -> LINEAGE-SPINE view (ancestors + descendants only)", f)

    f = build_forest(conn, filter_term="dac*")
    show("filter=dac* (trailing-star prefix)", f)

    f = build_forest(conn, filter_term=r"^SESSION__(dac|748)", regex=True)
    show("filter=regex ^SESSION__(dac|748)", f)

    f = build_forest(conn, deleted_filter="all")
    show("--deleted all", f)

    f = build_forest(conn, orphans_only=True)
    show("--orphans", f)

    f = build_forest(conn, root="8ace3e9d")
    show("--root 8ace3e9d (5-deep dazzlecmd chain)", f)

    f = build_forest(conn, max_nodes_per_root=6)
    show("max_nodes_per_root=6 (truncation)", f)

    # cycle guard
    conn.execute("UPDATE sessions SET parent_session_id='dac78227' WHERE session_id='f2d0d074'")
    conn.commit()
    f = build_forest(conn)
    show("cycle injected (f2d0d074 <-> dac78227)", f)
    conn.close()


if __name__ == "__main__":
    main()

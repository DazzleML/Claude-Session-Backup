#!/usr/bin/env python3
"""Reorganize private/claude root docs into type subfolders.

Why this is safe for the knowledge graph: `generate-backlinks.py` resolves
wikilinks by **basename**, so `[[2026-08-06__14-37-16__dev-workflow-process__x]]`
keeps resolving after the file moves into `dwp/`. Verified before and after by
`generate-backlinks.py --broken`.

Uses os.rename (same volume) so NTFS ctime/mtime/atime are preserved -- never
shutil.copy2 + unlink, which resets ctime.

Classification is filename-driven and deliberately conservative: anything that
does not match a known type stays at the root rather than being guessed at.

Usage:
    python tests/one-offs/reorg_vault_by_type.py            # dry run
    python tests/one-offs/reorg_vault_by_type.py --apply
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

VAULT = Path(r"C:\code\claude-projects\Claude-Session-Backup\private\claude")

# Order matters: first match wins.
RULES = [
    ("postmortems", ("full-postmortem", "lean-postmortem", "context-postmortem",
                     "mini-postmortem", "postmortem")),
    ("checkpoints", ("whereweare",)),
    ("plans", ("claude-plan", "__plan-", "implementation-plan")),
    ("dwp", ("dev-workflow-process",)),
    ("reports", ("reality-check", "-report", "__handoff__")),
    ("discussions", ("discuss_rnd", "final_assessment")),
    # Catch-all: everything else at the root is a design/analysis doc.
    ("designs", ("",)),
]

# Directories that already have a home and must not be walked.
SKIP_DIRS = {"_maps", "_oracle", "notes", "issues", "commits", "releases",
             "settings", "thinking", "postmortems", "checkpoints", "plans",
             "dwp", "reports", "discussions", "designs"}


def classify(name: str):
    low = name.lower()
    for folder, markers in RULES:
        if any(m in low for m in markers):
            return folder
    return None


def main() -> int:
    apply = "--apply" in sys.argv
    moves, unclassified = [], []

    for p in sorted(VAULT.glob("*.md")):
        dest_dir = classify(p.name)
        if dest_dir is None:
            unclassified.append(p.name)
            continue
        moves.append((p, VAULT / dest_dir / p.name))

    # Non-.md strays at root (e.g. .bak-pre-unwrap) stay put deliberately.
    by_dir = {}
    for src, dst in moves:
        by_dir.setdefault(dst.parent.name, []).append(src.name)

    for folder in sorted(by_dir):
        print(f"\n{folder}/  ({len(by_dir[folder])} files)")
        for n in sorted(by_dir[folder])[:3]:
            print(f"    {n}")
        if len(by_dir[folder]) > 3:
            print(f"    ... and {len(by_dir[folder]) - 3} more")

    print(f"\nSTAYING AT ROOT ({len(unclassified)} design/analysis docs):")
    for n in sorted(unclassified)[:6]:
        print(f"    {n}")
    if len(unclassified) > 6:
        print(f"    ... and {len(unclassified) - 6} more")

    print(f"\nTotal to move: {len(moves)}")

    if not apply:
        print("\n(dry run -- pass --apply to move)")
        return 0

    for folder in by_dir:
        (VAULT / folder).mkdir(exist_ok=True)

    moved = 0
    for src, dst in moves:
        if dst.exists():
            print(f"SKIP (exists): {dst.name}")
            continue
        os.rename(src, dst)   # preserves timestamps; same volume
        moved += 1
    print(f"\nMoved {moved} files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
rebuild_reality_check.py -- Phase 0 of the v0.3.11 safe-rebuild plan.

Goal: convert hypotheses into documented facts before any production code changes.
Mirrors the restore_reality_check.py pattern -- fully sandboxed in a temp dir,
never touches the real ~/.claude.

Plan reference: private/claude/2026-06-02__15-46-56__claude-plan__safe-update-umbrella-and-backfill-v0.3.11.md
DWP doc:        private/claude/2026-06-02__15-20-57__safe-rebuild-index-and-deleted-session-cache.md
Invariant note: private/claude/notes/architecture/2026-06-02__14-00-03__both_rebuild-index-must-not-lose-deleted-sessions.md

Usage:
    python tests/one-offs/rebuild_reality_check.py
    python tests/one-offs/rebuild_reality_check.py --no-report   # skip writing the markdown report

Exit codes:
    0 -- all scenarios executed (whether or not they confirm bugs)
    1 -- script error (couldn't run scenarios)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Make the csb package importable when running this script directly from the
# tests/one-offs/ folder.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── Fixture data ────────────────────────────────────────────────────────

PROJECT_SLUG = "C--scratch-csb-rebuild-reality-check"

# Three sessions: two we'll keep "live", one we'll cull mid-test.
SESSION_LIVE_1 = "11111111-aaaa-2222-bbbb-333333333333"
SESSION_LIVE_2 = "44444444-cccc-5555-dddd-666666666666"
SESSION_CULLED = "77777777-eeee-8888-ffff-999999999999"


def _session_events(uuid: str, name: str, folder: str) -> list[dict]:
    return [
        {"type": "custom-title", "customTitle": name, "sessionId": uuid},
        {"type": "user", "timestamp": "2026-06-01T10:00:00Z", "cwd": folder,
         "version": "test", "sessionId": uuid, "uuid": f"{uuid}-u1",
         "message": {"role": "user", "content": f"hello from {name}"}},
        {"type": "assistant", "timestamp": "2026-06-01T10:00:05Z", "cwd": folder,
         "sessionId": uuid, "uuid": f"{uuid}-u2"},
        # A second folder visit so folder_usage has more than one row
        {"type": "user", "timestamp": "2026-06-01T10:30:00Z",
         "cwd": folder + "/subdir",
         "sessionId": uuid, "uuid": f"{uuid}-u3"},
    ]


SESSIONS = {
    SESSION_LIVE_1: ("live-session-1", "/scratch/csb-rebuild-reality-check/projA"),
    SESSION_LIVE_2: ("live-session-2", "/scratch/csb-rebuild-reality-check/projA"),
    SESSION_CULLED: ("about-to-be-culled", "/scratch/csb-rebuild-reality-check/projB"),
}


# ── Report data structures ──────────────────────────────────────────────

@dataclass
class ScenarioRow:
    number: int
    name: str
    layer_tested: str
    hypothesis: str
    observed: str = ""
    verdict: str = ""  # CONFIRMED / DISPROVED / DOCUMENTED / ERROR
    notes: list[str] = field(default_factory=list)

    def md_row(self) -> str:
        def esc(s: str) -> str:
            return s.replace("|", "\\|").replace("\n", " ")
        return (f"| {self.number} | {esc(self.name)} | {esc(self.layer_tested)} | "
                f"{esc(self.hypothesis)} | {esc(self.observed)} | {self.verdict} |")


# ── Utilities ───────────────────────────────────────────────────────────

def run(cmd: list[str], cwd: Optional[Path] = None, env: Optional[dict] = None,
        check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, env=env,
        capture_output=True, text=True, check=check,
    )


def git(claude_dir: Path, *args: str, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(claude_dir), *args], env=env)


def git_env(claude_dir: Path) -> dict:
    base = dict(os.environ)
    base.update({
        "GIT_AUTHOR_NAME": "csb-rebuild-check",
        "GIT_AUTHOR_EMAIL": "csb@rebuild-check.local",
        "GIT_COMMITTER_NAME": "csb-rebuild-check",
        "GIT_COMMITTER_EMAIL": "csb@rebuild-check.local",
    })
    return base


def setup_sandbox(tmp: Path) -> Path:
    """Build a sandboxed ~/.claude with three sessions; git-init + commit."""
    claude = tmp / ".claude"
    projects = claude / "projects" / PROJECT_SLUG
    states = claude / "session-states"
    projects.mkdir(parents=True)
    states.mkdir(parents=True)

    for uuid, (name, folder) in SESSIONS.items():
        events = _session_events(uuid, name, folder)
        text = "\n".join(json.dumps(e, separators=(",", ":")) for e in events) + "\n"
        (projects / f"{uuid}.jsonl").write_bytes(text.encode("utf-8"))
        (states / f"{uuid}.json").write_text(json.dumps({
            "session_id": uuid,
            "transcript_path": str(projects / f"{uuid}.jsonl"),
            "original_cwd": folder,
            "cwd": folder,
            "current_name": name,
        }), encoding="utf-8")

    env = git_env(claude)
    run(["git", "init", str(claude)], env=env, check=True)
    git(claude, "config", "commit.gpgsign", "false", env=env)
    git(claude, "config", "user.name", "csb-rebuild-check", env=env)
    git(claude, "config", "user.email", "csb@rebuild-check.local", env=env)
    git(claude, "add", "-A", env=env)
    git(claude, "commit", "--no-gpg-sign", "-m", "initial commit -- 3 sessions", env=env)
    return claude


def run_csb_backup(claude_dir: Path, db_path: Path, no_commit: bool = False) -> int:
    """Run cmd_backup against the sandbox."""
    from claude_session_backup.commands import cmd_backup
    args = argparse.Namespace(
        claude_dir=str(claude_dir),
        db=str(db_path),
        quiet=False,
        no_commit=no_commit,
    )
    return cmd_backup(args)


def run_csb_rebuild(claude_dir: Path, db_path: Path) -> int:
    """Run the CURRENT (buggy) cmd_rebuild_index against the sandbox."""
    from claude_session_backup.commands import cmd_rebuild_index
    args = argparse.Namespace(
        claude_dir=str(claude_dir),
        db=str(db_path),
        quiet=False,
        no_commit=False,
    )
    return cmd_rebuild_index(args)


def cull_jsonl(claude_dir: Path, uuid: str) -> Path:
    """Simulate claude.exe culling a session: delete the JSONL."""
    p = claude_dir / "projects" / PROJECT_SLUG / f"{uuid}.jsonl"
    p.unlink()
    # Commit the deletion so it's in git history (mirrors what csb backup does)
    env = git_env(claude_dir)
    git(claude_dir, "add", "-A", env=env)
    git(claude_dir, "commit", "--no-gpg-sign", "-m", f"cull {uuid[:8]}", env=env)
    return p


# ── DB introspection helpers ────────────────────────────────────────────

def count_sessions(db_path: Path, deleted_only: bool = False) -> int:
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    if deleted_only:
        n = conn.execute("SELECT COUNT(*) FROM sessions WHERE deleted_at IS NOT NULL").fetchone()[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    return n


def session_row(db_path: Path, uuid: str) -> Optional[dict]:
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (uuid,)).fetchone()
    conn.close()
    return dict(r) if r else None


def folder_count_for(db_path: Path, uuid: str) -> int:
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM folder_usage WHERE session_id = ?",
                     (uuid,)).fetchone()[0]
    conn.close()
    return n


# ── Scenarios ───────────────────────────────────────────────────────────

def scenario_1_initial_backup(claude_dir: Path, db_path: Path, row: ScenarioRow) -> None:
    """csb backup on fresh repo -> 3 active sessions indexed."""
    rc = run_csb_backup(claude_dir, db_path, no_commit=True)
    n_total = count_sessions(db_path, deleted_only=False)
    n_deleted = count_sessions(db_path, deleted_only=True)
    row.observed = (f"rc={rc}; sessions total={n_total}; deleted={n_deleted}; "
                    f"folder_usage(culled)={folder_count_for(db_path, SESSION_CULLED)}")
    row.verdict = ("DOCUMENTED" if (rc == 0 and n_total == 3 and n_deleted == 0)
                   else "ERROR")
    if row.verdict == "ERROR":
        row.notes.append("Expected 3 active rows, 0 deleted after first backup")


def scenario_2_cull_and_backup(claude_dir: Path, db_path: Path, row: ScenarioRow) -> None:
    """Cull one JSONL -> csb backup -> 1 marked deleted with folder_usage intact."""
    cull_jsonl(claude_dir, SESSION_CULLED)
    rc = run_csb_backup(claude_dir, db_path, no_commit=True)
    n_total = count_sessions(db_path, deleted_only=False)
    n_deleted = count_sessions(db_path, deleted_only=True)
    folders_for_culled = folder_count_for(db_path, SESSION_CULLED)
    culled_row = session_row(db_path, SESSION_CULLED)
    row.observed = (
        f"rc={rc}; total={n_total}; deleted={n_deleted}; "
        f"folder_usage(culled)={folders_for_culled}; "
        f"culled.deleted_at={'set' if culled_row and culled_row.get('deleted_at') else 'unset'}; "
        f"culled.name={culled_row.get('session_name') if culled_row else 'N/A'}"
    )
    if (rc == 0 and n_total == 3 and n_deleted == 1
            and folders_for_culled >= 1
            and culled_row and culled_row.get("deleted_at")):
        row.verdict = "DOCUMENTED"  # baseline works
    else:
        row.verdict = "ERROR"
        row.notes.append("Expected culled session marked deleted with folder_usage intact")


def scenario_3_rebuild_loss(claude_dir: Path, db_path: Path, row: ScenarioRow,
                             pre_rebuild_state: dict) -> None:
    """csb rebuild-index (CURRENT IMPL) -> deleted-session row gone, folders gone (THE BUG)."""
    pre_total = pre_rebuild_state["total"]
    pre_deleted = pre_rebuild_state["deleted"]
    pre_folders_culled = pre_rebuild_state["folders_culled"]

    rc = run_csb_rebuild(claude_dir, db_path)
    post_total = count_sessions(db_path, deleted_only=False)
    post_deleted = count_sessions(db_path, deleted_only=True)
    post_folders_culled = folder_count_for(db_path, SESSION_CULLED)
    post_culled_row = session_row(db_path, SESSION_CULLED)

    row.observed = (
        f"rc={rc}; "
        f"PRE  total={pre_total} deleted={pre_deleted} folders(culled)={pre_folders_culled}; "
        f"POST total={post_total} deleted={post_deleted} folders(culled)={post_folders_culled}; "
        f"culled row exists post-rebuild={post_culled_row is not None}"
    )

    # The bug: post-rebuild, the culled session's row + folders should be gone
    if (post_culled_row is None and post_folders_culled == 0
            and post_deleted < pre_deleted):
        row.verdict = "CONFIRMED-BUG"
        row.notes.append(
            "Data-loss confirmed: deleted-session row + folder_usage erased by "
            "csb rebuild-index. The fix in Phase 2 must preserve these.")
    elif (post_culled_row is not None and post_folders_culled >= 1
            and post_deleted == pre_deleted):
        row.verdict = "DISPROVED"
        row.notes.append("Surprising: rebuild preserved the deleted-session row. "
                          "Re-check the cmd_rebuild_index implementation.")
    else:
        row.verdict = "ERROR"
        row.notes.append("Partial/unexpected outcome -- inspect manually.")


def scenario_4_extract_metadata_from_bytes(claude_dir: Path, db_path: Path,
                                           row: ScenarioRow) -> None:
    """Can we feed bytes from `git show` into the existing extract_metadata path?"""
    from claude_session_backup.metadata import extract_metadata

    file_path = f"projects/{PROJECT_SLUG}/{SESSION_CULLED}.jsonl"
    env = git_env(claude_dir)

    # Find the commit BEFORE deletion to get the file content
    log_result = git(claude_dir, "log", "--all", "-1", "--pretty=format:%H",
                     "--diff-filter=D", "--", file_path, env=env)
    if log_result.returncode != 0 or not log_result.stdout.strip():
        row.observed = "Could not find deletion commit"
        row.verdict = "ERROR"
        return
    delete_commit = log_result.stdout.strip()
    parent_result = git(claude_dir, "rev-parse", f"{delete_commit}~1", env=env)
    if parent_result.returncode != 0:
        row.observed = "Could not resolve parent of deletion commit"
        row.verdict = "ERROR"
        return
    parent_commit = parent_result.stdout.strip()

    # Get the historical blob bytes
    show_result = subprocess.run(
        ["git", "-C", str(claude_dir),
         "-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "core.safecrlf=false",
         "show", f"{parent_commit}:{file_path}"],
        capture_output=True, text=False, env=env, check=False,
    )
    if show_result.returncode != 0:
        row.observed = f"git show failed: rc={show_result.returncode}"
        row.verdict = "ERROR"
        return

    blob_bytes = show_result.stdout

    # Test: does extract_metadata accept these bytes? Try via a temp file
    # (the current API is path-only).
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        tf.write(blob_bytes)
        tf_path = Path(tf.name)
    try:
        meta = extract_metadata(tf_path)
        row.observed = (
            f"blob_bytes={len(blob_bytes)}B; "
            f"extract_metadata(temp-file) -> session_id={meta.session_id[:8]}..., "
            f"name={meta.session_name!r}, "
            f"folders={len(meta.folder_usage)}, "
            f"start_folder={meta.start_folder!r}"
        )
        # The signal we care about: did it parse cleanly + recover the expected metadata?
        if (meta.session_id == SESSION_CULLED
                and meta.session_name == SESSIONS[SESSION_CULLED][0]
                and len(meta.folder_usage) >= 1):
            row.verdict = "DOCUMENTED"
            row.notes.append(
                "extract_metadata works on the historical blob via a temp-file. "
                "Phase 4 should add `extract_metadata_from_bytes(blob)` to "
                "avoid the temp-file dance (cheap refactor of the parsing loop).")
        else:
            row.verdict = "ERROR"
            row.notes.append(f"Metadata didn't match: name={meta.session_name!r} "
                              f"expected {SESSIONS[SESSION_CULLED][0]!r}; "
                              f"folders={len(meta.folder_usage)}")
    finally:
        tf_path.unlink(missing_ok=True)


def scenario_5_git_list_deleted_jsonls(claude_dir: Path, row: ScenarioRow) -> None:
    """git log --all --diff-filter=D --name-only -- 'projects/*.jsonl' enumeration."""
    env = git_env(claude_dir)
    result = subprocess.run(
        ["git", "-C", str(claude_dir), "log", "--all",
         "--pretty=format:%H|%cI", "--name-only",
         "--diff-filter=D",
         "--", ":(glob)projects/*/*.jsonl"],
        capture_output=True, text=True, env=env, check=False,
    )
    if result.returncode != 0:
        row.observed = f"git log failed: rc={result.returncode}: {result.stderr[:200]}"
        row.verdict = "ERROR"
        return

    # Parse: alternating commit-line / path-line
    deletions = []
    current_commit = None
    current_iso = None
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line:
            current_commit = None
            current_iso = None
            continue
        if "|" in line and current_commit is None:
            current_commit, current_iso = line.split("|", 1)
            continue
        m = re.search(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", line)
        if m:
            deletions.append({
                "path": line, "uuid": m.group(1),
                "deleted_commit": current_commit, "deleted_at": current_iso,
            })

    row.observed = (f"raw_lines={len(result.stdout.splitlines())}; "
                    f"parsed_deletions={len(deletions)}; "
                    f"found_culled={any(d['uuid'] == SESSION_CULLED for d in deletions)}")
    if any(d["uuid"] == SESSION_CULLED for d in deletions):
        row.verdict = "DOCUMENTED"
        row.notes.append(
            "`git log --diff-filter=D` correctly enumerates the cull. "
            "Phase 3 can use this directly to populate git_deleted_jsonls.")
    else:
        row.verdict = "ERROR"
        row.notes.append("Expected to find the culled UUID in git log output")


def scenario_6_rename_bak_pattern(claude_dir: Path, db_path: Path,
                                   row: ScenarioRow) -> None:
    """os.rename for the .bak move-aside-then-restore pattern."""
    # Snapshot current bytes
    pre_size = db_path.stat().st_size
    bak_path = db_path.with_suffix(db_path.suffix + ".bak")

    # Move aside
    os.rename(str(db_path), str(bak_path))
    bak_exists = bak_path.exists()
    db_gone = not db_path.exists()

    # Simulate failure: restore the .bak
    os.rename(str(bak_path), str(db_path))
    restored_size = db_path.stat().st_size
    bak_cleaned = not bak_path.exists()

    row.observed = (
        f"pre_size={pre_size}; bak_after_move={bak_exists}; db_gone_after_move={db_gone}; "
        f"restored_size={restored_size}; bak_cleaned_after_restore={bak_cleaned}"
    )
    if (pre_size == restored_size and bak_exists and db_gone and bak_cleaned):
        row.verdict = "DOCUMENTED"
        row.notes.append("os.rename safely moves aside and restores. Phase 2 can use this pattern.")
    else:
        row.verdict = "ERROR"


def scenario_7_csb_list_deleted_after_rebuild(claude_dir: Path, db_path: Path,
                                              row: ScenarioRow) -> None:
    """csb list --deleted only after a rebuild -- what's visible?"""
    # Run after scenario 3 (so the rebuild has happened)
    n_deleted = count_sessions(db_path, deleted_only=True)
    folders_for_culled = folder_count_for(db_path, SESSION_CULLED)
    culled_row = session_row(db_path, SESSION_CULLED)

    row.observed = (f"deleted_count={n_deleted}; "
                    f"culled_row_present={culled_row is not None}; "
                    f"culled_folder_usage={folders_for_culled}")
    if n_deleted == 0 and culled_row is None:
        row.verdict = "CONFIRMED-BUG"
        row.notes.append(
            "`csb list --deleted only` is empty after rebuild -- exactly the "
            "user-visible symptom the invariant note describes.")
    elif n_deleted >= 1 and culled_row is not None:
        row.verdict = "DISPROVED"
    else:
        row.verdict = "ERROR"


# ── Report writer ───────────────────────────────────────────────────────

REPORT_HEADER = """# Phase 0 reality-check report -- safe `csb update rebuild-index` (v0.3.11)

**Run at:** {timestamp}
**Platform:** {platform}
**Python:** {python}
**csb HEAD:** {head}

This report is auto-generated by `tests/one-offs/rebuild_reality_check.py`. It
runs every scenario in a sandboxed temp directory; the real `~/.claude` is
never touched.

## Summary

{summary_line}

## Scenarios

| # | Name | Layer | Hypothesis | Observed | Verdict |
|---|------|-------|------------|----------|---------|
{rows}

## Notes per scenario

{notes}

## Verdict legend

- **CONFIRMED-BUG** -- the hypothesis (a bug) was observed exactly as expected.
- **DOCUMENTED** -- behavior recorded for design reference (no bug-or-fix sense).
- **DISPROVED** -- the bug hypothesis was NOT reproduced; revisit the design.
- **ERROR** -- the scenario itself failed to run cleanly; investigate.

## Plan reference

- Plan: `2026-06-02__15-46-56__claude-plan__safe-update-umbrella-and-backfill-v0.3.11.md`
- DWP: `2026-06-02__15-20-57__safe-rebuild-index-and-deleted-session-cache.md`
- Invariant note: `notes/architecture/2026-06-02__14-00-03__both_rebuild-index-must-not-lose-deleted-sessions.md`
"""


def write_report(rows: list[ScenarioRow], head: str, dest: Path) -> None:
    bug_count = sum(1 for r in rows if r.verdict == "CONFIRMED-BUG")
    err_count = sum(1 for r in rows if r.verdict == "ERROR")
    doc_count = sum(1 for r in rows if r.verdict == "DOCUMENTED")
    dis_count = sum(1 for r in rows if r.verdict == "DISPROVED")
    summary_line = (
        f"**{bug_count}** CONFIRMED-BUG / **{dis_count}** DISPROVED / "
        f"**{doc_count}** DOCUMENTED / **{err_count}** ERROR "
        f"across {len(rows)} scenarios."
    )

    table_rows = "\n".join(r.md_row() for r in rows)
    notes_blocks = []
    for r in rows:
        if r.notes:
            notes_blocks.append(f"### {r.number}. {r.name}\n\n"
                                + "\n".join(f"- {n}" for n in r.notes))
    notes = "\n\n".join(notes_blocks) or "_No per-scenario notes._"

    body = REPORT_HEADER.format(
        timestamp=datetime.now(timezone.utc).isoformat(),
        platform=f"{platform.system()} {platform.release()}",
        python=sys.version.split()[0],
        head=head,
        summary_line=summary_line,
        rows=table_rows,
        notes=notes,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")
    print(f"\nReport written: {dest}")


def get_head_short(claude_dir: Path = None) -> str:
    """Get the csb repo's HEAD (not the sandbox's)."""
    repo = _REPO_ROOT
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--pretty=format:%h %s"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "(unknown)"


# ── Orchestration ───────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-report", action="store_true",
                        help="Skip writing the markdown report.")
    args = parser.parse_args()

    rows = [
        ScenarioRow(1, "Initial backup (3 sessions)",
                    "cmd_backup + DB upsert",
                    "Fresh repo → 3 active rows, 0 deleted"),
        ScenarioRow(2, "Cull + backup (1 deleted)",
                    "cmd_backup deletion-detection + folder_usage preservation",
                    "After cull + backup, culled session is marked deleted_at, folder_usage intact"),
        ScenarioRow(3, "Rebuild-index data loss (THE BUG)",
                    "cmd_rebuild_index (current impl)",
                    "After rebuild, culled session's row + folder_usage erased"),
        ScenarioRow(4, "extract_metadata from historical bytes",
                    "metadata.extract_metadata + git show",
                    "Bytes from `git show <commit>:<path>` parse correctly via extract_metadata"),
        ScenarioRow(5, "git log --diff-filter=D enumeration",
                    "git ops (Phase 3 design)",
                    "`git log --all --diff-filter=D --name-only` lists culled JSONLs with commit + timestamp"),
        ScenarioRow(6, "os.rename .bak move-aside",
                    "Phase 2 crash-safety design",
                    "Move-aside-then-restore via os.rename is atomic + reversible"),
        ScenarioRow(7, "csb list --deleted only after rebuild",
                    "user-visible symptom",
                    "Post-rebuild, `csb list --deleted only` is empty (the user-visible bug)"),
    ]

    head = get_head_short()
    with tempfile.TemporaryDirectory(prefix="csb-rebuild-check-") as tmp_str:
        tmp = Path(tmp_str)
        print(f"=== Sandbox: {tmp} ===")
        claude_dir = setup_sandbox(tmp)
        db_path = tmp / "rebuild-check.db"
        print(f"=== Claude dir: {claude_dir} ===")

        # Scenario 1
        scenario_1_initial_backup(claude_dir, db_path, rows[0])
        print(f"  [1] {rows[0].verdict}: {rows[0].observed}")

        # Scenario 2 (depends on 1)
        scenario_2_cull_and_backup(claude_dir, db_path, rows[1])
        print(f"  [2] {rows[1].verdict}: {rows[1].observed}")

        # Capture pre-rebuild state for scenario 3
        pre_state = {
            "total": count_sessions(db_path, deleted_only=False),
            "deleted": count_sessions(db_path, deleted_only=True),
            "folders_culled": folder_count_for(db_path, SESSION_CULLED),
        }

        # Scenario 4 (before the destructive rebuild)
        scenario_4_extract_metadata_from_bytes(claude_dir, db_path, rows[3])
        print(f"  [4] {rows[3].verdict}: {rows[3].observed}")

        # Scenario 5 (still pre-rebuild)
        scenario_5_git_list_deleted_jsonls(claude_dir, rows[4])
        print(f"  [5] {rows[4].verdict}: {rows[4].observed}")

        # Scenario 6 (still pre-rebuild)
        scenario_6_rename_bak_pattern(claude_dir, db_path, rows[5])
        print(f"  [6] {rows[5].verdict}: {rows[5].observed}")

        # Scenario 3 (the destructive one)
        scenario_3_rebuild_loss(claude_dir, db_path, rows[2], pre_state)
        print(f"  [3] {rows[2].verdict}: {rows[2].observed}")

        # Scenario 7 (post-rebuild)
        scenario_7_csb_list_deleted_after_rebuild(claude_dir, db_path, rows[6])
        print(f"  [7] {rows[6].verdict}: {rows[6].observed}")

        if not args.no_report:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d__%H-%M-%S")
            dest = _REPO_ROOT / "private" / "claude" / f"{ts}__rebuild-reality-check-report.md"
            write_report(rows, head, dest)
        else:
            print("\n(skipping report per --no-report)")

    print("\n=== Summary ===")
    for r in rows:
        print(f"  [{r.number}] {r.verdict}: {r.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
restore_reality_check.py -- Phase 0 of the csb restore shoring-up plan.

Goal: convert hypotheses into documented facts before any production code changes.
Runs every scenario in a *fully sandboxed* temp directory. Does NOT touch the real
~/.claude. Produces a markdown report in private/claude/ with observed evidence.

Plan reference: private/claude/2026-05-16__16-30-43__claude-plan__shore-up-csb-restore-subsystem.md
Design doc:     private/claude/2026-05-16__14-57-24__csb-deleted-session-discovery-and-recovery.md

Usage:
    python tests/one-offs/restore_reality_check.py
    python tests/one-offs/restore_reality_check.py --report-only   # don't run, just print existing report

Exit codes:
    0 -- all scenarios executed (whether or not they confirm bugs)
    1 -- script error (couldn't run scenarios)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── Test fixture data ────────────────────────────────────────────────────

SESSION_UUID = "00000000-1111-2222-3333-444444444444"
PROJECT_SLUG = "C--scratch-csb-reality-check"  # sanitized fake path
# A JSONL with LF-only line endings AND an embedded \r inside a JSON string value
# (so we can test both line-ending translation and content preservation)
SAMPLE_EVENTS = [
    {"type": "custom-title", "customTitle": "reality-check", "sessionId": SESSION_UUID},
    {"type": "user", "timestamp": "2026-05-16T20:00:00Z", "cwd": "/scratch/csb-reality-check",
     "version": "test", "sessionId": SESSION_UUID, "uuid": "u1",
     "message": {"role": "user", "content": "embedded CR: a\rb (literal carriage return)"}},
    {"type": "assistant", "timestamp": "2026-05-16T20:00:05Z", "cwd": "/scratch/csb-reality-check",
     "sessionId": SESSION_UUID, "uuid": "u2"},
]


# ── Report data structures ───────────────────────────────────────────────

@dataclass
class ScenarioRow:
    number: int
    name: str
    layer_tested: str
    hypothesis: str
    observed: str = ""
    verdict: str = ""  # CONFIRMED / DISPROVED / N/A / SKIPPED
    notes: list[str] = field(default_factory=list)

    def md_row(self) -> str:
        # Pipes and newlines need to be escaped/squashed for markdown tables
        def esc(s: str) -> str:
            return s.replace("|", "\\|").replace("\n", " ")
        return f"| {self.number} | {esc(self.name)} | {esc(self.layer_tested)} | {esc(self.hypothesis)} | {esc(self.observed)} | {self.verdict} |"


# ── Utilities ────────────────────────────────────────────────────────────

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def run(cmd: list[str], cwd: Optional[Path] = None, env: Optional[dict] = None,
        check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def git(claude_dir: Path, *args: str, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(claude_dir), *args], env=env)


def git_env(claude_dir: Path) -> dict:
    """Minimal env for unattended git ops in tests."""
    base = dict(os.environ)
    base.update({
        "GIT_AUTHOR_NAME": "csb-reality-check",
        "GIT_AUTHOR_EMAIL": "csb@reality-check.local",
        "GIT_COMMITTER_NAME": "csb-reality-check",
        "GIT_COMMITTER_EMAIL": "csb@reality-check.local",
    })
    return base


def setup_sandbox(tmp: Path) -> Path:
    """Build a sandboxed ~/.claude lookalike: git repo, projects/, session-states/."""
    claude = tmp / ".claude"
    projects = claude / "projects" / PROJECT_SLUG
    states = claude / "session-states"
    projects.mkdir(parents=True)
    states.mkdir(parents=True)

    # Write the JSONL with explicit LF-only line endings (write_bytes bypasses
    # any text-mode translation Python might apply on Windows).
    jsonl_text = "\n".join(json.dumps(e, separators=(",", ":")) for e in SAMPLE_EVENTS) + "\n"
    jsonl_bytes = jsonl_text.encode("utf-8")
    jsonl_path = projects / f"{SESSION_UUID}.jsonl"
    jsonl_path.write_bytes(jsonl_bytes)

    # session-state sidecar (csb's metadata enrichment expects it)
    state_path = states / f"{SESSION_UUID}.json"
    state_path.write_text(json.dumps({
        "session_id": SESSION_UUID,
        "transcript_path": str(jsonl_path),
        "original_cwd": "/scratch/csb-reality-check",
        "cwd": "/scratch/csb-reality-check",
        "current_name": "reality-check",
    }), encoding="utf-8")

    # git init + initial commit
    env = git_env(claude)
    run(["git", "init", str(claude)], env=env, check=True)
    git(claude, "config", "commit.gpgsign", "false", env=env)
    git(claude, "config", "user.name", "csb-reality-check", env=env)
    git(claude, "config", "user.email", "csb@reality-check.local", env=env)

    # NOTE: deliberately do NOT set core.autocrlf here. We want to observe
    # whatever the host git's default behavior is. On Windows installers,
    # this is often "true" or "input" -- which is precisely what we want
    # to detect.
    git(claude, "add", "-A", env=env)
    git(claude, "commit", "--no-gpg-sign", "-m", "initial", env=env)

    return claude


def get_git_config(claude_dir: Path, key: str) -> str:
    """Read a git config value (effective, including system + global + local)."""
    env = git_env(claude_dir)
    result = git(claude_dir, "config", "--get", key, env=env)
    return (result.stdout or "").strip() or "(unset)"


# ── Scenario implementations ─────────────────────────────────────────────

def scenario_3_git_config(claude_dir: Path, row: ScenarioRow) -> None:
    """What's the host git's effective autocrlf / eol setting?"""
    autocrlf = get_git_config(claude_dir, "core.autocrlf")
    eol = get_git_config(claude_dir, "core.eol")
    safecrlf = get_git_config(claude_dir, "core.safecrlf")
    row.observed = (
        f"core.autocrlf={autocrlf}; core.eol={eol}; core.safecrlf={safecrlf}; "
        f"platform={platform.system()}; py={sys.version.split()[0]}"
    )
    row.verdict = "DOCUMENTED"
    row.notes.append(
        "These values affect every `git show` and `git checkout` in csb's restore path."
    )


def scenario_2_committed_bytes(claude_dir: Path, original_bytes: bytes, row: ScenarioRow) -> None:
    """Does the committed git blob match the bytes we wrote to disk?"""
    env = git_env(claude_dir)
    file_path = f"projects/{PROJECT_SLUG}/{SESSION_UUID}.jsonl"

    # `git show HEAD:<path>` with text=False returns raw blob bytes
    result = subprocess.run(
        ["git", "-C", str(claude_dir),
         "-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "core.safecrlf=false",
         "show", f"HEAD:{file_path}"],
        capture_output=True, text=False, env=env, check=False,
    )
    if result.returncode != 0:
        row.observed = f"git show failed: rc={result.returncode}"
        row.verdict = "ERROR"
        return

    blob_bytes = result.stdout
    blob_hash = sha256_bytes(blob_bytes)
    orig_hash = sha256_bytes(original_bytes)

    if blob_hash == orig_hash:
        row.observed = (
            f"blob sha256={blob_hash[:12]} == original sha256={orig_hash[:12]} "
            f"(both {len(blob_bytes)} bytes). Backup preserved bytes."
        )
        row.verdict = "DISPROVED"
        row.notes.append("With explicit -c core.autocrlf=false on the read, blob matches disk.")
    else:
        # Compute byte-level diff
        diff_summary = describe_byte_diff(original_bytes, blob_bytes)
        row.observed = (
            f"blob sha256={blob_hash[:12]} != original sha256={orig_hash[:12]} "
            f"(blob {len(blob_bytes)} bytes vs original {len(original_bytes)} bytes). "
            f"Diff: {diff_summary}"
        )
        row.verdict = "CONFIRMED"
        row.notes.append("Committed bytes differ from on-disk bytes -- corruption is at backup time, not just restore.")


def describe_byte_diff(a: bytes, b: bytes) -> str:
    """Cheap byte-diff summary for the report."""
    if a == b:
        return "identical"
    a_cr = a.count(b"\r")
    a_lf = a.count(b"\n")
    a_crlf = a.count(b"\r\n")
    b_cr = b.count(b"\r")
    b_lf = b.count(b"\n")
    b_crlf = b.count(b"\r\n")
    return (
        f"original CR={a_cr}/LF={a_lf}/CRLF={a_crlf}; "
        f"blob CR={b_cr}/LF={b_lf}/CRLF={b_crlf}; "
        f"len delta={len(b) - len(a):+d}"
    )


def scenario_1_restore_roundtrip(claude_dir: Path, db_path: Path,
                                  original_bytes: bytes, row: ScenarioRow) -> None:
    """Full backup -> delete -> restore -> byte-compare cycle via csb's actual code.

    Tests TWO paths in sequence:
      (a) The exact production path -- str(Path.relative_to()) which produces
          backslashes on Windows. Observes the real user-facing behavior.
      (b) A forward-slash bypass -- to isolate CRLF behavior from path-separator
          bugs. Tells us whether byte-fidelity would also fail even if the path
          bug were fixed.
    """
    from claude_session_backup.scanner import scan_projects
    from claude_session_backup.metadata import extract_metadata, read_session_state
    from claude_session_backup.index import open_db, init_schema, upsert_session, mark_deleted, get_session
    from claude_session_backup.git_ops import git_find_deleted_file, git_restore_file

    # Phase A: index the session (simulates csb backup --no-commit)
    conn = open_db(str(db_path))
    init_schema(conn)
    sessions = scan_projects(str(claude_dir))
    if not sessions:
        row.observed = "scan_projects returned no sessions -- fixture broken?"
        row.verdict = "ERROR"
        return
    sf = sessions[0]
    meta = extract_metadata(sf.jsonl_path)
    meta.project = sf.project
    if sf.state_file:
        state = read_session_state(sf.state_file)
        from claude_session_backup.metadata import enrich_metadata
        enrich_metadata(meta, state, None)
    rel_path = str(sf.jsonl_path.relative_to(claude_dir))  # backslash on Windows -- matches production
    upsert_session(conn, meta, rel_path, sf.jsonl_size, sf.jsonl_mtime, "2026-05-16T20:00:00Z")
    conn.commit()

    # Phase B: commit the JSONL to git (simulates csb backup full)
    env = git_env(claude_dir)
    git(claude_dir, "add", "-A", env=env)
    git(claude_dir, "commit", "--no-gpg-sign", "-m", "second", env=env)
    # ok if "nothing to commit" -- means it was in initial commit

    # Phase C: capture bytes BEFORE deletion (for comparison)
    pre_delete_bytes = sf.jsonl_path.read_bytes()
    pre_delete_hash = sha256_bytes(pre_delete_bytes)

    # Phase D: delete from disk and mark in DB
    sf.jsonl_path.unlink()
    mark_deleted(conn, sf.session_id, "2026-05-16T20:00:01Z")
    conn.commit()
    conn.close()

    # Phase E1: production-path restore -- exactly what cmd_restore does today
    conn = open_db(str(db_path))
    init_schema(conn)
    session_row = get_session(conn, sf.session_id)
    conn.close()
    if not session_row:
        row.observed = "DB row missing after mark_deleted -- shouldn't happen"
        row.verdict = "ERROR"
        return

    jsonl_rel_prod = session_row["jsonl_path"]  # whatever csb stored (backslash on Windows)
    commit_for_restore = git_find_deleted_file(str(claude_dir), jsonl_rel_prod)
    if not commit_for_restore:
        row.observed = "git_find_deleted_file returned None -- file not in git history"
        row.verdict = "ERROR"
        return

    full_dest = str(claude_dir / jsonl_rel_prod)
    prod_success = git_restore_file(str(claude_dir), commit_for_restore, jsonl_rel_prod, full_dest)
    prod_observation = ""
    prod_verdict = ""

    if not prod_success:
        prod_verdict = "CONFIRMED (broken)"
        prod_observation = (
            f"git_restore_file returned False with production path '{jsonl_rel_prod!r}'. "
            "Root cause: `git show <commit>:<path>` requires forward slashes; "
            "csb stores backslashes from str(Path.relative_to()) on Windows. "
            "Restore is FUNCTIONALLY BROKEN on Windows today -- not just byte-imperfect."
        )
        row.notes.append(
            "Phase 1 must include path-separator normalization (use `.as_posix()` "
            "when storing or passing paths to git operations), in addition to "
            "the planned byte-purity fix."
        )
    else:
        # Restore claimed success -- byte-compare
        restored_bytes_prod = Path(full_dest).read_bytes()
        if sha256_bytes(restored_bytes_prod) == pre_delete_hash:
            prod_verdict = "DISPROVED"
            prod_observation = "production-path restore succeeded byte-pure (path bug doesn't apply here)"
        else:
            prod_verdict = "CONFIRMED (byte-imperfect)"
            prod_observation = (
                f"production-path restore claimed success but bytes differ. "
                f"Diff: {describe_byte_diff(pre_delete_bytes, restored_bytes_prod)}"
            )

    # Phase E2: forward-slash bypass to isolate the CRLF question
    # Remove the file again (in case prod path succeeded) and retry with normalized path
    bypass_observation = ""
    try:
        if Path(full_dest).exists():
            Path(full_dest).unlink()
    except OSError:
        pass

    jsonl_rel_posix = (Path(jsonl_rel_prod).as_posix())
    bypass_success = git_restore_file(str(claude_dir), commit_for_restore, jsonl_rel_posix, full_dest)
    if not bypass_success:
        bypass_observation = f"forward-slash retry ALSO returned False -- both path AND something else are broken"
    else:
        restored_bytes_bypass = Path(full_dest).read_bytes()
        if sha256_bytes(restored_bytes_bypass) == pre_delete_hash:
            bypass_observation = (
                f"forward-slash retry succeeded byte-pure ({len(restored_bytes_bypass)} bytes). "
                "CRLF hypothesis is DISPROVED for this content -- the only Windows blocker is the path-separator bug."
            )
        else:
            bypass_observation = (
                f"forward-slash retry succeeded but bytes DIFFER. "
                f"Diff: {describe_byte_diff(pre_delete_bytes, restored_bytes_bypass)}. "
                "CRLF hypothesis is CONFIRMED -- byte fidelity is ALSO broken after fixing paths."
            )
            row.notes.append(
                "Both the path-separator bug AND the byte-purity bug are real. "
                "Phase 1 must fix both."
            )

    row.observed = f"[prod path] {prod_observation}  [bypass test] {bypass_observation}"
    row.verdict = prod_verdict


def scenario_7_backslash_path(claude_dir: Path, row: ScenarioRow) -> None:
    """Isolate the Windows backslash-in-git-show bug in a controlled probe."""
    from claude_session_backup.git_ops import git_show_file

    # Use HEAD (initial commit contains the JSONL)
    rel_back = f"projects\\{PROJECT_SLUG}\\{SESSION_UUID}.jsonl"
    rel_fwd = f"projects/{PROJECT_SLUG}/{SESSION_UUID}.jsonl"

    env = git_env(claude_dir)
    head = git(claude_dir, "rev-parse", "HEAD", env=env)
    if head.returncode != 0:
        row.observed = "could not resolve HEAD"
        row.verdict = "ERROR"
        return
    commit = head.stdout.strip()

    back_content = git_show_file(str(claude_dir), commit, rel_back)
    fwd_content = git_show_file(str(claude_dir), commit, rel_fwd)

    if platform.system() == "Windows":
        if back_content is None and fwd_content is not None:
            row.observed = (
                f"on Windows: `git show {commit[:8]}:{rel_back}` returns None; "
                f"`git show {commit[:8]}:{rel_fwd}` returns {len(fwd_content)} chars. "
                "Backslash-path bug CONFIRMED -- this is exactly the production code path."
            )
            row.verdict = "CONFIRMED"
            row.notes.append(
                "Phase 1 fix: normalize jsonl_path with `.as_posix()` everywhere it crosses "
                "the git boundary. Affects upsert_session storage AND git_restore_file argument."
            )
        elif back_content is not None and fwd_content is not None:
            row.observed = "both forms returned content -- backslash bug NOT present (unexpected)"
            row.verdict = "DISPROVED"
        else:
            row.observed = f"unexpected: backslash returned {type(back_content).__name__}, forward returned {type(fwd_content).__name__}"
            row.verdict = "ERROR"
    else:
        # On POSIX, "backslash" is just a literal character in the path, so this test is N/A
        row.observed = f"skipped on {platform.system()} (backslash isn't a path separator here)"
        row.verdict = "N/A"


def scenario_4_restore_no_db_row(claude_dir: Path, db_path: Path, row: ScenarioRow) -> None:
    """What happens to `csb restore` when the DB has no row but the file is in git?"""
    from claude_session_backup.index import open_db, init_schema, get_session

    # Wipe the DB (simulates rebuild-index losing the deleted_at flag) and reopen empty
    db_path.unlink(missing_ok=True)
    conn = open_db(str(db_path))
    init_schema(conn)
    session_row = get_session(conn, SESSION_UUID)
    conn.close()

    # Simulate what cmd_restore does (commands.py:316-322)
    if not session_row:
        row.observed = "get_session(prefix) returned None for a UUID present in git history."
        row.verdict = "CONFIRMED"
        row.notes.append("This is exactly what #28 addresses -- DB-row lookup blocks recovery.")
    else:
        row.observed = "get_session returned a row -- hypothesis wrong"
        row.verdict = "DISPROVED"


def scenario_6_git_find_deleted_file(claude_dir: Path, row: ScenarioRow) -> None:
    """Does git_find_deleted_file return a usable commit after a real deletion?"""
    from claude_session_backup.git_ops import git_find_deleted_file

    file_path = f"projects/{PROJECT_SLUG}/{SESSION_UUID}.jsonl"
    # Ensure the file is currently absent from disk (test runs sequentially; scenario_1 already deleted it)
    on_disk = (claude_dir / file_path).exists()

    commit = git_find_deleted_file(str(claude_dir), file_path)
    if commit:
        # Validate it's a real commit hash
        env = git_env(claude_dir)
        verify = git(claude_dir, "rev-parse", "--verify", commit, env=env)
        if verify.returncode == 0:
            row.observed = (
                f"returned commit {commit[:12]} (valid); file on disk: {on_disk}. "
                f"`git show` against this commit should yield the JSONL bytes."
            )
            row.verdict = "DISPROVED"  # hypothesis was "may not work"; it works
            row.notes.append("#28's premise that git_find_deleted_file is functional is confirmed.")
        else:
            row.observed = f"returned {commit[:12]} but rev-parse failed: {verify.stderr.strip()}"
            row.verdict = "CONFIRMED"
    else:
        row.observed = "git_find_deleted_file returned None"
        row.verdict = "CONFIRMED"
        row.notes.append("Cannot locate commit for deleted file -- restore would fail.")


def scenario_5_claude_resume(row: ScenarioRow) -> None:
    """Can claude --resume actually load a byte-perfect restored JSONL?"""
    row.observed = "Not auto-runnable: requires a real `claude` binary, a real user account, and a TTY. The script cannot launch claude safely in CI/sandbox without risk of interactive prompts and credential use. This scenario MUST be executed manually as part of the Phase 4 checklist."
    row.verdict = "MANUAL"
    row.notes.append(
        "Phase 4 checklist will document the manual procedure: restore a real session, "
        "run `claude --resume <uuid>`, confirm load + send one message, capture result."
    )


# ── Report writer ────────────────────────────────────────────────────────

def write_report(rows: list[ScenarioRow], report_path: Path, sandbox_path: Path) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    table_header = (
        "| # | Scenario | Layer tested | Hypothesis | Observed | Verdict |\n"
        "|---|---|---|---|---|---|\n"
    )
    table_body = "\n".join(r.md_row() for r in rows)

    notes_sections = []
    for r in rows:
        if r.notes:
            notes_md = "\n".join(f"- {n}" for n in r.notes)
            notes_sections.append(f"### Scenario {r.number} notes\n\n{notes_md}\n")

    confirmed = [r for r in rows if r.verdict == "CONFIRMED"]
    disproved = [r for r in rows if r.verdict == "DISPROVED"]
    manual = [r for r in rows if r.verdict == "MANUAL"]
    other = [r for r in rows if r.verdict not in {"CONFIRMED", "DISPROVED", "MANUAL"}]

    content = f"""# Restore Reality Check Report

**Run at:** {now}
**Platform:** {platform.system()} {platform.release()} (Python {sys.version.split()[0]})
**Sandbox:** `{sandbox_path}` (deleted after this report was written)
**Script:** `tests/one-offs/restore_reality_check.py`
**Plan:** `private/claude/2026-05-16__16-30-43__claude-plan__shore-up-csb-restore-subsystem.md`

---

## Summary

- **CONFIRMED** (hypothesis matches reality, bug-class is real): {len(confirmed)} of {len(rows)}
- **DISPROVED** (hypothesis was wrong, no fix needed for this layer): {len(disproved)} of {len(rows)}
- **MANUAL** (cannot be automated, deferred to Phase 4 checklist): {len(manual)} of {len(rows)}
- **Other** (DOCUMENTED/ERROR/SKIPPED): {len(other)} of {len(rows)}

---

## Findings table

{table_header}{table_body}

---

## Per-scenario notes

{''.join(notes_sections) if notes_sections else '(no additional notes)'}

---

## What this changes in the plan

Read the verdict column carefully -- each CONFIRMED row is a fix Phase 1/2/3 must
actually deliver; each DISPROVED row removes a fix from the plan (no need to fight
a battle that doesn't exist). MANUAL rows define the Phase 4 checklist scope.

If `claude --resume` (Scenario 5) ever fails on a byte-perfect restored JSONL
during Phase 4's manual execution, the plan must be revised before the v0.3.0-alpha
ship date -- byte-perfect restore that Claude Code rejects is still data loss from
the user's perspective.

---

## Reproducibility

To rerun this report:

```
cd C:\\code\\claude-projects\\Claude-Session-Backup
python tests/one-offs/restore_reality_check.py
```

The script is sandboxed -- it builds its own temp git repo, runs scenarios, and
deletes the sandbox before exit. It does NOT touch `~/.claude`.
"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(content.encode("utf-8"))


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--report-path", default=None,
        help="Output path for the markdown report. Defaults to "
             "private/claude/<timestamp>__restore-reality-check-report.md",
    )
    args = ap.parse_args()

    # Add the project root to sys.path so `import claude_session_backup` works
    project_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project_root))

    # Decide report destination
    if args.report_path:
        report_path = Path(args.report_path).resolve()
    else:
        ts = datetime.now().strftime("%Y-%m-%d__%H-%M-%S")
        report_path = project_root / "private" / "claude" / f"{ts}__restore-reality-check-report.md"

    print(f"[+] Reality check starting. Report will be written to:\n    {report_path}\n")

    # Build scenario rows in deterministic order
    rows = [
        ScenarioRow(
            number=1,
            name="Backup + delete + restore: byte-compare original vs restored",
            layer_tested="full restore pipeline (git show + write_text)",
            hypothesis="bytes differ on Windows due to CRLF translation in subprocess text mode + Path.write_text",
        ),
        ScenarioRow(
            number=2,
            name="Inspect committed git blob bytes vs on-disk source",
            layer_tested="csb backup -> git add (autocrlf smudge)",
            hypothesis="committed blob may already be CRLF-corrupted if host git autocrlf=true",
        ),
        ScenarioRow(
            number=3,
            name="Document host git config core.autocrlf / core.eol / core.safecrlf",
            layer_tested="environment baseline",
            hypothesis="host config will reveal which translation layer is active",
        ),
        ScenarioRow(
            number=4,
            name="csb restore <uuid> when DB row is wiped (rebuild-index simulation)",
            layer_tested="cmd_restore session lookup",
            hypothesis="cmd_restore bails with 'No session found' even though git has the file",
        ),
        ScenarioRow(
            number=5,
            name="claude --resume <uuid> on byte-perfect restored JSONL",
            layer_tested="Claude Code's JSONL load tolerance",
            hypothesis="loads cleanly given byte-pure restoration",
        ),
        ScenarioRow(
            number=6,
            name="git_find_deleted_file returns usable commit after deletion",
            layer_tested="restore lookup primitive (git_ops.py:209)",
            hypothesis="returns the parent commit of the deletion, hash is a valid rev",
        ),
        ScenarioRow(
            number=7,
            name="git show <commit>:<rel_path> with Windows backslash separator",
            layer_tested="git_show_file path argument (git_ops.py:197)",
            hypothesis="git show requires forward-slash paths; backslash silently returns nothing",
        ),
    ]

    exit_code = 0
    with tempfile.TemporaryDirectory(prefix="csb-reality-") as tmp_str:
        tmp = Path(tmp_str)
        try:
            print(f"[+] Sandbox: {tmp}")
            claude = setup_sandbox(tmp)
            db_path = tmp / "session-backup.db"
            print(f"[+] Sandbox claude dir: {claude}\n")

            jsonl_path = claude / "projects" / PROJECT_SLUG / f"{SESSION_UUID}.jsonl"
            original_bytes = jsonl_path.read_bytes()
            print(f"[+] Sample JSONL: {len(original_bytes)} bytes, sha256={sha256_bytes(original_bytes)[:12]}\n")

            # Scenario 3 first (no side effects, documents baseline)
            print("[*] Scenario 3: git config baseline")
            scenario_3_git_config(claude, rows[2])
            print(f"    {rows[2].verdict}: {rows[2].observed}\n")

            # Scenario 2: committed blob vs on-disk
            print("[*] Scenario 2: committed blob bytes")
            scenario_2_committed_bytes(claude, original_bytes, rows[1])
            print(f"    {rows[1].verdict}: {rows[1].observed}\n")

            # Scenario 6: git_find_deleted_file (before deletion -- file is currently present
            # so we test the "fallback to last commit where file existed" path)
            print("[*] Scenario 6: git_find_deleted_file probe (file still present)")
            scenario_6_git_find_deleted_file(claude, rows[5])
            print(f"    {rows[5].verdict}: {rows[5].observed}\n")

            # Scenario 1: full backup -> delete -> restore -> byte compare
            print("[*] Scenario 1: backup -> delete -> restore round trip")
            scenario_1_restore_roundtrip(claude, db_path, original_bytes, rows[0])
            print(f"    {rows[0].verdict}: {rows[0].observed}\n")

            # Scenario 4: post-rebuild-index lookup failure (wipes DB, so run last)
            print("[*] Scenario 4: cmd_restore lookup with wiped DB")
            scenario_4_restore_no_db_row(claude, db_path, rows[3])
            print(f"    {rows[3].verdict}: {rows[3].observed}\n")

            # Scenario 5: claude --resume -- MANUAL only
            print("[*] Scenario 5: claude --resume (manual)")
            scenario_5_claude_resume(rows[4])
            print(f"    {rows[4].verdict}: {rows[4].observed}\n")

            # Scenario 7: Windows backslash path bug isolation
            print("[*] Scenario 7: backslash path in git_show_file")
            scenario_7_backslash_path(claude, rows[6])
            print(f"    {rows[6].verdict}: {rows[6].observed}\n")

        except Exception as e:
            print(f"[!] Scenario execution error: {type(e).__name__}: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            exit_code = 1

        # Write the report regardless of partial failures
        write_report(rows, report_path, tmp)
        print(f"\n[+] Report written: {report_path}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

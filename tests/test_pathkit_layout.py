"""
Tests for ClaudePaths -- the single owner of the ~/.claude layout (GH #46).

Covers: accessor/constant agreement, POSIX-out guarantees on Windows,
rel/abs_of roundtrips, parse_rel against the exact production formats
(including pre-#46 backslash drift), the from_dir resolve policy with a
junction/symlink claude_dir, and (Phase 3) the no-scattered-joins guard.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from claude_session_backup.pathkit import ClaudePaths, ParsedRel

UUID = "aabbccdd-1122-3344-5566-77889900aabb"
SLUG = "C--code-test-project"


@pytest.fixture
def cp(tmp_path):
    return ClaudePaths.from_dir(tmp_path)


# ---------------------------------------------------------------- accessors


def test_first_level_accessors_match_constants(cp):
    assert cp.projects == cp.root / "projects"
    assert cp.session_states == cp.root / "session-states"
    assert cp.file_history == cp.root / "file-history"
    assert cp.sesslogs == cp.root / "sesslogs"
    assert cp.distilled == cp.root / "distilled"
    assert cp.fts_dir == cp.root / "csb-fts"
    assert cp.gitattributes == cp.root / ".gitattributes"
    assert cp.default_db == cp.root / "session-backup.db"
    assert cp.lock_file == cp.root / ".csb-backup.lock"
    assert cp.config_file == cp.root / "session-backup-config.json"
    assert cp.settings_file == cp.root / "settings.json"


def test_builders(cp):
    assert cp.jsonl(SLUG, UUID) == cp.projects / SLUG / f"{UUID}.jsonl"
    assert cp.session_dir(SLUG, UUID) == cp.projects / SLUG / UUID
    assert cp.distilled_md(SLUG, UUID) == cp.distilled / SLUG / f"{UUID}.md"


def test_from_dir_accepts_str_and_path(tmp_path):
    assert ClaudePaths.from_dir(str(tmp_path)).root == ClaudePaths.from_dir(tmp_path).root


def test_provenance_sets_partition_all_names():
    """Core Claude Code vs claude-session-logger vs csb-own: every layout
    name belongs to exactly one provenance tier (no overlap, none missing)."""
    tiers = [ClaudePaths.CORE_NAMES, ClaudePaths.COMPANION_NAMES, ClaudePaths.CSB_NAMES]
    union = set().union(*tiers)
    assert sum(len(t) for t in tiers) == len(union), "tiers overlap"
    declared = {
        ClaudePaths.PROJECTS, ClaudePaths.FILE_HISTORY, ClaudePaths.TASKS,
        ClaudePaths.SESSION_ENV, ClaudePaths.SETTINGS_FILE,
        ClaudePaths.SESSION_STATES, ClaudePaths.SESSLOGS,
        ClaudePaths.DISTILLED, ClaudePaths.FTS_DIR, ClaudePaths.DEFAULT_DB,
        ClaudePaths.LOCK_FILE, ClaudePaths.CONFIG_FILE, ClaudePaths.GITATTRIBUTES,
        ClaudePaths.CSB_LOGS,
    }
    assert union == declared


def test_frozen():
    cp = ClaudePaths.from_dir(".")
    with pytest.raises(Exception):
        cp.root = Path("/elsewhere")


# ------------------------------------------------------- relative POSIX form


def test_jsonl_rel_is_posix_forward_slash_everywhere(cp):
    rel = cp.jsonl_rel(SLUG, UUID)
    assert rel == f"projects/{SLUG}/{UUID}.jsonl"
    assert "\\" not in rel


def test_rel_roundtrips_with_abs_of(cp):
    rel = cp.rel(cp.jsonl(SLUG, UUID))
    assert rel == cp.jsonl_rel(SLUG, UUID)
    assert cp.abs_of(rel) == cp.jsonl(SLUG, UUID)


def test_abs_of_tolerates_backslash_drift(cp):
    # Pre-#46 DB rows may carry backslashes (test_commands.py divergence).
    assert cp.abs_of(f"projects\\{SLUG}\\{UUID}.jsonl") == cp.jsonl(SLUG, UUID)


def test_rel_raises_for_paths_outside_root(cp, tmp_path):
    outside = tmp_path.parent / "not-the-claude-dir" / "x.jsonl"
    with pytest.raises(ValueError):
        cp.rel(outside)


# ------------------------------------------------------------------ parse_rel


@pytest.mark.parametrize(
    "rel,expected",
    [
        (f"projects/{SLUG}/{UUID}.jsonl", ParsedRel("projects", SLUG, UUID)),
        (f"projects/{SLUG}/{UUID}/tool-results.json", ParsedRel("projects", SLUG, UUID)),
        (f"projects/{SLUG}", ParsedRel("projects", SLUG, None)),
        (f"projects\\{SLUG}\\{UUID}.jsonl", ParsedRel("projects", SLUG, UUID)),  # drift
        (f"session-states/{UUID}.json", ParsedRel("session-states", None, None)),
        ("sesslogs/", ParsedRel("sesslogs", None, None)),
        ("", ParsedRel("", None, None)),
    ],
)
def test_parse_rel(rel, expected):
    assert ClaudePaths.parse_rel(rel) == expected


# ------------------------------------------------- resolve policy (junction)


def _make_link(target: Path, link: Path) -> bool:
    """Junction on Windows (no elevation needed), symlink on POSIX.

    Returns False if the platform refuses (test skips rather than fails).
    """
    if sys.platform == "win32":
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' | Out-Null"],
            capture_output=True,
        )
        return r.returncode == 0 and link.exists()
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


def test_from_dir_resolves_junction_so_children_relativize(tmp_path):
    """THE resolve policy: a linked claude_dir resolves at construction, so
    every derived child shares the resolved prefix and rel() cannot raise
    (RF2 -- the commands.py:196 junction ValueError class of bug)."""
    real = tmp_path / "real-claude"
    (real / "projects" / SLUG).mkdir(parents=True)
    link = tmp_path / "linked-claude"
    if not _make_link(real, link):
        pytest.skip("platform refused junction/symlink creation")

    cp = ClaudePaths.from_dir(link)
    assert cp.root == real.resolve()

    jsonl = cp.jsonl(SLUG, UUID)
    jsonl.write_text("{}", encoding="utf-8")
    assert cp.rel(jsonl) == cp.jsonl_rel(SLUG, UUID)

    # And the hazard case rel() defends against: a path spelled VIA the link
    # (unresolved) still relativizes against the resolved root.
    via_link = link / "projects" / SLUG / f"{UUID}.jsonl"
    assert cp.rel(via_link) == cp.jsonl_rel(SLUG, UUID)


def test_scan_projects_via_junction_claude_dir(tmp_path):
    """End-to-end RF2 pin: scanning through a junction/symlink claude_dir
    yields children under the RESOLVED root (scanner builds walk roots from
    ClaudePaths), so the backup upsert's rel() computation is consistent by
    construction -- the pre-#46 resolved-vs-raw ValueError is unrepresentable."""
    from claude_session_backup.scanner import scan_projects

    real = tmp_path / "real-claude"
    (real / "projects" / SLUG).mkdir(parents=True)
    jsonl = real / "projects" / SLUG / f"{UUID}.jsonl"
    jsonl.write_text('{"type":"summary"}\n', encoding="utf-8")
    link = tmp_path / "linked-claude"
    if not _make_link(real, link):
        pytest.skip("platform refused junction/symlink creation")

    sessions = scan_projects(str(link))
    assert [s.session_id for s in sessions] == [UUID]

    # The scanner's walk root resolved the link, so children carry the
    # resolved prefix (this assertion goes red if scanner.py reverts to
    # bare Path(claude_dir) / "projects").
    assert str(sessions[0].jsonl_path).startswith(str(real.resolve()))

    # The production upsert seam (commands.py) then yields the canonical rel.
    cp = ClaudePaths.from_dir(str(link))
    assert cp.rel(sessions[0].jsonl_path) == cp.jsonl_rel(SLUG, UUID)


# ----------------------------------------------- home-repo prefix (git_ops)


def test_git_prefix_translation_when_repo_roots_at_parent(mock_claude_dir_nested):
    """First-ever pin of the home-repo configuration (repo at ~, claude_dir
    at ~/.claude): claude_dir-relative paths from ClaudePaths must round-trip
    through git_ops' repo-prefix translation -- `git show <commit>:<path>`
    needs the `.claude/` prefix prepended, `git log --name-only` output needs
    it stripped."""
    from claude_session_backup.git_ops import git_find_jsonl_by_uuid, git_show_file_bytes

    claude_dir = str(mock_claude_dir_nested)
    nested_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    rel = ClaudePaths.from_dir(claude_dir).jsonl_rel("C--code-test", nested_uuid)

    # show: claude_dir-relative in, bytes out (prefix prepended internally)
    blob = git_show_file_bytes(claude_dir, "HEAD", rel)
    assert blob is not None and b'"sessionId"' in blob

    # find: repo-root-relative git output translated BACK to claude_dir-relative
    assert git_find_jsonl_by_uuid(claude_dir, nested_uuid) == [rel]


# ------------------------------------------------------------- guard (#46)


def test_no_literal_first_level_joins_outside_pathkit_and_config():
    """The #46 invariant, enforced forever: no module under
    claude_session_backup/ may join claude_dir to a literal layout name --
    that knowledge lives in ClaudePaths. (Joins of VARIABLES, e.g. the
    NOISE_DIRS staging loops in git_ops, are git-classification iteration
    and intentionally pass.)"""
    pkg = Path(__file__).resolve().parent.parent / "claude_session_backup"
    allowed = {"pathkit.py", "config.py"}
    pattern = re.compile(
        r"""Path\(\s*claude_(?:dir|path)\s*\)\s*/\s*["']"""
        r"""|os\.path\.join\(\s*claude_(?:dir|path)\s*,\s*["']"""
    )
    offenders = []
    for src in sorted(pkg.glob("*.py")):
        if src.name in allowed:
            continue
        for lineno, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{src.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "literal claude_dir joins found outside pathkit/config "
        "(route them through ClaudePaths):\n" + "\n".join(offenders)
    )

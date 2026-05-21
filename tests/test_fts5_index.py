"""Tests for fts5_index.py -- the build_all orchestrator."""

import json
import os
from pathlib import Path

import pytest

from claude_session_backup.fts5_db import open_fts5_db
from claude_session_backup.fts5_index import build_all
from claude_session_backup.fts_paths import fts5_db_path


SID_A = "aaaaaaaa-1111-1111-1111-111111111111"
SID_B = "bbbbbbbb-2222-2222-2222-222222222222"


def _make_main_db(tmp_path: Path):
    """Initialize a main session-backup.db with schema_v3 (session_sources)."""
    from claude_session_backup.index import open_db, init_schema
    db_path = tmp_path / "session-backup.db"
    conn = open_db(str(db_path))
    init_schema(conn, quiet=True)
    return conn


def _insert_session(conn, *, session_id, project, jsonl_rel_path,
                    last_active="2026-05-17T10:00:00Z", deleted=False):
    conn.execute(
        "INSERT INTO sessions "
        "(session_id, project, session_name, jsonl_path, last_active_at, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, project, "n", jsonl_rel_path, last_active,
         "2026-05-17T11:00:00Z" if deleted else None),
    )
    conn.commit()


def _create_jsonl(claude_dir: Path, project_slug: str, session_id: str,
                  events: list[dict] | None = None) -> tuple[Path, str]:
    """Make a JSONL file under claude_dir/projects/<slug>/.
    Returns (abs_path, relative_path_for_db_column).
    """
    if events is None:
        events = [
            {"type": "user", "uuid": "u1", "timestamp": "2026-05-17T10:00:00Z",
             "message": {"content": "hello"}},
            {"type": "assistant", "uuid": "u2", "timestamp": "2026-05-17T10:00:05Z",
             "message": {"content": [{"type": "text", "text": "world"}]}},
        ]
    project_dir = claude_dir / "projects" / project_slug
    project_dir.mkdir(parents=True, exist_ok=True)
    jsonl = project_dir / f"{session_id}.jsonl"
    jsonl.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    rel = f"projects/{project_slug}/{session_id}.jsonl"
    return jsonl, rel


# ── build_all: basic single-session ──────────────────────────────────


def test_build_all_indexes_single_session(tmp_path):
    main = _make_main_db(tmp_path)
    claude_dir = tmp_path
    jsonl, rel = _create_jsonl(claude_dir, "C--code-x", SID_A)
    _insert_session(main, session_id=SID_A, project="C--code-x",
                    jsonl_rel_path=rel)

    stats = build_all(main, claude_dir, quiet=True)
    assert stats.sessions_considered == 1
    assert stats.sessions_indexed == 1
    assert stats.rows_inserted == 2  # USER + AI
    assert stats.projects_touched == 1

    # Per-project FTS5 DB was created
    fts_path = fts5_db_path(claude_dir, "C--code-x", "C--code-x", user="testuser")
    # Filename uses _current_user() in env; verify SOME csb-fts/*.db file exists
    csb_fts_dir = claude_dir / "csb-fts"
    assert csb_fts_dir.exists()
    dbs = list(csb_fts_dir.glob("C--code-x__*.db"))
    assert len(dbs) == 1


def test_build_all_idempotent_when_fresh(tmp_path):
    """Second build with no JSONL changes -> 0 newly-indexed sessions."""
    main = _make_main_db(tmp_path)
    claude_dir = tmp_path
    jsonl, rel = _create_jsonl(claude_dir, "C--code-x", SID_A)
    _insert_session(main, session_id=SID_A, project="C--code-x",
                    jsonl_rel_path=rel)

    stats1 = build_all(main, claude_dir, quiet=True)
    assert stats1.sessions_indexed == 1
    stats2 = build_all(main, claude_dir, quiet=True)
    assert stats2.sessions_indexed == 0
    assert stats2.sessions_skipped_fresh == 1


def test_build_all_force_reindex(tmp_path):
    """--force re-indexes even when fresh."""
    main = _make_main_db(tmp_path)
    claude_dir = tmp_path
    jsonl, rel = _create_jsonl(claude_dir, "C--code-x", SID_A)
    _insert_session(main, session_id=SID_A, project="C--code-x",
                    jsonl_rel_path=rel)

    build_all(main, claude_dir, quiet=True)
    stats = build_all(main, claude_dir, quiet=True, force=True)
    assert stats.sessions_indexed == 1  # re-indexed


def test_build_all_picks_up_mtime_change(tmp_path):
    """JSONL mtime advancing past last_jsonl_mtime triggers re-index."""
    main = _make_main_db(tmp_path)
    claude_dir = tmp_path
    jsonl, rel = _create_jsonl(claude_dir, "C--code-x", SID_A)
    _insert_session(main, session_id=SID_A, project="C--code-x",
                    jsonl_rel_path=rel)
    build_all(main, claude_dir, quiet=True)

    # Touch mtime forward + add a row
    import time
    time.sleep(0.05)
    jsonl.write_text(
        jsonl.read_text(encoding="utf-8")
        + json.dumps({"type": "user", "uuid": "u3", "message": {"content": "added"}})
        + "\n",
        encoding="utf-8",
    )
    os.utime(jsonl, (jsonl.stat().st_atime, jsonl.stat().st_mtime + 60))

    stats = build_all(main, claude_dir, quiet=True)
    assert stats.sessions_indexed == 1  # re-imported because newer


# ── build_all: filters ──────────────────────────────────────────────


def test_build_all_project_filter(tmp_path):
    """--project limits to one slug."""
    main = _make_main_db(tmp_path)
    claude_dir = tmp_path
    _, rel_a = _create_jsonl(claude_dir, "C--code-a", SID_A)
    _, rel_b = _create_jsonl(claude_dir, "C--code-b", SID_B)
    _insert_session(main, session_id=SID_A, project="C--code-a", jsonl_rel_path=rel_a)
    _insert_session(main, session_id=SID_B, project="C--code-b", jsonl_rel_path=rel_b)

    stats = build_all(main, claude_dir, project="C--code-a", quiet=True)
    assert stats.sessions_indexed == 1
    assert stats.projects_touched == 1


def test_build_all_session_filter(tmp_path):
    """session_id arg limits to one full UUID."""
    main = _make_main_db(tmp_path)
    claude_dir = tmp_path
    _, rel_a = _create_jsonl(claude_dir, "C--code-x", SID_A)
    _, rel_b = _create_jsonl(claude_dir, "C--code-x", SID_B)
    _insert_session(main, session_id=SID_A, project="C--code-x", jsonl_rel_path=rel_a)
    _insert_session(main, session_id=SID_B, project="C--code-x", jsonl_rel_path=rel_b)

    stats = build_all(main, claude_dir, session_id=SID_A, quiet=True)
    assert stats.sessions_indexed == 1


# ── Multi-project: separate DBs ──────────────────────────────────────


def test_build_all_creates_separate_db_per_project(tmp_path):
    main = _make_main_db(tmp_path)
    claude_dir = tmp_path
    _, rel_a = _create_jsonl(claude_dir, "C--code-a", SID_A)
    _, rel_b = _create_jsonl(claude_dir, "C--code-b", SID_B)
    _insert_session(main, session_id=SID_A, project="C--code-a", jsonl_rel_path=rel_a)
    _insert_session(main, session_id=SID_B, project="C--code-b", jsonl_rel_path=rel_b)

    build_all(main, claude_dir, quiet=True)
    dbs = sorted((claude_dir / "csb-fts").glob("*.db"))
    # Two separate per-project DBs
    assert len(dbs) == 2
    a_dbs = [d for d in dbs if d.name.startswith("C--code-a__")]
    b_dbs = [d for d in dbs if d.name.startswith("C--code-b__")]
    assert len(a_dbs) == 1 and len(b_dbs) == 1


# ── Excludes deleted sessions ────────────────────────────────────────


def test_build_all_skips_deleted_sessions(tmp_path):
    main = _make_main_db(tmp_path)
    claude_dir = tmp_path
    _, rel = _create_jsonl(claude_dir, "C--code-x", SID_A)
    _insert_session(main, session_id=SID_A, project="C--code-x",
                    jsonl_rel_path=rel, deleted=True)

    stats = build_all(main, claude_dir, quiet=True)
    assert stats.sessions_considered == 0
    assert stats.sessions_indexed == 0


def test_build_all_skips_missing_jsonl(tmp_path):
    """Session row points to a JSONL that doesn't exist on disk."""
    main = _make_main_db(tmp_path)
    claude_dir = tmp_path
    # No actual JSONL file created
    _insert_session(main, session_id=SID_A, project="C--code-x",
                    jsonl_rel_path="projects/C--code-x/missing.jsonl")

    stats = build_all(main, claude_dir, quiet=True)
    assert stats.sessions_indexed == 0


# ── Main-DB hint columns updated ─────────────────────────────────────


def test_build_all_updates_session_sources_hint_when_present(tmp_path):
    """If session_sources has a jsonl row, fts5_indexed_at gets updated."""
    main = _make_main_db(tmp_path)
    claude_dir = tmp_path
    _, rel = _create_jsonl(claude_dir, "C--code-x", SID_A)
    _insert_session(main, session_id=SID_A, project="C--code-x", jsonl_rel_path=rel)
    # Add a session_sources row for the JSONL (what cmd_backup does in production)
    abs_jsonl = str(claude_dir / rel)
    main.execute(
        "INSERT INTO session_sources "
        "(session_id, project, source_type, source_path) "
        "VALUES (?, ?, ?, ?)",
        (SID_A, "C--code-x", "jsonl", abs_jsonl),
    )
    main.commit()

    build_all(main, claude_dir, quiet=True)

    row = main.execute(
        "SELECT fts5_indexed_at, content_hash FROM session_sources "
        "WHERE session_id = ? AND source_type = 'jsonl'",
        (SID_A,),
    ).fetchone()
    assert row["fts5_indexed_at"] is not None
    assert row["content_hash"]  # non-empty

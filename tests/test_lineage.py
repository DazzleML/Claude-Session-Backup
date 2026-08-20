"""Tests for fork lineage: extraction, forest building, and tree rendering (#31).

Covers the v0.7.0 chain:
  metadata.py   -- forkedFrom extraction off the compact_boundary event
  migrations.py -- the v6 lineage columns
  lineage.py    -- forest construction, tiering, scoping, cycle guard
  lineage_render.py -- connector output in both charsets, info levels, JSON
"""

import json
import sqlite3

import pytest

from claude_session_backup.index import init_schema
from claude_session_backup.lineage import build_forest
from claude_session_backup.lineage_render import (
    forest_summary,
    render_forest,
    supports_unicode,
    to_json,
)
from claude_session_backup.metadata import extract_metadata_from_bytes


# ── fixtures / helpers ────────────────────────────────────────────────

@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn, quiet=True)
    yield conn
    conn.close()


def add(conn, sid, *, parent=None, name=None, deleted=None, forked_at=None,
        folder=r"C:\code\proj", last_active="2026-06-01T10:00:00Z",
        messages=10, size=1000):
    conn.execute(
        "INSERT INTO sessions (session_id, project, session_name, start_folder, "
        "started_at, last_active_at, message_count, jsonl_size, jsonl_path, "
        "deleted_at, parent_session_id, is_fork, forked_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "proj", name or f"NAME__{sid}", folder, "2026-05-01T10:00:00Z",
         last_active, messages, size, f"projects/proj/{sid}.jsonl", deleted,
         parent, 1 if parent else 0, forked_at),
    )
    conn.execute(
        "INSERT INTO folder_usage (session_id, folder_path, usage_count, is_start_folder) "
        "VALUES (?,?,?,?)", (sid, folder, 5, 1))
    conn.commit()


def ids_at_depth(forest):
    return [(n.session_id, n.depth) for r in forest.roots for n in r.walk()]


def jsonl(*events) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode("utf-8")


def boundary(parent="p-uuid", msg="m-uuid", ts="2026-06-01T12:00:00Z"):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": ts,
            "forkedFrom": {"sessionId": parent, "messageUuid": msg},
            "cwd": r"C:\code\proj"}


# ── extraction (metadata.py) ──────────────────────────────────────────

def test_extraction_reads_forkedfrom_from_boundary():
    meta = extract_metadata_from_bytes(jsonl(boundary()), "child", "proj")
    assert meta.is_fork is True
    assert meta.parent_session_id == "p-uuid"
    assert meta.parent_message_uuid == "m-uuid"
    assert meta.forked_at == "2026-06-01T12:00:00Z"


def test_extraction_ignores_boundary_without_forkedfrom():
    """In-place compaction writes a boundary row with NO forkedFrom -- that
    is a compaction, not a fork, and must not create a parent edge."""
    blob = jsonl({"type": "system", "subtype": "compact_boundary",
                  "timestamp": "2026-06-01T12:00:00Z",
                  "compactMetadata": {"trigger": "auto"}})
    meta = extract_metadata_from_bytes(blob, "s", "proj")
    assert meta.is_fork is False
    assert meta.parent_session_id is None


def test_extraction_reads_forkedfrom_from_any_carrier():
    """#97: Claude Code has carried the identical forkedFrom payload on at
    least four event shapes across CLI versions. The payload is the
    contract; the carrier is a moving label. Gating on compact_boundary
    dropped 51% of declared forks on a real machine.

    (Replaces test_extraction_ignores_forkedfrom_outside_boundary, whose
    premise its own fixture disproved: a TOP-LEVEL forkedFrom key on a
    user event is a real fork marker written by CLI 2.1.6x-8x -- merely
    "mentioning" forkedFrom happens inside message content, which the
    extractor never reads.)"""
    carriers = [
        {"type": "attachment", "timestamp": "2026-06-01T12:00:00Z"},
        {"type": "user", "timestamp": "2026-06-01T12:00:00Z",
         "message": {"content": "hi"}},
        {"type": "system", "subtype": "local_command",
         "timestamp": "2026-06-01T12:00:00Z"},
    ]
    for ev in carriers:
        ev["forkedFrom"] = {"sessionId": "p-uuid", "messageUuid": "m-uuid"}
        meta = extract_metadata_from_bytes(jsonl(ev), "child", "proj")
        assert meta.is_fork is True, f"carrier missed: {ev['type']}"
        assert meta.parent_session_id == "p-uuid"
        assert meta.forked_at == "2026-06-01T12:00:00Z"


def test_extraction_takes_the_first_forkedfrom_only():
    """First match wins: the first pointer is the fork that minted THIS
    file; later ones (however they arise) must not reassign the parent."""
    first = {"type": "attachment", "timestamp": "2026-06-01T12:00:00Z",
             "forkedFrom": {"sessionId": "real-parent",
                            "messageUuid": "m1"}}
    later = boundary(parent="impostor", ts="2026-06-02T12:00:00Z")
    meta = extract_metadata_from_bytes(jsonl(first, later), "child", "proj")
    assert meta.parent_session_id == "real-parent"
    assert meta.forked_at == "2026-06-01T12:00:00Z"


def test_extraction_ignores_forkedfrom_without_session_id():
    """The payload check still discriminates: a forkedFrom that names no
    session (malformed, or a non-dict) is not a parent edge."""
    blob = jsonl({"type": "user", "timestamp": "2026-06-01T12:00:00Z",
                  "forkedFrom": {"messageUuid": "m-only"}},
                 {"type": "user", "timestamp": "2026-06-01T12:00:01Z",
                  "forkedFrom": "not-a-dict"})
    meta = extract_metadata_from_bytes(blob, "s", "proj")
    assert meta.is_fork is False
    assert meta.parent_session_id is None


def test_extraction_finds_boundary_beyond_line_one():
    """Position-independent: 4 of 32 real forks on the dev vault carry the
    boundary row AFTER leading custom-title / agent-name rows, so reading
    only line 1 would miss them."""
    blob = jsonl(
        {"type": "custom-title", "customTitle": "RENAMED"},
        {"type": "agent-name", "name": "x"},
        boundary(parent="deep-parent"),
    )
    meta = extract_metadata_from_bytes(blob, "child", "proj")
    assert meta.is_fork is True
    assert meta.parent_session_id == "deep-parent"


def test_extraction_takes_first_boundary_only():
    """A later in-place compaction must not overwrite the minting fork."""
    blob = jsonl(
        boundary(parent="real-parent", ts="2026-06-01T12:00:00Z"),
        {"type": "user", "timestamp": "2026-06-01T13:00:00Z"},
        boundary(parent="later-parent", ts="2026-06-02T12:00:00Z"),
    )
    meta = extract_metadata_from_bytes(blob, "child", "proj")
    assert meta.parent_session_id == "real-parent"
    assert meta.forked_at == "2026-06-01T12:00:00Z"


# ── migration (schema v6) ─────────────────────────────────────────────

def test_migration_v6_adds_lineage_columns(db):
    cols = {r["name"] for r in db.execute("PRAGMA table_info(sessions)")}
    assert {"parent_session_id", "parent_message_uuid", "is_fork",
            "forked_at"} <= cols


def test_opening_a_v5_database_upgrades_without_error(tmp_path):
    """Regression: an EXISTING pre-v0.7.0 index must open cleanly.

    ``init_schema`` runs the baseline DDL before migrations, and that
    baseline also executes against old databases -- where `CREATE TABLE IF
    NOT EXISTS` is a no-op, so migration-added columns are absent. An index
    over ``parent_session_id`` in the baseline therefore raised
    `sqlite3.OperationalError: no such column: parent_session_id` on every
    csb command for an upgrading user, before the migration could run.
    """
    db = tmp_path / "v5.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    # A v5-era sessions table: no lineage columns.
    conn.executescript(
        """
        CREATE TABLE schema_info (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, project TEXT NOT NULL,
            session_name TEXT, start_folder TEXT, started_at TEXT,
            last_active_at TEXT, last_user_at TEXT,
            message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
            claude_version TEXT, jsonl_path TEXT, jsonl_size INTEGER DEFAULT 0,
            jsonl_mtime REAL DEFAULT 0, last_scanned_at TEXT, deleted_at TEXT,
            last_git_commit TEXT, metadata_validated_at TEXT
        );
        CREATE TABLE folder_usage (
            session_id TEXT NOT NULL, folder_path TEXT NOT NULL,
            usage_count INTEGER DEFAULT 1, is_start_folder INTEGER DEFAULT 0,
            PRIMARY KEY (session_id, folder_path)
        );
        INSERT INTO schema_info VALUES ('schema_version', '5');
        INSERT INTO sessions (session_id, project) VALUES ('old-sess', 'proj');
        """
    )
    conn.commit()

    init_schema(conn, quiet=True)   # must not raise

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert {"parent_session_id", "forked_at", "is_fork"} <= cols
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_sessions_parent" in idx
    # The pre-existing row survives and reads as a lineage-less root
    # (the builder keeps singletons; `csb tree` collapses them for display).
    forest = build_forest(conn)
    assert [r.session_id for r in forest.roots] == ["old-sess"]
    assert forest.roots[0].children == []
    conn.close()


def test_fresh_database_gets_migration_owned_indexes(db):
    """Fresh DBs skip migrations, so the index must still be created."""
    idx = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_sessions_parent" in idx


def test_migration_v6_is_idempotent_on_existing_db():
    """A v5-era DB upgrades cleanly, and re-running is a no-op."""
    from claude_session_backup.migrations import _v6_add_fork_lineage
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn, quiet=True)
    _v6_add_fork_lineage(conn)   # already applied by init_schema
    _v6_add_fork_lineage(conn)   # and again
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "forked_at" in cols
    conn.close()


# ── forest construction ───────────────────────────────────────────────

def test_empty_forest(db):
    forest = build_forest(db)
    assert forest.roots == []
    assert forest.total_nodes == 0


def test_single_root_single_child(db):
    add(db, "root")
    add(db, "kid", parent="root", forked_at="2026-06-02T00:00:00Z")
    forest = build_forest(db)
    assert ids_at_depth(forest) == [("root", 0), ("kid", 1)]


def test_deep_chain_depths(db):
    add(db, "a")
    for i, (child, parent) in enumerate([("b", "a"), ("c", "b"), ("d", "c"), ("e", "d")]):
        add(db, child, parent=parent, forked_at=f"2026-06-0{i + 1}T00:00:00Z")
    forest = build_forest(db)
    assert ids_at_depth(forest) == [("a", 0), ("b", 1), ("c", 2), ("d", 3), ("e", 4)]


def test_multi_root_forest(db):
    add(db, "r1"); add(db, "k1", parent="r1", forked_at="2026-06-02T00:00:00Z")
    add(db, "r2"); add(db, "k2", parent="r2", forked_at="2026-06-03T00:00:00Z")
    forest = build_forest(db)
    assert len(forest.roots) == 2
    assert forest.total_nodes == 4


def test_children_sort_by_forked_at_ascending(db):
    add(db, "root")
    add(db, "late", parent="root", forked_at="2026-06-09T00:00:00Z")
    add(db, "early", parent="root", forked_at="2026-06-01T00:00:00Z")
    add(db, "mid", parent="root", forked_at="2026-06-05T00:00:00Z")
    forest = build_forest(db)
    assert [c.session_id for c in forest.roots[0].children] == ["early", "mid", "late"]


def test_cycle_guard_breaks_and_reports(db):
    add(db, "a", parent="b", forked_at="2026-06-01T00:00:00Z")
    add(db, "b", parent="a", forked_at="2026-06-02T00:00:00Z")
    forest = build_forest(db)          # must terminate, not hang
    assert forest.cycles_broken
    assert forest.total_nodes >= 1


def test_phantom_parent_groups_siblings(db):
    """Two children pointing at an unindexed parent still render as one
    family under a placeholder rather than as unrelated roots."""
    add(db, "c1", parent="ghost", forked_at="2026-06-01T00:00:00Z")
    add(db, "c2", parent="ghost", forked_at="2026-06-02T00:00:00Z")
    forest = build_forest(db)
    assert len(forest.roots) == 1
    root = forest.roots[0]
    assert root.phantom is True and root.session_id == "ghost"
    assert {c.session_id for c in root.children} == {"c1", "c2"}
    assert forest.phantom_parents == ["ghost"]


# ── the three-tier rule (deleted scope) ───────────────────────────────

def test_deleted_ancestor_renders_as_dim_connector(db):
    """Parity default (active-only) still shows a PURGED ancestor, because
    hiding it would decapitate an in-scope chain."""
    add(db, "root")
    add(db, "gone", parent="root", deleted="2026-07-01T00:00:00Z",
        forked_at="2026-06-02T00:00:00Z")
    add(db, "alive", parent="gone", forked_at="2026-06-03T00:00:00Z")
    forest = build_forest(db)
    tiers = {n.session_id: n.in_population for r in forest.roots for n in r.walk()}
    assert tiers == {"root": True, "gone": False, "alive": True}


def test_deleted_leaf_hidden_by_default_shown_with_all(db):
    """A purged LEAF is not a structural connector -- it stays hidden until
    the user widens the scope."""
    add(db, "root")
    add(db, "deadleaf", parent="root", deleted="2026-07-01T00:00:00Z",
        forked_at="2026-06-02T00:00:00Z")
    default = {n.session_id for r in build_forest(db).roots for n in r.walk()}
    assert default == {"root"}
    widened = {n.session_id
               for r in build_forest(db, deleted_filter="all").roots
               for n in r.walk()}
    assert widened == {"root", "deadleaf"}


def test_deleted_only_flips_the_population(db):
    add(db, "root")
    add(db, "gone", parent="root", deleted="2026-07-01T00:00:00Z",
        forked_at="2026-06-02T00:00:00Z")
    forest = build_forest(db, deleted_filter="deleted")
    tiers = {n.session_id: n.in_population for r in forest.roots for n in r.walk()}
    assert tiers["gone"] is True
    assert tiers["root"] is False   # active ancestor -> dim connector


# ── filtering / selection ─────────────────────────────────────────────

def test_filter_renders_whole_component_with_match_marked(db):
    add(db, "root")
    add(db, "target", parent="root", name="FINDME", forked_at="2026-06-02T00:00:00Z")
    add(db, "cousin", parent="root", forked_at="2026-06-03T00:00:00Z")
    forest = build_forest(db, filter_term="FINDME")
    rendered = {n.session_id: n.matched for r in forest.roots for n in r.walk()}
    assert rendered == {"root": False, "target": True, "cousin": False}


def test_lineage_view_drops_cousins(db):
    add(db, "root")
    add(db, "target", parent="root", name="FINDME", forked_at="2026-06-02T00:00:00Z")
    add(db, "cousin", parent="root", forked_at="2026-06-03T00:00:00Z")
    forest = build_forest(db, filter_term="FINDME", component_view=False)
    assert {n.session_id for r in forest.roots for n in r.walk()} == {"root", "target"}


def test_filter_trailing_star_is_prefix_anchored(db):
    add(db, "a", name="ALPHA__one")
    add(db, "b", name="not-ALPHA__two")
    assert {n.session_id for r in build_forest(db, filter_term="ALPHA*").roots
            for n in r.walk() if n.matched} == {"a"}
    # plain substring still matches both
    assert {n.session_id for r in build_forest(db, filter_term="ALPHA").roots
            for n in r.walk() if n.matched} == {"a", "b"}


def test_filter_regex_mode(db):
    add(db, "a", name="v1-release")
    add(db, "b", name="v2-release")
    add(db, "c", name="draft")
    got = {n.session_id for r in build_forest(db, filter_term=r"^v\d-", regex=True).roots
           for n in r.walk() if n.matched}
    assert got == {"a", "b"}


def test_filter_matches_uuid_and_folder_vocabulary(db):
    add(db, "abc123", folder=r"C:\work\special")
    assert build_forest(db, filter_term="abc12").roots
    assert build_forest(db, filter_term="special").roots


def test_invalid_regex_raises(db):
    with pytest.raises(Exception):
        build_forest(db, filter_term="([unclosed", regex=True)


def test_scope_ids_restrict_selection_but_keep_ancestors(db):
    add(db, "root", folder=r"C:\other")
    add(db, "kid", parent="root", folder=r"C:\target", forked_at="2026-06-02T00:00:00Z")
    forest = build_forest(db, scope_ids={"kid"})
    got = {n.session_id for r in forest.roots for n in r.walk()}
    assert got == {"root", "kid"}   # ancestor pulled in for lineage


def test_orphans_only_keeps_childless_roots(db):
    add(db, "lonely")
    add(db, "root"); add(db, "kid", parent="root", forked_at="2026-06-02T00:00:00Z")
    forest = build_forest(db, orphans_only=True)
    assert [r.session_id for r in forest.roots] == ["lonely"]


def test_root_restricts_to_one_family(db):
    add(db, "r1"); add(db, "k1", parent="r1", forked_at="2026-06-02T00:00:00Z")
    add(db, "r2"); add(db, "k2", parent="r2", forked_at="2026-06-03T00:00:00Z")
    forest = build_forest(db, root="r2")
    assert {n.session_id for r in forest.roots for n in r.walk()} == {"r2", "k2"}


def test_truncation_elides_and_counts(db):
    add(db, "root")
    for i in range(10):
        add(db, f"k{i}", parent="root", forked_at=f"2026-06-{i + 1:02d}T00:00:00Z")
    forest = build_forest(db, max_nodes_per_root=4)
    assert forest.elided_nodes == 7          # 11 nodes, 4 kept
    assert forest.roots[0].elided_children == 7


# ── rendering ─────────────────────────────────────────────────────────

def test_render_unicode_connectors(db):
    add(db, "root")
    add(db, "a", parent="root", forked_at="2026-06-01T00:00:00Z")
    add(db, "b", parent="root", forked_at="2026-06-02T00:00:00Z")
    text = "\n".join(t for t, _ in render_forest(build_forest(db), charset="unicode"))
    assert "├── " in text and "└── " in text


def test_render_ascii_has_no_box_drawing(db):
    add(db, "root")
    add(db, "a", parent="root", forked_at="2026-06-01T00:00:00Z")
    add(db, "b", parent="root", forked_at="2026-06-02T00:00:00Z")
    text = "\n".join(t for t, _ in render_forest(build_forest(db), charset="ascii"))
    assert "+-- " in text and "\\-- " in text
    assert not any(ch in text for ch in "├└│")
    text.encode("cp437")  # must survive a legacy Windows console


def test_render_info_levels_are_cumulative(db):
    add(db, "root")
    f = build_forest(db)
    l0 = "\n".join(t for t, _ in render_forest(f, level=0, charset="ascii"))
    l1 = "\n".join(t for t, _ in render_forest(f, level=1, charset="ascii"))
    l2 = "\n".join(t for t, _ in render_forest(f, level=2, charset="ascii"))
    assert "started:" not in l0
    assert "started:" in l1 and "start at:" not in l1
    assert "start at:" in l2 and "messages" in l2


def test_named_session_hides_uuid_by_default(db):
    """Width economy: a named session shows just its name. 36 characters of
    UUID per row is noise in a view already spending width on indentation."""
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    add(db, uuid, name="PLAIN-NAME")
    text = "\n".join(t for t, _ in render_forest(build_forest(db), charset="ascii"))
    assert "PLAIN-NAME" in text
    assert uuid not in text


def test_unnamed_session_always_shows_uuid(db):
    """...but an unnamed session must still identify itself."""
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    add(db, uuid, name=None)
    db.execute("UPDATE sessions SET session_name = NULL WHERE session_id = ?", (uuid,))
    db.commit()
    text = "\n".join(t for t, _ in render_forest(build_forest(db), charset="ascii"))
    assert uuid in text


def test_uuid_and_shortid_flags_restore_identity(db):
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    add(db, uuid, name="PLAIN-NAME")
    f = build_forest(db)
    full = "\n".join(t for t, _ in render_forest(f, show_uuid=True, charset="ascii"))
    short = "\n".join(t for t, _ in render_forest(f, shortid=True, charset="ascii"))
    assert uuid in full
    assert uuid not in short
    assert "aaaaaaaa-...-eeeeeeee" in short


def test_headline_segments_are_styled_per_part(db):
    """Name and metadata get different styles (parity with list/scan), not
    one flat color for the whole row."""
    add(db, "s1", name="MY-SESSION", forked_at=None)
    _plain, segments = render_forest(build_forest(db), charset="ascii")[0]
    styled = {text.strip(): style for text, style in segments}
    assert styled.get("MY-SESSION") == "bold cyan"
    assert any(s == "dim" for t, s in segments if "ago" in t)


def test_render_honors_explicit_encoding_over_stream(db):
    """The console encoding is captured BEFORE stdout is reconfigured, so
    the caller passes it in; forcing stdout to UTF-8 first must not be able
    to defeat the ASCII fallback."""
    add(db, "root")
    add(db, "kid", parent="root", forked_at="2026-06-02T00:00:00Z")
    f = build_forest(db)
    # stream claims utf-8, but the real console was cp1252 -> ASCII wins
    utf8_stream = type("S", (), {"encoding": "utf-8"})()
    text = "\n".join(t for t, _ in render_forest(
        f, encoding="cp1252", stream=utf8_stream))
    assert "\\-- " in text and "└" not in text
    # cp437 natively carries the glyphs -> keep the pretty output
    text437 = "\n".join(t for t, _ in render_forest(
        f, encoding="cp437", stream=utf8_stream))
    assert "└── " in text437


def test_star_filter_matches_everything(db):
    """`csb tree * .` -- shells that do not glob pass `*` through literally;
    treating it as a literal asterisk would silently match nothing."""
    add(db, "root", name="ALPHA")
    add(db, "kid", parent="root", name="BETA", forked_at="2026-06-02T00:00:00Z")
    got = {n.session_id for r in build_forest(db, filter_term="*").roots
           for n in r.walk()}
    assert got == {"root", "kid"}


def test_ff_level_uses_real_folder_rows(db):
    """Regression: nodes must carry {folder_path, usage_count} rows, or the
    -ff level silently renders a wrong 'start at' and no other folders."""
    add(db, "s1", folder=r"C:\work\main")
    db.execute(
        "INSERT INTO folder_usage (session_id, folder_path, usage_count, "
        "is_start_folder) VALUES (?,?,?,?)", ("s1", r"C:\work\other", 3, 0))
    db.commit()
    node = build_forest(db).roots[0]
    folders = node.session.get("folders")
    assert folders and isinstance(folders[0], dict)
    assert {"folder_path", "usage_count", "is_start_folder"} <= set(folders[0])
    text = "\n".join(t for t, _ in render_forest(
        build_forest(db), level=2, charset="ascii"))
    assert r"C:\work\other (3x)" in text


def test_render_marks_purged_and_matched(db):
    """The match is an ACTIVE session whose parent was purged: the match
    gets the '*' marker, the purged ancestor is pulled in as a connector
    and carries '[purged]'."""
    add(db, "root")
    add(db, "gone", parent="root", deleted="2026-07-01T00:00:00Z",
        forked_at="2026-06-02T00:00:00Z")
    add(db, "alive", parent="gone", name="FINDME",
        forked_at="2026-06-03T00:00:00Z")
    text = "\n".join(t for t, _ in render_forest(
        build_forest(db, filter_term="FINDME"), charset="ascii"))
    assert "[purged]" in text
    assert "*" in text


def test_filter_matching_only_a_deleted_session_needs_widened_scope(db):
    """Parity with `csb list`: a filter that only hits a purged session
    finds nothing until the deleted scope is widened."""
    add(db, "root")
    add(db, "gone", parent="root", name="FINDME",
        deleted="2026-07-01T00:00:00Z", forked_at="2026-06-02T00:00:00Z")
    assert build_forest(db, filter_term="FINDME").roots == []
    widened = build_forest(db, filter_term="FINDME", deleted_filter="all")
    assert {n.session_id for r in widened.roots for n in r.walk()} == {"root", "gone"}


def test_render_phantom_marker(db):
    add(db, "c1", parent="ghost", forked_at="2026-06-01T00:00:00Z")
    text = "\n".join(t for t, _ in render_forest(build_forest(db), charset="ascii"))
    assert "[not indexed]" in text


def test_render_truncation_hint_names_root_flag(db):
    add(db, "root")
    for i in range(10):
        add(db, f"k{i}", parent="root", forked_at=f"2026-06-{i + 1:02d}T00:00:00Z")
    text = "\n".join(t for t, _ in render_forest(
        build_forest(db, max_nodes_per_root=4), charset="ascii"))
    assert "more -- csb tree --root root" in text


def test_json_shape_is_nested(db):
    add(db, "root")
    add(db, "kid", parent="root", forked_at="2026-06-02T00:00:00Z")
    payload = to_json(build_forest(db))
    assert len(payload) == 1
    assert payload[0]["session_id"] == "root"
    assert payload[0]["children"][0]["session_id"] == "kid"
    json.dumps(payload, default=str)   # must be serializable


def test_summary_counts(db):
    add(db, "root")
    add(db, "kid", parent="root", forked_at="2026-06-02T00:00:00Z")
    assert forest_summary(build_forest(db)) == "1 tree | 2 sessions"


def test_empty_state_hints_reindex_when_index_predates_lineage(db, capsys):
    """After the v6 migration an existing index has columns but no pointers.
    The empty state must name the one-time re-index, not suggest --orphans.
    """
    from types import SimpleNamespace

    from claude_session_backup import commands as cmd

    add(db, "s1"); add(db, "s2")

    args = SimpleNamespace(
        filter=None, path=None, regex=False, case_sensitive=False,
        directories_below=None, directory_only=None, root=None, orphans=False,
        lineage=False, n=None, max_nodes=None, sort="last-used", deleted=None,
        all=False, full_info=0, shortid=False, json=False, ascii=True,
        top=None, all_folders=False, quiet=True, claude_dir=None, db=None,
    )
    monkey = {"index_path": ":memory:", "claude_dir": "/tmp"}
    orig_config, orig_open, orig_init, orig_cleanup = (
        cmd._get_config, cmd.open_db, cmd.init_schema, cmd.read_cleanup_period)
    cmd._get_config = lambda a: monkey
    cmd.open_db = lambda p: db
    cmd.init_schema = lambda c, quiet=False: None
    cmd.read_cleanup_period = lambda d: 30
    try:
        rc = cmd.cmd_tree(args)
    finally:
        (cmd._get_config, cmd.open_db, cmd.init_schema,
         cmd.read_cleanup_period) = (orig_config, orig_open, orig_init, orig_cleanup)

    captured = capsys.readouterr()
    assert rc == 0
    assert "none of them forked" in captured.out
    assert "csb backup" in captured.err
    assert "--orphans" not in captured.err


def test_show_lineage_row_colors_purged_marker_red(db):
    """`csb show`'s lineage row is segmented, so the purged marker is RED
    (matching the view's `DELETED at:` line) instead of inheriting the
    name's cyan across the whole row."""
    from claude_session_backup import commands as cmd

    add(db, "parent", name="OLD-ONE", deleted="2026-07-01T00:00:00Z")
    add(db, "kid", parent="parent", name="NEW-ONE",
        forked_at="2026-06-02T00:00:00Z")
    session = dict(db.execute(
        "SELECT * FROM sessions WHERE session_id = 'kid'").fetchone())
    session["_lineage"] = cmd._fetch_lineage(db, "kid", session)

    rows = cmd._lineage_lines(session)
    assert rows and rows[0][0] == "Forked from:"
    styles = {text.strip(): style for text, style in rows[0][1]}
    assert styles["[purged]"] == "red"
    assert styles["OLD-ONE"] == "cyan"
    assert any(s == "dim" for t, s in rows[0][1] if "forked" in t)


def test_supports_unicode_probes_the_actual_encoding():
    """Detection probes the stream's real codepage rather than guessing
    from platform. cp1252 (PowerShell's default) cannot encode the box
    characters; cp437 (the classic DOS console) natively can -- they are
    original IBM PC glyphs -- so it correctly keeps the pretty output.
    """
    def stream(enc):
        return type("S", (), {"encoding": enc})()

    assert supports_unicode(stream("cp1252")) is False
    assert supports_unicode(stream("cp437")) is True
    assert supports_unicode(stream("utf-8")) is True
    assert supports_unicode(stream(None)) is False

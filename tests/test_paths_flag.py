"""--paths wiring: flag > config > default, loud unknowns, live view.

The rung machinery itself is covered in test_pathlevels.py; these tests
pin the CLI/config plumbing and the cmd_show integration seam.
"""

import os

import pytest

from claude_session_backup.commands import _scratch_escape_config


# ── the flag ─────────────────────────────────────────────────────────

def test_paths_flag_parses_every_selectable_level():
    from claude_session_backup.cli import build_parser
    from claude_session_backup.pathlevels import SELECTABLE_LEVELS

    for level in SELECTABLE_LEVELS:
        args = build_parser().parse_args(["show", "abc", "--paths", level])
        assert args.paths == level


def test_paths_flag_rejects_unknown_level_loudly(capsys):
    """AC-6: the error must NAME the valid levels (argparse choices)."""
    from claude_session_backup.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(["show", "abc", "--paths", "bogus"])
    err = capsys.readouterr().err
    assert "invalid choice" in err
    assert "suspected" in err and "cd" in err


def test_paths_flag_defaults_to_none_so_config_can_speak():
    from claude_session_backup.cli import build_parser
    assert build_parser().parse_args(["show", "abc"]).paths is None


# ── config precedence (AC-6) ─────────────────────────────────────────

def _show_with(monkeypatch, tmp_path, config_extra, argv_paths=None,
               folders=None):
    """Run cmd_show against a real scratch DB; capture the level applied
    by spying on apply_level."""
    import claude_session_backup.commands as commands_module
    import claude_session_backup.pathlevels as pl
    from claude_session_backup.commands import cmd_show
    from claude_session_backup.index import open_db, init_schema, upsert_session
    from claude_session_backup.metadata import SessionMetadata
    from types import SimpleNamespace

    db = str(tmp_path / "t.db")
    conn = open_db(db)
    init_schema(conn)
    meta = SessionMetadata(
        session_id="aaaa1111", session_name="s", project="p",
        start_folder=os.getcwd(), started_at="2026-08-01T00:00:00Z",
        last_active_at="2026-08-01T00:00:00Z", message_count=1,
        tool_call_count=1,
        folder_usage=folders or {os.getcwd(): 3},
    )
    upsert_session(conn, meta, "p.jsonl", 10, 0.0, "t1")
    conn.close()

    cfg = {"claude_dir": str(tmp_path / "claude"), "index_path": db}
    cfg.update(config_extra)
    monkeypatch.setattr(commands_module, "_get_config",
                        lambda args: cfg)

    seen = {}
    real = pl.apply_level

    def spy(rows, chosen):
        seen["level"] = chosen
        return real(rows, chosen)

    monkeypatch.setattr(pl, "apply_level", spy)

    args = SimpleNamespace(
        session_id="aaaa1111", paths=argv_paths, filters=None,
        show_all=False, quiet=True, claude_dir=None, db=None,
    )
    rc = cmd_show(args)
    return rc, seen.get("level")


def test_flag_beats_config(monkeypatch, tmp_path, capsys):
    rc, level = _show_with(monkeypatch, tmp_path,
                           {"paths_level": "cd"}, argv_paths="verified")
    assert rc == 0 and level == "verified"


def test_config_beats_default(monkeypatch, tmp_path, capsys):
    rc, level = _show_with(monkeypatch, tmp_path, {"paths_level": "cd"})
    assert rc == 0 and level == "cd"


def test_default_is_suspected(monkeypatch, tmp_path, capsys):
    rc, level = _show_with(monkeypatch, tmp_path, {})
    assert rc == 0 and level == "suspected"


def test_invalid_config_level_warns_and_defaults(monkeypatch, tmp_path,
                                                 capsys):
    """Corrupt config must not brick `csb show` -- but it must SAY so,
    naming the key and the valid levels' home. Never silent."""
    rc, level = _show_with(monkeypatch, tmp_path,
                           {"paths_level": "bogus"})
    assert rc == 0 and level == "suspected"
    assert "paths_level" in capsys.readouterr().err


# ── the scratch-escape knobs (AC-8) ──────────────────────────────────

def test_scratch_escape_defaults():
    assert _scratch_escape_config({}) == (10, 10)
    assert _scratch_escape_config(None) == (10, 10)


def test_scratch_escape_reads_config():
    assert _scratch_escape_config(
        {"scratch_escape_min_work": 3, "scratch_escape_top_rank": 5}
    ) == (3, 5)


def test_scratch_escape_accepts_string_ints():
    """csb config stores strings; the knob must coerce."""
    assert _scratch_escape_config(
        {"scratch_escape_min_work": "3"}) == (3, 10)


def test_scratch_escape_warns_on_garbage(capsys):
    assert _scratch_escape_config(
        {"scratch_escape_min_work": "lots"}) == (10, 10)
    assert "scratch_escape_min_work" in capsys.readouterr().err


def test_scratch_escape_lossy_float_warns(capsys):
    """Tester pass 1, Finding 3: `csb config` stores 3.5 as a JSON float,
    and `int(3.5)` never raises -- so the value silently became 3, the
    exact silent-reinterpretation class this code hunts. Lossy coercion
    must warn and fall back."""
    assert _scratch_escape_config(
        {"scratch_escape_min_work": 3.5}) == (10, 10)
    assert "scratch_escape_min_work" in capsys.readouterr().err


def test_scratch_escape_integral_float_is_fine(capsys):
    """3.0 loses nothing -- coerce silently; only LOSSY coercion warns."""
    assert _scratch_escape_config(
        {"scratch_escape_min_work": 3.0}) == (3, 10)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("bad", [float("inf"), float("-inf")])
def test_scratch_escape_infinity_warns_never_crashes(capsys, bad):
    """Round-2 regression IN the round-1 fix: the lossy-check added an
    int(val) path a genuine stored Infinity float can reach, and
    OverflowError is not a (TypeError, ValueError) -- uncaught traceback
    from `csb config scratch_escape_min_work Infinity`. A config value
    must never crash the tool; that is the requirement this release is
    built around."""
    assert _scratch_escape_config({"scratch_escape_min_work": bad}) == (10, 10)
    assert "scratch_escape_min_work" in capsys.readouterr().err


def test_scratch_escape_bool_warns(capsys):
    """`csb config scratch_escape_min_work true` stored JSON true;
    int(True)=1 and float(True)==1.0 slip the lossy check, so a
    fat-fingered boolean silently became threshold 1. A bool is not a
    count -- warn and fall back."""
    assert _scratch_escape_config({"scratch_escape_min_work": True}) == (10, 10)
    assert "scratch_escape_min_work" in capsys.readouterr().err


def test_touched_tier_renders_the_fold_marker(capsys, monkeypatch):
    """Tester pass 1, Finding 2 -- the FOURTH two-render-paths miss: the
    (~) marker existed only in the worked-tier loops, so a zero-work fold
    displayed with no indication a merge happened, in BOTH renderers.
    Forces the plain path; the rich path shares the fixed row-builders."""
    import claude_session_backup.commands as commands_module

    monkeypatch.setattr(commands_module, "HAS_RICH", False, raising=False)
    session = {
        "session_id": "x" * 36, "session_name": "s", "project": "p",
        "start_folder": "C:\\a", "started_at": None, "last_active_at": None,
        "last_user_at": None, "message_count": 1, "tool_call_count": 1,
        "claude_version": None, "jsonl_path": "p.jsonl", "jsonl_size": 1,
        "deleted_at": None, "_lineage": {},
        "folders": [
            {"folder_path": "C:\\a", "usage_count": 2, "is_start_folder": 1,
             "path_exists": 1},
            {"folder_path": "C:\\a\\healed", "usage_count": 0,
             "is_start_folder": 0, "path_exists": 1, "_approx_folded": True},
        ],
    }
    commands_module._render_show(session)
    out = capsys.readouterr().out
    touched = out.split("Also touched")[1]
    assert "(~)" in touched, (
        "a folded zero-work row rendered with no fold marker: %r" % touched)


def test_low_tier_renders_the_fold_marker(capsys, monkeypatch):
    """Round 2's judgment call, decided as: fix it. A zero-work fold onto
    a scratch-classified ancestor routes to the LOW tier, whose --all
    listing never checked _approx_folded -- the FIFTH render site with
    the marker convention silently inconsistent. One convention, all
    three tiers."""
    import claude_session_backup.commands as commands_module

    monkeypatch.setattr(commands_module, "HAS_RICH", False, raising=False)
    session = {
        "session_id": "x" * 36, "session_name": "s", "project": "p",
        "start_folder": "C:\\a", "started_at": None, "last_active_at": None,
        "last_user_at": None, "message_count": 1, "tool_call_count": 1,
        "claude_version": None, "jsonl_path": "p.jsonl", "jsonl_size": 1,
        "deleted_at": None, "_lineage": {},
        "folders": [
            {"folder_path": "C:\\a", "usage_count": 2, "is_start_folder": 1,
             "path_exists": 1},
            {"folder_path": "C:\\a\\Temp\\zeroscratch", "usage_count": 0,
             "is_start_folder": 0, "path_exists": 1,
             "_approx_folded": True},
        ],
    }
    commands_module._render_show(session, show_all=True)
    out = capsys.readouterr().out
    low = out.split("Low value")[1]
    assert "(~)" in low, (
        "a folded row in the low tier rendered with no fold marker: %r" % low)


def test_paths_and_filter_compose_fold_first():
    """The composition contract: rung view -> tiering -> min-work filter.

    Load-bearing for the fold: two leaves at 2x and 4x folding into one
    ancestor MERGE to 6x and pass `--filter min-work=5`. Filter-first
    would hide both before they could combine. `min-work` asks how much
    work happened HERE -- and the rung view is what defines "here"."""
    import os as _os
    from claude_session_backup.commands import _apply_work_filter
    from claude_session_backup.pathlevels import apply_level

    base = _os.getcwd()
    rows = [
        {"folder_path": base + "\\gone-a", "usage_count": 2,
         "is_start_folder": 0, "path_exists": 0, "provenance": "extracted"},
        {"folder_path": base + "\\gone-b", "usage_count": 4,
         "is_start_folder": 0, "path_exists": 0, "provenance": "extracted"},
    ]
    viewed, _ = apply_level(rows, "approximated")
    assert len(viewed) == 1 and viewed[0]["usage_count"] == 6

    kept, thin = _apply_work_filter(viewed, 5)
    assert [r["usage_count"] for r in kept] == [6], (
        "fold-then-filter must let merged work pass the threshold")
    assert thin == []


def test_tier_folders_honors_custom_thresholds():
    """The knobs actually move the F1 boundary."""
    from claude_session_backup.commands import _tier_folders

    scratch = "C:\\Users\\X\\AppData\\Local\\Temp\\claude\\p\\sid\\scratchpad"
    rows = [
        {"folder_path": os.getcwd(), "usage_count": 50,
         "is_start_folder": 1, "path_exists": 1},
        {"folder_path": scratch, "usage_count": 4,
         "is_start_folder": 0, "path_exists": 1},
    ]
    # default arms: rank 2 <= 10 -> escapes
    worked, _, _ = _tier_folders([dict(r) for r in rows])
    assert scratch in [f["folder_path"] for f in worked]
    # tightened arms: min_work 5 (4 fails), top_rank 1 (rank 2 fails)
    worked, _, low = _tier_folders([dict(r) for r in rows], 5, 1)
    assert scratch not in [f["folder_path"] for f in worked]
    assert scratch in [f["folder_path"] for f in low]

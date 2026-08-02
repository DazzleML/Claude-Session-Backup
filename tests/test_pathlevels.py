"""Unit tests for the path-exposure rung views (#56/H2).

The rungs are DISPLAY/QUERY views over one maximally-granular index --
most are filters, `approximated` is a fold. The invariant's measurable
shadow is conservation: work-credit sums survive every view.
"""

import pytest

from claude_session_backup import pathlevels as pl


def _row(path, count=1, exists=None, prov="extracted"):
    return {"folder_path": path, "usage_count": count,
            "is_start_folder": 0, "path_exists": exists,
            "provenance": prov}


# ── level derivation ─────────────────────────────────────────────────

def test_level_of_cd_wins_over_everything():
    assert pl.level_of(_row("C:\\x", exists=1, prov="cd")) == "cd"
    assert pl.level_of(_row("C:\\x", exists=0, prov="cd")) == "cd"


def test_level_of_verified_needs_the_probe():
    assert pl.level_of(_row("C:\\x", exists=1)) == "verified"


@pytest.mark.parametrize("exists", [0, None])
def test_level_of_missing_and_unknown_are_suspected(exists):
    """NULL is unknown, not missing -- both land at the neutral rung, but
    for different reasons; neither is ever promoted by guesswork."""
    assert pl.level_of(_row("C:\\x", exists=exists)) == "suspected"


def test_level_of_tolerates_pre_v8_rows():
    """Rows from an un-migrated index carry no provenance key at all."""
    row = {"folder_path": "C:\\x", "usage_count": 1, "path_exists": 1,
           "is_start_folder": 0}
    assert pl.level_of(row) == "verified"


# ── filter rungs ─────────────────────────────────────────────────────

def _fixture_rows(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    return [
        _row("C:\\stood-here", 5, exists=1, prov="cd"),
        _row(str(real), 4, exists=1),
        _row("C:\\gone\\somewhere", 3, exists=0),
        _row("C:\\unknown\\probe-failed", 2, exists=None),
    ]


def test_suspected_is_identity(tmp_path):
    rows = _fixture_rows(tmp_path)
    out, note = pl.apply_level(rows, "suspected")
    assert out == rows and note is None


def test_verified_keeps_cd_and_verified_only(tmp_path):
    out, _ = pl.apply_level(_fixture_rows(tmp_path), "verified")
    assert [r["folder_path"] for r in out] == [
        "C:\\stood-here", str(tmp_path / "real")]


def test_cd_is_the_hyper_condensed_view(tmp_path):
    out, _ = pl.apply_level(_fixture_rows(tmp_path), "cd")
    assert [r["folder_path"] for r in out] == ["C:\\stood-here"]


def test_superset_chain_on_filter_rungs(tmp_path):
    """AC-1, filter half: each colder selection is a subset."""
    rows = _fixture_rows(tmp_path)
    sus = {r["folder_path"] for r in pl.apply_level(rows, "suspected")[0]}
    ver = {r["folder_path"] for r in pl.apply_level(rows, "verified")[0]}
    cd = {r["folder_path"] for r in pl.apply_level(rows, "cd")[0]}
    assert cd <= ver <= sus


# ── the approximated fold ────────────────────────────────────────────

def test_approximated_folds_missing_leaf_to_existing_ancestor(tmp_path):
    """The user's example: `MoonshotAI\\a (2x) [not found]` counts as
    `MoonshotAI`. Here: a missing leaf under tmp_path folds into it."""
    rows = [_row(str(tmp_path) + "\\a", 2, exists=0)]
    out, _ = pl.apply_level(rows, "approximated")

    assert len(out) == 1
    assert out[0]["folder_path"] == str(tmp_path)
    assert out[0]["usage_count"] == 2
    assert out[0]["_approx_folded"] is True


def test_approximated_merges_into_an_existing_row(tmp_path):
    """When the ancestor already has its own row, counts MERGE."""
    rows = [
        _row(str(tmp_path), 4, exists=1),
        _row(str(tmp_path) + "\\gone-leaf", 2, exists=0),
    ]
    out, _ = pl.apply_level(rows, "approximated")

    assert len(out) == 1
    assert out[0]["usage_count"] == 6
    assert out[0]["_approx_folded"] is True


def test_approximated_conserves_work(tmp_path):
    """The invariant's measurable shadow: credit sums survive the fold.
    (Same conservation trick the collapse-note aggregate uses.)"""
    rows = _fixture_rows(tmp_path) + [
        _row(str(tmp_path) + "\\x\\y\\z", 7, exists=0),
        _row("Q:\\no\\such\\drive\\anywhere", 3, exists=0),
    ]
    out, _ = pl.apply_level(rows, "approximated")

    assert sum(r["usage_count"] for r in out) == \
        sum(r["usage_count"] for r in rows)


def test_approximated_keeps_the_unfoldable(tmp_path):
    """A foreign path with NO reachable ancestor stays as-is: healing
    what cannot be healed by DROPPING it would break conservation."""
    rows = [_row("Q:\\foreign\\machine\\path", 3, exists=0)]
    out, _ = pl.apply_level(rows, "approximated")

    assert out[0]["folder_path"] == "Q:\\foreign\\machine\\path"
    assert "_approx_folded" not in out[0]


def test_approximated_prefers_stored_knowledge_over_probes(tmp_path):
    """An ancestor already VERIFIED in the row set is used without a
    filesystem probe -- stored knowledge first."""
    rows = [
        _row("C:\\gone-parent", 1, exists=1),      # says verified (stored)
        _row("C:\\gone-parent\\leaf", 2, exists=0),
    ]
    out, _ = pl.apply_level(rows, "approximated")

    merged = {r["folder_path"]: r for r in out}
    assert merged["C:\\gone-parent"]["usage_count"] == 3


def test_approximated_does_not_mutate_input(tmp_path):
    rows = [_row(str(tmp_path) + "\\a", 2, exists=0)]
    before = [dict(r) for r in rows]
    pl.apply_level(rows, "approximated")
    assert rows == before


# ── reserved rungs and the alias ─────────────────────────────────────

@pytest.mark.parametrize("reserved", ["junk", "raw"])
def test_reserved_rungs_note_and_fall_back(tmp_path, reserved):
    """AC-5: succeed with an honest note -- never an error, never silently
    equal to suspected WITHOUT the note."""
    rows = _fixture_rows(tmp_path)
    out, note = pl.apply_level(rows, reserved)
    assert out == rows
    assert note is not None and reserved in note


def test_all_is_the_warm_pole_alias(tmp_path):
    rows = _fixture_rows(tmp_path)
    out, note = pl.apply_level(rows, "all")
    assert out == rows        # warm pole == raw == reserved -> everything
    assert note is not None   # and it says so


# ── the fold must never invent a place (tester pass 1, Finding 1) ───

def test_driveless_roots_never_fold_via_the_live_probe():
    """`os.path.isdir("\\")` is TRUE on Windows (current drive root), so
    a foreign POSIX-form row walked up to a fabricated `\\` ancestor that
    was never stored, verified, or real -- a dozen unrelated `\\home\\*`
    rows collapsed into one invented node, in 6 of 15 real sessions.

    The rule: the live probe only speaks for path forms this HOST can
    actually address (drive-letter, UNC). Driveless/POSIX-form rows may
    fold only via STORED-verified ancestors."""
    row = {"folder_path": "\\home\\a", "usage_count": 3,
           "is_start_folder": 0, "path_exists": None,
           "provenance": "extracted"}
    out, _ = pl.apply_level([row], "approximated")

    assert out[0]["folder_path"] == "\\home\\a", (
        "a driveless row folded onto %r -- an invented place"
        % out[0]["folder_path"])
    assert "_approx_folded" not in out[0]


def test_driveless_rows_still_fold_via_stored_knowledge():
    """The stored-knowledge branch stays open for POSIX-form rows: a
    verified `\\home` row IN THE SET is real knowledge, host-independent."""
    rows = [
        {"folder_path": "\\home", "usage_count": 1, "is_start_folder": 0,
         "path_exists": 1, "provenance": "extracted"},
        {"folder_path": "\\home\\gone-leaf", "usage_count": 2,
         "is_start_folder": 0, "path_exists": 0, "provenance": "extracted"},
    ]
    out, _ = pl.apply_level(rows, "approximated")

    merged = {r["folder_path"]: r for r in out}
    assert merged["\\home"]["usage_count"] == 3
    assert merged["\\home"].get("_approx_folded") is True


def test_unc_rows_may_still_live_probe():
    """UNC is a form this host CAN address -- an unreachable share simply
    probes false and the row stays put; no fabrication either way."""
    row = {"folder_path": "\\\\no-such-server\\share\\x", "usage_count": 1,
           "is_start_folder": 0, "path_exists": 0, "provenance": "extracted"}
    out, _ = pl.apply_level([row], "approximated")
    assert out[0]["folder_path"] == "\\\\no-such-server\\share\\x"


# ── the matching paths never import this module (D3 / AC-4) ─────────

def test_matching_paths_are_rung_blind():
    """`scan`/`search` matching must be structurally incapable of rung
    filtering: the query layer never imports pathlevels. Source-level
    assertion, cheap and honest -- the same caller-level lesson as the
    top_n gate."""
    import io
    for mod in ("index.py", "scanner.py", "search.py"):
        try:
            src = io.open(
                "claude_session_backup/" + mod, encoding="utf-8").read()
        except FileNotFoundError:
            continue
        assert "pathlevels" not in src, (
            "%s imports pathlevels -- matching must stay rung-blind" % mod)

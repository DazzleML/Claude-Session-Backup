"""Canary suite for the dazzle_lib.continuum import (#56/H2).

csb is the first Continuum consumer outside dazzlecmd's orbit, and the
module is not yet under dazzle-lib's api-stability lock -- a state the
user (its maintainer) accepted deliberately: *"it acts as a canary which
is helpful."* These tests ARE that canary: they pin the exact behaviors
csb relies on, so upstream churn fails here with a named test instead of
surfacing as a mysterious rung bug in `csb show`.

If one of these breaks after a dazzle-lib upgrade, the fix belongs in
EITHER csb's usage or upstream -- but the failure itself is the feature.
"""

import pytest

from dazzle_lib.continuum import Continuum, ContinuumBoundaryError

from claude_session_backup.pathlevels import PATH_EXPOSURE


# ── the primitive's contract, as csb consumes it ─────────────────────

def _probe():
    return Continuum(
        name="canary",
        ranks={"cold2": -2, "cold1": -1, "mid": 0, "warm1": 1},
        invariant="canary-inv",
    )


def test_canary_passes_is_at_or_colder():
    """THE load-bearing semantic: selection = rank(level) <= rank(threshold).
    Cold passes every threshold (the fall-through guarantee); warm needs
    the threshold cranked warmer. If this inverts upstream, every rung
    view in csb inverts with it."""
    c = _probe()
    assert c.passes("cold2", "mid")
    assert c.passes("mid", "mid")
    assert not c.passes("warm1", "mid")


def test_canary_levels_are_ordered_cold_to_warm():
    assert _probe().levels() == ("cold2", "cold1", "mid", "warm1")


def test_canary_poles_and_neutral():
    c = _probe()
    assert c.cold_pole() == "cold2"
    assert c.warm_pole() == "warm1"
    assert c.neutral() == "mid"


def test_canary_boundary_raises_never_clamps():
    """Stepping past a pole must raise, not silently clamp -- csb relies
    on loud failure at the edges."""
    with pytest.raises(ContinuumBoundaryError):
        _probe().step("cold2", -1)


def test_canary_unknown_level_is_loud():
    """A typo'd level name must raise an error that NAMES the valid
    levels -- csb forwards this class of message to users."""
    with pytest.raises(Exception) as exc:
        _probe().rank("nope")
    assert "levels" in str(exc.value)


# ── csb's own ladder, pinned ─────────────────────────────────────────

def test_path_exposure_ladder_shape():
    """The canonical ladder from the DWP -- stated in code exactly once,
    pinned here exactly once."""
    assert PATH_EXPOSURE.levels() == (
        "cd", "verified", "approximated", "suspected", "junk", "raw")
    assert PATH_EXPOSURE.neutral() == "suspected"
    assert PATH_EXPOSURE.cold_pole() == "cd"
    assert PATH_EXPOSURE.warm_pole() == "raw"
    assert PATH_EXPOSURE.rank("suspected") == 0


def test_path_exposure_invariant_names_the_mention():
    """The invariant string names what the removal test actually leaves
    standing -- the harvested mention and its credit -- not a pragmatic
    stand-in like 'today's behavior'. (Invariant-theory addendum.)"""
    assert PATH_EXPOSURE.invariant == "the harvested mention and its work credit"


def test_path_exposure_fall_through_chain():
    """cd passes every selectable threshold -- the user's switch-case
    fall-through, expressed as the THAC0 gate."""
    for threshold in PATH_EXPOSURE.levels():
        assert PATH_EXPOSURE.passes("cd", threshold)


def test_no_fraction_ranks():
    """Guard against the recorded upstream landmine: densify_between()
    promotes ranks to Fraction, which is not JSON-serializable. csb
    declares every rung statically; ranks must all be plain ints."""
    for level in PATH_EXPOSURE.levels():
        assert type(PATH_EXPOSURE.rank(level)) is int, level

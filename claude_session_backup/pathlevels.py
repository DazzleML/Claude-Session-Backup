"""The path-exposure ladder: harvest confidence as a Continuum (#56/H2).

The harvester inherently produces path mentions at different confidence
levels, and no single display cut answers every question -- ``.manifest``-
class rows exist at the expressive end no matter how many lexical rules
land, because lexical rules cannot decide a foreign path's nature. Instead
of fighting that shape-by-shape, this module exposes the spectrum itself
as selectable rungs with THAC0 fall-through: pick a level, get it and
everything colder.

The axis is a composition of two CONFLICTING readings of one mention:

    { PLACE-reading  <->  TEXT-reading }

the same string read as a *location* (cold side) versus as a *string*
(warm side). The removal test -- strip "trusted-as-place" from the cold
side and "raw-as-text" from the warm -- leaves the thing both sides share
and the index actually stores: **the harvested mention and its work
credit**. That is the invariant, and its zero is the unsigned quasi-state:
the mention as extracted, before the place-or-text judgment. Which is
``suspected`` -- today's behavior, conserved.

The measurable shadow of the invariant is a conservation law: work-credit
sums are preserved through every rung view, including the ``approximated``
fold (tested as a property, not assumed).

Rung views are DISPLAY/QUERY-time transforms over one maximally-granular
index -- never harvest modes. Matching (``scan``/``search``) is rung-blind
by design rule (the #56 display-never-changes-matching axiom); nothing in
this module is imported by the matching paths, and a test pins that.

Design: 2026-08-01__13-18-51__dev-workflow-process__path-trust-rungs-
continuum.md (including the invariant-theory addendum).
"""

import os
import re
from typing import Optional

from dazzle_lib.continuum import Continuum

#: The ladder. Cold = place-reading (trusted rows pass EVERY threshold,
#: like `error` in the THAC0 verbosity ladder -- that IS the fall-through
#: guarantee); warm = text-reading, opt-in expressiveness. Zero conserves
#: today's behavior. `junk` and `raw` are DECLARED but unpopulated until a
#: raw-harvest storage mode exists (ledger: deferred with trigger);
#: declaring them now costs nothing and avoids densify_between later,
#: whose Fraction ranks are a recorded JSON landmine upstream.
PATH_EXPOSURE = Continuum(
    name="path-exposure",
    ranks={
        "cd": -3,           # the shell provably stood there (cold pole)
        "verified": -2,     # the existence probe confirmed the folder
        "approximated": -1, # unverifiable leaves folded to real ancestors
        "suspected": 0,     # the mention as extracted (neutral, default)
        "junk": 1,          # RESERVED: rules-rejected shapes, retained
        "raw": 2,           # RESERVED: file-level candidates (warm pole)
    },
    invariant="the harvested mention and its work credit",
)

#: Levels a user may select today. `all` is CLI sugar for the warm pole.
SELECTABLE_LEVELS = tuple(PATH_EXPOSURE.levels()) + ("all",)

#: Rungs that are declared but carry no stored population yet. Selecting
#: one succeeds -- with an honest note -- rather than erroring or silently
#: equaling `suspected` (AC-5).
RESERVED_LEVELS = frozenset({"junk", "raw"})

RESERVED_NOTE = (
    "note: '{level}' is a declared rung with no stored population yet -- "
    "the harvester rejects those shapes at index time. Showing 'suspected' "
    "(everything stored). A raw-harvest storage mode would populate it."
)

DEFAULT_LEVEL = "suspected"

#: Provenance precedence when one folder is touched via several routes
#: across a session: coldest wins, because provenance feeds level
#: derivation and a folder the shell EVER stood in has earned `cd`.
PROVENANCE_ORDER = ("cd", "structured", "relative", "extracted")


def merge_provenance(old: Optional[str], new: Optional[str]) -> Optional[str]:
    """Coldest-wins merge of two provenance stamps."""
    if old is None:
        return new
    if new is None:
        return old
    try:
        return old if PROVENANCE_ORDER.index(old) <= PROVENANCE_ORDER.index(new) else new
    except ValueError:
        return old


def level_of(row) -> str:
    """Derive a stored row's rung, read-time, from provenance + existence.

    ``approximated`` is never a stored level -- it is a fold applied by
    :func:`apply_level`. ``junk``/``raw`` are never stored (rejected at
    harvest). So static derivation lands on exactly three rungs:

      provenance `cd`    -> ``cd``        (the shell stood there;
                                           ambient-cwd bases count -- the
                                           pre-#56 behavior this rung
                                           reconstructs was cwd-based)
      path_exists = 1    -> ``verified``
      otherwise          -> ``suspected`` (including NULL: unknown is not
                                           missing)
    """
    prov = _row_get(row, "provenance")
    if prov == "cd":
        return "cd"
    if _row_get(row, "path_exists") == 1:
        return "verified"
    return "suspected"


def _row_get(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def _nearest_existing_ancestor(path: str, known: dict) -> Optional[str]:
    """Walk up until a component that exists -- stored knowledge first
    (a row already verified), live probe second. Never past the drive/root.
    """
    cur = path
    while True:
        parent = os.path.dirname(cur.rstrip("\\/"))
        if not parent or parent == cur or re.fullmatch(r"[A-Za-z]:\\?", parent):
            return None
        if parent in known:
            if known[parent]:
                return parent
        elif re.match(r"^[A-Za-z]:", parent) or parent.startswith("\\\\"):
            # The live probe only speaks for path forms this HOST can
            # address: drive-letter and UNC. `os.path.isdir("\\")` is TRUE
            # on Windows (the current drive's root), so a driveless
            # POSIX-form chain walked up to a fabricated `\` node that was
            # never stored, verified, or real -- a dozen unrelated
            # `\home\*` rows collapsed into one invented place, found live
            # in 6 of 15 sessions by the first 0.7.3 tester pass. Foreign-
            # form rows still fold via STORED-verified ancestors above,
            # which are real knowledge regardless of host.
            try:
                if os.path.isdir(parent):
                    return parent
            except (OSError, ValueError):
                pass
        cur = parent


def apply_level(rows: list, chosen: str) -> tuple[list, Optional[str]]:
    """The rung view: ``(rows_for_display, note_or_None)``.

    Selection is ``PATH_EXPOSURE.passes(level_of(row), chosen)`` -- at-or-
    colder shows -- except ``approximated``, which is a TRANSFORM rather
    than a filter: suspected rows fold upward into their nearest existing
    ancestor (counts merged, row marked ``_approx_folded``), and a row
    with no reachable ancestor stays as-is, because healing what cannot
    be healed by dropping it would violate the conservation law.

    Rows are dicts (as produced by the index layer); returned rows may be
    new dicts. Input is never mutated.
    """
    if chosen == "all":
        chosen = PATH_EXPOSURE.warm_pole()
    note = None
    if chosen in RESERVED_LEVELS:
        note = RESERVED_NOTE.format(level=chosen)
        chosen = DEFAULT_LEVEL

    if chosen == DEFAULT_LEVEL:
        return list(rows), note

    if chosen == "approximated":
        known = {}
        for r in rows:
            known[r["folder_path"]] = (_row_get(r, "path_exists") == 1)
        out: dict = {}
        order: list = []

        def _absorb(key, row, folded):
            if key in out:
                tgt = out[key]
                tgt["usage_count"] = tgt["usage_count"] + row["usage_count"]
                if folded:
                    tgt["_approx_folded"] = True
            else:
                nr = dict(row)
                if folded:
                    nr["folder_path"] = key
                    nr["_approx_folded"] = True
                    # The fold lands on a directory that exists (that was
                    # the criterion), so the display hint follows.
                    nr["path_exists"] = 1
                out[key] = nr
                order.append(key)

        for r in rows:
            if level_of(r) in ("cd", "verified"):
                _absorb(r["folder_path"], r, folded=False)
                continue
            anc = _nearest_existing_ancestor(r["folder_path"], known)
            if anc is None:
                _absorb(r["folder_path"], r, folded=False)
            else:
                _absorb(anc, r, folded=True)
        return [out[k] for k in order], note

    # Plain filters: verified, cd.
    kept = [r for r in rows
            if PATH_EXPOSURE.passes(level_of(r), chosen)]
    return kept, note

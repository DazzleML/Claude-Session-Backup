"""
Fork lineage -- build the session forest from ``parent_session_id`` pointers.

Claude Code mints a new session file whenever a session is forked
(``/branch``, ``/rewind``-continue, ``claude --fork-session -r``), and the
new file carries a ``forkedFrom`` pointer at its parent (extracted into
``sessions.parent_session_id`` by :mod:`metadata`). Every session therefore
has AT MOST ONE parent, which makes the relationship graph a **forest** --
a set of independent trees, one per root session -- rather than a general
DAG. This module turns those flat pointers into that forest.

**Three-tier rendering model.** The builder decides WHICH nodes belong in
the output and tags each with the tier the renderer should use:

  - ``matched``       -- the node satisfied the user's FILTER (highlight).
  - ``in_population`` -- the node satisfies the active/deleted scope
    (``--deleted``); rendered normally.
  - neither           -- a **structural connector**: a node pulled in only
    because an in-scope descendant needs it to establish lineage (a purged
    ancestor), or a **phantom** placeholder for a parent UUID the index has
    never seen. Rendered dim.

That tiering is what lets the default scope stay at parity with
``csb list`` / ``csb scan`` (active sessions only) while never rendering a
decapitated chain: purged ancestors appear as dim connectors, but a purged
*leaf* with no in-scope descendants does not appear at all.

**Component view.** A FILTER selects nodes; the forest then renders every
connected component (chain family) containing at least one selected node,
so a matched session shows its ancestors above it and its descendants
below it in a single view. With no filter, every component renders.

The returned :class:`LineageNode` graph is a plain, JSON-serializable
structure with ``parent`` / ``children`` / ``depth`` -- deliberately shaped
to satisfy DazzleTreeLib's ``TreeAdapter`` contract (``get_children`` /
``get_parent`` / ``get_depth``) so a future interactive walker can traverse
it through that library without reshaping anything here. Rendering lives in
:mod:`lineage_render`; this module never formats.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterator, Optional

# Fields a FILTER is matched against -- the same vocabulary
# ``csb list <filter>`` / ``csb scan <term>`` use (see
# ``index.find_sessions_by_term``): identity, name, project, start folder,
# plus every recorded working directory. Matching happens in Python here
# (not SQL) because the forest is already fully in memory for the walk and
# because ``-E`` regex has no SQL equivalent -- one matcher, three modes.
_MATCH_FIELDS = ("session_id", "session_name", "project", "start_folder")

# Guard rail from #31: a single chain deeper/wider than this collapses with
# a "see csb tree --root <uuid>" hint so one runaway family can't bury the
# rest of the forest.
DEFAULT_MAX_NODES_PER_ROOT = 50


@dataclass
class LineageNode:
    """One session in the fork forest (or a phantom stand-in for one).

    ``session`` holds the full ``sessions`` row as a dict; for a phantom it
    holds only ``session_id`` so renderers can still show the UUID.
    """

    session_id: str
    session: dict
    parent: Optional["LineageNode"] = None
    children: list["LineageNode"] = field(default_factory=list)
    depth: int = 0
    # Rendering tiers (see module docstring).
    in_population: bool = False
    matched: bool = False
    phantom: bool = False
    # Set when this node's children were elided by the per-root cap.
    elided_children: int = 0

    # -- TreeAdapter-shaped accessors (DazzleTreeLib compatibility seam) --
    def get_children(self) -> Iterator["LineageNode"]:
        return iter(self.children)

    def get_parent(self) -> Optional["LineageNode"]:
        return self.parent

    def get_depth(self) -> int:
        return self.depth

    @property
    def is_deleted(self) -> bool:
        return bool(self.session.get("deleted_at"))

    def walk(self) -> Iterator["LineageNode"]:
        """Yield this node then every descendant, depth-first, in child order."""
        yield self
        for child in self.children:
            yield from child.walk()

    def to_dict(self) -> dict:
        """Nested JSON form: the session row plus a ``children`` array.

        This is the ``--json`` payload and the feed a future TUI walker
        consumes.
        """
        return {
            **self.session,
            "is_phantom": self.phantom,
            "in_scope": self.in_population,
            "matched": self.matched,
            "depth": self.depth,
            "elided_children": self.elided_children,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class Forest:
    """Result of a lineage build."""

    roots: list[LineageNode] = field(default_factory=list)
    # Diagnostics the caller may surface.
    cycles_broken: list[str] = field(default_factory=list)
    phantom_parents: list[str] = field(default_factory=list)
    total_nodes: int = 0
    elided_nodes: int = 0

    def walk(self) -> Iterator[LineageNode]:
        for root in self.roots:
            yield from root.walk()


def _build_matcher(term: Optional[str], regex: bool, case_sensitive: bool = False):
    """Return ``predicate(text) -> bool`` for the three FILTER modes.

    - ``regex=True``      -- Python ``re.search`` (the ``-E`` flag).
    - trailing ``*``      -- anchored prefix (``NAME*``), matching the
      trailing-wildcard convention ``csb scan -d <pattern>*`` already uses.
    - otherwise           -- case-insensitive substring, same as
      ``csb list <filter>``.

    Raises ``re.error`` for an invalid regex; the caller turns that into a
    clean CLI error.
    """
    if term is None:
        return None
    # A bare "*" (or an empty string) means "everything" -- users reach for
    # it as a placeholder when they really want the PATH argument, e.g.
    # `csb tree * .`. Shells that do not glob pass it through literally, and
    # matching it as a literal asterisk would silently find nothing.
    if term in ("", "*"):
        return None
    flags = 0 if case_sensitive else re.IGNORECASE
    if regex:
        pattern = re.compile(term, flags)
        return lambda text: bool(pattern.search(text))
    if term.endswith("*") and len(term) > 1:
        prefix = term[:-1]
        if not case_sensitive:
            prefix = prefix.lower()

        def _prefix(text: str) -> bool:
            return (text if case_sensitive else text.lower()).startswith(prefix)
        return _prefix
    needle = term if case_sensitive else term.lower()
    return lambda text: needle in (text if case_sensitive else text.lower())


def _session_matches(row: dict, folders: list[str], predicate) -> bool:
    """True if any searchable field of this session satisfies ``predicate``."""
    for key in _MATCH_FIELDS:
        value = row.get(key)
        if value and predicate(str(value)):
            return True
    return any(predicate(f) for f in folders)


def _deleted_ok(row: dict, deleted_filter: str) -> bool:
    """Population membership for the active/deleted scope (``--deleted``)."""
    deleted = bool(row.get("deleted_at"))
    if deleted_filter == "all":
        return True
    if deleted_filter == "deleted":
        return deleted
    return not deleted  # "active" (default)


def build_forest(
    conn: sqlite3.Connection,
    *,
    filter_term: Optional[str] = None,
    regex: bool = False,
    case_sensitive: bool = False,
    scope_ids: Optional[set[str]] = None,
    deleted_filter: str = "active",
    root: Optional[str] = None,
    orphans_only: bool = False,
    component_view: bool = True,
    max_nodes_per_root: int = DEFAULT_MAX_NODES_PER_ROOT,
) -> Forest:
    """Build the fork forest.

    Args:
        conn: open index connection.
        filter_term: FILTER positional -- substring / ``prefix*`` / regex.
        regex: interpret ``filter_term`` as a Python regex (``-E``).
        case_sensitive: case-sensitive FILTER matching.
        scope_ids: when given, only these session ids may be SELECTED
            (the ``-d`` / ``-D`` directory scope, resolved by the caller
            through the same helpers ``csb scan`` uses). Ancestors outside
            the set can still appear as structural connectors.
        deleted_filter: ``"active"`` (default) / ``"deleted"`` / ``"all"``.
        root: restrict to the component containing this session id (already
            resolved to a full UUID by the caller).
        orphans_only: keep only roots that have no children at all.
        component_view: when True (default), a match pulls in its WHOLE
            family -- the component is expanded from its topmost root, so
            siblings and cousins render alongside the match's own spine.
            When False, only the match's ancestors and descendants render
            (a tighter "lineage spine" view).
        max_nodes_per_root: per-component node cap before eliding.

    Returns:
        :class:`Forest` with ``roots`` ordered as the DB returned them --
        the caller applies ``--sort`` ordering to roots; children are
        always ordered by ``forked_at`` ascending (the temporal reading
        order specified in #31).
    """
    rows = {
        r["session_id"]: dict(r)
        for r in conn.execute("SELECT * FROM sessions").fetchall()
    }

    # Full folder rows, not just paths: the renderer's -ff level needs
    # {folder_path, usage_count, is_start_folder} to resolve "start at"
    # (slug disambiguation) and list the other folders, exactly as
    # csb list / csb search do. Ordered like those commands order them.
    folder_rows: dict[str, list[dict]] = {}
    for r in conn.execute(
        "SELECT session_id, folder_path, usage_count, is_start_folder "
        "FROM folder_usage ORDER BY usage_count DESC, is_start_folder DESC"
    ).fetchall():
        folder_rows.setdefault(r["session_id"], []).append(dict(r))
    folders = {sid: [f["folder_path"] for f in rows]
               for sid, rows in folder_rows.items()}

    predicate = _build_matcher(filter_term, regex, case_sensitive)

    # -- Population + selection -------------------------------------------
    # population: satisfies the deleted scope (parity with list/scan).
    # selected:   population AND the filter AND the directory scope. These
    #             are the nodes whose COMPONENTS get rendered.
    population: set[str] = set()
    selected: set[str] = set()
    matched: set[str] = set()
    for sid, row in rows.items():
        in_pop = _deleted_ok(row, deleted_filter)
        if in_pop:
            population.add(sid)
        hit = predicate is None or _session_matches(row, folders.get(sid, []), predicate)
        if hit and predicate is not None:
            matched.add(sid)
        if in_pop and hit and (scope_ids is None or sid in scope_ids):
            selected.add(sid)

    # -- Parent resolution + cycle guard ----------------------------------
    # A parent pointer at a UUID we have never indexed becomes a PHANTOM
    # node so siblings still group under a common placeholder instead of
    # being scattered as unrelated roots.
    cycles_broken: list[str] = []
    phantom_ids: set[str] = set()
    parent_of: dict[str, Optional[str]] = {}
    for sid, row in rows.items():
        pid = row.get("parent_session_id")
        if not pid or pid == sid:
            parent_of[sid] = None
            continue
        parent_of[sid] = pid
        if pid not in rows:
            phantom_ids.add(pid)

    for pid in phantom_ids:
        parent_of.setdefault(pid, None)

    def _ancestors(sid: str) -> list[str]:
        """Walk up from ``sid``; breaks (and records) any cycle."""
        chain: list[str] = []
        seen = {sid}
        cur = parent_of.get(sid)
        while cur is not None:
            if cur in seen:
                cycles_broken.append(cur)
                parent_of[chain[-1] if chain else sid] = None
                break
            chain.append(cur)
            seen.add(cur)
            cur = parent_of.get(cur)
        return chain

    children_of: dict[str, list[str]] = {}
    for sid, pid in parent_of.items():
        if pid is not None:
            children_of.setdefault(pid, []).append(sid)

    def _descendants(sid: str) -> Iterator[str]:
        stack = list(children_of.get(sid, []))
        seen: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            yield cur
            stack.extend(children_of.get(cur, []))

    # -- Which nodes render -----------------------------------------------
    # Every selected node, plus:
    #   - ALL its ancestors (structural connectors -- dim when out of
    #     population; this is what keeps a chain from rendering headless),
    #   - its in-population descendants (normal tier; an out-of-population
    #     descendant is a dead leaf and is NOT pulled in).
    # In component_view (default) the walk starts from each selected node's
    # topmost root instead of the node itself, so the match's siblings and
    # cousins come along -- "here is the whole family, and here is where
    # your match sits in it".
    render: set[str] = set()
    seeds: set[str] = set()
    for sid in selected:
        if component_view:
            chain = _ancestors(sid)
            seeds.add(chain[-1] if chain else sid)
        else:
            seeds.add(sid)

    for seed in seeds:
        for cand in [seed, *_descendants(seed)]:
            if cand in population:
                render.add(cand)
                render.update(_ancestors(cand))
    # A selected node always renders, even if the deleted scope would
    # otherwise exclude it (it is why we are drawing this component).
    render.update(selected)
    for sid in selected:
        render.update(_ancestors(sid))

    if root is not None:
        keep = {root} | set(_ancestors(root)) | set(_descendants(root))
        render &= keep
        if root in rows or root in phantom_ids:
            render.add(root)
            render.update(_ancestors(root))
            render.update(d for d in _descendants(root) if d in population)

    if not render:
        return Forest()

    # -- Materialize the node objects -------------------------------------
    nodes: dict[str, LineageNode] = {}
    for sid in render:
        row = rows.get(sid)
        phantom = row is None
        if row is not None:
            # Attach folder rows so the -ff renderer has the same data
            # csb list / csb search hand to the shared timeline helpers.
            row = {**row, "folders": folder_rows.get(sid, [])}
        nodes[sid] = LineageNode(
            session_id=sid,
            session=row if row is not None else {"session_id": sid},
            in_population=(sid in population) and not phantom,
            matched=sid in matched,
            phantom=phantom,
        )

    roots: list[LineageNode] = []
    for sid, node in nodes.items():
        pid = parent_of.get(sid)
        if pid is not None and pid in nodes:
            node.parent = nodes[pid]
            nodes[pid].children.append(node)
        else:
            roots.append(node)

    # Children read in fork order (#31). Sessions missing forked_at (roots
    # re-parented by a broken chain, phantoms) sort last but stably.
    def _child_key(n: LineageNode):
        return (n.session.get("forked_at") or "￿", n.session_id)

    def _assign_depth(node: LineageNode, depth: int) -> None:
        node.depth = depth
        node.children.sort(key=_child_key)
        for child in node.children:
            _assign_depth(child, depth + 1)

    for r in roots:
        _assign_depth(r, 0)

    if orphans_only:
        roots = [r for r in roots if not r.children]

    # -- Per-root cap ------------------------------------------------------
    elided_total = 0
    if max_nodes_per_root and max_nodes_per_root > 0:
        for r in roots:
            elided_total += _elide_beyond(r, max_nodes_per_root)

    total = sum(1 for _ in Forest(roots=roots).walk())
    return Forest(
        roots=roots,
        cycles_broken=sorted(set(cycles_broken)),
        phantom_parents=sorted(pid for pid in phantom_ids if pid in nodes),
        total_nodes=total,
        elided_nodes=elided_total,
    )


def _elide_beyond(root: LineageNode, cap: int) -> int:
    """Trim ``root``'s subtree to ``cap`` nodes, breadth-first.

    Breadth-first so the shallow shape of a big family survives -- the user
    still sees the branch structure and gets a per-node count of what was
    dropped, with the ``--root`` hint to drill in.
    """
    kept = 0
    queue = [root]
    keep: set[int] = set()
    while queue and kept < cap:
        node = queue.pop(0)
        keep.add(id(node))
        kept += 1
        queue.extend(node.children)
    if kept < cap and not queue:
        return 0

    elided = 0
    for node in list(root.walk()):
        if id(node) not in keep:
            continue
        survivors = [c for c in node.children if id(c) in keep]
        dropped = [c for c in node.children if id(c) not in keep]
        if dropped:
            count = sum(1 for d in dropped for _ in d.walk())
            node.elided_children = count
            elided += count
            node.children = survivors
    return elided

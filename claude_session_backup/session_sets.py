"""Named session sets: storage for curated groups of sessions (#60, #62).

A *named set* is a list of sessions the user says belong together --
``CSB-STACK``, ``DAZZLE-LIBS``, the three windows that go with one task.
It is deliberately the simplest thing that works: a name and a list of
session ids. No hooks, no boot fences, no observation machinery. That
independence is the point -- named sets answer "which sessions do I
reload together" (requires foresight), while boot epochs answer "what was
I running when it died" (requires none). They share a verb, a renderer,
and an addressing scheme, but not a definition.

**Storage lives in the git-backed store, not the index.** The file sits
at ``<claude_dir>/csb-sets.json`` and is committed in the *user* class of
csb's two-commit model (alongside settings and skills), which gives it
three properties the SQLite index cannot:

* It survives ``csb update rebuild-index`` **by construction** -- the
  rebuild reconstructs the index from git and never touches source data.
  Set membership is not derivable from transcripts, so an index table
  would be silently erased by a command csb actively recommends.
* It is versioned and recoverable. A bad edit is one ``git checkout``
  away, which is why a corrupt file here is an error rather than a
  silent reset.
* It rides existing backups. No new commit path, no new registration
  beyond one entry in ``git_ops.USER_FILES``.

Members are stored as **full UUIDs**, resolved once when added. Names
change (``/rename`` is encouraged); UUIDs do not.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SETS_FILENAME = "csb-sets.json"
FORMAT_VERSION = 1

# Words the `csb set` / `csb resume set` grammar claims. A set named
# `last` would shadow the boot epoch; `current` would shadow the live
# registry view (#64); one named `set` would be unreachable after
# `csb resume`; a bare integer would collide with `csb resume set <N>`
# index addressing.
RESERVED_NAMES = frozenset({"last", "set", "current"})

_BARE_INT_RE = re.compile(r"^\d+$")


class SetError(Exception):
    """A set operation failed. The message is user-facing."""


def sets_path(claude_dir) -> Path:
    """Where the sets file lives for a given claude dir."""
    return Path(claude_dir) / SETS_FILENAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_set_name(name: str) -> None:
    """Raise :class:`SetError` if ``name`` cannot be used as a set name.

    Only the grammar-reserved cases are rejected. Everything else is a
    convention documented in ``docs/naming.md``, not a rule -- csb should
    not be the first tool to tell a user their name is wrong.
    """
    if not name or not name.strip():
        raise SetError("A set name cannot be empty.")
    if name != name.strip():
        raise SetError(
            f"Set name {name!r} has leading or trailing whitespace -- "
            "use the trimmed form."
        )
    if name.lower() in RESERVED_NAMES:
        raise SetError(
            f"'{name}' is reserved -- 'last' names the most recent boot "
            "epoch and 'set' is a grammar token after `csb resume`. "
            "See docs/naming.md for set-name conventions."
        )
    if _BARE_INT_RE.match(name):
        raise SetError(
            f"'{name}' is reserved -- bare integers collide with "
            "`csb resume set <N>` index addressing. "
            "See docs/naming.md for set-name conventions."
        )


def _empty_doc() -> dict:
    return {"version": FORMAT_VERSION, "sets": {}}


def load_sets(claude_dir, *, warn=None) -> dict:
    """Read the sets file. Missing file -> an empty document.

    A corrupt or structurally wrong file raises :class:`SetError` rather
    than resetting: the file is user data, and silently replacing it with
    ``{}`` would destroy curated work to make a command succeed. The
    remedy (restore from the user-class git history) is in the message.

    Reserved names found in a hand-edited file are skipped with a warning
    rather than honored -- otherwise a set literally named ``last`` would
    shadow the boot epoch from inside the data.
    """
    path = sets_path(claude_dir)
    if not path.exists():
        return _empty_doc()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetError(
            f"Could not read {path}: {exc}. The file is committed in the "
            f"user class -- `git log -- {SETS_FILENAME}` in your backup "
            "store has prior versions."
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("sets"), dict):
        raise SetError(
            f"{path} is not a csb sets file (expected an object with a "
            "'sets' key). Refusing to overwrite it."
        )
    cleaned = {}
    for name, entry in raw["sets"].items():
        if name.lower() in RESERVED_NAMES or _BARE_INT_RE.match(name):
            if warn:
                warn(
                    f"Skipping set '{name}' from {path} -- that name is "
                    "reserved by the `csb set` grammar."
                )
            continue
        if isinstance(entry, dict) and isinstance(entry.get("members"), list):
            cleaned[name] = entry
    raw["sets"] = cleaned
    return raw


def save_sets(claude_dir, doc: dict) -> None:
    """Write the sets file atomically (temp file in the same dir + replace).

    Same-directory temp keeps the rename on one volume, so ``os.replace``
    is atomic: a crash mid-write leaves the previous file intact rather
    than a truncated one.
    """
    path = sets_path(claude_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{SETS_FILENAME}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        # Leave the original untouched; drop the temp file.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def get_set(claude_dir, name: str, *, warn=None) -> Optional[dict]:
    """One set's entry, or None. Name matching is case-insensitive."""
    doc = load_sets(claude_dir, warn=warn)
    for existing, entry in doc["sets"].items():
        if existing.lower() == name.lower():
            return entry
    return None


def resolve_set_name(claude_dir, name: str, *, warn=None) -> Optional[str]:
    """The stored spelling of ``name``, or None when no such set exists."""
    doc = load_sets(claude_dir, warn=warn)
    for existing in doc["sets"]:
        if existing.lower() == name.lower():
            return existing
    return None


def list_sets(claude_dir, *, warn=None) -> list[tuple[str, dict]]:
    """All sets as ``(name, entry)``, name-sorted (case-insensitive)."""
    doc = load_sets(claude_dir, warn=warn)
    return sorted(doc["sets"].items(), key=lambda kv: kv[0].lower())


def create_set(claude_dir, name: str, session_ids: list[str]) -> dict:
    """Create a new set. Raises :class:`SetError` if the name is taken."""
    validate_set_name(name)
    doc = load_sets(claude_dir)
    if resolve_set_name(claude_dir, name) is not None:
        raise SetError(
            f"A set named '{name}' already exists -- use `csb set add "
            f"{name} <session>` to extend it, or pick another name."
        )
    now = _utc_now_iso()
    entry = {
        "created_at": now,
        "updated_at": now,
        "members": [{"session_id": sid, "added_at": now} for sid in session_ids],
    }
    doc["sets"][name] = entry
    save_sets(claude_dir, doc)
    return entry


def delete_set(claude_dir, name: str) -> bool:
    """Remove a whole set. Returns False when it did not exist."""
    doc = load_sets(claude_dir)
    stored = None
    for existing in doc["sets"]:
        if existing.lower() == name.lower():
            stored = existing
            break
    if stored is None:
        return False
    del doc["sets"][stored]
    save_sets(claude_dir, doc)
    return True


def add_member(claude_dir, name: str, session_id: str) -> bool:
    """Add one member. Returns False when already present (idempotent)."""
    doc = load_sets(claude_dir)
    stored = None
    for existing in doc["sets"]:
        if existing.lower() == name.lower():
            stored = existing
            break
    if stored is None:
        raise SetError(
            f"No set named '{name}' -- create it with `csb set new {name} "
            "<session>`."
        )
    entry = doc["sets"][stored]
    if any(m["session_id"] == session_id for m in entry["members"]):
        return False
    entry["members"].append(
        {"session_id": session_id, "added_at": _utc_now_iso()}
    )
    entry["updated_at"] = _utc_now_iso()
    save_sets(claude_dir, doc)
    return True


def remove_member(claude_dir, name: str, session_id: str) -> bool:
    """Remove one member. Returns False when it was not a member."""
    doc = load_sets(claude_dir)
    stored = None
    for existing in doc["sets"]:
        if existing.lower() == name.lower():
            stored = existing
            break
    if stored is None:
        raise SetError(
            f"No set named '{name}' -- `csb set list` shows what exists."
        )
    entry = doc["sets"][stored]
    before = len(entry["members"])
    entry["members"] = [
        m for m in entry["members"] if m["session_id"] != session_id
    ]
    if len(entry["members"]) == before:
        return False
    entry["updated_at"] = _utc_now_iso()
    save_sets(claude_dir, doc)
    return True

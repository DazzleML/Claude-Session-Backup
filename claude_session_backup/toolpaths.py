"""Harvest work locations from tool-call arguments (#56).

``metadata.py`` historically derived a session's folders from each event's
``cwd``. Current Claude Code stamps ``cwd`` with the directory the session
was *launched* from and only ever follows it into descendants -- never into
another repository -- so a session that spends its life editing
``C:\\code\\other-repo`` records exactly one folder and is invisible to
``csb scan other-repo``.

The work locations ARE in the transcript, in ``tool_use.input``. This module
turns those inputs into folders, conservatively.

Two questions, two different units (see the design doc):

- **Presence** -- "did this session touch X at all?" Every folder a call
  touched gets recorded, so ``csb scan`` finds it. :func:`touched_folders`.
- **Work** -- "where did the work happen?" ONE folder per tool call, the
  place the call actually operated. :func:`primary_folder`.

Counting *events* (the old behavior) made the launch directory unbeatable:
a transcript carries ~5x more events than tool calls, so the launch folder
outranked the real workplace by construction. One call = one unit of work
fixes that.

Precision matters more than recall here: a missed folder is a row nobody
sees, while a bogus one is a wrong answer on a surface users read. Every
rule below was added in response to junk found in REAL transcripts, never
speculatively:

1. Anchored matches -- ``private/claude/x`` must not yield ``/claude/x``.
2. Real roots only -- a POSIX path must start at a known root or a
   drive-mapped prefix.
3. No devices -- ``2>/dev/null`` is not a working directory (delegated to
   ``dazzle_filekit.is_device_path``, which also covers Windows reserved
   names like ``NUL``/``CON``).
4. No shell metacharacters -- ``C:\\code\\$r`` and ``C:\\code\\*lib*`` are an
   unexpanded variable and a glob, not places.
5. No empty interior components -- escaped literals in embedded source code
   normalize into ``a\\\\b``.
6. No colon past the drive spec -- a colon-joined ``PYTHONPATH`` list or a
   pytest node id (``test_x.py::TestClass``) is not a place.
7. No leading ``~`` -- an unexpanded home marker cannot be resolved for a
   transcript from another machine, and resolving it against this one would
   fabricate a location.

Separately, a candidate that names a FILE is reduced to its parent
directory. That question is asked of the filesystem first
(``dazzle_filekit.classify_fs_object``) and only guessed from a trailing
extension when the path cannot be classified -- the guess misses every
extensionless file (``VERSION``, ``Makefile``, git hooks).
"""

import ntpath
import os
import re
from typing import Iterable, Optional

try:
    from dazzle_filekit import classify_fs_object, is_device_path
except ImportError:  # pragma: no cover - filekit is a hard dependency
    # Degrade rather than crashing the indexer; the lexical rules still apply.
    def is_device_path(path):  # type: ignore[misc]
        return False

    def classify_fs_object(path):  # type: ignore[misc]
        return "unknown"


# ── Tool -> where its paths live ──────────────────────────────────────

#: Tools whose input names a FILE; the folder is its dirname.
FILE_PATH_TOOLS = frozenset({"Read", "Edit", "Write", "NotebookEdit"})
#: Tools whose input names a DIRECTORY directly.
DIR_PATH_TOOLS = frozenset({"Glob", "Grep"})
#: Tools carrying a shell command string that may mention paths.
COMMAND_TOOLS = frozenset({"Bash", "PowerShell"})

#: POSIX roots we accept as the start of an absolute path.
#:
#: An allowlist rather than "any `/word/word`" because shell commands are
#: full of slash-delimited things that are not paths -- `sed -n '/start/,
#: /end/p'` being the common one. Requiring a plausible root rejects those
#: without needing to parse shell grammar.
#:
#: Covers the FHS roots, macOS additions, and the container/dev-box roots
#: people actually mount work under. A root missing from this list means
#: that path is skipped -- under-harvesting, which is the failure direction
#: we chose deliberately (a missed row beats a wrong one).
_POSIX_ROOTS = (
    "home|usr|opt|var|tmp|etc|srv|media|mnt|root|proc|sys|bin|sbin|lib"
    "|data|work|workspace|workspaces|app|apps|code|src|projects|repos"
    "|Users|Volumes|private|scratch|build|storage"
)

# A path must begin at a boundary -- start of string, whitespace, quote, or
# a shell operator -- so we never match inside `private/claude/x`.
_BOUNDARY = r"(?:(?<=^)|(?<=[\s\"'=(,;|&<>]))"

WIN_PATH = re.compile(_BOUNDARY + r"([A-Za-z]:[\\/][^\s\"'|<>;&,)]*)")
POSIX_PATH = re.compile(
    _BOUNDARY
    + r"(/(?:mnt/)?[A-Za-z]/[^\s\"'|<>;&,)]*"          # /c/... and /mnt/c/...
    + r"|/(?:" + _POSIX_ROOTS + r")(?:/[^\s\"'|<>;&,)]*)?)"
)

#: `cd` / `pushd` target -- the shell literally moved there, so it wins
#: primary resolution over any other path in the same command.
CD_TARGET = re.compile(
    r"(?:^|[\s;&|(])(?:cd|pushd)\s+(?:/d\s+)?[\"']?([^\s\"'&|;]+)", re.IGNORECASE
)

#: Shell metacharacters. A candidate containing any of these is a glob or an
#: unexpanded variable -- a query or a placeholder, never a location.
_METACHARS = re.compile(r"[*?$%{}\[\]]")

#: Path components that mark scratch space. Matched per-component and
#: exactly, so `C:\code\tmpl-project` is NOT scratch.
SCRATCH_COMPONENTS = frozenset({
    "temp", "tmp", ".tmp", "cache", ".cache", "__pycache__",
    "node_modules", ".pytest_cache", "scratchpad",
})


def normalize(path: str) -> str:
    """Collapse the four spellings of one folder into a single key.

    ``/c/code/x`` (Git Bash), ``/mnt/c/code/x`` (WSL), ``C:/code/x`` and
    ``C:\\code\\x`` are the same directory. ``find_sessions_by_folder_usage``
    matches on the backslash form, so normalize to that -- otherwise one
    folder splits across four rows and none of them carry the real count.

    **Deliberately NOT** ``dazzle_filekit.normalize_cross_platform_path``,
    despite the resemblance. That one is a *host-directional resolver*: it
    absolutizes against the current working directory and prepends the
    current drive, so ``""`` becomes the CWD, ``relative/path`` becomes
    ``<cwd>/relative/path``, and a Linux transcript's ``/home/user/code``
    becomes ``C:\\home\\user\\code``. csb indexes HISTORICAL transcripts
    that may come from other machines, so resolving against this machine's
    state would fabricate work locations. This function is purely lexical:
    no filesystem access, no CWD, no drive assumption.
    """
    if not path:
        return ""
    p = str(path).strip().strip("\"'")
    if not p:
        return ""
    # /c/code/x (Git Bash) or /mnt/c/code/x (WSL) -> C:/code/x
    #
    # AMBIGUITY, accepted knowingly: a single-letter first component is
    # read as a drive. That is correct for Git Bash and WSL, which is how
    # Windows transcripts spell drives -- but on a genuine POSIX host
    # `/e/project` is an ordinary directory named `e`, and this rule would
    # rewrite it to `E:\project`. Single-letter top-level directories are
    # rare on POSIX (and `/mnt/<letter>` is unambiguous), so the trade
    # favors the common case. Revisit if a POSIX run reports it.
    m = re.match(r"^/(?:mnt/)?([a-zA-Z])(/.*)?$", p)
    if m:
        p = f"{m.group(1).upper()}:{m.group(2) or '/'}"
    p = p.replace("/", "\\").rstrip("\\")
    # Collapse `.` and `..` components. Purely lexical -- no filesystem
    # access, no cwd -- so it stays safe for foreign transcripts. Without it
    # one real folder splits across spellings and the credit splits with it:
    # `MoonshotAI\..\DeepSeek` was ranked separately from `DeepSeek`.
    #
    # ntpath, not os.path: our canonical form is backslash-separated, and
    # os.path.normpath on a POSIX host would not treat `\` as a separator at
    # all, silently leaving `..` in place everywhere except Windows.
    if p:
        collapsed = ntpath.normpath(p)
        if collapsed != ".":            # normpath("") -> "."
            p = collapsed
    if re.match(r"^[a-zA-Z]:$", p):        # `C:` alone -> keep the root form
        return p
    if re.match(r"^[a-zA-Z]:", p):         # normalize drive-letter case
        p = p[0].upper() + p[1:]
    return p


def is_plausible_folder(path: str) -> bool:
    """Reject everything that looks like a path but is not a place.

    Devices, bare drive roots, rootless fragments, and shell
    metacharacters. Deliberately conservative: see the module docstring on
    why precision beats recall here.
    """
    if not path:
        return False
    if _METACHARS.search(path):
        return False                        # rule 4: glob / unexpanded var
    if is_device_path(path):
        return False                        # rule 3: /dev/null, NUL, CON...
    if re.fullmatch(r"[A-Za-z]:", path):
        return False                        # a drive, not a folder
    # NOTE: there is deliberately NO "rejects a single leading backslash"
    # rule here. An earlier revision had one, to kill fragments like
    # `\claude\commits` produced by matching INSIDE `private/claude/
    # commits` -- but the anchored regexes now prevent those at the source
    # (measured: zero fragments), and after normalization a genuine POSIX
    # absolute path is spelled exactly the same way (`/home/user/code` ->
    # `\home\user\code`). Keeping the rule made the harvester reject every
    # POSIX path, i.e. produce nothing at all on Linux and macOS.
    # rule 5: empty interior components. Source code embedded in a command
    # carries escaped literals -- `"C:\\\\.dzlp\\\\7"` in Python source is
    # four literal backslashes in the command text, which normalize into
    # empty path components. Real paths never have them (a leading `\\` is
    # UNC and is allowed).
    body = path[2:] if re.match(r"^[A-Za-z]:", path) else path.lstrip("\\")
    if "\\\\" in body:
        return False
    # rule 6: a colon past the drive spec. Real paths have at most one, in
    # `C:`. More than that means the token is something else that merely
    # contains a path -- a colon-joined `PYTHONPATH=/a:/b:/c` list collapsed
    # into one string, a pytest node id (`test_x.py::TestClass`), or prose
    # from a diagnostic `echo`. All three were found in a real index.
    if ":" in body:
        return False
    # rule 7: an unexpanded home marker. `~` cannot be resolved for a
    # transcript that may have come from another machine or another user, and
    # resolving it against THIS machine would fabricate a location. Measured:
    # a bare `~` had accumulated 26 work units, duplicating a path that was
    # also stored correctly expanded.
    #
    # Only the leading form is rejected -- `~` inside a component is a
    # Windows 8.3 short name (`PROGRA~1`), which is a real path.
    if path == "~" or path.startswith("~\\") or path.startswith("~/"):
        return False
    return True


#: Conventional filenames that carry no extension.
#:
#: Only consulted when the filesystem cannot answer -- which for a backup
#: tool is the common case, not the rare one: csb indexes history precisely
#: because files get deleted, renamed, or were never on this machine. A real
#: index had ``C:\\code\\dazzlecmd\\VERSION`` recorded as a working directory
#: for exactly this reason: the repo no longer has that file, so the probe
#: returned "nonexistent" and the extension guess saw a plain word.
#:
#: Matched case-insensitively against the final component only. Directories
#: with these names are vanishingly rare; files with them are everywhere.
EXTENSIONLESS_FILES = frozenset({
    "version", "license", "licence", "readme", "changelog", "authors",
    "notice", "copying", "contributing", "manifest", "codeowners",
    "makefile", "dockerfile", "vagrantfile", "rakefile", "gemfile",
    "procfile", "jenkinsfile", "brewfile", "justfile",
    # git hooks -- these show up constantly in transcripts
    "pre-commit", "post-commit", "pre-push", "pre-rebase", "commit-msg",
    "prepare-commit-msg", "post-merge", "post-checkout", "applypatch-msg",
})


def _looks_like_file(candidate: str) -> bool:
    """Guess whether a candidate names a file rather than a directory.

    A GUESS, used only when :func:`_folder_of` cannot get an answer from the
    filesystem. Two signals: a trailing extension, or a conventional
    extensionless filename (see :data:`EXTENSIONLESS_FILES`).
    """
    if re.search(r"\.[A-Za-z0-9]{1,6}$", candidate):
        return True
    final = re.split(r"[\\/]", candidate)[-1]
    return final.lower() in EXTENSIONLESS_FILES


#: Relative path-ish tokens (`tests/foo.py`, `private/claude/x.md`, `docs/`).
#: Deliberately loose -- ambiguity is resolved by the existence gate in
#: :func:`resolve_relative_paths`, not by the pattern.
REL_PATH = re.compile(
    _BOUNDARY + r"((?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]*)"
)


def extract_paths_from_command(command: str) -> list[str]:
    """Absolute paths mentioned in a shell command, in order of appearance."""
    if not command:
        return []
    return WIN_PATH.findall(command) + POSIX_PATH.findall(command)


def resolve_relative_paths(command: str, base: str) -> list[str]:
    """Relative path tokens resolved against ``base``, existence-gated.

    ``cd <repo> && pytest tests/test_x.py`` is the dominant shell idiom --
    measured, 74% of commands containing a ``cd`` also name a relative
    path -- and ignoring those attributes ALL the work to the repo root,
    hiding the subfolder where it actually happened.

    Relative tokens are ambiguous in a way absolute paths are not: a bare
    ``origin/main`` is a git ref, ``s/foo/bar/`` is a sed expression, and
    both look exactly like ``tests/one-offs``. So this resolver -- and
    ONLY this resolver -- gates on the folder existing.

    That is a different question from the existence *hint* recorded on
    absolute paths. Here it disambiguates **"is this a path at all?"**;
    there it would wrongly discard **"did this path survive?"**, which is
    real history (a deleted repo is still where work happened). Measured
    on a real transcript, the gate separated every genuine subfolder
    (``tests``, ``docs``, ``private\\claude\\commits``) from every
    artifact (``origin\\main``, ``s``, ``origin``) with no misses.
    """
    if not command or not base:
        return []
    out: list[str] = []
    for token in REL_PATH.findall(command):
        if token.startswith("/") or ":" in token:
            continue                               # absolute; handled above
        candidate = token.rstrip("/")
        if _looks_like_file(candidate):
            candidate = "/".join(candidate.split("/")[:-1])
        if not candidate or candidate in (".", ".."):
            continue
        resolved = normalize(base + "\\" + candidate.replace("/", "\\"))
        if not (resolved and is_plausible_folder(resolved)):
            continue
        if folder_exists(resolved) and resolved not in out:
            out.append(resolved)
    return out


def _folder_of(candidate: str) -> str:
    """Resolve a raw candidate to a FOLDER (dirname it if it names a file).

    Asks the filesystem first, via ``dazzle_filekit.classify_fs_object``, and
    only falls back to the trailing-extension guess when the path cannot be
    classified. The guess alone is badly wrong in both directions: it misses
    every extensionless file (``VERSION``, ``Makefile``, a git ``pre-commit``
    hook) and it would misread a directory that happens to carry a dotted
    suffix. Measured on a real index, 38 stored "folders" were files on disk
    and **30 of them carried credited work** -- ``dazzlesum.py`` was ranked a
    working directory with 36 units.

    The filesystem is authoritative only when it answers. A historical
    transcript routinely names paths that no longer exist, or that never
    existed on this machine, and those must still resolve sensibly -- hence
    the lexical fallback rather than a hard requirement to exist.
    """
    cand = candidate.rstrip("\\/")
    if not cand:
        return ""
    # NORMALIZE BEFORE PROBING. The candidate arrives in whatever spelling
    # the command used, and Windows cannot resolve the Git Bash / WSL forms:
    # `Path("/d/M/.gitignore").exists()` is False on Windows even when
    # `D:\M\.gitignore` is right there. Probing the raw form therefore
    # answered "nonexistent" for every drive-mapped POSIX path and fell
    # through to the lexical guess -- which is exactly where dotfiles slip,
    # since `.gitignore` has no extension the guess recognizes and cannot be
    # listed as a known filename either (`.git`, `.venv`, `.github` are
    # directories with the same shape).
    cand = normalize(cand) or cand
    try:
        kind = classify_fs_object(cand)
    except (OSError, ValueError):        # unreachable share, bad name
        kind = "unknown"
    if kind == "file":
        return os.path.dirname(cand)
    if kind == "directory":
        return cand
    # nonexistent / symlink / unknown -> fall back to the lexical guess
    return os.path.dirname(cand) if _looks_like_file(cand) else cand


def effective_cwd(tool_name: str, tool_input: dict,
                  cwd: Optional[str] = None) -> Optional[str]:
    """Where this call's shell actually stood: its ``cd`` target, else cwd."""
    if isinstance(tool_input, dict) and tool_name in COMMAND_TOOLS:
        for target in CD_TARGET.findall(tool_input.get("command") or ""):
            key = normalize(_folder_of(target))
            if key and is_plausible_folder(key):
                return key
    key = normalize(cwd or "")
    return key if key and is_plausible_folder(key) else None


def touched_folders_ordered(tool_name: str, tool_input: dict,
                            cwd: Optional[str] = None) -> list[str]:
    """Every folder this call touched, deduped, **in source order**.

    Order matters: it is what makes "the first path mentioned" a
    deterministic primary-resolution rule (see :func:`primary_folder`).

    Includes relative paths resolved against the call's effective cwd --
    without them, ``cd repo && pytest tests/x.py`` credits only ``repo``
    and the subfolder where the work happened disappears.
    """
    if not isinstance(tool_input, dict):
        return []

    raw: list[str] = []
    if tool_name in FILE_PATH_TOOLS:
        fp = tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(fp, str) and fp.strip():
            raw.append(os.path.dirname(fp.strip()) or fp.strip())
    elif tool_name in DIR_PATH_TOOLS:
        p = tool_input.get("path")
        if isinstance(p, str) and p.strip():
            # `path` USUALLY names a directory -- but Grep accepts a single
            # file, and taking the value verbatim stored those files as
            # folders. This was the larger of the two file-as-folder causes:
            # Read/Edit already dirname their input, so only this branch
            # leaked whole file paths into the ranking.
            raw.append(_folder_of(p.strip()))
    elif tool_name in COMMAND_TOOLS:
        cmd = tool_input.get("command")
        if isinstance(cmd, str):
            abs_paths = [_folder_of(c) for c in extract_paths_from_command(cmd)]
            # The `cd` ARGUMENT is itself an absolute path, but it says
            # where the shell STANDS -- not what the call worked on. Left
            # in place it would always sort first and bury the subfolder
            # the command actually operated in (`cd repo && pytest
            # tests/x.py` would forever read as work at the repo root).
            # Drop it from the work candidates; it is re-appended last
            # below, so it still counts as touched and still becomes the
            # primary when nothing more specific exists.
            base = effective_cwd(tool_name, tool_input, cwd)
            if base and abs_paths and normalize(abs_paths[0]) == base:
                abs_paths = abs_paths[1:]
            raw.extend(abs_paths)

    ordered: list[str] = []
    for candidate in raw:
        key = normalize(candidate)
        if key and is_plausible_folder(key) and key not in ordered:
            ordered.append(key)

    # Relative tokens resolved against where the shell stood, then the
    # shell's own position last -- so primary resolution prefers, in
    # order: an explicit absolute target, a resolved relative target, and
    # only then "wherever we happened to be standing".
    if tool_name in COMMAND_TOOLS:
        base = effective_cwd(tool_name, tool_input, cwd)
        if base:
            for resolved in resolve_relative_paths(
                    tool_input.get("command") or "", base):
                if resolved not in ordered:
                    ordered.append(resolved)
            if base not in ordered:
                ordered.append(base)
    return ordered


def touched_folders(tool_name: str, tool_input: dict,
                    cwd: Optional[str] = None) -> set[str]:
    """Every folder this tool call touched (normalized, filtered).

    Presence semantics: this is what lets ``csb scan`` find the session at
    all. A folder that is only ever a *secondary* touch still belongs here
    -- measured, 33% of folders are never any call's primary, and dropping
    them recreates the very bug #56 is about.
    """
    return set(touched_folders_ordered(tool_name, tool_input, cwd))


def primary_folder(tool_name: str, tool_input: dict,
                   cwd: Optional[str] = None) -> Optional[str]:
    """The ONE folder this call did its work in.

    Resolution order, **most specific first**:

    1. the structured path of a file/dir tool (``Read``/``Edit``/``Glob``)
    2. the first absolute path written in the command -- explicit intent
    3. a relative path resolved against the shell's position, e.g.
       ``cd repo && pytest tests/x.py`` -> ``repo\\tests``
    4. the effective cwd -- the ``cd`` target if there was one, else the
       session cwd. Reached when the call named no path at all
       (``cd repo && git status``), which genuinely happened *there*.

    Note what is deliberately NOT first: the ``cd`` target. A command that
    moves into a repository and then works on ``tests/x.py`` did its work
    in ``tests``, not at the root -- crediting the root buries every
    subfolder under one big number, which is exactly the flattening this
    issue set out to remove. The ``cd`` target still supplies the base for
    resolving those relative paths, and still wins when nothing more
    specific exists.

    Exactly one unit of work per call, so totals stay comparable to the
    session's tool-call count and the launch directory cannot win by
    accumulating one tick per transcript *event*.
    """
    ordered = touched_folders_ordered(tool_name, tool_input, cwd)
    if ordered:
        return ordered[0]
    return effective_cwd(tool_name, tool_input, cwd)


def looks_like_scratch(path: str) -> bool:
    """Whether a folder is scratch space (temp dirs, caches, build junk).

    Used for DISPLAY filtering only -- these folders are still recorded and
    still findable by an explicit ``csb scan``, because a heuristic should
    never make data unreachable. Keeping the classification out of the
    index also means retuning this list costs nothing (no re-index).

    Component-exact so a real project like ``C:\\code\\tmpl-project`` or
    ``C:\\code\\cache-warmer`` is NOT misclassified.
    """
    if not path:
        return False
    for part in re.split(r"[\\/]", path):
        if part.lower() in SCRATCH_COMPONENTS:
            return True
    return False


def folder_exists(path: str) -> Optional[bool]:
    """Best-effort existence probe -- a CONFIDENCE HINT, never a filter.

    Returns True/False, or **None** when the probe itself failed (an
    unreachable network path, a permissions error). None means "unknown"
    and must never be rendered as "missing": a folder that was genuinely
    worked in and later deleted is real history, and the transcript is a
    historical record.

    Guarded because a stat against an unmounted network drive can stall.
    """
    if not path:
        return None
    try:
        return os.path.isdir(path)
    except (OSError, ValueError):
        return None

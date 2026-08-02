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

#: `cd` / `pushd` target -- the shell literally moved there, which is the
#: FACT the primary ranking leans on.
#:
#: Quote-aware for the same reason QUOTED_WIN_PATH exists: a quoted target
#: may contain spaces, and slicing at the first whitespace fabricated a
#: fragment (`cd "C:\\Program Files\\App"` -> base `C:\\Program`) that then
#: WON primary as a "corroborated fact" while the real target failed the
#: prefix test against the wrong base. The general extractor was fixed
#: first and this parallel parser was missed -- caught by the third
#: adversarial pass.
CD_TARGET = re.compile(
    r"(?:^|[\s;&|(])(?:cd|pushd)\s+(?:/d\s+)?"
    r"(?:\"([^\"\n]+)\"|'([^'\n]+)'|([^\s\"'&|;]+))",
    re.IGNORECASE,
)

#: Shell metacharacters. A candidate containing any of these is a glob, an
#: unexpanded variable, or (backtick) a PowerShell escape / POSIX command
#: substitution absorbed into the match -- a query or a placeholder, never a
#: location. A real index carried ``C:\\Users\\Extreme` `` with a work unit.
#: (An 8.3 short name like ``PROGRA~1`` uses ``~``, not a backtick, and is
#: unaffected.)
_METACHARS = re.compile(r"[*?$%{}\[\]`]")

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
    # rule 8: an all-dots component (3+). Found as `C:\Users\...` -- prose
    # using "..." as et-cetera inside a command -- and dangerous precisely
    # because it DEFEATED the existence safety net: Win32 strips trailing
    # dots, so the probe resolved it to the real C:\Users and stamped it
    # verified-existing. No distinct directory can be addressed by an
    # all-dots name on Windows, so it is rejected before any probe.
    # (`.` and `..` never reach here -- normalize collapses them.)
    if re.search(r"(^|\\)\.{3,}(\\|$)", path):
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


#: Dot-names that are conventionally DIRECTORIES. The complement of this
#: list -- any other leading-dot final component -- defaults to FILE, since
#: dotfiles (.gitignore, .prettierrc, .npmrc, ...) vastly outnumber dot-dirs
#: in practice, and the two errors are not symmetric: a wrong "file"
#: mis-credits by one level (the parent), while a wrong "folder" fabricates
#: a place users see in rankings.
DOT_DIRECTORY_NAMES = frozenset({
    ".git", ".github", ".venv", ".vscode", ".idea", ".claude", ".ssh",
    ".config", ".cache", ".local", ".tox", ".mypy_cache", ".pytest_cache",
    ".gradle", ".m2", ".npm", ".cargo", ".rustup", ".docker", ".aws",
    ".azure", ".terraform", ".next", ".nuxt", ".svn", ".hg",
    ".devcontainer", ".vs", ".dzlp",
})


def _looks_like_file(candidate: str) -> bool:
    """Guess whether a candidate names a file rather than a directory.

    A GUESS, used only when :func:`_folder_of` cannot get an answer from the
    filesystem -- which for a backup tool is the COMMON case, since indexed
    transcripts routinely name paths that were deleted or never existed on
    this machine.

    Dot-names are decided FIRST, by :data:`DOT_DIRECTORY_NAMES`, never by
    the extension regex: ``.gitignore`` (9 letters) exceeds the extension
    cap and read as a folder, while ``.git`` FITS the cap and read as a
    file -- foreign transcripts got both directions wrong. After that: a
    trailing extension, or a conventional extensionless filename.
    """
    final = re.split(r"[\\/]", candidate)[-1]
    if final.startswith(".") and final not in (".", ".."):
        return final.lower() not in DOT_DIRECTORY_NAMES
    if re.search(r"\.[A-Za-z0-9]{1,6}$", candidate):
        return True
    return final.lower() in EXTENSIONLESS_FILES


#: Relative path-ish tokens (`tests/foo.py`, `private/claude/x.md`, `docs/`).
#: Deliberately loose -- ambiguity is resolved by the existence gate in
#: :func:`resolve_relative_paths`, not by the pattern.
REL_PATH = re.compile(
    _BOUNDARY + r"((?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]*)"
)


#: A QUOTED path may contain spaces AND commas -- the quote is the boundary
#: the bare patterns lack (space is a hard stop; comma is a deliberate
#: prose-list boundary, so `see C:\\a, C:\\b` never glues). The first cut of
#: this honored quotes for the DRIVE-LETTER form only; quoted Git-Bash/WSL
#: and POSIX spellings fell through to the bare pattern, and
#: `"/d/M/Software/_DeDRM, Proxies, ..."` truncated at the comma -- 14
#: credited work units on a fabricated fragment, found by the user in a
#: real session (the THIRD fix-one-call-site-miss-the-parallel instance of
#: this effort). A quote now protects all three spellings alike.
_ABS_HEAD = (
    r"[A-Za-z]:[\\/]"                  # C:\ and C:/
    r"|/(?:mnt/)?[A-Za-z]/"            # /c/... and /mnt/c/...
    r"|/(?:" + _POSIX_ROOTS + r")/"    # allowlisted POSIX roots
)
QUOTED_ABS_PATH = re.compile(
    r'"((?:' + _ABS_HEAD + r')[^"\n|<>]*)"'
    r"|'((?:" + _ABS_HEAD + r")[^'\n|<>]*)'"
)

#: Quoted RELATIVE token (contains a separator, no drive/root anchor).
#: Feeds resolve_relative_paths, where the EXISTENCE GATE disambiguates --
#: so commas and spaces inside quotes are safe by construction there too:
#: `cd /d/M && python "Software/_DeDRM, .../x.py"` resolves the real deep
#: folder instead of stopping at `Software`.
QUOTED_REL_TOKEN = re.compile(
    r'"([^"\n:|<>*?]*?/[^"\n:|<>*?]+)"'
    r"|'([^'\n:|<>*?]*?/[^'\n:|<>*?]+)'"
)


def extract_paths_from_command(command: str) -> list[str]:
    """Absolute paths mentioned in a shell command, in order of appearance.

    ORDER IS A CONTRACT: primary resolution is built on "the first path
    mentioned". An earlier implementation concatenated the Windows-form
    matches ahead of the POSIX-form matches, so ``/c/a`` at character 3
    sorted after a Windows-spelled literal 80 characters later -- while
    this docstring claimed source order. All patterns now merge on match
    position; overlapping spans dedupe to the earliest-starting, longest
    match, so the quoted form wins over the bare fragment it contains.
    """
    if not command:
        return []
    cands: list[tuple[int, int, str]] = []
    for m in QUOTED_ABS_PATH.finditer(command):
        text = m.group(1) or m.group(2)
        if text and text.strip():
            cands.append((m.start(), m.end(), text.strip()))
    for rx in (WIN_PATH, POSIX_PATH):
        for m in rx.finditer(command):
            cands.append((m.start(1), m.end(1), m.group(1)))
    cands.sort(key=lambda t: (t[0], -t[1]))
    out: list[str] = []
    covered: list[tuple[int, int]] = []
    for start, end, text in cands:
        if any(start >= cs and end <= ce for cs, ce in covered):
            continue                       # bare fragment inside a quoted span
        covered.append((start, end))
        out.append(text)
    return out


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
    tokens: list[str] = list(REL_PATH.findall(command))
    # Quoted relative tokens carry spaces and commas the bare REL pattern
    # cannot -- the existence gate below disambiguates them exactly as it
    # does every other relative token.
    for m in QUOTED_REL_TOKEN.finditer(command):
        tokens.append((m.group(1) or m.group(2) or "").strip())
    for token in tokens:
        if not token or token.startswith(("/", "\\")) or ":" in token:
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


def _effective_base(tool_name: str, tool_input: dict,
                    cwd: Optional[str] = None
                    ) -> tuple[Optional[str], bool]:
    """``(base, from_cd)`` -- where the shell stood, and whether an
    explicit ``cd``/``pushd`` put it there.

    The boolean matters because an explicit move is a *fact* the ranking
    can lean on (see :func:`touched_folders_ordered`), while the ambient
    session cwd is not.

    A RELATIVE target resolves lexically against the incoming cwd --
    ``cd ..`` is the parent, ``cd foo`` is ``cwd\\foo`` -- because taking
    the token at face value emitted bare words as folders: a literal
    ``-`` carried work credit in three real sessions, from the common
    ``cd "$BR"; ...; cd - >/dev/null`` loop idiom. ``cd -`` ("previous
    directory") is unknowable from the transcript and is discarded, as is
    any relative target when no cwd exists to resolve it. No ``_folder_of``
    on cd targets: a cd target is a directory by definition, and the
    file-guess could wrongly dirname a dotted directory name.

    Known simplification: multiple ``cd``s in one command resolve to the
    FIRST plausible target; a real shell ends at the last.
    """
    cwd_key = normalize(cwd or "")
    if not (cwd_key and is_plausible_folder(cwd_key)):
        cwd_key = None
    if isinstance(tool_input, dict) and tool_name in COMMAND_TOOLS:
        for m in CD_TARGET.finditer(tool_input.get("command") or ""):
            # Group 1/2: quoted target (may contain spaces); group 3: bare.
            t = (m.group(1) or m.group(2) or m.group(3) or "").strip()
            if not t or t == "-":
                continue                     # unknowable: previous directory
            if re.match(r"^[A-Za-z]:", t) or t.startswith(("/", "\\", "~")):
                cand = t                     # absolute-form (~ dies at rule 7)
            elif cwd_key:
                cand = cwd_key + "\\" + t.replace("/", "\\")
            else:
                continue                     # relative with nothing to anchor
            key = normalize(cand)
            if key and is_plausible_folder(key):
                return key, True
    return cwd_key, False


def effective_cwd(tool_name: str, tool_input: dict,
                  cwd: Optional[str] = None) -> Optional[str]:
    """Where this call's shell actually stood: its ``cd`` target, else cwd."""
    return _effective_base(tool_name, tool_input, cwd)[0]


def _ordered_with_provenance(tool_name: str, tool_input: dict,
                              cwd: Optional[str] = None
                              ) -> list[tuple[str, str]]:
    """(folder, provenance) pairs, deduped, in ranking order.

    THE single source of truth for both presence and provenance --
    ``touched_folders_ordered`` and ``touched_with_provenance`` are
    derivations, so the two views cannot drift.

    Provenance vocabulary (schema v8), coldest first:

      ``cd``         -- the shell stood there: an explicit cd/pushd target
                        OR the ambient session cwd used as a base. Both are
                        facts of position; the pre-#56 behavior the `cd`
                        rung reconstructs was cwd-based, so both qualify.
      ``structured`` -- a file/dir tool's own path argument.
      ``relative``   -- a relative token resolved against the base,
                        existence-gated at resolution.
      ``extracted``  -- an absolute path lifted out of command text
                        (operand or literal, undecidable).
    """
    if not isinstance(tool_input, dict):
        return []

    if tool_name in FILE_PATH_TOOLS:
        fp = tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(fp, str) and fp.strip():
            cand = os.path.dirname(fp.strip()) or fp.strip()
            key = normalize(cand)
            if key and is_plausible_folder(key):
                return [(key, "structured")]
        return []

    if tool_name in DIR_PATH_TOOLS:
        p = tool_input.get("path")
        if isinstance(p, str) and p.strip():
            # `path` USUALLY names a directory -- but Grep accepts a single
            # file, and taking the value verbatim stored those files as
            # folders (measured: 38 rows, 30 credited).
            key = normalize(_folder_of(p.strip()))
            if key and is_plausible_folder(key):
                return [(key, "structured")]
        return []

    if tool_name in COMMAND_TOOLS:
        cmd = tool_input.get("command")
        if not isinstance(cmd, str):
            return []
        abs_keys: list[str] = []
        for c in extract_paths_from_command(cmd):
            k = normalize(_folder_of(c))
            if k and is_plausible_folder(k) and k not in abs_keys:
                abs_keys.append(k)
        base, from_cd = _effective_base(tool_name, tool_input, cwd)
        if base and base in abs_keys:
            # The base is handled by rank below, never as a work
            # candidate in the abs list.
            abs_keys.remove(base)
        rel = resolve_relative_paths(cmd, base) if base else []

        # Rank by CERTAINTY of evidence, not syntax of mention.
        #
        # An explicit cd is a FACT: the shell demonstrably moved there.
        # An absolute path written in the command text is a GUESS:
        # operand or string literal, indistinguishable without executing
        # the command. So when a cd is present, only paths the move
        # CORROBORATES (inside the target's subtree, or resolved relative
        # to it) may outrank the target itself; everything else demotes
        # to presence. Measured on 4,532 real commands before adoption --
        # the decisive case was a literal inside sed replacement text
        # that happens to EXIST on disk, which neither payload parsing
        # nor an existence rule could catch.
        #
        # Without a cd the ambient cwd is NOT promoted the same way:
        # doing so re-flattens work onto the launch directory (928
        # measured commands), which is the original #56 bug. Ranking
        # there stays: explicit absolutes, resolved relatives, cwd.
        if from_cd and base:
            prefix = base + "\\"
            under = [k for k in abs_keys if k.startswith(prefix)]
            elsewhere = [k for k in abs_keys if not k.startswith(prefix)]
            seq = ([(k, "extracted") for k in under]
                   + [(k, "relative") for k in rel]
                   + [(base, "cd")]
                   + [(k, "extracted") for k in elsewhere])
        else:
            seq = ([(k, "extracted") for k in abs_keys]
                   + [(k, "relative") for k in rel]
                   + ([(base, "cd")] if base else []))
        ordered: list[tuple[str, str]] = []
        seen: dict = {}
        for k, prov in seq:
            if k not in seen:
                seen[k] = prov
                ordered.append((k, prov))
        return ordered

    return []


def touched_folders_ordered(tool_name: str, tool_input: dict,
                            cwd: Optional[str] = None) -> list[str]:
    """Every folder this call touched, deduped, **in ranking order**.

    Order matters: it is what makes "the first entry" a deterministic
    primary-resolution rule (see :func:`primary_folder`).
    """
    return [k for k, _ in
            _ordered_with_provenance(tool_name, tool_input, cwd)]


def touched_with_provenance(tool_name: str, tool_input: dict,
                            cwd: Optional[str] = None) -> dict:
    """``{folder: provenance}`` for one call (schema v8 stamping)."""
    return dict(_ordered_with_provenance(tool_name, tool_input, cwd))


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

    Ranked by **certainty of evidence, not syntax of mention** (0.7.2).

    With an explicit ``cd``/``pushd`` -- a *fact*, the shell demonstrably
    moved there:

    1. the structured path of a file/dir tool (``Read``/``Edit``/``Glob``)
    2. absolute paths **under** the cd target -- corroborated by the move,
       so subfolder precision survives
    3. relative paths resolved against the target
       (``cd repo && pytest tests/x.py`` -> ``repo\\tests``)
    4. the cd target itself
    5. absolute paths elsewhere -- uncorroborated *guesses* (operand or
       string literal, indistinguishable); presence only

    Without a cd, ranking is unchanged from #56: explicit absolutes, then
    resolved relatives, then the session cwd. The ambient cwd is NOT
    promoted the way a cd target is -- doing so re-flattens work onto the
    launch directory, which is the original bug.

    Existence plays no role in any of this: a deleted repo named as a
    plain operand still takes the credit, because the transcript is a
    historical record.

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

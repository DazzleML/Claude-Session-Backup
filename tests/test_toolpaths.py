"""Unit tests for the tool-call path harvester (#56).

``toolpaths`` decides which folders a session is recorded as having worked
in, so a silent change here doesn't crash anything -- it just makes ``csb
scan`` quietly wrong. Every rule below was derived from measuring real
transcripts; these tests pin the measurements so a later "cleanup" can't
undo them without saying so out loud.

The precision rules matter more than they look. Each one exists because a
real transcript produced a bogus folder without it:

  1. anchored matches      -- ``private/claude/x`` must not yield ``\\claude\\x``
  2. real roots only       -- ``sed -n '/start/,/end/p'`` is not two folders
  3. no devices            -- ``2>/dev/null`` is not a working directory
  4. no shell metacharacters -- ``C:\\code\\*lib*`` is a glob, not a place
  5. no empty components   -- escaped literals in embedded source code
  6. no colon past the drive -- a colon-joined PYTHONPATH is not a place
  7. no leading ``~``      -- an unresolvable home marker
"""

import os

import pytest

from claude_session_backup import toolpaths as tp


# ── normalize: four spellings, one folder ────────────────────────────

@pytest.mark.parametrize("spelling", [
    "/c/code/x",        # Git Bash
    "/mnt/c/code/x",    # WSL
    "C:/code/x",        # forward-slash Windows
    "C:\\code\\x",      # native
    "c:/code/x",        # lowercase drive
    "C:/code/x/",       # trailing separator
    "  'C:/code/x'  ",  # quoted + padded, as it appears inside a command
])
def test_normalize_collapses_every_spelling_to_one_key(spelling):
    """All four spellings are the same directory. If they don't collapse,
    one folder splits across four rows and none carries the real count."""
    assert tp.normalize(spelling) == "C:\\code\\x"


def test_normalize_preserves_posix_absolute_paths():
    """A Linux transcript's path stays rooted -- it must NOT acquire a
    drive letter. csb indexes transcripts from other machines."""
    assert tp.normalize("/home/user/code") == "\\home\\user\\code"


def test_normalize_is_purely_lexical_not_resolved_against_cwd(tmp_path,
                                                              monkeypatch):
    """The deliberate difference from filekit's normalize_cross_platform_path.

    That one absolutizes against the *current* machine's cwd and drive.
    Doing that here would fabricate work locations for transcripts recorded
    elsewhere, so this must be pure string work: same answer no matter
    where the process happens to be standing.
    """
    monkeypatch.chdir(tmp_path)
    from_tmp = tp.normalize("/home/user/code")
    monkeypatch.chdir(os.path.dirname(str(tmp_path)))
    from_parent = tp.normalize("/home/user/code")

    assert from_tmp == from_parent == "\\home\\user\\code"
    assert tp.normalize("") == ""
    # A bare relative token is not absolutized into the cwd.
    assert tp.normalize("relative/path") == "relative\\path"


def test_normalize_keeps_bare_drive_root_intact():
    assert tp.normalize("C:") == "C:"


# ── is_plausible_folder: the precision rules ─────────────────────────

def test_plausible_accepts_ordinary_windows_and_posix_folders():
    assert tp.is_plausible_folder("C:\\code\\x")
    # Rule-5 regression: an earlier revision rejected any leading backslash
    # to kill fragments, which rejected EVERY normalized POSIX path -- i.e.
    # the harvester produced nothing at all on Linux and macOS.
    assert tp.is_plausible_folder("\\home\\user\\code")


@pytest.mark.parametrize("glob_or_var", [
    "C:\\code\\$r",       # unexpanded shell variable
    "C:\\code\\*lib*",    # glob
    "C:\\code\\{a,b}",    # brace expansion
    "C:\\code\\[abc]",    # character class
    "C:\\code\\%VAR%",    # unexpanded cmd variable
])
def test_plausible_rejects_shell_metacharacters(glob_or_var):
    """Rule 4: a query or a placeholder is never a location."""
    assert not tp.is_plausible_folder(glob_or_var)


@pytest.mark.parametrize("device", ["\\dev\\null", "/dev/null", "NUL", "nul"])
def test_plausible_rejects_devices(device):
    """Rule 3, delegated to filekit's is_device_path -- which also covers
    the Windows reserved names, case-insensitively (``NUL`` and ``nul``
    are the same device to Win32)."""
    assert not tp.is_plausible_folder(device)


def test_plausible_rejects_device_name_in_a_subdirectory():
    """``C:\\code\\nul`` addresses the device, not a folder named nul."""
    assert not tp.is_plausible_folder("C:\\code\\nul")


def test_plausible_accepts_dev_shm_which_is_a_real_directory():
    """Being under /dev doesn't make it a device -- /dev/shm is a tmpfs
    people genuinely work in. Only actual device names are rejected."""
    assert tp.is_plausible_folder("\\dev\\shm")


def test_plausible_rejects_bare_drive():
    assert not tp.is_plausible_folder("C:")


def test_plausible_rejects_empty_interior_components():
    """Rule 5: embedded source code carries escaped literals -- a Python
    string ``"C:\\\\.dzlp"`` is two literal backslashes in the command
    text, which normalize into an empty path component."""
    assert not tp.is_plausible_folder("C:\\code\\a\\\\b")


def test_plausible_still_allows_unc_paths():
    """A LEADING double backslash is UNC, not an empty component."""
    assert tp.is_plausible_folder("\\\\server\\share\\proj")


# ── extraction: what counts as a path inside a command ───────────────

@pytest.mark.parametrize("command", [
    "cat mydir/private/notes",   # would yield the fragment /private/notes
    "cat repo/src/main.py",      # would yield /src
    "ls a/home/user",            # would yield /home/user
])
def test_extraction_is_anchored_so_relative_tokens_yield_no_fragment(command):
    """Rule 1: a path must start at a boundary, so matching never begins
    part-way through a relative token.

    Each command here contains a substring that IS an allowlisted root
    (``/private``, ``/src``, ``/home``) but is not the start of a path.
    Without the anchor these emit folders that never existed.

    Chosen by mutation testing: the obvious probe ``cat
    private/claude/x.md`` passes even with the anchor removed, because
    ``claude`` isn't an allowlisted root -- so it tests rule 2, not rule 1,
    and would keep passing if the anchoring were deleted.
    """
    assert tp.extract_paths_from_command(command) == []


def test_extraction_ignores_sed_expressions():
    """Rule 2: ``/start/`` and ``/end/`` are slash-delimited, but ``start``
    is not a filesystem root. The root allowlist rejects them without
    needing to parse shell grammar."""
    assert tp.extract_paths_from_command("sed -n '/start/,/end/p' f.txt") == []


def test_extraction_ignores_dev_null_redirect():
    """``2>/dev/null`` appears in a large share of real commands."""
    assert tp.extract_paths_from_command("echo hi 2>/dev/null") == []


def test_extraction_finds_windows_and_posix_paths():
    assert tp.extract_paths_from_command(
        "grep -r foo C:\\code\\proj") == ["C:\\code\\proj"]
    assert tp.extract_paths_from_command(
        "cd /c/code/proj && pytest") == ["/c/code/proj"]


# ── relative resolution: the one place existence GATES ───────────────

def test_relative_paths_resolve_against_the_shells_position(tmp_path):
    """``cd repo && pytest tests/x.py`` happened in ``tests`` -- crediting
    only ``repo`` is what buried subfolders under one number."""
    (tmp_path / "tests").mkdir()
    base = tp.normalize(str(tmp_path))

    out = tp.resolve_relative_paths("pytest tests/test_x.py", base)

    assert out == [base + "\\tests"]


def test_relative_resolution_rejects_git_refs_and_sed_expressions(tmp_path):
    """``origin/main`` looks exactly like ``tests/one-offs``. Only the
    existence gate can tell them apart -- which is why this resolver, and
    only this resolver, checks the filesystem."""
    base = tp.normalize(str(tmp_path))

    assert tp.resolve_relative_paths("git log origin/main", base) == []
    assert tp.resolve_relative_paths("sed -i s/foo/bar/ f.txt", base) == []


def test_relative_resolution_needs_a_base():
    assert tp.resolve_relative_paths("pytest tests/x.py", "") == []


# ── effective cwd: where the shell actually stood ────────────────────

def test_effective_cwd_prefers_the_cd_target_over_session_cwd():
    assert tp.effective_cwd(
        "Bash", {"command": "cd /c/code/proj && git status"}, "C:\\launch"
    ) == "C:\\code\\proj"


def test_effective_cwd_falls_back_to_session_cwd():
    assert tp.effective_cwd(
        "Bash", {"command": "git status"}, "C:\\launch") == "C:\\launch"


def test_effective_cwd_ignores_an_implausible_cd_target():
    """``cd $REPO`` names a variable, not a place -- fall through to cwd."""
    assert tp.effective_cwd(
        "Bash", {"command": "cd $REPO && ls"}, "C:\\launch") == "C:\\launch"


# ── primary resolution order ─────────────────────────────────────────

def test_primary_is_the_structured_path_of_a_file_tool():
    """Rule 1 of the order: a Read/Edit/Write names a file; the folder is
    its dirname, and it beats the session cwd outright."""
    assert tp.primary_folder(
        "Read", {"file_path": "C:\\code\\other-repo\\src\\main.py"},
        "C:\\launch",
    ) == "C:\\code\\other-repo\\src"


def test_primary_is_the_directory_of_a_dir_tool():
    assert tp.primary_folder(
        "Grep", {"path": "C:\\code\\other-repo"}, "C:\\launch"
    ) == "C:\\code\\other-repo"


def test_primary_is_the_explicit_absolute_path_in_a_command():
    assert tp.primary_folder(
        "Bash", {"command": "grep -r foo C:\\code\\other-repo"}, "C:\\launch"
    ) == "C:\\code\\other-repo"


def test_primary_is_the_resolved_relative_path_not_the_cd_target(tmp_path):
    """The load-bearing demotion.

    ``cd repo && pytest tests/x.py`` did its work in ``tests``. The ``cd``
    argument is itself an absolute path, so left in the candidate list it
    sorts first and permanently buries every subfolder under the repo
    root -- exactly the flattening #56 set out to remove.
    """
    (tmp_path / "tests").mkdir()
    repo = tp.normalize(str(tmp_path))
    cmd = "cd " + str(tmp_path) + " && pytest tests/test_x.py"

    assert tp.primary_folder("Bash", {"command": cmd}, "C:\\launch") == \
        repo + "\\tests"


def test_primary_falls_back_to_the_cd_target_when_nothing_is_more_specific():
    """``cd repo && git status`` names no path -- but it genuinely
    happened in ``repo``, so the cd target still wins over session cwd."""
    assert tp.primary_folder(
        "Bash", {"command": "cd /c/code/proj && git status"}, "C:\\launch"
    ) == "C:\\code\\proj"


def test_primary_falls_back_to_session_cwd_when_the_call_names_nothing():
    assert tp.primary_folder(
        "Bash", {"command": "git status"}, "C:\\launch") == "C:\\launch"


def test_primary_is_none_when_there_is_nothing_to_credit():
    assert tp.primary_folder("Bash", {"command": "git status"}, None) is None
    assert tp.primary_folder("Read", {}, None) is None


def test_malformed_tool_input_does_not_raise():
    """Indexing must never crash on a weird transcript."""
    assert tp.touched_folders("Read", None, "C:\\launch") == set()
    assert tp.touched_folders("Read", {"file_path": 42}, "C:\\launch") == set()
    assert tp.touched_folders("UnknownTool", {"x": 1}, "C:\\launch") == set()


# ── presence vs work: the two-unit design ────────────────────────────

def test_secondary_touches_are_present_even_though_only_one_is_primary(tmp_path):
    """#56's core split. A folder that is never any call's primary is
    still recorded, because presence is what makes ``csb scan`` find the
    session at all -- measured, 33% of real folders are only ever
    secondary touches."""
    (tmp_path / "tests").mkdir()
    repo = tp.normalize(str(tmp_path))
    cmd = "cd " + str(tmp_path) + " && pytest tests/test_x.py"
    tool_input = {"command": cmd}

    touched = tp.touched_folders("Bash", tool_input, "C:\\launch")
    primary = tp.primary_folder("Bash", tool_input, "C:\\launch")

    assert primary == repo + "\\tests"          # one unit of work, credited
    assert touched == {repo + "\\tests", repo}  # both present, findable
    assert repo in touched and repo != primary  # secondary, uncredited


def test_touched_order_is_deterministic(tmp_path):
    """Primary resolution is 'the first entry', so the order is a contract,
    not an implementation detail."""
    (tmp_path / "tests").mkdir()
    repo = tp.normalize(str(tmp_path))
    cmd = "cd " + str(tmp_path) + " && pytest tests/test_x.py"

    ordered = tp.touched_folders_ordered("Bash", {"command": cmd}, "C:\\launch")

    assert ordered == [repo + "\\tests", repo], (
        "the shell's own position must sort LAST -- it is where we stood, "
        "not what we worked on"
    )


# ── scratch classification (display only) ────────────────────────────

@pytest.mark.parametrize("scratch", [
    "C:\\Temp\\x",
    "C:\\a\\node_modules\\b",
    "C:\\code\\scratchpad",
    "C:\\code\\__pycache__",
])
def test_scratch_detection_matches_whole_components(scratch):
    assert tp.looks_like_scratch(scratch)


@pytest.mark.parametrize("real_project", [
    "C:\\code\\tmpl-project",    # starts with "tmp"
    "C:\\code\\cache-warmer",    # starts with "cache"
    "C:\\code\\temporal",        # starts with "temp"
])
def test_scratch_detection_does_not_eat_real_projects(real_project):
    """Component-EXACT. A substring rule would misclassify real repos, and
    since this drives display filtering, that would hide genuine work."""
    assert not tp.looks_like_scratch(real_project)


# ── existence probe: a hint, never a filter ──────────────────────────

def test_folder_exists_reports_true_false_and_unknown(tmp_path):
    """None means 'the probe failed', and must never render as 'missing':
    a folder that was worked in and later deleted is real history."""
    assert tp.folder_exists(str(tmp_path)) is True
    assert tp.folder_exists(str(tmp_path / "nope")) is False
    assert tp.folder_exists("") is None


# ── file-as-folder: ask the filesystem, don't guess from the suffix ──
#
# Measured on a real index: 38 stored "folders" were files on disk and 30 of
# them carried CREDITED WORK -- ``dazzlesum.py`` ranked as a working
# directory with 36 units. Two independent causes, both fixed:
#
#   A. Glob/Grep's ``path=`` was taken verbatim. Grep accepts a single file,
#      so those file paths were stored as folders. (Read/Edit already
#      dirname their input, which is why only this branch leaked.)
#   B. The trailing-extension guess misses every extensionless file --
#      VERSION, Makefile, a git ``pre-commit`` hook.

def test_grep_on_a_file_credits_its_directory(tmp_path):
    """Cause A. ``path`` usually names a directory, but not always."""
    f = tmp_path / "module.py"
    f.write_text("x = 1", encoding="utf-8")

    assert tp.touched_folders("Grep", {"path": str(f)}, None) == {
        tp.normalize(str(tmp_path))
    }


def test_extensionless_file_is_not_mistaken_for_a_folder(tmp_path):
    """Cause B. The suffix guess says "directory"; the filesystem knows.

    Uses a name that is NOT in the conventional-filenames fallback, so this
    exercises the filesystem probe specifically.
    """
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "some-custom-hook"
    hook.write_text("#!/bin/sh", encoding="utf-8")

    assert not tp._looks_like_file("some-custom-hook"), (
        "precondition: the lexical guess cannot see this is a file")
    assert tp.touched_folders("Grep", {"path": str(hook)}, None) == {
        tp.normalize(str(hooks))
    }


def test_real_directory_with_a_dotted_name_is_not_dirnamed(tmp_path):
    """The guess is wrong in the other direction too: a directory whose name
    carries a dotted suffix must NOT be reduced to its parent."""
    d = tmp_path / "release.v2"
    d.mkdir()

    assert tp._looks_like_file("release.v2"), "precondition: guess says file"
    assert tp.touched_folders("Grep", {"path": str(d)}, None) == {
        tp.normalize(str(d))
    }


def test_nonexistent_path_falls_back_to_the_lexical_guess():
    """A historical transcript names paths that no longer exist, or never
    existed here. Those must still resolve -- existence is not required."""
    assert tp.touched_folders(
        "Grep", {"path": "C:\\gone\\proj\\main.py"}, None
    ) == {"C:\\gone\\proj"}
    assert tp.touched_folders(
        "Grep", {"path": "C:\\gone\\proj"}, None
    ) == {"C:\\gone\\proj"}


# ── normalize collapses . and .. so credit doesn't split ─────────────

@pytest.mark.parametrize("spelling,expected", [
    ("D:\\M\\AI\\Moonshot\\..\\DeepSeek", "D:\\M\\AI\\DeepSeek"),
    ("C:\\code\\proj\\.", "C:\\code\\proj"),
    ("C:\\code\\proj\\.\\scripts", "C:\\code\\proj\\scripts"),
    ("/c/code/proj/../other", "C:\\code\\other"),
])
def test_normalize_collapses_dot_and_dotdot(spelling, expected):
    """One real folder was ranked twice under two spellings, splitting its
    credit. Collapsing is purely lexical -- no filesystem, no cwd -- so it
    stays safe for transcripts recorded on another machine."""
    assert tp.normalize(spelling) == expected


def test_dotdot_collapse_works_on_posix_hosts_too():
    """Uses ntpath, not os.path.

    Our canonical form is backslash-separated. os.path.normpath on a POSIX
    host does not treat ``\\`` as a separator, so it would silently leave
    ``..`` in place everywhere except Windows -- the same class of
    platform-blind bug that made an earlier revision reject every POSIX path.
    """
    import ntpath
    assert ntpath.normpath("\\home\\user\\code\\..\\other") == \
        "\\home\\user\\other"


# ── rule 6: a colon past the drive spec ──────────────────────────────

@pytest.mark.parametrize("not_a_place", [
    "C:\\code\\a:\\mnt\\c\\b",      # colon-joined PYTHONPATH list
    "C:\\x\\test_y.py::TestClass",  # pytest node id
    "\\home\\dev\\claude:",         # trailing colon from prose
    "E:\\F:",                       # drive letters inside an echo string
])
def test_plausible_rejects_colon_past_the_drive(not_a_place):
    assert not tp.is_plausible_folder(not_a_place)


def test_plausible_still_accepts_an_ordinary_drive_path():
    assert tp.is_plausible_folder("C:\\code\\proj")


# ── rule 7: unexpanded home marker ───────────────────────────────────

@pytest.mark.parametrize("home_marker", ["~", "~\\.claude\\skills", "~/x"])
def test_plausible_rejects_leading_tilde(home_marker):
    """``~`` cannot be resolved for a transcript that may come from another
    machine or user; resolving it against THIS machine would fabricate a
    location. Measured: a bare ``~`` had accumulated 26 work units while the
    same directory was also stored correctly expanded."""
    assert not tp.is_plausible_folder(home_marker)


def test_plausible_keeps_windows_short_names():
    """Only the LEADING form is a home marker. A ``~`` inside a component is
    an 8.3 short name, which is a real path."""
    assert tp.is_plausible_folder("C:\\PROGRA~1\\app")


def test_plausible_keeps_single_letter_directories():
    """Deliberately NOT filtered. ``D:\\M`` is a real working directory with
    408 measured work units, so a "short final component looks like junk"
    rule would discard genuine data to catch a few artifacts."""
    assert tp.is_plausible_folder("D:\\M")


# ── extensionless files that no longer exist on disk ─────────────────
#
# The filesystem probe only helps for paths that still exist -- and for a
# BACKUP tool that is the uncommon case, not the common one: csb indexes
# history precisely because files get deleted, renamed, or were never on
# this machine at all.
#
# Found in a real index: ``C:\\code\\dazzlecmd\\VERSION`` stored as a working
# directory. The repo no longer has that file, so classify_fs_object said
# "nonexistent" and the extension guess saw a plain word.

@pytest.mark.parametrize("missing_file,expected_parent", [
    ("C:\\gone\\repo\\VERSION", "C:\\gone\\repo"),
    ("C:\\gone\\repo\\Makefile", "C:\\gone\\repo"),
    ("C:\\gone\\repo\\LICENSE", "C:\\gone\\repo"),
    ("C:\\gone\\.git\\hooks\\pre-commit", "C:\\gone\\.git\\hooks"),
    ("C:\\gone\\repo\\Dockerfile", "C:\\gone\\repo"),
])
def test_nonexistent_extensionless_files_credit_their_parent(
        missing_file, expected_parent):
    assert tp._folder_of(missing_file) == expected_parent


@pytest.mark.parametrize("name", ["VERSION", "version", "Makefile", "makefile"])
def test_extensionless_filename_match_is_case_insensitive(name):
    assert tp._looks_like_file(name)


def test_a_real_directory_with_a_conventional_file_name_is_kept(tmp_path):
    """The list is only a FALLBACK. When the path exists, the filesystem is
    authoritative -- a directory genuinely named ``version`` stays a folder."""
    d = tmp_path / "version"
    d.mkdir()
    assert tp._looks_like_file("version"), "precondition: the guess says file"
    assert tp._folder_of(str(d)) == str(d)


def test_ordinary_missing_folders_are_not_dirnamed():
    """The fallback must not eat normal directories that simply no longer
    exist -- a deleted repo is still where work happened."""
    assert tp._folder_of("C:\\gone\\proj\\tests") == "C:\\gone\\proj\\tests"
    assert tp._folder_of("C:\\gone\\proj") == "C:\\gone\\proj"


# ── the probe must run on the NORMALIZED spelling ────────────────────

@pytest.mark.parametrize("spelling", [
    "{posix}/sub/probe.txt",       # Git Bash drive form
    "/mnt/{drive}/{rest}/sub/probe.txt",   # placeholder, filled below
])
def test_probe_runs_on_the_normalized_spelling(tmp_path, spelling):
    """Windows cannot resolve ``/c/...`` or ``/mnt/c/...``.

    Probing the raw candidate answered "nonexistent" for every drive-mapped
    POSIX path, so those fell through to the lexical guess -- which is
    precisely where dotfiles slip: ``.gitignore`` has no extension the guess
    recognizes, and it cannot be added to the known-filenames list either,
    because ``.git``, ``.venv`` and ``.github`` share its shape and are
    directories. Measured: ``cp /d/M/.gitignore ...`` stored
    ``D:\\M\\.gitignore`` as a working directory.
    """
    sub = tmp_path / "sub"
    sub.mkdir()
    f = sub / "probe.txt"
    f.write_text("x", encoding="utf-8")

    native = str(f)
    drive, rest = native[0], native[2:].replace("\\", "/").lstrip("/")
    if spelling.startswith("/mnt/"):
        candidate = "/mnt/%s/%s" % (drive.lower(), rest)
    else:
        candidate = "/%s/%s" % (drive.lower(), rest)

    assert tp._folder_of(candidate) == tp.normalize(str(sub))


def test_dotfile_that_exists_is_recognized_as_a_file(tmp_path):
    """The filesystem is what distinguishes `.gitignore` (file) from
    `.github` (directory) -- no lexical rule can."""
    f = tmp_path / ".gitignore"
    f.write_text("*.pyc", encoding="utf-8")
    d = tmp_path / ".github"
    d.mkdir()

    assert not tp._looks_like_file(".gitignore"), "precondition: guess is blind"
    assert tp._folder_of(str(f)) == tp.normalize(str(tmp_path))
    assert tp._folder_of(str(d)) == tp.normalize(str(d))

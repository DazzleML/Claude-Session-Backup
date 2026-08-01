"""0.7.2: relative cd targets (E-ledger) and cd-fact primary ranking.

Two changes that share one surgery site in ``toolpaths``.

**Relative cd targets.** ``cd -``, ``cd ..``, ``cd foo`` produced bare-word
"folders" -- a literal ``-`` carried work credit in three real sessions
(from the ``cd "$BR"; ...; cd - >/dev/null`` loop idiom). ``effective_cwd``
never resolved its target against anything.

**cd-fact ranking ("Option 4").** Rank primary by CERTAINTY of evidence,
not syntax of mention: an explicit cd is a fact (the shell moved there),
an absolute path in command text is a guess (operand or literal,
indistinguishable). Measured on 4,532 real commands before adoption; the
cd-present disagreement buckets are dominated by literals -- including one
inside sed replacement TEXT that happens to EXIST on disk, which neither a
payload parser nor an existence rule could catch. Restricted to explicit
cd/pushd: the unrestricted form would re-flatten 928 no-cd commands onto
the launch directory, recreating the original #56 bug.
"""

import pytest

from claude_session_backup import toolpaths as tp


# ── relative cd targets resolve, or die ──────────────────────────────

def test_cd_dotdot_resolves_to_the_parent():
    assert tp.effective_cwd(
        "Bash", {"command": "cd .. && ls"}, "C:\\a\\b") == "C:\\a"


def test_cd_bareword_resolves_under_cwd():
    assert tp.effective_cwd(
        "Bash", {"command": "mkdir foo && cd foo && ls"}, "C:\\a"
    ) == "C:\\a\\foo"


def test_cd_dash_is_unknowable_and_never_a_folder():
    """``cd "$BR"; ...; cd - >/dev/null`` is one of the most common shell
    idioms there is. ``-`` means "previous directory", which the transcript
    does not record -- fall through to cwd, and NEVER emit ``-`` as a
    place."""
    ti = {"command": "for b in x; do cd - >/dev/null; done"}
    assert tp.effective_cwd("Bash", ti, "C:\\launch") == "C:\\launch"
    assert "-" not in tp.touched_folders("Bash", ti, "C:\\launch")


def test_relative_cd_without_a_cwd_is_discarded():
    """No base to resolve against -> the target is meaningless, not a name."""
    ti = {"command": "cd foo && ls"}
    assert tp.effective_cwd("Bash", ti, None) is None
    assert "foo" not in tp.touched_folders("Bash", ti, None)


def test_cd_dotdot_leaves_no_dotdot_row():
    ti = {"command": "cd .. && git status"}
    assert tp.touched_folders("Bash", ti, "C:\\a\\b") == {"C:\\a"}


def test_absolute_cd_target_still_works():
    """The E2 surgery must not disturb the absolute case."""
    assert tp.effective_cwd(
        "Bash", {"command": "cd /c/code/proj && git status"}, "C:\\launch"
    ) == "C:\\code\\proj"


# ── the cd target outranks uncorroborated absolutes ──────────────────

def test_cd_fact_beats_uncorroborated_absolute(tmp_path):
    """AC-11, the sed case -- the one no other candidate fix could catch.

    The command cd's to X and mentions an absolute path elsewhere inside
    sed replacement TEXT. Existence cannot help (such literals can exist);
    payload-stripping cannot help (sed text is not a ``-c`` payload). Only
    the fact of the move can: the shell went to X, so X is the work.
    """
    cmd = ("cd " + str(tmp_path)
           + " && sed -i 's|old|C:\\\\somewhere\\\\else|' f.txt")
    ti = {"command": cmd}

    assert tp.primary_folder("Bash", ti, "C:\\launch") == \
        tp.normalize(str(tmp_path))
    assert "C:\\somewhere\\else" in tp.touched_folders(
        "Bash", ti, "C:\\launch"), (
        "demoted to presence, not erased -- it may still be a real place")


def test_corroborated_subfolder_keeps_precision(tmp_path):
    """AC-12: an absolute path UNDER the cd target is corroborated by the
    move -- subfolder precision must survive the reranking. Note existence
    is NOT required: corroboration is structural (under-prefix), not a
    filesystem probe."""
    sub = str(tmp_path) + "\\sub\\file.txt"
    cmd = "cd " + str(tmp_path) + " && cat " + sub

    assert tp.primary_folder("Bash", {"command": cmd}, "C:\\launch") == \
        tp.normalize(str(tmp_path)) + "\\sub"


def test_embedded_literal_loses_primary_to_the_cd_target(tmp_path):
    """AC-3 (revised): the measured ``C:\\code\\x`` case -- 6 real work
    units credited to a string literal inside a ``python -c`` payload. The
    literal keeps a presence row; the cd target takes the credit."""
    cmd = ("cd " + str(tmp_path)
           + " && python -c \"p='C:/code/xliteral'\"")
    ti = {"command": cmd}

    assert tp.primary_folder("Bash", ti, "C:\\launch") == \
        tp.normalize(str(tmp_path))
    assert "C:\\code\\xliteral" in tp.touched_folders("Bash", ti, "C:\\launch")


def test_no_cd_keeps_the_explicit_absolute_primary_even_when_gone():
    """AC-5 restated: WITHOUT a cd, ranking is unchanged -- and existence
    plays no role. A deleted repo is still where the work happened."""
    assert tp.primary_folder(
        "Bash", {"command": "grep foo C:\\gone-repo\\src"}, "C:\\launch"
    ) == "C:\\gone-repo\\src"


def test_relative_resolution_still_beats_the_cd_target(tmp_path):
    """The #56 subfolder fix survives Option 4: ``cd repo && pytest
    tests/x.py`` still credits ``tests``, not the repo root."""
    (tmp_path / "tests").mkdir()
    cmd = "cd " + str(tmp_path) + " && pytest tests/test_x.py"

    assert tp.primary_folder("Bash", {"command": cmd}, "C:\\launch") == \
        tp.normalize(str(tmp_path)) + "\\tests"


# ── extraction returns SOURCE order, and quoted paths keep spaces ────

def test_extraction_order_is_source_order_across_syntaxes():
    """AC-1. The old implementation returned all Windows-form matches,
    then all POSIX-form matches -- so `/c/a` at character 3 sorted AFTER a
    Windows-spelled path 80 characters later, corrupting "the first path
    mentioned", which primary resolution is built on. The docstring said
    "in order of appearance"; it was false and load-bearing.
    """
    assert tp.extract_paths_from_command(
        "cd /c/a && x C:\\b") == ["/c/a", "C:\\b"]
    # The measured real-command shape (GT-1): the cd target came second.
    got = tp.extract_paths_from_command(
        "cd /c/code/dazzle-filekit && python -c \"p='C:/code/x'\"")
    assert got[0] == "/c/code/dazzle-filekit", got


def test_quoted_windows_path_keeps_its_spaces():
    """AC-7 / B3. `\\s` is a hard boundary in the bare pattern, so
    `C:\\Program Files\\App` truncated at the space even when quoted --
    generating junk fragments like `C:\\PROGRAM` in a real index."""
    assert tp.extract_paths_from_command(
        'ls "C:\\Program Files\\App"') == ["C:\\Program Files\\App"]
    assert tp.extract_paths_from_command(
        "ls 'C:\\Program Files\\App'") == ["C:\\Program Files\\App"]


def test_overlapping_spans_dedupe_to_the_quoted_match():
    """AC-2. The bare pattern also fires INSIDE the quoted span; the
    candidate list must carry one entry, the full quoted path."""
    got = tp.extract_paths_from_command('x "C:\\Program Files\\App" y C:\\z')
    assert got == ["C:\\Program Files\\App", "C:\\z"]


def test_unquoted_spaced_path_still_truncates_documented_residual():
    """Deliberate residual: without quotes there is no boundary to trust.
    The fragment is junk, but it is NONEXISTENT junk, which the display
    tier hides. Pinned so a change here is noticed, not accidental."""
    assert tp.extract_paths_from_command(
        "ls C:\\Program Files\\App") == ["C:\\Program"]


# ── quoted cd targets keep their spaces too (tester pass 3, SEV-1) ───
#
# The quoted-path fix landed in general extraction but never reached the
# PARALLEL cd-target parser, so `cd "C:\Program Files\App"` truncated its
# base to `C:\Program` -- and because from_cd marks the base a FACT, the
# fabricated fragment won primary while the real target failed the
# corroboration prefix test against the wrong base and demoted to
# presence. Worse than a junk row: it corrupts the ranking mechanism this
# release is about. Latent in this vault only because its paths carry no
# spaces.

@pytest.mark.parametrize("cmd,expected", [
    ('cd "C:\\Program Files\\App" && dir', "C:\\Program Files\\App"),
    ("cd 'C:\\Program Files\\App' && dir", "C:\\Program Files\\App"),
    ('pushd "C:\\Program Files\\App" && dir', "C:\\Program Files\\App"),
    ('cd /d "C:\\Program Files\\App" && dir', "C:\\Program Files\\App"),
    ('cd "/home/user/My Documents/proj" && ls',
     "\\home\\user\\My Documents\\proj"),
])
def test_quoted_spaced_cd_target_is_the_base(cmd, expected):
    base, from_cd = tp._effective_base("Bash", {"command": cmd}, "C:\\launch")
    assert (base, from_cd) == (expected, True)


def test_quoted_spaced_cd_target_wins_primary_not_its_fragment():
    """The end-to-end shape the tester proved: the fragment must not
    appear ANYWHERE, and the real target takes the credit."""
    ti = {"command": 'cd "C:\\Program Files\\App" && dir'}

    assert tp.primary_folder("Bash", ti, "C:\\launch") == \
        "C:\\Program Files\\App"
    assert "C:\\Program" not in tp.touched_folders("Bash", ti, "C:\\launch")


def test_unquoted_cd_target_behavior_unchanged():
    """No-space targets keep working through the bare branch."""
    assert tp.effective_cwd(
        "Bash", {"command": "cd C:\\code\\proj && dir"}, "C:\\launch"
    ) == "C:\\code\\proj"


# ── quotes protect ALL path spellings (user-found, the THIRD parallel-
# site miss) ─────────────────────────────────────────────────────────
#
# The quoted-path fix honored quotes for drive-letter form only. Quoted
# Git-Bash/POSIX spellings fell to the bare pattern, whose comma boundary
# (a deliberate prose-list guard: `see C:\a, C:\b` must not glue) sliced
# `"/d/M/Software/_DeDRM, Proxies, Security, and Scripts"` down to a
# fragment carrying 14 real work units, `[not found]`-flagged -- which is
# how the user spotted it. Same class hit quoted RELATIVE tokens after a
# cd, crediting `Software` instead of the real deep folder.

def test_quoted_gitbash_path_keeps_commas_and_spaces():
    got = tp.extract_paths_from_command(
        'ls "/d/M/Software/_DeDRM, Proxies, Security, and Scripts"')
    assert got == ["/d/M/Software/_DeDRM, Proxies, Security, and Scripts"]
    assert tp.touched_folders(
        "Bash",
        {"command":
         'ls "/d/M/Software/_DeDRM, Proxies, Security, and Scripts"'},
        "D:\\M",
    ) >= {"D:\\M\\Software\\_DeDRM, Proxies, Security, and Scripts"}


def test_quoted_posix_root_path_keeps_commas_and_spaces():
    got = tp.extract_paths_from_command(
        "cat '/home/user/My Docs, Notes/f.txt'")
    assert got == ["/home/user/My Docs, Notes/f.txt"]


def test_quoted_relative_token_resolves_whole(tmp_path):
    """`cd base && python "Sub Dir, With Comma/inner/x.py"` must credit
    the real deep folder -- the existence gate disambiguates, exactly as
    it does for every other relative token."""
    deep = tmp_path / "Sub Dir, With Comma" / "inner"
    deep.mkdir(parents=True)
    cmd = ('cd ' + str(tmp_path)
           + ' && python "Sub Dir, With Comma/inner/x.py"')

    assert tp.primary_folder("Bash", {"command": cmd}, "C:\\launch") == \
        tp.normalize(str(deep))


def test_unquoted_comma_is_still_a_prose_boundary():
    """The guard the comma exists for: an enumeration must not glue."""
    assert tp.extract_paths_from_command(
        "see C:\\a, C:\\b for details") == ["C:\\a", "C:\\b"]
    assert tp.extract_paths_from_command(
        "in /c/x, /c/y today") == ["/c/x", "/c/y"]

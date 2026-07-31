"""Tests for CLI argument parsing and flag hoisting."""

import pytest

from claude_session_backup.cli import _hoist_common_flags, build_parser


def test_hoist_quiet_before_subcommand():
    result = _hoist_common_flags(["--quiet", "backup", "--no-commit"])
    # Hoisted flags go after all subcommand args
    assert result == ["backup", "--no-commit", "--quiet"]


def test_hoist_quiet_after_subcommand_unchanged():
    result = _hoist_common_flags(["backup", "--quiet", "--no-commit"])
    assert result == ["backup", "--quiet", "--no-commit"]


def test_hoist_short_flag():
    result = _hoist_common_flags(["-q", "backup"])
    assert result == ["backup", "-q"]


def test_hoist_claude_dir_with_value():
    result = _hoist_common_flags(["--claude-dir", "/tmp/.claude", "list"])
    assert result == ["list", "--claude-dir", "/tmp/.claude"]


def test_hoist_multiple_flags():
    result = _hoist_common_flags(["--quiet", "--claude-dir", "/tmp", "--db", "/tmp/db", "status"])
    assert result == ["status", "--quiet", "--claude-dir", "/tmp", "--db", "/tmp/db"]


def test_hoist_no_subcommand():
    result = _hoist_common_flags(["--version"])
    assert result == ["--version"]


def test_hoist_none_returns_none():
    assert _hoist_common_flags(None) is None


def test_hoist_preserves_subcommand_flags():
    result = _hoist_common_flags(["--quiet", "backup", "--no-commit"])
    assert "--no-commit" in result
    assert result.index("backup") < result.index("--no-commit")


def test_parse_quiet_before():
    parser = build_parser()
    argv = _hoist_common_flags(["--quiet", "backup", "--no-commit"])
    args = parser.parse_args(argv)
    assert args.quiet is True
    assert args.command == "backup"
    assert args.no_commit is True


def test_parse_quiet_after():
    parser = build_parser()
    args = parser.parse_args(["backup", "--quiet", "--no-commit"])
    assert args.quiet is True
    assert args.command == "backup"


def test_parse_no_quiet():
    parser = build_parser()
    args = parser.parse_args(["backup", "--no-commit"])
    assert args.quiet is False


def test_parse_claude_dir_before():
    parser = build_parser()
    argv = _hoist_common_flags(["--claude-dir", "/tmp/.claude", "status"])
    args = parser.parse_args(argv)
    assert args.claude_dir == "/tmp/.claude"
    assert args.command == "status"


def test_parse_list_with_filter_and_quiet():
    parser = build_parser()
    argv = _hoist_common_flags(["-q", "list", "vault", "-n", "5"])
    args = parser.parse_args(argv)
    assert args.quiet is True
    assert args.command == "list"
    assert args.filter == "vault"
    assert args.n == 5


# ── --sort flag tests ──────────────────────────────────────────────

def test_parse_list_default_sort_is_last_used():
    parser = build_parser()
    args = parser.parse_args(["list"])
    assert args.sort == "last-used"


def test_parse_list_with_sort_expiration():
    parser = build_parser()
    args = parser.parse_args(["list", "--sort", "expiration"])
    assert args.sort == "expiration"
    assert args.command == "list"


def test_parse_list_with_sort_all_choices():
    parser = build_parser()
    for choice in ("last-used", "expiration", "started", "oldest", "messages", "size"):
        args = parser.parse_args(["list", "--sort", choice])
        assert args.sort == choice


def test_parse_list_with_invalid_sort():
    import pytest
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["list", "--sort", "bogus"])


# ── --top / --all-folders flag tests (#21) ─────────────────────────

def test_parse_list_default_no_top_flag():
    parser = build_parser()
    args = parser.parse_args(["list"])
    assert args.top is None
    assert args.all_folders is False


def test_parse_list_top_n():
    parser = build_parser()
    args = parser.parse_args(["list", "--top", "10"])
    assert args.top == 10
    assert args.all_folders is False


def test_parse_list_all_folders():
    parser = build_parser()
    args = parser.parse_args(["list", "--all-folders"])
    assert args.top is None
    assert args.all_folders is True


def test_parse_list_top_and_all_folders_mutex():
    """`--top N` and `--all-folders` are mutually exclusive."""
    import pytest
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["list", "--top", "5", "--all-folders"])


def test_parse_scan_top_n():
    parser = build_parser()
    args = parser.parse_args(["scan", "--top", "7"])
    assert args.top == 7
    assert args.all_folders is False


def test_parse_scan_all_folders():
    parser = build_parser()
    args = parser.parse_args(["scan", "--all-folders"])
    assert args.all_folders is True


def test_resolve_top_folders_helper():
    """``_resolve_top_folders`` translates argparse flags into renderer values."""
    from claude_session_backup.commands import _resolve_top_folders
    from claude_session_backup.timeline import DEFAULT_TOP_FOLDERS

    class _Args:
        pass

    # Default: no flags, no config -> module-level default (3)
    args = _Args()
    args.top = None
    args.all_folders = False
    assert _resolve_top_folders(args) == DEFAULT_TOP_FOLDERS

    # --top N
    args.top = 8
    args.all_folders = False
    assert _resolve_top_folders(args) == 8

    # --all-folders -> None (renderer interprets None as "show all")
    args.top = None
    args.all_folders = True
    assert _resolve_top_folders(args) is None

    # Defensive: --all-folders wins even if --top is also somehow set
    args.top = 5
    args.all_folders = True
    assert _resolve_top_folders(args) is None


def test_resolve_top_folders_config_default():
    """Config ``display_top_folders`` is consulted when no CLI flag is set."""
    from claude_session_backup.commands import _resolve_top_folders

    class _Args:
        pass

    args = _Args()
    args.top = None
    args.all_folders = False

    # User config sets the renderer default
    assert _resolve_top_folders(args, {"display_top_folders": 7}) == 7

    # Negative config value -> show all
    assert _resolve_top_folders(args, {"display_top_folders": -1}) is None

    # Malformed config value -> falls back to module default rather than crash
    from claude_session_backup.timeline import DEFAULT_TOP_FOLDERS
    assert _resolve_top_folders(args, {"display_top_folders": "bogus"}) == DEFAULT_TOP_FOLDERS

    # CLI --top N still wins over config
    args.top = 2
    assert _resolve_top_folders(args, {"display_top_folders": 7}) == 2

    # CLI --all-folders still wins over config
    args.top = None
    args.all_folders = True
    assert _resolve_top_folders(args, {"display_top_folders": 7}) is None


# ── csb scan: -d/-D + term flag tests (#20) ────────────────────────

def test_scan_bare_no_args():
    """csb scan with no args: term/-d/-D all None."""
    parser = build_parser()
    args = parser.parse_args(["scan"])
    assert args.command == "scan"
    assert args.term is None
    assert args.directories_below is None
    assert args.directory_only is None


def test_scan_term_only():
    """csb scan amdead -> term='amdead', no -d/-D."""
    parser = build_parser()
    args = parser.parse_args(["scan", "amdead"])
    assert args.term == "amdead"
    assert args.directories_below is None
    assert args.directory_only is None


def test_scan_d_short_flag():
    """csb scan -d amdead -> directories_below='amdead', term=None."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-d", "amdead"])
    assert args.directories_below == "amdead"
    assert args.directory_only is None
    assert args.term is None


def test_scan_d_long_flag():
    """csb scan --directories-below amdead works the same as -d."""
    parser = build_parser()
    args = parser.parse_args(["scan", "--directories-below", "amdead"])
    assert args.directories_below == "amdead"


def test_scan_D_short_flag():
    """csb scan -D amdead -> directory_only='amdead' (uppercase = no descendants)."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-D", "amdead"])
    assert args.directory_only == "amdead"
    assert args.directories_below is None
    assert args.term is None


def test_scan_D_long_flag():
    """csb scan --directory-only amdead works the same as -D."""
    parser = build_parser()
    args = parser.parse_args(["scan", "--directory-only", "amdead"])
    assert args.directory_only == "amdead"


def test_scan_d_and_D_mutex():
    """-d and -D are mutually exclusive (only one path-strict mode at a time)."""
    import pytest
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scan", "-d", "amdead", "-D", "redditslack"])


def test_scan_d_with_term():
    """csb scan -d amdead my-paper -> path-scope + term filter combined."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-d", "amdead", "my-paper"])
    assert args.directories_below == "amdead"
    assert args.term == "my-paper"


def test_scan_D_with_term():
    """csb scan -D amdead my-paper -> exact-folder-scope + term filter."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-D", "amdead", "my-paper"])
    assert args.directory_only == "amdead"
    assert args.term == "my-paper"


def test_scan_dot_prefix_shortcut_with_term():
    """csb scan ./amdead my-paper -> dot-prefix shortcut + term filter (parses as 2 positionals)."""
    parser = build_parser()
    args = parser.parse_args(["scan", "./amdead", "my-paper"])
    # Parser-level: term = first positional, term2 = second positional.
    # cmd_scan handles the dot-prefix promotion + term2 swap.
    assert args.term == "./amdead"
    assert args.term2 == "my-paper"


def test_scan_bare_term_no_term2():
    """Single positional (no dot-prefix): term2 is None."""
    parser = build_parser()
    args = parser.parse_args(["scan", "amdead"])
    assert args.term == "amdead"
    assert args.term2 is None


def test_scan_three_positionals_rejected():
    """Three positionals should be rejected by argparse (only term + term2 defined)."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scan", "./amdead", "my-paper", "extra"])


def test_scan_d_with_wildcard():
    """Wildcard passes through verbatim (parser doesn't validate; cmd_scan does)."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-d", "amdead*"])
    assert args.directories_below == "amdead*"


def test_scan_D_with_wildcard():
    """Wildcard works with -D too."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-D", "amdead*"])
    assert args.directory_only == "amdead*"


def test_scan_d_absolute_path():
    """Absolute paths pass through verbatim (no cwd-relative resolution at parse time)."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-d", "C:\\code\\amdead"])
    assert args.directories_below == "C:\\code\\amdead"


def test_scan_d_requires_value():
    """-d alone (no value) is an argparse error."""
    import pytest
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scan", "-d"])


def test_scan_D_requires_value():
    """-D alone (no value) is an argparse error."""
    import pytest
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scan", "-D"])


def test_scan_term_with_n_flag():
    """csb scan amdead -n 5 -> term + custom result count."""
    parser = build_parser()
    args = parser.parse_args(["scan", "amdead", "-n", "5"])
    assert args.term == "amdead"
    assert args.n == 5


def test_scan_d_with_n_flag():
    """-d before -n parses correctly (no positional confusion)."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-d", "amdead", "-n", "5"])
    assert args.directories_below == "amdead"
    assert args.n == 5


def test_scan_no_index_with_d():
    """The index-bypass flag stacks with -d."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-NI", "-d", "amdead"])
    assert args.no_index is True
    assert args.directories_below == "amdead"


def test_scan_top_with_d():
    """--top works alongside -d (gates folder_usage matching)."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-d", "amdead", "--top", "5"])
    assert args.top == 5
    assert args.directories_below == "amdead"


def test_scan_all_folders_with_d():
    """--all-folders works alongside -d (removes top-N gate)."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-d", "amdead", "--all-folders"])
    assert args.all_folders is True
    assert args.directories_below == "amdead"


def test_scan_old_path_positional_removed():
    """The old path positional is gone -- a string positional is now 'term'.

    Sanity check that cli.py no longer exposes args.path on the scan parser.
    """
    parser = build_parser()
    args = parser.parse_args(["scan", "amdead"])
    # No args.path attribute on the new scan parser
    assert not hasattr(args, "path")
    # The string was captured as term, not path
    assert args.term == "amdead"


# ── csb scan -s / --start-dir-only tests (#20 follow-up) ───────────

def test_scan_s_short_flag():
    """csb scan -s amdead -> start_dir_only='amdead', no -d/-D."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-s", "amdead"])
    assert args.start_dir_only == "amdead"
    assert args.directories_below is None
    assert args.directory_only is None
    assert args.term is None


def test_scan_s_long_flag():
    """csb scan --start-dir-only amdead works the same as -s."""
    parser = build_parser()
    args = parser.parse_args(["scan", "--start-dir-only", "amdead"])
    assert args.start_dir_only == "amdead"


def test_scan_s_with_term():
    """csb scan -s amdead my-paper -> start-only-scope + term filter combined."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-s", "amdead", "my-paper"])
    assert args.start_dir_only == "amdead"
    assert args.term == "my-paper"


def test_scan_s_with_wildcard():
    """csb scan -s amdead* -> wildcard pattern for start_folder match."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-s", "amdead*"])
    assert args.start_dir_only == "amdead*"


def test_scan_s_and_d_mutex():
    """-s and -d are mutually exclusive (path-strict modes)."""
    import pytest
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scan", "-s", "amdead", "-d", "redditslack"])


def test_scan_s_and_D_mutex():
    """-s and -D are mutually exclusive."""
    import pytest
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scan", "-s", "amdead", "-D", "redditslack"])


def test_scan_s_requires_value():
    """-s alone (no value) is an argparse error."""
    import pytest
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scan", "-s"])


# ── csb search: v0.2.8 output-mode mutex + --session-id rename ───────


def test_search_session_id_accepts_value():
    """--session-id replaces --session; comma-separated values pass through."""
    parser = build_parser()
    args = parser.parse_args(["search", "foo", "--session-id", "abc1,def2"])
    assert args.session_id == "abc1,def2"


def test_search_canonical_session_id_attribute_is_session_id():
    """The v0.2.7 rename took effect: the argparse attribute is
    ``session_id``, not ``session``. (Note: at v0.3.5 the removal of
    ``--sessions-only`` happens to make ``--session`` a unique
    argparse prefix abbreviation for ``--session-id``, but the
    canonical attribute name is the only stable contract.)"""
    parser = build_parser()
    args = parser.parse_args(["search", "foo", "--session-id", "abc1"])
    assert args.session_id == "abc1"
    assert not hasattr(args, "session")  # legacy attribute is gone


def test_search_output_mode_mutex_json_vs_only_files():
    """--json + --only files is rejected (v0.3.5 mutex)."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["search", "foo", "--json", "--only", "files"])


def test_search_output_mode_mutex_json_vs_only_sessions():
    """--json + --only sessions is rejected (v0.3.5 mutex)."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["search", "foo", "--json", "--only", "sessions"])


def test_search_only_files_parses():
    """--only files alone is accepted; args.only == 'files'."""
    parser = build_parser()
    args = parser.parse_args(["search", "foo", "--only", "files"])
    assert args.only == "files"


def test_search_only_sessions_parses():
    """--only sessions alone is accepted; args.only == 'sessions'."""
    parser = build_parser()
    args = parser.parse_args(["search", "foo", "--only", "sessions"])
    assert args.only == "sessions"


def test_search_only_rejects_invalid_choice():
    """--only files-and-sessions is rejected with a clear choices list."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["search", "foo", "--only", "everything"])


def test_search_old_files_only_flag_gone():
    """v0.3.5 breaking: --files-only no longer exists."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["search", "foo", "--files-only"])


def test_search_old_sessions_only_flag_gone():
    """v0.3.5 breaking: --sessions-only no longer exists."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["search", "foo", "--sessions-only"])


# ── csb search --sort (v0.3.0) ───────────────────────────────────────


def test_search_sort_default_is_last_used():
    parser = build_parser()
    args = parser.parse_args(["search", "foo"])
    assert args.sort == "last-used"


def test_search_sort_accepts_all_csb_list_choices():
    """The choices set MUST match csb list's --sort, so users only learn
    one vocabulary."""
    parser = build_parser()
    for choice in ["last-used", "expiration", "started", "oldest", "messages", "size"]:
        args = parser.parse_args(["search", "foo", "--sort", choice])
        assert args.sort == choice


def test_search_sort_rejects_invalid_choice():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["search", "foo", "--sort", "bogus"])


# ── csb search --full-info / -f / -ff (v0.2.10) ─────────────────────


def test_search_full_info_default_is_zero():
    """No flag = level 0 (compact one-line header)."""
    parser = build_parser()
    args = parser.parse_args(["search", "foo"])
    assert args.full_info == 0


def test_search_full_info_long_flag_is_level_1():
    """--full-info = level 1 via action='count'."""
    parser = build_parser()
    args = parser.parse_args(["search", "foo", "--full-info"])
    assert args.full_info == 1


def test_search_short_f_is_level_1():
    """-f = level 1 (single increment)."""
    parser = build_parser()
    args = parser.parse_args(["search", "foo", "-f"])
    assert args.full_info == 1


def test_search_double_ff_is_level_2():
    """-ff = level 2 (two increments via standard argparse repeated-short)."""
    parser = build_parser()
    args = parser.parse_args(["search", "foo", "-ff"])
    assert args.full_info == 2


def test_search_full_info_combines_with_only_sessions():
    parser = build_parser()
    args = parser.parse_args(["search", "foo", "-ff", "--only", "sessions"])
    assert args.full_info == 2
    assert args.only == "sessions"


def test_search_triple_fff_does_not_crash():
    """-fff parses (=3); cmd_search caps at 2. argparse counts unbounded."""
    parser = build_parser()
    args = parser.parse_args(["search", "foo", "-fff"])
    assert args.full_info == 3


# ── internal `_check` subcommand: parseable but hidden ──────────────

def test_check_subcommand_parses_but_is_hidden():
    """`_check` is the SessionStart hook's detector: it must PARSE (the hook
    invokes it) yet stay out of `csb --help` (it's not a user-facing command)."""
    parser = build_parser()
    args = parser.parse_args(["_check", "--exclude", "abc123"])
    assert args.command == "_check"
    assert args.exclude == ["abc123"]
    # Hidden: no help= was passed, so argparse lists no entry, and the
    # add_subparsers metavar keeps it out of the usage line too.
    assert "_check" not in parser.format_help()


def test_check_subcommand_has_description(capsys):
    """`csb _check -h` must show a real description (not a bare usage line) so
    the hidden command doesn't feel shady to a user who finds it -- and points
    them at the public `csb status` surface."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["_check", "-h"])
    out = capsys.readouterr().out
    assert "health check" in out.lower()
    # Normalize argparse's line-wrapping -- the phrase must survive re-wraps
    # that land a break between "csb" and "status" (#52 description edit).
    assert "csb status" in " ".join(out.split())


# == #41: unified --deleted [only|all] grammar across list / search / scan ==
#
# One attach helper (cli.add_deleted_flag) + one normalizer
# (commands.deleted_mode). scan's old boolean --deleted + --all migrated;
# --all survives as a hidden deprecated alias until 0.4.


def test_parse_scan_deleted_bare_means_only():
    parser = build_parser()
    args = parser.parse_args(["scan", "--deleted"])
    assert args.deleted == "only"


def test_parse_scan_deleted_only_explicit():
    parser = build_parser()
    args = parser.parse_args(["scan", "--deleted", "only"])
    assert args.deleted == "only"


def test_parse_scan_deleted_all():
    parser = build_parser()
    args = parser.parse_args(["scan", "--deleted", "all"])
    assert args.deleted == "all"


def test_parse_scan_deleted_invalid_choice_rejected():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scan", "--deleted", "bogus"])


def test_parse_scan_all_alias_still_parses():
    parser = build_parser()
    args = parser.parse_args(["scan", "--all"])
    assert args.all is True
    assert args.deleted is None


def test_parse_list_and_search_grammar_unchanged():
    parser = build_parser()
    for cmd, extra in (("list", []), ("search", ["q"])):
        args = parser.parse_args([cmd] + extra + ["--deleted"])
        assert args.deleted == "only"
        args = parser.parse_args([cmd] + extra + ["--deleted", "all"])
        assert args.deleted == "all"
        # The deprecated --all ALIAS attribute exists only on scan; list and
        # search never define it (note: bare `--all` on list still prefix-
        # matches argparse's --all-folders -- unrelated, pre-existing).
        assert not hasattr(args, "all")


def test_deleted_mode_normalizer():
    import argparse as ap
    from claude_session_backup import commands as cmds
    assert cmds.deleted_mode(ap.Namespace(deleted=None)) == "live"
    assert cmds.deleted_mode(ap.Namespace(deleted="only")) == "only"
    assert cmds.deleted_mode(ap.Namespace(deleted="all")) == "all"
    assert cmds.deleted_mode(ap.Namespace()) == "live"  # flag not even defined


def test_deleted_mode_all_alias_maps_and_warns_once(capsys):
    import argparse as ap
    from claude_session_backup import commands as cmds
    cmds._warned_all_deprecated = False  # reset the once-guard for the test
    try:
        assert cmds.deleted_mode(ap.Namespace(deleted=None, all=True)) == "all"
        err1 = capsys.readouterr().err
        assert "deprecated" in err1 and "--deleted all" in err1
        # Second call: mapped, but silent.
        assert cmds.deleted_mode(ap.Namespace(deleted=None, all=True)) == "all"
        assert "deprecated" not in capsys.readouterr().err
    finally:
        cmds._warned_all_deprecated = False


# == #52: `python -m claude_session_backup` exit-code propagation ==


def test_module_invocation_propagates_exit_code(mock_claude_dir_repoless, tmp_path):
    """__main__.py must sys.exit(main()) -- a bare main() call always exited
    0, silently breaking rc-sensitive callers of the module form (notably the
    backup hook's `python -m ... _check` fallback, where a gap's rc 10
    collapsing to 0 reads as 'all backed up'). Uses `_check` with one
    unindexed session: gap detection is repo-independent (#52), so the
    expected rc 10 holds whether or not tmp sits inside an ancestor repo."""
    import os
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(repo_root)}
    r = subprocess.run(
        [_sys.executable, "-m", "claude_session_backup", "_check", "--quiet",
         "--claude-dir", str(mock_claude_dir_repoless),
         "--db", str(tmp_path / "modrc.db")],
        capture_output=True, text=True, timeout=120, env=env,
        cwd=str(tmp_path),  # NOT the repo root -- proves PYTHONPATH does the work
    )
    assert r.returncode == 10, (r.returncode, r.stdout, r.stderr)


# == v0.6.0 (#52): every-run no-protection banner + csb setup ==
#
# Unprotected is the EXCEPTION: cli.main() prints a loud stderr banner on
# EVERY interactive run against a repo-less claude dir until resolved --
# either a git repo exists, or the user recorded an explicit index-only
# sign-off (`csb setup --index-only` -> backup_mode in the config file).
# Internal commands, --quiet runs (the hook), and `setup` itself never
# show it. is_git_repo is patched at git_ops -- _maybe_repoless_banner
# imports it lazily, so the patch is picked up.

import json as _json_mod


def _cli_main(argv):
    from claude_session_backup.cli import main as cli_main
    return cli_main(argv)


def _banner_argv(claude, tmp_path, *extra):
    return ["list", "--claude-dir", str(claude), "--db", str(tmp_path / "fr.db"),
            *extra]


def _patch_repoless(monkeypatch, value=False):
    import claude_session_backup.git_ops as git_ops
    state = ("ok", "") if value else ("absent", "")
    monkeypatch.setattr(git_ops, "git_repo_state", lambda d: state)


def _patch_refused(monkeypatch, detail="fatal: detected dubious ownership in repository at 'X'"):
    import claude_session_backup.git_ops as git_ops
    monkeypatch.setattr(git_ops, "git_repo_state", lambda d: ("refused", detail))


def test_banner_repoless_shows_every_run(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    _patch_repoless(monkeypatch)
    rc = _cli_main(_banner_argv(mock_claude_dir_repoless, tmp_path))
    captured = capsys.readouterr()
    assert rc == 0
    assert "NO BACKUP PROTECTION" in captured.err
    assert "csb setup" in captured.err
    assert "NO BACKUP PROTECTION" not in captured.out  # stderr only

    # Second run: STILL shows -- the nag persists until resolved.
    _cli_main(_banner_argv(mock_claude_dir_repoless, tmp_path))
    assert "NO BACKUP PROTECTION" in capsys.readouterr().err


def test_banner_silent_with_repo(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    _patch_repoless(monkeypatch, value=True)
    _cli_main(_banner_argv(mock_claude_dir_repoless, tmp_path))
    assert "NO BACKUP PROTECTION" not in capsys.readouterr().err


def test_banner_silenced_by_index_only_ack(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """The recorded sign-off (backup_mode=index-only in the config file) is
    the ONLY repo-less state that silences the banner."""
    _patch_repoless(monkeypatch)
    cfg = mock_claude_dir_repoless / "session-backup-config.json"
    cfg.write_text(_json_mod.dumps({"backup_mode": "index-only"}), encoding="utf-8")
    _cli_main(_banner_argv(mock_claude_dir_repoless, tmp_path))
    assert "NO BACKUP PROTECTION" not in capsys.readouterr().err


def test_banner_not_shown_on_quiet_run(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """--quiet (how hooks call csb): captured stderr is not user
    communication -- no banner."""
    _patch_repoless(monkeypatch)
    _cli_main(_banner_argv(mock_claude_dir_repoless, tmp_path, "--quiet"))
    assert "NO BACKUP PROTECTION" not in capsys.readouterr().err


def test_banner_json_stdout_stays_clean(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    _patch_repoless(monkeypatch)
    rc = _cli_main(_banner_argv(mock_claude_dir_repoless, tmp_path, "--json"))
    captured = capsys.readouterr()
    assert rc == 0
    assert _json_mod.loads(captured.out) == []   # banner never corrupts stdout
    assert "NO BACKUP PROTECTION" in captured.err


# == csb setup: parsing + -V shorthand ==


def test_parse_setup_defaults():
    parser = build_parser()
    args = parser.parse_args(["setup"])
    assert args.command == "setup"
    assert args.auto is False
    assert args.index_only is False


def test_parse_setup_flags():
    parser = build_parser()
    assert parser.parse_args(["setup", "--auto"]).auto is True
    assert parser.parse_args(["setup", "--index-only"]).index_only is True


def test_version_short_flag_matches_long(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["-V"])
    short = capsys.readouterr().out
    with pytest.raises(SystemExit):
        parser.parse_args(["--version"])
    assert short == capsys.readouterr().out
    assert short.startswith("csb ")


# == v0.6.0: `csb help` alias ==


def test_help_alias_prints_help(capsys):
    """`csb help` must behave like `csb --help`, not error with
    'invalid choice'."""
    from claude_session_backup.cli import main as cli_main
    with pytest.raises(SystemExit) as exc:
        cli_main(["help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: csb" in out


def test_help_alias_with_command(capsys):
    """`csb help setup` -> `csb setup --help`."""
    from claude_session_backup.cli import main as cli_main
    with pytest.raises(SystemExit) as exc:
        cli_main(["help", "setup"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: csb setup" in out


def test_banner_refused_names_refusal_not_absence(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    """AC-R1 (banner): a refused repo gets the BLOCKED variant with git's
    words -- never the false "has no git repository" claim."""
    _patch_refused(monkeypatch)
    _cli_main(_banner_argv(mock_claude_dir_repoless, tmp_path))
    err = capsys.readouterr().err
    assert "BACKUPS BLOCKED" in err
    assert "EXISTS" in err
    assert "dubious ownership" in err
    assert "do NOT re-initialize" in err
    assert "has no git repository" not in err


def test_banner_git_error_variant(monkeypatch, mock_claude_dir_repoless, tmp_path, capsys):
    import claude_session_backup.git_ops as git_ops
    monkeypatch.setattr(git_ops, "git_repo_state",
                        lambda d: ("error", "git was not found on PATH"))
    _cli_main(_banner_argv(mock_claude_dir_repoless, tmp_path))
    err = capsys.readouterr().err
    assert "BACKUP STATE UNKNOWN" in err
    assert "has no git repository" not in err


# -- --filter KEY=VALUE vocabulary (v0.7.1) ----------------------------
#
# `--filter N` was renamed to `--filter min-work=N` before it ever shipped.
# Rationale: `filter` already meant "the keyword positional" on list/tree,
# and an int-valued flag of the same name made one word mean two things at
# two different units. Keys unify them: the positional is sugar for the
# common key, and everything that narrows is a --filter.
#
# The comparison direction lives in the KEY (`min-work=`) rather than an
# operator (`work>=10`) because `>` and `<` are redirection in cmd.exe and
# PowerShell and get eaten before csb sees them.


def test_filter_parses_key_value():
    from claude_session_backup.cli import build_parser
    args = build_parser().parse_args(["show", "abc", "--filter", "min-work=5"])
    assert dict(args.filters) == {"min-work": 5}


def test_filter_is_repeatable_and_ands():
    from claude_session_backup.cli import build_parser
    args = build_parser().parse_args(
        ["show", "abc", "--filter", "min-work=5", "--filter", "min-work=9"])
    assert len(args.filters) == 2


def test_filter_rejects_bare_number(capsys):
    """`--filter 5` was the old syntax. It must fail loudly rather than be
    silently reinterpreted."""
    from claude_session_backup.cli import build_parser
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        build_parser().parse_args(["show", "abc", "--filter", "5"])
    assert "KEY=VALUE" in capsys.readouterr().err


def test_filter_rejects_unknown_key(capsys):
    """A silently-ignored filter is a wrong answer wearing a right answer's
    clothes -- the same failure class as the --top match gate."""
    from claude_session_backup.cli import build_parser
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        build_parser().parse_args(["show", "abc", "--filter", "bogus=5"])
    err = capsys.readouterr().err
    assert "unknown filter key" in err
    assert "min-work" in err, "the error must name the valid keys"


def test_filter_rejects_non_integer_value(capsys):
    from claude_session_backup.cli import build_parser
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        build_parser().parse_args(["show", "abc", "--filter", "min-work=lots"])
    assert "expects" in capsys.readouterr().err


def test_show_all_does_not_share_a_dest_with_all_folders():
    """`csb show --all` and `csb list --all-folders` mean different things.

    They used to write to the SAME dest (`all_folders`), so any shared
    render path would have read one as the other. Latent, but a trap.
    """
    from claude_session_backup.cli import build_parser
    p = build_parser()
    show = p.parse_args(["show", "abc", "--all"])
    lst = p.parse_args(["list", "--all-folders"])

    assert show.show_all is True
    assert getattr(show, "all_folders", None) is not True, (
        "csb show --all still writes to the all_folders dest")
    assert lst.all_folders is True


# -- hints must name syntax the verb actually accepts ------------------
#
# Bitten three times now: `--all-folders` in show's low-value note (show has
# no such flag), `--filter 0` in the thin note (no longer valid syntax), and
# the --top help text advertising a match gate that had been removed. A hint
# is a user instruction; if it cannot be typed, it is a bug.

def test_render_hints_reference_only_flags_show_accepts(capsys):
    """Scan every flag token a `csb show` hint prints and confirm the show
    parser accepts it. Generic, so the next stale hint is caught for free."""
    import re
    from claude_session_backup.commands import (
        _print_low_value_note, _thin_note)
    from claude_session_backup.cli import build_parser

    _print_low_value_note(
        [{"folder_path": "C:\\code\\x", "usage_count": 0}], show_all=False)
    text = capsys.readouterr().out
    text += _thin_note([{"usage_count": 1}], 5)

    parser = build_parser()
    for flag in set(re.findall(r"--[a-z][a-z0-9-]*", text)):
        # A flag is acceptable if `csb show` knows it. Value-taking flags are
        # probed with a dummy that the type= callable will accept.
        try:
            parser.parse_args(["show", "abc", flag])
            continue
        except SystemExit:
            pass
        try:
            parser.parse_args(["show", "abc", flag, "min-work=1"])
        except SystemExit:
            raise AssertionError(
                "a `csb show` hint tells the user to type %r, which the show "
                "parser rejects. Hints must be executable. Text: %r"
                % (flag, text))


# -- --help groups flags by which question they answer ------------------
#
# csb has three kinds of narrowing -- what MATCHES, how much of each match
# is SHOWN, and how many results survive -- which had been rendering as one
# undifferentiated wall of flags. Conflating the first two is precisely what
# caused #56's match-gate bug, where a display cap silently decided which
# sessions existed. Making the boundary visible in --help is part of that
# fix, so these tests guard it rather than treating it as cosmetics.


def _group_titles(verb):
    from claude_session_backup.cli import build_parser
    sub = build_parser()._subparsers._group_actions[0].choices[verb]
    return [g.title for g in sub._action_groups if g._group_actions]


def _group_of(verb, flag):
    """Which group title owns `flag` on `verb` (None if ungrouped)."""
    from claude_session_backup.cli import build_parser
    sub = build_parser()._subparsers._group_actions[0].choices[verb]
    for group in sub._action_groups:
        for action in group._group_actions:
            if flag in action.option_strings:
                return group.title
    return None


@pytest.mark.parametrize("verb", ["scan", "list", "tree"])
def test_help_has_selection_and_display_groups(verb):
    titles = " | ".join(_group_titles(verb))
    assert "selection" in titles, titles
    assert "display" in titles, titles


@pytest.mark.parametrize("verb,flag", [
    ("scan", "-d"), ("scan", "-D"), ("scan", "-s"), ("scan", "--deleted"),
    ("scan", "--no-index"),
    ("list", "--deleted"),
    ("tree", "-d"), ("tree", "-D"), ("tree", "--root"), ("tree", "--orphans"),
    ("tree", "--lineage"),
])
def test_selection_flags_live_in_the_selection_group(verb, flag):
    assert "selection" in (_group_of(verb, flag) or ""), (
        "%s %s is not grouped as selection" % (verb, flag))


@pytest.mark.parametrize("verb,flag", [
    ("scan", "--top"), ("scan", "--all-folders"), ("scan", "--shortid"),
    ("list", "--top"), ("list", "--all-folders"),
    ("tree", "--top"), ("tree", "--all-folders"), ("tree", "-f"),
    ("tree", "--ascii"),
])
def test_display_flags_live_in_the_display_group(verb, flag):
    """--top especially. It is a display cap and nothing else; the release
    that removed its hidden match-gate should also stop it LOOKING like a
    selector."""
    assert "display" in (_group_of(verb, flag) or ""), (
        "%s %s is not grouped as display" % (verb, flag))


@pytest.mark.parametrize("verb,flag", [
    ("scan", "-n"), ("list", "-n"), ("list", "--sort"),
    ("tree", "-n"), ("tree", "--max-nodes"), ("tree", "--sort"),
])
def test_limit_flags_live_in_the_limits_group(verb, flag):
    assert "limits" in (_group_of(verb, flag) or ""), (
        "%s %s is not grouped as a limit" % (verb, flag))


@pytest.mark.parametrize("verb,pair", [
    ("scan", ["--top", "3", "--all-folders"]),
    ("list", ["--top", "3", "--all-folders"]),
    ("tree", ["--top", "3", "--all-folders"]),
    ("scan", ["-d", ".", "-D", "."]),
    ("tree", ["-d", ".", "-D", "."]),
])
def test_regrouping_preserved_mutual_exclusion(verb, pair):
    """Re-parenting a mutually-exclusive group into an argument group is
    easy to get wrong in a way that silently DROPS the exclusivity."""
    from claude_session_backup.cli import build_parser
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        build_parser().parse_args([verb] + pair)


# -- --no-index replaces --no-usage / -NU (v0.7.1, breaking) -----------
#
# The old name described a SIDE EFFECT ("only consider start_folder"),
# which reads as a duplicate of -s. What the flag actually does is bypass
# the SQLite index and read transcripts from disk. That distinction was
# nearly invisible while folder_usage held one folder per session; #56
# made the index rich enough for it to matter, so the flag got the name
# of its mechanism.


def test_no_usage_and_NU_are_gone():
    """A real cut, not an alias. If someone re-adds them as hidden
    aliases that is a deliberate decision -- this test should be updated
    rather than silently satisfied."""
    from claude_session_backup.cli import build_parser
    import pytest as _pytest
    for dead in ("--no-usage", "-NU"):
        with _pytest.raises(SystemExit):
            build_parser().parse_args(["scan", dead])


def test_no_index_sets_its_own_dest():
    from claude_session_backup.cli import build_parser
    args = build_parser().parse_args(["scan", "--no-index"])
    assert args.no_index is True
    assert not hasattr(args, "no_usage")


def test_no_index_help_leads_with_the_mechanism():
    """The old help described what falls out of the flag rather than what
    it does, which is exactly why it read as redundant with -s."""
    from claude_session_backup.cli import build_parser
    sub = build_parser()._subparsers._group_actions[0].choices["scan"]
    action = next(a for a in sub._actions if "--no-index" in a.option_strings)
    help_text = action.help.lower()
    assert "index" in help_text
    assert "deleted" in help_text, (
        "the help must warn that this cannot find deleted sessions")

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


def test_scan_no_usage_with_d():
    """Existing -NU flag stacks with new -d flag."""
    parser = build_parser()
    args = parser.parse_args(["scan", "-NU", "-d", "amdead"])
    assert args.no_usage is True
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

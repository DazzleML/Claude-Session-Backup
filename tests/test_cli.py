"""Tests for CLI argument parsing and flag hoisting."""

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

    # Default: no flags -> module-level default (3)
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

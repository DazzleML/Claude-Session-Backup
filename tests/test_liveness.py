"""Process-table liveness (#64/#67): matching, filtering, degradation.

The real process table is never read -- `_enumerate_processes` is the
seam. Fixtures mirror the shapes the design probe measured on a real
machine: resumed-by-UUID, resumed-by-NAME, bare `claude` (fresh), and
Claude Desktop's Electron processes.
"""

import pytest

import claude_session_backup.liveness as lv

UUID_A = "37f09835-4c0a-4bdb-9049-b379ea6b5771"

PROCS = [
    (100, "claude.EXE", f"claude.EXE --resume {UUID_A}"),
    (200, "claude", "claude --resume DAZZLECMD__git-actual-cmd"),
    (300, "claude", "claude"),  # fresh -- unattributable
    (400, "Claude.exe",
     r"C:\Program Files\WindowsApps\Claude.exe --type=renderer "
     "--user-data-dir=X"),  # Claude Desktop -- must never match
    (500, "python", "python -m pytest"),
]


@pytest.fixture
def scan(monkeypatch):
    monkeypatch.setattr(lv, "_enumerate_processes", lambda: list(PROCS))
    return lv.scan()


class TestScan:
    def test_uuid_resume_matched_lowercased(self, scan):
        assert scan.by_uuid == {UUID_A.lower(): 100}

    def test_name_resume_matched_verbatim(self, scan):
        assert scan.by_name == {"DAZZLECMD__git-actual-cmd": 200}

    def test_fresh_session_counted_as_bare(self, scan):
        assert scan.bare_pids == (300,)

    def test_desktop_and_unrelated_never_match(self, scan):
        all_pids = (set(scan.by_uuid.values()) | set(scan.by_name.values())
                    | set(scan.bare_pids))
        assert 400 not in all_pids
        assert 500 not in all_pids

    def test_enumeration_failure_degrades_not_raises(self, monkeypatch):
        monkeypatch.setattr(lv, "_enumerate_processes", lambda: None)
        result = lv.scan()
        assert result.ok is False
        assert result.by_uuid == {} and result.bare_pids == ()


class TestVerifyMember:
    def test_by_uuid_case_insensitive(self, scan):
        assert lv.verify_member(scan, UUID_A.upper(), None) == 100

    def test_by_name_when_uuid_absent(self, scan):
        assert lv.verify_member(
            scan, "cd69fce9-0000-0000-0000-000000000000",
            "DAZZLECMD__git-actual-cmd") == 200

    def test_no_proof_is_none_not_false(self, scan):
        # None means "no proof", not "not running" -- a fresh session is
        # invisible here by construction.
        assert lv.verify_member(
            scan, "99999999-0000-0000-0000-000000000000", "NOPE") is None


class TestParsers:
    @pytest.mark.parametrize("cmdline,expected", [
        (f"claude --resume {UUID_A}", UUID_A),
        ("claude -r MY-SESSION", "MY-SESSION"),
        ('claude --resume "QUOTED-NAME"', "QUOTED-NAME"),
        ("claude --resume --model opus", None),  # flag, not an identifier
        ("claude", None),
        ("claude -p hi", None),
    ])
    def test_resume_identifier(self, cmdline, expected):
        assert lv.resume_identifier(cmdline) == expected

    @pytest.mark.parametrize("cmdline,expected", [
        ("claude --resume x", True),
        (r"C:\Users\x\claude.EXE --resume y", True),
        (r"C:\WindowsApps\Claude.exe --type=renderer", False),
        ("claude-desktop-helper --user-data-dir=x", False),
        ("", False),
        ("python claude_thing.py", False),
    ])
    def test_is_claude_cli(self, cmdline, expected):
        assert lv.is_claude_cli(cmdline) is expected

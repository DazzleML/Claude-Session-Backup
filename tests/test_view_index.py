"""View-index addressing for set membership (#76): `set add NAME boot:15`.

The number a user just read is the cheapest correct address they hold.
V1 grammar: last-colon split, bare-int suffix, resolvable view/set
prefix -- anything else falls through to the ordinary vocabulary, so a
session literally named `notes:1` keeps resolving by name. Epoch
prefixes inherit the H8 fallthrough (grab what `show` DISPLAYED); every
grab echoes; a miss aborts the whole command (the half-built-set rule).
"""

import json
from datetime import datetime, timezone

import pytest

import claude_session_backup.cli as cli
import claude_session_backup.commands as commands_module
import claude_session_backup.live_registry as lr
from claude_session_backup.epochs import EVENT_BOOT, EVENT_SHUTDOWN_CLEAN, Fence
from claude_session_backup.index import init_schema, open_db
from claude_session_backup.session_sets import create_set, get_set

UUID_NEW = "aaaa9991-bbbb-cccc-dddd-000000000001"    # e0 (last)
UUID_DEEP = "aaaa9992-bbbb-cccc-dddd-000000000002"   # e2 (last~2)
UUID_NOTES = "aaaa9993-bbbb-cccc-dddd-000000000003"  # named "notes:1"


def _utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# e0 spans Jul 1->Jul 20 (NEW, NOTES); e1 Jun 20->Jul 1 EMPTY;
# e2 Jun 10->Jun 20 (DEEP); e3 Jun 1->Jun 10 empty.
FENCES = [
    Fence(_utc(2026, 6, 1, 8), EVENT_BOOT),
    Fence(_utc(2026, 6, 10, 12), EVENT_SHUTDOWN_CLEAN),
    Fence(_utc(2026, 6, 10, 12, 5), EVENT_BOOT),
    Fence(_utc(2026, 6, 20, 12), EVENT_SHUTDOWN_CLEAN),
    Fence(_utc(2026, 6, 20, 12, 5), EVENT_BOOT),
    Fence(_utc(2026, 7, 1, 12), EVENT_SHUTDOWN_CLEAN),
    Fence(_utc(2026, 7, 1, 12, 5), EVENT_BOOT),
    Fence(_utc(2026, 7, 20, 12), EVENT_SHUTDOWN_CLEAN),
    Fence(_utc(2026, 7, 20, 12, 5), EVENT_BOOT),
]


def _insert(conn, sid, name, last_active):
    conn.execute(
        "INSERT INTO sessions (session_id, session_name, project,"
        " start_folder, started_at, last_active_at, is_fork,"
        " message_count) VALUES (?, ?, 'C--code-test', 'C:\\code\\test',"
        " '2026-06-01T00:00:00Z', ?, 0, 5)",
        (sid, name, last_active),
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    db = tmp_path / "vi.db"
    conn = open_db(db)
    init_schema(conn, quiet=True)
    _insert(conn, UUID_DEEP, "DEEP__session", "2026-06-15T12:00:00Z")
    _insert(conn, UUID_NOTES, "notes:1", "2026-07-09T12:00:00Z")
    _insert(conn, UUID_NEW, "NEW__session", "2026-07-10T12:00:00Z")
    conn.commit()
    conn.close()
    monkeypatch.setattr(commands_module, "read_fences",
                        lambda *a, **k: list(FENCES))
    # `boot` needs only the boot instant; ancient boot => every session
    # is in-boot, hookless (no registry, no scan), activity-ordered:
    # DEEP=1, notes:1=2, NEW=3.
    monkeypatch.setattr(lr, "current_boot_utc", lambda: _utc(2020, 1, 1))
    import types

    return types.SimpleNamespace(claude_dir=claude_dir, db=db)


def _run(env, *argv):
    return cli.main(list(argv) + ["--claude-dir", str(env.claude_dir),
                                  "--db", str(env.db)])


class TestViewIndexGrammar:
    def test_boot_index_grabs_the_displayed_row(self, env, capsys):
        """RED-GREEN anchor: boot:2 is exactly what `show boot` numbers 2."""
        assert _run(env, "set", "new", "GRAB", "boot:2") == 0
        out = capsys.readouterr().out
        assert "boot:2 -> notes:1" in out          # the echo (V5)
        entry = get_set(env.claude_dir, "GRAB")
        assert [m["session_id"] for m in entry["members"]] == [UUID_NOTES]

    def test_epoch_index_inherits_the_fallthrough(self, env, capsys):
        """last~1 is EMPTY; the grab lands where `show last~1` settles
        (last~2's roster) -- read-a-number-get-that-number, everywhere."""
        assert _run(env, "set", "new", "ARCHAEO", "last~1:1") == 0
        entry = get_set(env.claude_dir, "ARCHAEO")
        assert [m["session_id"] for m in entry["members"]] == [UUID_DEEP]
        assert "last~1:1 -> DEEP__session" in capsys.readouterr().out

    def test_colon_named_session_falls_through(self, env, capsys):
        """No set named `notes` exists -> `notes:1` is an ordinary
        name query, resolving the session literally named that."""
        assert _run(env, "set", "new", "PLAIN", "notes:1") == 0
        entry = get_set(env.claude_dir, "PLAIN")
        assert [m["session_id"] for m in entry["members"]] == [UUID_NOTES]
        assert "->" not in capsys.readouterr().out  # no view-grab echo

    def test_out_of_range_aborts_whole_create(self, env, capsys):
        """The half-built-set rule: boot:99 fails -> NO set exists."""
        rc = _run(env, "set", "new", "BROKEN", "NEW__session", "boot:99")
        assert rc == 2
        assert get_set(env.claude_dir, "BROKEN") is None
        err = capsys.readouterr().err
        assert "no row 99" in err and "Nothing was changed" in err

    def test_mixed_tokens_keep_cli_order(self, env, capsys):
        assert _run(env, "set", "new", "MIX", "boot:2", "NEW__session") == 0
        entry = get_set(env.claude_dir, "MIX")
        assert [m["session_id"] for m in entry["members"]] == [
            UUID_NOTES, UUID_NEW]

    def test_named_set_index_and_self_referential_rm(self, env, capsys):
        """`set rm MYSET MYSET:2` removes the row you are looking at."""
        create_set(env.claude_dir, "MYSET", [UUID_NEW, UUID_DEEP])
        capsys.readouterr()
        assert _run(env, "set", "rm", "MYSET", "MYSET:2") == 0
        out = capsys.readouterr().out
        assert "MYSET:2 -> DEEP__session" in out
        entry = get_set(env.claude_dir, "MYSET")
        assert [m["session_id"] for m in entry["members"]] == [UUID_NEW]

    def test_add_by_epoch_index(self, env, capsys):
        create_set(env.claude_dir, "MYSET", [UUID_NEW])
        capsys.readouterr()
        assert _run(env, "set", "add", "MYSET", "last~2:1") == 0
        entry = get_set(env.claude_dir, "MYSET")
        assert UUID_DEEP in [m["session_id"] for m in entry["members"]]

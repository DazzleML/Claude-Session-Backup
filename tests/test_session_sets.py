"""Named session sets: storage layer + CRUD commands (#62).

The storage tests exercise `session_sets` directly (a temp claude dir,
no index needed). The command tests drive the real CLI so grammar,
resolution, and the shared roster renderer are covered end to end. One
integration test runs a real `csb backup` against a real git repo to
prove the sets file lands in the USER commit -- the claim that named
sets survive `rebuild-index` rests on the file being backed up at all.
"""

import json
import subprocess
from types import SimpleNamespace

import pytest

import claude_session_backup.cli as cli
import claude_session_backup.session_sets as session_sets
from claude_session_backup.git_ops import USER_FILES
from claude_session_backup.index import init_schema, open_db
from claude_session_backup.session_sets import (
    SetError,
    add_member,
    create_set,
    delete_set,
    get_set,
    list_sets,
    load_sets,
    remove_member,
    save_sets,
    sets_path,
    validate_set_name,
)

# Distinct leading bytes, like real UUIDs -- a shared prefix would make
# `csb set new <prefix>` ambiguous for reasons that have nothing to do
# with the code under test.
UUID_A = "a1b2c3d4-bbbb-cccc-dddd-00000000000a"
UUID_B = "b2c3d4e5-bbbb-cccc-dddd-00000000000b"
UUID_C = "c3d4e5f6-bbbb-cccc-dddd-00000000000c"


# ── name validation ──────────────────────────────────────────────────────

class TestValidateSetName:
    @pytest.mark.parametrize("name", ["last", "LAST", "Last", "set", "SET"])
    def test_grammar_tokens_rejected(self, name):
        # These would shadow `csb set show last` / `csb resume set ...`.
        with pytest.raises(SetError, match="reserved"):
            validate_set_name(name)

    @pytest.mark.parametrize("name", ["1", "42", "0042", "007"])
    def test_bare_integers_rejected(self, name):
        # Would collide with `csb resume set <N>` index addressing.
        with pytest.raises(SetError, match="reserved"):
            validate_set_name(name)

    @pytest.mark.parametrize("name", ["", "   ", "\t"])
    def test_empty_rejected(self, name):
        with pytest.raises(SetError):
            validate_set_name(name)

    def test_untrimmed_rejected(self):
        with pytest.raises(SetError, match="whitespace"):
            validate_set_name(" CSB-STACK")

    @pytest.mark.parametrize("name", [
        "CSB-STACK", "DAZZLE-LIBS", "2026-8-2__pre-update-restart",
        "last-one", "set-aside", "v1", "42-things",
    ])
    def test_conventional_and_near_miss_names_allowed(self, name):
        # Only the exact grammar tokens are reserved -- csb documents
        # conventions, it does not enforce them.
        validate_set_name(name)


# ── storage: load / save / atomicity ─────────────────────────────────────

class TestStorage:
    def test_missing_file_is_empty_doc(self, tmp_path):
        doc = load_sets(tmp_path)
        assert doc["sets"] == {}

    def test_round_trip(self, tmp_path):
        create_set(tmp_path, "CSB-STACK", [UUID_A, UUID_B])
        entry = get_set(tmp_path, "CSB-STACK")
        assert [m["session_id"] for m in entry["members"]] == [UUID_A, UUID_B]
        assert entry["created_at"] and entry["updated_at"]

    def test_file_is_valid_json_on_disk(self, tmp_path):
        create_set(tmp_path, "CSB-STACK", [UUID_A])
        raw = json.loads(sets_path(tmp_path).read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert "CSB-STACK" in raw["sets"]

    def test_corrupt_file_raises_and_never_resets(self, tmp_path):
        """A corrupt file is user data -- refuse, don't silently replace."""
        path = sets_path(tmp_path)
        path.write_text("{ this is not json", encoding="utf-8")
        with pytest.raises(SetError, match="Could not read"):
            load_sets(tmp_path)
        # The bad content is still there -- nothing overwrote it.
        assert path.read_text(encoding="utf-8") == "{ this is not json"

    def test_wrong_shape_raises(self, tmp_path):
        sets_path(tmp_path).write_text('["not", "a", "doc"]', encoding="utf-8")
        with pytest.raises(SetError, match="not a csb sets file"):
            load_sets(tmp_path)

    def test_reserved_name_in_hand_edited_file_is_skipped(self, tmp_path):
        """Defense in depth: a hand-added 'last' must not shadow the epoch."""
        sets_path(tmp_path).write_text(json.dumps({
            "version": 1,
            "sets": {
                "last": {"members": [{"session_id": UUID_A}]},
                "CSB-STACK": {"members": [{"session_id": UUID_B}]},
            },
        }), encoding="utf-8")
        warnings = []
        doc = load_sets(tmp_path, warn=warnings.append)
        assert "last" not in doc["sets"]
        assert "CSB-STACK" in doc["sets"]
        assert any("reserved" in w for w in warnings)

    def test_atomic_write_leaves_original_on_failure(self, tmp_path,
                                                     monkeypatch):
        create_set(tmp_path, "CSB-STACK", [UUID_A])
        before = sets_path(tmp_path).read_text(encoding="utf-8")

        def boom(src, dst):
            raise OSError("simulated crash during replace")

        monkeypatch.setattr(session_sets.os, "replace", boom)
        with pytest.raises(OSError):
            save_sets(tmp_path, {"version": 1, "sets": {}})
        # Original intact, and no temp litter left behind.
        assert sets_path(tmp_path).read_text(encoding="utf-8") == before
        leftovers = list(tmp_path.glob(".csb-sets.json.*"))
        assert leftovers == []


# ── CRUD semantics ───────────────────────────────────────────────────────

class TestCrud:
    def test_duplicate_name_rejected(self, tmp_path):
        create_set(tmp_path, "CSB-STACK", [UUID_A])
        with pytest.raises(SetError, match="already exists"):
            create_set(tmp_path, "CSB-STACK", [UUID_B])

    def test_name_lookup_is_case_insensitive(self, tmp_path):
        create_set(tmp_path, "CSB-STACK", [UUID_A])
        assert get_set(tmp_path, "csb-stack") is not None

    def test_add_is_idempotent(self, tmp_path):
        create_set(tmp_path, "CSB-STACK", [UUID_A])
        assert add_member(tmp_path, "CSB-STACK", UUID_B) is True
        assert add_member(tmp_path, "CSB-STACK", UUID_B) is False
        entry = get_set(tmp_path, "CSB-STACK")
        assert len(entry["members"]) == 2

    def test_remove_non_member_is_no_op(self, tmp_path):
        create_set(tmp_path, "CSB-STACK", [UUID_A])
        assert remove_member(tmp_path, "CSB-STACK", UUID_C) is False
        assert len(get_set(tmp_path, "CSB-STACK")["members"]) == 1

    def test_remove_member_updates_timestamp(self, tmp_path):
        create_set(tmp_path, "CSB-STACK", [UUID_A, UUID_B])
        assert remove_member(tmp_path, "CSB-STACK", UUID_A) is True
        assert [m["session_id"] for m in
                get_set(tmp_path, "CSB-STACK")["members"]] == [UUID_B]

    def test_add_to_missing_set_raises(self, tmp_path):
        with pytest.raises(SetError, match="No set named"):
            add_member(tmp_path, "NOPE", UUID_A)

    def test_delete_set(self, tmp_path):
        create_set(tmp_path, "CSB-STACK", [UUID_A])
        assert delete_set(tmp_path, "CSB-STACK") is True
        assert get_set(tmp_path, "CSB-STACK") is None
        assert delete_set(tmp_path, "CSB-STACK") is False

    def test_list_sets_sorted(self, tmp_path):
        create_set(tmp_path, "ZULU", [UUID_A])
        create_set(tmp_path, "alpha", [UUID_B])
        assert [n for n, _ in list_sets(tmp_path)] == ["alpha", "ZULU"]


# ── git integration: the file must actually be backed up ─────────────────

class TestGitClassification:
    def test_registered_as_user_class(self):
        assert session_sets.SETS_FILENAME in USER_FILES

    def test_lands_in_the_user_commit(self, mock_claude_dir, tmp_path,
                                      capsys):
        """The survives-rebuild claim rests on the file being committed.

        Runs a real `csb backup` against the fixture's real git repo and
        checks the sets file is in the tree afterward.
        """
        create_set(mock_claude_dir, "CSB-STACK", [UUID_A])
        rc = cli.main(["backup", "--claude-dir", str(mock_claude_dir),
                       "--db", str(tmp_path / "b.db"), "--quiet"])
        assert rc == 0
        tracked = subprocess.run(
            ["git", "ls-files", session_sets.SETS_FILENAME],
            cwd=str(mock_claude_dir), capture_output=True, text=True,
        ).stdout
        assert session_sets.SETS_FILENAME in tracked

    def test_survives_rebuild_index(self, mock_claude_dir, tmp_path):
        """`rebuild-index` reconstructs the INDEX; sets are source data."""
        db = tmp_path / "r.db"
        create_set(mock_claude_dir, "CSB-STACK", [UUID_A, UUID_B])
        cli.main(["backup", "--claude-dir", str(mock_claude_dir),
                  "--db", str(db), "--quiet", "--no-commit"])
        rc = cli.main(["update", "rebuild-index", "--claude-dir",
                       str(mock_claude_dir), "--db", str(db), "--quiet"])
        assert rc == 0
        entry = get_set(mock_claude_dir, "CSB-STACK")
        assert [m["session_id"] for m in entry["members"]] == [UUID_A, UUID_B]


# ── CLI: the CRUD verbs ──────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path):
    """A claude dir + index holding two resolvable sessions."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    db = tmp_path / "cli.db"
    conn = open_db(db)
    init_schema(conn, quiet=True)
    for sid, name in ((UUID_A, "ALPHA__session"), (UUID_B, "BETA__session")):
        conn.execute(
            "INSERT INTO sessions (session_id, session_name, project,"
            " start_folder, started_at, last_active_at, is_fork) VALUES"
            " (?, ?, 'C--code-test', 'C:\\code\\test',"
            " '2026-07-01T00:00:00Z', '2026-07-20T00:00:00Z', 0)",
            (sid, name),
        )
    conn.commit()
    conn.close()
    return SimpleNamespace(claude_dir=claude_dir, db=db)


def _run(env, *argv):
    return cli.main(list(argv) + ["--claude-dir", str(env.claude_dir),
                                  "--db", str(env.db)])


class TestCliCrud:
    def test_new_then_show(self, env, capsys):
        assert _run(env, "set", "new", "CSB-STACK", "ALPHA__session") == 0
        capsys.readouterr()
        assert _run(env, "set", "show", "CSB-STACK") == 0
        out = capsys.readouterr().out
        assert "ALPHA__session" in out
        assert "1." in out  # the shared numbered roster
        assert "csb resume ALPHA__session" in out

    def test_new_resolves_by_uuid_prefix(self, env, capsys):
        assert _run(env, "set", "new", "CSB-STACK", UUID_A[:8]) == 0
        entry = get_set(env.claude_dir, "CSB-STACK")
        assert entry["members"][0]["session_id"] == UUID_A

    def test_new_reserved_name_rejected_rc2(self, env, capsys):
        rc = _run(env, "set", "new", "last", "ALPHA__session")
        assert rc == 2
        assert "reserved" in capsys.readouterr().err

    def test_unresolvable_member_aborts_whole_create(self, env, capsys):
        """A half-built set is worse than none."""
        rc = _run(env, "set", "new", "CSB-STACK", "ALPHA__session",
                  "no-such-session-anywhere")
        assert rc != 0
        assert get_set(env.claude_dir, "CSB-STACK") is None

    def test_add_and_idempotent_add(self, env, capsys):
        _run(env, "set", "new", "CSB-STACK", "ALPHA__session")
        capsys.readouterr()
        assert _run(env, "set", "add", "CSB-STACK", "BETA__session") == 0
        assert "Added 1 session" in capsys.readouterr().out
        assert _run(env, "set", "add", "CSB-STACK", "BETA__session") == 0
        assert "already a member" in capsys.readouterr().out

    def test_rm_member_then_whole_set(self, env, capsys):
        _run(env, "set", "new", "CSB-STACK", "ALPHA__session", "BETA__session")
        capsys.readouterr()
        assert _run(env, "set", "rm", "CSB-STACK", "ALPHA__session") == 0
        assert "Removed 1 session" in capsys.readouterr().out
        assert _run(env, "set", "rm", "CSB-STACK") == 0
        out_err = capsys.readouterr()
        assert "Deleted set" in out_err.out
        assert "recoverable" in out_err.err
        assert get_set(env.claude_dir, "CSB-STACK") is None

    def test_rm_non_member_is_clear_no_op(self, env, capsys):
        _run(env, "set", "new", "CSB-STACK", "ALPHA__session")
        capsys.readouterr()
        assert _run(env, "set", "rm", "CSB-STACK", "BETA__session") == 0
        assert "not a member" in capsys.readouterr().out

    def test_operations_on_missing_set_rc1(self, env, capsys):
        assert _run(env, "set", "add", "NOPE", "ALPHA__session") == 1
        assert _run(env, "set", "rm", "NOPE") == 1

    def test_list_shows_sets_and_epoch_section(self, env, capsys):
        _run(env, "set", "new", "CSB-STACK", "ALPHA__session")
        capsys.readouterr()
        assert _run(env, "set", "list") == 0
        out = capsys.readouterr().out
        assert "CSB-STACK" in out and "1 member" in out
        assert "Boot epochs:" in out

    def test_list_empty(self, env, capsys):
        assert _run(env, "set", "list") == 0
        assert "No named sets yet" in capsys.readouterr().out

    def test_list_json(self, env, capsys):
        _run(env, "set", "new", "CSB-STACK", "ALPHA__session")
        capsys.readouterr()
        assert _run(env, "set", "list", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["sets"][0]["name"] == "CSB-STACK"
        assert payload["sets"][0]["members"] == 1

    def test_show_json_named_set(self, env, capsys):
        _run(env, "set", "new", "CSB-STACK", "ALPHA__session")
        capsys.readouterr()
        assert _run(env, "set", "show", "CSB-STACK", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["kind"] == "named"
        assert payload["epoch"] is None
        assert payload["members"][0]["index"] == 1

    def test_unresolvable_member_marked_not_dropped(self, env, capsys):
        """A set that silently shrinks lies about what it holds."""
        create_set(env.claude_dir, "CSB-STACK", [UUID_A, UUID_C])
        assert _run(env, "set", "show", "CSB-STACK") == 0
        out = capsys.readouterr().out
        assert "[not in index]" in out
        assert "2." in out  # still numbered, still present
        assert "no longer in the index" in out

    def test_empty_set_states_it(self, env, capsys):
        create_set(env.claude_dir, "EMPTY-SET", [])
        assert _run(env, "set", "show", "EMPTY-SET") == 0
        assert "no members" in capsys.readouterr().out

    def test_list_degrades_when_fences_unavailable(self, env, capsys,
                                                   monkeypatch):
        """Named sets are platform-independent -- listing must not fail
        where boot-fence reading cannot work."""
        import claude_session_backup.epochs as epochs
        monkeypatch.setattr(epochs.sys, "platform", "linux")
        _run(env, "set", "new", "CSB-STACK", "ALPHA__session")
        capsys.readouterr()
        assert _run(env, "set", "list") == 0
        out = capsys.readouterr().out
        assert "CSB-STACK" in out
        assert "unavailable" in out

    def test_list_advertises_the_boot_view(self, env, capsys, monkeypatch):
        """The epoch in progress must be discoverable from the overview.
        The boot VIEW shipped in v0.8.5 without a list row -- the one
        view `csb set list` didn't advertise (caught live by the user)."""
        from datetime import datetime, timezone

        import claude_session_backup.live_registry as lr
        monkeypatch.setattr(lr, "current_boot_utc",
                            lambda: datetime(2026, 8, 1,
                                             tzinfo=timezone.utc))
        assert _run(env, "set", "list") == 0
        out = capsys.readouterr().out
        assert "csb set show boot" in out
        assert "in progress" in out

    def test_list_json_gains_additive_boot_key(self, env, capsys,
                                               monkeypatch):
        """Additive alongside the legacy `epoch` key, which stays."""
        from datetime import datetime, timezone

        import claude_session_backup.live_registry as lr
        monkeypatch.setattr(lr, "current_boot_utc",
                            lambda: datetime(2026, 8, 1,
                                             tzinfo=timezone.utc))
        assert _run(env, "set", "list", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["boot"] == {"name": "boot",
                                   "boot_at": "2026-08-01T00:00:00Z"}
        assert "epoch" in payload

    def test_list_boot_row_absent_when_boot_unknowable(self, env, capsys,
                                                       monkeypatch):
        """No guessed row: unknowable boot instant -> no boot line.
        (Trivially true pre-fix; load-bearing once the row exists.)"""
        import claude_session_backup.live_registry as lr
        monkeypatch.setattr(lr, "current_boot_utc", lambda: None)
        assert _run(env, "set", "list") == 0
        out = capsys.readouterr().out
        assert "csb set show boot" not in out
        assert _run(env, "set", "list", "--json") == 0
        assert json.loads(capsys.readouterr().out)["boot"] is None

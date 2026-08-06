"""`csb resume set <N>`: index addressing into sets and epochs (#63).

claude is never actually launched here -- `subprocess.run` is mocked at
the module boundary; the human checklist covers real launches. The
load-bearing test in this file is the no-spawn guarantee: csb must
launch exactly one session in the terminal it was invoked from, and
must never create a window, tab, or terminal of its own.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import claude_session_backup.cli as cli
import claude_session_backup.commands as commands_module
import claude_session_backup.epochs as epochs
from claude_session_backup.index import init_schema, open_db
from claude_session_backup.session_sets import create_set

# The same canned fence history the Phase 1 tests use: a 2026-07-25
# update restart, prior epoch starting at the 07-15 boot.
FENCE_OUTPUT = (
    "2026-07-25T16:17:18.0000000Z|6005\n"
    "2026-07-25T16:16:32.0000000Z|6006\n"
    "2026-07-25T16:16:18.0000000Z|1074\n"
    "2026-07-15T08:18:17.0000000Z|6005\n"
    "2026-07-15T08:17:38.0000000Z|6006\n"
)

# Distinct leading bytes so prefixes never collide.
UUID_1 = "11111111-bbbb-cccc-dddd-000000000001"
UUID_2 = "22222222-bbbb-cccc-dddd-000000000002"
UUID_3 = "33333333-bbbb-cccc-dddd-000000000003"
UUID_GONE = "99999999-bbbb-cccc-dddd-000000000099"


@pytest.fixture
def fences(monkeypatch):
    monkeypatch.setattr(epochs.sys, "platform", "win32")
    monkeypatch.setattr(epochs, "_run_powershell", lambda *a, **k: FENCE_OUTPUT)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Claude dir + index with three in-window sessions, launcher mocked.

    Activity times are ordered so the roster is deterministic:
    ALPHA (07-18) = 1, BETA (07-20) = 2, GAMMA (07-22) = 3.
    """
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects" / "C--code-test").mkdir(parents=True)
    db = tmp_path / "resume.db"
    conn = open_db(db)
    init_schema(conn, quiet=True)
    for sid, nm, active in (
        (UUID_1, "ALPHA__session", "2026-07-18T00:00:00Z"),
        (UUID_2, "BETA__session", "2026-07-20T00:00:00Z"),
        (UUID_3, "GAMMA__session", "2026-07-22T00:00:00Z"),
    ):
        jsonl = claude_dir / "projects" / "C--code-test" / f"{sid}.jsonl"
        jsonl.write_text('{"type":"user"}\n', encoding="utf-8")
        conn.execute(
            "INSERT INTO sessions (session_id, session_name, project,"
            " start_folder, started_at, last_active_at, jsonl_path, is_fork)"
            " VALUES (?, ?, 'C--code-test', ?, '2026-07-01T00:00:00Z', ?, ?, 0)",
            (sid, nm, str(tmp_path), active,
             f"projects/C--code-test/{sid}.jsonl"),
        )
    conn.commit()
    conn.close()

    run_mock = MagicMock(return_value=SimpleNamespace(returncode=0))
    import subprocess as subprocess_module
    monkeypatch.setattr(subprocess_module, "run", run_mock)
    monkeypatch.setattr(commands_module.shutil, "which",
                        lambda name: "C:\\bin\\claude.exe")
    # Real signature is (ok, reason) -- a bare True unpacks to garbage.
    monkeypatch.setattr(commands_module, "_transcript_is_resumable",
                        lambda p: (True, ""))
    return SimpleNamespace(claude_dir=claude_dir, db=db, run=run_mock,
                           tmp=tmp_path)


def _run(env, *argv):
    """Invoke the CLI with this env's dirs, inserted BEFORE any `--`.

    Appending them blindly would put `--claude-dir`/`--db` into the
    passthrough for any command that uses `--`, silently falling back to
    the real ~/.claude index -- a test-isolation leak, not a fake pass.
    """
    argv = list(argv)
    flags = ["--claude-dir", str(env.claude_dir), "--db", str(env.db)]
    if "--" in argv:
        cut = argv.index("--")
        argv = argv[:cut] + flags + argv[cut:]
    else:
        argv = argv + flags
    return cli.main(argv)


def _claude_calls(env):
    """Only the `claude` invocations.

    subprocess.run is patched module-wide, so it also catches csb's own
    git calls (the repo-state banner). Filtering for claude keeps these
    assertions about launches rather than about incidental plumbing.
    """
    calls = []
    for call in env.run.call_args_list:
        argv = call[0][0] if call[0] else None
        if argv and str(argv[0]).replace(".exe", "").endswith("claude"):
            calls.append(argv)
    return calls


def _launched_uuid(env):
    """The UUID csb passed to `claude --resume` (exactly one launch)."""
    calls = _claude_calls(env)
    assert len(calls) == 1, f"expected 1 claude launch, got {len(calls)}"
    argv = calls[0]
    assert argv[1] == "--resume"
    return argv[2]


# ── the selector forms ───────────────────────────────────────────────────

class TestSelectorForms:
    def test_bare_index_uses_last_epoch(self, fences, env):
        assert _run(env, "resume", "--set", "2") == 0
        assert _launched_uuid(env) == UUID_2

    def test_explicit_last_is_equivalent(self, fences, env):
        assert _run(env, "resume", "--set", "last", "2") == 0
        assert _launched_uuid(env) == UUID_2

    def test_named_set_index(self, fences, env):
        create_set(env.claude_dir, "CSB-STACK", [UUID_3, UUID_1])
        assert _run(env, "resume", "--set", "CSB-STACK", "2") == 0
        assert _launched_uuid(env) == UUID_1  # insertion order, not activity

    def test_first_and_last_members(self, fences, env):
        assert _run(env, "resume", "--set", "1") == 0
        assert _launched_uuid(env) == UUID_1

    def test_plain_resume_still_works(self, fences, env):
        assert _run(env, "resume", "BETA__session") == 0
        assert _launched_uuid(env) == UUID_2


# ── the no-spawn guarantee (the load-bearing AC) ─────────────────────────

class TestNeverSpawns:
    def test_exactly_one_launch_no_terminal(self, fences, env, monkeypatch):
        """csb launches ONE claude in the current terminal -- nothing else.

        Any call to Popen, os.system, os.startfile, or a shell would mean
        csb had created a window of its own, which it must never do.
        """
        import os
        import subprocess as subprocess_module
        forbidden = []
        monkeypatch.setattr(subprocess_module, "Popen",
                            lambda *a, **k: forbidden.append("Popen"))
        monkeypatch.setattr(os, "system",
                            lambda *a, **k: forbidden.append("os.system"))
        if hasattr(os, "startfile"):
            monkeypatch.setattr(os, "startfile",
                                lambda *a, **k: forbidden.append("startfile"))

        assert _run(env, "resume", "--set", "2") == 0
        assert forbidden == []
        assert len(_claude_calls(env)) == 1
        # No shell, and cwd is a real directory -- the terminal csb was
        # invoked from, not a new one.
        kwargs = env.run.call_args[1]  # the claude launch is the last call
        assert kwargs.get("shell") in (None, False)
        assert "creationflags" not in kwargs

    def test_forwards_passthrough_to_claude(self, fences, env):
        assert _run(env, "resume", "--set", "2", "--", "--fork-session") == 0
        argv = _claude_calls(env)[0]
        assert argv[-1] == "--fork-session"
        assert argv[1] == "--resume" and argv[2] == UUID_2


# ── inherited behavior from the shared launcher ──────────────────────────

class TestSharedLauncher:
    def test_purged_member_routes_through_restore_on_resume(
            self, fences, env, monkeypatch, capsys):
        """A pruned member reaches the v0.3.14 restore path, not the launcher.

        Guaranteed structurally -- `_resume_session_row` is shared with
        `csb resume <query>`, so the pruned decision happens before any
        launch -- but pinned here so a future refactor cannot quietly
        route index addressing around it.
        """
        import sqlite3
        conn = sqlite3.connect(env.db)
        conn.execute("UPDATE sessions SET deleted_at = '2026-07-26T00:00:00Z'"
                     " WHERE session_id = ?", (UUID_2,))
        conn.commit()
        conn.close()

        seen = {}
        decision = {"value": "abort"}

        def fake_decision(args, session, name, verb="resume"):
            seen["session_id"] = session["session_id"]
            seen["verb"] = verb
            return decision["value"]

        monkeypatch.setattr(commands_module, "_resolve_pruned_decision",
                            fake_decision)

        # "abort" = the user declined to restore. That is a choice, not a
        # failure, so it exits 0 -- but nothing is launched.
        assert _run(env, "resume", "--set", "2") == 0
        assert seen["session_id"] == UUID_2
        assert seen["verb"] == "resume"
        assert _claude_calls(env) == []

        # "error" = non-TTY with no flag: exits 1, still no blind launch.
        seen.clear()
        decision["value"] = "error"
        assert _run(env, "resume", "--set", "2") == 1
        assert seen["session_id"] == UUID_2
        assert _claude_calls(env) == []

    def test_active_member_skips_the_pruned_path(self, fences, env,
                                                 monkeypatch):
        called = []
        monkeypatch.setattr(commands_module, "_resolve_pruned_decision",
                            lambda *a, **k: called.append(1) or "abort")
        assert _run(env, "resume", "--set", "1") == 0
        assert called == []
        assert _launched_uuid(env) == UUID_1


# ── the stable-index contract ────────────────────────────────────────────

class TestStableIndices:
    def test_show_and_resume_agree(self, fences, env, capsys):
        """Member N in `csb set show` is member N in `csb resume set`.

        Both go through _materialize_set_roster, which takes no filter
        parameters -- that absence is what keeps the numbers meaning the
        same thing in both commands.
        """
        assert _run(env, "set", "show", "last", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        for member in payload["members"]:
            env.run.reset_mock()
            assert _run(env, "resume", "--set", str(member["index"])) == 0
            assert _launched_uuid(env) == member["session_id"]

    def test_window_narrowing_keeps_canonical_indices(self, fences, env,
                                                      capsys):
        """A narrowed VIEW must not renumber. Regression for a real bug.

        The original version of this test used a window that excluded
        EVERY member, so it compared against an empty view and never
        observed a renumbering -- it passed while
        `csb set show last --window 60` was renumbering from 1 over a
        subset and `csb resume set 1` addressed the full roster. Two
        different sessions behind one number.

        This version pins a NON-EMPTY narrowed view: the survivors must
        keep their canonical numbers (gaps, not 1..n), and resuming by
        one of those numbers must land on the session the view showed.
        """
        # Shutdown is 2026-07-25T16:16Z; GAMMA (07-22) is ~88h before,
        # BETA (07-20) ~136h, ALPHA (07-18) ~184h. A 100h window keeps
        # GAMMA alone -- canonical index 3.
        assert _run(env, "set", "show", "last", "--window", "100",
                    "--json") == 0
        view = json.loads(capsys.readouterr().out)
        assert view["roster_size"] == 3
        assert view["hidden_by_window"] == 2
        assert [m["index"] for m in view["members"]] == [3], \
            "narrowed view renumbered instead of keeping canonical indices"
        assert view["members"][0]["session_id"] == UUID_3

        # The number the user just read must resolve to what they saw.
        assert _run(env, "resume", "--set", "3") == 0
        assert _launched_uuid(env) == UUID_3

    def test_wider_window_is_a_noop_not_an_expansion(self, fences, env,
                                                     capsys):
        """Nothing precedes the previous fence in THIS epoch."""
        assert _run(env, "set", "show", "last", "--window", "10000",
                    "--json") == 0
        view = json.loads(capsys.readouterr().out)
        assert view["hidden_by_window"] == 0
        assert [m["index"] for m in view["members"]] == [1, 2, 3]


# ── errors ───────────────────────────────────────────────────────────────

class TestErrors:
    def test_bare_dashset_is_the_reclaim_menu_of_last(self, fences, env,
                                                      capsys):
        """R1: bare `--set` (const="last") lists the last epoch's members
        not currently open -- with an empty registry, all of them."""
        rc = _run(env, "resume", "--set")
        out = capsys.readouterr().out
        assert rc == 0
        assert "available to reclaim" in out
        assert _claude_calls(env) == []

    def test_literal_set_is_just_a_query_now(self, fences, env, capsys):
        """The sentinel is gone: `csb resume set` resolves 'set' as an
        ordinary session query (here: no such session -> rc 1), never a
        grammar token. A session literally named `set` is resumable."""
        rc = _run(env, "resume", "set")
        assert rc == 1
        err_out = capsys.readouterr()
        assert "which member" not in err_out.err
        assert _claude_calls(env) == []

    def test_out_of_range_names_the_valid_range(self, fences, env, capsys):
        rc = _run(env, "resume", "--set", "99")
        err = capsys.readouterr().err
        assert rc == 1
        assert "1-3" in err
        assert _claude_calls(env) == []

    def test_zero_and_negative_rejected(self, fences, env, capsys):
        assert _run(env, "resume", "--set", "0") == 2
        assert "start at 1" in capsys.readouterr().err
        assert _claude_calls(env) == []

    def test_name_without_number_is_the_reclaim_menu(self, fences, env,
                                                     capsys):
        """Since #64, bare `csb resume set <name>` is the reclaim menu
        (what's still available), not an error. An unknown set still
        errors."""
        rc = _run(env, "resume", "--set", "CSB-STACK")
        err = capsys.readouterr().err
        assert rc == 1
        assert "No set named" in err

    def test_name_plus_non_numeric_still_errors(self, fences, env, capsys):
        rc = _run(env, "resume", "--set", "CSB-STACK", "two")
        err = capsys.readouterr().err
        assert rc == 2
        assert "not a roster number" in err

    def test_unknown_named_set(self, fences, env, capsys):
        rc = _run(env, "resume", "--set", "NO-SUCH-SET", "1")
        assert rc == 1
        assert "No set named" in capsys.readouterr().err

    def test_too_many_args_rejected_by_argparse(self, fences, env, capsys):
        with pytest.raises(SystemExit) as exc:
            _run(env, "resume", "--set", "A", "B", "C")
        assert exc.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err

    def test_empty_named_set(self, fences, env, capsys):
        create_set(env.claude_dir, "EMPTY-SET", [])
        rc = _run(env, "resume", "--set", "EMPTY-SET", "1")
        assert rc == 1
        assert "no members" in capsys.readouterr().err

    def test_member_no_longer_in_index(self, fences, env, capsys):
        create_set(env.claude_dir, "STALE", [UUID_GONE])
        rc = _run(env, "resume", "--set", "STALE", "1")
        err = capsys.readouterr().err
        assert rc == 1
        assert "no longer in the index" in err
        assert _claude_calls(env) == []

    def test_extra_args_on_plain_resume_rejected(self, fences, env, capsys):
        with pytest.raises(SystemExit) as exc:
            _run(env, "resume", "ALPHA__session", "extra")
        assert exc.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err


# ── grammar (parser level) ───────────────────────────────────────────────

class TestGrammar:
    @pytest.mark.parametrize("argv,from_set,query", [
        (["resume", "--set", "2"], "2", None),
        (["resume", "--set", "CSB-STACK", "2"], "CSB-STACK", "2"),
        (["resume", "--set", "last", "2"], "last", "2"),
        (["resume", "--set"], "last", None),          # const kicks in
        (["resume", "MY-SESSION"], None, "MY-SESSION"),
        (["resume", "set"], None, "set"),              # plain query now
    ])
    def test_forms_parse(self, argv, from_set, query):
        args = cli.build_parser().parse_args(argv)
        assert args.from_set == from_set
        assert args.session_id == query

    def test_passthrough_split_precedes_parsing(self):
        """`--` is carved off before argparse sees argv, which is why the
        index form needs no passthrough change at all."""
        head, passthrough = cli._split_passthrough(
            ["resume", "--set", "2", "--", "--fork-session"])
        assert passthrough == ["--fork-session"]
        args = cli.build_parser().parse_args(head)
        assert args.from_set == "2" and args.session_id is None

    def test_legacy_args_without_selector_still_dispatch(self, fences, env):
        """Namespaces built before #63 lack `selector` entirely -- the
        getattr guard in cmd_resume is what keeps them working."""
        args = SimpleNamespace(
            session_id="BETA__session", claude_dir=str(env.claude_dir),
            db=str(env.db), quiet=False, passthrough=[],
            restore_pruned=False, no_restore_pruned=False,
        )
        assert commands_module.cmd_resume(args) == 0
        assert _launched_uuid(env) == UUID_2


# ── the live-session guard (#67) ─────────────────────────────────────────

class TestLiveGuard:
    """Resuming an already-open session warns; forking is always exempt."""

    def _make_live(self, env, monkeypatch, uuid, pid=777):
        """Registry entry (the gate) + process scan (the verifier).

        The guard is REGISTRY-FIRST: without an entry the expensive scan
        never runs -- so making a session 'live' for these tests means
        seeding both signals, exactly as a real open session would.
        """
        import claude_session_backup.live_registry as lreg
        import claude_session_backup.liveness as lv
        from datetime import datetime, timezone
        lreg.record_session_start(env.claude_dir, uuid, source="startup")
        monkeypatch.setattr(lreg, "current_boot_utc",
                            lambda: datetime(2020, 1, 1,
                                             tzinfo=timezone.utc))
        monkeypatch.setattr(
            lv, "scan", lambda: lv.LiveScan(by_uuid={uuid.lower(): pid},
                                            ok=True))

    def test_dead_session_resumes_byte_identically(self, fences, env,
                                                   monkeypatch, capsys):
        """No registry entry -> no guard -- and no process scan either
        (registry-first: the ~free file check gates the 1-2s WMI query,
        so a normal resume pays zero scan latency)."""
        import claude_session_backup.liveness as lv

        def forbidden_scan():
            raise AssertionError("scan must not run without a registry hit")

        monkeypatch.setattr(lv, "scan", forbidden_scan)
        assert _run(env, "resume", "--set", "1") == 0
        assert "appears to be open" not in capsys.readouterr().err
        assert len(_claude_calls(env)) == 1

    def test_live_session_non_tty_warns_and_proceeds(self, fences, env,
                                                     monkeypatch, capsys):
        self._make_live(env, monkeypatch, UUID_1)
        monkeypatch.setattr(commands_module.sys.stdin, "isatty",
                            lambda: False)
        assert _run(env, "resume", "--set", "1") == 0
        err = capsys.readouterr().err
        assert "appears to be open" in err and "777" in err
        assert len(_claude_calls(env)) == 1  # advisory: still launched

    def test_no_allow_live_refuses(self, fences, env, monkeypatch, capsys):
        self._make_live(env, monkeypatch, UUID_1)
        assert _run(env, "resume", "--set", "1", "--no-allow-live") == 0
        err = capsys.readouterr().err
        assert "not resuming" in err
        assert "--fork-session" in err
        assert _claude_calls(env) == []

    def test_allow_live_skips_the_prompt(self, fences, env, monkeypatch,
                                         capsys):
        self._make_live(env, monkeypatch, UUID_1)
        assert _run(env, "resume", "--set", "1", "--allow-live") == 0
        assert len(_claude_calls(env)) == 1

    def test_fork_session_is_exempt(self, fences, env, monkeypatch, capsys):
        """Branching provably mints a new session id -- never warn."""
        self._make_live(env, monkeypatch, UUID_1)
        assert _run(env, "resume", "--set", "1", "--",
                    "--fork-session") == 0
        assert "appears to be open" not in capsys.readouterr().err
        argv = _claude_calls(env)[0]
        assert argv[-1] == "--fork-session"

    def test_other_passthrough_flags_are_not_exempt(self, fences, env,
                                                    monkeypatch, capsys):
        """`--model opus` leaves the collision intact -- still warn."""
        self._make_live(env, monkeypatch, UUID_1)
        monkeypatch.setattr(commands_module.sys.stdin, "isatty",
                            lambda: False)
        assert _run(env, "resume", "--set", "1", "--", "--model", "opus") == 0
        assert "appears to be open" in capsys.readouterr().err

    def test_registry_only_hit_warns_without_pid(self, fences, env,
                                                 monkeypatch, capsys):
        """The registry covers what argv cannot: a fresh session with no
        --resume identifier still triggers the guard (pid-less wording)."""
        import claude_session_backup.live_registry as lreg
        import claude_session_backup.liveness as lv
        monkeypatch.setattr(lv, "scan", lambda: lv.LiveScan(ok=True))
        lreg.record_session_start(env.claude_dir, UUID_1, source="startup")
        from datetime import datetime, timezone
        monkeypatch.setattr(lreg, "current_boot_utc",
                            lambda: datetime(2020, 1, 1,
                                             tzinfo=timezone.utc))
        monkeypatch.setattr(commands_module.sys.stdin, "isatty",
                            lambda: False)
        assert _run(env, "resume", "--set", "1") == 0
        err = capsys.readouterr().err
        assert "appears to be open" in err
        assert "pid" not in err  # registry-only: no process proof

    def test_guard_failure_never_breaks_resume(self, fences, env,
                                               monkeypatch, capsys):
        import claude_session_backup.liveness as lv

        def boom():
            raise RuntimeError("WMI on fire")

        monkeypatch.setattr(lv, "scan", boom)
        assert _run(env, "resume", "--set", "1") == 0
        assert len(_claude_calls(env)) == 1

    def test_fork_in_combo_is_still_exempt(self, fences, env, monkeypatch,
                                           capsys):
        """`-- --model opus --fork-session`: exemption keys on the fork
        flag's PRESENCE (session-id divergence), not on it being alone."""
        self._make_live(env, monkeypatch, UUID_1)
        assert _run(env, "resume", "--set", "1", "--",
                    "--model", "opus", "--fork-session") == 0
        assert "appears to be open" not in capsys.readouterr().err

    def test_tty_prompt_decline_aborts_accept_proceeds(self, fences, env,
                                                       monkeypatch, capsys):
        """TTY path: declining launches nothing (rc 0 -- a choice, not a
        failure; deliberate deviation from the draft's rc 1, mirroring
        the pruned-decision precedent). Accepting proceeds."""
        self._make_live(env, monkeypatch, UUID_1)
        monkeypatch.setattr(commands_module.sys.stdin, "isatty",
                            lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert _run(env, "resume", "--set", "1") == 0
        assert _claude_calls(env) == []

        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        assert _run(env, "resume", "--set", "1") == 0
        assert len(_claude_calls(env)) == 1

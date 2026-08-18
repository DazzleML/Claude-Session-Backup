"""`csb set show current`, `--from` promotion, the reclaim menu (#64).

The registry is real files in a temp claude dir; boot time and the
process scan are patched at their module seams. No real processes, no
real launches (launch mocking mirrors test_resume_set.py).
"""

import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import claude_session_backup.cli as cli
import claude_session_backup.commands as commands_module
import claude_session_backup.live_registry as lr
import claude_session_backup.liveness as lv
from claude_session_backup.index import init_schema, open_db
from claude_session_backup.session_sets import create_set, get_set

BOOT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

UUID_RUN = "11111111-bbbb-cccc-dddd-000000000001"   # indexed + process-verified
UUID_UNV = "22222222-bbbb-cccc-dddd-000000000002"   # indexed, registry-only
UUID_NEW = "33333333-bbbb-cccc-dddd-000000000003"   # NOT indexed (fresh)


@pytest.fixture
def env(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    db = tmp_path / "cur.db"
    conn = open_db(db)
    init_schema(conn, quiet=True)
    for sid, name in ((UUID_RUN, "RUNNING__session"),
                      (UUID_UNV, "UNVERIFIED__session")):
        conn.execute(
            "INSERT INTO sessions (session_id, session_name, project,"
            " start_folder, started_at, last_active_at, is_fork) VALUES"
            " (?, ?, 'C--code-test', 'C:\\code\\test',"
            " '2026-07-01T00:00:00Z', '2026-08-01T14:00:00Z', 0)",
            (sid, name),
        )
    conn.commit()
    conn.close()

    # Registry: three this-boot entries in a deliberate open order.
    for sid, ts, cwd in ((UUID_RUN, "2026-08-01T12:10:00Z", "C:/code/a"),
                         (UUID_UNV, "2026-08-01T12:20:00Z", "C:/code/b"),
                         (UUID_NEW, "2026-08-01T12:30:00Z", "C:/code/fresh")):
        lr.live_dir(claude_dir).mkdir(parents=True, exist_ok=True)
        lr.entry_path(claude_dir, sid).write_text(json.dumps({
            "session_id": sid, "started_at": ts, "source": "startup",
            "cwd": cwd,
        }), encoding="utf-8")

    monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
    monkeypatch.setattr(
        lv, "scan",
        lambda: lv.LiveScan(by_uuid={UUID_RUN.lower(): 4242}, ok=True))

    run_mock = MagicMock(return_value=SimpleNamespace(returncode=0))
    import subprocess as subprocess_module
    monkeypatch.setattr(subprocess_module, "run", run_mock)
    monkeypatch.setattr(commands_module.shutil, "which",
                        lambda name: "C:\\bin\\claude.exe")
    monkeypatch.setattr(commands_module, "_transcript_is_resumable",
                        lambda p: (True, ""))
    return SimpleNamespace(claude_dir=claude_dir, db=db, run=run_mock)


def _run(env, *argv):
    argv = list(argv)
    flags = ["--claude-dir", str(env.claude_dir), "--db", str(env.db)]
    if "--" in argv:
        cut = argv.index("--")
        argv = argv[:cut] + flags + argv[cut:]
    else:
        argv += flags
    return cli.main(argv)


def _claude_calls(env):
    calls = []
    for call in env.run.call_args_list:
        argv = call[0][0] if call[0] else None
        if argv and str(argv[0]).replace(".exe", "").endswith("claude"):
            calls.append(argv)
    return calls


class TestShowCurrent:
    def test_true_open_order_and_tiers(self, env, capsys):
        assert _run(env, "set", "show", "current", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["kind"] == "current"
        assert [m["index"] for m in payload["members"]] == [1, 2, 3]
        # Open order = started_at order, not activity, not name.
        assert [m["session_id"] for m in payload["members"]] == \
            [UUID_RUN, UUID_UNV, UUID_NEW]
        by_id = {m["session_id"]: m for m in payload["members"]}
        assert by_id[UUID_RUN]["live_status"] == "running"
        assert by_id[UUID_RUN]["pid"] == 4242
        assert by_id[UUID_UNV]["live_status"] == "unverified"
        assert by_id[UUID_NEW]["in_index"] is False

    def test_human_output_language(self, env, capsys):
        assert _run(env, "set", "show", "current") == 0
        out = capsys.readouterr().out
        assert "1 running" in out and "process-verified" in out
        assert "[no exit observed]" in out
        assert "RUNNING__session" in out
        # The fresh session falls back to its registry cwd.
        assert "C:/code/fresh" in out

    def test_running_rows_get_fork_hints_not_resume(self, env, capsys):
        """A plain resume hint on a RUNNING session would invite a second
        client onto one transcript (#67's scenario)."""
        _run(env, "set", "show", "current")
        out = capsys.readouterr().out
        assert "csb resume RUNNING__session -- --fork-session" in out
        assert "csb resume RUNNING__session]" not in out  # never a PLAIN hint
        # Unverified rows keep the plain hint -- they are NOT known live.
        assert "csb resume UNVERIFIED__session" in out

    def test_window_rejected(self, env, capsys):
        rc = _run(env, "set", "show", "current", "--window", "5")
        assert rc == 2
        assert "bounded by the boot" in capsys.readouterr().err

    def test_empty_registry_names_the_remedy(self, tmp_path, capsys,
                                             monkeypatch):
        claude_dir = tmp_path / "c2"
        claude_dir.mkdir()
        monkeypatch.setattr(lr, "current_boot_utc", lambda: BOOT)
        rc = cli.main(["set", "show", "current",
                       "--claude-dir", str(claude_dir),
                       "--db", str(tmp_path / "e.db")])
        captured = capsys.readouterr()
        assert rc == 0
        assert "live registry has no sessions" in captured.out
        assert "claude plugin update" in captured.err

    def test_pre_boot_entries_excluded(self, env, capsys):
        # An entry from before the boot belongs to the LAST epoch's
        # story, not to current.
        lr.entry_path(env.claude_dir, "99999999-old0-0000-0000-000000000009"
                      ).write_text(json.dumps({
                          "session_id": "99999999-old0-0000-0000-000000000009",
                          "started_at": "2026-07-30T00:00:00Z",
                          "source": "startup", "cwd": "",
                      }), encoding="utf-8")
        assert _run(env, "set", "show", "current", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["members"]) == 3

    def test_scan_failure_all_rows_unverified(self, env, capsys,
                                              monkeypatch):
        monkeypatch.setattr(lv, "scan", lambda: lv.LiveScan(ok=False))
        assert _run(env, "set", "show", "current") == 0
        out = capsys.readouterr().out
        assert "Process verification unavailable" in out
        assert "[running]" not in out


class TestPromotion:
    def test_from_current_freezes_the_group(self, env, capsys):
        assert _run(env, "set", "new", "MY-GROUP", "--from", "current") == 0
        entry = get_set(env.claude_dir, "MY-GROUP")
        assert [m["session_id"] for m in entry["members"]] == \
            [UUID_RUN, UUID_UNV, UUID_NEW]
        assert "from 'current'" in capsys.readouterr().out

    def test_from_plus_explicit_sessions_union(self, env, capsys):
        conn = sqlite3.connect(env.db)
        conn.execute(
            "INSERT INTO sessions (session_id, session_name, project,"
            " start_folder, started_at, last_active_at, is_fork) VALUES"
            " ('44444444-bbbb-cccc-dddd-000000000004', 'EXTRA__session',"
            " 'C--code-test', 'C:\\code\\test', '2026-07-01T00:00:00Z',"
            " '2026-07-02T00:00:00Z', 0)")
        conn.commit()
        conn.close()
        assert _run(env, "set", "new", "MY-GROUP", "EXTRA__session",
                    "--from", "current") == 0
        entry = get_set(env.claude_dir, "MY-GROUP")
        assert len(entry["members"]) == 4

    def test_new_with_neither_sessions_nor_from_errors(self, env, capsys):
        rc = _run(env, "set", "new", "EMPTYARGS")
        assert rc == 2
        assert "--from" in capsys.readouterr().err

    def test_current_is_reserved_as_a_set_name(self, env, capsys):
        rc = _run(env, "set", "new", "current", UUID_RUN[:8])
        assert rc == 2
        assert "reserved" in capsys.readouterr().err


class TestResumeSetCurrent:
    def test_index_addressing_into_current(self, env):
        assert _run(env, "resume", "--set", "current:2") == 0
        calls = _claude_calls(env)
        assert len(calls) == 1
        assert calls[0][2] == UUID_UNV

    def test_unindexed_member_still_launches(self, env):
        """A fresh session csb has not indexed yet is still resumable --
        claude --resume works regardless of csb's index."""
        assert _run(env, "resume", "--set", "current:3") == 0
        calls = _claude_calls(env)
        assert calls[0][2] == UUID_NEW


class TestReclaimMenu:
    def test_available_means_not_in_registry(self, env, capsys):
        # A named set: one member currently open (in registry), one not.
        create_set(env.claude_dir, "WORK", [UUID_RUN,
                                            "55555555-bbbb-cccc-dddd-000000000005"])
        conn = sqlite3.connect(env.db)
        conn.execute(
            "INSERT INTO sessions (session_id, session_name, project,"
            " start_folder, started_at, last_active_at, is_fork) VALUES"
            " ('55555555-bbbb-cccc-dddd-000000000005', 'CLOSED__session',"
            " 'C--code-test', 'C:\\code\\test', '2026-07-01T00:00:00Z',"
            " '2026-07-02T00:00:00Z', 0)")
        conn.commit()
        conn.close()

        rc = _run(env, "resume", "--set", "WORK")
        out = capsys.readouterr().out
        assert rc == 0
        assert "1 currently open" in out
        assert "1 available to reclaim" in out
        assert "CLOSED__session" in out
        assert "RUNNING__session" not in out          # open -> hidden
        assert "csb resume --set WORK:2" in out          # canonical index kept
        assert _claude_calls(env) == []                # menu launches nothing

    def test_exit_returns_a_member_to_the_menu(self, env, capsys):
        """The liveness rule end to end: closing a session (registry entry
        removed) puts it back on the reclaim list."""
        create_set(env.claude_dir, "WORK", [UUID_RUN])
        _run(env, "resume", "--set", "WORK")
        assert "nothing to reclaim" in capsys.readouterr().out
        lr.record_session_end(env.claude_dir, UUID_RUN)   # clean close
        _run(env, "resume", "--set", "WORK")
        out = capsys.readouterr().out
        assert "1 available to reclaim" in out
        assert "RUNNING__session" in out

    def test_all_open_states_it_plainly(self, env, capsys):
        create_set(env.claude_dir, "ALLOPEN", [UUID_RUN, UUID_UNV])
        rc = _run(env, "resume", "--set", "ALLOPEN")
        assert rc == 0
        assert "nothing to reclaim" in capsys.readouterr().out

    def test_unknown_set_still_errors(self, env, capsys):
        rc = _run(env, "resume", "--set", "NO-SUCH")
        assert rc == 1
        assert "No set named" in capsys.readouterr().err


class TestLastIntegration:
    def test_open_at_shutdown_badges_from_snapshot(self, env, capsys,
                                                   monkeypatch):
        """P4b: when the boundary snapshot covers the epoch's boot, the
        sessions frozen in it get badges in `csb set show last`."""
        import claude_session_backup.epochs as epochs
        monkeypatch.setattr(epochs.sys, "platform", "win32")
        monkeypatch.setattr(epochs, "_run_powershell", lambda *a, **k: (
            "2026-08-01T12:00:30.0000000Z|6005\n"
            "2026-07-25T16:16:32.0000000Z|6006\n"
            "2026-07-15T08:18:17.0000000Z|6005\n"
        ))
        # Sessions active in the epoch (Jul 15 -> Jul 25).
        conn = sqlite3.connect(env.db)
        for sid, name in (("66666666-bbbb-cccc-dddd-000000000006",
                           "EPOCH-OPEN__session"),
                          ("77777777-bbbb-cccc-dddd-000000000007",
                           "EPOCH-CLOSED__session")):
            conn.execute(
                "INSERT INTO sessions (session_id, session_name, project,"
                " start_folder, started_at, last_active_at, is_fork) VALUES"
                " (?, ?, 'C--code-test', 'C:\\code\\test',"
                " '2026-07-16T00:00:00Z', '2026-07-24T00:00:00Z', 0)",
                (sid, name))
        conn.commit()
        conn.close()
        # Snapshot: boot within tolerance of the fence's 12:00:30 boot.
        lr.snapshot_path(env.claude_dir).write_text(json.dumps({
            "version": 1, "boot_at": "2026-08-01T12:00:00Z",
            "captured_at": "2026-08-01T12:05:00Z",
            "open_at_shutdown": [
                {"session_id": "66666666-bbbb-cccc-dddd-000000000006",
                 "started_at": "2026-07-20T00:00:00Z", "source": "startup",
                 "cwd": ""},
            ],
        }), encoding="utf-8")

        assert _run(env, "set", "show", "last") == 0
        out = capsys.readouterr().out
        assert "[open at shutdown]" in out
        idx = out.index
        assert idx("EPOCH-OPEN__session") < idx("[open at shutdown]")

        # --open narrows to badged members, canonical indices kept.
        assert _run(env, "set", "show", "last", "--open", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["snapshot_available"] is True
        assert [m["session_id"] for m in payload["members"]] == \
            ["66666666-bbbb-cccc-dddd-000000000006"]
        assert payload["members"][0]["open_at_shutdown"] is True

    def test_open_without_snapshot_notes_and_shows_all(self, env, capsys,
                                                       monkeypatch):
        import claude_session_backup.epochs as epochs
        monkeypatch.setattr(epochs.sys, "platform", "win32")
        monkeypatch.setattr(epochs, "_run_powershell", lambda *a, **k: (
            "2026-07-25T16:17:18.0000000Z|6005\n"
            "2026-07-25T16:16:32.0000000Z|6006\n"
            "2026-07-15T08:18:17.0000000Z|6005\n"
        ))
        rc = _run(env, "set", "show", "last", "--open")
        captured = capsys.readouterr()
        assert rc == 0
        assert "no boundary snapshot covers this epoch" in captured.err

    def test_missing_snapshot_notes_even_without_open(self, env, capsys,
                                                      monkeypatch):
        """AC5. The note must NOT be gated behind `--open`.

        A bare `set show last` used to render every row badge-less and
        say nothing, so "nobody was open" and "csb has not swept yet"
        looked identical -- the exact ambiguity the tier ladder exists
        to forbid. Observed 2026-08-12: 27 minutes of confidently blank
        output between a forced restart and the first hook fire.
        """
        import claude_session_backup.epochs as epochs
        monkeypatch.setattr(epochs.sys, "platform", "win32")
        monkeypatch.setattr(epochs, "_run_powershell", lambda *a, **k: (
            "2026-07-25T16:17:18.0000000Z|6005\n"
            "2026-07-25T16:16:32.0000000Z|6006\n"
            "2026-07-15T08:18:17.0000000Z|6005\n"
        ))
        rc = _run(env, "set", "show", "last")  # NO --open
        captured = capsys.readouterr()
        assert rc == 0
        assert "UNKNOWN, not none" in captured.err

    def test_proven_empty_boundary_is_not_reported_as_unknown(
            self, env, capsys, monkeypatch):
        """AC12. A boundary csb swept and found empty is a MEASUREMENT.
        Reporting it as ignorance would waste the evidence the sweep
        just produced."""
        import claude_session_backup.epochs as epochs
        import claude_session_backup.live_registry as lr
        monkeypatch.setattr(epochs.sys, "platform", "win32")
        monkeypatch.setattr(epochs, "_run_powershell", lambda *a, **k: (
            "2026-07-25T16:17:18.0000000Z|6005\n"
            "2026-07-25T16:16:32.0000000Z|6006\n"
            "2026-07-15T08:18:17.0000000Z|6005\n"
        ))
        # A swept boundary that named nobody, keyed to this epoch's boot.
        boot = datetime(2026, 7, 25, 16, 17, 18, tzinfo=timezone.utc)
        lr.live_dir(env.claude_dir).mkdir(parents=True, exist_ok=True)
        lr.sweep_boundary(env.claude_dir, boot)

        rc = _run(env, "set", "show", "last", "--json")
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["snapshot_available"] is True, (
            "proven-zero must not collapse back into 'unknown'")


class TestSetForget:
    """`csb set forget` -- the user testifying about what csb cannot know.

    A Ctrl+C'd session leaves its entry behind (SessionEnd is cancelled
    before it can erase it) and a pid-less entry can never be resolved
    by evidence -- both read `[no exit observed]` forever, which is
    indistinguishable from crash evidence. The witness settles it.

    Decisions pinned here: RV1 (it lives in the `set` family, distinct
    from `set rm` which edits declared membership), RV2 (a row with no
    entry errors rather than silently succeeding), RV3 (plain unlink --
    git history is the archive).
    """

    def test_retracts_an_unverified_entry(self, env, capsys):
        """RED-GREEN anchor: the registry-only session (no process
        proof) is retractable, and its file is gone afterwards."""
        assert lr.entry_path(env.claude_dir, UUID_UNV).exists()
        rc = _run(env, "set", "forget", "current:2")
        out = capsys.readouterr().out
        assert rc == 0
        assert "Retracted" in out
        assert not lr.entry_path(env.claude_dir, UUID_UNV).exists()

    def test_refuses_a_verifiably_running_session(self, env, capsys):
        """Asserting 'it is closed' about a process csb can SEE is a
        mistake, not an override -- and the error names the escape."""
        rc = _run(env, "set", "forget", "current:1")
        err = capsys.readouterr().err
        assert rc == 1
        assert "RUNNING" in err and "--force" in err
        assert lr.entry_path(env.claude_dir, UUID_RUN).exists()

    def test_force_overrides_for_privacy(self, env, capsys):
        """--force is legitimate for privacy (an entry naming a path you
        want gone), which is why it exists at all."""
        rc = _run(env, "set", "forget", "current:1", "--force")
        assert rc == 0
        assert "Retracted" in capsys.readouterr().out
        assert not lr.entry_path(env.claude_dir, UUID_RUN).exists()

    def test_row_without_an_entry_errors(self, env, capsys):
        """RV2: an index-derived row is not an observation. Silently
        succeeding would hide that the row reappears next invocation."""
        import sqlite3
        conn = sqlite3.connect(env.db)
        conn.execute(
            "INSERT INTO sessions (session_id, session_name, project,"
            " start_folder, started_at, last_active_at, is_fork) VALUES"
            " ('99999999-bbbb-cccc-dddd-000000000009', 'NOENTRY__session',"
            " 'C--code-test', 'C:\code\test', '2026-07-01T00:00:00Z',"
            " '2026-08-01T14:00:00Z', 0)")
        conn.commit()
        conn.close()
        rc = _run(env, "set", "forget", "NOENTRY__session")
        err = capsys.readouterr().err
        assert rc == 1
        assert "nothing to retract" in err

    def test_bare_invocation_teaches_the_forms(self, env, capsys):
        rc = _run(env, "set", "forget")
        err = capsys.readouterr().err
        assert rc == 2
        assert "current:1" in err

    def test_retraction_echoes_where_the_record_lives(self, env, capsys):
        """RV3: removal is a plain unlink because csb-live/ rides the
        noise commits -- so the echo must say where history is kept, or
        'recoverable' is true but undiscoverable."""
        _run(env, "set", "forget", "current:2")
        err = capsys.readouterr().err
        assert "git log" in err and "csb-live" in err

    def test_forget_is_not_set_rm(self, env, capsys):
        """RV1: distinct verbs for opposite semantics. `set rm` edits
        membership you declared; it must not accept a live view."""
        rc = _run(env, "set", "rm", "current")
        assert rc == 1
        assert "No set named" in capsys.readouterr().err


class TestSetForgetPathTraversal:
    """SECURITY GAP found in the v0.9.10 tester-unbounded audit (2026-08-09),
    confirmed against real (unmocked) subprocess execution before being
    reduced to this regression anchor -- see
    tests/checklists/results/v0.9.10__set-forget__results__2026-08-08.md.

    ``live_registry.read_entries()`` keys each entry by
    ``raw.get("session_id") or path.stem`` -- the JSON BODY's
    ``session_id`` field wins over the entry's own filename.
    ``_materialize_current_roster`` surfaces every ``*.json`` file found
    in ``csb-live/`` this way, indexed and addressable via ordinary
    ``current:N`` tokens, with NO validation that the recorded
    ``session_id`` looks like a UUID or is otherwise filename-safe.
    ``cmd_set_forget`` then calls
    ``live_registry.entry_path(claude_dir, sid).unlink()``, where
    ``entry_path`` is a bare ``Path(claude_dir) / "csb-live" /
    f"{session_id}.json"`` -- no rejection of path separators or ``..``
    segments before that path is built and unlinked.

    Net effect: a file planted in ``csb-live/`` (by anything with write
    access to the Claude data dir -- a compromised plugin, another tool,
    a corrupted sync) with a crafted ``"session_id": "../<name>"`` body
    field becomes an addressable ``current:N`` row whose ``forget``
    deletes ``<claude_dir>/<name>.json`` instead of anything inside
    ``csb-live/``. Deeper ``../../..`` chains reach further outside
    ``claude_dir`` entirely -- this test exercises the shallowest case
    (escaping csb-live/ into its immediate parent) since that alone is
    enough to prove entry_path() performs no containment check at all.

    FIXED at both ends (v0.9.10): `is_safe_entry_id` rejects ids that
    could become a path (separators, `..`, leading dot, drive/UNC
    punctuation, over-long); `read_entries` honours a body-claimed id
    only when it is safe, falling back to the filename stem; and
    `cmd_set_forget` re-checks containment against live_dir at the point
    of no return, because that is the only place csb DELETES from a path
    built out of registry data.

    Why registry contents are untrusted input at all: `csb-live/` rides
    the backup store's commits, so an entry can arrive from another
    machine or a restored store, not just from a local hook.
    """

    def _plant(self, claude_dir, filename, claimed_id):
        """A crafted entry: innocuous filename, hostile body."""
        path = lr.live_dir(claude_dir) / f"{filename}.json"
        path.write_text(json.dumps({
            "session_id": claimed_id, "started_at": "2026-08-01T12:40:00Z",
            "source": "startup", "cwd": "C:/nope",
        }), encoding="utf-8")
        return path

    def test_crafted_session_id_cannot_escape_the_registry_directory(
            self, env, capsys):
        """RED-GREEN anchor: the traversal id must never be honoured."""
        canary = env.claude_dir / "canary.json"
        canary.write_text('{"marker": "do not delete me"}', encoding="utf-8")
        self._plant(env.claude_dir, "evil", "../canary")

        rc = _run(env, "set", "show", "current", "--json")
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        # The hostile id is never adopted: the entry is keyed by its
        # FILENAME, so no row can address anything outside csb-live/.
        assert all(m["session_id"] != "../canary"
                   for m in payload["members"])
        assert any(m["session_id"] == "evil" for m in payload["members"])

        # And forgetting that row removes the planted entry itself --
        # never the file its body pointed at.
        row = next(m for m in payload["members"]
                   if m["session_id"] == "evil")
        _run(env, "set", "forget", f"current:{row['index']}")
        capsys.readouterr()
        assert canary.exists(), "a crafted session_id escaped csb-live/"
        assert not (lr.live_dir(env.claude_dir) / "evil.json").exists()

    @pytest.mark.parametrize("bad", [
        "../canary", "..\canary", "a/b", "a\b", "C:evil", "..",
        ".hidden", "", "x" * 200,
    ])
    def test_entry_path_refuses_unsafe_ids(self, env, bad):
        """The funnel itself refuses, loudly -- callers unlink what it
        returns, so failing beats resolving somewhere unexpected."""
        with pytest.raises(ValueError):
            lr.entry_path(env.claude_dir, bad)

    def test_ordinary_uuids_still_work(self, env):
        """The guard must not break the normal case."""
        p = lr.entry_path(env.claude_dir, UUID_RUN)
        assert p.name == f"{UUID_RUN}.json"
        assert lr.is_safe_entry_id(UUID_RUN)


def test_set_actions_matches_the_registered_subparsers():
    """The implicit-show rewrite tests argv[1] against SET_ACTIONS, so a
    `set` subcommand missing from that tuple is silently rewritten into
    `csb set show <action>` and dies as "No set named 'X'".

    Cost the `forget` verb an hour of confusion when it was added; this
    pins the tuple against the parser's real subparsers so the two
    cannot drift again.
    """
    import argparse as _argparse

    parser = cli.build_parser()
    registered = None
    for action in parser._actions:
        if isinstance(action, _argparse._SubParsersAction):
            set_parser = action.choices.get("set")
            assert set_parser is not None, "no `set` command registered"
            for sub in set_parser._actions:
                if isinstance(sub, _argparse._SubParsersAction):
                    registered = set(sub.choices)
            break
    assert registered, "no `set` subcommands found on the parser"
    assert registered == set(cli.SET_ACTIONS), (
        f"SET_ACTIONS {sorted(cli.SET_ACTIONS)} does not match the "
        f"registered subcommands {sorted(registered)} -- a new action "
        "would be swallowed by the implicit-show rewrite"
    )

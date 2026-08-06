"""`csb set show last`: command layer + CLI grammar (#61).

The real `Get-WinEvent` is never invoked -- `epochs._run_powershell` is
patched (the module-level seam); the human checklist covers a real
reboot on a real box. DB access uses a real temp-file SQLite index so
`--db` routing is exercised end-to-end through `cli.main(argv)`.
"""

import json
import sqlite3
from types import SimpleNamespace

import pytest

import claude_session_backup.cli as cli
import claude_session_backup.commands as commands_module
import claude_session_backup.epochs as epochs
from claude_session_backup.index import init_schema, open_db

# One canned fence history, mirroring the real 2026-07-25 update restart
# (shutdown 16:16:32Z preceded by a 1074, boot 46s later, prior epoch
# starting at the 07-15 boot).
FENCE_OUTPUT = (
    "2026-07-25T16:17:18.0000000Z|6005\n"
    "2026-07-25T16:16:32.0000000Z|6006\n"
    "2026-07-25T16:16:18.0000000Z|1074\n"
    "2026-07-15T08:18:17.0000000Z|6005\n"
    "2026-07-15T08:17:38.0000000Z|6006\n"
)

UUID_IN = "aaaaaaaa-bbbb-cccc-dddd-000000000001"
UUID_OUT = "aaaaaaaa-bbbb-cccc-dddd-000000000002"
UUID_PURGED = "aaaaaaaa-bbbb-cccc-dddd-000000000003"


@pytest.fixture
def fences(monkeypatch):
    """Patch the PowerShell seam with the canned fence history."""
    monkeypatch.setattr(epochs.sys, "platform", "win32")
    monkeypatch.setattr(epochs, "_run_powershell",
                        lambda *a, **k: FENCE_OUTPUT)


@pytest.fixture
def db_path(tmp_path):
    """Temp index with one in-window, one out-of-window, one purged session."""
    path = tmp_path / "test.db"
    conn = open_db(path)
    init_schema(conn, quiet=True)

    def add(sid, name, last_active, deleted_at=None):
        conn.execute(
            "INSERT INTO sessions (session_id, session_name, project,"
            " start_folder, started_at, last_active_at, deleted_at, is_fork)"
            " VALUES (?, ?, 'C--code-test', 'C:\\code\\test',"
            " '2026-07-01T00:00:00Z', ?, ?, 0)",
            (sid, name, last_active, deleted_at),
        )

    add(UUID_IN, "IN-WINDOW__session", "2026-07-23T17:24:00Z")
    add(UUID_OUT, "OUT-OF-WINDOW__session", "2026-06-01T00:00:00Z")
    add(UUID_PURGED, "PURGED__session", "2026-07-24T10:00:00Z",
        deleted_at="2026-07-26T00:00:00Z")
    conn.execute(
        "INSERT INTO scan_history (scanned_at, sessions_found) VALUES"
        " ('2026-07-26T00:00:00Z', 3)"  # AFTER the shutdown -> no advisory
    )
    conn.commit()
    conn.close()
    return path


def _run(argv, tmp_path, db_path):
    """Drive the real CLI entry point (grammar + hoisting + dispatch)."""
    return cli.main(list(argv) + ["--claude-dir", str(tmp_path),
                                  "--db", str(db_path)])


# ── the roster ───────────────────────────────────────────────────────────

class TestSetShowLast:
    def test_roster_renders_in_window_sessions(self, fences, db_path,
                                               tmp_path, capsys):
        rc = _run(["set", "show", "last"], tmp_path, db_path)
        out = capsys.readouterr().out
        assert rc == 0
        assert "IN-WINDOW__session" in out
        assert "OUT-OF-WINDOW__session" not in out
        assert "csb resume IN-WINDOW__session" in out

    def test_shutdown_cause_labeled(self, fences, db_path, tmp_path, capsys):
        _run(["set", "show", "last"], tmp_path, db_path)
        out = capsys.readouterr().out
        assert "restart initiated by a process" in out

    def test_honest_language(self, fences, db_path, tmp_path, capsys):
        """Says 'active ...', orders by activity, never claims sessions
        were open. ("open order is not tracked" is the one allowed use.)"""
        _run(["set", "show", "last"], tmp_path, db_path)
        out = capsys.readouterr().out
        assert "active since the previous fence" in out
        assert "activity order" in out
        for forbidden in ("open at shutdown", "sessions open", "was open",
                          "were open"):
            assert forbidden not in out

    def test_purged_member_marked_with_resume_hint(self, fences, db_path,
                                                   tmp_path, capsys):
        _run(["set", "show", "last"], tmp_path, db_path)
        out = capsys.readouterr().out
        assert "PURGED__session" in out
        assert "[purged -- resume will offer restore-from-git]" in out
        assert "csb resume PURGED__session" in out  # plain hint; v0.3.14 path restores

    def test_stable_full_roster_numbering(self, fences, db_path, tmp_path,
                                          capsys):
        _run(["set", "show", "last"], tmp_path, db_path)
        out = capsys.readouterr().out
        # Activity ASC: IN-WINDOW (07-23) is 1, PURGED (07-24) is 2.
        assert out.index("1. IN-WINDOW__session") < out.index("2. PURGED__session")

    def test_window_narrows_the_view(self, fences, db_path, tmp_path, capsys):
        """--window narrows what is DISPLAYED, not the epoch itself.

        A 24h window from the 07-25 16:16 shutdown excludes both members
        (07-23 17:24 is ~47h before; 07-24 10:00 is ~30h before), so the
        view empties -- and says so in terms of the narrowing rather
        than blaming the epoch.
        """
        rc = _run(["set", "show", "last", "--window", "24"], tmp_path, db_path)
        out = capsys.readouterr().out
        assert rc == 0
        assert "IN-WINDOW__session" not in out
        assert "No sessions active within 24h of the shutdown" in out
        # The epoch header still reports the real epoch, not the filter.
        assert "since the previous fence" in out

    def test_missing_timestamp_counted_not_dropped(self, fences, db_path,
                                                   tmp_path, capsys):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO sessions (session_id, session_name, project,"
            " start_folder, started_at, last_active_at, is_fork) VALUES"
            " ('aaaaaaaa-bbbb-cccc-dddd-000000000004', 'NO-TS__session',"
            " 'C--code-test', 'C:\\code\\test', '2026-07-01T00:00:00Z',"
            " NULL, 0)"
        )
        conn.commit()
        conn.close()
        _run(["set", "show", "last"], tmp_path, db_path)
        out = capsys.readouterr().out
        assert "1 session lacks activity timestamps" in out


class TestDuplicateNameHints:
    """A hint that cannot resolve is worse than no hint.

    Session names collide readily -- one project branched across topics
    yields several rows sharing a name, differing only by folder. For
    those rows `csb resume <name>` errors on ambiguity, so the roster
    must offer the UUID instead of recommending a command it knows will
    fail. Found by the v0.8.2 checklist sweep against real data.
    """

    def _add_dupe(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO sessions (session_id, session_name, project,"
            " start_folder, started_at, last_active_at, is_fork) VALUES"
            " ('bbbbbbbb-cccc-dddd-eeee-000000000001', 'IN-WINDOW__session',"
            " 'C--code-other', 'C:\\code\\other', '2026-07-01T00:00:00Z',"
            " '2026-07-23T18:00:00Z', 0)"
        )
        conn.commit()
        conn.close()

    def test_duplicate_names_fall_back_to_uuid(self, fences, db_path,
                                               tmp_path, capsys):
        self._add_dupe(db_path)
        _run(["set", "show", "last"], tmp_path, db_path)
        out = capsys.readouterr().out
        # Both duplicate rows offer a resolvable UUID...
        assert "csb resume aaaaaaaa-bbbb-cccc-dddd-000000000001" in out
        assert "csb resume bbbbbbbb-cccc-dddd-eeee-000000000001" in out
        # ...and neither offers the ambiguous name.
        assert "csb resume IN-WINDOW__session" not in out

    def test_unique_names_still_get_readable_hints(self, fences, db_path,
                                                   tmp_path, capsys):
        self._add_dupe(db_path)
        _run(["set", "show", "last"], tmp_path, db_path)
        out = capsys.readouterr().out
        # PURGED__session is unique, so it keeps the friendly form.
        assert "csb resume PURGED__session" in out


# ── freshness advisory (#59 family) ──────────────────────────────────────

class TestFreshnessAdvisory:
    def test_stale_index_advises_on_stderr(self, fences, db_path, tmp_path,
                                           capsys):
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM scan_history")
        conn.execute(
            "INSERT INTO scan_history (scanned_at, sessions_found) VALUES"
            " ('2026-07-20T00:00:00Z', 3)"  # BEFORE the 07-25 shutdown
        )
        conn.commit()
        conn.close()
        _run(["set", "show", "last"], tmp_path, db_path)
        captured = capsys.readouterr()
        assert "before this shutdown" in captured.err
        assert "csb backup" in captured.err
        assert "before this shutdown" not in captured.out

    def test_fresh_index_no_advisory(self, fences, db_path, tmp_path, capsys):
        _run(["set", "show", "last"], tmp_path, db_path)
        assert "before this shutdown" not in capsys.readouterr().err


# ── --json ───────────────────────────────────────────────────────────────

class TestJson:
    def test_stdout_is_pure_json_with_envelope(self, fences, db_path,
                                               tmp_path, capsys):
        rc = _run(["set", "show", "last", "--json"], tmp_path, db_path)
        out = capsys.readouterr().out
        assert rc == 0
        payload = json.loads(out)  # parses == purity
        assert payload["kind"] == "epoch"
        assert payload["epoch"]["cause"] == "initiated-by-process"
        assert payload["epoch"]["window_source"] == "previous-fence"
        ids = [m["session_id"] for m in payload["members"]]
        assert UUID_IN in ids and UUID_OUT not in ids
        member = payload["members"][0]
        assert member["index"] == 1
        for key in ("session_name", "purged", "is_fork", "in_index"):
            assert key in member

    def test_stale_advisory_stays_off_json_stdout(self, fences, db_path,
                                                  tmp_path, capsys):
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE scan_history SET scanned_at = '2026-07-20T00:00:00Z'")
        conn.commit()
        conn.close()
        _run(["set", "show", "last", "--json"], tmp_path, db_path)
        captured = capsys.readouterr()
        json.loads(captured.out)  # still pure
        assert "csb backup" in captured.err

    def test_empty_view_still_emits_envelope(self, fences, db_path,
                                             tmp_path, capsys):
        rc = _run(["set", "show", "last", "--json", "--window", "1"],
                  tmp_path, db_path)
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["members"] == []
        # The EPOCH is unchanged by a display filter -- its window is
        # still the previous fence. The narrowing is reported separately,
        # which is what keeps `resume set <N>` addressable.
        assert payload["epoch"]["window_source"] == "previous-fence"
        assert payload["display_window_hours"] == 1
        assert payload["roster_size"] == 2
        assert payload["hidden_by_window"] == 2


# ── empty / error states ─────────────────────────────────────────────────

class TestEmptyAndErrorStates:
    def test_no_fences_graceful(self, monkeypatch, db_path, tmp_path, capsys):
        monkeypatch.setattr(epochs.sys, "platform", "win32")
        monkeypatch.setattr(epochs, "_run_powershell", lambda *a, **k: "")
        rc = _run(["set", "show", "last"], tmp_path, db_path)
        out = capsys.readouterr().out
        assert rc == 0
        assert "No shutdown fence found" in out

    def test_posix_clear_error(self, monkeypatch, db_path, tmp_path, capsys):
        monkeypatch.setattr(epochs.sys, "platform", "linux")
        rc = _run(["set", "show", "last"], tmp_path, db_path)
        captured = capsys.readouterr()
        assert rc == 1
        assert "Windows-only" in captured.err
        assert "#60" in captured.err

    def test_unknown_set_name_names_both_routes(self, fences, db_path,
                                                tmp_path, capsys):
        """A name that is neither 'last' nor an existing set errors with
        both ways out -- the epoch keyword and the listing command."""
        rc = _run(["set", "show", "CSB-STACK"], tmp_path, db_path)
        captured = capsys.readouterr()
        assert rc == 1
        assert "No set named 'CSB-STACK'" in captured.err
        assert "last" in captured.err
        assert "csb set list" in captured.err

    def test_empty_index_gets_guidance(self, fences, tmp_path, capsys):
        empty = tmp_path / "empty.db"
        conn = open_db(empty)
        init_schema(conn, quiet=True)
        conn.commit()
        conn.close()
        rc = _run(["set", "show", "last"], tmp_path, empty)
        out = capsys.readouterr().out
        assert rc == 0
        assert "csb backup" in out  # the index-is-empty guidance


# ── CLI grammar ──────────────────────────────────────────────────────────

class TestCliGrammar:
    def test_bare_set_menu_on_stderr_rc2(self, tmp_path, capsys):
        rc = cli.main(["set"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "csb set show last" in captured.err

    def test_common_flag_hoisting_both_positions(self, fences, db_path,
                                                 tmp_path, capsys):
        # `csb --quiet set show last` -- flags before the verb must hoist.
        rc = cli.main(["--quiet", "set", "show", "last",
                       "--claude-dir", str(tmp_path), "--db", str(db_path)])
        assert rc == 0

    def test_passthrough_rejected(self, tmp_path, db_path, capsys):
        rc = cli.main(["set", "show", "last", "--", "--fork-session"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "--" in captured.err

    def test_parser_accepts_window_float(self):
        args = cli.build_parser().parse_args(
            ["set", "show", "last", "--window", "36.5"])
        assert args.window == 36.5
        assert args.set_action == "show"
        assert args.set_name == "last"

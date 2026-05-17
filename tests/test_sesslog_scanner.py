"""Tests for sesslog folder discovery and source resolution."""

from pathlib import Path

import pytest

from claude_session_backup.sesslog_scanner import (
    SourceRow,
    list_sesslog_folders,
    list_session_sources,
    _extract_uuid,
)


SESSION_UUID = "f0ead009-7d34-4ab3-9246-aae835dacaa8"


# ── _extract_uuid ─────────────────────────────────────────────────────


def test_extract_uuid_canonical_format():
    name = "CLAUDE-SESSION-BACKUP__making-sure__f0ead009-7d34-4ab3-9246-aae835dacaa8_Extreme"
    assert _extract_uuid(name) == SESSION_UUID


def test_extract_uuid_uppercase_input_lowercased():
    name = "FOO__F0EAD009-7D34-4AB3-9246-AAE835DACAA8_User"
    assert _extract_uuid(name) == SESSION_UUID


def test_extract_uuid_no_match_returns_none():
    assert _extract_uuid("no-uuid-here") is None
    assert _extract_uuid("almost__1234-5678__not-quite") is None
    assert _extract_uuid(SESSION_UUID) is None  # no surrounding __ prefix


def test_extract_uuid_missing_user_suffix_returns_none():
    # No "_<user>" tail -> not a sesslog folder name
    assert _extract_uuid(f"FOO__{SESSION_UUID}") is None


# ── list_sesslog_folders ──────────────────────────────────────────────


def test_list_sesslog_folders_empty_dir(tmp_path):
    (tmp_path / "sesslogs").mkdir()
    assert list_sesslog_folders(tmp_path) == {}


def test_list_sesslog_folders_no_sesslogs_dir(tmp_path):
    assert list_sesslog_folders(tmp_path) == {}


def test_list_sesslog_folders_finds_uuid_folder(tmp_path):
    sesslogs = tmp_path / "sesslogs"
    folder = sesslogs / f"SESSION-NAME__{SESSION_UUID}_Extreme"
    folder.mkdir(parents=True)

    result = list_sesslog_folders(tmp_path)
    assert result == {SESSION_UUID: folder}


def test_list_sesslog_folders_skips_non_uuid_folders(tmp_path):
    sesslogs = tmp_path / "sesslogs"
    (sesslogs / "random-folder").mkdir(parents=True)
    (sesslogs / "no_uuid_here").mkdir()
    valid = sesslogs / f"VALID__{SESSION_UUID}_Extreme"
    valid.mkdir()

    result = list_sesslog_folders(tmp_path)
    assert result == {SESSION_UUID: valid}


def test_list_sesslog_folders_skips_files(tmp_path):
    sesslogs = tmp_path / "sesslogs"
    sesslogs.mkdir()
    (sesslogs / f"file__{SESSION_UUID}_Extreme.log").write_text("not a dir")

    assert list_sesslog_folders(tmp_path) == {}


def test_list_sesslog_folders_dedupes_uuid_last_wins(tmp_path):
    """If two folders share a UUID, the lex-later one wins (rename race)."""
    sesslogs = tmp_path / "sesslogs"
    earlier = sesslogs / f"AAAA__{SESSION_UUID}_Extreme"
    later = sesslogs / f"ZZZZ__{SESSION_UUID}_Extreme"
    earlier.mkdir(parents=True)
    later.mkdir()

    result = list_sesslog_folders(tmp_path)
    assert result[SESSION_UUID] == later


# ── list_session_sources ──────────────────────────────────────────────


def test_list_session_sources_only_jsonl_when_no_sesslog_folder(tmp_path):
    jsonl = tmp_path / "foo.jsonl"
    jsonl.write_text("{}")

    rows = list_session_sources(jsonl, sesslog_folder=None)
    assert len(rows) == 1
    assert rows[0].source_type == "jsonl"
    assert rows[0].source_path == str(jsonl)
    assert rows[0].size_bytes == 2


def test_list_session_sources_picks_up_convo_files(tmp_path):
    folder = tmp_path / f"session__{SESSION_UUID}_Extreme"
    folder.mkdir()
    convo = folder / ".convo_bash.exe__session.log"
    convo.write_text("[[2026-05-16 10:00:00]] {USER: hi}\n")

    jsonl = tmp_path / "foo.jsonl"
    jsonl.write_text("")

    rows = list_session_sources(jsonl, folder)
    types = [r.source_type for r in rows]
    assert "convo" in types
    assert "jsonl" in types
    assert "sesslog" not in types


def test_list_session_sources_picks_up_sesslog_main_files(tmp_path):
    folder = tmp_path / f"session__{SESSION_UUID}_Extreme"
    folder.mkdir()
    sesslog = folder / ".sesslog_bash.exe__session.log"
    sesslog.write_text("[[2026-05-16 10:00:00]] {USER: hi}\n")

    jsonl = tmp_path / "foo.jsonl"
    jsonl.write_text("")

    rows = list_session_sources(jsonl, folder)
    types = [r.source_type for r in rows]
    assert "sesslog" in types


def test_list_session_sources_filters_sesslog_subchannels(tmp_path):
    """Tool-call subchannels (.sesslog-bash_, .sesslog-grep_, ...) are ignored."""
    folder = tmp_path / f"session__{SESSION_UUID}_Extreme"
    folder.mkdir()
    (folder / ".sesslog-bash_pid.log").write_text("bash output")
    (folder / ".sesslog-grep_pid.log").write_text("grep output")
    (folder / ".sesslog-glob_pid.log").write_text("glob output")
    (folder / ".sesslog-powershell_pid.log").write_text("ps output")

    jsonl = tmp_path / "foo.jsonl"
    jsonl.write_text("")

    rows = list_session_sources(jsonl, folder)
    # Only JSONL should be present -- every sesslog file is a subchannel
    assert [r.source_type for r in rows] == ["jsonl"]


def test_list_session_sources_filters_ancillary_channels(tmp_path):
    """Shell, tasks, agents, overflow files are ignored."""
    folder = tmp_path / f"session__{SESSION_UUID}_Extreme"
    folder.mkdir()
    (folder / ".shell_bash.log").write_text("shell")
    (folder / ".shell-bash_bash.log").write_text("shell-bash")
    (folder / ".tasks_bash.log").write_text("tasks")
    (folder / ".agents_bash.log").write_text("agents")
    (folder / ".overflow_migrated_v0.3.7").write_text("migrated")

    jsonl = tmp_path / "foo.jsonl"
    jsonl.write_text("")

    rows = list_session_sources(jsonl, folder)
    assert [r.source_type for r in rows] == ["jsonl"]


def test_list_session_sources_realistic_folder(tmp_path):
    """End-to-end with all source types and ancillary noise."""
    folder = tmp_path / f"session__{SESSION_UUID}_Extreme"
    folder.mkdir()
    (folder / ".convo_bash.log").write_text("convo")
    (folder / ".sesslog_bash.log").write_text("sesslog")
    (folder / ".sesslog-bash_pid.log").write_text("subchannel")
    (folder / ".shell_bash.log").write_text("shell")
    (folder / ".tasks_bash.log").write_text("tasks")

    jsonl = tmp_path / "foo.jsonl"
    jsonl.write_text("jsonl content")

    rows = list_session_sources(jsonl, folder)
    types = sorted(r.source_type for r in rows)
    assert types == ["convo", "jsonl", "sesslog"]


def test_list_session_sources_records_size_and_mtime(tmp_path):
    folder = tmp_path / f"session__{SESSION_UUID}_Extreme"
    folder.mkdir()
    convo = folder / ".convo_bash.log"
    convo.write_text("hello")

    jsonl = tmp_path / "foo.jsonl"
    jsonl.write_text("world!")

    rows = list_session_sources(jsonl, folder)
    by_type = {r.source_type: r for r in rows}
    assert by_type["convo"].size_bytes == 5
    assert by_type["jsonl"].size_bytes == 6
    # mtimes are ISO 8601 strings, at minimum should look like 2026-...
    assert by_type["convo"].mtime is not None
    assert by_type["convo"].mtime.startswith("20")


def test_list_session_sources_missing_jsonl_still_recorded(tmp_path):
    """If JSONL is missing, the row is still emitted with NULL size/mtime."""
    folder = tmp_path / f"session__{SESSION_UUID}_Extreme"
    folder.mkdir()

    rows = list_session_sources(tmp_path / "absent.jsonl", folder)
    by_type = {r.source_type: r for r in rows}
    assert "jsonl" in by_type
    assert by_type["jsonl"].size_bytes is None
    assert by_type["jsonl"].mtime is None

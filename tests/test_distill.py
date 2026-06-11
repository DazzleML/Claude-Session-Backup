"""Tests for `csb distill` (#12): chat-log rendering, channel selection,
filters, policy gate, and output routing.

The distilled output is a READING layer over the preserved JSONL --
these tests pin the renderer's exact IM-log shape (the spec's
``YYYY/MM/DD hh:mm:ss <Identity>:`` + four-newline turn separation)."""

import argparse
import os
from pathlib import Path

import pytest

from claude_session_backup.distill import (
    ChatMessage,
    ToolLine,
    _identity,
    _is_plumbing,
    build_chat_messages,
    pick_channels,
    render_chat_log,
)
from claude_session_backup.index import init_schema, open_db, upsert_session
from claude_session_backup.metadata import SessionMetadata


UUID_D = "dddd0001-1111-2222-3333-444444444444"

_JSONL_CONTENT = (
    '{"type":"user","timestamp":"2026-05-01T10:00:00Z",'
    '"message":{"role":"user","content":"hello there"}}\n'
    '{"type":"assistant","timestamp":"2026-05-01T10:01:00Z",'
    '"message":{"role":"assistant","content":['
    '{"type":"text","text":"hi! let me read that file"},'
    '{"type":"tool_use","id":"t1","name":"Read",'
    '"input":{"file_path":"C:/code/x/file.py"}},'
    '{"type":"tool_use","id":"t2","name":"Bash",'
    '"input":{"command":"git status\\nsecond line ignored"}}]}}\n'
)

_CONVO_CONTENT = (
    "[[2026-05-01 10:00:00]] {USER: hello convo}\n"
    "[[2026-05-01 10:01:00]] {AI: hi from convo}\n"
    "[[2026-05-01 10:02:00]] {AGENT:explore: agent says things}\n"
)

_SESSLOG_MIXED = (
    "[[2026-05-01 10:00:00]] {USER: hello sesslog}\n"
    '[[2026-05-01 10:01:30]] {Read: "C:/code/x/file.py" preview}\n'
    "[[2026-05-01 10:01:40]] {Bash: git log --oneline}\n"
)

_SESSLOG_SHELL_ONLY = (
    '[[2026-05-01 10:01:30]] {Read: "C:/code/x/file.py" preview}\n'
    "[[2026-05-01 10:01:40]] {Bash: git log --oneline}\n"
)


def _row(source_type, source_path):
    return {"source_type": source_type, "source_path": str(source_path)}


# ── unit: identity + plumbing ────────────────────────────────────────────


def test_identity_mapping():
    assert _identity("USER") == "User"
    assert _identity("AI") == "Claude"
    assert _identity("AGENT") == "Agent"
    assert _identity("AGENT:explore") == "Agent:explore"


def test_plumbing_filter_recognizes_command_envelopes():
    assert _is_plumbing("<local-command-caveat>Caveat: ...")
    assert _is_plumbing("  <command-name>/rename</command-name>")
    assert _is_plumbing("<local-command-stdout>ok</local-command-stdout>")
    assert not _is_plumbing("normal message mentioning <command-name> later")


# ── renderer format exactness ────────────────────────────────────────────


def test_render_chat_log_exact_shape():
    """The spec, literally: timestamped identity header, verbatim block,
    FOUR newlines between turns, indented tool one-liners."""
    messages = [
        ChatMessage(ts_sort=1.0, ts_display="2026/05/01 10:00:00",
                    identity="User", text="hello there"),
        ChatMessage(ts_sort=2.0, ts_display="2026/05/01 10:01:00",
                    identity="Claude", text="hi!\nmulti-line reply",
                    tools=[ToolLine(2.5, "Read", "C:/code/x/file.py"),
                           ToolLine(2.6, "Bash", "git status")]),
    ]
    out = "".join(render_chat_log(
        messages, session_name="my-session", session_id=UUID_D,
        source_label="jsonl", mode="both",
    ))
    assert out.startswith("# my-session\n")
    assert f"session: {UUID_D}" in out
    assert "2026/05/01 10:00:00 <User>:\nhello there\n\n\n\n" in out
    assert "2026/05/01 10:01:00 <Claude>:\nhi!\nmulti-line reply\n" in out
    assert "\n    [Read] C:/code/x/file.py\n" in out
    assert "\n    [Bash] git status\n" in out
    # Four-newline turn separation (3 blank lines) after every turn.
    assert out.count("\n\n\n\n") >= 2


# ── channel selection ────────────────────────────────────────────────────


def test_pick_channels_prefers_jsonl(tmp_path):
    """jsonl > sesslog > convo: distill renders csb's own preserved
    record -- the verbatim JSONL beats the logger's derived channels
    (the OPPOSITE of search's speed-first preference). Tool stream
    follows the same order."""
    convo = tmp_path / ".convo_x.log"
    convo.write_text(_CONVO_CONTENT, encoding="utf-8")
    sesslog = tmp_path / ".sesslog_x.log"
    sesslog.write_text(_SESSLOG_MIXED, encoding="utf-8")
    jsonl = tmp_path / f"{UUID_D}.jsonl"
    jsonl.write_text(_JSONL_CONTENT, encoding="utf-8")
    ctype, cpath, tools = pick_channels(
        [_row("convo", convo), _row("sesslog", sesslog)], jsonl,
    )
    assert ctype == "jsonl" and cpath == str(jsonl)
    assert tools == [str(jsonl)]


def test_pick_channels_no_jsonl_sesslog_with_blocks(tmp_path):
    """Without a JSONL on disk, a conversation-bearing sesslog serves."""
    sesslog = tmp_path / ".sesslog_x.log"
    sesslog.write_text(_SESSLOG_MIXED, encoding="utf-8")
    ctype, cpath, tools = pick_channels([_row("sesslog", sesslog)], None)
    assert ctype == "sesslog" and cpath == str(sesslog)
    assert tools == [str(sesslog)]


def test_pick_channels_no_jsonl_shell_only_sesslog_falls_to_convo(tmp_path):
    """A block-less sesslog can't serve conversation (the #36 philosophy)
    -> fall to the convo channel; the sesslog STILL serves the tool
    stream."""
    sesslog = tmp_path / ".sesslog_shell.log"
    sesslog.write_text(_SESSLOG_SHELL_ONLY, encoding="utf-8")
    convo = tmp_path / ".convo_x.log"
    convo.write_text(_CONVO_CONTENT, encoding="utf-8")
    ctype, cpath, tools = pick_channels(
        [_row("sesslog", sesslog), _row("convo", convo)], None,
    )
    assert ctype == "convo" and cpath == str(convo)
    assert tools == [str(sesslog)]


def test_pick_channels_jsonl_only(tmp_path):
    """No logger files at all -- csb stays standalone."""
    jsonl = tmp_path / f"{UUID_D}.jsonl"
    jsonl.write_text(_JSONL_CONTENT, encoding="utf-8")
    ctype, cpath, tools = pick_channels([], jsonl)
    assert ctype == "jsonl"
    assert tools == [str(jsonl)]


# ── assembly: filters + tool attachment ──────────────────────────────────


def test_build_messages_jsonl_both_attaches_tools(tmp_path):
    jsonl = tmp_path / f"{UUID_D}.jsonl"
    jsonl.write_text(_JSONL_CONTENT, encoding="utf-8")
    msgs = build_chat_messages(
        convo_type="jsonl", convo_path=str(jsonl),
        tool_paths=[str(jsonl)], session_id=UUID_D, mode="both",
    )
    assert [m.identity for m in msgs] == ["User", "Claude"]
    claude = msgs[1]
    labels = sorted(t.label for t in claude.tools)
    assert labels == ["Bash", "Read"]
    bash = next(t for t in claude.tools if t.label == "Bash")
    assert bash.target == "git status"  # first line only, never the rest


def test_build_messages_convo_mode_has_no_tools(tmp_path):
    jsonl = tmp_path / f"{UUID_D}.jsonl"
    jsonl.write_text(_JSONL_CONTENT, encoding="utf-8")
    msgs = build_chat_messages(
        convo_type="jsonl", convo_path=str(jsonl),
        tool_paths=[str(jsonl)], session_id=UUID_D, mode="convo",
    )
    assert msgs and all(not m.tools for m in msgs)


def test_build_messages_tools_mode_drops_text(tmp_path):
    jsonl = tmp_path / f"{UUID_D}.jsonl"
    jsonl.write_text(_JSONL_CONTENT, encoding="utf-8")
    msgs = build_chat_messages(
        convo_type="jsonl", convo_path=str(jsonl),
        tool_paths=[str(jsonl)], session_id=UUID_D, mode="tools",
    )
    assert msgs
    assert all(m.tools for m in msgs)
    assert all(m.text == "" for m in msgs)


def test_build_messages_sesslog_convo_and_tools(tmp_path):
    sesslog = tmp_path / ".sesslog_x.log"
    sesslog.write_text(_SESSLOG_MIXED, encoding="utf-8")
    msgs = build_chat_messages(
        convo_type="sesslog", convo_path=str(sesslog),
        tool_paths=[str(sesslog)], session_id=UUID_D, mode="both",
    )
    assert [m.identity for m in msgs] == ["User"]
    labels = sorted(t.label for t in msgs[0].tools)
    assert labels == ["Bash", "Read"]


def test_build_messages_convo_channel(tmp_path):
    convo = tmp_path / ".convo_x.log"
    convo.write_text(_CONVO_CONTENT, encoding="utf-8")
    msgs = build_chat_messages(
        convo_type="convo", convo_path=str(convo),
        tool_paths=[], session_id=UUID_D, mode="both",
    )
    assert [m.identity for m in msgs] == ["User", "Claude", "Agent:explore"]
    assert msgs[0].ts_display == "2026/05/01 10:00:00"


def test_jsonl_plumbing_events_excluded(tmp_path):
    jsonl = tmp_path / f"{UUID_D}.jsonl"
    jsonl.write_text(
        '{"type":"user","timestamp":"2026-05-01T09:59:00Z",'
        '"message":{"role":"user","content":'
        '"<command-name>/rename</command-name>"}}\n' + _JSONL_CONTENT,
        encoding="utf-8",
    )
    msgs = build_chat_messages(
        convo_type="jsonl", convo_path=str(jsonl),
        tool_paths=[], session_id=UUID_D, mode="convo",
    )
    assert [m.identity for m in msgs] == ["User", "Claude"]
    assert "command-name" not in msgs[0].text


# ── cmd_distill flows ────────────────────────────────────────────────────


def _make_distill_env(tmp_path):
    claude = tmp_path / "claude"
    slug = "C--code-dproj"
    jsonl_rel = f"projects/{slug}/{UUID_D}.jsonl"
    full = claude / jsonl_rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(_JSONL_CONTENT, encoding="utf-8")
    db = tmp_path / "d.db"
    conn = open_db(db)
    init_schema(conn)
    upsert_session(
        conn,
        SessionMetadata(session_id=UUID_D, session_name="distill-me",
                        project=slug),
        jsonl_path=jsonl_rel, jsonl_size=100, jsonl_mtime=1700000000.0,
        scanned_at="2026-05-01T10:00:00Z",
    )
    conn.commit()
    conn.close()
    return claude, db


def _distill_args(db, claude, **kw):
    defaults = dict(
        query=None, claude_dir=str(claude), db=str(db), quiet=True,
        filter=None, source=None, output=None, stdout=False,
        restore_pruned=False, no_restore_pruned=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_cmd_distill_stdout_flag(tmp_path, capsys):
    from claude_session_backup.commands import cmd_distill
    claude, db = _make_distill_env(tmp_path)
    rc = cmd_distill(_distill_args(db, claude, query="distill-me", stdout=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "# distill-me" in out
    assert "<User>:\nhello there" in out
    assert "    [Bash] git status" in out


def test_cmd_distill_output_file(tmp_path, capsys):
    from claude_session_backup.commands import cmd_distill
    claude, db = _make_distill_env(tmp_path)
    dest = tmp_path / "log.md"
    rc = cmd_distill(_distill_args(
        db, claude, query=UUID_D[:8], output=str(dest),
    ))
    assert rc == 0
    text = dest.read_text(encoding="utf-8")
    assert "<User>:\nhello there" in text
    assert "Distilled" in capsys.readouterr().out


def test_cmd_distill_default_writes_canonical(tmp_path, capsys):
    """The DEFAULT (no flags) writes the canonical file -- a distilled log
    is a document, not console spam (user design call)."""
    from claude_session_backup.commands import cmd_distill
    claude, db = _make_distill_env(tmp_path)
    rc = cmd_distill(_distill_args(db, claude, query=UUID_D[:8]))
    assert rc == 0
    canonical = claude / "distilled" / "C--code-dproj" / f"{UUID_D}.md"
    assert canonical.is_file()
    assert "<User>:" in canonical.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "Distilled" in out and str(canonical) in out
    assert "<User>:" not in out  # the log itself did NOT hit stdout


def test_cmd_distill_bare_o_same_as_default(tmp_path, capsys):
    from claude_session_backup.commands import cmd_distill
    claude, db = _make_distill_env(tmp_path)
    rc = cmd_distill(_distill_args(db, claude, query=UUID_D[:8], output=""))
    assert rc == 0
    canonical = claude / "distilled" / "C--code-dproj" / f"{UUID_D}.md"
    assert canonical.is_file()


def test_cmd_distill_policy_never_refuses(tmp_path, capsys, monkeypatch):
    from claude_session_backup.commands import cmd_distill
    claude, db = _make_distill_env(tmp_path)
    monkeypatch.setattr(
        "claude_session_backup.commands._get_config",
        lambda args: {
            "claude_dir": str(claude), "index_path": str(db),
            "distill_policy": "never", "distill_filter": "both",
        },
    )
    rc = cmd_distill(_distill_args(db, claude, query="distill-me"))
    err = capsys.readouterr().err
    assert rc == 1
    assert "never" in err and "csb config distill_policy" in err


def test_cmd_distill_unresolved_query(tmp_path, capsys):
    from claude_session_backup.commands import cmd_distill
    claude, db = _make_distill_env(tmp_path)
    rc = cmd_distill(_distill_args(db, claude, query="zzz-nope"))
    assert rc == 1
    assert "no sessions match" in capsys.readouterr().err


# ── backup `always` hook ─────────────────────────────────────────────────


def test_refresh_distilled_files_writes_and_skips_fresh(tmp_path):
    from types import SimpleNamespace
    from claude_session_backup.commands import _refresh_distilled_files
    claude, db = _make_distill_env(tmp_path)
    conn = open_db(db)
    init_schema(conn)
    sf = SimpleNamespace(session_id=UUID_D, jsonl_mtime=1700000000.0)
    config = {"claude_dir": str(claude), "distill_filter": "both"}

    n = _refresh_distilled_files(conn, config, [sf], quiet=True)
    assert n == 1
    canonical = claude / "distilled" / "C--code-dproj" / f"{UUID_D}.md"
    assert canonical.is_file()

    # Second run: file is fresh (its mtime >= jsonl_mtime) -> skip.
    n2 = _refresh_distilled_files(conn, config, [sf], quiet=True)
    assert n2 == 0
    conn.close()
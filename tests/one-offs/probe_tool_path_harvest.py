"""Probe: what folders would a tool_use.input harvester actually find? (#56)

`folder_usage` is fed only by each event's `cwd`, which current Claude Code
stamps once (the launch dir) and never changes -- so cross-repo sessions are
invisible to `csb scan`. Issue #56 proposes harvesting directories from
tool-call arguments instead. This probe measures whether that premise holds
against a REAL transcript before any of it is designed:

  - which tools carry paths, and how many
  - how many distinct folders we'd gain over the cwd-only baseline
  - how much of the signal needs Bash-command extraction (the fiddly part)
  - what the noise looks like (paths that aren't plausible directories)

Usage:
    python tests/one-offs/probe_tool_path_harvest.py <transcript.jsonl>
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict

# Tools whose input carries a FILE path -> the folder is its dirname.
FILE_PATH_TOOLS = {"Read", "Edit", "Write", "NotebookEdit"}
# Tools whose input carries a DIRECTORY path directly.
DIR_PATH_TOOLS = {"Glob", "Grep"}
# Tools carrying a shell command string that may mention paths.
COMMAND_TOOLS = {"Bash", "PowerShell"}

# Absolute-path shapes we recognize. Deliberately conservative: a missed
# folder is a smaller failure than polluting the index (issue #56).
WIN_ABS = re.compile(r"[A-Za-z]:[\\/][^\s\"'|<>;&]*")
POSIX_ABS = re.compile(r"/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]*")

# --- STRICT mode (measured against the naive rules above) -------------
# The naive POSIX regex matches INSIDE relative paths: `private/claude/x`
# yields a bogus `/claude/x`. Anchor to a real boundary instead.
STRICT_WIN = re.compile(r"(?:(?<=^)|(?<=[\s\"'=(,;|&]))([A-Za-z]:[\\/][^\s\"'|<>;&,)]*)")
STRICT_POSIX = re.compile(
    r"(?:(?<=^)|(?<=[\s\"'=(,;|&]))"
    r"(/(?:mnt/[a-zA-Z]|[a-zA-Z])/[^\s\"'|<>;&,)]*"      # /c/... or /mnt/c/...
    r"|/(?:home|usr|opt|var|tmp|etc|srv|media|root)(?:/[^\s\"'|<>;&,)]*)?)"
)
# Character devices and shell sinks are not directories.
DEVICE_PATHS = {
    "\\DEV\\NULL", "\\DEV\\STDOUT", "\\DEV\\STDERR", "\\DEV\\ZERO", "\\DEV\\TTY",
    "NUL", "CON", "PRN", "AUX",
}


def strict_paths(cmd: str) -> list[str]:
    """Path candidates from a command under the tightened rules."""
    return [m for m in STRICT_WIN.findall(cmd)] + [m for m in STRICT_POSIX.findall(cmd)]


def is_plausible_folder(key: str) -> bool:
    """Reject devices, bare drive roots, and rootless fragments."""
    if not key or key.upper() in DEVICE_PATHS:
        return False
    if re.fullmatch(r"[A-Za-z]:", key):          # bare `C:` -- a drive, not a folder
        return False
    if key.startswith("\\") and not key.startswith("\\\\"):
        return False   # rootless fragment (`\claude\commits`) -- not an absolute path
    return True


def norm(path: str) -> str:
    """Collapse the four spellings of one folder into a single key.

    /c/code/x (Git Bash), C:/code/x, C:\\code\\x, /mnt/c/code/x (WSL) are
    the same directory. index.find_sessions_by_folder_usage matches on
    backslash form, so normalize to that.
    """
    p = path.strip().strip("\"'")
    m = re.match(r"^/(?:mnt/)?([a-zA-Z])/(.*)$", p)
    if m:  # /c/code/x or /mnt/c/code/x -> C:\code\x
        p = f"{m.group(1).upper()}:/{m.group(2)}"
    p = p.replace("/", "\\").rstrip("\\")
    if re.match(r"^[a-zA-Z]:", p):  # normalize drive letter case
        p = p[0].upper() + p[1:]
    return p


def harvest(inp: dict, tool: str, strict: bool = False) -> list[str]:
    """Candidate FOLDERS from one tool_use input block."""
    out = []
    if tool in FILE_PATH_TOOLS:
        fp = inp.get("file_path") or inp.get("notebook_path")
        if isinstance(fp, str) and fp.strip():
            out.append(os.path.dirname(fp) or fp)
    elif tool in DIR_PATH_TOOLS:
        p = inp.get("path")
        if isinstance(p, str) and p.strip():
            out.append(p)
    elif tool in COMMAND_TOOLS:
        cmd = inp.get("command")
        if isinstance(cmd, str):
            matches = strict_paths(cmd) if strict else (
                WIN_ABS.findall(cmd) + POSIX_ABS.findall(cmd))
            for m in matches:
                cand = m.rstrip("\\/")
                if not cand:
                    continue
                # A path ending in a file extension is a file -> take dirname.
                out.append(os.path.dirname(cand) if re.search(r"\.[A-Za-z0-9]{1,6}$", cand) else cand)
    return [o for o in out if o and o.strip()]


def main(path: str) -> int:
    cwds: Counter = Counter()
    by_tool: Counter = Counter()               # tool -> path hits
    folders: Counter = Counter()               # normalized folder -> hits
    folder_tools: dict = defaultdict(set)      # folder -> which tools saw it
    bash_only: Counter = Counter()             # folders ONLY seen via commands
    non_command_folders = set()
    tool_calls = 0

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("cwd"):
                cwds[norm(ev["cwd"])] += 1
            if ev.get("type") != "assistant":
                continue
            content = (ev.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                continue
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                    continue
                tool_calls += 1
                tool = block.get("name") or "?"
                inp = block.get("input")
                if not isinstance(inp, dict):
                    continue
                for raw in harvest(inp, tool):
                    key = norm(raw)
                    if not key:
                        continue
                    folders[key] += 1
                    by_tool[tool] += 1
                    folder_tools[key].add(tool)
                    if tool not in COMMAND_TOOLS:
                        non_command_folders.add(key)

    for k in folders:
        if k not in non_command_folders:
            bash_only[k] = folders[k]

    print(f"transcript: {path}")
    print(f"tool calls: {tool_calls}")
    print(f"\n--- cwd baseline (what csb indexes TODAY) ---")
    for p, c in cwds.most_common():
        print(f"  {c:6}x  {p}")
    print(f"  distinct cwd folders: {len(cwds)}")

    print(f"\n--- path hits by tool ---")
    for t, c in by_tool.most_common():
        print(f"  {c:6}  {t}")

    print(f"\n--- harvested folders (top 20 of {len(folders)}) ---")
    for p, c in folders.most_common(20):
        tools = ",".join(sorted(folder_tools[p]))
        new = "" if p in cwds else "  [NEW]"
        print(f"  {c:6}x  {p}{new}   <- {tools}")

    gained = [p for p in folders if p not in cwds]
    print(f"\n--- summary ---")
    print(f"  distinct folders harvested : {len(folders)}")
    print(f"  NEW folders (not any cwd)  : {len(gained)}")
    print(f"  folders seen ONLY via Bash/PowerShell commands: {len(bash_only)}")
    if bash_only:
        for p, c in bash_only.most_common(5):
            print(f"      {c:5}x  {p}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))

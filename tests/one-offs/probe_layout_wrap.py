"""
Probe: two-line layout wraps meta_text when Rich console.width < len(meta.plain).

Reproduces the three-line split observed during v0.3.11 checklist walk
(Section 6, HV.5) when csb list --deleted only is run in a piped/subprocess
context where Rich defaults to width=79 or 80.

Expected behavior: the id+meta line should be on one line (meta_text), the
restore: on a second line.
Observed behavior: Rich wraps the 87-char meta_text at col 79, producing:
  Line 1: "       id: <uuid> | 8788 messages | v2.1.61 | "
  Line 2: "val: 26-6-2"          <-- no indent, continuation of rich wrap
  Line 3: "       restore: claude --resume <uuid>"

This is NOT a terminal-soft-wrap; it's Rich emitting bare newlines into the
output stream when console.width < len(meta.plain).

Root cause: timeline.py:_format_rich_session() calls console.print(meta) at
line 468 (two-line path) without checking if meta.plain itself exceeds
console.width. Rich's console.print wraps at console.width by default.

Bug filed as part of v0.3.11 checklist findings. Fix: pass no_wrap=True
(with overflow='fold' or similar) or check meta length before printing.
"""

import io
import sys
from pathlib import Path

# Make sure we can import from the project
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from rich.console import Console
from rich.text import Text


def demonstrate_bug():
    # Simulate the conditions that trigger the bug:
    # - full_id = a real session UUID
    # - msg_count = 8788
    # - version = "2.1.61"
    # - validated = "26-6-2"
    # - console.width = 79 (Rich's default in piped context)

    full_id = "4d7565f3-cef5-41b3-9acf-6a3bde0d418d"
    msg_count = 8788
    version = "2.1.61"
    validated = "26-6-2"

    # Build the meta Text exactly as timeline.py does
    meta = Text("       ")
    meta.append(f"id: {full_id}", style="dim")
    meta.append(f" | {msg_count} messages", style="")
    meta.append(f" | v{version}", style="dim")
    meta.append(f" | val: {validated}", style="dim")

    restore_plain = f" | restore: claude --resume {full_id}"
    meta_len = len(meta.plain)
    combined_len = meta_len + len(restore_plain)

    print(f"meta.plain length:    {meta_len}")
    print(f"combined length:      {combined_len}")

    # Simulate two widths
    for width in [79, 120, 160]:
        buf = io.StringIO()
        console = Console(file=buf, width=width, highlight=False)
        print(f"\n=== console.width={width} ===")
        if combined_len <= width:
            print(f"  -> ONE-LINE mode (combined {combined_len} <= {width})")
        else:
            print(f"  -> TWO-LINE mode (combined {combined_len} > {width})")
            print(f"     meta_len={meta_len} vs width={width}: "
                  f"{'WILL WRAP' if meta_len > width else 'fits'}")
        console.print(meta)
        output = buf.getvalue()
        lines = output.split("\n")
        for i, line in enumerate(lines):
            if line:
                print(f"  raw line {i} (len={len(line)}): {repr(line)}")


def check_real_output():
    """Run csb list --deleted only -n 1 and count output lines per session."""
    import subprocess
    scratch = str(Path(__file__).parent.parent.parent / "tests" / "one-offs")
    # This is just a measurement; uses the real test.db if available
    import os
    db = os.path.join(os.environ.get("TEMP", "/tmp"), "csb-v0311-checklist", "test.db")
    if not Path(db).exists():
        print(f"Scratch DB not found at {db}; skipping real output check")
        return

    result = subprocess.run(
        ["csb", "--db", db, "list", "--deleted", "only", "-n", "3"],
        capture_output=True, text=False
    )
    lines = result.stdout.split(b"\n")
    print(f"\n=== Real output line count per session ===")
    print(f"Total output lines: {len(lines)}")
    for i, line in enumerate(lines):
        if b"restore:" in line or b"val:" in line or b"id:" in line:
            print(f"  line {i:3} (len={len(line):3}): {repr(line[:80])}")


if __name__ == "__main__":
    demonstrate_bug()
    check_real_output()

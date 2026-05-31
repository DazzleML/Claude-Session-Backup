"""One-off smoke test for `csb config settings:cleanupPeriodDays` passthrough.

Drives the real CLI (cli.main) against a throwaway temp ~/.claude so it never
touches the user's real settings.json. Prints a transcript of each invocation
with exit code, stdout, stderr, and the resulting on-disk settings.json.

Run: python tests/one-offs/smoke_config_ttl.py
"""

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import claude_session_backup.cli as cli


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def show(label, argv, claude_dir):
    rc, out, err = run(argv + ["--claude-dir", claude_dir])
    print(f"\n### {label}")
    print(f"$ csb {' '.join(argv)}")
    print(f"  rc={rc}")
    if out.strip():
        print("  STDOUT:")
        for line in out.rstrip("\n").splitlines():
            print(f"    {line}")
    if err.strip():
        print("  STDERR:")
        for line in err.rstrip("\n").splitlines():
            print(f"    {line}")
    return rc


def settings_dict(claude_dir):
    p = Path(claude_dir) / "settings.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def main():
    tmp = tempfile.mkdtemp(prefix="csb-smoke-")
    claude = Path(tmp) / ".claude"
    claude.mkdir(parents=True)
    settings = claude / "settings.json"
    # Seed a realistic settings.json: TTL + a permissions block to verify
    # the write preserves unrelated keys.
    settings.write_text(json.dumps({
        "cleanupPeriodDays": 90,
        "permissions": {"allow": ["Read", "Glob"]},
        "model": "opus",
    }, indent=2) + "\n", encoding="utf-8")

    cd = str(claude)
    print(f"temp claude dir: {cd}")

    show("GET (present, 90)", ["config", "settings:cleanupPeriodDays"], cd)
    show("SET 365", ["config", "settings:cleanupPeriodDays", "365"], cd)
    print("  -> settings.json now:", settings_dict(cd))
    assert settings_dict(cd)["cleanupPeriodDays"] == 365, "set 365 failed"
    assert settings_dict(cd)["permissions"] == {"allow": ["Read", "Glob"]}, "permissions clobbered!"
    assert settings_dict(cd)["model"] == "opus", "model clobbered!"

    show("SET 10 (reduction warning)", ["config", "settings:cleanupPeriodDays", "10"], cd)

    rc = show("SET 0 WITHOUT --force (should refuse, rc=2)", ["config", "settings:cleanupPeriodDays", "0"], cd)
    assert rc == 2, "0 without --force should refuse"
    assert settings_dict(cd)["cleanupPeriodDays"] == 10, "refused write should not change file"

    rc = show("SET 0 WITH --force (should write 0, rc=0)", ["config", "settings:cleanupPeriodDays", "0", "--force"], cd)
    assert rc == 0
    assert settings_dict(cd)["cleanupPeriodDays"] == 0

    rc = show("SET negative (reject, rc=2)", ["config", "settings:cleanupPeriodDays", "-5"], cd)
    assert rc == 2

    rc = show("SET non-int (reject, rc=2)", ["config", "settings:cleanupPeriodDays", "abc"], cd)
    assert rc == 2

    rc = show("bare cleanupPeriodDays (hint, rc=1)", ["config", "cleanupPeriodDays"], cd)
    assert rc == 1

    rc = show("unknown settings key (rc=1)", ["config", "settings:bogusKey"], cd)
    assert rc == 1

    rc = show("GET absent key -> default", ["config", "settings:cleanupPeriodDays"], cd)
    # set back to large for never-purge demo
    show("SET 36500 (never-purge idiom)", ["config", "settings:cleanupPeriodDays", "36500"], cd)

    # csb-own config still works (write to its own file, not settings.json)
    rc = show("csb-own: set display_top_folders 5", ["config", "display_top_folders", "5"], cd)
    assert rc == 0
    own = json.loads((claude / "session-backup-config.json").read_text(encoding="utf-8"))
    assert own["display_top_folders"] == 5
    assert "display_top_folders" not in settings_dict(cd), "csb key leaked into settings.json!"

    show("bare config dump (JSON stdout + settings hint on stderr)", ["config"], cd)

    print("\nALL SMOKE ASSERTIONS PASSED")


if __name__ == "__main__":
    sys.exit(main())

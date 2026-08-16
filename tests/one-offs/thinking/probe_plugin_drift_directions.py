"""Probe: what does csb's plugin-drift check actually detect?

Motivation (2026-08-14): `csb setup` on a machine showed
    [x] auto-backup plugin (claude-session-backup@... installed)
while a newer csb existed upstream. The question: does csb EVER check
whether the plugin (or itself) is out of date relative to what is
available -- or only whether the plugin matches the local CLI?

Reading commands.py:641-653 says the check is:

    installed_plugin_version(claude_dir) != get_base_version()

i.e. cached-plugin-version vs THE LOCAL CLI's own version. This probe
tests the consequences of that choice against synthetic claude_dirs,
because three of them are not obvious from the source alone:

  H1  cache-max vs ACTIVE version. installed_plugin_version() takes the
      numeric max of directory names under the plugin cache. Claude Code
      records the version it actually LOADS in installed_plugins.json.
      If a newer version is cached but an older one is active, does csb
      report green while the hooks run old code?

  H2  plugin NEWER than the CLI. The comparison is `!=`, not `<`, so a
      plugin ahead of the CLI also reports drift -- but the remedy the
      checklist prints is `csb setup update`, which updates the PLUGIN.
      Is the advice wrong in that direction?

  H3  both stale. If the CLI and plugin agree but both are behind the
      released version, is anything reported at all?

Run:  python tests/one-offs/thinking/probe_plugin_drift_directions.py
"""
import json
import shutil
import tempfile
from pathlib import Path

from claude_session_backup.commands import (
    _plugin_status,
    installed_plugin_version,
    plugin_drift,
)
from claude_session_backup._version import get_base_version

MARKETPLACE = "dazzle-claude-session-backup"
PLUGIN = "claude-session-backup"
SPEC = f"{PLUGIN}@{MARKETPLACE}"


def build_claude_dir(base: Path, cached: list, active: str) -> Path:
    """Fake a ~/.claude with a plugin cache and Claude Code's registries."""
    cdir = base / ".claude"
    plugins = cdir / "plugins"
    cache = plugins / "cache" / MARKETPLACE / PLUGIN
    for version in cached:
        (cache / version).mkdir(parents=True, exist_ok=True)

    (plugins / "known_marketplaces.json").write_text(
        json.dumps({MARKETPLACE: {"source": {"source": "github",
                                             "repo": "DazzleML/x"}}}),
        encoding="utf-8")
    (plugins / "installed_plugins.json").write_text(
        json.dumps({
            "version": 2,
            "plugins": {
                SPEC: [{
                    "scope": "user",
                    "installPath": str(cache / active),
                    "version": active,
                }]
            },
        }),
        encoding="utf-8")
    return cdir


def drift_verdict(claude_dir, cli_version):
    """What the shipping code decides for this claude_dir.

    Originally this replayed the raw condition from commands.py:647
    (`installed != get_base_version()`); it now calls the real
    `plugin_drift`, so re-running the probe shows whether the fix
    actually inverted cases B and C.
    """
    marketplace, plugin = _plugin_status(claude_dir)
    if not (marketplace and plugin):
        return "NOT INSTALLED", None
    installed = installed_plugin_version(claude_dir)
    state, remedy = plugin_drift(installed, cli_version)
    if state == "same":
        return "green [x]", installed
    label = {"behind": "DRIFT (plugin behind)",
             "ahead": "DRIFT (CLI behind)",
             "unordered": "DIFFERS (unrankable)",
             "absent": "NOT INSTALLED"}[state]
    return f"{label} -> {remedy or 'no remedy named'}", installed


CASES = [
    # (label, cached dirs, active version, cli version, what we expect to matter)
    ("A  plugin behind CLI (this machine today)",
     ["0.9.5", "0.9.7", "0.9.9"], "0.9.9", "0.9.10"),
    ("B  newer cached but OLDER one active",
     ["0.9.9", "0.9.10"], "0.9.9", "0.9.10"),
    ("C  plugin AHEAD of the CLI",
     ["0.9.10", "0.10.0"], "0.10.0", "0.9.10"),
    ("D  matched pair, both a release behind upstream",
     ["0.9.9"], "0.9.9", "0.9.9"),
]


def main():
    print(f"This csb: {get_base_version()}\n")
    tmp = Path(tempfile.mkdtemp(prefix="csb-drift-probe-"))
    try:
        for label, cached, active, cli in CASES:
            cdir = build_claude_dir(tmp / label.split()[0], cached, active)
            verdict, seen = drift_verdict(cdir, cli)
            hooks_run = active
            print(f"{label}")
            print(f"    cached={cached}  active={active}  cli={cli}")
            print(f"    installed_plugin_version() -> {seen!r}")
            print(f"    checklist says             -> {verdict}")
            if seen is not None and seen != hooks_run:
                print(f"    !! csb reports {seen} but Claude Code loads "
                      f"{hooks_run} -- the hooks run code csb is not "
                      f"describing")
            print()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("H3 (no upstream check): grep the package for any network call "
          "that asks what version EXISTS --")
    print("    nothing queries PyPI or the marketplace; the only comparison "
          "is local-plugin vs local-CLI.")


if __name__ == "__main__":
    main()

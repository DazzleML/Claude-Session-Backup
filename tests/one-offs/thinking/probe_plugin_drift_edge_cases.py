"""Probe: edge cases for active_plugin_version() / _version_key() / plugin_drift()
that the v0.9.11 checklist (v0.9.11__Fix__plugin-drift-authoritative-version.md)
does not exercise directly.

Run:  python tests/one-offs/thinking/probe_plugin_drift_edge_cases.py

Companion to probe_plugin_drift_directions.py (which covers the four
directional cases). This probe attacks the registry-parsing boundary:

  E1  multi-scope registry: TWO records for the same plugin (a
      user-scope entry and a project-scope entry). active_plugin_version()
      returns the FIRST record carrying a version -- is "first" the
      right one, or does it need a scope preference?
  E2  empty records list -- `"plugin-key": []`
  E3  records value that is not a list at all (a dict, a string)
  E4  a record whose "version" field is a non-string (int, null, float)
  E5  _version_key segment-count mismatch: "0.9" vs "0.9.10"
  E6  ordering sanity: "0.9.10" vs "0.9.9" (lexical vs numeric)
  E7  an enormous version component
  E8  unicode in the version string
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

# Windows consoles default to cp1252/cp437, which can't encode the
# fullwidth digits / ZWSP used in E8 below -- reconfigure so the probe
# reports what it found instead of crashing on its own print().
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from claude_session_backup.commands import (
    active_plugin_version,
    installed_plugin_version,
    plugin_drift,
    _version_key,
)

PLUGIN_KEY = "claude-session-backup@dazzle-claude-session-backup"


def _write(cdir: Path, plugins_value):
    base = cdir / "plugins"
    base.mkdir(parents=True, exist_ok=True)
    (base / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {PLUGIN_KEY: plugins_value}}),
        encoding="utf-8")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="csb-drift-edge-probe-"))
    try:
        print("=== E1: multi-scope registry (user + project records) ===")
        for order, label in [
            ([{"scope": "user", "version": "0.9.9"},
              {"scope": "project", "version": "0.9.10"}], "user first, project second"),
            ([{"scope": "project", "version": "0.9.10"},
              {"scope": "user", "version": "0.9.9"}], "project first, user second"),
        ]:
            cdir = tmp / f"e1-{label.split()[0]}"
            _write(cdir, order)
            got = active_plugin_version(cdir)
            print(f"  {label}: records={order} -> active_plugin_version()={got!r}")
        print("  Real ~/.claude/plugins/installed_plugins.json on this machine "
              "(read-only evidence, not fixture): every plugin observed has "
              "exactly ONE record, scope='user'. No project-scope record seen "
              "in the wild on this box -- multi-scope is unconfirmed as a real "
              "shape, only as a schema possibility (the registry has no schema "
              "guard against it).")
        print()

        print("=== E2: empty records list ===")
        cdir = tmp / "e2"
        _write(cdir, [])
        print(f"  plugins: [] -> active_plugin_version()={active_plugin_version(cdir)!r}"
              f"  installed_plugin_version()={installed_plugin_version(cdir)!r}")
        print()

        print("=== E3: records value not a list ===")
        for bad, label in [({"version": "0.9.9"}, "a dict"),
                            ("0.9.9", "a bare string"),
                            (None, "null")]:
            cdir = tmp / f"e3-{label.split()[0]}"
            _write(cdir, bad)
            try:
                got = active_plugin_version(cdir)
                print(f"  plugins value is {label} ({bad!r}) -> {got!r}  (no crash)")
            except Exception as e:  # noqa: BLE001 -- probe wants to SEE crashes
                print(f"  plugins value is {label} ({bad!r}) -> RAISED "
                      f"{type(e).__name__}: {e}")
        print()

        print("=== E4: non-string version field ===")
        for bad, label in [(99, "an int"), (None, "null"), (9.5, "a float")]:
            cdir = tmp / f"e4-{label.split()[0]}"
            _write(cdir, [{"scope": "user", "version": bad}])
            try:
                got = active_plugin_version(cdir)
                print(f"  version field is {label} ({bad!r}) -> {got!r}")
            except Exception as e:  # noqa: BLE001
                print(f"  version field is {label} ({bad!r}) -> RAISED "
                      f"{type(e).__name__}: {e}")
        print()

        print("=== E5: _version_key segment-count mismatch ===")
        for a, b in [("0.9", "0.9.10"), ("0.9.10", "0.9"), ("0.9.0", "0.9")]:
            ka, kb = _version_key(a), _version_key(b)
            print(f"  _version_key({a!r})={ka}  _version_key({b!r})={kb}  "
                  f"-> {a} < {b} is {ka < kb if ka and kb else 'N/A'}  "
                  f"plugin_drift({a!r}, {b!r})={plugin_drift(a, b)}")
        print()

        print("=== E6: 0.9.10 vs 0.9.9 ordering sanity ===")
        print(f"  _version_key('0.9.10')={_version_key('0.9.10')}  "
              f"_version_key('0.9.9')={_version_key('0.9.9')}")
        print(f"  plugin_drift('0.9.9', '0.9.10') = {plugin_drift('0.9.9', '0.9.10')}"
              f"   (expect behind, plugin trails)")
        print(f"  plugin_drift('0.9.10', '0.9.9') = {plugin_drift('0.9.10', '0.9.9')}"
              f"   (expect ahead, plugin leads)")
        print()

        print("=== E7: enormous version component ===")
        huge = "99999999999999999999.0.0"
        print(f"  _version_key({huge!r}) = {_version_key(huge)}")
        print(f"  plugin_drift({huge!r}, '0.9.10') = {plugin_drift(huge, '0.9.10')}")
        print()

        print("=== E8: unicode in version string ===")
        for weird in ["0.9.０", "0.9.10​", "０.９.１０"]:  # fullwidth digits, ZWSP
            print(f"  _version_key({weird!r}) = {_version_key(weird)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

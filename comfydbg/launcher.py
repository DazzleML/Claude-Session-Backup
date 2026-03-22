"""
ComfyUI startup launcher -- manages pip installs with package-level control.

Thin CMD wrapper (comfydbg) activates venv and delegates here.
Use 'comfydbg -h' or 'comfydbg <command> -h' for help on any command.
"""

import argparse
import subprocess
import sys
import re
import json
import platform
import urllib.request
import urllib.error
from pathlib import Path

DEFAULT_SKIP = ["comfyui-frontend-package"]
GIT_CLONE_DIR = Path.home() / "comfydbg-builds"


def _requirements_file():
    """Resolve requirements.txt from CWD at runtime, not import time."""
    return Path.cwd() / "requirements.txt"


def is_comfyui_directory(path):
    """Check if a path is a ComfyUI installation directory."""
    if not path.exists() or not path.is_dir():
        return False
    if (path / "main.py").exists() and (path / "requirements.txt").exists():
        return True
    if (path / "ComfyUI" / "main.py").exists():
        return True
    if (path / "custom_nodes").is_dir() and (path / "models").is_dir():
        return True
    return False


def discover_comfyui_installations():
    """Find ComfyUI installations on the system.

    Quick scan of common locations. Returns list of (description, path) tuples.
    """
    found = []

    # Current directory first
    cwd = Path.cwd()
    if is_comfyui_directory(cwd):
        found.append(("Current directory", cwd))

    # ComfyUI Desktop config (Windows)
    if platform.system() == "Windows":
        config_path = Path.home() / "AppData" / "Roaming" / "ComfyUI" / "config.json"
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                base_path_str = config.get("basePath", "")
                if base_path_str:
                    base_path = Path(base_path_str)
                    if base_path.exists() and is_comfyui_directory(base_path):
                        if base_path.resolve() not in [p.resolve() for _, p in found]:
                            found.append(("ComfyUI Desktop", base_path))
            except (json.JSONDecodeError, KeyError, OSError):
                pass

    # Common locations
    common_paths = [
        (Path.home() / "Documents" / "ComfyUI", "Documents"),
        (Path.home() / "ComfyUI", "Home folder"),
        (Path("C:/ComfyUI"), "C: drive"),
        (Path("C:/ComfyUI_windows_portable"), "Portable"),
        (Path("D:/ComfyUI"), "D: drive"),
        (Path("D:/ComfyUI_windows_portable"), "D: Portable"),
    ]

    for path, description in common_paths:
        if path.exists() and is_comfyui_directory(path):
            if path.resolve() not in [p.resolve() for _, p in found]:
                found.append((description, path))

    return found


def _detect_python():
    """Detect the correct Python executable for the ComfyUI project.

    Priority order (matches comfyui-triton-sageattention-installer):
      1. python_embeded/ (ComfyUI portable distribution)
      2. .venv/ (modern tooling: uv, poetry)
      3. venv/ (traditional virtualenv)
      4. sys.executable (fallback: whatever Python is running comfydbg)

    Returns the Path to the python executable.
    """
    cwd = Path.cwd()

    # Portable distribution (Windows)
    portable = cwd / "python_embeded" / "python.exe"
    if portable.exists():
        return str(portable)

    # .venv (uv, poetry, modern tooling)
    if platform.system() == "Windows":
        dot_venv = cwd / ".venv" / "Scripts" / "python.exe"
    else:
        dot_venv = cwd / ".venv" / "bin" / "python"
    if dot_venv.exists():
        return str(dot_venv)

    # venv (traditional)
    if platform.system() == "Windows":
        venv = cwd / "venv" / "Scripts" / "python.exe"
    else:
        venv = cwd / "venv" / "bin" / "python"
    if venv.exists():
        return str(venv)

    # Fallback to current interpreter
    return sys.executable


def _get_python():
    """Get the Python executable to use for pip/subprocess calls.

    Caches the result for the session. Call from functions, not at import time.
    """
    if not hasattr(_get_python, "_cached"):
        _get_python._cached = _detect_python()
    return _get_python._cached


# Known PyPI package name -> GitHub owner/repo mappings.
# Used by 'comfydbg install' so you don't have to look up the repo each time.
# "repo" is required. "build_recipe" is optional -- when present, it names a
# function in BUILD_RECIPES that can build a wheel from a clone + release assets
# when the generic install chain (git+URL, clone+pip) fails.
PYPI_TO_GITHUB = {
    "comfyui-frontend-package": {
        "repo": "Comfy-Org/ComfyUI_frontend",
        "build_recipe": "comfyui_frontend",
    },
    "comfyui-workflow-templates": {
        "repo": "Comfy-Org/ComfyUI_workflow_templates",
    },
    "comfyui-embedded-docs": {
        "repo": "Comfy-Org/ComfyUI_embedded_docs",
    },
}


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def parse_requirements(path):
    """Parse requirements.txt into dict of {normalized_name: original_line}."""
    entries = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^([A-Za-z0-9_][A-Za-z0-9._-]*)", line)
            if match:
                name = normalize_name(match.group(1))
                entries[name] = line
    return entries


def normalize_name(name):
    """Normalize package name for comparison (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pkg_arg(arg):
    """Parse a user-supplied package arg like 'pkg' or 'pkg==1.2.3'.
    Returns (normalized_name, version_override_or_None, original_arg)."""
    match = re.match(r"^([A-Za-z0-9_][A-Za-z0-9._-]*)(==.+)?$", arg)
    if not match:
        return None, None, arg
    name = normalize_name(match.group(1))
    version = match.group(2)  # e.g. "==1.39.19" or None
    return name, version, arg


def get_installed_version(pkg_name):
    """Get currently installed version of a package, or None."""
    try:
        result = subprocess.run(
            [_get_python(), "-m", "pip", "show", pkg_name],
            capture_output=True, text=True, timeout=15
        )
        for line in result.stdout.splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    except (subprocess.TimeoutExpired, Exception):
        pass
    return None


def get_available_version(pkg_name, req_line):
    """Get what version pip would install from the requirement line.
    For pinned (==) versions, just parse it. For others, ask pip."""
    pin_match = re.search(r"==([^\s,;]+)", req_line)
    if pin_match:
        return pin_match.group(1)
    try:
        result = subprocess.run(
            [_get_python(), "-m", "pip", "install", "--dry-run", req_line],
            capture_output=True, text=True, timeout=30
        )
        norm = normalize_name(pkg_name)
        for line in result.stdout.splitlines():
            if "would install" in line.lower():
                for token in line.split():
                    token_match = re.match(r"^(.+)-(\d[^\s]*)$", token)
                    if token_match and normalize_name(token_match.group(1)) == norm:
                        return token_match.group(2)
    except (subprocess.TimeoutExpired, Exception):
        pass
    return None


def show_skip_info(pkg_names, requirements):
    """Show version comparison for skipped packages."""
    print()
    print("=" * 60)
    print("  SKIPPED PACKAGES -- version check")
    print("=" * 60)
    for name in pkg_names:
        req_line = requirements.get(name, name)
        installed = get_installed_version(name) or "(not installed)"
        available = get_available_version(name, req_line) or "(unknown)"

        if installed == available:
            status = "UP TO DATE"
        elif installed == "(not installed)":
            status = "MISSING"
        else:
            status = "UPDATE AVAILABLE"

        print(f"  {name}")
        print(f"    Installed:  {installed}")
        print(f"    Available:  {available}  (from: {req_line})")
        print(f"    Status:     {status}")
        print()
    print("=" * 60)
    print()


def validate_packages(pkg_names, requirements):
    """Validate that all package names exist in requirements.txt."""
    bad = [name for name in pkg_names if name not in requirements]
    if bad:
        print(f"ERROR: Package(s) not found in requirements.txt: {', '.join(bad)}")
        print(f"Available packages: {', '.join(sorted(requirements.keys()))}")
        return False
    return True


def write_filtered_requirements(requirements, exclude_names, tmp_path):
    """Write a temporary requirements file excluding specified packages."""
    with open(tmp_path, "w") as f:
        for name, line in requirements.items():
            if name not in exclude_names:
                f.write(line + "\n")


def pip_install_requirements(req_file):
    """Run pip install -r <file>."""
    print(f"[PIP] Installing from {req_file}")
    return subprocess.call([_get_python(), "-m", "pip", "install", "-r", str(req_file)])


def pip_uninstall(pkg_name):
    """Run pip uninstall -y <pkg>."""
    print(f"[PIP] Uninstalling {pkg_name}")
    return subprocess.call([_get_python(), "-m", "pip", "uninstall", "-y", pkg_name])


def pip_install(pkg_spec):
    """Run pip install <pkg_spec> (e.g. 'pkg==1.2.3' or 'pkg')."""
    print(f"[PIP] Installing {pkg_spec}")
    return subprocess.call([_get_python(), "-m", "pip", "install", pkg_spec])


def launch_main(args):
    """Launch main.py with the given arguments."""
    cmd = [_get_python(), "main.py"] + args
    print(f"[LAUNCH] {' '.join(cmd)}")
    return subprocess.call(cmd)


def verify_venv():
    """Check that we found a project-local Python environment."""
    detected = _get_python()
    if detected == sys.executable:
        # No venv/portable found, using system Python
        print(f"[WARN] No project venv detected, using system Python.")
        print(f"  Using:    {detected}")
        print(f"  Searched: venv/, .venv/, python_embeded/")
        return False
    print(f"[ENV] Using: {detected}")
    return True


def get_pypi_versions(pkg_name):
    """Fetch all release versions from PyPI, sorted newest-first by upload date.
    Returns list of (version_string, upload_time) tuples, or None on failure."""
    url = f"https://pypi.org/pypi/{pkg_name}/json"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        print(f"[ERROR] Failed to query PyPI for {pkg_name}: {e}")
        return None

    versions_with_dates = []
    for version_str, files in data.get("releases", {}).items():
        if not files:
            continue
        upload_time = min(f.get("upload_time", "") for f in files)
        versions_with_dates.append((version_str, upload_time))

    versions_with_dates.sort(key=lambda x: x[1], reverse=True)
    return versions_with_dates


def get_github_releases(repo):
    """Fetch GitHub releases for owner/repo via gh CLI.
    Returns list of (tag, published_date) tuples newest-first, or None on failure."""
    try:
        result = subprocess.run(
            ["gh", "release", "list", "--repo", repo, "--limit", "50",
             "--json", "tagName,publishedAt"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            print(f"[ERROR] gh release list failed for {repo}: {result.stderr.strip()}")
            return None
        releases = json.loads(result.stdout)
        # Already sorted newest-first by gh
        return [(r["tagName"], r["publishedAt"]) for r in releases]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        print(f"[ERROR] Failed to query GitHub releases for {repo}: {e}")
        return None


def resolve_github_repo(pkg_name):
    """Resolve a package name to a GitHub owner/repo.
    Returns owner/repo string or None."""
    entry = PYPI_TO_GITHUB.get(pkg_name)
    if entry:
        return entry["repo"]
    return None


def resolve_build_recipe(pkg_name):
    """Get the build recipe name for a package, or None."""
    entry = PYPI_TO_GITHUB.get(pkg_name)
    if entry:
        return entry.get("build_recipe")
    return None


def show_version_list(versions, current_version, label="version"):
    """Display a numbered version list with CURRENT marker.
    versions: list of (version_or_tag, date_str) tuples, newest-first.
    Returns the index of current_version, or None if not found."""
    current_idx = None
    for i, (v, date) in enumerate(versions):
        tag_clean = v.lstrip("v")
        if tag_clean == current_version or v == current_version:
            current_idx = i
            break

    print(f"\n  {label} history (newest first):")
    # Show a window around current and target area
    show_count = min(15, len(versions))
    for i in range(show_count):
        v, date = versions[i]
        marker = ""
        if i == current_idx:
            marker = "  <-- CURRENT"
        date_short = date[:10] if date else ""
        print(f"    {i:3d}. {v:30s} {date_short}{marker}")
    if show_count < len(versions):
        print(f"    ... ({len(versions) - show_count} older {label}s)")

    return current_idx


def do_rollback_install(versions, current_version, rollback, pkg_name, install_fn, label="version"):
    """Common rollback logic for any version source.
    versions: list of (version_or_tag, date_str) tuples.
    install_fn: callable(target_version_string) -> exit code.
    Returns exit code."""
    # Find current position
    current_idx = None
    for i, (v, _) in enumerate(versions):
        tag_clean = v.lstrip("v")
        if tag_clean == current_version or v == current_version:
            current_idx = i
            break

    if current_idx is None:
        print(f"  [WARN] Current version {current_version} not found in {label} list.")
        show_version_list(versions, current_version, label)
        print(f"  Cannot determine rollback position.")
        return 1

    target_idx = current_idx + rollback
    if target_idx >= len(versions):
        avail = len(versions) - current_idx - 1
        print(f"ERROR: Only {avail} older {label}(s) available, requested -{rollback}.")
        show_version_list(versions, current_version, label)
        return 1

    target_version, target_date = versions[target_idx]

    # Show context window
    print(f"\n  {label} history (newest first):")
    start = max(0, current_idx - 2)
    end = min(len(versions), target_idx + 3)
    for i in range(start, end):
        v, date = versions[i]
        marker = ""
        if i == current_idx:
            marker = "  <-- CURRENT"
        elif i == target_idx:
            marker = "  <-- TARGET"
        date_short = date[:10] if date else ""
        print(f"    {v:30s} {date_short}{marker}")
    if end < len(versions):
        print(f"    ... ({len(versions) - end} older {label}s)")

    print(f"\n  Will install: {target_version}")
    pip_uninstall(pkg_name)
    return install_fn(target_version)


def do_head_install(versions, head_offset, pkg_name, install_fn, label="version"):
    """Install the Nth version from the head (newest) of the version list.
    head_offset=0 is newest, 1 is second newest, etc.
    versions: list of (version_or_tag, date_str) tuples.
    install_fn: callable(target_version_string) -> exit code."""
    # head_offset is 1-based from the user's perspective (HN 1 = newest)
    # but 0-indexed in the list, so HN 1 = index 0, HN 2 = index 1, etc.
    target_idx = head_offset - 1

    if target_idx < 0:
        target_idx = 0
    if target_idx >= len(versions):
        print(f"ERROR: Only {len(versions)} {label}(s) available, requested -HN {head_offset}.")
        show_version_list(versions, None, label)
        return 1

    target_version, target_date = versions[target_idx]

    # Show context
    print(f"\n  {label} history (newest first):")
    start = max(0, target_idx - 2)
    end = min(len(versions), target_idx + 5)
    for i in range(start, end):
        v, date = versions[i]
        marker = ""
        if i == target_idx:
            marker = "  <-- TARGET"
        date_short = date[:10] if date else ""
        print(f"    {v:30s} {date_short}{marker}")
    if end < len(versions):
        print(f"    ... ({len(versions) - end} older {label}s)")

    print(f"\n  Will install: {target_version}")
    pip_uninstall(pkg_name)
    return install_fn(target_version)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_skip(args):
    """Install requirements.txt, excluding specified packages (default: comfyui-frontend-package).
    Shows installed vs available version for each skipped package."""
    requirements = parse_requirements(_requirements_file())

    if args.packages:
        skip_list = []
        for arg in args.packages:
            name, _, _ = parse_pkg_arg(arg)
            if name is None:
                print(f"ERROR: Invalid package name: {arg}")
                return 1
            skip_list.append(name)
    else:
        skip_list = [normalize_name(p) for p in DEFAULT_SKIP]

    if not validate_packages(skip_list, requirements):
        return 1

    show_skip_info(skip_list, requirements)

    tmp_req = Path.cwd() / ".requirements_filtered.txt"
    try:
        write_filtered_requirements(requirements, set(skip_list), tmp_req)
        print(f"[MODE] skip -- excluding: {', '.join(skip_list)}")
        rc = pip_install_requirements(tmp_req)
        if rc != 0:
            print(f"[WARN] pip install exited with code {rc}")
    finally:
        tmp_req.unlink(missing_ok=True)

    return launch_main(args.main_args)


def cmd_full(args):
    """Install all packages from requirements.txt with no filtering."""
    if args.force_reinstall:
        print("[MODE] full -F -- force-reinstalling all requirements (no cache)")
        rc = subprocess.call([
            _get_python(), "-m", "pip", "install",
            "--force-reinstall", "--no-cache-dir",
            "-r", str(_requirements_file())
        ])
    else:
        print("[MODE] full -- installing all requirements as-is")
        rc = pip_install_requirements(_requirements_file())
    if rc != 0:
        print(f"[WARN] pip install exited with code {rc}")
    return launch_main(args.main_args)


def cmd_force(args):
    """Uninstall then reinstall specified packages (default: comfyui-frontend-package).
    Supports version pins like 'pkg==1.2.3'. Remaining requirements installed after."""
    requirements = parse_requirements(_requirements_file())

    if args.packages:
        force_list = []
        for arg in args.packages:
            name, version, original = parse_pkg_arg(arg)
            if name is None:
                print(f"ERROR: Invalid package name: {arg}")
                return 1
            force_list.append((name, version, original))
    else:
        force_list = [(normalize_name(p), None, p) for p in DEFAULT_SKIP]

    force_names = [name for name, _, _ in force_list]
    if not validate_packages(force_names, requirements):
        return 1

    print(f"[MODE] force -- uninstall/reinstall: {', '.join(force_names)}")

    for name, _, _ in force_list:
        pip_uninstall(name)

    for name, version, original in force_list:
        if version:
            pip_install(f"{name}{version}")
        else:
            req_line = requirements[name]
            pip_install(req_line)

    tmp_req = Path.cwd() / ".requirements_filtered.txt"
    try:
        write_filtered_requirements(requirements, set(force_names), tmp_req)
        print("[PIP] Installing remaining requirements")
        rc = pip_install_requirements(tmp_req)
        if rc != 0:
            print(f"[WARN] pip install exited with code {rc}")
    finally:
        tmp_req.unlink(missing_ok=True)

    return launch_main(args.main_args)


def cmd_install(args):
    """Install a single package with flexible source and version rollback.

    Three install sources:
      PyPI (default) -- pip install from PyPI registry
      GitHub git     -- pip install git+https://github.com/owner/repo@tag  (-G flag)
      GitHub wheel   -- download .whl from GitHub release assets           (-W flag)

    Rollback: -1/-2/-3 shortcuts, or -N <num> for any offset.
    Does NOT launch main.py -- standalone package management."""
    if not verify_venv():
        resp = input("Continue anyway? [y/N] ").strip().lower()
        if resp != "y":
            return 1

    # Determine package name and optional GitHub repo
    pkg_input = args.package
    github_repo = None

    # Auto-detect owner/repo format
    if "/" in pkg_input and not pkg_input.startswith("http"):
        github_repo = pkg_input
        # Try to find PyPI name from reverse mapping
        reverse_map = {v["repo"]: k for k, v in PYPI_TO_GITHUB.items()}
        name = reverse_map.get(github_repo)
        if not name:
            print(f"  GitHub repo: {github_repo}")
            print(f"  No known PyPI mapping. Use --git or --wheel mode.")
            if not (args.git or args.wheel):
                print(f"  Hint: add mapping to PYPI_TO_GITHUB in comfydbg launcher")
                print(f"        or specify: comfydbg install {github_repo} --git")
                return 1
            name = normalize_name(github_repo.split("/")[-1])
    else:
        name, _, _ = parse_pkg_arg(pkg_input)
        if name is None:
            print(f"ERROR: Invalid package name: {pkg_input}")
            return 1

    # Resolve GitHub repo if needed for -G or -W modes
    if (args.git or args.wheel) and not github_repo:
        github_repo = resolve_github_repo(name)
        if not github_repo:
            print(f"ERROR: No GitHub repo known for '{name}'.")
            print(f"  Either specify owner/repo directly:")
            print(f"    comfydbg install Comfy-Org/ComfyUI_frontend --git -HN 2")
            print(f"  Or add a mapping to PYPI_TO_GITHUB in comfydbg launcher")
            return 1

    requirements = parse_requirements(_requirements_file())
    installed = get_installed_version(name)

    source = "pypi"
    if args.git:
        source = "git"
    elif args.wheel:
        source = "wheel"

    print(f"\n[INSTALL] Package: {name}")
    print(f"  Source: {source}")
    if github_repo:
        print(f"  GitHub: {github_repo}")
    print(f"  Currently installed: {installed or '(not installed)'}")

    # Pre-uninstall if requested (clears pip cache issues)
    if args.uninstall and installed:
        print(f"  Pre-uninstall requested (-U)")
        pip_uninstall(name)
        installed = None  # reflect that it's now gone

    # Determine rollback mode
    current_nth = args.current_nth or 0
    head_nth = args.head_nth or 0

    if current_nth and head_nth:
        print("ERROR: Cannot use both -CN and -HN at the same time.")
        return 1

    rollback = current_nth  # default mode (from current)
    from_head = head_nth    # alternative mode (from newest)

    # ---- PyPI source (default) ----
    if source == "pypi":
        if name not in requirements:
            print(f"[WARN] {name} not in requirements.txt, installing from PyPI anyway")

        if rollback == 0 and from_head == 0:
            if name in requirements:
                req_line = requirements[name]
                print(f"  Installing: {req_line}")
                pip_uninstall(name)
                return pip_install(req_line)
            else:
                print(f"  Installing latest from PyPI")
                pip_uninstall(name)
                return pip_install(name)

        print(f"  Querying PyPI for version history...")
        versions = get_pypi_versions(name)
        if not versions:
            print(f"ERROR: No versions found on PyPI for {name}")
            return 1

        if from_head > 0:
            # From head: index directly into the list (0 = newest, 1 = second newest, etc)
            return do_head_install(
                versions, from_head, name,
                lambda v: pip_install(f"{name}=={v}"),
                label="PyPI release"
            )
        else:
            if not installed:
                print("ERROR: Cannot rollback from current -- package is not currently installed.")
                print("  Use -HN to select by position from newest instead.")
                return 1
            print(f"  Rollback: -{rollback} from current via PyPI")
            return do_rollback_install(
                versions, installed, rollback, name,
                lambda v: pip_install(f"{name}=={v}"),
                label="PyPI release"
            )

    # ---- GitHub git source ----
    elif source == "git":
        print(f"  Querying GitHub releases for {github_repo}...")
        releases = get_github_releases(github_repo)
        if not releases:
            print(f"ERROR: No releases found for {github_repo}")
            return 1

        def git_install(tag):
            rc = pip_install(f"git+https://github.com/{github_repo}.git@{tag}")
            if rc != 0:
                print(f"  [WARN] git+ failed, falling back to clone...")
                return _clone_and_install(github_repo, tag, name)
            return rc

        if rollback == 0 and from_head == 0:
            tag = releases[0][0]
            print(f"  Latest release: {tag}")
            pip_uninstall(name)
            return git_install(tag)

        if from_head > 0:
            print(f"  Position: {from_head} from newest via GitHub releases")
            return do_head_install(
                releases, from_head, name,
                git_install, label="GitHub release"
            )
        else:
            if not installed:
                print("ERROR: Cannot rollback from current -- not installed. Use -HN instead.")
                return 1
            print(f"  Rollback: -{rollback} from current via GitHub tags")
            return do_rollback_install(
                releases, installed, rollback, name,
                git_install, label="GitHub release"
            )

    # ---- GitHub wheel source ----
    elif source == "wheel":
        print(f"  Querying GitHub releases for {github_repo}...")
        releases = get_github_releases(github_repo)
        if not releases:
            print(f"ERROR: No releases found for {github_repo}")
            return 1

        if rollback == 0 and from_head == 0:
            tag = releases[0][0]
        elif from_head > 0:
            target_idx = from_head - 1
            if target_idx >= len(releases):
                print(f"ERROR: Only {len(releases)} release(s) available.")
                show_version_list(releases, installed, "GitHub release")
                return 1
            tag = releases[target_idx][0]
        else:
            if not installed:
                print("ERROR: Cannot rollback from current -- not installed. Use -HN instead.")
                return 1
            current_idx = None
            for i, (tag, _) in enumerate(releases):
                if tag.lstrip("v") == installed or tag == installed:
                    current_idx = i
                    break
            if current_idx is None:
                print(f"  [WARN] Current version {installed} not found in releases.")
                show_version_list(releases, installed, "GitHub release")
                return 1
            target_idx = current_idx + rollback
            if target_idx >= len(releases):
                print(f"ERROR: Only {len(releases) - current_idx - 1} older release(s).")
                show_version_list(releases, installed, "GitHub release")
                return 1
            tag = releases[target_idx][0]

        # Find .whl asset in the target release
        print(f"  Looking for .whl in release {tag}...")
        whl_url = _find_wheel_asset(github_repo, tag)
        if whl_url:
            print(f"  Found wheel: {whl_url.split('/')[-1]}")
            pip_uninstall(name)
            return pip_install(whl_url)

        # No wheel -- check for .tar.gz or .zip that might be installable
        print(f"  No .whl found in release {tag} assets.")
        print(f"  Falling back to PyPI for version {tag.lstrip('v')}...")
        pip_uninstall(name)
        return pip_install(f"{name}=={tag.lstrip('v')}")

    return 0


def _find_wheel_asset(repo, tag):
    """Find a .whl download URL in a GitHub release's assets."""
    try:
        result = subprocess.run(
            ["gh", "release", "view", tag, "--repo", repo,
             "--json", "assets", "--jq", ".assets[].url"],
            capture_output=True, text=True, timeout=15
        )
        for url in result.stdout.strip().splitlines():
            if url.endswith(".whl"):
                return url
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _clone_and_install(repo, tag, pkg_name):
    """Clone a GitHub repo to C:\\code-ext and pip install from local source.
    Used as fallback when pip install git+URL fails (no pyproject.toml etc).

    Fallback chain within this function:
      1. pip install from clone root
      2. pip install -e from clone root
      3. Build recipe (if one exists for this package)
      4. Give up with guidance
    """
    clone_dir = _ensure_clone(repo, tag)
    if clone_dir is None:
        return 1

    # Try pip install from local directory
    print(f"  Installing from local clone...")
    rc = subprocess.call(
        [_get_python(), "-m", "pip", "install", str(clone_dir)]
    )
    if rc == 0:
        return 0

    print(f"  Standard pip install failed, trying editable install...")
    rc = subprocess.call(
        [_get_python(), "-m", "pip", "install", "-e", str(clone_dir)]
    )
    if rc == 0:
        return 0

    # Both pip install attempts failed -- try build recipe
    recipe_name = resolve_build_recipe(pkg_name)
    if recipe_name and recipe_name in BUILD_RECIPES:
        print(f"  pip install failed. Trying build recipe: {recipe_name}")
        return BUILD_RECIPES[recipe_name](repo, tag, pkg_name, clone_dir)

    print(f"ERROR: All install methods failed for {pkg_name} from {repo}@{tag}")
    print(f"  This repo may need a build recipe added to comfydbg launcher")
    return 1


def _ensure_clone(repo, tag):
    """Clone (or update) a GitHub repo into C:\\code-ext and checkout the given tag.
    Returns the clone_dir Path, or None on failure."""
    repo_name = repo.split("/")[-1]
    clone_dir = GIT_CLONE_DIR / repo_name

    GIT_CLONE_DIR.mkdir(parents=True, exist_ok=True)

    if clone_dir.exists():
        print(f"  {clone_dir} already exists, updating...")
        subprocess.call(["git", "fetch", "--all"], cwd=str(clone_dir))
    else:
        print(f"  Cloning {repo} to {clone_dir}...")
        rc = subprocess.call(
            ["git", "clone", f"https://github.com/{repo}.git", str(clone_dir)]
        )
        if rc != 0:
            print(f"ERROR: git clone failed")
            return None

    # Checkout the target tag
    print(f"  Checking out {tag}...")
    rc = subprocess.call(["git", "checkout", tag], cwd=str(clone_dir))
    if rc != 0:
        print(f"ERROR: git checkout {tag} failed")
        return None

    return clone_dir


# ---------------------------------------------------------------------------
# Build recipes -- per-package build logic for repos that can't be pip-installed
# directly. Each recipe is a function(repo, tag, pkg_name, clone_dir) -> exit code.
# ---------------------------------------------------------------------------

def _recipe_comfyui_frontend(repo, tag, pkg_name, clone_dir):
    """Build comfyui-frontend-package from clone + GitHub release dist.zip.

    This repo is a JS/TS frontend -- no pyproject.toml at root. The PyPI package
    is built from a setup.py in the 'comfyui_frontend_package/' subdirectory,
    which expects pre-built static assets in its 'static/' folder.

    Steps:
      1. Download dist.zip from the GitHub release assets
      2. Extract into comfyui_frontend_package/comfyui_frontend_package/static/
      3. Build wheel with COMFYUI_FRONTEND_VERSION env var
      4. pip install the resulting .whl
    """
    import shutil
    import zipfile
    import tempfile
    import glob as globmod

    version = tag.lstrip("v")
    pkg_subdir = clone_dir / "comfyui_frontend_package"
    static_dir = pkg_subdir / "comfyui_frontend_package" / "static"

    if not pkg_subdir.exists():
        print(f"ERROR: Expected {pkg_subdir} not found in clone")
        return 1

    setup_py = pkg_subdir / "setup.py"
    if not setup_py.exists():
        print(f"ERROR: No setup.py found in {pkg_subdir}")
        return 1

    # Step 1: Download dist.zip from release
    print(f"  Downloading dist.zip from release {tag}...")
    with tempfile.TemporaryDirectory() as tmpdir:
        rc = subprocess.call(
            ["gh", "release", "download", tag, "--repo", repo,
             "--pattern", "dist.zip", "--dir", tmpdir]
        )
        if rc != 0:
            print(f"ERROR: Failed to download dist.zip from {repo} release {tag}")
            print(f"  This release may not have a dist.zip asset.")
            return 1

        dist_zip = Path(tmpdir) / "dist.zip"
        if not dist_zip.exists():
            print(f"ERROR: dist.zip not found after download")
            return 1

        # Step 2: Extract into static/
        if static_dir.exists():
            shutil.rmtree(static_dir)
        static_dir.mkdir(parents=True)

        print(f"  Extracting dist.zip to {static_dir}...")
        with zipfile.ZipFile(dist_zip, "r") as zf:
            zf.extractall(static_dir)

    # Step 3: Build wheel
    print(f"  Building wheel (version={version})...")
    env = dict(**subprocess.os.environ, COMFYUI_FRONTEND_VERSION=version)
    rc = subprocess.call(
        [_get_python(), "setup.py", "bdist_wheel"],
        cwd=str(pkg_subdir), env=env
    )
    if rc != 0:
        print(f"ERROR: Wheel build failed")
        return 1

    # Step 4: Find and install the wheel
    dist_dir = pkg_subdir / "dist"
    wheels = globmod.glob(str(dist_dir / "*.whl"))
    if not wheels:
        print(f"ERROR: No .whl file found in {dist_dir}")
        return 1

    # Pick the one matching our version (or the newest)
    target_whl = None
    for w in wheels:
        if version.replace(".", "") in Path(w).name or version in Path(w).name:
            target_whl = w
            break
    if not target_whl:
        target_whl = max(wheels, key=lambda w: Path(w).stat().st_mtime)

    print(f"  Installing {Path(target_whl).name}...")
    return pip_install(target_whl)


# Registry of build recipes
BUILD_RECIPES = {
    "comfyui_frontend": _recipe_comfyui_frontend,
}


def extract_workflow_from_image(image_path):
    """Extract embedded workflow JSON from a ComfyUI output image (PNG or WebP).
    Returns (workflow_dict, prompt_dict) or (None, None) on failure."""
    try:
        from PIL import Image
    except ImportError:
        print("ERROR: Pillow is required. Run: pip install Pillow")
        return None, None

    path = Path(image_path)
    if not path.exists():
        print(f"ERROR: File not found: {image_path}")
        return None, None

    workflow = None
    prompt = None

    try:
        img = Image.open(path)
    except Exception as e:
        print(f"ERROR: Cannot open image: {e}")
        return None, None

    if img.format == "PNG":
        # PNG stores workflow in tEXt chunks
        workflow_str = img.info.get("workflow") or img.text.get("workflow")
        prompt_str = img.info.get("prompt") or img.text.get("prompt")
        if workflow_str:
            try:
                workflow = json.loads(workflow_str)
            except json.JSONDecodeError:
                pass
        if prompt_str:
            try:
                prompt = json.loads(prompt_str)
            except json.JSONDecodeError:
                pass

    elif img.format == "WEBP":
        # WebP stores in EXIF with "Workflow:" and "Prompt:" markers
        exif_raw = img.info.get("exif", b"")
        if isinstance(exif_raw, bytes):
            exif_str = exif_raw.decode("utf-8", errors="replace")
        else:
            exif_str = str(exif_raw)

        for marker, target in [("Workflow:", "workflow"), ("Prompt:", "prompt")]:
            idx = exif_str.find(marker)
            if idx < 0:
                continue
            json_start = idx + len(marker)
            brace_count = 0
            json_end = json_start
            for i in range(json_start, len(exif_str)):
                if exif_str[i] == "{":
                    brace_count += 1
                elif exif_str[i] == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        json_end = i + 1
                        break
            try:
                data = json.loads(exif_str[json_start:json_end])
                if target == "workflow":
                    workflow = data
                else:
                    prompt = data
            except json.JSONDecodeError:
                pass
    else:
        print(f"[WARN] Unsupported image format: {img.format}")

    return workflow, prompt


def extract_workflow_from_json(json_path):
    """Load a workflow from a .json file. Returns (workflow_dict, None)."""
    path = Path(json_path)
    if not path.exists():
        print(f"ERROR: File not found: {json_path}")
        return None, None
    try:
        with open(path) as f:
            data = json.load(f)
        return data, None
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}")
        return None, None


def extract_workflow_versions(workflow):
    """Extract version info from a workflow dict.
    Returns a dict with frontendVersion, comfy-core version, node packages, etc."""
    info = {}
    extra = workflow.get("extra", {})
    info["frontendVersion"] = extra.get("frontendVersion", "(not set)")
    info["workflowRendererVersion"] = extra.get("workflowRendererVersion", "(not set)")

    # Extract node package versions
    node_packages = {}
    nodes = workflow.get("nodes", [])
    for node in nodes:
        props = node.get("properties", {})
        cnr_id = props.get("cnr_id", "")
        ver = props.get("ver", "")
        if cnr_id and ver and cnr_id not in node_packages:
            node_packages[cnr_id] = ver

    info["node_packages"] = node_packages
    info["total_nodes"] = len(nodes)

    # Collect unique node types
    node_types = set()
    for node in nodes:
        t = node.get("type", "")
        if t:
            node_types.add(t)
    info["node_types"] = sorted(node_types)

    return info


def _get_comfyui_version():
    """Get current ComfyUI backend version from git tags."""
    try:
        # Exact tag match first
        result = subprocess.run(
            ["git", "describe", "--tags", "--exact-match", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip().lstrip("v")
        # Nearest tag
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip().lstrip("v") + "+"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _get_custom_node_version(cnr_id):
    """Try to get the installed version of a custom node package.
    Checks custom_nodes/ directories for git commit hashes or version files."""
    custom_nodes_dir = Path.cwd() / "custom_nodes"
    if not custom_nodes_dir.exists():
        return None

    # Try common directory name patterns for the cnr_id
    candidates = [
        cnr_id,
        cnr_id.replace("_", "-"),
        cnr_id.replace("-", "_"),
        f"ComfyUI-{cnr_id}",
        f"comfyui-{cnr_id}",
        f"comfyui_{cnr_id}",
    ]

    for candidate in candidates:
        node_dir = custom_nodes_dir / candidate
        if not node_dir.is_dir():
            continue
        # Only check git if this directory is its own repo (has .git)
        git_dir = node_dir / ".git"
        if git_dir.exists():
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                    cwd=str(node_dir)
                )
                if result.returncode == 0:
                    return result.stdout.strip()
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
        # Check for version in pyproject.toml or __init__.py
        for ver_file in ["pyproject.toml", "version.py", "__init__.py"]:
            vf = node_dir / ver_file
            if vf.exists():
                try:
                    content = vf.read_text(errors="replace")
                    m = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
                    if m:
                        return m.group(1)
                except Exception:
                    pass
    return None


def cmd_detect(args):
    """Extract and display version fingerprint from a ComfyUI workflow image or JSON file.
    Compares backend, frontend, and custom node versions against current install
    to identify what has changed since the workflow was created."""
    file_path = args.file

    # Determine file type
    ext = Path(file_path).suffix.lower()
    if ext == ".json":
        workflow, prompt = extract_workflow_from_json(file_path)
    elif ext in (".png", ".webp", ".jpg", ".jpeg"):
        workflow, prompt = extract_workflow_from_image(file_path)
    else:
        print(f"ERROR: Unsupported file type: {ext}")
        print(f"  Supported: .json, .png, .webp")
        return 1

    if workflow is None:
        print("ERROR: Could not extract workflow from file.")
        return 1

    info = extract_workflow_versions(workflow)

    # Current install state
    current_frontend = get_installed_version("comfyui-frontend-package")
    current_backend = _get_comfyui_version()

    # Workflow versions
    wf_frontend = info["frontendVersion"]
    wf_backend = info["node_packages"].get("comfy-core", "(not set)")

    print()
    print("=" * 75)
    print(f"  Workflow Version Fingerprint")
    print(f"  Source: {Path(file_path).name}")
    print("=" * 75)

    # --- Backend (ComfyUI core) ---
    backend_status = "UNKNOWN"
    if current_backend and wf_backend != "(not set)":
        if current_backend.rstrip("+") == wf_backend:
            backend_status = "MATCH"
        else:
            backend_status = "MISMATCH"
    print(f"  Backend:    {wf_backend:<20s} Installed: {current_backend or '(unknown)':>12s}  [{backend_status}]")

    # --- Frontend ---
    frontend_status = "UNKNOWN"
    if current_frontend and wf_frontend != "(not set)":
        if current_frontend == wf_frontend:
            frontend_status = "MATCH"
        else:
            frontend_status = "MISMATCH"
    print(f"  Frontend:   {wf_frontend:<20s} Installed: {current_frontend or '(unknown)':>12s}  [{frontend_status}]")

    print(f"  Renderer:   {info['workflowRendererVersion']}")
    print(f"  Nodes:      {info['total_nodes']} total, {len(info['node_types'])} unique types")

    # --- Node packages with comparison ---
    print()
    print(f"  {'Package':<35s} {'Workflow':<16s} {'Installed':<16s} {'Status'}")
    print(f"  {'-'*35} {'-'*16} {'-'*16} {'-'*12}")

    mismatches = []
    missing = []
    matches = []

    for pkg, wf_ver in sorted(info["node_packages"].items()):
        if pkg == "comfy-core":
            continue  # already shown above

        # Get installed version for this custom node
        installed_ver = _get_custom_node_version(pkg)

        # Truncate git hashes for display
        def short_hash(v):
            if v and len(v) == 40 and all(c in "0123456789abcdef" for c in v):
                return v[:10]
            return v

        wf_display = short_hash(wf_ver) or "(unknown)"
        inst_display = short_hash(installed_ver) or "(not found)"

        # Determine status
        if installed_ver is None:
            status = "NOT FOUND"
            missing.append(pkg)
        elif wf_ver == installed_ver:
            status = "MATCH"
            matches.append(pkg)
        elif (len(wf_ver) == 40 and len(installed_ver) == 40
              and wf_ver != installed_ver):
            status = "CHANGED"
            mismatches.append(pkg)
        elif wf_ver != installed_ver:
            status = "CHANGED"
            mismatches.append(pkg)
        else:
            status = "OK"
            matches.append(pkg)

        print(f"  {pkg:<35s} {wf_display:<16s} {inst_display:<16s} [{status}]")

    # --- Summary ---
    print()
    print(f"  Summary:")
    print(f"    Matching:   {len(matches)} package(s)")
    if mismatches:
        print(f"    Changed:    {len(mismatches)} package(s): {', '.join(mismatches)}")
    if missing:
        print(f"    Not found:  {len(missing)} package(s): {', '.join(missing)}")

    # --- Recovery guidance ---
    if backend_status == "MISMATCH" or frontend_status == "MISMATCH" or mismatches:
        print()
        print(f"  Recovery suggestions:")
        if backend_status == "MISMATCH":
            print(f"    Backend:  git checkout v{wf_backend}")
            # Check what frontend that version expects
            try:
                result = subprocess.run(
                    ["git", "show", f"v{wf_backend}:requirements.txt"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if "comfyui-frontend-package" in line:
                            print(f"              (that release pins: {line.strip()})")
                            break
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
        if frontend_status == "MISMATCH":
            print(f"    Frontend: comfydbg force comfyui-frontend-package=={wf_frontend}")
        if mismatches:
            print(f"    Custom nodes with changes may need rollback in custom_nodes/")

    print()
    print("=" * 75)
    print()

    # Save extracted workflow to JSON if requested
    if args.save:
        out_path = Path(args.save)
        with open(out_path, "w") as f:
            json.dump(workflow, f, indent=2)
        print(f"  Workflow saved to: {out_path}")
        if prompt:
            prompt_path = out_path.with_stem(out_path.stem + "_prompt")
            with open(prompt_path, "w") as f:
                json.dump(prompt, f, indent=2)
            print(f"  Prompt saved to:   {prompt_path}")
        print()

    return 0


def cmd_version(args):
    """Show ComfyUI version info and installed package versions."""
    requirements = parse_requirements(_requirements_file())

    # Key packages shown by default (order matters for readability)
    key_packages = [
        "comfyui-frontend-package",
        "comfyui-workflow-templates",
        "comfyui-embedded-docs",
        "torch",
        "torchvision",
        "torchaudio",
    ]

    # ComfyUI git info
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H %h %ai %s", "-1"],
            capture_output=True, text=True, timeout=5
        )
        git_info = result.stdout.strip() if result.returncode == 0 else "(unknown)"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        git_info = "(unknown)"

    # Check for version tag
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--exact-match", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        git_tag = result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        git_tag = None

    if not git_tag:
        try:
            result = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                capture_output=True, text=True, timeout=5
            )
            nearest_tag = result.stdout.strip() if result.returncode == 0 else None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            nearest_tag = None
    else:
        nearest_tag = None

    # Python version
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    # Determine which packages to show
    if args.all:
        show_packages = list(requirements.keys())
    elif args.packages:
        show_packages = []
        for arg in args.packages:
            name, _, _ = parse_pkg_arg(arg)
            if name:
                show_packages.append(name)
    else:
        show_packages = key_packages

    # Print header
    print()
    print("=" * 65)
    print("  ComfyUI Version Info")
    print("=" * 65)
    if git_tag:
        print(f"  ComfyUI:    {git_tag}")
    elif nearest_tag:
        print(f"  ComfyUI:    {nearest_tag}+ (ahead of release)")
    else:
        print(f"  ComfyUI:    (no tag)")
    print(f"  Commit:     {git_info}")
    print(f"  Python:     {py_version}")
    detected = _get_python()
    print(f"  Executable: {detected}")
    if detected != sys.executable:
        print(f"  comfydbg:   {sys.executable} (host)")
    print()

    # Print package versions
    print(f"  {'Package':<40s} {'Installed':<15s} {'Required'}")
    print(f"  {'-'*40} {'-'*15} {'-'*20}")
    for name in show_packages:
        installed = get_installed_version(name) or "(not installed)"
        required = requirements.get(name, "(not in requirements)")
        # Flag mismatches
        flag = ""
        if installed == "(not installed)":
            flag = " [MISSING]"
        elif required != "(not in requirements)":
            # Extract pinned version if any
            pin_match = re.search(r"==([^\s,;]+)", required)
            if pin_match and pin_match.group(1) != installed:
                flag = " [MISMATCH]"
        print(f"  {name:<40s} {installed:<15s} {required}{flag}")

    print()
    print("=" * 65)
    print()
    return 0


# ---------------------------------------------------------------------------
# Bisect -- binary search for broken custom nodes
# ---------------------------------------------------------------------------

def _bisect_state_file():
    return Path.cwd() / ".bisect_state.json"

def _custom_nodes_dir():
    return Path.cwd() / "custom_nodes"

def _bisect_disabled_dir():
    return Path.cwd() / "custom_nodes_bisect_disabled"


def _get_custom_node_dirs():
    """Get sorted list of custom node directory names (excluding __pycache__)."""
    if not _custom_nodes_dir().exists():
        return []
    return sorted([
        d.name for d in _custom_nodes_dir().iterdir()
        if d.is_dir() and d.name != "__pycache__"
    ])


def _load_bisect_state():
    """Load bisect state from file, or None if no bisect in progress."""
    if not _bisect_state_file().exists():
        return None
    try:
        with open(_bisect_state_file()) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def _save_bisect_state(state):
    """Save bisect state to file."""
    with open(_bisect_state_file(), "w") as f:
        json.dump(state, f, indent=2)


def _clear_bisect_state():
    """Remove bisect state file."""
    _bisect_state_file().unlink(missing_ok=True)


def _bisect_launch(candidates, all_nodes, main_args):
    """Launch ComfyUI with only the candidate nodes enabled."""
    print(f"\n  Launching ComfyUI with {len(candidates)} of {len(all_nodes)} custom nodes enabled...")
    print(f"  Enabled: {', '.join(candidates[:5])}", end="")
    if len(candidates) > 5:
        print(f" ... and {len(candidates) - 5} more")
    else:
        print()
    print()
    print(f"  After testing, run:")
    print(f"    comfydbg bisect good    (if canvas works fine)")
    print(f"    comfydbg bisect bad     (if canvas is broken)")
    print()

    cmd = [_get_python(), "main.py",
           "--disable-all-custom-nodes",
           "--whitelist-custom-nodes"] + candidates + main_args
    print(f"[LAUNCH] {cmd[0]} main.py --disable-all-custom-nodes --whitelist-custom-nodes [{len(candidates)} nodes] {' '.join(main_args)}")
    return subprocess.call(cmd)


def cmd_bisect(args):
    """Binary search for a broken custom node.

    Progressively halves the set of enabled custom nodes to isolate
    which one is causing problems (e.g. canvas corruption).

    Uses --disable-all-custom-nodes + --whitelist-custom-nodes internally,
    so no files are moved or deleted."""

    action = args.action

    if action == "start":
        all_nodes = _get_custom_node_dirs()
        if not all_nodes:
            print("ERROR: No custom nodes found in custom_nodes/")
            return 1

        # Start by testing the first half
        mid = len(all_nodes) // 2
        test_set = all_nodes[:mid]
        other_set = all_nodes[mid:]

        state = {
            "all_nodes": all_nodes,
            "candidates": all_nodes,   # full set still under suspicion
            "test_set": test_set,       # currently being tested
            "other_set": other_set,     # the other half
            "round": 1,
            "history": [],
        }
        _save_bisect_state(state)

        print("=" * 65)
        print("  Bisect started")
        print("=" * 65)
        print(f"  Total custom nodes: {len(all_nodes)}")
        print(f"  Testing first half: {len(test_set)} nodes")
        print(f"  Remaining:          {len(other_set)} nodes (disabled)")

        return _bisect_launch(test_set, all_nodes, args.main_args)

    elif action == "good":
        state = _load_bisect_state()
        if not state:
            print("ERROR: No bisect in progress. Run 'comfydbg bisect start' first.")
            return 1

        # Current test set is good -> culprit is in the OTHER set
        test_set = state["test_set"]
        other_set = state["other_set"]
        round_num = state["round"]

        state["history"].append({
            "round": round_num,
            "tested": test_set,
            "result": "good",
            "conclusion": "culprit in other set",
        })

        # Narrow to the other set
        candidates = other_set
        if len(candidates) <= 1:
            # Found it!
            _save_bisect_state(state)
            print()
            print("=" * 65)
            print("  BISECT COMPLETE -- culprit found!")
            print("=" * 65)
            if candidates:
                print(f"  Broken custom node: {candidates[0]}")
                print(f"  Path: custom_nodes/{candidates[0]}/")
            else:
                print(f"  Could not isolate a single node.")
            print()
            print(f"  To disable it:")
            print(f"    Move custom_nodes/{candidates[0]} to custom_nodes_disabled/")
            print(f"  To clean up bisect state:")
            print(f"    comfydbg bisect reset")
            print("=" * 65)
            return 0

        mid = len(candidates) // 2
        new_test = candidates[:mid]
        new_other = candidates[mid:]

        state["candidates"] = candidates
        state["test_set"] = new_test
        state["other_set"] = new_other
        state["round"] = round_num + 1
        _save_bisect_state(state)

        print(f"\n  Round {round_num + 1}: testing {len(new_test)} of {len(candidates)} remaining candidates")
        return _bisect_launch(new_test, state["all_nodes"], args.main_args)

    elif action == "bad":
        state = _load_bisect_state()
        if not state:
            print("ERROR: No bisect in progress. Run 'comfydbg bisect start' first.")
            return 1

        # Current test set is bad -> culprit is in THIS set
        test_set = state["test_set"]
        round_num = state["round"]

        state["history"].append({
            "round": round_num,
            "tested": test_set,
            "result": "bad",
            "conclusion": "culprit in test set",
        })

        candidates = test_set
        if len(candidates) <= 1:
            # Found it!
            _save_bisect_state(state)
            print()
            print("=" * 65)
            print("  BISECT COMPLETE -- culprit found!")
            print("=" * 65)
            if candidates:
                print(f"  Broken custom node: {candidates[0]}")
                print(f"  Path: custom_nodes/{candidates[0]}/")
            else:
                print(f"  Could not isolate a single node.")
            print()
            print(f"  To disable it:")
            print(f"    Move custom_nodes/{candidates[0]} to custom_nodes_disabled/")
            print(f"  To clean up bisect state:")
            print(f"    comfydbg bisect reset")
            print("=" * 65)
            return 0

        mid = len(candidates) // 2
        new_test = candidates[:mid]
        new_other = candidates[mid:]

        state["candidates"] = candidates
        state["test_set"] = new_test
        state["other_set"] = new_other
        state["round"] = round_num + 1
        _save_bisect_state(state)

        print(f"\n  Round {round_num + 1}: testing {len(new_test)} of {len(candidates)} remaining candidates")
        return _bisect_launch(new_test, state["all_nodes"], args.main_args)

    elif action == "status":
        state = _load_bisect_state()
        if not state:
            print("No bisect in progress.")
            return 0

        print()
        print("=" * 65)
        print("  Bisect Status")
        print("=" * 65)
        print(f"  Total nodes:     {len(state['all_nodes'])}")
        print(f"  Candidates left: {len(state['candidates'])}")
        print(f"  Current round:   {state['round']}")
        print(f"  Testing:         {len(state['test_set'])} nodes")
        print()

        if state["history"]:
            print(f"  History:")
            for h in state["history"]:
                print(f"    Round {h['round']}: tested {len(h['tested'])} nodes -> {h['result']}")
        print()

        print(f"  Current test set:")
        for n in state["test_set"]:
            print(f"    {n}")
        print()
        print(f"  Remaining (disabled):")
        for n in state["other_set"]:
            print(f"    {n}")
        print("=" * 65)
        return 0

    elif action == "reset":
        if _load_bisect_state():
            _clear_bisect_state()
            print("Bisect state cleared. All custom nodes will load normally on next start.")
        else:
            print("No bisect in progress.")
        return 0

    elif action == "exclude":
        # Remove a problem node from the bisect (move to bisect_disabled dir)
        state = _load_bisect_state()
        if not args.node_name:
            print("ERROR: Specify which node to exclude.")
            print("  Usage: comfydbg bisect exclude <node-name>")
            return 1

        node_name = args.node_name
        src = _custom_nodes_dir() / node_name
        if not src.exists():
            print(f"ERROR: {node_name} not found in custom_nodes/")
            return 1

        _bisect_disabled_dir().mkdir(exist_ok=True)
        dst = _bisect_disabled_dir() / node_name
        print(f"  Moving {node_name} -> custom_nodes_bisect_disabled/")

        import shutil
        shutil.move(str(src), str(dst))

        # Remove from bisect state if active
        if state:
            excluded = state.get("excluded", [])
            excluded.append(node_name)
            state["excluded"] = excluded

            for key in ("all_nodes", "candidates", "test_set", "other_set"):
                if node_name in state[key]:
                    state[key].remove(node_name)

            _save_bisect_state(state)
            print(f"  Removed from bisect candidates ({len(state['candidates'])} remaining)")

            # Re-split current candidates so next good/bad works correctly
            candidates = state["candidates"]
            mid = len(candidates) // 2
            state["test_set"] = candidates[:mid]
            state["other_set"] = candidates[mid:]
            _save_bisect_state(state)
        else:
            print(f"  (no bisect in progress, node moved but no state to update)")

        print(f"  Done. Continue bisecting or run 'comfydbg bisect skip' to relaunch.")
        return 0

    elif action == "restore":
        # Move all excluded nodes back from bisect_disabled to custom_nodes
        if not _bisect_disabled_dir().exists():
            print("No bisect-disabled nodes to restore.")
            return 0

        import shutil
        restored = []
        for item in _bisect_disabled_dir().iterdir():
            dst = _custom_nodes_dir() / item.name
            if dst.exists():
                print(f"  [WARN] {item.name} already exists in custom_nodes/, skipping")
                continue
            shutil.move(str(item), str(dst))
            restored.append(item.name)

        if restored:
            print(f"  Restored {len(restored)} node(s): {', '.join(restored)}")
        else:
            print("  Nothing to restore.")

        # Clean up empty dir
        try:
            _bisect_disabled_dir().rmdir()
        except OSError:
            pass

        return 0

    elif action == "skip":
        # Skip both halves and test a random split instead
        state = _load_bisect_state()
        if not state:
            print("ERROR: No bisect in progress.")
            return 1

        # Can't determine from this set -- combine and resplit differently
        candidates = state["candidates"]
        round_num = state["round"]

        state["history"].append({
            "round": round_num,
            "tested": state["test_set"],
            "result": "skip",
            "conclusion": "inconclusive, reshuffled",
        })

        # Interleave instead of splitting by position
        even = [candidates[i] for i in range(0, len(candidates), 2)]
        odd = [candidates[i] for i in range(1, len(candidates), 2)]

        state["test_set"] = even
        state["other_set"] = odd
        state["round"] = round_num + 1
        _save_bisect_state(state)

        print(f"\n  Round {round_num + 1} (reshuffled): testing {len(even)} of {len(candidates)} candidates")
        return _bisect_launch(even, state["all_nodes"], args.main_args)

    else:
        print(f"ERROR: Unknown bisect action: {action}")
        print(f"  Valid actions: start, good, bad, skip, exclude, restore, status, reset")
        return 1


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def parse_rollback(value):
    """Parse rollback argument like '-1', '-2', etc. into a positive int."""
    match = re.match(r"^-(\d+)$", value)
    if not match:
        raise argparse.ArgumentTypeError(
            f"Invalid rollback '{value}'. Use -1, -2, -3, etc."
        )
    return int(match.group(1))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="comfydbg",
        description="ComfyUI startup manager, version detective, and troubleshooting toolkit.",
        epilog=(
            "examples:\n"
            "  comfydbg                                           default: skip comfyui-frontend-package\n"
            "  comfydbg skip pkg1 pkg2 -- --listen 0.0.0.0        skip packages, pass args to main.py\n"
            "  comfydbg full                                      install everything from requirements.txt\n"
            "  comfydbg force comfyui-frontend-package==1.39.19   force specific version\n"
            "  comfydbg install comfyui-frontend-package -1       rollback one PyPI version\n"
            "  comfydbg install comfyui-frontend-package -HN 5    5th from newest on PyPI\n"
            "  comfydbg install comfyui-frontend-package -G -CN 2 two GitHub releases back from current\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="available commands")

    # -- skip --
    p_skip = subparsers.add_parser(
        "skip",
        help="Install requirements.txt, excluding listed packages",
        description=(
            "Install from requirements.txt but exclude specified packages.\n"
            "Shows installed vs available version for each skipped package.\n"
            "Default: skips comfyui-frontend-package."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_skip.add_argument(
        "packages", nargs="*", metavar="PKG",
        help="Package(s) to exclude (default: comfyui-frontend-package)",
    )
    p_skip.add_argument(
        "main_args", nargs="*", default=[],
        help=argparse.SUPPRESS,  # populated by -- separator handling
    )
    p_skip.set_defaults(func=cmd_skip)

    # -- full --
    p_full = subparsers.add_parser(
        "full",
        help="Install all requirements.txt as-is",
        description=(
            "Run pip install -r requirements.txt with no filtering or exclusions.\n"
            "Use -F to force-reinstall everything and bypass pip cache."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_full.add_argument(
        "-F", "--force-reinstall", action="store_true",
        help="Force reinstall all packages, bypassing cache (pip --force-reinstall --no-cache-dir)",
    )
    p_full.add_argument(
        "main_args", nargs="*", default=[],
        help=argparse.SUPPRESS,
    )
    p_full.set_defaults(func=cmd_full)

    # -- force --
    p_force = subparsers.add_parser(
        "force",
        help="Uninstall/reinstall specified packages (clears pip cache issues)",
        description=(
            "Uninstall then reinstall specified packages to clear cached state.\n"
            "Supports version pins: comfydbg force comfyui-frontend-package==1.39.19\n"
            "Default: forces comfyui-frontend-package."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_force.add_argument(
        "packages", nargs="*", metavar="PKG[==VER]",
        help="Package(s) to force-reinstall, with optional version pin",
    )
    p_force.add_argument(
        "main_args", nargs="*", default=[],
        help=argparse.SUPPRESS,
    )
    p_force.set_defaults(func=cmd_force)

    # -- install --
    p_install = subparsers.add_parser(
        "install",
        help="Install a single package from PyPI, GitHub git, or GitHub wheel",
        description=(
            "Install a single package with flexible source and version rollback.\n"
            "Does NOT launch main.py -- standalone package management.\n\n"
            "Sources:\n"
            "  PyPI (default)   pip install from PyPI registry\n"
            "  GitHub git (-G)  pip install git+https://github.com/owner/repo@tag\n"
            "  GitHub wheel(-W) download .whl from GitHub release assets\n\n"
            "Rollback: -1/-2/-3 as shortcuts, or -N <num> for any offset.\n"
            "Steps back from the currently installed version through the\n"
            "version history of the selected source.\n\n"
            "Package can be a PyPI name or GitHub owner/repo:\n"
            "  comfyui-frontend-package    looked up in requirements.txt + PyPI\n"
            "  Comfy-Org/ComfyUI_frontend  treated as GitHub repo\n\n"
            "Known PyPI-to-GitHub mappings (no need to type owner/repo):\n"
            + "".join(f"  {k:40s} -> {v['repo']}\n" for k, v in PYPI_TO_GITHUB.items())
            + "\n"
            "examples:\n"
            "  comfydbg install comfyui-frontend-package          from requirements.txt\n"
            "  comfydbg install comfyui-frontend-package -1       one version back from current\n"
            "  comfydbg install comfyui-frontend-package -CN 5    five versions back from current\n"
            "  comfydbg install comfyui-frontend-package -HN 3    3rd from newest on PyPI\n"
            "  comfydbg install comfyui-frontend-package -G       latest GitHub tag\n"
            "  comfydbg install comfyui-frontend-package -G -HN 2 2nd newest GitHub release\n"
            "  comfydbg install Comfy-Org/ComfyUI_frontend -G -1  owner/repo shorthand\n"
            "  comfydbg install comfyui-frontend-package -W       wheel from GitHub release\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_install.add_argument(
        "package", metavar="PKG",
        help="Package name (PyPI) or owner/repo (GitHub)",
    )
    p_install.add_argument(
        "-1", dest="current_nth", action="store_const", const=1,
        help="Rollback 1 version from current (shortcut for -CN 1)",
    )
    p_install.add_argument(
        "-2", dest="current_nth", action="store_const", const=2,
        help="Rollback 2 versions from current (shortcut for -CN 2)",
    )
    p_install.add_argument(
        "-3", dest="current_nth", action="store_const", const=3,
        help="Rollback 3 versions from current (shortcut for -CN 3)",
    )
    p_install.add_argument(
        "-CN", "--current-nth", dest="current_nth", type=int, default=None,
        metavar="NUM",
        help="Rollback NUM versions from currently installed version",
    )
    p_install.add_argument(
        "-HN", "--head-nth", dest="head_nth", type=int, default=None,
        metavar="NUM",
        help="Rollback NUM versions from newest available (head of version list)",
    )
    p_install.add_argument(
        "-G", "--git", action="store_true",
        help="Install from GitHub git tag (pip install git+URL@tag)",
    )
    p_install.add_argument(
        "-W", "--wheel", action="store_true",
        help="Install .whl from GitHub release assets",
    )
    p_install.add_argument(
        "-U", "--uninstall", action="store_true",
        help="Uninstall the package first before installing (clears cached state)",
    )
    p_install.set_defaults(func=cmd_install)

    # -- version --
    p_version = subparsers.add_parser(
        "version",
        help="Show ComfyUI and package version info",
        description=(
            "Display ComfyUI git version, Python version, and installed\n"
            "package versions compared to requirements.txt.\n\n"
            "By default shows key packages (frontend, torch, etc).\n"
            "Use --all for everything, or name specific packages.\n\n"
            "examples:\n"
            "  comfydbg version                               key packages\n"
            "  comfydbg version --all                         all packages\n"
            "  comfydbg version comfyui-frontend-package      specific package\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_version.add_argument(
        "packages", nargs="*", metavar="PKG",
        help="Specific package(s) to show (default: key packages)",
    )
    p_version.add_argument(
        "-a", "--all", action="store_true",
        help="Show all packages from requirements.txt",
    )
    p_version.set_defaults(func=cmd_version)

    # -- detect --
    p_detect = subparsers.add_parser(
        "detect",
        help="Extract version fingerprint from a workflow image or JSON",
        description=(
            "Read a ComfyUI output image (.png, .webp) or workflow .json file\n"
            "and display the version fingerprint embedded in it: frontend version,\n"
            "comfy-core version, custom node packages and their versions.\n\n"
            "Compares against currently installed versions to highlight mismatches.\n"
            "Use --save to extract the workflow JSON from an image to a file.\n\n"
            "examples:\n"
            "  comfydbg detect output/my_image.webp\n"
            "  comfydbg detect output/my_image.png --save recovered_workflow.json\n"
            "  comfydbg detect workflow.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_detect.add_argument(
        "file", metavar="FILE",
        help="Image (.png, .webp) or workflow (.json) file to analyze",
    )
    p_detect.add_argument(
        "--save", "-s", metavar="OUTPUT",
        help="Save extracted workflow to a JSON file",
    )
    p_detect.set_defaults(func=cmd_detect)

    # -- bisect --
    p_bisect = subparsers.add_parser(
        "bisect",
        help="Binary search for a broken custom node",
        description=(
            "Isolate which custom node is causing problems (e.g. canvas\n"
            "corruption) by progressively halving the set of enabled nodes.\n\n"
            "No files are moved or deleted -- uses ComfyUI's\n"
            "--disable-all-custom-nodes + --whitelist-custom-nodes flags.\n\n"
            "Workflow:\n"
            "  1. comfydbg bisect start          launch with first half of nodes\n"
            "  2. Test the canvas / workflow\n"
            "  3. comfydbg bisect good           canvas OK -> culprit in other half\n"
            "     comfydbg bisect bad            canvas broken -> culprit in this half\n"
            "     comfydbg bisect skip           inconclusive -> reshuffle and retry\n"
            "     comfydbg bisect exclude <node> remove a crashing node and continue\n"
            "  4. Repeat until culprit is found\n"
            "  5. comfydbg bisect restore        move excluded nodes back\n"
            "  6. comfydbg bisect reset          clean up state\n\n"
            "Typically takes 5-6 rounds for ~60 nodes.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_bisect.add_argument(
        "action",
        choices=["start", "good", "bad", "skip", "exclude", "restore", "status", "reset"],
        help="Bisect action to perform",
    )
    p_bisect.add_argument(
        "node_name", nargs="?", default=None,
        help="Node name (used with 'exclude' action)",
    )
    p_bisect.add_argument(
        "main_args", nargs="*", default=[],
        help=argparse.SUPPRESS,
    )
    p_bisect.set_defaults(func=cmd_bisect)

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    raw_args = sys.argv[1:]

    # Split at '--' to separate launcher args from main.py args
    if "--" in raw_args:
        idx = raw_args.index("--")
        launcher_args = raw_args[:idx]
        main_args = raw_args[idx + 1:]
    else:
        launcher_args = raw_args
        main_args = []

    parser = build_parser()

    # Default to 'skip' when no subcommand given
    if not launcher_args or (launcher_args[0] not in ("skip", "full", "force", "install", "version", "detect", "bisect", "-h", "--help")):
        # Bare 'comfydbg' or 'comfydbg -- --listen' -> default skip mode
        namespace = argparse.Namespace(
            command="skip", packages=[], main_args=main_args or launcher_args, func=cmd_skip,
        )
    else:
        namespace = parser.parse_args(launcher_args)
        namespace.main_args = main_args

    if not hasattr(namespace, "func"):
        parser.print_help()
        return 0

    # Commands that need a ComfyUI directory (requirements.txt)
    needs_comfyui_dir = {"skip", "full", "force"}
    cmd = getattr(namespace, "command", None)
    if cmd in needs_comfyui_dir and not _requirements_file().exists():
        print(f"ERROR: No requirements.txt found in {Path.cwd()}")
        print(f"  comfydbg must be run from a ComfyUI project directory.")
        print()

        # Auto-discover ComfyUI installations
        installations = discover_comfyui_installations()
        if installations:
            print(f"  Found ComfyUI installations:")
            for i, (desc, path) in enumerate(installations, 1):
                print(f"    [{i}] {desc:20s} {path}")
            print()
            print(f"  Run from one of these:")
            for desc, path in installations:
                print(f"    cd {path} && comfydbg")
        else:
            print(f"  No ComfyUI installations found in common locations.")
            print(f"  Make sure you're in the ComfyUI root directory (containing main.py).")
        print()
        print(f"  Commands that work from any directory:")
        print(f"    comfydbg -h                    show help")
        print(f"    comfydbg detect <image>        workflow fingerprint")
        return 1

    return namespace.func(namespace)


if __name__ == "__main__":
    sys.exit(main() or 0)

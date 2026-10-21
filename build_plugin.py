"""Build and install the ScreenControl plugin.

    python build_plugin.py install              # build and install into Flow Launcher, restart Flow
    python build_plugin.py install --no-restart # install only; restart Flow yourself
    python build_plugin.py install --hotkey "Ctrl+Alt+F9"   # change the shortcut ("none" removes it)
    python build_plugin.py install-lib          # (re)create lib/ with the dependencies
    python build_plugin.py ScreenControl.zip    # package a distributable archive
    python build_plugin.py manifest --owner <github-user>   # Flow plugin store entry (see readme)

Only pure-Python wheels go into lib/, so the plugin runs on whatever Python
version Flow is configured with (by default its embedded 3.11), regardless of
the Python used to build it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIB = ROOT / "lib"
FLOGIN = "flogin @ git+https://github.com/cibere/flogin@478211372d3c7c95f9c242d4cadf763af0844b21"
# flogin is installed without its dependencies: its pip helper (requests) is
# stubbed out in screen_control/__init__.py, and the plugin reads and writes
# Flow's pipes itself (screen_control/plugin.py) instead of using aioconsole (GPL).
PURE_DEPS: tuple[str, ...] = ()
INSTALL_FOLDER = "ScreenControl"
DEFAULT_HOTKEY = "Ctrl + Alt + Shift + C"
HOTKEY_QUERY = "sc "
_MODIFIERS = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "windows": "Win"}
DEFAULT_FLOW_DATA = Path(os.environ.get("APPDATA", "")) / "FlowLauncher"


def pip(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(LIB), *args], check=True)


def install_lib() -> None:
    shutil.rmtree(LIB, ignore_errors=True)
    pip("--no-deps", FLOGIN)
    if PURE_DEPS:
        pip("--only-binary=:all:", "--platform", "any", "--implementation", "py", "--python-version", "3.11", *PURE_DEPS)
    compiled = [p for p in LIB.rglob("*") if p.suffix in (".pyd", ".dll", ".so")]
    if compiled:
        raise SystemExit(f"lib/ contains compiled files, which are Python-version specific: {compiled}")
    print(f"lib/ ready: {sorted(p.name for p in LIB.iterdir() if p.is_dir() and not p.name.endswith('.dist-info'))}")


def collect() -> list[Path]:
    if not (LIB / "flogin").is_dir():
        raise SystemExit("lib/ is missing: run `python build_plugin.py install-lib` first")

    files = [ROOT / "plugin.json", ROOT / "main.py", ROOT / "LICENSE", ROOT / "THIRD_PARTY_NOTICES.md"]
    files += sorted((ROOT / "assets").glob("*.png"))
    files += sorted((ROOT / "screen_control").rglob("*.py"))
    files += sorted(
        p
        for p in LIB.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.parent != LIB / "bin"
    )
    return files


RELEASE_ZIP = "Flow.Launcher.Plugin.ScreenControl.zip"
DEFAULT_REPO = "flow-launcher-screen-control"


def build(archive_name: str) -> None:
    files = collect()
    with zipfile.ZipFile(archive_name, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in files:
            zf.write(file, file.relative_to(ROOT).as_posix())
    check_archive(archive_name)
    print(f"Added {len(files)} files. Archive saved to {archive_name}")


def check_archive(archive_name: str) -> None:
    """What Flow and its plugin store expect from the release zip."""
    with zipfile.ZipFile(archive_name) as zf:
        names = zf.namelist()
        # The store's updater reads the *first* file ending in plugin.json.
        manifests = [n for n in names if n.endswith("plugin.json")]
        if manifests != ["plugin.json"] or names[0] != "plugin.json":
            raise SystemExit(f"plugin.json must be the first and only *plugin.json in the archive, got {manifests}")
        meta = json.loads(zf.read("plugin.json"))
        for required in ("ID", "Name", "Version", "Language", "ExecuteFileName", "IcoPath", "ActionKeyword"):
            if not meta.get(required):
                raise SystemExit(f"plugin.json is missing {required}")
        for path in (meta["ExecuteFileName"], meta["IcoPath"], "lib/flogin/__init__.py"):
            if path not in names:
                raise SystemExit(f"{path} is missing from the archive")
        compiled = [n for n in names if n.endswith((".pyd", ".dll", ".so", ".pyc"))]
        if compiled:
            raise SystemExit(f"compiled files would tie the plugin to one Python version: {compiled[:5]}")


# Same rules as Flow.Launcher.PluginsManifest/ci/src (validator.py, _utils.py).
_MANIFEST_FIELDS = (
    "ID", "Name", "Description", "Author", "Version", "Language", "Website", "UrlDownload", "UrlSourceCode", "IcoPath",
)
_DOWNLOAD_RE = re.compile(
    r"https://github\.com/(?P<author>[a-zA-Z0-9-]+)/(?P<repo>[a-zA-Z0-9\.\-\_]*)/releases/download/"
    r"(?P<version>[a-zA-Z\.0-9]+)/(?P<filename>.*)\.zip"
)
_LANGUAGES = ("csharp", "executable", "fsharp", "python", "javascript", "typescript",
              "python_v2", "executable_v2", "javascript_v2", "typescript_v2")


def manifest_entry(owner: str, repo: str, branch: str) -> dict[str, str]:
    meta = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    source = f"https://github.com/{owner}/{repo}"
    return {
        "ID": meta["ID"],
        "Name": meta["Name"],
        "Description": meta["Description"],
        "Author": meta["Author"],
        "Version": meta["Version"],
        "Language": meta["Language"],
        "Website": source,
        "UrlDownload": f"{source}/releases/download/v{meta['Version']}/{RELEASE_ZIP}",
        "UrlSourceCode": source,
        # The store requires a CDN URL for the icon (jsDelivr serves GitHub files).
        "IcoPath": f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{meta['IcoPath']}",
    }


def validate_manifest_entry(entry: dict[str, str]) -> list[str]:
    problems = [f"missing {f}" for f in _MANIFEST_FIELDS if not entry.get(f)]
    try:
        if uuid.UUID(entry["ID"]).version != 4:
            problems.append("ID is not a version 4 UUID")
    except (KeyError, ValueError):
        problems.append("ID is not a UUID")
    if entry.get("Language") not in _LANGUAGES:
        problems.append(f"Language must be one of {_LANGUAGES}")
    if not _DOWNLOAD_RE.fullmatch(entry.get("UrlDownload", "")):
        problems.append("UrlDownload does not match github.com/<owner>/<repo>/releases/download/<tag>/<name>.zip")
    if not entry.get("IcoPath", "").startswith("https://cdn.jsdelivr.net/"):
        problems.append("IcoPath must be a CDN URL")
    return problems


def write_manifest(owner: str, repo: str, branch: str, set_website: bool) -> None:
    if not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        raise SystemExit(f"{owner!r} is not a GitHub user or organisation name")
    entry = manifest_entry(owner, repo, branch)
    problems = validate_manifest_entry(entry)
    if problems:
        raise SystemExit("Manifest entry is invalid:\n- " + "\n- ".join(problems))
    out = ROOT / "release" / f"{entry['Name']}-{entry['ID']}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(entry, indent=4) + "\n", encoding="utf-8")
    print(f"Manifest entry written to {out}")
    if set_website:
        plugin_json = ROOT / "plugin.json"
        meta = json.loads(plugin_json.read_text(encoding="utf-8"))
        meta["Website"] = entry["Website"]
        plugin_json.write_text(json.dumps(meta, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"plugin.json Website set to {entry['Website']}")


# --------------------------------------------------------------------------- #
# Install into Flow Launcher
# --------------------------------------------------------------------------- #


def _flow_exe() -> Path | None:
    exe = Path(os.environ.get("LOCALAPPDATA", "")) / "FlowLauncher" / "Flow.Launcher.exe"
    return exe if exe.is_file() else None


def _powershell(script: str) -> str:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def _flow_running() -> bool:
    return bool(_powershell("Get-Process Flow.Launcher -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Id"))


def _stop_flow() -> bool:
    """Close Flow (gracefully first, so it saves its settings). True if it was running."""
    if not _flow_running():
        return False
    print("Closing Flow Launcher...")
    _powershell("Get-Process Flow.Launcher -ErrorAction SilentlyContinue | ForEach-Object { $_.CloseMainWindow() | Out-Null }")
    for _ in range(50):
        if not _flow_running():
            break
        time.sleep(0.1)
    else:
        subprocess.run(["taskkill", "/IM", "Flow.Launcher.exe", "/T"], capture_output=True, check=False)
        for _ in range(50):
            if not _flow_running():
                break
            time.sleep(0.1)
        else:
            subprocess.run(["taskkill", "/IM", "Flow.Launcher.exe", "/T", "/F"], capture_output=True, check=False)
            time.sleep(1)
    return True


def _stop_plugin_processes(folder: Path) -> None:
    # The plugin's Python process can outlive Flow for a moment.
    pattern = str(folder).replace("'", "''")
    _powershell(
        "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{pattern}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )


def _installed_copies(plugins_dir: Path, plugin_id: str, exclude: Path) -> list[Path]:
    copies = []
    for manifest in plugins_dir.glob("*/plugin.json"):
        if manifest.parent == exclude:
            continue
        try:
            if json.loads(manifest.read_text(encoding="utf-8-sig")).get("ID") == plugin_id:
                copies.append(manifest.parent)
        except (OSError, ValueError):
            continue
    return copies


def normalize_hotkey(spec: str) -> str:
    """'ctrl+shift+alt+b' -> 'Ctrl + Alt + Shift + B' (Flow's own format and order)."""
    mods: set[str] = set()
    key = None
    for part in (p.strip() for p in spec.split("+")):
        if not part:
            continue
        if part.lower() in _MODIFIERS:
            mods.add(_MODIFIERS[part.lower()])
        elif key is None:
            key = part.upper() if len(part) == 1 else part[0].upper() + part[1:]
        else:
            raise SystemExit(f"Invalid hotkey {spec!r}: more than one non-modifier key")
    if key is None or not mods:
        raise SystemExit(f"Invalid hotkey {spec!r}: use modifiers plus one key, e.g. Ctrl+Alt+Shift+C")
    return " + ".join([m for m in ("Ctrl", "Alt", "Shift", "Win") if m in mods] + [key])


def configure_hotkey(flow_data: Path, requested: str | None) -> str:
    """Make Flow open "sc" with a shortcut.

    requested=None keeps any shortcut the user already set for "sc" (so it can be
    rebound in Flow's settings), adding the default only if there is none.
    """
    settings_path = flow_data / "Settings" / "Settings.json"
    if not settings_path.is_file():
        return "Flow settings not found: hotkey not configured"
    raw = settings_path.read_text(encoding="utf-8-sig")
    settings = json.loads(raw)
    hotkeys = settings.get("CustomPluginHotkeys") or []
    ours = [h for h in hotkeys if str(h.get("ActionKeyword", "")).strip() == HOTKEY_QUERY.strip()]

    if requested is None:
        if ours:
            return f"Keeping your shortcut {ours[0].get('Hotkey')} for 'sc'"
        wanted: str | None = DEFAULT_HOTKEY
    elif requested.strip().lower() in ("none", "off", "remove", ""):
        wanted = None
    else:
        wanted = normalize_hotkey(requested)

    others = [h for h in hotkeys if h not in ours]
    clash = next((h for h in others if wanted and h.get("Hotkey", "").replace(" ", "") == wanted.replace(" ", "")), None)
    if clash:
        raise SystemExit(f"{wanted} is already used by Flow for '{clash.get('ActionKeyword')}'")
    settings["CustomPluginHotkeys"] = others + ([{"Hotkey": wanted, "ActionKeyword": HOTKEY_QUERY}] if wanted else [])

    shutil.copy2(settings_path, settings_path.with_name("Settings.json.screencontrol.bak"))
    tmp = settings_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, settings_path)
    return f"Shortcut {wanted} opens 'sc'" if wanted else "Removed the shortcut for 'sc'"


def install(flow_data: Path, restart: bool, hotkey: str | None = None) -> None:
    plugins_dir = flow_data / "Plugins"
    if not plugins_dir.is_dir():
        raise SystemExit(f"Flow Launcher plugins folder not found: {plugins_dir} (use --flow-data for portable installs)")
    if not (LIB / "flogin").is_dir():
        install_lib()

    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    files = collect()
    target = plugins_dir / INSTALL_FOLDER

    staging = plugins_dir / f".{INSTALL_FOLDER}.new"
    shutil.rmtree(staging, ignore_errors=True)
    for file in files:
        dest = staging / file.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, dest)

    # Only touch the running Flow when installing into the folder it uses.
    manages_flow = flow_data.resolve() == DEFAULT_FLOW_DATA.resolve()
    was_running = _stop_flow() if manages_flow else False
    old_copies = _installed_copies(plugins_dir, manifest["ID"], exclude=staging)
    for folder in old_copies:
        _stop_plugin_processes(folder)
        # Keep the log of the previous install, it is useful when reporting bugs.
        log = folder / "screen_control.log"
        if log.is_file():
            shutil.copy2(log, staging / "screen_control.log")
        shutil.rmtree(folder)
    if target.exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit(f"{target} exists and is not this plugin: move it away and run again")
    staging.rename(target)
    print(f"Installed ScreenControl {manifest['Version']} to {target} ({len(files)} files)")
    # Flow rewrites Settings.json when it exits, so only edit it while Flow is closed.
    if manages_flow or not _flow_running():
        print(configure_hotkey(flow_data, hotkey) + " (change it in Flow: Settings > Hotkey > Custom Query Hotkeys)")
    else:
        print("Flow is running: hotkey not configured (close Flow and run again)")

    exe = _flow_exe()
    if restart and manages_flow and exe is not None:
        subprocess.Popen([str(exe)], close_fds=True, creationflags=0x00000008)  # DETACHED_PROCESS
        print(f"Flow Launcher {'restarted' if was_running else 'started'}. Type 'sc' to use the plugin.")
    else:
        print("Start Flow Launcher to load the plugin, then type 'sc'.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("install-lib", help="(re)create lib/ with the bundled dependencies")
    p_install = sub.add_parser("install", help="install into Flow Launcher")
    p_install.add_argument("--no-restart", action="store_true", help="do not start Flow Launcher afterwards")
    p_install.add_argument(
        "--hotkey",
        help=f"shortcut that opens 'sc' (default {DEFAULT_HOTKEY}, kept if you already set one; 'none' removes it)",
    )
    p_install.add_argument(
        "--flow-data",
        type=Path,
        default=DEFAULT_FLOW_DATA,
        help=r"Flow's data folder (default %%APPDATA%%\FlowLauncher). Flow is only closed/restarted for the default folder.",
    )
    p_zip = sub.add_parser("zip", help="build a distributable archive")
    p_zip.add_argument("archive", nargs="?", default=RELEASE_ZIP)
    p_man = sub.add_parser("manifest", help="write the Flow plugin store entry into release/")
    p_man.add_argument("--owner", required=True, help="GitHub user or organisation hosting the repository")
    p_man.add_argument("--repo", default=DEFAULT_REPO, help=f"repository name (default {DEFAULT_REPO})")
    p_man.add_argument("--branch", default="main", help="default branch, for the icon URL (default main)")
    p_man.add_argument("--set-website", action="store_true", help="also set Website in plugin.json")

    # Keep the short form `python build_plugin.py Name.zip` working.
    argv = sys.argv[1:]
    if len(argv) == 1 and argv[0].endswith(".zip"):
        argv = ["zip", argv[0]]
    args = ap.parse_args(argv)

    if args.command == "install-lib":
        install_lib()
    elif args.command == "install":
        install(args.flow_data, restart=not args.no_restart, hotkey=args.hotkey)
    elif args.command == "manifest":
        write_manifest(args.owner, args.repo, args.branch, args.set_website)
    else:
        build(args.archive)


if __name__ == "__main__":
    main()

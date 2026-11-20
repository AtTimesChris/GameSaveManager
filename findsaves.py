#!/usr/bin/env python3
"""
findsaves.py - find where a game keeps its saves and settings (a Procmon-style helper)
=====================================================================================
Separate from savemanager.py: it never touches your backups or your saves. It only LOOKS.
It sees files that were WRITTEN (changed), not files that were only read.

Just double-click / run it with no arguments and use the menu. Everything is a number,
so the Steam Deck's on-screen keyboard (Steam + X) is enough.

The easy method (no time pressure):
  1. Menu 1  -> take a "before" snapshot of the places games usually save to
  2. Switch to the game, make a save, quit the game
  3. Menu 2  -> compare: it lists exactly what the game wrote, grouped by folder.
     The folder with the .sav / profile files in it is your save folder.

Command line (optional):
  python3 findsaves.py --prefixes                list Proton prefixes by recent activity
  python3 findsaves.py --id 2402812623 --watch   watch a Proton game live
  python3 findsaves.py /some/folder --minutes 5 --match sav
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path, PureWindowsPath

IS_WINDOWS = sys.platform.startswith("win")
HOME = Path.home()
COMPAT = HOME / ".local/share/Steam/steamapps/compatdata"
SNAPSHOT_FILE = Path(tempfile.gettempdir()) / "findsaves-snapshot.json"
LAST_FILE = Path(tempfile.gettempdir()) / "findsaves-last-changes.json"

# Folders that are never saves and are huge or change constantly.
SKIP_DIRS = {"cache", "gpucache", "code cache", "shadercache", "logs", "crashpad", "crashes",
             "trash", "baloo", "kwalletd", "steam", "chromium", "google-chrome", "mozilla",
             ".mozilla", "node_modules", "__pycache__", "temp", "tmp"}

# File patterns that Wine/Proton and the desktop rewrite all the time. --all turns this off.
NOISE = ["*.reg", "*/drive_c/windows/*", "*/drive_c/program files*", "*/drive_c/programdata/microsoft/*",
         "*/pfx/.update-timestamp", "*/version", "*/config_info", "*/tracked_files", "*.log", "*.tmp",
         "*/.config/*rc", "*/.config/kdeglobals", "*/.config/session/*", "*/.config/dconf/*",
         "*/.config/pulse/*", "*/.local/state/*", "*/.local/share/recently-used.xbel",
         "*/.local/share/klipper/*", "*/.config/plasma*", "*/.config/kate*", "*/.config/gtk-*"]


def fmt(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%d/%m/%y %H:%M:%S")


def human(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"

        size /= 1024


def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""


def is_noise(path):
    text = Path(path).as_posix().lower()
    return any(fnmatch.fnmatch(text, pattern) for pattern in NOISE)


def scan(roots):
    """{ 'full path': [modified time, size] } for every file under the roots (skipping junk folders)."""
    found = {}

    for root in roots:
        root = Path(root)

        if not root.exists():
            continue

        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]

            for name in files:
                path = os.path.join(folder, name)

                try:
                    stat = os.stat(path)
                except OSError:
                    continue

                found[path] = [stat.st_mtime, stat.st_size]

    return found


def changed_between(before, after):
    return {p: v for p, v in after.items() if p not in before or before[p] != v}


# ----------------------------------------------------------------------
# Where to look
# ----------------------------------------------------------------------
def common_roots():
    """The places games normally keep saves / settings."""
    if IS_WINDOWS:
        profile = Path(os.environ.get("USERPROFILE", str(HOME)))
        roots = [os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA"),
                 profile / "AppData" / "LocalLow", profile / "Documents", profile / "Saved Games"]
    else:
        roots = [HOME / ".config", HOME / ".local/share", HOME / ".var/app",
                 HOME / "Documents", HOME / "Desktop"]

    return [Path(r) for r in roots if r]


def sd_roots():
    base = Path("/run/media")
    roots = []

    if base.exists():
        for entry in base.iterdir():
            roots.append(entry)

            if entry.is_dir():
                roots.extend(p for p in entry.iterdir() if p.is_dir() and entry.name == "deck")

    return roots


def install_roots():
    """Windows: places games are installed (some keep their saves right next to the .exe)."""
    candidates = [Path(r"C:\Program Files (x86)\Steam\steamapps\common"), Path(r"C:\Program Files\Steam\steamapps\common")]

    for drive in "CDEFG":
        for sub in (r"Games", r"SteamLibrary\steamapps\common", r"Steam\steamapps\common", r"Games\Steam\steamapps\common"):
            candidates.append(Path(f"{drive}:\\{sub}"))

    seen = []

    for path in candidates:
        if path.exists() and path not in seen:
            seen.append(path)

    return seen


def all_compat_roots():
    """compatdata folders of every Steam library on the Deck (internal storage and the SD card)."""
    roots = [COMPAT]
    media = Path("/run/media")

    if media.exists():
        for entry in media.iterdir():
            roots.append(entry / "steamapps" / "compatdata")

            if entry.is_dir():
                for sub in entry.iterdir():
                    roots.append(sub / "steamapps" / "compatdata")

    return [r for r in roots if r.exists()]


def compat_root_for(prefix_id):
    for root in all_compat_roots():
        if (root / str(prefix_id)).exists():
            return root

    return COMPAT


def prefix_names():
    """{ id: name }  from your non-Steam shortcuts (shortcuts.vdf) and Steam's own game list (appmanifest files)."""
    names = {}

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import steamsetup

        for path in steamsetup.shortcut_files(None, "Linux"):
            root = steamsetup.parse_vdf(Path(path).read_bytes())
            _, shortcuts = steamsetup.field(root, "shortcuts")

            for entry in (shortcuts[2] if shortcuts else []):
                if entry[0] != 0:
                    continue

                _, appid = steamsetup.field(entry[2], "appid")
                name = steamsetup.get_text(entry[2], "AppName")

                if appid and name:
                    names[str(appid[2])] = name
    except Exception:
        pass

    for root in all_compat_roots():
        for manifest in root.parent.glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
                match = re.search(r'"name"\s+"([^"]+)"', text)
                names.setdefault(manifest.stem.split("_", 1)[1], match.group(1) if match else "")
            except OSError:
                continue

    return names


SKIP_FOLDER_NAMES = {"microsoft", "temp", "packages", "crashdumps", "d3dscache", "nvidia", "amd", "intel", "windows",
                     "application data", "my games", "unity", "unityplayer", "cache", "programs", "comms"}


def guess_prefix_games(prefix_path):
    """When no name is known: the folder names games created inside the prefix (often the game or studio name)."""
    base = Path(prefix_path) / "pfx" / "drive_c" / "users" / "steamuser"
    found = []

    for sub in ("AppData/Local", "AppData/Roaming", "AppData/LocalLow", "Documents", "Documents/My Games", "Saved Games"):
        try:
            for entry in (base / sub).iterdir():
                if entry.is_dir() and entry.name.lower() not in SKIP_FOLDER_NAMES and entry.name not in found:
                    found.append(entry.name)
        except OSError:
            continue

    return found[:3]


def prefix_label(prefix_id, names=None):
    """A readable name for a Proton ID: the real name when known, else a guess from its folders."""
    names = prefix_names() if names is None else names
    name = names.get(str(prefix_id))

    if name:
        return name

    guess = guess_prefix_games(compat_root_for(prefix_id) / str(prefix_id))
    return "maybe: " + ", ".join(guess) if guess else "(no name found)"


def prefix_list():
    """Proton prefixes (compatdata/<ID>) newest activity first, from every Steam library."""
    rows = []
    seen = set()

    for root in all_compat_roots():
        for folder in root.iterdir():
            if not folder.is_dir() or folder.name in seen:
                continue

            seen.add(folder.name)
            data = scan([folder / "pfx" / "drive_c" / "users"])
            newest = max((v[0] for p, v in data.items() if not is_noise(p)), default=0)
            rows.append((newest, folder.name))

    rows.sort(reverse=True)
    return rows


def choose_roots():
    """Menu: where should we look? Returns (label, [roots]) or None."""
    print()
    print("Where should I look for the game's files?")
    print("  1  Usual save places (config, local share, Documents...)  <- best for AppImages / recomps")

    if not IS_WINDOWS:
        print("  2  A Windows game running through Proton (pick from recently used)")
        print("  3  The microSD card")
    else:
        print("  3  Game install folders (Steam libraries, X:\\Games ...) - for saves kept next to the .exe")

    print("  4  A folder I type")
    print("  0  Back")
    choice = ask("Select: ").strip()

    if choice == "1":
        return "usual places", common_roots(), None

    if choice == "2" and not IS_WINDOWS:
        rows = prefix_list()[:10]

        if not rows:
            print("No Proton prefixes found.")
            return None

        print()
        print("Most recently used first (the game you just played is usually #1):")

        names = prefix_names()

        for number, (newest, name) in enumerate(rows, start=1):
            print(f"  {number}  ID {name}  {prefix_label(name, names)}   (last write {fmt(newest) if newest else '-'})")

        pick = ask("Select: ").strip()

        if pick.isdigit() and 1 <= int(pick) <= len(rows):
            name = rows[int(pick) - 1][1]
            return f"Proton prefix {name}", [compat_root_for(name) / name / "pfx" / "drive_c" / "users"], name

        return None

    if choice == "3" and not IS_WINDOWS:
        return "microSD card", sd_roots(), None

    if choice == "3" and IS_WINDOWS:
        roots = install_roots()

        if not roots:
            print("No install folders found - use option 4 and type the game's folder.")
            return None

        print("Will look in: " + "; ".join(str(r) for r in roots))
        print("(A snapshot of big libraries can take a minute or two.)")
        return "game install folders", roots, None

    if choice == "4":
        text = ask("Folder path: ").strip().strip('"')

        if text and Path(text).exists():
            return text, [Path(text)], None

        print("That folder doesn't exist.")

    return None


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------
def report(changes, match=None, show_all=False):
    rows = []

    for path, (modified, size) in changes.items():
        if not show_all and is_noise(path):
            continue

        if match and match.lower() not in path.lower():
            continue

        rows.append((path, modified, size))

    if not rows:
        print("\nNothing changed (or only background noise). Did the game actually save?")
        print("Tip: try menu option 'usual places' again, or look at the microSD / a Proton prefix.")
        return rows

    folders = {}

    for path, modified, size in rows:
        folders.setdefault(os.path.dirname(path), []).append((path, modified, size))

    ordered = sorted(folders.items(), key=lambda item: max(m for _, m, _ in item[1]), reverse=True)

    print()
    print(f"{len(rows)} file(s) changed in {len(folders)} folder(s). Newest folders first.")
    print("The save/settings folder is usually the one holding the .sav / profile files.")

    for folder, files in ordered[:8]:
        print()
        print(f"FOLDER: {folder}   ({len(files)} file(s))")

        for path, modified, size in sorted(files, key=lambda f: f[1], reverse=True)[:6]:
            print(f"    {os.path.basename(path):<40} {fmt(modified)}  {human(size)}")

        if len(files) > 6:
            print(f"    ...and {len(files) - 6} more")

    print()
    print("Choose menu 5 to match this to a game in Games.json and get a line to paste.")
    return rows


# ----------------------------------------------------------------------
# Matching a Windows path to a Steam Deck path
# ----------------------------------------------------------------------
SAVE_EXTS = (".sav", ".dat", ".save", ".sl2", ".bin", ".srm", ".mcd", ".mcr", ".sig", ".profile", ".xml", ".json")
GENERIC = {"savegames", "savegame", "saves", "save", "savedata", "saved", "profile", "profiles", "data", "config"}


def load_savemanager():
    """Re-uses savemanager.py (same folder) for Games.json, backup folders and fuzzy search."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import savemanager
        return savemanager
    except Exception as error:
        print(f"Could not load savemanager.py from the same folder ({error}).")
        return None


def squash(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def normalise(relative):
    """Lower case, and every run of digits becomes # (save0.sav and save3.sav look alike)."""
    return re.sub(r"\d+", "#", relative.lower())


def windows_to_proton(windows_path, proton_id):
    r"""
    C:\Users\<anyone>\X  ->  <compatdata>/<ID>/pfx/drive_c/users/steamuser/X
    C:\ProgramData\X     ->  .../drive_c/ProgramData/X       (other C:\ paths likewise)
    Other drive letters can't be converted (returns None).
    """
    text = windows_path.replace("/", "\\")
    base = compat_root_for(proton_id) / str(proton_id) / "pfx" / "drive_c"
    match = re.match(r"^[Cc]:\\Users\\[^\\]+\\?(.*)$", text)

    if match:
        return base / "users" / "steamuser" / Path(*match.group(1).split("\\")) if match.group(1) else base / "users" / "steamuser"

    match = re.match(r"^[Cc]:\\(.*)$", text)

    if match:
        return base / Path(*match.group(1).split("\\"))

    return None


def reference_files(sm, config, game, platform):
    """File names inside the game's existing backup (from the PC) - a fingerprint of its save folder."""
    settings = config.get("Settings", {})
    primary, fallback = sm.get_roots(settings, platform)

    for root in (primary, fallback):
        if root and sm.reachable(root):
            current = root / sm.safe_name(game["Name"]) / "Current"

            if current.exists():
                return list(sm.list_files(current, [], []).keys())

    return []


def build_candidates(sm, config, game, platform, rows, proton_id):
    candidates = {}

    def add(path, points, reason):
        entry = candidates.setdefault(str(path), {"score": 0, "reasons": []})
        entry["score"] += points

        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)

    windows_source = game.get("Source") or ""

    # A. Your Windows path, converted for the Proton prefix
    if proton_id and windows_source:
        projected = windows_to_proton(windows_source, proton_id)

        if projected is not None:
            if projected.exists():
                add(projected, 100, f"your Windows path converted for Proton ID {proton_id} - that folder exists")
            else:
                add(projected, 30, f"your Windows path converted for Proton ID {proton_id} - folder not created yet")

    # B. Which folder, when the game's changed files are laid out like the PC backup?
    refs = reference_files(sm, config, game, platform)

    if refs:
        ref_set = {normalise(r) for r in refs}
        found = {}

        for path, modified, size in rows:
            parts = Path(path).parts

            for k in range(1, len(parts)):
                relative = "/".join(parts[k:])

                if normalise(relative) in ref_set:
                    found.setdefault(Path(*parts[:k]), set()).add(relative)
                    break

        for root, relatives in found.items():
            add(root, 60 + 10 * len(relatives),
                f"{len(relatives)} changed file(s) have the same names/layout as your PC backup")

    # C. A folder whose name matches the game's folder name in the Windows path
    if windows_source:
        parts = list(PureWindowsPath(windows_source).parts)
        target_index = None

        for index in range(len(parts) - 1, 0, -1):
            if squash(parts[index]) not in GENERIC:
                target_index = index
                break

        if target_index is not None:
            target = squash(parts[target_index])
            tail = parts[target_index + 1:]
            seen = set()

            for path, modified, size in rows:
                for ancestor in Path(path).parents:
                    name = squash(ancestor.name)

                    if not name or ancestor in seen or ancestor == HOME:
                        continue

                    if name == target or (len(target) > 4 and
                                          __import__("difflib").SequenceMatcher(None, name, target).ratio() >= 0.85):
                        seen.add(ancestor)
                        candidate = ancestor.joinpath(*tail) if tail else ancestor
                        add(candidate if candidate.exists() else ancestor, 25,
                            f"folder name matches '{parts[target_index]}' from your Windows path")

    return sorted(candidates.items(), key=lambda item: item[1]["score"], reverse=True)


def copy_to_clipboard(text):
    for command in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        try:
            subprocess.run(command, input=text.encode("utf-8"), check=True, timeout=5)
            return True
        except Exception:
            continue

    return False


def confidence(score):
    if score >= 100:
        return "STRONG"

    if score >= 60:
        return "GOOD"

    return "WEAK"


def pick_prefix():
    rows = prefix_list()[:8]

    if not rows:
        return None

    print()
    print("Was this a Windows game running through Proton? Pick its ID (Enter = no):")

    names = prefix_names()

    for number, (newest, name) in enumerate(rows, start=1):
        print(f"  {number}  ID {name}  {prefix_label(name, names)}   (last write {fmt(newest) if newest else '-'})")

    pick = ask("Select: ").strip()

    if pick.isdigit() and 1 <= int(pick) <= len(rows):
        return rows[int(pick) - 1][1]

    return None


def action_suggest(rows=None, proton_id=None):
    """Matches what the game wrote to a game in Games.json and prints the line to paste."""
    sm = load_savemanager()

    if sm is None:
        return

    if rows is None:
        if not LAST_FILE.exists():
            print("\nRun a compare (menu 2) or a watch (menu 3) first.")
            return

        with open(LAST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        rows = [(p, v[0], v[1]) for p, v in data["changes"].items() if not is_noise(p)]
        proton_id = data.get("proton_id")

    config = sm.load_config()
    platform = sm.detect_platform()
    game = sm.choose_game(config["Games"])

    if game is None:
        return

    if proton_id is None and not IS_WINDOWS:
        proton_id = pick_prefix()

    print()
    print(f"Game: {game['Name']}")
    print(f"Windows save path in Games.json: {game.get('Source') or '(none)'}")

    if game.get("SourceSteamDeck"):
        print(f"SourceSteamDeck is already set to: {game['SourceSteamDeck']}")

    results = build_candidates(sm, config, game, platform, rows, proton_id)

    if not results:
        if not rows:
            print("\nNothing was written, so there is nothing to match. Save in the game and compare again.")
            return

        print("\nNo match from the Windows path (this game may have no Windows path yet) - here are the folders it wrote into instead.")
        folder = pick_folder(rank_folders(rows, game["Name"]))

        if folder is None:
            return

        key = "Source" if IS_WINDOWS else "SourceSteamDeck"
        value = sm.portable_path(folder, sm.detect_platform())
        backup = sm.set_game_field(game["Name"], key, value)
        print(f'\nSaved "{key}" for {game["Name"]}: {value}' + (f"  (old file kept as {backup.name})" if backup else ""))
        return

    print("\nBest guesses:")

    for number, (path, info) in enumerate(results[:3], start=1):
        print(f"\n  {number}. [{confidence(info['score'])}]  {path}")

        for reason in info["reasons"]:
            print(f"       - {reason}")

    best = results[0][0]
    key = "Source" if IS_WINDOWS else "SourceSteamDeck"
    line = f'            "{key}": {json.dumps(best, ensure_ascii=False)},'
    print()
    print("=" * 60)
    print(f' Paste this into Games.json inside the "{game["Name"]}" block')
    print(' (for example on a new line right under its "Source" line):')
    print("=" * 60)
    print()
    print(line)
    print()

    if copy_to_clipboard(line):
        print("(Also copied to the clipboard - just paste it.)")

    print()

    if ask(f'Add this to Games.json for "{game["Name"]}" for you now? [Y/n]: ').strip().lower() != "n":
        value = sm.portable_path(best, sm.detect_platform())
        backup = sm.set_game_field(game["Name"], key, value)

        if backup:
            print(f'Done - "{key}" saved for {game["Name"]}. (Old file kept as {backup.name}.)')
        else:
            print("Not saved (the game was not found in Games.json, or you said no).")

    script = Path(sm.__file__).resolve()
    print()
    print("=" * 60)
    print(f' And the Steam launch option for "{game["Name"]}"')
    print(" (Steam > the game > Properties > Launch Options):")
    print("=" * 60)
    print()
    if IS_WINDOWS:
        print("Windows Steam often does not expand %command% for non-Steam shortcuts, so use:")
        print(f'  Target:          "{sys.executable}"')
        print(f'  Launch Options:  "{script}" launch "{game["Name"]}" -- "FULL PATH TO THE GAME .EXE"')
        print("(use the shortcut's old Target as the .exe path)")
    else:
        print(f'"{sys.executable}" "{script}" launch "{game["Name"]}" -- %command%')
        print()
        print("If the shortcut already has launch options (EmuDeck ones, for example), keep them")
        print("AFTER %command%, e.g.  ... -- %command% <old options>")

    if len(results) > 1 and results[1][1]["score"] >= 60:
        print()
        print("Note: more than one folder matched. The game may keep saves and settings in")
        print("different folders. A Source can only be ONE folder, so choose the one holding the saves.")


def rank_folders(rows, name):
    """[(score, folder, [(path, modified)])] for the folders the game wrote into, best guess first."""
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 2]
    folders = {}

    for path, modified, size in rows:
        folders.setdefault(os.path.dirname(path), []).append((path, modified))

    scored = []

    for folder, files in folders.items():
        score = len(files)
        score += 5 * sum(1 for w in words if w in folder.lower())
        score += 3 * sum(1 for p, m in files if p.lower().endswith(SAVE_EXTS))
        scored.append((score, folder, files))

    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


def pick_folder(scored):
    """Shows the folders, lets you choose one (and go 'up' a level). Returns the folder or None."""
    print()
    print("Folders the game wrote into (best guess first):")

    for number, (score, folder, files) in enumerate(scored[:6], start=1):
        newest = max(m for p, m in files)
        print(f"  {number}  {folder}   ({len(files)} file(s), {fmt(newest)})" + ("   <- suggested" if number == 1 else ""))

    pick = ask("Which folder holds the SAVES? (number, Enter = 1, 0 = cancel): ").strip()

    if pick == "0":
        return None

    index = int(pick) - 1 if pick.isdigit() and 1 <= int(pick) <= min(6, len(scored)) else 0
    folder = scored[index][1]

    while True:
        print(f"\nChosen folder: {folder}")
        answer = ask("Press Enter to use it, or type U to use the folder ABOVE it (to include sub folders): ").strip().lower()

        if answer == "u" and os.path.dirname(folder) not in ("", folder):
            folder = os.path.dirname(folder)
        else:
            return folder


def add_game_wizard():
    """
    Add a NEW game (or add this device's folder to an existing one): you play the game once, and the
    folder it saved into is written to Games.json for you.
    """
    sm = load_savemanager()

    if sm is None:
        return

    platform = sm.detect_platform()
    name = ask("Name of the game (how it will be listed): ").strip()

    if not name:
        return

    chosen = choose_roots()

    if chosen is None:
        return

    label, roots, proton_id = chosen
    print(f"\nTaking a 'before' snapshot of: {label} ... (can take a minute)")
    before = scan(roots)
    print()
    print("NOW: start the game, make a save (change a setting too), then QUIT the game.")
    ask("Press Enter here when you have saved and closed the game... ")
    print("\nLooking at what the game wrote ...")
    changes = changed_between(before, scan(roots))
    rows = [(p, v[0], v[1]) for p, v in changes.items() if not is_noise(p)]

    if not rows:
        print("Nothing new was written. Did the game really save? Try again (or use 'I know the save folder').")
        return

    folder = pick_folder(rank_folders(rows, name))

    if folder is None:
        return

    process = ask("Program name of the game, e.g. Duskfade-Win64-Shipping (Enter = skip): ").strip()
    key = sm.source_key(platform)
    value = sm.portable_path(folder, platform)

    def change(data):
        game = sm.find_raw_game(data, name)

        if game is not None:
            game[key] = value

            if process:
                game.setdefault("Process", process)
        else:
            entry = {"Name": name, key: value}

            if process:
                entry["Process"] = process

            data["Games"].append(entry)

    backup = sm.edit_games_json(change)

    if not backup:
        print("Nothing was saved.")
        return

    script = Path(sm.__file__).resolve()
    print()
    print(f'Saved "{name}" in Games.json ({key}). The old file is kept as {backup.name}.')
    print("Start the manager again to see it. For automatic backups, put this in the game's Steam Launch Options:")

    if IS_WINDOWS:
        print(f'  Target: "{sys.executable}"   Launch Options: "{script}" launch "{name}" -- "FULL PATH TO THE GAME .EXE"')
    else:
        print(f'  "{sys.executable}" "{script}" launch "{name}" -- %command%')


# ----------------------------------------------------------------------
# Menu actions
# ----------------------------------------------------------------------
def action_snapshot():
    chosen = choose_roots()

    if chosen is None:
        return

    label, roots, proton_id = chosen
    print(f"\nTaking a snapshot of: {label} ... (can take a minute)")
    data = scan(roots)

    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump({"label": label, "roots": [str(r) for r in roots], "files": data,
                   "proton_id": proton_id}, f)

    print(f"Done: {len(data)} files noted.")
    print()
    print("NOW: switch to the game, make a save (and change a setting), then close the game.")
    print("Then come back here and choose  2  to compare.")


def action_compare():
    if not SNAPSHOT_FILE.exists():
        print("\nNo snapshot yet - choose 1 first.")
        return

    with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    print(f"\nComparing with the snapshot of: {snapshot['label']} ... (can take a minute)")
    after = scan(snapshot["roots"])
    finish(changed_between(snapshot["files"], after), snapshot.get("proton_id"))


def action_timed(seconds=None):
    chosen = choose_roots()

    if chosen is None:
        return

    label, roots, proton_id = chosen
    text = ask("Watch for how many seconds? [90]: ").strip()
    seconds = int(text) if text.isdigit() else 90
    before = scan(roots)
    print(f"\nWatching {label} for {seconds} seconds.")
    print("NOW: switch to the game and make a save. I will list what changed when the time is up.")

    for remaining in range(seconds, 0, -10):
        time.sleep(min(10, remaining))
        print(f"  ...{max(0, remaining - 10)} seconds left")

    finish(changed_between(before, scan(roots)), proton_id)


def finish(changes, proton_id):
    """Shows what changed, remembers it, and offers to match it to a game."""
    rows = report(changes)

    with open(LAST_FILE, "w", encoding="utf-8") as f:
        json.dump({"changes": changes, "proton_id": proton_id}, f)

    if rows and ask("\nMatch these to a game in Games.json and make the line to paste? [Y/n]: ").strip().lower() != "n":
        action_suggest(rows, proton_id)


def action_prefixes():
    rows = prefix_list()

    if not rows:
        print("\nNo Proton prefixes found (this is normal for native Linux games and AppImages).")
        return

    print("\nMost recently written Proton prefixes (the game you just played is usually first):")

    names = prefix_names()

    for newest, name in rows[:15]:
        print(f"  {name:<12} {prefix_label(name, names):<44} last write {fmt(newest) if newest else '-'}")


def menu():
    while True:
        print()
        print("=" * 52)
        print(" Find Saves - where does this game keep its files?")
        print("=" * 52)
        print(" 1  Take a BEFORE snapshot (then go save in the game)")
        print(" 2  Compare: what did the game write?")
        print(" 3  Watch for N seconds (does the same in one go)")

        if not IS_WINDOWS:
            print(" 4  List Proton game IDs by recent use")

        print(" 5  Match the last compare to a game in Games.json (make the line to paste)")
        print(" 6  ADD A NEW GAME: play it once and write its folder into Games.json")

        print(" Q  Quit")
        choice = ask("\nSelect: ").strip().lower()

        if choice in ("q", ""):
            return

        if choice == "1":
            action_snapshot()
        elif choice == "2":
            action_compare()
        elif choice == "3":
            action_timed()
        elif choice == "4" and not IS_WINDOWS:
            action_prefixes()
        elif choice == "5":
            action_suggest()
        elif choice == "6":
            add_game_wizard()

        ask("\nPress Enter to continue...")


# ----------------------------------------------------------------------
# Command line (old style, still works)
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Find where a game writes its saves")
    parser.add_argument("path", nargs="?", help="folder to search")
    parser.add_argument("--id", help="Proton prefix ID (searches that prefix's fake C: drive)")
    parser.add_argument("--prefixes", action="store_true", help="list Proton prefixes by recent activity")
    parser.add_argument("--minutes", type=int, default=5, help="look back this many minutes")
    parser.add_argument("--match", help="only paths containing this text, e.g. sav")
    parser.add_argument("--watch", action="store_true", help="keep running and print files as they are written")
    parser.add_argument("--all", action="store_true", help="don't hide noise")
    args = parser.parse_args()

    if not (args.path or args.id or args.prefixes):
        try:
            menu()
        except KeyboardInterrupt:
            print("\nCancelled.")

        return

    if args.prefixes:
        action_prefixes()
        return

    if args.id:
        root = compat_root_for(args.id) / args.id / "pfx" / "drive_c" / "users" / "steamuser"
    else:
        root = Path(args.path)

    if not root.exists():
        print(f"Folder not found: {root}")
        sys.exit(1)

    if args.watch:
        print(f"Watching {root}  (Ctrl+C to stop). Save in the game now...")
        before = scan([root])

        try:
            while True:
                time.sleep(2)
                now = scan([root])

                for path, value in changed_between(before, now).items():
                    if (args.all or not is_noise(path)) and (not args.match or args.match.lower() in path.lower()):
                        print(f"{fmt(value[0])}  {os.path.relpath(path, root)}")

                before = now
        except KeyboardInterrupt:
            print("\nStopped.")

        return

    cutoff = time.time() - args.minutes * 60
    recent = {p: v for p, v in scan([root]).items() if v[0] >= cutoff}
    report(recent, args.match, args.all)


if __name__ == "__main__":
    main()

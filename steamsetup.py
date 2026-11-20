#!/usr/bin/env python3
"""
steamsetup.py - one-step Steam setup for non-Steam shortcuts (Game Save Manager v14)
===================================================================================
Menu: O  |  python savemanager.py steamsetup [--yes]
Finds your Steam "non-Steam game" shortcuts, matches them to games in Games.json (by name), and puts the
right launch option into each one, so every game does the cloud-style check before it starts and backs up
while you play and when you close it. Existing launch options (EmuDeck, mangohud ...) are kept.

  * Steam has to be closed while the file is changed (it rewrites it when it exits). On the Steam Deck in
    DESKTOP mode the tool does this for you: it asks once (while Steam is still open and you can type),
    closes Steam, makes the change and starts Steam again. In gaming mode it refuses (Steam IS the screen).
  * Emulator shortcuts for Switch games are matched by the title ID in the rom's file name too.
  * A copy of shortcuts.vdf is saved next to it before anything is changed.
  * Steam games you installed from the store are not changed; use menu L for those (it prints the line).
"""

import difflib
import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------- Valve's binary key/value format
def read_cstr(data, pos):
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("utf-8", "surrogateescape"), end + 1


def parse_dict(data, pos):
    """[(type, key, value)] in file order. type 0 = nested dict, 1 = string, 2 = int32, 3 = float, 7 = uint64."""
    items = []

    while True:
        kind = data[pos]
        pos += 1

        if kind == 0x08:
            return items, pos

        key, pos = read_cstr(data, pos)

        if kind == 0x00:
            value, pos = parse_dict(data, pos)
        elif kind == 0x01:
            value, pos = read_cstr(data, pos)
        elif kind == 0x02:
            value = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        elif kind == 0x03:
            value = struct.unpack_from("<f", data, pos)[0]
            pos += 4
        elif kind == 0x07:
            value = struct.unpack_from("<Q", data, pos)[0]
            pos += 8
        else:
            raise ValueError(f"unknown field type {kind} at byte {pos - 1}")

        items.append((kind, key, value))


def dump_dict(items):
    out = bytearray()

    for kind, key, value in items:
        out.append(kind)
        out += key.encode("utf-8", "surrogateescape") + b"\x00"

        if kind == 0x00:
            out += dump_dict(value)
        elif kind == 0x01:
            out += value.encode("utf-8", "surrogateescape") + b"\x00"
        elif kind == 0x02:
            out += struct.pack("<I", value)
        elif kind == 0x03:
            out += struct.pack("<f", value)
        elif kind == 0x07:
            out += struct.pack("<Q", value)

    out.append(0x08)
    return bytes(out)


def parse_vdf(data):
    items, pos = parse_dict(data, 0)

    if pos != len(data):
        raise ValueError("unexpected data after the end of the file")

    return items


def dump_vdf(items):
    return dump_dict(items)


def field(entries, name):
    """(index, (type, key, value)) of a field, matching the key without caring about capitals."""
    for index, item in enumerate(entries):
        if item[1].lower() == name.lower():
            return index, item

    return None, None


def get_text(entries, name):
    _, item = field(entries, name)
    return item[2] if item and item[0] == 0x01 else ""


def set_text(entries, name, value):
    index, item = field(entries, name)

    if item:
        entries[index] = (0x01, item[1], value)
    else:
        entries.append((0x01, name, value))


# ---------------------------------------------------------------- finding things
def shortcut_files(sm, platform):
    found = []

    if platform == "Windows":
        root = sm.steam_root({}, platform)
        found += glob.glob(str(root / "userdata" / "*" / "config" / "shortcuts.vdf"))
    else:
        home = Path.home()

        for base in (home / ".local" / "share" / "Steam", home / ".steam" / "steam",
                     home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"):
            found += glob.glob(str(base / "userdata" / "*" / "config" / "shortcuts.vdf"))

    unique = []

    for path in found:
        real = os.path.realpath(path)

        if real not in unique:
            unique.append(real)

    return unique


def in_gaming_mode(platform):
    """Linux: gaming mode runs Steam with -gamepadui. Closing Steam there would end the session."""
    if platform == "Windows":
        return False

    try:
        out = subprocess.run(["pgrep", "-af", "steam"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False

    return "-gamepadui" in out


def close_steam(sm, platform, wait_seconds=45):
    """Asks Steam to exit and waits until it has. Returns True when it is gone."""
    import time

    try:
        if platform == "Windows":
            exe = sm.steam_root({}, platform) / "steam.exe"
            subprocess.run([str(exe), "-shutdown"], timeout=20)
        else:
            subprocess.run([shutil.which("steam") or "steam", "-shutdown"], timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass

    for _ in range(wait_seconds):
        if not steam_running(platform):
            time.sleep(2)
            return True

        time.sleep(1)

    return False


def start_steam(sm, platform):
    try:
        if platform == "Windows":
            subprocess.Popen([str(sm.steam_root({}, platform) / "steam.exe")], close_fds=True)
        else:
            subprocess.Popen([shutil.which("steam") or "steam"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)

        return True
    except OSError:
        return False


def steam_running(platform):
    try:
        if platform == "Windows":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"], capture_output=True, text=True, timeout=10).stdout
            return "steam.exe" in out.lower()

        return subprocess.run(["pgrep", "-x", "steam"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


# ---------------------------------------------------------------- names: cleaning and scoring
TAG_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
STOP_WORDS = {"the", "a", "an", "of", "and"}
NOISE_WORDS = {"recomp", "recompiled", "recompilation"}
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "xi": "11",
         "xii": "12", "xiii": "13", "xiv": "14", "xv": "15", "xvi": "16"}
ASK_SCORE = 0.70          # below this a game is not even suggested
AUTO_SCORE = 1.0          # only an identical set of words is matched without asking


def name_tokens(name):
    """
    'Mortal Shell II' -> ['mortal', 'shell', '2'].  Tags like (USA) (Disc 2) [0100...] are dropped, so are
    the, a, of, 'recomp', 'zEmu_' / 'yRecomp_' prefixes; apostrophes vanish; Roman numerals become numbers.
    """
    text = str(name).lower()
    text = re.sub(r"^(zemu_|yrecomp_|zrecomp_)", "", text)
    text = TAG_RE.sub(" ", text)
    text = text.replace("'", "").replace("\u2019", "")
    tokens = []

    for word in re.findall(r"[a-z0-9]+", text):
        if word in STOP_WORDS or word in NOISE_WORDS:
            continue

        word = ROMAN.get(word, word)

        if word.isdigit():
            word = str(int(word))

        tokens.append(word)

    return tokens


def similarity(tokens_a, tokens_b):
    """0..1 : 1.0 = the same words (any order). Otherwise the best of: shared words, one name inside the other, spelling."""
    set_a = set(tokens_a)
    set_b = set(tokens_b)

    if not set_a or not set_b:
        return 0.0

    if set_a == set_b:
        return 1.0

    shared = len(set_a & set_b)
    jaccard = shared / len(set_a | set_b)
    smaller = min(len(set_a), len(set_b))
    inside = 0.85 * shared / smaller if smaller >= 2 else 0.0
    spelling = 0.9 * difflib.SequenceMatcher(None, "".join(tokens_a), "".join(tokens_b)).ratio()
    return max(jaccard, inside, spelling)


def score_game(tokens, game):
    """Compares with the full name and with the part after ' - ' ('Switch - X', 'Rainbow Six - Vegas 1')."""
    name = game["Name"]
    best = similarity(tokens, name_tokens(name))

    if " - " in name:
        best = max(best, similarity(tokens, name_tokens(name.split(" - ", 1)[1])))

    return best


def find_by_name(games, name):
    wanted = str(name).lower()
    return next((g for g in games if g["Name"].lower() == wanted), None)


# ---------------------------------------------------------------- systems and emulators
EMULATORS = ["dolphin", "duckstation", "pcsx2", "rpcs3", "ppsspp", "ryujinx", "yuzu", "eden", "citron", "suyu",
             "azahar", "citra", "shadps4", "retroarch", "melonds", "mgba", "cemu", "xemu", "flycast", "mame", "scummvm"]
SWITCH_EMULATORS = {"ryujinx", "yuzu", "eden", "citron", "suyu"}
# folder names that mean a system (spaces and punctuation are ignored, capitals do not matter)
SYSTEM_ALIASES = {
    "gamecube": "gc", "gc": "gc", "ngc": "gc", "nintendogamecube": "gc",
    "wii": "wii", "nintendowii": "wii",
    "ps1": "ps1", "psx": "ps1", "psone": "ps1", "playstation": "ps1", "playstation1": "ps1", "sonyplaystation": "ps1",
    "ps2": "ps2", "playstation2": "ps2", "sonyplaystation2": "ps2",
    "ps3": "ps3", "playstation3": "ps3",
    "psp": "psp", "playstationportable": "psp",
    "ps4": "ps4", "playstation4": "ps4",
    "switch": "switch", "nintendoswitch": "switch",
    "3ds": "3ds", "nintendo3ds": "3ds", "n3ds": "3ds",
    "nds": "nds", "ds": "nds", "nintendods": "nds",
    "gba": "gba", "gameboyadvance": "gba", "gb": "gb", "gameboy": "gb", "gbc": "gbc", "gameboycolor": "gbc",
    "snes": "snes", "supernintendo": "snes", "sfc": "snes", "nes": "nes", "famicom": "nes",
    "n64": "n64", "nintendo64": "n64",
    "megadrive": "genesis", "genesis": "genesis", "segagenesis": "genesis", "mastersystem": "mastersystem",
    "dreamcast": "dreamcast", "saturn": "saturn",
}
SYSTEM_EMULATORS = {"gc": ["dolphin"], "wii": ["dolphin"], "ps1": ["duckstation"], "ps2": ["pcsx2"], "ps3": ["rpcs3"],
                    "psp": ["ppsspp"], "ps4": ["shadps4"], "switch": sorted(SWITCH_EMULATORS), "3ds": ["azahar", "citra"],
                    "nds": ["retroarch", "melonds"], "gba": ["retroarch", "mgba"], "gb": ["retroarch"], "gbc": ["retroarch"],
                    "snes": ["retroarch"], "nes": ["retroarch"], "n64": ["retroarch"], "genesis": ["retroarch"],
                    "mastersystem": ["retroarch"], "dreamcast": ["flycast", "retroarch"], "saturn": ["retroarch"]}
# words an entry name usually holds for that system (to tell 'zEmu_GC - Dolphin' from 'zEmu_Wii - Dolphin')
SYSTEM_NAME_WORDS = {"gc": ["gc", "gamecube"], "wii": ["wii"], "ps1": ["ps1", "psx"], "ps2": ["ps2"], "ps3": ["ps3"],
                     "psp": ["psp"], "ps4": ["ps4"], "3ds": ["3ds"]}
# only for roms that sit in no system folder: file types that name ONE system (.iso / .chd / .pbp do not)
EXTENSION_SYSTEMS = {"wbfs": "wii", "wad": "wii", "nsp": "switch", "xci": "switch", "3ds": "3ds", "cia": "3ds", "cci": "3ds",
                     "sfc": "snes", "smc": "snes", "nes": "nes", "gba": "gba", "gbc": "gbc", "z64": "n64", "n64": "n64",
                     "v64": "n64", "nds": "nds", "gcm": "gc"}
ROM_EXT = r"\.(iso|gcm|rvz|wbfs|wad|nsp|xci|cue|chd|img|pbp|cso|3ds|cia|cci|sfc|smc|nes|gba|gb|gbc|z64|n64|v64|nds|md|gen|sms|gg)(?=$|[\s\"'\])])"


def system_from_path(text):
    """The system named by the folder CLOSEST to the file: E:\\Gamecube\\Sonic Heroes.iso -> 'gc'."""
    found = None

    for part in str(text).lower().replace("\\", "/").split("/"):
        system = SYSTEM_ALIASES.get(re.sub(r"[^a-z0-9]", "", part))

        if system:
            found = system

    return found


def classify(exe, options):
    """
    Is this shortcut an EMULATOR playing a rom, or a native program (a recomp, a PC game)?
    Which emulator, and which system (from the rom's folder name, else its file type)?
    The system is only read from the launch OPTIONS (the rom), never from the program's own folder.
    """
    text = f"{exe} {options}".lower().replace("\\", "/")
    emulator = next((k for k in EMULATORS if re.search(rf"(?<![a-z]){k}(?![a-z])", text)), None)
    system = system_from_path(options)
    rom = re.search(ROM_EXT, options.lower())

    if system is None and rom and rom.group(1) in EXTENSION_SYSTEMS:
        system = EXTENSION_SYSTEMS[rom.group(1)]

    is_emulator = bool(emulator or system or rom or "/emulation/" in text or "/launchers/" in text)

    if emulator is None and is_emulator is False:
        system = None

    keys = [emulator] if emulator else SYSTEM_EMULATORS.get(system or "", [])
    switch = emulator in SWITCH_EMULATORS or system == "switch"
    return {"kind": "emulator" if is_emulator else "native", "emulator": emulator, "system": system, "keys": keys, "switch": switch}


def disc_number(options):
    """'(Disc 2)' / '(Disk 2 of 3)' / '(CD2)' in the rom's file name -> 2, else None."""
    found = re.search(r"\((?:disc|disk|cd)\s*(\d+)(?:\s*of\s*\d+)?[^)]*\)", options, re.I)
    return int(found.group(1)) if found else None


def recomp_like(game):
    return game.get("Platform") == "Recomp" or "recomp" in game["Name"].lower()


def emulator_entry(game):
    return game.get("Platform") == "Emulators" and not game.get("Virtual")


def entry_text(game):
    return (game["Name"] + " " + " ".join(str(v) for v in game.values() if isinstance(v, str))).lower()


def candidate_pool(info, games):
    """Entries a shortcut can match BY NAME: per-game emulator entries for emulators, normal entries for native programs."""
    if info["kind"] == "native":
        return [g for g in games if not emulator_entry(g) and not g.get("Virtual")]

    pool = []

    for game in games:
        if not game.get("Virtual"):
            continue

        if game.get("SwitchTitleID") and info["switch"]:
            pool.append(game)
        elif (game.get("PerGameLabel") or game.get("PerFileLabel")) and any(
                k in str(game.get("PerGameLabel") or game.get("PerFileLabel")).lower() for k in info["keys"]):
            pool.append(game)

    return pool


def emulator_candidates(info, games):
    """Whole-emulator entries (zEmu_GC - Dolphin ...) that fit this emulator, narrowed by the system's name words."""
    found = [g for g in games if emulator_entry(g)
             and any(re.search(rf"(?<![a-z]){k}(?![a-z])", entry_text(g)) for k in info["keys"])]
    found.sort(key=lambda g: (not g.get("Locations"), g["Name"]))
    words = SYSTEM_NAME_WORDS.get(info["system"] or "", [])
    narrowed = [g for g in found if any(w in g["Name"].lower() for w in words)]
    return narrowed if words and narrowed else found


# ---------------------------------------------------------------- what the program remembers
def matches_path(sm):
    return Path(sm.__file__).resolve().parent / "SteamMatches.json"


def load_matches(sm):
    """SteamMatches.json: answers you gave before. {'names': {shortcut key: game}, 'groups': {system or emulator: entry}}"""
    try:
        with open(matches_path(sm), "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}

    data.setdefault("names", {})
    data.setdefault("groups", {})
    return data


def save_matches(sm, data):
    path = matches_path(sm)
    temp = path.with_name(path.name + ".tmp")

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

    os.replace(temp, path)


def decide(name, exe, options, games, detect, settings, memory, session):
    """
    Which Games.json entry belongs to this shortcut?  Returns a dict:
      game, reason, mode, choices, info, key, group
    mode:  auto      a match was found (identical words, a remembered answer, a default, or the only fit)
           similar   a few games look alike: ask  (choices holds them, best first, with scores)
           emulator  several entries of the emulator fit: ask
           none      nothing found
    """
    info = classify(exe, options)
    tokens = name_tokens(name)
    key = " ".join(tokens) + "|" + info["kind"]
    group = info["system"] or info["emulator"]
    result = {"game": None, "reason": "no matching game in Games.json", "mode": "none", "choices": [], "info": info,
              "key": key, "group": group, "scores": {}}

    def done(game, reason):
        result.update(game=game, reason=reason, mode="auto")
        return result

    by_title = detect(exe + " " + options, games) if detect else None

    if by_title is not None:
        return done(by_title, "Switch title ID in the rom name")

    remembered = find_by_name(games, memory["names"].get(key, ""))

    if remembered is not None:
        return done(remembered, "your earlier answer")

    scored = sorted(((score_game(tokens, g), g) for g in candidate_pool(info, games)), key=lambda item: -item[0])
    top = [(score, g) for score, g in scored if score >= ASK_SCORE][:3]
    exact = [g for score, g in top if score >= AUTO_SCORE]

    if len(exact) == 1:
        return done(exact[0], "same name" + (" (a recomp)" if recomp_like(exact[0]) else ""))

    if top:
        result.update(mode="similar", choices=[g for _, g in top], scores={g["Name"]: score for score, g in top},
                      reason="similar names, please confirm")
        return result

    if info["kind"] == "emulator" and info["keys"]:
        choices = emulator_candidates(info, games)
        label = info["emulator"] or info["system"] or "emulator"

        if len(choices) == 1:
            return done(choices[0], f"the only {label} entry")

        if len(choices) > 1:
            sources = (("your choice for all", session), ("your saved choice", memory["groups"]),
                       ("SteamSetupDefaults", {str(k).lower(): v for k, v in settings.get("SteamSetupDefaults", {}).items()}))

            for why, mapping in sources:
                for candidate_key in (info["system"], info["emulator"]):
                    wanted = mapping.get(candidate_key) if candidate_key else None
                    game = find_by_name(games, wanted) if wanted else None

                    if game is not None:
                        return done(game, f"{why} ({candidate_key})")

            result.update(mode="emulator", choices=choices, reason=f"{label}: more than one entry fits")
            return result

    return result


def build_options(platform, python, script, game_name, exe, existing_options):
    """
    Linux / Deck:  "<python>" "<script>" launch "<Game>" -- %command% <old options>
    Windows:       Steam often ignores %command% for non-Steam shortcuts, so the shortcut starts Python and the
                   original program becomes the last argument (returns (new exe, new options)).
    """
    if platform == "Windows":
        original = exe.strip().strip('"')
        old = existing_options.strip()

        if "savemanager.py" in old and " launch " in old:
            tail = re.search(r'--\s+(.*)$', old)
            old_command = tail.group(1) if tail else f'"{original}"'
        else:
            old_command = f'"{original}"' + (f" {old}" if old else "")

        return f'"{python}"', f'"{script}" launch "{game_name}" -- {old_command}'

    old = existing_options.strip()

    if "savemanager.py" in old and " launch " in old:
        tail = re.search(r'--\s+(.*)$', old)
        rest = tail.group(1) if tail else "%command%"
    elif "%command%" in old:
        rest = old
    else:
        rest = ("%command% " + old).strip()

    return exe, f'"{python}" "{script}" launch "{game_name}" -- {rest}'


def already_done(platform, exe, options):
    return "savemanager.py" in options and " launch " in options


# What the manager puts into LaunchOptions:  "<script>" launch "<Game>" -- <the real command>
MANAGER_RE = re.compile(r'^\s*"?.*?savemanager\.py"?\s+launch\s+(?:--force-nas\s+)?"[^"]*"\s+--\s*(.*)$', re.S)


def strip_manager(exe, options):
    """(exe, options) as they were BEFORE the manager touched them, or None if this shortcut has no manager line."""
    found = MANAGER_RE.match(options)

    if not found:
        return None

    tail = found.group(1).strip()

    if tail.lower().startswith("%command%"):                      # Linux / Deck style
        return exe, tail[len("%command%"):].strip()

    parts = re.match(r'^"([^"]*)"\s*(.*)$', tail) or re.match(r'^(\S+)\s*(.*)$', tail)   # Windows style

    if not parts:
        return None

    return f'"{parts.group(1)}"', parts.group(2).strip()


def managed_count(path):
    """How many shortcuts in this file have a manager line (-1 = file could not be read)."""
    try:
        root = parse_vdf(Path(path).read_bytes())
    except (OSError, ValueError, IndexError):
        return -1

    _, shortcuts = field(root, "shortcuts")
    return sum(1 for e in (shortcuts[2] if shortcuts else [])
               if e[0] == 0x00 and strip_manager(get_text(e[2], "Exe"), get_text(e[2], "LaunchOptions")))


def state_word(count):
    return "clean" if count == 0 else (f"{count}-with-manager-lines" if count > 0 else "unreadable")


def backup_shortcuts(sm, path, label):
    """
    Copies shortcuts.vdf into Backups/Steam shortcuts/ with a name that says what it holds:
      shortcuts_85799292_2026-11-05_21-14-02_before-setup_clean.vdf              (no manager lines in it)
      shortcuts_85799292_2026-11-05_21-14-02_before-reset_6-with-manager-lines.vdf
    """
    user = Path(path).parent.parent.name              # the number folder inside userdata
    sm.BACKUP_STEAM.mkdir(parents=True, exist_ok=True)
    target = sm.BACKUP_STEAM / f"shortcuts_{user}_{datetime.now():%Y-%m-%d_%H-%M-%S}_{label}_{state_word(managed_count(path))}.vdf"
    shutil.copy2(path, target)
    return target


def import_legacy_backups(sm, paths):
    """Copies the old shortcuts.vdf.bak-<date> files into Backups/Steam shortcuts with readable names. The originals stay."""
    sm.BACKUP_STEAM.mkdir(parents=True, exist_ok=True)

    for path in paths:
        user = Path(path).parent.parent.name

        for old in sorted(glob.glob(path + ".bak-*")):
            stamp_text = old.rsplit(".bak-", 1)[1]

            try:
                stamp = datetime.strptime(stamp_text, "%Y%m%d-%H%M%S").strftime("%Y-%m-%d_%H-%M-%S")
            except ValueError:
                stamp = stamp_text

            target = sm.BACKUP_STEAM / f"shortcuts_{user}_{stamp}_older-copy_{state_word(managed_count(old))}.vdf"

            if not target.exists():
                shutil.copy2(old, target)


def undo_setup(sm, platform, paths, yes):
    """Puts a shortcuts.vdf.bak-... copy back (closing and restarting Steam like the setup does)."""
    todo = []

    for path in paths:
        user = Path(path).parent.parent.name
        found = glob.glob(path + ".bak-*") + glob.glob(str(sm.BACKUP_STEAM / f"shortcuts_{user}_*.vdf"))
        backups = sorted(found, key=os.path.getmtime)

        if not backups:
            continue

        choice = next((b for b in backups if b.endswith("_clean.vdf")), backups[0])   # prefer one with no manager lines

        if len(backups) > 1 and not yes:
            print(f"\n{path}\nThese backup copies exist (newest last):")

            for number, backup in enumerate(backups, start=1):
                print(f"  {number}  {os.path.basename(backup)}")

            pick = sm.ask("Which one to put back? (Enter = the oldest clean one): ").strip()
            choice = backups[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(backups) else choice

        todo.append((path, choice))

    if not todo:
        print("\nNo backup copies (shortcuts.vdf.bak-...) were found, so there is nothing to undo.")
        print("They are saved in the same folder as shortcuts.vdf:")
        print("  Windows: Steam folder > userdata > <your number> > config")
        return 1

    for path, backup in todo:
        print(f"\n  {path}\n  <- {backup}")

    was_running = steam_running(platform)

    if was_running and in_gaming_mode(platform):
        print("\nYou are in GAMING mode. Switch to Desktop mode first.")
        return 1

    if not yes and sm.ask("\nPut these back? [y/N]: ").strip().lower() != "y":
        return 1

    closed = False

    try:
        if was_running:
            closed = True

            if not close_steam(sm, platform):
                print("Steam did not close in time - nothing was changed.")
                return 1

        for path, backup in todo:
            backup_shortcuts(sm, path, "before-undo")
            shutil.copy2(backup, path)

        print("\nRestored.")
    finally:
        if closed:
            start_steam(sm, platform)
            print("Steam is starting again.")

    return 0


SCRIPT_RE = re.compile(r'"?([^"\s][^"]*savemanager\.py)"?\s+launch\b')
NAME_RE = re.compile(r'launch\s+(--force-nas\s+)?"([^"]*)"')


def managed_scripts(path):
    """[(shortcut name, path of the savemanager.py it starts)] for every shortcut that has a manager line."""
    try:
        root = parse_vdf(Path(path).read_bytes())
    except (OSError, ValueError, IndexError):
        return []

    _, shortcuts = field(root, "shortcuts")
    found = []

    for entry in (shortcuts[2] if shortcuts else []):
        if entry[0] != 0x00:
            continue

        options = get_text(entry[2], "LaunchOptions")
        match = SCRIPT_RE.search(options)

        if match and strip_manager(get_text(entry[2], "Exe"), options):
            found.append((get_text(entry[2], "AppName"), match.group(1)))

    return found


def with_steam_closed(sm, platform, work):
    """Runs work() while Steam is closed (desktop mode only) and always starts Steam again afterwards."""
    was_running = steam_running(platform)

    if was_running and in_gaming_mode(platform):
        print("\nYou are in GAMING mode. Switch to Desktop mode first.")
        return 1

    closed = False

    try:
        if was_running:
            print("\nClosing Steam ...")
            closed = True

            if not close_steam(sm, platform):
                print("Steam did not close in time - nothing was changed.")
                return 1

        return work()
    finally:
        if closed:
            print("\nStarting Steam again ...")
            start_steam(sm, platform)


def relink_setup(sm, platform, paths, yes):
    """
    Points every manager launch line at THIS copy of the program (use it after moving the program folder,
    or when shortcuts still point at an old folder). Game names and the real commands stay as they are.
    """
    python = sys.executable
    script = str(Path(sm.__file__).resolve())
    jobs = []

    for path in paths:
        try:
            root = parse_vdf(Path(path).read_bytes())
        except (OSError, ValueError, IndexError) as error:
            print(f"Could not read {path}: {error}")
            continue

        _, shortcuts = field(root, "shortcuts")
        changes = []

        for entry in (shortcuts[2] if shortcuts else []):
            if entry[0] != 0x00:
                continue

            fields = entry[2]
            exe = get_text(fields, "Exe")
            options = get_text(fields, "LaunchOptions")
            named = NAME_RE.search(options)

            if not (named and strip_manager(exe, options)):
                continue

            new_exe, new_options = build_options(platform, python, script, named.group(2), exe, options)

            if named.group(1):                                  # keep --force-nas
                new_options = new_options.replace(f'launch "{named.group(2)}"', f'launch --force-nas "{named.group(2)}"', 1)

            if (new_exe, new_options) != (exe, options):
                changes.append((fields, get_text(fields, "AppName"), new_exe, new_options))

        if changes:
            jobs.append((path, root, changes))

    if not jobs:
        print("\nEvery manager launch line already points at this program - nothing to change.")
        return 0

    print(f"\nThese shortcuts will point at:  {script}\n")

    for path, root, changes in jobs:
        for fields, name, new_exe, new_options in changes:
            print(f"  {name}")

    if not yes and sm.ask("\nRelink these now? A backup copy is saved first. [y/N]: ").strip().lower() != "y":
        return 1

    def work():
        for path, root, changes in jobs:
            saved = backup_shortcuts(sm, path, "before-relink")

            for fields, name, new_exe, new_options in changes:
                set_text(fields, "Exe", new_exe)
                set_text(fields, "LaunchOptions", new_options)

            temp = path + ".tmp"
            Path(temp).write_bytes(dump_vdf(root))
            os.replace(temp, path)
            print(f"\nRelinked {len(changes)} shortcut(s) in {path}\n(Backup: {saved})")

        return 0

    return with_steam_closed(sm, platform, work)


def reset_setup(sm, platform, paths, yes):
    """Takes the manager's launch lines OUT of every shortcut. Names, icons, other options and all other shortcuts stay as they are."""
    jobs = []

    for path in paths:
        try:
            root = parse_vdf(Path(path).read_bytes())
        except (OSError, ValueError, IndexError) as error:
            print(f"Could not read {path}: {error}")
            continue

        _, shortcuts = field(root, "shortcuts")
        hits = []

        for entry in (shortcuts[2] if shortcuts else []):
            if entry[0] != 0x00:
                continue

            fields = entry[2]
            result = strip_manager(get_text(fields, "Exe"), get_text(fields, "LaunchOptions"))

            if result:
                hits.append((fields, get_text(fields, "AppName"), result))

        if hits:
            jobs.append((path, root, hits))

    if not jobs:
        print("\nNo shortcut has a manager launch line - nothing to reset.")
        return 0

    print("\nThese shortcuts would go back to how they were before the manager:\n")

    for path, root, hits in jobs:
        for fields, name, (exe, options) in hits:
            print(f"  {name}\n      program : {exe}\n      options : {options or '(none)'}")

    print("\n(Renames such as '(Disc 2)' cannot be undone by this - only a backup file brings those back.)")
    if not yes and sm.ask("\nReset these now? A backup copy is saved first. [y/N]: ").strip().lower() != "y":
        return 1

    def work():
        for path, root, hits in jobs:
            saved = backup_shortcuts(sm, path, "before-reset")

            for fields, name, (exe, options) in hits:
                set_text(fields, "Exe", exe)
                set_text(fields, "LaunchOptions", options)

            temp = path + ".tmp"
            Path(temp).write_bytes(dump_vdf(root))
            os.replace(temp, path)
            print(f"\nReset {len(hits)} shortcut(s) in {path}\n(Backup: {saved})")

        remembered = matches_path(sm)

        if remembered.exists():                      # forget earlier answers so the new matching starts fresh
            sm.BACKUP_STEAM.mkdir(parents=True, exist_ok=True)
            shutil.move(str(remembered), str(sm.BACKUP_STEAM / f"SteamMatches_{datetime.now():%Y-%m-%d_%H-%M-%S}.json"))
            print("Your remembered answers (SteamMatches.json) were put aside in the same backup folder.")

        return 0

    return with_steam_closed(sm, platform, work)


def parse_pick(answer, count):
    """'2' -> (2, False);  '2a' or 'a2' -> (2, True) = use it for ALL the remaining shortcuts of that system / emulator."""
    found = re.fullmatch(r"(\d+)\s*(a?)|a\s*(\d+)", answer.strip().lower())

    if not found:
        return None, False

    number = int(found.group(1) or found.group(3))
    return (number if 1 <= number <= count else None), bool(found.group(2) or found.group(3))


def search_games(sm, games, text):
    """Fuzzy search by typed words (the same search the manager's menu uses). Returns the chosen game or None."""
    matches = sm.find_games(games, text)[:8]

    if not matches:
        print("   nothing like that in Games.json")
        return None

    for number, game in enumerate(matches, start=1):
        print(f"     {number}  {game['Name']}")

    pick = sm.ask("   Which one? (number, Enter = none): ").strip()
    return matches[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(matches) else None


def resolve_rows(sm, rows, games, session, learned_names, learned_groups):
    """Asks about shortcuts that were not matched for certain. Answers apply to this run and can be remembered."""
    skip_search = False

    for row in rows:
        if row["game"] is not None:
            continue

        group = row["group"]

        if row["mode"] == "emulator" and group in session:
            game = find_by_name(games, session[group])

            if game is not None:
                row.update(game=game, reason=f"your choice for all {group}")
                continue

        if row["mode"] == "similar":
            print(f"\n'{row['name']}' has no exact match. Is it one of these?")

            for number, game in enumerate(row["choices"], start=1):
                print(f"  {number}  {game['Name']}   ({row['scores'][game['Name']] * 100:.0f}% similar)")

            print("  0  no, leave it alone      s  search Games.json by name")
            answer = sm.ask("Choose: ").strip()

            if answer.lower() == "s":
                text = sm.ask("Part of the game's name: ").strip()
                game = search_games(sm, games, text) if text else None
            else:
                number, _ = parse_pick(answer, len(row["choices"]))
                game = row["choices"][number - 1] if number else None

            if game is not None:
                row.update(game=game, reason="your choice")
                learned_names[row["key"]] = game["Name"]

        elif row["mode"] == "emulator":
            print(f"\n'{row['name']}' looks like a {row['looks']}. Which Games.json entry should it use?")

            for number, game in enumerate(row["choices"], start=1):
                print(f"  {number}  {game['Name']}")

            print(f"  0  leave this shortcut alone      (add  a  to a number, e.g. 1a, to use it for ALL remaining {group or 'these'} shortcuts)")
            number, everything = parse_pick(sm.ask("Choose: "), len(row["choices"]))

            if number:
                game = row["choices"][number - 1]
                row.update(game=game, reason="your choice")

                if everything and group:
                    session[group] = game["Name"]
                    learned_groups[group] = game["Name"]
                else:
                    learned_names[row["key"]] = game["Name"]

        elif not skip_search:
            print(f"\n'{row['name']}': nothing in Games.json looks like it.")
            text = sm.ask("Type part of the game's name to search for it (Enter = leave alone, S = leave ALL unmatched alone): ").strip()

            if text.lower() == "s":
                skip_search = True
            elif text:
                game = search_games(sm, games, text)

                if game is not None:
                    row.update(game=game, reason="found by your search")
                    learned_names[row["key"]] = game["Name"]


def settle(row, only, skip):
    """Decides what happens to a shortcut once its game is known (or not)."""
    game = row["game"]
    haystack = (row["name"] + " " + (game["Name"] if game else "")).lower()

    if game is None:
        row["action"] = "none"
    elif only and not any(o in haystack for o in only):
        row["action"] = "left out (not in --only)"
    elif any(k in haystack for k in skip):
        row["action"] = "left out (skip list)"
    elif game.get("SteamSetup") is False:
        row["action"] = "left out (SteamSetup is false in Games.json)"
    elif row["done"]:
        row["action"] = "refresh"
    else:
        row["action"] = "set"


def run(sm, config, platform, files=None, yes=False, only=None, skip=None, undo=False, reset=False, relink=False):
    print()
    print("=" * 62)
    print(" Steam setup: add the launch option to your non-Steam shortcuts")
    print("=" * 62)
    settings = config.get("Settings", {})
    only = only or []
    skip = (skip or []) + [str(x).lower() for x in settings.get("SteamSetupSkip", [])]
    paths = files if files is not None else shortcut_files(sm, platform)

    if not paths:
        print("\nNo non-Steam shortcuts file was found. Add a game to Steam first (Games > Add a Non-Steam Game).")
        return 1

    import_legacy_backups(sm, paths)

    if undo:
        return undo_setup(sm, platform, paths, yes)

    if reset:
        return reset_setup(sm, platform, paths, yes)

    if relink:
        return relink_setup(sm, platform, paths, yes)

    was_running = steam_running(platform)

    if was_running and in_gaming_mode(platform):
        print("\nYou are in GAMING mode - Steam is the screen, so it cannot be closed here.")
        print("Switch to Desktop mode (Steam button > Power > Switch to Desktop) and run this again.")
        return 1

    games = [g for g in config["Games"] if sm.resolve_source(g, settings, platform)]
    python = sys.executable
    script = str(Path(sm.__file__).resolve())
    detect = getattr(sm, "detect_game_from_command", None)
    memory = load_matches(sm)
    session = {}
    learned_names = {}
    learned_groups = {}
    rename_discs = bool(settings.get("SteamSetupRenameDiscs", True))
    rows = []

    for path in paths:
        try:
            root = parse_vdf(Path(path).read_bytes())
        except (OSError, ValueError, IndexError) as error:
            print(f"\nCould not read {path}: {error}")
            continue

        _, shortcuts = field(root, "shortcuts")

        if not shortcuts:
            continue

        for index, entry in enumerate(shortcuts[2]):
            if entry[0] != 0x00:
                continue

            fields = entry[2]
            name = get_text(fields, "AppName")
            exe = get_text(fields, "Exe")
            options = get_text(fields, "LaunchOptions")
            row = decide(name, exe, options, games, detect, settings, memory, session)

            if row["game"] is None and already_done(platform, exe, options):
                named = NAME_RE.search(options)
                kept = find_by_name(games, named.group(2)) if named else None

                if kept is not None:
                    row.update(game=kept, reason="already set up for this game", mode="auto")

            info = row["info"]
            row.update(path=path, root=root, index=index, name=name, rename=None,
                       done=already_done(platform, exe, options),
                       looks=((info["emulator"] or info["system"] or "emulator") + " emulator shortcut")
                       if info["kind"] == "emulator" else "native program (recomp / PC game)")
            disc = disc_number(exe + " " + options)
            has_id = field(fields, "appid")[1] is not None

            if rename_discs and disc and has_id and not re.search(r"\b(disc|disk|cd)\s*\d", name, re.I):
                row["rename"] = f"{name} (Disc {disc})"

            rows.append(row)

    if not rows:
        print("\nNo shortcuts found.")
        return 1

    if not yes:
        resolve_rows(sm, rows, games, session, learned_names, learned_groups)

    for row in rows:
        settle(row, only, skip)

    print("\nHere is what would happen to each shortcut:\n")

    for number, row in enumerate(rows, start=1):
        print(f"  {number:>2}  {row['name'][:48]}   [{row['looks']}]")

        if row["game"]:
            print(f"        -> uses: {row['game']['Name']}   (because: {row['reason']})")
        else:
            print(f"        -> {row['reason']}")

        if row["rename"] and row["action"] in ("set", "refresh"):
            print(f"        will be renamed: {row['rename']}")

        print(f"        {row['action'] if row['action'] != 'none' else 'left alone'}")

    unmatched = [row for row in rows if row["game"] is None]

    if unmatched:
        print(f"\nNOT MATCHED ({len(unmatched)}) - these were left alone:")

        for row in unmatched:
            print(f"   - {row['name']}   [{row['looks']}]")

        print("   (Add the game to Games.json, or run this again and search for it by name.)")

    todo = [row for row in rows if row["game"] is not None and row["action"] in ("set", "refresh")]

    if not todo:
        print("\nNothing to change.")
        return 1

    if not yes:
        answer = sm.ask("\nLeave any out? Type their numbers separated by commas (for example 2,5), or press Enter to keep all: ")
        leave = {int(x) for x in re.findall(r"\d+", answer)}
        todo = [r for number, r in enumerate(rows, start=1) if r in todo and number not in leave]

        if not todo:
            print("Nothing left to change.")
            return 1

    print(f"\n{len(todo)} shortcut(s) will be changed. A backup copy of shortcuts.vdf is saved first.")
    print("(Undo later with:  python savemanager.py steamsetup --undo   or menu O > 2)")
    remember = False

    if not yes and (learned_names or learned_groups):
        remember = sm.ask("Remember your answers for next time (SteamMatches.json)? [Y/n]: ").strip().lower() != "n"

    if was_running:
        print("Steam is open: it will be CLOSED for a moment and STARTED AGAIN automatically.")
        print("(You will not be able to type until it is back - answer now.)")

    if not yes and sm.ask("Go ahead? [y/N]: ").strip().lower() != "y":
        return 1

    closed = False

    try:
        if was_running:
            print("\nClosing Steam ...")
            closed = True

            if not close_steam(sm, platform):
                print("Steam did not close in time - nothing was changed. Close it yourself and run this again.")
                return 1

        changed_files = {}

        for row in todo:
            _, shortcuts = field(row["root"], "shortcuts")
            fields = shortcuts[2][row["index"]][2]
            exe = get_text(fields, "Exe")
            options = get_text(fields, "LaunchOptions")
            new_exe, new_options = build_options(platform, python, script, row["game"]["Name"], exe, options)
            set_text(fields, "LaunchOptions", new_options)

            if new_exe != exe:
                set_text(fields, "Exe", new_exe)

            if row["rename"]:
                set_text(fields, "AppName", row["rename"])

            changed_files[row["path"]] = row["root"]

        for path, root in changed_files.items():
            backup = backup_shortcuts(sm, path, "before-setup")
            temp = path + ".tmp"
            Path(temp).write_bytes(dump_vdf(root))
            os.replace(temp, path)
            print(f"\nSaved {path}\n(Backup: {backup})")

        if remember:
            memory["names"].update(learned_names)
            memory["groups"].update(learned_groups)
            save_matches(sm, memory)
            print("Your answers were saved in SteamMatches.json.")
    finally:
        # Steam always comes back, even if something above went wrong
        if closed:
            print("\nStarting Steam again ...")
            start_steam(sm, platform)

    if not was_running:
        print("\nStart Steam now.")

    print("If anything looks wrong:  python savemanager.py steamsetup --undo")
    return 0


def main():
    """So that  python steamsetup.py [--undo] [--yes] [--only ..] [--skip ..]  works too."""
    import argparse

    parser = argparse.ArgumentParser(description="Add the launch option to non-Steam shortcuts")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--skip", default="")
    parser.add_argument("--undo", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import savemanager as sm

    platform = sm.detect_platform()
    config = sm.expand_games(sm.load_config(), platform)
    sys.exit(run(sm, config, platform, yes=args.yes, only=sm.name_filter(args.only),
                 skip=sm.name_filter(args.skip), undo=args.undo))


if __name__ == "__main__":
    main()

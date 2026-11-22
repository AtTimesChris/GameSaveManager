#!/usr/bin/env python3
"""
Game Save Manager v14 (Python)
=========================================
Runs on Windows 11 and Steam Deck (desktop mode or gaming mode).

New in this version
  - ONE shared Current per game (the newest save from any device) + Versions that record
    which device wrote them. meta.json says who wrote Current last. A per-device
    "last synced" memory (State/<device>.json) tells a safe pull from a real conflict.
  - Per game folder: Original-save shortcut + Backup / Restore launchers (per device).
  - launch command: pull a newer save before the game, back up after it (Steam launch option).
  - Restore command (interactive): pick any device's Current/Version, see size + dates,
    get a safety "Pre-restore" snapshot before anything is replaced.
  - Local fallback folder (e.g. the Deck's microSD) when the NAS isn't reachable,
    plus a "sync" command to copy it to the NAS later.
  - Path variables ({Docs} etc.) and %ENV% / ~ expansion in Games.json paths.
  - Run it with no arguments for a simple menu (works when double-clicked).
  - Include / Exclude wildcard filters per game (file names, extensions, folders).

Examples
  python savemanager.py                      (menu)
  python savemanager.py list
  python savemanager.py backup --dry-run
  python savemanager.py backup "elden ring"
  python savemanager.py backup --skip "Dragon Age 1 Mods"
  python savemanager.py restore "crysis 3"
  python savemanager.py sync

PowerShell cheat sheet used in the comments below:
  Join-Path a b          ->  Path(a) / b
  Test-Path $p           ->  p.exists()
  Get-ChildItem -Recurse ->  p.rglob("*")
  $x = @()               ->  x = []
  $h = @{}               ->  h = {}
  foreach ($a in $list)  ->  for a in list:
  "$name text"           ->  f"{name} text"
"""

import argparse    # reads the command line (like param() in PowerShell)
import difflib     # built-in fuzzy matching (replaces your Levenshtein code)
import fnmatch     # wildcard matching like *sav*
import hashlib     # file hashes (like Get-FileHash)
import json        # reads Games.json
import logging     # writes the log files
import os          # environment variables like %USERPROFILE%
import re          # regular expressions (-match in PowerShell)
import shutil      # copying and deleting folders (the robocopy part)
import socket      # gives us this computer's name
import subprocess  # starts the game and the pop-up dialogs
import sys         # tells us which operating system we are on
import threading   # lets us give up on a NAS that does not answer
import time        # pauses (like Start-Sleep)
from datetime import datetime
from pathlib import Path   # Path objects: folders and files that work on both OSes

__version__ = "14.6.0"
DEFAULT_VERSION_COUNT = 100

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "Games.json"

# Everything the program keeps "just in case" lives under Backups/
BACKUP_DIR = SCRIPT_DIR / "Backups"
BACKUP_GAMES = BACKUP_DIR / "Games.json"           # copies made when Games.json is edited
BACKUP_STEAM = BACKUP_DIR / "Steam shortcuts"      # copies of shortcuts.vdf
BACKUP_UPGRADES = BACKUP_DIR / "Program versions"  # old program files from before an upgrade
BACKUP_PACKAGES = BACKUP_DIR / "Packages"          # the upgrade .zip files you used
KEEP_GAMES_BACKUPS = 20                            # how many Games.json copies to keep

# exFAT / SD cards only store times to the nearest 2 seconds, so differences
# smaller than this count as "same time".
TIME_TOLERANCE_SECONDS = 2

# What a backup does to each file (shown in logs and in Save Information.txt).
R_NEW = "Copy to backup (new file)"
R_SAME = "Update backup (same time)"
R_NEWER = "Update backup (game save is newer)"
R_OLDER = "Update backup (backup was newer!)"
R_REMOVE = "Remove from backup (deleted in game save)"

log = logging.getLogger("savemanager")

# What the NAS upload is doing right now (so a slow upload can say where it was when we stopped waiting).
UPLOAD = {"stage": "not started"}

# Detail lines that go to the log FILE only (not the screen). Used by restore,
# which prints its own tidy tables.
flog = logging.getLogger("savemanager.filelog")
flog.propagate = False


# ----------------------------------------------------------------------
# Platform, config and paths
# ----------------------------------------------------------------------
def detect_platform():
    """Returns 'Windows', 'SteamDeck' or 'Linux'."""
    if sys.platform.startswith("win"):
        return "Windows"

    if Path("/home/deck").exists():
        return "SteamDeck"

    return "Linux"


def load_config():
    # utf-8-sig quietly removes the invisible "BOM" mark that
    # PowerShell's Set-Content -Encoding UTF8 puts at the start of files.
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        text = f.read()

    # JSON has no comments, so whole lines starting with # are ignored (handy for notes).
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return json.loads(text)


def get_device_name(settings, platform):
    """Settings.DeviceNames = { "Windows": "PC", "SteamDeck": "Deck" }"""
    names = settings.get("DeviceNames", {})

    return names.get(platform) or settings.get("DeviceName") or socket.gethostname()


MYIDS_FILE = SCRIPT_DIR / "MyIDs.json"
_MYIDS_CACHE = {}


def load_my_ids():
    """MyIDs.json: your own IDs (Steam user ID, Rockstar profile ...). Values still starting YOUR_ are ignored."""
    if "data" not in _MYIDS_CACHE:
        try:
            with open(MYIDS_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}

        _MYIDS_CACHE["data"] = {k: str(v).strip() for k, v in data.items()
                                if not k.startswith("_") and str(v).strip() and not str(v).startswith("YOUR_")}

    return _MYIDS_CACHE["data"]


def expand_path(text, settings, platform):
    """
    JSON can't hold variables, so Python does it:
      {Docs}        -> replaced from Settings.Variables.<platform>.Docs
      %USERPROFILE% -> Windows environment variable
      ~             -> home folder
    """
    variables = dict(settings.get("Variables", {}).get(platform, {}))
    variables.update(load_my_ids())

    # Repeat a few times so a variable may use another variable.
    for _ in range(3):
        for name, value in variables.items():
            text = text.replace("{" + name + "}", value)

    return os.path.expandvars(os.path.expanduser(text))


def get_roots(settings, platform):
    """Returns (primary backup folder, fallback folder). Either may be None."""
    primary = None
    fallback = None
    roots = settings.get("BackupRoots", {})

    if roots.get(platform):
        primary = Path(expand_path(roots[platform], settings, platform))
    elif platform == "Windows" and settings.get("BackupRoot"):
        primary = Path(expand_path(settings["BackupRoot"], settings, platform))

    fallbacks = settings.get("FallbackRoots", {})

    if fallbacks.get(platform):
        fallback = Path(expand_path(fallbacks[platform], settings, platform))

    return primary, fallback


REACH_TIMEOUT = {"seconds": 5.0}


def reachable(path, timeout=None):
    """
    path.exists() that gives up after a few seconds. A NAS mount whose network went
    away can make a normal exists() hang for minutes; this reports 'not reachable' instead.
    """
    result = []
    timeout = timeout if timeout is not None else REACH_TIMEOUT["seconds"]

    def check():
        try:
            result.append(Path(path).exists())
        except OSError:
            result.append(False)

    thread = threading.Thread(target=check, daemon=True)
    thread.start()
    thread.join(timeout)

    return bool(result and result[0])


def probe_nas(root, timeout=10.0):
    """
    Tiny speed test: writes and deletes a 128 KB file in the backup folder.
    Returns (delay in seconds for one small operation, KB per second) or None if it
    did not finish in time.
    """
    result = {}

    def work():
        try:
            probe = Path(root) / f".speedtest-{os.getpid()}.tmp"
            data = os.urandom(128 * 1024)
            start = time.time()
            os.stat(root)
            delay = time.time() - start
            start = time.time()

            with open(probe, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())

            write_time = max(time.time() - start, 0.0001)
            probe.unlink()
            result["value"] = (delay, 128 / write_time)
        except OSError:
            pass

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)

    return result.get("value")


def nas_state(settings, platform, total_bytes=0, file_count=1):
    """
    Is the NAS usable RIGHT NOW? Returns (state, folder, text):
      'fast'  - go ahead          'slow' - reachable but too slow for this backup
      'down'  - not reachable
    A backup is 'slow' when the estimated time is above Settings.SlowNasSeconds (default 20).
    """
    primary, fallback = get_roots(settings, platform)

    if not primary or not reachable(primary):
        return "down", None, "NAS not reachable"

    probe = probe_nas(primary)

    if probe is None:
        return "slow", primary, "NAS did not finish a tiny speed test"

    delay, kbps = probe
    estimate = total_bytes / (kbps * 1024) + delay * max(file_count, 1) * 6
    limit = float(settings.get("SlowNasSeconds", 20))
    text = f"NAS check: {kbps:.0f} KB/s, delay {delay * 1000:.0f} ms, estimated {estimate:.0f}s (limit {limit:.0f}s)"

    return ("fast" if estimate <= limit else "slow"), primary, text


def run_with_timeout(func, seconds):
    """Runs func() but stops waiting after 'seconds'. Returns (finished, value)."""
    box = {}

    def work():
        try:
            box["value"] = func()
        except Exception as error:
            box["error"] = error

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(seconds)

    if thread.is_alive():
        return False, None

    if "error" in box:
        raise box["error"]

    return True, box.get("value")


def choose_backup_root(settings, platform):
    """
    Returns (folder, using_fallback). Uses the main folder (NAS) when it is
    reachable, otherwise the local fallback. Never creates the main folder:
    if the NAS isn't mounted that would quietly fill the local disk instead.
    """
    primary, fallback = get_roots(settings, platform)

    if primary and reachable(primary):
        return primary, False

    if fallback:
        # Allowed to create the fallback folder only if its parent exists
        # (so an unplugged microSD doesn't get "created" on the internal disk).
        if not reachable(fallback) and reachable(fallback.parent):
            fallback.mkdir()

        if reachable(fallback):
            return fallback, True

    return None, False


def location_path(location, platform):
    """
    One entry of a game's "Locations" list. The path key is any key ending in Source
    (MemoryCardSource, SaveStateSource, Source) for Windows, or ending in SourceSteamDeck
    for the Deck (MemoryCardSourceSteamDeck ...).
    """
    suffix = "" if platform == "Windows" else platform

    for key, value in location.items():
        if isinstance(value, str) and key.endswith("Source" + suffix):
            return value

    return None


def save_locations(game, settings, platform):
    """[(label, path)] for this device. Normal games: one unlabelled path. 'Locations' games: one per location."""
    locations = game.get("Locations")

    if isinstance(locations, list) and locations:
        found = []

        for number, location in enumerate(locations, start=1):
            value = location_path(location, platform)

            if value:
                label = safe_name(str(location.get("Type") or location.get("Name") or f"Location {number}"))
                found.append((label, expand_path(value, settings, platform)))

        return found

    source = resolve_source(game, settings, platform)
    return [("", source)] if source else []


class SaveSet:
    """
    All the save folders of one game on this device. With several locations the backup keeps
    each one in its own sub folder named after its Type (e.g. 'Save Folder/', 'Save State Folder/').
    """

    def __init__(self, locations):
        self.locations = [(label, Path(path)) for label, path in locations]
        self.labelled = any(label for label, _ in self.locations)

    def exists(self):
        return any(path.exists() for _, path in self.locations)

    def folders(self):
        return [path for _, path in self.locations if path.is_dir()]

    def describe(self):
        if not self.labelled:
            return str(self.locations[0][1])

        return "; ".join(f"{label}: {path}" for label, path in self.locations)

    def files(self, include, exclude):
        result = {}

        for label, path in self.locations:
            if not path.exists():
                continue

            for relative, file_path in list_files(path, include, exclude).items():
                result[f"{label}/{relative}" if label else relative] = file_path

        return result

    def owns(self, key):
        """Backup files that belong to a location which exists right now (others are left alone)."""
        if not self.labelled:
            return True

        present = {label for label, path in self.locations if path.exists()}
        return key.split("/", 1)[0] in present

    def target(self, key):
        """Where a file from the backup goes on this device (None if its location has no path here)."""
        if not self.labelled:
            base = self.locations[0][1]
            looks_like_file = base.is_file() or (not base.exists() and base.suffix != "")
            return base if looks_like_file else base / key

        label, _, rest = key.partition("/")

        for name, path in self.locations:
            if name == label:
                return path / rest

        return None


def get_save_set(game, settings, platform):
    locations = save_locations(game, settings, platform)
    return SaveSet(locations) if locations else None


def resolve_source(game, settings, platform):
    """
    Windows   -> "Source"
    SteamDeck -> "SourceSteamDeck"
    Linux     -> "SourceLinux"
    Returns None when the game has no path for this device (it is skipped).
    """
    if isinstance(game.get("Locations"), list) and game["Locations"]:
        found = save_locations(game, settings, platform)
        return found[0][1] if found else None

    if platform == "Windows":
        key = "Source"
    else:
        key = "Source" + platform

    value = game.get(key)

    if value:
        return expand_path(value, settings, platform)

    return None


def safe_name(name):
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", name)
    return cleaned.strip().rstrip(".")


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def launched_by_steam():
    """True when Steam started this program (a game launch). Nobody can answer a text prompt then."""
    return any(name in os.environ for name in ("SteamGameId", "SteamAppId", "SteamOverlayGameId"))


def ask(prompt):
    """input() that doesn't crash or hang if there is no keyboard (EOF, or started by Steam)."""
    if launched_by_steam():
        return ""

    try:
        return input(prompt)
    except EOFError:
        return ""


def human_size(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(num)} B"

            return f"{num:.1f} {unit}"

        num /= 1024


def format_time(timestamp):
    if timestamp is None:
        return "-"

    return datetime.fromtimestamp(timestamp).strftime("%d/%m/%y %H:%M")


def make_stamp(suffix=None):
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d__%H-%M-%S")

    if suffix:
        return f"{stamp}__{suffix}"

    return f"{stamp}__{now.microsecond // 1000:03d}"


def setup_logging(label, device_name):
    """One log file per run in Logs/<device>/ so devices never share a log."""
    logs_folder = SCRIPT_DIR / "Logs" / safe_name(device_name)
    logs_folder.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%d-%m-%Y__%H-%M-%S")
    log_file = logs_folder / f"{safe_name(label)}-{stamp}.log"

    log.setLevel(logging.INFO)
    flog.setLevel(logging.INFO)
    log.handlers.clear()
    flog.handlers.clear()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(message)s", "%d-%m-%Y %H:%M:%S")
    )
    log.addHandler(file_handler)
    flog.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console_handler)

    return log_file


# ----------------------------------------------------------------------
# Listing and comparing files
# ----------------------------------------------------------------------
def file_hash(path):
    sha = hashlib.sha256()

    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)

    return sha.hexdigest()


def files_differ(file_a, file_b):
    """Different size = different (fast). Same size = compare hashes."""
    if file_a.stat().st_size != file_b.stat().st_size:
        return True

    return file_hash(file_a) != file_hash(file_b)


def is_wanted(relative, include, exclude):
    """
    Include / Exclude wildcard lists from Games.json. Matched against the path
    INSIDE the save folder, lower case, "/" separators. * matches across folders.
      "*.sav"        every .sav file at any depth
      "*shader*"     anything with 'shader' in its path
      "profiles/*"   everything under a top level 'Profiles' folder
      "*/profile.xml" a file called profile.xml in any sub folder
    """
    text = relative.lower()

    for pattern in exclude:
        if fnmatch.fnmatch(text, pattern.lower()):
            return False

    if include:
        return any(fnmatch.fnmatch(text, p.lower()) for p in include)

    return True


def list_files(root, include, exclude):
    """
    { "relative/path": Path } for every file under root, at every folder depth.
    rglob("*") walks the whole tree; is_file() just skips the folder entries.
    If root is a single file, that one file is returned.
    """
    root = Path(root)
    files = {}

    if root.is_file():
        files[root.name] = root
        return files

    if not root.exists():
        return files

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        relative = path.relative_to(root).as_posix()

        if is_wanted(relative, include, exclude):
            files[relative] = path

    return files


def compare(source_files, backup_files):
    """Which files differ, and which side is newer. Content decides IF, time decides WHICH."""
    differences = []

    for relative, src in sorted(source_files.items()):
        bak = backup_files.get(relative)
        src_time = src.stat().st_mtime

        if bak is None:
            differences.append({"file": relative, "result": R_NEW,
                                "source_time": src_time, "backup_time": None})
            continue

        if not files_differ(src, bak):
            continue

        bak_time = bak.stat().st_mtime

        if abs(src_time - bak_time) <= TIME_TOLERANCE_SECONDS:
            result = R_SAME
        elif src_time > bak_time:
            result = R_NEWER
        else:
            result = R_OLDER

        differences.append({"file": relative, "result": result,
                            "source_time": src_time, "backup_time": bak_time})

    for relative, bak in sorted(backup_files.items()):
        if relative not in source_files:
            differences.append({"file": relative, "result": R_REMOVE,
                                "source_time": None, "backup_time": bak.stat().st_mtime})

    return differences


def remove_empty_folders(root):
    for folder in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()


def replace_retry(src, dst, tries=6):
    """
    os.replace that tries again for a few seconds. On a NAS share (or with an antivirus / indexer watching the folder)
    Windows can refuse a rename for a moment because the freshly written files are still "in use"
    (PermissionError, WinError 5 or 32). The same rename a moment later works.
    """
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if attempt == tries - 1:
                raise

            time.sleep(0.5 * (attempt + 1))


def copy_atomic(src, dst):
    """Copies to a temporary name then renames, so an interrupted copy never leaves a half-written save."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    temp = dst.with_name(dst.name + ".part")
    shutil.copy2(src, temp)
    replace_retry(temp, dst)


def quick_same(file_a, file_b):
    """Same size and same modified time (copy2 keeps modified times). Reads no file contents."""
    stat_a = file_a.stat()
    stat_b = file_b.stat()

    return stat_a.st_size == stat_b.st_size and abs(stat_a.st_mtime - stat_b.st_mtime) <= 0.002


def mirror_folder(src_dir, dst_dir):
    """
    Makes dst_dir identical to src_dir (like robocopy /MIR). Used by sync and the NAS push.
    Files are compared by size + modified time, so nothing is read over a slow network.
    """
    copied = 0
    removed = 0
    UPLOAD["stage"] = "reading the file list on both sides"
    src_files = list_files(src_dir, [], [])
    dst_files = list_files(dst_dir, [], [])
    todo = []

    for relative, src in src_files.items():
        dst = dst_files.get(relative)

        if dst is None or not quick_same(src, dst):
            todo.append((relative, src))

    for number, (relative, src) in enumerate(todo, start=1):
        UPLOAD["stage"] = f"copying file {number} of {len(todo)}: {relative}"
        copy_atomic(src, Path(dst_dir) / relative)
        copied += 1

    UPLOAD["stage"] = "removing files that no longer exist"

    for relative, dst in dst_files.items():
        if relative not in src_files:
            dst.unlink()
            removed += 1

    if Path(dst_dir).exists():
        remove_empty_folders(Path(dst_dir))

    return copied, removed


# ----------------------------------------------------------------------
# Versions
# ----------------------------------------------------------------------
def version_time(folder):
    """Understands new yyyy-mm-dd names, your v12 dd-mm-yyyy names, else folder time."""
    for pattern in ("%Y-%m-%d__%H-%M-%S", "%d-%m-%Y__%H-%M-%S"):
        try:
            return datetime.strptime(folder.name[:20], pattern).timestamp()
        except ValueError:
            continue

    return folder.stat().st_mtime


def trim_versions(versions, keep, thin_after_days=0):
    """
    Keeps the newest 'keep' versions. Versions older than 'thin_after_days' days are thinned to the
    newest one per calendar day (0 = no thinning), so history stays long without piling up.
    """
    folders = [p for p in versions.iterdir() if p.is_dir() and not p.name.endswith(".partial")]
    folders.sort(key=version_time, reverse=True)

    for old in folders[keep:]:
        shutil.rmtree(old)
        log.info("Removed old version: %s", old.name)

    if thin_after_days > 0:
        cutoff = time.time() - thin_after_days * 86400
        days = set()

        for folder in folders[:keep]:
            moment = version_time(folder)

            if moment >= cutoff:
                continue

            day = datetime.fromtimestamp(moment).date()

            if day in days:
                shutil.rmtree(folder)
                log.info("Thinned old version: %s", folder.name)
            else:
                days.add(day)


# ----------------------------------------------------------------------
# Backing up one game
# ----------------------------------------------------------------------
def folder_digest(files):
    """
    One fingerprint for a whole save: file names + file contents (not dates).
    files = { "relative/path": Path }.  Two saves with the same digest are identical.
    """
    sha = hashlib.sha256()

    for relative in sorted(files):
        sha.update(relative.encode("utf-8"))
        sha.update(b"\0")
        sha.update(file_hash(files[relative]).encode("ascii"))
        sha.update(b"\n")

    return sha.hexdigest()


def read_meta(game_folder):
    """meta.json in the game's backup folder: who wrote Current last, when, and its digest."""
    try:
        with open(game_folder / "meta.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# What THIS device last synced with the NAS, per game. It lets us tell
# "the other device changed it" (safe to pull) from "both changed it" (conflict).
# Stored next to the script in State/<device>.json - never inside the shared folder.
def state_file(device_name):
    return SCRIPT_DIR / "State" / f"{safe_name(device_name)}.json"


def read_state(device_name):
    try:
        with open(state_file(device_name), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def get_base(device_name, game_name):
    return read_state(device_name).get(game_name)


def set_base(device_name, game_name, digest):
    data = read_state(device_name)
    data[game_name] = digest
    path = state_file(device_name)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def write_if_changed(path, text, newline=None):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                if f.read().replace("\r\n", "\n") == text:
                    return

        with open(path, "w", encoding="utf-8", newline=newline) as f:
            f.write(text)
    except OSError as error:
        log.warning("Could not write %s: %s", path.name, error)


def make_windows_shortcut(link_path, target):
    """Python has no built-in .lnk maker, so ask PowerShell's WScript.Shell (like v12 did)."""
    icon = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "shell32.dll") + ",3"
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{link}'); "
        "$s.TargetPath = '{target}'; $s.WorkingDirectory = '{target}'; "
        "$s.IconLocation = '{icon}'; $s.Description = 'Open original save folder'; $s.Save()"
    ).format(link=str(link_path).replace("'", "''"),
             target=str(target).replace("'", "''"),
             icon=icon.replace("'", "''"))

    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                   capture_output=True, timeout=20)


def helper_target(settings, platform, backup_root, game_name, device_name):
    """
    Where this device's shortcut + launcher files go. If a local fallback folder
    exists they go there (each device only sees its own); otherwise into the shared
    game folder with the device name in the file names.
    """
    primary, fallback = get_roots(settings, platform)

    if fallback and reachable(fallback):
        return fallback / safe_name(game_name), ""

    return backup_root / safe_name(game_name), safe_name(device_name)


def ensure_helpers(game, game_folder, tag, source, platform):
    """
    Puts these in the game's backup folder (named per device, because the
    NAS folder is shared and each device has its own script location):
      - a shortcut to the original save folder
      - 'Backup <game>' and 'Restore <game>' launchers for that game only
    """
    try:
        game_folder.mkdir(parents=True, exist_ok=True)
        name = game["Name"]
        label = safe_name(name)
        suffix = f" ({tag})" if tag else ""
        script = Path(__file__).resolve()
        python = sys.executable
        folder_to_open = Path(source)

        if folder_to_open.is_file():
            folder_to_open = folder_to_open.parent

        for action, title in (("backup", "Backup"), ("restore", "Restore")):
            if platform == "Windows":
                text = f'@echo off\n"{python}" "{script}" {action} "{name.replace("%", "%%")}"\npause\n'
                write_if_changed(game_folder / f"{title} {label}{suffix}.bat", text, "\r\n")
            else:
                path = game_folder / f"{title} {label}{suffix}.sh"
                text = (f'#!/bin/bash\n"{python}" "{script}" {action} "{name}"\n'
                        'echo\nread -r -p "Press Enter to close..."\n')
                write_if_changed(path, text)

                try:
                    path.chmod(0o755)
                except OSError:
                    pass

        if platform == "Windows":
            link = game_folder / f"{label} - Original Save{suffix}.lnk"

            if not link.exists():
                make_windows_shortcut(link, folder_to_open)
        else:
            text = (f"[Desktop Entry]\nType=Link\nName={label} - Original Save\n"
                    f"URL={folder_to_open.as_uri()}\nIcon=folder\n")
            write_if_changed(game_folder / f"{label} - Original Save{suffix}.desktop", text)
    except Exception as error:
        log.warning("Could not create the shortcut / launcher files: %s", error)


def backup_game(game, settings, platform, backup_root, device_name, dry_run, is_fallback=False, quiet=False):
    """Backs up one game (backup_one_game), then brings the other emulators' copies of a Switch game in step."""
    game = newest_switch_copy(game, platform)
    result = backup_one_game(game, settings, platform, backup_root, device_name, dry_run, is_fallback, quiet)

    if not dry_run and result in ("backed_up", "unchanged") and game.get("SwitchCopies"):
        try:
            keep_copies_in_step(game, settings, platform, device_name, backup_root)
        except Exception:
            log.exception("%s: could not bring the other emulators' copies in step", game["Name"])

    return result


def backup_one_game(game, settings, platform, backup_root, device_name, dry_run, is_fallback=False, quiet=False):
    """
    Layout:  <backup root>/<Game>/Current          the newest save (shared by all devices)
                                  /Versions/...    older saves (named by when + who wrote them)
                                  /meta.json       who wrote Current last, when, digest
    Returns: backed_up, unchanged, skipped, missing, refused, conflict, dry_run.
    """
    name = game["Name"]
    save_set = get_save_set(game, settings, platform)

    if not quiet:
        log.info("")
        log.info("=" * 45)
        log.info(name)
        log.info("=" * 45)

    if not save_set:
        if not quiet:
            log.info("SKIPPED: no save path set for %s", platform)

        return "skipped"

    source = save_set.describe()
    first_path = str(save_set.locations[0][1])

    if not save_set.exists():
        if game.get("Virtual"):
            if not quiet:
                log.info("Not on this device yet (no save folder here).")
        elif "YOUR_" in source:
            log.warning("This game's path still has a YOUR_... placeholder: %s", source)
            log.warning("Run  python savemanager.py ids  (menu M) to fill in your IDs.")
        else:
            log.warning("SOURCE MISSING: %s", source)

        return "missing"

    include = game.get("Include", [])
    exclude = list(settings.get("GlobalExclude", [])) + list(game.get("Exclude", []))

    game_folder = backup_root / safe_name(name)
    current = game_folder / "Current"
    versions = game_folder / "Versions"
    meta = read_meta(game_folder)

    log.info("Reading the game save folder...")
    source_files = save_set.files(include, exclude)

    # Fast path: if this save's fingerprint matches the one recorded in meta.json,
    # nothing changed - and nothing has to be read from the (maybe slow) backup folder.
    if source_files and meta.get("digest") and reachable(current):
        if folder_digest(source_files) == meta["digest"]:
            if not quiet:
                log.info("No changes detected (checked by fingerprint)")

            if not dry_run:
                if not is_fallback:
                    set_base(device_name, name, meta["digest"])

                ensure_helpers(game, *helper_target(settings, platform, backup_root, name, device_name), first_path, platform)

            return "unchanged"

    log.info("Reading the backup folder (slow on a slow connection)...")
    backup_files = {k: v for k, v in list_files(current, [], []).items() if save_set.owns(k)}

    if not source_files and backup_files:
        # Safety net: never wipe a backup because the source looks empty.
        log.warning("REFUSED: original save folder has no (wanted) files but the backup does.")
        log.warning("Check the path / Include / Exclude: %s", source)
        return "refused"

    if not source_files:
        log.info("Nothing to back up (no files found)")
        return "unchanged"

    differences = compare(source_files, backup_files)

    if not differences:
        log.info("No changes detected")

        if not dry_run:
            if not is_fallback:
                set_base(device_name, name, folder_digest(source_files))

            ensure_helpers(game, *helper_target(settings, platform, backup_root, name, device_name), first_path, platform)

        return "unchanged"

    log.info("Changes detected: %d file(s)", len(differences))
    log.info("  Game save (this device): %s", source)
    log.info("  Backup (Current)       : %s", current)

    for diff in differences:
        log.info("  %-42s %s  (game save %s | backup %s)", diff["result"], diff["file"],
                 format_time(diff["source_time"]), format_time(diff["backup_time"]))

    # Cloud-style safety: if ANOTHER device saved newer since we last synced,
    # do not overwrite it with an older local save.
    last_device = meta.get("last_device")

    if (not is_fallback and backup_files and meta.get("digest")
            and get_base(device_name, name) != meta["digest"] and last_device != device_name):
        remote_newest = max(p.stat().st_mtime for p in backup_files.values())
        local_newest = max(p.stat().st_mtime for p in source_files.values())

        if remote_newest > local_newest + TIME_TOLERANCE_SECONDS:
            log.warning("CONFLICT: %s saved a NEWER version (%s) than this device (%s).",
                        last_device or "another device", format_time(remote_newest), format_time(local_newest))
            log.warning("Not overwriting it. Start the game with the launch option, or use Restore, to choose.")
            return "conflict"

    if dry_run:
        log.info("DRY RUN - nothing written")
        return "dry_run"

    current.mkdir(parents=True, exist_ok=True)
    versions.mkdir(parents=True, exist_ok=True)

    # 1. Snapshot the old Current into Versions, named by who wrote it.
    if backup_files:
        author = safe_name(last_device) if last_device else "unknown"
        stamp = make_stamp(author)

        while (versions / stamp).exists():
            time.sleep(1)
            stamp = make_stamp(author)

        shutil.copytree(current, versions / stamp)
        log.info("Version snapshot created: %s", stamp)

        if last_device and last_device != device_name:
            log.warning("This replaced a save written by %s (kept in Versions).", last_device)
    else:
        log.info("Initial backup - no previous Current backup exists")

    # 2. Update Current to match the original (the "/MIR" part).
    for number, diff in enumerate(differences, start=1):
        relative = diff["file"]
        target = current / relative

        if len(differences) > 4:
            log.info("  copying %d of %d: %s", number, len(differences), relative)

        if diff["result"] == R_REMOVE:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_files[relative], target)   # copy2 keeps modified time

    remove_empty_folders(current)

    # 3. meta.json + Save Information.txt
    newest = max(p.stat().st_mtime for p in source_files.values())
    digest = folder_digest(source_files)

    meta = {
        "game": name,
        "last_device": device_name,
        "platform": platform,
        "source": source,
        "last_backup": datetime.now().isoformat(timespec="milliseconds"),
        "newest_save_time": datetime.fromtimestamp(newest).isoformat(timespec="milliseconds"),
        "file_count": len(source_files),
        "digest": digest,
    }

    with open(game_folder / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=4)

    if not is_fallback:
        set_base(device_name, name, digest)

    write_info(game, source, current, differences, game_folder, device_name)
    ensure_helpers(game, *helper_target(settings, platform, backup_root, name, device_name), first_path, platform)

    # 4. Trim old versions.
    trim_versions(versions, int(game.get("VersionCount") or settings.get("VersionCount", DEFAULT_VERSION_COUNT)),
                  int(settings.get("VersionThinAfterDays", 30)))

    log.info("Backup complete - files updated")
    return "backed_up"


def write_info(game, source, current, differences, game_folder, device_name):
    lines = ["=" * 40, f"Game : {game['Name']}", "=" * 40, "",
             f"Original Save Location (on {device_name}): {source}",
             f"Backup Location: {current}",
             f"Last Successful Backup: {datetime.now()}",
             f"Backed up by: {device_name}", "",
             "=" * 40, f"Changes During {game['Name']} Backup", "=" * 40,
             f"{'File':<45} {'Game save':<15} {'Backup':<15} What happened"]

    for diff in differences:
        lines.append(f"{diff['file']:<45} {format_time(diff['source_time']):<15} "
                     f"{format_time(diff['backup_time']):<15} {diff['result']}")

    lines.append("")
    lines.append("Restore: use the 'Restore' launcher in this folder, or  python savemanager.py restore")

    with open(game_folder / "Save Information.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ----------------------------------------------------------------------
# Finding / choosing games
# ----------------------------------------------------------------------
def find_games(games, query):
    """Exact name, then 'contains', then spelling-tolerant (per word), then whole-name."""
    wanted = query.strip().lower()

    exact = [g for g in games if g["Name"].lower() == wanted]

    if exact:
        return exact[:1]

    contains = [g for g in games if wanted in g["Name"].lower()]

    if contains:
        return contains

    results = []

    for game in games:
        for word in re.findall(r"[a-z0-9]+", game["Name"].lower()):
            if difflib.SequenceMatcher(None, wanted, word).ratio() >= 0.7:
                results.append(game)
                break

    if results:
        return results

    names = [g["Name"].lower() for g in games]
    close = difflib.get_close_matches(wanted, names, n=5, cutoff=0.5)

    return [g for g in games if g["Name"].lower() in close]


def choose_game(games, query=None):
    """Asks for a (partial) name and lets you pick if several match. Returns a game or None."""
    if not query:
        query = ask("Game name (or part of it): ")

    if not query.strip():
        return None

    matches = find_games(games, query)

    if not matches:
        print(f"No game matched: {query}")
        return None

    if len(matches) == 1:
        return matches[0]

    print()

    for number, game in enumerate(matches, start=1):
        print(f"  {number}. {game['Name']}")

    print("  0. Cancel")
    choice = ask("Select: ").strip()

    if choice.isdigit() and 1 <= int(choice) <= len(matches):
        return matches[int(choice) - 1]

    return None


# ----------------------------------------------------------------------
# Restore
# ----------------------------------------------------------------------
def summarise_folder(folder):
    count = 0
    size = 0
    newest = None

    for path in folder.rglob("*"):
        if path.is_file():
            stat = path.stat()
            count += 1
            size += stat.st_size

            if newest is None or stat.st_mtime > newest:
                newest = stat.st_mtime

    return count, size, newest


def read_last_backup(device_folder):
    try:
        with open(device_folder / "meta.json", "r", encoding="utf-8") as f:
            return datetime.fromisoformat(json.load(f)["last_backup"]).timestamp()
    except (OSError, KeyError, ValueError):
        return None


def add_point(points, where, device, kind, name, path, taken):
    key = (device, kind, name)

    if key in points:
        return

    count, size, newest = summarise_folder(path)

    if count == 0:
        return

    points[key] = {"device": device, "kind": kind, "name": name, "path": path,
                   "where": where, "count": count, "size": size,
                   "saved": newest, "taken": taken}


def version_device(name):
    """Who wrote a version? New names look like 2026-10-18__14-56-58__PC[__PreRestore]."""
    parts = name.split("__")

    if len(parts) >= 3 and not parts[2].isdigit() and parts[2] != "PreRestore":
        return parts[2]

    return None


def add_device_points(points, where, default_device, folder, device_from_meta=False):
    """Adds <folder>/Current and every <folder>/Versions/* as restore points."""
    current = folder / "Current"

    if current.exists():
        device = default_device

        if device_from_meta:
            device = read_meta(folder).get("last_device") or default_device

        add_point(points, where, device, "Current", "Current", current, read_last_backup(folder))

    versions = folder / "Versions"

    if versions.exists():
        for version in versions.iterdir():
            if not version.is_dir() or version.name.endswith(".partial"):
                continue

            kind = "Pre-restore" if "PreRestore" in version.name else "Version"
            device = version_device(version.name) or default_device
            add_point(points, where, device, kind, version.name, version, version_time(version))


def collect_restore_points(game, roots):
    """Every Current / Version, in every reachable backup folder."""
    points = {}

    for where, root in roots:
        game_folder = root / safe_name(game["Name"])

        if not game_folder.exists():
            continue

        # Current layout (same as your v12 layout): <Game>/Current + <Game>/Versions
        add_device_points(points, where, "v12 backup", game_folder, device_from_meta=True)

        # Short-lived test layout: <Game>/<Device>/Current
        for sub in sorted(game_folder.iterdir()):
            if sub.is_dir() and sub.name not in ("Current", "Versions") and (sub / "Current").exists():
                add_device_points(points, where, f"{sub.name} (old layout)", sub)

    found = list(points.values())
    found.sort(key=lambda p: p["saved"] or 0, reverse=True)
    return found


def print_points(points):
    print()
    print(f"{'#':>3}  {'Device':<12} {'Type':<12} {'Newest save file':<17} {'Backed up':<17} {'Files':>5} {'Size':>9}  Where")

    for number, p in enumerate(points, start=1):
        print(f"{number:>3}  {p['device']:<12} {p['kind']:<12} {format_time(p['saved']):<17} "
              f"{format_time(p['taken']):<17} {p['count']:>5} {human_size(p['size']):>9}  {p['where']}")


def describe_restore(restore_files, dest_files):
    changes = []

    for relative, rp in sorted(restore_files.items()):
        dp = dest_files.get(relative)
        r_stat = rp.stat()

        if dp is None:
            changes.append({"action": "ADD", "file": relative, "restore": r_stat,
                            "current": None, "note": ""})
            continue

        if not files_differ(rp, dp):
            continue

        d_stat = dp.stat()
        gap = r_stat.st_mtime - d_stat.st_mtime

        if abs(gap) <= TIME_TOLERANCE_SECONDS:
            note = "same time"
        elif gap > 0:
            note = "backup is NEWER"
        else:
            note = "game save is NEWER"

        changes.append({"action": "REPLACE", "file": relative, "restore": r_stat,
                        "current": d_stat, "note": note})

    for relative, dp in sorted(dest_files.items()):
        if relative not in restore_files:
            changes.append({"action": "REMOVE", "file": relative, "restore": None,
                            "current": dp.stat(), "note": ""})

    return changes


def apply_restore(game, settings, platform, device_name, save_set,
                  restore_files, dest_files, changes, remove_extra):
    """
    Takes a "Pre-restore" safety snapshot of the current save, then copies the
    restore point over it. Returns the number of files copied, or None if it
    stopped for safety.
    """
    removes = [c for c in changes if c["action"] == "REMOVE"]
    active_root, _ = choose_backup_root(settings, platform)

    if dest_files and active_root:
        snap = (active_root / safe_name(game["Name"]) / "Versions"
                / make_stamp(safe_name(device_name) + "__PreRestore"))

        for relative, path in dest_files.items():
            target = snap / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

        print(f"Pre-restore snapshot saved: {snap}")
        flog.info("Pre-restore snapshot: %s", snap)
    elif dest_files:
        print("No writable backup folder for the pre-restore snapshot - stopping to be safe.")
        return None

    copied = 0

    for change in changes:
        if change["action"] not in ("ADD", "REPLACE"):
            continue

        path = restore_files[change["file"]]
        target = save_set.target(change["file"])

        if target is None:
            flog.info("SKIPPED (no folder for this on this device): %s", change["file"])
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
        flog.info("COPIED %s -> %s", path, target)

    if remove_extra:
        for change in removes:
            dest_files[change["file"]].unlink()
            flog.info("REMOVED %s", change["file"])

        for folder in save_set.folders():
            remove_empty_folders(folder)

    if game.get("SwitchCopies"):
        try:
            keep_copies_in_step(game, settings, platform, device_name, active_root)
        except Exception:
            log.exception("%s: could not bring the other emulators' copies in step", game["Name"])

    return copied


def cmd_restore(args, config, platform):
    settings = config.get("Settings", {})
    device_name = get_device_name(settings, platform)
    log_file = setup_logging("Restore", device_name)

    game = choose_game(config["Games"], args.game)

    if game is None:
        return 1

    save_set = get_save_set(game, settings, platform)

    if not save_set:
        print(f"{game['Name']} has no save path for {platform} in Games.json.")
        return 1

    source = save_set.describe()
    primary, fallback = get_roots(settings, platform)
    roots = []

    if primary and reachable(primary):
        roots.append(("Main", primary))

    if fallback and reachable(fallback):
        roots.append(("Local", fallback))

    if not roots:
        print("No backup folder is reachable (NAS not connected and no local fallback).")
        return 1

    points = collect_restore_points(game, roots)

    if not points:
        print(f"No backups found for {game['Name']}.")
        return 1

    print()
    print(f"Restore: {game['Name']}")
    print(f"Original save location on this device: {source}")
    print_points(points)
    print()
    choice = ask("Choose restore point (0 = cancel): ").strip()

    if not (choice.isdigit() and 1 <= int(choice) <= len(points)):
        print("Cancelled.")
        return 1

    point = points[int(choice) - 1]
    include = game.get("Include", [])
    exclude = list(settings.get("GlobalExclude", [])) + list(game.get("Exclude", []))

    restore_files = list_files(point["path"], include, exclude)
    dest_files = save_set.files(include, exclude)
    changes = describe_restore(restore_files, dest_files)

    flog.info("Restore of %s from %s / %s / %s", game["Name"], point["device"], point["kind"], point["name"])

    if not changes:
        print("\nThe backup you picked is identical to the game save on this device. Nothing to do.")
        return 0

    print()
    print("=" * 60)
    print(" What would change: FROM the backup you picked  ->  TO the game save on this device")
    print("=" * 60)

    shown = 0

    for change in changes:
        flog.info("%s %s", change["action"], change["file"])

        if shown >= 40:
            continue

        shown += 1
        label = {"ADD": "COPY INTO GAME SAVE (new file)", "REPLACE": "REPLACE IN GAME SAVE",
                 "REMOVE": "DELETE FROM GAME SAVE (only in game save)"}[change["action"]]
        print(f"\n{label}\n    {change['file']}")

        if change["restore"]:
            print(f"    from backup   : {human_size(change['restore'].st_size):>9}   "
                  f"modified {format_time(change['restore'].st_mtime)}")

        if change["current"]:
            print(f"    in game save  : {human_size(change['current'].st_size):>9}   "
                  f"modified {format_time(change['current'].st_mtime)}"
                  + (f"   <- {change['note']}" if change["note"] else ""))

    if len(changes) > shown:
        print(f"\n...and {len(changes) - shown} more (full list in the log: {log_file})")

    adds = sum(1 for c in changes if c["action"] == "ADD")
    replaces = sum(1 for c in changes if c["action"] == "REPLACE")
    removes = [c for c in changes if c["action"] == "REMOVE"]
    older = sum(1 for c in changes if c["note"] == "game save is NEWER")

    print()
    print(f"Summary: {adds} file(s) to copy in, {replaces} to replace, {len(removes)} only in the game save")

    if older:
        print(f"WARNING: {older} file(s) in your game save are NEWER than the backup you picked.")

    remove_extra = False

    if removes:
        answer = ask(f"Also delete the {len(removes)} file(s) from the game save that are not in that backup? [y/N]: ")
        remove_extra = answer.strip().lower() == "y"

    print("\nClose the game before restoring.")

    if ask("Type YES to restore: ").strip() != "YES":
        print("Cancelled.")
        return 1

    copied = apply_restore(game, settings, platform, device_name, save_set,
                           restore_files, dest_files, changes, remove_extra)

    if copied is None:
        return 1

    # Tell the sync logic we have deliberately chosen: the next backup may replace Current.
    active_root, _ = choose_backup_root(settings, platform)

    if active_root:
        digest = read_meta(active_root / safe_name(game["Name"])).get("digest")

        if digest:
            set_base(device_name, game["Name"], digest)

    print(f"\nRestore complete: {copied} file(s) copied"
          + (f", {len(removes)} removed" if remove_extra else "") + ".")
    print("Your next backup will see this as a change and keep the old Current as a version.")
    return 0


# ----------------------------------------------------------------------
# Commands: list / backup / sync
# ----------------------------------------------------------------------
def cmd_list(args, config, platform):
    settings = config.get("Settings", {})

    for number, game in enumerate(config["Games"], start=1):
        source = resolve_source(game, settings, platform)
        where = source if source else "(no path for this device)"
        print(f"{number:>3}. {game['Name']}  [{game.get('Platform', '')}]  {where}")

    return 0


def cmd_backup(args, config, platform):
    settings = config.get("Settings", {})
    games = config["Games"]
    device_name = get_device_name(settings, platform)

    if args.game:
        selected = find_games(games, args.game)

        if not selected:
            print(f"No game matched: {args.game}")
            return 1

        if len(selected) > 1:
            print(f"'{args.game}' matches several games, be more specific:")

            for game in selected:
                print(f"  - {game['Name']}")

            return 1
    else:
        selected = list(games)

    skip_names = {s.lower() for s in args.skip}
    final = []
    excluded = []

    for game in selected:
        if game["Name"].lower() in skip_names or platform in game.get("SkipOn", []):
            excluded.append(game["Name"])
            continue

        if args.platform and game.get("Platform", "").lower() != args.platform.lower():
            continue

        final.append(game)

    label = "Backup-" + (args.game if args.game else "AllGames")
    log_file = setup_logging(label, device_name)

    backup_root, using_fallback = choose_backup_root(settings, platform)

    if backup_root is not None and not using_fallback and not args.force_nas:
        log.info("Checking how fast the NAS is...")
        state, _, text = nas_state(settings, platform, 0, max(len(final), 1))
        log.info(text)
        fallback_root = get_roots(settings, platform)[1]

        if state != "fast" and fallback_root and reachable(fallback_root):
            log.warning("NAS is too slow right now - backing up to the LOCAL folder instead.")
            log.warning("Run Sync later (or launch the game) to copy it to the NAS. Use --force-nas to insist.")
            backup_root, using_fallback = fallback_root, True

    if backup_root is None:
        log.error("No usable backup folder for %s.", platform)
        log.error("Main folder not found (is the NAS mounted?) and no FallbackRoots entry works.")
        return 1

    names = [g["Name"] for g in games]

    for dup in sorted({n for n in names if names.count(n) > 1}):
        log.warning("Games.json has more than one entry called '%s'", dup)

    log.info("Device: %s (%s)", device_name, platform)
    log.info("Backup folder: %s", backup_root)

    if using_fallback:
        log.warning("Saving to the LOCAL backup folder (the NAS is unreachable or too slow). Run 'sync' when it is back.")

    log.info("Games to process: %d", len(final))

    for name in excluded:
        log.info("Excluded (SkipOn / --skip): %s", name)

    counts = {}
    failed = []

    for game in final:
        try:
            result = backup_game(game, settings, platform, backup_root, device_name, args.dry_run, using_fallback)
        except Exception:
            log.exception("FAILED: %s", game["Name"])
            failed.append(game["Name"])
            result = "failed"

        counts[result] = counts.get(result, 0) + 1

    log.info("")
    log.info("=" * 45)
    log.info("SUMMARY: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    if failed:
        log.info("Failed games: %s", ", ".join(failed))

    if counts.get("conflict"):
        log.info("%d game(s) have a NEWER save from another device and were NOT overwritten (see CONFLICT lines).", counts["conflict"])

    log.info("Log file: %s", log_file)

    return 1 if failed else 0


def copy_missing(src_dir, dst_dir):
    """Copies files that do not exist on the other side yet. Never deletes or overwrites."""
    copied = 0

    for relative, src in list_files(src_dir, [], []).items():
        target = Path(dst_dir) / relative

        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copied += 1

    return copied


def copy_game_data(local_game, main_game):
    """Current + Versions + meta + info only (NOT this device's shortcut / launcher files)."""
    if (local_game / "Current").exists():
        copy_missing(local_game / "Current", main_game / "Current")

    main_game.mkdir(parents=True, exist_ok=True)

    for extra in ("Save Information.txt", "meta.json"):          # meta.json right after Current: it says the copy is complete
        if (local_game / extra).exists():
            copy_atomic(local_game / extra, main_game / extra)

    try:
        push_versions(local_game, main_game)                    # older versions last: a problem here must not undo the save
    except OSError as error:
        log.warning("The older versions were not copied to the NAS (%s). They follow next time.", error)


def current_digest(game_folder, game_name):
    """
    Digest of a game's Current backup. Folders made by v12 have no meta.json, so it is
    worked out once and written down (marked adopted_from_v12) so the next run is quick.
    """
    meta = read_meta(game_folder)

    if meta.get("digest"):
        return meta["digest"]

    files = list_files(game_folder / "Current", [], [])

    if not files:
        return None

    digest = folder_digest(files)
    meta.setdefault("game", game_name)
    meta["digest"] = digest
    meta["adopted_from_v12"] = True

    try:
        with open(game_folder / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=4)
    except OSError:
        pass

    return digest


PUSH_MARKER = ".push-in-progress.json"


def backup_time(meta):
    """When a backup was made (seconds), or None for old v12 folders that have no record of it."""
    try:
        return datetime.fromisoformat(meta["last_backup"]).timestamp() if meta.get("last_backup") else None
    except (ValueError, KeyError):
        return None


def push_versions(local_game, nas_game, keep=DEFAULT_VERSION_COUNT):
    """
    Copies version folders the NAS does not have yet (newest first), each one whole or not at
    all (copied under a .partial name, then renamed). Lower priority than the save itself.
    """
    local_versions = local_game / "Versions"

    if not local_versions.exists():
        return 0

    nas_versions = nas_game / "Versions"
    nas_versions.mkdir(parents=True, exist_ok=True)

    for leftover in nas_versions.glob("*.partial"):
        shutil.rmtree(leftover, ignore_errors=True)

    folders = [f for f in local_versions.iterdir() if f.is_dir() and not f.name.endswith(".partial")]
    folders.sort(key=version_time, reverse=True)
    copied = 0

    failures = 0

    for folder in folders[:keep]:
        target = nas_versions / folder.name

        if target.exists():
            continue

        temp = nas_versions / (folder.name + ".partial")

        try:
            shutil.copytree(folder, temp)
            replace_retry(temp, target)
            copied += 1
        except OSError as error:
            shutil.rmtree(temp, ignore_errors=True)
            failures += 1
            log.warning("Could not copy the version %s to the NAS (%s). It will be tried again next time.", folder.name, error)

            if failures >= 2:                       # the share keeps refusing: stop here, the save itself is already safe
                break

    try:
        trim_versions(nas_versions, keep)
    except OSError as error:
        log.warning("Could not trim the old versions on the NAS (%s).", error)

    return copied


def push_to_nas(game_name, local_game, nas_game, device_name):
    """
    Copies only what changed from this device's local backup to the NAS (no old-version
    copying, nothing read over the network). An interrupted push is harmless: files are
    renamed into place, meta.json is written last, and a marker file tells other devices
    to wait. Returns pushed, same, nas_changed or needs_sync.
    """
    UPLOAD["stage"] = "checking the NAS copy"
    base = get_base(device_name, game_name)
    local_digest = read_meta(local_game).get("digest")
    has_nas = (nas_game / "Current").exists()
    nas_meta = read_meta(nas_game) if has_nas else {}
    nas_digest = nas_meta.get("digest")

    if not local_digest:
        return "needs_sync"

    if has_nas and not nas_digest:
        return "needs_sync"          # an old v12 folder: 'Sync' adopts it once

    if nas_digest == local_digest:
        set_base(device_name, game_name, local_digest)
        return "same"

    if nas_digest and nas_digest != base and nas_meta.get("last_device") != device_name:
        return "nas_changed"         # another device saved since we last synced - launch / restore decides

    local_time = backup_time(read_meta(local_game))
    nas_time = backup_time(nas_meta)

    # The NAS still holding exactly what this device synced last means the local copy is simply newer work.
    if has_nas and nas_digest != base and (local_time is None or (nas_time is not None and local_time <= nas_time + 1)):
        return "stale_local"         # the local copy is OLDER than what is on the NAS - never push it over

    UPLOAD["stage"] = "writing the in-progress marker"
    nas_game.mkdir(parents=True, exist_ok=True)
    marker = nas_game / PUSH_MARKER

    with open(marker, "w", encoding="utf-8") as f:
        json.dump({"device": device_name, "started": datetime.now().isoformat(timespec="seconds")}, f)

    mirror_folder(local_game / "Current", nas_game / "Current")

    for extra in ("Save Information.txt", "meta.json"):          # meta.json last
        if (local_game / extra).exists():
            UPLOAD["stage"] = f"writing {extra}"
            copy_atomic(local_game / extra, nas_game / extra)

    UPLOAD["stage"] = "finishing"
    marker.unlink()
    set_base(device_name, game_name, local_digest)
    return "pushed"


def mark_busy(nas_game, device_name):
    """Tells other devices 'an upload to this game is in progress' (they skip their cloud check while the marker is fresh)."""
    nas_game.mkdir(parents=True, exist_ok=True)
    marker = nas_game / PUSH_MARKER

    with open(marker, "w", encoding="utf-8") as f:
        json.dump({"device": device_name, "started": datetime.now().isoformat(timespec="seconds")}, f)

    return marker


def sync_game_folder(local_game, primary, device_name):
    """
    One game: push the local (fallback) backup to the NAS when that is safe.
    Returns (status, game name); status is pushed, same, nas_newer or conflict.
    """
    main_game = primary / local_game.name
    game_name = read_meta(local_game).get("game") or local_game.name
    base = get_base(device_name, game_name)
    local_digest = current_digest(local_game, game_name)
    has_main = (main_game / "Current").exists()
    main_digest = current_digest(main_game, game_name) if has_main else None

    if not has_main:
        marker = mark_busy(main_game, device_name)

        try:
            copy_game_data(local_game, main_game)
        finally:
            marker.unlink(missing_ok=True)

        if local_digest:
            set_base(device_name, game_name, local_digest)

        return "pushed", game_name

    if local_digest and local_digest == main_digest:
        push_versions(local_game, main_game)
        set_base(device_name, game_name, local_digest)
        return "same", game_name

    local_time = backup_time(read_meta(local_game))
    main_time = backup_time(read_meta(main_game))

    if main_digest and main_digest == base and (local_time is None or (main_time is not None and local_time <= main_time + 1)):
        return "stale_local", game_name          # an older local copy: leave the NAS alone

    if main_digest and main_digest == base:
        # Keep the NAS's old Current as a version first, like a normal backup does.
        author = safe_name(read_meta(main_game).get("last_device") or "unknown")
        stamp = make_stamp(author)

        while (main_game / "Versions" / stamp).exists():
            time.sleep(1)
            stamp = make_stamp(author)

        marker = mark_busy(main_game, device_name)

        try:
            shutil.copytree(main_game / "Current", main_game / "Versions" / stamp)
            mirror_folder(local_game / "Current", main_game / "Current")
            copy_game_data(local_game, main_game)
        finally:
            marker.unlink(missing_ok=True)

        if local_digest:
            set_base(device_name, game_name, local_digest)

        return "pushed", game_name

    if local_digest and local_digest == base:
        return "nas_newer", game_name

    if local_time is None:
        return "stale_local", game_name          # an old v12 folder: nothing to decide

    return "conflict", game_name


def cmd_sync(args, config, platform):
    """
    Pushes backups made while the NAS was unreachable or slow (the local fallback) to the NAS.
      - game not on the NAS yet            -> copied
      - NAS unchanged since we last synced -> NAS Current replaced by ours
      - NAS has a newer save               -> left alone (launch / restore will pull it)
      - both changed                       -> left alone and listed, nothing is lost
    """
    settings = config.get("Settings", {})
    device_name = get_device_name(settings, platform)
    setup_logging("Sync", device_name)
    primary, fallback = get_roots(settings, platform)

    if not primary or not fallback:
        log.error("Sync needs both BackupRoots and FallbackRoots set for %s.", platform)
        return 1

    if not reachable(primary):
        log.error("Main backup folder not reachable: %s", primary)
        return 1

    if not reachable(fallback):
        log.info("No local fallback folder yet - nothing to sync.")
        return 0

    log.info("Comparing LOCAL backups (%s)", fallback)
    log.info("        with NAS backups   (%s)", primary)
    log.info("(Your original game save folders are not touched by sync.)")
    log.info("")
    pushed = 0
    nas_newer = []
    conflicts = []
    failed = []
    stale = 0
    folders = [f for f in sorted(fallback.iterdir()) if (f / "Current").exists()]

    for number, local_game in enumerate(folders, start=1):
        log.info("[%d/%d] %s", number, len(folders), local_game.name)

        try:
            status, game_name = sync_game_folder(local_game, primary, device_name)
        except Exception:
            log.exception("FAILED: %s (the other games are still being synced)", local_game.name)
            failed.append(local_game.name)
            continue

        if status == "pushed":
            log.info("    local backup copied to the NAS")
            pushed += 1
        elif status == "nas_newer":
            nas_newer.append(game_name)
        elif status == "stale_local":
            stale += 1
        elif status == "conflict":
            conflicts.append(game_name)

    log.info("")
    log.info("Sync finished: %d game(s) pushed to the NAS", pushed)

    if stale:
        log.info("%d old local backup(s) were OLDER than the NAS copies and were left alone.", stale)

    for game_name in nas_newer:
        log.info("NAS already has a newer save (will be pulled when you launch / restore): %s", game_name)

    for game_name in conflicts:
        log.warning("CONFLICT - both changed, left alone: %s  (use Restore and pick the one you want)", game_name)

    if failed:
        log.warning("COULD NOT SYNC (see the log above, they are tried again next time): %s", "; ".join(failed))

    return 1 if failed else 0


# ----------------------------------------------------------------------
# Launch wrapper (cloud-save style): check before the game, back up after
# ----------------------------------------------------------------------
def prompt_choice(title, message, labels):
    """
    Asks the user to pick one of the labels. Returns the index, or None if
    there was no way to ask. Uses the terminal if there is one, otherwise a
    pop-up (kdialog / zenity on the Deck, a Windows message box on the PC).
    """
    if sys.stdin is not None and sys.stdin.isatty() and not launched_by_steam():
        print()
        print(message)
        print()

        for number, label in enumerate(labels, start=1):
            print(f"  {number}. {label}")

        while True:
            answer = ask("Select: ").strip()

            if answer.isdigit() and 1 <= int(answer) <= len(labels):
                return int(answer) - 1

            if answer == "":
                return None

    if shutil.which("kdialog"):
        command = ["kdialog", "--title", title, "--yes-label", labels[0], "--no-label", labels[1]]

        if len(labels) > 2:
            command += ["--cancel-label", labels[2], "--yesnocancel", message]
        else:
            command += ["--yesno", message]

        try:
            return {0: 0, 1: 1, 2: 2}.get(subprocess.run(command, timeout=120).returncode)
        except subprocess.TimeoutExpired:
            return None

    if shutil.which("zenity"):
        command = ["zenity", "--question", "--title", title, "--text", message,
                   "--ok-label", labels[0], "--cancel-label", labels[1], "--width", "500"]

        try:
            return 0 if subprocess.run(command, timeout=120).returncode == 0 else 1
        except subprocess.TimeoutExpired:
            return None

    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        text = message + "\n\nYes = " + labels[0] + "\nNo = " + labels[1]

        if len(labels) > 2:
            text += "\nCancel = " + labels[2]

        answer = messagebox.askyesnocancel(title, text)
        root.destroy()

        if answer is True:
            return 0

        if answer is False:
            return 1

        return 2 if len(labels) > 2 else 1
    except Exception:
        return None


def describe_side(count, size, newest):
    return f"newest file {format_time(newest)}, {count} file(s), {human_size(size)}"


def pre_launch_check(game, settings, platform, device_name):
    """
    Cloud-save style check before the game starts. Returns True to carry on
    launching, False to cancel the launch.
      - Other device saved since we last synced, and we did not change anything -> pull it
      - We changed it, other device did not                                      -> keep ours
      - BOTH changed (or no history yet and theirs is newer)                      -> ask
    ConflictPolicy (per game or in Settings): Ask (default), AutoNewest, AlwaysLocal.
    """
    save_set = get_save_set(game, settings, platform)

    if not save_set:
        return True

    name = game["Name"]
    root, using_fallback = choose_backup_root(settings, platform)

    if root is None or using_fallback:
        log.warning("Main backup folder (NAS) not reachable - skipping the cloud check.")
        return True

    game_folder = root / safe_name(name)
    current = game_folder / "Current"

    if not current.exists():
        log.info("No cloud backup of this game yet.")
        return True

    marker = game_folder / PUSH_MARKER

    if marker.exists() and time.time() - marker.stat().st_mtime < 7200:
        log.warning("Another device is still uploading this save - skipping the cloud check this time.")
        return True

    include = game.get("Include", [])
    exclude = list(settings.get("GlobalExclude", [])) + list(game.get("Exclude", []))
    remote_files = list_files(current, include, exclude)

    if not remote_files:
        return True

    meta = read_meta(game_folder)
    remote_digest = meta.get("digest") or folder_digest(remote_files)
    remote_device = meta.get("last_device") or "another device"
    local_files = save_set.files(include, exclude)
    local_digest = folder_digest(local_files) if local_files else None
    base = get_base(device_name, name)

    if local_digest == remote_digest:
        log.info("This device already has the latest save.")
        set_base(device_name, name, remote_digest)
        return True

    if local_files and base == remote_digest:
        log.info("This device has changes that are not backed up yet - they will be backed up after the game.")
        return True

    remote_newest = max(p.stat().st_mtime for p in remote_files.values())
    local_newest = max((p.stat().st_mtime for p in local_files.values()), default=None)
    local_size = sum(p.stat().st_size for p in local_files.values())

    if not local_files:
        use_remote = True
        log.info("No local save found - pulling the save from %s.", remote_device)
    elif base is not None and base == local_digest and remote_newest > local_newest + TIME_TOLERANCE_SECONDS:
        use_remote = True
        log.info("%s saved a newer version and nothing changed here - pulling it.", remote_device)
    else:
        both_changed = base is not None

        if not both_changed and remote_newest <= local_newest + TIME_TOLERANCE_SECONDS:
            log.info("No sync history yet, and this device's save is newer - keeping it.")
            set_base(device_name, name, remote_digest)
            return True

        policy = game.get("ConflictPolicy") or settings.get("ConflictPolicy", "Ask")

        if policy == "AlwaysLocal":
            log.info("ConflictPolicy AlwaysLocal - keeping this device's save.")
            set_base(device_name, name, remote_digest)
            return True

        if policy == "AutoNewest":
            use_remote = remote_newest > local_newest
            log.info("ConflictPolicy AutoNewest - %s.", "using the cloud save" if use_remote else "keeping this device's save")
        else:
            why = ("Both devices changed this save since they last synced."
                   if both_changed else "A newer save was found in the cloud.")
            message = (f"{name}\n\n{why}\n\n"
                       f"This device ({device_name}):\n  {describe_side(len(local_files), local_size, local_newest)}\n\n"
                       f"Cloud save from {remote_device} (backed up {format_time(read_last_backup(game_folder))}):\n"
                       f"  {describe_side(len(remote_files), sum(p.stat().st_size for p in remote_files.values()), remote_newest)}\n\n"
                       f"Whichever you do not pick is kept in Versions.")
            labels = [f"Use the cloud save ({remote_device})", "Keep this device's save", "Cancel launch"]
            choice = prompt_choice(f"Save conflict - {name}", message, labels)

            if choice == 2:
                log.info("Launch cancelled by the user.")
                return False

            if choice is None:
                log.warning("Could not ask which save to use - keeping this device's save for now.")
                return True

            use_remote = choice == 0

    if not use_remote:
        log.info("Keeping this device's save (the cloud copy stays in Versions after the next backup).")
        set_base(device_name, name, remote_digest)
        return True

    changes = describe_restore(remote_files, local_files)
    copied = apply_restore(game, settings, platform, device_name, save_set,
                           remote_files, local_files, changes, True)

    if copied is None:
        log.warning("Could not take a safety snapshot - keeping this device's save.")
    else:
        log.info("Pulled %d file(s) from %s.", copied, remote_device)
        set_base(device_name, name, remote_digest)

    return True


def process_running(name):
    """Windows only: is a program with this name still running? (like v12's Get-Process)"""
    if not sys.platform.startswith("win"):
        return False

    exe = name if name.lower().endswith(".exe") else name + ".exe"
    result = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
                            capture_output=True, text=True)
    output = result.stdout.strip().lower()

    return output != "" and "no tasks are running" not in output


def direct_backup(game, settings, platform, device_name, force):
    """
    Backs up straight to the NAS (used with --force-nas / ForceNAS, or when there is no local
    folder). Falls back to local when the NAS is unreachable or too slow, unless forced.
    """
    primary, fallback = get_roots(settings, platform)
    save_set = get_save_set(game, settings, platform)
    include = game.get("Include", [])
    exclude = list(settings.get("GlobalExclude", [])) + list(game.get("Exclude", []))
    files = save_set.files(include, exclude) if save_set and save_set.exists() else {}
    total = sum(p.stat().st_size for p in files.values())
    local_ok = bool(fallback and reachable(fallback))

    if force:
        state, nas_root, text = ("fast" if primary and reachable(primary) else "down"), primary, "Forced NAS backup (no speed limit)"
    else:
        state, nas_root, text = nas_state(settings, platform, total, len(files))

    log.info(text)

    if state == "fast":
        timeout = float(settings.get("BackupTimeoutSeconds", 600 if force else 90))
        finished, result = run_with_timeout(
            lambda: backup_game(game, settings, platform, nas_root, device_name, False, False), timeout)

        if finished:
            return result, "NAS direct"

        log.warning("The backup to the NAS did not finish within %d seconds.", timeout)
        state = "slow"

    if local_ok:
        reason = "not reachable" if state == "down" else "too slow right now"
        log.warning("NAS %s - saving to the local backup folder instead.", reason)
        fallback.mkdir(parents=True, exist_ok=True)
        return backup_game(game, settings, platform, fallback, device_name, False, True), "pending (NAS " + reason + ")"

    log.error("NAS %s and there is no local backup folder - save NOT backed up.", state)
    return "failed", "none"


PROGRESS = {"window": None}


class ProgressWindow:
    """A small 'working on it' window (zenity) so you can see that a backup is running after the game closes."""

    def __init__(self, text):
        self.process = None

        if shutil.which("zenity"):
            try:
                self.process = subprocess.Popen(
                    ["zenity", "--progress", "--pulsate", "--no-cancel", "--auto-close", "--width=420",
                     "--title", "Game Save Manager", "--text", text],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
            except OSError:
                self.process = None

    def update(self, text):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write("# " + text.replace("\n", " ") + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError):
                pass

    def close(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write("100\n")
                self.process.stdin.flush()
                self.process.stdin.close()
            except (OSError, ValueError):
                pass

            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


def open_progress(settings, text):
    """Opens the window when Settings.ShowProgressWindow allows it (default: on, except on Windows)."""
    wanted = settings.get("ShowProgressWindow", not sys.platform.startswith("win"))
    PROGRESS["window"] = ProgressWindow(text) if wanted else None


def show_status(text):
    """Writes a status line to the log and, if open, to the progress window."""
    log.info(text)

    if PROGRESS["window"]:
        PROGRESS["window"].update(text)


def close_progress():
    if PROGRESS["window"]:
        PROGRESS["window"].close()
        PROGRESS["window"] = None


def seed_local_from_nas(game, settings, platform, local_game, nas_game):
    """
    First local backup of a game on this device while the NAS already holds a DIFFERENT save of it: copy the NAS's
    Current down first, so the new save turns it into a proper Version. Without this the upload would replace the
    NAS copy and nothing anywhere would remember the older save. Does nothing in every other case.
    """
    if (local_game / "Current").exists() or not (nas_game / "Current").exists():
        return False

    nas_meta = read_meta(nas_game)

    if not nas_meta.get("digest"):               # an old v12 folder: Sync adopts those
        return False

    save_set = get_save_set(game, settings, platform)

    if save_set and save_set.exists():
        include = game.get("Include", [])
        exclude = list(settings.get("GlobalExclude", [])) + list(game.get("Exclude", []))
        files = save_set.files(include, exclude)

        if files and folder_digest(files) == nas_meta["digest"]:
            return False                         # the NAS already has exactly this save

    local_game.mkdir(parents=True, exist_ok=True)
    temp = local_game / "Current.seed"
    shutil.rmtree(temp, ignore_errors=True)
    shutil.copytree(nas_game / "Current", temp)
    os.replace(temp, local_game / "Current")      # renaming a folder works because the target does not exist
    shutil.copy2(nas_game / "meta.json", local_game / "meta.json")
    log.info("Fetched the NAS's current save first, so it is kept as a version when this one replaces it.")
    return True


def seed_before_backup(game, settings, platform, primary, fallback):
    """Runs seed_local_from_nas with a time limit and never lets a problem stop the backup."""
    if not (primary and fallback and reachable(primary)):
        return

    name = safe_name(game["Name"])

    try:
        run_with_timeout(lambda: seed_local_from_nas(game, settings, platform, fallback / name, primary / name),
                         float(settings.get("NasPushSeconds", 25)) * 2)
    except Exception:
        log.exception("Could not fetch the NAS copy first (continuing without it)")


def post_game_backup(game, settings, platform, device_name, force_nas=False):
    """
    After the game closes: save to the LOCAL folder first (fast, so the save is safe at once),
    then copy what changed to the NAS, waiting at most Settings.NasPushSeconds (default 25).
    If the upload is not done in time we stop waiting; the next launch with a good connection
    (or Sync) finishes it. Returns (result, NAS status text).
    """
    primary, fallback = get_roots(settings, platform)
    force = force_nas or bool(game.get("ForceNAS"))

    if force or not (fallback and reachable(fallback)):
        return direct_backup(game, settings, platform, device_name, force)

    show_status(f"Saving {game['Name']} ...")
    seed_before_backup(game, settings, platform, primary, fallback)
    result = backup_game(game, settings, platform, fallback, device_name, False, True)

    if not primary or not reachable(primary):
        log.warning("NAS not reachable - the save is backed up locally and will be copied to the NAS later.")
        return result, "pending (NAS not reachable)"

    budget = float(settings.get("NasPushSeconds", 25))
    show_status(f"Saved. Uploading to the NAS (up to {budget:.0f} seconds) ...")
    name = game["Name"]
    finished, status = run_with_timeout(
        lambda: push_to_nas(name, fallback / safe_name(name), primary / safe_name(name), device_name), budget)

    if not finished:
        stage = UPLOAD["stage"]
        log.warning("The upload is taking longer than %d seconds - stopped waiting (it was: %s).", budget, stage)
        log.warning("The save is safe locally. It is copied to the NAS at the next launch with a good connection, or by Sync.")
        return result, f"pending (upload too slow - was {stage})"

    log.info("NAS: %s", status)

    if status in ("pushed", "same"):
        keep = int(game.get("VersionCount") or settings.get("VersionCount", DEFAULT_VERSION_COUNT))
        v_budget = float(settings.get("NasVersionSeconds", 10))
        done, _ = run_with_timeout(
            lambda: push_versions(fallback / safe_name(name), primary / safe_name(name), keep), v_budget)

        if not done:
            log.info("Older versions are still copying to the NAS - they will finish later. The latest save is already there.")

    return result, status


def notify(title, message):
    """Best-effort desktop pop-up (may not show in gaming mode)."""
    try:
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], timeout=5)
        elif shutil.which("kdialog"):
            subprocess.run(["kdialog", "--title", title, "--passivepopup", message, "5"], timeout=10)
    except Exception:
        pass


def detect_game_from_command(command_or_text, games):
    """
    Emulator shortcuts launch ONE rom, and the rom's file name usually contains its Switch title ID
    (...[0100C1F0051B6000].nsp). Returns the matching 'Switch - <game>' entry, or None.
    """
    text = " ".join(command_or_text) if isinstance(command_or_text, (list, tuple)) else str(command_or_text)

    for match in re.finditer(r"(?<![0-9A-Fa-f])(01[0-9A-Fa-f]{14})(?![0-9A-Fa-f])", text):
        title_id = match.group(1).upper()

        for game in games:
            if str(game.get("SwitchTitleID", "")).upper() == title_id:
                return game

    return None


def cmd_launch(args, config, platform):
    """
    python savemanager.py launch [--force-nas] "Game Name" -- <the game's command>
    A problem in the backup logic never stops the game from starting. In gaming mode the
    Steam button stays on Stop until this has finished, then returns to Play.
    """
    settings = config.get("Settings", {})
    device_name = get_device_name(settings, platform)
    command = list(args.command)

    if command and command[0] == "--":
        command = command[1:]

    if not command:
        print("Nothing to launch. Usage: launch \"Game Name\" -- <command>")
        return 1

    log_file = setup_logging("Launch-" + args.game, device_name)
    matches = find_games(config["Games"], args.game) if args.game.lower() != "auto" else []
    game = matches[0] if len(matches) == 1 else None
    detected = detect_game_from_command(command, config["Games"])

    # An emulator shortcut that names a whole-folder entry (or "auto") is switched to the per-game entry
    # when the rom's title ID is in the command line.
    if detected is not None and (game is None or (game.get("Platform") == "Emulators" and not game.get("Virtual")
                                                  and "switch" in game["Name"].lower())):
        log.info("Detected the Switch game from the rom name: %s", detected["Name"])
        game = detected

    # Windows games often expect to start in their own folder.
    cwd = None

    if sys.platform.startswith("win") and Path(command[0]).is_file():
        cwd = Path(command[0]).parent

    if game is None:
        log.error("Game '%s' not found (or matched several) in Games.json - launching without save sync.", args.game)
        return subprocess.run(command, cwd=cwd).returncode

    game = newest_switch_copy(game, platform)
    name = game["Name"]
    started = datetime.now()
    record_played(device_name, name)
    log.info("Launching %s on %s", name, device_name)

    def before_the_game():
        """Everything that happens before the game starts. Returns False to cancel the launch."""
        log.info("Step 1/3: checking the NAS ...")
        state, root, text = nas_state(settings, platform)
        log.info(text)

        if state != "fast":
            log.warning("NAS %s - skipping the cloud check before launch.", "not reachable" if state == "down" else "too slow")
            return True

        primary, fallback = get_roots(settings, platform)
        local_game = fallback / safe_name(name) if fallback else None

        # A save that was only backed up locally last time goes to the NAS first.
        if local_game and reachable(fallback) and (local_game / "Current").exists():
            log.info("Step 2/3: sending the save that was waiting for the NAS ...")
            budget = float(settings.get("NasPushSeconds", 25))
            finished, status = run_with_timeout(
                lambda: push_to_nas(name, local_game, primary / safe_name(name), device_name), budget)
            log.info("Pending local backup: %s", status if finished else "still uploading - skipped for now")

        log.info("Step 3/3: looking for a newer save from another device ...")
        return pre_launch_check(game, settings, platform, device_name) is not False

    skip_checks = bool(game.get("SkipLaunchChecks") or settings.get("SkipLaunchChecks"))

    if skip_checks:
        log.info("Launch checks are switched off (SkipLaunchChecks) - starting the game.")
    else:
        try:
            limit = float(settings.get("PreLaunchMaxSeconds", 120))
            finished, proceed = run_with_timeout(before_the_game, limit)

            if not finished:
                log.warning("The checks before the game took more than %d seconds - starting the game anyway.", limit)
            elif proceed is False:
                log.info("Launch cancelled by the user.")
                return 1
        except Exception:
            log.exception("Pre-launch check failed - launching anyway")

    if game.get("SwitchCopies"):
        try:
            keep_copies_in_step(game, settings, platform, device_name)       # the emulator you start has the newest save
        except Exception:
            log.exception("Could not bring the emulators' copies in step - starting anyway")

    stop_timer = start_timer_backups(game, settings, platform, device_name)
    exit_code = subprocess.run(command, cwd=cwd).returncode

    if stop_timer is not None:
        stop_timer.set()

    closed = datetime.now()
    log.info("Game command finished (exit code %s)", exit_code)

    # Some launchers hand over to another program and exit straight away.
    names = process_names(game)

    if names and sys.platform.startswith("win"):
        time.sleep(10)

        while process_running_any(names):
            time.sleep(5)

    open_progress(settings, f"Saving {name} ...")
    time.sleep(int(settings.get("WaitAfterGameExitSeconds", 5)))
    result, nas_status = "failed", "not attempted"

    try:
        BACKUP_LOCK.acquire(timeout=30)      # let a timed backup that is mid-way finish first

        try:
            result, nas_status = post_game_backup(game, settings, platform, device_name, args.force_nas)
        finally:
            if BACKUP_LOCK.locked():
                BACKUP_LOCK.release()

        notes = {"backed_up": "Save backed up.", "unchanged": "Save already up to date.",
                 "conflict": "CONFLICT - newer save from another device, not overwritten.",
                 "missing": "Save folder not found.", "refused": "Backup refused (see log).",
                 "failed": "Backup failed (see log)."}
        notify(name, notes.get(result, str(result)) + " NAS: " + str(nas_status))
    except Exception:
        log.exception("Post-game backup FAILED")

    show_status("Done.")
    close_progress()
    finished = datetime.now()

    # A tiny summary you can read (or share) instead of the whole log.
    try:
        summary = [f"Game: {name}", f"Device: {device_name}",
                   f"Started: {started:%d-%m-%Y %H:%M:%S}", f"Game closed: {closed:%H:%M:%S}",
                   f"Backup finished: {finished:%H:%M:%S}  ({(finished - closed).seconds} seconds after the game closed)",
                   f"Local/direct backup result: {result}", f"NAS: {nas_status}", f"Full log: {log_file}"]

        with open(log_file.parent / "last-run.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(summary) + "\n")
    except OSError:
        pass

    return exit_code


def group_games(config, platform, group):
    """Games on this device whose category (or platform) equals the group name, e.g. 'Switch'."""
    settings = config.get("Settings", {})
    state = load_menu_state(get_device_name(settings, platform))
    wanted = group.strip().lower()
    result = []

    for game in available_games(config["Games"], settings, platform):
        names = [c.lower() for c in game_categories(game, state)] + [str(game.get("Platform", "")).lower()]

        if wanted in names:
            result.append(game)

    return result


def backup_group(games, settings, platform, device_name):
    """Backs up every game in the group locally (quietly), then uploads what changed within the time limit."""
    primary, fallback = get_roots(settings, platform)
    results = {}

    if not (fallback and reachable(fallback)):
        root, using_fallback = choose_backup_root(settings, platform)

        if root is None:
            log.error("No backup folder available - nothing backed up.")
            return results

        for game in games:
            results[game["Name"]] = backup_game(game, settings, platform, root, device_name, False, using_fallback, True)

        return results

    for game in games:
        try:
            seed_before_backup(game, settings, platform, primary, fallback)
            results[game["Name"]] = backup_game(game, settings, platform, fallback, device_name, False, True, True)
        except Exception:
            log.exception("FAILED: %s", game["Name"])
            results[game["Name"]] = "failed"

    if primary and reachable(primary):
        deadline = time.time() + float(settings.get("NasPushSeconds", 25))
        ordered = sorted(games, key=lambda g: results.get(g["Name"]) != "backed_up")

        for game in ordered:
            name = game["Name"]
            remaining = deadline - time.time()

            if remaining <= 0:
                log.warning("Out of upload time - the rest is copied to the NAS next time.")
                break

            if not (fallback / safe_name(name) / "Current").exists():
                continue

            finished, status = run_with_timeout(
                lambda: push_to_nas(name, fallback / safe_name(name), primary / safe_name(name), device_name), remaining)

            if not finished:
                log.warning("Upload still running for %s - stopped waiting.", name)
                break

            if status == "pushed":
                log.info("NAS updated: %s", name)

    return results


def cmd_launchgroup(args, config, platform):
    """
    python savemanager.py launchgroup Switch -- <command>
    One launcher for a whole group of games (every Switch title for a generic emulator shortcut):
    checks each game for a newer save first, backs them all up every BackupIntervalMinutes while the
    emulator runs, and once more when it closes. Only changed games do any work.
    """
    settings = config.get("Settings", {})
    device_name = get_device_name(settings, platform)
    command = list(args.command)

    if command and command[0] == "--":
        command = command[1:]

    if not command:
        print('Nothing to launch. Usage: launchgroup Switch -- <command>')
        return 1

    log_file = setup_logging("LaunchGroup-" + args.group, device_name)
    games = group_games(config, platform, args.group)
    cwd = Path(command[0]).parent if sys.platform.startswith("win") and Path(command[0]).is_file() else None
    started = datetime.now()
    log.info("Group '%s' on %s: %d game(s) on this device", args.group, device_name, len(games))

    if not games:
        log.warning("No games in that group here - launching without save sync.")
        return subprocess.run(command, cwd=cwd).returncode

    try:
        state, root, text = nas_state(settings, platform)
        log.info(text)

        if state == "fast":
            deadline = time.time() + float(settings.get("GroupCheckSeconds", 20))
            checked = 0

            for game in games:
                if time.time() > deadline:
                    log.warning("Check time used up - %d of %d games checked.", checked, len(games))
                    break

                if pre_launch_check(newest_switch_copy(game, platform), settings, platform, device_name) is False:
                    log.info("Launch cancelled by the user.")
                    return 1

                checked += 1
        else:
            log.warning("NAS %s - skipping the cloud check before launch.", "not reachable" if state == "down" else "too slow")
    except Exception:
        log.exception("Pre-launch check failed - launching anyway")

    in_step = 0

    for game in games:
        if game.get("SwitchCopies"):
            try:
                in_step += keep_copies_in_step(game, settings, platform, device_name)
            except Exception:
                log.exception("%s: could not bring the emulators' copies in step", game["Name"])

    if in_step:
        log.info("Brought %d emulator cop%s up to the newest save.", in_step, "y" if in_step == 1 else "ies")

    minutes = float(settings.get("BackupIntervalMinutes", 0) or 0)
    stop = threading.Event()

    if minutes > 0:
        def loop():
            while not stop.wait(minutes * 60):
                try:
                    with BACKUP_LOCK:
                        log.info("Timed group backup...")
                        results = backup_group(games, settings, platform, device_name)
                        log.info("Timed group backup: %s", ", ".join(f"{k}={v}" for k, v in sorted(count_results(results).items())))
                except Exception:
                    log.exception("Timed group backup failed")

        threading.Thread(target=loop, daemon=True).start()

    exit_code = subprocess.run(command, cwd=cwd).returncode
    stop.set()
    closed = datetime.now()
    log.info("Command finished (exit code %s)", exit_code)
    time.sleep(int(settings.get("WaitAfterGameExitSeconds", 5)))
    summary = {}

    try:
        BACKUP_LOCK.acquire(timeout=30)

        try:
            summary = count_results(backup_group(games, settings, platform, device_name))
        finally:
            if BACKUP_LOCK.locked():
                BACKUP_LOCK.release()

        log.info("Final group backup: %s", ", ".join(f"{k}={v}" for k, v in sorted(summary.items())))
    except Exception:
        log.exception("Final group backup FAILED")

    try:
        lines = [f"Group: {args.group}", f"Device: {device_name}", f"Started: {started:%d-%m-%Y %H:%M:%S}",
                 f"Closed: {closed:%H:%M:%S}", f"Backup finished: {datetime.now():%H:%M:%S}",
                 "Results: " + ", ".join(f"{k}={v}" for k, v in sorted(summary.items())), f"Full log: {log_file}"]

        with open(log_file.parent / "last-run.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass

    return exit_code


def count_results(results):
    counts = {}

    for value in results.values():
        counts[value] = counts.get(value, 0) + 1

    return counts


STEAM_ID64_BASE = 76561197960265728


def steam_root(settings, platform):
    """Where Steam is installed (registry first, then Settings.Variables.SteamRoot, then the default)."""
    if sys.platform.startswith("win"):
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                path = Path(winreg.QueryValueEx(key, "SteamPath")[0])

            if path.exists():
                return path
        except Exception:
            pass

    value = settings.get("Variables", {}).get(platform, {}).get("SteamRoot", "C:/Program Files (x86)/Steam")
    return Path(expand_path(value, settings, platform))


def child_dirs(path, pattern=None):
    try:
        return sorted(d.name for d in Path(path).iterdir()
                      if d.is_dir() and (pattern is None or re.fullmatch(pattern, d.name)))
    except OSError:
        return []


def cmd_ids(args, config, platform):
    """
    Finds your own IDs on this PC (Steam user ID, Rockstar profile, Ubisoft ID ...) and writes them
    into MyIDs.json, so every {placeholder} in Games.json is filled in one go.
    """
    settings = config.get("Settings", {})

    if platform != "Windows":
        print("Run this on the Windows PC (the IDs are found in Windows folders). The Deck gets its paths from findsaves.")
        return 0

    env = lambda name: Path(os.environ[name]) if os.environ.get(name) else None
    profile = env("USERPROFILE") or Path.home()
    appdata = env("APPDATA") or profile / "AppData" / "Roaming"
    local = env("LOCALAPPDATA") or profile / "AppData" / "Local"
    docs_options = [profile / "Documents", profile / "OneDrive" / "Documents"]
    program_files = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    steam = steam_root(settings, platform)

    try:
        with open(MYIDS_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}

    def docs_children(*parts):
        found = []

        for docs in docs_options:
            found += child_dirs(docs.joinpath(*parts))

        return found

    def rockstar(chosen):
        names = []

        for docs in docs_options:
            for game in child_dirs(docs / "Rockstar Games"):
                names += child_dirs(docs / "Rockstar Games" / game / "Profiles", r"[0-9A-Fa-f]{8}")

        return sorted(set(names))

    steps = [
        ("SteamUserID", "Steam user ID (the number folder inside Steam's userdata)",
         lambda c: [n for n in child_dirs(steam / "userdata", r"\d+") if n != "0"]),
        ("SteamID64", "Steam ID64 (worked out from the Steam user ID)",
         lambda c: [str(int(c["SteamUserID"]) + STEAM_ID64_BASE)] if c.get("SteamUserID") else []),
        ("RockstarProfile", "Rockstar profile folder", rockstar),
        ("UbisoftID", "Ubisoft user ID",
         lambda c: child_dirs(program_files / "Ubisoft" / "Ubisoft Game Launcher" / "savegames", r"[0-9A-Fa-f-]{36}")),
        ("DarkSouls2ID", "Dark Souls II save ID", lambda c: child_dirs(appdata / "DarkSoulsII", r"[0-9A-Fa-f]{16}")),
        ("MegaMan2ID", "Mega Man Legacy Collection 2 ID", lambda c: child_dirs(appdata / "MegaMan2")),
        ("WarframeID", "Warframe ID", lambda c: child_dirs(local / "Warframe", r"[0-9A-Fa-f]{32}")),
        ("HaloMCCID", "Halo MCC ID",
         lambda c: child_dirs(steam / "userdata" / c.get("SteamUserID", "0") / "976730" / "remote", r"\d+")),
        ("RaymanProfile", "Rayman Legends profile folder", lambda c: docs_children("Rayman Legends")),
    ]

    print()
    print("Looking for your IDs on this PC. Press Enter to skip any game you do not have.")
    chosen = {}
    report = []

    for name, label, finder in steps:
        candidates = finder(chosen)
        current = str(data.get(name, ""))
        value = None

        if len(candidates) == 1:
            value, how = candidates[0], "found"
        elif len(candidates) > 1:
            print()
            print(f"{label}: more than one found")

            for number, candidate in enumerate(candidates, start=1):
                print(f"  {number}  {candidate}")

            pick = ask("Which is yours? (number, Enter = skip): ").strip()

            if pick.isdigit() and 1 <= int(pick) <= len(candidates):
                value, how = candidates[int(pick) - 1], "picked"
        else:
            if current and not current.startswith("YOUR_"):
                value, how = current, "kept"
            else:
                typed = ask(f"{label}: not found. Type it (Enter = skip): ").strip()

                if typed:
                    value, how = typed, "typed"

        if value:
            chosen[name] = value
            data[name] = value
            report.append((name, value, how))
        else:
            data.setdefault(name, "YOUR_" + name.upper())
            report.append((name, "-", "not set"))

    data.setdefault("_note", "Your own IDs. Filled in by: python savemanager.py ids. Lines still starting YOUR_ are ignored.")

    with open(MYIDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

    _MYIDS_CACHE.clear()
    print()
    print("Result (saved in MyIDs.json):")

    for name, value, how in report:
        print(f"  {name:<16} {value:<40} {how}")

    print()
    print("Games that use an ID marked 'not set' will say so when you back up. Run this again any time.")
    return 0


# ----------------------------------------------------------------------
# Adding games (edits Games.json safely)
# ----------------------------------------------------------------------
CATALOGUE_FILE = SCRIPT_DIR / "Games.catalogue.json"


def source_key(platform):
    """Which Games.json key holds the save folder for this device."""
    return "Source" if platform == "Windows" else "Source" + platform


def read_games_json():
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        text = f.read()

    had_notes = any(line.lstrip().startswith("#") for line in text.splitlines())
    clean = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return json.loads(clean), had_notes


def edit_games_json(change):
    """
    Safely changes Games.json: change(data) edits the data and returns False to cancel.
    A dated copy (Games.json.bak-...) is kept first (the newest 20 are kept in Backups/Games.json).
    Returns the backup path, or None if nothing was written.
    """
    data, had_notes = read_games_json()

    if had_notes:
        print("Note: your Games.json has # note lines. Saving from here removes them (a backup copy is kept).")

        if ask("Continue? [y/N]: ").strip().lower() != "y":
            return None

    if change(data) is False:
        return None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    BACKUP_GAMES.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_GAMES / f"Games.json.bak-{stamp}"
    shutil.copy2(CONFIG_PATH, backup)

    for old in sorted(BACKUP_GAMES.glob("Games.json.bak-*"))[:-KEEP_GAMES_BACKUPS]:
        old.unlink()

    temp = CONFIG_PATH.with_name("Games.json.tmp")

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

    os.replace(temp, CONFIG_PATH)
    return backup


def find_raw_game(data, name):
    for game in data["Games"]:
        if str(game.get("Name", "")).lower() == name.lower():
            return game

    return None


def set_game_field(name, key, value):
    """Sets one field of an existing game. Returns the backup path, or None if the game was not found."""
    def change(data):
        game = find_raw_game(data, name)

        if game is None:
            return False

        game[key] = value

    return edit_games_json(change)


def portable_path(path, platform):
    """Writes this PC's own folders as %USERPROFILE% / %APPDATA% / %LOCALAPPDATA% so the entry is tidy and portable."""
    text = str(path)

    if platform != "Windows":
        return text

    for variable in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        value = os.environ.get(variable)

        if value and text.lower().startswith(value.lower()):
            return f"%{variable}%" + text[len(value):]

    return text


def reload_config(config, platform):
    fresh = expand_switch(load_config(), platform)
    config.clear()
    config.update(fresh)


def unresolved_variables(entry, settings, platform):
    """{Names} in an entry that have no real value yet (they need MyIDs.json / Settings.Variables)."""
    variables = dict(settings.get("Variables", {}).get(platform, {}))
    variables.update(load_my_ids())
    missing = set()

    def check(text, depth=0):
        for name in re.findall(r"\{(\w+)\}", text):
            value = variables.get(name)

            if value is None or str(value).startswith("YOUR_"):
                missing.add(name)
            elif depth < 4:
                check(str(value), depth + 1)

    def walk(value):
        if isinstance(value, str):
            check(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(entry)
    return sorted(missing)


def after_adding(entry, config, platform):
    """Reload, then tell her if the new game still needs one of her IDs."""
    reload_config(config, platform)
    needed = unresolved_variables(entry, config.get("Settings", {}), platform)

    if needed:
        print()
        print("This game's path needs: " + ", ".join(needed))

        if platform == "Windows" and ask("Find those IDs on this PC now? [Y/n]: ").strip().lower() != "n":
            cmd_ids(argparse.Namespace(), config, platform)
            reload_config(config, platform)


def add_from_catalogue(config, platform):
    if not CATALOGUE_FILE.exists():
        print("Games.catalogue.json was not found next to the program.")
        return

    with open(CATALOGUE_FILE, "r", encoding="utf-8-sig") as f:
        catalogue = json.load(f)["Games"]

    have = {g["Name"].lower() for g in config["Games"]}
    available = [g for g in catalogue if g["Name"].lower() not in have]

    if not available:
        print("Everything in the catalogue is already in your Games.json.")
        return

    print(f"{len(available)} games in the catalogue are not in your Games.json yet.")
    entry = choose_game(available)

    if entry is None:
        return

    print(f"\nAdd '{entry['Name']}'?  Save folder (Windows): {entry.get('Source') or '(see entry)'}")

    if ask("[Y/n]: ").strip().lower() == "n":
        return

    backup = edit_games_json(lambda data: data["Games"].append(entry) or True)

    if backup:
        print(f"Added. (A copy of the old file is kept as {backup.name}.)")
        after_adding(entry, config, platform)


def add_typed_game(config, platform):
    name = ask("Game name (how it will be listed): ").strip()

    if not name:
        return

    path = ask("Save folder (paste the full path): ").strip().strip('"')

    if not path:
        return

    if not Path(expand_path(path, config.get("Settings", {}), platform)).exists():
        if ask("That folder does not exist (yet). Add it anyway? [y/N]: ").strip().lower() != "y":
            return

    process = ask("Program name of the game, e.g. Duskfade-Win64-Shipping (Enter = skip): ").strip()
    key = source_key(platform)
    value = portable_path(path, platform)
    entry = {"Name": name, key: value}

    def change(data):
        game = find_raw_game(data, name)

        if game is not None:
            game[key] = value

            if process:
                game.setdefault("Process", process)
        else:
            if process:
                entry["Process"] = process

            data["Games"].append(entry)

    backup = edit_games_json(change)

    if backup:
        print(f"Saved '{name}' ({key}). A copy of the old file is kept as {backup.name}.")
        after_adding(entry, config, platform)


def add_game_menu(config, platform):
    while True:
        print()
        print("=" * 52)
        print(" Add a game")
        print("=" * 52)
        print(" 1  Pick from the catalogue (ready-made entries)")
        print(" 2  A NEW game: find its save folder by playing it")
        print(" 3  I know the save folder - type it")
        print(" 4  Look it up on the internet (Steam genres + PCGamingWiki save folder)")
        print(" 0  Back")
        choice = ask("\nSelect: ").strip()

        if choice in ("0", ""):
            return

        if choice == "1":
            add_from_catalogue(config, platform)
        elif choice == "2":
            sys.path.insert(0, str(SCRIPT_DIR))

            try:
                import findsaves
            except Exception as error:
                print(f"Could not load findsaves.py from the same folder ({error}).")
                continue

            findsaves.add_game_wizard()
            reload_config(config, platform)
        elif choice == "3":
            add_typed_game(config, platform)
        elif choice == "4":
            sys.path.insert(0, str(SCRIPT_DIR))

            try:
                import gameinfo
            except Exception as error:
                print(f"Could not load gameinfo.py from the same folder ({error}).")
                continue

            gameinfo.lookup_flow(sys.modules[__name__], config, platform)


SETTING_ITEMS = [
    # (title, section or None, key, kind, help)
    ("Timed backups every N minutes (0 = off)", None, "BackupIntervalMinutes", "float", "while a game runs"),
    ("Versions to keep per game", None, "VersionCount", "int", "newest N are kept (default 100)"),
    ("Thin versions older than N days to one per day (0 = off)", None, "VersionThinAfterDays", "int", "default 30"),
    ("Seconds to wait for the NAS upload after a game", None, "NasPushSeconds", "float", "default 25"),
    ("NAS counts as slow above N seconds estimated", None, "SlowNasSeconds", "float", "default 20"),
    ("Seconds to wait for the NAS to answer", None, "NasReachableSeconds", "float", "default 5 (automount: 8)"),
    ("When both devices changed a save", None, "ConflictPolicy", ("Ask", "AutoNewest", "AlwaysLocal"), "default Ask"),
    ("Seconds to wait after a game closes", None, "WaitAfterGameExitSeconds", "int", "default 5"),
    ("Show the 'saving...' window (Deck)", None, "ShowProgressWindow", "bool", "default on"),
    ("Days without a backup before the dashboard warns", None, "StaleBackupDays", "int", "default 14"),
    ("This device's name", "DeviceNames", None, "text", "shown in versions and logs"),
    ("NAS backup folder on this device", "BackupRoots", None, "text", "e.g. N:/ or /home/deck/nas"),
    ("Local backup folder on this device", "FallbackRoots", None, "text", "used when the NAS is unreachable"),
]


def cmd_settings(args, config, platform):
    """Change common settings without editing Games.json by hand (a backup copy is made first)."""
    while True:
        data, _ = read_games_json()
        settings = data.setdefault("Settings", {})
        print()
        print("=" * 60)
        print(" Settings  (this device: " + platform + ")")
        print("=" * 60)

        for number, (title, section, key, kind, note) in enumerate(SETTING_ITEMS, start=1):
            value = settings.get(section, {}).get(platform, "-") if section else settings.get(key, "(default)")
            print(f" {number:>2}  {title}\n       now: {value}    ({note})")

        print("  0  Back")
        pick = ask("\nChange which one? ").strip()

        if pick in ("0", ""):
            return 0

        if not (pick.isdigit() and 1 <= int(pick) <= len(SETTING_ITEMS)):
            continue

        title, section, key, kind, note = SETTING_ITEMS[int(pick) - 1]

        if isinstance(kind, tuple):
            for number, option in enumerate(kind, start=1):
                print(f"  {number}  {option}")

            choice = ask("Choose: ").strip()

            if not (choice.isdigit() and 1 <= int(choice) <= len(kind)):
                continue

            value = kind[int(choice) - 1]
        else:
            text = ask(f"New value for '{title}' (Enter = cancel): ").strip()

            if not text:
                continue

            try:
                if kind == "int":
                    value = int(text)
                elif kind == "float":
                    value = float(text)
                    value = int(value) if value == int(value) else value
                elif kind == "bool":
                    value = text.lower() in ("y", "yes", "true", "on", "1")
                else:
                    value = text.strip('"')
            except ValueError:
                print("That is not a valid number.")
                continue

            if kind in ("int", "float") and value < 0:
                print("Use 0 or more.")
                continue

        def change(d):
            target = d.setdefault("Settings", {})

            if section:
                target.setdefault(section, {})[platform] = value
            else:
                target[key] = value

        backup = edit_games_json(change)

        if backup:
            print(f"Saved. (Old file kept as {backup.name}.) Restart the program for it to take effect.")


PROGRAM_FILES = ["savemanager.py", "findsaves.py", "gameinfo.py", "steamsetup.py", "healthcheck.py", "V14-GUIDE.txt",
                 "categories.py", "switchunify.py",
                 "NasMount.sh", "NasAutoMount.sh", "NasConnect.bat", "GameSaveManager.sh", "GameSaveManager.bat"]
MERGE_FILES = ["Categories.json", "SwitchTitles.json", "EmulatorTitles.json"]     # new entries added, yours kept
REPLACE_FILES = ["Games.catalogue.json"]
UPGRADE_BACKUPS = BACKUP_UPGRADES


def read_version(path):
    try:
        match = re.search(r'__version__\s*=\s*"([^"]+)"', Path(path).read_text(encoding="utf-8"))
        return match.group(1) if match else "unknown"
    except OSError:
        return "unknown"


def cmd_upgrade(args, config, platform):
    """
    python savemanager.py upgrade <new zip or folder>     (or just 'upgrade' and type/drag the path)
    Installs a new version over this one WITHOUT touching Games.json, MyIDs.json, State or Logs.
    The old program files are copied to Upgrade-backups/<date>/ first:  upgrade --rollback  restores them.
    """
    import tempfile
    import zipfile

    if args.rollback:
        folders = sorted(UPGRADE_BACKUPS.glob("*"), key=lambda f: f.stat().st_mtime) if UPGRADE_BACKUPS.exists() else []

        if not folders:
            print("No upgrade backups found.")
            return 1

        newest = folders[-1]

        for item in newest.iterdir():
            shutil.copy2(item, SCRIPT_DIR / item.name)

        print(f"Rolled back to the files saved in {newest.name}. Restart the program.")
        return 0

    source = args.source

    if not source:
        # look for a downloaded package in this folder and in Downloads
        found = []

        for folder in (SCRIPT_DIR, Path.home() / "Downloads", BACKUP_PACKAGES):
            for zip_path in folder.glob("*.zip") if folder.exists() else []:
                try:
                    with zipfile.ZipFile(zip_path) as archive:
                        if any(n.endswith(name) for n in archive.namelist() for name in PROGRAM_FILES):
                            found.append(zip_path)
                except (zipfile.BadZipFile, OSError):
                    continue

        found.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        if found:
            print("\nFound these packages (newest first):")

            for number, zip_path in enumerate(found, start=1):
                print(f"  {number}  {zip_path}")

            pick = ask("Which one? (number, or type/drag a path, Enter = cancel): ").strip().strip('"')
            source = str(found[int(pick) - 1]) if pick.isdigit() and 1 <= int(pick) <= len(found) else pick
        else:
            source = ask("Path of the new version (a .zip or a folder - you can drag it in): ").strip().strip('"')

    if not source:
        return 1

    # a bare name like v14.1.zip is looked for here, then in this program's folder, then in Downloads
    for base in (Path.cwd(), SCRIPT_DIR, Path.home() / "Downloads", BACKUP_PACKAGES):
        if (base / source).exists():
            source = str(base / source)
            break

    if not Path(source).exists():
        print(f"Could not find {source}")
        return 1

    source = Path(source)
    work = Path(tempfile.mkdtemp(prefix="gsm-upgrade-"))

    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            archive.extractall(work)

        root = work
    else:
        root = source

    candidates = [root] + [d for d in root.iterdir() if d.is_dir()] if root.is_dir() else []
    # a package may hold only the files that changed, so ANY program file makes it a package
    new_dir = next((d for d in candidates if any((d / name).exists() for name in PROGRAM_FILES)), None)

    if new_dir is None:
        print("None of the program files were found in that package.")
        return 1

    old_version = __version__
    new_version = read_version(new_dir / "savemanager.py") if (new_dir / "savemanager.py").exists() else f"{old_version} (only some files)"
    print(f"\nInstalled version: {old_version}\nNew version:       {new_version}")

    if new_dir.resolve() == SCRIPT_DIR.resolve():
        print("That is this same folder - nothing to do.")
        return 0

    if ask("Install it? Your Games.json, MyIDs.json, logs and settings are not touched. [y/N]: ").strip().lower() != "y":
        return 1

    backup = UPGRADE_BACKUPS / f"v{old_version}_{datetime.now():%Y%m%d-%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)

    for name in PROGRAM_FILES + MERGE_FILES + REPLACE_FILES:
        if (SCRIPT_DIR / name).exists():
            shutil.copy2(SCRIPT_DIR / name, backup / name)

    copied = []

    for name in PROGRAM_FILES + REPLACE_FILES:
        if (new_dir / name).exists():
            shutil.copy2(new_dir / name, SCRIPT_DIR / name)
            copied.append(name)

    merged = []

    for name in MERGE_FILES:
        if not (new_dir / name).exists():
            continue

        try:
            with open(new_dir / name, "r", encoding="utf-8-sig") as f:
                new_data = json.load(f)
        except ValueError:
            continue

        try:
            with open(SCRIPT_DIR / name, "r", encoding="utf-8-sig") as f:
                mine = json.load(f)
        except (OSError, ValueError):
            mine = {}

        added = 0

        for key, value in new_data.items():
            if key not in mine:
                mine[key] = value
                added += 1
            elif isinstance(value, dict) and isinstance(mine[key], dict):
                for sub, subvalue in value.items():
                    if sub not in mine[key]:
                        mine[key][sub] = subvalue
                        added += 1

        with open(SCRIPT_DIR / name, "w", encoding="utf-8") as f:
            json.dump(mine, f, indent=4, ensure_ascii=False)

        merged.append(f"{name} (+{added} new)")

    # NasMount.sh / NasAutoMount.sh hold YOUR settings at the top (NAS address, share, mount folder): keep them
    for name in ("NasMount.sh", "NasAutoMount.sh"):
        old_file = backup / name
        new_file = SCRIPT_DIR / name

        if old_file.exists() and new_file.exists():
            old_text = old_file.read_text(encoding="utf-8")
            new_text = new_file.read_text(encoding="utf-8")

            for variable in ("NAS_IP", "SHARE", "SUBPATH", "MOUNT_DIR", "CRED_FILE"):
                found = re.search(rf'^{variable}="([^"]*)"', old_text, re.M)

                if found:
                    new_text = re.sub(rf'^({variable}=")[^"]*(")', lambda m, v=found.group(1): m.group(1) + v + m.group(2),
                                      new_text, count=1, flags=re.M)

            with open(new_file, "w", encoding="utf-8", newline="\n") as f:
                f.write(new_text)

    for name in ("NasMount.sh", "NasAutoMount.sh", "GameSaveManager.sh"):
        try:
            (SCRIPT_DIR / name).chmod(0o755)
        except OSError:
            pass

    # keep the package you installed from, out of the main folder
    if source.is_file() and source.suffix.lower() == ".zip":
        BACKUP_PACKAGES.mkdir(parents=True, exist_ok=True)
        kept = BACKUP_PACKAGES / source.name

        if not kept.exists():
            shutil.copy2(source, kept)

        if source.parent.resolve() == SCRIPT_DIR.resolve():
            source.unlink()                       # it was lying in the main folder: the copy is enough

        print(f"The package is kept in Backups/Packages/{source.name}")

    # older packages lying in the program folder are no longer needed there
    for leftover in list(SCRIPT_DIR.glob("GameSaveManager*.zip")):
        if not (BACKUP_PACKAGES / leftover.name).exists():
            shutil.move(str(leftover), str(BACKUP_PACKAGES / leftover.name))
        else:
            leftover.unlink()

    print(f"\nUpdated: {', '.join(copied) or '-'}")
    print(f"Merged:  {', '.join(merged) or '-'}")
    print(f"The old version {old_version} is kept in  Backups/Program versions/{backup.name}  (undo with: upgrade --rollback)")
    print("Restart the program to use the new version.")
    return 0


def name_filter(text):
    """'Elden Ring, Duskfade' -> ['elden ring', 'duskfade'] (empty -> [])."""
    return [part.strip().lower() for part in str(text or "").split(",") if part.strip()]


def matches_filter(game, names):
    return any(n in game["Name"].lower() for n in names)


def analyze_for_pull(game, settings, platform, device_name, root):
    """
    Decides what to do about this game WITHOUT changing anything. Returns (state, details):
      uptodate / local_ahead / pull / conflict / nocloud / nopath / busy
    """
    save_set = get_save_set(game, settings, platform)

    if not save_set:
        return "nopath", {}

    name = game["Name"]
    game_folder = root / safe_name(name)
    current = game_folder / "Current"

    if not current.exists():
        return "nocloud", {}

    marker = game_folder / PUSH_MARKER

    if marker.exists() and time.time() - marker.stat().st_mtime < 7200:
        return "busy", {}

    include = game.get("Include", [])
    exclude = list(settings.get("GlobalExclude", [])) + list(game.get("Exclude", []))
    local_files = save_set.files(include, exclude)
    local_digest = folder_digest(local_files) if local_files else None
    remote_digest = current_digest(game_folder, name)

    if not remote_digest:
        return "nocloud", {}

    base = get_base(device_name, name)
    details = {"save_set": save_set, "local_files": local_files, "remote_digest": remote_digest,
               "device": read_meta(game_folder).get("last_device") or "another device",
               "game_folder": game_folder, "current": current, "include": include, "exclude": exclude}

    if local_digest == remote_digest:
        return "uptodate", details

    if local_files and base == remote_digest:
        return "local_ahead", details

    remote_files = list_files(current, include, exclude)
    details["remote_files"] = remote_files
    details["remote_newest"] = max((p.stat().st_mtime for p in remote_files.values()), default=0)
    details["local_newest"] = max((p.stat().st_mtime for p in local_files.values()), default=None)

    if not local_files:
        return "pull", details

    if base is not None and base == local_digest:
        # the NAS copy changed since we last synced - only fetch it when it really is the newer one
        return ("pull" if details["remote_newest"] > details["local_newest"] + TIME_TOLERANCE_SECONDS else "conflict"), details

    if base is None and details["remote_newest"] <= details["local_newest"] + TIME_TOLERANCE_SECONDS:
        return "local_ahead", details

    return "conflict", details


def do_pull(game, settings, platform, device_name, details):
    changes = describe_restore(details["remote_files"], details["local_files"])
    copied = apply_restore(game, settings, platform, device_name, details["save_set"],
                           details["remote_files"], details["local_files"], changes, True)

    if copied is None:
        return False

    set_base(device_name, game["Name"], details["remote_digest"])
    return True


def cmd_syncall(args, config, platform):
    """
    Sync everything: (1) send saves that were waiting for the NAS, (2) go through every game on this device
    and fetch the newest save from the NAS when another device saved later. Safe cases are done by
    themselves; if BOTH devices changed a save you are asked (or, with --yes, it is left alone and listed).
      python savemanager.py syncall [--only "a, b"] [--skip "a, b"] [--yes]
    """
    settings = config.get("Settings", {})
    device_name = get_device_name(settings, platform)
    setup_logging("SyncAll", device_name)
    primary, fallback = get_roots(settings, platform)

    if not primary or not reachable(primary):
        log.error("The NAS is not reachable - nothing to fetch.")
        return 1

    if fallback and reachable(fallback) and any((f / "Current").exists() for f in Path(fallback).iterdir()):
        log.info("Step 1: sending saves that were waiting for the NAS ...")
        cmd_sync(argparse.Namespace(), config, platform)
        setup_logging("SyncAll", device_name)

    only = name_filter(getattr(args, "only", ""))
    skip = name_filter(getattr(args, "skip", ""))
    games = [g for g in available_games(config["Games"], settings, platform)
             if (not only or matches_filter(g, only)) and not matches_filter(g, skip)]
    log.info("")
    log.info("Step 2: checking %d game(s) for a newer save from another device ...", len(games))
    interactive = not getattr(args, "yes", False) and not launched_by_steam()
    results = {"uptodate": 0, "nolocal": 0, "busy": 0}
    uploaded = []
    pulled = []
    conflicts = []
    kept = []
    failed = []
    policy_default = settings.get("ConflictPolicy", "Ask")

    for number, game in enumerate(games, start=1):
        name = game["Name"]

        try:
            state, details = analyze_for_pull(game, settings, platform, device_name, primary)
        except Exception:
            log.exception("FAILED: %s", name)
            failed.append(name)
            continue

        if state in ("local_ahead", "nocloud"):
            save_set = get_save_set(game, settings, platform)
            include = game.get("Include", [])
            exclude = list(settings.get("GlobalExclude", [])) + list(game.get("Exclude", []))

            if not (save_set and save_set.exists() and save_set.files(include, exclude)):
                results["nolocal"] += 1
                continue

            try:
                result, nas_status = post_game_backup(game, settings, platform, device_name)
                log.info("[%d/%d] %s: this device's save is newer - uploaded (%s)", number, len(games), name, nas_status)
                uploaded.append(name)
            except Exception:
                log.exception("FAILED to upload %s", name)
                failed.append(name)

            continue

        if state in ("uptodate", "busy"):
            results[state] += 1

            if state == "uptodate" and details:
                set_base(device_name, name, details["remote_digest"])

            continue

        if state == "nopath":
            continue

        if state == "pull":
            ok = do_pull(game, settings, platform, device_name, details)
            log.info("[%d/%d] %s: %s", number, len(games), name, f"newer save fetched from {details['device']}" if ok else "could not fetch")
            (pulled if ok else failed).append(name)
            continue

        # a real conflict: both devices changed it
        policy = game.get("ConflictPolicy") or policy_default

        if policy == "AlwaysLocal":
            set_base(device_name, name, details["remote_digest"])
            kept.append(name)
            continue

        if policy == "AutoNewest":
            use_remote = details["remote_newest"] > (details["local_newest"] or 0)
        elif interactive:
            message = (f"{name}\n\nBoth devices changed this save.\n\n"
                       f"This device ({device_name}):\n  newest file {format_time(details['local_newest'])}, "
                       f"{len(details['local_files'])} file(s)\n\n"
                       f"{details['device']} (on the NAS):\n  newest file {format_time(details['remote_newest'])}, "
                       f"{len(details['remote_files'])} file(s)\n\nThe one you do not pick is kept in Versions.")
            choice = prompt_choice(f"Save conflict - {name}", message,
                                   [f"Use the NAS save ({details['device']})", "Keep this device's save", "Skip for now"])

            if choice == 0:
                use_remote = True
            elif choice == 1:
                set_base(device_name, name, details["remote_digest"])
                kept.append(name)
                continue
            else:
                conflicts.append(name)
                continue
        else:
            conflicts.append(name)
            continue

        if use_remote:
            ok = do_pull(game, settings, platform, device_name, details)
            log.info("[%d/%d] %s: %s", number, len(games), name, "NAS save used (yours kept in Versions)" if ok else "could not fetch")
            (pulled if ok else failed).append(name)
        else:
            set_base(device_name, name, details["remote_digest"])
            kept.append(name)

    log.info("")
    log.info("=" * 45)
    log.info("Sync all finished")
    log.info("  Already up to date : %d", results["uptodate"])
    log.info("  Uploaded (newer here / not on the NAS yet): %d", len(uploaded))

    for name in uploaded[:12]:
        log.info("      ^ %s", name)

    log.info("  Fetched newer saves: %d", len(pulled))

    for name in pulled:
        log.info("      + %s", name)

    if kept:
        log.info("  Kept this device's save in a conflict: %d (%s)", len(kept), ", ".join(kept[:5]))

    if conflicts:
        log.warning("  NEEDS YOUR DECISION (both changed): %s", "; ".join(conflicts))
        log.warning("  Run  syncall  without --yes, or use Restore (menu E) for each.")

    if failed:
        log.warning("  Problems: %s", ", ".join(failed))

    return 1 if failed else 0


def cmd_launchoption(args, config, platform):
    """Prints what to put into Steam for a game."""
    game = choose_game(config["Games"], args.game)

    if game is None:
        return 1

    script = Path(__file__).resolve()
    print()

    if platform == "Windows":
        print("On Windows Steam often does NOT expand %command% for non-Steam shortcuts,")
        print("so make Steam start Python and hand it the game's real .exe instead:")
        print()
        print("  Target:          " + f'"{sys.executable}"')
        print("  Launch Options:  " + f'"{script}" launch "{game["Name"]}" -- "FULL PATH TO THE GAME .EXE"')
        print()
        print("Use the shortcut's OLD Target as the .exe path. Check Logs\\launch-calls.log afterwards.")
    else:
        print("Paste this into the game's Steam Launch Options:")
        print()
        print(f'"{sys.executable}" "{script}" launch "{game["Name"]}" -- %command%')
        print()
        print("To make this game always wait for the NAS (slow or not), use  launch --force-nas  instead,")
        print("or add  \"ForceNAS\": true  to the game in Games.json.")
        print()
        if game.get("Virtual"):
            print("One launcher for ALL Switch games (for a generic emulator shortcut):")
            print(f'  "{sys.executable}" "{script}" launchgroup Switch -- %command%')
            print()

        print("If the shortcut ALREADY has launch options (EmuDeck ones, for example), put the")
        print("line above at the very START and keep the old options AFTER %command%, like:")
        print(f'  "{sys.executable}" "{script}" launch "{game["Name"]}" -- %command% <old options here>')

    return 0


# ----------------------------------------------------------------------
# Menu (what you get when you double-click / run with no arguments)
# ----------------------------------------------------------------------
def cmd_recent(args, config, platform):
    """Recently backed-up games, read from each game's meta.json (newest first)."""
    settings = config.get("Settings", {})
    primary, fallback = get_roots(settings, platform)
    found = {}

    for where, root in (("Main", primary), ("Local", fallback)):
        if not root or not reachable(root):
            continue

        for game_folder in root.iterdir():
            meta = read_meta(game_folder) if game_folder.is_dir() else {}
            when = meta.get("last_backup")

            if not when:
                continue

            name = meta.get("game") or game_folder.name

            if name not in found or when > found[name][0]:
                found[name] = (when, meta.get("last_device") or "unknown", where)

    rows = sorted(found.items(), key=lambda item: item[1][0], reverse=True)[:args.count]

    if not rows:
        print("No backups with a record of when they were made yet.")
        return 0

    print()

    for name, (when, device, where) in rows:
        stamp = datetime.fromisoformat(when).strftime("%d/%m/%y %H:%M")
        print(f"  {stamp}  {name}   (by {device}, {where})")

    return 0


def cmd_desktopicon(args, config, platform):
    """Steam Deck: creates a double-clickable 'Game Save Manager' icon on the desktop."""
    if platform == "Windows":
        print("On Windows just double-click GameSaveManager.bat (or savemanager.py).")
        return 0

    desktop = Path.home() / "Desktop"

    if not desktop.exists():
        desktop = Path.home()

    icon = desktop / "Game Save Manager.desktop"
    script = Path(__file__).resolve()
    text = ("[Desktop Entry]\nType=Application\nName=Game Save Manager\n"
            f'Exec={sys.executable} "{script}"\nPath={script.parent}\n'
            "Terminal=true\nIcon=utilities-terminal\n")

    with open(icon, "w", encoding="utf-8") as f:
        f.write(text)

    icon.chmod(0o755)
    apps = Path.home() / ".local" / "share" / "applications"

    try:
        apps.mkdir(parents=True, exist_ok=True)
        entry = apps / "game-save-manager.desktop"

        with open(entry, "w", encoding="utf-8") as f:
            f.write(text)

        entry.chmod(0o755)
        print(f"Also added to the applications list: {entry}")
        print("(Steam > Add a Game > Add a Non-Steam Game now lists 'Game Save Manager'.)")
    except OSError:
        pass

    print(f"Created: {icon}")
    print("If double-clicking asks, right-click it and choose 'Allow Launching'.")
    return 0


def run_module(module_name, config, platform, args):
    """Runs categories.py / switchunify.py (they sit next to this file)."""
    sys.path.insert(0, str(SCRIPT_DIR))

    try:
        module = __import__(module_name)
    except ImportError as error:
        print(f"{module_name}.py is missing next to the program ({error}). Install the full package with Upgrade.")
        return 1

    return module.run(sys.modules[__name__], config, platform, args)


def tools_menu(config, platform):
    settings = config.get("Settings", {})
    device = get_device_name(settings, platform)
    primary, fallback = get_roots(settings, platform)
    blank = dict(game=None, skip=[], platform=None, dry_run=False, force_nas=False)

    def backup_one():
        game = choose_game(config["Games"])

        if game:
            cmd_backup(argparse.Namespace(**dict(blank, game=game["Name"])), config, platform)

    items = [
        ("Backup all games", lambda: cmd_backup(argparse.Namespace(**blank), config, platform)),
        ("Backup one game", backup_one),
        ("Restore a game", lambda: cmd_restore(argparse.Namespace(game=None), config, platform)),
        ("List games", lambda: cmd_list(argparse.Namespace(), config, platform)),
        ("Recently backed up", lambda: cmd_recent(argparse.Namespace(count=15), config, platform)),
    ]

    if primary and fallback:
        items.append(("Sync local backups to the NAS", lambda: cmd_sync(argparse.Namespace(), config, platform)))

    items.append(("Auto-assign categories (genres) to games", lambda: run_module(
        "categories", config, platform, argparse.Namespace(online=None, yes=False, overwrite=False))))
    items.append(("Switch: one save for all emulators (set up / check)", lambda: run_module(
        "switchunify", config, platform, argparse.Namespace(yes=False))))
    items.append(("Show the Steam launch option for a game",
                  lambda: cmd_launchoption(argparse.Namespace(game=None), config, platform)))

    if platform != "Windows":
        items.append(("Create a desktop icon for this menu",
                      lambda: cmd_desktopicon(argparse.Namespace(), config, platform)))

    while True:
        print()
        print("=" * 45)
        print(f" Tools   {device} ({platform})")
        print("=" * 45)

        for number, (label, _) in enumerate(items, start=1):
            print(f" {number}  {label}")

        print(" Q  Quit")
        choice = ask("\nSelect: ").strip().lower()

        if choice in ("q", ""):
            return 0

        if choice.isdigit() and 1 <= int(choice) <= len(items):
            items[int(choice) - 1][1]()
            ask("\nPress Enter to continue...")


# ----------------------------------------------------------------------
# Game menu + Service (timed background backups)
# ----------------------------------------------------------------------
BACKUP_LOCK = threading.Lock()


def process_names(game):
    """
    Every key that starts with 'Process' (Process, ProcessDX12, Processdmc1 ...) can hold one
    program name or a list of names. The game counts as running if ANY of them is running.
    """
    names = []

    for key, value in game.items():
        if not key.lower().startswith("process"):
            continue

        for item in (value if isinstance(value, list) else [value]):
            if isinstance(item, str) and item.strip():
                names.append(item.strip())

    return names


def linux_process_running(names):
    """Looks through /proc for a program whose command line contains one of the names as a file name."""
    wanted = set()

    for name in names:
        base = name.lower()
        wanted.add(base)
        wanted.add(base[:-4] if base.endswith(".exe") else base + ".exe")

    me = os.getpid()

    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == me:
            continue

        try:
            with open(f"/proc/{entry.name}/cmdline", "rb") as f:
                data = f.read()
        except OSError:
            continue

        for part in data.split(b"\0"):
            base = part.decode("utf-8", "ignore").replace("\\", "/").rsplit("/", 1)[-1].lower()

            if base in wanted:
                return True

    return False


def process_running_any(names):
    if not names:
        return False

    if sys.platform.startswith("win"):
        return any(process_running(n) for n in names)

    return linux_process_running(names)


def start_timer_backups(game, settings, platform, device_name):
    """
    While the game runs (under 'launch'), back up every BackupIntervalMinutes (game or Settings;
    0 = off). Returns an Event; set it to stop. Runs in the background and never blocks the game.
    """
    minutes = float(game.get("BackupIntervalMinutes") or settings.get("BackupIntervalMinutes", 0) or 0)

    if minutes <= 0:
        return None

    stop = threading.Event()

    def loop():
        while not stop.wait(minutes * 60):
            try:
                with BACKUP_LOCK:
                    log.info("Timed backup while the game is running...")
                    result, nas_status = post_game_backup(game, settings, platform, device_name)
                    log.info("Timed backup: %s | NAS: %s", result, nas_status)
            except Exception:
                log.exception("Timed backup failed")

    threading.Thread(target=loop, daemon=True).start()
    return stop


# ---- menu settings that belong to THIS device (pins, favourites, hidden, recently played) ----
def menu_state_file(device_name):
    return SCRIPT_DIR / "State" / f"{safe_name(device_name)}-menu.json"


def load_menu_state(device_name):
    try:
        with open(menu_state_file(device_name), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}

    data.setdefault("flags", {})
    data.setdefault("played", {})
    data.setdefault("categories", {})
    return data


def save_menu_state(device_name, state):
    path = menu_state_file(device_name)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)


def record_played(device_name, game_name):
    try:
        state = load_menu_state(device_name)
        state["played"][game_name] = datetime.now().isoformat(timespec="seconds")
        save_menu_state(device_name, state)
    except OSError:
        pass


def game_flag(state, game, key):
    """Your choice on this device wins; otherwise the value in Games.json (Pinned / Favourite / Hidden)."""
    override = state["flags"].get(game["Name"], {})

    if key in override:
        return bool(override[key])

    return bool(game.get(key))


def set_flag(device_name, game, key, value):
    state = load_menu_state(device_name)
    state["flags"].setdefault(game["Name"], {})[key] = value
    save_menu_state(device_name, state)


CATEGORIES_FILE = SCRIPT_DIR / "Categories.json"
_CATEGORY_CACHE = {}


def categories_file():
    """Categories.json: { "Game name": "Genre, Genre" }  (loaded once)."""
    if "data" not in _CATEGORY_CACHE:
        try:
            with open(CATEGORIES_FILE, "r", encoding="utf-8-sig") as f:
                _CATEGORY_CACHE["data"] = json.load(f)
        except (OSError, ValueError):
            _CATEGORY_CACHE["data"] = {}

    return _CATEGORY_CACHE["data"]


def split_categories(raw):
    parts = raw if isinstance(raw, list) else str(raw or "").split(",")
    return [str(p).strip() for p in parts if str(p).strip()]


def game_categories(game, state=None):
    """
    Your own choice on this device wins. Otherwise: Category in Games.json (also the old
    'Catagory' spelling) + the genres in Categories.json. Last resort: the Platform.
    """
    if state is not None and game["Name"] in state.get("categories", {}):
        return split_categories(state["categories"][game["Name"]]) or ([str(game["Platform"])] if game.get("Platform") else [])

    found = split_categories(game.get("Category") or game.get("Catagory"))
    found += split_categories(categories_file().get(game["Name"]))
    unique = []

    for item in found:
        if item.lower() not in [u.lower() for u in unique]:
            unique.append(item)

    return unique or ([str(game["Platform"]).strip()] if game.get("Platform") else [])


def set_category(device_name, game, text):
    state = load_menu_state(device_name)
    state.setdefault("categories", {})[game["Name"]] = text
    save_menu_state(device_name, state)


SWITCH_TITLES_FILE = SCRIPT_DIR / "SwitchTitles.json"


def load_switch_titles():
    """SwitchTitles.json: { "TITLEID": {"Name": "...", "Ryujinx": "<save id>/0", "RyujinxSteamDeck": "..."} }"""
    try:
        with open(SWITCH_TITLES_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}

    titles = {}

    for title_id, info in data.items():
        if title_id.startswith("_"):
            continue

        titles[title_id.upper()] = {"Name": info} if isinstance(info, str) else dict(info)

    return titles


def newest_time(path):
    try:
        times = [p.stat().st_mtime for p in Path(path).rglob("*") if p.is_file()]
        return max(times) if times else 0
    except OSError:
        return 0


def ryujinx_sub(info, suffix):
    """The Ryujinx save folder for a title on this device: Ryujinx (Windows), RyujinxSteamDeck, or RyujinxPC."""
    keys = ["Ryujinx" + suffix] + (["RyujinxPC", "RyujinxWindows"] if suffix == "" else [])

    for key in keys:
        if info.get(key):
            return info[key]

    return None


def ryujinx_path(base, sub):
    """A relative folder (0000000000000005/0) goes under the Ryujinx save folder; a full path is used as it is."""
    text = str(sub)
    return Path(text) if re.match(r"^([A-Za-z]:[\\/]|/)", text) else base / text


def ryujinx_folder_id(sub):
    parts = [p for p in re.split(r"[\\/]", str(sub)) if p]
    return (parts[-2] if len(parts) >= 2 and re.match(r"^([A-Za-z]:|/)", str(sub)) else parts[0] if parts else "").lower()


def switch_emulator_label(base):
    """The emulator a Switch source entry belongs to: SwitchEmulator, else the end of its name ('zEmu_Switch - Eden' -> Eden)."""
    if base.get("SwitchEmulator"):
        return str(base["SwitchEmulator"]).strip()

    name = str(base.get("Name", "")).strip()
    return name.rsplit(" - ", 1)[-1].strip() if " - " in name else (name or "Switch")


def same_path_key(path):
    """One spelling for comparing paths (Windows ignores case and accepts both slashes)."""
    return os.path.normcase(os.path.normpath(str(path)))


def expand_switch(config, platform):
    """
    Entries with  SwitchSource  (a yuzu / Eden / Suyu / Citron profile save folder) and/or
    SwitchRyujinxSource  (Ryujinx's bis/user/save folder) are SWITCH SOURCES, not games. Together they
    turn into ONE game per Switch title ("Switch - <name>"), however many emulators hold a copy of it:
      - the newest copy on this device is the one that is backed up / restored ("Source"),
      - every other emulator's copy is listed in SwitchCopies and kept in step with it (keep_copies_in_step),
        so whichever emulator you start, the same save is there.
    Each device uses its own paths (SwitchSourceSteamDeck ...) but the SAME game names, so the backups are
    shared between devices and between emulators.
    Ryujinx save folders that SwitchTitles.json does not map to a title yet are still backed up, as
    "Switch - Ryujinx other saves (<device>)" (setting SwitchRyujinxOtherSaves, default on).
    """
    settings = config.get("Settings", {})
    suffix = "" if platform == "Windows" else platform
    titles = load_switch_titles()
    games = []
    primary, fallback = get_roots(settings, platform)
    backed_up = set()

    for root in (primary, fallback):
        if root and reachable(root):
            try:
                backed_up.update(f.name for f in root.iterdir() if f.is_dir() and f.name.startswith("Switch - "))
            except OSError:
                pass

    bases = []
    sources = []           # (entry, emulator, yuzu-type profile folder, Ryujinx folder) on this device
    groups = {}            # (title ID, name suffix) -> {"base", "candidates": [(path, emulator, kind)], "processes"}
    ryujinx_roots = []     # (Ryujinx save folder, its entry) for the "other saves" game

    for game in config["Games"]:
        if not any(key.startswith("SwitchSource") or key.startswith("SwitchRyujinxSource") for key in game):
            games.append(game)
            continue

        bases.append(game)
        label = switch_emulator_label(game)
        base_text = game.get("SwitchSource" + suffix)
        ryu_text = game.get("SwitchRyujinxSource" + suffix)
        base = Path(expand_path(base_text, settings, platform)) if base_text else None
        ryu = Path(expand_path(ryu_text, settings, platform)) if ryu_text else None

        if ryu:
            ryujinx_roots.append((ryu, game))

        sources.append((game, label, base, ryu))
        ids = set()

        if base and base.is_dir():
            ids.update(e.name.upper() for e in base.iterdir() if e.is_dir() and re.fullmatch(r"[0-9A-Fa-f]{16}", e.name))

        for title_id, info in titles.items():
            ryu_sub = ryujinx_sub(info, suffix)

            if f"Switch - {info.get('Name')}" in backed_up:
                ids.add(title_id)

            if ryu and ryu_sub and ryujinx_path(ryu, ryu_sub).exists():
                ids.add(title_id)

        for title_id in ids:
            groups.setdefault((title_id, game.get("SwitchNameSuffix", "")), {"base": game, "candidates": [], "processes": []})

    # Every title gets a place in EVERY emulator on this device (same name suffix), so whichever one you start has it.
    for (title_id, name_suffix), group in groups.items():
        info = titles.get(title_id, {})
        ryu_sub = ryujinx_sub(info, suffix)

        for game, label, base, ryu in sources:
            if game.get("SwitchNameSuffix", "") != name_suffix:
                continue

            for process in process_names(game):
                if process not in group["processes"]:
                    group["processes"].append(process)

            if base:
                group["candidates"].append((base / title_id, label, "yuzu"))

            if ryu and ryu_sub:
                group["candidates"].append((ryujinx_path(ryu, ryu_sub), label if label != "Switch" else "Ryujinx", "ryujinx"))

    extra = []

    for (title_id, name_suffix) in sorted(groups):
        group = groups[(title_id, name_suffix)]
        unique = {}

        for path, label, kind in group["candidates"]:
            seen = unique.get(same_path_key(path))

            if seen is None:
                unique[same_path_key(path)] = (path, label, kind)
            elif label not in seen[1].split(" + "):                    # Eden and Suyu sharing one folder = one copy
                unique[same_path_key(path)] = (seen[0], f"{seen[1]} + {label}", kind)

        candidates = list(unique.values())
        existing = [c for c in candidates if c[0].exists()]

        if existing:
            source, source_label, _ = max(existing, key=lambda c: newest_time(c[0]))
        elif candidates:
            usable = [c for c in candidates if c[0].parent.is_dir()]          # the emulator is installed here
            source, source_label, _ = (usable or candidates)[0]
        else:
            continue

        info = titles.get(title_id, {})
        virtual = {k: v for k, v in group["base"].items()
                   if not k.startswith("Switch") and not k.lower().startswith("process")}

        for number, process in enumerate(group["processes"]):
            virtual["Process" if number == 0 else f"ProcessEmulator{number}"] = process

        virtual["Name"] = f"Switch - {info.get('Name') or title_id}{name_suffix}"
        virtual["Source" + suffix] = str(source)
        virtual["Category"] = "Switch"
        virtual["Virtual"] = True
        virtual["SwitchTitleID"] = title_id
        virtual["SwitchEmulators"] = sorted({label for _, label, _ in candidates})
        virtual["SwitchSourceEmulator"] = source_label

        # Other emulators' copies: a yuzu-type title folder can be created inside an existing profile folder;
        # a Ryujinx folder only counts once Ryujinx has made it (Ryujinx keeps its own list of save folders).
        copies = []

        for path, label, kind in candidates:
            if same_path_key(path) == same_path_key(source):
                continue

            if path.exists() or (kind == "yuzu" and path.parent.is_dir()):
                copies.append({"path": str(path), "emulator": label})

        if copies:
            virtual["SwitchCopies"] = copies

        extra.append(virtual)

    if settings.get("SwitchRyujinxOtherSaves", True) and ryujinx_roots:
        device_name = get_device_name(settings, platform)
        mapped = {ryujinx_folder_id(ryujinx_sub(info, suffix) or "") for info in titles.values()} - {""}
        done = set()

        for ryu, base_game in ryujinx_roots:
            if same_path_key(ryu) in done or not ryu.is_dir():
                continue

            done.add(same_path_key(ryu))

            try:
                folders = [e.name.lower() for e in ryu.iterdir() if e.is_dir()]
            except OSError:
                continue

            if not any(folder not in mapped for folder in folders):
                continue

            other = {k: v for k, v in base_game.items() if not k.startswith("Switch")}
            other["Name"] = f"Switch - Ryujinx other saves ({device_name})" + (f" {len(done)}" if len(done) > 1 else "")
            other["Source" + suffix] = str(ryu)
            other["Exclude"] = list(other.get("Exclude", [])) + [f"{folder}/*" for folder in sorted(mapped)]
            other["Category"] = "Switch"
            other["Virtual"] = True
            other["SwitchOtherSaves"] = True
            extra.append(other)

    config["Games"] = games + extra
    config["_SwitchBases"] = bases
    return config


def newest_switch_copy(game, platform):
    """
    The same Switch game, with its Source moved to whichever emulator's copy is newest RIGHT NOW.
    Games.json is read before the game starts, so after playing in another emulator its copy is the
    newest one; this makes sure that copy is the one backed up (and copied to the others), not the old one.
    """
    copies = game.get("SwitchCopies")

    if not copies:
        return game

    suffix = "" if platform == "Windows" else platform
    entries = [{"path": game.get("Source" + suffix, ""), "emulator": game.get("SwitchSourceEmulator") or "emulator"}]
    entries += list(copies)

    def age(entry):
        path = Path(str(entry["path"]))
        return newest_time(path) if path.exists() else -1

    best = max(entries, key=age)              # a tie keeps the current Source (it is first)

    if best is entries[0]:
        return game

    log.info("%s: %s has the newest copy - using it.", game["Name"], best.get("emulator"))
    newer = dict(game)
    newer["Source" + suffix] = str(best["path"])
    newer["SwitchSourceEmulator"] = best.get("emulator")
    newer["SwitchCopies"] = [e for e in entries if e is not best]
    return newer


def keep_copies_in_step(game, settings, platform, device_name, backup_root=None):
    """
    Makes every other emulator's copy of a Switch game the same as the game's save (the newest copy).
    Before a copy is replaced, its files go into the game's Versions as '<date>__<device>__Unify-<emulator>',
    so nothing is lost. Setting SwitchUnify: false switches this off. Returns how many copies were updated.
    """
    if not game.get("SwitchCopies") or not settings.get("SwitchUnify", True):
        return 0

    game = newest_switch_copy(game, platform)
    copies = game.get("SwitchCopies") or []

    suffix = "" if platform == "Windows" else platform
    source = Path(str(game.get("Source" + suffix, "")))
    source_files = list_files(source, [], [])

    if not source_files:
        return 0

    if backup_root is None:
        backup_root, _ = choose_backup_root(settings, platform)

    updated = 0

    for copy in copies:
        target = Path(copy["path"])
        label = safe_name(str(copy.get("emulator") or "emulator"))

        try:
            target_files = list_files(target, [], [])

            if set(target_files) == set(source_files) and all(
                    quick_same(source_files[r], target_files[r]) for r in source_files):
                continue

            if target_files:
                if backup_root is None:
                    log.warning("%s: no backup folder for a safety copy - %s's copy was left alone.", game["Name"], label)
                    continue

                snapshot = backup_root / safe_name(game["Name"]) / "Versions" / make_stamp(
                    f"{safe_name(device_name)}__Unify-{label}")

                for relative, path in target_files.items():
                    (snapshot / relative).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, snapshot / relative)

            mirror_folder(source, target)
            updated += 1
            log.info("%s: %s's copy now matches the newest save.", game["Name"], label)
        except OSError as error:
            log.warning("%s: could not update %s's copy (%s) - left as it was.", game["Name"], label, error)

    return updated


def write_switch_titles(data):
    with open(SWITCH_TITLES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def read_switch_raw():
    try:
        with open(SWITCH_TITLES_FILE, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


EMULATOR_TITLES_FILE = SCRIPT_DIR / "EmulatorTitles.json"


def load_emulator_titles():
    """EmulatorTitles.json: { "RPCS3": { "BLUS30443": "Demon's Souls" }, "PPSSPP": { ... } }"""
    try:
        with open(EMULATOR_TITLES_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}

    return {label: names for label, names in data.items() if not label.startswith("_") and isinstance(names, dict)}


def pergame_name(names, folder):
    """The folder name, its first 9 characters (PSP/PS3 IDs) or first 16 (Switch) can be listed."""
    return names.get(folder) or names.get(folder[:9]) or names.get(folder[:16])


def expand_pergame(config, platform):
    """
    A Games.json entry with  PerGameSource  (and PerGameSourceSteamDeck ...) and  PerGameLabel  turns
    into one game per sub folder: 'RPCS3 - <name>'. Names come from EmulatorTitles.json; anything
    not named yet shows its folder name (name it with: python savemanager.py library).
    Optional: PerGameFilter (a regular expression the folder name must match).
    """
    settings = config.get("Settings", {})
    suffix = "" if platform == "Windows" else platform
    titles = load_emulator_titles()
    games = []
    extra = []
    bases = []

    for game in config["Games"]:
        if not any(key.startswith("PerGameSource") for key in game):
            games.append(game)
            continue

        bases.append(game)
        text = game.get("PerGameSource" + suffix)

        if not text:
            continue

        label = game.get("PerGameLabel") or game["Name"]
        base = Path(expand_path(text, settings, platform))
        pattern = game.get("PerGameFilter") or r".+"
        names = titles.get(label, {})

        if not base.is_dir():
            continue

        for folder in sorted(base.iterdir()):
            if not folder.is_dir() or not re.fullmatch(pattern, folder.name):
                continue

            virtual = {k: v for k, v in game.items() if not k.startswith("PerGame")}
            virtual["Name"] = f"{label} - {pergame_name(names, folder.name) or folder.name}"
            virtual["Source" + suffix] = str(folder)
            virtual["Category"] = label
            virtual["Virtual"] = True
            virtual["PerGameLabel"] = label
            virtual["PerGameFolder"] = folder.name
            extra.append(virtual)

    config["Games"] = games + extra
    config["_PerGameBases"] = bases
    return config


def expand_perfile(config, platform):
    """
    A Games.json entry with  PerFileSource  (and PerFileSourceSteamDeck ...), PerFileGlob ("*.mcd") and PerFileLabel
    turns into one game per FILE: 'DuckStation - Parasite Eve_1'. Every file is backed up, restored and kept in step
    between devices on its own, so a card changed on the PC and a different card changed on the Deck never clash.
    Files that exist only on the NAS (made on another device) also appear, so they can be pulled to this one.
    """
    settings = config.get("Settings", {})
    suffix = "" if platform == "Windows" else platform
    games = []
    extra = []
    primary, fallback = get_roots(settings, platform)
    backed_up = set()

    for root in (primary, fallback):
        if root and reachable(root):
            try:
                backed_up.update(f.name for f in root.iterdir() if f.is_dir())
            except OSError:
                pass

    for game in config["Games"]:
        if not any(key.startswith("PerFileSource") for key in game):
            games.append(game)
            continue

        text = game.get("PerFileSource" + suffix)

        if not text:
            continue

        label = game.get("PerFileLabel") or game["Name"]
        pattern = game.get("PerFileGlob") or "*"
        extension = os.path.splitext(pattern)[1]            # "*.mcd" -> ".mcd"
        base = Path(expand_path(text, settings, platform))
        files = {}

        if base.is_dir():
            for path in sorted(base.glob(pattern)):
                if path.is_file():
                    files[path.stem] = path

        for folder in backed_up:                           # a file another device made: not here yet
            if extension and folder.startswith(f"{label} - ") and folder[len(label) + 3:] not in files:
                files[folder[len(label) + 3:]] = base / (folder[len(label) + 3:] + extension)

        for stem in sorted(files):
            virtual = {k: v for k, v in game.items() if not k.startswith("PerFile")}
            virtual["Name"] = f"{label} - {stem}"
            virtual["Source" + suffix] = str(files[stem])
            virtual["Category"] = label
            virtual["Virtual"] = True
            virtual["PerFileLabel"] = label
            extra.append(virtual)

    config["Games"] = games + extra
    return config


def expand_games(config, platform):
    return expand_perfile(expand_pergame(expand_switch(config, platform), platform), platform)


def cmd_library(args, config, platform):
    """Lists the per-game emulator folders found here and lets you name the unnamed ones (EmulatorTitles.json)."""
    found = [g for g in config["Games"] if g.get("PerGameFolder")]

    if not found:
        print("No per-game emulator folders found. Add an entry with PerGameSource / PerGameSourceSteamDeck and PerGameLabel.")
        return 0

    titles = load_emulator_titles()
    unnamed = []
    print()

    for game in sorted(found, key=lambda g: g["Name"].lower()):
        label = game["PerGameLabel"]
        folder = game["PerGameFolder"]
        named = pergame_name(titles.get(label, {}), folder) is not None
        print(f"  {label:<10} {folder:<28} {game['Name'] if named else '(not named yet)'}")

        if not named:
            unnamed.append((label, folder))

    while unnamed:
        print()

        for number, (label, folder) in enumerate(unnamed, start=1):
            print(f"  {number:>2}  {label}: {folder}")

        pick = ask("Name which one? (number, Enter = finish): ").strip()

        if not (pick.isdigit() and 1 <= int(pick) <= len(unnamed)):
            break

        label, folder = unnamed[int(pick) - 1]
        name = ask(f"Game name for {folder}: ").strip()

        if name:
            try:
                with open(EMULATOR_TITLES_FILE, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}

            data.setdefault(label, {})[folder] = name

            with open(EMULATOR_TITLES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)

            unnamed.remove((label, folder))
            print("Saved. Restart the program to see the new name.")

    return 0


def cmd_switch(args, config, platform):
    """
    Lists the Switch games found on this device, lets you name unknown title IDs, and lets you link
    Ryujinx's numbered save folders to games (all saved in SwitchTitles.json).
    """
    settings = config.get("Settings", {})
    suffix = "" if platform == "Windows" else platform
    titles = load_switch_titles()
    found = [g for g in config["Games"] if g.get("SwitchTitleID")]
    unknown = []
    print()

    if not found:
        print("No Switch saves found yet (add a game entry with SwitchSource / SwitchSourceSteamDeck).")

    for game in sorted(found, key=lambda g: g["Name"].lower()):
        title_id = game["SwitchTitleID"]
        known = title_id in titles
        source = resolve_source(game, settings, platform)
        print(f"  {title_id}  {game['Name'] if known else '(unknown - not named yet)'}   {format_time(newest_time(source)) if source else ''}")

        if not known:
            unknown.append(title_id)

    while unknown:
        print()

        for number, title_id in enumerate(unknown, start=1):
            print(f"  {number}  {title_id}")

        pick = ask("Name which one? (number, Enter = skip): ").strip()

        if not (pick.isdigit() and 1 <= int(pick) <= len(unknown)):
            break

        title_id = unknown[int(pick) - 1]
        name = ask(f"Game name for {title_id}: ").strip()

        if name:
            data = read_switch_raw()
            data[title_id] = {"Name": name}
            write_switch_titles(data)
            unknown.remove(title_id)
            print(f"Saved: {name}")

    # Ryujinx save folders (numbered, different on every install) that are not linked to a game yet
    for base in config.get("_SwitchBases", []):
        text = base.get("SwitchRyujinxSource" + suffix)

        if not text:
            continue

        ryu = Path(expand_path(text, settings, platform))
        mapped = {ryujinx_folder_id(ryujinx_sub(info, suffix) or "") for info in titles.values()}

        while ryu.is_dir():
            loose = [d for d in sorted(ryu.iterdir())
                     if d.is_dir() and (d / "0").is_dir() and d.name.lower() not in mapped]

            if not loose:
                break

            print()
            print("Ryujinx save folders not linked to a game yet (newest first = the game you played last):")
            loose.sort(key=newest_time, reverse=True)

            for number, folder in enumerate(loose, start=1):
                print(f"  {number}  {folder.name}   last changed {format_time(newest_time(folder))}")

            pick = ask("Link which folder? (number, Enter = finish): ").strip()

            if not (pick.isdigit() and 1 <= int(pick) <= len(loose)):
                break

            folder = loose[int(pick) - 1]
            known_titles = sorted(titles.items(), key=lambda item: item[1].get("Name", "").lower())
            print()

            for number, (title_id, info) in enumerate(known_titles, start=1):
                print(f"  {number:>2}  {info.get('Name')}")

            choice = ask("Which game is this? (number, or type a title ID): ").strip()
            title_id = None

            if choice.isdigit() and 1 <= int(choice) <= len(known_titles):
                title_id = known_titles[int(choice) - 1][0]
            elif re.fullmatch(r"[0-9A-Fa-f]{16}", choice):
                title_id = choice.upper()

            if title_id:
                data = read_switch_raw()
                entry = data.get(title_id, {"Name": titles.get(title_id, {}).get("Name") or title_id})
                entry = {"Name": entry} if isinstance(entry, str) else dict(entry)
                entry["Ryujinx" + suffix] = f"{folder.name}/0"
                data[title_id] = entry
                write_switch_titles(data)
                titles = load_switch_titles()
                mapped.add(folder.name.lower())
                print("Linked.")

    return 0


def available_games(games, settings, platform):
    """Only games that have a save path on THIS device."""
    return [g for g in games if resolve_source(g, settings, platform)]


def marks(state, game):
    text = ""

    if game_flag(state, game, "Pinned"):
        text += " [P]"

    if game_flag(state, game, "Favourite"):
        text += " [F]"

    if game_flag(state, game, "Hidden"):
        text += " [H]"

    return text


def pick_game(title, games, state, page_size=12):
    """A numbered list with pages. Returns the chosen game or None."""
    pages = max(1, (len(games) + page_size - 1) // page_size)
    page = 0

    if not games:
        print(f"\n{title}: nothing to show.")
        return None

    while True:
        chunk = games[page * page_size:(page + 1) * page_size]
        print()
        print(f"{title}   (page {page + 1}/{pages}, {len(games)} games)")
        print("-" * 52)

        for number, game in enumerate(chunk, start=1):
            print(f" {number:>2}  {game['Name']}{marks(state, game)}")

        print("\n n next page    p previous page    0 back")
        choice = ask("Select: ").strip().lower()

        if choice in ("0", "b", "q", ""):
            return None

        if choice == "n" and page + 1 < pages:
            page += 1
        elif choice == "p" and page > 0:
            page -= 1
        elif choice.isdigit() and 1 <= int(choice) <= len(chunk):
            return chunk[int(choice) - 1]


def show_details(game, settings, platform, device_name, state):
    print()
    print("=" * 52)
    print(f" {game['Name']}")
    print("=" * 52)
    print(f" Platform      : {game.get('Platform', '-')}")
    print(f" Categories    : {', '.join(game_categories(game, state)) or '-'}")
    save_set = get_save_set(game, settings, platform)
    print(f" Save folder   : {save_set.describe() if save_set else '-'}")
    print(f" Programs      : {', '.join(process_names(game)) or '(none set - timed backups only)'}")
    print(f" Pinned / Fav / Hidden : {game_flag(state, game, 'Pinned')} / "
          f"{game_flag(state, game, 'Favourite')} / {game_flag(state, game, 'Hidden')}")
    print(f" Last played here : {state['played'].get(game['Name'], '-')}")
    primary, fallback = get_roots(settings, platform)

    for label, root in (("NAS", primary), ("Local", fallback)):
        if root and reachable(root):
            meta = read_meta(root / safe_name(game["Name"]))
            print(f" Last backup ({label}) : {meta.get('last_backup', '-')}  by {meta.get('last_device', '-')}")


def game_menu(game, config, platform):
    settings = config.get("Settings", {})
    device_name = get_device_name(settings, platform)

    while True:
        state = load_menu_state(device_name)
        print()
        print("=" * 52)
        print(f" {game['Name']}{marks(state, game)}")
        print("=" * 52)
        print(" 1  Start monitoring (timed backups + backup when it closes)")
        print(" 2  Backup now")
        print(" 3  Restore")
        print(" 4  Details")
        print(f" 5  {'Unpin' if game_flag(state, game, 'Pinned') else 'Pin'}")
        print(f" 6  {'Remove from favourites' if game_flag(state, game, 'Favourite') else 'Add to favourites'}")
        print(f" 7  {'Unhide' if game_flag(state, game, 'Hidden') else 'Hide'}")
        print(f" 8  Set category (now: {', '.join(game_categories(game, state)) or '-'})")
        print(" 0  Back")
        choice = ask("\nSelect: ").strip()

        if choice in ("0", ""):
            return
        elif choice == "1":
            monitor_game(game, config, platform)
            ask("\nPress Enter to continue...")
        elif choice == "2":
            cmd_backup(argparse.Namespace(game=game["Name"], skip=[], platform=None, dry_run=False, force_nas=False),
                       config, platform)
            ask("\nPress Enter to continue...")
        elif choice == "3":
            cmd_restore(argparse.Namespace(game=game["Name"]), config, platform)
            ask("\nPress Enter to continue...")
        elif choice == "4":
            show_details(game, settings, platform, device_name, state)
            ask("\nPress Enter to continue...")
        elif choice == "5":
            set_flag(device_name, game, "Pinned", not game_flag(state, game, "Pinned"))
        elif choice == "6":
            set_flag(device_name, game, "Favourite", not game_flag(state, game, "Favourite"))
        elif choice == "7":
            set_flag(device_name, game, "Hidden", not game_flag(state, game, "Hidden"))
        elif choice == "8":
            text = ask("Category (comma separated, empty = use the platform): ").strip()
            set_category(device_name, game, text)


def monitor_game(game, config, platform):
    """
    The BackupService: back up now, then every BackupIntervalMinutes while the game runs,
    and once more when it closes. Use it for games you start yourself (not through Steam's
    launch option). Ctrl+C stops it after a final backup.
    """
    settings = config.get("Settings", {})
    device_name = get_device_name(settings, platform)
    name = game["Name"]
    names = process_names(game)
    minutes = float(game.get("BackupIntervalMinutes") or settings.get("BackupIntervalMinutes", 5) or 0)
    interval = minutes * 60
    setup_logging("Service-" + name, device_name)
    record_played(device_name, name)
    log.info("Monitoring %s on %s", name, device_name)
    log.info("Watching for: %s", ", ".join(names) if names else "(no program names set - timed backups only; Ctrl+C to stop)")
    log.info("Timed backups every %s minute(s)", f"{minutes:g}" if interval else "- never")

    def backup(reason):
        with BACKUP_LOCK:
            result, nas_status = post_game_backup(game, settings, platform, device_name)

        log.info("%s: %s | NAS: %s", reason, result, nas_status)

    try:
        if not pre_launch_check(game, settings, platform, device_name):
            log.info("Cancelled.")
            return 1
    except Exception:
        log.exception("Cloud check failed - carrying on")

    backup("Starting backup")
    seen = False
    closed = False
    next_backup = time.time() + interval if interval else None

    try:
        while True:
            running = process_running_any(names) if names else True

            if running:
                seen = True
            elif seen:
                closed = True
                break

            if next_backup and time.time() >= next_backup:
                if running:
                    backup("Timed backup")

                next_backup = time.time() + interval

            remaining = max(0, int(next_backup - time.time())) if next_backup else 0
            state_text = "running" if (running and names) else ("waiting for the game to start" if names else "monitoring")
            line = f"{name} | {state_text}" + (f" | next backup in {remaining // 60:02d}:{remaining % 60:02d}" if next_backup else "")
            sys.stdout.write("\033]0;" + line + "\007\r" + line + "   ")
            sys.stdout.flush()
            time.sleep(5)
    except KeyboardInterrupt:
        print()
        log.info("Stopped by the user - taking a final backup.")

    print()

    if closed:
        log.info("The game closed.")
        time.sleep(int(settings.get("WaitAfterGameExitSeconds", 5)))

    backup("Final backup")
    return 0


def service_menu(config, platform):
    """The home screen: Pinned, Favourites and Recent first, then search / lists / tools."""
    settings = config.get("Settings", {})
    device_name = get_device_name(settings, platform)
    games = config["Games"]

    while True:
        state = load_menu_state(device_name)
        here = available_games(games, settings, platform)
        visible = [g for g in here if not game_flag(state, g, "Hidden")]
        hidden = [g for g in here if game_flag(state, g, "Hidden")]
        pinned = [g for g in visible if game_flag(state, g, "Pinned")]
        favourites = [g for g in visible if game_flag(state, g, "Favourite") and g not in pinned]
        played = sorted(state["played"], key=lambda n: state["played"][n], reverse=True)
        recent = [g for n in played for g in visible if g["Name"] == n and g not in pinned and g not in favourites][:5]
        numbered = []

        print()
        print("=" * 52)
        print(f" Game Save Manager v{__version__}   {device_name} ({platform})")
        print("=" * 52)

        for title, items in (("Pinned", pinned), ("Favourites", favourites[:8]), ("Recent", recent)):
            if items:
                print(f"\n{title}\n{'-' * len(title)}")

                for game in items:
                    numbered.append(game)
                    print(f" {len(numbered):>2}  {game['Name']}")

        if not numbered:
            print("\n (No pinned, favourite or recent games yet - use S, A or C to find one.)")

        print()
        print("-" * 52)
        primary, fallback = get_roots(settings, platform)
        print(" S Search    A All games   C Categories  R Recent backups")
        print(" P Pinned    F Favourites  H Hidden      B Backup all now")
        print(" E Restore a game          L Steam launch line for a game")
        print(" N Add a game               M My IDs ({placeholders})")
        print(" D Dashboard   K Health check   G Settings   O Steam setup   U Upgrade")
        extras = ""

        if primary:
            extras += " Y Sync all (send + get newest)  "

        if platform != "Windows":
            extras += " I Desktop icon  "

        print(f"{extras} T More tools   Q Quit")
        print(f" ({len(visible)} games on this device, {len(games) - len(here)} have no save path here)")
        choice = ask("\nSelect (number or letter): ").strip().lower()
        picked = None

        if choice in ("q", ""):
            return 0
        elif choice.isdigit() and 1 <= int(choice) <= len(numbered):
            picked = numbered[int(choice) - 1]
        elif choice == "s":
            picked = choose_game(visible)
        elif choice == "a":
            picked = pick_game("All games", sorted(visible, key=lambda g: g["Name"].lower()), state)
        elif choice == "c":
            counts = {}

            for game in visible:
                for category in (game_categories(game, state) or ["(no category)"]):
                    counts.setdefault(category, []).append(game)

                if game.get("Platform"):
                    counts.setdefault("Platform: " + str(game["Platform"]), []).append(game)

            names = sorted(counts, key=str.lower)
            print()

            for number, category in enumerate(names, start=1):
                print(f" {number:>2}  {category}   ({len(counts[category])})")

            pick = ask("Select a category (0 = back): ").strip()

            if pick.isdigit() and 1 <= int(pick) <= len(names):
                category = names[int(pick) - 1]
                picked = pick_game(category, sorted(counts[category], key=lambda g: g["Name"].lower()), state)
        elif choice == "r":
            cmd_recent(argparse.Namespace(count=15), config, platform)
            ask("\nPress Enter to continue...")
        elif choice == "p":
            picked = pick_game("Pinned", pinned, state)
        elif choice == "f":
            picked = pick_game("Favourites", [g for g in visible if game_flag(state, g, "Favourite")], state)
        elif choice == "h":
            picked = pick_game("Hidden", hidden, state)
        elif choice == "b":
            cmd_backup(argparse.Namespace(game=None, skip=[], platform=None, dry_run=False, force_nas=False),
                       config, platform)
            ask("\nPress Enter to continue...")
        elif choice == "n":
            add_game_menu(config, platform)
        elif choice in ("d", "k", "o"):
            sys.path.insert(0, str(SCRIPT_DIR))

            try:
                if choice == "o":
                    import steamsetup
                    action = ask("Steam setup:  1 = set up shortcuts   2 = put a backup file back   "
                                 "3 = RESET (remove the manager's lines)   4 = RELINK (program was moved)   (Enter = 1): ").strip()
                    steamsetup.run(sys.modules[__name__], config, platform, undo=(action == "2"),
                                   reset=(action == "3"), relink=(action == "4"))
                else:
                    import healthcheck
                    (healthcheck.dashboard if choice == "d" else healthcheck.health)(sys.modules[__name__], config, platform)
            except ImportError as error:
                print(f"A needed file is missing next to the program ({error}).")

            ask("\nPress Enter to continue...")
        elif choice == "g":
            cmd_settings(argparse.Namespace(), config, platform)
        elif choice == "u":
            cmd_upgrade(argparse.Namespace(source=None, rollback=False), config, platform)
            ask("\nPress Enter to continue...")
        elif choice == "m":
            cmd_ids(argparse.Namespace(), config, platform)
            ask("\nPress Enter to continue...")
        elif choice == "e":
            cmd_restore(argparse.Namespace(game=None), config, platform)
            ask("\nPress Enter to continue...")
        elif choice == "l":
            cmd_launchoption(argparse.Namespace(game=None), config, platform)
            ask("\nPress Enter to continue...")
        elif choice == "y" and primary:
            cmd_syncall(argparse.Namespace(only="", skip="", yes=False), config, platform)
            ask("\nPress Enter to continue...")
        elif choice == "i" and platform != "Windows":
            cmd_desktopicon(argparse.Namespace(), config, platform)
            ask("\nPress Enter to continue...")
        elif choice == "t":
            tools_menu(config, platform)

        if picked is not None:
            game_menu(picked, config, platform)


def cmd_service(args, config, platform):
    """python savemanager.py service ["game"]  - the menu, or straight into monitoring one game."""
    if not args.game:
        return service_menu(config, platform)

    matches = find_games(config["Games"], args.game)
    game = matches[0] if len(matches) == 1 else choose_game(config["Games"], args.game)

    if game is None:
        return 1

    return monitor_game(game, config, platform)


def relaunch_in_terminal(argv):
    """Started with no keyboard window (a Steam shortcut, a desktop icon)? Reopen inside Konsole."""
    if sys.platform.startswith("win") or os.environ.get("SAVEMANAGER_TERM"):
        return False

    if sys.stdin is not None and sys.stdin.isatty():
        return False

    terminal = shutil.which("konsole")

    if not terminal:
        return False

    env = dict(os.environ, SAVEMANAGER_TERM="1")

    try:
        # --nofork: this program waits until the Konsole window is closed. Without it Konsole detaches, this program
        # ends at once, and Steam (gaming mode) believes the "game" stopped and switches back to the library.
        subprocess.run([terminal, "--nofork", "-e", sys.executable, str(Path(__file__).resolve())] + list(argv), env=env)
    except OSError:
        return False

    return True


def tidy_old_backups():
    """Moves backup files that earlier versions left in the main folder into Backups/. Safe to run every time."""
    moved = 0

    for old in list(SCRIPT_DIR.glob("Games.json.bak-*")):
        BACKUP_GAMES.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(BACKUP_GAMES / old.name))
        moved += 1

    old_upgrades = SCRIPT_DIR / "Upgrade-backups"

    if old_upgrades.is_dir():
        BACKUP_UPGRADES.mkdir(parents=True, exist_ok=True)

        for folder in list(old_upgrades.iterdir()):          # list() first: do not move things while walking the folder
            if not (BACKUP_UPGRADES / folder.name).exists():
                shutil.move(str(folder), str(BACKUP_UPGRADES / folder.name))
                moved += 1

        try:
            old_upgrades.rmdir()                  # only works if it is empty now
        except OSError:
            pass

    if moved:
        print(f"Tidied {moved} old backup item(s) into the Backups folder.")


# ----------------------------------------------------------------------
# Start
# ----------------------------------------------------------------------
def run():
    parser = argparse.ArgumentParser(description=f"Game Save Manager v{__version__}")
    parser.add_argument("--os", choices=["Windows", "SteamDeck", "Linux"],
                        help="pretend to be another device (for testing)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="list games and their paths on this device").set_defaults(func=cmd_list)

    p_backup = sub.add_parser("backup", help="back up all games, or one game")
    p_backup.add_argument("game", nargs="?", help="game name (partial / fuzzy is fine)")
    p_backup.add_argument("--skip", action="append", default=[], help="exact game name to skip (repeatable)")
    p_backup.add_argument("--platform", help="only games whose Platform matches, e.g. Recomp")
    p_backup.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    p_backup.add_argument("--force-nas", action="store_true", help="back up to the NAS even if it looks slow")
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", help="restore a game from any device's backup")
    p_restore.add_argument("game", nargs="?", help="game name (partial / fuzzy is fine)")
    p_restore.set_defaults(func=cmd_restore)

    sub.add_parser("sync", help="copy local fallback backups to the NAS").set_defaults(func=cmd_sync)

    p_service = sub.add_parser("service", help="game menu, or monitor one game (timed backups + backup on close)")
    p_service.add_argument("game", nargs="?")
    p_service.set_defaults(func=cmd_service)

    sub.add_parser("switch", help="list Switch titles found on this device and name unknown ones").set_defaults(func=cmd_switch)

    p_cats = sub.add_parser("categories", help="give every game genres (Categories.json), fix 'Catagory' spellings")
    p_cats.add_argument("--online", action="store_true", default=None, help="also ask Steam for genres of games no rule knows")
    p_cats.add_argument("--offline", dest="online", action="store_false", help="never use the internet")
    p_cats.add_argument("--overwrite", action="store_true", help="also replace genres that are already there")
    p_cats.add_argument("--yes", action="store_true", help="no questions")
    p_cats.set_defaults(func=lambda a, c, p: run_module("categories", c, p, a) or 0)

    p_unify = sub.add_parser("switchunify", help="one save per Switch game for all emulators (converts old whole-folder entries)")
    p_unify.add_argument("--yes", action="store_true", help="no questions")
    p_unify.set_defaults(func=lambda a, c, p: run_module("switchunify", c, p, a) or 0)

    sub.add_parser("tools", help="restore, sync, launch options and other tools").set_defaults(
        func=lambda a, c, p: tools_menu(c, p))

    p_launch = sub.add_parser("launch", help="check for a newer save, run the game, back up afterwards")
    p_launch.add_argument("--force-nas", action="store_true", help="wait for the NAS backup however long it takes")
    p_launch.add_argument("game", help="game name as in Games.json")
    p_launch.add_argument("command", nargs=argparse.REMAINDER, help="-- then the game's command")
    p_launch.set_defaults(func=cmd_launch)

    p_recent = sub.add_parser("recent", help="recently backed-up games")
    p_recent.add_argument("--count", type=int, default=15)
    p_recent.set_defaults(func=cmd_recent)

    sub.add_parser("desktopicon", help="Steam Deck: make a double-click icon").set_defaults(func=cmd_desktopicon)

    p_group = sub.add_parser("launchgroup", help="launch a command with save sync for a whole group of games (e.g. Switch)")
    p_group.add_argument("group", help="category or platform name, e.g. Switch")
    p_group.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command")
    p_group.set_defaults(func=cmd_launchgroup)

    sub.add_parser("addgame", help="add a game to Games.json (catalogue, by playing, or by typing the folder)").set_defaults(
        func=lambda a, c, p: add_game_menu(c, p))

    p_up = sub.add_parser("upgrade", help="install a new version without touching your settings or Games.json")
    p_up.add_argument("source", nargs="?", help="new .zip or folder")
    p_up.add_argument("--rollback", action="store_true", help="restore the files from before the last upgrade")
    p_up.set_defaults(func=cmd_upgrade)

    p_all = sub.add_parser("syncall", help="send waiting saves to the NAS, then fetch the newest save for every game")
    p_all.add_argument("--only", default="", help='only games containing these names, e.g. "elden, dark souls"')
    p_all.add_argument("--skip", default="", help="leave these games out")
    p_all.add_argument("--yes", action="store_true", help="do not ask about conflicts (they are listed instead)")
    p_all.set_defaults(func=cmd_syncall)

    sub.add_parser("settings", help="change settings from a menu").set_defaults(func=cmd_settings)
    sub.add_parser("library", help="name the games found in per-game emulator folders").set_defaults(func=cmd_library)
    sub.add_parser("version", help="show the version").set_defaults(func=lambda a, c, p: print("Game Save Manager", __version__) or 0)

    p_steam = sub.add_parser("steamsetup", help="put the launch options into your non-Steam shortcuts (closes and reopens Steam)")
    p_steam.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p_steam.add_argument("--only", default="", help='only these shortcuts/games, e.g. "duskfade, blue dragon"')
    p_steam.add_argument("--skip", default="", help="leave these out")
    p_steam.add_argument("--undo", action="store_true", help="put back the shortcuts file from before the last setup")
    p_steam.add_argument("--reset", action="store_true", help="remove the manager's launch lines from every shortcut (keeps everything else)")
    p_steam.add_argument("--relink", action="store_true", help="point every manager launch line at THIS copy of the program (after moving it)")
    p_steam.set_defaults(func=lambda a, c, p: __import__("steamsetup").run(
        sys.modules[__name__], c, p, yes=a.yes, only=name_filter(a.only), skip=name_filter(a.skip),
        undo=a.undo, reset=a.reset, relink=a.relink) or 0)

    for name, helptext in (("health", "check Games.json, Tailscale, the NAS, space and permissions"),
                           ("dashboard", "stale backups, pending uploads, storage"),
                           ("verify", "check that backed-up files still match their fingerprint")):
        def make(command):
            def run_tool(args, cfg, plat):
                sys.path.insert(0, str(SCRIPT_DIR))
                module = __import__("steamsetup" if command == "steamsetup" else "healthcheck")
                function = {"health": "health", "dashboard": "dashboard", "verify": "verify", "steamsetup": "run"}[command]
                return getattr(module, function)(sys.modules[__name__], cfg, plat) or 0

            return run_tool

        sub.add_parser(name, help=helptext).set_defaults(func=make(name))

    sub.add_parser("ids", help="find your own IDs (Steam, Rockstar, Ubisoft...) and save them in MyIDs.json").set_defaults(func=cmd_ids)

    p_option = sub.add_parser("launchoption", help="print the Steam launch option line for a game")
    p_option.add_argument("game", nargs="?")
    p_option.set_defaults(func=cmd_launchoption)

    args = parser.parse_args()
    platform = args.os or detect_platform()

    # A menu started from a Steam shortcut (or from the Konsole it opens) inherits Steam's game variables, which
    # would make every question answer itself with "nothing" and the window close at once. Only a game launch keeps them.
    if args.command not in ("launch", "launchgroup"):
        for variable in ("SteamGameId", "SteamAppId", "SteamOverlayGameId"):
            os.environ.pop(variable, None)
    try:
        config = expand_games(load_config(), platform)
    except FileNotFoundError:
        if args.command not in ("version", "upgrade"):
            print(f"Games.json was not found in {SCRIPT_DIR}")
            print("Put Games.json in the same folder as savemanager.py and try again.")
            sys.exit(1)

        config = {"Settings": {}, "Games": []}

    REACH_TIMEOUT["seconds"] = float(config.get("Settings", {}).get("NasReachableSeconds", 5))

    if args.command not in ("launch", "launchgroup", "upgrade"):     # never slow down a game start / never touch an upgrade
        try:
            tidy_old_backups()
        except OSError as error:
            print(f"(Could not tidy old backups: {error})")

    if args.command in (None, "service", "tools", "restore") and relaunch_in_terminal(sys.argv[1:]):
        sys.exit(0)

    if args.command is None:
        sys.exit(service_menu(config, platform))

    code = args.func(args, config, platform)

    if args.command in ("launch", "launchgroup"):
        # Leave at once (even if a network thread is still stuck) so Steam never waits on us.
        logging.shutdown()
        os._exit(code if isinstance(code, int) else 0)

    sys.exit(code)


def main():
    """Wraps run() so a crash is always written to Logs/crash.log (a double-clicked window closes fast)."""
    logs_folder = SCRIPT_DIR / "Logs"

    try:
        logs_folder.mkdir(parents=True, exist_ok=True)

        if "launch" in sys.argv or "launchgroup" in sys.argv:
            with open(logs_folder / "launch-calls.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%d-%m-%Y %H:%M:%S}  {sys.argv}\n")
    except OSError:
        pass

    try:
        run()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except Exception:
        import traceback

        text = traceback.format_exc()
        print(text)

        try:
            with open(logs_folder / "crash.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%d-%m-%Y %H:%M:%S}  {sys.argv}\n{text}\n")
        except OSError:
            pass

        if sys.stdin is not None and sys.stdin.isatty():
            ask("Something went wrong (saved to Logs/crash.log). Press Enter to close...")
        sys.exit(1)


if __name__ == "__main__":
    main()

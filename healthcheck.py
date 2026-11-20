#!/usr/bin/env python3
"""
healthcheck.py - Health check, dashboard and verify for Game Save Manager (v14)
==============================================================================
Started from the manager's menu (K = Health check, D = Dashboard) or:
    python savemanager.py health | dashboard | verify
It only LOOKS (the one write is a tiny test file that is deleted at once).
"""

import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

GB = 1024 ** 3
COUNTS = {"ok": 0, "warn": 0, "fail": 0}


def say(level, text, detail=""):
    tag = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "info": "[info]"}[level]

    if level in COUNTS:
        COUNTS[level] += 1

    print(f"  {tag}  {text}")

    if detail:
        print(f"          {detail}")


def human(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"

        num /= 1024


def title(text):
    print()
    print(text)
    print("-" * len(text))


def read_meta(sm, folder):
    return sm.read_meta(folder)


# ----------------------------------------------------------------------
# HEALTH CHECK
# ----------------------------------------------------------------------
def check_games(sm, config, platform):
    title("Games.json")
    settings = config.get("Settings", {})

    try:
        raw, had_notes = sm.read_games_json()
    except Exception as error:
        say("fail", "Games.json cannot be read", str(error))
        return

    games = raw.get("Games", [])
    say("ok", f"Games.json is valid ({len(games)} entries)")

    if had_notes:
        say("info", "It has # note lines (fine for this program, but other tools may complain)")

    names = [str(g.get("Name", "")) for g in games]

    if any(not n for n in names):
        say("fail", "Some entries have no Name")

    duplicates = sorted({n for n in names if n and names.count(n) > 1})

    if duplicates:
        say("warn", f"{len(duplicates)} name(s) used more than once", ", ".join(duplicates[:5]))
    else:
        say("ok", "No duplicate game names")

    typos = [g["Name"] for g in games if "Catagory" in g]

    if typos:
        say("warn", f"{len(typos)} entries use the misspelled key 'Catagory' (it still works)", ", ".join(typos[:4]))

    # a program name that belongs to a different emulator entry
    mixed = []

    for game in games:
        for process in sm.process_names(game):
            own = set(re.findall(r"[a-z0-9]{4,}", str(game.get("Name", "")).lower()))

            for other in games:
                if other is game:
                    continue

                tokens = set(re.findall(r"[a-z0-9]{4,}", str(other.get("Name", "")).lower())) - own

                if any(t in process.lower() for t in tokens if t not in ("emu", "recomp", "save", "states", "full")):
                    mixed.append(f"{game['Name']}: '{process}'")
                    break

    if mixed:
        say("warn", "A Process name looks like it belongs to another entry", "; ".join(sorted(set(mixed))[:3]))

    here = [g for g in config["Games"] if sm.resolve_source(g, settings, platform)]
    say("info", f"{len(here)} games have a save path on this device, {len(config['Games']) - len(here)} do not")

    placeholders = []
    missing_folder = []
    seen = {}

    for game in here:
        needed = sm.unresolved_variables(game, settings, platform)

        if needed:
            placeholders.append(f"{game['Name']} ({', '.join(needed)})")

        save_set = sm.get_save_set(game, settings, platform)

        if save_set and not save_set.exists() and not game.get("Virtual"):
            missing_folder.append(game["Name"])

        if save_set and not game.get("Virtual"):
            seen.setdefault(save_set.describe().lower(), []).append(game["Name"])

    if placeholders:
        say("warn", f"{len(placeholders)} game(s) still need one of your IDs (menu M)", "; ".join(placeholders[:4]))
    else:
        say("ok", "No unresolved {placeholders}")

    if missing_folder:
        say("info", f"{len(missing_folder)} save folder(s) do not exist on this device yet (normal for games not played here)",
            ", ".join(missing_folder[:6]) + (" ..." if len(missing_folder) > 6 else ""))

    shared = [names for names in seen.values() if len(names) > 1]

    if shared:
        say("warn", "Two entries point at the same folder (backed up twice)", " + ".join(shared[0]))


def check_settings(sm, config, platform):
    title("Settings")
    settings = config.get("Settings", {})
    primary, fallback = sm.get_roots(settings, platform)
    say("ok" if primary else "warn", f"NAS backup folder: {primary or 'not set for ' + platform}")
    say("ok" if fallback else "warn", f"Local backup folder: {fallback or 'not set for ' + platform}")
    say("info", f"This device is called '{sm.get_device_name(settings, platform)}'")

    for key in ("VersionCount", "BackupIntervalMinutes", "NasPushSeconds"):
        value = settings.get(key)

        if value is not None and (not isinstance(value, (int, float)) or value < 0):
            say("warn", f"{key} should be a number of 0 or more (it is {value!r})")


def check_network(sm, config, platform):
    title("Tailscale and the NAS")
    settings = config.get("Settings", {})
    exe = shutil.which("tailscale")

    if not exe and platform != "Windows":
        for candidate in ("/opt/tailscale/tailscale", "/usr/bin/tailscale"):
            if Path(candidate).exists():
                exe = candidate

    if exe:
        try:
            result = subprocess.run([exe, "status"], capture_output=True, text=True, timeout=8)
            say("ok" if result.returncode == 0 else "warn", "Tailscale is running" if result.returncode == 0
                else "Tailscale is installed but not connected", (result.stderr or result.stdout).strip().splitlines()[0][:100]
                if result.returncode != 0 and (result.stderr or result.stdout).strip() else "")
        except (OSError, subprocess.TimeoutExpired):
            say("warn", "Tailscale did not answer")
    else:
        say("info", "Tailscale is not installed here (fine if you only use the NAS at home)")

    primary, fallback = sm.get_roots(settings, platform)

    if not primary:
        return

    if not sm.reachable(primary):
        say("fail", f"The NAS folder is not reachable: {primary}", "Is the NAS mounted / connected? (Deck: NasMount.sh)")
    else:
        say("ok", "The NAS folder is reachable")
        probe = sm.probe_nas(primary)

        if probe is None:
            say("warn", "The NAS did not finish a small write test (very slow or read-only)")
        else:
            delay, kbps = probe
            say("ok" if kbps > 300 else "warn", f"NAS speed about {kbps:.0f} KB/s", "" if kbps > 300 else "Slow: uploads will be left for later")

        test = Path(primary) / f".health-{os.getpid()}.tmp"

        try:
            test.write_text("x", encoding="utf-8")
            skew = abs(time.time() - test.stat().st_mtime)
            test.unlink()
            say("ok", "You can write to the NAS folder")
            say("ok" if skew < 120 else "warn", f"Clock difference with the NAS: about {skew:.0f} seconds",
                "" if skew < 120 else "Time zones do not matter, but a wrong clock can mix up which save is newer. Turn on automatic time.")
        except OSError as error:
            say("fail", "Cannot write to the NAS folder", str(error)[:100])

        try:
            free = shutil.disk_usage(primary).free
            say("ok" if free > 5 * GB else "warn", f"NAS free space: {human(free)}")
        except OSError:
            pass

    if fallback:
        try:
            if sm.reachable(fallback):
                test = Path(fallback) / f".health-{os.getpid()}.tmp"
                test.write_text("x", encoding="utf-8")
                test.unlink()
                free = shutil.disk_usage(fallback).free
                say("ok" if free > 2 * GB else "warn", f"Local backup folder is writable, free space {human(free)}")
            else:
                say("warn", f"The local backup folder does not exist: {fallback}")
        except OSError as error:
            say("fail", "Cannot write to the local backup folder", str(error)[:100])

    if platform == "SteamDeck" and primary:
        try:
            fstab = Path("/etc/fstab").read_text(encoding="utf-8")
            ours = "game-save-manager" in fstab
        except OSError:
            ours = False

        say("ok" if ours else "info", "The NAS mounts automatically at boot" if ours
            else "The NAS does not mount by itself after a restart (optional: run NasAutoMount.sh)")


def pending_uploads(sm, config, platform):
    """Games saved locally whose copy on the NAS is missing or different."""
    settings = config.get("Settings", {})
    primary, fallback = sm.get_roots(settings, platform)

    if not (primary and fallback and sm.reachable(primary) and sm.reachable(fallback)):
        return None

    pending = []

    for folder in sorted(Path(fallback).iterdir()):
        if not (folder / "Current").exists():
            continue

        local = sm.read_meta(folder).get("digest")
        remote = sm.read_meta(Path(primary) / folder.name).get("digest") if (Path(primary) / folder.name / "Current").exists() else None

        if local != remote:
            pending.append(folder.name)

    return pending


def check_pending(sm, config, platform):
    title("Uploads and markers")
    pending = pending_uploads(sm, config, platform)

    if pending is None:
        say("info", "Pending uploads could not be checked (the NAS or the local folder is not reachable)")
    elif pending:
        say("warn", f"{len(pending)} game(s) saved locally are not on the NAS yet", ", ".join(pending[:6]) + " (menu Y or the next launch sends them)")
    else:
        say("ok", "Everything saved locally is on the NAS")

    primary, _ = sm.get_roots(config.get("Settings", {}), platform)

    if primary and sm.reachable(primary):
        stuck = []

        for folder in Path(primary).iterdir():
            marker = folder / sm.PUSH_MARKER

            if marker.exists() and time.time() - marker.stat().st_mtime > 7200:
                stuck.append(folder.name)

        if stuck:
            say("warn", f"{len(stuck)} upload(s) look stuck (marker older than 2 hours)", ", ".join(stuck[:4]))


def check_steam(sm, config, platform):
    title("Steam shortcuts")

    try:
        import steamsetup
    except ImportError:
        say("info", "steamsetup.py is missing next to savemanager.py")
        return

    paths = steamsetup.shortcut_files(sm, platform)

    if not paths:
        say("info", "No non-Steam shortcuts file found (fine if you do not use Steam shortcuts on this device)")
        return

    here = os.path.normcase(os.path.normpath(str(Path(sm.__file__).resolve())))
    total = 0
    wrong = []

    for path in paths:
        for name, script in steamsetup.managed_scripts(path):
            total += 1

            if os.path.normcase(os.path.normpath(script)) != here:
                wrong.append(f"{name} -> {script}")

    if not total:
        say("info", "No shortcut has the manager's launch line yet (menu O sets it up)")
    elif wrong:
        say("warn", f"{len(wrong)} of {total} shortcut(s) start a DIFFERENT copy of the program", "; ".join(wrong[:3]) + (" ..." if len(wrong) > 3 else ""))
        say("info", "Fix: menu O, then 4 (relink). Do this whenever the program folder moves.")
    else:
        say("ok", f"All {total} shortcut(s) with the manager's launch line start this program")


def check_program(sm):
    title("The program")
    say("info", f"Version {sm.__version__}, Python {sys.version.split()[0]}")

    for name in ("findsaves.py", "gameinfo.py", "steamsetup.py", "healthcheck.py", "Categories.json", "SwitchTitles.json"):
        if not (sm.SCRIPT_DIR / name).exists():
            say("warn", f"{name} is missing next to savemanager.py")


def health(sm, config, platform):
    COUNTS.update(ok=0, warn=0, fail=0)
    print()
    print("=" * 56)
    print(f" HEALTH CHECK   {sm.get_device_name(config.get('Settings', {}), platform)} ({platform})")
    print("=" * 56)

    for check in (check_games, check_settings, check_network, check_pending, check_steam):
        try:
            check(sm, config, platform)
        except Exception as error:
            say("fail", f"{check.__name__} crashed", repr(error)[:120])

    check_program(sm)
    print()
    print(f"Result: {COUNTS['ok']} ok, {COUNTS['warn']} warnings, {COUNTS['fail']} problems")
    return 0 if COUNTS["fail"] == 0 else 1


# ----------------------------------------------------------------------
# DASHBOARD
# ----------------------------------------------------------------------
def folder_size(path, limit_seconds=15):
    total = 0
    started = time.time()

    for folder, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass

        if time.time() - started > limit_seconds:
            return total, False

    return total, True


def dashboard(sm, config, platform):
    settings = config.get("Settings", {})
    days = int(settings.get("StaleBackupDays", 14))
    primary, fallback = sm.get_roots(settings, platform)
    print()
    print("=" * 56)
    print(f" DASHBOARD   {sm.get_device_name(settings, platform)} ({platform})")
    print("=" * 56)
    metas = {}

    for where, root in (("NAS", primary), ("Local", fallback)):
        if not root or not sm.reachable(root):
            continue

        for folder in Path(root).iterdir():
            meta = sm.read_meta(folder) if folder.is_dir() else {}

            if meta.get("last_backup"):
                name = meta.get("game") or folder.name
                stamp = datetime.fromisoformat(meta["last_backup"])

                if name not in metas or stamp > metas[name][0]:
                    metas[name] = (stamp, meta.get("last_device", "?"), where)

    here = [g for g in config["Games"] if sm.resolve_source(g, settings, platform) and not g.get("Hidden")]
    title("Not backed up for more than %d days (games that exist on this device)" % days)
    now = datetime.now()
    stale = []
    never = []

    for game in here:
        save_set = sm.get_save_set(game, settings, platform)

        if not save_set or not save_set.exists():
            continue

        if game["Name"] not in metas:
            never.append(game["Name"])
        elif (now - metas[game["Name"]][0]).days >= days:
            stale.append((metas[game["Name"]][0], game["Name"]))

    if never:
        say("warn", f"{len(never)} game(s) have a save here but have NEVER been backed up", ", ".join(never[:8]) + (" ..." if len(never) > 8 else ""))

    for stamp, name in sorted(stale)[:15]:
        say("warn", f"{name}: last backup {stamp:%d/%m/%y} ({(now - stamp).days} days ago)")

    if not never and not stale:
        say("ok", "Every game that has a save here was backed up recently")

    title("Uploads still waiting")
    pending = pending_uploads(sm, config, platform)

    if pending is None:
        say("info", "Not checked (NAS or local folder not reachable)")
    elif pending:
        for name in pending[:15]:
            say("warn", f"{name} is saved locally but not on the NAS yet")
    else:
        say("ok", "Nothing waiting")

    title("Most recent backups")

    for name, (stamp, device, where) in sorted(metas.items(), key=lambda item: item[1][0], reverse=True)[:6]:
        print(f"  {stamp:%d/%m/%y %H:%M}  {name}   (by {device}, {where})")

    title("Storage")

    if fallback and sm.reachable(fallback):
        size, complete = folder_size(fallback)
        say("info", f"Local backups use {human(size)}" + ("" if complete else " (at least - stopped counting)"))
        try:
            say("info", f"Free space there: {human(shutil.disk_usage(fallback).free)}")
        except OSError:
            pass

    if primary and sm.reachable(primary):
        try:
            say("info", f"NAS free space: {human(shutil.disk_usage(primary).free)}")
        except OSError:
            pass

    say("info", f"{len(metas)} games have a backup record")
    return 0


# ----------------------------------------------------------------------
# VERIFY
# ----------------------------------------------------------------------
def verify(sm, config, platform):
    """Re-reads every Current backup and checks it still matches the fingerprint written when it was backed up."""
    settings = config.get("Settings", {})
    primary, fallback = sm.get_roots(settings, platform)
    root = primary if primary and sm.reachable(primary) else fallback

    if not root or not sm.reachable(root):
        print("No backup folder is reachable.")
        return 1

    only = sm.ask("Check only games containing this text (Enter = all): ").strip().lower()
    folders = [f for f in sorted(Path(root).iterdir()) if (f / "Current").exists() and only in f.name.lower()]
    print(f"\nChecking {len(folders)} backup(s) in {root} (reads every file, so it can take a while) ...")
    bad = []
    unknown = 0

    for number, folder in enumerate(folders, start=1):
        meta = sm.read_meta(folder)

        if not meta.get("digest"):
            unknown += 1
            continue

        try:
            digest = sm.folder_digest(sm.list_files(folder / "Current", [], []))
        except OSError as error:
            bad.append((folder.name, f"cannot read: {error}"))
            continue

        if digest != meta["digest"]:
            bad.append((folder.name, "files differ from the fingerprint"))

        if number % 10 == 0:
            print(f"  ...{number} of {len(folders)}")

    print()

    if bad:
        for name, why in bad:
            say("warn", f"{name}: {why}", "Its saved copy changed after it was backed up (or the previous upload was cut off). Back it up again.")
    else:
        say("ok", f"All {len(folders) - unknown} checked backups match their fingerprints")

    if unknown:
        say("info", f"{unknown} older backup(s) have no fingerprint yet (they get one at their next backup or sync)")

    return 1 if bad else 0

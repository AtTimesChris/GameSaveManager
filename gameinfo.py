#!/usr/bin/env python3
"""
gameinfo.py - look a game up on the internet (Steam + PCGamingWiki) and add it to Games.json
============================================================================================
Menu: N (Add a game) > 4.  Or:  python savemanager.py addgame
Finds the Steam app ID, genres/tags and the save folder PCGamingWiki lists, turns it into a path for
THIS device (Windows folders, or the Proton folder on the Steam Deck), checks which one exists, and writes
the entry to Games.json (a backup copy is made first).

Needs internet only for the lookup. Nothing is sent except the game name / app ID in the web address.
The web addresses used (Steam's public store API, PCGamingWiki's public pages) are listed below in case
they ever change.
"""

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "GameSaveManager/14 (personal save backup tool)"
STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/?term={term}&l=english&cc=us"
STEAM_DETAILS = "https://store.steampowered.com/api/appdetails?appids={appid}&l=english"
STEAMSPY = "https://steamspy.com/api.php?request=appdetails&appid={appid}"
PCGW_BY_APPID = "https://www.pcgamingwiki.com/api/appid.php?appid={appid}"
PCGW_SEARCH = "https://www.pcgamingwiki.com/w/api.php?action=opensearch&search={term}&limit=5&format=json"
PCGW_RAW = "https://www.pcgamingwiki.com/w/index.php?title={title}&action=raw"


def http_get(url, timeout=12):
    """Returns (final url, text). Raises OSError on any network problem."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.geturl(), response.read().decode("utf-8", "replace")


def get_json(url):
    return json.loads(http_get(url)[1])


# ---------------------------------------------------------------- Steam
def steam_search(name):
    data = get_json(STEAM_SEARCH.format(term=urllib.parse.quote(name)))
    return [(item["id"], item["name"]) for item in data.get("items", [])[:6]]


def steam_genres(appid):
    """Steam's own genres (Action, RPG ...)."""
    data = get_json(STEAM_DETAILS.format(appid=appid)).get(str(appid), {})

    if not data.get("success"):
        return []

    return [g["description"] for g in data["data"].get("genres", [])]


def steam_tags(appid, count=4):
    """The user-voted store tags (Roguelike, Souls-like ...) from SteamSpy. Optional; returns [] on any problem."""
    try:
        tags = get_json(STEAMSPY.format(appid=appid)).get("tags", {})
    except (OSError, ValueError):
        return []

    if not isinstance(tags, dict):
        return []

    return [name for name, _ in sorted(tags.items(), key=lambda item: item[1], reverse=True)[:count]]


# ---------------------------------------------------------------- PCGamingWiki
def pcgw_title(appid=None, name=None):
    """The wiki page name for a game (by Steam app ID, else by name)."""
    if appid:
        try:
            final, _ = http_get(PCGW_BY_APPID.format(appid=appid))
            match = re.search(r"/wiki/([^?#]+)", final)

            if match:
                return urllib.parse.unquote(match.group(1)).replace("_", " ")
        except OSError:
            pass

    if name:
        data = get_json(PCGW_SEARCH.format(term=urllib.parse.quote(name)))

        if len(data) > 1 and data[1]:
            return data[1][0]

    return None


def pcgw_wikitext(title):
    return http_get(PCGW_RAW.format(title=urllib.parse.quote(title.replace(" ", "_"))))[1]


P_TOKENS = {
    "userprofile": "%USERPROFILE%", "appdata": "%APPDATA%", "localappdata": "%LOCALAPPDATA%",
    "programdata": "%PROGRAMDATA%", "public": "C:\\Users\\Public", "username": "%USERNAME%",
    "savedgames": "%USERPROFILE%\\Saved Games", "documents": "%USERPROFILE%\\Documents",
    "uid": "{SteamUserID}", "steam": "{SteamRoot}", "game": "{GameInstall}",
}


def convert_tokens(text):
    """{{p|userprofile\\Documents}} style tokens -> %USERPROFILE%\\Documents ; returns None for registry paths."""
    def replace(match):
        inner = match.group(1)
        head, _, rest = inner.partition("\\")
        base = P_TOKENS.get(head.strip().lower())
        return None if base is None else base + ("\\" + rest if rest else "")

    if re.search(r"\{\{p\|\s*hk", text, re.I):
        return None

    out = text

    for _ in range(3):
        new = re.sub(r"\{\{p\|([^{}|]+)\}\}", lambda m: replace(m) or "\0", out, flags=re.I)

        if new == out:
            break

        out = new

    out = re.sub(r"<!--.*?-->", "", out)
    out = out.replace("''", "").replace("<br>", "").strip()
    return None if "\0" in out else out


def parse_save_paths(wikitext):
    """[(os, path)] from {{Game data/saves|Windows|...}} lines. os is Windows / Linux / macOS / other."""
    results = []
    start = 0

    while True:
        found = wikitext.find("{{Game data/saves|", start)

        if found < 0:
            break

        depth = 0
        index = found

        while index < len(wikitext):
            if wikitext.startswith("{{", index):
                depth += 1
                index += 2
            elif wikitext.startswith("}}", index):
                depth -= 1
                index += 2

                if depth == 0:
                    break
            else:
                index += 1

        block = wikitext[found + 2:index - 2]
        start = index
        parts = []
        piece = ""
        level = 0
        i = 0

        while i < len(block):
            if block.startswith("{{", i):
                level += 1
                piece += "{{"
                i += 2
            elif block.startswith("}}", i):
                level -= 1
                piece += "}}"
                i += 2
            elif block[i] == "|" and level == 0:
                parts.append(piece)
                piece = ""
                i += 1
            else:
                piece += block[i]
                i += 1

        parts.append(piece)

        if len(parts) < 3:
            continue

        platform = parts[1].strip()

        for raw in parts[2:]:
            path = convert_tokens(raw)

            if not path or "{GameInstall}" in path:
                continue

            # a file pattern like ...\*.sav or ...\slot1.dat means: the folder it is in
            last = re.split(r"[\\/]", path.rstrip("\\/"))[-1]

            if "*" in last or re.search(r"\.\w{2,4}$", last):
                path = path[:max(path.rfind("\\"), path.rfind("/"))]

            results.append((platform, path.rstrip("\\/")))

    return results


# ---------------------------------------------------------------- converting for this device
def proton_path(windows_path, compat_root, appid, steam_user_id=None):
    """A Windows-style path -> the same place inside the game's Proton prefix on the Deck."""
    prefix = Path(compat_root) / str(appid) / "pfx" / "drive_c"
    text = windows_path.replace("/", "\\")
    table = [("%USERPROFILE%", prefix / "users" / "steamuser"),
             ("%APPDATA%", prefix / "users" / "steamuser" / "AppData" / "Roaming"),
             ("%LOCALAPPDATA%", prefix / "users" / "steamuser" / "AppData" / "Local"),
             ("%PROGRAMDATA%", prefix / "ProgramData")]

    for token, base in table:
        if text.upper().startswith(token):
            rest = text[len(token):].strip("\\")
            return str(base.joinpath(*[p for p in rest.split("\\") if p])) if rest else str(base)

    if text.startswith("{SteamRoot}"):
        # Steam cloud style folders live in the real Steam folder, not in the prefix
        rest = text[len("{SteamRoot}"):].strip("\\")
        base = Path.home() / ".local" / "share" / "Steam"
        text = str(base.joinpath(*[p for p in rest.split("\\") if p])) if rest else str(base)
        return text.replace("{SteamUserID}", steam_user_id or "{SteamUserID}")

    match = re.match(r"^[Cc]:\\(.*)$", text)

    if match:
        return str(prefix.joinpath(*[p for p in match.group(1).split("\\") if p]))

    return None


def compat_roots():
    """Steam libraries on the Deck that can hold compatdata (internal storage and the SD card)."""
    roots = [Path.home() / ".local" / "share" / "Steam" / "steamapps" / "compatdata"]
    media = Path("/run/media")

    if media.exists():
        for entry in media.iterdir():
            roots.append(entry / "steamapps" / "compatdata")

            if entry.is_dir():
                for sub in entry.iterdir():
                    roots.append(sub / "steamapps" / "compatdata")

    return roots


def deck_steam_user():
    userdata = Path.home() / ".local" / "share" / "Steam" / "userdata"

    try:
        ids = [d.name for d in userdata.iterdir() if d.is_dir() and d.name.isdigit() and d.name != "0"]
    except OSError:
        return None

    return ids[0] if len(ids) == 1 else None


def candidates_for_device(sm, platform, appid, wiki_paths):
    """Paths for THIS device, existing ones first. Returns [(path, note, exists)]."""
    found = []
    settings = {"Variables": {platform: {}}}

    if platform == "Windows":
        for kind, path in wiki_paths:
            if kind.lower().startswith("windows"):
                expanded = sm.expand_path(path, settings, platform)
                found.append((path, "Windows save folder listed on PCGamingWiki", Path(expanded).exists()))
    else:
        for kind, path in wiki_paths:
            if kind.lower().startswith("linux"):
                found.append((path, "native Linux folder listed on PCGamingWiki", Path(path).expanduser().exists()))

        if appid:
            roots = compat_roots()
            real_root = next((r for r in roots if (r / str(appid)).exists()), roots[0])

            for kind, path in wiki_paths:
                if kind.lower().startswith("windows"):
                    converted = proton_path(path, real_root, appid, deck_steam_user())

                    if converted:
                        found.append((converted, f"the Windows folder inside the game's Proton prefix (ID {appid})", Path(converted).exists()))

    seen = set()
    unique = []

    for path, note, exists in found:
        if path not in seen:
            seen.add(path)
            unique.append((path, note, exists))

    return sorted(unique, key=lambda item: not item[2])


# ---------------------------------------------------------------- the interactive flow
def lookup_flow(sm, config, platform):
    print()
    name = sm.ask("Game name to look up (Steam name is best): ").strip()

    if not name:
        return

    try:
        results = steam_search(name)
    except (OSError, ValueError):
        print("Could not reach Steam's store (no internet?). You can still add the game with options 1-3.")
        return

    appid = None
    steam_name = name

    if results:
        print("\nSteam found:")

        for number, (found_id, found_name) in enumerate(results, start=1):
            print(f"  {number}  {found_name}   (app {found_id})")

        print("  0  None of these (not on Steam / skip)")
        pick = sm.ask("Which one? (Enter = 1): ").strip()

        if pick != "0":
            index = int(pick) - 1 if pick.isdigit() and 1 <= int(pick) <= len(results) else 0
            appid, steam_name = results[index]
    else:
        print("Steam has no game with that name.")

    genres = []

    if appid:
        try:
            genres = steam_genres(appid)
            genres += [t for t in steam_tags(appid) if t not in genres]
        except (OSError, ValueError):
            print("(Could not read the genres.)")

    wiki_paths = []

    try:
        title = pcgw_title(appid, steam_name)

        if title:
            wiki_paths = parse_save_paths(pcgw_wikitext(title))
            print(f"\nPCGamingWiki page: {title} - {len(wiki_paths)} save location(s) listed")
        else:
            print("\nPCGamingWiki has no page for this game.")
    except (OSError, ValueError):
        print("\nCould not read PCGamingWiki.")

    candidates = candidates_for_device(sm, platform, appid, wiki_paths)
    key = sm.source_key(platform)
    chosen = None

    if candidates:
        print(f"\nSave folder(s) for THIS device ({platform}):")

        for number, (path, note, exists) in enumerate(candidates, start=1):
            print(f"  {number}  {path}\n       {note}{'   [exists here now]' if exists else '   [not created yet]'}")

        pick = sm.ask("Use which? (number, Enter = 1, 0 = none / I will type it): ").strip()

        if pick != "0":
            index = int(pick) - 1 if pick.isdigit() and 1 <= int(pick) <= len(candidates) else 0
            chosen = candidates[index][0]
    else:
        print("\nNo suggested save folder for this device.")

    if not chosen:
        typed = sm.ask("Save folder (paste it, or Enter to add the game without one): ").strip().strip('"')
        chosen = typed or None

    value = sm.portable_path(chosen, platform) if chosen else None
    category = ", ".join(genres[:5])
    print(f"\nWill add:  {name}   {key}: {value or '(none yet)'}   Steam app: {appid or '-'}   Categories: {category or '-'}")

    if sm.ask("Save to Games.json? [Y/n]: ").strip().lower() == "n":
        return

    def change(data):
        game = sm.find_raw_game(data, name)
        fields = {}

        if value:
            fields[key] = value

        if appid:
            fields["SteamAppID"] = appid

        if category:
            fields["Category"] = category

        if game is None:
            entry = {"Name": name}
            entry.update(fields)

            if appid:
                entry["Platform"] = "Steam"

            data["Games"].append(entry)
        else:
            for field, field_value in fields.items():
                game.setdefault(field, field_value) if field != key else game.__setitem__(key, field_value)

    backup = sm.edit_games_json(change)

    if backup:
        print(f"Saved. (Old file kept as {backup.name}.) Restart the manager to see it.")
        sm.reload_config(config, platform)

#!/usr/bin/env python3
"""Every decision about the Core Keeper server's files, and the hub's commands.

    fluxck.py apply                  write ServerConfig.json, Admins.json and PlayerBans.json from
                                     the environment and the panel's settings; print the extra
                                     command-line arguments, one per line (the entrypoint)
    fluxck.py state <state> [msg]    record what the supervisor is doing (flux/state.json)

    flux-ck status                   state, connection, players, world, build (one JSON document)
    flux-ck connect                  how to join: Game ID, and address + password when direct
    flux-ck settings                 the panel's settings and the server's own config
    flux-ck set-settings '<json>'    {"world": 2, "password": "...", "gameId": "...", "seed": "...",
                                     "worldMode": "Hard", "season": "Halloween", "activateAllContent": true,
                                     "allowOnlyPlatform": "", "maxPlayers": 8, "worldName": "..."}
    flux-ck worlds                   the 30 world slots: which exist, their size and age
    flux-ck admins                   admins and bans, and the players seen (to pick from)
    flux-ck set-admins '<json>'      {"admins": [<steamId>...], "bans": [<id>...]}
    flux-ck mods                     the installed mods (ModManifest.json), on or off
    flux-ck mod-install <zip>        unpack a mod archive into Mods/
    flux-ck mod-enable <dir> on|off  switch a mod on or off (off = moved to Mods.disabled/, kept)
    flux-ck mod-remove <dir>         delete a mod
    flux-ck mod-install-url <url>    download a mod archive from mod.io's CDN and install it
    flux-ck restart                  save, stop and start the server again (applies the above)
    flux-ck backups                  the backups in flux/backups (name, size, date, why)
    flux-ck restore <backup>         restore one: on the next start (the server is restarted now),
                                     after a safety backup of what is there
    flux-ck new-game-id              a valid random Game ID (28 characters)

    fluxck.py backup <why> [force]   make a backup (the supervisor): skipped when the newest is
                                     younger than FLUX_BACKUP_MIN_AGE_MINUTES unless forced; prunes
    fluxck.py do-restore             apply a pending restore (the supervisor, before a start)
    fluxck.py note <key> [value]     a field of flux/state.json the supervisor keeps (nextRestart,
                                     updateWaiting); no value removes it
    fluxck.py update-target <build>  remember the build a planned update restart is for
    fluxck.py update-check           after the update step: a target that was not reached is
                                     remembered as failed (the watcher skips it for 6 hours)
    fluxck.py update-skip <build>    exit 0 when that build failed less than 6 hours ago

Settings changed by the panel, admins and bans are applied on the NEXT start: the server owns
its files while it runs and rewrites them as it stops (measured: Admins.json and
ServerConfig.json are written back on quit), so an edit made under it would be undone.

Precedence: an environment variable that is SET to a value is applied on every start; one that is
not set, or set EMPTY (the marketplace writes every declared parameter, blank ones included),
leaves the panel's value, and then the file's. Except a NEW world's mode and seed, where the
panel's choice wins and the environment is the default (see for_new_world).
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

SERVER_DIR = os.environ.get("FLUX_SERVER_DIR", "/mnt/corekeeper/server")
DATA_DIR = os.environ.get("FLUX_DATA_DIR", "/mnt/corekeeper/data")
GAME_DATA = os.environ.get("FLUX_GAME_DATA", os.path.join(DATA_DIR, "DedicatedServer"))
MODS_DIR = os.environ.get("FLUX_MODS_DIR", os.path.join(DATA_DIR, "Mods"))
STATE_DIR = os.environ.get("FLUX_STATE_DIR", os.path.join(DATA_DIR, "flux"))
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")
ADMIN_EDITS_FILE = os.path.join(STATE_DIR, "admins-pending.json")
SEEN_FILE = os.path.join(STATE_DIR, "players-seen.json")
# The game's log is on the INSTALL volume (local to the node, never synced): Core Keeper writes
# leak warnings into it for as long as it runs, and on the synced data volume that was steady
# Syncthing traffic and a log that could fill the plan's disk.
GAME_LOG = os.environ.get("FLUX_GAME_LOG", os.path.join(SERVER_DIR, "flux-logs", "current.log"))
# Where the status command keeps its place in the log, so a poll reads only what is new.
ROSTER_CACHE = os.environ.get("FLUX_ROSTER_CACHE", "/tmp/flux-roster.json")
BACKUP_DIR = os.environ.get("FLUX_BACKUP_DIR", os.path.join(STATE_DIR, "backups"))
BACKUP_INDEX = os.path.join(BACKUP_DIR, "index.json")
RESTORE_FILE = os.path.join(STATE_DIR, "restore-pending.json")
UPDATE_TARGET_FILE = os.path.join(STATE_DIR, "update-target.json")
UPDATE_FAILED_FILE = os.path.join(STATE_DIR, "update-failed.json")
UPDATE_FAILED_SKIP = 6 * 3600
RESTART_MARKER = os.environ.get("FLUX_RESTART_MARKER", "/tmp/flux-restart-requested")
BACKUP_NAME_RE = re.compile(r"^\d{8}-\d{6}-corekeeper\.tar\.gz$")
# A mod archive bigger than this, once unpacked, is refused (a zip bomb fills the plan's disk).
MOD_MAX_BYTES = int(os.environ.get("FLUX_MOD_MAX_BYTES", str(512 * 1024 * 1024)))
# mod.io's file CDN: where `mod-install-url` may download from, nowhere else.
MODIO_HOSTS = re.compile(r"^([a-z0-9-]+\.)*(mod\.io|modapi\.io|modcdn\.io)$")
# Mods that are off: OUTSIDE Mods/, which the game loads whole (see the Mods section below).
DISABLED_DIR = os.environ.get("FLUX_MODS_DISABLED_DIR", os.path.join(DATA_DIR, "Mods.disabled"))
# How image 1.0.0 switched a mod off, inside Mods/; moved out by migrate_disabled.
DISABLED_SUFFIX = ".disabled"

WORLD_SLOTS = 30
# How long after its session starts the server needs before a quit saves a new world.
SETTLE_SECONDS = 15
MAX_PLAYERS_CAP = 100
WORLD_MODES = ["Normal", "Hard", "Creative", "Casual"]
SEASONS = ["Auto", "None", "Easter", "Halloween", "Christmas", "Valentine", "Anniversary",
           "CherryBlossom", "LunarNewYear"]
# "All" (or empty) = cross-play: no -allowonlyplatform.
PLATFORMS = ["All", "Steam", "Epic", "Microsoft", "GOG"]

# What a fresh ServerConfig.json holds (written by build 1.3.0.6 on its first start).
CONFIG_DEFAULTS = {
    "gameId": "",
    "password": "",
    "world": 0,
    "worldName": "Core Keeper Server",
    "worldSeed": "",
    "hashedWorldSeed": 0,
    "maxNumberPlayers": 8,
    "maxNumberPacketsSentPerFrame": 1,
    "networkSendRate": 20,
    "worldMode": 0,
    "seasonOverride": -1,
}

# Game ID (ARGUMENTS.txt): 15 to 28 alphanumerics, none of Y, y, x, 0 or O. The server replaces
# an invalid one with a random one, which would silently change how everybody joins.
GAME_ID_RE = re.compile(r"^[A-NP-XZa-wz1-9]{15,28}$")
GAME_ID_ALPHABET = "ABCDEFGHIJKLMNPQRSTUVWXZabcdefghijklmnopqrstuvwz123456789"
PASSWORD_MAX = 28

# The log lines that make the roster (build 1.3.0.6; formats from the game's own strings).
JOIN_RE = re.compile(r"\[userid:(\d+)\] player (.*?) connected(?: islocalplayer=(\w+))?\s*$")
LEAVE_RE = re.compile(r"Disconnected from userid:(\d+)(?: with reason (.*))?\s*$")
SESSION_RE = re.compile(r"Started session with info: (.*?)\s*$")
VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+\.\d+-[0-9a-f]+)\b")
BAN_RE = re.compile(r"\[userid:(\d+)\] Ban player (.*?) \(")


# ------------------------------------------------------------------------------------------------
# Files
# ------------------------------------------------------------------------------------------------

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, indent=4):
    """Written beside and renamed, so a cut-off write never leaves half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".flux-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def out(data):
    print(json.dumps(data, ensure_ascii=False))


def fail(message, code=1):
    out({"ok": False, "error": message})
    sys.exit(code)


# ------------------------------------------------------------------------------------------------
# Values
# ------------------------------------------------------------------------------------------------

def env_set(name):
    """A variable counts only with a value: the marketplace writes every declared parameter, the
    ones left blank as NAME=, and a blank one must not wipe what the panel set."""
    return str(os.environ.get(name, "")).strip() != ""


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def clean_text(value, limit):
    """One line of printable text: it ends up on the server's command line and in a JSON file."""
    value = re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()
    return value[:limit]


def clean_password(value):
    """A join password: one line, and no ';', which splits the game's IP;PORT;;PASSWORD join line."""
    return clean_text(str(value).replace(";", ""), PASSWORD_MAX)


def direct_connections():
    """Whether CK_PORT gives direct connections (the entrypoint's flux_game_port: unset is 27015,
    'relay', 'off', 'none', '0' or empty are the Steam relay only)."""
    if "CK_PORT" not in os.environ:
        return True
    return str(os.environ["CK_PORT"]).strip().lower() not in ("", "relay", "off", "none", "0")


def valid_game_id(value):
    return bool(GAME_ID_RE.match(value or ""))


def max_players(value):
    try:
        n = int(str(value).strip())
    except ValueError:
        return None
    return min(max(n, 1), MAX_PLAYERS_CAP)


def pick(value, choices):
    """The choice that matches value ignoring case, or None."""
    for choice in choices:
        if str(value).strip().lower() == choice.lower():
            return choice
    return None


def world_index(value):
    try:
        n = int(str(value).strip())
    except ValueError:
        return None
    return n if 0 <= n < WORLD_SLOTS else None


def steam_id(value):
    """A SteamID64 as the number it is stored as, or None."""
    value = str(value).strip()
    if re.fullmatch(r"7656119\d{10}", value):
        return int(value)
    return None


def player_id(value):
    value = str(value).strip()
    return int(value) if re.fullmatch(r"\d{1,20}", value) else None


# ------------------------------------------------------------------------------------------------
# apply: the files the server reads, and the arguments it needs
# ------------------------------------------------------------------------------------------------

def load_settings():
    data = read_json(SETTINGS_FILE, {})
    return data if isinstance(data, dict) else {}


def effective(settings, env_name, key):
    """(value, source) for one setting: the environment when set, else the panel, else None."""
    if env_set(env_name):
        return os.environ[env_name], "env"
    if key in settings:
        return settings[key], "panel"
    return None, None


def apply_config(config, settings, notes):
    """ServerConfig.json, edited in place: keys we do not own are kept as the server wrote them."""
    value, source = effective(settings, "CK_WORLD_NAME", "worldName")
    if value is not None:
        name = clean_text(value, 64)
        if name:
            config["worldName"] = name
            notes.append(f"worldName = {name} ({source})")

    value, source = effective(settings, "CK_MAX_PLAYERS", "maxPlayers")
    if value is not None:
        n = max_players(value)
        if n is None:
            notes.append(f"WARN maxPlayers {value!r} is not a number; kept {config.get('maxNumberPlayers')}")
        else:
            config["maxNumberPlayers"] = n
            notes.append(f"maxNumberPlayers = {n} ({source})")

    value, source = effective(settings, "CK_PASSWORD", "password")
    if value is not None:
        password = clean_password(value)
        config["password"] = password
        notes.append(f"password = {'(set)' if password else '(empty: the server makes a random one)'} ({source})")

    value, source = effective(settings, "CK_GAME_ID", "gameId")
    if value is not None:
        value = str(value).strip()
        if value == "":
            notes.append(f"gameId kept ({source} is empty)")
        elif valid_game_id(value):
            config["gameId"] = value
            notes.append(f"gameId = {value} ({source})")
        else:
            notes.append(f"WARN gameId {value!r} is not 15-28 letters and digits without Y, y, x, 0 and O; kept {config.get('gameId') or '(random)'}")

    value, source = effective(settings, "CK_WORLD", "world")
    if value is not None:
        n = world_index(value)
        if n is None:
            notes.append(f"WARN world {value!r} is not a slot from 0 to {WORLD_SLOTS - 1}; kept {config.get('world')}")
        else:
            config["world"] = n
            notes.append(f"world = {n} ({source})")
    return config


def for_new_world(settings, env_name, key):
    """(value, source) for a NEW world's mode or seed: the panel's choice when it made one (it
    creates worlds one at a time, each with its own), else the environment, the default for any
    new world. The opposite of `effective`, on purpose: the deploy's mode is a default, the
    world the owner creates from the panel is a decision."""
    if settings.get(key) not in (None, ""):
        return settings[key], "panel"
    if env_set(env_name):
        return os.environ[env_name], "env"
    return None, None


def command_args(config, settings, notes):
    """The arguments that exist only on the command line, or that only act on a new world."""
    args = []
    world_file = os.path.join(GAME_DATA, "worlds", f"{config.get('world', 0)}.world.gzip")
    new_world = not os.path.exists(world_file)

    value, source = for_new_world(settings, "CK_WORLD_MODE", "worldMode")
    if value is not None and str(value).strip():
        mode = pick(value, WORLD_MODES)
        if mode is None:
            notes.append(f"WARN worldMode {value!r} is not one of {', '.join(WORLD_MODES)}")
        elif new_world:
            args += ["-worldmode", mode]
            notes.append(f"worldmode = {mode} ({source}, a new world)")
        else:
            notes.append(f"worldmode {mode} not applied: it is chosen when a world is created, and world {config.get('world')} exists")

    value, source = for_new_world(settings, "CK_SEED", "seed")
    if value is not None and str(value).strip():
        seed = clean_text(value, 64)
        if new_world:
            args += ["-worldseed", seed]
            notes.append(f"worldseed = {seed} ({source}, a new world)")

    value, source = effective(settings, "CK_SEASON", "season")
    if value is not None and str(value).strip():
        season = pick(value, SEASONS)
        if season is None:
            notes.append(f"WARN season {value!r} is not one of {', '.join(SEASONS)}")
        elif season != "Auto":
            args += ["-season", season]
            notes.append(f"season = {season} ({source})")

    value, source = effective(settings, "CK_ALLOW_ONLY_PLATFORM", "allowOnlyPlatform")
    if value is not None and str(value).strip():
        platform = pick(value, PLATFORMS)
        if platform is None:
            notes.append(f"WARN allowOnlyPlatform {value!r} is not one of {', '.join(PLATFORMS)}")
        elif platform != "All":
            args += ["-allowonlyplatform", platform]
            notes.append(f"allowonlyplatform = {platform} ({source})")

    # The content bundles: from the panel, ONCE, for the world the server is about to load (the
    # bundles become that world's for good); the setting is then cleared, so a world switched to
    # or uploaded later is not changed too. CK_ACTIVATE_ALL_CONTENT, an owner's choice in the
    # deploy, applies to every world on every start, as documented.
    if env_set("CK_ACTIVATE_ALL_CONTENT"):
        if truthy(os.environ["CK_ACTIVATE_ALL_CONTENT"]):
            args.append("-activateallcontent")
            notes.append("activateallcontent (env, every world)")
    elif truthy(settings.get("activateAllContent")):
        args.append("-activateallcontent")
        settings.pop("activateAllContent", None)
        done = settings.setdefault("contentActivated", [])
        if config.get("world", 0) not in done:
            done.append(config.get("world", 0))
        write_json(SETTINGS_FILE, settings)
        notes.append(f"activateallcontent (panel, once, world {config.get('world', 0)})")
    return args


def entry_key(entry):
    """How the panel names one Admins.json / PlayerBans.json entry: "<SteamID64>" for a Steam
    player, "cp:<crossPlatformId>" for one who joined from Epic, Microsoft Store or GOG (their
    steamId is 0), None for an entry neither identifies (kept as it is, never touched)."""
    if not isinstance(entry, dict):
        return None
    sid = steam_id(entry.get("steamId"))
    if sid:
        return str(sid)
    cp = player_id(entry.get("crossPlatformId"))
    if cp:
        return f"cp:{cp}"
    return None


def key_ok(key):
    """A key the panel may send: a SteamID64, or cp:<digits> for a non-Steam player."""
    key = str(key).strip()
    return bool(steam_id(key)) or bool(re.fullmatch(r"cp:\d{1,20}", key))


def new_entry(key, existing, privileges=None):
    """An admin or ban entry as the server writes one (measured, 1.3.0.6: it keeps an entry with
    name, steamId and crossPlatformId and adds `index` itself). Only a Steam player can be added by
    id: a cross-platform id alone is the game's to assign."""
    seen = read_json(SEEN_FILE, {}) or {}
    name = (seen.get(key) or {}).get("name", "")
    index = max([e.get("index", 0) for e in existing if isinstance(e, dict)] + [0]) + 1
    entry = {"index": index, "name": name, "steamId": int(key), "crossPlatformId": 0}
    if privileges is not None:
        entry["privileges"] = privileges
    return entry


def apply_list(entries, wanted, privileges, label, notes):
    """The panel's list for admins or bans: entries whose key is in `wanted` are kept, Steam ids
    not there yet are added, entries the panel cannot name are kept as they are."""
    kept = [e for e in entries if entry_key(e) is None or entry_key(e) in wanted]
    have = {entry_key(e) for e in kept}
    for key in wanted:
        if key not in have and steam_id(key):
            kept.append(new_entry(key, kept, privileges))
    removed = len([e for e in entries if entry_key(e) is not None and entry_key(e) not in wanted])
    notes.append(f"{label} from the panel: {len(kept)} ({removed} removed)")
    return kept


def apply_admins(notes):
    """CK_ADMINS (SteamID64s, comma separated) are made admins; the panel's pending edits are
    applied. Admins added in game are kept: the variable adds, it never removes."""
    path = os.path.join(GAME_DATA, "Admins.json")
    data = read_json(path, {}) or {}
    if not isinstance(data, dict):
        notes.append("WARN Admins.json was not an object; left as it is")
        data = None
    admins = data.get("adminList") if data and isinstance(data.get("adminList"), list) else []
    bans_path = os.path.join(GAME_DATA, "PlayerBans.json")
    bans_data = read_json(bans_path, {}) or {}
    if not isinstance(bans_data, dict):
        notes.append("WARN PlayerBans.json was not an object; left as it is")
        bans_data = None
    bans = bans_data.get("banList") if bans_data and isinstance(bans_data.get("banList"), list) else []

    edits = read_json(ADMIN_EDITS_FILE)
    edits = edits if isinstance(edits, dict) else None
    if edits and isinstance(edits.get("admins"), list):
        wanted = [str(k) for k in edits["admins"] if key_ok(k)]
        admins = apply_list(admins, wanted, 2, "admins", notes)
    for raw in (os.environ.get("CK_ADMINS") or "").replace(";", ",").split(","):
        sid = steam_id(raw)
        if sid and str(sid) not in {entry_key(a) for a in admins}:
            admins.append(new_entry(str(sid), admins, 2))
            notes.append(f"admin added: {sid} (CK_ADMINS)")
    if edits and isinstance(edits.get("bans"), list):
        wanted = [str(k) for k in edits["bans"] if key_ok(k)]
        bans = apply_list(bans, wanted, None, "bans", notes)

    if data is not None:
        data["adminList"] = admins
        write_json(path, data)
    if bans_data is not None:
        bans_data["banList"] = bans
        write_json(bans_path, bans_data)
    if edits is not None:
        try:
            os.remove(ADMIN_EDITS_FILE)
        except OSError:
            pass


def ban_id(entry):
    """The panel's key for one PlayerBans.json entry (see entry_key); a bare id for anything else."""
    if isinstance(entry, dict):
        return entry_key(entry)
    pid = player_id(entry)
    return None if pid is None else str(pid)


def cmd_apply():
    os.makedirs(GAME_DATA, exist_ok=True)
    notes = []
    settings = load_settings()
    path = os.path.join(GAME_DATA, "ServerConfig.json")
    config = read_json(path)
    if not isinstance(config, dict):
        if os.path.exists(path):
            notes.append("WARN ServerConfig.json was not JSON; started again from the defaults (the old one is ServerConfig.json.broken)")
            shutil.copy2(path, path + ".broken")
        config = dict(CONFIG_DEFAULTS)
    config = apply_config(config, settings, notes)
    # With direct connections an empty password makes the server pick a random one ON EVERY
    # START, and the address players saved stops working. One is chosen once and kept.
    if os.environ.get("FLUX_DIRECT") == "1" and not config.get("password"):
        config["password"] = random_game_id(12)
        settings["password"] = config["password"]
        write_json(SETTINGS_FILE, settings)
        notes.append("password: none was set, so one was chosen and kept (Server Settings shows it)")
    write_json(path, config)
    apply_admins(notes)
    args = command_args(config, settings, notes)
    state = read_json(STATE_FILE, {}) or {}
    state["applied"] = int(time.time()) + 1
    write_json(STATE_FILE, state, indent=None)
    for note in notes:
        print(f"# {note}")
    for arg in args:
        print(arg)


# ------------------------------------------------------------------------------------------------
# What the server is doing
# ------------------------------------------------------------------------------------------------

def cmd_state(state, message=None):
    """`generation` counts the server's starts: the panel waits for it to change after asking for
    a restart, rather than comparing its own clock with this machine's."""
    data = read_json(STATE_FILE, {}) or {}
    if data.get("state") != state:
        data["since"] = int(time.time())
    if state == "starting":
        data["generation"] = int(data.get("generation") or 0) + 1
    data.update({"state": state, "message": message or None,
                 "image": os.environ.get("FLUX_IMAGE_VERSION", "dev")})
    write_json(STATE_FILE, data, indent=None)


NOTE_KEYS = ("nextRestart", "updateWaiting")


def cmd_note(key, value=None):
    """A field of state.json the supervisor keeps for the panel's Overview: nextRestart (epoch of
    the next daily restart), updateWaiting (the build a game update waits on an empty server for)."""
    if key not in NOTE_KEYS:
        fail(f"unknown note {key}", 64)
    data = read_json(STATE_FILE, {}) or {}
    if value in (None, ""):
        data.pop(key, None)
    else:
        data[key] = int(value) if str(value).isdigit() and key == "nextRestart" else value
    write_json(STATE_FILE, data, indent=None)


def read_log(path=GAME_LOG):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def roster_incremental(path=GAME_LOG, cache_path=ROSTER_CACHE):
    """roster(), reading only what the log gained since the last call: a poll runs `status`
    several times, and the log grows for as long as the server runs. The cache is per file (inode)
    and starts over when the file is new, shorter (rotated or truncated) or the cache is unreadable."""
    try:
        st = os.stat(path)
    except OSError:
        return None, [], None
    cache = read_json(cache_path, {}) or {}
    if cache.get("inode") != st.st_ino or cache.get("offset", 0) > st.st_size or cache.get("path") != path:
        cache = {"inode": st.st_ino, "path": path, "offset": 0, "session": None, "online": {}, "version": None}
    with open(path, "rb") as f:
        f.seek(cache["offset"])
        chunk = f.read()
    # Only whole lines: a line the server is still writing is read next time.
    end = chunk.rfind(b"\n") + 1
    lines = chunk[:end].decode("utf-8", "replace").replace("\x00", "").splitlines()
    session, online, version = cache.get("session"), dict(cache.get("online") or {}), cache.get("version")
    for line in lines:
        m = SESSION_RE.search(line)
        if m:
            session, online = m.group(1), {}
            continue
        if version is None and "ersion" in line:
            v = VERSION_RE.search(line)
            if v:
                version = v.group(1)
        m = JOIN_RE.search(line)
        if m and (m.group(3) or "False").lower() != "true":
            online[m.group(1)] = {"id": m.group(1), "name": m.group(2).strip()}
            continue
        m = LEAVE_RE.search(line)
        if m:
            online.pop(m.group(1), None)
    cache.update({"offset": cache["offset"] + end, "session": session, "online": online, "version": version})
    try:
        write_json(cache_path, cache, indent=None)
    except OSError:
        pass
    return session, list(online.values()), version


def roster(lines):
    """Who is on the server now, from the current generation's log."""
    online = {}
    session = None
    version = None
    for line in lines:
        m = SESSION_RE.search(line)
        if m:
            session = m.group(1)
            online = {}
            continue
        if version is None and "ersion" in line:
            v = VERSION_RE.search(line)
            if v:
                version = v.group(1)
        m = JOIN_RE.search(line)
        if m and (m.group(3) or "False").lower() != "true":
            online[m.group(1)] = {"id": m.group(1), "name": m.group(2).strip()}
            continue
        m = LEAVE_RE.search(line)
        if m:
            online.pop(m.group(1), None)
    return session, list(online.values()), version


def remember_players(players):
    """Every player ever seen, by id: the admin and ban pickers offer them by name."""
    if not players:
        return
    seen = read_json(SEEN_FILE, {}) or {}
    now = int(time.time())
    for p in players:
        entry = seen.get(p["id"], {})
        entry.update({"name": p["name"], "lastSeen": now})
        seen[p["id"]] = entry
    try:
        write_json(SEEN_FILE, seen, indent=None)
    except OSError:
        pass


def session_info(session):
    """`Started session with info:` is the Game ID alone (Steam relay), or
    `<public IP>;<port>;<Game ID>;<password>` with direct connections (build 1.3.0.6)."""
    if not session:
        return {}
    parts = session.split(";")
    if len(parts) >= 4:
        return {"publicIp": parts[0], "port": parts[1], "gameId": parts[2], "password": ";".join(parts[3:])}
    return {"gameId": session}


def installed_build():
    path = os.path.join(SERVER_DIR, "steamapps", f"appmanifest_{os.environ.get('FLUX_STEAM_APP', '1963720')}.acf")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            m = re.search(r'"buildid"\s+"(\d+)"', f.read())
            return m.group(1) if m else None
    except OSError:
        return None


def cmd_status():
    state = read_json(STATE_FILE, {}) or {}
    config = read_json(os.path.join(GAME_DATA, "ServerConfig.json"), {}) or {}
    session, players, version = roster_incremental()
    remember_players(players)
    info = session_info(session)
    out({
        "state": state.get("state", "stopped"),
        "message": state.get("message"),
        "since": state.get("since"),
        "generation": state.get("generation", 0),
        "nextRestart": state.get("nextRestart"),
        "updateWaiting": state.get("updateWaiting"),
        "restartPending": os.path.exists(RESTART_MARKER),
        "restorePending": (read_json(RESTORE_FILE, {}) or {}).get("backup"),
        "image": state.get("image"),
        "build": installed_build(),
        "version": version,
        "name": config.get("worldName"),
        "world": config.get("world", 0),
        "players": players,
        "max_players": config.get("maxNumberPlayers"),
        "gameId": info.get("gameId") or config.get("gameId") or None,
        # From CK_PORT, not from the session line: the log has none while the server updates,
        # starts or is stopped, and the panel must not say "Steam only" then.
        "direct": direct_connections(),
        "pending": changes_pending(state),
    })


def changes_pending(state):
    """True when the panel saved something the running server has not picked up yet."""
    if os.path.exists(ADMIN_EDITS_FILE):
        return True
    try:
        return os.path.getmtime(SETTINGS_FILE) > (state.get("applied") or 0)
    except OSError:
        return False


def cmd_connect():
    config = read_json(os.path.join(GAME_DATA, "ServerConfig.json"), {}) or {}
    session, _, _ = roster_incremental()
    info = session_info(session)
    out({
        "gameId": info.get("gameId") or config.get("gameId") or None,
        "direct": direct_connections(),
        "port": int(info["port"]) if info.get("port", "").isdigit() else None,
        "password": info.get("password", config.get("password") or None),
    })


# ------------------------------------------------------------------------------------------------
# The panel's settings
# ------------------------------------------------------------------------------------------------

def cmd_settings():
    config = read_json(os.path.join(GAME_DATA, "ServerConfig.json"), {}) or {}
    env = {name: os.environ[name] for name in (
        "CK_WORLD_NAME", "CK_MAX_PLAYERS", "CK_PASSWORD", "CK_GAME_ID", "CK_WORLD", "CK_WORLD_MODE",
        "CK_SEED", "CK_SEASON", "CK_ALLOW_ONLY_PLATFORM", "CK_ACTIVATE_ALL_CONTENT", "CK_PORT",
        "CK_RESTART_TIME") if name in os.environ}
    # The password is a secret in an environment variable: say that it is set, never what it is.
    if "CK_PASSWORD" in env:
        env["CK_PASSWORD"] = "(set)" if env["CK_PASSWORD"] else ""
    out({
        "settings": load_settings(),
        "config": config,
        "env": env,
        "choices": {"worldMode": WORLD_MODES, "season": SEASONS, "allowOnlyPlatform": PLATFORMS,
                    "worldSlots": WORLD_SLOTS, "maxPlayersCap": MAX_PLAYERS_CAP},
    })


def cmd_set_settings(raw):
    try:
        incoming = json.loads(raw)
    except ValueError:
        fail("not JSON")
    if not isinstance(incoming, dict):
        fail("expected a JSON object")
    settings = load_settings()
    errors = []
    for key, value in incoming.items():
        if value is None:
            settings.pop(key, None)
            continue
        if key == "world":
            if world_index(value) is None:
                errors.append(f"world must be a slot from 0 to {WORLD_SLOTS - 1}")
                continue
            value = world_index(value)
        elif key == "maxPlayers":
            if max_players(value) is None:
                errors.append("maxPlayers must be a number")
                continue
            value = max_players(value)
        elif key == "gameId":
            value = str(value).strip()
            if value and not valid_game_id(value):
                errors.append("gameId must be 15-28 letters and digits, without Y, y, x, 0 and O")
                continue
        elif key == "password":
            if ";" in str(value):
                errors.append("password cannot contain ';' (it splits the game's join line)")
                continue
            value = clean_password(value)
        elif key in ("worldName", "seed"):
            value = clean_text(value, 64)
        elif key == "worldMode":
            if value and pick(value, WORLD_MODES) is None:
                errors.append(f"worldMode must be one of {', '.join(WORLD_MODES)}")
                continue
        elif key == "season":
            if value and pick(value, SEASONS) is None:
                errors.append(f"season must be one of {', '.join(SEASONS)}")
                continue
        elif key == "allowOnlyPlatform":
            if str(value).strip() and pick(value, PLATFORMS) is None:
                errors.append("allowOnlyPlatform must be All, Steam, Epic, Microsoft or GOG")
                continue
        elif key == "activateAllContent":
            value = truthy(value)
        else:
            errors.append(f"unknown setting {key}")
            continue
        settings[key] = value
    if errors:
        fail("; ".join(errors), 2)
    write_json(SETTINGS_FILE, settings)
    out({"ok": True, "settings": settings, "appliesOn": "restart"})


def random_game_id(length=28):
    import secrets
    return "".join(secrets.choice(GAME_ID_ALPHABET) for _ in range(length))


# ------------------------------------------------------------------------------------------------
# Worlds
# ------------------------------------------------------------------------------------------------

def cmd_worlds():
    config = read_json(os.path.join(GAME_DATA, "ServerConfig.json"), {}) or {}
    active = config.get("world", 0)
    pending = load_settings().get("world")
    slots = []
    folder = os.path.join(GAME_DATA, "worlds")
    for i in range(WORLD_SLOTS):
        path = os.path.join(folder, f"{i}.world.gzip")
        try:
            st = os.stat(path)
        except OSError:
            if i == active or i == pending:
                slots.append({"index": i, "exists": False, "active": i == active, "next": i == pending})
            continue
        backups = sorted(n for n in os.listdir(folder) if n.startswith(f"{i}.world.gzip.") or n.startswith(f"{i}.world.")) if os.path.isdir(folder) else []
        slots.append({"index": i, "exists": True, "size": st.st_size, "modified": int(st.st_mtime),
                      "active": i == active, "next": i == pending, "extra": [b for b in backups if b != f"{i}.world.gzip"]})
    out({"active": active, "next": pending, "slots": slots, "path": "DedicatedServer/worlds",
         "count": WORLD_SLOTS})


# ------------------------------------------------------------------------------------------------
# Admins and bans
# ------------------------------------------------------------------------------------------------

def big_ints_as_str(entry):
    """IDs AS STRINGS: a SteamID64 is above 2^53, and a browser's JSON.parse rounds it to another
    player's number (76561198000000001 reads back as ...000000000)."""
    if not isinstance(entry, dict):
        return str(entry)
    return {k: (str(v) if isinstance(v, int) and abs(v) > 2 ** 53 else v) for k, v in entry.items()}


def cmd_admins():
    admins = (read_json(os.path.join(GAME_DATA, "Admins.json"), {}) or {}).get("adminList") or []
    bans = (read_json(os.path.join(GAME_DATA, "PlayerBans.json"), {}) or {}).get("banList") or []
    seen = read_json(SEEN_FILE, {}) or {}

    def row(entry):
        key = entry_key(entry)
        return {**big_ints_as_str(entry), "key": key,
                "kind": "steam" if key and not key.startswith("cp:") else ("crossplatform" if key else "unknown"),
                "seenName": (seen.get(key or "") or {}).get("name")}

    out({
        "admins": [row(a) for a in admins if isinstance(a, dict)],
        "bans": [{"id": ban_id(b), "name": (b.get("name") if isinstance(b, dict) else None) or (seen.get(str(ban_id(b))) or {}).get("name"),
                  "kind": row(b)["kind"] if isinstance(b, dict) else "unknown", "raw": big_ints_as_str(b)} for b in bans],
        "seen": [{"id": k, **v} for k, v in sorted(seen.items(), key=lambda kv: -kv[1].get("lastSeen", 0))],
        "pending": read_json(ADMIN_EDITS_FILE),
    })


def cmd_set_admins(raw):
    """{"admins": [<key>...], "bans": [<key>...]}: the WHOLE lists as the panel wants them, keys
    from `flux-ck admins` (a SteamID64, or cp:<id> for a non-Steam player). A Steam id not in a list
    yet is added (so a ban by SteamID64 is a ban); entries without a key are never touched."""
    try:
        edits = json.loads(raw)
    except ValueError:
        fail("not JSON")
    if not isinstance(edits, dict):
        fail("expected a JSON object")
    clean = read_json(ADMIN_EDITS_FILE, {}) or {}
    for name in ("admins", "bans"):
        if name not in edits:
            continue
        if not isinstance(edits[name], list):
            fail(f"{name} must be a list", 2)
        bad = [str(x) for x in edits[name] if not key_ok(x)]
        if bad:
            fail(f"not a SteamID64 (17 digits starting 7656119) or cp:<id>: {', '.join(bad)}", 2)
        clean[name] = list(dict.fromkeys(str(steam_id(x)) if steam_id(x) else str(x).strip() for x in edits[name]))
    write_json(ADMIN_EDITS_FILE, clean)
    out({"ok": True, "pending": clean, "appliesOn": "restart"})


# ------------------------------------------------------------------------------------------------
# Mods: folders with a ModManifest.json in Mods/ (the install's StreamingAssets/Mods links here).
#
# A MOD THAT IS OFF IS OUTSIDE Mods/, in Mods.disabled/: the game's loader loads EVERY folder in
# Mods/ whatever its name (measured, 1.3.0.6: "loaded mod MinimapPlus at .../Mods/MinimapPlus.disabled"),
# so a renamed folder is still a loaded mod. An archive is unpacked into flux/tmp, on the same
# volume, and renamed into place, so the loader never sees half a mod.
# ------------------------------------------------------------------------------------------------

def manifest_of(folder):
    for name in ("ModManifest.json", "modmanifest.json"):
        data = read_json(os.path.join(folder, name))
        if isinstance(data, dict):
            return data
    return None


def mod_entry(folder, name, enabled):
    manifest = manifest_of(folder) or {}
    size = 0
    for root, _, files in os.walk(folder):
        for f in files:
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return {
        "dir": name,
        "name": manifest.get("name") or manifest.get("displayName") or name,
        "version": manifest.get("version"),
        "author": manifest.get("author"),
        "dependencies": manifest.get("dependencies") or [],
        "enabled": enabled,
        "valid": bool(manifest),
        "size": size,
    }


def migrate_disabled():
    """Mods switched off by image 1.0.0's rename (`<name>.disabled` inside Mods/, which the game
    still loaded) are moved to Mods.disabled/."""
    if not os.path.isdir(MODS_DIR):
        return
    for name in os.listdir(MODS_DIR):
        if name.endswith(DISABLED_SUFFIX) and os.path.isdir(os.path.join(MODS_DIR, name)):
            os.makedirs(DISABLED_DIR, exist_ok=True)
            target = os.path.join(DISABLED_DIR, name[: -len(DISABLED_SUFFIX)])
            if not os.path.exists(target):
                os.replace(os.path.join(MODS_DIR, name), target)


def cmd_mods():
    migrate_disabled()
    mods = []
    for folder_root, enabled in ((MODS_DIR, True), (DISABLED_DIR, False)):
        if not os.path.isdir(folder_root):
            continue
        for name in os.listdir(folder_root):
            folder = os.path.join(folder_root, name)
            if os.path.isdir(folder) and not name.startswith("."):
                mods.append(mod_entry(folder, name, enabled))
    mods.sort(key=lambda m: m["dir"].lower())
    loader = read_json(os.path.join(GAME_DATA, "modloader", "config.json"), {}) or {}
    out({"mods": mods, "path": "Mods", "disabledPath": "Mods.disabled", "loader": loader})


def safe_dir(name):
    name = os.path.basename(str(name).strip())
    if not name or name in (".", "..") or name.startswith(".") or not re.fullmatch(r"[\w .+@()\[\]-]{1,120}", name):
        fail("not a mod folder name", 2)
    return name


def cmd_mod_install(archive):
    """Unpacks a mod archive (a .zip as mod.io serves it: the mod's folder, or its files at the
    top) into Mods/<name>, replacing that mod whether it was on or off. Paths that leave the
    folder are refused."""
    if not zipfile.is_zipfile(archive):
        fail("not a zip archive", 2)
    os.makedirs(MODS_DIR, exist_ok=True)
    tmp_root = os.path.join(STATE_DIR, "tmp")
    os.makedirs(tmp_root, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if n and not n.endswith("/")]
        if not names:
            fail("the archive is empty", 2)
        for n in names:
            if n.startswith("/") or ".." in n.replace("\\", "/").split("/"):
                fail(f"refused: {n} leaves the mod folder", 2)
        unpacked = sum(i.file_size for i in z.infolist())
        if unpacked > MOD_MAX_BYTES:
            fail(f"refused: the archive unpacks to {unpacked // (1024 * 1024)} MB, over the {MOD_MAX_BYTES // (1024 * 1024)} MB limit", 2)
        manifests = [n for n in names if os.path.basename(n).lower() == "modmanifest.json"]
        if not manifests:
            fail("no ModManifest.json in the archive: this is not a Core Keeper mod", 2)
        top = min(manifests, key=lambda n: n.count("/"))
        prefix = top[: -len(os.path.basename(top))]
        manifest = {}
        try:
            manifest = json.loads(z.read(top).decode("utf-8-sig"))
        except ValueError:
            pass
        base = prefix.rstrip("/").split("/")[-1] if prefix else ""
        name = safe_dir(manifest.get("name") or base or os.path.splitext(os.path.basename(archive))[0])
        dest = os.path.join(MODS_DIR, name)
        staging = tempfile.mkdtemp(dir=tmp_root, prefix="mod-")
        try:
            for n in names:
                if prefix and not n.startswith(prefix):
                    continue
                rel = n[len(prefix):]
                target = os.path.join(staging, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(n) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            for old in (dest, os.path.join(DISABLED_DIR, name)):
                if os.path.exists(old):
                    shutil.rmtree(old)
            os.replace(staging, dest)
        finally:
            if os.path.isdir(staging):
                shutil.rmtree(staging, ignore_errors=True)
    out({"ok": True, "dir": name, "name": manifest.get("name") or name, "version": manifest.get("version"),
         "appliesOn": "restart"})


def cmd_mod_install_url(url):
    """A mod archive from mod.io's CDN (the hub's mod.io catalogue gives the URL), downloaded and
    installed like an uploaded one. Only https on mod.io's own hosts: the panel's argument never
    makes the server fetch anything else."""
    parsed = urllib.parse.urlparse(str(url))
    if parsed.scheme != "https" or not MODIO_HOSTS.match(parsed.hostname or ""):
        fail("only https downloads from mod.io", 2)
    tmp_root = os.path.join(STATE_DIR, "tmp")
    os.makedirs(tmp_root, exist_ok=True)
    fd, archive = tempfile.mkstemp(dir=tmp_root, prefix="modio-", suffix=".zip")
    try:
        req = urllib.request.Request(str(url), headers={"User-Agent": "runonflux/corekeeper-server-flux"})
        with urllib.request.urlopen(req, timeout=120) as r, os.fdopen(fd, "wb") as f:
            host = urllib.parse.urlparse(r.geturl()).hostname or ""
            if not MODIO_HOSTS.match(host):
                fail("the download was redirected away from mod.io", 2)
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > MOD_MAX_BYTES:
                    fail("refused: the download is over the size limit", 2)
                f.write(chunk)
        cmd_mod_install(archive)
    except OSError as e:
        fail(f"the download failed: {e}", 1)
    finally:
        try:
            os.remove(archive)
        except OSError:
            pass


def cmd_mod_enable(name, on):
    migrate_disabled()
    name = safe_dir(name).removesuffix(DISABLED_SUFFIX)
    enabled, disabled = os.path.join(MODS_DIR, name), os.path.join(DISABLED_DIR, name)
    if not os.path.isdir(enabled) and not os.path.isdir(disabled):
        fail("no such mod", 2)
    if truthy(on):
        if os.path.isdir(disabled) and not os.path.exists(enabled):
            os.makedirs(MODS_DIR, exist_ok=True)
            os.replace(disabled, enabled)
    elif os.path.isdir(enabled) and not os.path.exists(disabled):
        os.makedirs(DISABLED_DIR, exist_ok=True)
        os.replace(enabled, disabled)
    out({"ok": True, "dir": name, "enabled": os.path.isdir(enabled), "appliesOn": "restart"})


def cmd_mod_remove(name):
    migrate_disabled()
    name = safe_dir(name).removesuffix(DISABLED_SUFFIX)
    removed = False
    for path in (os.path.join(MODS_DIR, name), os.path.join(DISABLED_DIR, name)):
        if os.path.isdir(path):
            shutil.rmtree(path)
            removed = True
    if not removed:
        fail("no such mod", 2)
    out({"ok": True, "dir": name, "appliesOn": "restart"})


def game_pids():
    return subprocess.run(["pgrep", "-f", r"^\./CoreKeeperServer "], capture_output=True, text=True).stdout.split()


def request_restart(reason):
    """Asks the supervisor for a restart and returns at once: it writes the marker the supervisor
    reads, with the earliest time it may stop the server (a world the server made seconds ago is
    not saved by a quit yet: a SIGTERM 2 s after the session started wrote no world file, 10 s
    after it did), and the supervisor sends the SIGTERM. One restart at a time: a request while one
    is pending joins it."""
    if os.path.exists(RESTART_MARKER):
        return False
    state = read_json(STATE_FILE, {}) or {}
    not_before = 0
    if state.get("state") == "running" and state.get("since"):
        not_before = int(state["since"]) + SETTLE_SECONDS
    tmp = RESTART_MARKER + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"{reason}\nnotbefore={not_before}\n")
    os.replace(tmp, RESTART_MARKER)
    return True


def cmd_restart():
    """Restarts the server in place (the supervisor starts the next one): how the panel applies
    saved settings without a redeploy. Returns at once; the panel waits for `generation` to move."""
    if not game_pids():
        fail("the server is not running", 3)
    state = read_json(STATE_FILE, {}) or {}
    started = request_restart("requested: from the panel")
    out({"ok": True, "alreadyPending": not started, "generation": state.get("generation", 0)})


# ------------------------------------------------------------------------------------------------
# Backups: flux/backups/<UTC>-corekeeper.tar.gz, made by the supervisor (on start and every few
# hours) and before a restore. index.json says why each was made.
#
# Kept: the FLUX_BACKUP_KEEP newest, AND the newest one older than a day and the newest one older
# than a week, so a server that crash-loops (each start a backup) can never rotate every good copy
# away. A start backup is skipped when the newest is younger than FLUX_BACKUP_MIN_AGE_MINUTES.
# ------------------------------------------------------------------------------------------------

def backup_files():
    try:
        names = [n for n in os.listdir(BACKUP_DIR) if BACKUP_NAME_RE.match(n)]
    except OSError:
        return []
    return sorted(names, reverse=True)


def backup_time(name):
    """The UTC time in a backup's name, as an epoch."""
    import calendar
    try:
        return calendar.timegm(time.strptime(name[:15], "%Y%m%d-%H%M%S"))
    except ValueError:
        return 0


def backups_to_keep(names, keep, now):
    """Names newest first. The `keep` newest, the newest older than 1 day, the newest older than 7."""
    kept = set(names[:max(keep, 0)])
    for age in (86400, 7 * 86400):
        older = [n for n in names if now - backup_time(n) >= age]
        if older:
            kept.add(older[0])
    return kept


def cmd_backup(why, force=None):
    if not os.path.isdir(os.path.join(GAME_DATA, "worlds")) or not any(
            f.endswith(".world.gzip") for f in os.listdir(os.path.join(GAME_DATA, "worlds"))):
        print("# backup skipped: no world yet")
        return
    names = backup_files()
    now = int(time.time())
    min_age = int(os.environ.get("FLUX_BACKUP_MIN_AGE_MINUTES", "60")) * 60
    if force != "force" and names and now - backup_time(names[0]) < min_age:
        print(f"# backup ({why}) skipped: the newest is {(now - backup_time(names[0])) // 60} min old")
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)) + "-corekeeper.tar.gz"
    if name in names:
        print(f"# backup ({why}) skipped: one was made this second")
        return
    out_path = os.path.join(BACKUP_DIR, name)
    try:
        # Python's tarfile, not tar: GNU tar exits 1 for "file changed as we read it", which is
        # not a broken archive. The worlds are written by the server only on a save.
        with tarfile.open(out_path + ".part", "w:gz") as t:
            t.add(GAME_DATA, arcname=os.path.basename(GAME_DATA))
        os.replace(out_path + ".part", out_path)
    except (OSError, tarfile.TarError) as e:
        try:
            os.remove(out_path + ".part")
        except OSError:
            pass
        print(f"# WARN backup ({why}) failed: {e}")
        return
    index = read_json(BACKUP_INDEX, {}) or {}
    index[name] = why
    keep = int(os.environ.get("FLUX_BACKUP_KEEP", "10"))
    names = backup_files()
    kept = backups_to_keep(names, keep, now)
    for old in names:
        if old not in kept:
            try:
                os.remove(os.path.join(BACKUP_DIR, old))
            except OSError:
                pass
            index.pop(old, None)
    write_json(BACKUP_INDEX, {k: v for k, v in index.items() if k in kept}, indent=None)
    print(f"# backup ({why}): flux/backups/{name}, {os.path.getsize(out_path) // 1024} KB")


def cmd_backups():
    index = read_json(BACKUP_INDEX, {}) or {}
    rows = []
    for n in backup_files():
        p = os.path.join(BACKUP_DIR, n)
        slots = []
        try:
            with tarfile.open(p) as t:
                slots = sorted(int(m.group(1)) for m in (re.search(r"worlds/(\d+)\.world\.gzip$", x) for x in t.getnames()) if m)
        except (OSError, tarfile.TarError):
            pass
        rows.append({"name": n, "size": os.path.getsize(p), "time": backup_time(n), "why": index.get(n), "slots": slots})
    out({"backups": rows, "path": "flux/backups", "pending": (read_json(RESTORE_FILE, {}) or {}).get("backup")})


def cmd_restore(name):
    """Restores a whole backup (every world, the settings, admins and bans) on the next start,
    after a safety backup of what is there now. The server is restarted now when it runs."""
    if not BACKUP_NAME_RE.match(str(name)) or not os.path.isfile(os.path.join(BACKUP_DIR, name)):
        fail("no such backup", 2)
    write_json(RESTORE_FILE, {"backup": name, "at": int(time.time())}, indent=None)
    restarted = bool(game_pids()) and request_restart(f"requested: restore {name}")
    out({"ok": True, "backup": name, "restarting": restarted, "appliesOn": "restart"})


def cmd_do_restore():
    """The supervisor, before a start: the pending restore, if any. A safety backup first, then
    the archive into a staging folder beside DedicatedServer/, then swapped in. A broken archive
    leaves everything as it was."""
    pending = read_json(RESTORE_FILE)
    if not isinstance(pending, dict):
        return
    try:
        os.remove(RESTORE_FILE)
    except OSError:
        pass
    name = pending.get("backup", "")
    src = os.path.join(BACKUP_DIR, name)
    if not BACKUP_NAME_RE.match(name) or not os.path.isfile(src):
        print(f"# WARN restore: {name} is not a backup; nothing restored")
        return
    cmd_backup("before restoring " + name, "force")
    parent = os.path.dirname(GAME_DATA)
    staging = tempfile.mkdtemp(dir=parent, prefix=".flux-restore-")
    try:
        with tarfile.open(src) as t:
            base = os.path.basename(GAME_DATA)
            members = [m for m in t.getmembers() if m.name == base or m.name.startswith(base + "/")]
            if not members or any(m.issym() or m.islnk() or ".." in m.name.split("/") for m in members):
                raise ValueError("not a Core Keeper backup")
            t.extractall(staging, members=members, filter="data")
        old = GAME_DATA + ".flux-old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(GAME_DATA):
            os.replace(GAME_DATA, old)
        os.replace(os.path.join(staging, os.path.basename(GAME_DATA)), GAME_DATA)
        shutil.rmtree(old, ignore_errors=True)
        print(f"# restored {name}")
    except (OSError, tarfile.TarError, ValueError) as e:
        print(f"# WARN restore of {name} failed ({e}); nothing changed")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ------------------------------------------------------------------------------------------------
# Game updates that did not land: a planned update restart remembers its target; after the update
# step a target the install did not reach is remembered as failed, and the watcher leaves that
# build alone for 6 hours instead of restarting the server for it every half hour.
# ------------------------------------------------------------------------------------------------

def cmd_update_target(build):
    write_json(UPDATE_TARGET_FILE, {"target": str(build), "at": int(time.time())}, indent=None)


def cmd_update_check():
    target = read_json(UPDATE_TARGET_FILE)
    if not isinstance(target, dict):
        return
    try:
        os.remove(UPDATE_TARGET_FILE)
    except OSError:
        pass
    have = installed_build()
    if have and have != target.get("target"):
        write_json(UPDATE_FAILED_FILE, {"target": target.get("target"), "at": int(time.time()), "installed": have}, indent=None)
        print(f"# WARN the update to build {target.get('target')} did not install (still {have}): not retried for 6 hours")
    else:
        try:
            os.remove(UPDATE_FAILED_FILE)
        except OSError:
            pass


def cmd_update_skip(build):
    failed = read_json(UPDATE_FAILED_FILE, {}) or {}
    recent = failed.get("target") == str(build) and time.time() - int(failed.get("at") or 0) < UPDATE_FAILED_SKIP
    sys.exit(0 if recent else 1)


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 64
    cmd, rest = argv[0], argv[1:]
    commands = {
        "apply": (cmd_apply, 0),
        "state": (cmd_state, (1, 2)),
        "status": (cmd_status, 0),
        "connect": (cmd_connect, 0),
        "settings": (cmd_settings, 0),
        "set-settings": (cmd_set_settings, 1),
        "worlds": (cmd_worlds, 0),
        "admins": (cmd_admins, 0),
        "set-admins": (cmd_set_admins, 1),
        "mods": (cmd_mods, 0),
        "mod-install": (cmd_mod_install, 1),
        "mod-enable": (cmd_mod_enable, 2),
        "mod-remove": (cmd_mod_remove, 1),
        "restart": (cmd_restart, 0),
        "backup": (cmd_backup, (1, 2)),
        "backups": (cmd_backups, 0),
        "restore": (cmd_restore, 1),
        "do-restore": (cmd_do_restore, 0),
        "note": (cmd_note, (1, 2)),
        "update-target": (cmd_update_target, 1),
        "update-check": (cmd_update_check, 0),
        "update-skip": (cmd_update_skip, 1),
        "mod-install-url": (cmd_mod_install_url, 1),
        "new-game-id": (lambda: out({"gameId": random_game_id()}), 0),
    }
    if cmd not in commands:
        print(__doc__, file=sys.stderr)
        return 64
    fn, arity = commands[cmd]
    allowed = arity if isinstance(arity, tuple) else (arity,)
    if len(rest) not in allowed:
        print(__doc__, file=sys.stderr)
        return 64
    fn(*rest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

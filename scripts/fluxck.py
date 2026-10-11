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
    flux-ck restart                  save, stop and start the server again (applies the above)
    flux-ck new-game-id              a valid random Game ID (28 characters)

Settings changed by the panel, admins and bans are applied on the NEXT start: the server owns
its files while it runs and rewrites them as it stops (measured: Admins.json and
ServerConfig.json are written back on quit), so an edit made under it would be undone.

Precedence: an environment variable that is SET (even to "") is applied on every start; one that
is not set leaves the panel's value, and then the file's. Except a NEW world's mode and seed,
where the panel's choice wins and the environment is the default (see for_new_world).
"""

import json
import os
import re
import shutil
import sys
import tempfile
import time
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
GAME_LOG = os.path.join(STATE_DIR, "current.log")
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
GAME_ID_RE = re.compile(r"^[A-NP-Za-wz1-9]{15,28}$")
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
    return name in os.environ


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def clean_text(value, limit):
    """One line of printable text: it ends up on the server's command line and in a JSON file."""
    value = re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()
    return value[:limit]


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
        password = clean_text(value, PASSWORD_MAX)
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

    value, source = effective(settings, "CK_ACTIVATE_ALL_CONTENT", "activateAllContent")
    if value is not None and truthy(value):
        args.append("-activateallcontent")
        notes.append(f"activateallcontent ({source})")
    return args


def apply_admins(notes):
    """CK_ADMINS (SteamID64s, comma separated) are made admins; the panel's pending edits are
    applied. Admins added in game are kept: the variable adds, it never removes."""
    path = os.path.join(GAME_DATA, "Admins.json")
    data = read_json(path, {}) or {}
    admins = data.get("adminList") if isinstance(data.get("adminList"), list) else []
    bans_path = os.path.join(GAME_DATA, "PlayerBans.json")
    bans_data = read_json(bans_path, {}) or {}
    bans = bans_data.get("banList") if isinstance(bans_data.get("banList"), list) else []

    edits = read_json(ADMIN_EDITS_FILE)
    wanted = None
    if isinstance(edits, dict) and isinstance(edits.get("admins"), list):
        wanted = [i for i in (steam_id(x) for x in edits["admins"]) if i]
    for raw in (os.environ.get("CK_ADMINS") or "").replace(";", ",").split(","):
        sid = steam_id(raw)
        if sid and wanted is not None and sid not in wanted:
            wanted.append(sid)
        if sid and wanted is None and not any(a.get("steamId") == sid for a in admins if isinstance(a, dict)):
            admins.append(admin_entry(sid, admins))
            notes.append(f"admin added: {sid} (CK_ADMINS)")
    if wanted is not None:
        kept = [a for a in admins if isinstance(a, dict) and a.get("steamId") in wanted]
        for sid in wanted:
            if not any(a.get("steamId") == sid for a in kept):
                kept.append(admin_entry(sid, kept))
        removed = len(admins) - len([a for a in admins if isinstance(a, dict) and a.get("steamId") in wanted])
        admins = kept
        notes.append(f"admins from the panel: {len(admins)} ({removed} removed)")
    if isinstance(edits, dict) and isinstance(edits.get("bans"), list):
        keep = {i for i in (player_id(x) for x in edits["bans"]) if i is not None}
        before = len(bans)
        bans = [b for b in bans if ban_id(b) in keep]
        notes.append(f"bans from the panel: {len(bans)} ({before - len(bans)} lifted)")

    data["adminList"] = admins
    write_json(path, data)
    bans_data["banList"] = bans
    write_json(bans_path, bans_data)
    if edits is not None:
        try:
            os.remove(ADMIN_EDITS_FILE)
        except OSError:
            pass


def admin_entry(sid, existing):
    """An admin as the server writes one (privileges 2 = the owner's level)."""
    seen = read_json(SEEN_FILE, {}) or {}
    name = (seen.get(str(sid)) or {}).get("name", "")
    index = max([a.get("index", 0) for a in existing if isinstance(a, dict)] + [0]) + 1
    return {"index": index, "privileges": 2, "name": name, "steamId": sid}


def ban_id(entry):
    """The player id of one PlayerBans.json entry, whatever the server named the field."""
    if isinstance(entry, dict):
        for key in ("steamId", "userId", "id", "playerId"):
            if key in entry:
                return player_id(entry[key])
        for value in entry.values():
            if isinstance(value, int):
                return value
    return player_id(entry)


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
    data = read_json(STATE_FILE, {}) or {}
    if data.get("state") != state:
        data["since"] = int(time.time())
    data.update({"state": state, "message": message or None,
                 "image": os.environ.get("FLUX_IMAGE_VERSION", "dev")})
    write_json(STATE_FILE, data, indent=None)


def read_log(path=GAME_LOG):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


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
    session, players, version = roster(read_log())
    remember_players(players)
    info = session_info(session)
    out({
        "state": state.get("state", "stopped"),
        "message": state.get("message"),
        "since": state.get("since"),
        "image": state.get("image"),
        "build": installed_build(),
        "version": version,
        "name": config.get("worldName"),
        "world": config.get("world", 0),
        "players": players,
        "max_players": config.get("maxNumberPlayers"),
        "gameId": info.get("gameId") or config.get("gameId") or None,
        "direct": "port" in info,
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
    session, _, _ = roster(read_log())
    info = session_info(session)
    out({
        "gameId": info.get("gameId") or config.get("gameId") or None,
        "direct": "port" in info,
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
            value = clean_text(value, PASSWORD_MAX)
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

def cmd_admins():
    admins = (read_json(os.path.join(GAME_DATA, "Admins.json"), {}) or {}).get("adminList") or []
    bans = (read_json(os.path.join(GAME_DATA, "PlayerBans.json"), {}) or {}).get("banList") or []
    seen = read_json(SEEN_FILE, {}) or {}
    # IDs AS STRINGS: a SteamID64 is above 2^53, and a browser's JSON.parse rounds it to another
    # player's number (76561198000000001 reads back as ...000000000).
    out({
        "admins": [{**a, "steamId": str(a.get("steamId"))} if isinstance(a, dict) else a for a in admins],
        "bans": [{"id": None if ban_id(b) is None else str(ban_id(b)), "name": (seen.get(str(ban_id(b))) or {}).get("name"),
                  "raw": {k: (str(v) if isinstance(v, int) and v > 2 ** 53 else v) for k, v in b.items()} if isinstance(b, dict) else str(b)}
                 for b in bans],
        "seen": [{"id": k, **v} for k, v in sorted(seen.items(), key=lambda kv: -kv[1].get("lastSeen", 0))],
        "pending": read_json(ADMIN_EDITS_FILE),
    })


def cmd_set_admins(raw):
    try:
        edits = json.loads(raw)
    except ValueError:
        fail("not JSON")
    if not isinstance(edits, dict):
        fail("expected a JSON object")
    clean = {}
    if "admins" in edits:
        if not isinstance(edits["admins"], list):
            fail("admins must be a list of SteamID64s")
        bad = [x for x in edits["admins"] if steam_id(x) is None]
        if bad:
            fail(f"not a SteamID64 (17 digits starting 7656119): {', '.join(map(str, bad))}", 2)
        clean["admins"] = [str(steam_id(x)) for x in edits["admins"]]
    if "bans" in edits:
        if not isinstance(edits["bans"], list) or any(player_id(x) is None for x in edits["bans"]):
            fail("bans must be a list of player ids", 2)
        clean["bans"] = [str(player_id(x)) for x in edits["bans"]]
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


def cmd_restart():
    """Restarts the server in place (the supervisor starts the next one): how the panel applies
    saved settings without a redeploy. SIGTERM saves first."""
    import signal
    import subprocess
    pids = subprocess.run(["pgrep", "-f", r"^\./CoreKeeperServer "], capture_output=True, text=True).stdout.split()
    if not pids:
        fail("the server is not running", 3)
    # A world the server made seconds ago is not saved by a quit yet (measured: a SIGTERM 2 s after
    # the session started wrote no world file; 10 s after, it did). So a restart that close to a
    # start waits for the world to settle first.
    state = read_json(STATE_FILE, {}) or {}
    if state.get("state") == "running" and state.get("since"):
        wait = int(state["since"]) + SETTLE_SECONDS - time.time()
        if 0 < wait <= SETTLE_SECONDS:
            time.sleep(wait)
    with open(os.environ.get("FLUX_RESTART_MARKER", "/tmp/flux-restart-requested"), "w") as f:
        f.write("requested: from the panel\n")
    for pid in pids:
        os.kill(int(pid), signal.SIGTERM)
    out({"ok": True})


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

#!/bin/bash
# Unit tests: the pure decisions in flux-lib.sh and fluxck.py's file handling. No Docker, no
# Steam, no game. Run from the repository root.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
# The daily restart is in TZ: the expectations below are UTC unless a test sets its own.
export TZ=UTC

# shellcheck source=scripts/flux-lib.sh
FLUX_LOG="" source scripts/flux-lib.sh

pass=0
fail=0
check() { # description, expected, actual
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'FAIL %s\n  expected: [%s]\n  actual:   [%s]\n' "$1" "$2" "$3"
  fi
}
status() { "$@" >/dev/null 2>&1; echo $?; }
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

# --- Inputs ------------------------------------------------------------------------------------
check "27015 is a port" "27015" "$(flux_game_port 27015)"
check "unset CK_PORT is 27015" "27015" "$(flux_game_port)"
check "a leading zero is not octal" "7778" "$(flux_game_port 07778)"
check "relay means no port" "" "$(flux_game_port relay)"
check "empty means no port" "" "$(flux_game_port '')"
check "port 80 is refused" "1" "$(status flux_game_port 80)"
check "65535 is refused (the query port is the next one)" "1" "$(status flux_game_port 65535)"
check "a word is not a port" "1" "$(status flux_game_port abc)"
check "restart time 5:00" "05:00" "$(flux_restart_time 5:00)"
check "restart time off" "" "$(flux_restart_time off)"
check "restart time 24:00 refused" "1" "$(status flux_restart_time 24:00)"
check "restart time words refused" "1" "$(status flux_restart_time noon)"
# 2026-10-11 12:00 UTC = 1791720000
check "next 05:00 after noon is tomorrow" "$((1791720000 - 43200 + 86400 + 18000))" "$(flux_next_daily 1791720000 05:00)"
check "next 13:30 after noon is today" "$((1791720000 + 5400))" "$(flux_next_daily 1791720000 13:30)"
check "the time is the zone's (Lisbon is UTC+1 in October)" "$((1791720000 + 5400 - 3600))" "$(TZ=Europe/Lisbon flux_next_daily 1791720000 13:30)"

# --- Restart budget ----------------------------------------------------------------------------
check "old restarts drop out of the window" "950 990" "$(flux_restarts_in_window 1000 100 800 950 990)"
check "a crash counts" "0" "$(status flux_restart_counts 'the server ended on its own (exit 139)')"
check "a game update does not count" "1" "$(status flux_restart_counts 'planned: game update 1 -> 2')"
check "a panel restart does not count" "1" "$(status flux_restart_counts 'requested: from the panel')"
check "prune keeps the newest" "c d" "$(flux_files_to_prune 2 a b c d | tr '\n' ' ' | sed 's/ $//')"

# --- Steam builds ------------------------------------------------------------------------------
cat >"${tmp}/manifest.acf" <<'EOF'
"AppState"
{
	"appid"		"1963720"
	"buildid"		"25825323"
}
EOF
check "installed build" "25825323" "$(flux_installed_build "${tmp}/manifest.acf")"
check "public build from app_info" "25900000" "$(printf '%s\n' '"1963720"' '{' '"depots"' '{' '"branches"' '{' '"public"' '{' '"buildid"		"25900000"' '}' '"beta"' '{' '"buildid"		"1"' '}' | flux_public_build)"

# --- fluxck.py ---------------------------------------------------------------------------------
data="${tmp}/data"
ck() { # VAR=value... command args: fluxck.py with exactly these variables
  local vars=()
  while [[ "${1:-}" == *=* ]]; do vars+=("$1"); shift; done
  env -i PATH="${PATH}" FLUX_DATA_DIR="${data}" FLUX_SERVER_DIR="${tmp}/server" "${vars[@]}" python3 scripts/fluxck.py "$@"
}
jget() { python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    d=d[int(k)] if isinstance(d,list) else d.get(k)
print(json.dumps(d) if isinstance(d,(dict,list)) else d)' "$@"; }
cfg="${data}/DedicatedServer/ServerConfig.json"

ck CK_WORLD_NAME="My Cave" CK_MAX_PLAYERS=16 FLUX_DIRECT=1 apply >"${tmp}/args"
check "a fresh config gets the name" "My Cave" "$(jget "${cfg}" worldName)"
check "a fresh config gets the players" "16" "$(jget "${cfg}" maxNumberPlayers)"
check "the server's own keys are written" "20" "$(jget "${cfg}" networkSendRate)"
pw="$(jget "${cfg}" password)"
check "direct connections get a kept password" "12" "${#pw}"
check "the kept password is in the panel's settings" "${pw}" "$(jget "${data}/flux/settings.json" password)"
ck FLUX_DIRECT=1 apply >/dev/null
check "the kept password is not replaced" "${pw}" "$(jget "${cfg}" password)"
check "no arguments for an existing setup" "" "$(grep -v '^#' "${tmp}/args")"

ck CK_MAX_PLAYERS=500 apply >/dev/null
check "players are capped at 100" "100" "$(jget "${cfg}" maxNumberPlayers)"
ck CK_MAX_PLAYERS=lots apply >"${tmp}/args"
check "a bad player count is kept" "100" "$(jget "${cfg}" maxNumberPlayers)"
check "and said" "1" "$(grep -c 'WARN maxPlayers' "${tmp}/args")"

ck CK_GAME_ID=ABCDEFGHJKLMNPQRSTUV apply >/dev/null
check "a valid Game ID is written" "ABCDEFGHJKLMNPQRSTUV" "$(jget "${cfg}" gameId)"
ck CK_GAME_ID=ABC0EFGHJKLMNPQRSTUV apply >/dev/null
check "a Game ID with 0 is refused (the server would make a random one)" "ABCDEFGHJKLMNPQRSTUV" "$(jget "${cfg}" gameId)"
ck CK_GAME_ID=short apply >/dev/null
check "a short Game ID is refused" "ABCDEFGHJKLMNPQRSTUV" "$(jget "${cfg}" gameId)"
check "new-game-id is valid" "0" "$(status python3 -c "import re,json,sys; g=json.loads(sys.argv[1])['gameId']; sys.exit(0 if re.match(r'^[A-NP-Za-wz1-9]{28}$', g) else 1)" "$(ck new-game-id)")"

check "the panel takes a world slot" "0" "$(status ck set-settings '{"world": 3, "worldMode": "hard", "seed": "abc", "season": "Christmas"}')"
ck apply >"${tmp}/args"
check "the panel's world is written" "3" "$(jget "${cfg}" world)"
check "a new world gets its mode, seed and season" "-worldmode Hard -worldseed abc -season Christmas" "$(grep -v '^#' "${tmp}/args" | tr '\n' ' ' | sed 's/ $//')"
mkdir -p "${data}/DedicatedServer/worlds" && echo x >"${data}/DedicatedServer/worlds/3.world.gzip"
ck apply >"${tmp}/args"
check "an existing world keeps its mode and seed" "-season Christmas" "$(grep -v '^#' "${tmp}/args" | tr '\n' ' ' | sed 's/ $//')"
rm -f "${data}/DedicatedServer/worlds/3.world.gzip"
ck CK_WORLD_MODE=Normal CK_SEED=envseed apply >"${tmp}/args"
check "a new world: the panel's mode and seed win over the deploy's default" "-worldmode Hard -worldseed abc -season Christmas" "$(grep -v '^#' "${tmp}/args" | tr '\n' ' ' | sed 's/ $//')"
ck set-settings '{"worldMode": null, "seed": null}' >/dev/null
ck CK_WORLD_MODE=Casual apply >"${tmp}/args"
check "without a panel choice the deploy's mode is the default" "-worldmode Casual -season Christmas" "$(grep -v '^#' "${tmp}/args" | tr '\n' ' ' | sed 's/ $//')"
echo x >"${data}/DedicatedServer/worlds/3.world.gzip"
ck CK_WORLD=5 apply >/dev/null
check "the environment wins over the panel" "5" "$(jget "${cfg}" world)"
check "a bad slot is refused" "2" "$(status ck set-settings '{"world": 30}')"
check "a bad mode is refused" "2" "$(status ck set-settings '{"worldMode": "Peaceful"}')"
check "an unknown setting is refused" "2" "$(status ck set-settings '{"pvp": true}')"
check "a bad Game ID is refused by the panel" "2" "$(status ck set-settings '{"gameId": "xxxxxxxxxxxxxxxxxxx"}')"
check "null clears a setting" "0" "$(status ck set-settings '{"season": null}')"
check "and it is gone" "None" "$(jget "${data}/flux/settings.json" season)"
check "the platform list" "0" "$(status ck set-settings '{"allowOnlyPlatform": "steam"}')"
ck apply >"${tmp}/args"
check "the platform is passed with its own spelling" "1" "$(grep -cx 'Steam' "${tmp}/args")"
ck CK_ALLOW_ONLY_PLATFORM=All apply >"${tmp}/args"
check "All is cross-play: no argument" "0" "$(grep -c 'allowonlyplatform' <(grep -v '^#' "${tmp}/args"))"

ck CK_ADMINS="76561198000000001, 76561198000000002,bad" apply >/dev/null
check "CK_ADMINS adds admins" "2" "$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["adminList"]))' "${data}/DedicatedServer/Admins.json")"
check "as the server writes them" "2" "$(jget "${data}/DedicatedServer/Admins.json" adminList.1.privileges)"
ck CK_ADMINS="76561198000000001" apply >/dev/null
check "CK_ADMINS never removes" "2" "$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["adminList"]))' "${data}/DedicatedServer/Admins.json")"
check "the panel's admin list" "0" "$(status ck set-admins '{"admins": ["76561198000000003"]}')"
check "is pending" "True" "$(ck status | python3 -c 'import json,sys;print(json.load(sys.stdin)["pending"])')"
ck apply >/dev/null
check "replaces the admins" "76561198000000003" "$(jget "${data}/DedicatedServer/Admins.json" adminList.0.steamId)"
check "and is no longer pending" "1" "$(status test -e "${data}/flux/admins-pending.json")"
check "a bad SteamID is refused" "2" "$(status ck set-admins '{"admins": ["123"]}')"
printf '{"banList": [{"steamId": 76561198000000009, "name": "griefer"}, {"steamId": 76561198000000008}]}' >"${data}/DedicatedServer/PlayerBans.json"
ck set-admins '{"bans": ["76561198000000008"]}' >/dev/null
ck apply >/dev/null
check "admins print their SteamID64 as a string (above 2^53)" "str" "$(ck admins | python3 -c 'import json,sys;print(type(json.load(sys.stdin)["admins"][0]["steamId"]).__name__)')"
check "lifting a ban keeps the others" "76561198000000008" "$(jget "${data}/DedicatedServer/PlayerBans.json" banList.0.steamId)"

# The roster, from log lines in the game's own format.
cat >"${data}/flux/current.log" <<'EOF'
Started session with info: 1.2.3.4;27015;rRwVS7A957ZWPnA;secret
[userid:76561198000000001] player Alice connected islocalplayer=False
[userid:3203127215] player Bob The Miner connected islocalplayer=False
[userid:76561198000000001] player Alice connected islocalplayer=True
Disconnected from userid:76561198000000001 with reason ClosedByRemote
EOF
check "the roster has who is on" "Bob The Miner" "$(ck status | python3 -c 'import json,sys;print(",".join(p["name"] for p in json.load(sys.stdin)["players"]))')"
check "the address comes from the session" "27015 secret" "$(ck connect | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["port"], d["password"])')"
check "players seen are remembered" "Bob The Miner" "$(jget "${data}/flux/players-seen.json" 3203127215.name)"
printf 'Started session with info: rRwVS7A957ZWPnA\n' >"${data}/flux/current.log"
check "relay mode is not direct" "False rRwVS7A957ZWPnA" "$(ck connect | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["direct"], d["gameId"])')"

# Mods.
mkdir -p "${tmp}/zip/CoolMod/Assets"
printf '{"name": "CoolMod", "version": "1.2.0", "author": "someone"}' >"${tmp}/zip/CoolMod/ModManifest.json"
echo x >"${tmp}/zip/CoolMod/Assets/a.bin"
(cd "${tmp}/zip" && python3 -c 'import zipfile,os
z=zipfile.ZipFile("../cool.zip","w")
for r,_,fs in os.walk("CoolMod"):
    for f in fs: z.write(os.path.join(r,f))')
check "a mod archive installs" "CoolMod" "$(ck mod-install "${tmp}/cool.zip" | python3 -c 'import json,sys;print(json.load(sys.stdin)["dir"])')"
check "with its files" "0" "$(status test -f "${data}/Mods/CoolMod/Assets/a.bin")"
check "the list reads its manifest" "1.2.0" "$(ck mods | python3 -c 'import json,sys;print(json.load(sys.stdin)["mods"][0]["version"])')"
ck mod-enable CoolMod off >/dev/null
check "off moves it out of Mods/ (the game loads every folder in it)" "0" "$(status test -d "${data}/Mods.disabled/CoolMod")"
check "and nothing of it is left in Mods/" "" "$(ls "${data}/Mods")"
check "and the list says off" "False" "$(ck mods | python3 -c 'import json,sys;print(json.load(sys.stdin)["mods"][0]["enabled"])')"
ck mod-enable CoolMod on >/dev/null
check "on brings it back" "0" "$(status test -d "${data}/Mods/CoolMod")"
python3 -c 'import zipfile; z=zipfile.ZipFile("'"${tmp}"'/evil.zip","w"); z.writestr("../../escape/ModManifest.json","{}")'
check "an archive leaving the folder is refused" "2" "$(status ck mod-install "${tmp}/evil.zip")"
python3 -c 'import zipfile; z=zipfile.ZipFile("'"${tmp}"'/nomod.zip","w"); z.writestr("readme.txt","hi")'
check "an archive without a manifest is refused" "2" "$(status ck mod-install "${tmp}/nomod.zip")"
check "a mod name with a slash is refused" "2" "$(status ck mod-remove '../flux')"
mkdir -p "${data}/Mods/OldStyle.disabled" && printf '{"name":"OldStyle"}' >"${data}/Mods/OldStyle.disabled/ModManifest.json"
check "image 1.0.0's renamed mods are moved out" "OldStyle False" "$(ck mods | python3 -c 'import json,sys;print(*[(m["dir"], m["enabled"]) for m in json.load(sys.stdin)["mods"] if m["dir"]=="OldStyle"][0])')"
check "a dotted name is refused" "2" "$(status ck mod-remove '.flux-x')"
check "removing a mod" "0" "$(status ck mod-remove CoolMod)"
check "removes it" "1" "$(status test -d "${data}/Mods/CoolMod")"

printf '\n%d passed, %d failed\n' "${pass}" "${fail}"
[ "${fail}" = "0" ]

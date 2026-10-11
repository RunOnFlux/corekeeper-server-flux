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
  env -i PATH="${PATH}" FLUX_DATA_DIR="${data}" FLUX_SERVER_DIR="${tmp}/server" FLUX_ROSTER_CACHE="${tmp}/roster.json" \
    FLUX_RESTART_MARKER="${tmp}/restart-marker" "${vars[@]}" python3 scripts/fluxck.py "$@"
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
log="${tmp}/server/flux-logs/current.log"
mkdir -p "$(dirname "${log}")"
cat >"${log}" <<'EOF'
Started session with info: 1.2.3.4;27015;rRwVS7A957ZWPnA;secret
[userid:76561198000000001] player Alice connected islocalplayer=False
[userid:3203127215] player Bob The Miner connected islocalplayer=False
[userid:76561198000000001] player Alice connected islocalplayer=True
Disconnected from userid:76561198000000001 with reason ClosedByRemote
EOF
check "the roster has who is on" "Bob The Miner" "$(ck status | python3 -c 'import json,sys;print(",".join(p["name"] for p in json.load(sys.stdin)["players"]))')"
check "the address comes from the session" "27015 secret" "$(ck connect | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["port"], d["password"])')"
check "players seen are remembered" "Bob The Miner" "$(jget "${data}/flux/players-seen.json" 3203127215.name)"
printf 'Started session with info: rRwVS7A957ZWPnA\n' >"${log}"
check "relay mode is not direct" "False rRwVS7A957ZWPnA" "$(ck CK_PORT=relay connect | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["direct"], d["gameId"])')"
check "direct comes from CK_PORT, not the log" "True" "$(ck status | python3 -c 'import json,sys;print(json.load(sys.stdin)["direct"])')"

# The roster reads only what the log gained.
printf 'Started session with info: 1.2.3.4;27015;rRwVS7A957ZWPnA;secret\n[userid:76561198000000001] player Alice connected islocalplayer=False\n' >"${log}"
ck status >/dev/null
printf '[userid:76561198000000002] player Carol connected islocalplayer=False\n' >>"${log}"
check "lines added later are read" "Alice,Carol" "$(ck status | python3 -c 'import json,sys;print(",".join(sorted(p["name"] for p in json.load(sys.stdin)["players"])))')"
check "the cache keeps its place" "1" "$(python3 -c 'import json,sys,os;c=json.load(open(sys.argv[1]));print(int(c["offset"]==os.path.getsize(sys.argv[2])))' "${tmp}/roster.json" "${log}")"
printf 'Started session with info: rRwVS7A957ZWPnA\n' >"${log}"
check "a shorter log starts over" "" "$(ck status | python3 -c 'import json,sys;print(",".join(p["name"] for p in json.load(sys.stdin)["players"]))')"
printf '[userid:3] player Half' >>"${log}"
check "a line still being written is not read" "" "$(ck status | python3 -c 'import json,sys;print(",".join(p["name"] for p in json.load(sys.stdin)["players"]))')"

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

# --- 1.0.2 --------------------------------------------------------------------------------------

# An empty env value is unset: the marketplace writes blank parameters as NAME=.
ck set-settings '{"world": 4}' >/dev/null
ck CK_WORLD= CK_WORLD_NAME= apply >/dev/null
check "an empty CK_WORLD leaves the panel's world" "4" "$(jget "${cfg}" world)"
check "an empty name keeps the name" "1" "$(python3 -c 'import json,sys;print(int(bool(json.load(open(sys.argv[1]))["worldName"])))' "${cfg}")"
ck set-settings '{"world": null}' >/dev/null

check "a Game ID with Y is refused" "2" "$(status ck set-settings '{"gameId": "ABCDEFGHJKLMNPQRSTUVY"}')"
check "a password with ; is refused" "2" "$(status ck set-settings '{"password": "ab;cd"}')"
check "an env password loses its ;" "abcd" "$(ck CK_PASSWORD='ab;cd' apply >/dev/null; jget "${cfg}" password)"

# The content bundles from the panel: once, for the world being loaded.
ck set-settings '{"activateAllContent": true}' >/dev/null
check "the first start gets them" "1" "$(ck apply | grep -cx -- '-activateallcontent')"
check "the next does not" "0" "$(ck apply | grep -cx -- '-activateallcontent')"
check "and the world is remembered" "1" "$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["contentActivated"]))' "${data}/flux/settings.json")"
check "CK_ACTIVATE_ALL_CONTENT is every start" "1" "$(ck CK_ACTIVATE_ALL_CONTENT=true apply | grep -cx -- '-activateallcontent')"

# Admins from other PC stores (steamId 0, a crossPlatformId) and entries the panel cannot name.
printf '{"adminList": [{"index": 1, "privileges": 2, "name": "EpicGuy", "steamId": 0, "crossPlatformId": 3203127215}, {"index": 2, "name": "?"}, {"index": 3, "privileges": 2, "name": "S", "steamId": 76561198000000003, "crossPlatformId": 0}]}' >"${data}/DedicatedServer/Admins.json"
check "a cross-platform admin has a key" "cp:3203127215" "$(ck admins | python3 -c 'import json,sys;print(json.load(sys.stdin)["admins"][0]["key"])')"
check "the panel may send it" "0" "$(status ck set-admins '{"admins": ["cp:3203127215", "76561198000000004"]}')"
ck apply >/dev/null
check "kept: the cross-platform admin, the unnamed entry and the new one" "EpicGuy,?,76561198000000004" "$(python3 -c 'import json,sys;print(",".join(a.get("name") or str(a.get("steamId")) for a in json.load(open(sys.argv[1]))["adminList"]))' "${data}/DedicatedServer/Admins.json")"
check "a SteamID64 above 2^53 survives" "76561198000000004" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["adminList"][2]["steamId"])' "${data}/DedicatedServer/Admins.json")"

# Bans by SteamID64, in the shape the server keeps (measured: it adds `index` itself).
printf '{"banList": []}' >"${data}/DedicatedServer/PlayerBans.json"
ck set-admins '{"bans": ["76561198000000009"]}' >/dev/null
ck apply >/dev/null
check "a ban by SteamID64" "76561198000000009 0" "$(python3 -c 'import json,sys;b=json.load(open(sys.argv[1]))["banList"][0];print(b["steamId"], b["crossPlatformId"])' "${data}/DedicatedServer/PlayerBans.json")"
check "an admins edit keeps a pending bans edit" "2" "$(ck set-admins '{"bans": ["76561198000000008"]}' >/dev/null; ck set-admins '{"admins": []}' >/dev/null; python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' "${data}/flux/admins-pending.json")"
ck apply >/dev/null

# Backups: skipped when one is young, kept by the rules, restored on the next start.
mkdir -p "${data}/DedicatedServer/worlds" && echo w0 >"${data}/DedicatedServer/worlds/0.world.gzip"
check "a first backup" "1" "$(ck backup start | grep -c 'backup (start): flux/backups/')"
check "a second within the hour is skipped" "1" "$(ck backup start | grep -c 'skipped')"
check "unless forced" "1" "$(sleep 1; ck backup panel force | grep -c 'backup (panel)')"
check "the list says why" "panel start" "$(ck backups | python3 -c 'import json,sys;print(" ".join(b["why"] for b in json.load(sys.stdin)["backups"]))')"
check "keep: the 2 newest, the newest over a day, the newest over a week" "a b d f" "$(PYTHONPATH=scripts python3 -c '
import fluxck as c
names=["20261011-120000-corekeeper.tar.gz","20261011-110000-corekeeper.tar.gz","20261011-100000-corekeeper.tar.gz","20261009-120000-corekeeper.tar.gz","20261008-120000-corekeeper.tar.gz","20261001-120000-corekeeper.tar.gz"]
now=c.backup_time(names[0])
k=c.backups_to_keep(names,2,now)
print(" ".join("abcdef"[i] for i,n in enumerate(names) if n in k))')"
first="$(ck backups | python3 -c 'import json,sys;print(json.load(sys.stdin)["backups"][-1]["name"])')"
echo changed >"${data}/DedicatedServer/worlds/0.world.gzip"
check "a bad backup name is refused" "2" "$(status ck restore '../x.tar.gz')"
check "restore is pending (no server to restart)" "False" "$(ck restore "${first}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["restarting"])')"
ck do-restore >"${tmp}/restore"
check "the restore puts the world back" "w0" "$(cat "${data}/DedicatedServer/worlds/0.world.gzip")"
check "after a safety backup" "1" "$(grep -c 'before restoring' "${tmp}/restore")"
check "and it is no longer pending" "1" "$(status test -e "${data}/flux/restore-pending.json")"

# Failed updates: a target that did not install is skipped for 6 hours.
mkdir -p "${tmp}/server/steamapps" && printf '"AppState"\n{\n\t"buildid"\t\t"100"\n}\n' >"${tmp}/server/steamapps/appmanifest_1963720.acf"
ck update-target 200
ck update-check >/dev/null
check "a build that did not install is skipped" "0" "$(status ck update-skip 200)"
check "another build is not" "1" "$(status ck update-skip 300)"
ck update-target 100
ck update-check >/dev/null
check "a reached target clears the failure" "1" "$(status ck update-skip 200)"

# The panel's restart is a marker the supervisor acts on, one at a time.
check "no server, no restart" "3" "$(status ck restart)"
check "the marker has a not-before" "notbefore=0" "$(PYTHONPATH=scripts FLUX_RESTART_MARKER="${tmp}/m2" python3 -c 'import fluxck as c; c.request_restart("requested: test")'; sed -n 2p "${tmp}/m2")"
check "a second request joins the first" "False" "$(PYTHONPATH=scripts FLUX_RESTART_MARKER="${tmp}/m2" python3 -c 'import fluxck as c; print(c.request_restart("requested: again"))')"
check "the supervisor reads it" "123" "$(printf 'x\nnotbefore=123\n' >"${tmp}/m3"; flux_marker_not_before "${tmp}/m3")"

# Notes and the start counter.
ck state starting
check "a start counts" "1" "$(ck status | python3 -c 'import json,sys;print(json.load(sys.stdin)["generation"])')"
ck note nextRestart 1791800000
check "the next daily restart" "1791800000" "$(ck status | python3 -c 'import json,sys;print(json.load(sys.stdin)["nextRestart"])')"
ck note nextRestart
check "removed" "None" "$(ck status | python3 -c 'import json,sys;print(json.load(sys.stdin)["nextRestart"])')"

# Mods: a zip bomb and downloads from elsewhere are refused.
python3 -c 'import zipfile,sys; z=zipfile.ZipFile(sys.argv[1],"w",zipfile.ZIP_DEFLATED); z.writestr("Big/ModManifest.json","{}"); z.writestr("Big/blob.bin", bytes(2*1024*1024))' "${tmp}/big.zip"
check "an archive over the limit is refused" "2" "$(status ck FLUX_MOD_MAX_BYTES=1048576 mod-install "${tmp}/big.zip")"
check "a download from elsewhere is refused" "2" "$(status ck mod-install-url 'https://example.com/mod.zip')"
check "plain http is refused" "2" "$(status ck mod-install-url 'http://g-5289.modapi.io/x.zip')"

printf '\n%d passed, %d failed\n' "${pass}" "${fail}"
[ "${fail}" = "0" ]

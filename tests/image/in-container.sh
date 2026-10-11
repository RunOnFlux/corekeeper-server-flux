#!/bin/bash
# shellcheck disable=SC2016  # wait_for conditions are single-quoted and eval-ed later, on purpose
# Runs INSIDE the image (tests/test-image.sh). The repository's tests/ folder is mounted at /t
# and scripts/ at /s.
set -uo pipefail
export LC_ALL=C

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
# Waits up to $2 seconds for the command in $1 to succeed.
wait_for() {
  local _
  for _ in $(seq 1 $(($2 * 5))); do
    eval "$1" && return 0
    sleep 0.2
  done
  return 1
}

# --- What the scripts need is in the image -----------------------------------------------------
for tool in steamcmd python3 Xvfb pgrep pkill timeout tail tar gzip; do
  check "${tool} is installed" "0" "$(status command -v "${tool}")"
done
check "flux-ck is on the PATH" "0" "$(status command -v flux-ck)"
check "flux-ck starts (usage)" "64" "$(status flux-ck)"
for script in flux-entrypoint.sh flux-lib.sh; do
  check "${script} parses" "0" "$(status bash -n "/opt/flux/${script}")"
done
check "the image's scripts are the repository's" "0" "$(status diff -r --exclude=__pycache__ /s /opt/flux --exclude=README.md)"
check "the unit tests pass with the image's Python" "0" "$(status bash -c 'cd /tmp && rm -rf repo && mkdir repo && cp -r /s repo/scripts && cp -r /t repo/tests && cd repo && ./tests/test-flux.sh')"
for lib in libXi.so.6 libatomic.so.1 libpulse.so.0; do
  check "${lib} is installed" "0" "$(status bash -c "ldconfig -p | grep -q ${lib}")"
done

# --- The supervisor, around a stand-in Steam and a stand-in server -----------------------------
# steamcmd "installs" a server that behaves like Core Keeper where it matters: it needs a display,
# logs its session start to -logfile, writes the world on SIGTERM, and exits.
cat >/usr/local/bin/steamcmd <<'STUB'
#!/bin/bash
dir=""
while [ $# -gt 0 ]; do
  [ "$1" = "+force_install_dir" ] && dir="$2"
  shift
done
[ -n "${dir}" ] || exit 0
mkdir -p "${dir}/linux64" "${dir}/CoreKeeperServer_Data/StreamingAssets" "${dir}/steamapps"
printf '"AppState"\n{\n\t"buildid"\t\t"100"\n}\n' >"${dir}/steamapps/appmanifest_1963720.acf"
cat >"${dir}/CoreKeeperServer" <<'GAME'
#!/bin/bash
# Re-executed under the real server's name: the supervisor and flux-ck find it by
# `^./CoreKeeperServer `, which a script run by bash would not match.
[ -z "${FLUX_STANDIN:-}" ] && FLUX_STANDIN=1 exec -a ./CoreKeeperServer bash "$0" "$@"
log="" data="" args="$*"
while [ $# -gt 0 ]; do
  case "$1" in -logfile) log="$2"; shift ;; -datapath) data="$2"; shift ;; esac
  shift
done
[ -n "${DISPLAY}" ] || { echo "no display" >>"${log}"; exit 1; }
echo "args: ${args}" >>"${log}"
world="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("world",0))' "${data}/ServerConfig.json")"
trap 'mkdir -p "${data}/worlds"; echo saved >"${data}/worlds/${world}.world.gzip"; echo "Got quit request" >>"${log}"; exit 0' TERM
echo "Started session with info: 1.2.3.4;27015;TestGameIdABCDEFG;pw" >>"${log}"
while true; do sleep 0.2; done
GAME
chmod +x "${dir}/CoreKeeperServer"
STUB
chmod +x /usr/local/bin/steamcmd

data=/mnt/corekeeper/data
export FLUX_RESTART_BACKOFF=1 FLUX_UPDATE_CHECK_MINUTES=0 CK_WORLD_NAME="Image Test" CK_MAX_PLAYERS=4
/opt/flux/flux-entrypoint.sh >/tmp/entry.log 2>&1 &
entry=$!
check "the server comes up" "0" "$(status wait_for 'grep -q "\"running\"" ${data}/flux/state.json 2>/dev/null' 30)"
check "on the virtual display" "0" "$(status pgrep -x Xvfb)"
check "with direct connections" "1" "$(grep -c -- '-port 27015' ${data}/flux/current.log)"
check "the mods folder is linked to the data volume" "${data}/Mods" "$(readlink /mnt/corekeeper/server/CoreKeeperServer_Data/StreamingAssets/Mods)"
check "status reads the session" "TestGameIdABCDEFG" "$(flux-ck status | python3 -c 'import json,sys;print(json.load(sys.stdin)["gameId"])')"
check "the config has the name" "Image Test" "$(python3 -c 'import json;print(json.load(open("'"${data}"'/DedicatedServer/ServerConfig.json"))["worldName"])')"

flux-ck set-settings '{"world": 4}' >/dev/null
flux-ck restart >/dev/null
check "a panel restart saves the world" "0" "$(status wait_for 'test -f ${data}/DedicatedServer/worlds/0.world.gzip' 20)"
check "and starts again on the new slot" "0" "$(status wait_for 'grep -q "\"running\"" ${data}/flux/state.json && python3 -c "import json,sys;sys.exit(0 if json.load(open(\"${data}/DedicatedServer/ServerConfig.json\"))[\"world\"]==4 else 1)"' 30)"
check "the restart was not counted as a crash" "0" "$(grep -c 'restarts within' /tmp/entry.log)"
check "the previous log is kept" "0" "$(status wait_for 'ls ${data}/flux/logs/*-server.log' 5)"
check "a backup was made on the second start" "0" "$(status wait_for 'ls ${data}/flux/backups/*-corekeeper.tar.gz' 5)"

wait_for 'grep -q "\"running\"" ${data}/flux/state.json' 20
kill -TERM "${entry}"
check "a stop ends the supervisor" "0" "$(status wait_for '! kill -0 ${entry} 2>/dev/null' 20)"
check "and saves the world" "0" "$(status test -f ${data}/DedicatedServer/worlds/4.world.gzip)"
check "and says stopped" "stopped" "$(python3 -c 'import json;print(json.load(open("'"${data}"'/flux/state.json"))["state"])')"

printf '\n%d passed, %d failed\n' "${pass}" "${fail}"
[ "${fail}" = "0" ] || { echo '--- entrypoint log'; cat /tmp/entry.log; exit 1; }

#!/bin/bash
# PID 1, and the supervisor of the Core Keeper server.
#
# The game runs as a CHILD, never exec, so a stop reaches us first and becomes a save, and a
# server that ends on its own is followed by the next one in place:
#
#   - every start: SteamCMD update, the mods folder linked to the data volume, a backup of the
#     worlds, ServerConfig.json / Admins.json / PlayerBans.json from the environment and the
#     panel's settings (fluxck.py apply), then the server on a virtual display (Xvfb: part of the
#     world generation runs on the GPU, so the server needs a display even when headless)
#   - SIGTERM/SIGINT from Docker: SIGTERM to the server, which saves and quits (3.9 s measured)
#   - a new build on Steam: an outdated server refuses players on the new client, so it is
#     restarted onto it, as soon as nobody is playing or after FLUX_UPDATE_WAIT_MINUTES
#   - CK_RESTART_TIME: a daily restart at that time (in TZ, UTC when unset), on an empty server if one comes within
#     FLUX_RESTART_WAIT_MINUTES (Core Keeper servers degrade over long sessions)
#   - the server ending on its own: a new generation in the same container; FLUX_RESTART_MAX_ATTEMPTS
#     unplanned ones inside FLUX_RESTART_WINDOW seconds end the container with 42
#   - a backup of the worlds and the server's settings on start (unless one is under an hour old)
#     and every FLUX_BACKUP_HOURS, kept on the data volume (flux/backups; fluxck.py backup has the
#     rules), and a backup restored when the panel asked for one (fluxck.py do-restore)
#   - a game update that did not install is not retried for 6 hours (fluxck.py update-check)
#   - the panel's restart (flux-ck restart) is a marker: the supervisor stops the server for it
#
# Same shape as runonflux/unturned-server-flux and runonflux/dst-server-flux.

set -uo pipefail
# shellcheck source=scripts/flux-lib.sh
source /opt/flux/flux-lib.sh

FLUX_RESTART_MAX_ATTEMPTS="${FLUX_RESTART_MAX_ATTEMPTS:-5}"
FLUX_RESTART_WINDOW="${FLUX_RESTART_WINDOW:-3600}"
FLUX_RESTART_BACKOFF="${FLUX_RESTART_BACKOFF:-10}"
# How long a server asked to stop may take before it is killed. Docker's own deadline on Flux is
# ten seconds, so this only matters for a restart, where nobody is waiting on us.
FLUX_STOP_GRACE="${FLUX_STOP_GRACE:-90}"
FLUX_BACKUP_HOURS="${FLUX_BACKUP_HOURS:-6}"
export FLUX_BACKUP_KEEP="${FLUX_BACKUP_KEEP:-10}"
FLUX_UPDATE_CHECK_MINUTES="${FLUX_UPDATE_CHECK_MINUTES:-30}"
FLUX_UPDATE_WAIT_MINUTES="${FLUX_UPDATE_WAIT_MINUTES:-60}"
FLUX_RESTART_WAIT_MINUTES="${FLUX_RESTART_WAIT_MINUTES:-60}"

s="${FLUX_SERVER_DIR}"
ck() { python3 /opt/flux/fluxck.py "$@"; }

rm -f "${FLUX_RESTART_MARKER}"
mkdir -p "${s}" "${FLUX_GAME_DATA}" "${FLUX_MODS_DIR}" "${FLUX_STATE_DIR}" "${FLUX_GAME_LOG_DIR}"
# An older image kept flux.log on the synced data volume: move it here, once.
flux_log_migrate "${FLUX_STATE_DIR}/flux.log"
flux_banner "Core Keeper"
# Image 1.0.x kept the game's logs on the data volume; they are local now.
rm -f "${FLUX_STATE_DIR}/current.log" 2>/dev/null
rm -rf "${FLUX_STATE_DIR}/logs" 2>/dev/null

if ! port="$(flux_game_port "${CK_PORT-27015}")"; then
  flux_log "ERROR CK_PORT='${CK_PORT:-}' is not a port from 1024 to 65534 (or 'relay'); refusing to start"
  ck state crashed "CK_PORT is not a usable port"
  exit 42
fi
if ! restart_at="$(flux_restart_time "${CK_RESTART_TIME:-}")"; then
  flux_log "WARN CK_RESTART_TIME='${CK_RESTART_TIME}' is not HH:MM; no daily restart"
  restart_at=""
fi
if [ -n "${port}" ]; then
  export FLUX_DIRECT=1
  flux_log "flux-entrypoint starting (image ${FLUX_IMAGE_VERSION:-dev}): direct connections on UDP ${port} (game) and $((port + 1)) (Steam query), and the Game ID through Steam"
else
  export FLUX_DIRECT=0
  flux_log "flux-entrypoint starting (image ${FLUX_IMAGE_VERSION:-dev}): Steam relay only (CK_PORT=relay), players join by Game ID, Steam players only"
fi

game_pid=""
tail_pid=""
watch_pid=""
backup_pid=""
cap_pid=""
terminating=0
generation=0
restart_history=()
xvfb_pid=""

# shellcheck disable=SC2329  # invoked by the trap below
term_handler() {
  terminating=1
  flux_log "stop requested: saving the world and shutting down, no restart will follow"
  local pid
  pid="$(flux_game_pid)"
  pid="${pid:-${game_pid}}"
  [ -n "${pid}" ] && kill -TERM "${pid}" 2>/dev/null
}
trap term_handler TERM INT

# The virtual display. One for the container's life; started again if it died.
ensure_display() {
  if [ -n "${xvfb_pid}" ] && kill -0 "${xvfb_pid}" 2>/dev/null; then
    return 0
  fi
  rm -f "/tmp/.X${FLUX_DISPLAY#:}-lock"
  Xvfb "${FLUX_DISPLAY}" -screen 0 1x1x24 -nolisten tcp >/dev/null 2>&1 &
  xvfb_pid=$!
  sleep 1
  kill -0 "${xvfb_pid}" 2>/dev/null
}

update_server() {
  local attempt validate
  if flux_truthy "${SKIP_UPDATE:-}" && [ -x "${s}/${FLUX_GAME_PROCESS}" ]; then
    flux_log "SKIP_UPDATE is set: not updating the server files"
    return 0
  fi
  ck state updating "Downloading the server from Steam"
  flux_log "updating the server from Steam (app ${FLUX_STEAM_APP})"
  # No `validate` on the first attempt: it re-hashes the whole install on every start. An install
  # that is actually broken fails the first attempt, and every retry validates.
  validate=""
  for attempt in 1 2 3 4 5; do
    # shellcheck disable=SC2086  # validate is "" or "validate"
    # app_info_update + a second app_update in the same run: SteamCMD answers "Missing
    # configuration" to a first app_update now and then (measured with other apps).
    if steamcmd +force_install_dir "${s}" +login anonymous +app_info_update 1 +app_update "${FLUX_STEAM_APP}" +app_update "${FLUX_STEAM_APP}" ${validate} +quit >/tmp/steamcmd.log 2>&1; then
      flux_log "server build $(flux_installed_build "${s}/steamapps/appmanifest_${FLUX_STEAM_APP}.acf")"
      return 0
    fi
    [ "${terminating}" = "1" ] && return 1
    validate="validate"
    flux_log "WARN steamcmd failed (attempt ${attempt}/5): $(grep -aE 'ERROR|Error|error' /tmp/steamcmd.log | tail -2 | tr '\n' ' ')retrying in 5s with validate"
    sleep 5
  done
  if [ -x "${s}/${FLUX_GAME_PROCESS}" ]; then
    flux_log "WARN could not update from Steam; starting the build already installed"
    return 0
  fi
  flux_log "ERROR could not install the server from Steam"
  return 1
}

# The server loads mods from the INSTALL (CoreKeeperServer_Data/StreamingAssets/Mods), which is
# local to the node and downloaded again on a new one. That folder is a link to Mods/ on the data
# volume, so the mods survive a move. A real folder already there (mods put in by hand) is moved
# across once, never overwriting one of ours.
link_mods() {
  local mods="${s}/CoreKeeperServer_Data/StreamingAssets/Mods" dir
  [ -d "${s}/CoreKeeperServer_Data/StreamingAssets" ] || return 0
  if [ -L "${mods}" ]; then
    [ "$(readlink "${mods}")" = "${FLUX_MODS_DIR}" ] && return 0
    rm -f "${mods}"
  elif [ -d "${mods}" ]; then
    for dir in "${mods}"/*/; do
      [ -d "${dir}" ] || continue
      if [ ! -e "${FLUX_MODS_DIR}/$(basename "${dir}")" ] && mv "${dir}" "${FLUX_MODS_DIR}/"; then
        flux_log "moved mod $(basename "${dir}") from the install onto the data volume"
      fi
    done
    rm -rf "${mods}"
  fi
  ln -s "${FLUX_MODS_DIR}" "${mods}"
}

# A backup (fluxck.py backup: what goes in, what is skipped and what is kept). $2 = force.
backup_world() {
  local line
  while IFS= read -r line; do
    flux_log "${line#\# }"
  done < <(ck backup "$1" ${2:+"$2"} 2>&1)
}

# shellcheck disable=SC2329  # run in the background below
backupper() {
  [ "${FLUX_BACKUP_HOURS}" -gt 0 ] 2>/dev/null || exit 0
  while true; do
    sleep $((FLUX_BACKUP_HOURS * 3600))
    backup_world "every ${FLUX_BACKUP_HOURS}h"
  done
}

# How many players are on the server now (from its log). "" when it cannot tell.
player_count() {
  python3 -c '
import json, subprocess, sys
try:
    print(len(json.loads(subprocess.run(["python3", "/opt/flux/fluxck.py", "status"], capture_output=True, text=True, timeout=20).stdout)["players"]))
except Exception:
    pass
' 2>/dev/null
}

# Asks the supervisor to restart the server: the marker says why; the main loop's
# wait_for_generation sends the SIGTERM, which saves and quits.
request_restart() {
  [ -f "${FLUX_RESTART_MARKER}" ] && return 0
  printf '%s\nnotbefore=0\n' "$1" >"${FLUX_RESTART_MARKER}"
  flux_log "$1: restarting the server"
}

# shellcheck disable=SC2329  # run in the background below
# The current game log, capped at FLUX_GAME_LOG_MAX_BYTES: its last 5 MB go to a rotated file and
# the live one is emptied in place (the server keeps writing it).
log_capper() {
  local current size
  current="$(flux_game_log)"
  while true; do
    sleep 60
    size="$(stat -c %s "${current}" 2>/dev/null || echo 0)"
    if [ "${size}" -gt "${FLUX_GAME_LOG_MAX_BYTES}" ] 2>/dev/null; then
      tail -c 5242880 "${current}" >"${FLUX_GAME_LOG_DIR}/$(date -u +%Y%m%d-%H%M%S)-server.log" 2>/dev/null
      : >"${current}"
      flux_prune "${FLUX_GAME_LOG_DIR}" '*-server.log' "${FLUX_GAME_LOG_KEEP}"
      flux_log "the game log reached $((size / 1048576)) MB: rotated"
    fi
  done
}

# Waits for an empty server for up to $1 minutes, checking every minute. Returns when it is
# empty or the time is up.
wait_for_empty() {
  local until=$(($(date -u +%s) + $1 * 60)) n
  while [ "$(date -u +%s)" -lt "${until}" ]; do
    n="$(player_count)"
    [ "${n}" = "0" ] && return 0
    sleep 60
  done
  return 0
}

# shellcheck disable=SC2329  # run in the background below
# Marks the server running once its session has started, then follows Steam for a newer build
# and keeps the daily restart.
watcher() {
  local line build public last_check=0 next_restart="" now
  while IFS= read -r line; do
    if [[ "${line}" == *"Started session with info:"* ]]; then
      ck state running
      flux_log "the server is up (generation ${generation})"
      break
    fi
  done < <(tail -n +1 -F "$(flux_game_log)" 2>/dev/null)
  if [ -n "${restart_at}" ]; then
    next_restart="$(flux_next_daily "$(date -u +%s)" "${restart_at}")"
    ck note nextRestart "${next_restart}"
  else
    ck note nextRestart
  fi
  ck note updateWaiting
  while true; do
    sleep 60
    now="$(date -u +%s)"
    if [ "${FLUX_UPDATE_CHECK_MINUTES}" -gt 0 ] 2>/dev/null && [ $((now - last_check)) -ge $((FLUX_UPDATE_CHECK_MINUTES * 60)) ]; then
      last_check="${now}"
      build="$(flux_installed_build "${s}/steamapps/appmanifest_${FLUX_STEAM_APP}.acf")"
      public="$(steamcmd +login anonymous +app_info_update 1 +app_info_print "${FLUX_STEAM_APP}" +quit 2>/dev/null | flux_public_build)"
      if [ -n "${public}" ] && [ -n "${build}" ] && [ "${public}" != "${build}" ] && ! ck update-skip "${public}"; then
        flux_log "a new server build is on Steam (${build} -> ${public}): restarting onto it when the server is empty, at most ${FLUX_UPDATE_WAIT_MINUTES} min from now"
        ck note updateWaiting "${public}"
        wait_for_empty "${FLUX_UPDATE_WAIT_MINUTES}"
        ck update-target "${public}"
        request_restart "planned: game update ${build} -> ${public}"
        exit 0
      fi
    fi
    if [ -n "${next_restart}" ] && [ "${now}" -ge "${next_restart}" ]; then
      flux_log "daily restart (${restart_at} ${TZ:-UTC}): waiting up to ${FLUX_RESTART_WAIT_MINUTES} min for an empty server"
      wait_for_empty "${FLUX_RESTART_WAIT_MINUTES}"
      request_restart "planned: the daily restart (${restart_at} ${TZ:-UTC})"
      exit 0
    fi
  done
}

start_generation() {
  local line args=() current
  generation=$((generation + 1))
  rm -f "${FLUX_RESTART_MARKER}"

  update_server || return 1
  while IFS= read -r line; do flux_log "${line#\# }"; done < <(ck update-check 2>&1)
  [ "${terminating}" = "1" ] && return 2
  link_mods
  # A restore the panel asked for, before anything reads the server's files.
  while IFS= read -r line; do flux_log "restore: ${line#\# }"; done < <(ck do-restore 2>&1)
  backup_world "start"

  # GameInfo.txt is written next to the executable once the session starts; an old one would
  # read as this generation's.
  rm -f "${s}/GameInfo.txt"
  # apply's stdout is the arguments (and "# " notes); its stderr is only ever logged. A failed
  # apply starts the server on the files as they are, never on an error message as arguments.
  local applied
  if applied="$(ck apply 2>/tmp/flux-apply.err)"; then
    while IFS= read -r line; do
      case "${line}" in
        "# "*) flux_log "config: ${line#\# }" ;;
        "") ;;
        *) args+=("${line}") ;;
      esac
    done <<<"${applied}"
  else
    flux_log "WARN applying the settings failed; starting on the files as they are: $(tail -3 /tmp/flux-apply.err | tr '\n' ' ')"
  fi

  if ! ensure_display; then
    flux_log "ERROR the virtual display (Xvfb) did not start"
    return 1
  fi

  current="$(flux_game_log)"
  if [ -s "${current}" ]; then
    mv "${current}" "${FLUX_GAME_LOG_DIR}/$(date -u +%Y%m%d-%H%M%S)-server.log"
  fi
  : >"${current}"
  flux_prune "${FLUX_GAME_LOG_DIR}" '*-server.log' "${FLUX_GAME_LOG_KEEP}"
  [ "${terminating}" = "1" ] && return 2

  if [ -n "${port}" ]; then
    args+=(-port "${port}")
  fi
  ck state starting "Loading the world"
  flux_banner "Core Keeper"
  flux_log "starting the server (generation ${generation}): ${args[*]}"

  # The game's log, onto the container's output.
  tail -n 0 -F "${current}" 2>/dev/null &
  tail_pid=$!

  (
    cd "${s}" || exit 1
    export DISPLAY="${FLUX_DISPLAY}"
    export LD_LIBRARY_PATH="${s}/linux64${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
    # -batchmode, NOT -nographics: part of the world generation runs on the GPU (Pugstorm's
    # README); Mesa's software renderer on the virtual display does it.
    exec "./${FLUX_GAME_PROCESS}" -batchmode -logfile "${current}" -datapath "${FLUX_GAME_DATA}" "${args[@]}" </dev/null >/dev/null 2>&1
  ) &
  game_pid=$!

  watcher &
  watch_pid=$!
  backupper &
  backup_pid=$!
  log_capper &
  cap_pid=$!
  return 0
}

sweep_generation() {
  local pid
  for pid in "${tail_pid}" "${watch_pid}" "${backup_pid}" "${cap_pid}"; do
    [ -n "${pid}" ] && kill "${pid}" 2>/dev/null
  done
  tail_pid="" watch_pid="" backup_pid="" cap_pid=""
  pkill -f "tail -n [0+1]* -F $(flux_game_log)" 2>/dev/null
  pkill -KILL -f "^\./${FLUX_GAME_PROCESS} " 2>/dev/null
  game_pid=""
}

# Waits for the game to end. A requested stop or restart that is not honoured within
# FLUX_STOP_GRACE ends it by force; the grace runs from when the request was first seen.
wait_for_generation() {
  local asked_at=0 now signalled=0
  while kill -0 "${game_pid}" 2>/dev/null; do
    sleep 1 &
    wait $!
    # A restart asked for by the panel, an update or the daily restart: the SIGTERM is sent here,
    # once, when the marker allows it (a new world first settles).
    if [ "${signalled}" = "0" ] && [ "${terminating}" = "0" ] && [ -f "${FLUX_RESTART_MARKER}" ] \
      && [ "$(date -u +%s)" -ge "$(flux_marker_not_before "${FLUX_RESTART_MARKER}")" ]; then
      signalled=1
      kill -TERM "$(flux_game_pid || true)" 2>/dev/null || kill -TERM "${game_pid}" 2>/dev/null
    fi
    if [ "${terminating}" = "1" ] || [ "${signalled}" = "1" ]; then
      now="$(date -u +%s)"
      [ "${asked_at}" = "0" ] && asked_at="${now}"
      if [ $((now - asked_at)) -ge "${FLUX_STOP_GRACE}" ]; then
        flux_log "WARN the server was asked to stop ${FLUX_STOP_GRACE}s ago and has not; ending it"
        kill -KILL "${game_pid}" 2>/dev/null
        break
      fi
    else
      asked_at=0
    fi
  done
  wait "${game_pid}" 2>/dev/null
}

while true; do
  if [ "${terminating}" = "1" ]; then
    flux_log "stopped on request before the server started"
    ck state stopped
    exit 0
  fi
  start_generation
  started=$?
  if [ "${started}" = "2" ]; then
    flux_log "stopped on request before the server started"
    ck state stopped
    exit 0
  elif [ "${started}" != "0" ]; then
    flux_log "ERROR the server could not be prepared; ending the container (42)"
    ck state crashed "The server could not be installed or started"
    exit 42
  fi
  wait_for_generation
  rc=$?

  reason=""
  if [ -f "${FLUX_RESTART_MARKER}" ]; then
    reason="$(head -1 "${FLUX_RESTART_MARKER}" 2>/dev/null)"
  fi
  sweep_generation
  rm -f "${FLUX_RESTART_MARKER}"

  if [ "${terminating}" = "1" ]; then
    flux_log "stopped on request (server exited ${rc})"
    ck state stopped
    exit 0
  fi

  [ -z "${reason}" ] && reason="the server ended on its own (exit ${rc})"

  if flux_restart_counts "${reason}"; then
    now="$(date -u +%s)"
    read -r -a restart_history <<<"$(flux_restarts_in_window "${now}" "${FLUX_RESTART_WINDOW}" "${restart_history[@]}")"
    restart_history+=("${now}")
    if [ "${#restart_history[@]}" -gt "${FLUX_RESTART_MAX_ATTEMPTS}" ]; then
      flux_log "${reason}: ${#restart_history[@]} restarts within ${FLUX_RESTART_WINDOW}s, so restarting it here is not working. Ending the container so the platform can rebuild or move it (42)."
      ck state crashed "The server keeps stopping: ${reason}"
      exit 42
    fi
  fi

  ck state restarting "${reason}"
  flux_log "${reason}: restarting the server in place in ${FLUX_RESTART_BACKOFF}s"
  sleep "${FLUX_RESTART_BACKOFF}" &
  wait $!
done

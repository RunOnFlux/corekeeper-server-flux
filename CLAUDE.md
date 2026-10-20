# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **Docker image repo**, not an application: the Core Keeper dedicated server for Flux,
`runonflux/corekeeper-server-flux`. steamcmd/steamcmd:ubuntu-24, the game's native Linux server on
Xvfb, and a supervisor in bash. Same family as `~/work/unturned-server-flux` and
`~/work/dst-server-flux`; read them for the patterns this one follows.

The rule that shapes everything: **the container supervises its own server.** PID 1 is
`flux-entrypoint.sh`, never the game. The game runs as a child so a stop becomes a save, and a
server that ends on its own is followed by the next one in place.

## Commands

```bash
./tests/test-flux.sh
docker build -t corekeeper-server-flux:local .
./tests/test-image.sh                    # the scripts INSIDE the built image; CI gates the publish on it
docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:stable -x scripts/*.sh tests/*.sh tests/image/*.sh
```

A change to what the image does bumps `VERSION` and gets a line under "Versions" in the README.

## The files

- `scripts/flux-entrypoint.sh`: PID 1. Per generation: SteamCMD update, the Mods link, a backup,
  `fluxck.py apply` (whose output is the extra arguments), the server on Xvfb, then the watcher
  (ready, game updates, daily restart) and the backupper.
- `scripts/fluxck.py`: every decision about the server's files (ServerConfig.json, Admins.json,
  PlayerBans.json, the panel's settings, mods) and the hub's commands (`flux-ck ...`). The one
  place those rules live.
- `scripts/flux-lib.sh`: shared helpers. **Sourcing it must stay side-effect free**: the tests
  source it directly.

## Things that will bite you

- **Not `-nographics`.** Part of the world generation runs on the GPU (Pugstorm's README): the
  server runs with `-batchmode` on Xvfb, drawing with Mesa's software renderer.
- **The server writes its JSON files back as it stops.** Anything the panel changes goes into
  `flux/settings.json` / `flux/admins-pending.json` and is applied by `apply` before the next
  start. Never edit ServerConfig.json or Admins.json under a running server.
- **Find the server by `^\./CoreKeeperServer `.** The stand-in server in the image test re-execs
  itself under that name.
- **An invalid Game ID is silently replaced by a random one**, which changes how every player
  joins. `fluxck.py` refuses one rather than pass it on.
- **With direct connections an empty password becomes a new random one on every start.** `apply`
  chooses one once and keeps it in the panel's settings.
- **No console.** stdin is ignored and there is no RCON: admin actions are in game. The roster is
  read from the game's log (`<install>/flux-logs/current.log`, passed with `-logfile`; local, never
  synced, capped), incrementally (`/tmp/flux-roster.json` keeps the offset).
- **The panel's restart is a marker** (`flux-ck restart` returns at once): `wait_for_generation`
  sends the SIGTERM when `notbefore=` in the marker allows. Nothing else signals the server for a
  restart.
- **Empty env = unset**: the marketplace writes blank declared parameters as `NAME=`.
- **The game loads EVERY folder in Mods/**, a renamed `X.disabled` too: a mod that is off is moved
  to `Mods.disabled/`, outside it. Archives are unpacked in `flux/tmp` and renamed into place.
- **A quit seconds after a new world starts saves nothing.** `flux-ck restart` waits 15 s after the
  session start (`SETTLE_SECONDS`).
- **SteamID64s are above 2^53.** Every ID the hub reads is printed as a string.
- **The world file is not gzip** despite `.world.gzip`. Opaque: copied, never parsed.
- **GameInfo.txt / GameID.txt land in the install directory**, next to the executable.
- **Flux stops a container with Docker's default 10 seconds.** SIGTERM save: 3.4-3.9 s measured.
- **flux.log is local** (`${FLUX_SERVER_DIR}/flux-logs/flux.log`, 5 MB + one `.1`, on the `ml:` mount), never the synced data volume. `flux_log_migrate` moves an older `flux/flux.log` there once. `entrypoint` creates the dir explicitly. The start-up banner is `flux_banner "<game>"` (container start and every generation); keep its text.

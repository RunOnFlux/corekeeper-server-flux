# corekeeper-server-flux

The Core Keeper dedicated server for [Flux](https://runonflux.io), as
`runonflux/corekeeper-server-flux`. The game's native Linux server (Steam app 1963720), installed
and updated with SteamCMD on every start, on a virtual display (Xvfb: Pugstorm's README says part
of the world generation runs on the GPU, so `-nographics` is not an option), under a supervisor
that:

- writes `ServerConfig.json`, `Admins.json` and `PlayerBans.json` from the environment and from
  the hub's panel (world slot, Game ID, password, mode, seed, season, platforms, admins, bans)
- saves the world on stop (SIGTERM: 3.4 s measured; Flux allows 10)
- restarts the server in place when it ends on its own, with a restart budget
- follows game updates: an outdated server refuses players on the new client, so a newer build
  on Steam means a restart onto it, as soon as nobody is playing (at most an hour later)
- restarts it daily at `CK_RESTART_TIME` (in `TZ`) if asked, on an empty server if one comes within the hour
- keeps the installed mods on the data volume, so they survive a move to another node
- makes a backup of the worlds and settings on start (unless one is under an hour old) and every
  6 hours, keeps them so a crash loop can never rotate the good ones away, and restores one when
  the panel asks (after a safety backup)
- does not restart for the same failed game update more than once every 6 hours
- gives the hub a JSON status (players, Game ID, address), the 30 world slots, the admins and
  bans, and the mods

## Volumes

| Path | Flux mount | What |
|---|---|---|
| `/mnt/corekeeper/data` | `g:` (synced) | `DedicatedServer/` (the server's `-datapath`: ServerConfig.json, Admins.json, PlayerBans.json, `worlds/<0-29>.world.gzip`, `worldgenparams/`), `Mods/` (the mods that are on), `Mods.disabled/` (the ones switched off: the game loads every folder in `Mods/`, whatever its name) and `flux/` (our log, state, settings, backups) |
| `/mnt/corekeeper/server` | `ml:server` (local) | the game install, about 570 MB. Downloaded again on a new node |

Spec: `g:/mnt/corekeeper/data|ml:server:/mnt/corekeeper/server`.

## Ports

| Variable | Default | |
|---|---|---|
| `CK_PORT` | 27015/udp | the game port for **direct connections**: what Epic, Microsoft Store and GOG players need (they join with address, port and password), and anyone joining by IP. The server also binds **the next port** (27016/udp), Steam's query port (A2S). `relay` = no direct connections: Steam players only, by Game ID |

Steam players always join by **Game ID**, through Steam's relay, whatever the ports. The port the
server binds is the one it tells players (GameInfo.txt, the session line), so the public port and
the container port must be the same number.

## Environment

A variable that is **set to a value** is applied on every start; one that is not set, **or set
empty** (the marketplace writes every declared parameter, blank ones as `NAME=`), leaves the
panel's value (`flux/settings.json`), and then the file's.

| Variable | | |
|---|---|---|
| `CK_WORLD_NAME` | `worldName` | the name players see |
| `CK_MAX_PLAYERS` | `maxNumberPlayers` | 1-100 (Pugstorm: 8 by default; more needs more resources) |
| `CK_PASSWORD` | `password` | direct connections only (the Game ID needs none). No `;` (it splits the join line). None set with direct connections: one is chosen once and kept |
| `CK_GAME_ID` | `gameId` | 15-28 letters and digits without Y, y, x, 0, O. Anything else is refused (the server would replace it with a random one, changing how everybody joins). Unset: the server's, kept in ServerConfig.json |
| `CK_WORLD` | `world` | the world slot, 0-29 |
| `CK_WORLD_MODE` | `-worldmode` | `Normal`, `Hard`, `Creative`, `Casual`. A NEW world only, and only as the default: a world created from the panel with its own mode gets that one |
| `CK_SEED` | `-worldseed` | a NEW world only, the default like `CK_WORLD_MODE` |
| `CK_SEASON` | `-season` | `Auto` (by date), `None`, `Easter`, `Halloween`, `Christmas`, `Valentine`, `Anniversary`, `CherryBlossom`, `LunarNewYear` |
| `CK_ALLOW_ONLY_PLATFORM` | `-allowonlyplatform` | `All` (cross-play, the default), or `Steam`, `Epic`, `Microsoft`, `GOG`: only that store |
| `CK_ACTIVATE_ALL_CONTENT` | `-activateallcontent` | adds the optional content bundles to the world being loaded (permanent), on EVERY start, so every world the server loads gets them. The panel's switch does it once, for one world. New 1.3 worlds have them all |
| `CK_ADMINS` | `Admins.json` | SteamID64s, comma separated. Adds, never removes (admins made in game are kept) |
| `CK_RESTART_TIME` | | `HH:MM`, a daily restart, in `TZ` (UTC when unset). Empty = none |
| `TZ` | | the time zone of `CK_RESTART_TIME`, e.g. `Europe/Lisbon` |

Supervisor knobs: `FLUX_BACKUP_HOURS` (6), `FLUX_BACKUP_KEEP` (10, plus the newest older than a
day and the newest older than a week), `FLUX_BACKUP_MIN_AGE_MINUTES` (60), `FLUX_GAME_LOG_MAX_BYTES`
(50 MB), `FLUX_MOD_MAX_BYTES` (512 MB unpacked), `FLUX_UPDATE_CHECK_MINUTES` (30,
0 = off), `FLUX_UPDATE_WAIT_MINUTES` (60), `FLUX_RESTART_WAIT_MINUTES` (60),
`FLUX_RESTART_MAX_ATTEMPTS` (5 in `FLUX_RESTART_WINDOW` 3600 s, then exit 42), `SKIP_UPDATE`.

## For the hub

Commands, run through appexec. Each prints one JSON document.

| Command | |
|---|---|
| `flux-ck status` | `state` (`updating`, `starting`, `running`, `restarting`, `crashed`, `stopped`) and its `message`, `generation` (counts the starts: wait for it to move after a restart), `nextRestart` (epoch), `updateWaiting` (a build waiting for an empty server), `restartPending`, `restorePending`, `players` (`id`, `name`), `max_players`, `name`, `world`, `gameId`, `direct` (from `CK_PORT`), `version` (1.3.0.6-6e73), `build` (Steam), `pending` (the panel saved something the server has not picked up yet). Reads only what the log gained since the last call |
| `flux-ck connect` | `gameId`, and with direct connections `port` and `password` |
| `flux-ck settings` / `set-settings '<json>'` | the panel's settings, the server's config, the env that overrides them, and the choices. Applied on the next start |
| `flux-ck worlds` | the world slots that exist (size, last saved), the active one and the next one |
| `flux-ck admins` / `set-admins '{"admins":[...],"bans":[...]}'` | admins, bans and every player seen (to pick from by name), every ID as a STRING (a SteamID64 is above 2^53 and a browser would round it). Each entry has a `key`: the SteamID64, or `cp:<crossPlatformId>` for a player from Epic, Microsoft Store or GOG. `set-admins` takes the whole lists as keys; a SteamID64 not in a list yet is added (a ban by SteamID64), an entry with no key is never touched. Applied on the next start |
| `flux-ck mods` / `mod-install <zip>` / `mod-install-url <https mod.io URL>` / `mod-enable <dir> on\|off` / `mod-remove <dir>` | the mods in `Mods/` (by their ModManifest.json). Archives over 512 MB unpacked are refused; downloads only from mod.io's hosts. Applied on the next start |
| `flux-ck restart` | a restart in place (how the panel applies the above): returns at once, the supervisor stops the server (not before 15 s after a start) and starts it again; a second request while one is pending joins it (`alreadyPending`) |
| `flux-ck backups` / `restore <name>` | the backups (name, size, time, why, world slots); a restore of a whole backup on the next start, after a safety backup, the server restarted now |
| `flux-ck new-game-id` | a valid random 28-character Game ID |

Why "on the next start": the server owns its files while it runs and writes ServerConfig.json and
Admins.json back as it stops (measured), so an edit made under it would be undone.

## Measured (build 25825323, version 1.3.0.6-6e73)

- Install 568 MB. A new world loads in ~20-25 s; ~1.3-1.5 GB RAM and ~4% of a core with nobody on.
- SIGTERM saves (`Got quit request`, world written) and exits in 3.4-3.9 s.
- With `-port P`: UDP P (dual-stack) and UDP P+1 (A2S info/players; its max players reads 100
  whatever the config says). Without it: no inbound port at all.
- The world file is NOT gzip despite its name (a length and an unknown compression): treat it as
  opaque. Characters live on the players' machines, not on the server.
- `GameInfo.txt` and `GameID.txt` are written next to the executable, in the install.
- No console: stdin is ignored, there is no RCON. Admins act in game (player list: kick, ban,
  make admin). The first player to join becomes admin.
- A quit seconds after a NEW world's session started saves nothing (2 s: no world file; 10 s: saved). `flux-ck restart` waits until 15 s after the start.
- The mod loader loads every folder in `Mods/`, `<name>.disabled` included: a mod that is off lives in `Mods.disabled/`.
- `Admins.json` entries: `{"index", "privileges": 2, "name", "steamId", "crossPlatformId"}`; one
  written without `crossPlatformId` is accepted and completed by the server.
- The log lines the roster is read from: `[userid:<id>] player <name> connected islocalplayer=False`,
  `Disconnected from userid:<id> with reason <reason>`, `Started session with info: <...>`.

## Versions

- **1.0.2** (2026-10-11): backups skipped when one is under an hour old and kept by age (a crash
  loop can no longer rotate every good one away), made with Python's tarfile, with the reason in
  `backups/index.json`; `flux-ck backups` / `restore`; a game update that did not install is not
  retried for 6 hours; `flux-ck restart` returns at once (the supervisor stops the server), one at
  a time, and `status` has a start counter, the next daily restart and a waiting update; empty env
  values count as unset; admins and bans keyed by SteamID64 or cross-platform id (non-Steam
  admins are kept), bans by SteamID64; the panel's content-bundles switch acts once; `direct`
  from `CK_PORT`; the game log on the local install volume, capped at 50 MB, read incrementally;
  `;` refused in passwords; the Game ID check no longer accepts `Y`; `apply`'s errors are logged,
  never passed to the server as arguments; mod archives capped at 512 MB unpacked;
  `mod-install-url` for mod.io; SteamCMD's "Missing configuration" worked around.
- **1.0.0** (2026-10-11): first release.

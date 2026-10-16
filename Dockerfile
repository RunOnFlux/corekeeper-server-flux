# Core Keeper dedicated server image for Flux.
#
# Written from scratch. The game's native Linux server (Steam app 1963720), installed and updated
# with SteamCMD on every start, on a virtual display (Xvfb: part of the world generation runs on
# the GPU), under a supervisor that writes the server's settings from the environment and the
# hub's panel, saves on stop, follows game updates, restarts daily if asked, keeps backups, and
# gives the hub a JSON status, the worlds, the admins and bans, and the mods. See README.md.
# The base. CI builds it pinned to the digest it resolved and records that digest in the
# org.opencontainers.image.base.digest label, which the scheduled check compares against
# (.github/ci/needs-rebuild.sh).
ARG BASE_IMAGE=steamcmd/steamcmd:ubuntu-24
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG FLUX_IMAGE_VERSION=dev

# xvfb + libxi6: what Pugstorm's _launch.sh installs. libatomic1 + libpulse0: needed since the
# February 2026 build. libgl1-mesa-dri: the software renderer the world generation draws with.
# python3, NOT python3-minimal: fluxck.py needs json and zipfile.
RUN apt-get update -y && \
    apt-get upgrade -y --no-install-recommends && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl procps tzdata python3 \
        xvfb libxi6 libatomic1 libpulse0 libgl1-mesa-dri && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY scripts/flux-lib.sh scripts/flux-entrypoint.sh scripts/fluxck.py /opt/flux/
RUN chmod 0755 /opt/flux/*.sh /opt/flux/fluxck.py && \
    ln -s /opt/flux/fluxck.py /usr/local/bin/flux-ck

# SteamCMD's self-update, done once here instead of on every container start; the weekly
# rebuild keeps it current.
RUN steamcmd +quit >/dev/null 2>&1; test -d /root/.local/share/Steam

ENV FLUX_IMAGE_VERSION=${FLUX_IMAGE_VERSION}

VOLUME ["/mnt/corekeeper/server", "/mnt/corekeeper/data"]

# The game port (direct connections: Epic, Microsoft Store and GOG players, and anyone joining by
# IP) and the Steam query port the server binds next to it.
EXPOSE 27015/udp 27016/udp

# The first start downloads about 570 MB. FluxOS does not act on health; this is for anyone
# running the image by hand.
HEALTHCHECK --interval=60s --timeout=10s --start-period=15m --retries=3 \
    CMD pgrep -f "^\./CoreKeeperServer " >/dev/null || exit 1

ENTRYPOINT []
CMD ["/opt/flux/flux-entrypoint.sh"]

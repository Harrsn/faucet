#!/usr/bin/env bash
# faucet-hook-wrapper.sh — Transmission completion hook for a CONTAINERIZED
# Faucet (transmission on the host, Faucet running in Docker).
#
# Use this when your torrent client runs on the host but Faucet runs in a
# container, so the completed file has to be reached *inside* the container and
# the host path translated to the container's view of it.
#
# (If you run Faucet bare-metal, you don't need this — just call
#  `python3 -m faucet.hook` directly. See docs/HOOKS.md.)
#
# SETUP
#   1. Edit the three variables below for your environment.
#   2. Make it executable:           chmod +x faucet-hook-wrapper.sh
#   3. Point Transmission at it (stop the daemon first; it rewrites settings on
#      exit), in settings.json:
#         "script-torrent-done-enabled": true,
#         "script-torrent-done-filename": "/opt/faucet-hook-wrapper.sh"
#
# HOW IT WORKS
#   Transmission exports TR_TORRENT_DIR / TR_TORRENT_NAME / TR_TORRENT_ID on
#   completion. We translate the host download path into the container's mount
#   path, then invoke Faucet's hook inside the container via `docker exec`.

set -uo pipefail

# ─── EDIT THESE FOR YOUR SETUP ───────────────────────────────────────────────
CONTAINER="faucet"                     # name of your Faucet container
HOST_DL="/mnt/nas/torrents"            # where Transmission downloads (host path)
CONTAINER_DL="/downloads"              # how the Faucet container sees HOST_DL
# ─────────────────────────────────────────────────────────────────────────────

# Translate the completed item's host path into the container's view.
host_path="${TR_TORRENT_DIR:-}/${TR_TORRENT_NAME:-}"
container_path="${host_path/$HOST_DL/$CONTAINER_DL}"

# Hand off to Faucet's hook inside the container. REMOVE_ON_COMPLETE and
# MEDIASORT_MODE are read from the container's own environment (set them in your
# Portainer/compose stack), so they don't need to be passed here.
exec docker exec \
  -e FAUCET_PATH="$container_path" \
  -e FAUCET_NAME="${TR_TORRENT_NAME:-}" \
  -e FAUCET_ID="${TR_TORRENT_ID:-}" \
  "$CONTAINER" python -m faucet.hook

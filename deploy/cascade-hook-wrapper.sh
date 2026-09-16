#!/usr/bin/env bash
# cascade-hook-wrapper.sh — host-side Transmission completion hook.
#
# Transmission runs on the host and now downloads to the NAS at
# /mnt/nas/torrents. The faucet container sees that same NAS path mounted at
# /downloads. This translates the host path to the container's view and invokes
# faucet's hook inside the container via docker exec.
#
# Install to /opt/cascade-hook-wrapper.sh, mode 755, root-owned.
#
# Wire it in /etc/transmission-daemon/settings.json (daemon stopped first):
#   "script-torrent-done-enabled": true,
#   "script-torrent-done-filename": "/opt/cascade-hook-wrapper.sh",
set -uo pipefail
CONTAINER="faucet"
HOST_DL="/mnt/nas/torrents"
CONTAINER_DL="/downloads"
# Translate the completed path into the container's view.
host_path="${TR_TORRENT_DIR:-}/${TR_TORRENT_NAME:-}"
container_path="${host_path/$HOST_DL/$CONTAINER_DL}"
# Hand off to faucet's hook inside the container. REMOVE_ON_COMPLETE and
# MEDIASORT_MODE come from the container's own env (the Portainer stack).
exec docker exec \
  -e FAUCET_PATH="$container_path" \
  -e FAUCET_NAME="${TR_TORRENT_NAME:-}" \
  -e FAUCET_ID="${TR_TORRENT_ID:-}" \
  "$CONTAINER" python -m faucet.hook

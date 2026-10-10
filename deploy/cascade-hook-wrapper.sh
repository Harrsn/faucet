#!/usr/bin/env bash
# cascade-hook-wrapper.sh — host-side Transmission completion hook.
#
# Transmission runs on the host and downloads to the NAS (/mnt/nas/torrents).
# This hands the completed path to faucet's hook inside the container,
# *as Transmission reports it*. Faucet translates it with Settings → Path
# mappings (e.g. /mnt/nas/torrents -> /nas/torrents), so the wrapper needs no
# knowledge of the container's mounts. (Before 2.0.2 it rewrote the path to
# /downloads itself; a path Faucet can already see is still used as-is.)
#
# Install to /opt/cascade-hook-wrapper.sh, mode 755, root-owned.
#
# Wire it in /etc/transmission-daemon/settings.json (daemon stopped first):
#   "script-torrent-done-enabled": true,
#   "script-torrent-done-filename": "/opt/cascade-hook-wrapper.sh",
set -uo pipefail
CONTAINER="faucet"
host_path="${TR_TORRENT_DIR:-}/${TR_TORRENT_NAME:-}"
# REMOVE_ON_COMPLETE, MEDIASORT_MODE and the destination libraries come from
# faucet itself (its settings and the stack env).
exec docker exec \
  -e FAUCET_PATH="$host_path" \
  -e FAUCET_NAME="${TR_TORRENT_NAME:-}" \
  -e FAUCET_ID="${TR_TORRENT_ID:-}" \
  "$CONTAINER" python -m faucet.hook

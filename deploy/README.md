# Host-side deploy artifacts

Files here run on the Docker **host**, not inside the faucet container, so they
are not copied in by the Dockerfile and have to be installed by hand. They live
in git so a host rebuild doesn't silently lose them.

## cascade-hook-wrapper.sh

Transmission's completion hook. Transmission runs on the host and writes to
`/mnt/nas/torrents`; the container sees the same NAS path as `/downloads`. The
wrapper translates between the two and calls `faucet.hook` inside the container
via `docker exec`.

Install:

```bash
sudo install -m 0755 -o root -g root \
  deploy/cascade-hook-wrapper.sh /opt/cascade-hook-wrapper.sh
```

The mode matters. Transmission execs this script directly, so a copy without
the execute bit fails silently at completion time — and files committed through
the GitHub API land as `644`, so don't just `cp` it.

Then, with the daemon **stopped** (transmission rewrites `settings.json` on
exit and will clobber edits made while it's running):

```bash
sudo systemctl stop transmission-daemon
sudo -e /etc/transmission-daemon/settings.json
```

```json
"script-torrent-done-enabled": true,
"script-torrent-done-filename": "/opt/cascade-hook-wrapper.sh",
```

```bash
sudo systemctl start transmission-daemon
```

Verify by completing a small torrent and checking that faucet logged the hook
firing. If nothing happens, check in this order: the execute bit, the path in
`settings.json`, and whether the `faucet` container is actually named `faucet`
(the wrapper hardcodes it).

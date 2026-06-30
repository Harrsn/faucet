# Completion hooks

Faucet sorts and (optionally) cleans up a download when it finishes. Your
torrent client triggers this by running `python -m faucet.hook` on completion.
The hook figures out *what* finished from environment variables the client sets.

In Docker, the bundled Transmission is wired for you. For bare-metal or your own
client, set it up as below.

## Transmission

Transmission exports `TR_TORRENT_DIR`, `TR_TORRENT_NAME`, and `TR_TORRENT_ID`
automatically. In `settings.json` (stop the daemon first — it rewrites on exit):

```json
"script-torrent-done-enabled": true,
"script-torrent-done-filename": "/path/to/faucet-hook.sh"
```

Where `faucet-hook.sh` is:

```bash
#!/usr/bin/env bash
set -a; . /path/to/faucet/.env; set +a
exec python3 -m faucet.hook
```

## qBittorrent

Options → Downloads → **Run external program on torrent completion**:

```
/path/to/faucet-hook.sh "%F" "%I"
```

`%F` is the content path, `%I` is the hash. Map them in the wrapper:

```bash
#!/usr/bin/env bash
set -a; . /path/to/faucet/.env; set +a
export FAUCET_PATH="$1" FAUCET_ID="$2"
exec python3 -m faucet.hook
```

## Deluge

Install the **Execute** plugin, add a "Torrent Complete" command pointing at a
wrapper. Deluge passes `torrentid`, `torrentname`, `torrentpath` as arguments:

```bash
#!/usr/bin/env bash
set -a; . /path/to/faucet/.env; set +a
export FAUCET_ID="$1" FAUCET_NAME="$2" FAUCET_PATH="$3/$2"
exec python3 -m faucet.hook
```

## Containerized Faucet (Transmission on host, Faucet in Docker)

If your torrent client runs on the host but Faucet runs in a container, the hook
has to reach the completed file *inside* the container, which means translating
the host download path to the container's mount path. A ready-made, commented
template is in [`docs/faucet-hook-wrapper.sh`](faucet-hook-wrapper.sh) — copy it,
edit the three variables at the top (`CONTAINER`, `HOST_DL`, `CONTAINER_DL`),
make it executable, and point `script-torrent-done-filename` at it.

Two things make this setup work cleanly:

- **Same storage for downloads and library.** Point the client's download dir
  and Faucet's `LIBRARY_ROOT` at the *same* filesystem/share (e.g. both on one
  NAS mount). Then the sorter relocates finished files with an instant rename —
  one copy, no duplication. If they're on different filesystems, the sorter must
  copy, which leaves the original behind (and fills your download disk).
- **`MEDIASORT_MODE`.** Set this in the container's environment. `auto` (default)
  tries hardlink → move → copy; on a CIFS/SMB share (no hardlinks) it moves.
  Set it to `move` explicitly if you want predictable move-and-clean behavior and
  don't need to seed.

## What the hook does

1. Runs the sorter (`faucet/sort.py`) on the completed path — renames and files
   it under `LIBRARY_ROOT/{movies,tvshows}`.
2. Appends an event to `EVENTS_FILE` (shown in the UI's Events tab).
3. Fires notifications per `NOTIFY_ON`.
4. If `REMOVE_ON_COMPLETE=1`, removes the torrent from the client (stops seeding).

If the sort fails, the torrent is **not** removed — you never lose a file that
didn't make it to the library.

## Catch-up sweep (safety net)

The hook handles the normal case, but it can miss: a client fires it before
files finish moving, a torrent finishes while the hook/wrapper is misconfigured,
or a manually-added download never triggers it. Those items sit downloaded but
unfiled. `faucet.sweep` is the safety net — run it on a timer and it sorts
anything the hook missed.

It is safe to run repeatedly on a live system:

- Only sorts a top-level item under `complete/` whose newest file has been
  unmodified for `SWEEP_SETTLE_MIN` minutes (default 15), so active downloads are
  skipped.
- Skips staging/in-progress content: any item named `temp`/`incomplete`/etc., or
  containing a nested `incomplete/` subtree or a partial file (`.part`, `.!qb`, …).
- Delegates to the same sorter, whose placement is idempotent (a same-size
  destination is skipped), so re-sweeping never duplicates.

Test it first (moves nothing):

```bash
docker exec faucet python -m faucet.sweep --dry-run
```

Then wire it on a timer. **systemd** (recommended — gives you run history):

```ini
# /etc/systemd/system/faucet-sweep.service
[Unit]
Description=Faucet completed-downloads catch-up sweep
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
# adjust the docker path (which docker) and container name for your host
ExecStart=/usr/bin/docker exec faucet python -m faucet.sweep
```

```ini
# /etc/systemd/system/faucet-sweep.timer
[Unit]
Description=Run the Faucet sweep every 30 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now faucet-sweep.timer
systemctl list-timers faucet-sweep.timer
```

Or **cron**:

```cron
*/30 * * * * /usr/bin/docker exec faucet python -m faucet.sweep >> /var/log/faucet-sweep.log 2>&1
```

Bare-metal (Faucet not in Docker)? Drop the `docker exec faucet` prefix and run
`python -m faucet.sweep` directly.

**Tuning.** 30-minute frequency pairs well with the 15-minute settle window —
don't go below ~20 min or you'll fight the settle delay. Override the window with
`--settle-min N` or the `SWEEP_SETTLE_MIN` env var; the scan dir defaults to
`$DOWNLOAD_DIR/complete` and can be overridden with `COMPLETE_DIR`. When the
sweep files something the hook missed, it writes a `sweep` event visible in the
UI's Events tab.

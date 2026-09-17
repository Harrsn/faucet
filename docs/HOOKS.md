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
  Note that two *separate* bind mounts count as different filesystems even when
  they point into the same share (`/mnt/nas/torrents:/downloads` +
  `/mnt/nas/media:/library`): rename fails with EXDEV and every move becomes a
  copy. Mounting the share's parent once and pointing both paths inside it
  restores instant renames.
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

### How the sorter protects your files

- **Nothing half-written is ever visible.** Copies are written to a hidden
  `.<name>.<pid>.faucet-partial` beside the destination, fsynced, size-checked,
  and only then renamed into place. A crash, a dropped share, or a container
  restart mid-copy leaves the library untouched (idle partials are cleaned up
  after 30 minutes).
- **Existing files are replaced only by provably better releases.** The sorter
  records the resolution and cam/telesync status of every file it places. A
  new release for the same path replaces it only if it ranks higher; equal,
  worse, or unknown quality keeps what you have.
- **Samples are skipped, extras are kept.** `Sample/` content is ignored;
  `Featurettes/`, `Behind The Scenes/`, `-trailer` files etc. go to the
  matching Plex extras folder. Two videos that would get the same name never
  overwrite each other (numbered `CD1`/`CD2` parts become `- pt1`/`- pt2`).
- **Subtitles keep their tags** (`Movie (2019).en.forced.srt`), including
  RARBG-style `Subs/` folders.
- **Executable "releases" are quarantined and defused.** A media download
  whose payload is executables with no video (a common bait for brand-new
  episodes) is moved to `_failed/` with every executable renamed
  `*.faucet-blocked`, so it can't be launched from the share. The live guard
  normally catches these earlier and pauses them — see "Fake releases" below.
- **Anything that can't be filed is quarantined, not deleted.** When the release
  is being consumed (`MEDIASORT_MODE=move` or `REMOVE_ON_COMPLETE=1`), leftover
  content — unparseable files, lower-quality duplicates, disc images, archives —
  is moved intact to `_failed/` next to the release (override with
  `QUARANTINE_DIR`). The sweep never touches `_failed/`; review it by hand.

The sorter's exit code tells the hook what's safe:

| rc | Meaning | Torrent removed? |
|----|---------|------------------|
| 0 | Everything of value filed (or left seeding) | yes, if `REMOVE_ON_COMPLETE=1` |
| 4 | Filed; some content quarantined to `_failed/` | yes — nothing is left inside it |
| 5 | Suspicious (executables, no video); quarantined and defused | yes — nothing is left inside it |
| 2 | I/O error; release left in place for retry | no |
| 1 | Library not mounted / no input | no |

## Fake releases

Faucet checks every download three times:

1. **Before grabbing** — search results and hunter candidates whose name is an
   executable (`Show.S01E01.1080p.exe`) are dropped (`BLOCK_EXECUTABLE_RELEASES`).
   Hidden counts show up next to search results.
2. **While downloading** — every `GUARD_INTERVAL_SECONDS` (60) Faucet reads each
   torrent's file list once its metadata arrives. A media torrent whose files are
   executables with no video is **paused**, flagged in Activity → Transfers and
   on the dashboard, recorded as a `suspicious` event, and notified when
   `failed` or `suspicious` is in `NOTIFY_ON`. The release is never grabbed
   again and the episode goes back to wanted. Faucet never deletes it: remove it
   yourself, or resume it if you're sure (the guard won't pause it twice).
   Games and software are exempt.
3. **After downloading** — the sorter backstop above (exit code 5).

New episodes also aren't hunted until `AIR_DELAY_DAYS` (default 1) after their
air date: bait uploads appear hours before a broadcast, real ones after it.

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

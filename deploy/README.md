# Host-side deploy artifacts

Files here run on the Docker **host**, not inside the faucet container, so they
are not copied in by the Dockerfile and have to be installed by hand. They live
in git so a host rebuild doesn't silently lose them.

## How config actually resolves

Three layers, highest priority **last**:

| Layer | Holds | Owner |
| --- | --- | --- |
| `deploy/stack.yml` → `environment:` | `EVENTS_FILE`, `HUNT_MAX_*`, `MEDIASORT_MODE`, `DEFAULT_LANGUAGE` | this repo |
| `/opt/cascade-config/faucet.secrets.env` | `SESSION_SECRET` | host, root:root 600 |
| `/opt/cascade-config/faucet.env` | everything in `config.py`'s `WIZARD_KEYS` | Faucet's Settings panel |

The third layer is the one that surprises people. `config.py::_load_persisted()`
writes that file's keys **into `os.environ`** at import time, so it overrides
anything the container was started with. Setting `JACKETT_URL` or `CLIENT_PASS`
in the stack file does nothing while `faucet.env` defines them — change those in
Settings, not in compose.

Corollary worth remembering when rotating credentials: rotating at the source
(Discord, Jackett, Transmission) and updating the stack file is **not enough**.
`faucet.env` still holds the old value and still wins.

## deploy.sh

Replaces the old manual sequence — `git pull`, `docker build` by hand, click
redeploy in Portainer — with one command.

```bash
sudo install -m 0755 -o root -g root deploy/deploy.sh /usr/local/sbin/faucet-deploy
sudo /usr/local/sbin/faucet-deploy
```

**Install it root-owned; don't sudo it from the repo path.** The checkout is
owned by the ops account, so a `NOPASSWD` sudoers rule pointing at
`/opt/faucet/deploy/deploy.sh` would let that account rewrite the script and
run anything as root. The installed copy has to be somewhere it can't write.
Re-run the `install` after any change to the script — the repo copy is the
source, `/usr/local/sbin/faucet-deploy` is what actually runs.

What it does: fast-forward the checkout (as the owning account, so the tree
doesn't turn root-owned), rebuild `faucet:local`, then
`docker compose -p cascade -f deploy/stack.yml up -d --force-recreate faucet`.

Two details that are load-bearing:

- **`--force-recreate`.** The image tag never changes, so compose otherwise
  sees no reason to replace the running container and the deploy silently
  succeeds while changing nothing.
- **`merge --ff-only`.** A diverged or dirty checkout stops the deploy instead
  of producing a merge commit on a production box.

It exits non-zero if the container isn't `running` afterward, so a failed
deploy reports as failed rather than looking clean.

To deploy a specific tag rather than the head of `main`, check it out by hand
first — the script deliberately has no ref argument, because its whole job is
"make the box match `main`".

## stack.yml

The compose definition, previously living only in Portainer's data dir under
`/data/appdata/portainer/compose/18/` — root-only, untracked, and carrying four
live secrets inline.

Portainer still shows the stack (the compose labels are unchanged), but
`deploy.sh` is what deploys now. Portainer's own "Update the stack" button
would redeploy from *its* stored copy, which no longer gets updated — so use
the script, not the button.

Portainer can't run this deploy itself: it only mounts `/var/run/docker.sock`
and its own data directory, so neither a `build:` context pointing at
`/opt/faucet` nor an `env_file:` at a host path would resolve from inside it.

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

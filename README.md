<div align="center">

# 🌊 Faucet

**The whole search → download → sort → manage loop for your media, in one lightweight self-hosted app.**

No five-container *arr stack. One service over your indexer and torrent client, with a clean web UI.

[![CI](https://github.com/Harrsn/faucet/actions/workflows/ci.yml/badge.svg)](https://github.com/Harrsn/faucet/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

</div>

---

## What it does

Search across your indexers, pick a result, and Faucet hands it to your torrent
client, watches it download, then automatically renames and files it into your
library in a Plex/Jellyfin layout — all from one page.

- **🔍 Unified search** across every indexer Jackett/Prowlarr exposes, ranked by seeders, with type/quality badges (1080p · WEB-DL · MKV).
- **⬇️ Pick a client** — Transmission, qBittorrent, or Deluge. Same UI, swap one env var.
- **📊 Live dashboard** — disk free, total speed, active/seeding counts, per-torrent progress, pause/resume/remove, file lists.
- **🗂️ Auto-sort** — finished downloads are renamed and filed into `movies/` and `tvshows/` (`Show/Season 01/Show - S01E03.mkv`) with `guessit`.
- **🔔 Notifications** — Discord, Telegram, ntfy, Gotify, or any webhook on completion.
- **🎨 Themes** — dark/light + accent presets.
- **⌨️ Keyboard-driven** — arrow-key through results, Enter to add, search history.
- **🪄 First-run wizard** — guided setup with live connection testing; no config files to edit.

> **Heads up:** Faucet is a tool for managing your own downloads. You're responsible
> for what you download and for complying with the law where you live.

## Screenshots

<!-- Replace with real captures: docs/screenshot-search.png etc. -->
| Search + badges | Activity dashboard | Events feed |
|---|---|---|
| ![search](docs/screenshot-search.png) | ![activity](docs/screenshot-activity.png) | ![events](docs/screenshot-events.png) |

## Quickstart (Docker)

```bash
git clone https://github.com/Harrsn/faucet.git
cd faucet
cp .env.example .env        # set JACKETT_API_KEY (and client creds if not default)
docker compose up -d
```

Open **http://localhost:8088**.

The compose stack bundles **Jackett** (add your indexers at `:9117`) and
**Transmission** so it works out of the box. Already running your own? Delete
that service from `docker-compose.yml` and point the env vars at yours.

### First run

On first launch Faucet opens a **setup wizard** — no file editing needed:

1. Open Jackett at `http://localhost:9117`, add a few indexers, copy the **API key**.
2. In Faucet, the wizard walks you through indexer → client → library, testing each
   connection live before saving.
3. Search, click **add**, watch it download and get filed into `./library`.

## Bare-metal

```bash
pip install -e .
cp .env.example .env        # fill in URLs + key
set -a; . ./.env; set +a
uvicorn faucet.app:app --host 0.0.0.0 --port 8088
```

Wire the completion hook in your client to run `python -m faucet.hook` so finished
downloads get sorted — see [docs/HOOKS.md](docs/HOOKS.md) for per-client setup.

## Configuration

All via environment / `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `JACKETT_API_KEY` | — | **Required.** Indexer API key. |
| `JACKETT_URL` | `http://127.0.0.1:9117` | Jackett/Prowlarr base URL. |
| `DOWNLOAD_CLIENT` | `transmission` | `transmission` \| `qbittorrent` \| `deluge`. |
| `CLIENT_URL` | Transmission RPC | Client web/RPC endpoint. |
| `CLIENT_USER` / `CLIENT_PASS` | — | Client credentials. |
| `LIBRARY_ROOT` | `/library` | Where sorted media is filed. |
| `DOWNLOAD_DIR` | `/downloads` | Active download dir. |
| `REMOVE_ON_COMPLETE` | `0` | Remove finished torrents (stops seeding). |
| `NOTIFY_URLS` | — | Comma-separated notification targets. |
| `NOTIFY_ON` | `completed,sorted,failed` | Which events notify. |
| `UI_THEME` / `UI_ACCENT` | `dark` / `blue` | Appearance. |

Full list and notification URL formats are in [.env.example](.env.example).

## Library tidy tool

Normalize existing season folders to `Season NN` and merge scattered duplicates:

```bash
python -m faucet.libtidy                 # dry-run, shows the plan
python -m faucet.libtidy --tv --apply    # execute TV renames
```

## How it compares

Faucet isn't trying to replace Sonarr/Radarr's deep per-series automation. It's for
people who want **one simple app** that does search, grab, sort, and manage without
running Prowlarr + Sonarr + Radarr + a request frontend. If you want a single pane of
glass and minimal moving parts, that's the niche.

## Contributing

Issues and PRs welcome. `pytest -q` runs the suite; new client backends just implement
`faucet/clients/base.py`'s `DownloadClient` interface.

## License

MIT — see [LICENSE](LICENSE).

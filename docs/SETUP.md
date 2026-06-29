# First-run setup

On first launch — before any indexer key or client is configured — Faucet opens
a **setup wizard** automatically. No file editing required.

The wizard walks three steps:

1. **Indexer** — enter your Jackett URL and API key, then **test connection** to
   confirm the key works against a live search.
2. **Download client** — pick Transmission / qBittorrent / Deluge, enter the URL
   and credentials, and **test** that it authenticates.
3. **Library & finish** — set the library root, app name, accent color, and
   optional notifications. Finish writes the config and Faucet is live.

## Where settings are stored

The wizard writes to `FAUCET_CONFIG_FILE` (default `/config/faucet.env`). These
values override process environment and survive restarts, so you can configure
entirely through the UI. You can still set everything via `.env` / environment if
you prefer — the wizard only appears when the minimum config is missing.

## Re-running

Delete `/config/faucet.env` (or clear `JACKETT_API_KEY`) and reload to get the
wizard back. The `/api/config` endpoint reports `configured: true/false`, and
`/health` checks live reachability of the indexer and client.

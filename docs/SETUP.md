# First-run setup

Faucet configures through its own UI — no file editing required for the basics,
though everything can also be set via environment / `.env` if you prefer.

## 1. Create the admin account

On first launch you'll land on the sign-in page. Click **Register** and create
the first account — **the first account ever created becomes the admin**, active
immediately. Anyone who registers afterward lands in a *pending* state and needs
admin approval before they can sign in.

> Set a long, fixed **`SESSION_SECRET`** in your environment first. Without it,
> Faucet generates one and stores it, but a fixed value keeps everyone's sessions
> valid across restarts and redeploys. Generate one with:
> `python -c "import secrets; print(secrets.token_urlsafe(48))"`

## 2. Configure connections

Open **Settings** (admin only) and fill in the **Connections** tab:

1. **Jackett URL + API key** — the key is on your Jackett dashboard (top-right).
   Add a few indexers there first.
2. **Download client** — pick Transmission / qBittorrent / Deluge and enter the
   RPC URL and credentials.
3. Hit **Test connections** to confirm both the indexer and client are reachable
   and authenticate, live, before relying on them.

In the **Metadata** tab, paste a free **TMDb API key**
(https://www.themoviedb.org/settings/api) to enable title search, posters, and
episode tracking, and set a **default quality profile** and language.

In the **Advanced** tab, confirm the library/download **paths** show green
(mounted & writable) — the status chip tells you immediately if a path isn't
mounted the way the container expects.

## 3. Import your library

Go to **Shows** or **Movies** and hit **import library** to auto-monitor
everything already on disk, or **add** individual titles. Faucet scans,
reconciles against TMDb, and starts hunting what's missing.

## Where settings are stored

UI-edited connection/path/behavior settings persist to `FAUCET_CONFIG_FILE`
(default `/config/faucet.env`). These values override process environment and
survive restarts, so you can configure entirely through the UI. The TMDb key,
default language, and default profile are stored in the database. Secrets that
should stay out of the app (the session key, DB path, listen port) live only in
your environment / stack.

The `/health` endpoint reports live reachability of the indexer and client.

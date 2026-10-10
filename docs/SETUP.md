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

Everything you set in the UI — connections, paths, behavior, the TMDb key,
default language and profile — is stored in Faucet's database
(`cascade.db` in the config volume, owner-only). Upgrading from a version that
used `faucet.env`, its values are imported once on first start and the file
is renamed `faucet.env.imported`; after that, environment values for those
settings are ignored (a startup warning names them). The environment keeps
only bootstrap: the session key, the data path, the listen port, and tunables
that have no UI yet.

The `/health` endpoint reports live reachability of the indexer and client.

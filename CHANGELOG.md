# Changelog

## Unreleased (Phase 8)

### Added

- **Two-factor sign-in with an authenticator app** (TOTP: Bitwarden,
  1Password, Google or Microsoft Authenticator). Set it up from **Account**
  (click your name in the sidebar): scan a QR code, confirm a code, save ten
  one-time recovery codes. Sign-in becomes password → code; a recovery code
  works in place of a code (each once). Codes can't be replayed, wrong ones
  count toward sign-in throttling, and setting up or changing it needs the
  password.
- **Required for admins.** An admin without it can sign in but only reach
  the setup page until it's on (`FAUCET_REQUIRE_ADMIN_2FA=0` to turn the
  requirement off). Optional for members.
- Admins can reset someone's two-factor (Users & requests → ⋯ → Reset
  two-factor) for a lost phone; locked out yourself, on the server:
  `docker exec faucet python -m faucet.mfa disable <username>`.
- **Account page**: two-factor and password in one place.

### Fixed

- A sign-in page loaded before two-factor existed sent the password, took
  "now enter your code" as success, and bounced back to itself in a loop.
  The sign-in page now tells the server it knows the code step; an older
  page signing in to a two-factor account gets "Faucet was updated —
  refresh this page to sign in" instead.
- The code field's auto-submit only works on the code step: a password
  manager filling the (hidden) field as the page loads no longer submits the
  form. Its listeners were also being attached inside `render()`.
- If the sign-in script ever fails to run, the browser's own form submit is
  a POST (the password can't end up in the address bar) and leads back to
  the sign-in page with a "try again" message.

## 2.3.0 — 2026-10-11

### Added

- **Per-user library access.** An admin can limit a member to some
  libraries (Settings → Users & requests → ⋯ → Library access): the
  sidebar, Home, Calendar, search, the TV and movie lists and every show and
  movie page then only show those — enforced by the server, which answers
  404 for anything hidden. A member's approved requests are filed into
  their "requests go to" library (or, when they can see only one library of
  a kind, that one). Requesting a title that's already in a library they
  can't see says so without revealing where. Admins always see everything;
  members with no limit see everything, as before.
  `PUT /api/admin/users/{id}/libraries`.
- **Calendar feed.** Calendar → "Subscribe on your phone" creates a private
  link (`/cal/<token>.ics`) for Apple, Google or Outlook calendars: every
  episode of your shows from two weeks back to three months ahead as an
  all-day event, refreshing every few hours, limited to the libraries you
  can see. Only a hash of the link is stored, so it's shown once; making a
  new one revokes the old, and a disabled account's link stops working.
  `GET/POST/DELETE /api/me/calendar`.
- **Alerts.** Faucet tells you when something needs attention — once when
  it starts, once when it clears — through your notification targets, and
  shows it on Home while it lasts: an indexer failing every search for an
  hour, a library's (or the downloads folder's) disk under 10% free, the
  download client not answering for 10 minutes, the same episode or movie
  stalling three times in a day, and Faucet restarting after an unclean
  exit (killed, out of memory, power loss — not a deploy). Each check and
  its threshold is in Settings → Integrations → Alerts, with "Check now".
  Checks run every five minutes. `GET /api/alerts`, `POST /api/alerts/check`.

## 2.2.1 — 2026-10-11

### Changed

- **New icon**: the app's gold drop on a dark tile, as the browser favicon
  and the README logo. The favicon is served from the content-hashed
  `/app/<version>/` path like the UI modules, so a new icon shows on the next
  load instead of after the browser's (and Cloudflare's) favicon cache
  expires; `/favicon.svg` and `/static/favicon.svg` still work.

## Unreleased (documentation, shipped with 2.2.1)

### Changed

- **New README**, with new screenshots (shot list in `docs/screenshots/`).
- **The bundled `docker-compose.yml` uses one data mount** (`./data` → `/data`,
  with `media/` and `torrents/` inside) for Faucet and Transmission, so a
  finished download moves into the library by rename instead of a full copy.
  `.env.example` is cut down to what the environment is still for
  (`SESSION_SECRET`, compose paths, proxy and sign-in hardening, operations);
  everything else is set in the setup wizard. Existing installs keep their own
  compose files and are unaffected.
- `docs/HOOKS.md` points at `deploy/cascade-hook-wrapper.sh` and path mappings;
  the old path-rewriting `docs/faucet-hook-wrapper.sh` is gone.

## 2.2.0 — 2026-10-11

### Added

- **Every library is its own section in the sidebar**, like Plex: "TV
  Shows", "Mamaw's Shows", "Movies", "Mamaw's Movies", each with its title
  count, each opening a grid of only that library's titles (filters and sort
  remembered per section). A title belongs to the library it's filed into, so
  a relative's shows no longer mix into yours. Show and movie pages put their
  library in the breadcrumb and highlight its section; the sidebar updates
  when a title's "Save to" changes or a library is added. Members see the
  same sections. `GET /api/nav`; `/api/series` and `/api/movies` carry each
  title's library.

## 2.1.0 — 2026-10-11

### Added

- **Each show and movie has a library it's filed into** ("Save to", like
  Sonarr/Radarr root folders) — for a separate collection such as a
  relative's own Plex libraries. Titles whose files live in a non-default
  library are assigned to it automatically (shows by their linked folder,
  movies by their file); a choice made by hand is never overridden. The
  completion hook and sweep route each finished download to its title's
  library; anything unmatched or ambiguous goes to the defaults.
  `POST /api/series/{id}/library`, `POST /api/movies/{id}/library`.
- **Import titles per library** (Settings → Libraries): adopt one
  library's shows or movies without touching the rest of what's on disk, and
  choose what happens next — just track them, get new episodes, or hunt the
  back catalogue. `POST /api/libraries/{id}/import`,
  `GET /api/libraries/import`.

## 2.0.2 — 2026-10-11

### Fixed

- **Every finished download was copied into the library over the network.**
  Inside the container `/downloads` and `/library` are separate bind mounts
  (even of the same NAS share), so the sorter's rename failed and it fell
  back to copy + delete — gigabytes through the share, with Transmission's
  RPC stalling meanwhile. Faucet now shows, per library, whether moves are an
  **instant rename** or a **copy**, read from the kernel's mount table
  (Settings → Libraries and → Path mappings), so libraries and downloads can
  be put on one mount.

### Added

- **Finished downloads folder** setting (`COMPLETE_DIR`, Settings → Path
  mappings): where completed downloads land as Faucet sees it. `DOWNLOAD_DIR`
  stays what Faucet tells the *client*; it used to double as both.
- **Re-pointing a library keeps what Faucet knows** when the new folder is
  the same one through another mount (`/library/tvshows` →
  `/nas/media/tvshows`): every file record and show-folder link is carried
  over, so nothing is rescanned or re-hunted. A genuinely different folder
  drops the old folder's records and is scanned fresh.
- `deploy/cascade-hook-wrapper.sh` hands Faucet the path as Transmission
  reports it; Settings → Path mappings translates it.

## 2.0.1 — 2026-10-10

### Fixed

- **Home and Activity could take a minute or more to load.** They asked the
  download client for its transfer list inside the request. Transmission
  answers RPC on the thread that does its disk I/O, so while a finished
  download was being copied onto the NAS the page waited out the client
  timeout (90s on this install). The list now comes from a background
  poller (every `TRANSFERS_POLL_SECONDS`, 5; at most a 10s wait per poll;
  idle after a minute without readers): pages read the last good list
  instantly and say so when the client is slow. Grabs and pause / resume /
  remove trigger an immediate refresh. A cold cache (just after a restart)
  wakes the poller at once instead of letting the first page wait out its
  interval.

## 2.0.0 — 2026-10-10

A new interface and a new configuration model. **Upgrading:** settings that
lived in `faucet.env` or the environment are imported into the database on
first start and the environment is ignored for them afterwards (a startup
warning names any it skips); `/classic` is gone.

### Added

- **First-run setup.** A fresh install opens on "Welcome to Faucet": create
  the admin account, then a guided setup — libraries, download client, path
  mapping, indexer, TMDb, and importing what's already on disk (in the
  background; big libraries take minutes). Any step can be skipped or
  finished later, and everything is changeable in Settings afterwards.
  Existing, configured installs never see it.
  `GET /api/setup/state`, `POST /api/setup/import`, `POST /api/setup/complete`.
- **New sign-in page** in the app's design: sign in, request an account,
  set a password from a reset link.
- **Fix location** for movies in the new UI: point a movie at a file Faucet
  didn't recognise, from a file picker.

### Removed

- **The classic UI (`/classic`).** Everything it did is in the new UI, except
  the file browser's move/mkdir tools and the legacy Auto-grab query
  subscriptions page (their APIs remain; existing subscriptions still run).

### Security

- **Strict script policy:** `script-src 'self'` — no inline script anywhere,
  so an HTML-injection bug can't run script. A test fails the build if any
  page carries an inline `<script>` or `on…=` handler.

- **New UI, step 4a: the remaining pages.** **Discover** (TMDb lists as
  rails for TV and movies; titles you have link to their page, the rest
  get Monitor or Request on the card), **Calendar** (last week and the
  next three, by day, filterable), **Requests** (members see theirs; admins
  approve, decline or delete, waiting first), **Activity** (live downloads
  with pause / resume / remove, history, event log) and **Wanted** (every
  title the hunter is after, what's due now and when each is searched
  next). Nothing in the new UI opens the classic one any more.
  `GET /api/wanted/summary`.

- **New UI, step 3b: the rest of Settings.** **General** (default audio
  language and quality profile, plus everyday behaviour), **Quality
  profiles** (list and editor), **Users & requests** (approve, disable,
  roles, auto-approve a member's requests, password-reset links, create
  accounts), **Hunting**, and **Maintenance & backup** (database size and
  backup, duplicate files and retiring them, retention). Settings no longer
  sends anything to the classic UI.
- **Behaviour settings live in the store.** The hunting, retry, stall,
  search, refresh and retention values that were environment variables are
  edited on those pages, validated against sensible ranges, and apply on
  the next scheduler tick without a restart. Values set in the environment
  are imported once (an existing store adopts them on first start of this
  version); after that the environment is ignored for them, with a warning.
  `GET/PUT /api/settings/tunables`.

### Changed

- Quality profiles are validated (known resolutions and sources, sane
  ranges, a real language code). Deleting one moves its shows and movies to
  the default profile, clears it as the default if it was, and the last
  profile can't be deleted. `GET /api/profiles/options`.
- The create-account form tells password managers to keep out (Bitwarden
  used to fill the admin's own credentials into it).
- `deploy/stack.yml` drops `HUNT_MAX_ACTIVE`, `HUNT_MAX_PER_RUN` (they were
  the defaults) and the unused `DEFAULT_LANGUAGE`.

- **New UI, step 3a: Settings.** A full Settings page with its own section
  list. **Libraries**: library cards (folder, file count, last scan,
  unreachable / read-only warnings), add and edit with a folder picker over
  the container's mounts, remove, scan all; and **Show folders**: every show
  folder with the show it's linked to — link or relink by hand, or monitor
  a show from TMDb and link it in one step. **Integrations**: Jackett (with
  per-indexer health over 24h and Probe now), download client, TMDb, and
  notifications — each with a live Test. **Path mappings**: edit the table
  and translate a test path. Sections not moved yet open the classic UI.
- `GET /api/libraries/browse`, `GET/PUT /api/settings/notifications`,
  `POST /api/settings/notifications/test`, `POST /api/settings/test-tmdb`.

### Changed

- **The app title is fixed: Faucet.** `APP_TITLE` is no longer a setting;
  a stored value is ignored.
- **Notification targets are write-only**, like keys and passwords: the
  API shows each one's service and host, never the webhook path or token.
  The classic settings form no longer edits them (so it can't blank them).

### Fixed

- **A deploy could leave browsers on the previous UI for hours.** Cloudflare's
  Browser Cache TTL rewrote the modules' `no-cache` to `max-age=14400`, so
  6.2 shipped while browsers kept running 6.1's JavaScript (TV Shows still
  opened the classic page). The shell now loads every module and stylesheet
  from `/app/<content hash>/…`: each deploy that changes the UI gets new
  URLs, cached forever; an old page left open across a deploy keeps working.

### Added

- **New UI, step 2: TV Shows and Movies.** Poster grids with filters
  (missing, complete, continuing, ended, not monitored / on disk, wanted,
  upgrading), sort and a title filter. Show pages: backdrop header, genres
  and status, completion, Hunt missing, monitor mode and quality profile,
  Fix match and Remove in a ⋯ menu; season cards; episode rows with TMDb
  stills, air dates and a status each (on disk / downloading / missing with
  the next search time / unaired / not monitored); per-episode and
  season-pack release search; Hunt log and Files tabs. Movie pages: what's
  on disk (quality, source, size, path), what the hunter is doing, release
  search, hunt log. `GET /api/series/{id}/detail`,
  `GET /api/movies/{id}/releases`, `POST /api/movies/{id}/grab`.
- Home groups a season pack into one card ("S02 · 7 episodes") that opens
  the show at that season; chips on posters are opaque; "still hunting"
  never shows 100% while something is wanted.

### Fixed

- Episode stills: seasons cached before stills were kept are re-fetched,
  and pages read stills from the cache only (missing ones are fetched in
  the background) so Home and show pages never wait on TMDb.
- The Shows list counts aired, non-special episodes — the same total the
  show page shows.

- **New UI, step 1 (Direction A).** A sidebar shell (Library · Browse ·
  Manage — members see only the first two), a ⌘K / `/` search palette
  (your library instantly, TMDb with one-click Monitor or Request, and for
  admins releases across every indexer on demand), and a new Home: the
  newest arrival as a hero with its backdrop, then rails — recently added
  episodes with TMDb stills, downloads (live), movies, and shows still
  being hunted — with arrow buttons instead of a bare scrollbar. Native
  ES modules, no build step, no dependencies; every value is escaped by the
  one template helper that touches the DOM. Works on phones (drawer menu).
  `GET /api/home`.
- **The previous UI stays at `/classic`** while the rest is rebuilt; pages
  the new UI doesn't have yet open there (deep links such as
  `/classic#show/12`).

- **Libraries.** Folders Faucet scans and files into are records
  (`name`, TV or Movies, folder, which one new downloads go to) instead of
  the fixed `LIBRARY_ROOT/tvshows` + `LIBRARY_ROOT/movies`. An existing
  install starts with exactly those two (they keep following
  `LIBRARY_ROOT` until edited), so nothing moves. Add as many as you like,
  anywhere the container can see; they can't nest. The scanner walks all of
  them; a library whose folder is unreachable keeps its rows instead of
  "losing" its files. The hook and sweep hand the sorter the default
  libraries. Removing a library never touches files.
  `GET/POST /api/libraries`, `PATCH/DELETE /api/libraries/{id}`,
  `POST /api/libraries/scan` (background).
- **Show folders are linked to series by id.** A folder is matched to a
  series once — automatically when the names agree (year tags and
  punctuation ignored; ambiguous names are left for you), or by hand — and
  every file in it is stamped with the series. Ownership checks use the
  link first and fall back to name matching only for unlinked folders, and
  a folder linked to one series never counts for a similarly-named other.
  A renamed TMDb title no longer un-owns a show. Manual links win and
  survive rescans. `GET /api/libraries/folders`,
  `PUT /api/libraries/{id}/links`.
- **Path mappings** between the download client's paths and Faucet's
  (`/mnt/nas/torrents` → `/nas/torrents`). The completion hook maps a path
  it can't see, so the host wrapper no longer has to rewrite paths.
  `GET/PUT /api/path-mappings`, `GET /api/path-mappings/test`.

### Changed

- The dashboard's episode count is canonical episodes owned (as the Shows
  page counts them), not every file whose folder resembles a monitored show.
- "Settings imported…" is logged once logging is up (it was lost before).

- **Settings live in the database.** Connections, paths, behavior and
  notifications are stored in `cascade.db` (now mode 0600) instead of
  `faucet.env`. On first start every value from the old file — or from the
  environment — is imported once and the file is renamed
  `faucet.env.imported`; after that the database is the only source and an
  environment value for a setting is ignored with a startup warning. This
  ends the "changed it in compose, nothing happened" layering, and is the
  base for the setup wizard.
- **TV searches skip movie-only indexers and vice versa**, from the
  categories in each indexer's caps.
- **Stragglers don't hold a search.** Once most indexers have answered, the
  rest get `SEARCH_STRAGGLER_SECONDS` (10) more and are then left behind
  (they finish in the background). A cold Cloudflare solve used to make
  every search take 12s for no results.
- **Indexer health shows each indexer over 24h**: searches, average and
  p95 latency, results per search, empty and failed counts, and flags an
  indexer that never returns anything.

- **Every indexer is searched on its own, in parallel.** Jackett's aggregate
  `all` endpoint made each search as slow as the slowest indexer (a
  Cloudflare challenge being solved held up every result) and failed the
  whole search when one indexer timed out. Each configured indexer is now
  queried directly, all at once, with its own `SEARCH_TIMEOUT` (60s); a slow
  or broken one costs only its own results, reported per search ("8
  indexers searched, 1 failed" in the interactive search).
- **Structured queries.** Using each indexer's Torznab caps: `tvsearch` with
  `season`/`ep` (and TVDB/IMDb/TMDb id when the indexer supports it),
  `movie` by IMDb id; text-only indexers get the classic "Title S01E02" /
  "Title Year". TV searches are limited to TV categories and movie
  searches to movie categories. External ids come from TMDb (cached 30
  days).
- Results from several indexers are merged by info-hash, keeping the
  best-seeded copy.
- The indexer list (1h) and caps (24h) are cached; the indexer probe and a
  Jackett settings change refresh them.

## 1.6.1 — 2026-10-10

### Fixed

- **Indexer links that redirect to a magnet now add.** Jackett's download
  link for a public tracker answers `302 Location: magnet:?…`; following
  that with `requests` raised "No connection adapters were found", so the
  grab failed. Trackers whose results carry no magnet URL (EZTV and 1337x
  among them) could never be grabbed from. Redirects are now followed by
  hand for Transmission and Deluge, and a response that isn't a torrent
  file is refused instead of being sent to the client.
- **A failed add backs the want off** like a fruitless search. It used to be
  retried every tick, writing a `grab_failed` history row each time —
  38,731 rows (50 MB of the 53 MB database) in September.
- **History retention**: `HISTORY_DAYS` (365), and `HISTORY_FAILED_DAYS` (14)
  for failure noise; title/detail capped at 500 characters. The first tick
  after upgrading removes the backlog and the daily VACUUM reclaims it.

## 1.6.0 — 2026-10-10

Full-repo review: hunt correctness, security hardening, ops visibility.

### Added

- **Database backup** from Settings → Advanced: a consistent online copy of
  `cascade.db` (SQLite backup API) with sessions, reset tokens and the TMDb
  cache left out. The same panel shows the database size and its largest
  tables. `GET /api/settings/backup`.
- **`/metrics`** in the Prometheus text format, no client library: wants by
  status/reason, library counts, history and hunt outcomes over 24h, search
  results/latency, per-indexer probe status, disk free, DB size, last cycle.
  Bearer `METRICS_TOKEN` or an admin session; 404 when neither is set up.

- **Hunt log.** Every hunt attempt is recorded: query, how many results the
  indexer returned, how many were rejected and why (already tried, wrong
  title, pack, not an upgrade, seeders, size, resolution, cam, language),
  what was grabbed (tracker, seeders) and when the next search is due.
  "Hunt log" on show and movie pages; the per-episode search modal shows
  the episode's want state and last attempt. `GET /api/wanted/log`.
- **Indexer health** (Dashboard → Services → indexer health). Per-search
  telemetry over 24h (latency, failures by kind, results per tracker); a
  per-indexer probe that queries each Jackett indexer on its own — the
  aggregate endpoint waits for the slowest one, so a single hung tracker
  shows up as timeouts on every search — daily and on demand; and hunter
  grab outcomes per tracker (stalled vs completed).
- **Duplicate copies** are recorded by the scanner, listed in the scan
  report with their size, and can be retired to `_superseded/` (reversible,
  same folder layout). They are no longer re-parsed on every scan.
  `GET /api/library/duplicates`, `POST /api/library/duplicates/retire`.

### Changed

- **Jackett's own error text** is shown in the indexer probe, the hunt log and
  search errors ("eztv: Challenge detected but FlareSolverr is not
  configured") instead of "400 Client Error: Bad Request".
- **The indexer probe allows for FlareSolverr**: 60s per indexer, two at a
  time, and it runs in the background (the UI polls) — a full probe can
  outlast a reverse proxy's request timeout.
- **Housekeeping each tick**: expired TMDb cache rows are pruned, the
  database is VACUUMed (at most daily) when mostly free pages,
  `events.jsonl` rotates at 5 MiB, and Activity reads only the end of it.
- **The sweep logs a leftover it can never sort once**, not every tick.
- **Lint is enforced**: ruff (pinned) with a correctness rule set runs on
  `faucet/` and `tests/` and fails CI.
- **Ranking favours live swarms.** Seeders count logarithmically, and a
  release under `HUNT_HEALTHY_SEEDERS` (5) ranks below a healthy release one
  resolution step lower — most backlog grabs were near-dead torrents that
  stalled. Trackers whose recent hunter grabs mostly stalled are ranked down.

### Security

- **Credentials are redacted** from error messages, history, the Settings
  connection test and logs. `requests` errors embed the full URL, so a
  Jackett timeout used to surface `apikey=<key>`; client errors could carry
  `user:password@`. A logging filter masks anything that slips through.
- **Requests take title, year and poster from TMDb**, not from the browser.
  Members could previously put arbitrary text and image URLs in front of an
  admin. The UI only renders `image.tmdb.org` image URLs (an escaped
  `&#39;` inside `url('…')` is decoded back to a quote before CSS parses it).
- **Login throttling is per IP and per (IP, username).** The old per-account
  lock let anyone lock the admin out with 5 wrong passwords, and its
  distinct message confirmed which usernames exist. Throttled attempts get a
  generic 429 with `Retry-After`; the account-wide lock remains only as a
  backstop at 50 failures. Self-registration is limited per IP.
- **Real client IPs behind a proxy.** Forwarding headers are honoured only
  from `TRUSTED_PROXIES` (`auto` = the container's gateway). Sessions record
  the real address, and cookies get `Secure` behind Cloudflare Tunnel —
  before, Caddy's `X-Forwarded-Proto: http` left them without it.
- **`faucet.env` is written atomically with mode 0600** (existing files are
  tightened on start) and values containing a newline are rejected — one
  used to inject arbitrary variables, `SESSION_SECRET` included.
- **`PATCH /api/settings` only writes known keys**, validated; the TMDb key
  is write-only like the other secrets.
- **Security headers** on every response: CSP pinning all origins,
  `frame-ancestors 'none'`, `nosniff`, `Referrer-Policy`.
- **First-run race closed**: concurrent registrations on an empty database
  can no longer all become admin.
- **The connection test no longer sends saved credentials to a new URL**;
  testing a different Jackett/client URL requires re-entering the secret.
- **Members no longer see library file paths** in movie details.

### Fixed

- **Episode lists now refresh.** A show's episodes were read from TMDb once,
  when it was added, so new seasons, new episodes and TBA episodes that later
  got an air date never became wants or reached the calendar. Monitored shows
  are re-read every `SERIES_REFRESH_HOURS` (12; ended shows weekly), at most
  `SERIES_REFRESH_PER_TICK` (5) per tick. Episodes TMDb stops listing are
  dropped with their wants — only for seasons TMDb actually answered for.
- **Ignored / in-library titles are no longer hunted.** Unmonitoring a title
  left its wants in place and the hunter kept grabbing it. Its wants are now
  dropped immediately, every reconcile purges leftovers, and the hunter skips
  wants of unmonitored or paused titles.
- **Indexer load is bounded.** Every want was searched on every tick until the
  grab cap was hit — hundreds of queries per cycle for a backlog with no
  releases. Fruitless searches now back off exponentially (capped at 6h for
  recent episodes, 72h otherwise), each tick makes at most
  `HUNT_MAX_SEARCHES` (40) queries, and the newest episodes are searched
  first. A manual per-show hunt ignores the backoff. Indexer errors don't
  count as a miss.
- **One cycle at a time.** The scheduler tick and the "check now" button could
  run concurrently and grab two releases for the same want; a second caller is
  now refused.
- **Multi-episode files count for every episode** they contain
  (`S01E01E02`, `S01E03-E04`); only the first was recorded, so the rest were
  hunted forever. A multi-episode file is never retired as superseded.
- **Stuck grabs are re-queued** after `GRAB_RETRY_HOURS` even when reconcile
  doesn't revisit the episode.
- **Telegram notifications** use escaped HTML instead of Markdown, which
  rejected most release names (`_`, `*`) with HTTP 400; ntfy titles go in a
  query parameter (headers are latin-1). Notification HTTP errors are now
  logged instead of silently ignored.

## 1.5.0

### Added

- **Movie quality upgrades.** The scanner records each movie file's source
  (cam-family rips detected from the name) and keeps the best file per
  movie. Reconcile flags owned movies for upgrade when the file is a
  CAM/TS/telesync or below the profile's target resolution; the hunter
  grabs a better copy (per-movie, never packs). Movie cards show an
  "upgrading" chip; the detail page marks cam files. Honors
  `HUNT_UPGRADES=0`.
- **Discover page.** TMDb trending / popular / upcoming poster walls
  (6-hour cache). Titles already monitored are badged and deep-link to
  their detail page; everything else is one click to add (admin) or
  request (users).
- **Calendar.** Every episode airing across monitored shows — past week
  through the next two — grouped by day with on-disk / grabbed / missing /
  upcoming status. Click through to the show. Readable by all users.
- **Season-pack interactive search.** The hourglass on season headers:
  verified packs for that exact season, profile-ranked, one-click grab
  that flips the whole season's wants.
- **Discord notifications are rich embeds** with the poster as thumbnail;
  add `added` to `NOTIFY_ON` to get grab notifications from the hunter.
- **The catch-up sweep runs automatically** each scheduler tick
  (`SWEEP_ENABLED=0` to opt out) — no more manual `docker exec` cron.
- README refreshed with new-UI screenshots and features.

## 1.4.0

### Added

- **Per-episode interactive search** (the Sonarr hourglass). Every aired
  episode row in show detail has a search button; it opens a modal listing
  releases for that exact episode — title-verified (no spin-offs), season
  packs excluded, and **episode-number-verified** (a search for S03E02 can
  never show or grab an S03E07 release; multi-episode releases like
  S01E01-03 count for each episode they cover). Ranked by the show's
  profile with profile-failing releases listed but marked, and a one-click
  **grab** that also flips the wanted row and blocklists the release so the
  background hunter won't double-grab.
- New endpoints: `GET /api/series/{id}/episodes/{s}/{e}/releases` and
  `POST .../grab` (admin-only).

### Fixed

- The background hunter now also verifies the episode NUMBER on every
  candidate release, not just the show title.

## 1.3.1

### Fixed

- **Hunts double-check the disk at grab time.** The wanted table can hold
  stale rows (a want flipped back by the stall handler, or rows created while
  the NAS mount hiccuped) — episodes/movies already on disk were re-grabbed
  from them. Every episode and movie want is now verified against the live
  library inventory immediately before searching; stale wants are deleted,
  not downloaded. Applies to the season-pack pre-pass too.
- **Prune safety valve.** On a CIFS/NFS mount, one sick scan could make a
  whole subtree "vanish", prune its rows, and mark owned episodes missing.
  If more than `PRUNE_MAX_FRACTION` (default 20%) of known files look gone
  at once, pruning is skipped for that scan and a warning is logged.

## 1.3.0

### Changed — full UI overhaul (poster-first)

- **Shows and Movies are now poster grids** — TMDb art with completion bars
  along the card edge, status chips (monitored/complete/paused · in
  library/wanted), hover quick-hunt, plus filter, status and sort controls.
  Titles without art get a deterministic gradient placeholder.
- **Detail pages got a cinematic hero**: full-width TMDb backdrop (fetched at
  w1280 now instead of w342), big poster, facts row (year, seasons, rating,
  genres), overview, and all management controls inline. Below: a completion
  strip and per-season accordions with episode rows (on disk / missing /
  unaired pills, detected quality, air dates). Newest season first; the first
  incomplete season starts open. Remove moved into the detail hero.
- **Dashboard rebuilt**: stat tiles (storage bar, services, throughput,
  library, wanted, members), download rows with progress + inline stall
  warning, recent history, and two poster rails — "Hunting next" and
  "Recently added shows".
- Nav polish: avatar button with your initial, brand click goes home, and
  `/` focuses search from anywhere.
- All functionality preserved: search (title + release modes), requests,
  users, settings, profiles, auto-grab, activity drawer, file browser, fix
  match/location, mobile bottom nav + sheet, light/dark theme.

## 1.2.1

### Fixed

- **Season packs re-downloaded seasons already on disk.** The pack pre-pass
  fired for ANY season with 2+ wanted episodes — including quality-*upgrade*
  wants — without checking ownership. A 720p library under a 1080p profile
  re-downloaded entire owned seasons (e.g. a 14 GB S04 pack for 2 missing
  episodes). Packs now require the season to be ≥90% missing, and upgrade
  wants only ever hunt per-episode.
- New env `HUNT_UPGRADES=0` disables quality-upgrade hunting entirely
  (missing episodes still hunt).

## 1.2.0

### Added

- **Stalled-download handling** (`faucet/stalls.py`). Every scheduler tick
  snapshots each downloading transfer's progress. A transfer with zero
  progress for `STALL_HOURS` (default 4) is removed from the client with its
  partial data, its release stays blocklisted in the grabbed table, and every
  want it was grabbed for flips straight back to `wanted` — the same tick's
  hunt then grabs a different release. Season packs re-queue the whole
  grabbed season. `STALL_ACTION=flag` logs/notifies without acting. Stall
  events appear in history as `stalled` and notify when `failed` is in
  `NOTIFY_ON`. Seeding/queued/checking transfers are never touched.

## 1.1.0

### Fixed

- **Hunting could grab the wrong show.** Indexer search is substring-based, so
  hunting *Trailer Park Boys* also returned *Trailer Park Boys: Out of the
  Park*, *…The Animated Series*, and every other spin-off — and the
  best-seeded one won. A new release-title verifier (`faucet/releasematch.py`)
  now requires the release's title portion to equal the monitored title
  (watermarks, trailing years, and country tags tolerated; supersets
  rejected). Applied to episode hunts, season-pack hunts, and movie hunts.
- **Season packs must now match the exact season.** A pack whose season number
  couldn't be parsed was previously accepted for *any* season.
- **Deleted files no longer haunt the library.** The scanner prunes DB rows
  whose files vanished from disk, so removed episodes/movies get re-hunted
  instead of being counted as owned forever.
- **Failed grabs retry.** A want stuck in `grabbed` with nothing on disk flips
  back to `wanted` after `GRAB_RETRY_HOURS` (default 48) instead of orphaning
  the episode permanently.
- **TBA episodes aren't hunted.** Episodes with no air date were treated as
  aired and searched forever.
- **Wanted-table duplicates.** Wants are now keyed on (series, season,
  episode) — TMDb retitling an episode used to duplicate the want and hunt it
  twice. Existing duplicates are collapsed on startup.
- **Language detection false positives.** *It (2017)* was detected as Italian
  (the `it` token) and filtered out of English profiles; dual-audio `ITA ENG`
  releases were treated as foreign. Short language tags now require an
  uppercase tag positioned after the title, and dual-audio with English
  passes.
- **Movie matching.** `have_movie` compares normalized titles (punctuation/
  case-insensitive) with year fuzz; the importer distinguishes same-title
  remakes (*Dune* 1984 vs 2021) and no longer blind-links the top TMDb result
  when nothing actually matches the folder name.
- **Cam releases are rejected** (CAM/TS/TC/screener) for profile-driven
  automatic grabs unless a profile explicitly lists `CAM`. A bare `TS` token
  is no longer mistaken for a `.ts` file extension.
- **Sorter robustness.** One unparseable file no longer aborts sorting the
  rest of a release; multi-season range names can't crash the planner; leftover
  cleanup never deletes folders still holding subtitle files.
- **Re-adding a title no longer wipes its quality profile** when the new add
  carries none (e.g. approving a user request).

### Security

- **First-run lockdown.** With zero users in the DB, only the login/register
  surface is reachable. Previously an empty (or wiped/unmounted) database left
  the *entire* admin API open, unauthenticated.
- **CSRF protection is now app-wide** for every state-changing API call
  (`/api/add`, torrent actions, settings, profiles, library ops…), not just
  the auth router.
- **`/api/settings` no longer leaks the session-signing secret** or ships the
  entire TMDb response cache to the browser.
- **`/health` is minimal for anonymous callers**; internal reachability detail
  (hostnames, error strings) is admin-only.
- Expired sessions and used reset tokens are purged on each scheduler tick.
- `pyproject.toml` now declares `bcrypt` and `itsdangerous` (a pip install of
  the package previously produced a broken auth stack).

### Changed

- `/api/series` computes have/total/wanted in one pass instead of re-reading
  the whole library inventory per show.
- TMDb years are returned as integers.
- New env: `GRAB_RETRY_HOURS` (see `.env.example`).

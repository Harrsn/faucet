# Changelog

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

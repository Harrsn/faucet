# Changelog

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

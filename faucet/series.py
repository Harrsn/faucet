"""Monitored series + reconciliation (Layers 2-3).

A "monitored series" is a show Faucet tracks end to end: it pulls the canonical
episode list from TMDb (what *should* exist), the library scanner records what's
on disk (what *does* exist), and reconcile() diffs them to populate the `wanted`
table with missing episodes — and, when a quality profile is set, episodes held
below the profile's preferred resolution (upgrades).

The hunter (scheduler) then targets the wanted set with episode-specific
searches. This module owns series CRUD, episode-list refresh, and the diff.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from . import db
from . import tmdb
from . import library

log = logging.getLogger("faucet.series")

# resolution rank for upgrade comparison (higher = better)
_RES_RANK = {"2160p": 4, "1080p": 3, "720p": 2, "480p": 1, None: 0, "": 0}

# A want marked 'grabbed' whose download never materialized (client error,
# dead torrent, stalled seed) is retried after this many hours — otherwise a
# single failed grab orphans the episode forever.
GRAB_RETRY_HOURS = int(os.environ.get("GRAB_RETRY_HOURS", "48"))


def air_delay_days() -> int:
    """AIR_DELAY_DAYS: whole days after an episode's air date before it is
    hunted (default 1). TMDb only gives dates, so 0 means 'from the air date'
    — which races the broadcast and invites fake early releases."""
    try:
        return max(0, int(os.environ.get("AIR_DELAY_DAYS", "1")))
    except ValueError:
        return 1


def hunt_cutoff() -> str:
    """Latest air date (YYYY-MM-DD) that is eligible for hunting today."""
    from datetime import timedelta
    return (datetime.now().date() - timedelta(days=air_delay_days())).isoformat()


def hunt_eligible(air_date: str | None) -> bool:
    return bool(air_date) and air_date <= hunt_cutoff()


def add_series(tmdb_id: int, title: str, year: int | None, poster: str | None,
               profile_id: int | None = None) -> int:
    """Start monitoring a series. Pulls its episode list immediately."""
    db.init()
    details = tmdb.details(tmdb_id, "tv")
    total_seasons = details.get("seasons") or 0
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO series (tmdb_id, title, year, poster, profile_id, monitored, "
            "total_seasons, added_ts, last_refresh) VALUES (?,?,?,?,?,1,?,?,?) "
            # re-adding an existing show must not WIPE its assigned profile when
            # the caller passes none (e.g. a request approval with no profile)
            "ON CONFLICT(tmdb_id) DO UPDATE SET monitored=1, "
            "profile_id=COALESCE(excluded.profile_id, series.profile_id)",
            (tmdb_id, title, year, poster, profile_id, total_seasons, now, now))
        sid = cur.lastrowid
        if not sid:  # conflict path — fetch existing id
            sid = c.execute("SELECT id FROM series WHERE tmdb_id=?", (tmdb_id,)).fetchone()["id"]
    refresh_episodes(sid)
    return sid


def refresh_episodes(series_id: int, fresh: bool = False) -> int:
    """Pull the canonical episode list from TMDb into series_episodes.

    With `fresh` (the scheduled refresh) TMDb is re-read rather than served
    from cache, the season count is re-read too — a show that gains a season
    after it was added would otherwise never list it — and, for every season
    TMDb actually answered for, episodes it no longer lists are dropped along
    with their wants (TMDb renumbers and deletes placeholder episodes; a
    stale row is a want that can never be satisfied).

    A partial read (some season unreadable) is not recorded as a full
    refresh: the show comes due again in about an hour.
    Returns the number of episodes upserted."""
    s = get_series(series_id)
    if not s:
        return 0
    total = s.get("total_seasons") or 0
    status = None
    if fresh:
        d = tmdb.details(s["tmdb_id"], "tv", fresh=True)
        status = d.get("status")
        if d.get("seasons"):
            total = int(d["seasons"])
    n = 0
    complete = True
    if fresh:
        per_season = ((no, tmdb.season_episodes(s["tmdb_id"], no, fresh=True))
                      for no in range(1, total + 1))
    else:
        # first pull (add / Fix Match): cached reads are fine
        per_season = [(None, tmdb.episodes(s["tmdb_id"], total))]
    for season_no, eps in per_season:
        if eps is None:
            complete = False
            continue
        listed = set()
        with db.connect() as c:
            for e in eps:
                if e.get("episode") is None:
                    continue
                listed.add((int(e["season"]), int(e["episode"])))
                c.execute(
                    "INSERT INTO series_episodes (series_id, season, episode, title, air_date) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(series_id, season, episode) DO UPDATE SET "
                    "title=excluded.title, air_date=excluded.air_date",
                    (series_id, e["season"], e["episode"], e.get("title", ""),
                     e.get("air_date", "")))
                n += 1
            if season_no is not None and listed:
                # an empty answer is never trusted for pruning
                for r in c.execute("SELECT episode FROM series_episodes "
                                   "WHERE series_id=? AND season=?",
                                   (series_id, season_no)).fetchall():
                    if (season_no, r["episode"]) in listed:
                        continue
                    c.execute("DELETE FROM series_episodes WHERE series_id=? "
                              "AND season=? AND episode=?",
                              (series_id, season_no, r["episode"]))
                    c.execute("DELETE FROM wanted WHERE kind='episode' AND series_id=? "
                              "AND season=? AND episode=?",
                              (series_id, season_no, r["episode"]))
                    log.info("%s S%02dE%02d no longer listed by TMDb; dropped",
                             s["title"], season_no, r["episode"])
    with db.connect() as c:
        if total != (s.get("total_seasons") or 0):
            log.info("%s: TMDb now lists %d season(s) (was %d)",
                     s["title"], total, s.get("total_seasons") or 0)
            c.execute("UPDATE series SET total_seasons=? WHERE id=?", (total, series_id))
        stamp = datetime.now()
        if not complete:
            # partial read: due again in about an hour rather than on every
            # tick (a season TMDb never serves would otherwise hog a slot)
            from datetime import timedelta
            stamp -= timedelta(hours=max(0.0, refresh_interval_hours(status) - 1))
        c.execute("UPDATE series SET last_refresh=? WHERE id=?",
                  (stamp.isoformat(timespec="seconds"), series_id))
    return n


def refresh_interval_hours(tmdb_status: str | None) -> float:
    """How stale a show's episode list may get. Airing shows gain episodes
    and air dates weekly; ended ones almost never change."""
    try:
        active = float(os.environ.get("SERIES_REFRESH_HOURS", "12"))
    except ValueError:
        active = 12.0
    if (tmdb_status or "").lower() in ("ended", "canceled", "cancelled"):
        return max(active, 24 * 7.0)
    return active


def refresh_stale(limit: int | None = None) -> dict:
    """Re-pull episode lists for monitored shows whose list is older than
    their refresh interval, stalest first, at most `limit` per call
    (SERIES_REFRESH_PER_TICK, default 5) to keep TMDb traffic bounded.

    Before this existed, a show's episode list was fetched once — when it was
    added — so every episode TMDb announced afterwards (new seasons, newly
    dated TBA episodes) was invisible to reconcile, the hunter and the
    calendar."""
    if limit is None:
        try:
            limit = int(os.environ.get("SERIES_REFRESH_PER_TICK", "5"))
        except ValueError:
            limit = 5
    out = {"refreshed": 0, "checked": 0}
    if limit <= 0 or not tmdb.enabled():
        return out
    now = datetime.now()
    due = []
    for s in list_series():
        if not s.get("monitored") or (s.get("monitor_mode") or "all") == "none":
            continue
        out["checked"] += 1
        status = (tmdb.details(s["tmdb_id"], "tv") or {}).get("status")
        try:
            last = datetime.fromisoformat(s["last_refresh"]) if s.get("last_refresh") else None
        except ValueError:
            last = None
        age_h = (now - last).total_seconds() / 3600 if last else float("inf")
        if age_h >= refresh_interval_hours(status):
            due.append((age_h, s))
    due.sort(key=lambda x: x[0], reverse=True)
    for _, s in due[:limit]:
        try:
            refresh_episodes(s["id"], fresh=True)
            out["refreshed"] += 1
        except Exception as e:                   # noqa: BLE001 - one bad show can't stop the rest
            log.warning("episode refresh failed for %s: %s", s["title"], e)
    return out


def get_series(series_id: int) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    return dict(r) if r else None


def list_series() -> list[dict]:
    with db.connect() as c:
        rows = c.execute("SELECT * FROM series ORDER BY title").fetchall()
    return [dict(r) for r in rows]


def delete_series(series_id: int) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM series WHERE id=?", (series_id,))
        c.execute("DELETE FROM series_episodes WHERE series_id=?", (series_id,))
        c.execute("DELETE FROM wanted WHERE series_id=?", (series_id,))


def _profile_min_res(profile_id: int | None) -> str | None:
    """The most-preferred resolution in a profile (top of its list), used as the
    upgrade target. None if no profile / no resolutions."""
    if not profile_id:
        return None
    import json
    with db.connect() as c:
        r = c.execute("SELECT resolutions FROM profiles WHERE id=?", (profile_id,)).fetchone()
    if not r:
        return None
    res = json.loads(r["resolutions"] or "[]")
    return res[0] if res else None


def reconcile(series_id: int) -> dict:
    """Diff canonical episodes vs. library for one series; populate `wanted`.
    Respects the series monitor_mode: 'none' skips entirely, 'future' only wants
    episodes that aired on/after the day the show was added (no backlog hunting),
    'all' wants every missing aired episode. Returns {missing, upgrades, have}."""
    s = get_series(series_id)
    if not s:
        return {"missing": 0, "upgrades": 0, "have": 0}
    mode = s.get("monitor_mode") or "all"
    if mode == "none":
        # clear any outstanding wants for a paused show
        with db.connect() as c:
            c.execute("DELETE FROM wanted WHERE series_id=? AND status='wanted'", (series_id,))
        return {"missing": 0, "upgrades": 0, "have": 0, "paused": True}
    # 'future' mode: only want episodes airing on/after the add date
    cutoff = None
    if mode == "future" and s.get("added_ts"):
        cutoff = s["added_ts"][:10]               # YYYY-MM-DD

    target_res = _profile_min_res(s.get("profile_id"))
    target_rank = _RES_RANK.get(target_res, 0)
    # HUNT_UPGRADES=0 disables quality-upgrade hunting entirely (missing
    # episodes still hunt) — for libraries where re-downloading owned content
    # at higher quality is unwanted.
    if os.environ.get("HUNT_UPGRADES", "1").strip().lower() in ("0", "false", "no", "off"):
        target_rank = 0

    with db.connect() as c:
        canonical = c.execute(
            "SELECT season, episode, title, air_date FROM series_episodes WHERE series_id=?",
            (series_id,)).fetchall()

    missing = upgrades = have = 0
    for ep in canonical:
        season, episode = ep["season"], ep["episode"]
        # skip episodes that haven't aired (or aired too recently: real
        # releases lag the broadcast and early "releases" are fakes) —
        # including ones with NO air date (TBA / unannounced), which would
        # search forever for releases that can't exist
        if not hunt_eligible(ep["air_date"]):
            continue
        # 'future' mode: skip episodes that aired before the show was added
        if cutoff and ep["air_date"] and ep["air_date"] < cutoff:
            # never want backlog episodes; clear any stale want and count owned
            _clear_wanted(series_id, season, episode)
            if library.have_episode(s["title"], season, episode):
                have += 1
            continue
        # season 0 is "specials" — skip from missing-hunting by default
        if season == 0:
            continue
        owned = library.have_episode(s["title"], season, episode)
        if not owned:
            _add_wanted(series_id, season, episode, ep["title"], "missing")
            missing += 1
        else:
            have += 1
            # Upgrade check: only when we KNOW the owned quality and it's below
            # the profile target. Unknown quality (None) is left alone — we won't
            # re-grab an episode just because its resolution couldn't be detected.
            owned_q = owned.get("quality")
            if target_rank and owned_q and _RES_RANK.get(owned_q, 0) < target_rank:
                _add_wanted(series_id, season, episode, ep["title"], "upgrade")
                upgrades += 1
            else:
                # satisfied at (or above) target — retire any want, including a
                # stale 'grabbed' row (those used to linger forever)
                _clear_wanted(series_id, season, episode, any_status=True)
    return {"missing": missing, "upgrades": upgrades, "have": have}


def _add_wanted(series_id: int, season: int, episode: int, title: str, reason: str) -> None:
    """Upsert one episode want keyed on (series, season, episode) — NOT on the
    episode title, which TMDb can rename between refreshes. A row stuck in
    'grabbed' with nothing on disk is flipped back to 'wanted' after
    GRAB_RETRY_HOURS so a failed download eventually retries."""
    from . import wants
    wants.upsert("episode", series_id, season, episode, title, reason, GRAB_RETRY_HOURS)


def _clear_wanted(series_id: int, season: int, episode: int,
                  any_status: bool = False) -> None:
    q = ("DELETE FROM wanted WHERE kind='episode' AND series_id=? "
         "AND season=? AND episode=?")
    if not any_status:
        q += " AND status='wanted'"
    with db.connect() as c:
        c.execute(q, (series_id, season, episode))


def purge_unmonitored_wants() -> int:
    """Drop wants (any status) for shows and movies that are no longer
    monitored or are paused. Ignoring a title sets monitored=0 but used to
    leave its wants behind, and since reconcile skips unmonitored titles
    nothing ever cleared them — the hunter kept downloading ignored shows."""
    with db.connect() as c:
        n = c.execute(
            "DELETE FROM wanted WHERE kind='episode' AND series_id IN ("
            "  SELECT id FROM series WHERE monitored=0 OR monitor_mode='none')").rowcount
        n += c.execute(
            "DELETE FROM wanted WHERE kind='movie' AND series_id IN ("
            "  SELECT id FROM movies WHERE monitored=0)").rowcount
    if n:
        log.info("Dropped %d want(s) for unmonitored titles", n)
    return n


def reconcile_all() -> dict:
    total = {"missing": 0, "upgrades": 0, "have": 0, "series": 0}
    purge_unmonitored_wants()
    for s in list_series():
        if not s.get("monitored"):
            continue
        r = reconcile(s["id"])
        total["missing"] += r["missing"]
        total["upgrades"] += r["upgrades"]
        total["have"] += r["have"]
        total["series"] += 1
    return total


def list_wanted(status: str = "wanted") -> list[dict]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT w.*, s.title AS series_title FROM wanted w "
            "LEFT JOIN series s ON s.id=w.series_id WHERE w.status=? ORDER BY w.id",
            (status,)).fetchall()
    return [dict(r) for r in rows]


def episode_breakdown(series_id: int) -> dict:
    """Full season/episode view for the detail page: every canonical episode
    with whether it's owned (and at what quality). Grouped by season."""
    from . import library
    s = get_series(series_id)
    if not s:
        return {"series": None, "seasons": []}
    with db.connect() as c:
        canon = c.execute(
            "SELECT season, episode, title, air_date FROM series_episodes "
            "WHERE series_id=? ORDER BY season, episode", (series_id,)).fetchall()
    from datetime import datetime
    today = datetime.now().date().isoformat()
    seasons = {}
    have = total = 0
    for ep in canon:
        if ep["season"] == 0:
            continue  # specials shown separately if ever needed
        owned = library.have_episode(s["title"], ep["season"], ep["episode"])
        # no air date = TBA/unannounced -> not aired (matches reconcile())
        aired = bool(ep["air_date"]) and ep["air_date"] <= today
        if aired:
            total += 1
            if owned:
                have += 1
        seasons.setdefault(ep["season"], []).append({
            "episode": ep["episode"], "title": ep["title"], "air_date": ep["air_date"],
            "have": bool(owned), "quality": owned.get("quality") if owned else None,
            "aired": aired,
        })
    season_list = [{"season": k, "episodes": v,
                    "have": sum(1 for e in v if e["have"]),
                    "total": sum(1 for e in v if e["aired"])}
                   for k, v in sorted(seasons.items())]
    return {"series": s, "seasons": season_list, "have": have, "total": total}


def hunt_series(series_id: int, max_grabs: int = 3) -> dict:
    """Reconcile then hunt wanted episodes for ONE series, respecting a cap."""
    from . import scheduler
    reconcile(series_id)
    return scheduler.hunt_wanted(series_filter=series_id, max_override=max_grabs)


def set_monitor_mode(series_id: int, mode: str) -> None:
    """Set a show's monitor mode: all | future | none."""
    if mode not in ("all", "future", "none"):
        mode = "all"
    with db.connect() as c:
        c.execute("UPDATE series SET monitor_mode=? WHERE id=?", (mode, series_id))
    reconcile(series_id)

"""Monitored series + reconciliation (Layers 2-3).

A "monitored series" is a show Cascade tracks end to end: it pulls the canonical
episode list from TMDb (what *should* exist), the library scanner records what's
on disk (what *does* exist), and reconcile() diffs them to populate the `wanted`
table with missing episodes — and, when a quality profile is set, episodes held
below the profile's preferred resolution (upgrades).

The hunter (scheduler) then targets the wanted set with episode-specific
searches. This module owns series CRUD, episode-list refresh, and the diff.
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import db
from . import tmdb
from . import library

log = logging.getLogger("cascade.series")

# resolution rank for upgrade comparison (higher = better)
_RES_RANK = {"2160p": 4, "1080p": 3, "720p": 2, "480p": 1, None: 0, "": 0}


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
            "ON CONFLICT(tmdb_id) DO UPDATE SET monitored=1, profile_id=excluded.profile_id",
            (tmdb_id, title, year, poster, profile_id, total_seasons, now, now))
        sid = cur.lastrowid
        if not sid:  # conflict path — fetch existing id
            sid = c.execute("SELECT id FROM series WHERE tmdb_id=?", (tmdb_id,)).fetchone()["id"]
    refresh_episodes(sid)
    return sid


def refresh_episodes(series_id: int) -> int:
    """Pull the canonical episode list from TMDb into series_episodes."""
    s = get_series(series_id)
    if not s:
        return 0
    eps = tmdb.episodes(s["tmdb_id"], s.get("total_seasons") or 0)
    n = 0
    with db.connect() as c:
        for e in eps:
            if e.get("episode") is None:
                continue
            c.execute(
                "INSERT INTO series_episodes (series_id, season, episode, title, air_date) "
                "VALUES (?,?,?,?,?) ON CONFLICT(series_id, season, episode) DO UPDATE SET "
                "title=excluded.title, air_date=excluded.air_date",
                (series_id, e["season"], e["episode"], e.get("title", ""), e.get("air_date", "")))
            n += 1
        c.execute("UPDATE series SET last_refresh=? WHERE id=?",
                  (datetime.now().isoformat(timespec="seconds"), series_id))
    return n


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
    Returns {missing, upgrades, have}."""
    s = get_series(series_id)
    if not s:
        return {"missing": 0, "upgrades": 0, "have": 0}
    target_res = _profile_min_res(s.get("profile_id"))
    target_rank = _RES_RANK.get(target_res, 0)

    with db.connect() as c:
        canonical = c.execute(
            "SELECT season, episode, title, air_date FROM series_episodes WHERE series_id=?",
            (series_id,)).fetchall()

    missing = upgrades = have = 0
    today = datetime.now().date().isoformat()
    for ep in canonical:
        season, episode = ep["season"], ep["episode"]
        # skip episodes that haven't aired yet (no point hunting them)
        if ep["air_date"] and ep["air_date"] > today:
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
                _clear_wanted(series_id, season, episode)
    return {"missing": missing, "upgrades": upgrades, "have": have}


def _add_wanted(series_id: int, season: int, episode: int, title: str, reason: str) -> None:
    with db.connect() as c:
        c.execute(
            "INSERT INTO wanted (kind, series_id, season, episode, title, reason, status) "
            "VALUES ('episode',?,?,?,?,?,'wanted') "
            "ON CONFLICT(kind, series_id, season, episode, title) DO UPDATE SET "
            "reason=excluded.reason WHERE wanted.status='wanted'",
            (series_id, season, episode, title or "", reason))


def _clear_wanted(series_id: int, season: int, episode: int) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM wanted WHERE kind='episode' AND series_id=? "
                  "AND season=? AND episode=? AND status='wanted'",
                  (series_id, season, episode))


def reconcile_all() -> dict:
    total = {"missing": 0, "upgrades": 0, "have": 0, "series": 0}
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
        aired = not ep["air_date"] or ep["air_date"] <= today
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

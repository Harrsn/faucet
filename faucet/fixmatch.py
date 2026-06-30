"""Fix Match — admin overrides for auto-matched library titles.

Auto-import is a best guess. This module lets an admin correct it, Sonarr/Radarr
style:

  * set_status   — flip a title between monitored / in_library / ignored
  * fix_match    — re-link a show/movie to a *different* TMDb entry (wrong auto
                   match, or a featurette that matched a real film), wiping the
                   old canonical episode/wanted rows and re-reconciling against
                   the new entry immediately
  * search       — TMDb search with poster/year/overview previews for the picker

'kind' is always "show" or "movie".
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import db, tmdb, library
from . import series as series_mod
from . import movies as movies_mod

log = logging.getLogger("faucet.fixmatch")

VALID_STATUS = {"monitored", "in_library", "ignored"}


# ── status ───────────────────────────────────────────────────────────────────
def set_status(kind: str, item_id: int, status: str) -> dict:
    """Change a title's library status. 'monitored' actively hunts missing;
    'in_library' means we have it and shouldn't hunt; 'ignored' means imported
    but unwanted (no hunting, excluded from missing counts)."""
    if status not in VALID_STATUS:
        return {"error": f"invalid status '{status}'"}
    table = "series" if kind == "show" else "movies"
    # monitored flag stays in sync so existing hunt logic (which reads
    # `monitored`) skips anything not actively monitored.
    monitored = 1 if status == "monitored" else 0
    with db.connect() as c:
        row = c.execute(f"SELECT id FROM {table} WHERE id=?", (item_id,)).fetchone()
        if not row:
            return {"error": "not found"}
        c.execute(f"UPDATE {table} SET lib_status=?, monitored=? WHERE id=?",
                  (status, monitored, item_id))
        # Movies carry a separate have/wanted `status` that drives the UI pill.
        # Marking 'in_library' asserts the file exists -> have. Other statuses
        # leave the on-disk determination to reconcile(), which sets it from the
        # actual library scan.
        if kind == "movie" and status == "in_library":
            c.execute("UPDATE movies SET status='have' WHERE id=?", (item_id,))
    log.info("set %s %s status -> %s", kind, item_id, status)
    return {"ok": True, "kind": kind, "id": item_id, "status": status}


# ── search preview ───────────────────────────────────────────────────────────
def search(query: str, kind: str, limit: int = 8) -> list[dict]:
    """TMDb candidates for the Fix Match picker: id, title, year, poster,
    overview. kind 'show' -> tv search, 'movie' -> movie search."""
    tmdb_kind = "tv" if kind == "show" else "movie"
    if not tmdb.enabled():
        return []
    results = tmdb.search(query, tmdb_kind) or []
    out = []
    for r in results[:limit]:
        out.append({
            "tmdb_id": r.get("tmdb_id"),
            "title": r.get("title"),
            "year": r.get("year"),
            "poster": r.get("poster"),
            "overview": (r.get("overview") or "")[:300],
        })
    return out


# ── fix match (re-link) ──────────────────────────────────────────────────────
def fix_match(kind: str, item_id: int, new_tmdb_id: int) -> dict:
    """Re-link an existing monitored title to a different TMDb entry. Pulls the
    new entry's metadata, updates the row, clears stale canonical/wanted data,
    and re-reconciles against the library immediately so have/missing counts are
    correct right away."""
    if kind == "show":
        return _fix_show(item_id, new_tmdb_id)
    return _fix_movie(item_id, new_tmdb_id)


def _fix_show(series_id: int, new_tmdb_id: int) -> dict:
    s = series_mod.get_series(series_id)
    if not s:
        return {"error": "series not found"}
    details = tmdb.details(new_tmdb_id, "tv")
    if not details:
        return {"error": "couldn't fetch new TMDb entry"}

    new_title = details.get("title") or s["title"]
    new_year = details.get("year")
    new_poster = details.get("poster")
    total_seasons = details.get("seasons") or 0
    now = datetime.now().isoformat(timespec="seconds")

    with db.connect() as c:
        # guard: another row may already hold the target tmdb_id (UNIQUE)
        clash = c.execute("SELECT id FROM series WHERE tmdb_id=? AND id<>?",
                          (new_tmdb_id, series_id)).fetchone()
        if clash:
            return {"error": "another show is already linked to that TMDb id"}
        # wipe stale canonical episodes + wanted rows for this series
        c.execute("DELETE FROM series_episodes WHERE series_id=?", (series_id,))
        c.execute("DELETE FROM wanted WHERE series_id=?", (series_id,))
        c.execute(
            "UPDATE series SET tmdb_id=?, title=?, year=?, poster=?, "
            "total_seasons=?, last_refresh=?, lib_status='monitored', monitored=1 "
            "WHERE id=?",
            (new_tmdb_id, new_title, new_year, new_poster, total_seasons, now, series_id))

    series_mod.refresh_episodes(series_id)   # pull new canonical list
    rec = series_mod.reconcile(series_id)    # re-match library files + counts
    log.info("fixed show %s -> tmdb %s (%s)", series_id, new_tmdb_id, new_title)
    return {"ok": True, "kind": "show", "id": series_id,
            "tmdb_id": new_tmdb_id, "title": new_title, "year": new_year,
            "poster": new_poster, "reconcile": rec}


def _fix_movie(movie_id: int, new_tmdb_id: int) -> dict:
    with db.connect() as c:
        row = c.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
        if not row:
            return {"error": "movie not found"}
        clash = c.execute("SELECT id FROM movies WHERE tmdb_id=? AND id<>?",
                          (new_tmdb_id, movie_id)).fetchone()
        if clash:
            return {"error": "another movie is already linked to that TMDb id"}
    details = tmdb.details(new_tmdb_id, "movie")
    if not details:
        return {"error": "couldn't fetch new TMDb entry"}
    new_title = details.get("title") or row["title"]
    new_year = details.get("year")
    new_poster = details.get("poster")
    with db.connect() as c:
        c.execute(
            "UPDATE movies SET tmdb_id=?, title=?, year=?, poster=?, "
            "lib_status='monitored', monitored=1 WHERE id=?",
            (new_tmdb_id, new_title, new_year, new_poster, movie_id))
    rec = movies_mod.reconcile(movie_id)     # re-check library for the file
    log.info("fixed movie %s -> tmdb %s (%s)", movie_id, new_tmdb_id, new_title)
    return {"ok": True, "kind": "movie", "id": movie_id,
            "tmdb_id": new_tmdb_id, "title": new_title, "year": new_year,
            "poster": new_poster, "reconcile": rec}

"""Monitored movies (Radarr-side).

Mirrors the series module for movies, but movies are simpler — there's no
episode list, just "do we have this movie or not." Add a movie (from TMDb),
the library scanner records movies on disk, and reconcile() marks each monitored
movie as have/wanted by matching normalized title + year. The hunter searches
for wanted movies and grabs the best release per profile.
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import db
from . import tmdb
from . import library

log = logging.getLogger("faucet.movies")


def add_movie(tmdb_id: int, title: str, year: int | None, poster: str | None,
              profile_id: int | None = None) -> int:
    db.init()
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO movies (tmdb_id, title, year, poster, profile_id, monitored, added_ts) "
            "VALUES (?,?,?,?,?,1,?) "
            "ON CONFLICT(tmdb_id) DO UPDATE SET monitored=1, profile_id=excluded.profile_id",
            (tmdb_id, title, year, poster, profile_id, now))
        mid = cur.lastrowid
        if not mid:
            mid = c.execute("SELECT id FROM movies WHERE tmdb_id=?", (tmdb_id,)).fetchone()["id"]
    reconcile(mid)
    return mid


def get_movie(movie_id: int) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
    return dict(r) if r else None


def list_movies() -> list[dict]:
    with db.connect() as c:
        rows = c.execute("SELECT * FROM movies ORDER BY title").fetchall()
    return [dict(r) for r in rows]


def delete_movie(movie_id: int) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM movies WHERE id=?", (movie_id,))
        c.execute("DELETE FROM wanted WHERE kind='movie' AND series_id=?", (movie_id,))


def _movie_matches(lib_title: str, lib_year, mon_title: str, mon_year) -> bool:
    """Does a library movie match a monitored movie? Handles the common case
    where the disk folder is a truncated/abbreviated form of TMDb's canonical
    title ('The Chronicles of Narnia' folder vs 'The Chronicles of Narnia: The
    Lion, the Witch and the Wardrobe'). Strategy:
      - if years are both known and differ by >1, it's NOT a match (this is what
        disambiguates three Narnia films all foldered as 'The Chronicles of Narnia')
      - then accept exact normalized equality, OR one title being a token-subset
        of the other (prefix/contains), so truncated folders still match.
    """
    lk = library.normalize_title(lib_title)
    mk = library.normalize_title(mon_title)
    if not lk or not mk:
        return False
    if lk == mk:
        # exact title: allow a 1-year metadata fuzz
        if lib_year and mon_year and abs(int(lib_year) - int(mon_year)) > 1:
            return False
        return True
    # Subset/truncated-title match: the year is doing the disambiguation work
    # (e.g. Mockingjay Part 1 2014 vs Part 2 2015 share a truncated folder name),
    # so require EXACT year agreement here. If either year is missing we can't
    # safely disambiguate a subset match, so refuse.
    if not lib_year or not mon_year or int(lib_year) != int(mon_year):
        return False
    a, b = sorted([lk.split(), mk.split()], key=len)  # a = shorter
    if b[:len(a)] == a:                  # contiguous prefix
        return True
    if set(a).issubset(set(b)) and len(a) >= 2:   # all shorter words present
        return True
    return False


def reconcile(movie_id: int) -> dict:
    """Mark a monitored movie have/wanted by matching the library. Uses
    year-anchored subset matching so truncated disk folders still match TMDb's
    full canonical titles. Populates the wanted table for missing movies."""
    m = get_movie(movie_id)
    if not m:
        return {"have": False}
    with db.connect() as c:
        lib = c.execute("SELECT title, year, quality FROM library_movies").fetchall()
    owned = None
    for r in lib:
        if _movie_matches(r["title"], r["year"], m["title"], m.get("year")):
            owned = r
            break
    have = owned is not None
    with db.connect() as c:
        c.execute("UPDATE movies SET status=? WHERE id=?",
                  ("have" if have else "wanted", movie_id))
        if have:
            c.execute("DELETE FROM wanted WHERE kind='movie' AND series_id=? AND status='wanted'",
                      (movie_id,))
        else:
            title = f"{m['title']} {m['year']}" if m.get("year") else m["title"]
            exists = c.execute(
                "SELECT 1 FROM wanted WHERE kind='movie' AND series_id=? AND status='wanted'",
                (movie_id,)).fetchone()
            if not exists:
                c.execute(
                    "INSERT INTO wanted (kind, series_id, title, reason, status) "
                    "VALUES ('movie',?,?, 'missing','wanted')",
                    (movie_id, title))
    return {"have": have}


def reconcile_all() -> dict:
    total = {"have": 0, "wanted": 0}
    for m in list_movies():
        if not m.get("monitored"):
            continue
        r = reconcile(m["id"])
        total["have" if r["have"] else "wanted"] += 1
    return total


def movie_detail(movie_id: int) -> dict:
    """Detail view for a movie: the monitored entry, the on-disk file (if owned,
    with quality/size/path), and TMDb metadata."""
    m = get_movie(movie_id)
    if not m:
        return {"movie": None}
    # find the owned library file via the same matcher reconcile uses
    with db.connect() as c:
        lib = c.execute("SELECT title, year, quality, path, size FROM library_movies").fetchall()
    owned = None
    for r in lib:
        if _movie_matches(r["title"], r["year"], m["title"], m.get("year")):
            owned = dict(r)
            break
    meta = {}
    try:
        meta = tmdb.details(m["tmdb_id"], "movie")
    except Exception:                            # noqa: BLE001
        meta = {}
    return {"movie": m, "file": owned, "meta": meta}

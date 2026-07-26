"""Library auto-import (the Sonarr/Radarr 'import existing library' flow).

The scanner records what's on disk; this module turns that into monitored
entries automatically. It finds every distinct show in /tvshows and movie in
/movies, matches each to TMDb, and creates a monitored series/movie — so a full
existing library populates the Shows and Movies pages without adding each by
hand.

Matching uses TMDb search on the normalized folder name; the top result is
taken when its normalized title agrees. Unmatched titles are reported so the
user can add them manually rather than silently dropped.
"""
from __future__ import annotations

import logging

from . import db
from . import tmdb
from . import library
from . import series as series_mod
from . import movies as movies_mod

log = logging.getLogger("faucet.import")


def _distinct_shows() -> list[str]:
    with db.connect() as c:
        rows = c.execute("SELECT DISTINCT show_name FROM library_episodes").fetchall()
    return [r["show_name"] for r in rows if r["show_name"]]


def _distinct_movies() -> list[dict]:
    with db.connect() as c:
        rows = c.execute("SELECT DISTINCT title, year FROM library_movies").fetchall()
    return [{"title": r["title"], "year": r["year"]} for r in rows if r["title"]]


def _already_have_series(show_name: str) -> bool:
    key = library.normalize_title(show_name)
    for s in series_mod.list_series():
        if library.normalize_title(s["title"]) == key:
            return True
    return False


def _already_have_movie(title: str, year) -> bool:
    """Same-title-different-year films ('Dune' 1984 vs 2021) are distinct — a
    title-only check silently skipped every remake in the library."""
    key = library.normalize_title(title)
    for m in movies_mod.list_movies():
        if library.normalize_title(m["title"]) != key:
            continue
        if year and m.get("year") and abs(int(m["year"]) - int(year)) > 1:
            continue
        return True
    return False


def _clean_for_search(name: str):
    """Turn a library folder name into a clean TMDb search query + extracted
    year. 'Silicon Valley (2014)' -> ('Silicon Valley', 2014);
    'Stranger Things [1080p]' -> ('Stranger Things', None). Keeps the readable
    title (unlike normalize_title, which lowercases and strips punctuation)."""
    import re
    year = None
    m = re.search(r"\((19|20)\d{2}\)", name or "")
    if m:
        year = int(m.group(0).strip("()"))
    q = re.sub(r"\[[^\]]*\]", " ", name or "")     # drop [1080p] etc.
    q = re.sub(r"\((19|20)\d{2}\)", " ", q)         # drop (YEAR)
    q = " ".join(q.split()).strip()
    return q, year


def _best_tmdb_match(name: str, kind: str, year=None) -> dict | None:
    """Search TMDb and return the result whose normalized title matches the
    folder name. kind: 'tv' | 'movie'. Searches with a cleaned query (year and
    bracket tags removed) since TMDb doesn't match 'Show (2014)' well."""
    query, folder_year = _clean_for_search(name)
    year = year or folder_year
    results = tmdb.search(query or name, kind)
    key = library.normalize_title(name)
    # exact normalized match first
    for r in results:
        if library.normalize_title(r["title"]) == key:
            if year and r.get("year") and abs(int(r["year"]) - int(year)) > 1:
                continue
            return r
    # year-aware fallback: if we have a year, prefer a result that matches it
    # AND shares the folder title's tokens (a bare year match can still be a
    # totally different film released the same year)
    key_tokens = set(key.split())
    if year:
        for r in results:
            if not (r.get("year") and abs(int(r["year"]) - int(year)) <= 1):
                continue
            rt = set(library.normalize_title(r["title"]).split())
            if key_tokens and (key_tokens <= rt or rt <= key_tokens):
                return r
    # last resort: the top TMDb result, but only when its title meaningfully
    # overlaps the folder name — never link something entirely unrelated
    # (blind top-result linking is how 'Trailer Park Boys' folders end up
    # monitored as a spin-off). Otherwise report unmatched for manual review.
    for r in results[:3]:
        rt = set(library.normalize_title(r["title"]).split())
        if key_tokens and (key_tokens <= rt or rt <= key_tokens):
            return r
    return None


def import_library(profile_id: int | None = None, default_monitor: bool = True) -> dict:
    """Discover shows + movies on disk and auto-create monitored entries.
    Requires a TMDb key (for matching). Returns a summary with imported and
    unmatched lists."""
    db.init()
    if not tmdb.enabled():
        return {"error": "TMDb key required for auto-import (set it in Settings)."}

    # make sure the on-disk inventory is current
    library.scan()

    result = {"shows_imported": 0, "shows_skipped": 0, "movies_imported": 0,
              "movies_skipped": 0, "unmatched": []}

    # shows
    for name in _distinct_shows():
        if _already_have_series(name):
            result["shows_skipped"] += 1
            continue
        match = _best_tmdb_match(name, "tv")
        if not match:
            result["unmatched"].append({"name": name, "kind": "show"})
            continue
        try:
            sid = series_mod.add_series(match["tmdb_id"], match["title"], match.get("year"),
                                  match.get("poster"), profile_id)
            series_mod.reconcile(sid)
            result["shows_imported"] += 1
            log.info("Auto-imported show: %s (tmdb %s)", match["title"], match["tmdb_id"])
        except Exception as e:                       # noqa: BLE001
            log.warning("Import failed for show %s: %s", name, e)
            result["unmatched"].append({"name": name, "kind": "show"})

    # movies
    for mv in _distinct_movies():
        if _already_have_movie(mv["title"], mv["year"]):
            result["movies_skipped"] += 1
            continue
        match = _best_tmdb_match(mv["title"], "movie", mv.get("year"))
        if not match:
            result["unmatched"].append({"name": mv["title"], "kind": "movie"})
            continue
        try:
            movies_mod.add_movie(match["tmdb_id"], match["title"], match.get("year"),
                                 match.get("poster"), profile_id)
            result["movies_imported"] += 1
            log.info("Auto-imported movie: %s (tmdb %s)", match["title"], match["tmdb_id"])
        except Exception as e:                       # noqa: BLE001
            log.warning("Import failed for movie %s: %s", mv["title"], e)
            result["unmatched"].append({"name": mv["title"], "kind": "movie"})

    return result

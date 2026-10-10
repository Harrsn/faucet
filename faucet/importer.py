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


def _in(path: str, prefixes) -> bool:
    return prefixes is None or any(path.startswith(p) for p in prefixes)


def _distinct_shows(prefixes=None) -> list[str]:
    with db.connect() as c:
        rows = c.execute("SELECT show_name, path FROM library_episodes").fetchall()
    seen: list[str] = []
    for r in rows:
        if r["show_name"] and _in(r["path"] or "", prefixes) and r["show_name"] not in seen:
            seen.append(r["show_name"])
    return seen


def _distinct_movies(prefixes=None) -> list[dict]:
    with db.connect() as c:
        rows = c.execute("SELECT title, year, path FROM library_movies").fetchall()
    out, seen = [], set()
    for r in rows:
        key = (r["title"], r["year"])
        if r["title"] and _in(r["path"] or "", prefixes) and key not in seen:
            seen.add(key)
            out.append({"title": r["title"], "year": r["year"]})
    return out


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


def import_library(profile_id: int | None = None, default_monitor: bool = True,
                   library_ids: list[int] | None = None, monitor: str = "all") -> dict:
    """Discover shows + movies on disk and auto-create monitored entries.
    Requires a TMDb key (for matching). Returns a summary with imported and
    unmatched lists.

    `library_ids` limits it to those libraries (import Mamaw's folder without
    adopting everything else on disk). `monitor`: 'all' hunts what's missing
    and better copies, 'future' only new episodes, 'none' just tracks what's
    there. Imported titles are filed into the library their files are in."""
    from . import libraries as libs_mod
    db.init()
    if not tmdb.enabled():
        return {"error": "TMDb key required for auto-import (set it in Settings)."}
    if monitor not in ("all", "future", "none"):
        monitor = "all"
    prefixes = None
    if library_ids:
        prefixes = [lib["path"].rstrip("/") + "/" for lib in libs_mod.all_libraries()
                    if lib["id"] in set(library_ids)]

    # make sure the on-disk inventory is current
    library.scan()

    result = {"shows_imported": 0, "shows_skipped": 0, "movies_imported": 0,
              "movies_skipped": 0, "unmatched": []}

    # shows
    for name in _distinct_shows(prefixes):
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
            if monitor != "all":
                series_mod.set_monitor_mode(sid, monitor)    # reconciles
            else:
                series_mod.reconcile(sid)
            result["shows_imported"] += 1
            log.info("Auto-imported show: %s (tmdb %s)", match["title"], match["tmdb_id"])
        except Exception as e:                       # noqa: BLE001
            log.warning("Import failed for show %s: %s", name, e)
            result["unmatched"].append({"name": name, "kind": "show"})

    # movies
    for mv in _distinct_movies(prefixes):
        if _already_have_movie(mv["title"], mv["year"]):
            result["movies_skipped"] += 1
            continue
        match = _best_tmdb_match(mv["title"], "movie", mv.get("year"))
        if not match:
            result["unmatched"].append({"name": mv["title"], "kind": "movie"})
            continue
        try:
            mid = movies_mod.add_movie(match["tmdb_id"], match["title"], match.get("year"),
                                       match.get("poster"), profile_id)
            if monitor == "none" and mid:
                with db.connect() as c:          # track it, don't hunt it (not even upgrades)
                    c.execute("UPDATE movies SET monitored=0 WHERE id=?", (mid,))
                    c.execute("DELETE FROM wanted WHERE kind='movie' AND series_id=?", (mid,))
            result["movies_imported"] += 1
            log.info("Auto-imported movie: %s (tmdb %s)", match["title"], match["tmdb_id"])
        except Exception as e:                       # noqa: BLE001
            log.warning("Import failed for movie %s: %s", mv["title"], e)
            result["unmatched"].append({"name": mv["title"], "kind": "movie"})

    # link the new shows to their folders and file every new title into the
    # library its files are in
    try:
        libs_mod.auto_link()
        result["assigned"] = libs_mod.assign_from_files()
    except Exception as e:                       # noqa: BLE001
        log.warning("post-import linking failed: %s", e)
    return result

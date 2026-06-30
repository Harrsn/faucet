"""TMDb metadata — search movies/TV by title, fetch posters and details.

Turns Faucet from a release-string search box into a media app: users search
"Dune", pick the right title from poster results, and Faucet builds the indexer
query from the canonical title + year. Results are cached in the DB to stay well
under TMDb rate limits and keep the UI snappy.

A TMDb API key (free) is required; set it via the settings editor or TMDB_API_KEY.
Without a key, title search is simply disabled and Faucet falls back to raw
indexer search — so this is purely additive.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import requests

from . import db

_BASE = "https://api.themoviedb.org/3"
_IMG = "https://image.tmdb.org/t/p/w342"
_CACHE_TTL = 60 * 60 * 24  # 24h


def api_key() -> str:
    # DB setting wins (set via UI), else env.
    return db.get_setting("tmdb_key") or os.environ.get("TMDB_API_KEY", "")


def enabled() -> bool:
    return bool(api_key())


def _get(path: str, params: dict) -> dict:
    params = {**params, "api_key": api_key()}
    r = requests.get(f"{_BASE}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _cache_get(key: str):
    row = db.get_setting(f"_tmdbcache:{key}")
    if row and isinstance(row, dict) and row.get("exp", 0) > time.time():
        return row.get("data")
    return None


def _cache_put(key: str, data):
    db.set_setting(f"_tmdbcache:{key}", {"exp": time.time() + _CACHE_TTL, "data": data})


def _poster(path: Optional[str]) -> Optional[str]:
    return f"{_IMG}{path}" if path else None


def search(query: str, kind: str = "multi") -> list[dict]:
    """Search TMDb. kind: multi | movie | tv. Returns normalized records."""
    if not enabled() or not query.strip():
        return []
    ck = f"search:{kind}:{query.lower()}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    path = {"movie": "/search/movie", "tv": "/search/tv"}.get(kind, "/search/multi")
    try:
        data = _get(path, {"query": query})
    except requests.RequestException:
        return []

    out = []
    for r in data.get("results", []):
        media = r.get("media_type") or kind
        if media not in ("movie", "tv"):
            continue
        title = r.get("title") or r.get("name") or ""
        date = r.get("release_date") or r.get("first_air_date") or ""
        year = date[:4] if date else None
        out.append({
            "tmdb_id": r.get("id"),
            "media_type": media,
            "title": title,
            "year": year,
            "overview": (r.get("overview") or "")[:280],
            "poster": _poster(r.get("poster_path")),
            "rating": round(r.get("vote_average", 0), 1),
            # the query Faucet will run against indexers for this title
            "search_query": f"{title} {year}".strip() if year else title,
        })
    # popularity-ish: TMDb already returns by relevance; keep order
    _cache_put(ck, out)
    return out


def episodes(tmdb_id: int, total_seasons: int) -> list[dict]:
    """Fetch the full canonical episode list across all seasons.
    Returns [{season, episode, title, air_date}] — the 'what should exist'
    data reconciliation diffs against the library. Cached per season."""
    if not enabled():
        return []
    out = []
    for season_no in range(1, (total_seasons or 0) + 1):
        ck = f"season:{tmdb_id}:{season_no}"
        cached = _cache_get(ck)
        if cached is not None:
            out.extend(cached)
            continue
        try:
            data = _get(f"/tv/{tmdb_id}/season/{season_no}", {})
        except requests.RequestException:
            continue
        season_eps = []
        for ep in data.get("episodes", []):
            if ep.get("episode_number") is None:
                continue
            season_eps.append({
                "season": ep.get("season_number", season_no),
                "episode": ep.get("episode_number"),
                "title": ep.get("name", ""),
                "air_date": ep.get("air_date") or "",
            })
        _cache_put(ck, season_eps)
        out.extend(season_eps)
    return out


def details(tmdb_id: int, media_type: str) -> dict:
    if not enabled():
        return {}
    ck = f"details:{media_type}:{tmdb_id}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    path = f"/{'movie' if media_type=='movie' else 'tv'}/{tmdb_id}"
    try:
        d = _get(path, {})
    except requests.RequestException:
        return {}
    title = d.get("title") or d.get("name") or ""
    date = d.get("release_date") or d.get("first_air_date") or ""
    out = {
        "tmdb_id": tmdb_id, "media_type": media_type, "title": title,
        "year": date[:4] if date else None,
        "overview": d.get("overview", ""),
        "poster": _poster(d.get("poster_path")),
        "backdrop": _poster(d.get("backdrop_path")),
        "rating": round(d.get("vote_average", 0), 1),
        "genres": [g["name"] for g in d.get("genres", [])],
        "seasons": d.get("number_of_seasons"),
        "runtime": d.get("runtime"),
    }
    _cache_put(ck, out)
    return out

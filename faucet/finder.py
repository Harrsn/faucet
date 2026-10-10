"""Per-indexer parallel search.

Searching Jackett's aggregate 'all' endpoint made every query as slow as the
slowest indexer: one Cloudflare challenge being solved through FlareSolverr
held up every result, and one indexer timing out failed the whole search,
results from the healthy ones included. Here each configured indexer is
queried on its own, all at once, each with its own timeout; a slow or broken
indexer costs only its own results.

Queries are structured where the indexer allows it (its Torznab caps):
`tvsearch` with season/ep, by TVDB/IMDb/TMDb id when supported, `movie` by
IMDb id. Indexers that only do free text get the classic "Title S01E02" /
"Title Year" query. Results are merged and de-duplicated by info-hash.

Env:
  SEARCH_TIMEOUT       seconds per indexer (default 60: a cold Cloudflare
                       solve takes 10-15s and FlareSolverr solves serially)
  SEARCH_CONCURRENCY   indexers queried at once (default 8)
"""
from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import config as cfgmod
from . import db
from . import search as searchmod

log = logging.getLogger("faucet.finder")

_INDEXERS_KEY = "_torznab:indexers"
_CAPS_KEY = "_torznab:caps:"
_INDEXERS_TTL = 3600
_CAPS_TTL = 24 * 3600
_BTIH = re.compile(r"btih:([A-Za-z0-9]{32,40})", re.IGNORECASE)


def _int_env(key: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(key, str(default))))
    except ValueError:
        return default


def _cached(key: str, ttl: float):
    v = db.get_setting(key)
    if isinstance(v, dict) and v.get("exp", 0) > time.time():
        return v.get("data")
    return None


def _store(key: str, data, ttl: float) -> None:
    db.set_setting(key, {"exp": time.time() + ttl, "data": data})


def indexer_ids() -> list[str]:
    """The indexers to query: JACKETT_INDEXER as a comma list, or for 'all'
    every configured Jackett indexer (cached for an hour). [] when the list
    can't be read — callers fall back to the aggregate endpoint."""
    c = cfgmod.config
    raw = (c.jackett_indexer or "all").strip()
    if raw and raw.lower() != "all":
        return [x.strip() for x in raw.split(",") if x.strip()]
    cached = _cached(_INDEXERS_KEY, _INDEXERS_TTL)
    if cached:
        return cached
    items = searchmod.indexers(c.jackett_url, c.jackett_api_key, min(15, c.request_timeout))
    ids = [x["id"] for x in items]
    if ids:
        _store(_INDEXERS_KEY, ids, _INDEXERS_TTL)
    return ids


def indexer_caps(ix: str) -> dict:
    """{function: set(params)}, cached a day; on failure free-text only."""
    cached = _cached(_CAPS_KEY + ix, _CAPS_TTL)
    if cached is not None:
        return {k: set(v) for k, v in cached.items()}
    c = cfgmod.config
    try:
        caps = searchmod.caps(c.jackett_url, c.jackett_api_key, ix, min(20, c.request_timeout))
    except searchmod.SearchError as e:
        log.info("caps for %s unavailable (%s); free-text search only", ix, e)
        return {"search": {"q"}}
    _store(_CAPS_KEY + ix, {k: sorted(v) for k, v in caps.items()}, _CAPS_TTL)
    return caps


def forget_indexers() -> None:
    """Drop the cached indexer list and caps (indexers added/removed in Jackett)."""
    with db.connect() as c:
        c.execute("DELETE FROM settings WHERE key=? OR key LIKE ?",
                  (_INDEXERS_KEY, _CAPS_KEY.replace("_", "\\_") + "%"))


# ── query planning ──────────────────────────────────────────────────────────

def classic_query(kind: str, title: str = "", season=None, episode=None,
                  year=None, query: str = "") -> str:
    if kind == "episode":
        return f"{title} S{int(season):02d}E{int(episode):02d}"
    if kind == "season":
        return f"{title} S{int(season):02d}"
    if kind == "movie":
        return f"{title} {year}" if year else title
    return query


def plan(caps: dict, kind: str, title: str = "", season=None, episode=None,
         year=None, query: str = "", ids: dict | None = None) -> tuple[str, str, dict, str]:
    """(mode, q, params, category) for one indexer."""
    ids = ids or {}
    if kind in ("episode", "season") and "tv-search" in caps:
        p = caps["tv-search"]
        params: dict = {}
        for idkey, val in (("tvdbid", ids.get("tvdb_id")), ("imdbid", ids.get("imdb_id")),
                           ("tmdbid", ids.get("tmdb_id"))):
            if val and idkey in p:
                params[idkey] = val
                break
        if "season" in p and (kind == "season" or "ep" in p):
            params["season"] = int(season)
            if kind == "episode":
                params["ep"] = int(episode)
            q = "" if any(k in params for k in ("tvdbid", "imdbid", "tmdbid")) else title
            return "tvsearch", q, params, "tv"
        q = classic_query(kind, title, season, episode)
        return "tvsearch", "" if params else q, params, "tv"
    if kind == "movie" and "movie-search" in caps:
        p = caps["movie-search"]
        imdb = ids.get("imdb_id")
        if imdb and "imdbid" in p:
            return "movie", "", {"imdbid": imdb}, "movies"
        if ids.get("tmdb_id") and "tmdbid" in p:
            return "movie", "", {"tmdbid": ids["tmdb_id"]}, "movies"
        return "movie", classic_query("movie", title, year=year), {}, "movies"
    cat = {"episode": "tv", "season": "tv", "movie": "movies"}.get(kind, "all")
    return "search", classic_query(kind, title, season, episode, year, query), {}, cat


# ── the search ──────────────────────────────────────────────────────────────

def _key(r: dict) -> str:
    m = _BTIH.search(r.get("href") or "")
    if m:
        return "h:" + m.group(1).lower()
    return "t:" + (r.get("title") or "").lower() + "|" + str(r.get("size") or 0)


def find(kind: str, *, title: str = "", season=None, episode=None, year=None,
         query: str = "", ids: dict | None = None, category: str = "all",
         limit: int | None = None) -> searchmod.Results:
    """Search for an episode, season pack, movie, or free text (kind 'free').

    Raises SearchError only when every indexer failed (or the indexer isn't
    configured); partial failures are returned in `.errors`."""
    c = cfgmod.config
    limit = limit or c.search_limit
    ixs = indexer_ids()
    if not ixs:
        # can't enumerate indexers: the aggregate endpoint, classic query
        q = classic_query(kind, title, season, episode, year, query)
        cat = category if kind == "free" else {"movie": "all"}.get(kind, "all")
        out = searchmod.search(c.jackett_url, c.jackett_api_key, c.jackett_indexer or "all",
                               q, cat, limit, c.request_timeout)
        if not isinstance(out, searchmod.Results):
            out = searchmod.Results(out)
        out.errors, out.searched = [], 1
        return out

    timeout = _int_env("SEARCH_TIMEOUT", 60)

    def one(ix):
        if kind == "free":
            mode, q, params, cat = "search", query, {}, category
        else:
            mode, q, params, cat = plan(indexer_caps(ix), kind, title, season, episode,
                                        year, query, ids)
        try:
            if mode == "search" and not params:
                res = searchmod.search(c.jackett_url, c.jackett_api_key, ix, q, cat,
                                       limit, timeout)
            else:
                res = searchmod.search(c.jackett_url, c.jackett_api_key, ix, q, cat,
                                       limit, timeout, mode=mode, params=params)
            return ix, res, None
        except searchmod.SearchError as e:
            return ix, None, str(e)

    merged: dict = {}
    errors, hidden = [], 0
    with ThreadPoolExecutor(max_workers=min(len(ixs), _int_env("SEARCH_CONCURRENCY", 8))) as pool:
        for ix, res, err in pool.map(one, ixs):
            if err:
                errors.append((ix, err))
                continue
            hidden += getattr(res, "hidden", 0)
            for r in res:
                k = _key(r)
                if k not in merged or (r.get("seeders") or 0) > (merged[k].get("seeders") or 0):
                    merged[k] = r
    if errors and len(errors) == len(ixs):
        raise searchmod.SearchError(
            f"all {len(ixs)} indexers failed (first: {errors[0][0]}: {errors[0][1]})")
    out = searchmod.Results(sorted(merged.values(), key=lambda r: r.get("seeders") or 0,
                                   reverse=True)[:limit])
    out.hidden, out.errors, out.searched = hidden, errors, len(ixs)
    if errors:
        log.info("search %r: %d/%d indexers failed (%s)", title or query, len(errors), len(ixs),
                 "; ".join(f"{i}: {m[:80]}" for i, m in errors[:3]))
    return out


def ids_for(kind: str, local_id) -> dict:
    """TMDb/TVDB/IMDb ids for a monitored series ('episode'/'season') or
    movie, for ID-based search. {} when unknown — never fatal."""
    try:
        from . import tmdb
        table = "movies" if kind == "movie" else "series"
        with db.connect() as c:
            row = c.execute(f"SELECT tmdb_id FROM {table} WHERE id=?", (local_id,)).fetchone()
        if not row or not row["tmdb_id"]:
            return {}
        out = {"tmdb_id": row["tmdb_id"]}
        out.update(tmdb.external_ids(row["tmdb_id"], "movie" if kind == "movie" else "tv"))
        return out
    except Exception:                            # noqa: BLE001
        return {}

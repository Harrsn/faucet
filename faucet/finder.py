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

Indexers whose caps say they don't carry the content are skipped (no TV
searches to a movies-only indexer). Once most indexers have answered, the
rest get SEARCH_STRAGGLER_SECONDS more and are then left behind.

Env:
  SEARCH_TIMEOUT            seconds per indexer (default 60: a cold Cloudflare
                            solve takes 10-15s and FlareSolverr solves serially)
  SEARCH_CONCURRENCY        indexers queried at once (default 8)
  SEARCH_STRAGGLER_SECONDS  grace for the rest once a majority answered (10)
"""
from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

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
        fallback = {"search": {"q"}}
        _store(_CAPS_KEY + ix, {k: sorted(v) for k, v in fallback.items()}, 3600)
        return fallback
    _store(_CAPS_KEY + ix, {k: sorted(v) for k, v in caps.items()}, _CAPS_TTL)
    return caps


_KIND_CAT = {"episode": 5000, "season": 5000, "movie": 2000}


def carries(caps: dict, kind: str) -> bool:
    """Does this indexer carry the content being searched? Unknown = yes.
    YTS (movies) answered every TV search with nothing; SubsPlease (anime)
    likewise for movies."""
    want = _KIND_CAT.get(kind)
    cats = caps.get("_cats")
    return want is None or not cats or want in cats


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
    grace = float(_int_env("SEARCH_STRAGGLER_SECONDS", 10))
    capsmap: dict = {}
    if kind != "free":
        # cached a day; the first search after that fetches them all at once
        with ThreadPoolExecutor(max_workers=min(len(ixs), 8)) as cp:
            capsmap = dict(zip(ixs, cp.map(indexer_caps, ixs), strict=True))
    skipped = [ix for ix in ixs if kind != "free" and not carries(capsmap[ix], kind)]
    targets = [ix for ix in ixs if ix not in skipped] or ixs

    def one(ix):
        if kind == "free":
            mode, q, params, cat = "search", query, {}, category
        else:
            mode, q, params, cat = plan(capsmap[ix], kind, title, season, episode,
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
    errors, hidden, slow = [], 0, []
    # Not a `with` block: leaving one waits for every thread. Stragglers are
    # left to finish (and record their telemetry) in the background.
    pool = ThreadPoolExecutor(max_workers=min(len(targets), _int_env("SEARCH_CONCURRENCY", 8)),
                              thread_name_prefix="search")
    futures = {pool.submit(one, ix): ix for ix in targets}
    pending = set(futures)
    majority = len(targets) // 2 + 1
    deadline = None
    try:
        while pending:
            wait_for = timeout + 5 if deadline is None else max(0.0, deadline - time.monotonic())
            done, pending = wait(pending, timeout=wait_for, return_when=FIRST_COMPLETED)
            for fut in done:
                ix, res, err = fut.result()
                if err:
                    errors.append((ix, err))
                    continue
                hidden += getattr(res, "hidden", 0)
                for r in res:
                    k = _key(r)
                    if k not in merged or (r.get("seeders") or 0) > (merged[k].get("seeders") or 0):
                        merged[k] = r
            answered = len(futures) - len(pending)
            if deadline is None and answered >= majority:
                # most indexers have answered: the rest get a short grace,
                # not the full timeout (one cold Cloudflare solve used to
                # stretch every search to 12s for zero results)
                deadline = time.monotonic() + grace
            if pending and deadline is not None and time.monotonic() >= deadline:
                slow = [futures[f] for f in pending]
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if errors and len(errors) == len(targets):
        raise searchmod.SearchError(
            f"all {len(targets)} indexers failed (first: {errors[0][0]}: {errors[0][1]})")
    out = searchmod.Results(sorted(merged.values(), key=lambda r: r.get("seeders") or 0,
                                   reverse=True)[:limit])
    out.hidden, out.errors, out.searched = hidden, errors, len(targets)
    out.skipped, out.slow = skipped, slow
    if errors or slow:
        log.info("search %r: %d/%d indexers failed, %d too slow (%s)", title or query,
                 len(errors), len(targets), len(slow),
                 "; ".join([f"{i}: {m[:80]}" for i, m in errors[:3]] + [f"{i}: slow" for i in slow]))
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

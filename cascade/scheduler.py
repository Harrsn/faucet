"""Background scheduler — periodic auto-grab for subscriptions.

Cascade has no separate worker; this runs as an asyncio task inside the FastAPI
app (started from the lifespan handler). Honoring the "one lightweight service"
design: no extra container, no cron, no user setup. The loop wakes on an
interval, checks each enabled subscription, and grabs the best new release per
the subscription's quality profile.

Blocking work (indexer HTTP, client adds) is offloaded to a thread so the web
process keeps serving requests. The `grabbed` table dedupes so a release is
never grabbed twice across runs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime

from . import db
from . import search as searchmod
from . import profiles as prof
from .config import config
from .clients import make_client, DownloadClientError

log = logging.getLogger("cascade.scheduler")

# how often the loop wakes, in seconds (default 30 min)
INTERVAL = int(os.environ.get("RSS_INTERVAL_SECONDS", str(30 * 60)))

_task: asyncio.Task | None = None
_last_run: dict = {"ts": None, "checked": 0, "grabbed": 0}


def _load_profile(profile_id: int | None) -> dict | None:
    if not profile_id:
        return None
    with db.connect() as c:
        row = c.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    if not row:
        return None
    p = dict(row)
    p["resolutions"] = json.loads(p.get("resolutions") or "[]")
    p["sources"] = json.loads(p.get("sources") or "[]")
    return p


def check_subscription(sub: dict) -> dict:
    """Run one subscription: search, dedupe, rank by profile, grab the best new
    release. Returns a small result summary. Synchronous (called in a thread)."""
    title = sub.get("title") or sub.get("query") or f"sub {sub.get('id')}"
    query = sub.get("query") or sub.get("title") or ""
    result = {"id": sub.get("id"), "title": title, "grabbed": None,
              "considered": 0, "error": None}
    if not query:
        result["error"] = "no query"
        return result

    try:
        results = searchmod.search(
            config.jackett_url, config.jackett_api_key, config.jackett_indexer,
            query, "all", config.search_limit, config.request_timeout)
    except searchmod.SearchError as e:
        result["error"] = f"search failed: {e}"
        return result
    result["considered"] = len(results)

    # drop releases we've already grabbed (dedupe key = release title)
    fresh = [r for r in results if not db.already_grabbed(r["title"])]
    if not fresh:
        db.update_subscription(sub["id"], last_check=datetime.now().isoformat(timespec="seconds"))
        return result

    # rank by the subscription's profile if it has one; else take best-seeded
    profile = _load_profile(sub.get("profile_id"))
    if profile:
        ranked = prof.rank(fresh, profile)
    else:
        ranked = sorted(fresh, key=lambda x: x.get("seeders", 0), reverse=True)
    if not ranked:
        db.update_subscription(sub["id"], last_check=datetime.now().isoformat(timespec="seconds"))
        return result

    pick = ranked[0]
    # mark grabbed FIRST (atomic dedupe) so a crash mid-add can't double-grab
    if not db.mark_grabbed(pick["title"], sub["id"]):
        return result  # someone/another tick grabbed it between search and now

    try:
        client = make_client(config.client_kind, config.client_url,
                             config.client_user, config.client_pass, config.request_timeout)
        add = client.add(pick["href"], config.download_dir or None)
        result["grabbed"] = pick["title"]
        db.add_history("added", pick["title"], f"auto-grab: {title}")
        db.update_subscription(
            sub["id"],
            last_check=datetime.now().isoformat(timespec="seconds"),
            last_grab=datetime.now().isoformat(timespec="seconds"))
        log.info("Auto-grabbed '%s' for subscription '%s' (id=%s)",
                 pick["title"], title, add.id)
    except DownloadClientError as e:
        result["error"] = f"add failed: {e}"
        log.warning("Auto-grab add failed for '%s': %s", pick["title"], e)
        # roll back the grabbed marker so it retries next tick
        with db.connect() as c:
            c.execute("DELETE FROM grabbed WHERE title=?", (pick["title"],))
    return result


def _try_season_pack(title, season, profile):
    """Search for a season pack for this show/season. Returns the best matching
    pack release (a dict with title/href) or None. Only returns true season
    packs (classify_pack -> 'season' for the right season); ignores series packs
    (too large, per design) and individual episodes."""
    from . import packs
    query = f"{title} S{int(season):02d}"
    try:
        results = searchmod.search(
            config.jackett_url, config.jackett_api_key, config.jackett_indexer,
            query, "all", config.search_limit, config.request_timeout)
    except searchmod.SearchError:
        return None
    candidates = []
    for r in results:
        if db.already_grabbed(r["title"]):
            continue
        cls = packs.classify_pack(r["title"])
        if cls["kind"] == "season" and (cls["season"] in (season, None)):
            candidates.append(r)
    if not candidates:
        return None
    if profile:
        ranked = prof.rank(candidates, profile)
    else:
        ranked = sorted(candidates, key=lambda x: x.get("seeders", 0), reverse=True)
    return ranked[0] if ranked else None


def hunt_wanted(series_filter=None, max_override=None) -> dict:
    """Search for and grab wanted items (missing + upgrades), respecting two
    caps so an unattended run can't flood the client:
      MAX_ACTIVE  — don't grab if this many torrents are already downloading
      MAX_PER_RUN — grab at most this many per cycle
    Remaining wants stay 'wanted' and get picked up on the next tick.
    series_filter: if set, only hunt wants for that series id (per-show hunt).
    max_override: override the per-run cap (used by per-show hunt)."""
    from . import series as series_mod
    db.init()

    max_active = int(os.environ.get("HUNT_MAX_ACTIVE", "5"))
    max_per_run = max_override if max_override is not None else int(os.environ.get("HUNT_MAX_PER_RUN", "3"))

    # how many torrents are already downloading right now?
    active = 0
    try:
        client0 = make_client(config.client_kind, config.client_url,
                             config.client_user, config.client_pass, config.request_timeout)
        active = sum(1 for t in client0.list_transfers()
                     if getattr(t, "status", "") == "downloading")
    except Exception:                            # noqa: BLE001
        active = 0

    wanted = series_mod.list_wanted("wanted")
    if series_filter is not None:
        wanted = [w for w in wanted if w.get("series_id") == series_filter]
    grabbed = 0
    details = []
    if active >= max_active:
        log.info("Hunt skipped: %d already downloading (cap %d).", active, max_active)
        return {"wanted": len(wanted), "grabbed": 0, "details": [],
                "skipped_reason": f"{active} active >= cap {max_active}"}

    budget = min(max_per_run, max_active - active)

    # ── Season-pack pre-pass ──
    # Group episode-wants by (series, season); for any season with 2+ wanted
    # episodes, try to grab a single season pack instead of N episodes. A pack
    # uses one client slot but satisfies many wants. Episodes covered by a
    # grabbed pack are removed from the per-episode pass below.
    covered_episode_wants = set()        # ids of wanted rows a pack will cover
    if budget > 0:
        from collections import defaultdict
        by_season = defaultdict(list)
        for w in wanted:
            if w.get("kind", "episode") == "movie":
                continue
            if w.get("series_id") is None or w.get("season") is None:
                continue
            by_season[(w["series_id"], w["season"])].append(w)
        for (sid, season), eps in by_season.items():
            if grabbed >= budget:
                break
            if len(eps) < 2:                 # need 2+ missing to prefer a pack
                continue
            title = eps[0].get("series_title") or ""
            if not title:
                continue
            pack = _try_season_pack(title, season, _load_profile_for_series(sid))
            if not pack:
                continue
            # grab the pack
            if not db.mark_grabbed(pack["title"], None):
                continue
            try:
                client = make_client(config.client_kind, config.client_url,
                                     config.client_user, config.client_pass, config.request_timeout)
                client.add(pack["href"], config.download_dir or None)
                grabbed += 1
                db.add_history("added", pack["title"], f"season pack: {title} S{int(season):02d} ({len(eps)} eps)")
                # mark every wanted episode in this season as grabbed-by-pack
                with db.connect() as c:
                    for w in eps:
                        c.execute("UPDATE wanted SET status='grabbed', last_search=? WHERE id=?",
                                  (datetime.now().isoformat(timespec="seconds"), w["id"]))
                        covered_episode_wants.add(w["id"])
                details.append({"want": f"{title} S{int(season):02d} (pack)",
                                "reason": "pack", "grabbed": pack["title"], "error": None,
                                "covers": len(eps)})
                log.info("Grabbed season pack '%s' covering %d episodes", pack["title"], len(eps))
            except DownloadClientError as e:
                with db.connect() as c:
                    c.execute("DELETE FROM grabbed WHERE title=?", (pack["title"],))
                details.append({"want": f"{title} S{int(season):02d} (pack)",
                                "reason": "pack", "grabbed": None, "error": f"add failed: {e}"})

    for w in wanted:
        if grabbed >= budget:
            break
        if w.get("id") in covered_episode_wants:
            continue
        kind = w.get("kind", "episode")
        if kind == "movie":
            query = w.get("title") or ""
            series_id = w.get("series_id")  # for movies this is the movie id
            profile = _load_profile_for_movie(series_id)
        else:
            title = w.get("series_title") or ""
            season, episode = w.get("season"), w.get("episode")
            if not title or season is None or episode is None:
                continue
            query = f"{title} S{int(season):02d}E{int(episode):02d}"
            profile = _load_profile_for_series(w.get("series_id"))
        if not query:
            continue
        res = {"want": query, "reason": w.get("reason"), "grabbed": None, "error": None}
        try:
            results = searchmod.search(
                config.jackett_url, config.jackett_api_key, config.jackett_indexer,
                query, "all", config.search_limit, config.request_timeout)
        except searchmod.SearchError as e:
            res["error"] = f"search failed: {e}"
            details.append(res)
            continue

        fresh = [r for r in results if not db.already_grabbed(r["title"])]
        if profile:
            ranked = prof.rank(fresh, profile)
        else:
            ranked = sorted(fresh, key=lambda x: x.get("seeders", 0), reverse=True)
        if not ranked:
            details.append(res)
            continue

        pick = ranked[0]
        if not db.mark_grabbed(pick["title"], None):
            details.append(res)
            continue
        try:
            client = make_client(config.client_kind, config.client_url,
                                 config.client_user, config.client_pass, config.request_timeout)
            client.add(pick["href"], config.download_dir or None)
            res["grabbed"] = pick["title"]
            grabbed += 1
            db.add_history("added", pick["title"], f"hunt: {query} ({w.get('reason')})")
            with db.connect() as c:
                c.execute("UPDATE wanted SET status='grabbed', last_search=? WHERE id=?",
                          (datetime.now().isoformat(timespec="seconds"), w["id"]))
            log.info("Hunt grabbed '%s' for %s", pick["title"], query)
        except DownloadClientError as e:
            res["error"] = f"add failed: {e}"
            with db.connect() as c:
                c.execute("DELETE FROM grabbed WHERE title=?", (pick["title"],))
        details.append(res)
    return {"wanted": len(wanted), "grabbed": grabbed, "details": details}


def _load_profile_for_movie(movie_id):
    if not movie_id:
        return None
    with db.connect() as c:
        row = c.execute("SELECT profile_id FROM movies WHERE id=?", (movie_id,)).fetchone()
    return _load_profile(row["profile_id"]) if row else None


def _load_profile_for_series(series_id):
    if not series_id:
        return None
    with db.connect() as c:
        row = c.execute("SELECT profile_id FROM series WHERE id=?", (series_id,)).fetchone()
    return _load_profile(row["profile_id"]) if row else None


def run_once() -> dict:
    """One full cycle: check query-subscriptions, then run the library-aware
    pipeline (scan → reconcile monitored series → hunt wanted)."""
    db.init()
    # 1. legacy query subscriptions
    subs = db.list_subscriptions(enabled_only=True)
    grabbed = 0
    details = []
    for sub in subs:
        r = check_subscription(sub)
        details.append(r)
        if r["grabbed"]:
            grabbed += 1

    # 2. library-aware pipeline
    lib_summary = {}
    try:
        from . import library, series as series_mod, movies as movies_mod
        library.scan()
        recon = series_mod.reconcile_all()
        movies_mod.reconcile_all()
        hunt = hunt_wanted()
        grabbed += hunt.get("grabbed", 0)
        lib_summary = {"reconcile": recon, "hunt_grabbed": hunt.get("grabbed", 0),
                       "wanted": hunt.get("wanted", 0)}
    except Exception as e:                       # noqa: BLE001 - never kill the tick
        log.warning("Library pipeline error: %s", e)
        lib_summary = {"error": str(e)}

    _last_run.update(ts=datetime.now().isoformat(timespec="seconds"),
                     checked=len(subs), grabbed=grabbed)
    return {"checked": len(subs), "grabbed": grabbed, "details": details,
            "library": lib_summary}


def last_run() -> dict:
    return dict(_last_run)


async def _loop():
    log.info("RSS scheduler started (interval=%ss).", INTERVAL)
    # small initial delay so startup isn't blocked by a check
    await asyncio.sleep(10)
    while True:
        try:
            # offload the blocking work to a thread
            res = await asyncio.to_thread(run_once)
            if res["grabbed"]:
                log.info("Scheduler tick: %d checked, %d grabbed.",
                         res["checked"], res["grabbed"])
        except Exception as e:                       # noqa: BLE001 - never kill the loop
            log.warning("Scheduler tick error: %s", e)
        await asyncio.sleep(INTERVAL)


def start():
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
    return _task


def stop():
    global _task
    if _task and not _task.done():
        _task.cancel()
        _task = None

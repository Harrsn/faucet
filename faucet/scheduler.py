"""Background scheduler — periodic auto-grab for subscriptions.

Faucet has no separate worker; this runs as an asyncio task inside the FastAPI
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
import threading
from datetime import datetime

from . import db
from . import search as searchmod
from . import profiles as prof
from . import huntlog
from . import config as _cfgmod
from .clients import make_client, DownloadClientError

def _cfg():
    """The live config. Settings saves swap `faucet.config.config` for a new
    object (config.reload); binding it at import time left the scheduler
    hunting with whatever was configured when the process started."""
    return _cfgmod.config


log = logging.getLogger("faucet.scheduler")

# how often the loop wakes, in seconds (default 30 min)
INTERVAL = int(os.environ.get("RSS_INTERVAL_SECONDS", str(30 * 60)))

_task: asyncio.Task | None = None
_last_run: dict = {"ts": None, "checked": 0, "grabbed": 0}


def _lang_filter(results: list) -> list:
    """Apply the global default-language filter to a candidate list. Used on the
    no-profile path so foreign-language releases (e.g. Russian dubs) are dropped
    even when a show has no quality profile assigned. Releases whose language
    can't be determined are kept."""
    try:
        lang = (db.get_setting("default_language", "en") or "en").strip().lower()
    except Exception:                            # noqa: BLE001
        lang = "en"
    if not lang or lang == "any":
        return results
    from . import langdetect
    return [r for r in results if langdetect.matches(r, lang)]


def _rank_no_profile(candidates: list) -> list:
    """Rank when no profile is set: drop wrong-language releases, then sort by
    seeders."""
    filtered = _lang_filter(candidates)
    return sorted(filtered, key=lambda x: x.get("seeders", 0), reverse=True)


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
            _cfg().jackett_url, _cfg().jackett_api_key, _cfg().jackett_indexer,
            query, "all", _cfg().search_limit, _cfg().request_timeout)
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
        ranked = _rank_no_profile(fresh)
    if not ranked:
        db.update_subscription(sub["id"], last_check=datetime.now().isoformat(timespec="seconds"))
        return result

    pick = ranked[0]
    # mark grabbed FIRST (atomic dedupe) so a crash mid-add can't double-grab
    if not db.mark_grabbed(pick["title"], sub["id"]):
        return result  # someone/another tick grabbed it between search and now

    try:
        client = make_client(_cfg().client_kind, _cfg().client_url,
                             _cfg().client_user, _cfg().client_pass, _cfg().request_timeout)
        add = client.add(pick["href"], _cfg().download_dir or None)
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


_last_pack: dict = {}       # stats from the most recent _try_season_pack, for the hunt log


def _try_season_pack(title, season, profile):
    """Search for a season pack for this show/season. Returns the best matching
    pack release (a dict with title/href) or None. Only returns true season
    packs (classify_pack -> 'season' for the right season); ignores series packs
    (too large, per design) and individual episodes."""
    from . import packs
    from . import releasematch
    query = f"{title} S{int(season):02d}"
    try:
        results = searchmod.search(
            _cfg().jackett_url, _cfg().jackett_api_key, _cfg().jackett_indexer,
            query, "all", _cfg().search_limit, _cfg().request_timeout)
    except searchmod.SearchError as e:
        _last_pack.update(error=str(e))
        return None
    reasons: dict = {}
    _last_pack.update(results=len(results), reasons=reasons)
    candidates = []
    for r in results:
        if db.already_grabbed(r["title"]):
            reasons["blocklisted"] = reasons.get("blocklisted", 0) + 1
            continue
        # the release must actually BE this show — substring search happily
        # returns spin-offs ("Trailer Park Boys: Out of the Park ...")
        if not releasematch.matches_series(r["title"], title):
            reasons["wrong_title"] = reasons.get("wrong_title", 0) + 1
            continue
        cls = packs.classify_pack(r["title"])
        # require the EXACT season: a pack whose season can't be determined
        # may be any season (or a mislabeled range) — too risky to grab
        if cls["kind"] == "season" and cls["season"] == season:
            candidates.append(r)
        else:
            reasons["pack"] = reasons.get("pack", 0) + 1
    if not candidates:
        return None
    ranked = _rank_for_hunt(candidates, profile, reasons, _tracker_penalties())
    return ranked[0] if ranked else None


def _tracker_penalties() -> dict[str, float]:
    """Score penalty per tracker whose recent hunter grabs mostly stalled.

    A tracker needs HUNT_TRACKER_MIN_GRABS (4) settled grabs in the last 14
    days, and a stall rate of at least HUNT_TRACKER_STALL_RATE (0.5), before
    it is penalised — enough to push its releases below a healthy
    alternative one resolution step lower, never enough to exclude it."""
    try:
        min_n = int(os.environ.get("HUNT_TRACKER_MIN_GRABS", "4"))
        rate = float(os.environ.get("HUNT_TRACKER_STALL_RATE", "0.5"))
    except ValueError:
        min_n, rate = 4, 0.5
    out = {}
    try:
        for t, d in huntlog.tracker_outcomes().items():
            settled = d["stalled"] + d["completed"]
            if settled >= min_n and d["stall_rate"] is not None and d["stall_rate"] >= rate:
                out[t] = 120.0
    except Exception:                            # noqa: BLE001 - ranking must not fail on stats
        return {}
    return out


def _rank_for_hunt(candidates: list, profile, reasons: dict | None = None,
                   trackers: dict | None = None) -> list:
    """Rank for an unattended grab: the profile's hard filters (rejections
    counted into `reasons` by bucket), its score — or, without a profile,
    the default-language filter and swarm health — minus any tracker
    penalty."""
    reasons = reasons if reasons is not None else {}
    if profile:
        kept = []
        for r in candidates:
            ok, why = prof.passes(r, profile)
            if ok:
                rr = dict(r)
                rr["_score"] = prof.score(r, profile)
                kept.append(rr)
            else:
                b = huntlog.profile_bucket(why)
                reasons[b] = reasons.get(b, 0) + 1
    else:
        langok = _lang_filter(candidates)
        if len(langok) != len(candidates):
            reasons["language"] = reasons.get("language", 0) + len(candidates) - len(langok)
        kept = [dict(r, _score=prof.health(r)) for r in langok]
    for r in kept:
        pen = (trackers or {}).get(r.get("tracker") or "unknown", 0.0)
        if pen:
            r["_score"] -= pen
            r["_tracker_penalty"] = pen
    kept.sort(key=lambda x: (x["_score"], x.get("seeders", 0)), reverse=True)
    return kept


_hunt_lock = threading.Lock()
_run_lock = threading.Lock()


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _hunt_candidates(series_filter=None) -> list[dict]:
    """The wants a hunt may act on: status 'wanted', belonging to a title that
    is still monitored (and not paused), each annotated with its episode's
    air date. Ordered so the most promising searches happen first while the
    per-tick search budget lasts: never-searched before backed-off, then the
    most recently aired (new episodes are the ones with live releases)."""
    from . import series as series_mod
    wanted = series_mod.list_wanted("wanted")
    with db.connect() as c:
        live_series = {r["id"] for r in c.execute(
            "SELECT id FROM series WHERE monitored=1 AND "
            "COALESCE(monitor_mode, 'all') != 'none'")}
        live_movies = {r["id"] for r in c.execute(
            "SELECT id FROM movies WHERE monitored=1")}
        air = {(r["series_id"], r["season"], r["episode"]): r["air_date"] or ""
               for r in c.execute("SELECT series_id, season, episode, air_date "
                                  "FROM series_episodes")}
    out = []
    for w in wanted:
        kind = w.get("kind", "episode")
        sid = w.get("series_id")
        if series_filter is not None and (kind == "movie" or sid != series_filter):
            continue
        if kind == "movie":
            if sid not in live_movies:
                continue
            # movies have no air date to rank by and are few; keep them at the
            # front of their search-count tier rather than starved behind a
            # large episode backlog
            w["_air"] = ""
            w["_rank_date"] = "9999-12-31"
        else:
            if sid not in live_series:
                continue
            w["_air"] = air.get((sid, w.get("season"), w.get("episode")), "")
        out.append(w)
    out.sort(key=lambda w: ((w.get("search_count") or 0) > 0,
                            _neg_date(w.get("_rank_date") or w["_air"])))
    return out


def _neg_date(d: str) -> int:
    """Sort key that puts later dates first; undated last."""
    try:
        return -int(d.replace("-", "")[:8])
    except ValueError:
        return 0


def _is_recent(air_date: str) -> bool:
    if not air_date:
        return False
    from datetime import date, timedelta
    days = _int_env("WANT_RECENT_DAYS", 14)
    return air_date >= (date.today() - timedelta(days=days)).isoformat()


def hunt_wanted(series_filter=None, max_override=None) -> dict:
    """Search for and grab wanted items (missing + upgrades), within caps so an
    unattended run can't flood the client or the indexer:
      HUNT_MAX_ACTIVE     don't grab if this many torrents are already downloading
      HUNT_MAX_PER_RUN    grab at most this many per cycle
      HUNT_MAX_SEARCHES   indexer queries per cycle (default 40)
    Wants whose last searches found nothing are backed off (faucet.wants) and
    skipped until due. Remaining wants stay 'wanted' for the next tick.
    series_filter: only hunt that series id — a manual "hunt now", which
    ignores backoff. max_override: override the per-run grab cap."""
    if not _hunt_lock.acquire(blocking=False):
        log.info("Hunt skipped: another hunt is already running.")
        return {"wanted": 0, "grabbed": 0, "details": [],
                "skipped_reason": "a hunt is already running"}
    try:
        return _hunt_wanted(series_filter, max_override)
    finally:
        _hunt_lock.release()


def _hunt_wanted(series_filter=None, max_override=None) -> dict:
    from . import series as series_mod
    from . import wants
    db.init()

    if not (_cfg().jackett_url and _cfg().jackett_api_key):
        # every search would fail the same way; say so once instead of
        # silently failing hundreds of wants
        log.warning("Hunt skipped: indexer not configured (Jackett URL/API key "
                    "missing — Settings → Connections).")
        return {"wanted": 0, "grabbed": 0, "details": [],
                "skipped_reason": "indexer not configured"}

    manual = series_filter is not None
    max_active = _int_env("HUNT_MAX_ACTIVE", 5)
    max_per_run = (max_override if max_override is not None
                   else _int_env("HUNT_MAX_PER_RUN", 3))
    max_searches = _int_env("HUNT_MAX_SEARCHES", 40)

    # how many torrents are already downloading right now?
    active = 0
    try:
        client0 = make_client(_cfg().client_kind, _cfg().client_url,
                             _cfg().client_user, _cfg().client_pass, _cfg().request_timeout)
        active = sum(1 for t in client0.list_transfers()
                     if getattr(t, "status", "") == "downloading")
    except Exception:                            # noqa: BLE001
        active = 0

    wanted = _hunt_candidates(series_filter)
    grabbed = 0
    searches = 0
    deferred = 0
    details = []
    if active >= max_active:
        log.info("Hunt skipped: %d already downloading (cap %d).", active, max_active)
        return {"wanted": len(wanted), "grabbed": 0, "details": [],
                "skipped_reason": f"{active} active >= cap {max_active}"}

    budget = min(max_per_run, max_active - active)
    now = datetime.now()
    trackers = _tracker_penalties()
    try:
        huntlog.prune()
    except Exception:                            # noqa: BLE001
        pass

    def due(w):
        return manual or wants.is_due(w, now)

    # ── Season-pack pre-pass ──
    # Group MISSING episode-wants by (series, season); when a season is
    # (almost) entirely missing, grab one season pack instead of N episodes.
    # Two guards keep packs from re-downloading content already on disk:
    #   * only reason='missing' counts — quality-UPGRADE wants never justify a
    #     pack (a 720p library with a 1080p profile used to re-download every
    #     season it already owned)
    #   * ownership check — a pack is only grabbed when at most ~10% of the
    #     season's aired episodes are on disk; owning 8/10 and grabbing a
    #     14 GB pack for the other 2 re-downloads the 8.
    # A season is only searched for a pack when at least one of its wants is
    # due, so a fruitless pack search backs off with its episodes.
    covered_episode_wants = set()        # ids of wanted rows a pack will cover
    if budget > 0:
        from collections import defaultdict
        from . import library as _lib
        by_season = defaultdict(list)
        for w in wanted:
            if w.get("kind", "episode") == "movie":
                continue
            if w.get("series_id") is None or w.get("season") is None:
                continue
            if (w.get("reason") or "missing") != "missing":
                continue                     # upgrades hunt per-episode only
            if not _episode_eligible(w):
                continue                     # aired too recently (AIR_DELAY_DAYS)
            # drop stale wants for episodes already on disk (see per-episode
            # ownership check below) so they can't inflate a season into
            # pack-worthiness
            if (w.get("series_title") and w.get("episode") is not None
                    and _lib.have_episode(w["series_title"], int(w["season"]),
                                          int(w["episode"]))):
                with db.connect() as c:
                    c.execute("DELETE FROM wanted WHERE id=?", (w["id"],))
                w["_gone"] = True
                continue
            by_season[(w["series_id"], w["season"])].append(w)
        cutoff = series_mod.hunt_cutoff()
        for (sid, season), eps in by_season.items():
            if grabbed >= budget or searches >= max_searches:
                break
            if len(eps) < 2:                 # need 2+ missing to prefer a pack
                continue
            if not any(due(w) for w in eps):
                continue
            title = eps[0].get("series_title") or ""
            if not title:
                continue
            # ownership guard: how many episodes of this season have aired?
            with db.connect() as c:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM series_episodes WHERE series_id=? "
                    "AND season=? AND air_date != '' AND air_date <= ?",
                    (sid, season, cutoff)).fetchone()
            aired = row["n"] if row else 0
            owned = max(0, aired - len(eps))
            if aired <= 0 or owned > max(1, aired // 10):
                continue                     # own too much of it — episode grabs instead
            searches += 1
            _last_pack.clear()
            pack = _try_season_pack(title, season, _load_profile_for_series(sid))
            pack_key = {"kind": "pack", "series_id": sid, "season": season,
                        "query": f"{title} S{int(season):02d}"}
            if not pack:
                huntlog.record(**pack_key, outcome="error" if _last_pack.get("error") else "miss",
                               results=_last_pack.get("results", 0),
                               reasons=_last_pack.get("reasons"),
                               detail=_last_pack.get("error", ""))
                continue
            # grab the pack
            if not db.mark_grabbed(pack["title"], None):
                continue
            try:
                client = make_client(_cfg().client_kind, _cfg().client_url,
                                     _cfg().client_user, _cfg().client_pass, _cfg().request_timeout)
                client.add(pack["href"], _cfg().download_dir or None)
                grabbed += 1
                db.add_history("added", pack["title"], f"season pack: {title} S{int(season):02d} ({len(eps)} eps)")
                # mark every wanted episode in this season as grabbed-by-pack
                wants.mark_grabbed([w["id"] for w in eps])
                covered_episode_wants.update(w["id"] for w in eps)
                details.append({"want": f"{title} S{int(season):02d} (pack)",
                                "reason": "pack", "grabbed": pack["title"], "error": None,
                                "covers": len(eps)})
                log.info("Grabbed season pack '%s' covering %d episodes", pack["title"], len(eps))
                huntlog.record(**pack_key, outcome="grabbed",
                               results=_last_pack.get("results", 0), kept=1,
                               reasons=_last_pack.get("reasons"), pick=pack,
                               detail=f"covers {len(eps)} episode(s)")
                _notify_grab(pack["title"], f"{title} S{int(season):02d} pack", series_id=sid)
            except DownloadClientError as e:
                with db.connect() as c:
                    c.execute("DELETE FROM grabbed WHERE title=?", (pack["title"],))
                details.append({"want": f"{title} S{int(season):02d} (pack)",
                                "reason": "pack", "grabbed": None, "error": f"add failed: {e}"})
                _grab_failed(pack["title"], f"{title} S{int(season):02d} pack", e)

    search_errors: list[str] = []
    for w in wanted:
        if grabbed >= budget:
            break
        if w.get("id") in covered_episode_wants or w.get("_gone"):
            continue
        if not due(w):
            deferred += 1
            continue
        if searches >= max_searches:
            deferred += 1
            continue
        kind = w.get("kind", "episode")
        movie_title, movie_year = None, None
        owned = None
        if kind == "movie":
            query = w.get("title") or ""
            series_id = w.get("series_id")  # for movies this is the movie id
            profile = _load_profile_for_movie(series_id)
            with db.connect() as c:
                mrow = c.execute("SELECT title, year, status FROM movies WHERE id=?",
                                 (series_id,)).fetchone()
            if mrow:
                movie_title, movie_year = mrow["title"], mrow["year"]
                # last-second ownership check: a stale want (mount blip, stall
                # flip, old DB row) must never re-download something on disk —
                # EXCEPT quality upgrades, which by definition target an owned
                # movie
                if mrow["status"] == "have" and w.get("reason") != "upgrade":
                    with db.connect() as c:
                        c.execute("DELETE FROM wanted WHERE id=?", (w["id"],))
                    continue
        else:
            title = w.get("series_title") or ""
            season, episode = w.get("season"), w.get("episode")
            if not title or season is None or episode is None:
                continue
            if not _episode_eligible(w):
                continue                     # aired too recently (AIR_DELAY_DAYS)
            # last-second ownership check against the live library inventory —
            # the wanted table can be stale (a want flipped back by the stall
            # handler, or rows created during a NAS-mount hiccup). Grabbing is
            # expensive; a SELECT is not.
            from . import library as _lib
            owned = _lib.have_episode(title, int(season), int(episode))
            if owned and w.get("reason") != "upgrade":
                with db.connect() as c:
                    c.execute("DELETE FROM wanted WHERE id=?", (w["id"],))
                continue
            query = f"{title} S{int(season):02d}E{int(episode):02d}"
            profile = _load_profile_for_series(w.get("series_id"))
        if not query:
            continue
        res = {"want": query, "reason": w.get("reason"), "grabbed": None, "error": None}
        log_key = {"kind": kind, "series_id": w.get("series_id"),
                   "season": None if kind == "movie" else season,
                   "episode": None if kind == "movie" else episode, "query": query}
        searches += 1
        try:
            results = searchmod.search(
                _cfg().jackett_url, _cfg().jackett_api_key, _cfg().jackett_indexer,
                query, "all", _cfg().search_limit, _cfg().request_timeout)
        except searchmod.SearchError as e:
            # transient (indexer down / timed out): no backoff, retry next tick
            res["error"] = f"search failed: {e}"
            details.append(res)
            search_errors.append(str(e))
            huntlog.record(**log_key, outcome="error", detail=str(e))
            continue

        reasons: dict = {}

        def keep(rows, bucket, pred, reasons=reasons):
            out = [r for r in rows if pred(r)]
            if len(out) != len(rows):
                reasons[bucket] = reasons.get(bucket, 0) + len(rows) - len(out)
            return out

        fresh = keep(results, "blocklisted", lambda r: not db.already_grabbed(r["title"]))
        # Verify every candidate actually IS the wanted title. Indexer search is
        # substring-based, so "Trailer Park Boys S01E01" also returns
        # "Trailer Park Boys: Out of the Park: Europe S01E01" — without this
        # check the best-seeded spin-off wins and the wrong show is grabbed.
        from . import releasematch as _rm
        if kind == "movie":
            if movie_title:
                fresh = keep(fresh, "wrong_title", lambda r, t=movie_title, y=movie_year: _rm.matches_movie(
                    r["title"], t, y))
        else:
            # For episode wants, never grab a season or series pack here — the
            # pack pre-pass already handled pack-worthy seasons, and an episode
            # search can return huge multi-season packs (e.g. "S01-S21") that
            # contain the query episode. Grabbing those re-downloads seasons we
            # already own.
            from . import packs as _packs
            fresh = keep(fresh, "pack", lambda r: _packs.classify_pack(
                r["title"]).get("kind") == "single")
            fresh = keep(fresh, "wrong_title", lambda r, t=title, s=int(season), e=int(episode): _rm.matches_episode(
                r["title"], t, s, e))
        if w.get("reason") == "upgrade":
            # An upgrade has to beat the file on disk. The profile alone isn't
            # enough: a ["1080p", "720p"] profile happily ranks a 720p release
            # for a 720p-owned movie, the sorter refuses it (not better), and
            # the want re-grabs another copy every GRAB_RETRY_HOURS.
            from . import quality as _q
            if kind == "movie":
                from . import movies as _mov
                is_owned, owned_q, owned_cam = _mov.owned_quality(series_id)
            else:
                from . import library as _lib
                is_owned = bool(owned)
                owned_q, owned_cam = _lib.owned_file_quality(
                    owned.get("path"), owned.get("quality")) if owned else (None, False)
            if is_owned:
                before = len(fresh)
                fresh = keep(fresh, "not_upgrade",
                             lambda r, q=owned_q, cam=owned_cam: _q.is_upgrade(r["title"], q, cam))
                if not fresh:
                    res["error"] = (f"no release better than owned "
                                    f"{owned_q or 'unknown'}{' CAM' if owned_cam else ''} "
                                    f"({before} candidates)")
        ranked = _rank_for_hunt(fresh, profile, reasons, trackers)
        if not ranked:
            res["next_search"] = wants.record_miss(
                w["id"], w.get("search_count") or 0, _is_recent(w.get("_air", "")))
            details.append(res)
            huntlog.record(**log_key, outcome="miss", results=len(results), kept=0,
                           reasons=reasons, detail=res.get("error") or "",
                           next_search=res["next_search"])
            continue

        pick = ranked[0]
        if not db.mark_grabbed(pick["title"], None):
            details.append(res)
            continue
        try:
            client = make_client(_cfg().client_kind, _cfg().client_url,
                                 _cfg().client_user, _cfg().client_pass, _cfg().request_timeout)
            client.add(pick["href"], _cfg().download_dir or None)
            res["grabbed"] = pick["title"]
            grabbed += 1
            db.add_history("added", pick["title"], f"hunt: {query} ({w.get('reason')})")
            wants.mark_grabbed(w["id"])
            log.info("Hunt grabbed '%s' for %s (%s seeders, %s)", pick["title"], query,
                     pick.get("seeders"), pick.get("tracker") or "?")
            huntlog.record(**log_key, outcome="grabbed", results=len(results),
                           kept=len(ranked), reasons=reasons, pick=pick)
            _notify_grab(pick["title"], query,
                         series_id=None if kind == "movie" else w.get("series_id"),
                         movie_id=w.get("series_id") if kind == "movie" else None)
        except DownloadClientError as e:
            res["error"] = f"add failed: {e}"
            with db.connect() as c:
                c.execute("DELETE FROM grabbed WHERE title=?", (pick["title"],))
            _grab_failed(pick["title"], query, e)
            huntlog.record(**log_key, outcome="add_failed", results=len(results),
                           kept=len(ranked), reasons=reasons, pick=pick, detail=str(e))
        details.append(res)
    if search_errors:
        log.warning("Hunt: %d search(es) failed this pass (first: %s)",
                    len(search_errors), search_errors[0])
    log.info("Hunt: %d candidate(s), %d search(es), %d grabbed, %d deferred",
             len(wanted), searches, grabbed, deferred)
    return {"wanted": len(wanted), "grabbed": grabbed, "details": details,
            "searches": searches, "deferred": deferred}


def _grab_failed(release: str, context: str, err: Exception) -> None:
    """A grab the client refused. It used to vanish without a trace (F33)."""
    log.warning("Hunt add failed for '%s' (%s): %s", release, context, err)
    try:
        db.add_history("grab_failed", release, f"{context}: {err}")
    except Exception:                            # noqa: BLE001
        pass


def _episode_eligible(w: dict) -> bool:
    """Has this episode want's air date passed AIR_DELAY_DAYS? A want created
    before the delay was configured (or before the episode's date moved)
    must not be hunted early either."""
    from . import series as series_mod
    with db.connect() as c:
        row = c.execute("SELECT air_date FROM series_episodes WHERE series_id=? "
                        "AND season=? AND episode=?",
                        (w.get("series_id"), w.get("season"), w.get("episode"))).fetchone()
    if row is None:
        return True                          # not in the canonical list: nothing to gate on
    return series_mod.hunt_eligible(row["air_date"])


def _notify_grab(release_title: str, context: str,
                 series_id=None, movie_id=None) -> None:
    """Poster-embedded grab notification (when 'added' is in NOTIFY_ON)."""
    if "added" not in _cfg().notify_on or not _cfg().notify_urls:
        return
    poster = None
    try:
        with db.connect() as c:
            if series_id:
                r = c.execute("SELECT poster FROM series WHERE id=?",
                              (series_id,)).fetchone()
            elif movie_id:
                r = c.execute("SELECT poster FROM movies WHERE id=?",
                              (movie_id,)).fetchone()
            else:
                r = None
        poster = r["poster"] if r else None
    except Exception:                            # noqa: BLE001
        poster = None
    try:
        from .notify import notify
        notify(_cfg().notify_urls, f"Grabbed · {context}", release_title, poster)
    except Exception:                            # noqa: BLE001
        pass


def _load_profile_for_movie(movie_id):
    if not movie_id:
        return _default_profile()
    with db.connect() as c:
        row = c.execute("SELECT profile_id FROM movies WHERE id=?", (movie_id,)).fetchone()
    p = _load_profile(row["profile_id"]) if row and row["profile_id"] else None
    return p or _default_profile()


def _default_profile():
    """The global fallback profile, applied to shows/movies that have no profile
    of their own. Set via the 'default_profile_id' setting. None if unset."""
    try:
        pid = db.get_setting("default_profile_id", None)
    except Exception:                            # noqa: BLE001
        pid = None
    return _load_profile(int(pid)) if pid else None


def _load_profile_for_series(series_id):
    if not series_id:
        return _default_profile()
    with db.connect() as c:
        row = c.execute("SELECT profile_id FROM series WHERE id=?", (series_id,)).fetchone()
    p = _load_profile(row["profile_id"]) if row and row["profile_id"] else None
    return p or _default_profile()


def run_once() -> dict:
    """One full cycle: check query-subscriptions, then run the library-aware
    pipeline (scan → refresh → reconcile → hunt).

    Serialized: the scheduler tick and the "check now" button both land here,
    and two overlapping cycles could grab two different releases for the
    same want. A second caller gets {"skipped_reason": ...} immediately."""
    if not _run_lock.acquire(blocking=False):
        log.info("Cycle skipped: another cycle is already running.")
        return {"checked": 0, "grabbed": 0, "details": [], "library": {},
                "stalls": {}, "skipped_reason": "a cycle is already running"}
    try:
        return _run_once()
    finally:
        _run_lock.release()


def _run_once() -> dict:
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

    # 1b. fake-release guard (also runs on its own fast loop)
    try:
        from . import safety
        safety.check_transfers()
    except Exception as e:                       # noqa: BLE001 - never kill the tick
        log.warning("Safety check error: %s", e)

    # 2. stalled-download handling: remove dead torrents FIRST so their client
    # slots are free for this same tick's hunt, and their wants re-queue now
    stall_summary = {}
    try:
        from . import stalls
        stall_summary = stalls.check()
        if stall_summary.get("stalled"):
            log.info("Stall check: removed %d stalled download(s), re-queued %d want(s)",
                     len(stall_summary["stalled"]), stall_summary.get("flipped", 0))
    except Exception as e:                       # noqa: BLE001 - never kill the tick
        log.warning("Stall check error: %s", e)
        stall_summary = {"error": str(e)}

    # 3. catch-up sweep: file anything sitting in complete/ that the hook
    # missed, BEFORE the scan so freshly-filed episodes count this tick
    if os.environ.get("SWEEP_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off"):
        try:
            from . import sweep as sweep_mod
            sw = sweep_mod.run()
            if sw.get("swept"):
                log.info("Sweep filed %d item(s) the hook missed.", sw["swept"])
        except Exception as e:                   # noqa: BLE001 - never kill the tick
            log.warning("Sweep error: %s", e)

    # 4. library-aware pipeline
    lib_summary = {}
    try:
        from . import library, series as series_mod, movies as movies_mod, wants
        library.scan()
        refresh = series_mod.refresh_stale()
        requeued = wants.requeue_stale_grabs(series_mod.GRAB_RETRY_HOURS)
        if requeued:
            log.info("Re-queued %d grab(s) older than %dh that never landed.",
                     requeued, series_mod.GRAB_RETRY_HOURS)
        recon = series_mod.reconcile_all()
        movies_mod.reconcile_all()
        hunt = hunt_wanted()
        grabbed += hunt.get("grabbed", 0)
        lib_summary = {"reconcile": recon, "hunt_grabbed": hunt.get("grabbed", 0),
                       "wanted": hunt.get("wanted", 0), "refreshed": refresh,
                       "requeued": requeued, "searches": hunt.get("searches", 0),
                       "deferred": hunt.get("deferred", 0)}
    except Exception as e:                       # noqa: BLE001 - never kill the tick
        log.warning("Library pipeline error: %s", e)
        lib_summary = {"error": str(e)}

    # 4b. indexer probe (daily by default) + telemetry retention
    try:
        from . import indexerhealth
        indexerhealth.prune()
        indexerhealth.probe_if_due()
    except Exception as e:                       # noqa: BLE001 - never kill the tick
        log.warning("Indexer probe error: %s", e)

    # 4c. database / file hygiene (TMDb cache, events rotation, VACUUM)
    try:
        from . import maintenance
        maintenance.run()
    except Exception as e:                       # noqa: BLE001 - never kill the tick
        log.warning("Maintenance error: %s", e)

    # 5. housekeeping: purge expired sessions + used/expired reset tokens
    # (they previously accumulated forever)
    try:
        now = datetime.now().isoformat(timespec="seconds")
        with db.connect() as c:
            c.execute("DELETE FROM sessions WHERE expires_ts IS NOT NULL AND expires_ts < ?",
                      (now,))
            c.execute("DELETE FROM reset_tokens WHERE used=1 OR "
                      "(expires_ts IS NOT NULL AND expires_ts < ?)", (now,))
    except Exception:                            # noqa: BLE001
        pass

    _last_run.update(ts=datetime.now().isoformat(timespec="seconds"),
                     checked=len(subs), grabbed=grabbed)
    return {"checked": len(subs), "grabbed": grabbed, "details": details,
            "library": lib_summary, "stalls": stall_summary}


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


GUARD_INTERVAL = int(os.environ.get("GUARD_INTERVAL_SECONDS", "60"))
_guard_task: asyncio.Task | None = None


async def _guard_loop():
    """Payload check on a short cadence: a 1 GB fake finishes in a couple of
    minutes, far inside the main tick interval."""
    from . import safety
    await asyncio.sleep(15)
    while True:
        try:
            await asyncio.to_thread(safety.check_transfers)
        except Exception as e:                       # noqa: BLE001 - never kill the loop
            log.warning("Safety guard error: %s", e)
        await asyncio.sleep(GUARD_INTERVAL)


def start():
    global _task, _guard_task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
    if GUARD_INTERVAL > 0 and (_guard_task is None or _guard_task.done()):
        _guard_task = asyncio.create_task(_guard_loop())
    return _task


def stop():
    global _task, _guard_task
    for t in (_task, _guard_task):
        if t and not t.done():
            t.cancel()
    _task = _guard_task = None

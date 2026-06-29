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


def run_once() -> dict:
    """Check all enabled subscriptions once. Synchronous; safe to call from an
    endpoint (the 'check now' button) or the loop."""
    db.init()
    subs = db.list_subscriptions(enabled_only=True)
    grabbed = 0
    details = []
    for sub in subs:
        r = check_subscription(sub)
        details.append(r)
        if r["grabbed"]:
            grabbed += 1
    _last_run.update(ts=datetime.now().isoformat(timespec="seconds"),
                     checked=len(subs), grabbed=grabbed)
    return {"checked": len(subs), "grabbed": grabbed, "details": details}


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

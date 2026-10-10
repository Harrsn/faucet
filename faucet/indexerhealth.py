"""Indexer health.

Three views of "is searching working":

* search telemetry — every Torznab query the app makes (hunter, interactive,
  manual) records its latency, result count, error, and which trackers the
  results came from. Kept INDEXER_STATS_DAYS (7).
* per-indexer probe — Jackett's aggregate 'all' endpoint waits for the
  slowest indexer, so one hung tracker turns every search into a 30s read
  timeout with no hint which one. The probe queries each configured indexer
  on its own, with its own timeout, and reports latency and errors. Runs on
  demand and every INDEXER_PROBE_HOURS (24; 0 = only on demand).
* grab outcomes per tracker — from the hunt log: how many of a tracker's
  releases stalled vs completed.
"""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from . import db

log = logging.getLogger("faucet.indexers")

PROBE_KEY = "_indexer_probe"


def _ensure(c) -> None:
    c.execute(
        "CREATE TABLE IF NOT EXISTS search_events ("
        "  id       INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ts       TEXT NOT NULL,"
        "  indexer  TEXT,"
        "  ok       INTEGER,"
        "  ms       INTEGER,"
        "  results  INTEGER,"
        "  error    TEXT,"
        "  trackers TEXT"                      # JSON {tracker: results}
        ")")
    c.execute("CREATE INDEX IF NOT EXISTS ix_search_events_ts ON search_events (ts)")


def record_search(indexer: str, ok: bool, ms: int, results: int = 0,
                  error: str = "", trackers: dict | None = None) -> None:
    """Called by faucet.search for every query. Never raises."""
    try:
        with db.connect() as c:
            _ensure(c)
            c.execute("INSERT INTO search_events (ts, indexer, ok, ms, results, error, trackers) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (datetime.now().isoformat(timespec="seconds"), indexer or "all",
                       1 if ok else 0, int(ms), int(results), (error or "")[:300],
                       json.dumps(trackers or {})))
    except Exception:                            # noqa: BLE001
        pass


def prune() -> None:
    try:
        days = max(1, int(os.environ.get("INDEXER_STATS_DAYS", "7")))
    except ValueError:
        days = 7
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as c:
        _ensure(c)
        c.execute("DELETE FROM search_events WHERE ts < ?", (cutoff,))


def _pct(sorted_vals: list[int], p: float) -> int | None:
    if not sorted_vals:
        return None
    k = min(len(sorted_vals) - 1, max(0, int(round(p * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def summary(hours: int = 24) -> dict:
    """Search telemetry over the last `hours`."""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with db.connect() as c:
        _ensure(c)
        rows = c.execute("SELECT ok, ms, results, error, trackers FROM search_events "
                         "WHERE ts >= ?", (cutoff,)).fetchall()
    ms = sorted(r["ms"] for r in rows if r["ok"])
    errors: dict[str, int] = {}
    trackers: dict[str, int] = {}
    for r in rows:
        if not r["ok"]:
            e = r["error"] or "error"
            key = ("timeout" if "timed out" in e.lower() or "timeout" in e.lower()
                   else e.split(":")[0][:80])
            errors[key] = errors.get(key, 0) + 1
        try:
            for t, n in json.loads(r["trackers"] or "{}").items():
                trackers[t] = trackers.get(t, 0) + int(n)
        except (ValueError, TypeError, AttributeError):
            pass
    return {
        "hours": hours, "searches": len(rows),
        "failed": sum(1 for r in rows if not r["ok"]),
        "empty": sum(1 for r in rows if r["ok"] and not r["results"]),
        "p50_ms": _pct(ms, 0.5), "p95_ms": _pct(ms, 0.95),
        "errors": errors,
        "trackers": dict(sorted(trackers.items(), key=lambda kv: -kv[1])),
    }


def probe(timeout: int | None = None) -> dict:
    """Query every configured Jackett indexer individually. Stored, and
    returned: {ts, indexers: [{id, name, ok, ms, results, error}]}."""
    from . import config as cfgmod
    from . import search as searchmod
    c = cfgmod.config
    timeout = timeout or min(20, max(5, c.request_timeout))
    out = {"ts": datetime.now().isoformat(timespec="seconds"), "indexers": []}
    if not (c.jackett_url and c.jackett_api_key):
        out["error"] = "indexer not configured"
        return out
    items = searchmod.indexers(c.jackett_url, c.jackett_api_key, timeout)
    if not items:
        out["error"] = "couldn't list indexers from Jackett"
        db.set_setting(PROBE_KEY, out)
        return out

    def one(ix):
        t0 = time.monotonic()
        try:
            res = searchmod.search(c.jackett_url, c.jackett_api_key, ix["id"], "the",
                                   "all", 5, timeout, record=False)
            return {**ix, "ok": True, "ms": int((time.monotonic() - t0) * 1000),
                    "results": len(res), "error": ""}
        except searchmod.SearchError as e:
            return {**ix, "ok": False, "ms": int((time.monotonic() - t0) * 1000),
                    "results": 0, "error": str(e)[:200]}

    with ThreadPoolExecutor(max_workers=4) as pool:
        out["indexers"] = list(pool.map(one, items))
    out["indexers"].sort(key=lambda x: (x["ok"], -x["ms"]))
    db.set_setting(PROBE_KEY, out)
    bad = [x["name"] for x in out["indexers"] if not x["ok"]]
    if bad:
        log.warning("Indexer probe: %d/%d failing: %s", len(bad), len(out["indexers"]),
                    ", ".join(bad))
    return out


def last_probe() -> dict | None:
    return db.get_setting(PROBE_KEY, None)


def probe_if_due() -> dict | None:
    try:
        hours = float(os.environ.get("INDEXER_PROBE_HOURS", "24"))
    except ValueError:
        hours = 24.0
    if hours <= 0:
        return None
    last = last_probe() or {}
    try:
        age = (datetime.now() - datetime.fromisoformat(last.get("ts", ""))).total_seconds()
    except ValueError:
        age = float("inf")
    if age < hours * 3600:
        return None
    return probe()


def report() -> dict:
    from . import huntlog
    return {"search": summary(24), "probe": last_probe(),
            "grabs": huntlog.tracker_outcomes()}

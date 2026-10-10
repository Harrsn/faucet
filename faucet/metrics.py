"""/metrics in the Prometheus text exposition format, hand-written (no client
library). Everything is read from the database and the last cached probe, so
a scrape never touches the indexer or the download client.

Auth: `Authorization: Bearer <METRICS_TOKEN>` when METRICS_TOKEN is set, or an
admin session. Without either the endpoint answers 404.
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta

from . import db


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class _Out:
    def __init__(self):
        self.lines: list[str] = []
        self._seen: set[str] = set()

    def metric(self, name: str, mtype: str, help_: str, samples) -> None:
        """samples: iterable of (labels_dict_or_None, value)."""
        if name not in self._seen:
            self.lines.append(f"# HELP {name} {help_}")
            self.lines.append(f"# TYPE {name} {mtype}")
            self._seen.add(name)
        for labels, value in samples:
            if value is None:
                continue
            lab = ""
            if labels:
                lab = "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items()) + "}"
            self.lines.append(f"{name}{lab} {float(value):g}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def render() -> str:
    o = _Out()
    from . import __version__
    o.metric("faucet_info", "gauge", "Build information.", [({"version": __version__}, 1)])

    day = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    with db.connect() as c:
        o.metric("faucet_wanted", "gauge", "Wanted items by status and reason.",
                 [({"status": r["status"], "reason": r["reason"] or ""}, r["n"])
                  for r in c.execute("SELECT status, reason, COUNT(*) AS n FROM wanted "
                                     "GROUP BY status, reason")])
        o.metric("faucet_series", "gauge", "Monitored-series rows.",
                 [(None, c.execute("SELECT COUNT(*) FROM series").fetchone()[0])])
        o.metric("faucet_movies", "gauge", "Monitored-movie rows by status.",
                 [({"status": r["status"]}, r["n"]) for r in c.execute(
                     "SELECT status, COUNT(*) AS n FROM movies GROUP BY status")])
        o.metric("faucet_library_files", "gauge", "Files the scanner knows about.",
                 [({"kind": "episode"}, c.execute(
                     "SELECT COUNT(DISTINCT path) FROM library_episodes").fetchone()[0]),
                  ({"kind": "movie"}, c.execute(
                      "SELECT COUNT(*) FROM library_movies").fetchone()[0])])
        o.metric("faucet_history_events_24h", "gauge",
                 "History events in the last 24h (added, completed, stalled, ...).",
                 [({"event": r["event"]}, r["n"]) for r in c.execute(
                     "SELECT event, COUNT(*) AS n FROM history WHERE ts >= ? "
                     "GROUP BY event", (day,))])
        o.metric("faucet_requests", "gauge", "User requests by status.",
                 [({"status": r["status"]}, r["n"]) for r in c.execute(
                     "SELECT status, COUNT(*) AS n FROM requests GROUP BY status")])
        o.metric("faucet_users", "gauge", "User accounts by status.",
                 [({"status": r["status"]}, r["n"]) for r in c.execute(
                     "SELECT status, COUNT(*) AS n FROM users GROUP BY status")])
        try:
            o.metric("faucet_hunt_attempts_24h", "gauge", "Hunt attempts in the last 24h by outcome.",
                     [({"outcome": r["outcome"]}, r["n"]) for r in c.execute(
                         "SELECT outcome, COUNT(*) AS n FROM hunt_log WHERE ts >= ? "
                         "GROUP BY outcome", (day,))])
        except Exception:                        # noqa: BLE001 - table appears on first hunt
            pass

    from . import indexerhealth
    s = indexerhealth.summary(24)
    o.metric("faucet_searches_24h", "gauge", "Indexer searches in the last 24h by result.",
             [({"result": "ok"}, s["searches"] - s["failed"] - s["empty"]),
              ({"result": "empty"}, s["empty"]), ({"result": "failed"}, s["failed"])])
    o.metric("faucet_search_latency_ms", "gauge", "Search latency over 24h.",
             [({"quantile": "0.5"}, s["p50_ms"]), ({"quantile": "0.95"}, s["p95_ms"])])
    p = indexerhealth.last_probe() or {}
    ixs = p.get("indexers") or []
    o.metric("faucet_indexer_up", "gauge", "Last probe result per indexer (1 = ok).",
             [({"indexer": x.get("name") or x.get("id")}, 1 if x.get("ok") else 0) for x in ixs])
    o.metric("faucet_indexer_probe_ms", "gauge", "Last probe latency per indexer.",
             [({"indexer": x.get("name") or x.get("id")}, x.get("ms")) for x in ixs])
    if p.get("ts"):
        try:
            o.metric("faucet_indexer_probe_timestamp_seconds", "gauge", "When the last probe ran.",
                     [(None, datetime.fromisoformat(p["ts"]).timestamp())])
        except ValueError:
            pass

    from . import scheduler
    lr = scheduler.last_run()
    if lr.get("ts"):
        o.metric("faucet_last_cycle_timestamp_seconds", "gauge",
                 "When the scheduler cycle last finished.",
                 [(None, datetime.fromisoformat(lr["ts"]).timestamp())])

    from . import config as cfgmod
    for label, path in (("downloads", cfgmod.config.disk_path),
                        ("library", os.environ.get("LIBRARY_ROOT", "/library"))):
        try:
            du = shutil.disk_usage(path)
        except OSError:
            continue
        o.metric("faucet_disk_free_bytes", "gauge", "Free bytes on a mounted path.",
                 [({"path": label}, du.free)])
        o.metric("faucet_disk_total_bytes", "gauge", "Total bytes on a mounted path.",
                 [({"path": label}, du.total)])
    try:
        o.metric("faucet_db_bytes", "gauge", "SQLite database file size.",
                 [(None, db._db_path().stat().st_size)])
    except OSError:
        pass
    o.metric("faucet_scrape_timestamp_seconds", "gauge", "Time of this scrape.",
             [(None, time.time())])
    return o.text()


def authorized(request) -> bool | None:
    """True = serve, False = 401, None = 404 (endpoint not enabled)."""
    import secrets
    token = os.environ.get("METRICS_TOKEN", "")
    hdr = request.headers.get("authorization", "")
    if token and hdr.lower().startswith("bearer "):
        return secrets.compare_digest(hdr[7:].strip(), token)
    from . import auth
    user = auth.session_user(request.cookies.get(auth.SESSION_COOKIE))
    if user and user.get("role") == "admin":
        return True
    return False if token else None

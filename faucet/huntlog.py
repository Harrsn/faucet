"""Hunt log — what the hunter did for each want, and why.

Before this, a want that never got grabbed was a black box: was it searched,
did the indexer return anything, and if so why was every result thrown away
(wrong show, failed the profile, not an upgrade, already tried)? Every hunt
attempt now leaves one row here, surfaced on the show/movie pages and in the
interactive-search modal.

Env:
  HUNT_LOG_DAYS  how long entries are kept (default 14)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from . import db

# Rejection buckets, in the order the hunter applies its filters. The keys are
# stable (the UI maps them to labels); profile reasons are refined below.
REASONS = ("blocklisted", "wrong_title", "pack", "not_upgrade", "seeders", "size",
           "resolution", "cam", "language", "format", "profile")


def _ensure(c) -> None:
    c.execute(
        "CREATE TABLE IF NOT EXISTS hunt_log ("
        "  id        INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ts        TEXT NOT NULL,"
        "  kind      TEXT,"                 # episode | movie | pack
        "  series_id INTEGER,"              # movie id for movies (as in `wanted`)
        "  season    INTEGER,"
        "  episode   INTEGER,"
        "  query     TEXT,"
        "  outcome   TEXT,"                 # grabbed | miss | error | add_failed
        "  results   INTEGER DEFAULT 0,"    # what the indexer returned
        "  kept      INTEGER DEFAULT 0,"    # what survived every filter
        "  reasons   TEXT,"                 # JSON {bucket: count}
        "  release   TEXT,"                 # grabbed release
        "  tracker   TEXT,"
        "  seeders   INTEGER,"
        "  size      INTEGER,"
        "  detail    TEXT,"                 # error text / extra note
        "  next_search TEXT"
        ")")
    c.execute("CREATE INDEX IF NOT EXISTS ix_hunt_log_item ON hunt_log "
              "(kind, series_id, season, episode, id)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_hunt_log_ts ON hunt_log (ts)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_hunt_log_release ON hunt_log (release)")


def profile_bucket(why: str) -> str:
    """Map a profiles.passes() reason string to a bucket."""
    w = (why or "").lower()
    for key, bucket in (("seeders", "seeders"), ("size", "size"),
                        ("resolution", "resolution"), ("cam", "cam"),
                        ("language", "language"), ("format", "format")):
        if w.startswith(key):
            return bucket
    return "profile"


def record(*, kind: str, series_id, season=None, episode=None, query: str = "",
           outcome: str, results: int = 0, kept: int = 0, reasons: dict | None = None,
           pick: dict | None = None, detail: str = "", next_search: str | None = None) -> None:
    pick = pick or {}
    try:
        with db.connect() as c:
            _ensure(c)
            c.execute(
                "INSERT INTO hunt_log (ts, kind, series_id, season, episode, query, outcome, "
                "results, kept, reasons, release, tracker, seeders, size, detail, next_search) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), kind, series_id, season,
                 episode, query, outcome, int(results or 0), int(kept or 0),
                 json.dumps({k: v for k, v in (reasons or {}).items() if v}),
                 pick.get("title"), pick.get("tracker"), pick.get("seeders"),
                 pick.get("size"), (detail or "")[:500], next_search))
    except Exception:                            # noqa: BLE001 - logging must never break a hunt
        pass


def prune() -> int:
    try:
        days = max(1, int(os.environ.get("HUNT_LOG_DAYS", "14")))
    except ValueError:
        days = 14
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as c:
        _ensure(c)
        return c.execute("DELETE FROM hunt_log WHERE ts < ?", (cutoff,)).rowcount


def entries(*, kind: str | None = None, series_id=None, season=None, episode=None,
            limit: int = 100) -> list[dict]:
    """Newest first. kind='episode' with series_id returns the show's episode
    AND season-pack attempts."""
    where, args = [], []
    if kind == "episode":
        where.append("kind IN ('episode','pack')")
    elif kind:
        where.append("kind=?")
        args.append(kind)
    for col, val in (("series_id", series_id), ("season", season), ("episode", episode)):
        if val is not None:
            where.append(f"{col}=?")
            args.append(val)
    q = "SELECT * FROM hunt_log"
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    with db.connect() as c:
        _ensure(c)
        rows = c.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["reasons"] = json.loads(d.get("reasons") or "{}")
        except ValueError:
            d["reasons"] = {}
        out.append(d)
    return out


def tracker_outcomes(days: int = 14) -> dict[str, dict]:
    """Per tracker: how many hunter grabs in the window, and how many of
    those releases later stalled or completed (matched through history by
    release name)."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    out: dict[str, dict] = {}
    with db.connect() as c:
        _ensure(c)
        rows = c.execute(
            "SELECT h.tracker, h.release, "
            "  EXISTS(SELECT 1 FROM history x WHERE x.title=h.release AND x.event='stalled') AS stalled, "
            "  EXISTS(SELECT 1 FROM history x WHERE x.title=h.release "
            "         AND x.event IN ('completed','sorted')) AS completed "
            "FROM hunt_log h WHERE h.outcome='grabbed' AND h.ts >= ? "
            "AND h.release IS NOT NULL", (cutoff,)).fetchall()
    for r in rows:
        t = r["tracker"] or "unknown"
        d = out.setdefault(t, {"grabs": 0, "stalled": 0, "completed": 0})
        d["grabs"] += 1
        d["stalled"] += 1 if r["stalled"] else 0
        d["completed"] += 1 if r["completed"] else 0
    for d in out.values():
        settled = d["stalled"] + d["completed"]
        d["stall_rate"] = round(d["stalled"] / settled, 2) if settled else None
    return out

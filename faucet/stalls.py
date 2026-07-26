"""Stalled-download handling (the Sonarr 'failed download handling' role).

A torrent whose swarm died sits in the client forever — often at 99% missing
one piece — eating a HUNT_MAX_ACTIVE slot while its episode never arrives.
Each scheduler tick this module snapshots every downloading transfer's
progress; a transfer that has made NO progress for STALL_HOURS is declared
stalled and handled:

  1. removed from the client (with its partial data),
  2. its release stays in the `grabbed` table, so the SAME release is never
     picked again (the blocklist),
  3. every `wanted` row it was grabbed for flips back to 'wanted' — the very
     next hunt pass grabs a DIFFERENT release immediately, instead of waiting
     out GRAB_RETRY_HOURS,
  4. a 'stalled' history event is recorded (and notified when 'failed' is in
     NOTIFY_ON).

Mapping a removed release back to its wants uses the same strict title
verification the hunter uses (releasematch), so a stalled manual grab that
matches no monitored title is simply removed and logged.

Env:
  STALL_HOURS   hours with zero progress before a download is stalled (default 4)
  STALL_ACTION  'remove' (default) or 'flag' (log/notify only, touch nothing)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from . import db
from .config import config
from .clients import make_client, DownloadClientError

log = logging.getLogger("faucet.stalls")

STALL_HOURS = float(os.environ.get("STALL_HOURS", "4"))
STALL_ACTION = os.environ.get("STALL_ACTION", "remove").strip().lower()


def _now() -> datetime:
    return datetime.now()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _ensure_table(c) -> None:
    c.execute(
        "CREATE TABLE IF NOT EXISTS transfer_progress ("
        "  id          TEXT PRIMARY KEY,"     # client transfer id/hash
        "  name        TEXT,"
        "  percent     REAL DEFAULT 0,"
        "  last_change TEXT"                  # last time percent moved
        ")")


def _flip_wants_for_release(name: str) -> int:
    """Flip the wanted rows this release was grabbed for back to 'wanted'.
    Returns how many rows were flipped."""
    from . import packs, releasematch
    flipped = 0
    cls = packs.classify_pack(name)
    with db.connect() as c:
        series = c.execute("SELECT id, title FROM series").fetchall()
        for s in series:
            if not releasematch.matches_series(name, s["title"]):
                continue
            if cls["kind"] == "single":
                import re
                m = re.search(r"(?i)s(\d{1,2})\s*[.\s]?e(\d{1,3})", name) \
                    or re.search(r"\b(\d{1,2})x(\d{2,3})\b", name)
                if not m:
                    continue
                cur = c.execute(
                    "UPDATE wanted SET status='wanted' WHERE kind='episode' "
                    "AND series_id=? AND season=? AND episode=? AND status='grabbed'",
                    (s["id"], int(m.group(1)), int(m.group(2))))
                flipped += cur.rowcount
            elif cls["kind"] == "season" and cls["season"] is not None:
                cur = c.execute(
                    "UPDATE wanted SET status='wanted' WHERE kind='episode' "
                    "AND series_id=? AND season=? AND status='grabbed'",
                    (s["id"], cls["season"]))
                flipped += cur.rowcount
            if flipped:
                return flipped
        movies = c.execute("SELECT id, title, year FROM movies").fetchall()
        for m_ in movies:
            if not releasematch.matches_movie(name, m_["title"], m_["year"]):
                continue
            cur = c.execute(
                "UPDATE wanted SET status='wanted' WHERE kind='movie' "
                "AND series_id=? AND status='grabbed'", (m_["id"],))
            flipped += cur.rowcount
            if flipped:
                return flipped
    return flipped


def check() -> dict:
    """One stall pass. Called from the scheduler tick BEFORE hunting, so a
    removed corpse frees its client slot for the same tick's re-hunt."""
    result = {"checked": 0, "stalled": [], "flipped": 0, "errors": []}
    try:
        client = make_client(config.client_kind, config.client_url,
                             config.client_user, config.client_pass,
                             config.request_timeout)
        transfers = client.list_transfers()
    except Exception as e:                             # noqa: BLE001
        result["errors"].append(f"client unreachable: {e}")
        return result

    now = _now()
    active_ids = set()
    stalled: list = []
    with db.connect() as c:
        _ensure_table(c)
        for t in transfers:
            # only in-flight downloads can stall; seeding/stopped/queued are fine
            if t.status != "downloading" or t.percent >= 100:
                continue
            result["checked"] += 1
            active_ids.add(str(t.id))
            row = c.execute("SELECT percent, last_change FROM transfer_progress "
                            "WHERE id=?", (str(t.id),)).fetchone()
            if row is None or abs((row["percent"] or 0) - t.percent) > 0.05:
                # first sighting, or progress moved — (re)start the clock
                c.execute(
                    "INSERT INTO transfer_progress (id, name, percent, last_change) "
                    "VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "name=excluded.name, percent=excluded.percent, "
                    "last_change=excluded.last_change",
                    (str(t.id), t.name, t.percent, _iso(now)))
                continue
            # no progress since last sighting — stalled long enough?
            try:
                since = datetime.fromisoformat(row["last_change"])
            except (TypeError, ValueError):
                since = now
            hours = (now - since).total_seconds() / 3600.0
            if hours >= STALL_HOURS:
                stalled.append(t)
        # drop snapshots for transfers that no longer exist / finished
        for r in c.execute("SELECT id FROM transfer_progress").fetchall():
            if r["id"] not in active_ids:
                c.execute("DELETE FROM transfer_progress WHERE id=?", (r["id"],))

    for t in stalled:
        entry = {"id": t.id, "name": t.name, "percent": t.percent}
        if STALL_ACTION == "flag":
            log.warning("STALLED (flag only): %s at %.1f%%", t.name, t.percent)
            db.add_history("stalled", t.name,
                           f"no progress for {STALL_HOURS:g}h at {t.percent:.1f}% (flagged)")
            result["stalled"].append(entry)
            continue
        try:
            client.remove(t.id, delete_data=True)
        except DownloadClientError as e:
            result["errors"].append(f"remove failed for {t.name}: {e}")
            continue
        with db.connect() as c:
            c.execute("DELETE FROM transfer_progress WHERE id=?", (str(t.id),))
        flipped = _flip_wants_for_release(t.name or "")
        entry["flipped"] = flipped
        result["flipped"] += flipped
        result["stalled"].append(entry)
        db.add_history("stalled", t.name,
                       f"removed after {STALL_HOURS:g}h without progress at "
                       f"{t.percent:.1f}%; {flipped} want(s) re-queued")
        log.warning("STALLED: removed '%s' (%.1f%%), re-queued %d want(s)",
                    t.name, t.percent, flipped)
        if "failed" in config.notify_on:
            try:
                from .notify import notify
                notify(config.notify_urls, "Stalled download removed",
                       f"{t.name} ({t.percent:.1f}%) — will retry a different release")
            except Exception:                        # noqa: BLE001
                pass
    return result

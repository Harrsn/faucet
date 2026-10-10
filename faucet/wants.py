"""The `wanted` table's write path, shared by series and movie reconcile.

One row per wanted item, keyed on (kind, series_id, season, episode). For
movies `series_id` holds the movie id and season/episode are NULL, so the key
is enforced by the `ux_wanted_key` expression index (db.py) rather than the
table's UNIQUE constraint: SQLite treats NULLs in a UNIQUE constraint as
distinct, which let two concurrent reconciles insert the same movie twice.

`upsert` is atomic across threads and processes: BEGIN IMMEDIATE takes the
write lock before the existence check, so select-then-insert can't race.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import db


def upsert(kind: str, series_id: int, season: int | None, episode: int | None,
           title: str, reason: str, retry_hours: float) -> str:
    """Create or refresh one want. Returns what happened:
    'inserted' | 'updated' | 'requeued' (a stale grab flipped back to wanted)
    | 'kept' (an in-flight grab, or a status this doesn't manage).

    A 'grabbed' row whose last_search is older than `retry_hours` means the
    download never landed; it goes back to 'wanted' so it retries."""
    title = title or ""
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "INSERT OR IGNORE INTO wanted (kind, series_id, season, episode, title, reason, status) "
            "VALUES (?,?,?,?,?,?,'wanted')",
            (kind, series_id, season, episode, title, reason))
        if c.execute("SELECT changes() AS n").fetchone()["n"]:
            return "inserted"
        row = c.execute(
            "SELECT id, status, last_search FROM wanted WHERE kind=? AND series_id=? "
            "AND season IS ? AND episode IS ? ORDER BY id LIMIT 1",
            (kind, series_id, season, episode)).fetchone()
        if row is None:                           # pragma: no cover - index guarantees a row
            return "kept"
        if row["status"] == "wanted":
            c.execute("UPDATE wanted SET reason=?, title=? WHERE id=?",
                      (reason, title, row["id"]))
            return "updated"
        if row["status"] == "grabbed":
            retry_before = (datetime.now() - timedelta(hours=retry_hours)
                            ).isoformat(timespec="seconds")
            if not row["last_search"] or row["last_search"] < retry_before:
                c.execute("UPDATE wanted SET status='wanted', reason=?, title=?, "
                          "search_count=0, next_search=NULL WHERE id=?",
                          (reason, title, row["id"]))
                return "requeued"
        return "kept"


# ── hunt backoff ─────────────────────────────────────────────────────────────
#
# Every 'wanted' row used to be searched on every scheduler tick until the
# per-run grab cap was hit. With a large backlog that mostly has no releases
# (old seasons nobody seeds), that meant hundreds of indexer queries every
# 30 minutes for results that can't change — slow ticks, and a good way to get
# throttled or banned by trackers. A fruitless search now pushes the want's
# next_search out exponentially, capped lower for recently aired episodes
# (releases are still appearing) than for backlog.

def _hours_env(key: str, default: float) -> float:
    import os
    try:
        return max(0.0, float(os.environ.get(key, str(default))))
    except ValueError:
        return default


def backoff_hours(search_count: int, recent: bool) -> float:
    """Delay before the next search after `search_count` fruitless ones:
    1h, 2h, 4h, ... capped at WANT_BACKOFF_RECENT_HOURS (default 6) for
    recent episodes and WANT_BACKOFF_MAX_HOURS (default 72) otherwise."""
    cap = (_hours_env("WANT_BACKOFF_RECENT_HOURS", 6) if recent
           else _hours_env("WANT_BACKOFF_MAX_HOURS", 72))
    n = max(1, int(search_count))
    return min(float(2 ** min(n - 1, 16)), cap)


def is_due(want: dict, now: datetime | None = None) -> bool:
    ns = want.get("next_search")
    if not ns:
        return True
    return ns <= (now or datetime.now()).isoformat(timespec="seconds")


def record_miss(want_id: int, search_count: int, recent: bool) -> str:
    """A search found nothing grabbable: count it and schedule the next try.
    Returns the next_search timestamp."""
    n = (search_count or 0) + 1
    now = datetime.now()
    nxt = (now + timedelta(hours=backoff_hours(n, recent))).isoformat(timespec="seconds")
    with db.connect() as c:
        c.execute("UPDATE wanted SET search_count=?, next_search=?, last_search=? "
                  "WHERE id=? AND status='wanted'",
                  (n, nxt, now.isoformat(timespec="seconds"), want_id))
    return nxt


def mark_grabbed(want_ids, when: str | None = None) -> None:
    """Flip wants to 'grabbed' and reset their backoff, so if the download
    fails and the want is re-queued it is searched immediately."""
    when = when or datetime.now().isoformat(timespec="seconds")
    ids = [want_ids] if isinstance(want_ids, int) else list(want_ids)
    with db.connect() as c:
        for wid in ids:
            c.execute("UPDATE wanted SET status='grabbed', last_search=?, "
                      "search_count=0, next_search=NULL WHERE id=?", (when, wid))


def requeue(where_sql: str, params: tuple) -> int:
    """Flip matching 'grabbed' wants back to 'wanted', due immediately."""
    with db.connect() as c:
        return c.execute(
            "UPDATE wanted SET status='wanted', search_count=0, next_search=NULL "
            f"WHERE status='grabbed' AND {where_sql}", params).rowcount


def requeue_stale_grabs(retry_hours: float) -> int:
    """Re-queue every 'grabbed' want older than `retry_hours`.

    upsert() already does this, but only for wants reconcile still visits —
    a grabbed want whose episode reconcile skips (paused 'future' backlog,
    a re-dated episode) sat in 'grabbed' forever. This sweep doesn't depend
    on reconcile; anything actually on disk is cleared by the reconcile that
    follows it."""
    cutoff = (datetime.now() - timedelta(hours=retry_hours)).isoformat(timespec="seconds")
    return requeue("(last_search IS NULL OR last_search < ?)", (cutoff,))

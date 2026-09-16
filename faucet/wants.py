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
                c.execute("UPDATE wanted SET status='wanted', reason=?, title=? WHERE id=?",
                          (reason, title, row["id"]))
                return "requeued"
        return "kept"

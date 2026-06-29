"""Request / approval flow (Overseerr-style).

Regular users browse the library and request new titles; admins (or trusted
auto-approve users) turn requests into monitored shows/movies that the hunter
then fills. Requests carry per-user attribution but feed one shared library.

States: pending -> approved -> fulfilled, or pending -> declined.
- pending:   awaiting an admin decision
- approved:  accepted; the title has been added to the monitored library
- declined:  rejected by an admin
- fulfilled: (optional later) everything requested is now on disk

Auto-approve: a user with can_autoapprove=1 skips the queue — their request is
approved and added immediately.
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import db

log = logging.getLogger("faucet.requests")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _existing_library_match(media_type: str, tmdb_id: int) -> dict | None:
    """Is this title already monitored? Returns {kind, id} or None."""
    with db.connect() as c:
        if media_type == "tv":
            r = c.execute("SELECT id FROM series WHERE tmdb_id=?", (tmdb_id,)).fetchone()
            if r:
                return {"kind": "series", "id": r["id"]}
        else:
            r = c.execute("SELECT id FROM movies WHERE tmdb_id=?", (tmdb_id,)).fetchone()
            if r:
                return {"kind": "movie", "id": r["id"]}
    return None


def create_request(user: dict, media_type: str, tmdb_id: int, title: str,
                   year: int | None = None, poster: str | None = None,
                   profile_id: int | None = None) -> dict:
    """Create a request for a user. Auto-approves if the user is trusted.
    Returns {status, request_id, auto_approved, already_*}."""
    media_type = "tv" if media_type in ("tv", "series") else "movie"

    # already in the library?
    match = _existing_library_match(media_type, tmdb_id)
    if match:
        return {"status": "already_available", "kind": match["kind"], "id": match["id"]}

    # already requested (and not declined)?
    with db.connect() as c:
        dup = c.execute(
            "SELECT id, status FROM requests WHERE tmdb_id=? AND media_type=? "
            "AND status IN ('pending','approved','fulfilled')",
            (tmdb_id, media_type)).fetchone()
        if dup:
            return {"status": "already_requested", "request_id": dup["id"],
                    "request_status": dup["status"]}

    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO requests (ts, user, user_id, media_type, tmdb_id, title, "
            "year, poster, status) VALUES (?,?,?,?,?,?,?,?,'pending')",
            (_now(), user.get("username"), user["id"], media_type, tmdb_id,
             title, year, poster))
        req_id = cur.lastrowid

    # trusted users skip the queue
    if user.get("can_autoapprove"):
        approve_request(req_id, decider=user, profile_id=profile_id)
        return {"status": "auto_approved", "request_id": req_id, "auto_approved": True}

    return {"status": "pending", "request_id": req_id, "auto_approved": False}


def approve_request(req_id: int, decider: dict, profile_id: int | None = None) -> dict:
    """Approve a request: add the title to the monitored library and mark the
    request approved. Idempotent-ish (re-approving an approved request re-adds
    only if missing)."""
    with db.connect() as c:
        r = c.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
        if not r:
            return {"status": "not_found"}
        r = dict(r)
    if r["status"] == "declined":
        return {"status": "declined"}

    link = {}
    try:
        if r["media_type"] == "tv":
            from . import series as series_mod
            sid = series_mod.add_series(r["tmdb_id"], r["title"], r.get("year"),
                                        r.get("poster"), profile_id)
            series_mod.reconcile(sid)
            link = {"series_id": sid}
        else:
            from . import movies as movies_mod
            mid = movies_mod.add_movie(r["tmdb_id"], r["title"], r.get("year"),
                                       r.get("poster"), profile_id)
            link = {"movie_id": mid}
    except Exception as e:                       # noqa: BLE001
        log.warning("approve add failed for request %s: %s", req_id, e)
        return {"status": "add_failed", "error": str(e)}

    with db.connect() as c:
        c.execute(
            "UPDATE requests SET status='approved', decided_by=?, decided_ts=?, "
            "series_id=?, movie_id=? WHERE id=?",
            (decider["id"], _now(), link.get("series_id"), link.get("movie_id"), req_id))
    return {"status": "approved", **link}


def decline_request(req_id: int, decider: dict) -> dict:
    with db.connect() as c:
        r = c.execute("SELECT status FROM requests WHERE id=?", (req_id,)).fetchone()
        if not r:
            return {"status": "not_found"}
        c.execute(
            "UPDATE requests SET status='declined', decided_by=?, decided_ts=? WHERE id=?",
            (decider["id"], _now(), req_id))
    return {"status": "declined"}


def delete_request(req_id: int) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM requests WHERE id=?", (req_id,))


def list_requests(user: dict, scope: str = "auto") -> list[dict]:
    """List requests. Admins see all; regular users see only their own. scope
    'mine' forces own-only even for admins (for a 'My Requests' view)."""
    own_only = scope == "mine" or user.get("role") != "admin"
    with db.connect() as c:
        if own_only:
            rows = c.execute(
                "SELECT * FROM requests WHERE user_id=? ORDER BY id DESC",
                (user["id"],)).fetchall()
        else:
            rows = c.execute("SELECT * FROM requests ORDER BY "
                             "CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC").fetchall()
    return [dict(r) for r in rows]


def pending_count() -> int:
    with db.connect() as c:
        return c.execute(
            "SELECT COUNT(*) AS n FROM requests WHERE status='pending'").fetchone()["n"]

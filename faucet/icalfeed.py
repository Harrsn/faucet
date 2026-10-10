"""Private iCalendar feed of a user's upcoming (and recent) episodes.

Each user can create a secret link — https://<host>/cal/<token>.ics — to
subscribe to in a phone or desktop calendar. Only a SHA-256 of the token is
stored, so the link is shown once; regenerating it revokes the old one. The
feed honours the user's library access, and stops working if the account is
disabled.

TMDb gives air dates, not air times, so episodes are all-day events.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import date, datetime, timedelta, timezone

from . import db

PAST_DAYS = 14
AHEAD_DAYS = 90


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def ensure_schema(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS calendar_feeds ("
              "  user_id    INTEGER PRIMARY KEY,"
              "  token_hash TEXT NOT NULL UNIQUE,"
              "  created_ts TEXT,"
              "  last_used  TEXT)")


def create(user_id: int) -> str:
    """A new token for the user (replacing any old one). Returned once."""
    token = secrets.token_urlsafe(24)
    with db.connect() as c:
        ensure_schema(c)
        c.execute("INSERT INTO calendar_feeds (user_id, token_hash, created_ts) VALUES (?,?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET token_hash=excluded.token_hash, "
                  "created_ts=excluded.created_ts, last_used=NULL",
                  (user_id, _hash(token), datetime.now().isoformat(timespec="seconds")))
    return token


def revoke(user_id: int) -> None:
    with db.connect() as c:
        ensure_schema(c)
        c.execute("DELETE FROM calendar_feeds WHERE user_id=?", (user_id,))


def status(user_id: int) -> dict | None:
    with db.connect() as c:
        ensure_schema(c)
        r = c.execute("SELECT created_ts, last_used FROM calendar_feeds WHERE user_id=?",
                      (user_id,)).fetchone()
    return dict(r) if r else None


def user_for(token: str) -> dict | None:
    """The active user a token belongs to, or None."""
    if not token or len(token) > 200:
        return None
    with db.connect() as c:
        ensure_schema(c)
        r = c.execute("SELECT u.* FROM calendar_feeds f JOIN users u ON u.id=f.user_id "
                      "WHERE f.token_hash=? AND u.status='active'", (_hash(token),)).fetchone()
        if r:
            c.execute("UPDATE calendar_feeds SET last_used=? WHERE user_id=?",
                      (datetime.now().isoformat(timespec="seconds"), r["id"]))
    return dict(r) if r else None


# ── rendering ───────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (str(s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", ""))


def _fold(line: str) -> str:
    """RFC 5545: lines over 75 octets continue on the next line after a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def episodes_for(user: dict) -> list[dict]:
    from . import access
    from .library import owned_index, owned_for
    lo = (date.today() - timedelta(days=PAST_DAYS)).isoformat()
    hi = (date.today() + timedelta(days=AHEAD_DAYS)).isoformat()
    visible = access.visible_series_ids(user)
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT e.season, e.episode, e.title AS ep_title, e.air_date, s.id AS series_id, "
            "s.title FROM series_episodes e JOIN series s ON s.id=e.series_id "
            "WHERE s.monitored=1 AND e.season > 0 AND e.air_date >= ? AND e.air_date <= ? "
            "ORDER BY e.air_date, s.title, e.season, e.episode", (lo, hi))]
        idx = owned_index(c)
    out = []
    for r in rows:
        if visible is not None and r["series_id"] not in visible:
            continue
        r["owned"] = (r["season"], r["episode"]) in owned_for(idx, r["series_id"], r["title"])
        out.append(r)
    return out


def render(user: dict, base_url: str, host: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    today = date.today().isoformat()
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Faucet//Episode calendar//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Faucet",
             "X-WR-CALDESC:Episodes of the shows Faucet tracks",
             "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    for e in episodes_for(user):
        d = date.fromisoformat(e["air_date"])
        code = f"S{e['season']:02d}E{e['episode']:02d}"
        state = ("on disk" if e["owned"] else "upcoming" if e["air_date"] > today
                 else "not downloaded yet")
        lines += [
            "BEGIN:VEVENT",
            f"UID:faucet-{e['series_id']}-{e['season']}-{e['episode']}@{_esc(host)}",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_esc(e['title'])} · {code}",
            f"DESCRIPTION:{_esc((e['ep_title'] or code) + ' — ' + state)}",
            f"URL:{base_url}/#/shows/{e['series_id']}/{e['season']}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"

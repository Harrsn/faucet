"""Grab history and the blocklist, put together from what Faucet already
records:

  history   'added' for every grab (automatic, season pack, manual), and what
            happened after: completed / sorted / stalled / removed / failed /
            suspicious / grab_failed
  hunt_log  for automatic grabs: how many releases were found, how many
            survived the filters, what was rejected and why, tracker, seeders
  grabbed   every release ever grabbed; the hunter never picks one again —
            that's the blocklist (a stalled or fake release stays dead)

Release names are matched loosely (case and punctuation ignored), because
the client sometimes reports a torrent under a slightly different name than
the indexer listed it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from . import db

DONE = ("completed", "sorted")
PROBLEMS = ("stalled", "failed", "suspicious", "grab_failed")
AFTER = DONE + PROBLEMS + ("removed",)


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _state(events: list[dict]) -> dict:
    """What became of a grab, from its later events (oldest first)."""
    kinds = {e["event"] for e in events}
    last = events[-1] if events else None
    if kinds & set(DONE):
        state = "done"
    elif kinds & set(PROBLEMS):
        state = "problem"
    elif "removed" in kinds:
        state = "removed"
    else:
        state = "pending"
    return {"state": state, "event": last["event"] if last else None,
            "event_ts": last["ts"] if last else None, "detail": last["detail"] if last else None}


def _events_by_release(c, names: set[str], since: str | None = None) -> dict:
    """{normalised release: [events oldest→newest]} for the AFTER events."""
    if not names:
        return {}
    q = ("SELECT ts, event, title, detail FROM history WHERE event IN (%s)" % ",".join("?" * len(AFTER)))
    args: list = list(AFTER)
    if since:
        q += " AND ts >= ?"
        args.append(since)
    out: dict = {}
    for r in c.execute(q + " ORDER BY ts", args):
        k = _norm(r["title"])
        if k in names:
            out.setdefault(k, []).append(dict(r))
    return out


def _titles(c) -> tuple[dict, dict]:
    shows = {r["id"]: r["title"] for r in c.execute("SELECT id, title FROM series")}
    films = {r["id"]: (r["title"], r["year"]) for r in c.execute("SELECT id, title, year FROM movies")}
    return shows, films


_SXE = re.compile(r"(?i)\bS(\d{1,2})(?:\s*E(\d{1,3}))?\b")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def label_from_release(name: str | None) -> str | None:
    """'Impractical.Jokers.S13E02.1080p.WEB' → 'Impractical Jokers S13E02', for
    grabs the hunt log doesn't describe (manual ones)."""
    clean = re.sub(r"[._]+", " ", name or "").strip()
    m = _SXE.search(clean)
    if m:
        code = f"S{int(m.group(1)):02d}" + (f"E{int(m.group(2)):02d}" if m.group(2) else "")
        head = clean[:m.start()].strip(" -[]()")
        return f"{head} {code}".strip() or None
    y = _YEAR.search(clean)
    if y and y.start() > 0:
        return f"{clean[:y.start()].strip(' -[]()')} ({y.group(0)})"
    return None


def grabs(days: int = 30, limit: int = 300, q: str = "") -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as c:
        added = [dict(r) for r in c.execute(
            "SELECT id, ts, title, detail, size FROM history WHERE event='added' AND ts >= ? "
            "ORDER BY ts DESC, id DESC LIMIT ?", (since, limit * 2 if q else limit))]
        if q:
            ql = q.lower()
            added = [a for a in added if ql in (a["title"] or "").lower()
                     or ql in (a["detail"] or "").lower()][:limit]
        names = {_norm(a["title"]) for a in added}
        events = _events_by_release(c, names, since)
        hunts: dict = {}
        rels = [a["title"] for a in added if a["title"]]
        for i in range(0, len(rels), 400):
            chunk = rels[i:i + 400]
            try:
                for r in c.execute("SELECT * FROM hunt_log WHERE outcome='grabbed' AND release IN (%s) "
                                   "ORDER BY id" % ",".join("?" * len(chunk)), chunk):
                    hunts[r["release"]] = dict(r)
            except Exception:                    # noqa: BLE001 - no hunt log yet
                break
        shows, films = _titles(c)
    out = []
    for a in added:
        h = hunts.get(a["title"])
        detail = a["detail"] or ""
        item = {"id": a["id"], "ts": a["ts"], "release": a["title"],
                "size": (h or {}).get("size") or a["size"] or None,
                "how": ("manual" if detail.startswith("manual") or detail == "added to client"
                        else "pack" if detail.startswith("season pack") else "auto"),
                "note": detail, "tracker": (h or {}).get("tracker"),
                "seeders": (h or {}).get("seeders"), "link": None,
                "label": label_from_release(a["title"]), "why": None,
                **_state([e for e in events.get(_norm(a["title"]), []) if e["ts"] >= a["ts"]])}
        if h:
            kind, sid = h["kind"], h["series_id"]
            if kind == "movie" and sid in films:
                t, y = films[sid]
                item["label"] = f"{t} ({y})" if y else t
                item["link"] = {"kind": "movie", "id": sid}
            elif sid in shows:
                code = (f"S{int(h['season']):02d}" + (f"E{int(h['episode']):02d}" if h["episode"] is not None
                                                      and kind != "pack" else "")) if h["season"] is not None else ""
                item["label"] = f"{shows[sid]} {code}".strip()
                item["link"] = {"kind": "show", "id": sid, "season": h["season"]}
            try:
                reasons = json.loads(h["reasons"] or "{}")
            except ValueError:
                reasons = {}
            item["why"] = {"found": h["results"], "kept": h["kept"], "rejected": reasons}
        out.append(item)
    return out


def blocklist(problems_only: bool = True, q: str = "", limit: int = 500) -> dict:
    with db.connect() as c:
        rows = [dict(r) for r in c.execute("SELECT id, ts, title FROM grabbed ORDER BY ts DESC, id DESC")]
        if q:
            rows = [r for r in rows if q.lower() in (r["title"] or "").lower()]
        events = _events_by_release(c, {_norm(r["title"]) for r in rows})
    items, counts = [], {"all": len(rows), "problems": 0}
    for r in rows:
        st = _state(events.get(_norm(r["title"]), []))
        if st["state"] == "problem":
            counts["problems"] += 1
        if problems_only and st["state"] != "problem":
            continue
        items.append({"id": r["id"], "ts": r["ts"], "release": r["title"], **st})
    return {"items": items[:limit], "counts": counts, "truncated": len(items) > limit}


def allow(entry_id: int) -> str | None:
    """Take a release off the blocklist so the hunter may pick it again."""
    with db.connect() as c:
        r = c.execute("SELECT title FROM grabbed WHERE id=?", (entry_id,)).fetchone()
        if not r:
            return None
        c.execute("DELETE FROM grabbed WHERE id=?", (entry_id,))
    return r["title"]

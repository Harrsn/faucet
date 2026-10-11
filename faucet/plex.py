"""Plex: tell it the moment a download is filed, so it shows up in Plex in
seconds rather than at the next scheduled scan (Plex can't watch a network
share for changes, so on a NAS that scan is the only way new files appear).

  * Connection: PLEX_URL + PLEX_TOKEN in the settings store (the token is
    write-only, like the other keys).
  * Pairing: each Faucet library is paired with a Plex library and the path
    Plex sees it at — Faucet's /nas/media/tvshows may be Plex's
    /data/tvshows. Paired automatically by the longest matching tail of the
    folder path (tvshows ↔ tvshows, Mamaw/Shows ↔ Mamaw/Shows), or by hand.
  * Scans: after the sorter files something, the hook and the sweep ask
    Plex to scan only the folders that changed
    (/library/sections/<id>/refresh?path=…). A failed scan never fails the
    sort; it's logged and shown in Settings.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from urllib.parse import quote

from . import db

log = logging.getLogger("faucet.plex")

TIMEOUT = 10
_KIND = {"tv": "show", "movie": "movie"}


class PlexError(Exception):
    pass


def settings() -> tuple[str, str]:
    return (os.environ.get("PLEX_URL", "").strip().rstrip("/"),
            os.environ.get("PLEX_TOKEN", "").strip())


def enabled() -> bool:
    url, token = settings()
    return bool(url and token)


def _get(path: str, url: str | None = None, token: str | None = None, timeout: int = TIMEOUT) -> dict:
    import requests
    u, t = settings()
    url, token = (url or u).rstrip("/"), token or t
    if not url or not token:
        raise PlexError("Plex isn't set up")
    try:
        r = requests.get(url + path, headers={"X-Plex-Token": token, "Accept": "application/json"},
                         timeout=timeout)
    except requests.RequestException as e:
        raise PlexError(f"can't reach Plex at {url}: {type(e).__name__}") from None
    if r.status_code == 401:
        raise PlexError("Plex refused the token (401)")
    if r.status_code >= 400:
        raise PlexError(f"Plex answered {r.status_code}")
    try:
        return r.json().get("MediaContainer", {}) if r.content else {}
    except ValueError:
        return {}


def ensure_schema(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS plex_links ("
              "  library_id   INTEGER PRIMARY KEY,"
              "  section_key  TEXT,"                  # NULL = deliberately not paired
              "  section_title TEXT,"
              "  plex_path    TEXT,"
              "  auto         INTEGER NOT NULL DEFAULT 1)")
    c.execute("CREATE TABLE IF NOT EXISTS plex_scans ("
              "  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, section TEXT,"
              "  path TEXT, ok INTEGER, error TEXT)")


# ── server ──────────────────────────────────────────────────────────────────

def sections(url: str | None = None, token: str | None = None) -> list[dict]:
    mc = _get("/library/sections", url, token)
    out = []
    for d in mc.get("Directory", []) or []:
        out.append({"key": str(d.get("key")), "title": d.get("title"), "type": d.get("type"),
                    "locations": [loc.get("path") for loc in d.get("Location", []) or [] if loc.get("path")]})
    return out


def test(url: str | None = None, token: str | None = None) -> dict:
    """Server name, version and libraries — or why not."""
    try:
        ident = _get("/", url, token)
        return {"ok": True, "name": ident.get("friendlyName") or "Plex",
                "version": ident.get("version"), "sections": sections(url, token)}
    except PlexError as e:
        return {"ok": False, "msg": str(e)}


# ── pairing ─────────────────────────────────────────────────────────────────

def _score(a: str, b: str) -> tuple[int, int, int]:
    """How well two folder paths match, best first: trailing components in
    common (case-insensitive, for Windows Plex servers), how many of those
    match exactly in case, then the shallower path. So …/movies prefers
    /data/movies over /data/Mamaw/Movies, while …/Mamaw/Movies prefers
    /data/Mamaw/Movies (two components in common beats one)."""
    pa = [p for p in a.rstrip("/\\").replace("\\", "/").split("/") if p]
    pb = [p for p in b.rstrip("/\\").replace("\\", "/").split("/") if p]
    n = exact = 0
    while n < min(len(pa), len(pb)) and pa[-1 - n].lower() == pb[-1 - n].lower():
        exact += pa[-1 - n] == pb[-1 - n]
        n += 1
    return n, exact, -len(pb)


def suggest(lib: dict, secs: list[dict]) -> dict | None:
    """The Plex library and location that best match a Faucet library: of the
    right kind, best score, and unambiguous (a true tie pairs nothing)."""
    ranked = []
    for s in secs:
        if s["type"] != _KIND.get(lib["kind"]):
            continue
        for loc in s["locations"]:
            sc = _score(lib["path"], loc)
            if sc[0]:
                ranked.append((sc, {"key": s["key"], "title": s["title"], "path": loc}))
    if not ranked:
        return None
    ranked.sort(key=lambda x: x[0], reverse=True)
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0] and ranked[0][1]["path"] != ranked[1][1]["path"]:
        return None
    return ranked[0][1]


def links() -> dict[int, dict]:
    with db.connect() as c:
        ensure_schema(c)
        return {r["library_id"]: dict(r) for r in c.execute("SELECT * FROM plex_links")}


def auto_pair(secs: list[dict]) -> int:
    """Pair every library that hasn't been paired (or unpaired) by hand."""
    from . import libraries
    current = links()
    n = 0
    with db.connect() as c:
        ensure_schema(c)
        for lib in libraries.all_libraries():
            have = current.get(lib["id"])
            if have and not have["auto"]:
                continue
            s = suggest(lib, secs)
            if s and (not have or have["section_key"] != s["key"] or have["plex_path"] != s["path"]):
                c.execute("INSERT INTO plex_links (library_id, section_key, section_title, plex_path, auto) "
                          "VALUES (?,?,?,?,1) ON CONFLICT(library_id) DO UPDATE SET section_key=excluded.section_key, "
                          "section_title=excluded.section_title, plex_path=excluded.plex_path, auto=1",
                          (lib["id"], s["key"], s["title"], s["path"]))
                n += 1
    return n


def set_link(library_id: int, section_key: str | None, plex_path: str | None, secs: list[dict]) -> None:
    """Pair by hand (section + the path Plex uses), or None to not pair."""
    title = None
    if section_key is not None:
        s = next((x for x in secs if x["key"] == str(section_key)), None)
        if not s:
            raise ValueError("That Plex library doesn't exist.")
        title = s["title"]
        plex_path = (plex_path or (s["locations"][0] if s["locations"] else "")).strip()
        if not plex_path.startswith("/") and ":" not in plex_path[:3]:
            raise ValueError("The Plex path should be the folder as Plex sees it, e.g. /data/tvshows.")
    with db.connect() as c:
        ensure_schema(c)
        c.execute("INSERT INTO plex_links (library_id, section_key, section_title, plex_path, auto) "
                  "VALUES (?,?,?,?,0) ON CONFLICT(library_id) DO UPDATE SET section_key=excluded.section_key, "
                  "section_title=excluded.section_title, plex_path=excluded.plex_path, auto=0",
                  (library_id, section_key, title, plex_path if section_key else None))


def to_plex(path: str) -> tuple[str, str] | None:
    """(section key, the folder as Plex sees it) for a Faucet path."""
    from . import libraries
    lib = libraries.containing(path)
    if not lib:
        return None
    link = links().get(lib["id"])
    if not link or not link["section_key"] or not link["plex_path"]:
        return None
    rel = os.path.relpath(path, lib["path"])
    sep = "\\" if "\\" in link["plex_path"] else "/"
    target = link["plex_path"].rstrip("/\\")
    if rel != ".":
        target = target + sep + rel.replace("/", sep)
    return link["section_key"], target


# ── scanning ────────────────────────────────────────────────────────────────

def _record(section: str, path: str, ok: bool, error: str | None) -> None:
    try:
        with db.connect() as c:
            ensure_schema(c)
            c.execute("INSERT INTO plex_scans (ts, section, path, ok, error) VALUES (?,?,?,?,?)",
                      (datetime.now().isoformat(timespec="seconds"), section, path, 1 if ok else 0, error))
            c.execute("DELETE FROM plex_scans WHERE id NOT IN (SELECT id FROM plex_scans ORDER BY id DESC LIMIT 100)")
    except Exception:                            # noqa: BLE001 - bookkeeping never matters more than the sort
        pass


def scan_folder(section: str, plex_path: str | None) -> bool:
    q = f"?path={quote(plex_path, safe='')}" if plex_path else ""
    try:
        _get(f"/library/sections/{quote(str(section), safe='')}/refresh{q}")
        _record(section, plex_path or "(whole library)", True, None)
        return True
    except PlexError as e:
        log.warning("Plex scan of %s failed: %s", plex_path or f"section {section}", e)
        _record(section, plex_path or "(whole library)", False, str(e))
        return False


def scan_files(files: list[str]) -> int:
    """Ask Plex to scan the folders these just-filed files are in (each
    folder once). Returns how many scans Plex accepted. Never raises."""
    if not enabled() or not files:
        return 0
    seen, done = set(), 0
    for f in files:
        folder = os.path.dirname(f.rstrip("/")) or f
        target = to_plex(folder)
        if not target or target in seen:
            continue
        seen.add(target)
        done += scan_folder(*target)
    return done


def recent_scans(limit: int = 10) -> list[dict]:
    with db.connect() as c:
        ensure_schema(c)
        return [dict(r) for r in c.execute("SELECT * FROM plex_scans ORDER BY id DESC LIMIT ?", (limit,))]


def placed_log() -> str:
    """A temp file the sorter appends each filed path to (FAUCET_PLACED_LOG)."""
    import tempfile
    fd, p = tempfile.mkstemp(prefix="faucet-placed-", suffix=".txt")
    os.close(fd)
    return p


def scan_from_log(log_path: str) -> int:
    try:
        with open(log_path) as f:
            files = [line.strip() for line in f if line.strip()]
    except OSError:
        files = []
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass
    try:
        return scan_files(files)
    except Exception as e:                       # noqa: BLE001 - never fail a sort over Plex
        log.warning("Plex scan after sorting failed: %s", e)
        return 0



# ── watched status ──────────────────────────────────────────────────────────
#
# Read from Plex (for the account the token belongs to) into plex_watched,
# keyed by TMDb id so it lines up with Faucet's shows and movies whatever the
# folders are called. Refreshed every scheduler tick and on demand.

PAGE = 2000


def ensure_watched_schema(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS plex_watched ("
              "  kind TEXT NOT NULL,"                  # episode | movie
              "  tmdb_id INTEGER NOT NULL,"
              "  season INTEGER NOT NULL DEFAULT -1,"
              "  episode INTEGER NOT NULL DEFAULT -1,"
              "  views INTEGER NOT NULL DEFAULT 0,"
              "  last_viewed TEXT,"
              "  PRIMARY KEY (kind, tmdb_id, season, episode))")


def _tmdb(item: dict) -> int | None:
    for g in item.get("Guid") or []:
        gid = str(g.get("id") or "")
        if gid.startswith("tmdb://"):
            try:
                return int(gid[7:])
            except ValueError:
                return None
    return None


def _all(section: str, kind: int) -> list[dict]:
    """Every item of one type in a section, page by page."""
    out, start = [], 0
    while True:
        mc = _get(f"/library/sections/{quote(section, safe='')}/all?type={kind}&includeGuids=1"
                  f"&X-Plex-Container-Start={start}&X-Plex-Container-Size={PAGE}", timeout=60)
        items = mc.get("Metadata") or []
        out += items
        total = int(mc.get("totalSize") or mc.get("size") or len(items))
        start += len(items)
        if not items or start >= total:
            return out


def _when(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def sync_watched() -> dict:
    """Pull watched episodes and movies from every paired Plex library."""
    if not enabled():
        return {"ok": False, "msg": "Plex isn't set up"}
    secs = {s["key"]: s for s in sections()}
    paired = {lk["section_key"] for lk in links().values() if lk.get("section_key")}
    rows, unmatched = [], 0
    for key in paired:
        s = secs.get(key)
        if not s:
            continue
        if s["type"] == "show":
            shows = {str(x["ratingKey"]): _tmdb(x) for x in _all(key, 2)}
            for ep in _all(key, 4):
                if not ep.get("viewCount"):
                    continue
                tid = shows.get(str(ep.get("grandparentRatingKey")))
                if tid is None or ep.get("parentIndex") is None or ep.get("index") is None:
                    unmatched += 1
                    continue
                rows.append(("episode", tid, int(ep["parentIndex"]), int(ep["index"]),
                             int(ep["viewCount"]), _when(ep.get("lastViewedAt"))))
        elif s["type"] == "movie":
            for mv in _all(key, 1):
                if not mv.get("viewCount"):
                    continue
                tid = _tmdb(mv)
                if tid is None:
                    unmatched += 1
                    continue
                rows.append(("movie", tid, -1, -1, int(mv["viewCount"]), _when(mv.get("lastViewedAt"))))
    with db.connect() as c:
        ensure_watched_schema(c)
        c.execute("DELETE FROM plex_watched")          # mirror Plex: un-watching counts too
        c.executemany("INSERT OR REPLACE INTO plex_watched (kind, tmdb_id, season, episode, views, last_viewed) "
                      "VALUES (?,?,?,?,?,?)", rows)
    db.set_setting("_plex_watched_sync", datetime.now().isoformat(timespec="seconds"))
    return {"ok": True, "episodes": sum(r[0] == "episode" for r in rows),
            "movies": sum(r[0] == "movie" for r in rows), "unmatched": unmatched}


def watched_episodes(tmdb_id: int) -> dict[tuple[int, int], dict]:
    with db.connect() as c:
        ensure_watched_schema(c)
        return {(r["season"], r["episode"]): {"views": r["views"], "last_viewed": r["last_viewed"]}
                for r in c.execute("SELECT season, episode, views, last_viewed FROM plex_watched "
                                   "WHERE kind='episode' AND tmdb_id=?", (tmdb_id,))}


def watched_movie(tmdb_id: int) -> dict | None:
    with db.connect() as c:
        ensure_watched_schema(c)
        r = c.execute("SELECT views, last_viewed FROM plex_watched WHERE kind='movie' AND tmdb_id=?",
                      (tmdb_id,)).fetchone()
    return dict(r) if r else None


def watched_status() -> dict:
    with db.connect() as c:
        ensure_watched_schema(c)
        n = dict(c.execute("SELECT SUM(kind='episode') AS episodes, SUM(kind='movie') AS movies "
                           "FROM plex_watched").fetchone())
    return {"last_sync": db.get_setting("_plex_watched_sync"),
            "episodes": n["episodes"] or 0, "movies": n["movies"] or 0}

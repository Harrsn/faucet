"""Cascade — self-hosted search → download → sort → manage for media.

One lightweight app over your indexer (Jackett/Prowlarr) and torrent client
(Transmission/qBittorrent/Deluge). This module exposes the HTTP API and serves
the single-page UI.
"""
from __future__ import annotations

import os
import json
import shutil
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as _cfg
from .clients import make_client, DownloadClientError
from . import search as searchmod
from . import tmdb as tmdbmod
from . import db
from .notify import notify

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("faucet")

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # start the background auto-grab scheduler
    try:
        from . import scheduler
        scheduler.start()
    except Exception as e:                       # noqa: BLE001 - never block startup
        log.warning("Scheduler failed to start: %s", e)
    yield
    try:
        from . import scheduler
        scheduler.stop()
    except Exception:                            # noqa: BLE001
        pass


app = FastAPI(title=_cfg.config.app_title, lifespan=lifespan)
STATIC = Path(__file__).parent / "static"

GB = 1024 ** 3

# initialize the database on import (idempotent)
try:
    db.init()
except Exception as e:                       # noqa: BLE001 - never block startup
    log.warning("DB init failed: %s", e)

# ── authentication wiring ──
from fastapi import Request as _Request
from fastapi.responses import JSONResponse as _JSONResponse
from . import auth as _auth
from . import auth_routes as _auth_routes

app.include_router(_auth_routes.router)

# Paths reachable without a session. Everything else requires login. This is
# fail-closed: any new route is protected by default unless added here.
_PUBLIC_PREFIXES = ("/static/", "/favicon", "/login", "/register", "/reset",
                    "/api/auth/", "/health")


def _is_public(path: str) -> bool:
    if path in ("/login", "/register", "/reset"):
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


@app.middleware("http")
async def _auth_gate(request: _Request, call_next):
    path = request.url.path
    # Before any user exists, allow through so the first-run admin setup works.
    try:
        no_users = _auth.user_count() == 0
    except Exception:                            # noqa: BLE001
        no_users = False
    if no_users or _is_public(path):
        return await call_next(request)
    user = _auth.session_user(request.cookies.get(_auth.SESSION_COOKIE))
    if not user:
        # API calls get 401 JSON; page loads redirect to the login screen.
        if path.startswith("/api/"):
            return _JSONResponse({"detail": "Authentication required."}, status_code=401)
        from fastapi.responses import RedirectResponse as _Redirect
        return _Redirect(url="/login", status_code=302)
    # admin-only: management endpoints (writes, search, torrents, settings).
    # Library *reads* (GET series/movies) are allowed for any logged-in user so
    # they can browse what's available; everything else stays admin-only.
    if _admin_only(path, request.method) and user.get("role") != "admin":
        if path.startswith("/api/"):
            return _JSONResponse({"detail": "Admin access required."}, status_code=403)
        from fastapi.responses import RedirectResponse as _Redirect
        return _Redirect(url="/", status_code=302)
    request.state.user = user
    return await call_next(request)


# Endpoints regular users may READ: browse the monitored library and search
# TMDb (to request). Safe, non-indexer, non-torrent reads.
_USER_READABLE = ("/api/series", "/api/movies", "/api/meta/")

# Always admin-only regardless of method — raw indexer search, torrent client,
# download management, settings, profiles, subscriptions, hunts, library ops.
_ADMIN_ALWAYS = (
    "/api/search", "/api/indexers", "/api/torrent", "/api/transfers",
    "/api/add", "/api/profiles", "/api/subscriptions", "/api/library",
    "/api/settings", "/api/setup", "/api/admin", "/api/stats", "/api/config",
    "/api/events", "/api/history", "/api/wanted",
)


def _admin_only(path: str, method: str) -> bool:
    # /api/requests is user-level (router enforces per-action perms).
    if path.startswith("/api/requests"):
        return False
    if any(path.startswith(p) for p in _ADMIN_ALWAYS):
        return True
    # series/movies/meta: GET is user-readable; writes are admin-only.
    if any(path.startswith(p) for p in _USER_READABLE):
        return method not in ("GET", "HEAD")
    # any other /api/ not explicitly public/readable: admin-only by default
    if path.startswith("/api/"):
        return True
    return False


def cfg():
    """Always return the live (possibly hot-reloaded) config object."""
    return _cfg.config


def client():
    """Build the configured download client per request (cheap, stateless)."""
    return make_client(cfg().client_kind, cfg().client_url,
                       cfg().client_user, cfg().client_pass, cfg().request_timeout)


# ----------------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------------
class AddRequest(BaseModel):
    magnet: str
    title: str | None = None


class TorrentAction(BaseModel):
    action: str


# ----------------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------------
@app.get("/api/search")
def api_search(q: str = Query(..., min_length=1), cat: str = Query("all"),
               limit: int = Query(None)):
    lim = limit or cfg().search_limit
    try:
        results = searchmod.search(cfg().jackett_url, cfg().jackett_api_key,
                                   cfg().jackett_indexer, q, cat, lim,
                                   cfg().request_timeout)
    except searchmod.SearchError as e:
        raise HTTPException(502, str(e))
    return {"query": q, "category": cat, "total": len(results), "results": results}


@app.post("/api/add")
def api_add(req: AddRequest):
    if not req.magnet:
        raise HTTPException(400, "No magnet/href provided.")
    try:
        res = client().add(req.magnet, cfg().download_dir or None)
    except DownloadClientError as e:
        raise HTTPException(502, str(e))
    log.info("Added: %s (id=%s, dup=%s)", res.name, res.id, res.duplicate)
    if not res.duplicate:
        try:
            db.add_history("added", res.name or req.title or "", "added to client")
        except Exception:                    # noqa: BLE001
            pass
    return {"status": "ok", "id": res.id, "name": res.name, "duplicate": res.duplicate}


# ----------------------------------------------------------------------------
# Transfers + controls
# ----------------------------------------------------------------------------
def _fmt_eta(eta: int) -> str:
    if eta is None or eta < 0:
        return "—"
    if eta < 60:
        return f"{eta}s"
    if eta < 3600:
        return f"{eta // 60}m"
    if eta < 86400:
        return f"{eta // 3600}h {(eta % 3600) // 60}m"
    return f"{eta // 86400}d"


@app.get("/api/transfers")
def api_transfers():
    try:
        xs = client().list_transfers()
    except DownloadClientError as e:
        raise HTTPException(502, str(e))
    out = []
    for t in xs:
        out.append({
            "id": t.id, "name": t.name, "percent": t.percent,
            "down_h": searchmod.human_size(t.down_rate) + "/s",
            "status": t.status, "eta_h": _fmt_eta(t.eta), "ratio": t.ratio,
            "size_h": searchmod.human_size(t.size), "error": t.error, "done": t.done,
        })
    out.sort(key=lambda x: (x["done"], -x["percent"]))
    return {"transfers": out}


@app.post("/api/torrent/{tid}")
def api_torrent_action(tid: str, req: TorrentAction):
    c = client()
    try:
        if req.action == "pause":
            c.pause(tid)
        elif req.action == "resume":
            c.resume(tid)
        elif req.action == "remove":
            c.remove(tid, delete_data=False)
        elif req.action == "remove-data":
            c.remove(tid, delete_data=True)
        else:
            raise HTTPException(400, f"Unknown action: {req.action}")
    except DownloadClientError as e:
        raise HTTPException(502, str(e))
    log.info("Torrent %s: %s", tid, req.action)
    return {"status": "ok", "id": tid, "action": req.action}


@app.get("/api/torrent/{tid}/files")
def api_torrent_files(tid: str):
    try:
        files = client().files(tid)
    except DownloadClientError as e:
        raise HTTPException(502, str(e))
    return {"id": tid, "files": [{
        "name": f.name, "path": f.path, "size_h": searchmod.human_size(f.size),
        "percent": f.percent, "wanted": f.wanted} for f in files]}


# ----------------------------------------------------------------------------
# Stats + events
# ----------------------------------------------------------------------------
@app.get("/api/stats")
def api_stats():
    out = {"disk": None, "down_total": 0, "up_total": 0,
           "downloading": 0, "seeding": 0, "total": 0}
    try:
        du = shutil.disk_usage(cfg().disk_path)
        out["disk"] = {"free": du.free, "total": du.total,
                       "free_h": searchmod.human_size(du.free),
                       "total_h": searchmod.human_size(du.total),
                       "pct_used": round(du.used / du.total * 100, 1) if du.total else 0}
    except OSError:
        pass
    try:
        for t in client().list_transfers():
            out["down_total"] += t.down_rate
            out["up_total"] += t.up_rate
            out["total"] += 1
            if t.status == "downloading":
                out["downloading"] += 1
            elif t.status == "seeding":
                out["seeding"] += 1
    except DownloadClientError:
        pass
    out["down_total_h"] = searchmod.human_size(out["down_total"]) + "/s"
    out["up_total_h"] = searchmod.human_size(out["up_total"]) + "/s"
    return out


@app.get("/api/events")
def api_events(limit: int = Query(60, ge=1, le=300)):
    p = Path(cfg().events_file)
    if not p.exists():
        return {"events": []}
    try:
        lines = p.read_text(errors="ignore").strip().splitlines()
    except OSError:
        return {"events": []}
    events = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            events.append({"ts": "", "event": "raw", "msg": line})
    events.reverse()
    return {"events": events}


# ----------------------------------------------------------------------------
# Config / health / setup
# ----------------------------------------------------------------------------
@app.get("/api/config")
def api_config():
    """Non-secret config the UI needs (theme, title, thresholds, client kind)."""
    return {"title": cfg().app_title, "theme": cfg().ui_theme,
            "accent": cfg().ui_accent, "client": cfg().client_kind,
            "big_download_gb": cfg().big_download_gb,
            "configured": cfg().configured()}


# ----------------------------------------------------------------------------
# First-run setup wizard
# ----------------------------------------------------------------------------
class WizardTest(BaseModel):
    # indexer
    jackett_url: str | None = None
    jackett_api_key: str | None = None
    jackett_indexer: str | None = "all"
    # client
    client_kind: str | None = None
    client_url: str | None = None
    client_user: str | None = ""
    client_pass: str | None = ""


class WizardSave(WizardTest):
    library_root: str | None = None
    download_dir: str | None = None
    notify_urls: str | None = None
    ui_theme: str | None = None
    ui_accent: str | None = None
    app_title: str | None = None


@app.post("/api/setup/test")
def api_setup_test(w: WizardTest):
    """Validate indexer + client credentials live, without saving anything."""
    result = {"indexer": None, "client": None}
    # indexer: a real search call is the only true test of the key
    if w.jackett_url and w.jackett_api_key:
        try:
            searchmod.search(w.jackett_url.rstrip("/"), w.jackett_api_key,
                             w.jackett_indexer or "all", "test", "all", 1, 10)
            result["indexer"] = {"ok": True, "msg": "Indexer reachable, key accepted."}
        except searchmod.SearchError as e:
            result["indexer"] = {"ok": False, "msg": str(e)}
    else:
        result["indexer"] = {"ok": False, "msg": "URL and API key required."}
    # client
    if w.client_kind and w.client_url:
        try:
            make_client(w.client_kind, w.client_url, w.client_user or "",
                        w.client_pass or "", 10).test()
            result["client"] = {"ok": True, "msg": f"{w.client_kind} reachable, auth OK."}
        except Exception as e:                       # noqa: BLE001
            result["client"] = {"ok": False, "msg": str(e)}
    else:
        result["client"] = {"ok": False, "msg": "Client type and URL required."}
    return result


@app.post("/api/setup/save")
def api_setup_save(w: WizardSave):
    """Persist wizard values and hot-reload cfg(). Returns the new state."""
    from .config import save
    mapping = {
        "JACKETT_URL": (w.jackett_url or "").rstrip("/") or None,
        "JACKETT_API_KEY": w.jackett_api_key,
        "JACKETT_INDEXER": w.jackett_indexer,
        "DOWNLOAD_CLIENT": w.client_kind,
        "CLIENT_URL": w.client_url,
        "CLIENT_USER": w.client_user,
        "CLIENT_PASS": w.client_pass,
        "LIBRARY_ROOT": w.library_root,
        "DOWNLOAD_DIR": w.download_dir,
        "NOTIFY_URLS": w.notify_urls,
        "UI_THEME": w.ui_theme,
        "UI_ACCENT": w.ui_accent,
        "APP_TITLE": w.app_title,
    }
    clean = {k: v for k, v in mapping.items() if v is not None and v != ""}
    try:
        save(clean)
    except OSError as e:
        raise HTTPException(500, f"Couldn't write config file: {e}. "
                                 "Is the /config volume writable?")
    log.info("Setup wizard saved config (%d keys).", len(clean))
    return {"status": "ok", "configured": cfg().configured()}


@app.get("/api/indexers")
def api_indexers():
    """Configured indexers from Jackett for the settings dropdown."""
    c = cfg()
    items = searchmod.indexers(c.jackett_url, c.jackett_api_key, c.request_timeout)
    return {"indexers": items}


@app.get("/api/meta/search")
def api_meta_search(q: str = Query(..., min_length=1), kind: str = Query("multi")):
    """TMDb title search -> poster results. Empty list if no key configured."""
    return {"enabled": tmdbmod.enabled(), "results": tmdbmod.search(q, kind)}


@app.get("/api/meta/details/{media_type}/{tmdb_id}")
def api_meta_details(media_type: str, tmdb_id: int):
    return tmdbmod.details(tmdb_id, media_type)


# --- in-app settings editor (post-setup config changes) ---
class SettingsPatch(BaseModel):
    values: dict


@app.get("/api/settings")
def api_settings_get():
    """Current effective settings the editor can change (non-secret + DB-stored)."""
    from .config import WIZARD_KEYS
    c = cfg()
    env_view = {
        "JACKETT_URL": c.jackett_url, "JACKETT_INDEXER": c.jackett_indexer,
        "DOWNLOAD_CLIENT": c.client_kind, "CLIENT_URL": c.client_url,
        "CLIENT_USER": c.client_user, "LIBRARY_ROOT": os.environ.get("LIBRARY_ROOT", ""),
        "DOWNLOAD_DIR": c.download_dir, "NOTIFY_URLS": ",".join(c.notify_urls),
        "UI_THEME": c.ui_theme, "UI_ACCENT": c.ui_accent, "APP_TITLE": c.app_title,
        "REMOVE_ON_COMPLETE": os.environ.get("REMOVE_ON_COMPLETE", "0"),
    }
    return {"env": env_view, "db": db.all_settings(),
            "tmdb_enabled": tmdbmod.enabled(),
            "wizard_keys": sorted(WIZARD_KEYS)}


@app.patch("/api/settings")
def api_settings_patch(p: SettingsPatch):
    """Update settings. Env-style keys persist to the wizard config file;
    everything else (e.g. tmdb_key) goes to the DB settings table."""
    from .config import save, WIZARD_KEYS
    env_updates, db_updates = {}, {}
    for k, v in p.values.items():
        if k in WIZARD_KEYS:
            env_updates[k] = v
        else:
            db_updates[k] = v
    if env_updates:
        try:
            save(env_updates)
        except OSError as e:
            raise HTTPException(500, f"Couldn't persist config: {e}")
    for k, v in db_updates.items():
        db.set_setting(k, v)
    log.info("Settings updated (%d env, %d db).", len(env_updates), len(db_updates))
    return {"status": "ok"}


# --- history + stats dashboard ---
@app.get("/api/history")
def api_history(limit: int = Query(100, ge=1, le=500)):
    return {"history": db.recent_history(limit)}


@app.get("/api/history/stats")
def api_history_stats():
    s = db.history_stats()
    s["completed_bytes_h"] = searchmod.human_size(s["completed_bytes"])
    return s



# --- quality profiles ---
class ProfileBody(BaseModel):
    name: str
    min_seeders: int = 1
    resolutions: list[str] = []
    sources: list[str] = []
    max_size_gb: float = 0
    min_size_gb: float = 0
    language: str = "en"


@app.get("/api/profiles")
def api_profiles_list():
    import json as _json
    with db.connect() as c:
        rows = c.execute("SELECT * FROM profiles ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["resolutions"] = _json.loads(d.get("resolutions") or "[]")
        d["sources"] = _json.loads(d.get("sources") or "[]")
        out.append(d)
    return {"profiles": out}


@app.post("/api/profiles")
def api_profiles_create(p: ProfileBody):
    import json as _json
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO profiles (name, min_seeders, resolutions, sources, max_size_gb, min_size_gb, language) "
            "VALUES (?,?,?,?,?,?,?)",
            (p.name, p.min_seeders, _json.dumps(p.resolutions), _json.dumps(p.sources),
             p.max_size_gb, p.min_size_gb, p.language))
        pid = cur.lastrowid
    return {"status": "ok", "id": pid}


@app.put("/api/profiles/{pid}")
def api_profiles_update(pid: int, p: ProfileBody):
    import json as _json
    with db.connect() as c:
        c.execute(
            "UPDATE profiles SET name=?, min_seeders=?, resolutions=?, sources=?, "
            "max_size_gb=?, min_size_gb=?, language=? WHERE id=?",
            (p.name, p.min_seeders, _json.dumps(p.resolutions), _json.dumps(p.sources),
             p.max_size_gb, p.min_size_gb, p.language, pid))
    return {"status": "ok"}


@app.delete("/api/profiles/{pid}")
def api_profiles_delete(pid: int):
    with db.connect() as c:
        c.execute("DELETE FROM profiles WHERE id=?", (pid,))
    return {"status": "ok"}


@app.get("/api/search/best")
def api_search_best(q: str = Query(..., min_length=1), cat: str = Query("all"),
                    profile_id: int = Query(...)):
    """Search and return the single best release per the given profile, plus the
    full ranked list. Powers the 'auto-pick' button."""
    import json as _json
    from . import profiles as prof
    with db.connect() as c:
        row = c.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Profile not found")
    profile = dict(row)
    profile["resolutions"] = _json.loads(profile.get("resolutions") or "[]")
    profile["sources"] = _json.loads(profile.get("sources") or "[]")
    try:
        results = searchmod.search(cfg().jackett_url, cfg().jackett_api_key,
                                   cfg().jackett_indexer, q, cat, cfg().search_limit,
                                   cfg().request_timeout)
    except searchmod.SearchError as e:
        raise HTTPException(502, str(e))
    ranked = prof.rank(results, profile)
    return {"query": q, "profile": profile["name"], "best": ranked[0] if ranked else None,
            "ranked": ranked, "considered": len(results), "qualified": len(ranked)}


# --- subscriptions (RSS auto-grab) ---
class SubscriptionBody(BaseModel):
    title: str
    query: str = ""
    media_type: str = "tv"
    profile_id: int | None = None
    enabled: bool = True


@app.get("/api/subscriptions")
def api_subs_list():
    return {"subscriptions": db.list_subscriptions()}


@app.post("/api/subscriptions")
def api_subs_create(s: SubscriptionBody):
    sid = db.create_subscription(s.title, s.query or s.title, s.media_type, s.profile_id)
    return {"status": "ok", "id": sid}


@app.put("/api/subscriptions/{sid}")
def api_subs_update(sid: int, s: SubscriptionBody):
    db.update_subscription(sid, title=s.title, query=s.query or s.title,
                           media_type=s.media_type, profile_id=s.profile_id,
                           enabled=1 if s.enabled else 0)
    return {"status": "ok"}


@app.delete("/api/subscriptions/{sid}")
def api_subs_delete(sid: int):
    db.delete_subscription(sid)
    return {"status": "ok"}


@app.post("/api/subscriptions/check")
def api_subs_check():
    """Run all enabled subscriptions now (the 'check now' button)."""
    from . import scheduler
    return scheduler.run_once()


@app.get("/api/subscriptions/status")
def api_subs_status():
    from . import scheduler
    return {"last_run": scheduler.last_run(), "interval_seconds": scheduler.INTERVAL}


# --- monitored series (library-aware tracking) ---
class SeriesAdd(BaseModel):
    tmdb_id: int
    title: str
    year: int | None = None
    poster: str | None = None
    profile_id: int | None = None


@app.get("/api/series")
def api_series_list():
    from . import series as series_mod
    from .library import normalize_title
    out = []
    for s in series_mod.list_series():
        with db.connect() as c:
            # canonical episodes for this series
            canon = c.execute(
                "SELECT season, episode FROM series_episodes WHERE series_id=?",
                (s["id"],)).fetchall()
            total = len(canon)
            # library episodes, matched to this show by normalized title
            lib = c.execute("SELECT show_name, season, episode FROM library_episodes").fetchall()
            want = c.execute("SELECT COUNT(*) AS n FROM wanted WHERE series_id=? AND status='wanted'",
                             (s["id"],)).fetchone()["n"]
        key = normalize_title(s["title"])
        owned = {(r["season"], r["episode"]) for r in lib
                 if normalize_title(r["show_name"]) == key}
        canon_set = {(r["season"], r["episode"]) for r in canon}
        # "have" = canonical episodes we actually own (so it can't exceed total)
        have = len(owned & canon_set) if canon_set else len(owned)
        s.update(have=have, total=total, wanted=want)
        out.append(s)
    return {"series": out}


@app.post("/api/series")
def api_series_add(s: SeriesAdd):
    from . import series as series_mod
    sid = series_mod.add_series(s.tmdb_id, s.title, s.year, s.poster, s.profile_id)
    series_mod.reconcile(sid)
    return {"status": "ok", "id": sid}


@app.delete("/api/series/{sid}")
def api_series_delete(sid: int):
    from . import series as series_mod
    series_mod.delete_series(sid)
    return {"status": "ok"}


@app.post("/api/series/{sid}/monitor")
def api_series_monitor(sid: int, mode: str = Query("all")):
    from . import series as series_mod
    series_mod.set_monitor_mode(sid, mode)
    return {"status": "ok", "mode": mode}


@app.get("/api/series/{sid}/episodes")
def api_series_episodes(sid: int):
    from . import series as series_mod
    return series_mod.episode_breakdown(sid)


@app.post("/api/series/{sid}/hunt")
def api_series_hunt(sid: int):
    from . import series as series_mod
    return series_mod.hunt_series(sid)


@app.get("/api/series/{sid}/wanted")
def api_series_wanted(sid: int):
    from . import series as series_mod
    return {"wanted": [w for w in series_mod.list_wanted() if w.get("series_id") == sid]}


@app.post("/api/library/import")
def api_library_import(profile_id: int | None = Query(None)):
    """Auto-discover shows + movies on disk and monitor them (Sonarr-style
    library import). Requires a TMDb key."""
    from . import importer
    return importer.import_library(profile_id)


@app.get("/api/library/report")
def api_library_report():
    from . import library
    return library.scan_report()


@app.post("/api/library/scan")
def api_library_scan(force: bool = False):
    from . import library
    return library.scan(force=force)


@app.post("/api/library/reconcile")
def api_library_reconcile():
    from . import series as series_mod
    return series_mod.reconcile_all()


@app.get("/api/wanted")
def api_wanted():
    from . import series as series_mod
    return {"wanted": series_mod.list_wanted()}


# --- monitored movies (Radarr-side) ---
class MovieAdd(BaseModel):
    tmdb_id: int
    title: str
    year: int | None = None
    poster: str | None = None
    profile_id: int | None = None


@app.get("/api/movies")
def api_movies_list():
    from . import movies as movies_mod
    return {"movies": movies_mod.list_movies()}


@app.post("/api/movies")
def api_movies_add(m: MovieAdd):
    from . import movies as movies_mod
    mid = movies_mod.add_movie(m.tmdb_id, m.title, m.year, m.poster, m.profile_id)
    return {"status": "ok", "id": mid}


@app.get("/api/movies/{mid}/detail")
def api_movie_detail(mid: int):
    from . import movies as movies_mod
    return movies_mod.movie_detail(mid)


@app.delete("/api/movies/{mid}")
def api_movies_delete(mid: int):
    from . import movies as movies_mod
    movies_mod.delete_movie(mid)
    return {"status": "ok"}


@app.get("/health")
def health():
    status = {"indexer": "unknown", "client": "unknown"}
    try:
        import requests
        requests.get(f"{cfg().jackett_url}/", timeout=5)
        status["indexer"] = "reachable"
    except Exception:
        status["indexer"] = "unreachable"
    try:
        client().test()
        status["client"] = "reachable"
    except Exception as e:
        status["client"] = f"error: {e}"
    return status


@app.get("/favicon.svg")
def favicon():
    f = STATIC / "favicon.svg"
    if f.exists():
        return FileResponse(f, media_type="image/svg+xml")
    raise HTTPException(404)


@app.get("/login")
def login_page():
    f = STATIC / "login.html"
    if f.exists():
        return FileResponse(f)
    raise HTTPException(404, "Login page not installed.")


@app.get("/register")
def register_page():
    f = STATIC / "login.html"  # same page, register tab
    if f.exists():
        return FileResponse(f)
    raise HTTPException(404)


@app.get("/reset")
def reset_page():
    f = STATIC / "login.html"
    if f.exists():
        return FileResponse(f)
    raise HTTPException(404)


@app.get("/")
def index():
    f = STATIC / "index.html"
    if f.exists():
        return FileResponse(f)
    raise HTTPException(404, "UI not installed.")


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

"""Faucet — self-hosted search → download → sort → manage for media.

One lightweight app over your indexer (Jackett/Prowlarr) and torrent client
(Transmission/qBittorrent/Deluge). This module exposes the HTTP API and serves
the single-page UI.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from . import config as _cfg
from .clients import make_client, DownloadClientError
from . import search as searchmod
from . import tmdb as tmdbmod
from . import db

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
from .redact import install as _install_redaction, redact as _redact  # noqa: E402
_install_redaction()
log = logging.getLogger("faucet")
for _lvl, _msg in _cfg.pending_messages:
    logging.getLogger("faucet.config").log(_lvl, _msg)
_cfg.pending_messages.clear()

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # an unclean previous exit (killed, OOM, power loss) raises an alert
    try:
        from . import alerts
        alerts.started()
    except Exception as e:                       # noqa: BLE001 - never block startup
        log.warning("Startup alert check failed: %s", e)
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
    try:
        from . import alerts
        alerts.stopped()
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
_PUBLIC_PREFIXES = ("/static/", "/app/", "/favicon", "/login", "/register", "/reset",
                    "/api/auth/", "/health", "/cal/")


def _is_public(path: str) -> bool:
    # /metrics does its own auth (bearer token or admin session)
    if path in ("/login", "/register", "/reset", "/metrics"):
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


# State-changing API endpoints that must work WITHOUT a CSRF token because no
# session (and thus no CSRF cookie) exists yet when they're called.
_CSRF_EXEMPT = ("/api/auth/login", "/api/auth/register", "/api/auth/reset/consume")
# (/api/auth/* is public, so the gate never applies CSRF there anyway; the
# sign-in endpoints — password, code, passkey — have no session to forge)


def _csrf_ok(request: _Request) -> bool:
    import secrets as _secrets
    cookie = request.cookies.get(_auth_routes.CSRF_COOKIE)
    header = request.headers.get("x-csrf-token")
    return bool(cookie and header and _secrets.compare_digest(cookie, header))


@app.middleware("http")
async def _auth_gate(request: _Request, call_next):
    path = request.url.path
    # First-run (no users yet): expose ONLY the public pages + auth endpoints
    # so the admin can be registered. Everything else stays locked — the old
    # behavior of waving EVERY request through unauthenticated meant a wiped
    # or freshly-mounted DB left the entire admin API open to the internet
    # until someone happened to register.
    try:
        no_users = _auth.user_count() == 0
    except Exception:                            # noqa: BLE001
        no_users = False
    if _is_public(path):
        return await call_next(request)
    user = None if no_users else _auth.session_user(
        request.cookies.get(_auth.SESSION_COOKIE))
    if not user:
        # API calls get 401 JSON; page loads redirect to the login screen.
        if path.startswith("/api/"):
            return _JSONResponse({"detail": "Authentication required."}, status_code=401)
        from fastapi.responses import RedirectResponse as _Redirect
        return _Redirect(url="/login", status_code=302)
    # CSRF (double-submit cookie) for EVERY state-changing API call, not just
    # the auth router's endpoints — /api/add, torrent actions, settings, etc.
    # were previously unprotected.
    if (request.method in ("POST", "PUT", "PATCH", "DELETE")
            and path.startswith("/api/") and path not in _CSRF_EXEMPT
            and not _csrf_ok(request)):
        return _JSONResponse({"detail": "CSRF check failed."}, status_code=403)
    # admin-only: management endpoints (writes, search, torrents, settings).
    # Library *reads* (GET series/movies) are allowed for any logged-in user so
    # they can browse what's available; everything else stays admin-only.
    if _admin_only(path, request.method) and user.get("role") != "admin":
        if path.startswith("/api/"):
            return _JSONResponse({"detail": "Admin access required."}, status_code=403)
        from fastapi.responses import RedirectResponse as _Redirect
        return _Redirect(url="/", status_code=302)
    # admins must have two-factor sign-in: until they do, the API only answers
    # what setting it up needs (pages still load, so the app can walk them
    # through it)
    if (user.get("role") == "admin" and path.startswith("/api/")
            and not path.startswith(("/api/me/2fa", "/api/me/passkeys", "/api/auth/"))):
        from . import mfa
        if mfa.required_for(user) and not mfa.enabled(user["id"]):
            return _JSONResponse({"detail": "Set up two-factor sign-in first.",
                                  "code": "mfa_setup_required"}, status_code=403)
    request.state.user = user
    return await call_next(request)


# Content-Security-Policy for the UI. Scripts: our own modules only — no
# inline script, no eval, no third-party code, so an HTML-injection bug
# can't become script execution. Styles keep 'unsafe-inline' (the UI sets
# per-element style attributes); the policy still pins every origin (no
# exfiltration to arbitrary hosts, no framing, no plugins, no <base> hijack).
_CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https://image.tmdb.org",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "object-src 'none'",
    "form-action 'self'",
))
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


@app.middleware("http")
async def _security_headers(request: _Request, call_next):
    # registered after _auth_gate, so it wraps it: auth redirects and 401s
    # carry the headers too
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if request.url.path.startswith("/static/app/"):
        # ES modules import each other by bare path; after a deploy a cached
        # old module next to a fresh one breaks the page. Revalidate every
        # time (an ETag 304 is cheap).
        response.headers["Cache-Control"] = "no-cache"
    return response


# Endpoints regular users may READ: browse the monitored library, search
# TMDb (to request), browse Discover, and see the calendar. Safe, non-indexer,
# non-torrent reads.
_USER_READABLE = ("/api/series", "/api/movies", "/api/meta/", "/api/calendar", "/api/home",
                  "/api/nav")

# Always admin-only regardless of method — raw indexer search, torrent client,
# download management, settings, profiles, subscriptions, hunts, library ops.
_ADMIN_ALWAYS = (
    "/api/search", "/api/indexers", "/api/torrent", "/api/transfers",
    "/api/add", "/api/profiles", "/api/subscriptions", "/api/library",
    "/api/settings", "/api/admin", "/api/stats", "/api/config",
    "/api/events", "/api/history", "/api/wanted",
)


def _admin_only(path: str, method: str) -> bool:
    # /api/requests is user-level (router enforces per-action perms); /api/me/
    # is the signed-in user's own things (calendar link).
    if path.startswith(("/api/requests", "/api/me/")):
        return False
    if any(path.startswith(p) for p in _ADMIN_ALWAYS):
        return True
    # interactive release search/grab hit the indexer + client — admin-only
    # even though they live under the user-readable /api/series prefix
    if "/releases" in path or "/grab" in path:
        return True
    # series/movies/meta: GET is user-readable; writes are admin-only.
    if any(path.startswith(p) for p in _USER_READABLE):
        return method not in ("GET", "HEAD")
    # any other /api/ not explicitly public/readable: admin-only by default
    if path.startswith("/api/"):
        return True
    return False


def _user(request) -> dict:
    return getattr(request.state, "user", None) or {}


def _guard_title(request, kind: str, title_id: int) -> None:
    """404 — not 403, so a member can't probe what's hidden from them — for a
    show or movie in a library the user can't see."""
    from . import access
    visible = (access.visible_series_ids if kind == "tv" else access.visible_movie_ids)(_user(request))
    if visible is not None and title_id not in visible:
        raise HTTPException(404, "Not found")


def cfg():
    """Always return the live (possibly hot-reloaded) config object."""
    return _cfg.config


def client():
    """Build the configured download client per request (cheap, stateless)."""
    return make_client(cfg().client_kind, cfg().client_url,
                       cfg().client_user, cfg().client_pass, cfg().request_timeout)


# the transfer poller builds its client through this module's client() — the
# name is looked up per poll, so settings changes (and test fakes) apply
from . import transfers as _transfers_mod  # noqa: E402
_transfers_mod.configure(lambda: client())


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
    if not (cfg().jackett_url and cfg().jackett_api_key):
        raise HTTPException(503, "Indexer not configured — set the Jackett URL and "
                                 "API key in Settings → Connections.")
    from . import finder
    try:
        results = finder.find("free", query=q, category=cat, limit=lim)
    except searchmod.SearchError as e:
        raise HTTPException(502, str(e))
    return {"query": q, "category": cat, "total": len(results), "results": results,
            "hidden": getattr(results, "hidden", 0), **_search_meta(results)}


def _search_meta(results) -> dict:
    """How many indexers were searched and which failed, for the UI."""
    return {"indexers": getattr(results, "searched", 1),
            "indexer_errors": [{"indexer": i, "error": e}
                               for i, e in getattr(results, "errors", [])]}


@app.post("/api/add")
def api_add(req: AddRequest):
    if not req.magnet:
        raise HTTPException(400, "No magnet/href provided.")
    try:
        res = client().add(req.magnet, cfg().download_dir or None)
        _refresh_transfers()
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


def _refresh_transfers() -> None:
    from . import transfers
    transfers.refresh_soon()


@app.get("/api/transfers")
def api_transfers():
    """The client's transfers from the background poller (faucet.transfers):
    never waits on the client. `client` says whether the latest poll worked
    and how old the list is."""
    from . import transfers
    snap = transfers.snapshot()
    if snap["ok"] is False and not snap["items"]:
        raise HTTPException(502, snap["error"] or "Download client not responding")
    xs = snap["items"]
    try:
        from . import safety
        flagged = safety.flags()
    except Exception:                            # noqa: BLE001 - never break the list
        flagged = {}
    out = []
    for t in xs:
        f = flagged.get(str(t.id))
        out.append({
            "id": t.id, "name": t.name, "percent": t.percent,
            "down_h": searchmod.human_size(t.down_rate) + "/s",
            "status": t.status, "eta_h": _fmt_eta(t.eta), "ratio": t.ratio,
            "size": t.size,
            "size_h": searchmod.human_size(t.size), "error": t.error, "done": t.done,
            "flag": f["reason"] if f and f.get("name") == t.name else None,
        })
    out.sort(key=lambda x: (x["done"], -x["percent"]))
    return {"transfers": out, "client": {"ok": snap["ok"], "error": snap["error"],
                                         "as_of": snap["as_of"], "stale": snap["stale"]}}


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
    from . import transfers
    transfers.refresh_soon()
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
    from . import transfers as _tr
    try:
        for t in _tr.snapshot()["items"]:
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


def config_warnings(client_ok: bool | None = None) -> list[str]:
    """Problems that silently stop Faucet from working. Cheap: no network."""
    c = cfg()
    w = []
    if not c.jackett_url or not c.jackett_api_key:
        w.append("Jackett API key is not set — search and hunting are disabled. "
                 "Settings → Connections.")
    if not c.client_url:
        w.append("No download client URL configured. Settings → Connections.")
    elif client_ok is False:
        w.append(f"Download client ({c.client_kind}) is unreachable.")
    try:
        from . import safety
        n = len(safety.flags())
        if n:
            w.append(f"{n} suspicious download{'s' if n != 1 else ''} paused for review "
                     "(Activity → Transfers).")
    except Exception:                            # noqa: BLE001
        pass
    return w


@app.get("/api/dashboard")
def api_dashboard():
    """Consolidated admin overview: storage, live activity, library health,
    requests and users. One call so the dashboard isn't a waterfall of fetches."""
    out: dict = {}

    # ── storage ──
    disk = None
    try:
        du = shutil.disk_usage(cfg().disk_path)
        disk = {"free": du.free, "used": du.used, "total": du.total,
                "free_h": searchmod.human_size(du.free),
                "used_h": searchmod.human_size(du.used),
                "total_h": searchmod.human_size(du.total),
                "pct_used": round(du.used / du.total * 100, 1) if du.total else 0}
    except OSError:
        pass
    out["disk"] = disk

    # ── live activity (current transfers) ──
    active = []
    down_total = up_total = downloading = seeding = 0
    from . import transfers as _tr
    _snap = _tr.snapshot()
    client_ok = _snap["ok"] is not False
    try:
        for t in _snap["items"]:
            down_total += t.down_rate
            up_total += t.up_rate
            if t.status == "downloading":
                downloading += 1
            elif t.status == "seeding":
                seeding += 1
            if t.status in ("downloading", "checking", "queued") or (0 < t.percent < 100):
                active.append({
                    "name": t.name, "percent": round(t.percent, 1),
                    "down_rate": t.down_rate, "up_rate": t.up_rate,
                    "down_h": searchmod.human_size(t.down_rate) + "/s",
                    "status": t.status, "eta": t.eta, "size": t.size,
                    "size_h": searchmod.human_size(t.size),
                })
    except DownloadClientError:
        client_ok = False
    active.sort(key=lambda a: a["down_rate"], reverse=True)
    out["indexer"] = {"configured": bool(cfg().jackett_url and cfg().jackett_api_key)}
    out["warnings"] = config_warnings(client_ok=client_ok)
    out["transfers"] = {
        "active": active[:8], "active_count": len(active),
        "downloading": downloading, "seeding": seeding,
        "down_h": searchmod.human_size(down_total) + "/s",
        "up_h": searchmod.human_size(up_total) + "/s",
        "client_ok": client_ok,
    }

    # ── library health + requests + users + recent history (one db pass) ──
    with db.connect() as c:
        def scalar(q, *a):
            row = c.execute(q, a).fetchone()
            return (row[0] if row else 0) or 0
        # episodes "have" can't use a series_id join: library_episodes are matched
        # to series by normalized title (same as reconcile / the Shows page), not
        # by a stored series_id. Compute the same way so the count is real.
        from .library import owned_index, owned_for
        idx = owned_index(c)
        canon: dict = {}
        for r in c.execute("SELECT series_id, season, episode FROM series_episodes"):
            canon.setdefault(r["series_id"], set()).add((r["season"], r["episode"]))
        # "have" = canonical episodes owned, counted by folder link first,
        # then by name for folders not linked to any series
        episodes_have = sum(
            len(owned_for(idx, r["id"], r["title"]) & canon.get(r["id"], set()))
            for r in c.execute("SELECT id, title FROM series").fetchall())
        out["library"] = {
            "shows": scalar("SELECT COUNT(*) FROM series"),
            "movies": scalar("SELECT COUNT(*) FROM movies"),
            "episodes_have": episodes_have,
            "episodes_total": scalar("SELECT COUNT(*) FROM series_episodes"),
            "movies_have": scalar("SELECT COUNT(*) FROM movies WHERE status='have'"),
            "wanted": scalar("SELECT COUNT(*) FROM wanted WHERE status='wanted'"),
        }
        out["requests"] = {
            "pending": scalar("SELECT COUNT(*) FROM requests WHERE status='pending'"),
            "total": scalar("SELECT COUNT(*) FROM requests"),
        }
        out["users"] = {
            "total": scalar("SELECT COUNT(*) FROM users"),
            "active": scalar("SELECT COUNT(*) FROM users WHERE status='active'"),
            "pending": scalar("SELECT COUNT(*) FROM users WHERE status='pending'"),
            "admins": scalar("SELECT COUNT(*) FROM users WHERE role='admin'"),
        }
        try:
            hist = c.execute(
                "SELECT ts, event, title, size FROM history "
                "ORDER BY id DESC LIMIT 8").fetchall()
            out["recent"] = [{"ts": r["ts"], "event": r["event"],
                              "title": r["title"],
                              "size_h": searchmod.human_size(r["size"] or 0)}
                             for r in hist]
        except sqlite3.Error:
            out["recent"] = []
    return out


@app.get("/api/events")
def api_events(limit: int = Query(60, ge=1, le=300)):
    from . import maintenance
    return {"events": maintenance.tail_events(limit)}


# ----------------------------------------------------------------------------
# Config / health / setup
# ----------------------------------------------------------------------------
@app.get("/api/config")
def api_config():
    """Non-secret config the UI needs (theme, title, thresholds, client kind)."""
    return {"title": cfg().app_title, "theme": cfg().ui_theme,
            "client": cfg().client_kind,
            "big_download_gb": cfg().big_download_gb,
            "configured": cfg().configured()}


# ----------------------------------------------------------------------------
# Connection test (used by the Settings panel)
# ----------------------------------------------------------------------------
class ConnTest(BaseModel):
    # all optional: when omitted, the current saved config is tested instead,
    # so the Settings "Test connections" button works before OR after saving.
    jackett_url: str | None = None
    jackett_api_key: str | None = None
    jackett_indexer: str | None = None
    client_kind: str | None = None
    client_url: str | None = None
    client_user: str | None = None
    client_pass: str | None = None


@app.post("/api/settings/test")
def api_settings_test(w: ConnTest):
    """Live-validate indexer + download-client credentials without saving.
    Any field left null falls back to the current configuration, so this can
    test typed-but-unsaved values or verify what's already live.
    (Admin-gated by the auth middleware via the /api/settings prefix.)"""
    c = cfg()
    j_url = (w.jackett_url if w.jackett_url is not None else c.jackett_url).rstrip("/")
    j_idx = w.jackett_indexer if w.jackett_indexer is not None else c.jackett_indexer
    k_kind = w.client_kind if w.client_kind is not None else c.client_kind
    k_url = w.client_url if w.client_url is not None else c.client_url
    k_user = w.client_user if w.client_user is not None else c.client_user
    # Secrets are write-only in the editor: null means "use the saved one" —
    # but only against the SAVED endpoint. Testing a different URL with the
    # saved key/password would hand them to whatever host was typed in.
    result = {"indexer": None, "client": None}
    j_key = w.jackett_api_key
    if j_key is None:
        if j_url != (c.jackett_url or "").rstrip("/") and c.jackett_api_key:
            result["indexer"] = {"ok": False, "msg": "Enter the API key to test a "
                                 "different Jackett URL (the saved key is only "
                                 "sent to the saved URL)."}
        j_key = c.jackett_api_key
    k_pass = w.client_pass
    client_blocked = False
    if k_pass is None:
        if k_url != c.client_url and c.client_pass:
            result["client"] = {"ok": False, "msg": "Enter the password to test a "
                                "different client URL (the saved password is only "
                                "sent to the saved URL)."}
            client_blocked = True
        k_pass = c.client_pass
    # indexer: a real search call is the only true test of the key
    if result["indexer"] is not None:
        pass
    elif j_url and j_key:
        try:
            searchmod.search(j_url, j_key, j_idx or "all", "test", "all", 1, 10)
            result["indexer"] = {"ok": True, "msg": "Indexer reachable, key accepted."}
        except searchmod.SearchError as e:
            result["indexer"] = {"ok": False, "msg": str(e)}
    else:
        result["indexer"] = {"ok": False, "msg": "Jackett URL and API key required."}
    # client: ping + auth
    if client_blocked:
        pass
    elif k_kind and k_url:
        try:
            make_client(k_kind, k_url, k_user or "", k_pass or "", 10).test()
            result["client"] = {"ok": True, "msg": f"{k_kind} reachable, auth OK."}
        except Exception as e:                       # noqa: BLE001
            result["client"] = {"ok": False, "msg": _redact(e, k_pass)}
    else:
        result["client"] = {"ok": False, "msg": "Client type and URL required."}
    return result


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


@app.get("/api/meta/discover")
def api_meta_discover(kind: str = Query("tv"), list_name: str = Query("trending", alias="list")):
    """Browsable TMDb lists for the Discover page. Items already monitored are
    annotated with their library ids so the UI can badge + deep-link them."""
    results = tmdbmod.discover(kind, list_name)
    with db.connect() as c:
        series_ids = {r["tmdb_id"]: r["id"] for r in c.execute(
            "SELECT tmdb_id, id FROM series")}
        movie_ids = {r["tmdb_id"]: r["id"] for r in c.execute(
            "SELECT tmdb_id, id FROM movies")}
    out = []
    for r in results:
        rr = dict(r)
        if r["media_type"] == "tv" and r["tmdb_id"] in series_ids:
            rr["in_library"], rr["library_id"] = True, series_ids[r["tmdb_id"]]
        elif r["media_type"] == "movie" and r["tmdb_id"] in movie_ids:
            rr["in_library"], rr["library_id"] = True, movie_ids[r["tmdb_id"]]
        else:
            rr["in_library"] = False
        out.append(rr)
    return {"enabled": tmdbmod.enabled(), "kind": kind, "list": list_name,
            "results": out}


@app.get("/api/calendar")
def api_calendar(request: _Request, past: int = Query(7, ge=0, le=30),
                 days: int = Query(14, ge=1, le=60)):
    """Air-date calendar across monitored shows: every canonical episode airing
    from `past` days ago through `days` days ahead, with ownership + hunt
    status. Grouped client-side; this returns a flat, date-sorted list."""
    from datetime import date, timedelta as _td
    lo = (date.today() - _td(days=past)).isoformat()
    hi = (date.today() + _td(days=days)).isoformat()
    with db.connect() as c:
        eps = c.execute(
            "SELECT e.season, e.episode, e.title AS ep_title, e.air_date, "
            "s.id AS series_id, s.title, s.poster, s.monitor_mode "
            "FROM series_episodes e JOIN series s ON s.id=e.series_id "
            "WHERE s.monitored=1 AND e.air_date >= ? AND e.air_date <= ? "
            "ORDER BY e.air_date, s.title, e.season, e.episode",
            (lo, hi)).fetchall()
        from .library import owned_index, owned_for
        idx = owned_index(c)
        grabbed = {(r["series_id"], r["season"], r["episode"]): r["status"]
                   for r in c.execute("SELECT series_id, season, episode, status "
                                      "FROM wanted WHERE kind='episode'")}
    today = date.today().isoformat()
    from . import access
    visible = access.visible_series_ids(_user(request))
    out = []
    for e in eps:
        if visible is not None and e["series_id"] not in visible:
            continue
        owned = (e["season"], e["episode"]) in owned_for(idx, e["series_id"], e["title"])
        wstatus = grabbed.get((e["series_id"], e["season"], e["episode"]))
        if e["air_date"] > today:
            status = "upcoming"
        elif owned:
            status = "have"
        elif wstatus == "grabbed":
            status = "grabbed"
        else:
            status = "missing"
        out.append({"date": e["air_date"], "series_id": e["series_id"],
                    "title": e["title"], "poster": e["poster"],
                    "season": e["season"], "episode": e["episode"],
                    "ep_title": e["ep_title"], "status": status})
    return {"from": lo, "to": hi, "today": today, "episodes": out}


# --- in-app settings editor (post-setup config changes) ---
class SettingsPatch(BaseModel):
    values: dict


@app.get("/api/settings")
def api_settings_get():
    """Current effective settings the editor can change (non-secret + DB-stored).
    Exposes the full editable environment except structural/secret keys."""
    from .config import WIZARD_KEYS, PATH_KEYS
    c = cfg()
    env_view = {
        # connections
        "JACKETT_URL": c.jackett_url, "JACKETT_INDEXER": c.jackett_indexer,
        "DOWNLOAD_CLIENT": c.client_kind, "CLIENT_URL": c.client_url,
        "CLIENT_USER": c.client_user,
        # paths
        "LIBRARY_ROOT": os.environ.get("LIBRARY_ROOT", "") or c.browse_root,
        "DOWNLOAD_DIR": c.download_dir, "DISK_PATH": c.disk_path,
        "BROWSE_ROOT": c.browse_root,
        # behavior
        "REMOVE_ON_COMPLETE": os.environ.get("REMOVE_ON_COMPLETE", "0"),
        "REQUEST_TIMEOUT": str(c.request_timeout), "SEARCH_LIMIT": str(c.search_limit),
        "BIG_DOWNLOAD_GB": str(c.big_download_gb),
        "NOTIFY_ON": ",".join(c.notify_on),
        # metadata / ui
        "UI_THEME": c.ui_theme,
    }
    env_view["AIR_DELAY_DAYS"] = os.environ.get("AIR_DELAY_DAYS", "1")
    # secrets are editable but never returned; show only whether one is set.
    env_view["CLIENT_PASS_SET"] = bool(c.client_pass)
    env_view["JACKETT_API_KEY_SET"] = bool(c.jackett_api_key)
    env_view["PLEX_URL"] = os.environ.get("PLEX_URL", "")
    env_view["PLEX_TOKEN_SET"] = bool(os.environ.get("PLEX_TOKEN"))
    # notification URLs carry webhook tokens: write-only, like the keys
    env_view["NOTIFY_URLS_COUNT"] = len(c.notify_urls)
    env_view["TMDB_KEY_SET"] = tmdbmod.enabled()
    # live status of each path (exists / writable inside the container)
    path_status = {k: _path_status(env_view.get(k, "")) for k in PATH_KEYS}
    db_view = {k: v for k, v in db.all_settings().items() if k not in _DB_SECRET_KEYS}
    return {"env": env_view, "db": db_view,
            "tmdb_enabled": tmdbmod.enabled(),
            "path_status": path_status,
            "wizard_keys": sorted(WIZARD_KEYS)}


def _path_status(path: str) -> dict:
    """Report whether a configured path exists and is writable in the container."""
    if not path:
        return {"set": False, "exists": False, "writable": False}
    p = Path(path)
    exists = p.exists()
    writable = False
    if exists:
        writable = os.access(path, os.W_OK)
    return {"set": True, "exists": exists, "writable": writable,
            "is_dir": p.is_dir() if exists else False}


# DB-backed settings the editor may write. Anything else used to be stored
# verbatim under whatever key the request named.
_DB_SETTING_KEYS = {"tmdb_key", "default_language", "default_profile_id"}
_DB_SECRET_KEYS = {"tmdb_key", "session_secret"}


def _clean_db_setting(k: str, v):
    """Validated value for a DB setting, or raise ValueError."""
    if k == "default_profile_id":
        if v in (None, "", 0, "0"):
            return ""
        pid = int(str(v).strip())
        with db.connect() as c:
            if not c.execute("SELECT 1 FROM profiles WHERE id=?", (pid,)).fetchone():
                raise ValueError(f"no profile with id {pid}")
        return pid
    v = "" if v is None else str(v).strip()
    if k == "default_language":
        if not (v == "any" or (v.isalpha() and 2 <= len(v) <= 3)):
            raise ValueError("must be an ISO-639 code like 'en', or 'any'")
        return v.lower()
    if k == "tmdb_key":
        if not v or len(v) > 512 or not all(ch.isalnum() or ch in "._-" for ch in v):
            raise ValueError("doesn't look like a TMDb API key")
        return v
    raise ValueError("not an editable setting")


@app.patch("/api/settings")
def api_settings_patch(p: SettingsPatch):
    """Update settings. Env-style keys persist to the wizard config file
    (overrides env, survives restarts); everything else goes to the DB settings
    table. Path keys are validated and any problems returned as warnings."""
    from .config import save, WIZARD_KEYS, PATH_KEYS
    env_updates, db_updates, warnings = {}, {}, []
    for k, v in p.values.items():
        if k == "NOTIFY_URLS":
            # write-only targets: they're managed through
            # /api/settings/notifications, which never echoes them back
            warnings.append("NOTIFY_URLS: edit notification targets in Settings → "
                            "Integrations; not changed here.")
            continue
        if k in WIZARD_KEYS:
            # validate path keys but don't block the save — warn instead, since
            # a path may legitimately be created later or mounted differently.
            if k in PATH_KEYS and v:
                st = _path_status(str(v))
                if not st["exists"]:
                    warnings.append(f"{k}: '{v}' does not exist in the container "
                                    f"(is it mounted?). Saved anyway.")
                elif not st["writable"]:
                    warnings.append(f"{k}: '{v}' exists but isn't writable. Saved anyway.")
            if k == "AIR_DELAY_DAYS":
                try:
                    days = int(str(v).strip())
                    if not 0 <= days <= 30:
                        raise ValueError
                except ValueError:
                    warnings.append(f"AIR_DELAY_DAYS: '{v}' must be a whole number "
                                    "from 0 to 30. Not saved.")
                    continue
                v = str(days)
            env_updates[k] = v
        elif k in _DB_SETTING_KEYS:
            try:
                db_updates[k] = _clean_db_setting(k, v)
            except ValueError as e:
                warnings.append(f"{k}: {e}. Not saved.")
        else:
            warnings.append(f"{k}: not an editable setting. Ignored.")
    if {"JACKETT_URL", "JACKETT_API_KEY", "JACKETT_INDEXER"} & set(env_updates):
        from . import finder
        finder.forget_indexers()
    if env_updates:
        try:
            save(env_updates)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except OSError as e:
            raise HTTPException(500, f"Couldn't persist config: {e}")
    for k, v in db_updates.items():
        db.set_setting(k, v)
    # some changes (paths, download client) only fully apply on restart
    needs_restart = any(k in env_updates for k in
                        ("DOWNLOAD_CLIENT", "CLIENT_URL", "LIBRARY_ROOT",
                         "DOWNLOAD_DIR", "DISK_PATH", "BROWSE_ROOT"))
    log.info("Settings updated (%d env, %d db).", len(env_updates), len(db_updates))
    return {"status": "ok", "warnings": warnings, "needs_restart": needs_restart}


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
_PROFILE_RES = ["2160p", "1080p", "720p", "480p"]
_PROFILE_SOURCES = ["REMUX", "BluRay", "WEB-DL", "WEBRip", "HDTV", "DVDRip", "CAM"]


class ProfileBody(BaseModel):
    name: str
    min_seeders: int = 1
    resolutions: list[str] = []
    sources: list[str] = []
    max_size_gb: float = 0
    min_size_gb: float = 0
    language: str = "en"

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = (v or "").strip()
        if not v or len(v) > 60:
            raise ValueError("a name of 1–60 characters")
        return v

    @field_validator("resolutions")
    @classmethod
    def _res(cls, v):
        bad = [x for x in v if x not in _PROFILE_RES]
        if bad:
            raise ValueError(f"unknown resolution(s): {', '.join(bad)}")
        return list(dict.fromkeys(v))

    @field_validator("sources")
    @classmethod
    def _src(cls, v):
        bad = [x for x in v if x not in _PROFILE_SOURCES]
        if bad:
            raise ValueError(f"unknown source(s): {', '.join(bad)}")
        return list(dict.fromkeys(v))

    @field_validator("min_seeders")
    @classmethod
    def _seed(cls, v):
        if not 0 <= v <= 10000:
            raise ValueError("between 0 and 10000")
        return v

    @field_validator("max_size_gb", "min_size_gb")
    @classmethod
    def _size(cls, v):
        if not 0 <= v <= 1000:
            raise ValueError("between 0 and 1000 GB (0 = no limit)")
        return v

    formats: dict = {}

    @field_validator("formats")
    @classmethod
    def _formats(cls, v):
        from . import profiles as _p
        v = v or {}
        if v.get("codec", "") not in _p.CODECS:
            raise ValueError("codec: " + " | ".join(x or "(none)" for x in _p.CODECS))
        if v.get("hdr", "") not in _p.HDR:
            raise ValueError("hdr: " + " | ".join(x or "(none)" for x in _p.HDR))
        for k in ("prefer_groups", "never_groups", "never_words"):
            xs = v.get(k) or []
            if not isinstance(xs, list) or len(xs) > 50 or any(len(str(x)) > 40 for x in xs):
                raise ValueError(f"{k}: up to 50 entries of up to 40 characters")
        return _p.formats({"formats": v})

    @field_validator("language")
    @classmethod
    def _lang(cls, v):
        v = (v or "").strip().lower()
        if not (v == "any" or (v.isalpha() and 2 <= len(v) <= 3)):
            raise ValueError("an ISO-639 code like 'en', or 'any'")
        return v


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
        from . import profiles as _p
        d["formats"] = _p.formats(d)
        out.append(d)
    return {"profiles": out}


@app.post("/api/profiles")
def api_profiles_create(p: ProfileBody):
    import json as _json
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO profiles (name, min_seeders, resolutions, sources, max_size_gb, min_size_gb, language, formats) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (p.name, p.min_seeders, _json.dumps(p.resolutions), _json.dumps(p.sources),
             p.max_size_gb, p.min_size_gb, p.language, _json.dumps(p.formats)))
        pid = cur.lastrowid
    return {"status": "ok", "id": pid}


@app.put("/api/profiles/{pid}")
def api_profiles_update(pid: int, p: ProfileBody):
    import json as _json
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM profiles WHERE id=?", (pid,)).fetchone():
            raise HTTPException(404, "No such profile")
        c.execute(
            "UPDATE profiles SET name=?, min_seeders=?, resolutions=?, sources=?, "
            "max_size_gb=?, min_size_gb=?, language=?, formats=? WHERE id=?",
            (p.name, p.min_seeders, _json.dumps(p.resolutions), _json.dumps(p.sources),
             p.max_size_gb, p.min_size_gb, p.language, _json.dumps(p.formats), pid))
    return {"status": "ok"}


@app.get("/api/profiles/options")
def api_profile_options():
    return {"resolutions": _PROFILE_RES, "sources": _PROFILE_SOURCES}


@app.delete("/api/profiles/{pid}")
def api_profiles_delete(pid: int):
    """Delete a profile; shows and movies using it fall back to the default
    profile instead of pointing at nothing."""
    with db.connect() as c:
        if c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] <= 1:
            raise HTTPException(400, "Keep at least one profile.")
        c.execute("UPDATE series SET profile_id=NULL WHERE profile_id=?", (pid,))
        c.execute("UPDATE movies SET profile_id=NULL WHERE profile_id=?", (pid,))
        c.execute("DELETE FROM profiles WHERE id=?", (pid,))
    if str(db.get_setting("default_profile_id") or "") == str(pid):
        db.set_setting("default_profile_id", "")
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
    from . import finder
    try:
        results = finder.find("free", query=q, category=cat)
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
def api_series_list(request: _Request):
    from . import series as series_mod
    from collections import defaultdict
    # one pass over shared tables instead of re-reading ALL library episodes
    # per show (the old loop was O(shows × library) on every page load)
    with db.connect() as c:
        # aired, non-special episodes: the same "total" the show page shows
        from datetime import date as _date
        today = _date.today().isoformat()
        canon_by_series = defaultdict(set)
        for r in c.execute("SELECT series_id, season, episode FROM series_episodes "
                           "WHERE season > 0 AND air_date != '' AND air_date <= ?", (today,)):
            canon_by_series[r["series_id"]].add((r["season"], r["episode"]))
        from .library import owned_index, owned_for
        idx = owned_index(c)
        wanted_by_series = {r["series_id"]: r["n"] for r in c.execute(
            "SELECT series_id, COUNT(*) AS n FROM wanted "
            "WHERE status='wanted' GROUP BY series_id")}
    out = []
    for s in series_mod.list_series():
        canon_set = canon_by_series.get(s["id"], set())
        owned = owned_for(idx, s["id"], s["title"])
        # "have" = canonical episodes we actually own (so it can't exceed total)
        have = len(owned & canon_set) if canon_set else len(owned)
        meta = tmdbmod.cached_details(s["tmdb_id"], "tv") if s.get("tmdb_id") else {}
        s.update(have=have, total=len(canon_set),
                 wanted=wanted_by_series.get(s["id"], 0),
                 tv_status=meta.get("status"),
                 ended=meta.get("status") in ("Ended", "Canceled"))
        out.append(s)
    from . import libraries as _libs
    default, enabled = _libs.effective_map("tv")
    for s in out:
        s["lib"] = _libs.resolve(s.get("library_id"), default, enabled)
    from . import access
    return {"series": access.filter_titles(_user(request), out)}


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


@app.post("/api/series/{sid}/profile")
def api_series_profile(sid: int, profile_id: int | None = Query(None)):
    """Assign (or clear, with profile_id omitted/0) a quality profile to a show."""
    with db.connect() as c:
        c.execute("UPDATE series SET profile_id=? WHERE id=?",
                  (profile_id or None, sid))
    return {"status": "ok", "profile_id": profile_id or None}


@app.post("/api/movies/{mid}/profile")
def api_movie_profile(mid: int, profile_id: int | None = Query(None)):
    """Assign (or clear) a quality profile to a movie."""
    with db.connect() as c:
        c.execute("UPDATE movies SET profile_id=? WHERE id=?",
                  (profile_id or None, mid))
    return {"status": "ok", "profile_id": profile_id or None}


@app.get("/api/series/{sid}/episodes")
def api_series_episodes(sid: int, request: _Request):
    _guard_title(request, "tv", sid)
    from . import series as series_mod
    return series_mod.episode_breakdown(sid)


@app.post("/api/series/{sid}/hunt")
def api_series_hunt(sid: int):
    from . import series as series_mod
    return series_mod.hunt_series(sid)


@app.get("/api/series/{sid}/wanted")
def api_series_wanted(sid: int, request: _Request):
    _guard_title(request, "tv", sid)
    from . import series as series_mod
    return {"wanted": [w for w in series_mod.list_wanted() if w.get("series_id") == sid]}


@app.get("/api/series/{sid}/episodes/{season}/{episode}/releases")
def api_episode_releases(sid: int, season: int, episode: int):
    """Interactive per-episode search (the Sonarr hourglass): every release the
    indexer returns for this exact episode, title-verified so spin-offs and
    season packs never appear, ranked by the show's profile. Releases that
    fail the profile's hard constraints are still listed (marked with the
    reason) so the admin can override deliberately."""
    from . import series as series_mod, releasematch, packs
    from . import profiles as prof
    from . import scheduler as sched
    s = series_mod.get_series(sid)
    if not s:
        raise HTTPException(404, "Series not found")
    query = f"{s['title']} S{season:02d}E{episode:02d}"
    from . import finder
    try:
        results = finder.find("episode", title=s["title"], season=season, episode=episode,
                              ids=finder.ids_for("episode", sid))
    except searchmod.SearchError as e:
        raise HTTPException(502, str(e))
    matched = [r for r in results
               if packs.classify_pack(r["title"]).get("kind") == "single"
               and releasematch.matches_episode(r["title"], s["title"],
                                                season, episode)]
    profile = sched._load_profile_for_series(sid)
    out = []
    for r in matched:
        rr = dict(r)
        if profile:
            ok, why = prof.passes(r, profile)
            rr["_passes"], rr["_why"] = ok, why
            rr["_score"] = prof.score(r, profile) if ok else 0
        else:
            rr["_passes"], rr["_why"], rr["_score"] = True, "", r.get("seeders", 0)
        out.append(rr)
    out.sort(key=lambda x: (not x["_passes"], -x["_score"], -x.get("seeders", 0)))
    return {"query": query, "profile": profile["name"] if profile else None,
            "considered": len(results), "releases": out,
            "hidden": getattr(results, "hidden", 0), **_search_meta(results)}


@app.get("/api/series/{sid}/seasons/{season}/releases")
def api_season_releases(sid: int, season: int):
    """Interactive season-pack search: verified packs for this exact season."""
    from . import series as series_mod, releasematch, packs
    from . import profiles as prof
    from . import scheduler as sched
    s = series_mod.get_series(sid)
    if not s:
        raise HTTPException(404, "Series not found")
    query = f"{s['title']} S{season:02d}"
    from . import finder
    try:
        results = finder.find("season", title=s["title"], season=season,
                              ids=finder.ids_for("season", sid))
    except searchmod.SearchError as e:
        raise HTTPException(502, str(e))
    matched = []
    for r in results:
        if not releasematch.matches_series(r["title"], s["title"]):
            continue
        cls = packs.classify_pack(r["title"])
        if cls["kind"] == "season" and cls["season"] == season:
            matched.append(r)
    profile = sched._load_profile_for_series(sid)
    out = []
    for r in matched:
        rr = dict(r)
        if profile:
            ok, why = prof.passes(r, profile)
            rr["_passes"], rr["_why"] = ok, why
            rr["_score"] = prof.score(r, profile) if ok else 0
        else:
            rr["_passes"], rr["_why"], rr["_score"] = True, "", r.get("seeders", 0)
        out.append(rr)
    out.sort(key=lambda x: (not x["_passes"], -x["_score"], -x.get("seeders", 0)))
    return {"query": query, "profile": profile["name"] if profile else None,
            "considered": len(results), "releases": out,
            "hidden": getattr(results, "hidden", 0), **_search_meta(results)}


@app.post("/api/series/{sid}/seasons/{season}/grab")
def api_season_grab(sid: int, season: int, req: AddRequest):
    """Grab a season pack from the interactive search: adds to the client,
    blocklists the release, and flips every outstanding want in that season to
    'grabbed' so the hunter stands down."""
    from . import series as series_mod
    from datetime import datetime as _dt
    s = series_mod.get_series(sid)
    if not s:
        raise HTTPException(404, "Series not found")
    if not req.magnet:
        raise HTTPException(400, "No magnet/href provided.")
    try:
        res = client().add(req.magnet, cfg().download_dir or None)
        _refresh_transfers()
    except DownloadClientError as e:
        raise HTTPException(502, str(e))
    title = req.title or res.name or ""
    if title:
        db.mark_grabbed(title, None)
    db.add_history("added", title,
                   f"manual season pack: {s['title']} S{season:02d}")
    with db.connect() as c:
        c.execute("UPDATE wanted SET status='grabbed', last_search=?, "
                  "search_count=0, next_search=NULL "
                  "WHERE kind='episode' AND series_id=? AND season=? "
                  "AND status='wanted'",
                  (_dt.now().isoformat(timespec="seconds"), sid, season))
    return {"status": "ok", "id": res.id, "name": res.name, "duplicate": res.duplicate}


@app.post("/api/series/{sid}/episodes/{season}/{episode}/grab")
def api_episode_grab(sid: int, season: int, episode: int, req: AddRequest):
    """Grab a specific release for a specific episode (from the interactive
    search modal). Unlike raw /api/add this also records the release in the
    grabbed table and flips the episode's wanted row to 'grabbed', so the
    background hunter won't double-grab a different release meanwhile."""
    from . import series as series_mod
    from datetime import datetime as _dt
    s = series_mod.get_series(sid)
    if not s:
        raise HTTPException(404, "Series not found")
    if not req.magnet:
        raise HTTPException(400, "No magnet/href provided.")
    try:
        res = client().add(req.magnet, cfg().download_dir or None)
        _refresh_transfers()
    except DownloadClientError as e:
        raise HTTPException(502, str(e))
    title = req.title or res.name or ""
    if title:
        db.mark_grabbed(title, None)
    db.add_history("added", title,
                   f"manual grab: {s['title']} S{season:02d}E{episode:02d}")
    with db.connect() as c:
        c.execute("UPDATE wanted SET status='grabbed', last_search=?, "
                  "search_count=0, next_search=NULL "
                  "WHERE kind='episode' AND series_id=? AND season=? AND episode=?",
                  (_dt.now().isoformat(timespec="seconds"), sid, season, episode))
    return {"status": "ok", "id": res.id, "name": res.name, "duplicate": res.duplicate}


# ---------------------------------------------------------------------------
# Detail pages (new UI): one call each
# ---------------------------------------------------------------------------
_still_fetching: set = set()


def _fill_stills_async(tmdb_id: int, seasons: list[int]) -> None:
    """Fetch seasons whose stills aren't cached yet, in the background, so
    the page renders now and has them on the next visit."""
    import threading
    todo = [n for n in seasons if (tmdb_id, n) not in _still_fetching]
    if not todo or not tmdbmod.enabled():
        return
    for n in todo:
        _still_fetching.add((tmdb_id, n))

    def run():
        try:
            for n in todo:
                try:
                    tmdbmod.season_episodes(tmdb_id, n)
                except Exception:                # noqa: BLE001 - art is optional
                    pass
        finally:
            for n in todo:
                _still_fetching.discard((tmdb_id, n))
    threading.Thread(target=run, name="tmdb-stills", daemon=True).start()


def _profiles_for(user: dict) -> list:
    if user.get("role") != "admin":
        return []
    with db.connect() as c:
        return [{"id": r["id"], "name": r["name"]}
                for r in c.execute("SELECT id, name FROM profiles ORDER BY name")]


def _save_to(kind: str, title_id: int, chosen) -> dict:
    """Where a title's downloads are filed, and the libraries it could be."""
    from . import libraries
    cur = libraries.title_library(kind, title_id)
    return {"library_id": chosen, "current": cur["name"] if cur else None,
            "options": [{"id": lib["id"], "name": lib["name"], "path": lib["path"],
                         "default": bool(lib["is_default"])} for lib in libraries.of_kind(kind)]}


class SaveToBody(BaseModel):
    library_id: int | None = None       # None = follow the default library


@app.post("/api/series/{sid}/library")
def api_series_library(sid: int, body: SaveToBody):
    from . import libraries
    try:
        libraries.set_title_library("tv", sid, body.library_id)
    except LookupError:
        raise HTTPException(404, "Series not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _save_to("tv", sid, body.library_id)


@app.post("/api/movies/{mid}/library")
def api_movie_library(mid: int, body: SaveToBody):
    from . import libraries
    try:
        libraries.set_title_library("movie", mid, body.library_id)
    except LookupError:
        raise HTTPException(404, "Movie not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _save_to("movie", mid, body.library_id)


@app.get("/api/series/{sid}/detail")
def api_series_detail(sid: int, request: _Request):
    """Everything the show page needs: the series, TMDb metadata, every
    season with episode status (on disk / downloading / wanted / unaired /
    not monitored), stills, and — admins — profiles and folder links."""
    _guard_title(request, "tv", sid)
    from . import series as series_mod
    user = getattr(request.state, "user", None) or {}
    bd = series_mod.episode_breakdown(sid)
    s = bd.get("series")
    if not s:
        raise HTTPException(404, "Series not found")
    meta = tmdbmod.details(s["tmdb_id"], "tv") if s.get("tmdb_id") else {}
    with db.connect() as c:
        wants = {(r["season"], r["episode"]): dict(r) for r in c.execute(
            "SELECT season, episode, status, reason, next_search, search_count, last_search "
            "FROM wanted WHERE kind='episode' AND series_id=?", (sid,))}
    mode = s.get("monitor_mode") or "all"
    missing_stills = []
    seasons = []
    from . import plex as _plex
    watched = _plex.watched_episodes(s["tmdb_id"]) if s.get("tmdb_id") else {}
    for sn in bd["seasons"]:
        cached = tmdbmod.cached_season(s["tmdb_id"], sn["season"]) if s.get("tmdb_id") else None
        if cached is None and s.get("tmdb_id"):
            missing_stills.append(sn["season"])
        stills = {e["episode"]: e.get("still") for e in (cached or [])}
        eps, counts = [], {"have": 0, "downloading": 0, "wanted": 0, "unaired": 0}
        for e in sn["episodes"]:
            w = wants.get((sn["season"], e["episode"]))
            if e["have"] and not (w and w["reason"] == "upgrade" and w["status"] == "grabbed"):
                status = "have"
            elif w and w["status"] == "grabbed":
                status = "downloading"
            elif not e["aired"]:
                status = "unaired"
            elif w:
                status = "wanted"
            else:
                status = "skipped" if (not s.get("monitored") or mode == "none") else "missing"
            if status in counts:
                counts[status] += 1
            seen = watched.get((sn["season"], e["episode"]))
            eps.append({**e, "status": status, "still": stills.get(e["episode"]),
                        "upgrade": bool(w and w["reason"] == "upgrade"),
                        "next_search": w["next_search"] if w else None,
                        "searches": w["search_count"] if w else 0,
                        "watched": seen["last_viewed"] or True if seen else None})
        seasons.append({"season": sn["season"], "have": sn["have"], "total": sn["total"],
                        "watched": sum(1 for x in eps if x["watched"]),
                        "episodes": eps, **{k: v for k, v in counts.items() if k != "have"}})
    if missing_stills:
        _fill_stills_async(s["tmdb_id"], missing_stills)
    from . import libraries as _libs
    _lib = _libs.title_library("tv", sid)
    out = {"series": s, "meta": meta, "seasons": seasons,
           "library": {"id": _lib["id"], "name": _lib["name"]} if _lib else None,
           "have": bd["have"], "total": bd["total"],
           "wanted": sum(x["wanted"] for x in seasons),
           "downloading": sum(x["downloading"] for x in seasons),
           "profiles": _profiles_for(user)}
    if user.get("role") == "admin":
        from . import libraries
        out["save_to"] = _save_to("tv", sid, s.get("library_id"))
        out["folders"] = [{"library": x["library"], "library_id": x["library_id"],
                           "folder": x["folder"], "path": x["library_path"].rstrip("/") + "/" + x["folder"],
                           "manual": not x["auto"]} for x in libraries.links(sid)]
    return out


@app.get("/api/movies/{mid}/releases")
def api_movie_releases(mid: int):
    """Interactive search for one movie: title-verified, ranked by its profile,
    with releases that fail the profile listed (and why) for a deliberate
    override."""
    from . import movies as movies_mod, releasematch
    from . import profiles as prof
    from . import scheduler as sched
    from . import finder
    m = movies_mod.get_movie(mid)
    if not m:
        raise HTTPException(404, "Movie not found")
    try:
        results = finder.find("movie", title=m["title"], year=m.get("year"),
                              ids=finder.ids_for("movie", mid))
    except searchmod.SearchError as e:
        raise HTTPException(502, str(e))
    matched = [r for r in results
               if releasematch.matches_movie(r["title"], m["title"], m.get("year"))]
    profile = sched._load_profile_for_movie(mid)
    out = []
    for r in matched:
        rr = dict(r)
        if profile:
            ok, why = prof.passes(r, profile)
            rr["_passes"], rr["_why"] = ok, why
            rr["_score"] = prof.score(r, profile) if ok else 0
        else:
            rr["_passes"], rr["_why"], rr["_score"] = True, "", r.get("seeders", 0)
        out.append(rr)
    out.sort(key=lambda x: (not x["_passes"], -x["_score"], -x.get("seeders", 0)))
    return {"query": f"{m['title']} {m.get('year') or ''}".strip(),
            "profile": profile["name"] if profile else None,
            "considered": len(results), "releases": out,
            "hidden": getattr(results, "hidden", 0), **_search_meta(results)}


@app.post("/api/movies/{mid}/grab")
def api_movie_grab(mid: int, req: AddRequest):
    """Grab a release for this movie: adds it, records it, and flips the
    movie's want to 'grabbed' so the hunter stands down."""
    from . import movies as movies_mod
    from datetime import datetime as _dt
    m = movies_mod.get_movie(mid)
    if not m:
        raise HTTPException(404, "Movie not found")
    if not req.magnet:
        raise HTTPException(400, "No magnet/href provided.")
    try:
        res = client().add(req.magnet, cfg().download_dir or None)
        _refresh_transfers()
    except DownloadClientError as e:
        raise HTTPException(502, str(e))
    title = req.title or res.name or ""
    if title:
        db.mark_grabbed(title, None)
    db.add_history("added", title, f"manual grab: {m['title']} ({m.get('year') or '?'})")
    with db.connect() as c:
        c.execute("UPDATE wanted SET status='grabbed', last_search=?, search_count=0, "
                  "next_search=NULL WHERE kind='movie' AND series_id=?",
                  (_dt.now().isoformat(timespec="seconds"), mid))
    return {"status": "ok", "id": res.id, "name": res.name, "duplicate": res.duplicate}


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


@app.get("/api/wanted/summary")
def api_wanted_summary():
    """What the hunter is after, per show / movie: counts, upgrades, and when
    each title is next searched (the soonest of its wants)."""
    from datetime import datetime as _dt
    now = _dt.now().isoformat(timespec="seconds")
    with db.connect() as c:
        rows = c.execute(
            "SELECT w.kind, w.series_id, w.status, w.reason, w.next_search, w.search_count, "
            "s.title AS s_title, s.poster AS s_poster, m.title AS m_title, m.poster AS m_poster, "
            "m.year AS m_year FROM wanted w "
            "LEFT JOIN series s ON w.kind='episode' AND s.id=w.series_id "
            "LEFT JOIN movies m ON w.kind='movie' AND m.id=w.series_id "
            "WHERE w.status IN ('wanted','grabbed')").fetchall()
    groups: dict = {}
    for r in rows:
        kind = "movie" if r["kind"] == "movie" else "show"
        key = (kind, r["series_id"])
        g = groups.setdefault(key, {
            "kind": kind, "id": r["series_id"],
            "title": (r["m_title"] if kind == "movie" else r["s_title"]) or "?",
            "poster": r["m_poster"] if kind == "movie" else r["s_poster"],
            "year": r["m_year"] if kind == "movie" else None,
            "wanted": 0, "downloading": 0, "upgrades": 0, "searches": 0, "next_search": None,
            "due": 0})
        if r["status"] == "grabbed":
            g["downloading"] += 1
            continue
        g["wanted"] += 1
        g["searches"] += r["search_count"] or 0
        if r["reason"] == "upgrade":
            g["upgrades"] += 1
        ns = r["next_search"]
        if not ns or ns <= now:
            g["due"] += 1
        elif not g["next_search"] or ns < g["next_search"]:
            g["next_search"] = ns
    items = sorted(groups.values(), key=lambda g: (-g["due"], -g["wanted"], g["title"].lower()))
    return {"items": items,
            "totals": {"wanted": sum(g["wanted"] for g in items),
                       "downloading": sum(g["downloading"] for g in items),
                       "upgrades": sum(g["upgrades"] for g in items),
                       "due": sum(g["due"] for g in items)}}


@app.get("/api/wanted/log")
def api_wanted_log(kind: str = Query(..., pattern="^(episode|movie)$"),
                   series_id: int = Query(...), season: int | None = None,
                   episode: int | None = None,
                   limit: int = Query(100, ge=1, le=500)):
    """Hunt history for a show (kind=episode, optionally one season/episode)
    or a movie (kind=movie, series_id = movie id), newest first, plus the
    current want row(s) with their backoff state."""
    from . import huntlog
    entries = huntlog.entries(kind=kind, series_id=series_id, season=season,
                              episode=episode, limit=limit)
    q = "SELECT * FROM wanted WHERE kind=? AND series_id=?"
    args: list = [kind, series_id]
    if season is not None:
        q += " AND season=?"
        args.append(season)
    if episode is not None:
        q += " AND episode=?"
        args.append(episode)
    with db.connect() as c:
        wants = [dict(r) for r in c.execute(q + " ORDER BY season, episode", args)]
    return {"entries": entries, "wants": wants}


@app.get("/api/indexers/health")
def api_indexer_health():
    from . import indexerhealth
    return indexerhealth.report()


@app.post("/api/indexers/probe")
def api_indexer_probe():
    """Starts a probe in the background; poll /api/indexers/health until
    `probing` is false."""
    from . import indexerhealth
    started = indexerhealth.probe_async()
    return {"started": started, "probing": True}


@app.get("/api/library/duplicates")
def api_library_duplicates():
    from . import library
    return library.duplicates()


# --- libraries, folder links, path mappings ---
class LibraryBody(BaseModel):
    name: str | None = None
    kind: str | None = None
    path: str | None = None
    is_default: bool | None = None
    enabled: bool | None = None


def _library_view(lib: dict) -> dict:
    p = lib["path"]
    exists = os.path.isdir(p)
    with db.connect() as c:
        prefix = p.rstrip("/") + "/"
        table = "library_episodes" if lib["kind"] == "tv" else "library_movies"
        files = c.execute(f"SELECT COUNT(DISTINCT path) FROM {table} WHERE substr(path,1,?)=?",
                          (len(prefix), prefix)).fetchone()[0]
    return {**lib, "exists": exists,
            "writable": exists and os.access(p, os.W_OK), "files": files}


def _downloads_view() -> dict:
    """Where finished downloads land (as Faucet sees them) and its mount."""
    from . import mounts, sweep
    d = str(sweep._complete_dir())
    m = mounts.mount_of(d) if os.path.isdir(d) else None
    return {"path": d, "exists": os.path.isdir(d),
            "mount": m["point"] if m else None,
            "configured": bool(os.environ.get("COMPLETE_DIR"))}


def _moves(lib_path: str, dl: dict) -> str | None:
    """'rename' when a finished download can be moved into the library by
    renaming, 'copy' when it has to be copied, None when unknown."""
    from . import mounts
    if not dl["exists"] or not os.path.isdir(lib_path):
        return None
    same = mounts.same_mount(dl["path"], lib_path)
    return None if same is None else ("rename" if same else "copy")


@app.get("/api/libraries")
def api_libraries():
    from . import libraries, mounts
    dl = _downloads_view()
    libs = []
    for lib in libraries.all_libraries():
        v = _library_view(lib)
        m = mounts.mount_of(lib["path"]) if v["exists"] else None
        v.update(mount=m["point"] if m else None, moves=_moves(lib["path"], dl))
        libs.append(v)
    return {"libraries": libs, "mappings": libraries.mappings(), "downloads": dl}


@app.post("/api/libraries")
def api_libraries_add(body: LibraryBody):
    from . import libraries
    try:
        lid, warnings = libraries.add(body.name or "", body.kind or "", body.path or "",
                                      bool(body.is_default))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok", "id": lid, "warnings": warnings,
            "library": _library_view(libraries.get(lid))}


@app.patch("/api/libraries/{lib_id}")
def api_libraries_update(lib_id: int, body: LibraryBody):
    from . import libraries
    try:
        warnings = libraries.update(lib_id, **body.model_dump(exclude={"kind"}))
    except LookupError:
        raise HTTPException(404, "No such library")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok", "warnings": warnings,
            "library": _library_view(libraries.get(lib_id))}


@app.delete("/api/libraries/{lib_id}")
def api_libraries_delete(lib_id: int):
    from . import libraries
    if not libraries.get(lib_id):
        raise HTTPException(404, "No such library")
    libraries.remove(lib_id)
    return {"status": "ok"}


_scan_thread = None


@app.post("/api/libraries/scan")
def api_libraries_scan(force: bool = False):
    """Scan every library in the background (a full scan over a network
    share can outlast a reverse proxy's request timeout)."""
    import threading
    from . import library
    global _scan_thread
    if _scan_thread is not None and _scan_thread.is_alive():
        return {"started": False, "running": True}
    _scan_thread = threading.Thread(target=library.scan, kwargs={"force": force},
                                    name="library-scan", daemon=True)
    _scan_thread.start()
    return {"started": True, "running": True}


@app.get("/api/libraries/folders")
def api_library_folders():
    """Show folders in every TV library with the series each is linked to
    (or the candidates when it couldn't be linked automatically)."""
    from . import libraries
    linked = {(x["library_id"], x["folder"]): x for x in libraries.links()}
    out = []
    for lib in libraries.of_kind("tv"):
        for folder in libraries.folders(lib["path"]):
            x = linked.get((lib["id"], folder))
            out.append({"library_id": lib["id"], "library": lib["name"], "folder": folder,
                        "series_id": x["series_id"] if x else None,
                        "series": x["series"] if x else None,
                        "manual": bool(x and not x["auto"])})
    return {"folders": out}


class LinkBody(BaseModel):
    folder: str
    series_id: int | None = None      # None = unlink


@app.put("/api/libraries/{lib_id}/links")
def api_library_link(lib_id: int, body: LinkBody):
    """Link a show folder to a series by hand (or unlink it). A manual link
    is never replaced by automatic matching."""
    from . import libraries, series as series_mod
    lib = libraries.get(lib_id)
    if not lib or lib["kind"] != "tv":
        raise HTTPException(404, "No such TV library")
    if body.folder not in libraries.folders(lib["path"]):
        raise HTTPException(404, "No such folder in that library")
    if body.series_id is None:
        libraries.unlink_folder(lib_id, body.folder)
    else:
        if not series_mod.get_series(body.series_id):
            raise HTTPException(404, "No such series")
        libraries.link_folder(lib_id, body.folder, body.series_id, auto=False)
        series_mod.reconcile(body.series_id)
    return {"status": "ok"}


def _browse_roots() -> list[str]:
    """Top-level folders the library picker may browse: the mounts Faucet is
    configured with (BROWSE_ROOT, LIBRARY_ROOT, the download folders) and the
    folder above each existing library. Directories only — this lists names,
    never file contents."""
    from . import libraries
    c = cfg()
    cands = [c.browse_root, os.environ.get("LIBRARY_ROOT", ""), c.download_dir, c.disk_path]
    cands += [str(Path(lib["path"]).parent) for lib in libraries.all_libraries()]
    roots: list[str] = []
    for r in cands:
        if not r:
            continue
        try:
            rp = str(Path(r).resolve())
        except OSError:
            continue
        if rp != "/" and os.path.isdir(rp) and rp not in roots:
            roots.append(rp)
    # drop roots nested inside another root
    return sorted(r for r in roots if not any(r != o and r.startswith(o + "/") for o in roots))


@app.get("/api/libraries/browse")
def api_library_browse(path: str = ""):
    """Folders for the library picker (admin). Without a path: the roots."""
    from . import libraries
    roots = _browse_roots()
    libs = {lib["path"].rstrip("/"): lib for lib in libraries.all_libraries()}
    if not path:
        return {"roots": roots, "path": None, "parent": None,
                "dirs": [{"name": r, "path": r, "library": (libs.get(r) or {}).get("name")}
                         for r in roots]}
    try:
        p = Path(path).resolve()
    except OSError:
        raise HTTPException(400, "Bad path")
    sp = str(p)
    root = next((r for r in roots if sp == r or sp.startswith(r + "/")), None)
    if root is None or not p.is_dir():
        raise HTTPException(404, "Not a folder Faucet can browse")
    dirs = []
    try:
        for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
            if e.name.startswith(".") or not e.is_dir(follow_symlinks=False):
                continue
            ep = str(Path(e.path))
            lib = libs.get(ep)
            inside = libraries.containing(ep)
            dirs.append({"name": e.name, "path": ep,
                         "library": lib["name"] if lib else None,
                         "in_library": (inside or {}).get("name") if not lib else None})
            if len(dirs) >= 500:
                break
    except OSError as e:
        raise HTTPException(403, f"Can't read {sp}: {e.strerror}")
    return {"roots": roots, "path": sp,
            "parent": None if sp == root else str(p.parent),
            "writable": os.access(sp, os.W_OK), "dirs": dirs}


# --- notifications: targets are write-only (they carry webhook tokens) ---
_NOTIFY_EVENTS = ["added", "completed", "sorted", "failed", "suspicious"]
_NOTIFY_SCHEMES = ("https://", "http://", "discord://", "telegram://", "ntfy://",
                   "ntfys://", "gotify://")


def _notify_kind(url: str) -> str:
    if url.startswith("discord://") or "discord.com/api/webhooks" in url:
        return "Discord"
    for pre, name in (("telegram://", "Telegram"), ("ntfy", "ntfy"), ("gotify://", "Gotify")):
        if url.startswith(pre):
            return name
    return "Webhook"


def _notify_display(url: str) -> str:
    """Enough to recognise a target, nothing that would let you post to it."""
    from urllib.parse import urlsplit
    # host only: on ntfy.sh the topic IS the secret, on Discord the path is
    u = urlsplit(url)
    return f"{u.scheme}://{u.hostname or ''}/…"


@app.get("/api/settings/notifications")
def api_notifications_get():
    c = cfg()
    return {"targets": [{"i": i, "kind": _notify_kind(u), "display": _notify_display(u)}
                        for i, u in enumerate(c.notify_urls)],
            "notify_on": c.notify_on, "events": _NOTIFY_EVENTS}


class NotifyBody(BaseModel):
    keep: list[int] = []
    add: list[str] = []
    notify_on: list[str] | None = None


@app.put("/api/settings/notifications")
def api_notifications_put(body: NotifyBody):
    """Rebuild the target list from the indexes to keep plus new URLs (the
    existing URLs are never sent to the browser, so they can't be resent)."""
    from .config import save
    c = cfg()
    urls = [c.notify_urls[i] for i in body.keep if 0 <= i < len(c.notify_urls)]
    for u in body.add:
        u = (u or "").strip()
        if not u:
            continue
        if not u.startswith(_NOTIFY_SCHEMES) or any(ch in u for ch in ",\n\r\t "):
            raise HTTPException(400, f"Not a notification URL: {u[:40]}…")
        urls.append(u)
    values = {"NOTIFY_URLS": ",".join(dict.fromkeys(urls))}
    if body.notify_on is not None:
        bad = [e for e in body.notify_on if e not in _NOTIFY_EVENTS]
        if bad:
            raise HTTPException(400, f"Unknown events: {', '.join(bad)}")
        values["NOTIFY_ON"] = ",".join(body.notify_on)
    try:
        save(values)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return api_notifications_get()


class NotifyTest(BaseModel):
    i: int | None = None
    url: str | None = None


@app.post("/api/settings/notifications/test")
def api_notifications_test(body: NotifyTest):
    """Send a test message to one saved target (by index) or a typed URL."""
    from . import notify as notify_mod
    from .redact import redact
    c = cfg()
    url = c.notify_urls[body.i] if body.i is not None and 0 <= body.i < len(c.notify_urls) \
        else (body.url or "").strip()
    if not url or not url.startswith(_NOTIFY_SCHEMES):
        raise HTTPException(400, "Nothing to test")
    try:
        notify_mod._dispatch(url, "Faucet test", "If you can read this, notifications work.")
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "msg": redact(e, url)}
    return {"ok": True, "msg": f"Sent to {_notify_kind(url)}."}


class TmdbTest(BaseModel):
    key: str | None = None


@app.post("/api/settings/test-tmdb")
def api_settings_test_tmdb(body: TmdbTest):
    """Check a TMDb key (typed, or the saved one) with one cheap API call."""
    import requests as _rq
    key = (body.key or "").strip() or tmdbmod.api_key()
    if not key:
        return {"ok": False, "msg": "No TMDb key set."}
    try:
        r = _rq.get("https://api.themoviedb.org/3/configuration", params={"api_key": key},
                    timeout=10)
    except _rq.RequestException as e:
        return {"ok": False, "msg": f"Couldn't reach TMDb: {type(e).__name__}"}
    if r.status_code == 401:
        return {"ok": False, "msg": "TMDb rejected the key."}
    if r.status_code != 200:
        return {"ok": False, "msg": f"TMDb answered HTTP {r.status_code}."}
    return {"ok": True, "msg": "TMDb key accepted."}


# --- behaviour settings (Settings → General / Hunting / Maintenance) ---
@app.get("/api/settings/tunables")
def api_tunables(section: str = Query(..., pattern="^(general|hunting|maintenance|alerts)$")):
    from . import tunables
    groups: dict = {}
    for k, s in tunables.SPEC.items():
        if s["section"] != section:
            continue
        cur = os.environ.get(k)
        value = cur if cur not in (None, "") else tunables.default_str(k)
        groups.setdefault(s["group"], []).append({
            **{x: s[x] for x in ("key", "label", "kind", "unit", "help", "min", "max")},
            "choices": [{"value": v, "label": lbl} for v, lbl in (s["choices"] or [])],
            "default": tunables.default_str(k), "value": value,
            "changed": value != tunables.default_str(k)})
    return {"section": section, "groups": [{"name": g, "items": items} for g, items in groups.items()]}


class TunablesBody(BaseModel):
    values: dict


@app.put("/api/settings/tunables")
def api_tunables_put(body: TunablesBody):
    """Validate and store; values apply on the next tick (the code reads them
    from the environment, which the store updates immediately)."""
    from . import tunables
    from .config import save
    unknown = [k for k in body.values if k not in tunables.KEYS]
    if unknown:
        raise HTTPException(400, f"Not a behaviour setting: {', '.join(unknown)}")
    try:
        clean = {k: tunables.validate(k, v) for k, v in body.values.items()}
        save(clean)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok", "saved": sorted(clean)}


# --- first-run setup ---
_import_job: dict = {"running": False, "result": None, "started": None}


def _setup_complete() -> bool:
    """Done once finished in the wizard — or, for installs that predate it,
    as soon as they're plainly configured (they never see the wizard)."""
    if db.get_setting("setup_complete"):
        return True
    with db.connect() as c:
        titles = c.execute("SELECT (SELECT COUNT(*) FROM series) + (SELECT COUNT(*) FROM movies)").fetchone()[0]
    if cfg().configured() and titles:
        db.set_setting("setup_complete", "1")
        return True
    return False


@app.get("/api/setup/state")
def api_setup_state():
    from . import libraries
    c = cfg()
    libs = libraries.all_libraries()
    with db.connect() as conn:
        shows = conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        movies = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
    return {"complete": _setup_complete(),
            "steps": {"libraries": any(os.path.isdir(lib["path"]) for lib in libs),
                      "client": bool(c.client_url and (c.client_pass or c.client_user)),
                      "indexer": bool(c.jackett_api_key),
                      "tmdb": tmdbmod.enabled()},
            "libraries": [{"name": lib["name"], "kind": lib["kind"], "path": lib["path"],
                           "exists": os.path.isdir(lib["path"])} for lib in libs],
            "shows": shows, "movies": movies,
            "import": {k: v for k, v in _import_job.items()}}


def _start_import(library_ids=None, monitor: str = "all") -> dict:
    import threading
    from datetime import datetime as _dt
    from . import importer
    if _import_job["running"]:
        return {"started": False, "running": True}
    if not tmdbmod.enabled():
        raise HTTPException(400, "Add a TMDb key first — importing matches folders against TMDb.")

    def run():
        try:
            _import_job["result"] = importer.import_library(None, library_ids=library_ids,
                                                            monitor=monitor)
        except Exception as e:                   # noqa: BLE001
            _import_job["result"] = {"error": str(e)}
        finally:
            _import_job["running"] = False

    _import_job.update(running=True, result=None, library_ids=library_ids,
                       started=_dt.now().isoformat(timespec="seconds"))
    threading.Thread(target=run, name="library-import", daemon=True).start()
    return {"started": True, "running": True}


@app.post("/api/setup/import")
def api_setup_import():
    """Import what's already on disk (monitor every show and movie found),
    in the background — matching a big library against TMDb takes minutes."""
    return _start_import()


class ImportBody(BaseModel):
    monitor: str = "all"


@app.post("/api/libraries/{lib_id}/import")
def api_library_import_one(lib_id: int, body: ImportBody):
    """Import one library's titles (e.g. a relative's folder) without adopting
    everything else on disk; each is filed into that library."""
    from . import libraries
    if not libraries.get(lib_id):
        raise HTTPException(404, "No such library")
    if body.monitor not in ("all", "future", "none"):
        raise HTTPException(400, "monitor: all, future or none")
    return _start_import([lib_id], body.monitor)


@app.get("/api/libraries/import")
def api_library_import_state():
    return {k: v for k, v in _import_job.items()}


@app.post("/api/setup/complete")
def api_setup_complete():
    db.set_setting("setup_complete", "1")
    return {"complete": True}


def _public_base(request) -> str:
    """The URL people reach Faucet at: https when the browser connection is
    (directly or per a trusted proxy — Cloudflare → Caddy → here is plain
    http on the last hop)."""
    from . import netutil
    host = request.headers.get("host") or request.url.netloc
    return f"{'https' if netutil.is_https(request) else 'http'}://{host}"


# --- Plex ---
class PlexTestBody(BaseModel):
    url: str | None = None
    token: str | None = None          # omitted: the saved one


@app.post("/api/plex/test")
def api_plex_test(body: PlexTestBody):
    from . import plex
    url = (body.url or "").strip().rstrip("/") or None
    if url and not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "The address starts with http:// or https://")
    return plex.test(url, (body.token or "").strip() or None)


@app.get("/api/plex")
def api_plex():
    """Connection, the server's libraries, and how each Faucet library is
    paired (auto-pairing anything not set by hand)."""
    from . import libraries, plex
    out = {"configured": plex.enabled(), "url": plex.settings()[0], "ok": None,
           "sections": [], "pairs": [], "scans": plex.recent_scans(),
           "watched": plex.watched_status()}
    if not plex.enabled():
        return out
    t = plex.test()
    out.update(ok=t["ok"], msg=t.get("msg"), name=t.get("name"), version=t.get("version"))
    secs = t.get("sections") or []
    out["sections"] = secs
    if t["ok"]:
        plex.auto_pair(secs)
    links = plex.links()
    for lib in libraries.all_libraries():
        lk = links.get(lib["id"]) or {}
        out["pairs"].append({"library_id": lib["id"], "name": lib["name"], "kind": lib["kind"],
                             "path": lib["path"], "section_key": lk.get("section_key"),
                             "section_title": lk.get("section_title"), "plex_path": lk.get("plex_path"),
                             "auto": bool(lk.get("auto", 1)), "set": bool(lk)})
    return out


@app.post("/api/plex/watched/sync")
def api_plex_watched_sync():
    """Read what's been watched in Plex now, instead of at the next tick."""
    from . import plex
    try:
        r = plex.sync_watched()
    except plex.PlexError as e:
        raise HTTPException(502, str(e))
    if not r.get("ok"):
        raise HTTPException(400, r.get("msg") or "Plex isn't set up")
    return {**r, **plex.watched_status()}


class PlexPairBody(BaseModel):
    library_id: int
    section_key: str | None = None   # None = don't pair this library
    plex_path: str | None = None


@app.put("/api/plex/pairs")
def api_plex_pair(body: PlexPairBody):
    from . import libraries, plex
    if not libraries.get(body.library_id):
        raise HTTPException(404, "No such library")
    secs = plex.sections() if body.section_key else []
    try:
        plex.set_link(body.library_id, body.section_key, body.plex_path, secs)
    except (ValueError, plex.PlexError) as e:
        raise HTTPException(400, str(e))
    return {"status": "ok"}


class PlexScanBody(BaseModel):
    library_id: int


@app.post("/api/plex/scan")
def api_plex_scan(body: PlexScanBody):
    """Scan a paired library's whole folder in Plex now."""
    from . import plex
    link = plex.links().get(body.library_id)
    if not link or not link["section_key"]:
        raise HTTPException(400, "That library isn't paired with a Plex library.")
    ok = plex.scan_folder(link["section_key"], link["plex_path"])
    if not ok:
        raise HTTPException(502, plex.recent_scans(1)[0]["error"] if plex.recent_scans(1) else "Plex scan failed")
    return {"status": "ok"}


@app.get("/api/grabs")
def api_grabs(days: int = Query(30, ge=1, le=365), limit: int = Query(300, ge=1, le=1000),
              q: str = Query("", max_length=200)):
    """Every grab — automatic, season pack, manual — with why it was picked
    (for automatic ones: found / kept / rejected-by-reason, tracker, seeders)
    and what became of it (done, problem, removed, still pending)."""
    from . import grabhistory
    return {"grabs": grabhistory.grabs(days, limit, q.strip())}


@app.get("/api/blocklist")
def api_blocklist(filter: str = Query("problems", pattern="^(problems|all)$"),
                  q: str = Query("", max_length=200)):
    """Releases the hunter won't grab again. 'problems' = the ones that went
    wrong (stalled, failed, flagged); 'all' includes every past grab."""
    from . import grabhistory
    return grabhistory.blocklist(filter == "problems", q.strip())


@app.delete("/api/blocklist/{entry_id}")
def api_blocklist_allow(entry_id: int):
    """Allow a release again (e.g. a stall that was the tracker's fault)."""
    from . import grabhistory
    title = grabhistory.allow(entry_id)
    if title is None:
        raise HTTPException(404, "Not on the blocklist")
    log.info("blocklist: allowed %s again", title)
    return {"status": "ok", "release": title}


@app.get("/api/alerts")
def api_alerts():
    """Conditions that need attention right now (also on Home for admins)."""
    from . import alerts
    return {"alerts": alerts.active()}


@app.post("/api/alerts/check")
def api_alerts_check():
    """Run the checks now instead of waiting for the next few minutes."""
    from . import alerts
    r = alerts.check()
    return {**r, "alerts": alerts.active()}


# --- personal calendar feed ---
@app.get("/api/me/calendar")
def api_my_calendar(request: _Request):
    from . import icalfeed
    u = _user(request)
    return {"feed": icalfeed.status(u["id"]) if u.get("id") else None}


@app.post("/api/me/calendar")
def api_my_calendar_create(request: _Request):
    """A new private calendar link (replaces the old one). Shown once."""
    from . import icalfeed
    u = _user(request)
    if not u.get("id"):
        raise HTTPException(401, "Sign in")
    token = icalfeed.create(u["id"])
    base = _public_base(request)
    return {"url": f"{base}/cal/{token}.ics", "feed": icalfeed.status(u["id"])}


@app.delete("/api/me/calendar")
def api_my_calendar_revoke(request: _Request):
    from . import icalfeed
    u = _user(request)
    if u.get("id"):
        icalfeed.revoke(u["id"])
    return {"feed": None}


@app.get("/cal/{token}.ics")
def calendar_feed(token: str, request: _Request):
    """The subscription itself — no session (calendar apps can't sign in);
    the unguessable token is the credential. Unknown or revoked: 404."""
    from fastapi.responses import Response as _Resp
    from . import icalfeed
    user = icalfeed.user_for(token)
    if not user:
        raise HTTPException(404, "Not found")
    body = icalfeed.render(user, _public_base(request), request.url.hostname or "faucet")
    return _Resp(body, media_type="text/calendar; charset=utf-8",
                 headers={"Cache-Control": "private, max-age=900",
                          "Content-Disposition": 'inline; filename="faucet.ics"'})


class MappingsBody(BaseModel):
    mappings: list[dict]


@app.get("/api/path-mappings")
def api_path_mappings():
    from . import libraries
    dl = _downloads_view()
    return {"mappings": libraries.mappings(), "downloads": dl,
            "moves": [{"library": lib["name"], "path": lib["path"], "moves": _moves(lib["path"], dl)}
                      for lib in libraries.all_libraries()]}


@app.put("/api/path-mappings")
def api_path_mappings_set(body: MappingsBody):
    from . import libraries
    try:
        return {"mappings": libraries.set_mappings(body.mappings)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/path-mappings/test")
def api_path_mappings_test(path: str = Query(..., min_length=1)):
    from . import libraries
    local = libraries.to_local(path)
    return {"client_path": path, "local_path": local, "exists": os.path.exists(local)}


class RetireBody(BaseModel):
    paths: list[str] | None = None      # None = every recorded duplicate


@app.post("/api/library/duplicates/retire")
def api_library_duplicates_retire(body: RetireBody):
    from . import library
    return library.retire_duplicates(body.paths)


# --- monitored movies (Radarr-side) ---
class MovieAdd(BaseModel):
    tmdb_id: int
    title: str
    year: int | None = None
    poster: str | None = None
    profile_id: int | None = None


@app.get("/api/movies")
def api_movies_list(request: _Request):
    from . import movies as movies_mod
    out = movies_mod.list_movies()
    # annotate outstanding wants (missing vs upgrade) for the UI chips
    with db.connect() as c:
        wants = {r["series_id"]: r["reason"] for r in c.execute(
            "SELECT series_id, reason FROM wanted WHERE kind='movie' "
            "AND status IN ('wanted','grabbed')")}
    from . import libraries as _libs
    default, enabled = _libs.effective_map("movie")
    for m in out:
        m["want_reason"] = wants.get(m["id"])
        m["lib"] = _libs.resolve(m.get("library_id"), default, enabled)
    from . import access
    return {"movies": access.filter_titles(_user(request), out)}


@app.get("/api/nav")
def api_nav(request: _Request):
    """Library sections for the sidebar (members too, only the libraries they
    can see): each library as its own entry, like Plex."""
    from . import access, libraries
    a = access.allowed(_user(request))
    secs = libraries.sections()
    return {"sections": secs if a is None else [x for x in secs if x["id"] in a]}


@app.post("/api/movies")
def api_movies_add(m: MovieAdd):
    from . import movies as movies_mod
    mid = movies_mod.add_movie(m.tmdb_id, m.title, m.year, m.poster, m.profile_id)
    return {"status": "ok", "id": mid}


@app.get("/api/movies/{mid}/detail")
def api_movie_detail(mid: int, request: _Request):
    _guard_title(request, "movie", mid)
    from . import movies as movies_mod
    out = movies_mod.movie_detail(mid)
    if not out.get("movie"):
        raise HTTPException(404, "Movie not found")
    user = getattr(request.state, "user", None) or {}
    if user.get("role") != "admin" and out.get("file"):
        # members see that it's on disk and at what quality, not where
        out["file"] = {k: v for k, v in out["file"].items() if k != "path"}
    with db.connect() as c:
        w = c.execute("SELECT status, reason, next_search, search_count, last_search "
                      "FROM wanted WHERE kind='movie' AND series_id=?", (mid,)).fetchone()
    out["want"] = dict(w) if w else None
    out["profiles"] = _profiles_for(user)
    from . import libraries as _libs
    _lib = _libs.title_library("movie", mid)
    out["library"] = {"id": _lib["id"], "name": _lib["name"]} if _lib else None
    from . import plex as _plex
    m = out.get("movie") or {}
    out["watched"] = _plex.watched_movie(m["tmdb_id"]) if m.get("tmdb_id") else None
    if user.get("role") == "admin":
        out["save_to"] = _save_to("movie", mid, out["movie"].get("library_id"))
    return out


@app.delete("/api/movies/{mid}")
def api_movies_delete(mid: int):
    from . import movies as movies_mod
    movies_mod.delete_movie(mid)
    return {"status": "ok"}


@app.get("/metrics")
def metrics(request: _Request):
    """Prometheus metrics. Bearer METRICS_TOKEN, or an admin session."""
    from fastapi.responses import PlainTextResponse
    from . import metrics as metricsmod
    ok = metricsmod.authorized(request)
    if ok is None:
        raise HTTPException(404)
    if not ok:
        raise HTTPException(401, "Bearer token required.",
                            headers={"WWW-Authenticate": "Bearer"})
    return PlainTextResponse(metricsmod.render(),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/settings/backup")
def api_settings_backup():
    """Download a consistent copy of the database (sessions and reset tokens
    stripped, TMDb cache dropped). Restore by stopping Faucet and replacing
    cascade.db in the config volume."""
    from starlette.background import BackgroundTask
    from . import maintenance
    path = maintenance.backup_db()
    log.info("Database backup downloaded (%d bytes)", path.stat().st_size)
    return FileResponse(path, media_type="application/vnd.sqlite3",
                        filename=maintenance.backup_filename(),
                        headers={"Cache-Control": "no-store"},
                        background=BackgroundTask(path.unlink, missing_ok=True))


@app.get("/api/settings/storage")
def api_settings_storage():
    from . import maintenance
    return maintenance.db_stats()


@app.get("/health")
def health(request: _Request):
    """Public liveness probe (Docker healthcheck). Detail — which internal
    services are reachable, and their error strings — is admin-only: on an
    internet-facing deployment those messages leak internal hostnames."""
    user = _auth.session_user(request.cookies.get(_auth.SESSION_COOKIE))
    if not user or user.get("role") != "admin":
        return {"status": "ok"}
    status = {"status": "ok", "indexer": "unknown", "client": "unknown",
              "warnings": config_warnings()}
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
        status["client"] = f"error: {_redact(e)}"
    return status


@app.get("/favicon.svg")
def favicon():
    f = STATIC / "favicon.svg"
    if f.exists():
        return FileResponse(f, media_type="image/svg+xml")
    raise HTTPException(404)


_NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
             "Pragma": "no-cache"}

APP_DIR = STATIC / "app"


def _asset_version() -> str:
    """A hash of every UI module and stylesheet. The shell loads them from
    /app/<hash>/…, so each deploy that changes the UI gets new URLs and no
    cache — browser or CDN — can hand out a stale module next to a fresh
    one (6.2 shipped while Cloudflare kept serving 6.1's links.js)."""
    import hashlib
    h = hashlib.sha256()
    if APP_DIR.exists():
        for f in sorted(APP_DIR.rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(APP_DIR)).encode())
                h.update(f.read_bytes())
    return h.hexdigest()[:12]


ASSET_VERSION = _asset_version()


@app.get("/")
def index():
    # never cache the app shell: a cached copy would render after logout and
    # look like an auth bypass (the API calls behind it 401, but the frame
    # still appears). No-store forces every load to hit the auth gate.
    return _shell("index.html")


_ASSET_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".json": "application/json", ".svg": "image/svg+xml"}


@app.get("/app/{version}/{path:path}")
def app_asset(version: str, path: str):
    """UI modules under a content-hashed prefix. The current version is
    cacheable forever; any other (an old page still open after a deploy)
    is served fresh with no-cache, so it keeps working until reloaded."""
    root = APP_DIR.resolve()
    f = (APP_DIR / path).resolve()
    if root not in f.parents or not f.is_file() or f.suffix not in _ASSET_TYPES:
        raise HTTPException(404, "Not found")
    cache = ("public, max-age=31536000, immutable" if version == ASSET_VERSION
             else "no-cache")
    return FileResponse(f, media_type=_ASSET_TYPES[f.suffix],
                        headers={"Cache-Control": cache})


def _shell(name: str):
    f = STATIC / name
    if not f.exists():
        raise HTTPException(404, "UI not installed.")
    body = f.read_text().replace("/static/app/", f"/app/{ASSET_VERSION}/")
    return HTMLResponse(body, headers=_NO_STORE)


@app.post("/login")
def auth_page_post():
    """Only reached if the sign-in script didn't run and the browser submitted
    the form itself: send it back to the page (POST keeps the password out of
    the address bar; nothing submitted this way is used)."""
    from fastapi.responses import RedirectResponse as _Redirect
    return _Redirect(url="/login?retry=1", status_code=303)


@app.get("/login")
@app.get("/register")
@app.get("/reset")
def auth_page():
    """Sign in, first-run admin account, and password reset: one page,
    driven by the path and /api/auth/me."""
    return _shell("auth.html")


# ---------------------------------------------------------------------------
# Home: one call for the new UI's landing page
# ---------------------------------------------------------------------------
def _stills(tmdb_id, season) -> dict:
    """Cached stills only; seasons not cached yet are fetched in the
    background so Home never waits on TMDb."""
    cached = tmdbmod.cached_season(tmdb_id, season)
    if cached is None:
        _fill_stills_async(tmdb_id, [season])
        return {}
    return {e["episode"]: e.get("still") for e in cached}


@app.get("/api/home")
def api_home(request: _Request):
    """Hero + rails. Members get the library rails; admins also get
    downloads, what's still being hunted, and live throughput."""
    from datetime import datetime as _dt, timedelta as _td
    from . import movies as movies_mod
    from .library import owned_index, owned_for
    user = getattr(request.state, "user", None) or {}
    admin = user.get("role") == "admin"
    out: dict = {"admin": admin}
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT le.series_id, le.season, le.episode, le.quality, le.mtime, "
            "s.title, s.poster, s.tmdb_id, se.title AS ep_title "
            "FROM library_episodes le JOIN series s ON s.id = le.series_id "
            "LEFT JOIN series_episodes se ON se.series_id = le.series_id "
            "AND se.season = le.season AND se.episode = le.episode "
            "ORDER BY le.mtime DESC LIMIT 80")]
        lib_movies = [dict(r) for r in c.execute(
            "SELECT title, year, quality, source, mtime FROM library_movies "
            "ORDER BY mtime DESC LIMIT 60")]
        monitored = [dict(r) for r in c.execute("SELECT id, title, year, poster FROM movies")]
        movie_wants = {r["series_id"]: r["reason"] for r in c.execute(
            "SELECT series_id, reason FROM wanted WHERE kind='movie' "
            "AND status IN ('wanted','grabbed')")}
        hunting = []
        if admin:
            idx = owned_index(c)
            canon: dict = {}
            today = _dt.now().date().isoformat()
            for r in c.execute("SELECT series_id, season, episode FROM series_episodes "
                               "WHERE season > 0 AND air_date != '' AND air_date <= ?", (today,)):
                canon.setdefault(r["series_id"], set()).add((r["season"], r["episode"]))
            for r in c.execute(
                    "SELECT s.id, s.title, s.poster, COUNT(w.id) AS n FROM wanted w "
                    "JOIN series s ON s.id = w.series_id WHERE w.kind='episode' "
                    "AND w.status='wanted' GROUP BY s.id ORDER BY n DESC LIMIT 20"):
                tot = canon.get(r["id"], set())
                have = len(owned_for(idx, r["id"], r["title"]) & tot)
                # floor, and never "100%" while something is still wanted
                pct = (100 * have // len(tot)) if tot else 0
                hunting.append({"id": r["id"], "title": r["title"], "poster": r["poster"],
                                "wanted": r["n"], "pct": min(pct, 99)})
    from . import access
    vis_series = access.visible_series_ids(user)
    vis_movies = access.visible_movie_ids(user)
    if vis_series is not None:
        rows = [r for r in rows if r["series_id"] in vis_series]
    if vis_movies is not None:
        monitored = [m for m in monitored if m["id"] in vis_movies]
    stills: dict = {}
    episodes = []
    for r in rows:
        key = (r["tmdb_id"], r["season"])
        if key not in stills:
            stills[key] = _stills(*key) if r["tmdb_id"] else {}
        episodes.append({
            "series_id": r["series_id"], "title": r["title"], "poster": r["poster"],
            "season": r["season"], "episode": r["episode"],
            "ep_title": r["ep_title"] or "", "quality": r["quality"],
            "added": _dt.fromtimestamp(r["mtime"] or 0).isoformat(timespec="seconds"),
            "still": stills[key].get(r["episode"])})
    out["episodes"] = episodes

    hero = None
    if episodes:
        first = episodes[0]
        cutoff = (_dt.fromisoformat(first["added"]) - _td(days=3)).isoformat()
        fresh = [e for e in episodes if e["series_id"] == first["series_id"]
                 and e["added"] >= cutoff]
        with db.connect() as c:
            srow = c.execute("SELECT tmdb_id FROM series WHERE id=?",
                             (first["series_id"],)).fetchone()
        meta = tmdbmod.details(srow["tmdb_id"], "tv") if srow and srow["tmdb_id"] else {}
        hero = {"series_id": first["series_id"], "title": first["title"],
                "poster": first["poster"], "backdrop": meta.get("backdrop"),
                "overview": meta.get("overview", ""), "year": meta.get("year"),
                "new": [{"season": e["season"], "episode": e["episode"],
                         "ep_title": e["ep_title"], "quality": e["quality"]} for e in fresh]}
    out["hero"] = hero

    movies_out, seen = [], set()
    for lm in lib_movies:
        for m in monitored:
            if m["id"] in seen:
                continue
            if movies_mod._movie_matches(lm["title"], lm["year"], m["title"], m["year"]):
                seen.add(m["id"])
                movies_out.append({"id": m["id"], "title": m["title"], "year": m["year"],
                                   "poster": m["poster"], "quality": lm["quality"],
                                   "cam": lm["source"] == "CAM",
                                   "want": movie_wants.get(m["id"])})
                break
        if len(movies_out) >= 20:
            break
    out["movies"] = movies_out

    if admin:
        out["hunting"] = hunting
        from . import transfers
        snap = transfers.snapshot()                # never waits on the client
        downloads, rate, active = [], 0, 0
        for t in snap["items"]:
            rate += t.down_rate
            if t.status == "downloading":
                active += 1
            if t.status in ("downloading", "checking", "queued") or 0 < t.percent < 100:
                downloads.append({"id": t.id, "name": t.name, "percent": round(t.percent, 1),
                                  "status": t.status, "down_rate": t.down_rate,
                                  "eta": t.eta, "size": t.size, "error": t.error})
        client_ok = snap["ok"] is not False or bool(snap["items"])
        downloads.sort(key=lambda d: d["down_rate"], reverse=True)
        out["downloads"] = downloads
        with db.connect() as c:
            wanted_total = c.execute("SELECT COUNT(*) FROM wanted WHERE status='wanted'"
                                     ).fetchone()[0]
        try:
            from . import alerts as _alerts
            out["alerts"] = _alerts.active()
        except Exception:                        # noqa: BLE001 - Home never fails on this
            out["alerts"] = []
        out["stats"] = {"downloading": active, "down_rate": rate, "client_ok": client_ok,
                        "client_slow": snap["ok"] is False and bool(snap["items"]),
                        "client_as_of": snap["as_of"], "wanted": wanted_total}
    return out


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

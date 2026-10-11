"""FastAPI auth wiring: dependencies, CSRF, and endpoints.

Connects the auth core (faucet/auth.py) to the web app:
- current_user / require_user / require_admin dependencies resolve the signed
  session cookie to a user and enforce access.
- CSRF: state-changing requests must echo the csrf cookie in an X-CSRF header
  (double-submit cookie pattern). Safe for a cookie-based session app.
- Endpoints for register / login / logout / me / change-password, plus admin
  user management and password-reset flows.

Route gating model (per operator choice): login required for everything;
management endpoints are admin-only; other authenticated users get the rest.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from . import auth

router = APIRouter()
log = logging.getLogger("faucet.auth")

CSRF_COOKIE = "faucet_csrf"


# ── dependencies ────────────────────────────────────────────────────────────

def current_user(request: Request) -> dict | None:
    cookie = request.cookies.get(auth.SESSION_COOKIE)
    return auth.session_user(cookie)


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Authentication required.")
    return u


def require_admin(request: Request) -> dict:
    u = require_user(request)
    if u.get("role") != "admin":
        raise HTTPException(403, "Admin access required.")
    return u


def verify_csrf(request: Request) -> None:
    """Double-submit CSRF check for state-changing requests."""
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get("x-csrf-token")
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise HTTPException(403, "CSRF check failed.")


def _set_session_cookies(response: Response, signed: str, secure: bool) -> None:
    response.set_cookie(auth.SESSION_COOKIE, signed, httponly=True, samesite="lax",
                        secure=secure, max_age=auth.SESSION_DAYS * 86400, path="/")
    # CSRF token is readable by JS (not httponly) so the frontend can echo it
    csrf = secrets.token_urlsafe(24)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="lax",
                        secure=secure, max_age=auth.SESSION_DAYS * 86400, path="/")


def _secure_request(request: Request) -> bool:
    # proxy TLS termination is honoured only from a trusted proxy (netutil)
    from . import netutil
    return netutil.is_https(request)


def _retry_after(seconds: float) -> HTTPException:
    import math
    return HTTPException(429, auth.LOCKED,
                         headers={"Retry-After": str(max(1, math.ceil(seconds)))})


# ── models ──────────────────────────────────────────────────────────────────

class RegisterIn(BaseModel):
    username: str
    password: str
    email: str | None = None


class LoginIn(BaseModel):
    username: str
    password: str


class ChangePwIn(BaseModel):
    current_password: str
    new_password: str


class AdminCreateIn(BaseModel):
    username: str
    password: str
    email: str | None = None
    role: str = "user"
    status: str = "active"


class ResetConsumeIn(BaseModel):
    token: str
    new_password: str


class RequestIn(BaseModel):
    media_type: str
    tmdb_id: int
    title: str
    year: int | None = None
    poster: str | None = None


# ── request / approval flow ─────────────────────────────────────────────────

@router.post("/api/requests")
def create_request_ep(body: RequestIn, request: Request,
                      user: dict = Depends(require_user)):
    verify_csrf(request)
    from . import requests_flow
    if body.media_type not in ("tv", "series", "movie"):
        raise HTTPException(400, "media_type must be 'tv' or 'movie'.")
    out = requests_flow.create_request(
        user, body.media_type, body.tmdb_id, body.title, body.year, body.poster)
    if out.get("status") == "unknown_title":
        raise HTTPException(400, out["error"])
    return out


@router.get("/api/requests")
def list_requests_ep(request: Request, scope: str = "auto",
                     user: dict = Depends(require_user)):
    from . import requests_flow
    return {"requests": requests_flow.list_requests(user, scope)}


@router.get("/api/requests/pending_count")
def pending_count_ep(admin: dict = Depends(require_admin)):
    from . import requests_flow
    return {"pending": requests_flow.pending_count()}


@router.post("/api/requests/{req_id}/approve")
def approve_request_ep(req_id: int, request: Request,
                       admin: dict = Depends(require_admin)):
    verify_csrf(request)
    from . import requests_flow
    return requests_flow.approve_request(req_id, decider=admin)


@router.post("/api/requests/{req_id}/decline")
def decline_request_ep(req_id: int, request: Request,
                       admin: dict = Depends(require_admin)):
    verify_csrf(request)
    from . import requests_flow
    return requests_flow.decline_request(req_id, decider=admin)


@router.delete("/api/requests/{req_id}")
def delete_request_ep(req_id: int, request: Request,
                      admin: dict = Depends(require_admin)):
    verify_csrf(request)
    from . import requests_flow
    requests_flow.delete_request(req_id)
    return {"status": "ok"}


class LibraryAccessBody(BaseModel):
    libraries: list[int] | None = None          # None / [] = every library
    request_tv_library: int | None = None
    request_movie_library: int | None = None


@router.put("/api/admin/users/{uid}/libraries")
def admin_set_libraries(uid: int, body: LibraryAccessBody, request: Request,
                        admin: dict = Depends(require_admin)):
    """Limit a member to some libraries and choose where their approved
    requests are filed. Admins always see everything."""
    verify_csrf(request)
    from . import access
    try:
        access.set_access(uid, body.libraries, body.request_tv_library,
                          body.request_movie_library)
    except LookupError:
        raise HTTPException(404, "No such user")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok"}


@router.post("/api/admin/users/{uid}/2fa/reset")
def admin_reset_mfa(uid: int, request: Request, admin: dict = Depends(require_admin)):
    """Turn off someone's two-factor sign-in and remove their passkeys (lost
    phone). Admins are asked to set two-factor up again at next sign-in."""
    verify_csrf(request)
    from . import mfa, passkeys
    if not auth.get_user(uid):
        raise HTTPException(404, "No such user")
    mfa.disable(uid)
    passkeys.remove_all(uid)          # a lost phone took its passkeys with it
    log.info("two-factor sign-in reset for user %s by %s", uid, admin["username"])
    return {"status": "ok"}


@router.post("/api/admin/users/{uid}/autoapprove")
def admin_set_autoapprove(uid: int, request: Request, enabled: int,
                          admin: dict = Depends(require_admin)):
    verify_csrf(request)
    from . import db as _db
    with _db.connect() as c:
        c.execute("UPDATE users SET can_autoapprove=? WHERE id=?",
                  (1 if enabled else 0, uid))
    return {"status": "ok"}


# ── Fix Match (admin) ────────────────────────────────────────────────────────
@router.get("/api/admin/match-search")
def admin_match_search(q: str, kind: str,
                       admin: dict = Depends(require_admin)):
    """TMDb candidates for the Fix Match picker. kind: 'show' | 'movie'."""
    from . import fixmatch
    if kind not in ("show", "movie"):
        return {"error": "kind must be 'show' or 'movie'"}
    return {"results": fixmatch.search(q, kind)}


@router.post("/api/admin/title/{kind}/{item_id}/status")
async def admin_set_title_status(kind: str, item_id: int, request: Request,
                           admin: dict = Depends(require_admin)):
    """Set a title's library status: monitored | in_library | ignored.
    Namespaced under /title/ so the {kind} param can't shadow sibling admin
    routes like /api/admin/users/{uid}/status."""
    verify_csrf(request)
    from . import fixmatch
    if kind not in ("show", "movie"):
        return {"error": "kind must be 'show' or 'movie'"}
    body = await request.json()
    res = fixmatch.set_status(kind, item_id, body.get("status", ""))
    return res


@router.post("/api/admin/title/{kind}/{item_id}/fixmatch")
async def admin_fix_match(kind: str, item_id: int, request: Request,
                          admin: dict = Depends(require_admin)):
    """Re-link a title to a different TMDb entry, re-reconciling immediately."""
    verify_csrf(request)
    from . import fixmatch
    if kind not in ("show", "movie"):
        return {"error": "kind must be 'show' or 'movie'"}
    body = await request.json()
    tmdb_id = body.get("tmdb_id")
    if not tmdb_id:
        return {"error": "tmdb_id required"}
    return fixmatch.fix_match(kind, item_id, int(tmdb_id))


# ── File browser (admin) — scoped to a single configured root ────────────────
@router.get("/api/admin/files/list")
def admin_files_list(path: str = "", admin: dict = Depends(require_admin)):
    from . import filebrowser
    return filebrowser.list_dir(path)


@router.post("/api/admin/files/move")
async def admin_files_move(request: Request, admin: dict = Depends(require_admin)):
    verify_csrf(request)
    from . import filebrowser
    body = await request.json()
    return filebrowser.move(body.get("src", ""), body.get("dest_dir", ""),
                            body.get("name") or None)


@router.post("/api/admin/files/mkdir")
async def admin_files_mkdir(request: Request, admin: dict = Depends(require_admin)):
    verify_csrf(request)
    from . import filebrowser
    body = await request.json()
    return filebrowser.mkdir(body.get("parent", ""), body.get("name", ""))


@router.post("/api/admin/movie/{movie_id}/link-file")
async def admin_link_movie_file(movie_id: int, request: Request,
                                admin: dict = Depends(require_admin)):
    """Point a movie at a file the admin selected in the browser (no move)."""
    verify_csrf(request)
    from . import filebrowser, fixmatch
    body = await request.json()
    abs_path = filebrowser.resolve_abs(body.get("rel", ""))
    if not abs_path:
        return {"error": "file not found or outside the allowed root"}
    return fixmatch.link_movie_file(movie_id, abs_path)


# ── public auth endpoints ───────────────────────────────────────────────────

@router.get("/api/auth/me")
def auth_me(request: Request):
    u = current_user(request)
    if not u:
        return {"authenticated": False, "setup_needed": auth.user_count() == 0}
    from . import mfa
    on = mfa.enabled(u["id"])
    return {"authenticated": True, "user": {
        "id": u["id"], "username": u["username"], "email": u.get("email"),
        "role": u["role"], "status": u["status"], "mfa": on},
        "mfa_setup_required": mfa.required_for(u) and not on}


@router.post("/api/auth/register")
def auth_register(body: RegisterIn, request: Request):
    from . import netutil
    ip = netutil.client_ip(request)
    wait = netutil.register_blocked(ip)
    if wait:
        raise _retry_after(wait)
    netutil.register_hit(ip)
    uid, err = auth.create_user(body.username, body.password, body.email)
    if err:
        raise HTTPException(400, err)
    first = (auth.get_user(uid) or {}).get("role") == "admin"
    # first user is auto-admin+active; others are pending
    if first:
        return {"status": "ok", "admin_created": True,
                "message": "Admin account created. You can log in now."}
    return {"status": "ok", "pending": True,
            "message": "Account created and awaiting admin approval."}


@router.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response):
    from . import netutil
    ip = netutil.client_ip(request)
    wait = netutil.login_blocked(ip, body.username)
    if wait:
        raise _retry_after(wait)
    user, err = auth.authenticate(body.username, body.password)
    if err:
        if err == auth.LOCKED:
            raise _retry_after(auth.LOCK_MINUTES * 60)
        if err == "Invalid username or password.":
            netutil.login_failed(ip, body.username)
            log.warning("failed login for %r from %s", body.username[:60], ip)
        raise HTTPException(401, err)
    from . import mfa
    if mfa.enabled(user["id"]) and request.headers.get("x-faucet-auth") != "2":
        # a sign-in page from before two-factor existed would take the
        # "enter your code" answer as success and bounce back to itself
        raise HTTPException(409, "Faucet was updated — refresh this page to sign in.")
    if mfa.enabled(user["id"]):
        # password is right; the session waits for the code (a short-lived,
        # signed ticket carries the step across)
        return {"status": "mfa_required", "ticket": mfa.issue_ticket(user["id"])}
    netutil.login_succeeded(ip, body.username)
    ua = request.headers.get("user-agent", "")
    signed = auth.create_session(user["id"], ip, ua)
    _set_session_cookies(response, signed, _secure_request(request))
    return {"status": "ok", "user": {"username": user["username"], "role": user["role"]},
            "mfa_setup_required": mfa.required_for(user)}


class MfaLoginIn(BaseModel):
    ticket: str
    code: str


@router.post("/api/auth/login/mfa")
def auth_login_mfa(body: MfaLoginIn, request: Request, response: Response):
    """Second step: an authenticator code (or a recovery code) for the ticket
    the password step returned."""
    from . import mfa, netutil
    ip = netutil.client_ip(request)
    uid = mfa.redeem_ticket(body.ticket)
    user = auth.get_user(uid) if uid else None
    if not user or user.get("status") != "active":
        raise HTTPException(401, "That sign-in expired — enter your password again.")
    wait = netutil.login_blocked(ip, user["username"])
    if wait:
        raise _retry_after(wait)
    kind = mfa.verify(user["id"], body.code)
    if not kind:
        netutil.login_failed(ip, user["username"])
        log.warning("wrong two-factor code for %r from %s", user["username"][:60], ip)
        raise HTTPException(401, "That code didn't work. Codes change every 30 seconds.")
    netutil.login_succeeded(ip, user["username"])
    signed = auth.create_session(user["id"], ip, request.headers.get("user-agent", ""))
    _set_session_cookies(response, signed, _secure_request(request))
    left = mfa.status(user["id"])["recovery_left"]
    return {"status": "ok", "user": {"username": user["username"], "role": user["role"]},
            "used": kind, "recovery_left": left}


# ── passkeys ──
def _rp(request: Request) -> tuple[str, str]:
    """(relying-party id, expected origin) — the host and URL the browser used."""
    from . import netutil
    host = request.headers.get("host") or request.url.netloc
    scheme = "https" if netutil.is_https(request) else "http"
    return host.split(":")[0], f"{scheme}://{host}"


class PasskeyLoginIn(BaseModel):
    challenge_id: str
    credential: dict


@router.post("/api/auth/passkey/options")
def auth_passkey_options(request: Request):
    from . import passkeys
    return passkeys.authentication_options(_rp(request)[0])


@router.post("/api/auth/passkey")
def auth_passkey(body: PasskeyLoginIn, request: Request, response: Response):
    """Sign in with a passkey: the device checked who you are (biometrics or
    PIN), so this is both factors — no password, no code."""
    from . import netutil, passkeys
    ip = netutil.client_ip(request)
    wait = netutil.login_blocked(ip, "")
    if wait:
        raise _retry_after(wait)
    rp_id, origin = _rp(request)
    user = passkeys.authenticate(body.challenge_id, body.credential, rp_id, origin)
    if not user:
        netutil.login_failed(ip, "")
        log.warning("failed passkey sign-in from %s", ip)
        raise HTTPException(401, "That passkey didn't work here. Try again, or sign in with your password.")
    netutil.login_succeeded(ip, user["username"])
    signed = auth.create_session(user["id"], ip, request.headers.get("user-agent", ""))
    _set_session_cookies(response, signed, _secure_request(request))
    from . import mfa
    return {"status": "ok", "user": {"username": user["username"], "role": user["role"]},
            "mfa_setup_required": mfa.required_for(user) and not mfa.enabled(user["id"])}


class PasskeyAddIn(BaseModel):
    challenge_id: str
    credential: dict
    name: str = ""


class PasskeyRenameIn(BaseModel):
    name: str


@router.get("/api/me/passkeys")
def me_passkeys(user: dict = Depends(require_user)):
    from . import passkeys
    return {"passkeys": passkeys.list_for(user["id"])}


@router.post("/api/me/passkeys/options")
def me_passkey_options(body: MfaPasswordIn, request: Request, user: dict = Depends(require_user)):
    """Adding a passkey needs the password, like setting up two-factor."""
    verify_csrf(request)
    from . import passkeys
    if not auth.verify_password(body.password, user["pw_hash"]):
        raise HTTPException(400, "Password is incorrect.")
    return passkeys.registration_options(user, _rp(request)[0])


@router.post("/api/me/passkeys")
def me_passkey_add(body: PasskeyAddIn, request: Request, user: dict = Depends(require_user)):
    verify_csrf(request)
    from . import passkeys
    rp_id, origin = _rp(request)
    try:
        out = passkeys.register(user, body.challenge_id, body.credential, body.name, rp_id, origin)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:                       # noqa: BLE001 - verification failures
        log.warning("passkey registration failed for %s: %s", user["username"], e)
        raise HTTPException(400, "Your device's answer didn't check out — try again.")
    log.info("passkey %r added for %s", out["name"], user["username"])
    return out


@router.patch("/api/me/passkeys/{pid}")
def me_passkey_rename(pid: int, body: PasskeyRenameIn, request: Request,
                      user: dict = Depends(require_user)):
    verify_csrf(request)
    from . import passkeys
    try:
        if not passkeys.rename(user["id"], pid, body.name):
            raise HTTPException(404, "No such passkey")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok"}


@router.delete("/api/me/passkeys/{pid}")
def me_passkey_remove(pid: int, request: Request, user: dict = Depends(require_user)):
    verify_csrf(request)
    from . import passkeys
    if not passkeys.remove(user["id"], pid):
        raise HTTPException(404, "No such passkey")
    log.info("passkey %s removed by %s", pid, user["username"])
    return {"status": "ok"}


# ── two-factor management (the signed-in user's own) ──
class MfaPasswordIn(BaseModel):
    password: str


class MfaEnableIn(BaseModel):
    code: str


@router.get("/api/me/2fa")
def me_mfa(user: dict = Depends(require_user)):
    from . import mfa
    return {**mfa.status(user["id"]), "required": mfa.required_for(user)}


@router.post("/api/me/2fa/setup")
def me_mfa_setup(body: MfaPasswordIn, request: Request, user: dict = Depends(require_user)):
    """Start setup: a new secret as a QR code (and as text). Needs the
    password, so a borrowed signed-in browser can't bind its own phone."""
    verify_csrf(request)
    from . import mfa
    if not auth.verify_password(body.password, user["pw_hash"]):
        raise HTTPException(400, "Password is incorrect.")
    secret = mfa.begin(user["id"])
    uri = mfa.otpauth_uri(secret, user["username"], request.url.hostname or "faucet")
    return {"secret": secret, "uri": uri, "qr_svg": mfa.qr_svg(uri)}


@router.post("/api/me/2fa/enable")
def me_mfa_enable(body: MfaEnableIn, request: Request, user: dict = Depends(require_user)):
    verify_csrf(request)
    from . import mfa
    codes = mfa.confirm(user["id"], body.code)
    if codes is None:
        raise HTTPException(400, "That code didn't match — check the time on your phone and try the next one.")
    log.info("two-factor sign-in enabled for %s", user["username"])
    return {"enabled": True, "recovery_codes": codes}


@router.post("/api/me/2fa/recovery")
def me_mfa_recovery(body: MfaPasswordIn, request: Request, user: dict = Depends(require_user)):
    verify_csrf(request)
    from . import mfa
    if not auth.verify_password(body.password, user["pw_hash"]):
        raise HTTPException(400, "Password is incorrect.")
    if not mfa.enabled(user["id"]):
        raise HTTPException(400, "Two-factor sign-in isn't on.")
    return {"recovery_codes": mfa.new_recovery_codes(user["id"])}


@router.post("/api/me/2fa/disable")
def me_mfa_disable(body: MfaPasswordIn, request: Request, user: dict = Depends(require_user)):
    verify_csrf(request)
    from . import mfa
    if mfa.required_for(user):
        raise HTTPException(400, "Admins must keep two-factor sign-in on. Set it up again "
                                 "on a new device instead.")
    if not auth.verify_password(body.password, user["pw_hash"]):
        raise HTTPException(400, "Password is incorrect.")
    mfa.disable(user["id"])
    log.info("two-factor sign-in disabled by %s", user["username"])
    return {"enabled": False}


@router.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"status": "ok"}


@router.post("/api/auth/change-password")
def auth_change_password(body: ChangePwIn, request: Request, response: Response,
                         user: dict = Depends(require_user)):
    verify_csrf(request)
    if not auth.verify_password(body.current_password, user["pw_hash"]):
        raise HTTPException(400, "Current password is incorrect.")
    err = auth.set_password(user["id"], body.new_password)
    if err:
        raise HTTPException(400, err)
    # password change revoked all sessions (incl. this one) — issue a fresh one
    from . import netutil
    ip = netutil.client_ip(request)
    signed = auth.create_session(user["id"], ip, request.headers.get("user-agent", ""))
    _set_session_cookies(response, signed, _secure_request(request))
    return {"status": "ok"}


@router.post("/api/auth/reset/consume")
def auth_reset_consume(body: ResetConsumeIn):
    err = auth.consume_reset_token(body.token, body.new_password)
    if err:
        raise HTTPException(400, err)
    return {"status": "ok"}


# ── admin user management ───────────────────────────────────────────────────

@router.get("/api/admin/users")
def admin_users(admin: dict = Depends(require_admin)):
    return {"users": auth.list_users()}


@router.post("/api/admin/users")
def admin_create_user(body: AdminCreateIn, request: Request,
                      admin: dict = Depends(require_admin)):
    verify_csrf(request)
    uid, err = auth.create_user(body.username, body.password, body.email,
                                role=body.role, status=body.status)
    if err:
        raise HTTPException(400, err)
    return {"status": "ok", "id": uid}


@router.post("/api/admin/users/{uid}/status")
def admin_set_status(uid: int, request: Request, status: str,
                     admin: dict = Depends(require_admin)):
    verify_csrf(request)
    if uid == admin["id"] and status != "active":
        raise HTTPException(400, "You can't disable your own account.")
    auth.set_status(uid, status)
    return {"status": "ok"}


@router.post("/api/admin/users/{uid}/role")
def admin_set_role(uid: int, request: Request, role: str,
                   admin: dict = Depends(require_admin)):
    verify_csrf(request)
    if uid == admin["id"] and role != "admin":
        raise HTTPException(400, "You can't remove your own admin role.")
    auth.set_role(uid, role)
    return {"status": "ok"}


@router.post("/api/admin/users/{uid}/reset")
def admin_reset_password(uid: int, request: Request,
                         admin: dict = Depends(require_admin)):
    verify_csrf(request)
    token = auth.create_reset_token(uid)
    # returned to the admin to hand off (email delivery comes later)
    return {"status": "ok", "token": token,
            "reset_path": f"/reset?token={token}"}


@router.delete("/api/admin/users/{uid}")
def admin_delete_user(uid: int, request: Request,
                      admin: dict = Depends(require_admin)):
    verify_csrf(request)
    if uid == admin["id"]:
        raise HTTPException(400, "You can't delete your own account.")
    auth.delete_user(uid)
    return {"status": "ok"}

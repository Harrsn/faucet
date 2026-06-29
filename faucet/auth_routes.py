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

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from . import auth

router = APIRouter()

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
    # honour proxy TLS termination (Caddy/Cloudflare) via X-Forwarded-Proto
    xfp = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or xfp == "https"


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


# ── public auth endpoints ───────────────────────────────────────────────────

@router.get("/api/auth/me")
def auth_me(request: Request):
    u = current_user(request)
    if not u:
        return {"authenticated": False, "setup_needed": auth.user_count() == 0}
    return {"authenticated": True, "user": {
        "id": u["id"], "username": u["username"], "email": u.get("email"),
        "role": u["role"], "status": u["status"]}}


@router.post("/api/auth/register")
def auth_register(body: RegisterIn):
    first = auth.user_count() == 0
    uid, err = auth.create_user(body.username, body.password, body.email)
    if err:
        raise HTTPException(400, err)
    # first user is auto-admin+active; others are pending
    if first:
        return {"status": "ok", "admin_created": True,
                "message": "Admin account created. You can log in now."}
    return {"status": "ok", "pending": True,
            "message": "Account created and awaiting admin approval."}


@router.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response):
    user, err = auth.authenticate(body.username, body.password)
    if err:
        raise HTTPException(401, err)
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    signed = auth.create_session(user["id"], ip, ua)
    _set_session_cookies(response, signed, _secure_request(request))
    return {"status": "ok", "user": {"username": user["username"], "role": user["role"]}}


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
    ip = request.client.host if request.client else ""
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

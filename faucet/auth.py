"""Authentication core.

Security primitives for Faucet's user system. Built to be the security
perimeter when Faucet is exposed directly (no Cloudflare Access in front), so
nothing here is best-effort: bcrypt password hashing, server-side sessions with
instant revocation, signed session cookies, failed-login lockout, and a
pending-approval gate on self-registration.

Design notes
------------
* Passwords: bcrypt with a per-hash salt (bcrypt embeds the salt). Never store
  or log plaintext. Verification is constant-time via bcrypt.checkpw.
* Sessions: a random opaque token is stored server-side (sessions table); the
  cookie carries that token *signed* with itsdangerous so a tampered cookie is
  rejected before any DB lookup. Deleting the session row revokes access
  immediately (logout, account disable, password change all do this).
* Lockout: after MAX_FAILED failed logins a user is locked for LOCK_MINUTES.
  This blunts online brute force even though the login page is public.
* Roles: 'admin' (manage users, approve requests) and 'user' (make requests).
* Status: 'pending' (self-registered, inert), 'active', 'disabled'.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta

import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from . import db

log = logging.getLogger("faucet.auth")

SESSION_COOKIE = "faucet_session"
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
MAX_FAILED = int(os.environ.get("LOGIN_MAX_FAILED", "5"))
LOCK_MINUTES = int(os.environ.get("LOGIN_LOCK_MINUTES", "15"))
RESET_TOKEN_HOURS = 24


def _now() -> datetime:
    return datetime.now()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ── cookie signing ──────────────────────────────────────────────────────────

def _secret() -> str:
    """Signing secret for session cookies. Sourced from SESSION_SECRET, or a
    persisted random one in the DB so sessions survive restarts. If neither, a
    new one is generated and stored — but operators should set SESSION_SECRET
    explicitly in production so it's stable and not readable from the DB alone."""
    s = os.environ.get("SESSION_SECRET")
    if s:
        return s
    stored = db.get_setting("session_secret")
    if stored:
        return stored
    gen = secrets.token_urlsafe(48)
    db.set_setting("session_secret", gen)
    return gen


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt="faucet-session")


def sign_token(token: str) -> str:
    return _serializer().dumps(token)


def unsign_token(signed: str) -> str | None:
    try:
        return _serializer().loads(signed, max_age=SESSION_DAYS * 86400)
    except (BadSignature, SignatureExpired):
        return None


# ── password hashing ────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), pw_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _validate_password(plain: str) -> str | None:
    if not plain or len(plain) < 8:
        return "Password must be at least 8 characters."
    if len(plain) > 200:
        return "Password too long."
    return None


# ── user lifecycle ──────────────────────────────────────────────────────────

def user_count() -> int:
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def get_user(user_id: int) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(r) if r else None


def get_user_by_name(username: str) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                      (username,)).fetchone()
    return dict(r) if r else None


def list_users() -> list[dict]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, username, email, role, status, created_ts, last_login, "
            "can_autoapprove FROM users ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def create_user(username: str, password: str, email: str | None = None,
                role: str = "user", status: str = "pending") -> tuple[int | None, str | None]:
    """Create a user. Returns (user_id, error). The very first user created is
    forced to admin+active (bootstrap). Subsequent self-registrations default to
    pending/user."""
    username = (username or "").strip()
    if not username or len(username) > 60:
        return None, "Invalid username."
    if not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        return None, "Username may only contain letters, numbers, _ . -"
    perr = _validate_password(password)
    if perr:
        return None, perr
    if get_user_by_name(username):
        return None, "That username is taken."
    # bootstrap: first account ever is the admin
    if user_count() == 0:
        role, status = "admin", "active"
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO users (username, email, pw_hash, role, status, created_ts) "
            "VALUES (?,?,?,?,?,?)",
            (username, (email or "").strip() or None, hash_password(password),
             role, status, _iso(_now())))
        return cur.lastrowid, None


def set_password(user_id: int, new_password: str) -> str | None:
    perr = _validate_password(new_password)
    if perr:
        return perr
    with db.connect() as c:
        c.execute("UPDATE users SET pw_hash=?, failed_logins=0, locked_until=NULL WHERE id=?",
                  (hash_password(new_password), user_id))
    # revoke all existing sessions on password change
    revoke_user_sessions(user_id)
    return None


def set_status(user_id: int, status: str) -> None:
    if status not in ("pending", "active", "disabled"):
        return
    with db.connect() as c:
        c.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
    if status == "disabled":
        revoke_user_sessions(user_id)


def set_role(user_id: int, role: str) -> None:
    if role not in ("admin", "user"):
        return
    with db.connect() as c:
        c.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))


def delete_user(user_id: int) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM users WHERE id=?", (user_id,))  # cascades sessions


# ── login + lockout ─────────────────────────────────────────────────────────

def _is_locked(user: dict) -> bool:
    lu = user.get("locked_until")
    if not lu:
        return False
    try:
        return datetime.fromisoformat(lu) > _now()
    except ValueError:
        return False


def authenticate(username: str, password: str) -> tuple[dict | None, str | None]:
    """Verify credentials. Returns (user, error). Increments failed-login count
    and applies a temporary lock after MAX_FAILED. Generic error messages avoid
    leaking whether a username exists."""
    user = get_user_by_name(username)
    if not user:
        # do a dummy hash to keep timing similar whether or not the user exists
        verify_password(password, "$2b$12$" + "x" * 53)
        return None, "Invalid username or password."
    if _is_locked(user):
        return None, "Account temporarily locked. Try again later."
    if not verify_password(password, user["pw_hash"]):
        failed = (user.get("failed_logins") or 0) + 1
        locked_until = None
        if failed >= MAX_FAILED:
            locked_until = _iso(_now() + timedelta(minutes=LOCK_MINUTES))
            failed = 0
        with db.connect() as c:
            c.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?",
                      (failed, locked_until, user["id"]))
        return None, "Invalid username or password."
    if user["status"] == "pending":
        return None, "Your account is awaiting admin approval."
    if user["status"] == "disabled":
        return None, "This account has been disabled."
    # success — reset counters, stamp last_login
    with db.connect() as c:
        c.execute("UPDATE users SET failed_logins=0, locked_until=NULL, last_login=? WHERE id=?",
                  (_iso(_now()), user["id"]))
    return get_user(user["id"]), None


# ── sessions ────────────────────────────────────────────────────────────────

def create_session(user_id: int, ip: str = "", ua: str = "") -> str:
    """Create a server-side session and return the SIGNED cookie value."""
    token = secrets.token_urlsafe(32)
    now = _now()
    with db.connect() as c:
        c.execute(
            "INSERT INTO sessions (id, user_id, created_ts, expires_ts, ip, user_agent) "
            "VALUES (?,?,?,?,?,?)",
            (token, user_id, _iso(now), _iso(now + timedelta(days=SESSION_DAYS)),
             ip[:64], (ua or "")[:256]))
    return sign_token(token)


def session_user(signed_cookie: str | None) -> dict | None:
    """Resolve a signed session cookie to a user, or None. Checks signature,
    expiry, session existence, and that the account is still active."""
    if not signed_cookie:
        return None
    token = unsign_token(signed_cookie)
    if not token:
        return None
    with db.connect() as c:
        s = c.execute("SELECT * FROM sessions WHERE id=?", (token,)).fetchone()
        if not s:
            return None
        if s["expires_ts"] and s["expires_ts"] < _iso(_now()):
            c.execute("DELETE FROM sessions WHERE id=?", (token,))
            return None
        u = c.execute("SELECT * FROM users WHERE id=?", (s["user_id"],)).fetchone()
    if not u or u["status"] != "active":
        return None
    return dict(u)


def destroy_session(signed_cookie: str | None) -> None:
    if not signed_cookie:
        return
    token = unsign_token(signed_cookie)
    if token:
        with db.connect() as c:
            c.execute("DELETE FROM sessions WHERE id=?", (token,))


def revoke_user_sessions(user_id: int) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


# ── password reset tokens (admin-generated now, emailable later) ─────────────

def create_reset_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with db.connect() as c:
        c.execute(
            "INSERT INTO reset_tokens (token, user_id, created_ts, expires_ts) "
            "VALUES (?,?,?,?)",
            (token, user_id, _iso(now), _iso(now + timedelta(hours=RESET_TOKEN_HOURS))))
    return token


def consume_reset_token(token: str, new_password: str) -> str | None:
    """Validate a reset token and set the new password. Returns error or None."""
    with db.connect() as c:
        r = c.execute("SELECT * FROM reset_tokens WHERE token=?", (token,)).fetchone()
        if not r or r["used"]:
            return "Invalid or already-used reset link."
        if r["expires_ts"] and r["expires_ts"] < _iso(_now()):
            return "This reset link has expired."
        uid = r["user_id"]
    perr = set_password(uid, new_password)
    if perr:
        return perr
    with db.connect() as c:
        c.execute("UPDATE reset_tokens SET used=1 WHERE token=?", (token,))
    return None

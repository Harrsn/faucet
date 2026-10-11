"""Two-factor sign-in with authenticator codes (TOTP, RFC 6238).

  * 6 digits, 30-second steps, SHA-1 (what every authenticator app does),
    accepting the previous and next step for clock drift, and never the same
    step twice for one account (a code can't be replayed).
  * Ten one-time recovery codes, stored hashed (bcrypt), for a lost phone.
  * Required for admins (FAUCET_REQUIRE_ADMIN_2FA, default on); optional for
    members. An admin without it is let in only to set it up.
  * Sign-in is two steps: the password returns a short-lived signed ticket,
    and the code redeems it for a session. Wrong codes count as failed
    sign-ins for throttling, like wrong passwords.

Lost every device and code? On the server:
    docker exec faucet python -m faucet.mfa disable <username>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import sys
import time
from urllib.parse import quote

from . import db

STEP = 30
DIGITS = 6
WINDOW = 1
RECOVERY_CODES = 10
TICKET_SECONDS = 300
ISSUER = "Faucet"


def required_for(user: dict) -> bool:
    return (user or {}).get("role") == "admin" and \
        os.environ.get("FAUCET_REQUIRE_ADMIN_2FA", "1").lower() not in ("0", "false", "no", "off")


def ensure_schema(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS user_mfa ("
              "  user_id    INTEGER PRIMARY KEY,"
              "  secret     TEXT,"                  # base32, once enabled
              "  pending    TEXT,"                  # base32, during setup
              "  enabled    INTEGER NOT NULL DEFAULT 0,"
              "  last_step  INTEGER NOT NULL DEFAULT 0,"
              "  enabled_ts TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS user_recovery_codes ("
              "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
              "  user_id INTEGER NOT NULL,"
              "  code_hash TEXT NOT NULL,"
              "  used_ts TEXT)")


# ── TOTP ────────────────────────────────────────────────────────────────────

def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    n = struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF
    return str(n % 10 ** DIGITS).zfill(DIGITS)


def code_at(secret: str, t: float | None = None) -> str:
    return _code(secret, int((time.time() if t is None else t) // STEP))


def _match(secret: str, code: str, after_step: int, now: float | None = None) -> int | None:
    """The step a code is valid for (within the window, newer than
    `after_step`), or None."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != DIGITS:
        return None
    cur = int((time.time() if now is None else now) // STEP)
    for step in range(cur - WINDOW, cur + WINDOW + 1):
        if step > after_step and hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, username: str, host: str) -> str:
    label = quote(f"{ISSUER} ({host}):{username}")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")


def qr_svg(uri: str) -> str:
    import io

    import segno
    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="svg", scale=5, border=2, dark="#0E1013",
                                    light="#FFFFFF", xmldecl=False, svgns=True)
    return buf.getvalue().decode()


# ── per-user state ──────────────────────────────────────────────────────────

def status(user_id: int) -> dict:
    with db.connect() as c:
        ensure_schema(c)
        r = c.execute("SELECT enabled, enabled_ts FROM user_mfa WHERE user_id=?", (user_id,)).fetchone()
        left = c.execute("SELECT COUNT(*) FROM user_recovery_codes WHERE user_id=? AND used_ts IS NULL",
                         (user_id,)).fetchone()[0]
    return {"enabled": bool(r and r["enabled"]), "since": r["enabled_ts"] if r else None,
            "recovery_left": left}


def enabled(user_id: int) -> bool:
    return status(user_id)["enabled"]


def begin(user_id: int) -> str:
    """Start (or restart) setup: a fresh pending secret."""
    secret = new_secret()
    with db.connect() as c:
        ensure_schema(c)
        c.execute("INSERT INTO user_mfa (user_id, pending) VALUES (?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET pending=excluded.pending", (user_id, secret))
    return secret


def confirm(user_id: int, code: str) -> list[str] | None:
    """Finish setup with a code from the app: enable, and return fresh
    recovery codes (shown once). None if the code doesn't match."""
    from datetime import datetime
    with db.connect() as c:
        ensure_schema(c)
        r = c.execute("SELECT pending FROM user_mfa WHERE user_id=?", (user_id,)).fetchone()
        if not r or not r["pending"]:
            return None
        step = _match(r["pending"], code, -1)
        if step is None:
            return None
        c.execute("UPDATE user_mfa SET secret=pending, pending=NULL, enabled=1, last_step=?, "
                  "enabled_ts=? WHERE user_id=?",
                  (step, datetime.now().isoformat(timespec="seconds"), user_id))
    return new_recovery_codes(user_id)


def new_recovery_codes(user_id: int) -> list[str]:
    from . import auth
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(RECOVERY_CODES)]
    with db.connect() as c:
        ensure_schema(c)
        c.execute("DELETE FROM user_recovery_codes WHERE user_id=?", (user_id,))
        c.executemany("INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (?,?)",
                      [(user_id, auth.hash_password(code)) for code in codes])
    return codes


def verify(user_id: int, code: str) -> str | None:
    """Check a sign-in code: 'totp', 'recovery', or None. Consumes what it
    accepts (the TOTP step, or the recovery code)."""
    from datetime import datetime

    from . import auth
    raw = (code or "").strip()
    with db.connect() as c:
        ensure_schema(c)
        r = c.execute("SELECT secret, last_step FROM user_mfa WHERE user_id=? AND enabled=1",
                      (user_id,)).fetchone()
        if not r:
            return None
        if sum(ch.isdigit() for ch in raw) == DIGITS and len(raw.replace(" ", "")) == DIGITS:
            step = _match(r["secret"], raw, r["last_step"])
            if step is not None:
                c.execute("UPDATE user_mfa SET last_step=? WHERE user_id=?", (step, user_id))
                return "totp"
            return None
        norm = raw.lower().replace(" ", "")
        for rc in c.execute("SELECT id, code_hash FROM user_recovery_codes "
                            "WHERE user_id=? AND used_ts IS NULL", (user_id,)).fetchall():
            if auth.verify_password(norm, rc["code_hash"]):
                c.execute("UPDATE user_recovery_codes SET used_ts=? WHERE id=?",
                          (datetime.now().isoformat(timespec="seconds"), rc["id"]))
                return "recovery"
    return None


def disable(user_id: int) -> None:
    with db.connect() as c:
        ensure_schema(c)
        c.execute("DELETE FROM user_mfa WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM user_recovery_codes WHERE user_id=?", (user_id,))


# ── the password → code ticket ──────────────────────────────────────────────

def _ticket_serializer():
    from itsdangerous import URLSafeTimedSerializer

    from . import auth
    return URLSafeTimedSerializer(auth._secret(), salt="faucet-mfa-ticket")


def issue_ticket(user_id: int) -> str:
    return _ticket_serializer().dumps({"u": user_id})


def redeem_ticket(ticket: str) -> int | None:
    from itsdangerous import BadSignature
    try:
        return int(_ticket_serializer().loads(ticket, max_age=TICKET_SECONDS)["u"])
    except (BadSignature, KeyError, TypeError, ValueError):
        return None


def _cli(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "disable":
        print("usage: python -m faucet.mfa disable <username>")
        return 2
    from . import auth
    db.init()
    u = auth.get_user_by_name(argv[2])
    if not u:
        print(f"no user {argv[2]!r}")
        return 1
    disable(u["id"])
    print(f"Two-factor sign-in turned off for {u['username']}. "
          + ("They'll be asked to set it up again at their next sign-in." if required_for(u) else ""))
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))

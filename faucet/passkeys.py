"""Passkeys (WebAuthn): sign in with Face ID, Touch ID, Windows Hello, a
security key or a password manager's passkey — no password, no code.

  * Discoverable credentials: the sign-in page asks "any passkey for this
    site?", the authenticator offers the user's, and the credential id finds
    the account. Browsers also offer them straight from the username field.
  * User verification is required at sign-in (the fingerprint / face / PIN
    on the device), so a passkey alone is both factors; it signs in even an
    admin whose authenticator-code two-factor is on. Admins still need
    authenticator codes set up — passkeys sit on top of that baseline.
  * Phishing-resistant by construction: the browser binds each assertion to
    this site's origin, which the server checks, along with a one-time,
    server-issued challenge (5 minutes) and the signature counter.
  * Adding one needs the password; Account lists them (with last use) and
    removes them; an admin's "Reset two-factor" removes them too.

The relying-party id is the host the browser used (e.g. requests.korodi.us),
so passkeys belong to that hostname.
"""
from __future__ import annotations

import base64
import json
import secrets
import time
from datetime import datetime

from . import db

RP_NAME = "Faucet"
CHALLENGE_SECONDS = 300
MAX_PENDING = 500


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def ensure_schema(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS user_passkeys ("
              "  id            INTEGER PRIMARY KEY AUTOINCREMENT,"
              "  user_id       INTEGER NOT NULL,"
              "  credential_id TEXT NOT NULL UNIQUE,"   # base64url
              "  public_key    TEXT NOT NULL,"          # base64url COSE key
              "  sign_count    INTEGER NOT NULL DEFAULT 0,"
              "  transports    TEXT,"
              "  name          TEXT,"
              "  synced        INTEGER NOT NULL DEFAULT 0,"
              "  created_ts    TEXT,"
              "  last_used     TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS passkey_handles ("
              "  user_id INTEGER PRIMARY KEY, handle TEXT NOT NULL UNIQUE)")
    c.execute("CREATE TABLE IF NOT EXISTS webauthn_challenges ("
              "  id TEXT PRIMARY KEY, challenge TEXT NOT NULL, user_id INTEGER,"
              "  kind TEXT NOT NULL, expires REAL NOT NULL)")


def _handle(c, user_id: int) -> bytes:
    """A random per-user id for authenticators (never the database id)."""
    r = c.execute("SELECT handle FROM passkey_handles WHERE user_id=?", (user_id,)).fetchone()
    if r:
        return _unb64(r["handle"])
    h = secrets.token_bytes(32)
    c.execute("INSERT INTO passkey_handles (user_id, handle) VALUES (?,?)", (user_id, _b64(h)))
    return h


def _store_challenge(c, challenge: bytes, kind: str, user_id: int | None) -> str:
    now = time.time()
    c.execute("DELETE FROM webauthn_challenges WHERE expires < ?", (now,))
    if c.execute("SELECT COUNT(*) FROM webauthn_challenges").fetchone()[0] >= MAX_PENDING:
        c.execute("DELETE FROM webauthn_challenges WHERE id IN (SELECT id FROM webauthn_challenges "
                  "ORDER BY expires LIMIT 100)")
    cid = secrets.token_urlsafe(18)
    c.execute("INSERT INTO webauthn_challenges (id, challenge, user_id, kind, expires) VALUES (?,?,?,?,?)",
              (cid, _b64(challenge), user_id, kind, now + CHALLENGE_SECONDS))
    return cid


def _take_challenge(c, cid: str, kind: str) -> tuple[bytes, int | None] | None:
    """One use only: the row is deleted whether or not it's still valid."""
    r = c.execute("SELECT * FROM webauthn_challenges WHERE id=?", (cid or "",)).fetchone()
    if not r:
        return None
    c.execute("DELETE FROM webauthn_challenges WHERE id=?", (cid,))
    if r["kind"] != kind or r["expires"] < time.time():
        return None
    return _unb64(r["challenge"]), r["user_id"]


# ── adding a passkey ────────────────────────────────────────────────────────

def registration_options(user: dict, rp_id: str) -> dict:
    import webauthn
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                          PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement,
                                          UserVerificationRequirement)
    with db.connect() as c:
        ensure_schema(c)
        handle = _handle(c, user["id"])
        existing = [PublicKeyCredentialDescriptor(id=_unb64(r["credential_id"]))
                    for r in c.execute("SELECT credential_id FROM user_passkeys WHERE user_id=?",
                                       (user["id"],))]
        opts = webauthn.generate_registration_options(
            rp_id=rp_id, rp_name=RP_NAME, user_name=user["username"], user_id=handle,
            user_display_name=user["username"], exclude_credentials=existing,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED))
        cid = _store_challenge(c, opts.challenge, "register", user["id"])
    return {"challenge_id": cid, "options": json.loads(webauthn.options_to_json(opts))}


def register(user: dict, challenge_id: str, credential: dict, name: str,
             rp_id: str, origin: str) -> dict:
    """Verify the authenticator's response and save the passkey."""
    import webauthn
    with db.connect() as c:
        ensure_schema(c)
        got = _take_challenge(c, challenge_id, "register")
    if not got or got[1] != user["id"]:
        raise ValueError("That took too long — try adding the passkey again.")
    v = webauthn.verify_registration_response(
        credential=credential, expected_challenge=got[0], expected_rp_id=rp_id,
        expected_origin=origin, require_user_verification=True)
    transports = (credential.get("response") or {}).get("transports") or []
    label = (name or "").strip()[:60] or f"Passkey added {datetime.now():%b %-d, %Y}"
    with db.connect() as c:
        ensure_schema(c)
        if c.execute("SELECT 1 FROM user_passkeys WHERE credential_id=?",
                     (_b64(v.credential_id),)).fetchone():
            raise ValueError("That passkey is already registered.")
        cur = c.execute(
            "INSERT INTO user_passkeys (user_id, credential_id, public_key, sign_count, transports, "
            "name, synced, created_ts) VALUES (?,?,?,?,?,?,?,?)",
            (user["id"], _b64(v.credential_id), _b64(v.credential_public_key), v.sign_count,
             json.dumps(transports), label, 1 if v.credential_backed_up else 0,
             datetime.now().isoformat(timespec="seconds")))
        pid = cur.lastrowid
    return {"id": pid, "name": label}


# ── signing in with one ─────────────────────────────────────────────────────

def authentication_options(rp_id: str) -> dict:
    import webauthn
    from webauthn.helpers.structs import UserVerificationRequirement
    opts = webauthn.generate_authentication_options(
        rp_id=rp_id, allow_credentials=[], user_verification=UserVerificationRequirement.REQUIRED)
    with db.connect() as c:
        ensure_schema(c)
        cid = _store_challenge(c, opts.challenge, "login", None)
    return {"challenge_id": cid, "options": json.loads(webauthn.options_to_json(opts))}


def authenticate(challenge_id: str, credential: dict, rp_id: str, origin: str) -> dict | None:
    """The account a valid assertion belongs to (and record its use), or None."""
    import webauthn
    with db.connect() as c:
        ensure_schema(c)
        got = _take_challenge(c, challenge_id, "login")
        if not got:
            return None
        cred_id = str(credential.get("rawId") or credential.get("id") or "")
        row = c.execute("SELECT * FROM user_passkeys WHERE credential_id=?", (cred_id,)).fetchone()
    if not row:
        return None
    try:
        v = webauthn.verify_authentication_response(
            credential=credential, expected_challenge=got[0], expected_rp_id=rp_id,
            expected_origin=origin, credential_public_key=_unb64(row["public_key"]),
            credential_current_sign_count=row["sign_count"], require_user_verification=True)
    except Exception:                            # noqa: BLE001 - any verification failure
        return None
    with db.connect() as c:
        c.execute("UPDATE user_passkeys SET sign_count=?, last_used=? WHERE id=?",
                  (v.new_sign_count, datetime.now().isoformat(timespec="seconds"), row["id"]))
        u = c.execute("SELECT * FROM users WHERE id=? AND status='active'", (row["user_id"],)).fetchone()
    return dict(u) if u else None


# ── managing ────────────────────────────────────────────────────────────────

def list_for(user_id: int) -> list[dict]:
    with db.connect() as c:
        ensure_schema(c)
        return [dict(r) for r in c.execute(
            "SELECT id, name, synced, created_ts, last_used FROM user_passkeys "
            "WHERE user_id=? ORDER BY created_ts", (user_id,))]


def rename(user_id: int, pid: int, name: str) -> bool:
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Give it a name.")
    with db.connect() as c:
        ensure_schema(c)
        return c.execute("UPDATE user_passkeys SET name=? WHERE id=? AND user_id=?",
                         (name, pid, user_id)).rowcount > 0


def remove(user_id: int, pid: int) -> bool:
    with db.connect() as c:
        ensure_schema(c)
        return c.execute("DELETE FROM user_passkeys WHERE id=? AND user_id=?",
                         (pid, user_id)).rowcount > 0


def remove_all(user_id: int) -> None:
    with db.connect() as c:
        ensure_schema(c)
        c.execute("DELETE FROM user_passkeys WHERE user_id=?", (user_id,))

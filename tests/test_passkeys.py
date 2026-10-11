"""Passkeys end to end, against a software authenticator that does the real
cryptography (P-256 keys, CBOR attestation, signed assertions)."""
from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import struct
from types import SimpleNamespace

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

RP_ID = "testserver"
ORIGIN = "http://testserver"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftAuthenticator:
    """A platform authenticator in software: one discoverable credential."""

    def __init__(self, uv=True):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0
        self.uv = uv
        self.user_handle = None

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def _flags(self, attested=False) -> int:
        return 0x01 | (0x04 if self.uv else 0) | 0x08 | 0x10 | (0x40 if attested else 0)  # UP UV BE BS AT

    def create(self, options, origin=ORIGIN, rp_id=RP_ID) -> dict:
        self.user_handle = options["user"]["id"]
        cdata = json.dumps({"type": "webauthn.create", "challenge": options["challenge"],
                            "origin": origin, "crossOrigin": False}).encode()
        auth = (hashlib.sha256(rp_id.encode()).digest() + bytes([self._flags(True)])
                + struct.pack(">I", self.count) + b"\0" * 16
                + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(cdata), "attestationObject": b64(att),
                             "transports": ["internal", "hybrid"]},
                "clientExtensionResults": {}, "authenticatorAttachment": "platform"}

    def get(self, options, origin=ORIGIN, rp_id=RP_ID) -> dict:
        self.count += 1
        cdata = json.dumps({"type": "webauthn.get", "challenge": options["challenge"],
                            "origin": origin, "crossOrigin": False}).encode()
        auth = hashlib.sha256(rp_id.encode()).digest() + bytes([self._flags()]) + struct.pack(">I", self.count)
        sig = self.key.sign(auth + hashlib.sha256(cdata).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(cdata), "authenticatorData": b64(auth),
                             "signature": b64(sig), "userHandle": self.user_handle},
                "clientExtensionResults": {}, "authenticatorAttachment": "platform"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("SESSION_SECRET", "t")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db", "mfa", "passkeys", "auth", "auth_routes", "app")}
    mods["db"].init()
    from fastapi.testclient import TestClient

    def client(user=None, pw=None):
        c = TestClient(mods["app"].app, follow_redirects=False)
        c.headers["X-Faucet-Auth"] = "2"
        if user:
            assert c.post("/api/auth/login", json={"username": user, "password": pw}).json()["status"] == "ok"
            c.headers["X-CSRF-Token"] = c.cookies.get("faucet_csrf")
        return c

    client().post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    return SimpleNamespace(m=mods, client=client, admin=client("admin", "adminpass123"))


def _add(c, dev, password="adminpass123", name="Phone"):
    assert c.post("/api/me/passkeys/options", json={"password": "nope"}).status_code == 400
    o = c.post("/api/me/passkeys/options", json={"password": password}).json()
    assert o["options"]["authenticatorSelection"]["residentKey"] == "required"
    r = c.post("/api/me/passkeys", json={"challenge_id": o["challenge_id"],
                                         "credential": dev.create(o["options"]), "name": name})
    assert r.status_code == 200, r.text
    return r.json()


def _sign_in(c, dev, **kw):
    o = c.post("/api/auth/passkey/options").json()
    assert o["options"]["userVerification"] == "required" and o["options"].get("allowCredentials", []) == []
    return c.post("/api/auth/passkey", json={"challenge_id": o["challenge_id"],
                                             "credential": dev.get(o["options"], **kw)})


def test_add_and_sign_in_with_a_passkey(env):
    dev = SoftAuthenticator()
    added = _add(env.admin, dev, name="iPhone")
    listed = env.admin.get("/api/me/passkeys").json()["passkeys"]
    assert [(p["name"], p["synced"], p["last_used"]) for p in listed] == [("iPhone", 1, None)]
    fresh = env.client()
    r = _sign_in(fresh, dev)
    assert r.status_code == 200 and r.json()["user"]["username"] == "admin"
    assert fresh.get("/api/auth/me").json()["authenticated"]
    assert env.admin.get("/api/me/passkeys").json()["passkeys"][0]["last_used"]
    assert env.admin.patch(f"/api/me/passkeys/{added['id']}", json={"name": "Work phone"}).status_code == 200
    assert env.admin.delete(f"/api/me/passkeys/{added['id']}").status_code == 200
    assert _sign_in(env.client(), dev).status_code == 401                     # removed: no longer works


def test_passkey_skips_the_authenticator_code(env):
    from faucet import mfa
    c = env.admin
    r = c.post("/api/me/2fa/setup", json={"password": "adminpass123"}).json()
    c.post("/api/me/2fa/enable", json={"code": mfa.code_at(r["secret"])})
    dev = SoftAuthenticator()
    _add(c, dev)
    assert _sign_in(env.client(), dev).json()["status"] == "ok"               # both factors on the device


def test_rejections(env):
    dev = SoftAuthenticator()
    _add(env.admin, dev)
    c = env.client()
    assert _sign_in(c, dev, origin="https://evil.example").status_code == 401  # phished origin
    assert _sign_in(c, dev, rp_id="evil.example").status_code == 401           # wrong site
    o = c.post("/api/auth/passkey/options").json()
    cred = dev.get(o["options"])
    assert c.post("/api/auth/passkey", json={"challenge_id": o["challenge_id"], "credential": cred}).status_code == 200
    again = env.client()
    assert again.post("/api/auth/passkey", json={"challenge_id": o["challenge_id"],
                                                 "credential": cred}).status_code == 401   # challenge used once
    stranger = SoftAuthenticator()
    assert _sign_in(env.client(), stranger).status_code == 401                 # unknown credential
    lazy = SoftAuthenticator(uv=False)                                         # a key with no PIN/biometric
    o = env.admin.post("/api/me/passkeys/options", json={"password": "adminpass123"}).json()
    r = env.admin.post("/api/me/passkeys", json={"challenge_id": o["challenge_id"],
                                                 "credential": lazy.create(o["options"])})
    assert r.status_code == 400                                                # refused at registration
    dev.uv = False
    assert _sign_in(env.client(), dev).status_code == 401                      # and at sign-in


def test_registration_bound_to_its_user_and_admin_reset(env):
    env.admin.post("/api/admin/users", json={"username": "mamaw", "password": "mamawpass1234",
                                             "role": "user", "status": "active"})
    m = env.client("mamaw", "mamawpass1234")
    o = env.admin.post("/api/me/passkeys/options", json={"password": "adminpass123"}).json()
    dev = SoftAuthenticator()
    r = m.post("/api/me/passkeys", json={"challenge_id": o["challenge_id"], "credential": dev.create(o["options"])})
    assert r.status_code == 400                                                # someone else's challenge
    mine = SoftAuthenticator()
    _add(m, mine, password="mamawpass1234")
    from faucet import mfa
    s = m.post("/api/me/2fa/setup", json={"password": "mamawpass1234"}).json()
    m.post("/api/me/2fa/enable", json={"code": mfa.code_at(s["secret"])})
    uid = [u["id"] for u in env.admin.get("/api/admin/users").json()["users"] if u["username"] == "mamaw"][0]
    assert env.admin.post(f"/api/admin/users/{uid}/2fa/reset").status_code == 200
    assert _sign_in(env.client(), mine).status_code == 401                     # lost phone: passkey gone
    assert m.get("/api/me/passkeys").status_code in (200, 401)


def test_disabled_account_cannot_use_its_passkey(env):
    env.admin.post("/api/admin/users", json={"username": "bob", "password": "bobpass1234",
                                             "role": "user", "status": "active"})
    bob = env.client("bob", "bobpass1234")
    dev = SoftAuthenticator()
    _add(bob, dev, password="bobpass1234")
    uid = [u["id"] for u in env.admin.get("/api/admin/users").json()["users"] if u["username"] == "bob"][0]
    env.admin.post(f"/api/admin/users/{uid}/status", params={"status": "disabled"})
    assert _sign_in(env.client(), dev).status_code == 401

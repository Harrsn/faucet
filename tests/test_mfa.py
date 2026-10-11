"""Two-factor sign-in: RFC 6238 codes, the two-step login, recovery codes,
replay protection, and admins being made to set it up."""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


def test_rfc6238_vector():
    from faucet import mfa
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"          # "12345678901234567890"
    assert mfa.code_at(secret, 59) == "287082"            # RFC 6238 SHA1 at T=59 (last 6 digits)
    assert mfa.code_at(secret, 1111111109) == "081804"
    assert mfa._match(secret, "287082", -1, now=59 + 30) == 1          # one step of drift ok
    assert mfa._match(secret, "287082", -1, now=59 + 90) is None       # three steps: no
    assert mfa._match(secret, "287082", 1, now=59) is None             # already used: no replay


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("SESSION_SECRET", "t")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db", "mfa", "auth", "auth_routes", "app")}
    mods["db"].init()
    from fastapi.testclient import TestClient

    def client():
        return TestClient(mods["app"].app, follow_redirects=False)

    def csrf(c):
        c.headers.update({"X-CSRF-Token": c.cookies.get("faucet_csrf")})
        return c
    return SimpleNamespace(m=mods, client=client, csrf=csrf, mfa=mods["mfa"], monkeypatch=monkeypatch)


def _enable(env, c, password):
    r = c.post("/api/me/2fa/setup", json={"password": password}).json()
    assert r["uri"].startswith("otpauth://totp/") and "<svg" in r["qr_svg"]
    wrong = "000000" if env.mfa.code_at(r["secret"]) != "000000" else "111111"
    assert c.post("/api/me/2fa/enable", json={"code": wrong}).status_code == 400
    out = c.post("/api/me/2fa/enable", json={"code": env.mfa.code_at(r["secret"])}).json()
    assert out["enabled"] and len(out["recovery_codes"]) == 10
    return r["secret"], out["recovery_codes"]


def test_two_step_login_recovery_and_replay(env):
    c = env.client()
    c.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    env.csrf(c)
    assert c.post("/api/me/2fa/setup", json={"password": "wrong"}).status_code == 400
    secret, codes = _enable(env, c, "adminpass123")

    d = env.client()
    r = d.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"}).json()
    assert r["status"] == "mfa_required" and "faucet_session" not in d.cookies   # no session yet
    assert d.post("/api/auth/login/mfa", json={"ticket": r["ticket"], "code": "123456"}).status_code == 401
    assert d.post("/api/auth/login/mfa", json={"ticket": "forged", "code": "123456"}).status_code == 401
    # the code used to enable can't be replayed
    assert d.post("/api/auth/login/mfa", json={"ticket": r["ticket"], "code": env.mfa.code_at(secret)}).status_code == 401
    ok = d.post("/api/auth/login/mfa", json={"ticket": r["ticket"], "code": codes[0]})
    assert ok.status_code == 200 and ok.json()["used"] == "recovery" and ok.json()["recovery_left"] == 9
    assert d.get("/api/auth/me").json()["authenticated"]
    e = env.client()
    t = e.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"}).json()["ticket"]
    assert e.post("/api/auth/login/mfa", json={"ticket": t, "code": codes[0]}).status_code == 401  # used up


@pytest.mark.requires_admin_2fa
def test_admin_without_2fa_can_only_set_it_up(env):
    c = env.client()
    c.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"}).json()
    assert r["mfa_setup_required"] is True
    env.csrf(c)
    assert c.get("/api/auth/me").json()["mfa_setup_required"] is True
    blocked = c.get("/api/series")
    assert blocked.status_code == 403 and blocked.json()["code"] == "mfa_setup_required"
    assert c.get("/api/me/2fa").json() == {"enabled": False, "since": None, "recovery_left": 0, "required": True}
    _enable(env, c, "adminpass123")
    assert c.get("/api/series").status_code == 200
    assert c.post("/api/me/2fa/disable", json={"password": "adminpass123"}).status_code == 400  # admins keep it


def test_members_optional_and_admin_reset(env):
    env.monkeypatch.setenv("FAUCET_REQUIRE_ADMIN_2FA", "0")
    a = env.client()
    a.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    a.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    env.csrf(a)
    a.post("/api/admin/users", json={"username": "mamaw", "password": "mamawpass1234", "role": "user", "status": "active"})
    m = env.client()
    assert m.post("/api/auth/login", json={"username": "mamaw", "password": "mamawpass1234"}).json()["status"] == "ok"
    env.csrf(m)
    assert m.get("/api/series").status_code == 200                       # members aren't forced
    _enable(env, m, "mamawpass1234")
    uid = [u for u in a.get("/api/admin/users").json()["users"] if u["username"] == "mamaw"][0]
    assert uid["mfa"] == 1
    assert a.post(f"/api/admin/users/{uid['id']}/2fa/reset").status_code == 200
    again = env.client()
    assert again.post("/api/auth/login", json={"username": "mamaw", "password": "mamawpass1234"}).json()["status"] == "ok"
    assert m.post("/api/me/2fa/disable", json={"password": "mamawpass1234"}).status_code == 200


def test_cli_disable(env, capsys):
    c = env.client()
    c.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    env.csrf(c)
    _enable(env, c, "adminpass123")
    assert env.mfa._cli(["x", "disable", "admin"]) == 0
    assert not env.mfa.enabled(1)
    assert env.mfa._cli(["x", "disable", "nobody"]) == 1

"""Phase 6.4b: sign-in page, strict script CSP, first-run setup."""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

STATIC = Path(__file__).resolve().parent.parent / "faucet" / "static"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("SESSION_SECRET", "t")
    from faucet import config as c0
    for k in c0.WIZARD_KEYS:
        monkeypatch.delenv(k, raising=False)
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db", "tmdb", "auth", "auth_routes", "importer", "app")}
    mods["db"].init()
    from fastapi.testclient import TestClient

    def client():
        return TestClient(mods["app"].app, follow_redirects=False)
    return SimpleNamespace(m=mods, client=client, db=mods["db"], tmp=tmp_path)


def _admin(env):
    c = env.client()
    assert c.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"}).json()["admin_created"]
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    c.headers.update({"X-CSRF-Token": c.cookies.get("faucet_csrf")})
    return c


def test_no_page_carries_inline_script():
    for f in STATIC.glob("*.html"):
        body = f.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", body), f.name
        assert not re.search(r"\son[a-z]+=", body), f.name


def test_auth_pages_and_strict_script_csp(env):
    c = env.client()
    for path in ("/login", "/register", "/reset"):
        r = c.get(path)
        assert r.status_code == 200 and f"/app/{env.m['app'].ASSET_VERSION}/auth.js" in r.text
        assert "no-store" in r.headers["cache-control"]
    csp = c.get("/login").headers["content-security-policy"]
    script = next(p for p in csp.split("; ") if p.startswith("script-src"))
    assert script == "script-src 'self'"


def test_fresh_install_needs_setup(env):
    assert env.client().get("/api/auth/me").json()["setup_needed"] is True
    admin = _admin(env)
    s = admin.get("/api/setup/state").json()
    assert s["complete"] is False and s["steps"] == {"libraries": False, "client": False,
                                                       "indexer": False, "tmdb": False}
    assert admin.post("/api/setup/import").status_code == 400          # needs a TMDb key
    assert admin.post("/api/setup/complete").json()["complete"] is True
    assert admin.get("/api/setup/state").json()["complete"] is True


def test_existing_configured_install_skips_the_wizard(env, monkeypatch):
    admin = _admin(env)
    monkeypatch.setenv("JACKETT_API_KEY", "k")
    env.m["config"].save({"JACKETT_API_KEY": "k", "CLIENT_URL": "http://t:9091/transmission/rpc"})
    with env.db.connect() as c:
        c.execute("INSERT INTO series (tmdb_id,title) VALUES (1,'Show')")
    assert admin.get("/api/setup/state").json()["complete"] is True
    assert env.db.get_setting("setup_complete")


def test_setup_import_runs_in_the_background(env, monkeypatch):
    admin = _admin(env)
    monkeypatch.setattr(env.m["tmdb"], "enabled", lambda: True)
    import threading
    gate = threading.Event()

    def fake_import(profile_id=None):
        gate.wait(5)
        return {"shows_imported": 3, "movies_imported": 1, "unmatched": []}
    monkeypatch.setattr(env.m["importer"], "import_library", fake_import)
    assert admin.post("/api/setup/import").json()["started"] is True
    assert admin.post("/api/setup/import").json()["started"] is False   # one at a time
    gate.set()
    import time
    for _ in range(50):
        st = admin.get("/api/setup/state").json()["import"]
        if not st["running"]:
            break
        time.sleep(0.05)
    assert st["result"]["shows_imported"] == 3


def test_members_cannot_touch_setup(env):
    admin = _admin(env)
    admin.post("/api/admin/users", json={"username": "bob", "password": "bobpass1234",
                                         "role": "user", "status": "active"})
    bob = env.client()
    bob.post("/api/auth/login", json={"username": "bob", "password": "bobpass1234"})
    bob.headers.update({"X-CSRF-Token": bob.cookies.get("faucet_csrf")})
    assert bob.get("/api/setup/state").status_code == 403
    assert bob.post("/api/setup/complete").status_code == 403

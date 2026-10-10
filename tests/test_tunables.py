"""Phase 6.3b: behaviour settings in the store, profiles validation."""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


def _fresh_cfg(tmp_path, monkeypatch, env=None):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("FAUCET_CONFIG_FILE", str(tmp_path / "config" / "faucet.env"))
    from faucet import config as c0
    for k in c0.WIZARD_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    return importlib.reload(c0)


def test_store_from_1_7_imports_the_behaviour_settings_once(tmp_path, monkeypatch):
    cfg = _fresh_cfg(tmp_path, monkeypatch, env={"JACKETT_URL": "http://j:9117"})
    # a 1.7 store: imported, but before tunables existed
    cfg._db_write({cfg.IMPORTED_KEY: {"keys": ["JACKETT_URL"], "from_file": False}})
    cfg = _fresh_cfg(tmp_path, monkeypatch, env={"HUNT_MAX_ACTIVE": "8", "STALL_HOURS": "nope"})
    assert cfg.stored()["HUNT_MAX_ACTIVE"] == "8"
    assert "STALL_HOURS" not in cfg.stored()                    # invalid: not imported
    cfg.save({"HUNT_MAX_ACTIVE": "12"})
    cfg = _fresh_cfg(tmp_path, monkeypatch, env={"HUNT_MAX_ACTIVE": "8"})   # restart
    assert cfg.stored()["HUNT_MAX_ACTIVE"] == "12"
    import os
    assert os.environ["HUNT_MAX_ACTIVE"] == "12"


def test_save_validates_tunables(tmp_path, monkeypatch):
    cfg = _fresh_cfg(tmp_path, monkeypatch, env={"JACKETT_URL": "http://j:9117"})
    for k, v in (("HUNT_MAX_ACTIVE", "0"), ("HUNT_MAX_ACTIVE", "x"), ("STALL_ACTION", "explode"),
                 ("HUNT_TRACKER_STALL_RATE", "2")):
        with pytest.raises(ValueError):
            cfg.save({k: v})
    cfg.save({"HUNT_UPGRADES": "off", "HUNT_TRACKER_STALL_RATE": "0.25"})
    assert cfg.stored()["HUNT_UPGRADES"] == "0" and cfg.stored()["HUNT_TRACKER_STALL_RATE"] == "0.25"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("SESSION_SECRET", "t")
    from faucet import config as c0
    for k in c0.WIZARD_KEYS:
        monkeypatch.delenv(k, raising=False)
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db", "auth", "auth_routes", "scheduler", "app")}
    mods["db"].init()
    from fastapi.testclient import TestClient

    def client(user=None, pw=None):
        c = TestClient(mods["app"].app, follow_redirects=False)
        if user:
            assert c.post("/api/auth/login", json={"username": user, "password": pw}).status_code == 200
            c.headers.update({"X-CSRF-Token": c.cookies.get("faucet_csrf")})
        return c

    client().post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    admin = client("admin", "adminpass123")
    admin.post("/api/admin/users", json={"username": "bob", "password": "bobpass1234",
                                         "role": "user", "status": "active"})
    return SimpleNamespace(m=mods, admin=admin, bob=client("bob", "bobpass1234"), db=mods["db"])


def test_tunables_api_applies_on_the_next_tick(app):
    g = app.admin.get("/api/settings/tunables", params={"section": "hunting"}).json()
    items = {it["key"]: it for grp in g["groups"] for it in grp["items"]}
    assert items["HUNT_MAX_ACTIVE"]["value"] == "5" and items["HUNT_MAX_ACTIVE"]["changed"] is False
    assert app.admin.put("/api/settings/tunables", json={"values": {"HUNT_MAX_ACTIVE": "9"}}).status_code == 200
    assert app.m["scheduler"]._int_env("HUNT_MAX_ACTIVE", 5) == 9        # no restart
    g = app.admin.get("/api/settings/tunables", params={"section": "hunting"}).json()
    assert {it["key"]: it for grp in g["groups"] for it in grp["items"]}["HUNT_MAX_ACTIVE"]["changed"] is True
    for bad in ({"HUNT_MAX_ACTIVE": "999"}, {"JACKETT_URL": "http://x"}, {"STALL_ACTION": "nuke"}):
        assert app.admin.put("/api/settings/tunables", json={"values": bad}).status_code == 400, bad
    assert app.bob.get("/api/settings/tunables", params={"section": "hunting"}).status_code == 403
    assert app.admin.get("/api/settings/tunables", params={"section": "nope"}).status_code == 422


def test_profile_validation_and_safe_delete(app):
    assert app.admin.post("/api/profiles", json={"name": "Bad", "resolutions": ["8k"]}).status_code == 422
    assert app.admin.post("/api/profiles", json={"name": " ", "min_seeders": 1}).status_code == 422
    assert app.admin.post("/api/profiles", json={"name": "X", "language": "english"}).status_code == 422
    pid = app.admin.post("/api/profiles", json={"name": "4K", "resolutions": ["2160p"],
                                                "sources": ["REMUX", "WEB-DL"], "min_seeders": 3}).json()["id"]
    with app.db.connect() as c:
        sid = c.execute("INSERT INTO series (tmdb_id,title,profile_id) VALUES (1,'S',?)", (pid,)).lastrowid
    app.db.set_setting("default_profile_id", pid)
    assert app.admin.delete(f"/api/profiles/{pid}").status_code == 200
    with app.db.connect() as c:
        assert c.execute("SELECT profile_id FROM series WHERE id=?", (sid,)).fetchone()[0] is None
        last = [r[0] for r in c.execute("SELECT id FROM profiles")]
    assert str(app.db.get_setting("default_profile_id") or "") == ""
    for p in last[1:]:
        app.admin.delete(f"/api/profiles/{p}")
    assert app.admin.delete(f"/api/profiles/{last[0]}").status_code == 400     # keep one
    assert app.admin.put("/api/profiles/9999", json={"name": "Z"}).status_code == 404
    assert app.admin.get("/api/profiles/options").json()["resolutions"][0] == "2160p"

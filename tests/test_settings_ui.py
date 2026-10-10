"""Phase 6.3a: library folder picker, write-only notification targets,
TMDb key test."""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    (nas / "media" / "tvshows" / "Show").mkdir(parents=True)
    (nas / "media" / "movies").mkdir()
    (nas / "media" / "kids").mkdir()
    (nas / "media" / ".hidden").mkdir()
    (nas / "media" / "file.txt").write_text("x")
    (tmp_path / "outside").mkdir()
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(nas / "media"))
    monkeypatch.setenv("BROWSE_ROOT", str(nas))
    monkeypatch.setenv("SESSION_SECRET", "t")
    monkeypatch.setenv("NOTIFY_URLS", "https://discord.com/api/webhooks/123/SECRETTOKEN,ntfy://ntfy.sh/mytopic")
    monkeypatch.setenv("NOTIFY_ON", "completed,failed")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db", "tmdb", "notify", "auth", "auth_routes", "app")}
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
    return SimpleNamespace(m=mods, admin=admin, bob=client("bob", "bobpass1234"),
                           nas=nas, tmp=tmp_path)


def test_browse_lists_folders_under_the_mounts_only(app):
    roots = app.admin.get("/api/libraries/browse").json()
    assert str(app.nas) in roots["roots"]
    d = app.admin.get("/api/libraries/browse", params={"path": str(app.nas / "media")}).json()
    names = {x["name"]: x for x in d["dirs"]}
    assert set(names) == {"tvshows", "movies", "kids"}               # no files, no dot-dirs
    assert names["tvshows"]["library"] == "TV Shows" and names["kids"]["library"] is None
    inner = app.admin.get("/api/libraries/browse", params={"path": str(app.nas / "media" / "tvshows")}).json()
    assert inner["dirs"][0]["in_library"] == "TV Shows"
    assert d["parent"] == str(app.nas)
    for bad in (str(app.tmp / "outside"), "/etc", str(app.nas / ".." / "outside")):
        assert app.admin.get("/api/libraries/browse", params={"path": bad}).status_code == 404, bad
    assert app.bob.get("/api/libraries/browse").status_code == 403


def test_notification_targets_are_write_only(app):
    g = app.admin.get("/api/settings/notifications").json()
    assert [t["kind"] for t in g["targets"]] == ["Discord", "ntfy"]
    assert "SECRETTOKEN" not in str(g) and "123" not in g["targets"][0]["display"]
    s = app.admin.get("/api/settings").json()
    assert "NOTIFY_URLS" not in s["env"] and s["env"]["NOTIFY_URLS_COUNT"] == 2
    r = app.admin.patch("/api/settings", json={"values": {"NOTIFY_URLS": ""}}).json()
    assert any("NOTIFY_URLS" in w for w in r["warnings"])
    assert len(app.m["config"].config.notify_urls) == 2              # the classic form can't wipe them


def test_notification_edit_keep_add_and_events(app):
    r = app.admin.put("/api/settings/notifications", json={
        "keep": [1], "add": ["gotify://push.example/AbCdEf"], "notify_on": ["added", "failed"]})
    assert r.status_code == 200
    urls = app.m["config"].config.notify_urls
    assert urls == ["ntfy://ntfy.sh/mytopic", "gotify://push.example/AbCdEf"]
    assert app.m["config"].config.notify_on == ["added", "failed"]
    for bad in ({"add": ["javascript:alert(1)"]}, {"add": ["https://a,https://b"]},
                {"notify_on": ["everything"]}):
        assert app.admin.put("/api/settings/notifications", json={"keep": [0, 1], **bad}).status_code == 400


def test_notification_test_sends_to_the_saved_target(app, monkeypatch):
    sent = []
    monkeypatch.setattr(app.m["notify"], "_dispatch", lambda url, t, b, image=None: sent.append(url))
    assert app.admin.post("/api/settings/notifications/test", json={"i": 0}).json()["ok"] is True
    assert sent == ["https://discord.com/api/webhooks/123/SECRETTOKEN"]

    def boom(url, *a, **k):
        raise RuntimeError(f"401 for {url}")
    monkeypatch.setattr(app.m["notify"], "_dispatch", boom)
    r = app.admin.post("/api/settings/notifications/test", json={"i": 0}).json()
    assert r["ok"] is False and "SECRETTOKEN" not in r["msg"]


def test_tmdb_key_test(app, monkeypatch):
    import requests

    class R:
        def __init__(self, code):
            self.status_code = code
    seen = []
    monkeypatch.setattr(requests, "get", lambda url, params=None, timeout=None: seen.append(params["api_key"]) or R(401 if params["api_key"] == "bad" else 200))
    assert app.admin.post("/api/settings/test-tmdb", json={"key": "bad"}).json()["ok"] is False
    assert app.admin.post("/api/settings/test-tmdb", json={"key": "good1"}).json()["ok"] is True
    assert seen == ["bad", "good1"]
    assert app.bob.post("/api/settings/test-tmdb", json={"key": "x"}).status_code == 403

"""Phase 6.4a: the wanted summary behind the new Wanted page."""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("SESSION_SECRET", "t")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db", "auth", "auth_routes", "app")}
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


def test_wanted_summary_groups_by_title(app):
    later = (datetime.now() + timedelta(hours=5)).isoformat(timespec="seconds")
    sooner = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
    with app.db.connect() as c:
        a = c.execute("INSERT INTO series (tmdb_id,title,poster) VALUES (1,'Alpha',NULL)").lastrowid
        b = c.execute("INSERT INTO series (tmdb_id,title) VALUES (2,'Bravo')").lastrowid
        m = c.execute("INSERT INTO movies (tmdb_id,title,year) VALUES (3,'Rags',2012)").lastrowid
        rows = [("episode", a, 1, 1, "wanted", "missing", None, 2),
                ("episode", a, 1, 2, "wanted", "missing", later, 1),
                ("episode", a, 1, 3, "wanted", "upgrade", sooner, 0),
                ("episode", a, 1, 4, "grabbed", "missing", None, 0),
                ("episode", b, 1, 1, "wanted", "missing", later, 5),
                ("movie", m, None, None, "wanted", "upgrade", None, 0),
                ("episode", b, 1, 9, "done", "missing", None, 0)]
        for r in rows:
            c.execute("INSERT INTO wanted (kind,series_id,season,episode,status,reason,next_search,search_count) "
                      "VALUES (?,?,?,?,?,?,?,?)", r)
    d = app.admin.get("/api/wanted/summary").json()
    by = {(i["kind"], i["title"]): i for i in d["items"]}
    alpha = by[("show", "Alpha")]
    assert (alpha["wanted"], alpha["downloading"], alpha["upgrades"], alpha["due"], alpha["searches"]) == (3, 1, 1, 1, 3)
    assert alpha["next_search"] == sooner
    assert by[("show", "Bravo")]["due"] == 0 and by[("movie", "Rags")]["year"] == 2012
    assert d["totals"] == {"wanted": 5, "downloading": 1, "upgrades": 2, "due": 2}
    assert d["items"][0]["title"] in ("Alpha", "Rags")                 # due first
    assert app.bob.get("/api/wanted/summary").status_code == 403

"""Phase 6.2: show/movie detail endpoints, movie interactive search, list
status, stills cache handling."""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    root = tmp_path / "media"
    (root / "tvshows").mkdir(parents=True)
    (root / "movies").mkdir()
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(root))
    monkeypatch.setenv("SESSION_SECRET", "t")
    monkeypatch.setenv("JACKETT_API_KEY", "key123456")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db", "tmdb", "search", "finder", "auth", "auth_routes", "app")}
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


def _show(db):
    with db.connect() as c:
        sid = c.execute("INSERT INTO series (tmdb_id,title,monitored) VALUES (7,'Show',1)").lastrowid
        for e, air in ((1, "2020-01-01"), (2, "2020-01-08"), (3, "2020-01-15"), (4, "2099-01-01"), (5, "")):
            c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) "
                      "VALUES (?,1,?,?,?)", (sid, e, f"Ep {e}", air))
        c.execute("INSERT INTO library_episodes (series_id,show_name,season,episode,quality,path,size,mtime) "
                  "VALUES (?,'Show',1,1,'1080p','/x/1.mkv',1,1)", (sid,))
        c.execute("INSERT INTO wanted (kind,series_id,season,episode,status,reason) "
                  "VALUES ('episode',?,1,2,'grabbed','missing')", (sid,))
        c.execute("INSERT INTO wanted (kind,series_id,season,episode,status,reason,next_search,search_count) "
                  "VALUES ('episode',?,1,3,'wanted','missing','2030-01-01T00:00:00',4)", (sid,))
        c.execute("INSERT INTO profiles (name) VALUES ('HD')")
    return sid


def test_series_detail_statuses_and_role_split(app, monkeypatch):
    sid = _show(app.db)
    fills = []
    monkeypatch.setattr(app.m["app"], "_fill_stills_async", lambda tid, seasons: fills.append((tid, seasons)))
    monkeypatch.setattr(app.m["tmdb"], "details", lambda *a, **k: {"status": "Ended", "backdrop": None})
    monkeypatch.setattr(app.m["tmdb"], "cached_season", lambda tid, s: None)
    d = app.admin.get(f"/api/series/{sid}/detail").json()
    eps = {e["episode"]: e for e in d["seasons"][0]["episodes"]}
    assert [eps[n]["status"] for n in (1, 2, 3, 4, 5)] == \
        ["have", "downloading", "wanted", "unaired", "unaired"]
    assert eps[3]["searches"] == 4 and eps[3]["next_search"].startswith("2030")
    assert d["have"] == 1 and d["total"] == 3 and d["wanted"] == 1 and d["downloading"] == 1
    assert "HD" in [p["name"] for p in d["profiles"]] and "folders" in d
    assert fills == [(7, [1])]                                   # stills fetched in the background
    b = app.bob.get(f"/api/series/{sid}/detail").json()
    assert b["profiles"] == [] and "folders" not in b
    assert app.admin.get("/api/series/999/detail").status_code == 404


def test_not_monitored_episodes_read_as_skipped(app, monkeypatch):
    sid = _show(app.db)
    with app.db.connect() as c:
        c.execute("UPDATE series SET monitor_mode='none' WHERE id=?", (sid,))
        c.execute("DELETE FROM wanted WHERE series_id=? AND episode=3", (sid,))
    monkeypatch.setattr(app.m["tmdb"], "details", lambda *a, **k: {})
    monkeypatch.setattr(app.m["tmdb"], "cached_season", lambda tid, s: [])
    d = app.admin.get(f"/api/series/{sid}/detail").json()
    assert {e["episode"]: e["status"] for e in d["seasons"][0]["episodes"]}[3] == "skipped"


def test_series_list_carries_tv_status_from_cache_only(app, monkeypatch):
    _show(app.db)
    monkeypatch.setattr(app.m["tmdb"], "cached_details", lambda tid, kind: {"status": "Ended"})
    s = app.admin.get("/api/series").json()["series"][0]
    assert s["tv_status"] == "Ended" and s["ended"] is True


def test_season_cache_without_stills_is_refetched(app, monkeypatch):
    T = app.m["tmdb"]
    monkeypatch.setattr(T, "enabled", lambda: True)
    T._cache_put("season:7:1", [{"season": 1, "episode": 1, "title": "x", "air_date": ""}])
    assert T.cached_season(7, 1) is None
    calls = []
    monkeypatch.setattr(T, "_get", lambda path, params: calls.append(path) or {"episodes": [
        {"episode_number": 1, "season_number": 1, "name": "x", "still_path": "/s.jpg"}]})
    eps = T.season_episodes(7, 1)
    assert calls == ["/tv/7/season/1"] and eps[0]["still"].endswith("/s.jpg")
    assert T.cached_season(7, 1)[0]["still"].endswith("/s.jpg")


def _movie(db, want=True):
    with db.connect() as c:
        mid = c.execute("INSERT INTO movies (tmdb_id,title,year) VALUES (9,'Rags',2012)").lastrowid
        if want:
            c.execute("INSERT INTO wanted (kind,series_id,status,reason,search_count) "
                      "VALUES ('movie',?,'wanted','missing',2)", (mid,))
    return mid


def test_movie_releases_are_verified_and_ranked(app, monkeypatch):
    mid = _movie(app.db)
    S = app.m["search"]
    monkeypatch.setattr(app.m["finder"], "find", lambda *a, **k: S.Results([
        {"title": "Rags.2012.1080p.WEB-DL", "href": "magnet:a", "seeders": 50, "size": 1, "badges": {}},
        {"title": "Rags.to.Riches.2012.1080p", "href": "magnet:b", "seeders": 90, "size": 1, "badges": {}},
        {"title": "Rags.2012.720p.HDTV", "href": "magnet:c", "seeders": 10, "size": 1, "badges": {}}]))
    monkeypatch.setattr(app.m["finder"], "ids_for", lambda *a: {})
    d = app.admin.get(f"/api/movies/{mid}/releases").json()
    assert [r["href"] for r in d["releases"]] == ["magnet:a", "magnet:c"]   # wrong title dropped
    assert app.bob.get(f"/api/movies/{mid}/releases").status_code == 403


def test_movie_grab_stands_the_hunter_down(app, monkeypatch):
    mid = _movie(app.db)

    class C:
        def add(self, href, d=None):
            return SimpleNamespace(id="1", name="Rags.2012.1080p", duplicate=False)
    monkeypatch.setattr(app.m["app"], "client", lambda: C())
    r = app.admin.post(f"/api/movies/{mid}/grab", json={"magnet": "magnet:a", "title": "Rags.2012.1080p"})
    assert r.status_code == 200
    d = app.admin.get(f"/api/movies/{mid}/detail").json()
    assert d["want"]["status"] == "grabbed" and d["want"]["search_count"] == 0


def test_movie_detail_404_and_member_view(app, monkeypatch):
    mid = _movie(app.db)
    monkeypatch.setattr(app.m["tmdb"], "details", lambda *a, **k: {})
    assert app.admin.get("/api/movies/999/detail").status_code == 404
    b = app.bob.get(f"/api/movies/{mid}/detail").json()
    assert b["want"]["reason"] == "missing" and b["profiles"] == []

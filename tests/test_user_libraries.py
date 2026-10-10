"""Per-user library access: a member limited to some libraries sees only
those — everywhere, enforced on the server — and their approved requests are
filed into their library."""
from __future__ import annotations

import importlib
from datetime import date
from types import SimpleNamespace

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "media"
    for d in ("tvshows", "movies", "Mamaw/Shows", "Mamaw/Movies"):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(root))
    monkeypatch.setenv("SESSION_SECRET", "t")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db", "tmdb", "libraries", "access", "requests_flow",
                      "auth", "auth_routes", "app")}
    mods["db"].init()
    monkeypatch.setattr(mods["tmdb"], "enabled", lambda: False)
    LB = mods["libraries"]
    ms, _ = LB.add("Mamaw's Shows", "tv", str(root / "Mamaw" / "Shows"))
    mm, _ = LB.add("Mamaw's Movies", "movie", str(root / "Mamaw" / "Movies"))
    from fastapi.testclient import TestClient

    def client(user=None, pw=None):
        c = TestClient(mods["app"].app, follow_redirects=False)
        if user:
            assert c.post("/api/auth/login", json={"username": user, "password": pw}).status_code == 200
            c.headers.update({"X-CSRF-Token": c.cookies.get("faucet_csrf")})
        return c

    client().post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    admin = client("admin", "adminpass123")
    for name in ("mamaw", "bob"):
        admin.post("/api/admin/users", json={"username": name, "password": f"{name}pass1234",
                                             "role": "user", "status": "active"})
    users = {u["username"]: u["id"] for u in admin.get("/api/admin/users").json()["users"]}
    db = mods["db"]
    today = date.today().isoformat()
    with db.connect() as c:
        fg = c.execute("INSERT INTO series (tmdb_id,title,monitored) VALUES (1,'Family Guy',1)").lastrowid
        k = c.execute("INSERT INTO series (tmdb_id,title,monitored,library_id) VALUES (2,'The Kardashians',1,?)",
                      (ms,)).lastrowid
        for sid in (fg, k):
            c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) VALUES (?,1,1,'E1',?)",
                      (sid, today))
        dune = c.execute("INSERT INTO movies (tmdb_id,title,year) VALUES (3,'Dune',2021)").lastrowid
        elvis = c.execute("INSERT INTO movies (tmdb_id,title,year,library_id) VALUES (4,'Speedway',1968,?)",
                          (mm,)).lastrowid
    return SimpleNamespace(m=mods, admin=admin, client=client, users=users, ms=ms, mm=mm,
                           fg=fg, k=k, dune=dune, elvis=elvis, db=db)


def _limit(env, name, libs, rtv=None, rmv=None):
    r = env.admin.put(f"/api/admin/users/{env.users[name]}/libraries",
                      json={"libraries": libs, "request_tv_library": rtv, "request_movie_library": rmv})
    assert r.status_code == 200, r.text


def test_a_limited_member_only_sees_their_libraries(env, monkeypatch):
    _limit(env, "mamaw", [env.ms, env.mm])
    mamaw = env.client("mamaw", "mamawpass1234")
    monkeypatch.setattr(env.m["tmdb"], "details", lambda *a, **k: {})
    monkeypatch.setattr(env.m["tmdb"], "cached_season", lambda *a: [])
    assert [s["name"] for s in mamaw.get("/api/nav").json()["sections"]] == ["Mamaw's Shows", "Mamaw's Movies"]
    assert [s["title"] for s in mamaw.get("/api/series").json()["series"]] == ["The Kardashians"]
    assert [m["title"] for m in mamaw.get("/api/movies").json()["movies"]] == ["Speedway"]
    for url in (f"/api/series/{env.fg}/detail", f"/api/series/{env.fg}/episodes",
                f"/api/series/{env.fg}/wanted", f"/api/movies/{env.dune}/detail"):
        assert mamaw.get(url).status_code == 404, url              # hidden, not forbidden
    assert mamaw.get(f"/api/series/{env.k}/detail").status_code == 200
    assert {e["title"] for e in mamaw.get("/api/calendar").json()["episodes"]} == {"The Kardashians"}
    # everyone else is untouched
    bob = env.client("bob", "bobpass1234")
    assert len(bob.get("/api/series").json()["series"]) == 2
    assert len(env.admin.get("/api/nav").json()["sections"]) == 4


def test_home_is_filtered(env):
    _limit(env, "mamaw", [env.ms, env.mm])
    with env.db.connect() as c:
        for sid, name, p in ((env.fg, "Family Guy", "/x/fg.mkv"), (env.k, "The Kardashians", "/x/k.mkv")):
            c.execute("INSERT INTO library_episodes (series_id,show_name,season,episode,path,size,mtime) "
                      "VALUES (?,?,1,1,?,1,1)", (sid, name, p))
    mamaw = env.client("mamaw", "mamawpass1234")
    home = mamaw.get("/api/home").json()
    assert {e["title"] for e in home["episodes"]} == {"The Kardashians"}
    assert home["hero"]["title"] == "The Kardashians"


def test_requests_are_filed_into_the_requesters_library(env):
    _limit(env, "mamaw", [env.ms, env.mm])                         # one of each: implicit target
    mamaw = env.client("mamaw", "mamawpass1234")
    r = mamaw.post("/api/requests", json={"media_type": "tv", "tmdb_id": 50, "title": "The Voice",
                                          "year": 2011}).json()
    env.admin.post(f"/api/requests/{r['request_id']}/approve")
    with env.db.connect() as c:
        lib = c.execute("SELECT library_id FROM series WHERE tmdb_id=50").fetchone()[0]
    assert lib == env.ms
    assert [s["title"] for s in mamaw.get("/api/series").json()["series"]] == ["The Kardashians", "The Voice"]


def test_a_title_in_a_hidden_library_is_already_elsewhere(env):
    _limit(env, "mamaw", [env.ms, env.mm])
    mamaw = env.client("mamaw", "mamawpass1234")
    r = mamaw.post("/api/requests", json={"media_type": "tv", "tmdb_id": 1, "title": "Family Guy"}).json()
    assert r["status"] == "already_elsewhere" and "Family Guy" not in str(r)


def test_access_validation_and_admin_only(env):
    uid = env.users["mamaw"]
    put = lambda body: env.admin.put(f"/api/admin/users/{uid}/libraries", json=body)  # noqa: E731
    assert put({"libraries": [9999]}).status_code == 400
    assert put({"libraries": [env.ms], "request_tv_library": env.mm}).status_code == 400   # wrong kind
    tv_default = [x for x in env.admin.get("/api/nav").json()["sections"] if x["kind"] == "tv" and x["default"]][0]["id"]
    assert put({"libraries": [env.ms], "request_tv_library": tv_default}).status_code == 400  # can't see it
    assert put({"libraries": None}).status_code == 200                                     # back to all
    assert env.admin.put("/api/admin/users/9999/libraries", json={"libraries": None}).status_code == 404
    bob = env.client("bob", "bobpass1234")
    assert bob.put(f"/api/admin/users/{uid}/libraries", json={"libraries": None}).status_code == 403

"""Per-title libraries: a relative's folder (Mamaw's Shows / Movies) gets its
own libraries; titles living there are filed there; finished downloads for
them are routed there; importing that one library adopts only its titles."""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

MB = 1024 * 1024


def mk(p, mb=2):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * int(mb * MB))


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "media"
    for d in ("tvshows", "movies", "Mamaw/Shows", "Mamaw/Movies"):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(root))
    mods = {m: importlib.reload(importlib.import_module("faucet." + m)) for m in ("config", "db")}
    mods["db"].init()
    for m in ("tmdb", "libraries", "library", "series", "movies", "importer"):
        mods[m] = importlib.reload(importlib.import_module("faucet." + m))
    monkeypatch.setattr(mods["library"], "MIN_SIZE", 1 * MB)
    LB = mods["libraries"]
    ms, _ = LB.add("Mamaw's Shows", "tv", str(root / "Mamaw" / "Shows"))
    mm, _ = LB.add("Mamaw's Movies", "movie", str(root / "Mamaw" / "Movies"))
    return SimpleNamespace(root=root, db=mods["db"], LB=LB, L=mods["library"], S=mods["series"],
                           I=mods["importer"], T=mods["tmdb"], ms=ms, mm=mm)


def _show(db, title, tmdb):
    with db.connect() as c:
        return c.execute("INSERT INTO series (tmdb_id,title,monitored) VALUES (?,?,1)",
                         (tmdb, title)).lastrowid


def test_titles_in_mamaws_folders_are_filed_there(env):
    mk(env.root / "Mamaw/Shows/The Kardashians/Season 01/The Kardashians - S01E01.mkv")
    mk(env.root / "tvshows/Family Guy/Season 01/Family Guy - S01E01.mkv")
    mk(env.root / "Mamaw/Movies/Blue Hawaii (1961)/Blue Hawaii (1961).mkv")
    k = _show(env.db, "The Kardashians", 1)
    fg = _show(env.db, "Family Guy", 2)
    with env.db.connect() as c:
        bh = c.execute("INSERT INTO movies (tmdb_id,title,year) VALUES (3,'Blue Hawaii',1961)").lastrowid
    env.L.scan()
    with env.db.connect() as c:
        lib = {r["title"]: r["library_id"] for r in c.execute("SELECT title, library_id FROM series")}
        mv = c.execute("SELECT library_id FROM movies WHERE id=?", (bh,)).fetchone()[0]
    assert lib == {"The Kardashians": env.ms, "Family Guy": None}       # default stays NULL
    assert mv == env.mm
    assert env.LB.title_library("tv", k)["name"] == "Mamaw's Shows"
    assert env.LB.title_library("tv", fg)["name"] == "TV Shows"


def test_a_chosen_library_is_never_overridden(env):
    mk(env.root / "Mamaw/Shows/The Voice/Season 01/The Voice - S01E01.mkv")
    v = _show(env.db, "The Voice", 4)
    tv_default = [x for x in env.LB.of_kind("tv") if x["is_default"]][0]["id"]
    env.LB.set_title_library("tv", v, tv_default)
    env.L.scan()
    with env.db.connect() as c:
        assert c.execute("SELECT library_id FROM series WHERE id=?", (v,)).fetchone()[0] == tv_default
    with pytest.raises(ValueError):
        env.LB.set_title_library("tv", v, env.mm)                        # a movies library


def test_finished_downloads_route_to_the_titles_library(env):
    k = _show(env.db, "The Kardashians", 1)
    env.LB.set_title_library("tv", k, env.ms)
    with env.db.connect() as c:
        m = c.execute("INSERT INTO movies (tmdb_id,title,year) VALUES (5,'Speedway',1968)").lastrowid
    env.LB.set_title_library("movie", m, env.mm)
    mamaw_shows, mamaw_movies = str(env.root / "Mamaw/Shows"), str(env.root / "Mamaw/Movies")
    assert env.LB.route("The.Kardashians.S07E03.1080p.WEB.h264-EDITH") == {"FAUCET_TV_DIR": mamaw_shows}
    assert env.LB.route("Speedway.1968.1080p.BluRay.x264") == {"FAUCET_MOVIES_DIR": mamaw_movies}
    assert env.LB.route("Family.Guy.S22E01.1080p.WEB") == {}
    assert env.LB.route("The.Kardashians.Unrelated.Spinoff.S01E01") == {}  # spin-off: not her show
    env_ = env.LB.sorter_env("The.Kardashians.S07E03.1080p.WEB.h264-EDITH")
    assert env_["FAUCET_TV_DIR"] == mamaw_shows and env_["FAUCET_MOVIES_DIR"].endswith("/movies")


def test_importing_one_library_adopts_only_its_titles(env, monkeypatch):
    mk(env.root / "Mamaw/Shows/The Kardashians/Season 01/The Kardashians - S01E01.mkv")
    mk(env.root / "tvshows/Supernatural/Season 01/Supernatural - S01E01.mkv")
    mk(env.root / "Mamaw/Movies/Speedway (1968)/Speedway (1968).mkv")
    monkeypatch.setattr(env.T, "enabled", lambda: True)
    ids = {"The Kardashians": 11, "Supernatural": 12, "Speedway": 13}
    monkeypatch.setattr(env.I, "_best_tmdb_match", lambda name, kind, year=None: {
        "tmdb_id": ids[name], "title": name, "year": year} if name in ids else None)
    monkeypatch.setattr(env.S, "refresh_series", lambda *a, **k: None, raising=False)
    r = env.I.import_library(None, library_ids=[env.ms, env.mm], monitor="none")
    assert r["shows_imported"] == 1 and r["movies_imported"] == 1
    with env.db.connect() as c:
        shows = {x["title"]: dict(x) for x in c.execute("SELECT title, library_id, monitor_mode FROM series")}
        movie = dict(c.execute("SELECT library_id, monitored FROM movies").fetchone())
        wants = c.execute("SELECT COUNT(*) FROM wanted").fetchone()[0]
    assert set(shows) == {"The Kardashians"}                             # Supernatural left alone
    assert shows["The Kardashians"]["library_id"] == env.ms and shows["The Kardashians"]["monitor_mode"] == "none"
    assert movie == {"library_id": env.mm, "monitored": 0} and wants == 0


def test_save_to_api(env, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "t")
    for m in ("auth", "auth_routes", "app"):
        importlib.reload(importlib.import_module("faucet." + m))
    from fastapi.testclient import TestClient

    from faucet import app as appmod
    monkeypatch.setattr(appmod.tmdbmod, "details", lambda *a, **k: {})
    c = TestClient(appmod.app, follow_redirects=False)
    c.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    c.headers.update({"X-CSRF-Token": c.cookies.get("faucet_csrf")})
    k = _show(env.db, "The Kardashians", 1)
    d = c.get(f"/api/series/{k}/detail").json()["save_to"]
    assert d["current"] == "TV Shows" and {o["name"] for o in d["options"]} == {"TV Shows", "Mamaw's Shows"}
    r = c.post(f"/api/series/{k}/library", json={"library_id": env.ms}).json()
    assert r["current"] == "Mamaw's Shows"
    assert c.post(f"/api/series/{k}/library", json={"library_id": env.mm}).status_code == 400
    assert c.post(f"/api/series/{k}/library", json={"library_id": None}).json()["current"] == "TV Shows"
    assert c.post("/api/series/999/library", json={"library_id": None}).status_code == 404
    assert c.post(f"/api/libraries/{env.ms}/import", json={"monitor": "everything"}).status_code == 400


def test_library_sections_for_the_sidebar(env, monkeypatch):
    k = _show(env.db, "The Kardashians", 1)
    _show(env.db, "Family Guy", 2)
    env.LB.set_title_library("tv", k, env.ms)
    with env.db.connect() as c:
        c.execute("INSERT INTO movies (tmdb_id,title,year) VALUES (3,'Speedway',1968)")
    secs = env.LB.sections()
    assert [(s["name"], s["kind"], s["count"], s["default"]) for s in secs] == [
        ("TV Shows", "tv", 1, True), ("Mamaw's Shows", "tv", 1, False),
        ("Movies", "movie", 1, True), ("Mamaw's Movies", "movie", 0, False)]
    # a title pointed at a disabled library falls back to the default section
    env.LB.update(env.ms, enabled=False)
    assert [s["count"] for s in env.LB.sections() if s["kind"] == "tv"] == [2]

    monkeypatch.setenv("SESSION_SECRET", "t")
    for m in ("auth", "auth_routes", "app"):
        importlib.reload(importlib.import_module("faucet." + m))
    from fastapi.testclient import TestClient

    from faucet import app as appmod
    c = TestClient(appmod.app, follow_redirects=False)
    c.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    c.headers.update({"X-CSRF-Token": c.cookies.get("faucet_csrf")})
    c.post("/api/admin/users", json={"username": "bob", "password": "bobpass1234",
                                     "role": "user", "status": "active"})
    env.LB.update(env.ms, enabled=True)
    bob = TestClient(appmod.app, follow_redirects=False)
    bob.post("/api/auth/login", json={"username": "bob", "password": "bobpass1234"})
    assert [s["name"] for s in bob.get("/api/nav").json()["sections"]][:2] == ["TV Shows", "Mamaw's Shows"]
    libs = {s["title"]: s["lib"] for s in bob.get("/api/series").json()["series"]}
    tv_default = [s for s in env.LB.sections() if s["kind"] == "tv" and s["default"]][0]["id"]
    assert libs == {"The Kardashians": env.ms, "Family Guy": tv_default}

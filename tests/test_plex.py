"""Plex: pairing Faucet libraries with Plex's, translating paths, and asking
Plex to scan exactly the folders the sorter filed into."""
from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

import pytest

SECTIONS = [
    {"key": "1", "title": "TV Shows", "type": "show", "locations": ["/data/tvshows"]},
    {"key": "2", "title": "Movies", "type": "movie", "locations": ["/data/movies"]},
    {"key": "3", "title": "Mamaws Shows", "type": "show", "locations": ["/data/Mamaw/Shows"]},
    {"key": "4", "title": "Mamaws Movies", "type": "movie", "locations": ["/data/Mamaw/Movies"]},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "nas" / "media"
    for d in ("tvshows", "movies", "Mamaw/Shows", "Mamaw/Movies"):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(root))
    monkeypatch.setenv("SESSION_SECRET", "t")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db", "libraries", "plex", "auth", "auth_routes", "app")}
    mods["db"].init()
    L = mods["libraries"]
    L.add("Mamaw's Shows", "tv", str(root / "Mamaw" / "Shows"))
    L.add("Mamaw's Movies", "movie", str(root / "Mamaw" / "Movies"))
    calls = []

    def fake_get(path, url=None, token=None, timeout=10):
        calls.append(path)
        if path == "/":
            return {"friendlyName": "Korodi.tv", "version": "1.42.1.10060-4e8b05daf"}
        if path == "/library/sections":
            return {"Directory": [{"key": s["key"], "title": s["title"], "type": s["type"],
                                   "Location": [{"path": p} for p in s["locations"]]} for s in SECTIONS]}
        return {}
    monkeypatch.setattr(mods["plex"], "_get", fake_get)
    monkeypatch.setenv("PLEX_URL", "http://10.0.0.17:32400")
    monkeypatch.setenv("PLEX_TOKEN", "secret-token-xyz")
    libs = {lib["name"]: lib for lib in L.all_libraries()}
    return SimpleNamespace(m=mods, P=mods["plex"], L=L, root=root, calls=calls, libs=libs)


def test_pairs_libraries_by_folder_name(env):
    assert env.P.auto_pair(SECTIONS) == 4
    got = {env.L.get(k)["name"]: (v["section_title"], v["plex_path"]) for k, v in env.P.links().items()}
    assert got == {"TV Shows": ("TV Shows", "/data/tvshows"), "Movies": ("Movies", "/data/movies"),
                   "Mamaw's Shows": ("Mamaws Shows", "/data/Mamaw/Shows"),
                   "Mamaw's Movies": ("Mamaws Movies", "/data/Mamaw/Movies")}
    assert env.P.auto_pair(SECTIONS) == 0                                  # nothing changes twice


def test_ambiguous_or_no_match_stays_unpaired(env):
    lib = {"kind": "tv", "path": "/nas/media/tvshows"}
    two = [{"key": "1", "title": "A", "type": "show", "locations": ["/x/tvshows"]},
           {"key": "9", "title": "B", "type": "show", "locations": ["/y/tvshows"]}]
    assert env.P.suggest(lib, two) is None
    assert env.P.suggest(lib, [{"key": "2", "title": "M", "type": "movie", "locations": ["/data/tvshows"]}]) is None
    assert env.P.suggest({"kind": "tv", "path": "/nas/telly"}, SECTIONS) is None


def test_scans_exactly_the_filed_folders(env):
    env.P.auto_pair(SECTIONS)
    tv = env.root / "tvshows"
    files = [str(tv / "Family Guy" / "Season 23" / "Family Guy - S23E04.mkv"),
             str(tv / "Family Guy" / "Season 23" / "Family Guy - S23E05.mkv"),
             str(env.root / "Mamaw" / "Movies" / "Speedway (1968)" / "Speedway (1968).mkv"),
             "/somewhere/else/entirely.mkv"]
    env.calls.clear()
    assert env.P.scan_files(files) == 2                                    # one per folder, unknown skipped
    assert env.calls == ["/library/sections/1/refresh?path=%2Fdata%2Ftvshows%2FFamily%20Guy%2FSeason%2023",
                         "/library/sections/4/refresh?path=%2Fdata%2FMamaw%2FMovies%2FSpeedway%20%281968%29"]
    assert [s["ok"] for s in env.P.recent_scans()] == [1, 1]


def test_windows_plex_paths(env):
    env.P.set_link(env.libs["TV Shows"]["id"], "1", r"D:\Media\TV", SECTIONS)
    sec, path = env.P.to_plex(str(env.root / "tvshows" / "Bob's Burgers" / "Season 15"))
    assert (sec, path) == ("1", r"D:\Media\TV\Bob's Burgers\Season 15")


def test_manual_pairing_survives_auto_pairing_and_unpairing(env):
    env.P.auto_pair(SECTIONS)
    tv = env.libs["TV Shows"]["id"]
    env.P.set_link(tv, None, None, SECTIONS)                               # "don't pair this one"
    env.P.auto_pair(SECTIONS)
    assert env.P.links()[tv]["section_key"] is None
    assert env.P.to_plex(str(env.root / "tvshows" / "X")) is None
    with pytest.raises(ValueError):
        env.P.set_link(tv, "99", "/data/x", SECTIONS)


def test_sorter_reports_what_it_filed(env, tmp_path, monkeypatch):
    log = env.P.placed_log()
    monkeypatch.setenv("FAUCET_PLACED_LOG", log)
    from faucet import sort
    sort._note_placed(tmp_path / "a" / "b.mkv")
    sort._note_placed(tmp_path / "a" / "c.mkv")
    with open(log) as f:
        assert f.read().split() == [str(tmp_path / "a" / "b.mkv"), str(tmp_path / "a" / "c.mkv")]
    env.P.scan_from_log(log)
    assert not os.path.exists(log)                                         # cleaned up


def test_a_failing_plex_never_breaks_anything(env, monkeypatch):
    env.P.auto_pair(SECTIONS)

    def down(*a, **k):
        raise env.P.PlexError("can't reach Plex at http://10.0.0.17:32400: ConnectTimeout")
    monkeypatch.setattr(env.P, "_get", down)
    assert env.P.scan_files([str(env.root / "tvshows" / "A" / "Season 01" / "a.mkv")]) == 0
    assert env.P.recent_scans()[0]["ok"] == 0 and "ConnectTimeout" in env.P.recent_scans()[0]["error"]
    log = env.P.placed_log()
    with open(log, "w") as f:
        f.write(str(env.root / "tvshows" / "A" / "Season 01" / "a.mkv") + "\n")
    assert env.P.scan_from_log(log) == 0


def test_api(env):
    from fastapi.testclient import TestClient
    c = TestClient(env.m["app"].app, follow_redirects=False)
    c.headers["X-Faucet-Auth"] = "2"
    c.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    c.headers["X-CSRF-Token"] = c.cookies.get("faucet_csrf")
    d = c.get("/api/plex").json()
    assert d["ok"] and d["name"] == "Korodi.tv" and len(d["sections"]) == 4
    assert {p["name"]: p["section_title"] for p in d["pairs"]}["Mamaw's Shows"] == "Mamaws Shows"
    s = c.get("/api/settings").json()
    assert s["env"]["PLEX_TOKEN_SET"] is True and "secret-token-xyz" not in str(s)
    tv = env.libs["TV Shows"]["id"]
    assert c.put("/api/plex/pairs", json={"library_id": tv, "section_key": "1", "plex_path": "tvshows"}).status_code == 400
    assert c.put("/api/plex/pairs", json={"library_id": tv, "section_key": "1",
                                          "plex_path": "/mnt/tv"}).status_code == 200
    assert c.post("/api/plex/scan", json={"library_id": tv}).status_code == 200
    assert env.calls[-1] == "/library/sections/1/refresh?path=%2Fmnt%2Ftv"
    assert c.post("/api/plex/test", json={"url": "ftp://x"}).status_code == 400


def test_case_and_depth_break_ties(env):
    movies = [{"key": "2", "title": "Movies", "type": "movie", "locations": ["/data/movies"]},
              {"key": "4", "title": "Mamaws Movies", "type": "movie", "locations": ["/data/Mamaw/Movies"]}]
    assert env.P.suggest({"kind": "movie", "path": "/nas/media/movies"}, movies)["key"] == "2"
    assert env.P.suggest({"kind": "movie", "path": "/nas/media/Mamaw/Movies"}, movies)["key"] == "4"
    # Windows Plex: different case only, still pairs
    win = [{"key": "7", "title": "TV", "type": "show", "locations": [r"D:\\Media\\TVShows"]}]
    assert env.P.suggest({"kind": "tv", "path": "/nas/media/tvshows"}, win)["key"] == "7"


def _plex_library(monkeypatch, env, watched_eps, page=2):
    """A fake Plex with Family Guy (TMDb 1434) in section 1, Speedway (TMDb
    5) in section 4, paginated `page` items at a time."""
    shows = [{"ratingKey": "100", "title": "Family Guy", "Guid": [{"id": "imdb://tt0182576"}, {"id": "tmdb://1434"}]},
             {"ratingKey": "200", "title": "Local Thing", "Guid": []}]
    eps = [{"grandparentRatingKey": "100", "parentIndex": 23, "index": i, "viewCount": 1 if (23, i) in watched_eps else 0,
            "lastViewedAt": 1791700000 + i} for i in range(1, 6)]
    eps.append({"grandparentRatingKey": "200", "parentIndex": 1, "index": 1, "viewCount": 2})
    movies = [{"title": "Speedway", "Guid": [{"id": "tmdb://5"}], "viewCount": 3, "lastViewedAt": 1791600000}]
    base = env.P._get

    def fake(path, url=None, token=None, timeout=10):
        import urllib.parse as up
        if "/all?" not in path:
            return base(path, url, token, timeout)
        q = dict(up.parse_qsl(path.split("?", 1)[1]))
        sec = path.split("/")[3]
        items = {("1", "2"): shows, ("1", "4"): eps, ("4", "1"): movies}.get((sec, q["type"]), [])
        start, size = int(q["X-Plex-Container-Start"]), min(page, int(q["X-Plex-Container-Size"]))
        return {"Metadata": items[start:start + size], "totalSize": len(items), "size": len(items[start:start + size])}
    monkeypatch.setattr(env.P, "_get", fake)


def test_watched_status_sync(env, monkeypatch):
    env.P.auto_pair(SECTIONS)
    _plex_library(monkeypatch, env, {(23, 1), (23, 2), (23, 4)})
    r = env.P.sync_watched()
    assert r == {"ok": True, "episodes": 3, "movies": 1, "unmatched": 1}     # Local Thing has no TMDb id
    w = env.P.watched_episodes(1434)
    assert sorted(w) == [(23, 1), (23, 2), (23, 4)] and w[(23, 1)]["last_viewed"]
    assert env.P.watched_movie(5)["views"] == 3
    _plex_library(monkeypatch, env, {(23, 1)})                              # un-watched in Plex
    env.P.sync_watched()
    assert sorted(env.P.watched_episodes(1434)) == [(23, 1)]
    assert env.P.watched_status()["episodes"] == 1


def test_watched_shows_on_detail_pages(env, monkeypatch):
    from fastapi.testclient import TestClient
    env.P.auto_pair(SECTIONS)
    _plex_library(monkeypatch, env, {(23, 1), (23, 2)})
    db = env.m["db"]
    with db.connect() as c:
        sid = c.execute("INSERT INTO series (tmdb_id,title,monitored) VALUES (1434,'Family Guy',1)").lastrowid
        for i in (1, 2, 3):
            c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) VALUES (?,23,?,?,'2025-01-01')",
                      (sid, i, f"E{i}"))
        mid = c.execute("INSERT INTO movies (tmdb_id,title,year) VALUES (5,'Speedway',1968)").lastrowid
    c = TestClient(env.m["app"].app, follow_redirects=False)
    c.headers["X-Faucet-Auth"] = "2"
    c.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    c.headers["X-CSRF-Token"] = c.cookies.get("faucet_csrf")
    monkeypatch.setattr(env.m["app"].tmdbmod, "details", lambda *a, **k: {})
    monkeypatch.setattr(env.m["app"].tmdbmod, "cached_season", lambda *a, **k: [])
    r = c.post("/api/plex/watched/sync").json()
    assert (r["episodes"], r["movies"]) == (2, 1)
    d = c.get(f"/api/series/{sid}/detail").json()
    s23 = next(x for x in d["seasons"] if x["season"] == 23)
    assert s23["watched"] == 2 and [bool(e["watched"]) for e in s23["episodes"]] == [True, True, False]
    assert c.get(f"/api/movies/{mid}/detail").json()["watched"]["views"] == 3

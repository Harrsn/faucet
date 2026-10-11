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

"""Grab history (why each grab, what became of it) and the blocklist."""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("SESSION_SECRET", "t")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db", "huntlog", "grabhistory", "auth", "auth_routes", "app")}
    mods["db"].init()
    db = mods["db"]

    def at(minutes):
        return (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")

    def hist(ts, event, title, detail=""):
        with db.connect() as c:
            c.execute("INSERT INTO history (ts, event, title, detail) VALUES (?,?,?,?)", (ts, event, title, detail))

    with db.connect() as c:
        fg = c.execute("INSERT INTO series (tmdb_id,title) VALUES (1,'Family Guy')").lastrowid
        mv = c.execute("INSERT INTO movies (tmdb_id,title,year) VALUES (2,'Rags',2012)").lastrowid
    good, dead, manual = ("Family.Guy.S23E04.1080p.WEB.h264-ETHEL", "Family.Guy.S23E05.1080p.WEB.h264-DEAD",
                          "Rags.2012.1080p.WEBRip.x264")
    for rel, sid, kind, ep in ((good, fg, "episode", 4), (dead, fg, "episode", 5)):
        mods["huntlog"].record(kind=kind, series_id=sid, season=23, episode=ep, query="Family Guy",
                               outcome="grabbed", results=14, kept=6,
                               reasons={"wrong_title": 3, "seeders": 5, "blocklisted": 0},
                               pick={"title": rel, "tracker": "EZTV", "seeders": 212, "size": 1_400_000_000})
        db.mark_grabbed(rel)
    hist(at(60), "added", good, "hunt: Family Guy S23E04 (missing)")
    hist(at(50), "completed", good.replace(".", " "), "download finished")      # client spaces the name
    hist(at(49), "sorted", good, "→ /library/tvshows/Family Guy")
    hist(at(40), "added", dead, "hunt: Family Guy S23E05 (missing)")
    hist(at(10), "stalled", dead, "removed after 4h without progress")
    hist(at(5), "added", manual, f"manual grab: Rags ({mv})")
    db.mark_grabbed(manual)
    from fastapi.testclient import TestClient
    c = TestClient(mods["app"].app, follow_redirects=False)
    c.headers["X-Faucet-Auth"] = "2"
    c.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    c.headers["X-CSRF-Token"] = c.cookies.get("faucet_csrf")
    return SimpleNamespace(c=c, fg=fg, good=good, dead=dead, manual=manual, db=db)


def test_grabs_explain_and_follow_up(env):
    g = {x["release"]: x for x in env.c.get("/api/grabs").json()["grabs"]}
    ok = g[env.good]
    assert ok["label"] == "Family Guy S23E04" and ok["link"] == {"kind": "show", "id": env.fg, "season": 23}
    assert ok["why"] == {"found": 14, "kept": 6, "rejected": {"wrong_title": 3, "seeders": 5}}
    assert (ok["tracker"], ok["seeders"], ok["how"], ok["state"]) == ("EZTV", 212, "auto", "done")
    bad = g[env.dead]
    assert bad["state"] == "problem" and bad["event"] == "stalled" and "4h" in bad["detail"]
    assert g[env.manual]["how"] == "manual" and g[env.manual]["state"] == "pending"
    assert list(g) == [env.manual, env.dead, env.good]                       # newest first
    assert [x["release"] for x in env.c.get("/api/grabs", params={"q": "s23e05"}).json()["grabs"]] == [env.dead]


def test_blocklist_and_allow_again(env):
    b = env.c.get("/api/blocklist").json()
    assert b["counts"] == {"all": 3, "problems": 1}
    assert [(x["release"], x["state"]) for x in b["items"]] == [(env.dead, "problem")]
    every = env.c.get("/api/blocklist", params={"filter": "all"}).json()["items"]
    assert {x["release"] for x in every} == {env.good, env.dead, env.manual}
    assert env.db.already_grabbed(env.dead)
    assert env.c.delete(f"/api/blocklist/{b['items'][0]['id']}").json()["release"] == env.dead
    assert not env.db.already_grabbed(env.dead)                               # the hunter may pick it again
    assert env.c.delete("/api/blocklist/99999").status_code == 404
    assert env.c.get("/api/blocklist", params={"filter": "nope"}).status_code == 422


def test_labels_for_grabs_without_a_hunt_log_row():
    from faucet.grabhistory import label_from_release as lab
    assert lab("Impractical.Jokers.S13E02.1080p.WEB") == "Impractical Jokers S13E02"
    assert lab("SpongeBob.SquarePants.S13.1080p.WEB") == "SpongeBob SquarePants S13"
    assert lab("Rags.2012.1080p.WEBRip.x264") == "Rags (2012)"
    assert lab("something") is None

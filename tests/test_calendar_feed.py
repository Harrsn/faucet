"""Private iCal feed: a secret link per user, honours library access,
revocable, valid iCalendar."""
from __future__ import annotations

import importlib
import re
from datetime import date, timedelta
from types import SimpleNamespace

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "media"
    for d in ("tvshows", "movies", "Mamaw/Shows"):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(root))
    monkeypatch.setenv("SESSION_SECRET", "t")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db", "libraries", "access", "icalfeed", "auth", "auth_routes", "app")}
    mods["db"].init()
    ms, _ = mods["libraries"].add("Mamaw's Shows", "tv", str(root / "Mamaw" / "Shows"))
    from fastapi.testclient import TestClient

    def client(user=None, pw=None):
        c = TestClient(mods["app"].app, follow_redirects=False)
        if user:
            assert c.post("/api/auth/login", json={"username": user, "password": pw}).status_code == 200
            c.headers.update({"X-CSRF-Token": c.cookies.get("faucet_csrf")})
        return c

    client().post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    admin = client("admin", "adminpass123")
    admin.post("/api/admin/users", json={"username": "mamaw", "password": "mamawpass1234",
                                         "role": "user", "status": "active"})
    uid = [u["id"] for u in admin.get("/api/admin/users").json()["users"] if u["username"] == "mamaw"][0]
    admin.put(f"/api/admin/users/{uid}/libraries", json={"libraries": [ms]})
    soon = (date.today() + timedelta(days=3)).isoformat()
    with mods["db"].connect() as c:
        fg = c.execute("INSERT INTO series (tmdb_id,title,monitored) VALUES (1,'Family Guy',1)").lastrowid
        k = c.execute("INSERT INTO series (tmdb_id,title,monitored,library_id) VALUES (2,'The Kardashians',1,?)",
                      (ms,)).lastrowid
        c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) VALUES "
                  "(?,23,4,'Peter, Lois; and a very long title that needs folding across lines',?)", (fg, soon))
        c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) VALUES (?,8,2,'E2',?)",
                  (k, soon))
    return SimpleNamespace(m=mods, admin=admin, client=client, uid=uid, db=mods["db"], fg=fg, k=k)


def _token(url):
    return re.search(r"/cal/([^/]+)\.ics$", url).group(1)


def test_feed_is_valid_and_private(env):
    r = env.admin.post("/api/me/calendar").json()
    assert r["url"].startswith("http://testserver/cal/") and r["url"].endswith(".ics")
    anon = env.client()
    feed = anon.get(r["url"].replace("http://testserver", ""))
    assert feed.status_code == 200 and feed.headers["content-type"].startswith("text/calendar")
    body = feed.text
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert body.count("BEGIN:VEVENT") == 2
    assert "SUMMARY:Family Guy · S23E04" in body
    assert "Peter\\, Lois\\; and" in body.replace("\r\n ", "")           # escaped, then unfolded
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    assert "DTSTART;VALUE=DATE:" in body and f"#/shows/{env.fg}/23" in body
    with env.db.connect() as c:                                          # only a hash is stored
        stored = c.execute("SELECT token_hash FROM calendar_feeds").fetchone()[0]
    assert _token(r["url"]) not in stored and len(stored) == 64
    assert env.admin.get("/api/me/calendar").json()["feed"]["last_used"]


def test_feed_honours_library_access(env):
    mamaw = env.client("mamaw", "mamawpass1234")
    url = mamaw.post("/api/me/calendar").json()["url"]
    body = env.client().get(url.replace("http://testserver", "")).text
    assert "The Kardashians" in body and "Family Guy" not in body


def test_regenerate_revoke_and_disable(env):
    first = env.admin.post("/api/me/calendar").json()["url"].replace("http://testserver", "")
    second = env.admin.post("/api/me/calendar").json()["url"].replace("http://testserver", "")
    anon = env.client()
    assert anon.get(first).status_code == 404 and anon.get(second).status_code == 200
    env.admin.delete("/api/me/calendar")
    assert anon.get(second).status_code == 404
    mamaw = env.client("mamaw", "mamawpass1234")
    url = mamaw.post("/api/me/calendar").json()["url"].replace("http://testserver", "")
    env.admin.post(f"/api/admin/users/{env.uid}/status", params={"status": "disabled"})
    assert anon.get(url).status_code == 404
    assert anon.get("/cal/not-a-real-token.ics").status_code == 404
    assert anon.post("/api/me/calendar").status_code in (401, 403)

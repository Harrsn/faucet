"""Alerts fire once when a problem starts, once when it clears, can be
switched off, and catch an unclean shutdown."""
from __future__ import annotations

import importlib
from collections import namedtuple
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "media"
    for d in ("tvshows", "movies"):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(root))
    monkeypatch.setenv("COMPLETE_DIR", str(tmp_path))
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db", "indexerhealth", "libraries", "transfers", "alerts")}
    mods["db"].init()
    sent = []
    monkeypatch.setattr(mods["alerts"], "_send", lambda t, b: sent.append(t))
    monkeypatch.setattr(mods["transfers"], "snapshot",
                        lambda: {"ok": True, "age": 1, "error": None, "items": [],
                                 "as_of": None, "stale": False})
    monkeypatch.setattr("shutil.disk_usage", lambda p: Usage(1000, 500, 500))    # healthy by default
    A = mods["alerts"]
    return SimpleNamespace(A=A, db=mods["db"], sent=sent, mods=mods, monkeypatch=monkeypatch)


def _search(env, indexer, ok, minutes_ago):
    from faucet import indexerhealth
    with env.db.connect() as c:
        indexerhealth._ensure(c)
        ts = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        c.execute("INSERT INTO search_events (ts, indexer, ok, ms, results, error) VALUES (?,?,?,?,?,?)",
                  (ts, indexer, 1 if ok else 0, 100, 0, None if ok else "Cloudflare challenge failed"))


def test_indexer_down_fires_once_then_resolves(env):
    for m in (50, 30, 10):
        _search(env, "1337x", False, m)
    _search(env, "eztv", True, 5)
    _search(env, "eztv", True, 4)
    env.A.check()
    env.A.check()                                             # still down: not re-sent
    assert env.sent == ["⚠ Indexer 1337x is down"]
    assert [a["title"] for a in env.A.active()] == ["Indexer 1337x is down"]
    _search(env, "1337x", True, 1)
    env.A.check()
    assert env.sent[-1] == "✓ Resolved: Indexer 1337x is down" and env.A.active() == []
    _search(env, "solo", False, 1)                            # one failure isn't evidence
    env.A.check()
    assert len(env.sent) == 2


def test_disk_client_and_switching_off(env):
    env.monkeypatch.setattr("shutil.disk_usage", lambda p: Usage(1000, 960, 40))
    env.monkeypatch.setattr(env.mods["transfers"], "snapshot",
                            lambda: {"ok": False, "age": 3600, "error": "Read timed out", "items": [],
                                     "as_of": None, "stale": True})
    env.A.check()
    titles = sorted(env.sent)
    assert any(t.startswith("⚠ Low disk space") for t in titles)
    assert "⚠ Download client unreachable" in titles
    assert len([t for t in titles if "Low disk" in t]) == 1    # one disk, one alert
    env.monkeypatch.setenv("ALERT_DISK", "0")
    env.A.check()
    assert not any("disk" in a["key"] for a in env.A.active())  # dropped quietly
    assert not any(t.startswith("✓ Resolved: Low disk") for t in env.sent)


def test_repeated_stalls_of_one_episode(env):
    for name in ("Family.Guy.S23E04.1080p.WEB.h264-ETHEL", "Family Guy S23E04 720p HDTV x264",
                 "Family.Guy.S23E04.1080p.HEVC.x265-MeGusta"):
        env.db.add_history("stalled", name, "removed")
    env.db.add_history("stalled", "Bobs.Burgers.S15E01.1080p", "removed")
    env.A.check()
    assert env.sent == ["⚠ Family Guy S23E04 keeps stalling"]


def test_unclean_shutdown(env):
    assert env.A.started() is False                           # first start: clean
    assert env.A.started() is True                            # never stopped: unclean
    assert env.sent == ["⚠ Faucet restarted unexpectedly"]
    env.A.stopped()
    assert env.A.started() is False
    env.monkeypatch.setenv("ALERT_CRASH", "0")
    env.A.started()
    assert len(env.sent) == 1


def test_alerts_api_and_home(env, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "t")
    for m in ("auth", "auth_routes", "app"):
        importlib.reload(importlib.import_module("faucet." + m))
    from fastapi.testclient import TestClient

    from faucet import app as appmod
    env.A.apply([("client", True, "Download client unreachable", "No answer")])
    c = TestClient(appmod.app, follow_redirects=False)
    c.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    c.headers.update({"X-CSRF-Token": c.cookies.get("faucet_csrf")})
    assert [a["title"] for a in c.get("/api/alerts").json()["alerts"]] == ["Download client unreachable"]
    assert c.get("/api/home").json()["alerts"][0]["key"] == "client"
    g = c.get("/api/settings/tunables", params={"section": "alerts"}).json()
    assert {i["key"] for grp in g["groups"] for i in grp["items"]} >= {"ALERT_INDEXER", "ALERT_CRASH"}
    c.post("/api/admin/users", json={"username": "bob", "password": "bobpass1234", "role": "user", "status": "active"})
    bob = TestClient(appmod.app, follow_redirects=False)
    bob.post("/api/auth/login", json={"username": "bob", "password": "bobpass1234"})
    assert bob.get("/api/alerts").status_code == 403
    assert "alerts" not in bob.get("/api/home").json()


def test_client_needs_time_before_alerting(env, monkeypatch):
    """Right after a start the client may not have answered yet: don't cry wolf."""
    monkeypatch.setattr(env.mods["transfers"], "snapshot",
                        lambda: {"ok": False, "age": None, "error": "refused", "items": [],
                                 "as_of": None, "stale": True})
    assert env.A.check_client()[0][1] is False
    monkeypatch.setattr(env.A, "_STARTED", env.A.time.monotonic() - 3600)
    assert env.A.check_client()[0][1] is True

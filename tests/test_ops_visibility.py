"""Ops visibility (phase 4a of the 2026-10 review).

V1  hunt log: every hunt attempt is recorded with what the indexer returned
    and why each result was rejected.
V2  ranking: near-dead swarms lose to healthy lower-resolution releases;
    trackers whose grabs keep stalling are ranked down.
V3  indexer health: per-search telemetry and a per-indexer probe.
V4  duplicates: redundant copies are recorded (and no longer re-parsed on
    every scan), reported, and can be retired to _superseded/.
"""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

MB = 1024 * 1024


def mk(path: Path, mb: float = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * int(mb * MB))
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("JACKETT_API_KEY", "k")
    monkeypatch.setenv("HUNT_MAX_PER_RUN", "5")
    monkeypatch.setenv("HUNT_MAX_ACTIVE", "10")
    for k in ("HUNT_UPGRADES", "HUNT_MAX_SEARCHES", "HUNT_HEALTHY_SEEDERS",
              "INDEXER_PROBE_HOURS"):
        monkeypatch.delenv(k, raising=False)
    mods = {}
    for name in ("config", "db"):
        mods[name] = importlib.reload(importlib.import_module(f"faucet.{name}"))
    mods["db"].init()
    for name in ("quality", "search", "profiles", "huntlog", "indexerhealth", "library",
                 "wants", "series", "movies", "scheduler"):
        mods[name] = importlib.reload(importlib.import_module(f"faucet.{name}"))
    monkeypatch.setattr(mods["library"], "MIN_SIZE", 1 * MB)
    return SimpleNamespace(tmp=tmp_path, lib=lib, db=mods["db"], L=mods["library"],
                           S=mods["series"], SCH=mods["scheduler"], H=mods["huntlog"],
                           P=mods["profiles"], IH=mods["indexerhealth"],
                           SE=mods["search"])


def _series(db, title, eps=((1, 1),), profile_id=None):
    with db.connect() as c:
        sid = c.execute("INSERT INTO series (tmdb_id,title,monitored,profile_id) "
                        "VALUES (?,?,1,?)", (abs(hash(title)) % 10**6, title,
                                             profile_id)).lastrowid
        for s, e in eps:
            c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) "
                      "VALUES (?,?,?,?,'2020-01-01')", (sid, s, e, f"E{e}"))
    return sid


def _profile(db, resolutions, min_seeders=0):
    with db.connect() as c:
        return c.execute("INSERT INTO profiles (name,min_seeders,resolutions,sources,language) "
                         "VALUES ('P',?,?,?, 'any')",
                         (min_seeders, json.dumps(resolutions),
                          json.dumps(["WEB-DL", "BluRay", "HDTV"]))).lastrowid


def _rel(title, seeders=50, tracker="good", href=None):
    from faucet.search import parse_badges
    return {"title": title, "href": href or f"magnet:{title}", "seeders": seeders,
            "peers": 1, "size": 1024 ** 3, "badges": parse_badges(title), "tracker": tracker}


class _Added:
    id, name, duplicate = "a", "x", False


class _Client:
    def __init__(self):
        self.added = []

    def list_transfers(self):
        return []

    def add(self, href, *a, **k):
        self.added.append(href)
        return _Added()


@pytest.fixture
def hunter(env, monkeypatch):
    st = SimpleNamespace(client=_Client(), results=[])
    monkeypatch.setattr(env.SCH, "make_client", lambda *a, **k: st.client)
    monkeypatch.setattr(env.SCH.searchmod, "search", lambda *a, **k: list(st.results))
    monkeypatch.setattr(env.SCH, "_try_season_pack", lambda *a: None)
    return st


# ── V1: hunt log ────────────────────────────────────────────────────────────

def test_miss_is_logged_with_rejection_reasons(env, hunter):
    pid = _profile(env.db, ["1080p"], min_seeders=10)
    sid = _series(env.db, "Show", profile_id=pid)
    env.S.reconcile(sid)
    from faucet import db
    db.mark_grabbed("Show.S01E01.1080p.WEB-DL.x264-OLD")
    hunter.results = [
        _rel("Show.S01E01.1080p.WEB-DL.x264-OLD"),            # blocklisted
        _rel("Other.Show.S01E01.1080p.WEB-DL.x264-GRP"),       # wrong title
        _rel("Show.S01E02.1080p.WEB-DL.x264-GRP"),             # wrong episode
        _rel("Show.S01.1080p.WEB-DL.x264-GRP"),                # season pack
        _rel("Show.S01E01.1080p.WEB-DL.x264-THIN", seeders=2),  # profile: seeders
        _rel("Show.S01E01.720p.WEB-DL.x264-LOW"),              # profile: resolution
    ]
    env.SCH.hunt_wanted()
    [e] = env.H.entries(kind="episode", series_id=sid)
    assert e["outcome"] == "miss" and e["results"] == 6 and e["kept"] == 0
    assert e["reasons"] == {"blocklisted": 1, "wrong_title": 2, "pack": 1,
                            "seeders": 1, "resolution": 1}
    assert e["next_search"] and (e["season"], e["episode"]) == (1, 1)


def test_grab_is_logged_with_tracker_and_seeders(env, hunter):
    sid = _series(env.db, "Show")
    env.S.reconcile(sid)
    hunter.results = [_rel("Show.S01E01.1080p.WEB-DL.x264-GRP", seeders=77, tracker="TPB")]
    env.SCH.hunt_wanted()
    [e] = env.H.entries(kind="episode", series_id=sid)
    assert (e["outcome"], e["tracker"], e["seeders"]) == ("grabbed", "TPB", 77)
    assert e["release"] == "Show.S01E01.1080p.WEB-DL.x264-GRP"


def test_search_failure_is_logged(env, monkeypatch):
    sid = _series(env.db, "Show")
    env.S.reconcile(sid)
    monkeypatch.setattr(env.SCH, "make_client", lambda *a, **k: _Client())

    def boom(*a, **k):
        raise env.SCH.searchmod.SearchError("Read timed out")
    monkeypatch.setattr(env.SCH.searchmod, "search", boom)
    env.SCH.hunt_wanted()
    [e] = env.H.entries(kind="episode", series_id=sid)
    assert e["outcome"] == "error" and "timed out" in e["detail"]


def test_log_is_pruned(env, monkeypatch):
    env.H.record(kind="movie", series_id=1, outcome="miss")
    old = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    with env.db.connect() as c:
        c.execute("UPDATE hunt_log SET ts=?", (old,))
    env.H.record(kind="movie", series_id=1, outcome="miss")
    assert env.H.prune() == 1 and len(env.H.entries(kind="movie", series_id=1)) == 1


# ── V2: ranking ─────────────────────────────────────────────────────────────

def test_near_dead_swarm_loses_to_healthy_lower_resolution(env):
    prof = {"resolutions": ["1080p", "720p"], "sources": [], "language": "any",
            "min_seeders": 0}
    dead = _rel("X.S01E01.1080p.WEB-DL", seeders=3)
    alive = _rel("X.S01E01.720p.WEB-DL", seeders=300)
    assert env.P.rank([dead, alive], prof)[0]["title"] == alive["title"]
    healthy = _rel("X.S01E01.1080p.WEB-DL", seeders=40)
    assert env.P.rank([healthy, alive], prof)[0]["title"] == healthy["title"]


def _grab_history(env, tracker, n, stalled):
    for i in range(n):
        rel = f"Rel.{tracker}.{i}"
        env.H.record(kind="episode", series_id=1, season=1, episode=i, outcome="grabbed",
                     pick={"title": rel, "tracker": tracker, "seeders": 9})
        env.db.add_history("stalled" if i < stalled else "completed", rel)


def test_tracker_outcomes_and_penalty(env):
    _grab_history(env, "deadtracker", 4, stalled=4)
    _grab_history(env, "goodtracker", 4, stalled=0)
    out = env.H.tracker_outcomes()
    assert out["deadtracker"]["stall_rate"] == 1.0 and out["goodtracker"]["stall_rate"] == 0.0
    pens = env.SCH._tracker_penalties()
    assert set(pens) == {"deadtracker"}
    prof = {"resolutions": ["1080p", "720p"], "sources": [], "language": "any"}
    ranked = env.SCH._rank_for_hunt(
        [_rel("Y.S01E01.1080p.WEB-DL", 80, "deadtracker"),
         _rel("Y.S01E01.720p.WEB-DL", 80, "goodtracker")], prof, {}, pens)
    assert ranked[0]["tracker"] == "goodtracker"


def test_too_few_settled_grabs_is_no_penalty(env):
    _grab_history(env, "newtracker", 3, stalled=3)
    assert env.SCH._tracker_penalties() == {}


# ── V3: indexer health ──────────────────────────────────────────────────────

def test_search_telemetry(env, monkeypatch):
    xml = (b'<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>'
           b'<item><title>A.S01E01.1080p</title><link>magnet:?xt=1</link>'
           b'<torznab:attr name="tracker" value="TrackerA"/></item>'
           b'<item><title>A.S01E01.720p</title><link>magnet:?xt=2</link>'
           b'<torznab:attr name="tracker" value="TrackerB"/></item></channel></rss>')

    class R:
        content = xml
        def raise_for_status(self): pass
    monkeypatch.setattr(env.SE.requests, "get", lambda *a, **k: R())
    env.SE.search("http://j", "key123456", "all", "a", "all", 10)

    def timeout(*a, **k):
        raise env.SE.requests.ReadTimeout("Read timed out. (read timeout=30)")
    monkeypatch.setattr(env.SE.requests, "get", timeout)
    with pytest.raises(env.SE.SearchError):
        env.SE.search("http://j", "key123456", "all", "a", "all", 10)
    s = env.IH.summary()
    assert (s["searches"], s["failed"]) == (2, 1)
    assert s["errors"] == {"timeout": 1}
    assert s["trackers"] == {"TrackerA": 1, "TrackerB": 1}


def test_probe_finds_the_failing_indexer(env, monkeypatch):
    from faucet import config as cfgmod
    monkeypatch.setattr(cfgmod.config, "jackett_url", "http://j")
    monkeypatch.setattr(cfgmod.config, "jackett_api_key", "key123456")
    monkeypatch.setattr(env.SE, "indexers", lambda *a, **k: [
        {"id": "fast", "name": "Fast"}, {"id": "hung", "name": "Hung"}])

    def search(url, key, ix, *a, **k):
        assert k.get("record") is False                 # probes don't pollute telemetry
        if ix == "hung":
            raise env.SE.SearchError("Read timed out")
        return [1, 2, 3]
    monkeypatch.setattr(env.SE, "search", search)
    out = env.IH.probe()
    assert [(x["id"], x["ok"]) for x in out["indexers"]] == [("hung", False), ("fast", True)]
    assert env.IH.last_probe()["indexers"][1]["results"] == 3
    assert env.IH.probe_if_due() is None                  # just ran
    monkeypatch.setenv("INDEXER_PROBE_HOURS", "0")
    with env.db.connect() as c:
        c.execute("DELETE FROM settings WHERE key=?", (env.IH.PROBE_KEY,))
    assert env.IH.probe_if_due() is None                  # disabled


# ── V4: duplicates ──────────────────────────────────────────────────────────

def _two_copies(env):
    a = mk(env.lib / "tvshows" / "Show" / "Season 01" / "Show - S01E01.720p.mkv")
    b = mk(env.lib / "tvshows" / "Show" / "Season 1" / "Show.S01E01.720p.HDTV.mkv")
    (b.parent / (b.stem + ".en.srt")).write_text("sub")
    return a, b


def test_duplicate_is_recorded_and_not_reparsed(env, monkeypatch):
    _two_copies(env)
    env.L.scan()
    d = env.L.duplicates()
    assert d["count"] == 1 and d["bytes"] == 2 * MB
    calls = []
    real = env.L.guessit
    monkeypatch.setattr(env.L, "guessit", lambda n: calls.append(n) or real(n))
    stats = env.L.scan()
    assert calls == [] and stats["skipped"] == 2


def test_retire_moves_duplicate_and_sidecars(env):
    a, b = _two_copies(env)
    env.L.scan()
    loser = Path(env.L.duplicates()["items"][0]["path"])
    winner = Path(env.L.duplicates()["items"][0]["winner_path"])
    out = env.L.retire_duplicates()
    assert out["moved"] == 1 and not loser.exists() and winner.exists()
    parked = env.lib / "_superseded" / loser.relative_to(env.lib)
    assert parked.exists()
    assert (parked.parent / (loser.stem + ".en.srt")).exists() or \
        not (loser.parent / (loser.stem + ".en.srt")).exists()
    assert env.L.duplicates()["count"] == 0
    assert (env.lib / "_superseded" / ".plexignore").exists()


def test_retire_only_touches_recorded_duplicates(env):
    a, b = _two_copies(env)
    env.L.scan()
    out = env.L.retire_duplicates([str(a), str(b), "/etc/passwd"])
    moved_or_kept = {p.exists() for p in (a, b)}
    assert out["moved"] == 1 and moved_or_kept == {True, False}
    assert {s["why"] for s in out["skipped"]} == {"not a recorded duplicate"}


def test_retire_refuses_when_the_better_copy_is_gone(env):
    _two_copies(env)
    env.L.scan()
    item = env.L.duplicates()["items"][0]
    Path(item["winner_path"]).unlink()
    out = env.L.retire_duplicates()
    assert out["moved"] == 0 and Path(item["path"]).exists()


def test_duplicate_takes_over_when_the_winner_disappears(env):
    _two_copies(env)
    env.L.scan()
    item = env.L.duplicates()["items"][0]
    Path(item["winner_path"]).unlink()
    env.L.scan()          # prunes the stale winner + dupe record
    env.L.scan()          # re-judges the former duplicate
    assert env.L.have_episode("Show", 1, 1)["path"] == item["path"]
    assert env.L.duplicates()["count"] == 0


def test_multi_episode_file_winning_one_episode_is_not_a_duplicate(env):
    mk(env.lib / "tvshows" / "Show" / "Season 01" / "Show.S01E01.1080p.mkv")
    mk(env.lib / "tvshows" / "Show" / "Season 01" / "Show.S01E01E02.720p.mkv")
    env.L.scan()
    assert env.L.duplicates()["count"] == 0
    assert env.L.have_episode("Show", 1, 2)["path"].endswith("S01E01E02.720p.mkv")


# ── API surface ─────────────────────────────────────────────────────────────

def test_ops_endpoints_are_admin_only(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("SESSION_SECRET", "t")
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "lib"))
    for m in ("config", "db", "auth", "auth_routes", "app"):
        importlib.reload(importlib.import_module("faucet." + m))
    from fastapi.testclient import TestClient

    from faucet import app as appmod
    from faucet import db
    db.init()
    admin = TestClient(appmod.app, follow_redirects=False)
    admin.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    admin.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    admin.headers.update({"X-CSRF-Token": admin.cookies.get("faucet_csrf")})
    admin.post("/api/admin/users", json={"username": "bob", "password": "bobpass1234",
                                         "role": "user", "status": "active"})
    bob = TestClient(appmod.app, follow_redirects=False)
    bob.post("/api/auth/login", json={"username": "bob", "password": "bobpass1234"})
    for path in ("/api/wanted/log?kind=episode&series_id=1", "/api/indexers/health",
                 "/api/library/duplicates"):
        assert admin.get(path).status_code == 200, path
        assert bob.get(path).status_code == 403, path
    r = admin.get("/api/wanted/log?kind=episode&series_id=1").json()
    assert r == {"entries": [], "wants": []}
    assert admin.post("/api/library/duplicates/retire", json={}).json()["moved"] == 0

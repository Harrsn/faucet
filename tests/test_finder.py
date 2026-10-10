"""Phase 5a: per-indexer parallel, structured Torznab search."""
from __future__ import annotations

import importlib
import threading
import time
from types import SimpleNamespace

import pytest

CAPS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<caps><server title="Jackett"/><searching>
  <search available="yes" supportedParams="q"/>
  <tv-search available="yes" supportedParams="q,season,ep,tvdbid"/>
  <movie-search available="no" supportedParams="q,imdbid"/>
</searching></caps>"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("JACKETT_API_KEY", "key123456")
    monkeypatch.setenv("JACKETT_URL", "http://jackett:9117")
    monkeypatch.setenv("JACKETT_INDEXER", "all")
    for k in ("SEARCH_TIMEOUT", "SEARCH_CONCURRENCY"):
        monkeypatch.delenv(k, raising=False)
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db")}
    mods["db"].init()
    for m in ("search", "tmdb", "finder"):
        mods[m] = importlib.reload(importlib.import_module("faucet." + m))
    return SimpleNamespace(**mods)


def _r(title, seeders=10, href=None, tracker="t"):
    return {"title": title, "href": href or f"magnet:?xt=urn:btih:{abs(hash(title)):040d}",
            "seeders": seeders, "size": 1, "tracker": tracker, "badges": {}}


# ── planning ────────────────────────────────────────────────────────────────

def test_caps_parsing(env, monkeypatch):
    class R:
        status_code, content = 200, CAPS_XML
    monkeypatch.setattr(env.search.requests, "get", lambda *a, **k: R())
    caps = env.search.caps("http://j", "key123456", "eztv")
    assert caps == {"search": {"q"}, "tv-search": {"q", "season", "ep", "tvdbid"}}


def test_plan_prefers_ids_then_structured_then_text(env):
    P = env.finder.plan
    tv = {"search": {"q"}, "tv-search": {"q", "season", "ep", "tvdbid"}}
    assert P(tv, "episode", "Show", 1, 2, ids={"tvdb_id": 99}) == \
        ("tvsearch", "", {"tvdbid": 99, "season": 1, "ep": 2}, "tv")
    assert P(tv, "episode", "Show", 1, 2) == \
        ("tvsearch", "Show", {"season": 1, "ep": 2}, "tv")
    assert P(tv, "season", "Show", 3) == ("tvsearch", "Show", {"season": 3}, "tv")
    text_only = {"search": {"q"}}
    assert P(text_only, "episode", "Show", 1, 2) == ("search", "Show S01E02", {}, "tv")
    movies = {"search": {"q"}, "movie-search": {"q", "imdbid"}}
    assert P(movies, "movie", "Dune", year=2021, ids={"imdb_id": "tt1160419"}) == \
        ("movie", "", {"imdbid": "tt1160419"}, "movies")
    assert P(movies, "movie", "Dune", year=2021) == ("movie", "Dune 2021", {}, "movies")


def test_indexer_list_from_setting(env, monkeypatch):
    monkeypatch.setattr(env.config.config, "jackett_indexer", "eztv, 1337x")
    assert env.finder.indexer_ids() == ["eztv", "1337x"]


def test_indexer_list_is_cached(env, monkeypatch):
    calls = []
    monkeypatch.setattr(env.search, "indexers", lambda *a, **k: calls.append(1) or [
        {"id": "eztv", "name": "EZTV"}, {"id": "tpb", "name": "TPB"}])
    assert env.finder.indexer_ids() == ["eztv", "tpb"]
    assert env.finder.indexer_ids() == ["eztv", "tpb"] and len(calls) == 1
    env.finder.forget_indexers()
    env.finder.indexer_ids()
    assert len(calls) == 2


# ── the search ──────────────────────────────────────────────────────────────

def _wire(env, monkeypatch, ixs, behave):
    monkeypatch.setattr(env.finder, "indexer_ids", lambda: list(ixs))
    monkeypatch.setattr(env.finder, "indexer_caps",
                        lambda ix: {"search": {"q"}, "tv-search": {"q", "season", "ep"}})
    calls = []
    lock = threading.Lock()

    def search(url, key, ix, q, cat, limit, timeout, record=True, mode="search", params=None):
        with lock:
            calls.append((ix, q, mode, dict(params or {}), cat))
        return behave(ix)
    monkeypatch.setattr(env.search, "search", search)
    return calls


def test_queries_every_indexer_in_parallel(env, monkeypatch):
    def behave(ix):
        time.sleep(0.3)
        return env.search.Results([_r(f"Show.S01E02.{ix}")])
    calls = _wire(env, monkeypatch, ["a", "b", "c", "d"], behave)
    t0 = time.monotonic()
    out = env.finder.find("episode", title="Show", season=1, episode=2)
    assert time.monotonic() - t0 < 0.8                  # not 4 × 0.3s
    assert {c[0] for c in calls} == {"a", "b", "c", "d"}
    assert all(c[2] == "tvsearch" and c[3] == {"season": 1, "ep": 2} and c[4] == "tv"
               for c in calls)
    assert len(out) == 4 and out.searched == 4 and out.errors == []


def test_one_failing_indexer_costs_only_its_results(env, monkeypatch):
    def behave(ix):
        if ix == "cloudflared":
            raise env.search.SearchError("Read timed out")
        return env.search.Results([_r(f"Show.S01E02.{ix}")])
    _wire(env, monkeypatch, ["ok1", "cloudflared", "ok2"], behave)
    out = env.finder.find("episode", title="Show", season=1, episode=2)
    assert len(out) == 2 and out.errors == [("cloudflared", "Read timed out")]


def test_all_failing_raises(env, monkeypatch):
    def behave(ix):
        raise env.search.SearchError("down")
    _wire(env, monkeypatch, ["a", "b"], behave)
    with pytest.raises(env.search.SearchError, match="all 2 indexers failed"):
        env.finder.find("episode", title="Show", season=1, episode=2)


def test_duplicates_across_indexers_merge_keeping_best_seeded(env, monkeypatch):
    same = "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01"

    def behave(ix):
        return env.search.Results([_r("Show.S01E02.1080p", seeders={"a": 5, "b": 90}[ix],
                                      href=same, tracker=ix)])
    _wire(env, monkeypatch, ["a", "b"], behave)
    out = env.finder.find("episode", title="Show", season=1, episode=2)
    assert len(out) == 1 and out[0]["seeders"] == 90 and out[0]["tracker"] == "b"


def test_falls_back_to_aggregate_when_indexers_unlisted(env, monkeypatch):
    monkeypatch.setattr(env.finder, "indexer_ids", lambda: [])
    seen = []
    monkeypatch.setattr(env.search, "search",
                        lambda url, key, ix, q, *a, **k: seen.append((ix, q)) or [])
    env.finder.find("episode", title="Show", season=1, episode=2)
    env.finder.find("movie", title="Dune", year=2021)
    assert seen == [("all", "Show S01E02"), ("all", "Dune 2021")]


def test_external_ids_cached(env, monkeypatch):
    calls = []
    monkeypatch.setattr(env.tmdb, "enabled", lambda: True)
    monkeypatch.setattr(env.tmdb, "_get", lambda path, params: calls.append(path) or {
        "imdb_id": "tt0110", "tvdb_id": 42, "facebook_id": "x"})
    assert env.tmdb.external_ids(5, "tv") == {"imdb_id": "tt0110", "tvdb_id": 42}
    assert env.tmdb.external_ids(5, "tv") == {"imdb_id": "tt0110", "tvdb_id": 42}
    assert calls == ["/tv/5/external_ids"]


def test_ids_for_a_monitored_series(env, monkeypatch):
    with env.db.connect() as c:
        sid = c.execute("INSERT INTO series (tmdb_id,title) VALUES (1419,'Show')").lastrowid
    monkeypatch.setattr(env.tmdb, "external_ids", lambda tid, kind: {"tvdb_id": 77})
    assert env.finder.ids_for("episode", sid) == {"tmdb_id": 1419, "tvdb_id": 77}
    assert env.finder.ids_for("movie", 999) == {}


# ── the hunter uses it ──────────────────────────────────────────────────────

def test_hunter_sends_structured_episode_search(env, monkeypatch, tmp_path):
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "lib"))
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("huntlog", "library", "wants", "series", "scheduler")}
    S, SCH = mods["series"], mods["scheduler"]
    with env.db.connect() as c:
        sid = c.execute("INSERT INTO series (tmdb_id,title,monitored) VALUES (1,'Show',1)").lastrowid
        c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) "
                  "VALUES (?,2,5,'E','2020-01-01')", (sid,))
    S.reconcile(sid)

    class Client:
        def list_transfers(self):
            return []

    monkeypatch.setattr(SCH, "make_client", lambda *a, **k: Client())
    calls = _wire(env, monkeypatch, ["eztv", "tpb"], lambda ix: env.search.Results([]))
    monkeypatch.setattr(env.finder, "ids_for", lambda kind, i: {"tmdb_id": 1})
    SCH.hunt_wanted()
    assert sorted(c[0] for c in calls) == ["eztv", "tpb"]
    assert all(c[2] == "tvsearch" and c[1] == "Show" and c[3] == {"season": 2, "ep": 5}
               for c in calls)

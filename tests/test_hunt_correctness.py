"""Hunt correctness regressions (phase 2 of the 2026-10 review).

R1: a show's episode list was pulled from TMDb once, when it was added, so
    new seasons, new episodes and newly-dated TBA episodes never became wants.
R2: ignoring a title (monitored=0) left its wants behind and the hunter kept
    downloading it.
R3: every want was searched every tick — hundreds of indexer queries per
    cycle for a backlog that has no releases.
R4: the scheduler tick and the "check now" button could hunt concurrently.
R5: a multi-episode file only counted as its first episode.
R6: 'grabbed' wants reconcile never revisits stayed grabbed forever.
"""
from __future__ import annotations

import importlib
import json
import threading
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
    for k in ("HUNT_UPGRADES", "HUNT_MAX_SEARCHES", "SERIES_REFRESH_HOURS",
              "SERIES_REFRESH_PER_TICK", "AIR_DELAY_DAYS"):
        monkeypatch.delenv(k, raising=False)
    mods = {}
    for name in ("config", "db"):
        mods[name] = importlib.reload(importlib.import_module(f"faucet.{name}"))
    mods["db"].init()
    for name in ("quality", "tmdb", "library", "wants", "series", "movies",
                 "fixmatch", "scheduler"):
        mods[name] = importlib.reload(importlib.import_module(f"faucet.{name}"))
    monkeypatch.setattr(mods["library"], "MIN_SIZE", 1 * MB)
    return SimpleNamespace(tmp=tmp_path, lib=lib, db=mods["db"], T=mods["tmdb"],
                           L=mods["library"], W=mods["wants"], S=mods["series"],
                           M=mods["movies"], F=mods["fixmatch"], SCH=mods["scheduler"])


def _series(db, title, eps=((1, 1),), air="2020-01-01", **cols):
    fields = {"tmdb_id": abs(hash(title)) % 10**6, "title": title, "monitored": 1,
              "total_seasons": 1, **cols}
    with db.connect() as c:
        sid = c.execute(
            f"INSERT INTO series ({','.join(fields)}) VALUES ({','.join('?' * len(fields))})",
            tuple(fields.values())).lastrowid
        for s, e in eps:
            c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) "
                      "VALUES (?,?,?,?,?)", (sid, s, e, f"E{e}", air))
    return sid


def _wants(db, **where):
    q = " AND ".join(f"{k}=?" for k in where) or "1=1"
    with db.connect() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM wanted WHERE {q}",
                                           tuple(where.values())).fetchall()]


class _Added:
    id = "a"
    name = "x"
    duplicate = False


class _Client:
    def __init__(self):
        self.added = []

    def list_transfers(self):
        return []

    def add(self, href, *a, **k):
        self.added.append(href)
        return _Added()


def _release(title, href):
    from faucet.search import parse_badges
    return {"title": title, "href": href, "seeders": 50, "size": 2 * 1024 ** 3,
            "badges": parse_badges(title)}


@pytest.fixture
def hunter(env, monkeypatch):
    """Fake client + a counting fake indexer whose results tests can swap."""
    client = _Client()
    state = SimpleNamespace(client=client, queries=[], results=[])

    def search(*a, **k):
        state.queries.append(a[3])
        return list(state.results)

    monkeypatch.setattr(env.SCH, "make_client", lambda *a, **k: client)
    monkeypatch.setattr(env.SCH.searchmod, "search", search)
    return state


# ── R1: scheduled episode-list refresh ───────────────────────────────────────

class _FakeTMDb:
    def __init__(self, seasons, status="Returning Series"):
        self.seasons = seasons          # {season_no: [(ep, air_date)] or None (fails)}
        self.status = status
        self.calls = []

    def install(self, T, monkeypatch):
        monkeypatch.setattr(T, "enabled", lambda: True)
        monkeypatch.setattr(T, "details", self.details)
        monkeypatch.setattr(T, "season_episodes", self.season_episodes)

    def details(self, tmdb_id, media_type, fresh=False):
        return {"seasons": max(self.seasons), "status": self.status}

    def season_episodes(self, tmdb_id, season_no, fresh=False):
        self.calls.append((season_no, fresh))
        eps = self.seasons.get(season_no)
        if eps is None:
            return None
        return [{"season": season_no, "episode": e, "title": f"E{e}", "air_date": d}
                for e, d in eps]


def test_refresh_picks_up_a_new_season_and_it_gets_hunted(env, monkeypatch):
    sid = _series(env.db, "Show", eps=((1, 1),), last_refresh="2026-01-01T00:00:00")
    fake = _FakeTMDb({1: [(1, "2020-01-01")], 2: [(1, "2021-01-01"), (2, "2021-01-08")]})
    fake.install(env.T, monkeypatch)

    assert env.S.refresh_stale()["refreshed"] == 1
    assert env.S.get_series(sid)["total_seasons"] == 2
    assert (2, True) in fake.calls                     # fresh read, not cache
    env.S.reconcile(sid)
    assert {(w["season"], w["episode"]) for w in _wants(env.db)} == {(1, 1), (2, 1), (2, 2)}


def test_refresh_fills_in_a_tba_air_date(env, monkeypatch):
    sid = _series(env.db, "Show", eps=((1, 1),), air="")
    env.S.reconcile(sid)
    assert _wants(env.db) == []                         # undated: not hunted
    _FakeTMDb({1: [(1, "2020-05-05")]}).install(env.T, monkeypatch)
    env.S.refresh_stale()
    env.S.reconcile(sid)
    assert len(_wants(env.db, season=1, episode=1)) == 1


def test_refresh_drops_episodes_tmdb_no_longer_lists(env, monkeypatch):
    sid = _series(env.db, "Show", eps=((1, 1), (1, 2), (1, 99)))
    env.S.reconcile(sid)
    assert len(_wants(env.db)) == 3
    _FakeTMDb({1: [(1, "2020-01-01"), (2, "2020-01-01")]}).install(env.T, monkeypatch)
    env.S.refresh_stale()
    with env.db.connect() as c:
        eps = {r["episode"] for r in c.execute(
            "SELECT episode FROM series_episodes WHERE series_id=?", (sid,))}
    assert eps == {1, 2}
    assert {w["episode"] for w in _wants(env.db)} == {1, 2}


def test_failed_season_read_prunes_nothing_and_retries(env, monkeypatch):
    sid = _series(env.db, "Show", eps=((1, 1), (2, 1)), total_seasons=2,
                  last_refresh="2026-01-01T00:00:00")
    _FakeTMDb({1: [(1, "2020-01-01")], 2: None}).install(env.T, monkeypatch)
    env.S.refresh_stale()
    s = env.S.get_series(sid)
    # not recorded as a full refresh: due again in ~1h, not in 12h
    due_at = datetime.fromisoformat(s["last_refresh"]) + timedelta(hours=12)
    assert timedelta(minutes=50) < due_at - datetime.now() < timedelta(minutes=70)
    assert env.S.refresh_stale()["refreshed"] == 0      # and not on every tick
    with env.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM series_episodes WHERE series_id=? "
                         "AND season=2", (sid,)).fetchone()[0] == 1


def test_empty_season_answer_never_prunes(env, monkeypatch):
    sid = _series(env.db, "Show", eps=((1, 1), (1, 2)))
    _FakeTMDb({1: []}).install(env.T, monkeypatch)
    env.S.refresh_stale()
    with env.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM series_episodes WHERE series_id=?",
                         (sid,)).fetchone()[0] == 2


def test_refresh_cadence_and_per_tick_cap(env, monkeypatch):
    recent = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    two_days = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
    _series(env.db, "Fresh", last_refresh=recent)
    _series(env.db, "Ended", last_refresh=two_days)
    for i in range(4):
        _series(env.db, f"Stale{i}", last_refresh=None)
    fake = _FakeTMDb({1: [(1, "2020-01-01")]})
    fake.install(env.T, monkeypatch)
    monkeypatch.setattr(env.T, "details", lambda tid, mt, fresh=False: {
        "seasons": 1,
        "status": "Ended" if tid == abs(hash("Ended")) % 10**6 else "Returning Series"})
    monkeypatch.setenv("SERIES_REFRESH_PER_TICK", "3")
    assert env.S.refresh_stale()["refreshed"] == 3
    assert env.S.refresh_stale()["refreshed"] == 1      # the 4th stale show
    assert env.S.refresh_stale()["refreshed"] == 0      # Fresh: <12h; Ended: <7d


def test_paused_show_is_not_refreshed(env, monkeypatch):
    _series(env.db, "Paused", monitor_mode="none")
    fake = _FakeTMDb({1: [(1, "2020-01-01")]})
    fake.install(env.T, monkeypatch)
    assert env.S.refresh_stale()["refreshed"] == 0


# ── R2: unmonitored titles are never hunted ──────────────────────────────────

def test_ignoring_a_show_drops_its_wants(env):
    sid = _series(env.db, "Show", eps=((1, 1), (1, 2)))
    env.S.reconcile(sid)
    assert len(_wants(env.db)) == 2
    assert env.F.set_status("show", sid, "ignored")["ok"]
    assert _wants(env.db) == []


def test_ignoring_a_movie_drops_its_want(env):
    mid = env.M.add_movie(5, "Gone", 2001, None)
    assert len(_wants(env.db, kind="movie")) == 1
    env.F.set_status("movie", mid, "ignored")
    assert _wants(env.db, kind="movie") == []


def test_hunter_skips_leftover_wants_of_unmonitored_titles(env, hunter):
    sid = _series(env.db, "Show")
    env.S.reconcile(sid)
    with env.db.connect() as c:                         # an old DB's leftover state
        c.execute("UPDATE series SET monitored=0 WHERE id=?", (sid,))
    hunter.results = [_release("Show.S01E01.1080p.WEB-DL", "magnet:x")]
    env.SCH.hunt_wanted()
    assert hunter.queries == [] and hunter.client.added == []
    env.S.reconcile_all()                               # and the next reconcile clears them
    assert _wants(env.db) == []


# ── R3: backoff and search budget ────────────────────────────────────────────

def test_fruitless_search_backs_off(env, hunter):
    sid = _series(env.db, "Show")
    env.S.reconcile(sid)
    env.SCH.hunt_wanted()
    assert len(hunter.queries) == 1
    w = _wants(env.db)[0]
    assert w["search_count"] == 1 and w["next_search"] > datetime.now().isoformat()
    env.SCH.hunt_wanted()
    assert len(hunter.queries) == 1                     # not due yet


def test_backoff_doubles_and_caps(env):
    assert [env.W.backoff_hours(n, recent=False) for n in (1, 2, 3, 4)] == [1, 2, 4, 8]
    assert env.W.backoff_hours(30, recent=False) == 72
    assert env.W.backoff_hours(30, recent=True) == 6


def test_reconcile_keeps_the_backoff(env, hunter):
    sid = _series(env.db, "Show")
    env.S.reconcile(sid)
    env.SCH.hunt_wanted()
    before = _wants(env.db)[0]
    env.S.reconcile(sid)
    after = _wants(env.db)[0]
    assert (after["search_count"], after["next_search"]) == (
        before["search_count"], before["next_search"])


def test_search_error_does_not_back_off(env, monkeypatch):
    sid = _series(env.db, "Show")
    env.S.reconcile(sid)
    monkeypatch.setattr(env.SCH, "make_client", lambda *a, **k: _Client())

    def boom(*a, **k):
        raise env.SCH.searchmod.SearchError("Read timed out")
    monkeypatch.setattr(env.SCH.searchmod, "search", boom)
    env.SCH.hunt_wanted()
    w = _wants(env.db)[0]
    assert (w["search_count"] or 0) == 0 and w["next_search"] is None


def test_search_budget_per_cycle(env, hunter, monkeypatch):
    monkeypatch.setenv("HUNT_MAX_SEARCHES", "3")
    sid = _series(env.db, "Show", eps=[(1, e) for e in range(1, 9)])
    env.S.reconcile(sid)
    monkeypatch.setattr(env.SCH, "_try_season_pack", lambda *a: None)
    r = env.SCH.hunt_wanted()
    # the season-pack attempt spends one of the three searches
    assert r["searches"] == 3 and len(hunter.queries) == 2 and r["deferred"] == 6


def test_newest_episodes_are_searched_first(env, hunter, monkeypatch):
    monkeypatch.setenv("HUNT_MAX_SEARCHES", "1")
    sid = _series(env.db, "Show", eps=((1, 1),), air="2019-01-01")
    with env.db.connect() as c:
        c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) "
                  "VALUES (?,2,1,'new','2024-01-01')", (sid,))
    env.S.reconcile(sid)
    env.SCH.hunt_wanted()
    assert hunter.queries == ["Show S02E01"]


def test_movies_are_not_starved_by_an_episode_backlog(env, hunter, monkeypatch):
    monkeypatch.setenv("HUNT_MAX_SEARCHES", "1")
    sid = _series(env.db, "Show", eps=((1, 1), (2, 1)))   # no season is pack-worthy
    env.S.reconcile(sid)
    env.M.add_movie(7, "Film", 2020, None)
    env.SCH.hunt_wanted()
    assert hunter.queries == ["Film 2020"]


def test_show_hunt_never_touches_a_movie_with_the_same_id(env, hunter):
    sid = _series(env.db, "Show")
    mid = env.M.add_movie(7, "Film", 2020, None)
    assert sid == mid                                    # ids collide across tables
    env.S.hunt_series(sid)
    assert hunter.queries == ["Show S01E01"]


def test_manual_show_hunt_ignores_backoff(env, hunter):
    sid = _series(env.db, "Show")
    env.S.reconcile(sid)
    env.SCH.hunt_wanted()
    hunter.results = [_release("Show.S01E01.1080p.WEB-DL.x264-GRP", "magnet:x")]
    env.S.hunt_series(sid)
    assert hunter.client.added == ["magnet:x"]


def test_requeued_grab_is_due_immediately(env, hunter):
    sid = _series(env.db, "Show")
    env.S.reconcile(sid)
    env.SCH.hunt_wanted()                                # miss -> backed off
    hunter.results = [_release("Show.S01E01.1080p.WEB-DL.x264-GRP", "magnet:x")]
    env.S.hunt_series(sid)                               # grabbed
    w = _wants(env.db)[0]
    assert w["status"] == "grabbed" and w["next_search"] is None
    old = (datetime.now() - timedelta(hours=100)).isoformat(timespec="seconds")
    with env.db.connect() as c:
        c.execute("UPDATE wanted SET last_search=?", (old,))
    assert env.W.requeue_stale_grabs(48) == 1
    hunter.results = [_release("Show.S01E01.720p.HDTV.x264-OTH", "magnet:y")]
    env.SCH.hunt_wanted()
    assert hunter.client.added == ["magnet:x", "magnet:y"]


# ── R4: no concurrent hunts / cycles ─────────────────────────────────────────

def test_second_hunt_is_refused_while_one_runs(env, monkeypatch):
    sid = _series(env.db, "Show")
    env.S.reconcile(sid)
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(env.SCH, "make_client", lambda *a, **k: _Client())

    def slow_search(*a, **k):
        entered.set()
        release.wait(5)
        return []
    monkeypatch.setattr(env.SCH.searchmod, "search", slow_search)
    t = threading.Thread(target=env.SCH.hunt_wanted)
    t.start()
    assert entered.wait(5)
    assert env.SCH.hunt_wanted()["skipped_reason"] == "a hunt is already running"
    release.set()
    t.join(5)
    assert not env.SCH._hunt_lock.locked()


def test_second_cycle_is_refused_while_one_runs(env, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(env.SCH, "_run_once", lambda: gate.wait(5) or {})
    t = threading.Thread(target=env.SCH.run_once)
    t.start()
    try:
        for _ in range(100):
            if env.SCH._run_lock.locked():
                break
            threading.Event().wait(0.01)
        assert env.SCH.run_once()["skipped_reason"] == "a cycle is already running"
    finally:
        gate.set()
        t.join(5)


# ── R5: multi-episode files ──────────────────────────────────────────────────

def test_multi_episode_file_owns_every_episode(env):
    mk(env.lib / "tvshows" / "Show" / "Season 01" / "Show - S01E01E02.mkv")
    mk(env.lib / "tvshows" / "Show" / "Season 01" / "Show.S01E03-E04.720p.mkv")
    env.L.scan()
    sid = _series(env.db, "Show", eps=[(1, e) for e in range(1, 6)])
    r = env.S.reconcile(sid)
    assert r["have"] == 4 and r["missing"] == 1
    assert [w["episode"] for w in _wants(env.db)] == [5]
    env.L.scan()                                         # stable on rescan
    assert env.S.reconcile(sid)["have"] == 4


def test_multi_episode_file_is_never_retired_by_a_single(env):
    multi = mk(env.lib / "tvshows" / "Show" / "Season 01" / "Show.S01E01E02.480p.mkv")
    env.L.scan()
    single = mk(env.lib / "tvshows" / "Show" / "Season 01" / "Show.S01E01.1080p.mkv")
    with env.db.connect() as c:                          # sorter-placed, so retirement is allowed
        c.execute("INSERT INTO library_files (path, quality, size) VALUES (?,?,?)",
                  (str(single), "1080p", single.stat().st_size))
    stats = env.L.scan()
    assert multi.exists() and not stats.get("superseded_moved")
    assert env.L.have_episode("Show", 1, 2)["path"] == str(multi)
    assert env.L.have_episode("Show", 1, 1)["path"] == str(single)


# ── R6: stale grabs ─────────────────────────────────────────────────────────

def test_stale_grab_requeued_even_when_reconcile_skips_it(env):
    sid = _series(env.db, "Show", monitor_mode="future", added_ts="2025-01-01T00:00:00")
    old = (datetime.now() - timedelta(hours=100)).isoformat(timespec="seconds")
    fresh = datetime.now().isoformat(timespec="seconds")
    with env.db.connect() as c:
        c.execute("INSERT INTO wanted (kind,series_id,season,episode,status,last_search,"
                  "search_count,next_search) VALUES ('episode',?,1,1,'grabbed',?,3,'2999-01-01')",
                  (sid, old))
        c.execute("INSERT INTO wanted (kind,series_id,season,episode,status,last_search) "
                  "VALUES ('episode',?,1,2,'grabbed',?)", (sid, fresh))
    assert env.W.requeue_stale_grabs(48) == 1
    stale = _wants(env.db, episode=1)[0]
    assert stale["status"] == "wanted" and stale["next_search"] is None
    assert _wants(env.db, episode=2)[0]["status"] == "grabbed"


def test_migration_adds_backoff_columns_to_an_old_db(env):
    with env.db.connect() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(wanted)")}
    assert {"search_count", "next_search"} <= cols


def test_refresh_profile_json_roundtrip_unaffected(env):
    # guard: the scheduled refresh must not touch profile assignment
    with env.db.connect() as c:
        pid = c.execute("INSERT INTO profiles (name,resolutions,sources) VALUES "
                        "('P',?,?)", (json.dumps(["1080p"]), "[]")).lastrowid
    sid = _series(env.db, "Show", profile_id=pid)
    env.S.refresh_episodes(sid, fresh=False)
    assert env.S.get_series(sid)["profile_id"] == pid


# ── long-missing episodes: relax the seeder minimum ─────────────────────────

def _profiled_show(env, title, air="2010-05-01", min_seeders=5):
    with env.db.connect() as c:
        pid = c.execute("INSERT INTO profiles (name, min_seeders, resolutions, sources) "
                        "VALUES ('Strict', ?, '[]', '[]')", (min_seeders,)).lastrowid
    sid = _series(env.db, title, air=air, profile_id=pid)
    env.S.reconcile(sid)
    return sid


def _searched(env, n):
    with env.db.connect() as c:
        c.execute("UPDATE wanted SET search_count=?, next_search=NULL", (n,))


def _thin(title):
    r = _release(title, "magnet:thin")
    r["seeders"] = 2
    return r


def test_long_missing_episode_takes_a_thinly_seeded_release(env, hunter):
    _profiled_show(env, "American Dad")
    hunter.results = [_thin("American.Dad.S01E01.720p.WEB.x264")]
    _searched(env, 1)
    env.SCH.hunt_wanted()
    assert hunter.client.added == []                    # profile: 2 < 5 seeders
    _searched(env, 4)                                   # searched enough, aired long ago
    env.SCH.hunt_wanted()
    assert hunter.client.added == ["magnet:thin"]
    with env.db.connect() as c:
        note = c.execute("SELECT detail FROM history WHERE event='added'").fetchone()[0]
        hl = c.execute("SELECT detail FROM hunt_log WHERE outcome='grabbed'").fetchone()[0]
    assert "long-missing" in note and "1 seeder" in note and "relaxed to 1" in hl


def test_better_seeded_releases_still_win(env, hunter):
    _profiled_show(env, "American Dad")
    good = _release("American.Dad.S01E01.1080p.WEB.x264-GOOD", "magnet:good")
    good["seeders"] = 3                                  # also below 5, but more than 2
    hunter.results = [_thin("American.Dad.S01E01.720p.WEB.x264"), good]
    _searched(env, 6)
    env.SCH.hunt_wanted()
    assert hunter.client.added == ["magnet:good"]


@pytest.mark.parametrize("why", ["recent", "few_searches", "switched_off", "zero_seed_floor"])
def test_relaxing_has_limits(env, hunter, monkeypatch, why):
    from datetime import date, timedelta
    air = (date.today() - timedelta(days=2)).isoformat() if why == "recent" else "2010-05-01"
    _profiled_show(env, "American Dad", air=air)
    rel = _thin("American.Dad.S01E01.720p.WEB.x264")
    if why == "zero_seed_floor":
        rel["seeders"] = 0                               # default floor is 1
    hunter.results = [rel]
    _searched(env, 2 if why == "few_searches" else 6)
    if why == "switched_off":
        monkeypatch.setenv("HUNT_RELAX_SEEDERS", "0")
    env.SCH.hunt_wanted()
    assert hunter.client.added == []


def test_relaxing_never_applies_to_upgrades_and_movies_go_by_searches(env):
    R = env.SCH._relaxed
    assert R({"reason": "upgrade", "search_count": 50, "_air": "2001-01-01"}) is None
    assert R({"reason": "missing", "kind": "movie", "search_count": 4}) == 1
    assert R({"reason": "missing", "kind": "movie", "search_count": 3}) is None
    assert R({"reason": "missing", "kind": "episode", "search_count": 9, "_air": ""}) is None   # TBA

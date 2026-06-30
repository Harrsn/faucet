"""Cascade test suite. Run with: pytest -q"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from faucet import search
from faucet.clients import make_client, DownloadClientError
from faucet.clients.base import Transfer, TransferFile, AddResult


# ---------------- search / badges ----------------
def test_human_size():
    assert search.human_size(0) == "0.0 B"
    assert search.human_size(1024) == "1.0 KB"
    assert search.human_size(int(1.5 * 1024**3)) == "1.5 GB"


@pytest.mark.parametrize("title,res,src,ext", [
    ("Dune 2024 2160p BluRay x265.mkv", "2160p", "BluRay", "MKV"),
    ("Show S01E01 1080p WEB-DL", "1080p", "WEB-DL", None),
    ("Movie 720p HDTV XviD.avi", "720p", "HDTV", "AVI"),
    ("Plain release name", None, None, None),
])
def test_badges(title, res, src, ext):
    b = search.parse_badges(title)
    assert b["res"] == res and b["source"] == src and b["ext"] == ext


def test_search_requires_key():
    with pytest.raises(search.SearchError):
        search.search("http://x", "", "all", "q", "all", 10)


# ---------------- client factory ----------------
def test_factory_known_clients():
    for kind in ("transmission", "qbittorrent", "deluge"):
        c = make_client(kind, "http://localhost", "u", "p")
        assert c.name == kind


def test_factory_unknown():
    with pytest.raises(DownloadClientError):
        make_client("notaclient", "http://x")


# ---------------- transmission parsing (no network) ----------------
def test_transmission_transfer_mapping(monkeypatch):
    from faucet.clients.transmission import TransmissionClient
    c = TransmissionClient("http://x")
    monkeypatch.setattr(c, "_rpc", lambda m, a: {"torrents": [
        {"id": 1, "name": "T", "percentDone": 0.5, "rateDownload": 1000,
         "rateUpload": 0, "status": 4, "eta": 60, "uploadRatio": 0.5,
         "totalSize": 2000, "errorString": ""}]})
    xs = c.list_transfers()
    assert len(xs) == 1
    t = xs[0]
    assert t.percent == 50.0 and t.status == "downloading" and not t.done


def test_transmission_done_flag():
    t = Transfer("1", "x", 100.0, 0, 0, "seeding", -1, 2.0, 100)
    assert t.done


# ---------------- app endpoints with mocked client ----------------
@pytest.fixture
def client_app(monkeypatch):
    os.environ["JACKETT_API_KEY"] = "test"
    from faucet import app as appmod

    class Mock:
        name = "transmission"
        def test(self): return True
        def add(self, m, d=None): return AddResult(id="1", name="Test")
        def list_transfers(self):
            return [Transfer("1", "Dune", 45.0, 5_000_000, 0, "downloading", 600, 0.0, 4_000_000_000)]
        def files(self, i):
            return [TransferFile("dune.mkv", "Dune/dune.mkv", 4_000_000_000, 45.0, True)]
        def pause(self, i): pass
        def resume(self, i): pass
        def remove(self, i, delete_data=False): pass

    monkeypatch.setattr(appmod, "client", lambda: Mock())
    monkeypatch.setattr(appmod.searchmod, "search",
                        lambda *a, **k: [{"title": "Dune 1080p", "href": "magnet:x",
                                          "is_magnet": True, "seeders": 400, "peers": 9,
                                          "size": 4_000_000_000, "size_h": "3.7 GB",
                                          "tracker": "t", "badges": {"res": "1080p", "source": None, "ext": None}}])
    from fastapi.testclient import TestClient
    return TestClient(appmod.app)


def test_api_search(client_app):
    r = client_app.get("/api/search?q=dune")
    assert r.status_code == 200 and r.json()["total"] == 1


def test_api_add(client_app):
    r = client_app.post("/api/add", json={"magnet": "magnet:x"})
    assert r.status_code == 200 and r.json()["name"] == "Test"


def test_api_add_empty(client_app):
    assert client_app.post("/api/add", json={"magnet": ""}).status_code == 400


def test_api_transfers(client_app):
    r = client_app.get("/api/transfers")
    assert r.json()["transfers"][0]["percent"] == 45.0


def test_api_files(client_app):
    assert client_app.get("/api/torrent/1/files").json()["files"][0]["name"] == "dune.mkv"


def test_api_action(client_app):
    assert client_app.post("/api/torrent/1", json={"action": "pause"}).json()["action"] == "pause"


def test_api_action_bad(client_app):
    assert client_app.post("/api/torrent/1", json={"action": "nope"}).status_code == 400


def test_api_config(client_app):
    cfg = client_app.get("/api/config").json()
    assert cfg["title"] and "accent" in cfg


# ---------------- database ----------------
def test_db_settings_and_history(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    db.set_setting("k", {"a": 1})
    assert db.get_setting("k") == {"a": 1}
    db.add_history("completed", "X", "sorted", 1000)
    assert len(db.recent_history()) == 1
    s = db.history_stats()
    assert s["completed_count"] == 1 and s["completed_bytes"] == 1000


def test_db_default_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM profiles").fetchone()["n"]
    assert n >= 1


# ---------------- tmdb ----------------
def test_tmdb_disabled_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    from faucet import tmdb
    importlib.reload(tmdb)
    assert not tmdb.enabled()
    assert tmdb.search("dune") == []


def test_tmdb_parse_and_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    from faucet import tmdb
    importlib.reload(tmdb)
    db.set_setting("tmdb_key", "k")
    tmdb._get = lambda path, params: {"results": [
        {"id": 1, "media_type": "movie", "title": "Dune", "release_date": "2021-01-01",
         "poster_path": "/p.jpg", "vote_average": 8.0},
        {"id": 2, "media_type": "person", "name": "x"}]}
    res = tmdb.search("dune")
    assert len(res) == 1 and res[0]["year"] == "2021"
    assert res[0]["search_query"] == "Dune 2021"


# ---------------- setup wizard ----------------
def test_config_save_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("CASCADE_CONFIG_FILE", str(tmp_path / "cascade.env"))
    # other tests may have set these in the process env; clear for isolation
    for k in ("JACKETT_API_KEY", "CLIENT_URL", "DOWNLOAD_CLIENT", "UI_ACCENT"):
        monkeypatch.delenv(k, raising=False)
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    assert not cfgmod.config.configured()
    cfgmod.save({"JACKETT_API_KEY": "k", "CLIENT_URL": "http://c",
                 "DOWNLOAD_CLIENT": "deluge", "UI_ACCENT": "rose"})
    assert cfgmod.config.configured()
    assert cfgmod.config.client_kind == "deluge"
    assert cfgmod.config.ui_accent == "rose"


def test_config_save_whitelist(tmp_path, monkeypatch):
    monkeypatch.setenv("CASCADE_CONFIG_FILE", str(tmp_path / "cascade.env"))
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    cfgmod.save({"EVIL": "x", "JACKETT_INDEXER": "1337x"})
    body = (tmp_path / "cascade.env").read_text()
    assert "EVIL" not in body
    assert "JACKETT_INDEXER=1337x" in body


# ---------------- content classification ----------------
def test_classify_game_by_platform():
    from faucet.classify import classify, dest_folder
    r = classify("Lego Harry Potter Years 1-4 PS3", 1000)
    assert r["type"] == "game" and r["platform"] == "PS3"
    assert dest_folder("game") == "games"


def test_classify_movie_and_tv_by_category():
    from faucet.classify import classify
    assert classify("Dune 2024 1080p BluRay", 2000)["type"] == "movie"
    assert classify("The Office S03E07", 5000)["type"] == "tv"


def test_classify_game_by_scene_group_no_category():
    from faucet.classify import classify
    r = classify("Cyberpunk 2077 v2.1 REPACK FitGirl", None)
    assert r["type"] == "game"


def test_classify_switch_and_console():
    from faucet.classify import classify
    assert classify("Super Mario Odyssey NSW", None)["platform"] == "Nintendo Switch"
    assert classify("Elden Ring PS5", None)["type"] == "game"


# ---------------- quality profiles ----------------
def test_profile_passes_and_score():
    from faucet import profiles as p
    GB = 1024 ** 3
    prof = {"min_seeders": 3, "resolutions": ["1080p", "720p"],
            "sources": ["WEB-DL", "BluRay"], "max_size_gb": 8, "min_size_gb": 0}
    good = {"seeders": 50, "size": int(4 * GB), "badges": {"res": "1080p", "source": "WEB-DL"}}
    toobig = {"seeders": 50, "size": int(40 * GB), "badges": {"res": "1080p", "source": "WEB-DL"}}
    lowseed = {"seeders": 1, "size": int(4 * GB), "badges": {"res": "1080p", "source": "WEB-DL"}}
    assert p.passes(good, prof)[0] is True
    assert p.passes(toobig, prof)[0] is False
    assert p.passes(lowseed, prof)[0] is False


def test_profile_ranking_prefers_better():
    from faucet import profiles as p
    GB = 1024 ** 3
    prof = {"min_seeders": 0, "resolutions": ["1080p", "720p"],
            "sources": ["WEB-DL", "BluRay"], "max_size_gb": 0}
    results = [
        {"title": "720p WEB", "seeders": 5, "size": GB, "badges": {"res": "720p", "source": "WEB-DL"}},
        {"title": "1080p WEB", "seeders": 5, "size": GB, "badges": {"res": "1080p", "source": "WEB-DL"}},
    ]
    assert p.best(results, prof)["title"] == "1080p WEB"


def test_profile_api_crud(client_app):
    created = client_app.post("/api/profiles", json={
        "name": "T", "min_seeders": 2, "resolutions": ["1080p"], "sources": ["WEB-DL"]})
    pid = created.json()["id"]
    names = [p["name"] for p in client_app.get("/api/profiles").json()["profiles"]]
    assert "T" in names
    client_app.delete(f"/api/profiles/{pid}")


# ---------------- subscriptions / scheduler ----------------
def test_subscription_crud(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    sid = db.create_subscription("Show", "show s01", "tv", 1)
    assert db.get_subscription(sid)["title"] == "Show"
    db.update_subscription(sid, enabled=0)
    assert db.get_subscription(sid)["enabled"] == 0
    assert len(db.list_subscriptions(enabled_only=True)) == 0
    db.delete_subscription(sid)
    assert db.get_subscription(sid) is None


def test_grabbed_dedupe(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    assert db.mark_grabbed("Release.X.1080p") is True
    assert db.already_grabbed("Release.X.1080p") is True
    assert db.mark_grabbed("Release.X.1080p") is False  # dedupe


def test_scheduler_grabs_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("JACKETT_API_KEY", "k")
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    from faucet import scheduler as sch
    importlib.reload(sch)
    GB = 1024 ** 3
    db.create_subscription("Show", "show s01", "tv", 1)
    sch.searchmod.search = lambda *a, **k: [
        {"title": "Show S01E01 1080p WEB-DL", "href": "magnet:x", "seeders": 50,
         "size": int(2 * GB), "badges": {"res": "1080p", "source": "WEB-DL"}}]

    class FakeAdd:
        id = "a"; name = "x"; duplicate = False

    class FakeClient:
        def add(self, *a, **k):
            return FakeAdd()
    sch.make_client = lambda *a, **k: FakeClient()
    r1 = sch.run_once()
    assert r1["grabbed"] == 1
    r2 = sch.run_once()
    assert r2["grabbed"] == 0   # dedupe: same release not grabbed twice


# ---------------- library awareness (scan/reconcile/hunt) ----------------
def _fake_library(tmp_path):
    tv = tmp_path / "lib" / "tvshows" / "Test Show" / "Season 01"
    tv.mkdir(parents=True)
    (tv / "Test Show - S01E01 1080p.mkv").write_bytes(b"x" * (60 * 1024 * 1024))
    (tv / "Test Show - S01E02 720p.mkv").write_bytes(b"x" * (60 * 1024 * 1024))
    return tmp_path / "lib"


def test_library_scan_and_have(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(_fake_library(tmp_path)))
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    from faucet import library as L
    importlib.reload(L)
    s = L.scan()
    assert s["episodes"] == 2
    assert L.have_episode("Test Show", 1, 1)
    assert not L.have_episode("Test Show", 1, 5)
    # incremental: rescan skips
    assert L.scan()["skipped"] == 2


def test_reconcile_missing_and_upgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(_fake_library(tmp_path)))
    import importlib, json
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    from faucet import library as L
    importlib.reload(L)
    L.scan()
    from faucet import tmdb as T
    importlib.reload(T)
    T.details = lambda tid, mt: {"seasons": 1, "title": "Test Show"}
    T.episodes = lambda tid, n: [
        {"season": 1, "episode": 1, "title": "P", "air_date": "2020-01-01"},
        {"season": 1, "episode": 2, "title": "T", "air_date": "2020-01-08"},
        {"season": 1, "episode": 3, "title": "Th", "air_date": "2020-01-15"}]
    from faucet import series as S
    importlib.reload(S)
    with db.connect() as c:
        c.execute("INSERT INTO profiles (name,min_seeders,resolutions,sources,max_size_gb) "
                  "VALUES (?,?,?,?,?)", ("HD", 1, json.dumps(["1080p"]), json.dumps(["WEB-DL"]), 10))
        pid = c.execute("SELECT id FROM profiles WHERE name='HD'").fetchone()["id"]
    sid = S.add_series(123, "Test Show", 2020, None, pid)
    r = S.reconcile(sid)
    assert r["missing"] == 1   # S01E03
    assert r["upgrades"] == 1  # S01E02 in 720p, want 1080p
    assert r["have"] == 2


def test_title_normalization():
    from faucet.library import normalize_title as nt
    # real-world mismatches that must collapse to the same key
    assert nt("Bobs Burgers") == nt("Bob's Burgers")
    assert nt("American Dad") == nt("American Dad!")
    assert nt("Stranger Things [1080p]") == nt("Stranger Things")
    assert nt("Ted (2024) [720p]") == nt("Ted")
    assert nt("Rick and Morty") == nt("Rick & Morty")


def test_movie_monitor_and_reconcile(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    lib = tmp_path / "lib" / "movies" / "Dune (2021)"
    lib.mkdir(parents=True)
    (lib / "Dune (2021) 1080p.mkv").write_bytes(b"x" * (60 * 1024 * 1024))
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "lib"))
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    from faucet import library as L
    importlib.reload(L)
    L.scan()
    from faucet import movies as M
    importlib.reload(M)
    m1 = M.add_movie(1, "Dune", 2021, None, None)
    m2 = M.add_movie(2, "Dune Part Two", 2024, None, None)
    assert M.get_movie(m1)["status"] == "have"
    assert M.get_movie(m2)["status"] == "wanted"
    assert M.reconcile_all() == {"have": 1, "wanted": 1}


def test_library_auto_import(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    tv = tmp_path / "lib" / "tvshows" / "The Office" / "Season 01"
    tv.mkdir(parents=True)
    (tv / "The Office - S01E01.mkv").write_bytes(b"x" * (60 * 1024 * 1024))
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "lib"))
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    db.set_setting("tmdb_key", "k")
    from faucet import library as L
    importlib.reload(L)
    from faucet import tmdb as T
    importlib.reload(T)
    T.enabled = lambda: True
    T.search = lambda q, kind="multi": [{"tmdb_id": 1, "media_type": "tv",
        "title": "The Office", "year": "2005", "poster": None, "search_query": "The Office"}]
    T.details = lambda tid, mt: {"seasons": 1, "title": "x"}
    T.episodes = lambda tid, n: []
    from faucet import series as S
    importlib.reload(S)
    from faucet import movies as M
    importlib.reload(M)
    from faucet import importer as I
    importlib.reload(I)
    r = I.import_library()
    assert r["shows_imported"] == 1
    assert any(s["title"] == "The Office" for s in S.list_series())


def test_movie_subset_matching():
    from faucet.movies import _movie_matches as mm
    # truncated disk folders should match full TMDb titles when years agree
    assert mm("The Chronicles of Narnia", 2005,
              "The Chronicles of Narnia: The Lion, the Witch and the Wardrobe", 2005)
    assert mm("The Hobbit Battle Of The Five Armies", 2014,
              "The Hobbit: The Battle of the Five Armies", 2014)
    # franchise entries with shared truncated folder names disambiguate by year
    assert not mm("The Hunger Games Mockingjay", 2015,
                  "The Hunger Games: Mockingjay - Part 1", 2014)
    assert not mm("The Chronicles of Narnia", 2008,
                  "The Chronicles of Narnia: The Lion, the Witch and the Wardrobe", 2005)
    # exact title tolerates 1-year metadata fuzz; unrelated titles don't match
    assert mm("Dune", 2021, "Dune", 2020)
    assert not mm("The Batman", 2022, "Batman Begins", 2005)


def test_pack_classification():
    from faucet.packs import classify_pack as cp
    assert cp("American Dad S01E07 1080p")["kind"] == "single"
    assert cp("American Dad S03 1080p WEB-DL") == {"kind": "season", "season": 3}
    assert cp("American Dad Season 3 Complete 1080p") == {"kind": "season", "season": 3}
    assert cp("American Dad Seasons 1-15 1080p")["kind"] == "series"
    assert cp("Stranger Things Complete Series")["kind"] == "series"
    assert cp("The Office Season 2 1080p")["season"] == 2


def test_scan_report(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    tv = tmp_path / "lib" / "tvshows" / "Good Show" / "Season 01"
    tv.mkdir(parents=True)
    (tv / "Good Show - S01E01.mkv").write_bytes(b"x" * (60 * 1024 * 1024))
    (tv / "mysteryfile.mkv").write_bytes(b"x" * (60 * 1024 * 1024))  # unparsable
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "lib"))
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    from faucet import library as L
    importlib.reload(L)
    L.scan()
    rep = L.scan_report()
    assert rep["total"] == 1
    assert rep["unparsed_tv"][0]["name"] == "mysteryfile.mkv"


def test_monitor_mode_future_skips_backlog(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    (tmp_path / "lib" / "tvshows").mkdir(parents=True)
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "lib"))
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    from faucet import library as L
    importlib.reload(L)
    L.scan()
    from faucet import tmdb as T
    importlib.reload(T)
    T.details = lambda tid, mt: {"seasons": 1, "title": "Show"}
    T.episodes = lambda tid, n: [{"season": 1, "episode": e, "title": f"E{e}",
                                  "air_date": f"2020-01-0{e}"} for e in range(1, 4)]
    from faucet import series as S
    importlib.reload(S)
    sid = S.add_series(1, "Show", 2020, None, None)
    assert S.reconcile(sid)["missing"] == 3
    S.set_monitor_mode(sid, "future")
    with db.connect() as c:
        w = c.execute("SELECT COUNT(*) n FROM wanted WHERE series_id=? AND status='wanted'",
                      (sid,)).fetchone()["n"]
    assert w == 0  # backlog skipped


def test_episode_hunt_rejects_series_packs(tmp_path, monkeypatch):
    """A per-episode hunt must never grab a multi-season pack that merely
    contains the wanted episode (re-downloading seasons you own)."""
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("JACKETT_API_KEY", "k")
    monkeypatch.setenv("HUNT_MAX_PER_RUN", "5")
    monkeypatch.setenv("HUNT_MAX_ACTIVE", "10")
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    from faucet import scheduler as SCH
    importlib.reload(SCH)
    GB = 1024 ** 3
    with db.connect() as c:
        c.execute("INSERT INTO series (tmdb_id,title,total_seasons,monitored) VALUES (1,'AD',21,1)")
        sid = c.execute("SELECT id FROM series WHERE title='AD'").fetchone()["id"]
        c.execute("INSERT INTO wanted (kind,series_id,season,episode,title,reason,status) "
                  "VALUES ('episode',?,1,1,'AD S01E01','missing','wanted')", (sid,))
    SCH.searchmod.search = lambda *a, **k: [
        {"title": "AD 2005.S01-S21.720p-Zero00", "href": "magnet:s", "seeders": 500,
         "size": int(200 * GB), "badges": {"res": "720p"}},
        {"title": "AD S01E01 720p", "href": "magnet:e", "seeders": 10,
         "size": int(GB), "badges": {"res": "720p"}}]

    class FA:
        id = "a"; name = "x"; duplicate = False

    class FC:
        def list_transfers(self): return []
        def add(self, *a, **k): return FA()
    SCH.make_client = lambda *a, **k: FC()
    r = SCH.hunt_wanted()
    grabbed = [d["grabbed"] for d in r["details"] if d.get("grabbed")]
    assert grabbed and "S01E01" in grabbed[0] and "S01-S21" not in grabbed[0]


def test_multisegment_episode_parsing(tmp_path, monkeypatch):
    """SpongeBob-style multi-segment files (S01E01ab) should parse, while real
    extras with no season/episode stay unmatched."""
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    sb = tmp_path / "lib" / "tvshows" / "SpongeBob SquarePants" / "Season 01"
    sb.mkdir(parents=True)
    (sb / "SpongeBob SquarePants - S01E01abc - Help Wanted WEBDL-1080p.mkv").write_bytes(b"x" * (60 * 1024 * 1024))
    (sb / "SpongeBob.SquarePants.S01E02a.Bubblestand.1080p.mkv").write_bytes(b"x" * (60 * 1024 * 1024))
    (sb / "Behind the Scenes.mkv").write_bytes(b"x" * (60 * 1024 * 1024))  # real extra
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "lib"))
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    from faucet import library as L
    importlib.reload(L)
    s = L.scan()
    assert s["episodes"] == 2          # the two multi-segment files parsed
    assert L.have_episode("SpongeBob SquarePants", 1, 1)
    assert L.have_episode("SpongeBob SquarePants", 1, 2)
    rep = L.scan_report()
    # only the genuine extra remains unparsed
    assert rep["total"] == 1
    assert "Behind the Scenes" in rep["unparsed_tv"][0]["name"]


def _auth_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    from faucet import auth
    importlib.reload(auth)
    return auth


def test_auth_first_user_is_admin(tmp_path, monkeypatch):
    auth = _auth_setup(tmp_path, monkeypatch)
    uid, err = auth.create_user("admin1", "supersecret123")
    assert err is None
    u = auth.get_user(uid)
    assert u["role"] == "admin" and u["status"] == "active"
    assert u["pw_hash"].startswith("$2b$")


def test_auth_pending_gate_and_login(tmp_path, monkeypatch):
    auth = _auth_setup(tmp_path, monkeypatch)
    auth.create_user("admin1", "supersecret123")          # bootstrap admin
    uid2, _ = auth.create_user("guest", "guestpass123")    # pending
    u2 = auth.get_user(uid2)
    assert u2["status"] == "pending"
    _, err = auth.authenticate("guest", "guestpass123")
    assert "approval" in err
    auth.set_status(uid2, "active")
    user, err = auth.authenticate("guest", "guestpass123")
    assert user and not err


def test_auth_sessions_and_revocation(tmp_path, monkeypatch):
    auth = _auth_setup(tmp_path, monkeypatch)
    uid, _ = auth.create_user("admin1", "supersecret123")
    cookie = auth.create_session(uid)
    assert auth.session_user(cookie)["username"] == "admin1"
    assert auth.session_user(cookie + "x") is None         # tampered
    auth.destroy_session(cookie)
    assert auth.session_user(cookie) is None               # revoked


def test_auth_lockout(tmp_path, monkeypatch):
    auth = _auth_setup(tmp_path, monkeypatch)
    auth.create_user("admin1", "supersecret123")
    for _ in range(5):
        auth.authenticate("admin1", "wrong")
    _, err = auth.authenticate("admin1", "supersecret123")
    assert "locked" in err.lower()


def test_request_flow_lifecycle(tmp_path, monkeypatch):
    """User requests -> pending -> admin approves -> monitored; trusted users
    auto-approve; duplicates are blocked."""
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("SESSION_SECRET", "test")
    import importlib
    from faucet import config as cfgmod
    importlib.reload(cfgmod)
    from faucet import db
    importlib.reload(db)
    db.init()
    from faucet import auth
    importlib.reload(auth)
    from faucet import series as smod
    importlib.reload(smod)
    smod.add_series = lambda *a, **k: 999
    smod.reconcile = lambda sid: None
    from faucet import requests_flow
    importlib.reload(requests_flow)

    aid, _ = auth.create_user("admin", "adminpass123")
    admin = auth.get_user(aid)
    bid, _ = auth.create_user("bob", "bobpass1234")
    auth.set_status(bid, "active")
    bob = auth.get_user(bid)

    r = requests_flow.create_request(bob, "tv", 1396, "Breaking Bad", 2008)
    assert r["status"] == "pending"
    a = requests_flow.approve_request(r["request_id"], decider=admin)
    assert a["status"] == "approved" and a.get("series_id") == 999
    assert requests_flow.list_requests(admin)[0]["status"] == "approved"

    with db.connect() as c:
        c.execute("UPDATE users SET can_autoapprove=1 WHERE id=?", (bid,))
    bob = auth.get_user(bid)
    r2 = requests_flow.create_request(bob, "tv", 1100, "Some Show", 2020)
    assert r2["status"] == "auto_approved"

    r3 = requests_flow.create_request(bob, "tv", 1396, "Breaking Bad", 2008)
    assert r3["status"] in ("already_requested", "already_available")


def test_request_gating(tmp_path, monkeypatch):
    """Regular users can browse library + TMDb but not indexer search/torrents."""
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("SESSION_SECRET", "test")
    monkeypatch.setenv("JACKETT_API_KEY", "k")
    import importlib
    for m in ["config", "db", "auth", "requests_flow", "auth_routes", "app"]:
        importlib.reload(importlib.import_module("faucet." + m))
    from faucet import db, app as appmod
    db.init()
    from fastapi.testclient import TestClient
    admin = TestClient(appmod.app, follow_redirects=False)
    admin.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    admin.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    acsrf = admin.cookies.get("faucet_csrf")
    admin.post("/api/admin/users",
               json={"username": "bob", "password": "bobpass1234", "role": "user", "status": "active"},
               headers={"X-CSRF-Token": acsrf})
    bob = TestClient(appmod.app, follow_redirects=False)
    bob.post("/api/auth/login", json={"username": "bob", "password": "bobpass1234"})
    bcsrf = bob.cookies.get("faucet_csrf")
    assert bob.get("/api/series").status_code == 200          # browse library
    assert bob.get("/api/search?q=x").status_code == 403       # indexer blocked
    assert bob.get("/api/transfers").status_code == 403        # torrents blocked
    assert bob.post("/api/add", json={}, headers={"X-CSRF-Token": bcsrf}).status_code == 403
    assert bob.post("/api/series", json={}, headers={"X-CSRF-Token": bcsrf}).status_code == 403


def test_sorter_no_duplicate_on_same_share(tmp_path, monkeypatch):
    """On CIFS (hardlink unsupported) but same share, files MOVE without
    duplicating; only genuine cross-fs falls back to copy."""
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "ev.jsonl"))
    import importlib, os, logging
    from faucet import sort as S
    importlib.reload(S)
    S.MODE = "auto"
    logging.disable(logging.CRITICAL)

    # simulate CIFS: hardlink unsupported
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("cifs")))

    src = tmp_path / "torrents" / "show.mkv"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x" * 1000)
    dest = tmp_path / "media" / "tvshows" / "show.mkv"
    S.place(src, dest, dry=False)
    assert dest.exists() and not src.exists()      # moved, no duplicate

    # cross-fs: rename also fails -> copy (source kept)
    monkeypatch.setattr(os, "rename", lambda *a, **k: (_ for _ in ()).throw(OSError("xdev")))
    src2 = tmp_path / "dl" / "m.mkv"
    src2.parent.mkdir(parents=True)
    src2.write_bytes(b"q" * 800)
    dest2 = tmp_path / "lib2" / "m.mkv"
    S.place(src2, dest2, dry=False)
    assert dest2.exists() and src2.exists()        # copied (last resort)
    logging.disable(logging.NOTSET)


def test_clean_torrent_name_strips_watermarks():
    from faucet.clients.transmission import _clean_torrent_name as cn
    assert cn("[www.UIndex.org](https://www.UIndex.org) - American Dad S01E01 1080p") \
        == "American Dad S01E01 1080p"
    assert cn("www.SomeTracker.net - American.Dad.S05E01.1080p") \
        == "American.Dad.S05E01.1080p"
    assert "/" not in cn("[x](https://y/z) - Show S01E01")     # no path seps survive
    assert cn("American Dad S01E01 1080p") == "American Dad S01E01 1080p"  # clean passes


def test_sorter_recovers_mangled_path(tmp_path):
    import logging
    from faucet import sort as S
    logging.disable(logging.CRITICAL)
    real = tmp_path / "American Dad S01E01 Pilot 1080p"
    real.mkdir()
    (real / "American Dad S01E01.mkv").write_bytes(b"x" * (60 * 1024 * 1024))
    mangled = "[www.UIndex.org](https://www.UIndex.org) - American Dad S01E01 Pilot 1080p"
    rec = S._recover_inputs(str(tmp_path), mangled)
    assert rec and any(p.exists() for p in rec)
    logging.disable(logging.NOTSET)


def test_cleanup_release_dir(tmp_path):
    import logging
    from faucet import sort as S
    logging.disable(logging.CRITICAL)
    # junk-only dir -> removed
    d = tmp_path / "release"
    d.mkdir()
    (d / "Torrent Downloaded From UIndex.org.txt").write_text("junk")
    (d / "poster.jpg").write_bytes(b"x" * 1000)
    S._cleanup_release_dir(d)
    assert not d.exists()
    # dir with a sizeable unknown file -> kept (safety)
    d2 = tmp_path / "release2"
    d2.mkdir()
    (d2 / "important.bin").write_bytes(b"x" * (10 * 1024 * 1024))
    S._cleanup_release_dir(d2)
    assert d2.exists()
    logging.disable(logging.NOTSET)

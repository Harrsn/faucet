"""Phase 5b.1: settings live in the database (imported once), and the
parallel search skips indexers that don't carry the content and stops
waiting on stragglers."""
from __future__ import annotations

import importlib
import logging
import stat
import threading
import time
from types import SimpleNamespace

import pytest


def _fresh(tmp_path, monkeypatch, env=None, legacy=None):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("FAUCET_CONFIG_FILE", str(tmp_path / "config" / "faucet.env"))
    from faucet import config as c0
    for k in c0.WIZARD_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    if legacy is not None:
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "faucet.env").write_text(legacy)
    return importlib.reload(importlib.import_module("faucet.config"))


def test_import_once_from_file_and_environment(tmp_path, monkeypatch, caplog):
    cfg = _fresh(tmp_path, monkeypatch,
                 env={"JACKETT_URL": "http://from-env:9117", "APP_TITLE": "EnvTitle"},
                 legacy="# old\nJACKETT_API_KEY=abc123\nAPP_TITLE=FileTitle\nEVIL=1\n")
    st = cfg.stored()
    assert st == {"JACKETT_URL": "http://from-env:9117", "JACKETT_API_KEY": "abc123",
                  "APP_TITLE": "FileTitle"}                        # the file won, as before
    assert cfg.config.app_title == "FileTitle"
    assert not (tmp_path / "config" / "faucet.env").exists()
    assert (tmp_path / "config" / "faucet.env.imported").exists()

    # afterwards the database is the source: a new environment value is ignored
    monkeypatch.setenv("APP_TITLE", "ChangedInCompose")
    caplog.set_level(logging.WARNING, logger="faucet.config")
    cfg.reload()
    assert cfg.config.app_title == "FileTitle"
    assert any("APP_TITLE is set in the environment" in r.getMessage() for r in caplog.records)


def test_import_happens_once(tmp_path, monkeypatch):
    cfg = _fresh(tmp_path, monkeypatch, env={"JACKETT_API_KEY": "first"})
    cfg.save({"JACKETT_API_KEY": "edited-in-ui"})
    cfg = _fresh(tmp_path, monkeypatch, env={"JACKETT_API_KEY": "first"})   # restart
    assert cfg.config.jackett_api_key == "edited-in-ui"


def test_nothing_to_import_creates_no_files(tmp_path, monkeypatch):
    cfg = _fresh(tmp_path, monkeypatch)
    assert cfg.stored() == {} and not (tmp_path / "config").exists()


def test_save_persists_across_restart_and_is_hidden_from_generic_settings(tmp_path, monkeypatch):
    cfg = _fresh(tmp_path, monkeypatch)
    db = importlib.reload(importlib.import_module("faucet.db"))
    db.init()
    cfg.save({"CLIENT_PASS": "hunter22", "CLIENT_URL": "http://t:9091/transmission/rpc"})
    cfg = _fresh(tmp_path, monkeypatch)
    assert cfg.config.client_pass == "hunter22"
    assert not any(k.startswith("cfg.") for k in db.all_settings())
    assert stat.S_IMODE(cfg._db_path().stat().st_mode) == 0o600


# ── search refinements ──────────────────────────────────────────────────────

@pytest.fixture
def fenv(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("JACKETT_API_KEY", "key123456")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db")}
    mods["db"].init()
    for m in ("search", "finder", "indexerhealth"):
        mods[m] = importlib.reload(importlib.import_module("faucet." + m))
    return SimpleNamespace(**mods)


def test_caps_categories_and_carries(fenv, monkeypatch):
    xml = (b'<caps><searching><search available="yes" supportedParams="q"/>'
           b'<movie-search available="yes" supportedParams="q,imdbid"/></searching>'
           b'<categories><category id="2000" name="Movies"><subcat id="2040" name="HD"/>'
           b'</category></categories></caps>')

    class R:
        status_code, content = 200, xml
    monkeypatch.setattr(fenv.search.requests, "get", lambda *a, **k: R())
    caps = fenv.search.caps("http://j", "key123456", "yts")
    assert caps["_cats"] == {2000}
    assert fenv.finder.carries(caps, "movie") and not fenv.finder.carries(caps, "episode")
    assert fenv.finder.carries({"search": {"q"}}, "episode")          # unknown = yes


def _wire(fenv, monkeypatch, caps_by_ix, behave):
    monkeypatch.setattr(fenv.finder, "indexer_ids", lambda: list(caps_by_ix))
    monkeypatch.setattr(fenv.finder, "indexer_caps", lambda ix: caps_by_ix[ix])
    asked = []
    lock = threading.Lock()

    def search(url, key, ix, *a, **k):
        with lock:
            asked.append(ix)
        return behave(ix)
    monkeypatch.setattr(fenv.search, "search", search)
    return asked


TV = {"search": {"q"}, "tv-search": {"q", "season", "ep"}, "_cats": {5000}}
MOVIES = {"search": {"q"}, "_cats": {2000}}


def test_tv_search_skips_movie_only_indexers(fenv, monkeypatch):
    asked = _wire(fenv, monkeypatch, {"eztv": TV, "yts": MOVIES},
                  lambda ix: fenv.search.Results([]))
    out = fenv.finder.find("episode", title="Show", season=1, episode=1)
    assert asked == ["eztv"] and out.skipped == ["yts"] and out.searched == 1


def test_stragglers_are_left_behind(fenv, monkeypatch):
    monkeypatch.setenv("SEARCH_STRAGGLER_SECONDS", "1")
    release = threading.Event()

    def behave(ix):
        if ix == "slow":
            release.wait(10)
            return fenv.search.Results([])
        return fenv.search.Results([{"title": f"Show.S01E01.{ix}", "href": f"magnet:{ix}",
                                     "seeders": 5, "size": 1}])
    _wire(fenv, monkeypatch, {"a": TV, "b": TV, "c": TV, "slow": TV}, behave)
    t0 = time.monotonic()
    out = fenv.finder.find("episode", title="Show", season=1, episode=1)
    took = time.monotonic() - t0
    release.set()
    assert 0.9 < took < 3 and out.slow == ["slow"] and len(out) == 3 and out.errors == []


def test_per_indexer_stats(fenv):
    rec = fenv.indexerhealth.record_search
    for _ in range(3):
        rec("eztv", True, 800, 40)
    for _ in range(6):
        rec("1337x", True, 11500, 0)
    rec("1337x", False, 60000, 0, error="Read timed out")
    s = fenv.indexerhealth.summary(24)
    by = {x["indexer"]: x for x in s["indexers"]}
    assert s["indexers"][0]["indexer"] == "eztv"                    # most useful first
    assert by["eztv"] == {"indexer": "eztv", "searches": 3, "failed": 0, "empty": 0,
                          "results": 120, "avg_ms": 800, "p95_ms": 800}
    assert by["1337x"]["empty"] == 6 and by["1337x"]["failed"] == 1 and by["1337x"]["results"] == 0


def test_unwritable_data_dir_falls_back_to_environment(tmp_path, monkeypatch):
    """CI caught this: a setting in the environment and no writable data
    directory made importing faucet.config raise PermissionError."""
    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv("EVENTS_FILE", str(blocked / "config" / "events.jsonl"))
    monkeypatch.setenv("BROWSE_ROOT", "/somewhere")
    from faucet import config as c0
    import os as _os
    if _os.geteuid() == 0:
        monkeypatch.setattr(c0.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(
            PermissionError("denied")))
        cfg = c0
        assert cfg._load_persisted(into_env=True) == {}
    else:
        cfg = importlib.reload(c0)
    assert cfg.Config().browse_root == "/somewhere"

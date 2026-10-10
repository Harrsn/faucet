"""Phase 4b: operations and hygiene.

M1  Jackett's error text reaches the probe and hunt log (not "400 Bad Request").
M2  The probe allows for FlareSolverr (longer timeout, low concurrency) and
    runs in the background.
M3  Database backup download; TMDb cache pruning; VACUUM; events rotation.
M4  /metrics in Prometheus text format with bearer-token or admin auth.
M5  The sweep logs a never-sortable leftover once, not every tick.
M6  Telegram/ntfy notifications survive real release names.
"""
from __future__ import annotations

import importlib
import json
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

JACKETT_400 = (b'<?xml version="1.0" encoding="UTF-8"?>\n<error code="900" description='
               b'"Jackett.Common.IndexerException: Exception (eztv): Challenge detected but '
               b'FlareSolverr is not configured&#xA; ---&gt; FlareSolverrSharp.Exceptions.'
               b'FlareSolverrException: Challenge detected" />')


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("FAUCET_CONFIG_FILE", str(tmp_path / "config" / "faucet.env"))
    monkeypatch.setenv("SESSION_SECRET", "t")
    for k in ("METRICS_TOKEN", "EVENTS_MAX_BYTES", "DB_VACUUM_FREE_PCT",
              "INDEXER_PROBE_TIMEOUT", "INDEXER_PROBE_CONCURRENCY"):
        monkeypatch.delenv(k, raising=False)
    mods = {}
    for m in ("config", "db", "search", "indexerhealth", "maintenance", "metrics",
              "auth", "auth_routes", "app"):
        mods[m] = importlib.reload(importlib.import_module("faucet." + m))
    mods["db"].init()
    return SimpleNamespace(tmp=tmp_path, **mods)


def _admin(env):
    from fastapi.testclient import TestClient
    c = TestClient(env.app.app, follow_redirects=False)
    c.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    c.headers.update({"X-CSRF-Token": c.cookies.get("faucet_csrf") or ""})
    return c


# ── M1 ──────────────────────────────────────────────────────────────────────

def test_jackett_error_body_becomes_the_message(env, monkeypatch):
    class R:
        status_code, reason, content = 400, "Bad Request", JACKETT_400
    monkeypatch.setattr(env.search.requests, "get", lambda *a, **k: R())
    with pytest.raises(env.search.SearchError) as ei:
        env.search.search("http://j", "key123456", "eztv", "x", "all", 5)
    assert str(ei.value) == ("HTTP 400: eztv: Challenge detected but FlareSolverr "
                             "is not configured")


def test_non_torznab_error_body_falls_back_to_reason(env, monkeypatch):
    class R:
        status_code, reason, content = 502, "Bad Gateway", b"<html>nginx</html>"
    monkeypatch.setattr(env.search.requests, "get", lambda *a, **k: R())
    with pytest.raises(env.search.SearchError, match="HTTP 502: Bad Gateway"):
        env.search.search("http://j", "key123456", "all", "x", "all", 5)


# ── M2 ──────────────────────────────────────────────────────────────────────

def test_probe_uses_long_timeout_and_two_workers(env, monkeypatch):
    monkeypatch.setattr(env.config.config, "jackett_url", "http://j")
    monkeypatch.setattr(env.config.config, "jackett_api_key", "key123456")
    monkeypatch.setattr(env.search, "indexers", lambda *a, **k: [
        {"id": f"i{n}", "name": f"I{n}"} for n in range(5)])
    seen, live, peak = [], [0], [0]
    lock = threading.Lock()

    def search(url, key, ix, q, cat, lim, timeout, record=True):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        seen.append(timeout)
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return []
    monkeypatch.setattr(env.search, "search", search)
    env.indexerhealth.probe()
    assert set(seen) == {60} and peak[0] <= 2


def test_probe_runs_in_background_once(env, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(env.indexerhealth, "probe", lambda: gate.wait(5))
    assert env.indexerhealth.probe_async() is True
    assert env.indexerhealth.probing() is True
    assert env.indexerhealth.probe_async() is False
    gate.set()
    for _ in range(100):
        if not env.indexerhealth.probing():
            break
        time.sleep(0.02)
    assert env.indexerhealth.probing() is False


# ── M3 ──────────────────────────────────────────────────────────────────────

def test_backup_is_consistent_and_strips_credentials(env):
    admin = _admin(env)
    env.db.set_setting("_tmdbcache:x", {"exp": time.time() + 999, "data": "x" * 1000})
    env.db.set_setting("default_language", "en")
    r = admin.get("/api/settings/backup")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.sqlite3")
    assert "attachment" in r.headers["content-disposition"]
    out = env.tmp / "b.db"
    out.write_bytes(r.content)
    c = sqlite3.connect(out)
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM settings WHERE key LIKE '_tmdbcache:%'").fetchone()[0] == 0
    assert c.execute("SELECT value FROM settings WHERE key='default_language'").fetchone()[0] == '"en"'
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    import tempfile
    from pathlib import Path
    assert not any(Path(tempfile.gettempdir()).glob("faucet-backup-*"))  # temp copy removed


def test_prune_tmdb_cache_drops_only_expired(env):
    env.db.set_setting("_tmdbcache:old", {"exp": time.time() - 1, "data": 1})
    env.db.set_setting("_tmdbcache:new", {"exp": time.time() + 999, "data": 1})
    env.db.set_setting("tmdb_key", "abc")
    assert env.maintenance.prune_tmdb_cache() == 1
    assert env.db.get_setting("_tmdbcache:new") is not None
    assert env.db.get_setting("tmdb_key") == "abc"


def test_vacuum_when_mostly_free_and_at_most_daily(env):
    with env.db.connect() as c:
        c.execute("CREATE TABLE junk (b BLOB)")
        c.executemany("INSERT INTO junk VALUES (?)", [(b"x" * 4000,) for _ in range(500)])
    with env.db.connect() as c:
        c.execute("DROP TABLE junk")
    assert env.maintenance.db_stats()["free_pct"] > 50
    before = env.maintenance.db_stats()["bytes"]
    assert env.maintenance.maybe_vacuum() is True
    assert env.maintenance.db_stats()["bytes"] < before
    assert env.maintenance.maybe_vacuum() is False             # once a day


def test_events_rotate_and_tail(env, monkeypatch):
    p = env.tmp / "config" / "events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps({"event": "e", "n": i}) + "\n" for i in range(2000)))
    tail = env.maintenance.tail_events(5)
    assert [e["n"] for e in tail] == [1999, 1998, 1997, 1996, 1995]
    monkeypatch.setenv("EVENTS_MAX_BYTES", "1000")
    assert env.maintenance.rotate_events() is True
    assert not p.exists() and (p.parent / "events.jsonl.1").exists()
    assert env.maintenance.tail_events(5) == []


# ── M4 ──────────────────────────────────────────────────────────────────────

def test_metrics_auth(env, monkeypatch):
    from fastapi.testclient import TestClient
    anon = TestClient(env.app.app, follow_redirects=False)
    assert anon.get("/metrics").status_code == 404             # not enabled
    monkeypatch.setenv("METRICS_TOKEN", "s3cret-token")
    assert anon.get("/metrics").status_code == 401
    assert anon.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = anon.get("/metrics", headers={"Authorization": "Bearer s3cret-token"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    monkeypatch.delenv("METRICS_TOKEN")
    assert _admin(env).get("/metrics").status_code == 200       # admin session


def test_metrics_content(env, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "s3cret-token")
    with env.db.connect() as c:
        c.execute("INSERT INTO wanted (kind,series_id,season,episode,status,reason) "
                  "VALUES ('episode',1,1,1,'wanted','missing')")
    env.db.set_setting(env.indexerhealth.PROBE_KEY, {"ts": "2026-10-10T03:00:00", "indexers": [
        {"id": "eztv", "name": "EZTV", "ok": True, "ms": 46},
        {"id": "x", "name": 'Odd "Name"', "ok": False, "ms": 9}]})
    from fastapi.testclient import TestClient
    body = TestClient(env.app.app).get(
        "/metrics", headers={"Authorization": "Bearer s3cret-token"}).text
    assert 'faucet_wanted{status="wanted",reason="missing"} 1' in body
    assert 'faucet_indexer_up{indexer="EZTV"} 1' in body
    assert 'faucet_indexer_up{indexer="Odd \\"Name\\""} 0' in body
    assert "# TYPE faucet_searches_24h gauge" in body
    for line in body.splitlines():                              # every sample parses
        if line and not line.startswith("#"):
            float(line.rsplit(" ", 1)[1])


# ── M5 ──────────────────────────────────────────────────────────────────────

def test_sweep_logs_a_leftover_once(tmp_path, monkeypatch, caplog):
    (tmp_path / "complete" / "Old.Release").mkdir(parents=True)
    (tmp_path / "complete" / "Old.Release" / "info.nfo").write_text("x")
    monkeypatch.setenv("COMPLETE_DIR", str(tmp_path / "complete"))
    sweep = importlib.reload(importlib.import_module("faucet.sweep"))
    caplog.set_level(logging.INFO, logger="faucet.sweep")
    sweep.run()
    sweep.run()
    assert sum("SKIP (no video): Old.Release" in r.getMessage()
               for r in caplog.records if r.levelno == logging.INFO) == 1


# ── M6 ──────────────────────────────────────────────────────────────────────

def test_telegram_and_ntfy_payloads(monkeypatch):
    from faucet import notify as N
    sent = []

    class R:
        def raise_for_status(self): pass

    monkeypatch.setattr(N.requests, "post", lambda url, **k: sent.append((url, k)) or R())
    N._telegram("telegram://123:ABC/42", "Grabbed · Show", "Show_S01E01_<x>*")
    url, k = sent[-1]
    assert k["json"]["parse_mode"] == "HTML"
    assert k["json"]["text"] == "<b>Grabbed · Show</b>\nShow_S01E01_&lt;x&gt;*"
    N._ntfy("ntfy://ntfy.sh/topic", "Grabbed — “Show”", "body")
    url, k = sent[-1]
    assert url == "https://ntfy.sh/topic" and k["params"] == {"title": "Grabbed — “Show”"}
    assert "headers" not in k

"""1.6.1: Jackett download links that redirect to a magnet.

Jackett's /dl/ link for a public tracker answers 302 Location: magnet:?...
requests' redirect handling raised InvalidSchema on that Location, so the add
failed — and the hunter retried it every tick, writing 38k grab_failed history
rows (50 MB) in a month.
"""
from __future__ import annotations

import base64
import importlib
from datetime import datetime, timedelta


import pytest

MAGNET = "magnet:?xt=urn:btih:34D6CD0FC95CADC23DE88075CEFE96B0DED9F52E&dn=Show%20S01E01"


class _Resp:
    def __init__(self, status=200, body=b"", location=None):
        self.status_code = status
        self.content = body
        self.headers = {"location": location} if location else {}
        self.is_redirect = status in (301, 302, 303, 307, 308) and bool(location)


@pytest.fixture
def tr(monkeypatch):
    from faucet.clients import transmission as T
    return T


def _route(monkeypatch, T, table):
    calls = []

    def get(url, **k):
        assert k.get("allow_redirects") is False
        calls.append(url)
        return table[url]
    monkeypatch.setattr(T.fetch_torrent.__globals__["requests"], "get", get)
    return calls


def test_redirect_to_magnet(tr, monkeypatch):
    calls = _route(monkeypatch, tr, {
        "http://jackett:9117/dl/eztv/?jackett_apikey=k&path=x": _Resp(302, location=MAGNET)})
    assert tr.fetch_torrent("http://jackett:9117/dl/eztv/?jackett_apikey=k&path=x", 10) \
        == ("magnet", MAGNET)
    assert len(calls) == 1


def test_redirect_chain_to_torrent_file(tr, monkeypatch):
    torrent = b"d8:announce3:urle"
    _route(monkeypatch, tr, {
        "http://j/dl/a": _Resp(302, location="/dl/b"),
        "http://j/dl/b": _Resp(301, location="https://tracker.example/t.torrent"),
        "https://tracker.example/t.torrent": _Resp(200, torrent)})
    kind, val = tr.fetch_torrent("http://j/dl/a", 10)
    assert kind == "metainfo" and base64.b64decode(val) == torrent


def test_body_that_is_a_magnet(tr, monkeypatch):
    _route(monkeypatch, tr, {"http://j/dl/c": _Resp(200, MAGNET.encode() + b"\n")})
    assert tr.fetch_torrent("http://j/dl/c", 10) == ("magnet", MAGNET)


def test_error_page_is_not_sent_as_a_torrent(tr, monkeypatch):
    _route(monkeypatch, tr, {"http://j/dl/d": _Resp(200, b"<html>Cloudflare</html>")})
    with pytest.raises(tr.DownloadClientError, match="not a torrent"):
        tr.fetch_torrent("http://j/dl/d", 10)


def test_redirect_loop_is_bounded(tr, monkeypatch):
    _route(monkeypatch, tr, {"http://j/x": _Resp(302, location="http://j/x")})
    with pytest.raises(tr.DownloadClientError, match="too many redirects"):
        tr.fetch_torrent("http://j/x", 10)


def test_client_add_hands_transmission_the_magnet(tr, monkeypatch):
    _route(monkeypatch, tr, {"http://j/dl/e": _Resp(302, location=MAGNET)})
    sent = {}
    c = tr.TransmissionClient.__new__(tr.TransmissionClient)
    c.timeout = 10

    def rpc(method, args):
        sent[method] = args
        return {"torrent-added": {"id": 7, "name": "Show S01E01"}}
    c._rpc = rpc
    res = c.add("http://j/dl/e")
    assert sent["torrent-add"]["filename"] == MAGNET and "metainfo" not in sent["torrent-add"]
    assert res.id == "7"


# ── the hunter backs off a release the client refuses ───────────────────────

def test_failed_add_backs_off_the_want(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "lib"))
    monkeypatch.setenv("JACKETT_API_KEY", "k")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db")}
    mods["db"].init()
    for m in ("huntlog", "library", "wants", "series", "scheduler"):
        mods[m] = importlib.reload(importlib.import_module("faucet." + m))
    db, S, SCH = mods["db"], mods["series"], mods["scheduler"]
    with db.connect() as c:
        sid = c.execute("INSERT INTO series (tmdb_id,title,monitored) VALUES (1,'Show',1)").lastrowid
        c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) "
                  "VALUES (?,1,1,'E','2020-01-01')", (sid,))
    S.reconcile(sid)

    class Refuses:
        def list_transfers(self):
            return []

        def add(self, *a, **k):
            from faucet.clients import DownloadClientError
            raise DownloadClientError("couldn't fetch torrent: boom")
    from faucet.search import parse_badges
    title = "Show.S01E01.1080p.WEB-DL.x264-GRP"
    monkeypatch.setattr(SCH, "make_client", lambda *a, **k: Refuses())
    monkeypatch.setattr(SCH.searchmod, "search", lambda *a, **k: [
        {"title": title, "href": "http://j/dl/x", "seeders": 50, "size": 1,
         "badges": parse_badges(title)}])
    SCH.hunt_wanted()
    SCH.hunt_wanted()
    with db.connect() as c:
        fails = c.execute("SELECT COUNT(*) FROM history WHERE event='grab_failed'").fetchone()[0]
        w = dict(c.execute("SELECT * FROM wanted").fetchone())
    assert fails == 1 and w["search_count"] == 1 and w["next_search"]


# ── history retention + detail cap ──────────────────────────────────────────

def test_history_retention_and_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    for k in ("HISTORY_DAYS", "HISTORY_FAILED_DAYS"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(importlib.import_module("faucet.config"))
    db = importlib.reload(importlib.import_module("faucet.db"))
    db.init()
    maint = importlib.reload(importlib.import_module("faucet.maintenance"))
    db.add_history("grab_failed", "x" * 900, "y" * 5000)
    with db.connect() as c:
        t, d = c.execute("SELECT length(title), length(detail) FROM history").fetchone()
    assert (t, d) == (500, 500)

    def at(days, event):
        ts = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with db.connect() as c:
            c.execute("INSERT INTO history (ts, event, title) VALUES (?,?,?)", (ts, event, event))
    at(20, "grab_failed")     # past the 14-day failure window
    at(20, "completed")       # well inside the 365-day window
    at(400, "completed")      # past it
    assert maint.prune_history() == 2
    with db.connect() as c:
        left = sorted(r[0] for r in c.execute("SELECT event FROM history"))
    assert left == ["completed", "grab_failed"]   # the 20-day completed + today's failure



def test_deluge_resolves_links_itself(tr, monkeypatch):
    from faucet.clients import deluge as D
    _route(monkeypatch, tr, {"http://j/dl/f": _Resp(302, location=MAGNET)})
    calls = []
    c = D.DelugeClient.__new__(D.DelugeClient)
    c.timeout = 10
    c._ensure_auth = lambda: None
    hashes = iter([[], ["abc"]])
    c._hashes = lambda: next(hashes)
    c._call = lambda m, a: calls.append((m, a)) or {"name": "Show"}
    c.add("http://j/dl/f")
    assert calls[0] == ("core.add_torrent_magnet", [MAGNET, {}])

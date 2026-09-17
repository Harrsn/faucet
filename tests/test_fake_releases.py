"""Fake-release defenses, the air-date delay, and the config/hunter fixes
that the S29E01 incident exposed.

The bait release that started this: 'South Park S29E01 South America 1080p
WEB-DL x265 NTb.exe', grabbed minutes before the episode aired; a second copy
arrived as a bare magnet whose display name had no extension at all.
"""
from __future__ import annotations

import importlib
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from faucet.clients.base import AddResult, DownloadClientError, Transfer, TransferFile

MB = 1024 * 1024
BAIT = "South Park S29E01 South America 1080p WEB-DL x265 NTb.exe"
MAGNET_NAME = "South+Park+S29E01+South+America+1080p+WEB-DL+x265+NTb"


def mk(path, mb, fill=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fill * int(mb * MB))
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    dl = tmp_path / "downloads" / "complete"
    lib.mkdir()
    dl.mkdir(parents=True)
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("FAUCET_CONFIG_FILE", str(tmp_path / "config" / "faucet.env"))
    monkeypatch.setenv("LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("JACKETT_URL", "http://jackett:9117")
    monkeypatch.setenv("JACKETT_API_KEY", "k")
    monkeypatch.setenv("HUNT_MAX_PER_RUN", "5")
    monkeypatch.setenv("HUNT_MAX_ACTIVE", "10")
    monkeypatch.setenv("MEDIASORT_MODE", "move")
    monkeypatch.setenv("MEDIASORT_MIN_MB", "1")
    monkeypatch.setenv("MEDIASORT_LOG", str(tmp_path / "sort.log"))
    monkeypatch.setenv("NOTIFY_URLS", "")
    for k in ("REMOVE_ON_COMPLETE", "QUARANTINE_DIR", "AIR_DELAY_DAYS",
              "BLOCK_EXECUTABLE_RELEASES", "HUNT_UPGRADES"):
        monkeypatch.delenv(k, raising=False)
    mods = {}
    for name in ("config", "db"):
        mods[name] = importlib.reload(importlib.import_module(f"faucet.{name}"))
    mods["db"].init()
    for name in ("safety", "search", "library", "wants", "series", "movies",
                 "stalls", "scheduler", "sort"):
        mods[name] = importlib.reload(importlib.import_module(f"faucet.{name}"))
    return SimpleNamespace(tmp=tmp_path, lib=lib, dl=dl, **mods)


class FakeClient:
    name = "transmission"

    def __init__(self, transfers=(), files=None, pause_error=None):
        self.transfers = list(transfers)
        self.file_map = dict(files or {})
        self.paused, self.added = [], []
        self.pause_error = pause_error

    def test(self):
        return True

    def list_transfers(self):
        return list(self.transfers)

    def files(self, tid):
        return [TransferFile(p.rsplit("/", 1)[-1], p, s, 0.0) for p, s in
                self.file_map.get(str(tid), [])]

    def pause(self, tid):
        if self.pause_error:
            raise DownloadClientError(self.pause_error)
        self.paused.append(str(tid))

    def resume(self, tid):
        pass

    def remove(self, tid, delete_data=False):
        pass

    def add(self, href, *a, **k):
        self.added.append(href)
        return AddResult(id="9", name="x")


def xfer(tid, name, status="downloading", pct=8.0):
    return Transfer(str(tid), name, pct, 1_000_000, 0, status, 60, 0.0, 1_000 * MB)


def _series(db, title, eps, profile_id=None):
    with db.connect() as c:
        sid = c.execute("INSERT INTO series (tmdb_id,title,monitored,profile_id) "
                        "VALUES (?,?,1,?)", (abs(hash(title)) % 10**6, title,
                                             profile_id)).lastrowid
        for s, e, air in eps:
            c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) "
                      "VALUES (?,?,?,?,?)", (sid, s, e, f"E{e}", air))
    return sid


def _history(db, event):
    return [r for r in db.recent_history(200) if r["event"] == event]


def _release(title, href="magnet:x"):
    from faucet.search import parse_badges
    return {"title": title, "href": href, "seeders": 90, "size": MB * 900,
            "badges": parse_badges(title)}


TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


# ── layer 1: release-name filter ─────────────────────────────────────────────

@pytest.mark.parametrize("name,bad", [
    (BAIT, True),
    ("Some.Movie.2019.1080p.WEB-DL.x264.scr", True),
    ("Show S01E01 [1080p].exe ]", True),
    ("Tool.lnk", True),
    ("South.Park.S29E01.1080p.WEB.h264-ETHEL", False),
    ("[www.torrenting.com] - Show S01E01 720p", False),       # .com watermark
    ("Movie.2019.1080p.mkv", False),
    ("Game.Title.PS4.pkg", False),                              # console, not Windows
    (MAGNET_NAME, False),                                       # can't tell from the name
])
def test_executable_release_names(env, name, bad):
    assert env.safety.is_executable_name(name) is bad


TORZNAB = """<?xml version="1.0"?><rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
<item><title>{a}</title><link>magnet:?xt=a</link><size>1000</size>
  <torznab:attr name="seeders" value="900"/></item>
<item><title>{b}</title><link>magnet:?xt=b</link><size>1000</size>
  <torznab:attr name="seeders" value="12"/></item>
</channel></rss>"""


class _Resp:
    def __init__(self, text):
        self.content = text.encode()

    def raise_for_status(self):
        pass


def test_search_hides_executable_releases(env, monkeypatch):
    xml = TORZNAB.format(a=BAIT, b="South.Park.S29E01.1080p.WEB.h264-ETHEL")
    monkeypatch.setattr(env.search.requests, "get", lambda *a, **k: _Resp(xml))
    res = env.search.search("http://j", "k", "all", "south park", "all", 50)
    assert [r["title"] for r in res] == ["South.Park.S29E01.1080p.WEB.h264-ETHEL"]
    assert res.hidden == 1

    monkeypatch.setenv("BLOCK_EXECUTABLE_RELEASES", "0")
    res = env.search.search("http://j", "k", "all", "south park", "all", 50)
    assert len(res) == 2 and res.hidden == 0


def test_torznab_error_document_is_an_error(env, monkeypatch):
    """A wrong API key comes back as HTTP 200 + <error>; it used to look like
    'no results'."""
    xml = '<?xml version="1.0"?><error code="100" description="Invalid API Key" />'
    monkeypatch.setattr(env.search.requests, "get", lambda *a, **k: _Resp(xml))
    with pytest.raises(env.search.SearchError, match="Invalid API Key"):
        env.search.search("http://j", "bad", "all", "x", "all", 5)


# ── layer 2: payload check on live transfers ─────────────────────────────────

@pytest.mark.parametrize("paths,bad", [
    (["South Park S29E01.exe"], True),
    (["Release/setup.exe", "Release/readme.txt"], True),
    (["Release/Sample/sample.mkv", "Release/player.exe"], True),   # a sample isn't content
    (["Release/ep.mkv", "Release/codec.exe"], False),              # real video present
    (["Release/ep.mkv"], False),
    ([], False),
])
def test_payload_problem(env, paths, bad):
    assert bool(env.safety.payload_problem(paths)) is bad


def test_guard_pauses_and_flags_bait_then_respects_resume(env):
    sid = _series(env.db, "South Park", [(29, 1, YESTERDAY)])
    with env.db.connect() as c:
        c.execute("INSERT INTO wanted (kind,series_id,season,episode,title,reason,status) "
                  "VALUES ('episode',?,29,1,'x','missing','grabbed')", (sid,))
    client = FakeClient([xfer(1, BAIT)], {"1": [(BAIT, 1000 * MB)]})

    r = env.safety.check_transfers(client)
    assert client.paused == ["1"]
    assert r["flagged"][0]["flipped"] == 1
    with env.db.connect() as c:
        assert c.execute("SELECT status FROM wanted").fetchone()[0] == "wanted"
    assert "executable payload" in env.safety.flags()["1"]["reason"]
    assert _history(env.db, "suspicious")

    # admin resumes it on purpose: the guard must not fight them
    client.paused.clear()
    env.safety.check_transfers(client)
    assert client.paused == []


def test_guard_waits_for_magnet_metadata(env):
    client = FakeClient([xfer(1, MAGNET_NAME, pct=0.0)], {})
    env.safety.check_transfers(client)
    assert client.paused == [] and env.safety.flags() == {}
    # metadata arrives: the torrent is renamed and its files are known
    client.transfers = [xfer(1, BAIT)]
    client.file_map = {"1": [(BAIT, 1000 * MB)]}
    env.safety.check_transfers(client)
    assert client.paused == ["1"]


def test_guard_leaves_games_and_real_releases_alone(env):
    client = FakeClient(
        [xfer(1, "Some.Game-CODEX"), xfer(2, "South.Park.S29E01.1080p.WEB.h264-ETHEL")],
        {"1": [("Some.Game-CODEX/setup.exe", 5000 * MB)],
         "2": [("South.Park.S29E01.1080p.WEB.h264-ETHEL.mkv", 900 * MB)]})
    r = env.safety.check_transfers(client)
    assert r["checked"] == 2 and client.paused == [] and env.safety.flags() == {}


def test_guard_retries_when_pause_fails(env):
    client = FakeClient([xfer(1, BAIT)], {"1": [(BAIT, 1000 * MB)]}, pause_error="rpc down")
    r = env.safety.check_transfers(client)
    assert r["errors"] and env.safety.flags() == {}
    client.pause_error = None
    env.safety.check_transfers(client)
    assert client.paused == ["1"]


def test_guard_rechecks_reused_ids_and_prunes_gone_ones(env):
    client = FakeClient([xfer(1, "Real.Show.S01E01.1080p")],
                        {"1": [("Real.Show.S01E01.1080p.mkv", 900 * MB)]})
    env.safety.check_transfers(client)
    # Transmission restarted and handed id 1 to a different torrent
    client.transfers = [xfer(1, BAIT)]
    client.file_map = {"1": [(BAIT, 1000 * MB)]}
    env.safety.check_transfers(client)
    assert client.paused == ["1"]
    client.transfers = []
    env.safety.check_transfers(client)
    assert env.safety.flags() == {}


# ── layer 3: sorter backstop ─────────────────────────────────────────────────

def test_sorter_quarantines_and_neutralizes_bait(env):
    rel = env.dl / "South Park S29E01 South America 1080p WEB-DL x265 NTb"
    mk(rel / BAIT, 2)
    assert env.sort.sort_release(rel, dry=False) == env.sort.EXIT_SUSPICIOUS
    parked = env.dl / "_failed" / rel.name
    assert (parked / (BAIT + ".faucet-blocked")).exists()
    assert not list(parked.rglob("*.exe"))
    assert not list(env.lib.rglob("*"))


def test_sorter_single_file_bait(env):
    f = mk(env.dl / BAIT, 2)
    assert env.sort.sort_release(f, dry=False) == env.sort.EXIT_SUSPICIOUS
    assert (env.dl / "_failed" / (BAIT + ".faucet-blocked")).exists()


def test_sorter_leaves_seeding_bait_in_place(env):
    env.sort.MODE = "copy"
    rel = env.dl / "Bait"
    f = mk(rel / BAIT, 2)
    assert env.sort.sort_release(rel, dry=False) == env.sort.EXIT_SUSPICIOUS
    assert f.exists() and not (env.dl / "_failed").exists()


def test_sorter_files_real_release_with_bundled_exe(env):
    rel = env.dl / "South.Park.S29E01.1080p.WEB.h264-ETHEL"
    mk(rel / "South.Park.S29E01.1080p.WEB.h264-ETHEL.mkv", 2)
    mk(rel / "codec-pack.exe", 1)
    assert env.sort.sort_release(rel, dry=False) == env.sort.EXIT_OK
    assert (env.lib / "tvshows" / "South Park" / "Season 29"
            / "South Park - S29E01.mkv").exists()


def test_sorter_games_keep_their_executables(env):
    rel = env.dl / "Some.Game-CODEX"
    mk(rel / "setup.exe", 2)
    mk(rel / "data.bin", 2)
    assert env.sort.sort_release(rel, dry=False) != env.sort.EXIT_SUSPICIOUS


def test_hook_removes_quarantined_bait_and_records_it(env, monkeypatch):
    monkeypatch.setenv("REMOVE_ON_COMPLETE", "1")
    monkeypatch.setenv("FAUCET_PATH", str(env.dl / "x"))
    monkeypatch.setenv("FAUCET_ID", "7")
    from faucet import hook
    importlib.reload(hook)
    removed = []
    monkeypatch.setattr(hook, "make_client", lambda *a, **k: SimpleNamespace(
        remove=lambda tid, d=False: removed.append((tid, d))))
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=5))
    assert hook.main() == 0
    assert removed == [("7", True)]
    assert _history(env.db, "suspicious")


# ── air-date delay ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("delay,air,wanted", [
    (None, TODAY, 0),        # default: day after
    (None, YESTERDAY, 1),
    ("0", TODAY, 1),
    ("2", YESTERDAY, 0),
])
def test_reconcile_respects_air_delay(env, monkeypatch, delay, air, wanted):
    if delay is not None:
        monkeypatch.setenv("AIR_DELAY_DAYS", delay)
    sid = _series(env.db, "South Park", [(29, 1, air)])
    assert env.series.reconcile(sid)["missing"] == wanted


def test_hunter_skips_want_that_aired_too_recently(env, monkeypatch):
    """A want created before the delay existed must not be hunted early."""
    sid = _series(env.db, "South Park", [(29, 1, TODAY), (28, 1, "2025-10-15")])
    with env.db.connect() as c:
        for s in (29, 28):
            c.execute("INSERT INTO wanted (kind,series_id,season,episode,title,reason) "
                      "VALUES ('episode',?,?,1,'x','missing')", (sid, s))
    queries = []
    client = FakeClient()
    monkeypatch.setattr(env.scheduler, "make_client", lambda *a, **k: client)
    monkeypatch.setattr(env.scheduler.searchmod, "search",
                        lambda *a, **k: queries.append(a[3]) or [])
    env.scheduler.hunt_wanted()
    assert queries == ["South Park S28E01"]


# ── hunter / config fixes ────────────────────────────────────────────────────

def test_hunt_short_circuits_without_indexer_key(env, monkeypatch):
    monkeypatch.setenv("JACKETT_API_KEY", "")
    env.config.reload()
    sid = _series(env.db, "Show", [(1, 1, "2020-01-01")])
    env.series.reconcile(sid)
    called = []
    monkeypatch.setattr(env.scheduler.searchmod, "search", lambda *a, **k: called.append(a))
    r = env.scheduler.hunt_wanted()
    assert r["skipped_reason"] == "indexer not configured" and called == []


def test_scheduler_sees_key_saved_after_startup(env, monkeypatch):
    """F15: the key saved in Settings reached search but never the hunter."""
    monkeypatch.setenv("JACKETT_API_KEY", "")
    env.config.reload()
    importlib.reload(env.scheduler)                # "process start" with no key
    sid = _series(env.db, "Show", [(1, 1, "2020-01-01")])
    env.series.reconcile(sid)
    keys = []
    monkeypatch.setattr(env.scheduler, "make_client", lambda *a, **k: FakeClient())
    monkeypatch.setattr(env.scheduler.searchmod, "search",
                        lambda *a, **k: keys.append(a[1]) or [])
    env.config.save({"JACKETT_API_KEY": "saved-in-settings"})   # the Settings panel path
    env.scheduler.hunt_wanted()
    assert keys == ["saved-in-settings"]


def test_hunt_add_failure_is_recorded(env, monkeypatch, caplog):
    sid = _series(env.db, "Show", [(1, 1, "2020-01-01")])
    env.series.reconcile(sid)

    class Refuses(FakeClient):
        def add(self, href, *a, **k):
            raise DownloadClientError("disk full")

    monkeypatch.setattr(env.scheduler, "make_client", lambda *a, **k: Refuses())
    monkeypatch.setattr(env.scheduler.searchmod, "search",
                        lambda *a, **k: [_release("Show.S01E01.1080p.WEB-DL.x264-GRP")])
    with caplog.at_level("WARNING", logger="faucet.scheduler"):
        env.scheduler.hunt_wanted()
    assert "disk full" in caplog.text
    assert "disk full" in _history(env.db, "grab_failed")[0]["detail"]


def test_hunt_search_failures_are_summarized(env, monkeypatch, caplog):
    sid = _series(env.db, "Show", [(1, e, "2020-01-01") for e in (1, 2, 3)])
    env.series.reconcile(sid)
    monkeypatch.setattr(env.scheduler, "make_client", lambda *a, **k: FakeClient())

    def boom(*a, **k):
        raise env.search.SearchError("Indexer query failed: timeout")

    monkeypatch.setattr(env.scheduler.searchmod, "search", boom)
    with caplog.at_level("WARNING", logger="faucet.scheduler"):
        env.scheduler.hunt_wanted()
    lines = [r for r in caplog.records if "search(es) failed" in r.getMessage()]
    assert len(lines) == 1 and "3 search(es)" in lines[0].getMessage()


# ── API / UI contract ────────────────────────────────────────────────────────

@pytest.fixture
def api(env, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    from faucet import auth as authmod
    importlib.reload(authmod)
    from faucet import app as appmod
    importlib.reload(appmod)
    client = FakeClient([xfer(1, BAIT), xfer(2, "Real.Show.S01E01.1080p")],
                        {"1": [(BAIT, 1000 * MB)],
                         "2": [("Real.Show.S01E01.1080p.mkv", 900 * MB)]})
    monkeypatch.setattr(appmod, "client", lambda: client)
    from fastapi.testclient import TestClient
    tc = TestClient(appmod.app)
    tc.post("/api/auth/register", json={"username": "admin", "password": "supersecret123"})
    tc.post("/api/auth/login", json={"username": "admin", "password": "supersecret123"})
    tc.headers.update({"X-CSRF-Token": tc.cookies.get("faucet_csrf") or ""})
    return SimpleNamespace(http=tc, app=appmod, client=client)


def test_transfers_carry_the_flag(env, api):
    env.safety.check_transfers(api.client)
    xs = {x["id"]: x for x in api.http.get("/api/transfers").json()["transfers"]}
    assert "executable payload" in xs["1"]["flag"] and xs["2"]["flag"] is None
    warnings = api.http.get("/api/dashboard").json()["warnings"]
    assert any("suspicious download" in w for w in warnings)


def test_missing_key_is_explained_everywhere(env, api, monkeypatch):
    monkeypatch.setenv("JACKETT_API_KEY", "")
    env.config.reload()
    r = api.http.get("/api/search?q=south+park")
    assert r.status_code == 503 and "API key" in r.json()["detail"]
    d = api.http.get("/api/dashboard").json()
    assert d["indexer"]["configured"] is False
    assert any("Jackett API key is not set" in w for w in d["warnings"])
    assert any("Jackett API key" in w for w in api.http.get("/health").json()["warnings"])
    assert api.http.get("/api/settings").json()["env"]["JACKETT_API_KEY_SET"] is False


def test_jackett_key_and_air_delay_save_from_settings(env, api):
    r = api.http.patch("/api/settings", json={"values": {
        "JACKETT_API_KEY": "new-key", "AIR_DELAY_DAYS": "2"}}).json()
    assert r["status"] == "ok" and not r["warnings"]
    env_view = api.http.get("/api/settings").json()["env"]
    assert env_view["JACKETT_API_KEY_SET"] is True and env_view["AIR_DELAY_DAYS"] == "2"
    assert "new-key" not in json.dumps(env_view)          # never echoed back
    assert env.config.config.jackett_api_key == "new-key"

    r = api.http.patch("/api/settings", json={"values": {"AIR_DELAY_DAYS": "soon"}}).json()
    assert "AIR_DELAY_DAYS" in r["warnings"][0]
    assert api.http.get("/api/settings").json()["env"]["AIR_DELAY_DAYS"] == "2"


def test_search_reports_hidden_count(env, api, monkeypatch):
    res = env.search.Results([_release("South.Park.S29E01.1080p.WEB.h264-ETHEL")])
    res.hidden = 3
    monkeypatch.setattr(api.app.searchmod, "search", lambda *a, **k: res)
    body = api.http.get("/api/search?q=south+park").json()
    assert body["total"] == 1 and body["hidden"] == 3

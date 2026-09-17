"""Upgrade and wanted-table correctness.

F14: an upgrade that the sorter files as 'Title (Year).mkv' carries no quality
     tag, so the scanner kept the old tagged file as "best" and the upgrade
     want re-grabbed every GRAB_RETRY_HOURS forever.
F19: prod's `wanted` table still has the old title-keyed UNIQUE constraint, and
     wants were created select-then-insert.
F20: movie wants (NULL season/episode) were never unique.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

MB = 1024 * 1024


def mk(path: Path, mb: float, fill: bytes = b"x") -> Path:
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
    monkeypatch.setenv("LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("JACKETT_API_KEY", "k")
    monkeypatch.setenv("HUNT_MAX_PER_RUN", "5")
    monkeypatch.setenv("HUNT_MAX_ACTIVE", "10")
    monkeypatch.setenv("MEDIASORT_MODE", "move")
    monkeypatch.setenv("MEDIASORT_MIN_MB", "1")
    monkeypatch.setenv("MEDIASORT_LOG", str(tmp_path / "sort.log"))
    for k in ("REMOVE_ON_COMPLETE", "QUARANTINE_DIR", "HUNT_UPGRADES"):
        monkeypatch.delenv(k, raising=False)
    mods = {}
    for name in ("config", "db"):
        mods[name] = importlib.reload(importlib.import_module(f"faucet.{name}"))
    mods["db"].init()
    for name in ("quality", "library", "wants", "series", "movies", "scheduler", "sort"):
        mods[name] = importlib.reload(importlib.import_module(f"faucet.{name}"))
    monkeypatch.setattr(mods["library"], "MIN_SIZE", 1 * MB)
    return SimpleNamespace(tmp=tmp_path, lib=lib, dl=dl, db=mods["db"], L=mods["library"],
                           W=mods["wants"], S=mods["series"], M=mods["movies"],
                           SCH=mods["scheduler"], SORT=mods["sort"])


def _profile(db, resolutions):
    with db.connect() as c:
        return c.execute("INSERT INTO profiles (name,min_seeders,resolutions,sources) "
                         "VALUES ('P',0,?,?)",
                         (json.dumps(resolutions), json.dumps(["WEB-DL", "BluRay"]))).lastrowid


def _sort_in(env, release: str, mb: float = 3):
    """File a release through the real sorter, exactly as the hook would."""
    rel = env.dl / release
    mk(rel / f"{release}.mkv", mb, b"N")
    assert env.SORT.sort_release(rel, dry=False) == env.SORT.EXIT_OK


def _count_wants(db, **where):
    q = " AND ".join(f"{k}=?" for k in where) or "1=1"
    with db.connect() as c:
        return c.execute(f"SELECT COUNT(*) AS n FROM wanted WHERE {q}",
                         tuple(where.values())).fetchone()["n"]


def _series(db, title, profile_id, eps=((1, 1),)):
    with db.connect() as c:
        sid = c.execute("INSERT INTO series (tmdb_id,title,monitored,profile_id) "
                        "VALUES (?,?,1,?)", (abs(hash(title)) % 10**6, title,
                                             profile_id)).lastrowid
        for s, e in eps:
            c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) "
                      "VALUES (?,?,?,?,'2020-01-01')", (sid, s, e, f"E{e}"))
    return sid


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


# ── F14: upgrades clear once the sorter files the better copy ────────────────

def test_movie_upgrade_clears_when_sorter_files_better_copy(env):
    mk(env.lib / "movies" / "Rags (2012)" / "Rags.2012.720p.WEB-DL.x264.mkv", 2)
    env.L.scan()
    mid = env.M.add_movie(12, "Rags", 2012, None, _profile(env.db, ["1080p"]))
    assert env.M.reconcile(mid)["upgrade"] is True

    _sort_in(env, "Rags.2012.1080p.WEB-DL.x264-GRP")
    canonical = env.lib / "movies" / "Rags (2012)" / "Rags (2012).mkv"
    assert canonical.exists()

    stats = env.L.scan()
    assert stats.get("superseded") == 1
    r = env.M.reconcile(mid)
    assert r["have"] is True and r["upgrade"] is False
    assert _count_wants(env.db, kind="movie", series_id=mid) == 0
    for force in (False, True, True):             # stable across rescans
        env.L.scan(force=force)
        with env.db.connect() as c:
            row = c.execute("SELECT path, quality FROM library_movies").fetchone()
        assert row["path"] == str(canonical) and row["quality"] == "1080p"


def test_cam_upgrade_clears_when_real_source_lands(env):
    mk(env.lib / "movies" / "Zootopia 2 (2025)" / "Zootopia 2 2025 1080p TS EN-RGB.mp4", 2)
    env.L.scan()
    mid = env.M.add_movie(11, "Zootopia 2", 2025, None, _profile(env.db, ["1080p"]))
    assert env.M.reconcile(mid)["upgrade"] is True

    _sort_in(env, "Zootopia.2.2025.1080p.WEB-DL.x264-GRP")
    env.L.scan()
    assert env.M.reconcile(mid)["upgrade"] is False
    with env.db.connect() as c:
        row = c.execute("SELECT path, source FROM library_movies").fetchone()
    assert row["path"].endswith("Zootopia 2 (2025).mkv") and row["source"] is None


def test_unrecorded_canonical_file_never_triggers_an_upgrade(env):
    """A pre-existing Faucet-sorted file has no quality anywhere. Unknown is
    left alone rather than re-downloaded."""
    mk(env.lib / "movies" / "Rags (2012)" / "Rags (2012).mkv", 2)
    env.L.scan()
    mid = env.M.add_movie(12, "Rags", 2012, None, _profile(env.db, ["1080p"]))
    assert env.M.reconcile(mid) == {"have": True, "upgrade": False}


@pytest.mark.parametrize("tagged_first", [True, False])
def test_episode_best_file_wins_regardless_of_order(env, tagged_first):
    season = env.lib / "tvshows" / "Show" / "Season 01"
    if tagged_first:
        mk(season / "Show.S01E01.720p.WEB-DL.mkv", 2)
        env.L.scan()
        _sort_in(env, "Show.S01E01.1080p.WEB-DL-GRP")
    else:
        _sort_in(env, "Show.S01E01.1080p.WEB-DL-GRP")
        env.L.scan()
        mk(season / "Show.S01E01.720p.WEB-DL.mkv", 2)
    for force in (False, True, False, True):
        env.L.scan(force=force)
        owned = env.L.have_episode("Show", 1, 1)
        assert owned["quality"] == "1080p"
        assert owned["path"].endswith("Show - S01E01.mkv")
    sid = _series(env.db, "Show", _profile(env.db, ["1080p"]))
    r = env.S.reconcile(sid)
    assert r["upgrades"] == 0 and r["have"] == 1
    assert _count_wants(env.db, series_id=sid) == 0


def test_row_saved_before_record_is_rescanned(env):
    f = mk(env.lib / "movies" / "Rags (2012)" / "Rags (2012).mkv", 2)
    env.L.scan()
    with env.db.connect() as c:
        assert c.execute("SELECT quality FROM library_movies").fetchone()["quality"] is None
        c.execute("INSERT INTO library_files (path, quality, size) VALUES (?,?,?)",
                  (str(f), "2160p", f.stat().st_size))
    stats = env.L.scan()                          # not forced
    assert stats["skipped"] == 0
    with env.db.connect() as c:
        assert c.execute("SELECT quality FROM library_movies").fetchone()["quality"] == "2160p"
    assert env.L.scan()["skipped"] == 1           # and then it settles


def test_record_for_a_replaced_file_is_ignored(env):
    f = mk(env.lib / "movies" / "Rags (2012)" / "Rags (2012).mkv", 2)
    with env.db.connect() as c:
        c.execute("INSERT INTO library_files (path, quality, size) VALUES (?,?,?)",
                  (str(f), "2160p", 12345))
    env.L.scan()
    with env.db.connect() as c:
        assert c.execute("SELECT quality FROM library_movies").fetchone()["quality"] is None


def test_records_for_deleted_files_are_pruned(env):
    _sort_in(env, "Rags.2012.1080p.WEB-DL.x264-GRP")
    env.L.scan()
    canonical = env.lib / "movies" / "Rags (2012)" / "Rags (2012).mkv"
    with env.db.connect() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM library_files").fetchone()["n"] == 1
    canonical.unlink()
    env.L.scan()
    with env.db.connect() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM library_files").fetchone()["n"] == 0


# ── F14: the hunter only grabs releases that beat the owned file ─────────────

def _movie_upgrade_setup(env):
    mk(env.lib / "movies" / "Rags (2012)" / "Rags.2012.720p.WEB-DL.x264.mkv", 2)
    env.L.scan()
    mid = env.M.add_movie(12, "Rags", 2012, None, _profile(env.db, ["1080p", "720p"]))
    with env.db.connect() as c:          # target 1080p is what flags the upgrade
        c.execute("UPDATE profiles SET resolutions=? WHERE id=(SELECT profile_id "
                  "FROM movies WHERE id=?)", (json.dumps(["1080p", "720p"]), mid))
    assert env.M.reconcile(mid)["upgrade"] is True
    return mid


def test_hunter_skips_same_quality_upgrade(env):
    mid = _movie_upgrade_setup(env)
    client = _Client()
    env.SCH.make_client = lambda *a, **k: client
    env.SCH.searchmod.search = lambda *a, **k: [
        _release("Rags.2012.720p.BluRay.x264-OTHER", "magnet:same")]
    r = env.SCH.hunt_wanted()
    assert client.added == [] and r["grabbed"] == 0
    assert "no release better than owned 720p" in r["details"][0]["error"]
    assert _count_wants(env.db, kind="movie", series_id=mid, status="wanted") == 1


def test_hunter_grabs_a_real_upgrade(env):
    _movie_upgrade_setup(env)
    client = _Client()
    env.SCH.make_client = lambda *a, **k: client
    env.SCH.searchmod.search = lambda *a, **k: [
        _release("Rags.2012.1080p.WEB-DL.x264-GRP", "magnet:better"),
        _release("Rags.2012.720p.BluRay.x264-OTHER", "magnet:same")]
    env.SCH.hunt_wanted()
    assert client.added == ["magnet:better"]


def test_hunter_cam_upgrade_needs_a_real_source(env):
    mk(env.lib / "movies" / "Zootopia 2 (2025)" / "Zootopia 2 2025 1080p TS EN-RGB.mp4", 2)
    env.L.scan()
    mid = env.M.add_movie(11, "Zootopia 2", 2025, None, _profile(env.db, ["1080p", "720p"]))
    assert env.M.reconcile(mid)["upgrade"] is True
    client = _Client()
    env.SCH.make_client = lambda *a, **k: client
    env.SCH.searchmod.search = lambda *a, **k: [
        _release("Zootopia.2.2025.1080p.HDTS.x264-CAMGRP", "magnet:cam"),
        _release("Zootopia.2.2025.720p.WEB-DL.x264-GRP", "magnet:720"),
        _release("Zootopia.2.2025.1080p.WEB-DL.x264-GRP", "magnet:1080")]
    env.SCH.hunt_wanted()
    assert client.added == ["magnet:1080"]


def test_hunter_episode_upgrade_filter(env):
    mk(env.lib / "tvshows" / "Show" / "Season 01" / "Show.S01E01.720p.WEB-DL.mkv", 2)
    env.L.scan()
    sid = _series(env.db, "Show", _profile(env.db, ["1080p", "720p"]))
    with env.db.connect() as c:
        c.execute("UPDATE profiles SET resolutions=? WHERE id=?",
                  (json.dumps(["1080p", "720p"]), c.execute(
                      "SELECT profile_id FROM series WHERE id=?", (sid,)).fetchone()[0]))
    # the series side upgrades against the profile's first resolution
    assert env.S.reconcile(sid)["upgrades"] == 1
    client = _Client()
    env.SCH.make_client = lambda *a, **k: client
    env.SCH.searchmod.search = lambda *a, **k: [
        _release("Show.S01E01.720p.HDTV.x264-OTHER", "magnet:same")]
    env.SCH.hunt_wanted()
    assert client.added == []
    env.SCH.searchmod.search = lambda *a, **k: [
        _release("Show.S01E01.720p.HDTV.x264-OTHER", "magnet:same"),
        _release("Show.S01E01.1080p.WEB-DL.x264-GRP", "magnet:better")]
    env.SCH.hunt_wanted()
    assert client.added == ["magnet:better"]


# ── F19 / F20: one want per item, atomically ─────────────────────────────────

OLD_PROD_WANTED = """
CREATE TABLE wanted (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    series_id   INTEGER,
    season      INTEGER,
    episode     INTEGER,
    title       TEXT,
    reason      TEXT,
    status      TEXT DEFAULT 'wanted',
    last_search TEXT,
    UNIQUE(kind, series_id, season, episode, title)
);
"""


def test_old_prod_schema_is_deduped_and_keyed(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    importlib.reload(importlib.import_module("faucet.config"))
    db = importlib.reload(importlib.import_module("faucet.db"))
    path = db._db_path()
    path.parent.mkdir(parents=True)
    raw = sqlite3.connect(path)
    raw.executescript(OLD_PROD_WANTED)
    rows = [
        ("episode", 1, 1, 1, "Old Title", "wanted", None),
        ("episode", 1, 1, 1, "New Title", "grabbed", "2026-09-01T00:00:00"),  # in flight
        ("episode", 1, 1, 1, "Newer Title", "wanted", None),
        ("movie", 7, None, None, "Rags 2012", "wanted", None),
        ("movie", 7, None, None, "Rags 2012", "wanted", None),               # NULLs: allowed
        ("episode", 1, 1, 2, "E2", "wanted", None),
    ]
    raw.executemany("INSERT INTO wanted (kind,series_id,season,episode,title,status,last_search) "
                    "VALUES (?,?,?,?,?,?,?)", rows)
    raw.commit()
    raw.close()

    db.init()
    with db.connect() as c:
        got = c.execute("SELECT kind, series_id, season, episode, status FROM wanted "
                        "ORDER BY kind, episode").fetchall()
        assert [tuple(r) for r in got] == [
            ("episode", 1, 1, 1, "grabbed"), ("episode", 1, 1, 2, "wanted"),
            ("movie", 7, None, None, "wanted")]
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='ux_wanted_key'").fetchone()
        # the retitle that used to slip past the title-keyed constraint
        c.execute("INSERT OR IGNORE INTO wanted (kind,series_id,season,episode,title) "
                  "VALUES ('episode',1,1,1,'Retitled')")
        c.execute("INSERT OR IGNORE INTO wanted (kind,series_id,title) VALUES ('movie',7,'x')")
        assert c.execute("SELECT COUNT(*) FROM wanted").fetchone()[0] == 3


def test_movie_wants_are_unique(env):
    for _ in range(3):
        env.W.upsert("movie", 7, None, None, "Rags 2012", "missing", 48)
    assert _count_wants(env.db, kind="movie", series_id=7) == 1
    with pytest.raises(sqlite3.IntegrityError), env.db.connect() as c:
        c.execute("INSERT INTO wanted (kind, series_id, title) VALUES ('movie', 7, 'dup')")


@pytest.mark.parametrize("kind,season,episode", [("episode", 1, 1), ("movie", None, None)])
def test_concurrent_upserts_create_one_row(env, kind, season, episode):
    n = 16
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker(i):
        try:
            barrier.wait()
            results.append(env.W.upsert(kind, 3, season, episode, f"title {i}", "missing", 48))
        except Exception as e:                    # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert results.count("inserted") == 1
    assert _count_wants(env.db, kind=kind, series_id=3) == 1


def test_upsert_grab_retry_semantics(env):
    W, db = env.W, env.db
    assert W.upsert("episode", 1, 1, 1, "Pilot", "missing", 48) == "inserted"
    assert W.upsert("episode", 1, 1, 1, "Pilot (retitled)", "missing", 48) == "updated"
    recent = datetime.now().isoformat(timespec="seconds")
    stale = (datetime.now() - timedelta(hours=49)).isoformat(timespec="seconds")
    with db.connect() as c:
        c.execute("UPDATE wanted SET status='grabbed', last_search=?", (recent,))
    assert W.upsert("episode", 1, 1, 1, "Pilot", "upgrade", 48) == "kept"
    with db.connect() as c:
        c.execute("UPDATE wanted SET last_search=?", (stale,))
    assert W.upsert("episode", 1, 1, 1, "Pilot", "upgrade", 48) == "requeued"
    with db.connect() as c:
        row = c.execute("SELECT status, reason, title FROM wanted").fetchone()
    assert tuple(row) == ("wanted", "upgrade", "Pilot")
    assert _count_wants(db) == 1


# ── superseded files are retired to <LIBRARY_ROOT>/_superseded/ ──────────────

def _rags_upgrade(env):
    old = mk(env.lib / "movies" / "Rags (2012)" / "Rags.2012.720p.WEB-DL.x264.mkv", 2, b"O")
    (old.parent / "Rags.2012.720p.WEB-DL.x264.en.srt").write_text("old subs")
    env.L.scan()
    _sort_in(env, "Rags.2012.1080p.WEB-DL.x264-GRP")
    return old


def test_upgrade_retires_old_file_and_its_subs(env):
    old = _rags_upgrade(env)
    stats = env.L.scan()
    assert stats["superseded_moved"] == 1
    parked = env.lib / "_superseded" / "movies" / "Rags (2012)"
    assert (parked / old.name).read_bytes() == b"O" * 2 * MB
    assert (parked / "Rags.2012.720p.WEB-DL.x264.en.srt").read_text() == "old subs"
    assert not old.exists()
    assert (env.lib / "movies" / "Rags (2012)" / "Rags (2012).mkv").exists()
    assert (env.lib / "_superseded" / ".plexignore").read_text() == "*\n"
    assert (env.lib / "_superseded" / ".ignore").exists()
    stats = env.L.scan()                          # settles: nothing left to retire
    assert "superseded" not in stats
    with env.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM library_movies").fetchone()[0] == 1


def test_episode_upgrade_retires_old_file(env):
    old = mk(env.lib / "tvshows" / "Show" / "Season 01" / "Show.S01E01.720p.WEB-DL.mkv", 2)
    env.L.scan()
    _sort_in(env, "Show.S01E01.1080p.WEB-DL-GRP")
    assert env.L.scan()["superseded_moved"] == 1
    assert (env.lib / "_superseded" / "tvshows" / "Show" / "Season 01" / old.name).exists()


def test_unrecorded_winner_never_retires_anything(env):
    """A better file dropped in by hand (no sorter record) may be partial or
    mislabeled — the old copy stays put."""
    old = mk(env.lib / "movies" / "Rags (2012)" / "Rags.2012.720p.WEB-DL.x264.mkv", 2)
    env.L.scan()
    mk(env.lib / "movies" / "Rags (2012)" / "Rags.2012.1080p.WEB-DL.x264.mkv", 3)
    stats = env.L.scan()
    assert stats["superseded"] == 1 and stats["superseded_moved"] == 0
    assert old.exists()


def test_other_folder_is_never_touched(env):
    """Collection folders / Fix Location: a better copy elsewhere doesn't
    retire a file in a different folder."""
    old = mk(env.lib / "movies" / "Rags Collection" / "Rags.2012.720p.WEB-DL.x264.mkv", 2)
    env.L.scan()
    _sort_in(env, "Rags.2012.1080p.WEB-DL.x264-GRP")
    stats = env.L.scan()
    assert stats["superseded"] == 1 and stats["superseded_moved"] == 0
    assert old.exists()


def test_superseded_action_keep(env, monkeypatch):
    monkeypatch.setenv("SUPERSEDED_ACTION", "keep")
    old = _rags_upgrade(env)
    assert env.L.scan()["superseded_moved"] == 0
    assert old.exists()


def test_nothing_retired_when_the_mount_looks_sick(env):
    old = _rags_upgrade(env)
    with env.db.connect() as c:                   # 20 rows whose files "vanished"
        for i in range(20):
            c.execute("INSERT INTO library_episodes (show_name,season,episode,path,size,mtime) "
                      "VALUES ('Gone',1,?,?,1,1)",
                      (i + 1, str(env.lib / "tvshows" / "Gone" / f"e{i}.mkv")))
    stats = env.L.scan()
    assert stats.get("prune_skipped") and "superseded_moved" not in stats
    assert old.exists()


def test_retired_name_collision_is_numbered(env):
    parked = env.lib / "_superseded" / "movies" / "Rags (2012)"
    mk(parked / "Rags.2012.720p.WEB-DL.x264.mkv", 1, b"P")
    old = _rags_upgrade(env)
    env.L.scan()
    assert (parked / "Rags.2012.720p.WEB-DL.x264.mkv").read_bytes()[:1] == b"P"
    assert (parked / f"{old.stem} (2){old.suffix}").read_bytes()[:1] == b"O"

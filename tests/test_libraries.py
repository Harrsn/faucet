"""Phase 5b.2: libraries, show-folder links by id, path mappings."""
from __future__ import annotations

import importlib
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
    root = tmp_path / "media"
    (root / "tvshows").mkdir(parents=True)
    (root / "movies").mkdir()
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(root))
    monkeypatch.delenv("HUNT_UPGRADES", raising=False)
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db")}
    mods["db"].init()
    for m in ("libraries", "library", "wants", "series", "movies"):
        mods[m] = importlib.reload(importlib.import_module("faucet." + m))
    monkeypatch.setattr(mods["library"], "MIN_SIZE", 1 * MB)
    return SimpleNamespace(tmp=tmp_path, root=root, db=mods["db"], LB=mods["libraries"],
                           L=mods["library"], S=mods["series"])


def _series(db, title, eps=((1, 1), (1, 2))):
    with db.connect() as c:
        sid = c.execute("INSERT INTO series (tmdb_id,title,monitored) VALUES (?,?,1)",
                        (abs(hash(title)) % 10**6, title)).lastrowid
        for s, e in eps:
            c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) "
                      "VALUES (?,?,?,?,'2020-01-01')", (sid, s, e, f"E{e}"))
    return sid


# ── libraries ───────────────────────────────────────────────────────────────

def test_seeded_from_the_old_layout(env):
    libs = env.LB.all_libraries()
    assert [(x["name"], x["kind"], x["path"], x["is_default"]) for x in libs] == [
        ("TV Shows", "tv", str(env.root / "tvshows"), 1),
        ("Movies", "movie", str(env.root / "movies"), 1)]
    assert env.LB.sorter_env() == {"FAUCET_TV_DIR": str(env.root / "tvshows"),
                                   "FAUCET_MOVIES_DIR": str(env.root / "movies")}


def test_seeded_libraries_follow_library_root_until_edited(env, monkeypatch, tmp_path):
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "elsewhere"))
    assert env.LB.default_path("tv") == str(tmp_path / "elsewhere" / "tvshows")
    tv = env.LB.of_kind("tv")[0]
    (tmp_path / "picked").mkdir()
    env.LB.update(tv["id"], path=str(tmp_path / "picked"))
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "third"))
    assert env.LB.default_path("tv") == str(tmp_path / "picked")


def test_validation(env, tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        env.LB.add("x", "tv", "relative/path")
    with pytest.raises(ValueError, match="nest"):
        env.LB.add("x", "tv", str(env.root / "tvshows" / "Kids"))
    with pytest.raises(ValueError, match="nest"):
        env.LB.add("x", "movie", str(env.root))
    with pytest.raises(ValueError, match="already"):
        env.LB.add("x", "tv", str(env.root / "tvshows") + "/")
    with pytest.raises(ValueError, match="kind"):
        env.LB.add("x", "music", str(tmp_path / "m"))
    lid, warnings = env.LB.add("4K", "movie", str(tmp_path / "not-yet"))
    assert lid and any("doesn't exist" in w for w in warnings)


def test_default_per_kind(env, tmp_path):
    (tmp_path / "kids").mkdir()
    lid, _ = env.LB.add("Kids", "tv", str(tmp_path / "kids"), is_default=True)
    assert env.LB.default_path("tv") == str(tmp_path / "kids")
    assert env.LB.default_path("movie") == str(env.root / "movies")
    env.LB.remove(lid)
    assert env.LB.default_path("tv") == str(env.root / "tvshows")   # promoted back


def test_scan_walks_every_library_and_spares_unmounted_ones(env, tmp_path):
    kids = tmp_path / "kids"
    mk(env.root / "tvshows" / "Show" / "Season 01" / "Show - S01E01.mkv")
    mk(kids / "Cartoon" / "Season 01" / "Cartoon - S01E01.mkv")
    lid, _ = env.LB.add("Kids", "tv", str(kids))
    env.L.scan()
    assert env.L.have_episode("Cartoon", 1, 1) and env.L.have_episode("Show", 1, 1)
    for p in kids.rglob("*"):                       # the kids share drops off the network
        if p.is_file():
            p.unlink()
    for p in sorted(kids.rglob("*"), reverse=True):
        p.rmdir()
    kids.rmdir()
    env.L.scan()
    assert env.L.have_episode("Cartoon", 1, 1)      # rows kept, nothing re-hunted


def test_removing_a_library_forgets_its_files_but_never_touches_them(env, tmp_path):
    f = mk(tmp_path / "kids" / "Cartoon" / "Season 01" / "Cartoon - S01E01.mkv")
    lid, _ = env.LB.add("Kids", "tv", str(tmp_path / "kids"))
    env.L.scan()
    env.LB.remove(lid)
    assert f.exists() and env.L.have_episode("Cartoon", 1, 1) is None


def test_superseded_parks_beside_each_library(env, tmp_path):
    kids = tmp_path / "shares" / "kids"
    old = mk(kids / "Cartoon" / "Season 01" / "Cartoon.S01E01.720p.mkv")
    env.LB.add("Kids", "tv", str(kids))
    env.L.scan()
    new = mk(kids / "Cartoon" / "Season 01" / "Cartoon - S01E01.mkv")
    with env.db.connect() as c:
        c.execute("INSERT INTO library_files (path, quality, size) VALUES (?, '1080p', ?)",
                  (str(new), new.stat().st_size))
    env.L.scan(force=True)
    assert not old.exists()
    assert (tmp_path / "shares" / "_superseded" / "kids" / "Cartoon" / "Season 01" / old.name).exists()


# ── folder links ────────────────────────────────────────────────────────────

def test_folder_with_a_year_tag_links_automatically(env):
    mk(env.root / "tvshows" / "Trailer Park Boys (2001)" / "Season 01" / "TPB - S01E01.mkv")
    sid = _series(env.db, "Trailer Park Boys")
    stats = env.L.scan()
    assert stats["folders_linked"] == 1
    assert env.L.have_episode("Trailer Park Boys", 1, 1, sid)["series_id"] == sid
    assert env.S.reconcile(sid)["have"] == 1


def test_manual_link_fixes_a_folder_name_matching_can_never_get(env):
    mk(env.root / "tvshows" / "SwearNet Show" / "Season 01" / "x - S01E01.mkv")
    sid = _series(env.db, "The SwearNet Show")
    env.L.scan()
    assert env.S.reconcile(sid)["missing"] == 2                     # not matched by name
    tv = env.LB.of_kind("tv")[0]
    env.LB.link_folder(tv["id"], "SwearNet Show", sid)
    assert env.S.reconcile(sid) == {"missing": 1, "upgrades": 0, "have": 1}
    env.L.scan(force=True)                                         # survives rescans
    assert env.S.reconcile(sid)["have"] == 1


def test_a_folder_linked_to_another_series_never_counts(env):
    mk(env.root / "tvshows" / "Trailer Park Boys" / "Season 01" / "TPB - S01E01.mkv")
    animated = _series(env.db, "Trailer Park Boys: The Animated Series")
    live = _series(env.db, "Trailer Park Boys")
    env.L.scan()
    tv = env.LB.of_kind("tv")[0]
    env.LB.link_folder(tv["id"], "Trailer Park Boys", animated)     # by hand
    env.L.scan()
    assert env.L.have_episode("Trailer Park Boys", 1, 1, live) is None
    assert env.L.have_episode("Trailer Park Boys: The Animated Series", 1, 1, animated)


def test_ambiguous_folder_is_left_unlinked(env):
    mk(env.root / "tvshows" / "The Office" / "Season 01" / "x - S01E01.mkv")
    _series(env.db, "The Office")
    with env.db.connect() as c:                     # US and UK both normalise the same
        c.execute("INSERT INTO series (tmdb_id,title,monitored) VALUES (2,'The Office',1)")
    stats = env.L.scan()
    assert stats["folders_linked"] == 0 and stats["folders_unlinked"] == 1


# ── path mappings ───────────────────────────────────────────────────────────

def test_path_mappings(env):
    env.LB.set_mappings([{"remote": "/mnt/nas/torrents", "local": "/nas/torrents"},
                         {"remote": "/mnt/nas/torrents/tv", "local": "/tvdl"}])
    assert env.LB.to_local("/mnt/nas/torrents/complete/X") == "/nas/torrents/complete/X"
    assert env.LB.to_local("/mnt/nas/torrents/tv/Y") == "/tvdl/Y"            # longest wins
    assert env.LB.to_local("/mnt/nas/torrentsX/Z") == "/mnt/nas/torrentsX/Z"  # not a prefix
    with pytest.raises(ValueError):
        env.LB.set_mappings([{"remote": "relative", "local": "/x"}])
    with pytest.raises(ValueError):
        env.LB.set_mappings([{"remote": "/a", "local": "/x"}, {"remote": "/a/", "local": "/y"}])


def test_hook_maps_only_paths_it_cannot_see(env, tmp_path, monkeypatch):
    hook = importlib.reload(importlib.import_module("faucet.hook"))
    env.LB.set_mappings([{"remote": "/mnt/nas/torrents", "local": str(tmp_path / "dl")}])
    assert hook._to_local("/mnt/nas/torrents/complete/A") == str(tmp_path / "dl" / "complete" / "A")
    here = tmp_path / "already-local"
    here.mkdir()
    assert hook._to_local(str(here)) == str(here)


def test_sorter_files_into_the_default_libraries(env, tmp_path, monkeypatch):
    monkeypatch.setenv("FAUCET_TV_DIR", str(tmp_path / "tvlib"))
    monkeypatch.setenv("FAUCET_MOVIES_DIR", str(tmp_path / "movlib"))
    sort = importlib.reload(importlib.import_module("faucet.sort"))
    assert sort.TV_DIR == tmp_path / "tvlib" and sort.MOVIES_DIR == tmp_path / "movlib"
    monkeypatch.delenv("FAUCET_TV_DIR")
    monkeypatch.delenv("FAUCET_MOVIES_DIR")
    sort = importlib.reload(sort)
    assert sort.TV_DIR == env.root / "tvshows"                     # run by hand: old layout


# ── API ─────────────────────────────────────────────────────────────────────

def test_library_api(env, tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "t")
    for m in ("auth", "auth_routes", "app"):
        importlib.reload(importlib.import_module("faucet." + m))
    from fastapi.testclient import TestClient

    from faucet import app as appmod
    admin = TestClient(appmod.app, follow_redirects=False)
    admin.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    admin.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    admin.headers.update({"X-CSRF-Token": admin.cookies.get("faucet_csrf")})
    (tmp_path / "kids").mkdir()
    r = admin.post("/api/libraries", json={"name": "Kids", "kind": "tv",
                                           "path": str(tmp_path / "kids")})
    assert r.status_code == 200 and r.json()["library"]["exists"] is True
    lid = r.json()["id"]
    assert admin.post("/api/libraries", json={"name": "Bad", "kind": "tv",
                                              "path": str(tmp_path / "kids" / "sub")}
                      ).status_code == 400
    assert admin.patch(f"/api/libraries/{lid}", json={"is_default": True}).json()["library"][
        "is_default"] == 1
    assert len(admin.get("/api/libraries").json()["libraries"]) == 3
    assert admin.put("/api/path-mappings", json={"mappings": [
        {"remote": "/mnt/nas", "local": "/nas"}]}).status_code == 200
    t = admin.get("/api/path-mappings/test", params={"path": "/mnt/nas/torrents"}).json()
    assert t["local_path"] == "/nas/torrents"
    assert admin.delete(f"/api/libraries/{lid}").status_code == 200
    anon = TestClient(appmod.app, follow_redirects=False)
    assert anon.get("/api/libraries").status_code == 401

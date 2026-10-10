"""Fix B: moves into a library are renames when both sit on one mount.
Detect the mount, carry records over when a library is re-pointed at the
same folder through another mount, and make the finished-downloads folder a
setting."""
from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

import pytest

MOUNTINFO = """\
21 1 0:19 / / rw - overlay overlay rw
30 21 0:44 / /nas rw,relatime - cifs //nas.lan/share rw
31 21 0:44 /media /library rw,relatime - cifs //nas.lan/share rw
32 21 0:44 /torrents /downloads rw,relatime - cifs //nas.lan/share rw
33 21 0:50 / /config rw - ext4 /dev/sda1 rw
34 30 0:51 / /nas/other\\040share rw - cifs //nas.lan/other rw
"""


def test_mount_detection(monkeypatch):
    from faucet import mounts
    monkeypatch.setattr(os.path, "realpath", lambda p: p)
    assert mounts.mount_of("/nas/media/tvshows", MOUNTINFO)["point"] == "/nas"
    assert mounts.mount_of("/library/tvshows", MOUNTINFO)["point"] == "/library"
    assert mounts.mount_of("/nas/other share/x", MOUNTINFO)["point"] == "/nas/other share"
    # two bind mounts of one share are still two mounts: a copy
    assert mounts.same_mount("/downloads/complete", "/library/tvshows", MOUNTINFO) is False
    # everything under the one /nas bind: a rename
    assert mounts.same_mount("/nas/torrents/complete", "/nas/media/tvshows", MOUNTINFO) is True
    # a separate share mounted inside /nas: a copy again
    assert mounts.same_mount("/nas/torrents/complete", "/nas/other share/tv", MOUNTINFO) is False


@pytest.fixture
def env(tmp_path, monkeypatch):
    real = tmp_path / "share" / "media"
    (real / "tvshows" / "Show" / "Season 01").mkdir(parents=True)
    (real / "tvshows" / "Show" / "Season 01" / "Show - S01E01.mkv").write_bytes(b"x" * 10)
    (real / "movies").mkdir()
    (tmp_path / "nas").mkdir()
    os.symlink(tmp_path / "share" / "media", tmp_path / "nas" / "media")   # "another mount"
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(real))
    mods = {m: importlib.reload(importlib.import_module("faucet." + m)) for m in ("config", "db")}
    mods["db"].init()
    for m in ("libraries", "sweep"):
        mods[m] = importlib.reload(importlib.import_module("faucet." + m))
    return SimpleNamespace(tmp=tmp_path, real=real, db=mods["db"], LB=mods["libraries"],
                           sweep=mods["sweep"], config=mods["config"])


def _seed_rows(env):
    old = str(env.real / "tvshows" / "Show" / "Season 01" / "Show - S01E01.mkv")
    with env.db.connect() as c:
        sid = c.execute("INSERT INTO series (tmdb_id,title) VALUES (1,'Show')").lastrowid
        c.execute("INSERT INTO library_episodes (series_id,show_name,season,episode,path,size,mtime) "
                  "VALUES (?,'Show',1,1,?,10,1)", (sid, old))
        c.execute("INSERT INTO library_files (path, quality, size) VALUES (?, '1080p', 10)", (old,))
    tv = env.LB.of_kind("tv")[0]
    env.LB.link_folder(tv["id"], "Show", sid)
    return tv, sid


def test_same_folder_through_another_mount_carries_records(env):
    tv, sid = _seed_rows(env)
    new = str(env.tmp / "nas" / "media" / "tvshows")
    assert env.LB.same_folder(tv["path"], new)
    warnings = env.LB.update(tv["id"], path=new)
    assert any("carried over" in w for w in warnings)
    with env.db.connect() as c:
        paths = [r[0] for r in c.execute("SELECT path FROM library_episodes")]
        files = [r[0] for r in c.execute("SELECT path FROM library_files")]
        links = c.execute("SELECT series_id FROM library_links WHERE library_id=?", (tv["id"],)).fetchall()
    assert paths == [new + "/Show/Season 01/Show - S01E01.mkv"] and files == paths
    assert [r[0] for r in links] == [sid]                      # links kept: nothing re-hunted


def test_a_different_folder_forgets_the_old_records(env):
    tv, _ = _seed_rows(env)
    other = env.tmp / "elsewhere"
    (other / "Other").mkdir(parents=True)
    warnings = env.LB.update(tv["id"], path=str(other))
    assert any("Different folder" in w for w in warnings)
    with env.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM library_episodes").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM library_links").fetchone()[0] == 0


def test_finished_downloads_folder_is_a_setting(env):
    assert str(env.sweep._complete_dir()) == "/downloads/complete"
    env.config.save({"COMPLETE_DIR": "/nas/torrents/complete"})
    assert str(env.sweep._complete_dir()) == "/nas/torrents/complete"
    env.config.save({"COMPLETE_DIR": ""})
    assert str(env.sweep._complete_dir()) == "/downloads/complete"


def test_wrapper_passes_the_clients_path():
    body = (__import__("pathlib").Path(__file__).parent.parent / "deploy" / "cascade-hook-wrapper.sh").read_text()
    assert 'FAUCET_PATH="$host_path"' in body and "/downloads" not in body.split("set -uo")[1]

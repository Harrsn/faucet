"""Sorter data-safety regressions.

Every test here encodes a way faucet/sort.py used to destroy or misfile data
in production (MEDIASORT_MODE=move, REMOVE_ON_COMPLETE=1, /downloads and
/library as separate mounts so rename() fails with EXDEV).
"""
from __future__ import annotations

import errno
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
MB = 1024 * 1024


def mk(path: Path, mb: float, fill: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fill * int(mb * MB))
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    complete = tmp_path / "downloads" / "complete"
    lib.mkdir()
    complete.mkdir(parents=True)
    monkeypatch.setenv("LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("MEDIASORT_MIN_MB", "1")
    monkeypatch.setenv("MEDIASORT_LOG", str(tmp_path / "sort.log"))
    monkeypatch.setenv("MEDIASORT_MODE", "move")
    for k in ("REMOVE_ON_COMPLETE", "QUARANTINE_DIR", "FAUCET_PATH", "CASCADE_PATH",
              "TR_TORRENT_DIR", "TR_TORRENT_NAME", "DRY_RUN"):
        monkeypatch.delenv(k, raising=False)
    from faucet import config as cfg
    importlib.reload(cfg)
    from faucet import db
    importlib.reload(db)
    db.init()
    from faucet import sort as S
    importlib.reload(S)
    return SimpleNamespace(S=S, db=db, lib=lib, dl=complete, tmp=tmp_path)


def _no_same_fs_moves(monkeypatch, S):
    """Production shape: hardlink and rename both fail across the mounts."""
    monkeypatch.setattr(S, "_try_link", lambda *a: False)
    monkeypatch.setattr(S, "_try_rename", lambda *a: False)


def _record(db, path: Path, quality, cam=False):
    with db.connect() as c:
        c.execute("INSERT INTO library_files (path, quality, is_cam) VALUES (?,?,?)",
                  (str(path), quality, int(cam)))


MOVIE_DIR = "Some Movie (2019)"
MOVIE_FILE = "Some Movie (2019).mkv"


# ── F1: nothing unfiled is ever deleted with the torrent ─────────────────────

def test_unparseable_release_is_quarantined_not_deleted(env):
    rel = env.dl / "[Grp] Show - 13 [1080p]"
    mk(rel / "[Grp] Show - 13 [1080p].mkv", 2)
    rc = env.S.sort_release(rel, dry=False)
    assert rc == env.S.EXIT_QUARANTINED
    assert not rel.exists()
    assert (env.dl / "_failed" / rel.name / "[Grp] Show - 13 [1080p].mkv").exists()
    assert not list(env.lib.rglob("*.mkv"))


def test_quarantine_name_collisions_are_numbered(env):
    for _ in range(2):
        rel = env.dl / "Unparseable Thing"
        mk(rel / "clip.mkv", 2)
        assert env.S.sort_release(rel, dry=False) == env.S.EXIT_QUARANTINED
    names = sorted(p.name for p in (env.dl / "_failed").iterdir())
    assert names == ["Unparseable Thing", "Unparseable Thing (2)"]


def test_seeding_release_is_left_alone(env):
    """copy mode without REMOVE_ON_COMPLETE: the torrent keeps seeding, so
    unfiled content stays exactly where it is."""
    env.S.MODE = "copy"
    rel = env.dl / "[Grp] Show - 13 [1080p]"
    f = mk(rel / "[Grp] Show - 13 [1080p].mkv", 2)
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_OK
    assert f.exists() and not (env.dl / "_failed").exists()


def test_missing_input_is_retryable_not_success(env):
    assert env.S.sort_release(env.dl / "gone", dry=False) == env.S.EXIT_RETRY


def test_sorter_subprocess_exit_code_and_package_imports(env):
    """Runs sort.py exactly like the hook does (script path, cwd = package
    parent, no PYTHONPATH). The game release only lands in games/ if
    faucet.classify imported — it silently didn't in production."""
    game = env.dl / "Some.Game-CODEX"
    mk(game / "setup.iso", 2)
    run_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    run_env["FAUCET_PATH"] = str(game)
    r = subprocess.run([sys.executable, str(REPO / "faucet" / "sort.py")],
                       env=run_env, cwd=str(env.tmp), capture_output=True, text=True,
                       check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (env.lib / "games" / "Windows" / "Some.Game-CODEX" / "setup.iso").exists()

    bad = env.dl / "[Grp] Show - 13 [1080p]"
    mk(bad / "ep.mkv", 2)
    run_env["FAUCET_PATH"] = str(bad)
    r = subprocess.run([sys.executable, str(REPO / "faucet" / "sort.py")],
                       env=run_env, cwd=str(env.tmp), capture_output=True, text=True,
                       check=False)
    assert r.returncode == 4, r.stdout + r.stderr


@pytest.mark.parametrize("rc,removed,event", [(0, True, "sorted"),
                                              (4, True, "quarantined"),
                                              (2, False, "sort_failed"),
                                              (1, False, "sort_failed")])
def test_hook_removes_torrent_only_when_nothing_is_left(env, monkeypatch, rc, removed, event):
    monkeypatch.setenv("REMOVE_ON_COMPLETE", "1")
    monkeypatch.setenv("FAUCET_PATH", str(env.dl / "x"))
    monkeypatch.setenv("FAUCET_ID", "7")
    from faucet import hook
    importlib.reload(hook)
    calls = []

    class FakeClient:
        def remove(self, tid, delete_data=False):
            calls.append((tid, delete_data))

    monkeypatch.setattr(hook, "make_client", lambda *a, **k: FakeClient())
    monkeypatch.setattr(hook.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=rc))
    hook.main()
    assert bool(calls) is removed
    events = [r["event"] for r in env.db.recent_history()]
    assert event in events


# ── F2: samples / extras / duplicates never overwrite the feature ────────────

@pytest.mark.parametrize("sample_first", [True, False])
def test_sample_and_featurette_never_overwrite_feature(env, monkeypatch, sample_first):
    _no_same_fs_moves(monkeypatch, env.S)
    rel = env.dl / "Some.Movie.2019.2160p.UHD.BluRay.x265-GRP"
    feature = mk(rel / "Some.Movie.2019.2160p.UHD.BluRay.x265-GRP.mkv", 3, b"F")
    # the sample is BIGGER than the feature: size must not decide what's a sample
    mk(rel / "Sample" / "some.movie.2019.2160p.sample.mkv", 5, b"S")
    mk(rel / "Featurettes" / "Behind.The.Scenes.mkv", 4, b"B")
    if not sample_first:
        monkeypatch.setattr(env.S, "_files", lambda root, _f=env.S._files: list(reversed(_f(root))))
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_OK
    movie = env.lib / "movies" / MOVIE_DIR
    assert (movie / MOVIE_FILE).read_bytes() == b"F" * 3 * MB
    assert (movie / "Featurettes" / "Behind.The.Scenes.mkv").read_bytes()[:1] == b"B"
    assert not feature.exists() and not rel.exists()      # sample was junk
    assert not list(movie.rglob("*sample*"))


def test_inline_trailer_suffix_is_an_extra(env):
    rel = env.dl / "Some.Movie.2019.1080p.WEB-DL-GRP"
    mk(rel / "Some.Movie.2019.1080p.WEB-DL-GRP.mkv", 3)
    mk(rel / "Some.Movie-trailer.mkv", 2)
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_OK
    assert (env.lib / "movies" / MOVIE_DIR / "Trailers" / "Some.Movie.mkv").exists()


def test_trailer_park_boys_is_not_a_trailer(env):
    rel = env.dl / "Trailer.Park.Boys.S01E01.1080p.WEB-DL-GRP"
    mk(rel / "Trailer.Park.Boys.S01E01.1080p.WEB-DL-GRP.mkv", 2)
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_OK
    assert (env.lib / "tvshows" / "Trailer Park Boys" / "Season 01"
            / "Trailer Park Boys - S01E01.mkv").exists()


def test_unnumbered_duplicates_keep_largest_and_quarantine_rest(env):
    rel = env.dl / "Some.Movie.2019.1080p.BluRay-GRP"
    mk(rel / "Some.Movie.2019.1080p.BluRay-GRP.mkv", 4, b"A")
    mk(rel / "Some.Movie.2019.1080p.BluRay-GRP.Alt.Cut.mkv", 2, b"B")
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_QUARANTINED
    assert (env.lib / "movies" / MOVIE_DIR / MOVIE_FILE).read_bytes()[:1] == b"A"
    left = list((env.dl / "_failed").rglob("*.mkv"))
    assert [p.read_bytes()[:1] for p in left] == [b"B"]


def test_cd_parts_are_stacked(env):
    rel = env.dl / "Some.Movie.2019.1080p.BluRay-GRP"
    mk(rel / "Some.Movie.2019.1080p.BluRay-GRP.CD1.mkv", 2, b"1")
    mk(rel / "Some.Movie.2019.1080p.BluRay-GRP.CD2.mkv", 2, b"2")
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_OK
    movie = env.lib / "movies" / MOVIE_DIR
    assert (movie / "Some Movie (2019) - pt1.mkv").read_bytes()[:1] == b"1"
    assert (movie / "Some Movie (2019) - pt2.mkv").read_bytes()[:1] == b"2"


# ── F3 / F9: an interrupted copy never damages the library ───────────────────

def _dying_copy(fsrc, fdst, length=0):
    fdst.write(fsrc.read(100))
    raise OSError(errno.EIO, "CIFS connection dropped")


@pytest.mark.parametrize("mode", ["move", "auto", "copy", "hardlink"])
def test_failed_copy_leaves_existing_library_file_intact(env, monkeypatch, mode):
    env.S.MODE = mode
    _no_same_fs_moves(monkeypatch, env.S)
    movie = env.lib / "movies" / MOVIE_DIR
    existing = mk(movie / MOVIE_FILE, 2, b"O")
    _record(env.db, existing, "720p")                  # upgrade is approved...
    rel = env.dl / "Some.Movie.2019.1080p.BluRay-GRP"
    src = mk(rel / "Some.Movie.2019.1080p.BluRay-GRP.mkv", 3, b"N")
    monkeypatch.setattr(shutil, "copyfileobj", _dying_copy)   # ...and the copy dies
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_RETRY
    assert existing.read_bytes() == b"O" * 2 * MB
    assert src.exists()
    assert not list(movie.glob(".*faucet-partial"))


def test_final_path_never_holds_a_partial_file(env, monkeypatch):
    _no_same_fs_moves(monkeypatch, env.S)
    dest = env.lib / "movies" / MOVIE_DIR / MOVIE_FILE
    real = shutil.copyfileobj
    seen = []

    def watching(fsrc, fdst, length=0):
        seen.append(dest.exists())
        return real(fsrc, fdst, length)

    monkeypatch.setattr(shutil, "copyfileobj", watching)
    rel = env.dl / "Some.Movie.2019.1080p.BluRay-GRP"
    mk(rel / "Some.Movie.2019.1080p.BluRay-GRP.mkv", 2)
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_OK
    assert seen == [False] and dest.exists()


def test_stale_partials_from_killed_sorters_are_reaped(env):
    movie = env.lib / "movies" / MOVIE_DIR
    stale = mk(movie / f".{MOVIE_FILE}.99999.faucet-partial", 1)
    live = mk(movie / f".{MOVIE_FILE}.88888.faucet-partial", 1)
    old = stale.stat().st_mtime - env.S.STALE_PARTIAL_SECS - 60
    os.utime(stale, (old, old))
    rel = env.dl / "Some.Movie.2019.1080p.BluRay-GRP"
    mk(rel / "Some.Movie.2019.1080p.BluRay-GRP.mkv", 2)
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_OK
    assert not stale.exists() and live.exists()


# ── F3: replace only when provably better ────────────────────────────────────

@pytest.mark.parametrize("recorded,cam,incoming,replaced", [
    ("720p", False, "1080p", True),     # upgrade
    ("1080p", True, "1080p", True),     # cam -> real source at same resolution
    ("1080p", False, "720p", False),    # downgrade
    ("1080p", False, "1080p", False),   # sideways
    (None, False, "2160p", False),      # existing quality never recorded
])
def test_replace_only_if_better(env, monkeypatch, recorded, cam, incoming, replaced):
    _no_same_fs_moves(monkeypatch, env.S)
    movie = env.lib / "movies" / MOVIE_DIR
    existing = mk(movie / MOVIE_FILE, 2, b"O")
    if recorded:
        _record(env.db, existing, recorded, cam)
    rel = env.dl / f"Some.Movie.2019.{incoming}.BluRay-GRP"
    mk(rel / f"Some.Movie.2019.{incoming}.BluRay-GRP.mkv", 3, b"N")
    rc = env.S.sort_release(rel, dry=False)
    if replaced:
        assert rc == env.S.EXIT_OK
        assert existing.read_bytes()[:1] == b"N"
        with env.db.connect() as c:
            row = c.execute("SELECT quality, is_cam FROM library_files WHERE path=?",
                            (str(existing),)).fetchone()
        assert row["quality"] == incoming and row["is_cam"] == 0
    else:
        assert rc == env.S.EXIT_QUARANTINED
        assert existing.read_bytes() == b"O" * 2 * MB
        assert list((env.dl / "_failed").rglob("*.mkv"))


def test_same_size_duplicate_is_treated_as_filed(env):
    movie = env.lib / "movies" / MOVIE_DIR
    mk(movie / MOVIE_FILE, 2)
    rel = env.dl / "Some.Movie.2019.1080p.BluRay-GRP"
    mk(rel / "Some.Movie.2019.1080p.BluRay-GRP.mkv", 2)
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_OK
    assert not rel.exists()


def test_new_file_quality_is_recorded(env):
    rel = env.dl / "Some.Movie.2019.1080p.HDTS.x264-GRP"
    mk(rel / "Some.Movie.2019.1080p.HDTS.x264-GRP.mkv", 2)
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_OK
    with env.db.connect() as c:
        row = c.execute("SELECT quality, is_cam, release FROM library_files").fetchone()
    assert row["quality"] == "1080p" and row["is_cam"] == 1
    assert "HDTS" in row["release"]


# ── F4: subtitles keep their language tags ───────────────────────────────────

def test_sidecar_subtitles_keep_language_and_flags(env):
    rel = env.dl / "Some.Movie.2019.1080p.BluRay-GRP"
    stem = "Some.Movie.2019.1080p.BluRay-GRP"
    mk(rel / f"{stem}.mkv", 2)
    for tag in ("en", "es", "en.forced"):
        (rel / f"{stem}.{tag}.srt").write_text(tag)
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_OK
    movie = env.lib / "movies" / MOVIE_DIR
    for tag in ("en", "es", "en.forced"):
        assert (movie / f"Some Movie (2019).{tag}.srt").read_text() == tag


def test_subs_folder_is_attached_to_movie(env):
    rel = env.dl / "Some.Movie.2019.1080p.BluRay-GRP"
    mk(rel / "Some.Movie.2019.1080p.BluRay-GRP.mkv", 2)
    (rel / "Subs").mkdir()
    (rel / "Subs" / "2_English.srt").write_text("full")
    (rel / "Subs" / "3_English.srt").write_text("sdh")
    (rel / "Subs" / "4_French.srt").write_text("fr")
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_OK
    movie = env.lib / "movies" / MOVIE_DIR
    # the English pair would collide once the ordinal is dropped, so it's kept
    assert (movie / "Some Movie (2019).2_English.srt").read_text() == "full"
    assert (movie / "Some Movie (2019).3_English.srt").read_text() == "sdh"
    assert (movie / "Some Movie (2019).French.srt").read_text() == "fr"
    assert not rel.exists()


def test_per_episode_subs_folders_follow_their_episode(env):
    rel = env.dl / "Show.S01.1080p.WEB-DL-GRP"
    for ep in ("01", "02"):
        stem = f"Show.S01E{ep}.1080p.WEB-DL-GRP"
        mk(rel / f"{stem}.mkv", 2)
        d = rel / "Subs" / stem
        d.mkdir(parents=True)
        (d / "2_English.srt").write_text(ep)
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_OK
    season = env.lib / "tvshows" / "Show" / "Season 01"
    assert (season / "Show - S01E01.English.srt").read_text() == "01"
    assert (season / "Show - S01E02.English.srt").read_text() == "02"


def test_colliding_subtitle_is_never_overwritten(env):
    movie = env.lib / "movies" / MOVIE_DIR
    mk(movie / MOVIE_FILE, 2)
    (movie / "Some Movie (2019).en.srt").write_text("original")
    rel = env.dl / "Some.Movie.2019.1080p.BluRay-GRP"
    stem = "Some.Movie.2019.1080p.BluRay-GRP"
    mk(rel / f"{stem}.mkv", 2)
    (rel / f"{stem}.en.srt").write_text("different subtitle")
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_QUARANTINED
    assert (movie / "Some Movie (2019).en.srt").read_text() == "original"


# ── F6: disc images and packed video aren't games ────────────────────────────

def test_movie_disc_image_is_not_filed_as_game(env):
    rel = env.dl / "Some.Movie.2019.1080p.BluRay-GRP"
    mk(rel / "some.movie.iso", 2)
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_QUARANTINED
    assert not (env.lib / "games").exists()


def test_rar_packed_tv_is_not_filed_as_game(env):
    rel = env.dl / "Show.S01E01.1080p.WEB-DL-GRP"
    mk(rel / "show.s01e01.rar", 2)
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_QUARANTINED
    assert not (env.lib / "games").exists()


def test_console_rom_is_a_game_whatever_the_name(env):
    rel = env.dl / "Something Unrecognisable"
    mk(rel / "title.nsp", 2)
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_OK
    assert list((env.lib / "games").rglob("title.nsp"))


def test_existing_game_is_quarantined_not_deleted(env):
    rel = env.dl / "Some.Game-CODEX"
    mk(rel / "setup.iso", 2)
    (env.lib / "games" / "Windows" / "Some.Game-CODEX").mkdir(parents=True)
    assert env.S.sort_release(rel, dry=False) == env.S.EXIT_QUARANTINED
    assert (env.dl / "_failed" / "Some.Game-CODEX" / "setup.iso").exists()


# ── guard rails ──────────────────────────────────────────────────────────────

def test_quarantine_and_cleanup_refuse_dangerous_roots(env):
    S = env.S
    assert not S._safe_to_remove(env.lib)
    assert not S._safe_to_remove(env.lib / "movies")
    assert not S._safe_to_remove(Path("/downloads"))
    assert not S._safe_to_remove(env.dl / "_failed" / "already-quarantined")
    assert S._safe_to_remove(env.dl / "Some.Release")


def test_dry_run_touches_nothing(env):
    rel = env.dl / "[Grp] Show - 13 [1080p]"
    f = mk(rel / "ep.mkv", 2)
    good = env.dl / "Some.Movie.2019.1080p.BluRay-GRP"
    g = mk(good / "Some.Movie.2019.1080p.BluRay-GRP.mkv", 2)
    assert env.S.sort_release(rel, dry=True) == env.S.EXIT_QUARANTINED
    assert env.S.sort_release(good, dry=True) == env.S.EXIT_OK
    assert f.exists() and g.exists()
    assert not list(env.lib.rglob("*.mkv"))

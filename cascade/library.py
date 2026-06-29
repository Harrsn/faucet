"""Library scanner — inventory what's actually on disk (Layer 1).

Walks the library tree, parses each video file with guessit, and records what
shows/episodes and movies are present, with detected quality. This is the
"what exists" truth that reconciliation diffs against the canonical episode
list to find missing items.

Incremental: files are keyed by path and we skip ones whose mtime hasn't
changed since the last scan, so repeat scans over a CIFS-mounted NAS stay cheap.
Runs in a background thread (never blocks the web process).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from . import db
from .config import config

try:
    from guessit import guessit
except ImportError:                              # pragma: no cover
    guessit = None

log = logging.getLogger("cascade.library")

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
MIN_SIZE = 50 * 1024 * 1024  # ignore sub-50MB junk/samples

_RES_TOKENS = [("2160p", ("2160p", "4k", "uhd")), ("1080p", ("1080p",)),
               ("720p", ("720p",)), ("480p", ("480p",))]


def normalize_title(name: str) -> str:
    """Canonical key for matching a library folder name to a TMDb title.
    Strips bracketed tags ([1080p], [720p]), trailing year in parens, and
    normalizes punctuation/whitespace/case so 'Bobs Burgers', \"Bob's Burgers\",
    'American Dad' / 'American Dad!', and 'Stranger Things [1080p]' all collapse
    to the same key."""
    import re
    n = name or ""
    n = re.sub(r"\[[^\]]*\]", " ", n)        # drop [1080p], [BDRip], etc.
    n = re.sub(r"\((19|20)\d{2}\)", " ", n)  # drop a trailing (YEAR)
    n = n.lower()
    n = n.replace("&", "and")
    n = n.replace("'", "").replace("\u2019", "")  # drop apostrophes: Bob's -> Bobs
    n = re.sub(r"[^a-z0-9]+", " ", n)         # remaining punctuation -> space
    return " ".join(n.split()).strip()


def _library_root() -> Path:
    return Path(os.environ.get("LIBRARY_ROOT", "/library"))


def _detect_quality(name: str) -> str | None:
    n = name.lower()
    for label, toks in _RES_TOKENS:
        if any(t in n for t in toks):
            return label
    return None


def _scan_tv(root: Path, stats: dict) -> None:
    tv = root / "tvshows"
    if not tv.exists():
        return
    for f in tv.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_size < MIN_SIZE:
            continue
        # incremental: skip unchanged files already recorded
        with db.connect() as c:
            row = c.execute("SELECT mtime FROM library_episodes WHERE path=?",
                            (str(f),)).fetchone()
        if row and abs((row["mtime"] or 0) - st.st_mtime) < 1:
            stats["skipped"] += 1
            continue

        info = guessit(f.name) if guessit else {}
        # Derive the show name from the folder structure, NOT guessit's filename
        # parse — release tags like [BDRip][1080p][h.265] often throw guessit's
        # title off. The show folder is the level directly under tvshows/.
        show = None
        rel = f.relative_to(tv)
        if len(rel.parts) >= 1:
            show = rel.parts[0]          # e.g. "Rick and Morty"
        # fall back to guessit only if there's no folder (file directly in tvshows/)
        if not show:
            show = (info.get("title") or "").strip()
        show = (show or "").strip()
        season = info.get("season")
        episode = info.get("episode")
        if not show or season is None or episode is None:
            stats["unparsed"] += 1
            continue
        if isinstance(episode, list):
            episode = episode[0]
        quality = _detect_quality(f.name) or _detect_quality(str(f))
        with db.connect() as c:
            c.execute(
                "INSERT INTO library_episodes (show_name, season, episode, quality, path, size, mtime) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(season, episode, show_name) DO UPDATE SET "
                "quality=excluded.quality, path=excluded.path, size=excluded.size, mtime=excluded.mtime",
                (show, int(season), int(episode), quality, str(f), st.st_size, st.st_mtime))
        stats["episodes"] += 1


def _scan_movies(root: Path, stats: dict) -> None:
    mv = root / "movies"
    if not mv.exists():
        return
    for f in mv.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_size < MIN_SIZE:
            continue
        with db.connect() as c:
            row = c.execute("SELECT mtime FROM library_movies WHERE path=?",
                            (str(f),)).fetchone()
        if row and abs((row["mtime"] or 0) - st.st_mtime) < 1:
            stats["skipped"] += 1
            continue
        info = guessit(f.name) if guessit else {}
        title = (info.get("title") or "").strip()
        if not title:
            stats["unparsed"] += 1
            continue
        year = info.get("year")
        quality = _detect_quality(f.name)
        with db.connect() as c:
            c.execute(
                "INSERT INTO library_movies (title, year, quality, path, size, mtime) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(title, year) DO UPDATE SET "
                "quality=excluded.quality, path=excluded.path, size=excluded.size, mtime=excluded.mtime",
                (title, year, quality, str(f), st.st_size, st.st_mtime))
        stats["movies"] += 1


def scan() -> dict:
    """Full incremental scan of the library. Returns counts."""
    db.init()
    root = _library_root()
    stats = {"episodes": 0, "movies": 0, "skipped": 0, "unparsed": 0}
    if not root.exists():
        log.warning("Library root %s not present (NAS mounted?)", root)
        stats["error"] = "library root not found"
        return stats
    if guessit is None:
        stats["error"] = "guessit not installed"
        return stats
    _scan_tv(root, stats)
    _scan_movies(root, stats)
    log.info("Library scan: %d episodes, %d movies (%d skipped, %d unparsed)",
             stats["episodes"], stats["movies"], stats["skipped"], stats["unparsed"])
    return stats


def have_episode(show_name: str, season: int, episode: int) -> dict | None:
    """Is this episode already on disk? Matches on the normalized show title so
    folder-name quirks (tags, punctuation, apostrophes) don't cause misses."""
    key = normalize_title(show_name)
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM library_episodes WHERE season=? AND episode=?",
            (season, episode)).fetchall()
    for r in rows:
        if normalize_title(r["show_name"]) == key:
            return dict(r)
    return None


def have_movie(title: str, year: int | None = None) -> dict | None:
    with db.connect() as c:
        if year:
            r = c.execute("SELECT * FROM library_movies WHERE title=? AND year=?",
                          (title, year)).fetchone()
        else:
            r = c.execute("SELECT * FROM library_movies WHERE title=?", (title,)).fetchone()
    return dict(r) if r else None

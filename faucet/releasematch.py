"""Release-title verification — does a release actually BELONG to the title we
asked for?

Indexer text search is substring-based: querying "Trailer Park Boys S01E01"
happily returns "Trailer Park Boys: Out of the Park: Europe S01E01",
"Trailer Park Boys: The Animated Series S01E01", and every other spin-off
whose name *contains* the query. Ranking by profile/seeders then grabs
whichever of those is healthiest — i.e. very possibly the wrong show.

This module extracts the title portion of a release name (the text before the
season/episode or year marker), normalizes it the same way the library scanner
does, and requires it to EQUAL the monitored title. Supersets ("... Out of the
Park") are rejected; benign decorations (tracker watermarks, a trailing year,
country tags like (US)/(UK)) are tolerated.

Used by the hunter for episode searches, season-pack searches, and movie
searches. Deliberately strict: a false negative costs one skipped release; a
false positive downloads an entire wrong show.
"""
from __future__ import annotations

import re

from .library import normalize_title

# season/episode markers that end the title portion of a TV release name
_EP_MARKER = re.compile(
    r"(?ix)\b(?:"
    r"s\d{1,2}\s*[.\s]?e\d{1,3}"      # S01E01 / S01.E01 / S01 E01
    r"|s\d{1,2}\b"                     # S01 (season pack)
    r"|season[\s._-]*\d{1,2}"          # Season 1
    r"|series[\s._-]*\d{1,2}"          # Series 1 (UK)
    r"|\d{1,2}x\d{2,3}"                # 1x01
    r"|complete[\s._-]+series"         # Complete Series
    r")")

# a standalone year token (movie names: title ends where the year starts)
_YEAR = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")

# quality/source tokens that end the title portion when no year is present
_QUALITY = re.compile(
    r"(?ix)\b(?:2160p|1080p|720p|480p|4k|uhd|remux|blu-?ray|bd-?rip|brrip|"
    r"web-?dl|web-?rip|hdtv|dvd-?rip|x26[45]|h\.?26[45]|hevc|avc|hdr10?|"
    r"dolby|dts|aac|ac3|10bit|hdcam|telesync|multi|proper|repack)\b")

# leading tracker watermarks: "[www.Site.org] - ", "www.Site.net - ", markdown links
_WATERMARK = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*(?:\([^)]*\)\s*)?[-_.\s]*|(?:https?://)?www\.[^\s]+\s*[-_.]+\s*)",
    re.IGNORECASE)

# tokens that may trail a show title without changing its identity
_IGNORABLE_TRAILING = {"us", "uk", "au", "ca", "nz", "the", "a", "an",
                       "uncensored", "extended", "remastered", "internal",
                       "proper", "repack"}


def _clean(release: str) -> str:
    r = release or ""
    r = _WATERMARK.sub("", r)
    r = re.sub(r"\[[^\]]*\]", " ", r)          # drop bracketed tags anywhere
    return r


def title_before_episode_marker(release: str) -> str | None:
    """The raw title portion of a TV release name (text before SxxExx/Season N),
    or None if no episode/season marker is present."""
    r = _clean(release)
    m = _EP_MARKER.search(r)
    if not m:
        return None
    return r[:m.start()]


def title_before_year(release: str) -> tuple[str, int | None]:
    """(title portion, year) for a movie release name. If no year token, the
    whole cleaned name is the title portion and year is None."""
    r = _clean(release)
    # a year at position 0 is part of the title itself ("1917", "2012"), so the
    # first *non-leading* year token is the one that ends the title portion
    for m in _YEAR.finditer(r):
        if m.start() > 0:
            return r[:m.start()], int(m.group(0))
    # no year: end the title at the first quality/source token instead
    q = _QUALITY.search(r)
    if q and q.start() > 0:
        return r[:q.start()], None
    return r, None


def _tokens(raw_title: str) -> list[str]:
    return normalize_title(raw_title).split()


def _strip_ignorable(tokens: list[str], want_len: int) -> list[str]:
    """Drop a trailing year and known-benign trailing tokens until the token
    list is no longer than the wanted title (or nothing benign remains)."""
    toks = list(tokens)
    while len(toks) > want_len:
        t = toks[-1]
        if _YEAR.fullmatch(t) or t in _IGNORABLE_TRAILING:
            toks.pop()
            continue
        break
    return toks


def matches_series(release_title: str, series_title: str,
                   year: int | None = None) -> bool:
    """True when the release's show-title portion equals the monitored show's
    title (after normalization, watermark stripping, and dropping a trailing
    year / country tag). Rejects spin-offs whose names extend the title."""
    want = _tokens(series_title)
    if not want:
        return False
    portion = title_before_episode_marker(release_title)
    if portion is None:
        return False
    got = _strip_ignorable(_tokens(portion), len(want))
    return got == want


def matches_movie(release_title: str, movie_title: str,
                  year: int | None = None) -> bool:
    """True when the release's title portion equals the monitored movie's title
    and, when both years are known, they agree within 1 (metadata fuzz)."""
    want = _tokens(movie_title)
    if not want:
        return False
    portion, rel_year = title_before_year(release_title)
    got = _strip_ignorable(_tokens(portion), len(want))
    if got != want:
        return False
    if year and rel_year and abs(int(year) - rel_year) > 1:
        return False
    return True

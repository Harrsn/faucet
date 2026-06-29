"""Content classification — decide what a release *is* and where it should go.

Strategy (most reliable first):
  1. Torznab/Newznab CATEGORY from the indexer is the authoritative signal for
     content TYPE. Jackett tags every result with standardized category numbers:
        1000-1999  console games
        4000-4999  PC / games / apps
        2000-2999  movies
        5000-5999  TV
        3000-3999  audio
        7000-7999  books
     The indexer knows its own catalog far better than a title parser, so the
     category decides movie vs. tv vs. game vs. other.
  2. NAME PARSING enriches what categories don't capture: the specific platform
     for games (PS5, Switch, Windows…) and quality for video (1080p, BluRay…).

The result drives both the search-result badges and the sorter's destination
folder (/movies, /tvshows, /games, /other).
"""
from __future__ import annotations

import re

# ---- content type from category number ----
def type_from_category(cat: int | None) -> str | None:
    if not cat:
        return None
    if 1000 <= cat < 2000:
        return "game"          # console
    if 4000 <= cat < 5000:
        return "game"          # PC/games/apps (often games; name parsing refines)
    if 2000 <= cat < 3000:
        return "movie"
    if 5000 <= cat < 6000:
        return "tv"
    if 3000 <= cat < 4000:
        return "audio"
    if 7000 <= cat < 8000:
        return "book"
    return None


# ---- platform detection (games) from the release name ----
# order matters: more specific tokens first
_PLATFORMS = [
    ("Nintendo Switch", r"\b(nsw|switch|nintendo switch)\b"),
    ("PS5", r"\b(ps5|playstation 5)\b"),
    ("PS4", r"\b(ps4|playstation 4)\b"),
    ("PS3", r"\b(ps3|playstation 3)\b"),
    ("PS2", r"\b(ps2|playstation 2)\b"),
    ("PSP", r"\b(psp)\b"),
    ("PS Vita", r"\b(ps ?vita|psvita)\b"),
    ("Xbox Series", r"\b(xbox series|xsx)\b"),
    ("Xbox 360", r"\b(xbox ?360|x360)\b"),
    ("Xbox", r"\b(xbox|xbla)\b"),
    ("Wii U", r"\b(wii ?u|wiiu)\b"),
    ("Wii", r"\b(wii)\b"),
    ("3DS", r"\b(3ds)\b"),
    ("Nintendo DS", r"\b(nds|nintendo ds)\b"),
    ("GameCube", r"\b(gamecube|ngc|gcn)\b"),
    ("macOS", r"\b(macos|mac os|osx|mac)\b"),
    ("Linux", r"\b(linux)\b"),
    # Windows last among desktop since many PC releases imply Windows
    ("Windows", r"\b(windows|win(?:32|64)?|pc(?:dvd)?|repack|gog|codex|plaza|flt|skidrow|tenoke|rune|empress|fitgirl|dodi)\b"),
]


def detect_platform(name: str) -> str | None:
    n = name.lower()
    for label, pat in _PLATFORMS:
        if re.search(pat, n):
            return label
    return None


# console platforms imply a game even if the category was ambiguous
_CONSOLE_LABELS = {"Nintendo Switch", "PS5", "PS4", "PS3", "PS2", "PSP", "PS Vita",
                   "Xbox Series", "Xbox 360", "Xbox", "Wii U", "Wii", "3DS",
                   "Nintendo DS", "GameCube"}

# scene groups / tokens that strongly imply a PC game release
_GAME_HINT = re.compile(
    r"\b(repack|gog|codex|plaza|flt|skidrow|tenoke|rune|empress|fitgirl|dodi|"
    r"goldberg|razor1911|reloaded|prophet|hoodlum|update v\d|crack(?:ed|fix)?)\b",
    re.I)

_TV_HINT = re.compile(r"\b(s\d{1,2}e\d{1,2}|season \d+|complete series|\d{1,2}x\d{2})\b", re.I)
_MOVIE_HINT = re.compile(r"\b(19|20)\d{2}\b.*\b(1080p|720p|2160p|bluray|web-?dl|webrip|bdrip|hdrip|x264|x265|hevc)\b", re.I)


def classify(name: str, category: int | None = None) -> dict:
    """Return {type, platform, confidence}. Category wins for type when present;
    name hints break ties or fill gaps when the indexer gave no category."""
    name = name or ""
    platform = detect_platform(name)
    ctype = type_from_category(category)
    confidence = "category" if ctype else "name"

    # if a console platform was detected, it's a game regardless of fuzzy category
    if platform in _CONSOLE_LABELS:
        ctype = "game"
        confidence = "platform"

    # PC category (4000s) often mixes games + apps: confirm "game" via hints,
    # otherwise leave as game but mark lower confidence
    if ctype is None:
        # no usable category — fall back entirely to name parsing
        if _TV_HINT.search(name):
            ctype = "tv"
        elif _GAME_HINT.search(name) or platform:
            ctype = "game"
        elif _MOVIE_HINT.search(name):
            ctype = "movie"
        else:
            ctype = "other"
        confidence = "name"

    return {"type": ctype, "platform": platform, "confidence": confidence}


# destination subfolder for the sorter
_FOLDER = {"movie": "movies", "tv": "tvshows", "game": "games",
           "audio": "music", "book": "books", "other": "other"}


def dest_folder(ctype: str) -> str:
    return _FOLDER.get(ctype, "other")

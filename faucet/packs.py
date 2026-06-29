"""Season-pack detection.

When hunting many missing episodes of a season, grabbing one season pack beats
grabbing 20 individual episodes — faster, better seeded, kinder to trackers.
The challenge is inferring what a release *contains* from its name alone (we
can't open the torrent). These heuristics classify a release as:

  - single  : one episode (S03E07)
  - season  : a full season pack (S03, "Season 3", "Complete Season 3")
  - series  : a whole-series/complete pack (we DETECT these but, per design,
              don't prefer them — they're huge)

Detection is conservative: when in doubt, treat as single (safer to grab one
episode than wrongly assume a pack covers what it doesn't).
"""
from __future__ import annotations

import re

# S03E07 / 3x07 / S03E07E08 — has an explicit episode number => single
_EP_PATTERNS = [
    re.compile(r"s\d{1,2}\s*e\d{1,3}", re.I),       # S03E07, S03 E07
    re.compile(r"\b\d{1,2}x\d{1,3}\b", re.I),        # 3x07
]
# S03 / Season 3 / Series 3 (no episode) => season pack
_SEASON_PATTERNS = [
    re.compile(r"\bs(\d{1,2})\b(?!\s*e\d)", re.I),   # S03 not followed by E
    re.compile(r"\bseason\s*(\d{1,2})\b", re.I),
    re.compile(r"\bseries\s*(\d{1,2})\b", re.I),     # UK usage
]
# complete / whole series / season ranges => series pack (detected, not preferred)
_SERIES_PATTERNS = [
    re.compile(r"\bcomplete\b", re.I),
    re.compile(r"\bseasons?\s*\d{1,2}\s*[-–to]+\s*\d{1,2}\b", re.I),  # seasons 1-5
    re.compile(r"\bs\d{1,2}\s*[-–]\s*s?\d{1,2}\b", re.I),             # S01-S05
    re.compile(r"\ball\s+seasons?\b", re.I),
]


def classify_pack(title: str) -> dict:
    """Classify a release name. Returns {kind, season}. kind in
    single|season|series; season is the season number when determinable."""
    t = title or ""

    # series/complete packs first — but a release with an explicit single
    # episode is never a series pack even if 'complete' appears in junk
    has_episode = any(p.search(t) for p in _EP_PATTERNS)

    if not has_episode:
        # "Season 3 Complete" / "S03 Complete" names a single season → season pack,
        # not a whole-series pack. Only treat as series when no single season is named.
        single_season = None
        for p in _SEASON_PATTERNS:
            m = p.search(t)
            if m:
                try:
                    single_season = int(m.group(1))
                except (ValueError, IndexError):
                    single_season = None
                break
        is_range = bool(re.search(r"seasons?\s*\d{1,2}\s*[-–to]+\s*\d{1,2}", t, re.I)
                        or re.search(r"\bs\d{1,2}\s*[-–]\s*s?\d{1,2}\b", t, re.I)
                        or re.search(r"\ball\s+seasons?\b", t, re.I))
        if is_range:
            return {"kind": "series", "season": None}
        if single_season is not None:
            return {"kind": "season", "season": single_season}
        for p in _SERIES_PATTERNS:
            if p.search(t):
                return {"kind": "series", "season": None}

    if has_episode:
        # could still be a multi-episode single-season thing, but treat as single
        return {"kind": "single", "season": _first_season(t)}

    for p in _SEASON_PATTERNS:
        m = p.search(t)
        if m:
            try:
                return {"kind": "season", "season": int(m.group(1))}
            except (ValueError, IndexError):
                return {"kind": "season", "season": None}

    return {"kind": "single", "season": _first_season(t)}


def _first_season(t: str) -> int | None:
    m = re.search(r"s(\d{1,2})\s*e\d", t, re.I) or re.search(r"\b(\d{1,2})x\d", t, re.I)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, IndexError):
            return None
    return None

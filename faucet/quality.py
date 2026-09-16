"""Release quality detection and ranking, shared by the scanner and the sorter.

Dependency-free on purpose: ``faucet/sort.py`` imports this while running as a
subprocess of the completion hook, and must not drag in the DB or config just
to compare two release names.
"""
from __future__ import annotations

import re

# resolution tokens, best first
RES_TOKENS = [("2160p", ("2160p", "4k", "uhd")), ("1080p", ("1080p",)),
              ("720p", ("720p",)), ("480p", ("480p",))]

# higher = better; unknown resolution ranks 0
RES_RANK = {"2160p": 4, "1080p": 3, "720p": 2, "480p": 1, None: 0, "": 0}

_CAM_RE = re.compile(
    r"\b(hd-?cam|cam-?rip|telesync|hd-?ts|telecine|dvd-?scr|screener|"
    r"ts|tc|cam|scr)\b", re.IGNORECASE)


def detect_quality(name: str) -> str | None:
    """Resolution label ('1080p', ...) found in a release/file name, or None."""
    n = (name or "").lower()
    for label, toks in RES_TOKENS:
        if any(t in n for t in toks):
            return label
    return None


def detect_cam(name: str) -> bool:
    """Cam-family rip (CAM/TS/TC/telesync/screener). A '1080p TS' is 1080p in
    name only, so these always rank below a real source."""
    return bool(_CAM_RE.search(name or ""))


def file_rank(quality: str | None, is_cam: bool) -> float:
    """Comparable rank: resolution, with cam-family rips half a step lower."""
    r = RES_RANK.get(quality, 0)
    return r - 0.5 if is_cam else r


def is_better(new_quality: str | None, new_cam: bool,
              old_quality: str | None, old_cam: bool) -> bool:
    """True only when the new file is PROVABLY better than the old one.

    Unknown on either side is never "better": an unrecognised incoming release
    must not replace a file, and a file whose quality was never recorded can't
    be shown to be worse. Equal rank is not better either.
    """
    if not new_quality or not old_quality:
        return False
    return file_rank(new_quality, new_cam) > file_rank(old_quality, old_cam)


def is_upgrade(release_name: str, owned_quality: str | None, owned_cam: bool) -> bool:
    """Would grabbing `release_name` improve on the owned file?

    A cam of unknown resolution is beaten by any real source whose resolution
    is known; otherwise the release must be provably better (is_better). An
    upgrade grab that isn't better only gets quarantined by the sorter, so
    without this check the hunter re-downloads same-quality copies every
    GRAB_RETRY_HOURS forever."""
    new_q, new_cam = detect_quality(release_name), detect_cam(release_name)
    if owned_cam and not owned_quality:
        return bool(new_q) and not new_cam
    return is_better(new_q, new_cam, owned_quality, owned_cam)

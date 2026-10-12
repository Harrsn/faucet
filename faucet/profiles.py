"""Quality profiles — score and rank releases against a user's rules.

A profile expresses preferences ("1080p WEB-DL, 3+ seeders, under 8GB"). Given
a list of search results, the engine filters out anything that violates a hard
constraint (min seeders, size caps), then scores the rest so the best match per
the profile's *preference order* floats to the top. This powers two things:
  - an "auto-pick" button next to a search (grab the best release now), and
  - RSS auto-grab later (pick the best release for a followed show, unattended).

Profiles live in the DB (see db.py `profiles` table). A profile dict looks like:
    {id, name, min_seeders, resolutions: [..pref order..],
     sources: [..pref order..], max_size_gb, min_size_gb}
"""
from __future__ import annotations

import json
import re

GB = 1024 ** 3


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        try:
            return json.loads(v)
        except ValueError:
            return [s.strip() for s in v.split(",") if s.strip()]
    return []


CODECS = ("", "prefer-hevc", "prefer-h264", "only-hevc", "only-h264")
HDR = ("", "prefer", "avoid", "never")


def formats(profile: dict) -> dict:
    """A profile's preferred-format rules, normalised (stored as JSON)."""
    raw = profile.get("formats")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else {}
        except ValueError:
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    norm = lambda xs: [str(x).strip() for x in (xs or []) if str(x).strip()]  # noqa: E731
    return {"codec": raw.get("codec") if raw.get("codec") in CODECS else "",
            "hdr": raw.get("hdr") if raw.get("hdr") in HDR else "",
            "prefer_groups": norm(raw.get("prefer_groups")),
            "never_groups": norm(raw.get("never_groups")),
            "never_words": norm(raw.get("never_words"))}


def _has_word(title: str, word: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])",
                     (title or "").lower().replace(".", " ").replace("_", " ")) is not None


def _format_reject(result: dict, f: dict) -> str | None:
    badges = result.get("badges") or {}
    title = result.get("title") or ""
    group = (badges.get("group") or "").lower()
    if group and group in {g.lower() for g in f["never_groups"]}:
        return f"format: release group {badges['group']} is on the never list"
    for w in f["never_words"]:
        if _has_word(title, w):
            return f"format: contains '{w}'"
    if f["hdr"] == "never" and badges.get("hdr"):
        return "format: HDR"
    if f["codec"] == "only-hevc" and badges.get("codec") != "HEVC":
        return f"format: {badges.get('codec') or 'unknown codec'}, profile wants HEVC only"
    if f["codec"] == "only-h264" and badges.get("codec") != "H.264":
        return f"format: {badges.get('codec') or 'unknown codec'}, profile wants H.264 only"
    return None


def format_score(result: dict, profile: dict) -> float:
    f = formats(profile)
    badges = result.get("badges") or {}
    s = 0.0
    want = {"prefer-hevc": "HEVC", "prefer-h264": "H.264"}.get(f["codec"])
    if want and badges.get("codec") == want:
        s += 30                       # beats a source step (20), never a resolution step (100)
    if f["hdr"] == "prefer" and badges.get("hdr"):
        s += 40
    elif f["hdr"] == "avoid" and badges.get("hdr"):
        s -= 40
    group = (badges.get("group") or "").lower()
    if group and group in {g.lower() for g in f["prefer_groups"]}:
        s += 30
    return s


def passes(result: dict, profile: dict) -> tuple[bool, str]:
    """Hard constraints. Returns (ok, reason_if_not)."""
    min_seed = int(profile.get("min_seeders") or 0)
    if result.get("seeders", 0) < min_seed:
        return False, f"seeders {result.get('seeders',0)} < {min_seed}"

    size = result.get("size", 0) or 0
    max_gb = float(profile.get("max_size_gb") or 0)
    min_gb = float(profile.get("min_size_gb") or 0)
    if max_gb and size > max_gb * GB:
        return False, f"size {size/GB:.1f}GB > {max_gb}GB cap"
    if min_gb and size and size < min_gb * GB:
        return False, f"size {size/GB:.1f}GB < {min_gb}GB floor"

    # If the profile lists resolutions, the release must match one of them
    # (releases with no detectable resolution — e.g. games — are exempt).
    res_pref = _as_list(profile.get("resolutions"))
    badges = result.get("badges") or {}
    if res_pref and badges.get("res") and badges["res"] not in res_pref:
        return False, f"resolution {badges['res']} not in profile"

    # Cam-family releases (CAM/TS/TC/screeners) are never acceptable for an
    # unattended profile-driven grab unless the profile explicitly lists CAM.
    src_pref = _as_list(profile.get("sources"))
    if badges.get("source") == "CAM" and "CAM" not in src_pref:
        return False, "cam/telesync release"

    # Preferred language hard filter. Releases we can't classify ('und') pass,
    # so we never drop something just because detection was unsure.
    lang_pref = _effective_language(profile)
    if lang_pref and lang_pref != "any":
        from . import langdetect
        if not langdetect.matches(result, lang_pref):
            return False, f"language {langdetect.detect(result)} != {lang_pref}"
    why = _format_reject(result, formats(profile))
    if why:
        return False, why
    return True, ""


def score(result: dict, profile: dict) -> float:
    """Higher is better. Combines preference-order position for resolution and
    source with a seeder bonus, so among acceptable releases the most-preferred
    quality wins and ties break toward health."""
    badges = result.get("badges") or {}
    res_pref = _as_list(profile.get("resolutions"))
    src_pref = _as_list(profile.get("sources"))

    s = 0.0
    # resolution: earlier in the pref list = higher score (weighted heaviest)
    if badges.get("res") in res_pref:
        s += (len(res_pref) - res_pref.index(badges["res"])) * 100
    # source: next most important
    if badges.get("source") in src_pref:
        s += (len(src_pref) - src_pref.index(badges["source"])) * 20
    s += health(result)
    s += format_score(result, profile)
    # preferred-language bonus: an exact match outranks an undetermined one,
    # which outranks (but here we've already filtered out) the wrong language.
    lang_pref = _effective_language(profile)
    if lang_pref and lang_pref != "any":
        from . import langdetect
        if langdetect.detect(result) == lang_pref:
            s += 50
    return s


def health(result: dict) -> float:
    """Swarm-health term of the score.

    Logarithmic, so 10 vs 100 seeders matters far more than 100 vs 1000; and
    a release under HUNT_HEALTHY_SEEDERS (default 5) takes a penalty larger
    than one resolution step. Old backlog torrents on public trackers often
    report a handful of seeders that never connect — the hunter used to take
    the 1080p with 3 seeders over the 720p with 300, and most of those grabs
    stalled. Indexer seeder counts are self-reported, so treat low ones as
    probably dead."""
    import math
    import os
    try:
        floor = int(os.environ.get("HUNT_HEALTHY_SEEDERS", "5"))
    except ValueError:
        floor = 5
    seeders = max(0, int(result.get("seeders") or 0))
    s = min(100.0, 40.0 * math.log10(1 + seeders))
    if seeders < floor:
        s -= 150.0
    return s


def _effective_language(profile: dict) -> str:
    """The language to enforce for this profile: the profile's own setting, else
    the global default setting, else 'en'."""
    lang = (profile.get("language") or "").strip().lower()
    if lang:
        return lang
    try:
        from . import db
        g = db.get_setting("default_language", "en")
        return (g or "en").strip().lower()
    except Exception:                            # noqa: BLE001
        return "en"


def rank(results: list[dict], profile: dict) -> list[dict]:
    """Filter by hard constraints, then sort by score desc. Each returned
    result gets a `_score` and `_rejected` is omitted (rejects are dropped)."""
    kept = []
    for r in results:
        ok, _ = passes(r, profile)
        if ok:
            rr = dict(r)
            rr["_score"] = score(r, profile)
            kept.append(rr)
    kept.sort(key=lambda x: x["_score"], reverse=True)
    return kept


def best(results: list[dict], profile: dict) -> dict | None:
    """The single best release for this profile, or None if nothing qualifies."""
    ranked = rank(results, profile)
    return ranked[0] if ranked else None

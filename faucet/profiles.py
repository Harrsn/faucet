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
    # health: a gentle log-ish bonus so seeders break ties but don't dominate
    seeders = result.get("seeders", 0)
    s += min(seeders, 100) * 0.5
    return s


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

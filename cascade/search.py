"""Indexer search via Jackett/Prowlarr Torznab aggregate endpoint."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

CATS = {"movies": "2000", "tv": "5000", "all": ""}
_NS = "{http://torznab.com/schemas/2015/feed}"


class SearchError(Exception):
    pass


def human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


def parse_badges(title: str) -> dict:
    t = title.lower()
    badges = {"ext": None, "res": None, "source": None}
    for ext in ("mkv", "mp4", "avi", "m4v", "ts"):
        if re.search(rf"\b{ext}\b", t) or t.endswith("." + ext):
            badges["ext"] = ext.upper()
            break
    for pat, label in ((r"\b(2160p|4k|uhd)\b", "2160p"), (r"\b1080p\b", "1080p"),
                       (r"\b720p\b", "720p"), (r"\b480p\b", "480p")):
        if re.search(pat, t):
            badges["res"] = label
            break
    for pat, label in ((r"\bremux\b", "REMUX"), (r"\b(blu-?ray|bdrip|brrip)\b", "BluRay"),
                       (r"\bweb-?dl\b", "WEB-DL"), (r"\bwebrip\b", "WEBRip"),
                       (r"\bhdtv\b", "HDTV"), (r"\bdvdrip\b", "DVDRip")):
        if re.search(pat, t):
            badges["source"] = label
            break
    return badges


def _attr(item, name):
    for a in item.findall(f"{_NS}attr"):
        if a.get("name") == name:
            return a.get("value")
    return None


def indexers(jackett_url: str, api_key: str, timeout: int = 15) -> list[dict]:
    """List configured indexers from Jackett (its Torznab 'all' caps endpoint
    exposes the set, but the cleaner source is the admin indexers API). Returns
    [{id, name}] for the UI dropdown. Empty list on any failure (UI falls back
    to a plain text field)."""
    if not api_key:
        return []
    # Jackett admin API: configured indexers with their ids + names.
    url = f"{jackett_url}/api/v2.0/indexers?configured=true&apikey={api_key}"
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return []
    out = []
    for ix in data:
        if isinstance(ix, dict) and ix.get("id"):
            out.append({"id": ix["id"], "name": ix.get("name", ix["id"])})
    out.sort(key=lambda x: x["name"].lower())
    return out


def search(jackett_url: str, api_key: str, indexer: str, query: str,
           category: str, limit: int, timeout: int = 30) -> list[dict]:
    if not api_key:
        raise SearchError("Indexer API key not configured.")
    cat = CATS.get(category, "")
    url = (f"{jackett_url}/api/v2.0/indexers/{indexer}/results/torznab/api"
           f"?apikey={api_key}&t=search&q={quote(query)}")
    if cat:
        url += f"&cat={cat}"
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        raise SearchError(f"Indexer query failed: {e}")
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        raise SearchError(f"Bad XML from indexer: {e}")

    results = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        magnet = _attr(item, "magneturl")
        link = item.findtext("link") or ""
        enc = item.find("enclosure")
        enc_url = enc.get("url") if enc is not None else ""
        href = magnet or (link if link.startswith("magnet:") else "") or enc_url
        if not href:
            continue
        seeders = _attr(item, "seeders")
        peers = _attr(item, "peers")
        size = item.findtext("size") or _attr(item, "size") or "0"
        tracker = _attr(item, "tracker") or item.findtext("jackettindexer") or ""
        try:
            size_i = int(size)
        except (ValueError, TypeError):
            size_i = 0
        results.append({
            "title": title, "href": href, "is_magnet": href.startswith("magnet:"),
            "seeders": int(seeders) if seeders and seeders.isdigit() else 0,
            "peers": int(peers) if peers and peers.isdigit() else 0,
            "size": size_i, "size_h": human_size(size_i),
            "tracker": tracker, "badges": parse_badges(title),
        })
    results.sort(key=lambda x: x["seeders"], reverse=True)
    return results[:limit]

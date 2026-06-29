"""Indexer search via Jackett/Prowlarr Torznab aggregate endpoint."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

from .classify import classify

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
    """List configured indexers from Jackett via the Torznab 't=indexers'
    capability, which uses the same API key as search and returns XML:

        <indexers><indexer id="1337x" configured="true"><title>1337x</title>...

    Returns [{id, name}] for the UI dropdown. Empty on any failure (the UI
    falls back to a plain text field). The admin JSON API isn't used because it
    sits behind the dashboard and serves HTML to API-key requests."""
    if not api_key:
        return []
    url = (f"{jackett_url}/api/v2.0/indexers/all/results/torznab/api"
           f"?t=indexers&configured=true&apikey={api_key}")
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError):
        return []
    out = []
    for ix in root.iter("indexer"):
        iid = ix.get("id")
        if not iid:
            continue
        # only include configured ones if the attribute is present
        if ix.get("configured", "true").lower() == "false":
            continue
        title_el = ix.find("title")
        name = title_el.text if title_el is not None and title_el.text else iid
        out.append({"id": iid, "name": name})
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
        # capture the indexer category (authoritative for content type)
        cat_attr = _attr(item, "category")
        try:
            cat_num = int(cat_attr) if cat_attr else None
        except (ValueError, TypeError):
            cat_num = None
        try:
            size_i = int(size)
        except (ValueError, TypeError):
            size_i = 0
        klass = classify(title, cat_num)
        results.append({
            "title": title, "href": href, "is_magnet": href.startswith("magnet:"),
            "seeders": int(seeders) if seeders and seeders.isdigit() else 0,
            "peers": int(peers) if peers and peers.isdigit() else 0,
            "size": size_i, "size_h": human_size(size_i),
            "tracker": tracker, "badges": parse_badges(title),
            "ctype": klass["type"], "platform": klass["platform"],
            "category": cat_num,
        })
    results.sort(key=lambda x: x["seeders"], reverse=True)
    return results[:limit]

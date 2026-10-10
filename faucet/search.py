"""Indexer search via Jackett/Prowlarr Torznab aggregate endpoint."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

from .classify import classify

CATS = {
    "all": "",
    "movies": "2000",
    "tv": "5000",
    "games": "1000,4000",   # console + PC/games
    "console": "1000",
    "pc": "4000",
    "music": "3000",
    "books": "7000",
}
_NS = "{http://torznab.com/schemas/2015/feed}"


class SearchError(Exception):
    """Indexer failure. The message is always credential-free: requests'
    exception text embeds the full URL, apikey included."""

    def __init__(self, msg="", *secrets):
        from .redact import redact
        super().__init__(redact(msg, *secrets))


class Results(list):
    """Search results; `hidden` counts releases dropped as unsafe
    (faucet.safety.filter_release_names); `errors` lists (indexer, message)
    for indexers that failed in a multi-indexer search."""
    hidden: int = 0
    errors: list = []
    searched: int = 1
    skipped: list = []
    slow: list = []


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
        # dot-anchored: a bare 'TS' token is a telesync tag, not a .ts file
        if re.search(rf"\.{ext}\b", t):
            badges["ext"] = ext.upper()
            break
    for pat, label in ((r"\b(2160p|4k|uhd)\b", "2160p"), (r"\b1080p\b", "1080p"),
                       (r"\b720p\b", "720p"), (r"\b480p\b", "480p")):
        if re.search(pat, t):
            badges["res"] = label
            break
    for pat, label in ((r"\bremux\b", "REMUX"), (r"\b(blu-?ray|bdrip|brrip)\b", "BluRay"),
                       (r"\bweb-?dl\b", "WEB-DL"), (r"\bwebrip\b", "WEBRip"),
                       (r"\bhdtv\b", "HDTV"), (r"\bdvdrip\b", "DVDRip"),
                       # cam-family last so a real source tag always wins
                       (r"\b(hd-?cam|cam-?rip|telesync|hd-?ts|telecine|"
                        r"dvd-?scr|screener|ts|tc|cam|scr)\b", "CAM")):
        if re.search(pat, t):
            badges["source"] = label
            break
    return badges


def torznab_error(body: bytes) -> str:
    """The human part of a Torznab/Jackett <error description="...">, or "".

    Jackett's descriptions are .NET exception dumps; keep the first line and
    drop the exception class names, e.g. "Jackett.Common.IndexerException:
    Exception (eztv): Challenge detected but FlareSolverr is not configured"
    -> "eztv: Challenge detected but FlareSolverr is not configured"."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return ""
    if root.tag != "error":
        return ""
    desc = (root.get("description") or "").strip().splitlines()
    first = desc[0].strip() if desc else ""
    m = re.match(r"^[\w.]+Exception:\s*Exception \(([^)]+)\):\s*(.*)$", first)
    if m:
        first = f"{m.group(1)}: {m.group(2)}"
    else:
        first = re.sub(r"^[\w.]+Exception:\s*", "", first)
    # Jackett wraps raw site HTML into some messages; keep it short and tag-free
    first = re.sub(r"<[^>]+>", " ", first)
    return " ".join(first.split())[:240]


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


def caps(jackett_url: str, api_key: str, indexer: str, timeout: int = 15) -> dict:
    """An indexer's Torznab capabilities: {function: set(supported params)}
    for each available function among 'search', 'tv-search', 'movie-search'.
    Raises SearchError when they can't be read."""
    url = (f"{jackett_url}/api/v2.0/indexers/{quote(indexer, safe='')}"
           f"/results/torznab/api?apikey={quote(api_key, safe='')}&t=caps")
    try:
        r = requests.get(url, timeout=timeout)
    except requests.RequestException as e:
        raise SearchError(f"caps query failed: {e}", api_key) from None
    status = getattr(r, "status_code", 200) or 200
    if status >= 400:
        raise SearchError(f"HTTP {status}: {torznab_error(r.content) or 'caps failed'}",
                          api_key)
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        raise SearchError(f"Bad caps XML: {e}", api_key) from None
    out: dict = {}
    # top-level categories the indexer carries (5030 'TV/SD' -> 5000), so
    # a TV search can skip a movies-only indexer and vice versa
    cats = set()
    for cat in root.iter("category"):
        try:
            cats.add(int(cat.get("id", "0")) // 1000 * 1000)
        except ValueError:
            pass
    cats.discard(0)
    if cats:
        out["_cats"] = cats
    searching = root.find("searching")
    for el in (list(searching) if searching is not None else []):
        if el.tag not in ("search", "tv-search", "movie-search"):
            continue
        if (el.get("available") or "no").lower() != "yes":
            continue
        out[el.tag] = {p.strip().lower() for p in (el.get("supportedParams") or "q").split(",")
                       if p.strip()}
    return out


def search(jackett_url: str, api_key: str, indexer: str, query: str,
           category: str, limit: int, timeout: int = 30, record: bool = True,
           mode: str = "search", params: dict | None = None) -> Results:
    """Torznab search. Every call is timed and recorded for the indexer-health
    view (faucet.indexerhealth) unless `record` is False.

    `mode` is the Torznab function: 'search' (free text), 'tvsearch' or
    'movie'; `params` adds its structured parameters (season, ep, imdbid,
    tvdbid, tmdbid). faucet.finder picks them per indexer from its caps."""
    import time
    t0 = time.monotonic()
    try:
        out = _search(jackett_url, api_key, indexer, query, category, limit, timeout,
                      mode=mode, params=params)
    except SearchError as e:
        if record:
            _record(indexer, False, t0, error=str(e))
        raise
    if record:
        trackers: dict = {}
        for r in out:
            t = r.get("tracker") or "unknown"
            trackers[t] = trackers.get(t, 0) + 1
        _record(indexer, True, t0, results=len(out), trackers=trackers)
    return out


def _record(indexer, ok, t0, **kw) -> None:
    import time
    try:
        from .indexerhealth import record_search
        record_search(indexer, ok, int((time.monotonic() - t0) * 1000), **kw)
    except Exception:                            # noqa: BLE001 - telemetry never breaks search
        pass


def _search(jackett_url: str, api_key: str, indexer: str, query: str,
            category: str, limit: int, timeout: int = 30, mode: str = "search",
            params: dict | None = None) -> Results:
    if not api_key:
        raise SearchError("Indexer API key not configured.")
    if mode not in ("search", "tvsearch", "movie"):
        raise SearchError(f"unsupported Torznab mode {mode!r}")
    cat = CATS.get(category, "")
    url = (f"{jackett_url}/api/v2.0/indexers/{quote(indexer or 'all', safe='')}"
           f"/results/torznab/api?apikey={quote(api_key, safe='')}&t={mode}")
    if query:
        url += f"&q={quote(query)}"
    for k, v in (params or {}).items():
        if v not in (None, ""):
            url += f"&{quote(str(k), safe='')}={quote(str(v), safe='')}"
    if cat:
        url += f"&cat={cat}"
    try:
        r = requests.get(url, timeout=timeout)
    except requests.RequestException as e:
        raise SearchError(f"Indexer query failed: {e}", api_key) from None
    status = getattr(r, "status_code", 200) or 200
    if status >= 400:
        # Jackett explains failures in a Torznab <error> body on the 4xx/5xx
        # ("Challenge detected but FlareSolverr is not configured", a 403 from
        # the site, a disabled indexer). raise_for_status() threw that away
        # and left only "400 Bad Request".
        raise SearchError(
            f"HTTP {status}: {torznab_error(r.content) or getattr(r, 'reason', '') or 'error'}",
            api_key)
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        raise SearchError(f"Bad XML from indexer: {e}", api_key) from None
    if root.tag == "error":
        # Torznab reports failures (bad API key, disabled indexer) as HTTP 200
        # with an <error> document — previously read as "no results"
        raise SearchError(f"Indexer error {root.get('code', '?')}: "
                          f"{torznab_error(r.content) or 'unknown'}", api_key)

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
    from .safety import filter_release_names
    kept, hidden = filter_release_names(results)
    kept.sort(key=lambda x: x["seeders"], reverse=True)
    out = Results(kept[:limit])
    out.hidden = hidden
    return out

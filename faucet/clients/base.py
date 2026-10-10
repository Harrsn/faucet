"""Download client abstraction.

Faucet talks to torrent clients through a small uniform interface so the rest
of the app never needs to know whether it's driving Transmission, qBittorrent,
or Deluge. Each backend implements DownloadClient; the factory in __init__
picks one from config.
"""
from __future__ import annotations

import base64
from abc import ABC, abstractmethod

import requests
from dataclasses import dataclass
from typing import Optional


@dataclass
class Transfer:
    """A normalized view of one torrent, identical across all clients."""
    id: str
    name: str
    percent: float          # 0..100
    down_rate: int          # bytes/s
    up_rate: int            # bytes/s
    status: str             # downloading | seeding | stopped | checking | queued | error
    eta: int                # seconds, -1 if unknown
    ratio: float
    size: int               # total bytes
    error: Optional[str] = None

    @property
    def done(self) -> bool:
        return self.percent >= 100


@dataclass
class TransferFile:
    name: str
    path: str
    size: int
    percent: float
    wanted: bool = True


@dataclass
class AddResult:
    id: Optional[str]
    name: str
    duplicate: bool = False


class DownloadClientError(Exception):
    """Raised for any client-level failure (auth, connectivity, bad response).

    Messages are redacted on construction: they end up in HTTP responses,
    history and logs, and requests' error text carries the full URL (with
    any user:password@ or session id in it)."""

    def __init__(self, *args):
        from ..redact import redact
        super().__init__(*(redact(a) if isinstance(a, str) else a for a in args))


class DownloadClient(ABC):
    """Uniform interface every client backend implements."""

    name: str = "base"

    @abstractmethod
    def test(self) -> bool:
        """Return True if the client is reachable and authenticated."""

    @abstractmethod
    def add(self, magnet_or_url: str, download_dir: Optional[str] = None) -> AddResult:
        ...

    @abstractmethod
    def list_transfers(self) -> list[Transfer]:
        ...

    @abstractmethod
    def files(self, transfer_id: str) -> list[TransferFile]:
        ...

    @abstractmethod
    def pause(self, transfer_id: str) -> None:
        ...

    @abstractmethod
    def resume(self, transfer_id: str) -> None:
        ...

    @abstractmethod
    def remove(self, transfer_id: str, delete_data: bool = False) -> None:
        ...


def fetch_torrent(url: str, timeout: int, max_hops: int = 5) -> tuple[str, str]:
    """Resolve an indexer download link to ('magnet', uri) or
    ('metainfo', base64 .torrent).

    Redirects are followed by hand. requests' own redirect handling raised
    InvalidSchema ("No connection adapters were found for 'magnet:...'") the
    moment a Location pointed at a magnet URI, so every Jackett link that
    resolves to a magnet failed to add — 38k grab_failed history rows in one
    month, and no grabs from trackers whose results carry no magneturl."""
    current = url
    for _ in range(max_hops + 1):
        if current.startswith("magnet:"):
            return "magnet", current
        try:
            resp = requests.get(current, timeout=timeout, allow_redirects=False)
        except requests.RequestException as e:
            raise DownloadClientError(f"couldn't fetch torrent: {e}") from None
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            if not loc:
                raise DownloadClientError(
                    f"couldn't fetch torrent: HTTP {resp.status_code} without a Location")
            current = loc if "://" in loc or loc.startswith("magnet:") \
                else requests.compat.urljoin(current, loc)
            continue
        if resp.status_code >= 400:
            raise DownloadClientError(f"couldn't fetch torrent: HTTP {resp.status_code}")
        body = resp.content or b""
        if body[:7] == b"magnet:":
            return "magnet", body.decode("utf-8", "ignore").strip()
        if not body.startswith(b"d"):
            # bencoded torrents start with 'd'; anything else is an error page
            raise DownloadClientError("couldn't fetch torrent: response is not a torrent file")
        return "metainfo", base64.b64encode(body).decode()
    raise DownloadClientError("couldn't fetch torrent: too many redirects")

"""Client factory: build the configured download client."""
from __future__ import annotations

from .base import (AddResult, DownloadClient, DownloadClientError, Transfer,
                   TransferFile)
from .transmission import TransmissionClient
from .qbittorrent import QBittorrentClient
from .deluge import DelugeClient

_CLIENTS = {
    "transmission": TransmissionClient,
    "qbittorrent": QBittorrentClient,
    "deluge": DelugeClient,
}


def make_client(kind: str, url: str, username: str = "", password: str = "",
                timeout: int = 30) -> DownloadClient:
    kind = (kind or "transmission").lower()
    cls = _CLIENTS.get(kind)
    if cls is None:
        raise DownloadClientError(
            f"Unknown download client '{kind}'. "
            f"Supported: {', '.join(_CLIENTS)}")
    # Deluge takes no username
    if kind == "deluge":
        return cls(url=url, password=password, timeout=timeout)
    return cls(url=url, username=username, password=password, timeout=timeout)


__all__ = ["make_client", "DownloadClient", "DownloadClientError",
           "Transfer", "TransferFile", "AddResult"]

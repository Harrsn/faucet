"""Download client abstraction.

Faucet talks to torrent clients through a small uniform interface so the rest
of the app never needs to know whether it's driving Transmission, qBittorrent,
or Deluge. Each backend implements DownloadClient; the factory in __init__
picks one from config.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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
    """Raised for any client-level failure (auth, connectivity, bad response)."""


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

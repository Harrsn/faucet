"""Transmission backend for the DownloadClient interface."""
from __future__ import annotations

import base64
import re
from typing import Optional

import requests

from .base import (AddResult, DownloadClient, DownloadClientError, Transfer, fetch_torrent,
                   TransferFile)

# Transmission status codes -> normalized labels
_STATUS = {0: "stopped", 1: "checking", 2: "checking", 3: "queued",
           4: "downloading", 5: "queued", 6: "seeding"}


_URL_MD = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")        # [text](url) markdown
_TRACKER_PREFIX = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*[-_.]*\s*|(?:https?://)?www\.[^\s]+\s*[-_.]+\s*)",
    re.IGNORECASE)


def _clean_torrent_name(name: str) -> str:
    """Strip tracker-site watermarks and any path-separator characters from a
    torrent name so it can't shred the on-disk folder path.

    Handles: '[www.UIndex.org](https://www.UIndex.org) - Show S01E01',
    'www.SomeTracker.net - Show S01E01', and bare embedded slashes.
    """
    if not name:
        return name
    cleaned = _URL_MD.sub(r"\1", name)         # [text](url) -> text
    cleaned = _TRACKER_PREFIX.sub("", cleaned)  # drop a leading site watermark
    cleaned = cleaned.replace("/", " ").replace("\\", " ")  # kill path seps
    cleaned = " ".join(cleaned.split()).strip(" -_.")
    return cleaned or name


class TransmissionClient(DownloadClient):
    name = "transmission"

    def __init__(self, url: str, username: str = "", password: str = "",
                 timeout: int = 30):
        self.url = url
        self.username = username
        self.password = password
        self.timeout = timeout
        self._session_id = ""

    # -- internal RPC with CSRF handshake --
    def _rpc(self, method: str, arguments: dict) -> dict:
        s = requests.Session()
        if self.username:
            s.auth = (self.username, self.password)
        headers = {}
        if self._session_id:
            headers["X-Transmission-Session-Id"] = self._session_id
        payload = {"method": method, "arguments": arguments}
        for _ in range(2):
            try:
                r = s.post(self.url, json=payload, headers=headers,
                           timeout=self.timeout)
            except requests.RequestException as e:
                raise DownloadClientError(f"Transmission unreachable: {e}")
            if r.status_code == 409:
                self._session_id = r.headers.get("X-Transmission-Session-Id", "")
                headers["X-Transmission-Session-Id"] = self._session_id
                continue
            if r.status_code == 401:
                raise DownloadClientError("Transmission auth failed (check user/pass).")
            try:
                data = r.json()
            except ValueError:
                raise DownloadClientError("Transmission returned non-JSON.")
            if data.get("result") != "success":
                raise DownloadClientError(f"Transmission: {data.get('result')}")
            return data.get("arguments", {})
        raise DownloadClientError("Transmission CSRF handshake failed.")

    def test(self) -> bool:
        self._rpc("session-get", {})
        return True

    def add(self, magnet_or_url: str, download_dir: Optional[str] = None) -> AddResult:
        args: dict = {"paused": False}
        if magnet_or_url.startswith("magnet:"):
            # magnets go straight to Transmission
            args["filename"] = magnet_or_url
        elif magnet_or_url.startswith("http"):
            # Indexer .torrent URLs often 302-redirect (e.g. Jackett -> tracker),
            # and Transmission won't follow that redirect. Fetch the torrent
            # ourselves and hand over the bytes — or the magnet the redirect
            # chain ends at (Jackett's /dl/ links for public trackers do that).
            kind, value = fetch_torrent(magnet_or_url, self.timeout)
            args["filename" if kind == "magnet" else "metainfo"] = value
        else:
            # raw torrent file contents
            args["metainfo"] = base64.b64encode(magnet_or_url.encode()).decode()
        if download_dir:
            args["download-dir"] = download_dir
        a = self._rpc("torrent-add", args)
        t = a.get("torrent-added") or a.get("torrent-duplicate") or {}
        tid = t.get("id")
        raw_name = t.get("name", "")
        # Tracker watermarks sometimes embed a URL in the torrent name, e.g.
        # '[www.UIndex.org](https://www.UIndex.org) - Show S01E01'. The '/' chars
        # become path separators and shred the download folder so the sorter
        # can't find it. Rename the torrent to a clean name at add time.
        clean = _clean_torrent_name(raw_name)
        if tid is not None and clean and clean != raw_name:
            try:
                self._rpc("torrent-rename-path",
                          {"ids": [tid], "path": raw_name, "name": clean})
                raw_name = clean
            except DownloadClientError:
                pass  # non-fatal; the sorter's recovery fallback still handles it
        return AddResult(id=str(tid) if tid is not None else None,
                         name=raw_name,
                         duplicate="torrent-duplicate" in a)

    def list_transfers(self) -> list[Transfer]:
        fields = ["id", "name", "percentDone", "rateDownload", "rateUpload",
                  "status", "eta", "uploadRatio", "totalSize", "errorString"]
        a = self._rpc("torrent-get", {"fields": fields})
        out = []
        for t in a.get("torrents", []):
            out.append(Transfer(
                id=str(t.get("id")),
                name=t.get("name", ""),
                percent=round(t.get("percentDone", 0) * 100, 1),
                down_rate=t.get("rateDownload", 0),
                up_rate=t.get("rateUpload", 0),
                status=_STATUS.get(t.get("status", 0), "?"),
                eta=t.get("eta", -1),
                ratio=round(t.get("uploadRatio", 0), 2),
                size=t.get("totalSize", 0),
                error=t.get("errorString") or None,
            ))
        return out

    def files(self, transfer_id: str) -> list[TransferFile]:
        a = self._rpc("torrent-get", {"ids": [int(transfer_id)],
                                      "fields": ["files", "fileStats"]})
        torrents = a.get("torrents", [])
        if not torrents:
            return []
        files = torrents[0].get("files", [])
        stats = torrents[0].get("fileStats", [])
        out = []
        for i, f in enumerate(files):
            length = f.get("length", 0)
            done = f.get("bytesCompleted", 0)
            out.append(TransferFile(
                name=f.get("name", "").split("/")[-1],
                path=f.get("name", ""),
                size=length,
                percent=round(done / length * 100, 1) if length else 0,
                wanted=stats[i].get("wanted", True) if i < len(stats) else True,
            ))
        return out

    def pause(self, transfer_id: str) -> None:
        self._rpc("torrent-stop", {"ids": [int(transfer_id)]})

    def resume(self, transfer_id: str) -> None:
        self._rpc("torrent-start", {"ids": [int(transfer_id)]})

    def remove(self, transfer_id: str, delete_data: bool = False) -> None:
        self._rpc("torrent-remove", {"ids": [int(transfer_id)],
                                     "delete-local-data": delete_data})

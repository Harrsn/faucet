"""Transmission backend for the DownloadClient interface."""
from __future__ import annotations

import base64
from typing import Optional

import requests

from .base import (AddResult, DownloadClient, DownloadClientError, Transfer,
                   TransferFile)

# Transmission status codes -> normalized labels
_STATUS = {0: "stopped", 1: "checking", 2: "checking", 3: "queued",
           4: "downloading", 5: "queued", 6: "seeding"}


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
            # ourselves (following redirects) and hand over the actual bytes.
            try:
                resp = requests.get(magnet_or_url, timeout=self.timeout,
                                    allow_redirects=True)
                resp.raise_for_status()
                body = resp.content
                # Some indexers redirect a .torrent link to a magnet; honor that.
                final = resp.url or ""
                if body[:7] == b"magnet:" or final.startswith("magnet:"):
                    args["filename"] = (body.decode("utf-8", "ignore")
                                        if body[:7] == b"magnet:" else final)
                else:
                    args["metainfo"] = base64.b64encode(body).decode()
            except requests.RequestException as e:
                raise DownloadClientError(f"couldn't fetch torrent: {e}")
        else:
            # raw torrent file contents
            args["metainfo"] = base64.b64encode(magnet_or_url.encode()).decode()
        if download_dir:
            args["download-dir"] = download_dir
        a = self._rpc("torrent-add", args)
        t = a.get("torrent-added") or a.get("torrent-duplicate") or {}
        return AddResult(id=str(t.get("id")) if t.get("id") is not None else None,
                         name=t.get("name", ""),
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

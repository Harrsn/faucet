"""qBittorrent backend (Web API v2)."""
from __future__ import annotations

from typing import Optional

import requests

from .base import (AddResult, DownloadClient, DownloadClientError, Transfer,
                   TransferFile)

# qBittorrent state strings -> normalized labels
_STATE = {
    "downloading": "downloading", "stalledDL": "downloading",
    "metaDL": "downloading", "forcedDL": "downloading",
    "uploading": "seeding", "stalledUP": "seeding", "forcedUP": "seeding",
    "pausedDL": "stopped", "pausedUP": "stopped",
    "checkingDL": "checking", "checkingUP": "checking", "checkingResumeData": "checking",
    "queuedDL": "queued", "queuedUP": "queued",
    "error": "error", "missingFiles": "error",
}


class QBittorrentClient(DownloadClient):
    name = "qbittorrent"

    def __init__(self, url: str, username: str = "", password: str = "",
                 timeout: int = 30):
        self.base = url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._s = requests.Session()
        self._authed = False

    def _login(self):
        try:
            r = self._s.post(f"{self.base}/api/v2/auth/login",
                             data={"username": self.username, "password": self.password},
                             timeout=self.timeout)
        except requests.RequestException as e:
            raise DownloadClientError(f"qBittorrent unreachable: {e}")
        if r.text.strip() != "Ok.":
            raise DownloadClientError("qBittorrent auth failed (check user/pass).")
        self._authed = True

    def _req(self, method: str, path: str, **kw):
        if not self._authed:
            self._login()
        try:
            r = self._s.request(method, f"{self.base}{path}", timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise DownloadClientError(f"qBittorrent request failed: {e}")
        if r.status_code == 403:           # session expired -> re-login once
            self._authed = False
            self._login()
            r = self._s.request(method, f"{self.base}{path}", timeout=self.timeout, **kw)
        return r

    def test(self) -> bool:
        self._login()
        return True

    def add(self, magnet_or_url: str, download_dir: Optional[str] = None) -> AddResult:
        # qBittorrent's add endpoint doesn't echo the hash; check hashes before/after.
        before = {t["hash"] for t in self._list_raw()}
        data = {"urls": magnet_or_url}
        if download_dir:
            data["savepath"] = download_dir
        r = self._req("POST", "/api/v2/torrents/add", data=data)
        if r.text.strip().lower() == "fails.":
            raise DownloadClientError("qBittorrent rejected the torrent.")
        after = self._list_raw()
        new = [t for t in after if t["hash"] not in before]
        if new:
            return AddResult(id=new[0]["hash"], name=new[0].get("name", ""))
        # already present -> duplicate
        return AddResult(id=None, name="", duplicate=True)

    def _list_raw(self) -> list[dict]:
        r = self._req("GET", "/api/v2/torrents/info")
        try:
            return r.json()
        except ValueError:
            return []

    def list_transfers(self) -> list[Transfer]:
        out = []
        for t in self._list_raw():
            eta = t.get("eta", -1)
            out.append(Transfer(
                id=t["hash"],
                name=t.get("name", ""),
                percent=round(t.get("progress", 0) * 100, 1),
                down_rate=t.get("dlspeed", 0),
                up_rate=t.get("upspeed", 0),
                status=_STATE.get(t.get("state", ""), "?"),
                eta=eta if eta and eta < 8640000 else -1,   # qb uses 8640000 for "infinity"
                ratio=round(t.get("ratio", 0), 2),
                size=t.get("size", 0),
                error="missing files" if t.get("state") == "missingFiles" else None,
            ))
        return out

    def files(self, transfer_id: str) -> list[TransferFile]:
        r = self._req("GET", "/api/v2/torrents/files", params={"hash": transfer_id})
        try:
            files = r.json()
        except ValueError:
            return []
        out = []
        for f in files:
            size = f.get("size", 0)
            out.append(TransferFile(
                name=f.get("name", "").split("/")[-1],
                path=f.get("name", ""),
                size=size,
                percent=round(f.get("progress", 0) * 100, 1),
                wanted=f.get("priority", 1) != 0,
            ))
        return out

    def pause(self, transfer_id: str) -> None:
        self._req("POST", "/api/v2/torrents/pause", data={"hashes": transfer_id})

    def resume(self, transfer_id: str) -> None:
        self._req("POST", "/api/v2/torrents/resume", data={"hashes": transfer_id})

    def remove(self, transfer_id: str, delete_data: bool = False) -> None:
        self._req("POST", "/api/v2/torrents/delete",
                  data={"hashes": transfer_id, "deleteFiles": str(delete_data).lower()})

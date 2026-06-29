"""Deluge backend (Web JSON-RPC via the deluge-web daemon)."""
from __future__ import annotations

from typing import Optional

import requests

from .base import (AddResult, DownloadClient, DownloadClientError, Transfer,
                   TransferFile)

# Deluge state strings -> normalized labels
_STATE = {
    "Downloading": "downloading", "Seeding": "seeding", "Paused": "stopped",
    "Checking": "checking", "Queued": "queued", "Error": "error",
    "Allocating": "checking", "Moving": "checking",
}


class DelugeClient(DownloadClient):
    name = "deluge"

    def __init__(self, url: str, password: str = "", timeout: int = 30, **_):
        # url points at deluge-web, e.g. http://host:8112
        self.base = url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self._s = requests.Session()
        self._rid = 0
        self._authed = False

    def _call(self, method: str, params: list):
        self._rid += 1
        try:
            r = self._s.post(f"{self.base}/json",
                             json={"method": method, "params": params, "id": self._rid},
                             timeout=self.timeout)
        except requests.RequestException as e:
            raise DownloadClientError(f"Deluge unreachable: {e}")
        try:
            data = r.json()
        except ValueError:
            raise DownloadClientError("Deluge returned non-JSON.")
        if data.get("error"):
            raise DownloadClientError(f"Deluge: {data['error'].get('message')}")
        return data.get("result")

    def _ensure_auth(self):
        if self._authed:
            return
        if not self._call("auth.login", [self.password]):
            raise DownloadClientError("Deluge auth failed (check password).")
        # make sure the web UI is connected to a daemon
        if not self._call("web.connected", []):
            hosts = self._call("web.get_hosts", [])
            if hosts:
                self._call("web.connect", [hosts[0][0]])
        self._authed = True

    def test(self) -> bool:
        self._ensure_auth()
        return True

    def add(self, magnet_or_url: str, download_dir: Optional[str] = None) -> AddResult:
        self._ensure_auth()
        options = {}
        if download_dir:
            options["download_location"] = download_dir
        before = set(self._hashes())
        if magnet_or_url.startswith("magnet:"):
            self._call("core.add_torrent_magnet", [magnet_or_url, options])
        else:
            self._call("core.add_torrent_url", [magnet_or_url, options])
        after = self._hashes()
        new = [h for h in after if h not in before]
        if new:
            name = self._call("core.get_torrent_status", [new[0], ["name"]]).get("name", "")
            return AddResult(id=new[0], name=name)
        return AddResult(id=None, name="", duplicate=True)

    def _hashes(self) -> list[str]:
        res = self._call("core.get_torrents_status", [{}, ["name"]]) or {}
        return list(res.keys())

    def list_transfers(self) -> list[Transfer]:
        self._ensure_auth()
        fields = ["name", "progress", "download_payload_rate", "upload_payload_rate",
                  "state", "eta", "ratio", "total_size", "message"]
        res = self._call("core.get_torrents_status", [{}, fields]) or {}
        out = []
        for h, t in res.items():
            out.append(Transfer(
                id=h,
                name=t.get("name", ""),
                percent=round(t.get("progress", 0), 1),
                down_rate=int(t.get("download_payload_rate", 0)),
                up_rate=int(t.get("upload_payload_rate", 0)),
                status=_STATE.get(t.get("state", ""), "?"),
                eta=int(t.get("eta", 0)) or -1,
                ratio=round(t.get("ratio", 0), 2),
                size=int(t.get("total_size", 0)),
                error=t.get("message") if t.get("state") == "Error" else None,
            ))
        return out

    def files(self, transfer_id: str) -> list[TransferFile]:
        self._ensure_auth()
        st = self._call("core.get_torrent_status",
                        [transfer_id, ["files", "file_progress", "file_priorities"]]) or {}
        files = st.get("files", [])
        prog = st.get("file_progress", [])
        prio = st.get("file_priorities", [])
        out = []
        for i, f in enumerate(files):
            size = f.get("size", 0)
            out.append(TransferFile(
                name=f.get("path", "").split("/")[-1],
                path=f.get("path", ""),
                size=size,
                percent=round((prog[i] if i < len(prog) else 0) * 100, 1),
                wanted=(prio[i] if i < len(prio) else 1) != 0,
            ))
        return out

    def pause(self, transfer_id: str) -> None:
        self._ensure_auth(); self._call("core.pause_torrent", [[transfer_id]])

    def resume(self, transfer_id: str) -> None:
        self._ensure_auth(); self._call("core.resume_torrent", [[transfer_id]])

    def remove(self, transfer_id: str, delete_data: bool = False) -> None:
        self._ensure_auth(); self._call("core.remove_torrent", [transfer_id, delete_data])

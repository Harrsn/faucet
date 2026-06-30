#!/usr/bin/env python3
"""Faucet post-process hook.

Invoked by the download client when a torrent completes. It:
  1. runs the sorter to file the media onto your library,
  2. appends an event for the UI feed,
  3. fires notifications,
  4. optionally removes the finished torrent (stops seeding).

It's client-agnostic: the completing client passes the download path and an id
via environment variables. Mappings for each client are below.

  Transmission:  TR_TORRENT_DIR / TR_TORRENT_NAME / TR_TORRENT_ID
  qBittorrent:   pass "%F" (content path) and "%I" (hash) -> CASCADE_PATH / CASCADE_ID
  Deluge:        Execute plugin passes torrentid + name + path -> CASCADE_* (see docs)

Configure via the same .env the app uses.
"""
from __future__ import annotations

import os
import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path

# Make the faucet package importable whether installed or run from source.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from faucet.config import config           # noqa: E402
from faucet.notify import notify           # noqa: E402
from faucet.clients import make_client, DownloadClientError  # noqa: E402
try:
    from faucet import db                  # noqa: E402
except Exception:                            # noqa: BLE001 - DB is optional for the hook
    db = None


def _path_size(path: str) -> int:
    """Total bytes of the completed download (file or dir tree)."""
    p = Path(path)
    try:
        if p.is_file():
            return p.stat().st_size
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        pass
    return 0


def record(kind: str, name: str, detail: str = "", size: int = 0):
    """Write both the JSONL event (live feed) and the DB history (stats)."""
    write_event(kind, name, detail)
    if db is not None:
        try:
            db.init()
            db.add_history(kind, name, detail, size)
        except Exception:                    # noqa: BLE001 - never break the hook
            pass


def resolve_completion():
    """Return (path, name, tid) from whichever client called us."""
    # Transmission
    td, tn = os.environ.get("TR_TORRENT_DIR"), os.environ.get("TR_TORRENT_NAME")
    if td and tn:
        return os.path.join(td, tn), tn, os.environ.get("TR_TORRENT_ID")
    # Generic (qBittorrent / Deluge / manual) via FAUCET_* (CASCADE_* still works)
    path = os.environ.get("FAUCET_PATH") or os.environ.get("CASCADE_PATH")
    name = (os.environ.get("FAUCET_NAME") or os.environ.get("CASCADE_NAME")
            or (os.path.basename(path) if path else ""))
    tid = os.environ.get("FAUCET_ID") or os.environ.get("CASCADE_ID")
    if path:
        return path, name, tid
    return None, None, None


def write_event(kind: str, name: str, msg: str):
    rec = {"ts": datetime.now().isoformat(timespec="seconds"),
           "event": kind, "name": name, "msg": msg}
    try:
        with open(config.events_file, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def main():
    path, name, tid = resolve_completion()
    if not path:
        print("No completion info in environment; nothing to do.", file=sys.stderr)
        return 0

    size = _path_size(path)
    record("completed", name, "download finished, sorting", size)
    if "completed" in config.notify_on:
        notify(config.notify_urls, "Download complete", name)

    # 1. sort — delegate to the sorter script, pointed at the completed path
    sorter = Path(__file__).resolve().parent / "sort.py"
    env = dict(os.environ, FAUCET_PATH=path, CASCADE_PATH=path)
    res = subprocess.run([sys.executable, str(sorter)], env=env)
    if res.returncode != 0:
        record("sort_failed", name, f"sort failed (rc={res.returncode})")
        if "failed" in config.notify_on:
            notify(config.notify_urls, "Sort failed", name)
        return res.returncode

    record("sorted", name, "filed onto library")
    if "sorted" in config.notify_on:
        notify(config.notify_urls, "Sorted to library", name)

    # 2. optional auto-remove
    if os.environ.get("REMOVE_ON_COMPLETE", "0") in ("1", "true", "yes") and tid:
        try:
            make_client(config.client_kind, config.client_url, config.client_user,
                        config.client_pass, config.request_timeout).remove(tid, True)
            record("removed", name, "removed from client (auto-cleanup)")
        except DownloadClientError as e:
            record("remove_failed", name, f"remove failed: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

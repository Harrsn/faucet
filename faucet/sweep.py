#!/usr/bin/env python3
"""Periodic catch-up sweep for the completed-downloads directory.

The completion hook (Transmission script-torrent-done -> faucet.hook) handles the
normal case. But a hook can miss: the client fires it before files finish moving,
a torrent is added/finished while the wrapper is misconfigured, or a manual grab
never triggers it. Those items sit in the complete/ dir, downloaded but never
filed. This sweep is the safety net — run it on a timer and it sorts anything the
hook missed.

It is SAFE to run repeatedly and on a live system:
  * Only processes top-level items under COMPLETE_DIR whose newest file hasn't
    changed for >= SETTLE_MINUTES, so anything still downloading is skipped.
  * Delegates to faucet.sort, whose place() is idempotent (already-filed files at
    the same size are skipped), so re-sweeping costs nothing and never duplicates.
  * Honors the same MEDIASORT_MODE / REMOVE_ON_COMPLETE env as the hook.

Runs INSIDE the faucet container (paths are the container's view):
    docker exec faucet python -m faucet.sweep
Add --dry-run to preview without moving anything.

Env:
  COMPLETE_DIR     dir to scan          (default: $DOWNLOAD_DIR/complete, else /downloads/complete)
  SWEEP_SETTLE_MIN minutes a release must be quiet before sorting   (default: 15)
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv", ".flv", ".webm"}


def _complete_dir() -> Path:
    explicit = os.environ.get("COMPLETE_DIR")
    if explicit:
        return Path(explicit)
    dl = os.environ.get("DOWNLOAD_DIR") or "/downloads"
    return Path(dl) / "complete"


def _newest_mtime(item: Path) -> float:
    """Most recent mtime across the item (file, or any file under a dir)."""
    if item.is_file():
        try:
            return item.stat().st_mtime
        except OSError:
            return 0.0
    newest = 0.0
    for p in item.rglob("*"):
        try:
            if p.is_file():
                newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return newest


def _has_video(item: Path) -> bool:
    if item.is_file():
        return item.suffix.lower() in VIDEO_EXTS
    for p in item.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            return True
    return False


def _sort_one(item: Path, dry: bool) -> int:
    """Invoke the existing sorter on one completed item (same path the hook uses)."""
    sorter = Path(__file__).resolve().parent / "sort.py"
    env = dict(os.environ, FAUCET_PATH=str(item), CASCADE_PATH=str(item))
    cmd = [sys.executable, str(sorter)]
    if dry:
        cmd.append("--dry-run")
    return subprocess.run(cmd, env=env).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="Catch-up sweep for completed downloads.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be sorted without moving anything.")
    ap.add_argument("--settle-min", type=int,
                    default=int(os.environ.get("SWEEP_SETTLE_MIN", "15")),
                    help="Minutes an item must be unmodified before sorting (default 15).")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("faucet.sweep")

    cdir = _complete_dir()
    if not cdir.exists():
        log.error("Complete dir not found: %s (is the NAS mounted in the container?)", cdir)
        return 1

    settle_secs = max(0, args.settle_min) * 60
    now = time.time()

    items = sorted(p for p in cdir.iterdir() if not p.name.startswith("."))
    if not items:
        log.info("Nothing in %s — clean.", cdir)
        return 0

    swept = skipped_active = skipped_novideo = failed = 0
    for item in items:
        if not _has_video(item):
            skipped_novideo += 1
            log.info("SKIP (no video): %s", item.name)
            continue
        age = now - _newest_mtime(item)
        if age < settle_secs:
            skipped_active += 1
            log.info("SKIP (still settling, %dm old < %dm): %s",
                     int(age // 60), args.settle_min, item.name)
            continue
        log.info("SORTING: %s", item.name)
        rc = _sort_one(item, args.dry_run)
        if rc == 0:
            swept += 1
        else:
            failed += 1
            log.warning("sort returned rc=%d for %s", rc, item.name)

    # one summary line; only record a real event if we actually did work
    log.info("Sweep done: %d sorted, %d still-active, %d no-video, %d failed (dry=%s).",
             swept, skipped_active, skipped_novideo, failed, args.dry_run)
    if swept and not args.dry_run:
        try:
            from .hook import write_event
            write_event("sweep", "catch-up sweep",
                        f"filed {swept} item(s) the hook had missed")
        except Exception:                              # noqa: BLE001
            pass
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

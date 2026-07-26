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

# Directory names that mean "this is not a finished release" — staging areas,
# partial-download trees, and client scratch dirs. A top-level item under
# complete/ whose name is one of these, OR which contains an `incomplete/`
# subtree, is never swept (those are in-progress or migration leftovers, not
# completed downloads). Matched case-insensitively.
EXCLUDE_NAMES = {"temp", "tmp", "incomplete", "incomplete_downloads",
                 ".incomplete", "partial", "_unpack", "_failed"}
# File suffixes that signal a partial download — their presence means the item
# isn't done, so skip the whole item.
PARTIAL_SUFFIXES = {".part", ".!qb", ".bts", ".dctmp"}


def _complete_dir() -> Path:
    explicit = os.environ.get("COMPLETE_DIR")
    if explicit:
        return Path(explicit)
    dl = os.environ.get("DOWNLOAD_DIR") or "/downloads"
    return Path(dl) / "complete"


def _is_excluded(item: Path) -> str | None:
    """Return a reason string if this item must NOT be swept, else None.

    Guards against in-progress and staging content that looks 'settled' by mtime
    but isn't a finished release (e.g. an old `temp/data/torrents/incomplete/`
    migration tree parked in complete/)."""
    if item.name.lower() in EXCLUDE_NAMES:
        return f"staging/in-progress dir name ('{item.name}')"
    if item.is_dir():
        for p in item.rglob("*"):
            # any nested incomplete/temp dir => not a clean release
            if p.is_dir() and p.name.lower() in EXCLUDE_NAMES:
                return f"contains an in-progress subtree ('{p.name}/')"
            if p.is_file() and p.suffix.lower() in PARTIAL_SUFFIXES:
                return f"contains a partial-download file ('{p.name}')"
    elif item.suffix.lower() in PARTIAL_SUFFIXES:
        return "partial-download file"
    return None


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


def run(dry: bool = False, settle_min: int | None = None) -> dict:
    """One sweep pass. Importable (the scheduler calls this every tick) as well
    as runnable from the CLI. Returns counters; {'error': ...} when the
    complete dir is unreachable."""
    log = logging.getLogger("faucet.sweep")
    if settle_min is None:
        settle_min = int(os.environ.get("SWEEP_SETTLE_MIN", "15"))

    cdir = _complete_dir()
    if not cdir.exists():
        log.warning("Complete dir not found: %s (is the NAS mounted in the container?)", cdir)
        return {"error": "complete dir not found", "swept": 0, "failed": 0}

    settle_secs = max(0, settle_min) * 60
    now = time.time()

    items = sorted(p for p in cdir.iterdir() if not p.name.startswith("."))
    if not items:
        return {"swept": 0, "failed": 0, "skipped": 0}

    swept = skipped_active = skipped_novideo = skipped_excluded = failed = 0
    for item in items:
        reason = _is_excluded(item)
        if reason:
            skipped_excluded += 1
            log.info("SKIP (%s): %s", reason, item.name)
            continue
        if not _has_video(item):
            skipped_novideo += 1
            log.info("SKIP (no video): %s", item.name)
            continue
        age = now - _newest_mtime(item)
        if age < settle_secs:
            skipped_active += 1
            log.info("SKIP (still settling, %dm old < %dm): %s",
                     int(age // 60), settle_min, item.name)
            continue
        log.info("SORTING: %s", item.name)
        rc = _sort_one(item, dry)
        if rc == 0:
            swept += 1
        else:
            failed += 1
            log.warning("sort returned rc=%d for %s", rc, item.name)

    if swept or failed:
        log.info("Sweep done: %d sorted, %d still-active, %d excluded, %d no-video, "
                 "%d failed (dry=%s).",
                 swept, skipped_active, skipped_excluded, skipped_novideo,
                 failed, dry)
    if swept and not dry:
        try:
            from .hook import write_event
            write_event("sweep", "catch-up sweep",
                        f"filed {swept} item(s) the hook had missed")
        except Exception:                              # noqa: BLE001
            pass
    return {"swept": swept, "failed": failed,
            "skipped": skipped_active + skipped_excluded + skipped_novideo}


def main() -> int:
    ap = argparse.ArgumentParser(description="Catch-up sweep for completed downloads.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be sorted without moving anything.")
    ap.add_argument("--settle-min", type=int,
                    default=int(os.environ.get("SWEEP_SETTLE_MIN", "15")),
                    help="Minutes an item must be unmodified before sorting (default 15).")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = run(dry=args.dry_run, settle_min=args.settle_min)
    if r.get("error"):
        return 1
    return 1 if r.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())

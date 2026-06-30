"""Filesystem browser + mover, scoped to a single configured root.

SECURITY MODEL (this endpoint lists and MOVES files on the host, so every path
is treated as hostile input):

  * One configured root (BROWSE_ROOT, default: the library root's mount). Nothing
    outside it is ever listed, read, or written.
  * Every incoming path is resolved with realpath() (collapsing '..' and
    following symlinks) and then checked to be inside the resolved root. A path
    that escapes — via '..', an absolute path, or a symlink pointing outside —
    is rejected. There is no code path that touches a file without this check.
  * Moves are validated on BOTH source and destination; both must resolve inside
    the root. We never overwrite an existing destination.

All functions return plain dicts (never raise to the caller) so the API layer
can surface a clean error.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import config

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".webm"}


def _root() -> Path:
    """The single allowed root, resolved. Configurable via BROWSE_ROOT; defaults
    to the library root (which in the container is the NAS mount)."""
    raw = (os.environ.get("BROWSE_ROOT")
           or getattr(config, "library_root", None)
           or os.environ.get("LIBRARY_ROOT")
           or "/library")
    try:
        return Path(raw).resolve()
    except OSError:
        return Path("/library")


def _safe(p: str | Path) -> Path | None:
    """Resolve p and return it ONLY if it lives inside the root. Else None.
    This is the single chokepoint every operation goes through."""
    root = _root()
    try:
        # Resolve against root for relative paths; absolute paths resolve as-is
        # but must still land inside root.
        candidate = (root / p) if not str(p).startswith("/") else Path(p)
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved == root or root in resolved.parents:
        return resolved
    return None


def _rel(p: Path) -> str:
    """Path relative to root, for display (leading '' means root itself)."""
    root = _root()
    try:
        r = p.resolve().relative_to(root)
        return "" if str(r) == "." else str(r)
    except ValueError:
        return ""


def list_dir(rel_path: str = "") -> dict:
    """List one directory under the root. Returns entries with name, kind
    (dir|video|file), size, and the rel path for navigation."""
    target = _safe(rel_path)
    if target is None:
        return {"error": "path outside the allowed root"}
    if not target.exists():
        return {"error": "path not found"}
    if not target.is_dir():
        return {"error": "not a directory"}

    entries = []
    try:
        for child in sorted(target.iterdir(),
                            key=lambda c: (not c.is_dir(), c.name.lower())):
            try:
                is_dir = child.is_dir()
                size = child.stat().st_size if not is_dir else 0
            except OSError:
                continue
            kind = ("dir" if is_dir else
                    "video" if child.suffix.lower() in VIDEO_EXTS else "file")
            entries.append({
                "name": child.name,
                "kind": kind,
                "size": size,
                "rel": _rel(child),
            })
    except OSError as e:
        return {"error": f"couldn't read directory: {e}"}

    parent = _rel(target.parent) if target != _root() else None
    at_root = (target == _root())
    return {
        "root": str(_root()),
        "cwd": _rel(target),
        "parent": parent,
        "at_root": at_root,
        "entries": entries,
    }


def move(src_rel: str, dest_dir_rel: str, new_name: str | None = None) -> dict:
    """Move a file/dir to a destination directory, both validated inside root.
    Never overwrites. Creates the destination dir if needed (inside root)."""
    src = _safe(src_rel)
    if src is None:
        return {"error": "source outside the allowed root"}
    if not src.exists():
        return {"error": "source not found"}
    dest_dir = _safe(dest_dir_rel)
    if dest_dir is None:
        return {"error": "destination outside the allowed root"}

    name = new_name or src.name
    # reject a name that tries to traverse
    if "/" in name or "\\" in name or name in ("..", "."):
        return {"error": "invalid destination name"}
    dest = dest_dir / name
    # final safety: the assembled destination must still be inside root
    if _safe(_rel(dest_dir) + "/" + name if _rel(dest_dir) else name) is None:
        return {"error": "destination escapes the allowed root"}
    if dest.exists():
        return {"error": "destination already exists (won't overwrite)"}

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        # same-fs rename is instant; cross-fs falls back to copy+del via shutil
        try:
            os.rename(str(src), str(dest))
        except OSError:
            shutil.move(str(src), str(dest))
    except (OSError, shutil.Error) as e:
        return {"error": f"move failed: {e}"}
    return {"ok": True, "src": _rel(src), "dest": _rel(dest)}


def mkdir(parent_rel: str, name: str) -> dict:
    """Create a subdirectory under a validated parent (inside root)."""
    if "/" in name or "\\" in name or name in ("..", "."):
        return {"error": "invalid directory name"}
    parent = _safe(parent_rel)
    if parent is None:
        return {"error": "parent outside the allowed root"}
    target = _safe((_rel(parent) + "/" + name) if _rel(parent) else name)
    if target is None:
        return {"error": "path escapes the allowed root"}
    if target.exists():
        return {"error": "already exists"}
    try:
        target.mkdir(parents=True)
    except OSError as e:
        return {"error": f"couldn't create: {e}"}
    return {"ok": True, "rel": _rel(target)}

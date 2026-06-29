#!/usr/bin/env python3
"""
mediasort.py — parse, rename, and file movie/TV releases into a Plex/Jellyfin tree.

Modes:
  Manual:  mediasort.py /path/to/file_or_dir [more paths...]
  Hook:    invoked by transmission-daemon with TR_TORRENT_DIR / TR_TORRENT_NAME
           in the environment (no args needed).

Behavior:
  Movies -> <MEDIA_ROOT>/movies/<Title> (<Year>)/<Title> (<Year>).<ext>
  TV     -> <MEDIA_ROOT>/tvshows/<Show>/Season NN/<Show> - SNNENN.<ext>

  - Operates on video files above MIN_SIZE_MB (skips samples/junk).
  - Pulls along same-basename subtitle sidecars (.srt/.ass/.sub).
  - HARDLINKs by default (instant, keeps seeding intact, no double disk use).
    Falls back to copy across filesystems. Set MODE="move" to move instead.
  - Dry-run with --dry-run or DRY_RUN=1.
  - Idempotent: skips if destination already exists with same size.

Requires: pip install guessit
"""

import os
import sys
import shutil
import logging
import argparse
from pathlib import Path

try:
    from guessit import guessit
except ImportError:
    sys.stderr.write("guessit not installed. Run: pip3 install guessit\n")
    sys.exit(2)

try:
    from faucet.classify import classify, dest_folder
except Exception:                                # noqa: BLE001 - sorter can run standalone
    classify = None
    dest_folder = None

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
MEDIA_ROOT = Path(os.environ.get("LIBRARY_ROOT", os.environ.get("MEDIA_ROOT", "/library")))
MOVIES_DIR = MEDIA_ROOT / "movies"
TV_DIR = MEDIA_ROOT / "tvshows"
GAMES_DIR = MEDIA_ROOT / "games"
OTHER_DIR = MEDIA_ROOT / "other"

# Non-video content (games, software) is filed as a whole release rather than
# per-file. These extensions/markers identify game/disc releases.
GAME_EXTS = {".iso", ".bin", ".cue", ".nsp", ".xci", ".rom", ".pkg", ".rvz",
             ".wbfs", ".chd", ".rpx", ".cia", ".3ds", ".nds", ".gba"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz"}

MODE = os.environ.get("MEDIASORT_MODE", "auto")  # auto | hardlink | copy | move
MIN_SIZE_MB = int(os.environ.get("MEDIASORT_MIN_MB", "50"))
LOG_FILE = os.environ.get("MEDIASORT_LOG", "/var/log/mediasort.log")

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}

# Characters illegal on most filesystems (incl. CIFS/Windows-backed shares).
ILLEGAL = '<>:"/\\|?*'


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def setup_logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(LOG_FILE))
    except (PermissionError, FileNotFoundError):
        pass  # no log file access (e.g. running unprivileged) — stdout only
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def sanitize(name: str) -> str:
    """Strip filesystem-illegal chars; collapse whitespace; trim trailing dots."""
    cleaned = "".join(c for c in name if c not in ILLEGAL)
    cleaned = " ".join(cleaned.split())
    return cleaned.rstrip(". ")


def iter_video_files(root: Path):
    """Yield video files under root above the size threshold."""
    if root.is_file():
        candidates = [root]
    else:
        candidates = [p for p in root.rglob("*") if p.is_file()]
    for p in candidates:
        if p.suffix.lower() not in VIDEO_EXTS:
            continue
        if p.stat().st_size < MIN_SIZE_MB * 1024 * 1024:
            logging.info("SKIP (under %dMB): %s", MIN_SIZE_MB, p.name)
            continue
        yield p


def find_sidecars(video: Path):
    """Find subtitle files sharing the video's basename stem in the same dir."""
    stem = video.stem
    for p in video.parent.iterdir():
        if (
            p.is_file()
            and p.suffix.lower() in SUB_EXTS
            and p.stem.startswith(stem)
        ):
            yield p


def best_parse_source(video: Path):
    """
    Build the best string to feed guessit. Release metadata (show, season,
    episode, proper casing) lives in the *release folder*, not always the inner
    file — which may be generic ('info.mkv') or buried under junk subdirs like
    'info/', 'sample/', 'subs/'. Walk up the ancestry, skip junk and generic
    dirs, and prepend the first ancestor that actually parses as media so the
    inner filename can't drag the result to a bogus title.
    """
    # Dirs that never carry useful release info.
    JUNK = {"downloads", "incomplete", "complete", "info", "sample", "samples",
            "subs", "subtitles", "extras", "featurettes", "proof", "screens"}

    candidates = []
    for anc in video.parents:
        name = anc.name
        if not name or name.lower() in JUNK:
            continue
        # Looks like a release name if it has separators and some length.
        if any(c in name for c in ".-_ ") and len(name) >= 6:
            candidates.append(name)
        # Don't climb past the download root once we have something.
        if len(candidates) >= 2:
            break

    # Prefer the richest candidate: the one guessit can pull a season/episode
    # or year from. Fall back to the longest, then to the bare filename.
    best = None
    for c in candidates:
        g = guessit(c)
        if g.get("type") == "episode" and g.get("season") is not None \
                and g.get("episode") is not None:
            best = c
            break
        if g.get("type") == "movie" and g.get("year"):
            best = best or c
    if best is None and candidates:
        best = max(candidates, key=len)

    return f"{best}/{video.name}" if best else video.name


def plan_destination(video: Path):
    """Return (dest_dir, dest_basename) or None if unparseable."""
    info = guessit(best_parse_source(video))
    vtype = info.get("type")
    ext = video.suffix.lower()

    if vtype == "movie":
        title = info.get("title")
        if not title:
            return None
        year = info.get("year")
        folder = sanitize(f"{title} ({year})" if year else title)
        base = folder
        return MOVIES_DIR / folder, base + ext

    if vtype == "episode":
        show = info.get("title")
        season = info.get("season")
        episode = info.get("episode")
        if show is None or season is None or episode is None:
            return None
        # guessit can return a list for multi-episode files
        if isinstance(episode, list):
            ep_tag = "".join(f"E{e:02d}" for e in episode)
        else:
            ep_tag = f"E{int(episode):02d}"
        show_s = sanitize(show)
        dest_dir = TV_DIR / show_s / f"Season {int(season):02d}"
        base = sanitize(f"{show_s} - S{int(season):02d}{ep_tag}")
        return dest_dir, base + ext

    return None


def place(src: Path, dest: Path, dry: bool):
    """Place src at dest using the best available method. Idempotent on equal size.

    Strategy (MODE='auto', the default):
      1. hardlink — one copy of bytes, source kept so torrents keep seeding.
         Works only on the same *local* filesystem (not CIFS/SMB).
      2. move (rename) — instant, no duplication, when 1 isn't supported but
         src and dest share a filesystem (e.g. both on the same NAS share).
         Source disappears, so seeding of that torrent stops.
      3. copy — last resort, only when src and dest are genuinely on different
         filesystems. This is the one that duplicates bytes; we avoid it unless
         nothing else works.
    Explicit MODE='move'/'copy'/'hardlink' forces a single method.
    """
    if dest.exists() and dest.stat().st_size == src.stat().st_size:
        logging.info("EXISTS (same size), skipping: %s", dest)
        return
    if dry:
        logging.info("DRY-RUN %s -> %s", src, dest)
        return

    dest.parent.mkdir(parents=True, exist_ok=True)

    # explicit single-method modes
    if MODE == "move":
        shutil.move(str(src), str(dest))
        logging.info("MOVED %s -> %s", src.name, dest)
        return
    if MODE == "copy":
        if dest.exists():
            dest.unlink()
        shutil.copy2(src, dest)
        logging.info("COPIED %s -> %s", src.name, dest)
        return

    # auto / hardlink: try hardlink, then move (same-fs rename), then copy.
    # 1. hardlink (keeps seeding, zero extra space — same local fs only)
    if MODE in ("auto", "hardlink"):
        try:
            if dest.exists():
                dest.unlink()
            os.link(src, dest)
            logging.info("LINKED %s -> %s", src.name, dest)
            return
        except OSError:
            logging.info("hardlink not supported here (likely CIFS/SMB)")

    # 2. move via rename — instant and dup-free when src/dest share a filesystem
    #    (your case: download dir and library both on the same NAS share).
    try:
        if dest.exists():
            dest.unlink()
        os.rename(src, dest)
        logging.info("MOVED (same-fs rename) %s -> %s", src.name, dest)
        return
    except OSError:
        logging.info("same-fs rename not possible (cross-filesystem) — copying")

    # 3. copy — genuinely different filesystems; the only case that duplicates.
    if dest.exists():
        dest.unlink()
    shutil.copy2(src, dest)
    logging.info("COPIED (cross-fs) %s -> %s", src.name, dest)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def resolve_inputs(args):
    """CLI paths if given, else Transmission hook env vars."""
    if args.paths:
        return [Path(p) for p in args.paths]
    cp = os.environ.get("FAUCET_PATH") or os.environ.get("CASCADE_PATH")
    if cp:
        return [Path(cp)]
    td = os.environ.get("TR_TORRENT_DIR")
    tn = os.environ.get("TR_TORRENT_NAME")
    if td and tn:
        return [Path(td) / tn]
    return []


def handle_game(root: Path, platform: str | None, dry: bool) -> bool:
    """File a whole game/software release into /games/<Platform>/<Name>.

    Games aren't single video files — they're ISOs, archives, or folders of
    files that must stay together. So we move/link the entire release directory
    (or file) intact rather than picking through it. No renaming: game release
    names carry version/scene info worth preserving.
    """
    sub = sanitize(platform) if platform else "PC"
    name = sanitize(root.stem if root.is_file() else root.name)
    dest = GAMES_DIR / sub / name
    if dest.exists():
        logging.info("GAME EXISTS, skipping: %s", dest)
        return False
    if dry:
        logging.info("DRY-RUN game %s -> %s", root.name, dest)
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if MODE == "move":
            shutil.move(str(root), str(dest))
        else:
            # try same-fs rename first (instant, no dup), then copy the tree.
            try:
                os.rename(str(root), str(dest))
            except OSError:
                if root.is_dir():
                    shutil.copytree(str(root), str(dest))
                else:
                    shutil.copy2(str(root), str(dest))
        logging.info("GAME filed %s -> %s", root.name, dest)
        return True
    except (OSError, shutil.Error) as e:
        logging.error("game file failed for %s: %s", root.name, e)
        return False


def release_is_game(root: Path) -> bool:
    """Heuristic: does this release look like a game/disc rather than video?
    Checks for game/disc file extensions anywhere in the tree."""
    files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
    for p in files:
        if p.suffix.lower() in GAME_EXTS:
            return True
    # archive-only release with no video inside also leans game/software
    has_video = any(p.suffix.lower() in VIDEO_EXTS for p in files)
    has_archive = any(p.suffix.lower() in ARCHIVE_EXTS for p in files)
    return has_archive and not has_video


def main():
    ap = argparse.ArgumentParser(description="Sort media into Plex/Jellyfin tree.")
    ap.add_argument("paths", nargs="*", help="File(s) or dir(s) to process.")
    ap.add_argument("--dry-run", action="store_true", help="Show actions only.")
    args = ap.parse_args()

    setup_logging()
    dry = args.dry_run or os.environ.get("DRY_RUN") == "1"

    inputs = resolve_inputs(args)
    if not inputs:
        logging.error("No input paths (no args and no TR_TORRENT_* env). Exiting.")
        sys.exit(1)

    if not MEDIA_ROOT.exists():
        logging.error("MEDIA_ROOT %s not present — is the NAS mounted?", MEDIA_ROOT)
        sys.exit(1)

    processed = 0
    for root in inputs:
        if not root.exists():
            logging.warning("Input not found: %s", root)
            continue

        # Classify the release first. Games/software file as a whole release;
        # video falls through to the existing per-file movie/TV logic.
        ctype = None
        platform = None
        if classify is not None:
            k = classify(root.stem if root.is_file() else root.name)
            ctype, platform = k["type"], k["platform"]

        if ctype == "game" or release_is_game(root):
            if handle_game(root, platform, dry):
                processed += 1
            continue

        for video in iter_video_files(root):
            plan = plan_destination(video)
            if not plan:
                logging.warning("UNPARSEABLE, skipping: %s", video.name)
                continue
            dest_dir, dest_name = plan
            dest = dest_dir / dest_name
            place(video, dest, dry)
            for sub in find_sidecars(video):
                place(sub, dest_dir / (dest.stem + sub.suffix), dry)
            processed += 1

    logging.info("Done. %d video file(s) handled (mode=%s, dry=%s).",
                 processed, MODE, dry)


if __name__ == "__main__":
    main()

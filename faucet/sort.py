#!/usr/bin/env python3
"""
sort.py — parse, rename, and file movie/TV releases into a Plex/Jellyfin tree.

Modes:
  Manual:  sort.py /path/to/file_or_dir [more paths...]
  Hook:    run by faucet.hook / faucet.sweep with FAUCET_PATH (or Transmission's
           TR_TORRENT_DIR / TR_TORRENT_NAME) in the environment.

Layout:
  Movies  -> <LIBRARY_ROOT>/movies/<Title> (<Year>)/<Title> (<Year>).<ext>
             multi-part:  <Title> (<Year>) - pt1.<ext>, - pt2 ...
             extras:      <Title> (<Year>)/Featurettes/<name>.<ext>  (Plex folders)
  TV      -> <LIBRARY_ROOT>/tvshows/<Show>/Season NN/<Show> - SNNENN.<ext>
  Subs    -> next to their video, keeping language/flag tags:
             <Title> (<Year>).en.forced.srt

Safety model (every rule here exists because its absence lost data):
  * Nothing is ever written at its final path until it is complete: copies go
    to a hidden '.<name>.<pid>.faucet-partial' beside the destination, are fsynced and
    size-checked, then renamed into place.
  * An existing library file is replaced only when the incoming release is
    PROVABLY better (faucet.quality.is_better) against the quality recorded
    when the existing file was sorted (library_files table). Unknown on either
    side keeps the existing file.
  * Samples are skipped; extras go to Plex extras folders; two videos that
    would land on the same path never overwrite each other.
  * When the release is being consumed (MEDIASORT_MODE=move, or the hook will
    delete it via REMOVE_ON_COMPLETE), anything that could not be filed is
    moved to a quarantine dir (default: <release parent>/_failed, which the
    sweep skips) instead of being deleted with the torrent.

Exit codes (the hook only removes the torrent on 0, 4 or 5):
  0  done; everything of value was filed (or the release is left seeding)
  1  fatal before any work (no inputs, library not mounted)
  2  transient failure; the release was left in place so it can be retried
  4  done; some content was quarantined for review
  5  suspicious: a media release that is only executables — quarantined with
     its executables renamed '*.faucet-blocked' (see faucet/safety.py)

Env:
  LIBRARY_ROOT / MEDIA_ROOT   library root (default /library)
  MEDIASORT_MODE              auto | hardlink | copy | move (default auto)
  MEDIASORT_MIN_MB            smallest video treated as content (default 50)
  QUARANTINE_DIR              override the quarantine location
  REMOVE_ON_COMPLETE          same flag the hook uses
  DRY_RUN=1 / --dry-run       log actions only
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Run as a script (the hook does `python /app/faucet/sort.py`), sys.path[0] is
# the package dir itself, so `import faucet...` silently failed in production.
_PKG_PARENT = str(Path(__file__).resolve().parent.parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

try:
    from guessit import guessit
except ImportError:
    sys.stderr.write("guessit not installed. Run: pip3 install guessit\n")
    sys.exit(1)

try:
    from faucet.classify import classify
except Exception:                                # noqa: BLE001 - sorter can run standalone
    classify = None

try:
    from faucet.quality import RES_RANK, detect_cam, detect_quality, is_better
except Exception:                                # noqa: BLE001 - standalone: never replace
    RES_RANK = {}

    def detect_quality(name):
        return None

    def detect_cam(name):
        return False

    def is_better(*_a):
        return False

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
MEDIA_ROOT = Path(os.environ.get("LIBRARY_ROOT", os.environ.get("MEDIA_ROOT", "/library")))
MOVIES_DIR = MEDIA_ROOT / "movies"
TV_DIR = MEDIA_ROOT / "tvshows"
GAMES_DIR = MEDIA_ROOT / "games"
OTHER_DIR = MEDIA_ROOT / "other"

MODE = os.environ.get("MEDIASORT_MODE", "auto")  # auto | hardlink | copy | move
MIN_SIZE_MB = int(os.environ.get("MEDIASORT_MIN_MB", "50"))
LOG_FILE = os.environ.get("MEDIASORT_LOG", "/var/log/mediasort.log")
QUARANTINE_NAME = "_failed"                      # faucet.sweep skips this name

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_RETRY = 2
EXIT_QUARANTINED = 4
EXIT_SUSPICIOUS = 5

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz"}
# Unambiguous console/ROM formats. .iso/.bin/.cue are also used for movie and
# software disc images, so on their own they don't make a release a game.
CONSOLE_EXTS = {".nsp", ".xci", ".rom", ".pkg", ".rvz", ".wbfs", ".chd", ".rpx",
                ".cia", ".3ds", ".nds", ".gba"}
DISC_EXTS = {".iso", ".bin", ".cue"}
GAME_EXTS = CONSOLE_EXTS | DISC_EXTS
# Tracker/scene clutter that's safe to delete with a release. Anything NOT
# listed (archives, disc images, audio, unknown formats) is kept, whatever its
# size: a size heuristic once classed small ISOs and RAR volumes as junk.
JUNK_EXTS = {".txt", ".nfo", ".url", ".sfv", ".jpg", ".jpeg", ".png", ".gif",
             ".webp", ".md5", ".sha1", ".srr", ".srs", ".diz", ".exe", ".lnk",
             ".html", ".htm", ".db", ".ini", ".torrent", ".log", ".ds_store"}

# Characters illegal on most filesystems (incl. CIFS/Windows-backed shares).
ILLEGAL = '<>:"/\\|?*'

PARTIAL_SUFFIX = ".faucet-partial"
STALE_PARTIAL_SECS = 30 * 60
COPY_CHUNK = 16 * 1024 * 1024

# place() outcomes
PLACED, EXISTS, KEPT, DRY = "placed", "exists", "kept", "dry"

# Plex local-extras folders, keyed by normalized directory name
EXTRA_DIRS = {
    "behind the scenes": "Behind The Scenes", "behindthescenes": "Behind The Scenes",
    "deleted scenes": "Deleted Scenes", "deleted": "Deleted Scenes",
    "featurettes": "Featurettes", "featurette": "Featurettes",
    "interviews": "Interviews", "interview": "Interviews",
    "scenes": "Scenes", "shorts": "Shorts",
    "trailers": "Trailers", "trailer": "Trailers",
    "extras": "Other", "extra": "Other", "bonus": "Other",
    "bonus features": "Other", "special features": "Other", "other": "Other",
}
# Plex inline-extra suffixes ("Movie-trailer.mkv"). Deliberately a suffix match
# only: a bare "trailer" token would swallow every Trailer Park Boys episode.
_INLINE_EXTRA = re.compile(r"-(behindthescenes|deleted|featurette|interview|trailer)$", re.IGNORECASE)
_INLINE_KIND = {"behindthescenes": "Behind The Scenes", "deleted": "Deleted Scenes",
                "featurette": "Featurettes", "interview": "Interviews",
                "trailer": "Trailers"}
_SAMPLE_STEM = re.compile(r"(^|[ ._\-\[(])sample($|[ ._\-\])])", re.IGNORECASE)
_SUB_DIRS = {"subs", "subtitles", "sub"}
_CAM_SOURCES = {"camera", "hd camera", "telesync", "hd telesync", "telecine",
                "hd telecine", "screener"}


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


def _norm(name: str) -> str:
    return " ".join(re.split(r"[\s._\-]+", name.lower())).strip()


def _truthy(key: str) -> bool:
    return os.environ.get(key, "0").strip().lower() in ("1", "true", "yes", "on")


def _files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    out = []
    for p in root.rglob("*"):
        try:
            if p.is_file() and not p.name.endswith(PARTIAL_SUFFIX):
                out.append(p)
        except OSError:
            continue                              # vanished mid-walk (NAS hiccup)
    return sorted(out)


def _rel_dirs(root: Path, p: Path) -> tuple[str, ...]:
    if root.is_file() or root == p:
        return ()
    try:
        return p.relative_to(root).parts[:-1]
    except ValueError:
        return ()


def iter_video_files(root: Path):
    """Yield video files under root above the size threshold."""
    for p in _files(root):
        if p.suffix.lower() not in VIDEO_EXTS:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size >= MIN_SIZE_MB * 1024 * 1024:
            yield p


def is_sample(root: Path, video: Path) -> bool:
    if any(_norm(d) in ("sample", "samples") for d in _rel_dirs(root, video)):
        return True
    return bool(_SAMPLE_STEM.search(video.stem))


def extra_kind(root: Path, video: Path) -> str | None:
    """Plex extras folder for this video, or None if it isn't an extra."""
    for d in _rel_dirs(root, video):
        kind = EXTRA_DIRS.get(_norm(d))
        if kind:
            return kind
    m = _INLINE_EXTRA.search(video.stem)
    return _INLINE_KIND[m.group(1).lower()] if m else None


def find_sidecars(video: Path):
    """Subtitle files next to the video whose name is the video's stem, or the
    stem followed by '.<tags>' (Movie.en.srt, Movie.en.forced.srt)."""
    stem = video.stem
    try:
        entries = list(video.parent.iterdir())
    except OSError:
        return
    for p in entries:
        if (p.is_file() and p.suffix.lower() in SUB_EXTS
                and (p.stem == stem or p.stem.startswith(stem + "."))):
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
    junk = {"downloads", "incomplete", "complete", "info", "sample", "samples",
            "subs", "subtitles", "extras", "featurettes", "proof", "screens",
            QUARANTINE_NAME}

    candidates = []
    for anc in video.parents:
        name = anc.name
        if not name or name.lower() in junk:
            continue
        if any(c in name for c in ".-_ ") and len(name) >= 6:
            candidates.append(name)
        if len(candidates) >= 2:
            break

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


# ----------------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------------
@dataclass
class Plan:
    src: Path
    dest: Path
    kind: str                       # movie | episode | extra
    anchor: Path                    # folder extras attach under
    size: int = 0
    part: int | None = None
    quality: str | None = None
    is_cam: bool = False
    release: str = ""
    role: str = "feature"           # feature | part | extra
    note: str = ""


def _as_int(v):
    if isinstance(v, list):
        v = v[0] if v else None
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _plan(video: Path) -> Plan | None:
    source = best_parse_source(video)
    info = guessit(source)
    vtype = info.get("type")
    ext = video.suffix.lower()
    res = info.get("screen_size")
    quality = res if res in RES_RANK and res else detect_quality(source)
    src_tag = str(info.get("source") or "").lower()
    is_cam = detect_cam(source) or src_tag in _CAM_SOURCES
    part = _as_int(info.get("cd")) or _as_int(info.get("part"))

    if vtype == "movie":
        title = info.get("title")
        if not title:
            return None
        year = info.get("year")
        if not year:
            # 'Title (Year)' is the whole naming contract, and a yearless
            # "movie" is usually a misparse (a bare 'ep.mkv' inside an
            # episode folder parses as a movie titled "ep")
            return None
        folder = sanitize(f"{title} ({year})")
        if not folder:
            return None
        dest_dir = MOVIES_DIR / folder
        return Plan(video, dest_dir / (folder + ext), "movie", dest_dir,
                    part=part, quality=quality, is_cam=is_cam, release=source)

    if vtype == "episode":
        show, season, episode = info.get("title"), info.get("season"), info.get("episode")
        if show is None or season is None or episode is None:
            return None
        # a season range spans folders — can't be filed as one episode
        if isinstance(season, list):
            return None
        if isinstance(episode, list):
            ep_tag = "".join(f"E{int(e):02d}" for e in episode)
        else:
            ep_tag = f"E{int(episode):02d}"
        show_s = sanitize(str(show))
        if not show_s:
            return None
        dest_dir = TV_DIR / show_s / f"Season {int(season):02d}"
        base = sanitize(f"{show_s} - S{int(season):02d}{ep_tag}")
        return Plan(video, dest_dir / (base + ext), "episode", TV_DIR / show_s,
                    quality=quality, is_cam=is_cam, release=source)
    return None


def plan_destination(video: Path):
    """Return (dest_dir, dest_basename) or None if unparseable."""
    p = _plan(video)
    return (p.dest.parent, p.dest.name) if p else None


def _resolve_group(plans: list[Plan], unfiled: list) -> list[Plan]:
    """Several videos in one release planned onto the same path. Numbered parts
    (CD1/CD2) are stacked Plex-style; otherwise the largest wins and the rest
    are left unfiled — never overwritten onto each other."""
    if len(plans) == 1:
        return plans
    parts = [p.part for p in plans]
    if all(parts) and len(set(parts)) == len(parts):
        for p in plans:
            p.role = "part"
            p.dest = p.dest.with_name(f"{p.dest.stem} - pt{p.part}{p.dest.suffix}")
        return plans
    plans = sorted(plans, key=lambda p: p.size, reverse=True)
    keep = plans[0]
    for other in plans[1:]:
        unfiled.append((other.src, f"same destination as larger {keep.src.name}"))
    return [keep]


def _sub_suffixes(video_stem: str, subs: list[Path]) -> list[tuple[Path, str]]:
    """Name tags for each subtitle. 'Movie.en.forced.srt' -> '.en.forced';
    RARBG-style 'Subs/2_English.srt' -> '.English' (the ordinal is kept only
    when dropping it would make two subtitle names collide)."""
    raw = {}
    for s in subs:
        if s.stem == video_stem:
            toks = []
        elif s.stem.startswith(video_stem + "."):
            toks = s.stem[len(video_stem) + 1:].split(".")
        else:
            toks = s.stem.split(".")
        raw[s] = [t for t in (sanitize(t) for t in toks) if t]
    stripped = {s: [re.sub(r"^\d+_", "", t) or t for t in toks] for s, toks in raw.items()}
    counts = Counter((".".join(t).lower(), s.suffix.lower()) for s, t in stripped.items())
    out = []
    for s in subs:
        toks = stripped[s]
        if counts[(".".join(toks).lower(), s.suffix.lower())] > 1:
            toks = raw[s]
        out.append((s, "".join("." + t for t in toks)))
    return out


def _collect_subs(root: Path, placements: list[Plan]) -> dict[Path, list[tuple[Path, str]]]:
    all_subs = [p for p in _files(root) if p.suffix.lower() in SUB_EXTS]
    claimed: set[Path] = set()
    per_video: dict[Path, list[Path]] = {}
    for plan in placements:
        stem = plan.src.stem
        mine = [s for s in all_subs if s not in claimed and (
            (s.parent == plan.src.parent
             and (s.stem == stem or s.stem.startswith(stem + ".")))
            or (s.parent != plan.src.parent and s.parent.name == stem))]   # Subs/<stem>/..
        claimed.update(mine)
        per_video[plan.src] = mine
    primaries = [p for p in placements if p.role != "extra"]
    if len(primaries) == 1 and primaries[0].kind == "movie":
        loose = [s for s in all_subs if s not in claimed
                 and any(_norm(d) in _SUB_DIRS for d in _rel_dirs(root, s))]
        per_video[primaries[0].src].extend(loose)
    return {v: _sub_suffixes(v.stem, subs) for v, subs in per_video.items() if subs}


# ----------------------------------------------------------------------------
# Library quality history
# ----------------------------------------------------------------------------
_DB = None


def _db():
    global _DB
    if _DB is None:
        try:
            from faucet import db as dbmod
            dbmod.init()
            _DB = dbmod
        except Exception as e:                   # noqa: BLE001
            logging.warning("library DB unavailable (%s); existing files will "
                            "never be replaced", e)
            _DB = False
    return _DB or None


def _recorded_quality(path: Path) -> tuple[str | None, bool]:
    dbm = _db()
    if dbm:
        try:
            with dbm.connect() as c:
                row = c.execute("SELECT quality, is_cam FROM library_files WHERE path=?",
                                (str(path),)).fetchone()
            if row:
                return row["quality"], bool(row["is_cam"])
        except Exception as e:                   # noqa: BLE001
            logging.warning("quality lookup failed for %s: %s", path, e)
    # a hand-placed file may still carry its release tags in the name
    return detect_quality(path.name), detect_cam(path.name)


def _record(plan: Plan) -> None:
    dbm = _db()
    if not dbm:
        return
    try:
        size = plan.dest.stat().st_size
        with dbm.connect() as c:
            c.execute(
                "INSERT INTO library_files (path, quality, is_cam, release, size, sorted_ts) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                "quality=excluded.quality, is_cam=excluded.is_cam, "
                "release=excluded.release, size=excluded.size, sorted_ts=excluded.sorted_ts",
                (str(plan.dest), plan.quality, int(plan.is_cam), plan.release, size,
                 datetime.now().isoformat(timespec="seconds")))
    except Exception as e:                       # noqa: BLE001 - never fail a sort on this
        logging.warning("could not record quality for %s: %s", plan.dest, e)


def _better_than_existing(plan: Plan) -> bool:
    old_q, old_cam = _recorded_quality(plan.dest)
    ok = is_better(plan.quality, plan.is_cam, old_q, old_cam)

    def fmt(q, cam):
        return f"{q or 'unknown'}{' CAM' if cam else ''}"

    plan.note = (f"library has {plan.dest.name} ({fmt(old_q, old_cam)}); "
                 f"incoming is {fmt(plan.quality, plan.is_cam)}")
    return ok


# ----------------------------------------------------------------------------
# Placement
# ----------------------------------------------------------------------------
def _partial(dest: Path) -> Path:
    # per-process name: two sorters racing on one release (hook + sweep) each
    # build their own copy and the atomic rename lets exactly one win intact
    return dest.with_name(f".{dest.name}.{os.getpid()}{PARTIAL_SUFFIX}")


def _reap_stale_partials(dest: Path) -> None:
    """Remove partials left for this destination by killed sorters. A live
    copy rewrites its partial continuously, so only idle ones are touched."""
    cutoff = time.time() - STALE_PARTIAL_SECS
    try:
        for p in dest.parent.glob(f".{glob.escape(dest.name)}.*{PARTIAL_SUFFIX}"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    logging.info("removed stale partial %s", p.name)
            except OSError:
                continue
    except OSError:
        pass


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logging.warning("could not remove %s: %s", p, e)


def _try_link(src: Path, dest: Path) -> bool:
    tmp = _partial(dest)
    _unlink_quiet(tmp)
    try:
        os.link(src, tmp)
    except OSError:
        return False
    try:
        os.replace(tmp, dest)
    except OSError:
        _unlink_quiet(tmp)
        raise
    return True


def _try_rename(src: Path, dest: Path) -> bool:
    """Same-filesystem move. rename(2) is atomic and replaces dest in one step."""
    try:
        os.rename(src, dest)
        return True
    except OSError:
        return False


def _copy_atomic(src: Path, dest: Path) -> None:
    """Copy to a hidden partial beside dest, fsync, verify, then swap it in.
    An interrupted copy never touches dest."""
    tmp = _partial(dest)
    _unlink_quiet(tmp)
    try:
        with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, COPY_CHUNK)
            fdst.flush()
            os.fsync(fdst.fileno())
        try:
            shutil.copystat(src, tmp)
        except OSError:
            pass                                  # CIFS may refuse metadata; content is what matters
        want, got = src.stat().st_size, tmp.stat().st_size
        if want != got:
            raise OSError(f"short copy of {src.name}: {got} of {want} bytes")
        os.replace(tmp, dest)
    except BaseException:
        _unlink_quiet(tmp)
        raise


def _transfer(src: Path, dest: Path) -> str:
    if MODE in ("auto", "hardlink") and _try_link(src, dest):
        return "LINKED"
    if MODE in ("auto", "move") and _try_rename(src, dest):
        return "MOVED"
    # hardlink mode falls back to copy, never to a move: the point of hardlink
    # mode is that the torrent keeps its data
    _copy_atomic(src, dest)
    if MODE == "move":
        src.unlink()
        return "MOVED (copy+delete)"
    return "COPIED"


def place(src: Path, dest: Path, dry: bool, replace_if=None) -> str:
    """Put src at dest. Returns PLACED, EXISTS (same size already there), KEPT
    (a different file is there and replace_if() didn't approve replacing it)
    or DRY. Raises OSError on I/O failure, leaving dest untouched."""
    replacing = False
    if dest.exists():
        if dest.stat().st_size == src.stat().st_size:
            logging.info("EXISTS (same size), skipping: %s", dest)
            return EXISTS
        if replace_if is None or not replace_if():
            return KEPT
        replacing = True
    if dry:
        logging.info("DRY-RUN %s%s -> %s", "(replace) " if replacing else "", src, dest)
        return DRY
    dest.parent.mkdir(parents=True, exist_ok=True)
    _reap_stale_partials(dest)
    how = _transfer(src, dest)
    logging.info("%s%s %s -> %s", "REPLACED/" if replacing else "", how, src.name, dest)
    return PLACED


# ----------------------------------------------------------------------------
# Leftovers: cleanup and quarantine
# ----------------------------------------------------------------------------
def _is_valuable(root: Path, p: Path, handled: set) -> bool:
    if p in handled:
        return False
    ext = p.suffix.lower()
    if ext in VIDEO_EXTS:
        return not is_sample(root, p)
    if ext in SUB_EXTS:
        return True
    if ext in JUNK_EXTS or p.name.lower() in (".ds_store", "thumbs.db", "desktop.ini"):
        return False
    if "sample" in p.name.lower() and ext not in DISC_EXTS | ARCHIVE_EXTS:
        return False
    try:
        return p.stat().st_size > 0
    except OSError:
        return True                               # can't tell — keep it


def _safe_to_remove(root: Path) -> bool:
    """Refuse to delete or relocate anything that isn't clearly one release."""
    try:
        r = root.resolve()
        lib = MEDIA_ROOT.resolve()
    except OSError:
        return False
    if len(r.parts) < 3 or r == lib or r in lib.parents or lib in r.parents:
        return False
    return r.parent.name != QUARANTINE_NAME


def _cleanup_release_dir(root: Path, handled: set | frozenset = frozenset()) -> bool:
    """Remove a leftover release folder when nothing of value remains (only
    tracker clutter, samples, empty files, and files already filed).
    Returns True if removed."""
    if not _safe_to_remove(root):
        logging.warning("refusing to clean up %s", root)
        return False
    try:
        for p in _files(root):
            if _is_valuable(root, p, set(handled)):
                logging.info("leaving release dir (has %s)", p.name)
                return False
        shutil.rmtree(root)
        logging.info("cleaned up leftover release dir: %s", root.name)
        return True
    except OSError as e:
        logging.warning("could not clean release dir %s: %s", root, e)
        return False


def quarantine_dir(root: Path) -> Path:
    explicit = os.environ.get("QUARANTINE_DIR")
    return Path(explicit) if explicit else root.parent / QUARANTINE_NAME


def quarantine(root: Path) -> Path | None:
    """Move a whole release (dir or file) into the quarantine dir, intact.
    Returns where it went, or None if it couldn't be moved (the caller must
    then not let the hook delete it)."""
    if not _safe_to_remove(root):
        logging.error("refusing to quarantine %s", root)
        return None
    base = quarantine_dir(root)
    target = base / root.name
    n = 2
    while target.exists():
        target = base / f"{root.stem if root.is_file() else root.name} ({n}){root.suffix if root.is_file() else ''}"
        n += 1
    try:
        base.mkdir(parents=True, exist_ok=True)
        if not _try_rename(root, target):
            tmp = _partial(target)
            if root.is_dir():
                shutil.rmtree(tmp, ignore_errors=True)
                shutil.copytree(root, tmp)
                os.rename(tmp, target)
                shutil.rmtree(root)
            else:
                _copy_atomic(root, target)
                root.unlink()
    except OSError as e:
        logging.error("QUARANTINE FAILED for %s: %s", root, e)
        return None
    logging.warning("QUARANTINED %s -> %s", root.name, target)
    return target


def _consumed(handled: set) -> bool:
    """Is this release being used up? True when its files are moved away
    (MODE=move), when the hook will delete it (REMOVE_ON_COMPLETE), or in auto
    mode when every filed video actually moved (nothing left seeding)."""
    if MODE == "move" or _truthy("REMOVE_ON_COMPLETE"):
        return True
    if MODE == "auto":
        vids = [p for p in handled if p.suffix.lower() in VIDEO_EXTS]
        return bool(vids) and not any(p.exists() for p in vids)
    return False


def _finish(root: Path, handled: set, unfiled: list, dry: bool) -> int:
    for p, why in unfiled:
        logging.warning("NOT FILED %s: %s", p.name, why)
    if root.is_file():
        leftover = root.exists() and root not in handled
        valuable = [root] if leftover else []
    else:
        valuable = [p for p in _files(root) if _is_valuable(root, p, handled)]
    if dry:
        if valuable:
            logging.info("DRY-RUN would quarantine %s (%d item(s) not filed)",
                         root.name, len(valuable))
            return EXIT_QUARANTINED
        return EXIT_OK
    if not _consumed(handled):
        return EXIT_OK                            # still seeding; leave it be
    if not valuable:
        if root.is_dir():
            _cleanup_release_dir(root, handled)
        return EXIT_OK
    for p in valuable:
        if not any(p == u for u, _ in unfiled):
            logging.warning("NOT FILED %s: no rule for this file", p.name)
    return EXIT_QUARANTINED if quarantine(root) is not None else EXIT_RETRY


# ----------------------------------------------------------------------------
# Games
# ----------------------------------------------------------------------------
def release_is_game(root: Path, ctype: str | None = None) -> bool:
    """Console/ROM formats always mean a game. Disc images and archive-only
    releases are a game only when the classifier says so — otherwise they're
    movie/software discs or packed video and must not vanish into games/."""
    files = _files(root)
    exts = {p.suffix.lower() for p in files}
    if exts & CONSOLE_EXTS:
        return True
    if ctype in ("movie", "tv"):
        return False
    if exts & VIDEO_EXTS:
        return False
    return ctype == "game" and bool(exts & (DISC_EXTS | ARCHIVE_EXTS))


def handle_game(root: Path, platform: str | None, dry: bool) -> str:
    """File a whole game/software release intact into games/<Platform>/<name>.
    Returns PLACED, EXISTS or DRY; raises OSError on failure (dest untouched)."""
    sub = sanitize(platform) if platform else "PC"
    name = sanitize(root.stem if root.is_file() else root.name)
    dest = GAMES_DIR / sub / name
    if dest.exists():
        logging.warning("GAME EXISTS: %s", dest)
        return EXISTS
    if dry:
        logging.info("DRY-RUN game %s -> %s", root.name, dest)
        return DRY
    dest.parent.mkdir(parents=True, exist_ok=True)
    if MODE in ("auto", "move") and _try_rename(root, dest):
        logging.info("GAME moved %s -> %s", root.name, dest)
        return PLACED
    tmp = _partial(dest)
    try:
        if root.is_dir():
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(root, tmp)
            os.rename(tmp, dest)
        else:
            _copy_atomic(root, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if MODE == "move":
        if root.is_dir():
            shutil.rmtree(root)
        else:
            root.unlink()
    logging.info("GAME filed %s -> %s", root.name, dest)
    return PLACED


# ----------------------------------------------------------------------------
# One release
# ----------------------------------------------------------------------------
def _classify(root: Path) -> tuple[str | None, str | None]:
    if classify is None:
        return None, None
    k = classify(root.stem if root.is_file() else root.name)
    return k["type"], k["platform"]


def sort_video_release(root: Path, dry: bool) -> int:
    handled: set[Path] = set()
    unfiled: list[tuple[Path, str]] = []
    retry = False
    groups: dict[str, list[Plan]] = {}
    extras: list[tuple[Path, str]] = []

    for v in (p for p in _files(root) if p.suffix.lower() in VIDEO_EXTS):
        if is_sample(root, v):
            logging.info("SKIP sample: %s", v.name)
            continue
        kind = extra_kind(root, v)
        if kind:
            extras.append((v, kind))
            continue
        try:
            size = v.stat().st_size
            plan = _plan(v)
        except OSError as e:
            logging.error("cannot read %s: %s", v, e)
            retry = True
            continue
        if size < MIN_SIZE_MB * 1024 * 1024:
            unfiled.append((v, f"under {MIN_SIZE_MB} MB and not named as a sample"))
            continue
        if plan is None:
            unfiled.append((v, "unparseable (no title+year, or no season/episode)"))
            continue
        plan.size = size
        groups.setdefault(str(plan.dest).lower(), []).append(plan)

    placements: list[Plan] = []
    for plans in groups.values():
        placements.extend(_resolve_group(plans, unfiled))

    anchors = {p.anchor for p in placements}
    anchor = anchors.pop() if len(anchors) == 1 else None
    for v, kind in extras:
        if anchor is None:
            unfiled.append((v, f"{kind} extra with no single title to attach to"))
            continue
        name = sanitize(_INLINE_EXTRA.sub("", v.stem)) or "extra"
        placements.append(Plan(v, anchor / kind / (name + v.suffix.lower()), "extra",
                               anchor, role="extra"))

    subs = _collect_subs(root, placements)

    for plan in placements:
        policy = None if plan.role == "extra" else (lambda p=plan: _better_than_existing(p))
        try:
            outcome = place(plan.src, plan.dest, dry, replace_if=policy)
        except OSError as e:
            logging.error("failed to file %s: %s", plan.src.name, e)
            retry = True
            continue
        if outcome == KEPT:
            unfiled.append((plan.src, plan.note or f"{plan.dest.name} already in library"))
            continue
        handled.add(plan.src)
        if outcome == PLACED and plan.role != "extra":
            _record(plan)
        for sub, suffix in subs.get(plan.src, []):
            sdest = plan.dest.with_name(plan.dest.stem + suffix + sub.suffix.lower())
            try:
                so = place(sub, sdest, dry)
            except OSError as e:
                logging.error("failed to file subtitle %s: %s", sub.name, e)
                retry = True
                continue
            if so == KEPT:
                unfiled.append((sub, f"subtitle {sdest.name} already exists"))
            else:
                handled.add(sub)

    if retry:
        logging.error("I/O errors while filing %s — left in place for retry", root.name)
        return EXIT_RETRY
    return _finish(root, handled, unfiled, dry)


def suspicious_reason(root: Path, ctype: str | None) -> str | None:
    """A media release whose payload is executables with no video."""
    if ctype == "game":
        return None
    try:
        from faucet.safety import payload_problem
    except Exception:                            # noqa: BLE001 - standalone
        return None
    rels = [str(p.relative_to(root)) if root.is_dir() else p.name for p in _files(root)]
    return payload_problem(rels)


def quarantine_suspicious(root: Path, reason: str, dry: bool) -> int:
    logging.warning("SUSPICIOUS release %s: %s", root.name, reason)
    if dry:
        return EXIT_SUSPICIOUS
    if not (MODE == "move" or _truthy("REMOVE_ON_COMPLETE")):
        return EXIT_SUSPICIOUS                    # still seeding; the guard paused it
    try:
        from faucet.safety import neutralize
    except Exception:                            # noqa: BLE001
        neutralize = None
    moved = quarantine(root)
    if moved is None:
        return EXIT_RETRY
    if neutralize:
        for p in neutralize(moved):
            logging.warning("neutralized %s", p)
    return EXIT_SUSPICIOUS


def sort_release(root: Path, dry: bool) -> int:
    if not root.exists():
        logging.warning("Input not found: %s", root)
        return EXIT_RETRY
    ctype, platform = _classify(root)
    reason = suspicious_reason(root, ctype)
    if reason:
        return quarantine_suspicious(root, reason, dry)
    if release_is_game(root, ctype):
        try:
            outcome = handle_game(root, platform, dry)
        except OSError as e:
            logging.error("game file failed for %s: %s", root.name, e)
            return EXIT_RETRY
        if outcome != EXISTS:
            return EXIT_OK
        return _finish(root, set(), [(root, "game already in library")], dry)
    return sort_video_release(root, dry)


# ----------------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------------
def _recover_inputs(td: str, tn: str) -> list:
    """When TR_TORRENT_DIR/TR_TORRENT_NAME join to a path that doesn't exist
    (e.g. the name contains '/' from a tracker URL watermark and shreds the
    path), recover the real items by scanning TR_TORRENT_DIR for entries that
    match. We match on a slash-stripped, separator-collapsed comparison so a
    folder literally named '[site](https://site) - Show S01E01' is still found.
    """
    parent = Path(td)
    if not parent.exists():
        return []

    def norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    want = norm(tn)
    matches = []
    try:
        for entry in parent.iterdir():
            en = norm(entry.name)
            if en == want or (want and (en.endswith(want) or want.endswith(en))):
                matches.append(entry)
    except OSError:
        return []
    return matches


def resolve_inputs(args):
    """CLI paths if given, else hook env vars (see _recover_inputs)."""
    if args.paths:
        return [Path(p) for p in args.paths]
    cp = os.environ.get("FAUCET_PATH") or os.environ.get("CASCADE_PATH")
    td = os.environ.get("TR_TORRENT_DIR")
    tn = os.environ.get("TR_TORRENT_NAME")
    if cp:
        p = Path(cp)
        if p.exists():
            return [p]
        scan = p
        while scan != scan.parent and not scan.exists():
            scan = scan.parent
        match_name = tn or Path(cp).name
        rec = _recover_inputs(str(scan), match_name)
        if rec:
            logging.info("recovered %d input(s) by scanning %s", len(rec), scan)
            return rec
        return [p]
    if td and tn:
        p = Path(td) / tn
        if p.exists():
            return [p]
        rec = _recover_inputs(td, tn)
        if rec:
            logging.info("recovered %d input(s) by scanning %s", len(rec), td)
            return rec
        return [p]
    return []


_SEVERITY = {EXIT_OK: 0, EXIT_QUARANTINED: 1, EXIT_SUSPICIOUS: 2, EXIT_RETRY: 3}


def main() -> int:
    ap = argparse.ArgumentParser(description="Sort media into Plex/Jellyfin tree.")
    ap.add_argument("paths", nargs="*", help="File(s) or dir(s) to process.")
    ap.add_argument("--dry-run", action="store_true", help="Show actions only.")
    args = ap.parse_args()

    setup_logging()
    dry = args.dry_run or os.environ.get("DRY_RUN") == "1"

    inputs = resolve_inputs(args)
    if not inputs:
        logging.error("No input paths (no args and no FAUCET_PATH / TR_TORRENT_* env).")
        return EXIT_FATAL
    if not MEDIA_ROOT.exists():
        logging.error("LIBRARY_ROOT %s not present — is the NAS mounted?", MEDIA_ROOT)
        return EXIT_FATAL

    rc = EXIT_OK
    for root in inputs:
        r = sort_release(root, dry)
        if _SEVERITY.get(r, 3) > _SEVERITY.get(rc, 3):
            rc = r
    logging.info("Done: rc=%d (mode=%s, dry=%s).", rc, MODE, dry)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
libtidy.py — audit and normalize a Plex/Jellyfin media library.

Scope: TV season folders -> "Season NN" (zero-padded, no year/source tags).
Also flags movie folders that deviate from "Title (Year)" but does NOT rename
them automatically (movie titles are riskier to mangle — it reports only).

SAFE BY DEFAULT: prints a plan and changes nothing. Add --apply to execute.

Usage:
  libtidy.py                          # dry-run, both movies + tv
  libtidy.py --tv                     # tv only
  libtidy.py --movies                 # movies report only
  libtidy.py --apply                  # actually rename (tv) after you've reviewed
  libtidy.py --root /mnt/nas/media    # override library root

What it does to TV season folders:
  "Season 9 (2026)"      -> "Season 09"
  "Season 1 (BluRay)"    -> "Season 01"
  "Season 09"            -> "Season 09"   (already good, skipped)
  "Specials" / "Season 0"-> left alone (Specials is canonical)

Merge handling: if normalizing produces a name that ALREADY exists (e.g. both
"Season 9" and "Season 09" map to "Season 09"), files are MOVED into the existing
target and the now-empty source folder is removed. Collisions on identical
filenames are reported and SKIPPED (never overwritten).
"""

import os
import re
import sys
import shutil
import argparse
from pathlib import Path

DEFAULT_ROOT = Path(os.environ.get("MEDIA_ROOT", "/mnt/nas/media"))

# Matches "Season 9", "Season 09", "Season 9 (2026)", "Season 1 (BluRay)",
# "Season 12 (AMZN WEB-DL)", "season 3", etc. Captures the number.
SEASON_RE = re.compile(r"^season\s+(\d{1,3})\b.*$", re.IGNORECASE)
SPECIALS_NAMES = {"specials", "season 0", "season 00"}

# Movie folder ideal: "Title (Year)"
MOVIE_OK_RE = re.compile(r"^.+\(\d{4}\)$")


def c(code, s):
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s


def normalize_season(name: str):
    """Return canonical 'Season NN' for a season-like folder, or None to skip."""
    if name.strip().lower() in SPECIALS_NAMES:
        return None
    m = SEASON_RE.match(name.strip())
    if not m:
        return None
    num = int(m.group(1))
    return f"Season {num:02d}"


def merge_into(src: Path, dst: Path, apply: bool, log):
    """Move files from src into existing dst; report filename collisions."""
    collisions = []
    moved = 0
    for item in src.iterdir():
        target = dst / item.name
        if target.exists():
            collisions.append(item.name)
            continue
        if apply:
            shutil.move(str(item), str(target))
        moved += 1
    log.append((src, dst, moved, collisions))
    if apply and not collisions:
        try:
            src.rmdir()
        except OSError:
            pass  # not empty (collisions remain) — leave it
    return moved, collisions


def process_show(show_dir: Path, apply: bool):
    """Yield (action, detail) tuples for one show's season folders."""
    actions = []
    for sub in sorted(show_dir.iterdir()):
        if not sub.is_dir():
            continue
        canon = normalize_season(sub.name)
        if canon is None:
            continue                      # not a season folder, or Specials
        if sub.name == canon:
            continue                      # already correct
        target = show_dir / canon
        if target.exists() and target != sub:
            # merge case
            merged = []
            moved, collisions = merge_into(sub, target, apply, merged)
            actions.append(("merge", sub, target, moved, collisions))
        else:
            if apply:
                sub.rename(target)
            actions.append(("rename", sub, target, None, None))
    return actions


def audit_tv(root: Path, apply: bool):
    tv = root / "tvshows"
    if not tv.is_dir():
        print(c("31", f"No tvshows dir at {tv}"))
        return
    print(c("1", f"\n=== TV  ({'APPLY' if apply else 'DRY-RUN'}) ===\n"))
    total = 0
    for show in sorted(tv.iterdir()):
        if not show.is_dir():
            continue
        acts = process_show(show, apply)
        if not acts:
            continue
        print(c("36", show.name))
        for kind, src, dst, moved, collisions in acts:
            if kind == "rename":
                print(f"  {c('33','RENAME')}  {src.name!r}  ->  {dst.name!r}")
                total += 1
            else:
                cs = f"  [skipped {len(collisions)} dupe file(s): {', '.join(collisions)}]" if collisions else ""
                print(f"  {c('35','MERGE ')}  {src.name!r}  ->  {dst.name!r}  ({moved} file(s) moved){c('31', cs)}")
                total += 1
    if total == 0:
        print(c("32", "All TV season folders already conform. Nothing to do."))
    else:
        print(c("1", f"\n{total} folder action(s) {'applied' if apply else 'planned'}."))
        if not apply:
            print(c("32", "Re-run with --apply to execute."))


def audit_movies(root: Path):
    mv = root / "movies"
    if not mv.is_dir():
        print(c("31", f"No movies dir at {mv}"))
        return
    print(c("1", "\n=== MOVIES (report only — not auto-renamed) ===\n"))
    bad = 0
    for m in sorted(mv.iterdir()):
        if not m.is_dir():
            continue
        if not MOVIE_OK_RE.match(m.name):
            print(f"  {c('33','CHECK')}  {m.name!r}   (expected 'Title (Year)')")
            bad += 1
    if bad == 0:
        print(c("32", "All movie folders match 'Title (Year)'."))
    else:
        print(c("1", f"\n{bad} movie folder(s) deviate. Rename these by hand — "
                      "auto-renaming movie titles is too lossy to do blindly."))


def main():
    ap = argparse.ArgumentParser(description="Audit/normalize media library folders.")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--apply", action="store_true", help="execute TV renames (default: dry-run)")
    ap.add_argument("--tv", action="store_true", help="TV only")
    ap.add_argument("--movies", action="store_true", help="movies only")
    args = ap.parse_args()

    if not args.root.is_dir():
        sys.exit(f"Root not found: {args.root} — is the NAS mounted?")

    do_tv = args.tv or not args.movies
    do_mv = args.movies or not args.tv

    if do_tv:
        audit_tv(args.root, args.apply)
    if do_mv:
        audit_movies(args.root)

    if args.apply:
        print(c("33", "\nDone. Trigger a library rescan in Plex/Jellyfin so it "
                      "re-matches the renamed folders."))


if __name__ == "__main__":
    main()

"""Libraries, show-folder links, and client path mappings.

A library is a folder Faucet scans for what you own and files new downloads
into: {name, kind 'tv'|'movie', path, is_default}. They replace the fixed
LIBRARY_ROOT/tvshows and LIBRARY_ROOT/movies layout; an existing install is
seeded with exactly those two on first start, so nothing moves.

Folder links tie a show folder inside a TV library to a monitored series by
id. Matching used to normalise the folder name and the TMDb title on every
lookup, so "Trailer Park Boys" vs "Trailer Park Boys (2001)" vs a renamed
TMDb title decided whether 200 episodes counted as owned. A link is made
once — automatically when the names agree, or by hand (Fix Match) — and the
scanner then stamps series_id on every file in that folder.

Path mappings translate the download client's view of a path into Faucet's
(Transmission writes to /mnt/nas/torrents, Faucet sees /nas/torrents), so the
completion hook can be handed the client's raw path.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from . import db

log = logging.getLogger("faucet.libraries")

KINDS = ("tv", "movie")
_SEEDED = "_libraries_seeded"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_schema(c) -> None:
    c.execute(
        "CREATE TABLE IF NOT EXISTS libraries ("
        "  id         INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  name       TEXT NOT NULL,"
        "  kind       TEXT NOT NULL,"            # tv | movie
        "  path       TEXT NOT NULL UNIQUE,"
        "  is_default INTEGER DEFAULT 0,"        # new downloads of this kind go here
        "  enabled    INTEGER DEFAULT 1,"
        "  auto_root  INTEGER DEFAULT 0,"        # seeded from LIBRARY_ROOT and follows it until edited
        "  created_ts TEXT,"
        "  last_scan  TEXT"
        ")")
    c.execute(
        "CREATE TABLE IF NOT EXISTS library_links ("
        "  library_id INTEGER NOT NULL,"
        "  folder     TEXT NOT NULL,"            # show folder name inside the library
        "  series_id  INTEGER NOT NULL,"
        "  auto       INTEGER DEFAULT 1,"        # 0 = set by hand (Fix Match): never overwritten
        "  created_ts TEXT,"
        "  PRIMARY KEY (library_id, folder)"
        ")")
    c.execute(
        "CREATE TABLE IF NOT EXISTS path_mappings ("
        "  id     INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  remote TEXT NOT NULL UNIQUE,"         # as the download client sees it
        "  local  TEXT NOT NULL"                 # as Faucet sees it
        ")")


def seed(c) -> None:
    """First start with libraries: register the folders the old fixed layout
    used, so an existing install keeps scanning and sorting exactly as before."""
    row = c.execute("SELECT value FROM settings WHERE key=?", (_SEEDED,)).fetchone()
    if row:
        return
    if not c.execute("SELECT 1 FROM libraries LIMIT 1").fetchone():
        root = Path(os.environ.get("LIBRARY_ROOT", "") or "/library")
        for name, kind, sub in (("TV Shows", "tv", "tvshows"), ("Movies", "movie", "movies")):
            c.execute("INSERT OR IGNORE INTO libraries (name, kind, path, is_default, auto_root, "
                      "created_ts) VALUES (?,?,?,1,1,?)", (name, kind, str(root / sub), _now()))
    c.execute("INSERT INTO settings (key, value) VALUES (?, '1') "
              "ON CONFLICT(key) DO UPDATE SET value='1'", (_SEEDED,))


# ── libraries ───────────────────────────────────────────────────────────────

_SUB = {"tv": "tvshows", "movie": "movies"}


def _follow_root(c) -> None:
    """Seeded libraries follow LIBRARY_ROOT until someone edits them, so the
    old setting (and an install that changes it) keeps working."""
    root = Path(os.environ.get("LIBRARY_ROOT", "") or "/library")
    for r in c.execute("SELECT id, kind, path FROM libraries WHERE auto_root=1").fetchall():
        want = str(root / _SUB.get(r["kind"], r["kind"]))
        if r["path"] != want:
            try:
                c.execute("UPDATE libraries SET path=? WHERE id=?", (want, r["id"]))
            except Exception:                    # noqa: BLE001 - a clash keeps the old path
                pass


def all_libraries(enabled_only: bool = False) -> list[dict]:
    with db.connect() as c:
        _follow_root(c)
        q = "SELECT * FROM libraries"
        if enabled_only:
            q += " WHERE enabled=1"
        return [dict(r) for r in c.execute(q + " ORDER BY kind DESC, name")]


def of_kind(kind: str) -> list[dict]:
    return [lib for lib in all_libraries(enabled_only=True) if lib["kind"] == kind]


def get(lib_id: int) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT * FROM libraries WHERE id=?", (lib_id,)).fetchone()
    return dict(r) if r else None


def default_path(kind: str) -> str | None:
    """Where new downloads of this kind are filed."""
    libs = of_kind(kind)
    for lib in libs:
        if lib["is_default"]:
            return lib["path"]
    return libs[0]["path"] if libs else None


def containing(path) -> dict | None:
    """The library a path lives in (the deepest, if they nest)."""
    p = str(path)
    best = None
    for lib in all_libraries():
        root = lib["path"].rstrip("/")
        if (p == root or p.startswith(root + "/")) and (best is None or len(root) > len(best["path"])):
            best = lib
    return best


def _clean_path(path: str) -> str:
    p = os.path.normpath(str(path or "").strip())
    if not p.startswith("/") or p == "/":
        raise ValueError("library folder must be an absolute path below /")
    return p


def validate(path: str, kind: str, ignore_id: int | None = None) -> tuple[str, list[str]]:
    """(clean path, warnings). Raises ValueError for a path that can't be a
    library: relative, nested inside / containing another library, or
    unreadable. A missing folder is only a warning (a NAS may be down)."""
    if kind not in KINDS:
        raise ValueError("kind must be 'tv' or 'movie'")
    p = _clean_path(path)
    for lib in all_libraries():
        if lib["id"] == ignore_id:
            continue
        other = lib["path"].rstrip("/")
        if p == other:
            raise ValueError(f"already a library: {lib['name']}")
        if p.startswith(other + "/") or other.startswith(p + "/"):
            raise ValueError(f"overlaps the {lib['name']} library ({other}); libraries can't nest")
    warnings = []
    if not os.path.isdir(p):
        warnings.append(f"{p} doesn't exist (or isn't mounted) right now")
    elif not os.access(p, os.R_OK | os.X_OK):
        raise ValueError(f"{p} isn't readable by Faucet")
    elif not os.access(p, os.W_OK):
        warnings.append(f"{p} is read-only: Faucet can scan it but not file downloads into it")
    return p, warnings


def add(name: str, kind: str, path: str, is_default: bool = False) -> tuple[int, list[str]]:
    name = (name or "").strip()[:80] or ("TV Shows" if kind == "tv" else "Movies")
    p, warnings = validate(path, kind)
    with db.connect() as c:
        first = not c.execute("SELECT 1 FROM libraries WHERE kind=?", (kind,)).fetchone()
        if is_default or first:
            c.execute("UPDATE libraries SET is_default=0 WHERE kind=?", (kind,))
        lid = c.execute("INSERT INTO libraries (name, kind, path, is_default, created_ts) "
                        "VALUES (?,?,?,?,?)",
                        (name, kind, p, 1 if (is_default or first) else 0, _now())).lastrowid
    log.info("Library added: %s (%s) at %s", name, kind, p)
    return lid, warnings


def update(lib_id: int, **fields) -> list[str]:
    lib = get(lib_id)
    if not lib:
        raise LookupError("no such library")
    warnings: list[str] = []
    sets: dict = {}
    if "name" in fields and fields["name"]:
        sets["name"] = str(fields["name"]).strip()[:80]
    if "path" in fields and fields["path"] and fields["path"] != lib["path"]:
        sets["path"], warnings = validate(fields["path"], lib["kind"], ignore_id=lib_id)
        sets["auto_root"] = 0                    # hand-set: stops following LIBRARY_ROOT
    if "enabled" in fields and fields["enabled"] is not None:
        sets["enabled"] = 1 if fields["enabled"] else 0
    moved = None
    with db.connect() as c:
        if fields.get("is_default"):
            c.execute("UPDATE libraries SET is_default=0 WHERE kind=?", (lib["kind"],))
            sets["is_default"] = 1
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            c.execute(f"UPDATE libraries SET {cols} WHERE id=?", (*sets.values(), lib_id))
        if "path" in sets:
            if same_folder(lib["path"], sets["path"]):
                # the same folder seen through another mount (/library/tvshows
                # -> /nas/media/tvshows): carry every record over, links
                # included, so nothing is rescanned or re-hunted
                n = _rewrite_prefix(c, lib["path"], sets["path"])
                moved = "carried"
                warnings.append(f"Same folder through another mount — {n} file records carried over.")
            else:
                # a different folder: what Faucet knew about the old one goes
                _forget_prefix(c, lib["path"], lib["kind"])
                c.execute("DELETE FROM library_links WHERE library_id=?", (lib_id,))
                moved = "forgotten"
                warnings.append("Different folder — the old folder's records were dropped; "
                                "it's scanned fresh on the next scan.")
    if moved:
        log.info("Library %s moved %s -> %s (records %s)", lib["name"], lib["path"],
                 sets["path"], moved)
    return warnings


# tables holding absolute library paths: (table, column)
_PATH_COLUMNS = (("library_episodes", "path"), ("library_movies", "path"),
                 ("library_files", "path"), ("library_dupes", "path"),
                 ("library_dupes", "winner_path"))


def same_folder(old: str, new: str) -> bool:
    """Is `new` the same directory as `old`, reached another way? Both must
    be reachable and agree on their entries and on a sample of file sizes
    and times (enough to tell a bind mount from a different folder)."""
    try:
        a = sorted(e for e in os.listdir(old) if not e.startswith("."))
        b = sorted(e for e in os.listdir(new) if not e.startswith("."))
    except OSError:
        return False
    if not a or a != b:
        return False
    samples = []
    for name in a[:6]:
        sub = os.path.join(old, name)
        if os.path.isdir(sub):
            try:
                samples += [os.path.join(name, e) for e in sorted(os.listdir(sub))[:2]]
            except OSError:
                return False
        else:
            samples.append(name)
    for rel in samples:
        try:
            sa, sb = os.stat(os.path.join(old, rel)), os.stat(os.path.join(new, rel))
        except OSError:
            return False
        if (sa.st_size, int(sa.st_mtime)) != (sb.st_size, int(sb.st_mtime)):
            return False
    return bool(samples)


def _prefix(p: str) -> str:
    return p.rstrip("/") + "/"


def _rewrite_prefix(c, old: str, new: str) -> int:
    o, n = _prefix(old), _prefix(new)
    total = 0
    for table, col in _PATH_COLUMNS:
        try:
            cur = c.execute(f"UPDATE OR IGNORE {table} SET {col} = ? || substr({col}, ?) "
                            f"WHERE substr({col}, 1, ?) = ?", (n, len(o) + 1, len(o), o))
            total += cur.rowcount if col == "path" and table in ("library_episodes",
                                                                  "library_movies") else 0
            # rows that collided with an existing new-path row are stale twins
            c.execute(f"DELETE FROM {table} WHERE substr({col}, 1, ?) = ?", (len(o), o))
        except Exception as e:                   # noqa: BLE001 - a table may not exist yet
            if "no such table" not in str(e):
                raise
    return total


def _forget_prefix(c, old: str, kind: str) -> None:
    o = _prefix(old)
    table = "library_episodes" if kind == "tv" else "library_movies"
    c.execute(f"DELETE FROM {table} WHERE substr(path, 1, ?) = ?", (len(o), o))
    try:
        c.execute("DELETE FROM library_dupes WHERE substr(path, 1, ?) = ?", (len(o), o))
    except Exception as e:                       # noqa: BLE001
        if "no such table" not in str(e):
            raise


def remove(lib_id: int) -> None:
    """Stop scanning a library. Files are never touched; the rows the scanner
    recorded for it are dropped so nothing counts as owned from a folder
    Faucet no longer looks at."""
    lib = get(lib_id)
    if not lib:
        return
    prefix = lib["path"].rstrip("/") + "/"
    with db.connect() as c:
        c.execute("DELETE FROM libraries WHERE id=?", (lib_id,))
        c.execute("DELETE FROM library_links WHERE library_id=?", (lib_id,))
        table = "library_episodes" if lib["kind"] == "tv" else "library_movies"
        c.execute(f"DELETE FROM {table} WHERE substr(path, 1, ?) = ?", (len(prefix), prefix))
        if lib["is_default"]:
            nxt = c.execute("SELECT id FROM libraries WHERE kind=? ORDER BY id LIMIT 1",
                            (lib["kind"],)).fetchone()
            if nxt:
                c.execute("UPDATE libraries SET is_default=1 WHERE id=?", (nxt["id"],))
    log.info("Library removed: %s (%s)", lib["name"], lib["path"])


def mark_scanned(lib_ids) -> None:
    with db.connect() as c:
        for lid in lib_ids:
            c.execute("UPDATE libraries SET last_scan=? WHERE id=?", (_now(), lid))


def sorter_env(release_name: str = "") -> dict:
    """Environment for the sorter subprocess: where to file TV and movies.
    The sorter stays free of the database; the hook and sweep resolve the
    default libraries — or, for a title filed elsewhere, its library — and
    hand them over."""
    env = {}
    tv, mv = default_path("tv"), default_path("movie")
    if tv:
        env["FAUCET_TV_DIR"] = tv
    if mv:
        env["FAUCET_MOVIES_DIR"] = mv
    try:
        env.update(route(release_name))
    except Exception as e:                       # noqa: BLE001 - routing never blocks a sort
        log.warning("routing %r failed (%s); using the default libraries", release_name, e)
    return env


# ── folder links ────────────────────────────────────────────────────────────

def link_folder(library_id: int, folder: str, series_id: int, auto: bool = False) -> None:
    """Tie a show folder to a series. A manual link (auto=False) is never
    replaced by automatic matching."""
    with db.connect() as c:
        if auto:
            c.execute("INSERT INTO library_links (library_id, folder, series_id, auto, created_ts) "
                      "VALUES (?,?,?,1,?) ON CONFLICT(library_id, folder) DO UPDATE SET "
                      "series_id=excluded.series_id WHERE library_links.auto=1",
                      (library_id, folder, series_id, _now()))
        else:
            c.execute("INSERT INTO library_links (library_id, folder, series_id, auto, created_ts) "
                      "VALUES (?,?,?,0,?) ON CONFLICT(library_id, folder) DO UPDATE SET "
                      "series_id=excluded.series_id, auto=0",
                      (library_id, folder, series_id, _now()))
        lib = c.execute("SELECT path FROM libraries WHERE id=?", (library_id,)).fetchone()
        if lib:
            _stamp(c, lib["path"], folder, series_id)
        _claim(c, "series", series_id, library_id)


def unlink_folder(library_id: int, folder: str) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM library_links WHERE library_id=? AND folder=?", (library_id, folder))
        lib = c.execute("SELECT path FROM libraries WHERE id=?", (library_id,)).fetchone()
        if lib:
            _stamp(c, lib["path"], folder, None)


def _folder_prefix(lib_path: str, folder: str) -> str:
    return lib_path.rstrip("/") + "/" + folder + "/"


def _stamp(c, lib_path: str, folder: str, series_id) -> None:
    prefix = _folder_prefix(lib_path, folder)
    c.execute("UPDATE library_episodes SET series_id=? WHERE substr(path, 1, ?) = ?",
              (series_id, len(prefix), prefix))


def auto_link() -> dict:
    """Link every unlinked show folder whose name matches a series title
    (normalised, year tag ignored), and stamp series_id on its files.
    Ambiguous folders (two series normalise the same) are left unlinked."""
    from .library import normalize_title
    out = {"linked": 0, "unlinked": []}
    with db.connect() as c:
        by_title: dict = {}
        for s in c.execute("SELECT id, title FROM series"):
            by_title.setdefault(normalize_title(s["title"]), []).append(s["id"])
        linked = {(r["library_id"], r["folder"]): r["series_id"]
                  for r in c.execute("SELECT library_id, folder, series_id FROM library_links")}
        libs = [dict(r) for r in c.execute("SELECT id, path FROM libraries WHERE kind='tv'")]
    for lib in libs:
        for folder in folders(lib["path"]):
            key = (lib["id"], folder)
            if key in linked:
                with db.connect() as c:
                    _stamp(c, lib["path"], folder, linked[key])
                continue
            ids = by_title.get(normalize_title(folder), [])
            if len(ids) == 1:
                link_folder(lib["id"], folder, ids[0], auto=True)
                out["linked"] += 1
            else:
                out["unlinked"].append({"library_id": lib["id"], "folder": folder,
                                        "candidates": ids})
    return out


def folders(lib_path: str) -> list[str]:
    """Show folders the scanner has seen under a TV library."""
    prefix = lib_path.rstrip("/") + "/"
    seen = set()
    with db.connect() as c:
        for r in c.execute("SELECT DISTINCT path FROM library_episodes WHERE substr(path, 1, ?) = ?",
                           (len(prefix), prefix)):
            rest = r["path"][len(prefix):]
            if "/" in rest:
                seen.add(rest.split("/", 1)[0])
    return sorted(seen)


def links(series_id: int | None = None) -> list[dict]:
    with db.connect() as c:
        q = ("SELECT l.*, b.name AS library, b.path AS library_path, s.title AS series "
             "FROM library_links l JOIN libraries b ON b.id=l.library_id "
             "LEFT JOIN series s ON s.id=l.series_id")
        args: tuple = ()
        if series_id is not None:
            q += " WHERE l.series_id=?"
            args = (series_id,)
        return [dict(r) for r in c.execute(q + " ORDER BY b.name, l.folder", args)]


# ── where each title's downloads go ─────────────────────────────────────────

def _claim(c, table: str, title_id: int, library_id: int) -> None:
    """A title whose files live in a non-default library is filed there from
    now on — unless someone already chose its library."""
    lib = c.execute("SELECT kind, is_default FROM libraries WHERE id=?", (library_id,)).fetchone()
    if lib is None or lib["is_default"]:
        return
    if (table == "series") != (lib["kind"] == "tv"):
        return
    c.execute(f"UPDATE {table} SET library_id=? WHERE id=? AND library_id IS NULL",
              (library_id, title_id))


def assign_from_files() -> dict:
    """Give unassigned titles the library their files are in, when that's a
    non-default one (Mamaw's Shows, say). Shows go by their linked folders,
    movies by the file that satisfies them. Never overrides a choice."""
    from . import movies as movies_mod
    out = {"series": 0, "movies": 0}
    with db.connect() as c:
        for r in c.execute("SELECT l.series_id, GROUP_CONCAT(DISTINCT l.library_id) AS libs "
                           "FROM library_links l JOIN series s ON s.id=l.series_id "
                           "WHERE s.library_id IS NULL GROUP BY l.series_id").fetchall():
            libs = {int(x) for x in (r["libs"] or "").split(",") if x}
            if len(libs) == 1:
                before = c.total_changes
                _claim(c, "series", r["series_id"], libs.pop())
                out["series"] += c.total_changes - before
        unassigned = [dict(m) for m in c.execute(
            "SELECT id, title, year FROM movies WHERE library_id IS NULL").fetchall()]
    for m in unassigned:
        row = movies_mod._owned_row(m)
        lib = containing(row["path"]) if row and row["path"] else None
        if lib:
            with db.connect() as c:
                before = c.total_changes
                _claim(c, "movies", m["id"], lib["id"])
                out["movies"] += c.total_changes - before
    return out


def effective_map(kind: str) -> tuple[int | None, set]:
    """(default library id, enabled library ids) for resolving many titles'
    libraries at once: a title's library is its library_id when that's an
    enabled library of the kind, else the default."""
    libs = of_kind(kind)
    default = next((lib["id"] for lib in libs if lib["is_default"]), libs[0]["id"] if libs else None)
    return default, {lib["id"] for lib in libs}


def resolve(library_id, default: int | None, enabled: set) -> int | None:
    return library_id if library_id in enabled else default


def sections() -> list[dict]:
    """The sidebar's library sections: every enabled library with how many
    titles it holds, TV first, defaults first within a kind."""
    out = []
    with db.connect() as c:
        for kind, table in (("tv", "series"), ("movie", "movies")):
            default, enabled = effective_map(kind)
            counts: dict = {}
            for r in c.execute(f"SELECT library_id FROM {table}"):
                lid = resolve(r["library_id"], default, enabled)
                counts[lid] = counts.get(lid, 0) + 1
            for lib in sorted(of_kind(kind), key=lambda x: (not x["is_default"], x["name"].lower())):
                out.append({"id": lib["id"], "name": lib["name"], "kind": kind,
                            "default": bool(lib["is_default"]), "count": counts.get(lib["id"], 0)})
    return out


def title_library(kind: str, title_id: int) -> dict | None:
    """The library a show ('tv') or movie ('movie') is filed into."""
    table = "series" if kind == "tv" else "movies"
    with db.connect() as c:
        r = c.execute(f"SELECT b.* FROM {table} t JOIN libraries b ON b.id=t.library_id "
                      f"WHERE t.id=? AND b.enabled=1 AND b.kind=?", (title_id, kind)).fetchone()
    if r:
        return dict(r)
    for lib in of_kind(kind):
        if lib["is_default"]:
            return lib
    libs = of_kind(kind)
    return libs[0] if libs else None


def set_title_library(kind: str, title_id: int, library_id: int | None) -> None:
    """Choose where a title is filed; None = follow the default library."""
    table = "series" if kind == "tv" else "movies"
    if library_id is not None:
        lib = get(library_id)
        if lib is None or lib["kind"] != kind:
            raise ValueError(f"not a {'TV' if kind == 'tv' else 'Movies'} library")
    with db.connect() as c:
        if not c.execute(f"SELECT 1 FROM {table} WHERE id=?", (title_id,)).fetchone():
            raise LookupError("no such title")
        c.execute(f"UPDATE {table} SET library_id=? WHERE id=?", (library_id, title_id))


def route(release_name: str) -> dict:
    """Destination overrides for one finished download: if the release is a
    monitored title filed in a non-default library, point the sorter there.
    Only unambiguous matches route; anything else uses the defaults."""
    from . import releasematch
    name = (release_name or "").strip()
    if not name:
        return {}
    out = {}
    with db.connect() as c:
        shows = c.execute("SELECT s.title, s.library_id, b.path FROM series s JOIN libraries b "
                          "ON b.id=s.library_id WHERE b.enabled=1 AND b.kind='tv'").fetchall()
        films = c.execute("SELECT m.title, m.year, m.library_id, b.path FROM movies m JOIN libraries b "
                          "ON b.id=m.library_id WHERE b.enabled=1 AND b.kind='movie'").fetchall()
    tv = {r["path"] for r in shows if releasematch.matches_series(name, r["title"])}
    if len(tv) == 1:
        out["FAUCET_TV_DIR"] = tv.pop()
    if releasematch.title_before_episode_marker(name) is None:      # not an episode
        mv = {r["path"] for r in films if releasematch.matches_movie(name, r["title"], r["year"])}
        if len(mv) == 1:
            out["FAUCET_MOVIES_DIR"] = mv.pop()
    return out


# ── path mappings ───────────────────────────────────────────────────────────

def mappings() -> list[dict]:
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM path_mappings ORDER BY length(remote) DESC")]


def set_mappings(items: list[dict]) -> list[dict]:
    """Replace all mappings. Each {remote, local}: absolute, normalised."""
    clean = []
    for it in items or []:
        remote = os.path.normpath(str(it.get("remote", "")).strip())
        local = os.path.normpath(str(it.get("local", "")).strip())
        if not remote.startswith("/") or not local.startswith("/"):
            raise ValueError("path mappings must be absolute paths")
        clean.append((remote, local))
    if len({r for r, _ in clean}) != len(clean):
        raise ValueError("each client path can be mapped only once")
    with db.connect() as c:
        c.execute("DELETE FROM path_mappings")
        c.executemany("INSERT INTO path_mappings (remote, local) VALUES (?,?)", clean)
    return mappings()


def to_local(path: str) -> str:
    """The client's path as Faucet sees it (longest matching prefix wins);
    unchanged when no mapping applies."""
    p = str(path or "")
    for m in mappings():
        remote = m["remote"].rstrip("/")
        if p == remote or p.startswith(remote + "/"):
            return m["local"].rstrip("/") + p[len(remote):]
    return p

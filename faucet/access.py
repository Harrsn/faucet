"""Which libraries a user can see.

Admins see everything. A member sees every library unless an admin has
limited them to a list (users.library_access, JSON) — e.g. a relative whose
account should only show their own collection. Everything a member can read
(sidebar sections, the TV and movie lists, show and movie pages, Home,
Calendar, search) is filtered through here; the check is on the server, not
just hidden in the interface.

A member's approved requests are filed into their "requests go to" library
for that kind, or — when they can only see one library of the kind — that
one.
"""
from __future__ import annotations

import json

from . import db


def _row(user: dict | None) -> dict | None:
    if not user or not user.get("id"):
        return None
    if "library_access" in user:
        return user
    with db.connect() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return dict(r) if r else None


def allowed(user: dict | None) -> set[int] | None:
    """Library ids the user may see; None = all of them."""
    if user and user.get("role") == "admin":
        return None
    u = _row(user)
    if not u or not u.get("library_access"):
        return None
    try:
        ids = json.loads(u["library_access"])
    except (TypeError, ValueError):
        return None
    if not isinstance(ids, list) or not ids:
        return None
    return {int(x) for x in ids if str(x).isdigit()}


def can_see(user: dict | None, library_id: int | None) -> bool:
    a = allowed(user)
    return a is None or library_id in a


def filter_titles(user: dict | None, titles: list[dict]) -> list[dict]:
    """Keep titles (with their resolved 'lib') the user may see."""
    a = allowed(user)
    return titles if a is None else [t for t in titles if t.get("lib") in a]


def visible_series_ids(user: dict | None) -> set[int] | None:
    """Series the user may see; None = all."""
    a = allowed(user)
    if a is None:
        return None
    from . import libraries
    default, enabled = libraries.effective_map("tv")
    with db.connect() as c:
        return {r["id"] for r in c.execute("SELECT id, library_id FROM series")
                if libraries.resolve(r["library_id"], default, enabled) in a}


def visible_movie_ids(user: dict | None) -> set[int] | None:
    a = allowed(user)
    if a is None:
        return None
    from . import libraries
    default, enabled = libraries.effective_map("movie")
    with db.connect() as c:
        return {r["id"] for r in c.execute("SELECT id, library_id FROM movies")
                if libraries.resolve(r["library_id"], default, enabled) in a}


def request_library(user: dict | None, kind: str) -> int | None:
    """Where this user's approved requests of a kind are filed (None = the
    default library)."""
    from . import libraries
    u = _row(user)
    if not u:
        return None
    col = "request_tv_library" if kind == "tv" else "request_movie_library"
    lib_kind = "tv" if kind == "tv" else "movie"
    chosen = u.get(col)
    libs = {lib["id"]: lib for lib in libraries.of_kind(lib_kind)}
    if chosen in libs:
        return chosen
    a = allowed(u)
    if a is not None:
        mine = [i for i in libs if i in a]
        if len(mine) == 1:
            return mine[0]
    return None


def set_access(user_id: int, library_ids: list[int] | None,
               request_tv: int | None, request_movie: int | None) -> None:
    """Limit a member to these libraries (None or [] = all) and choose where
    their requests go. Validates every id against the libraries table."""
    from . import libraries
    known = {lib["id"]: lib for lib in libraries.all_libraries()}
    ids = sorted({int(i) for i in (library_ids or [])})
    bad = [i for i in ids if i not in known]
    if bad:
        raise ValueError(f"unknown librar{'y' if len(bad) == 1 else 'ies'}: {bad}")
    for value, kind in ((request_tv, "tv"), (request_movie, "movie")):
        if value is not None and (value not in known or known[value]["kind"] != kind):
            raise ValueError(f"requests for {'TV' if kind == 'tv' else 'movies'} must go to a "
                             f"{'TV' if kind == 'tv' else 'Movies'} library")
        if value is not None and ids and value not in ids:
            raise ValueError("requests can only go to a library the user can see")
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise LookupError("no such user")
        c.execute("UPDATE users SET library_access=?, request_tv_library=?, "
                  "request_movie_library=? WHERE id=?",
                  (json.dumps(ids) if ids else None, request_tv, request_movie, user_id))

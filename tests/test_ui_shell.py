"""Phase 6.1: the UI shell and /api/home (6.4b: the classic UI is gone)."""
from __future__ import annotations

import importlib
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

STATIC = Path(__file__).resolve().parent.parent / "faucet" / "static"
APP = STATIC / "app"


# ── static structure ────────────────────────────────────────────────────────

def test_every_module_import_resolves():
    imp = re.compile(r"""(?:from|import)\s*\(?\s*["'](\.{1,2}/[^"']+)["']""")
    mods = list(APP.rglob("*.js"))
    assert mods
    for m in mods:
        for rel in imp.findall(m.read_text()):
            assert (m.parent / rel).resolve().exists(), f"{m.name} imports missing {rel}"


def test_only_dom_js_touches_innerhtml():
    """All rendering goes through dom.html (auto-escaping) + mount."""
    for m in APP.rglob("*.js"):
        if m.name == "dom.js":
            continue
        src = m.read_text()
        assert "innerHTML" not in src and "insertAdjacentHTML" not in src, m.name
        assert not re.search(r"\son[a-z]+=", src), f"inline handler in {m.name}"


def test_shell_loads_modules_only():
    shell = (STATIC / "index.html").read_text()
    assert '<script type="module" src="/static/app/main.js"></script>' in shell
    assert "<script>" not in shell and " onclick=" not in shell


# ── serving ─────────────────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    root = tmp_path / "media"
    (root / "tvshows").mkdir(parents=True)
    (root / "movies").mkdir()
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("LIBRARY_ROOT", str(root))
    monkeypatch.setenv("SESSION_SECRET", "t")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m))
            for m in ("config", "db", "tmdb", "auth", "auth_routes", "app")}
    mods["db"].init()
    from fastapi.testclient import TestClient

    def client(user=None, pw=None):
        c = TestClient(mods["app"].app, follow_redirects=False)
        if user:
            assert c.post("/api/auth/login", json={"username": user, "password": pw}).status_code == 200
            c.headers.update({"X-CSRF-Token": c.cookies.get("faucet_csrf")})
        return c

    boot = client()
    boot.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    admin = client("admin", "adminpass123")
    admin.post("/api/admin/users", json={"username": "bob", "password": "bobpass1234",
                                         "role": "user", "status": "active"})
    return SimpleNamespace(m=mods, admin=admin, bob=client("bob", "bobpass1234"),
                           anon=client(), db=mods["db"])


def test_routes_and_caching(app):
    r = app.admin.get("/")
    assert r.status_code == 200 and "/main.js" in r.text
    assert "no-store" in r.headers["cache-control"]
    assert app.admin.get("/classic").status_code == 404
    assert app.anon.get("/").status_code == 302
    m = app.anon.get("/static/app/main.js")                     # static: public, revalidated
    assert m.status_code == 200 and m.headers["cache-control"] == "no-cache"


def _seed(db):
    now = time.time()
    with db.connect() as c:
        sid = c.execute("INSERT INTO series (tmdb_id,title,monitored,poster) VALUES "
                        "(1,'Show',1,'https://image.tmdb.org/t/p/w342/a.jpg')").lastrowid
        for e in (1, 2, 3):
            c.execute("INSERT INTO series_episodes (series_id,season,episode,title,air_date) "
                      "VALUES (?,1,?,?,'2020-01-01')", (sid, e, f"Ep {e}"))
        c.execute("INSERT INTO library_episodes (series_id,show_name,season,episode,quality,"
                  "path,size,mtime) VALUES (?,'Show',1,1,'1080p','/x/1.mkv',1,?)", (sid, now))
        c.execute("INSERT INTO library_episodes (series_id,show_name,season,episode,quality,"
                  "path,size,mtime) VALUES (?,'Show',1,2,'720p','/x/2.mkv',1,?)", (sid, now - 60))
        c.execute("INSERT INTO wanted (kind,series_id,season,episode,status,reason) "
                  "VALUES ('episode',?,1,3,'wanted','missing')", (sid,))
        mid = c.execute("INSERT INTO movies (tmdb_id,title,year) VALUES (9,'Rags',2012)").lastrowid
        c.execute("INSERT INTO library_movies (title,year,quality,source,path,size,mtime) "
                  "VALUES ('Rags',2012,'720p','CAM','/m/r.mkv',1,?)", (now,))
        c.execute("INSERT INTO wanted (kind,series_id,status,reason) "
                  "VALUES ('movie',?,'wanted','upgrade')", (mid,))
    return sid, mid


class _NoClient:
    def list_transfers(self):
        from faucet.clients import DownloadClientError
        raise DownloadClientError("down")


def test_home_for_admin_and_member(app, monkeypatch):
    sid, mid = _seed(app.db)
    monkeypatch.setattr(app.m["app"], "client", lambda: _NoClient())
    monkeypatch.setattr(app.m["tmdb"], "cached_season", lambda tid, s: [
        {"episode": 1, "still": "https://image.tmdb.org/t/p/w300/s1.jpg"}])
    monkeypatch.setattr(app.m["tmdb"], "details", lambda *a, **k: {
        "backdrop": "https://image.tmdb.org/t/p/w1280/b.jpg", "overview": "O", "year": 2011})
    a = app.admin.get("/api/home").json()
    assert a["admin"] is True
    assert [(e["episode"], e["ep_title"], e["still"]) for e in a["episodes"]] == [
        (1, "Ep 1", "https://image.tmdb.org/t/p/w300/s1.jpg"), (2, "Ep 2", None)]
    assert a["hero"]["title"] == "Show" and len(a["hero"]["new"]) == 2
    assert a["hero"]["backdrop"].endswith("/b.jpg")
    assert a["movies"] == [{"id": mid, "title": "Rags", "year": 2012, "poster": None,
                            "quality": "720p", "cam": True, "want": "upgrade"}]
    assert a["hunting"] == [{"id": sid, "title": "Show", "wanted": 1, "pct": 66,
                             "poster": "https://image.tmdb.org/t/p/w342/a.jpg"}]
    assert a["stats"]["client_ok"] is False and a["stats"]["wanted"] == 2
    b = app.bob.get("/api/home").json()
    assert b["admin"] is False and "downloads" not in b and "hunting" not in b and "stats" not in b
    assert len(b["episodes"]) == 2


def test_home_empty_library(app, monkeypatch):
    monkeypatch.setattr(app.m["app"], "client", lambda: _NoClient())
    d = app.admin.get("/api/home").json()
    assert d["episodes"] == [] and d["hero"] is None and d["movies"] == []


def test_shell_loads_content_hashed_modules(app):
    """Cloudflare rewrote no-cache to max-age=14400, so browsers kept 6.1's
    modules after 6.2 deployed. Modules now load from /app/<hash>/."""
    v = app.m["app"].ASSET_VERSION
    shell = app.admin.get("/").text
    assert f'src="/app/{v}/main.js"' in shell and f'href="/app/{v}/styles/app.css"' in shell
    assert "/static/app/" not in shell
    cur = app.anon.get(f"/app/{v}/links.js")                     # public, like /static
    assert cur.status_code == 200 and "immutable" in cur.headers["cache-control"]
    assert cur.headers["content-type"].startswith("text/javascript")
    old = app.anon.get("/app/0123456789ab/links.js")              # a page open across a deploy
    assert old.status_code == 200 and old.headers["cache-control"] == "no-cache"
    for bad in ("/app/x/../../app.py", "/app/x/%2e%2e/%2e%2e/app.py", "/app/x/package.json.bak",
                "/app/x/nope.js"):
        r = app.anon.get(bad)                     # never the file; a normalised
        assert r.status_code in (302, 404) and "import" not in r.text, bad   # path is just auth-gated


def test_asset_version_changes_with_content(app, tmp_path, monkeypatch):
    m = app.m["app"]
    before = m._asset_version()
    d = tmp_path / "appdir"
    (d / "ui").mkdir(parents=True)
    (d / "main.js").write_text("a")
    monkeypatch.setattr(m, "APP_DIR", d)
    v1 = m._asset_version()
    (d / "ui" / "x.js").write_text("b")
    assert v1 != m._asset_version() and before != v1

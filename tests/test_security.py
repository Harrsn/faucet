"""Security hardening regressions (phase 3 of the 2026-10 review).

S1  API keys leaked through requests' exception text (URL incl. apikey=).
S2  Request title/year/poster were taken from the client; poster URLs could
    break out of url('...') in a style attribute.
S3  Login lockout was per username only: anyone could lock the admin out,
    and the "locked" message revealed which usernames exist.
S4  faucet.env: non-atomic, world-readable, newline injection.
    PATCH /api/settings stored any key in the DB.
S5  No security headers.
S6  First-run race: concurrent registrations could all become admin.
S7  Settings "test" sent saved credentials to an arbitrary URL.
S8  Members could see library file paths.
"""
from __future__ import annotations

import importlib
import logging
import os
import stat
import threading
from types import SimpleNamespace

import pytest
import requests


# ── S1: redaction ────────────────────────────────────────────────────────────

def test_redact_shapes():
    from faucet.redact import redact
    s = redact("GET http://10.0.0.17:9117/api/v2.0/indexers/all/results/torznab/api"
               "?apikey=abcdef123456&t=search&q=x failed")
    assert "abcdef123456" not in s and "apikey=***" in s and "q=x" in s
    assert redact("http://admin:hunter22@host:9091/rpc") == "http://admin:***@host:9091/rpc"
    assert "ABCdef" not in redact("https://api.telegram.org/bot123:ABCdef-_x/sendMessage")
    assert "tok3n" not in redact("https://discord.com/api/webhooks/42/tok3n_value")
    assert redact("plain words: password reset") == "plain words: password reset"


def test_redact_masks_live_secrets_anywhere(monkeypatch):
    from faucet.redact import redact
    monkeypatch.setenv("JACKETT_API_KEY", "zzkeyzz999")
    assert redact("weird place zzkeyzz999 here") == "weird place *** here"


def _fresh_search():
    # other test modules assign faucet.search.search directly; start clean
    return importlib.reload(importlib.import_module("faucet.search"))


def test_search_error_never_carries_the_key(monkeypatch):
    search = _fresh_search()

    def boom(url, **k):
        raise requests.ConnectionError(f"HTTPConnectionPool: Max retries exceeded with url: {url}")
    monkeypatch.setattr(search.requests, "get", boom)
    with pytest.raises(search.SearchError) as ei:
        search.search("http://jackett:9117", "SECRETKEY123", "all", "dune", "all", 10)
    assert "SECRETKEY123" not in str(ei.value) and "apikey=***" in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


def test_indexer_name_cannot_inject_query(monkeypatch):
    search = _fresh_search()
    seen = {}

    class R:
        content = b"<rss><channel></channel></rss>"
        def raise_for_status(self): pass

    def fake(url, **k):
        seen["url"] = url
        return R()
    monkeypatch.setattr(search.requests, "get", fake)
    search.search("http://j", "key123456", "x/../all?apikey=evil&", "q", "all", 5)
    assert "apikey=evil" not in seen["url"]


def test_client_error_is_redacted():
    from faucet.clients.base import DownloadClientError
    e = DownloadClientError("Transmission unreachable: http://u:pw12345@h:9091/transmission/rpc")
    assert "pw12345" not in str(e)


def test_log_lines_are_redacted(caplog):
    from faucet.redact import RedactingFilter
    caplog.handler.addFilter(RedactingFilter())
    logging.getLogger("faucet.test").warning("query %s failed", "http://j/api?apikey=leakyleaky1")
    assert "leakyleaky1" not in caplog.text and "apikey=***" in caplog.text


# ── app fixture ─────────────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("FAUCET_CONFIG_FILE", str(tmp_path / "faucet.env"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("JACKETT_API_KEY", "savedkey123")
    monkeypatch.setenv("JACKETT_URL", "http://jackett.saved:9117")
    for k in ("TRUSTED_PROXIES", "COOKIE_SECURE", "CLIENT_IP_HEADERS",
              "LOGIN_MAX_FAILED", "LOGIN_IP_MAX_FAILED", "TMDB_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    mods = {}
    for m in ("config", "db", "tmdb", "auth", "requests_flow", "auth_routes", "app"):
        mods[m] = importlib.reload(importlib.import_module("faucet." + m))
    mods["db"].init()
    from fastapi.testclient import TestClient

    def client(user=None, pw=None):
        c = TestClient(mods["app"].app, follow_redirects=False)
        if user:
            r = c.post("/api/auth/login", json={"username": user, "password": pw})
            assert r.status_code == 200, r.text
            c.headers.update({"X-CSRF-Token": c.cookies.get("faucet_csrf") or ""})
        return c

    boot = client()
    assert boot.post("/api/auth/register",
                     json={"username": "admin", "password": "adminpass123"}).status_code == 200
    return SimpleNamespace(m=mods, client=client, tmp=tmp_path)


def _member(app, name="bob", pw="bobpass1234"):
    admin = app.client("admin", "adminpass123")
    r = admin.post("/api/admin/users", json={"username": name, "password": pw,
                                             "role": "user", "status": "active"})
    assert r.status_code == 200, r.text
    return app.client(name, pw)


# ── S2: requests use TMDb's metadata ─────────────────────────────────────────

def test_request_metadata_comes_from_tmdb(app, monkeypatch):
    T = app.m["tmdb"]
    monkeypatch.setattr(T, "enabled", lambda: True)
    monkeypatch.setattr(T, "details", lambda tid, mt, fresh=False: {
        "title": "Dune", "year": 2021, "poster": "https://image.tmdb.org/t/p/w342/d.jpg"})
    bob = _member(app)
    r = bob.post("/api/requests", json={
        "media_type": "movie", "tmdb_id": 438631, "title": "<img src=x onerror=alert(1)>",
        "year": 1066, "poster": "x');background:url(//evil.example/p"})
    assert r.status_code == 200, r.text
    with app.m["db"].connect() as c:
        row = dict(c.execute("SELECT title, year, poster FROM requests").fetchone())
    assert row == {"title": "Dune", "year": 2021,
                   "poster": "https://image.tmdb.org/t/p/w342/d.jpg"}


def test_unknown_tmdb_id_is_refused(app, monkeypatch):
    T = app.m["tmdb"]
    monkeypatch.setattr(T, "enabled", lambda: True)
    monkeypatch.setattr(T, "details", lambda *a, **k: {})
    bob = _member(app)
    r = bob.post("/api/requests", json={"media_type": "tv", "tmdb_id": 1, "title": "x"})
    assert r.status_code == 400
    r = bob.post("/api/requests", json={"media_type": "game", "tmdb_id": 1, "title": "x"})
    assert r.status_code == 400


def test_ui_only_renders_tmdb_image_urls():
    html = (__import__("pathlib").Path(__file__).parent.parent / "faucet" / "static"
            / "index.html").read_text()
    assert "background-image:url('${esc(" not in html
    assert 'src="${r.poster?esc(r.poster)' not in html
    assert "function safeImg" in html


# ── S3: login throttling ─────────────────────────────────────────────────────

def test_wrong_passwords_throttle_the_pair_not_the_account(app):
    c = app.client()
    for _ in range(5):
        assert c.post("/api/auth/login", json={"username": "admin",
                                               "password": "nope"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    # the account itself is not locked: a different address still gets in
    from faucet import netutil
    assert netutil.login_blocked("203.0.113.9", "admin") == 0
    with app.m["db"].connect() as c2:
        assert c2.execute("SELECT locked_until FROM users WHERE username='admin'"
                          ).fetchone()[0] is None


def test_unknown_and_known_usernames_throttle_identically(app):
    c = app.client()
    for user in ("admin", "nobody-here"):
        codes = [c.post("/api/auth/login", json={"username": user, "password": "x"}
                        ).status_code for _ in range(6)]
        assert codes == [401] * 5 + [429]


def test_ip_wide_limit_across_usernames(app, monkeypatch):
    monkeypatch.setenv("LOGIN_IP_MAX_FAILED", "4")
    c = app.client()
    for i in range(4):
        c.post("/api/auth/login", json={"username": f"u{i}", "password": "x"})
    assert c.post("/api/auth/login", json={"username": "admin",
                                           "password": "adminpass123"}).status_code == 429


def test_success_clears_the_pair_counter(app):
    c = app.client()
    for _ in range(4):
        c.post("/api/auth/login", json={"username": "admin", "password": "x"})
    assert c.post("/api/auth/login", json={"username": "admin",
                                           "password": "adminpass123"}).status_code == 200
    for _ in range(4):
        assert c.post("/api/auth/login", json={"username": "admin",
                                               "password": "x"}).status_code == 401


def test_registration_is_rate_limited(app):
    c = app.client()
    codes = [c.post("/api/auth/register", json={"username": f"spam{i}",
                                                "password": "longenough1"}).status_code
             for i in range(6)]
    assert codes[:4] == [200] * 4 and codes[-1] == 429   # admin's signup was the 1st


def _req(peer, headers=None, scheme="http"):
    from starlette.datastructures import Headers
    return SimpleNamespace(client=SimpleNamespace(host=peer),
                           headers=Headers(headers or {}),
                           url=SimpleNamespace(scheme=scheme))


def test_forwarding_headers_only_from_trusted_proxy(monkeypatch):
    from faucet import netutil
    spoof = {"cf-connecting-ip": "1.2.3.4", "x-forwarded-for": "5.6.7.8"}
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    assert netutil.client_ip(_req("10.0.0.50", spoof)) == "10.0.0.50"
    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.1")
    assert netutil.client_ip(_req("10.0.0.50", spoof)) == "10.0.0.50"
    assert netutil.client_ip(_req("172.18.0.1", spoof)) == "1.2.3.4"
    assert netutil.client_ip(_req("172.18.0.1", {"x-forwarded-for": "9.9.9.9, 127.0.0.1"})
                             ) == "127.0.0.1"           # right-most hop only


def test_trusted_proxies_auto_reads_default_gateway(monkeypatch, tmp_path):
    from faucet import netutil
    route = tmp_path / "route"
    route.write_text("Iface\tDestination\tGateway\tFlags\n"
                     "eth0\t00000000\t010012AC\t0003\n")       # 172.18.0.1
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: real_open(
        route if p == "/proc/net/route" else p, *a, **k))
    monkeypatch.setenv("TRUSTED_PROXIES", "auto")
    assert [str(n) for n in netutil.trusted_networks()] == ["172.18.0.1/32"]


def test_https_detection(monkeypatch):
    from faucet import netutil
    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.1")
    behind_caddy = {"cf-visitor": '{"scheme":"https"}', "x-forwarded-proto": "http"}
    assert netutil.is_https(_req("172.18.0.1", behind_caddy)) is True
    assert netutil.is_https(_req("10.0.0.50", behind_caddy)) is False
    monkeypatch.setenv("COOKIE_SECURE", "1")
    assert netutil.is_https(_req("10.0.0.50")) is True


# ── S4: config file + settings API ───────────────────────────────────────────

def test_config_save_rejects_newlines_and_writes_0600(app):
    cfg = app.m["config"]
    with pytest.raises(ValueError):
        cfg.save({"APP_TITLE": "x\nSESSION_SECRET=owned"})
    assert "SESSION_SECRET" not in (app.tmp / "faucet.env").read_text() \
        if (app.tmp / "faucet.env").exists() else True
    cfg.save({"APP_TITLE": "Faucet Test"})
    p = app.tmp / "faucet.env"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert "APP_TITLE=Faucet Test" in p.read_text()
    assert [x.name for x in app.tmp.iterdir() if x.name.startswith(".faucet-env-")] == []


def test_existing_world_readable_config_is_tightened(app):
    p = app.tmp / "faucet.env"
    p.write_text("APP_TITLE=x\n")
    os.chmod(p, 0o644)
    app.m["config"].harden_permissions()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_settings_patch_newline_is_a_400(app):
    admin = app.client("admin", "adminpass123")
    r = admin.patch("/api/settings", json={"values": {"APP_TITLE": "a\nb"}})
    assert r.status_code == 400


def test_settings_patch_only_known_db_keys(app):
    admin = app.client("admin", "adminpass123")
    r = admin.patch("/api/settings", json={"values": {
        "session_secret": "owned", "_tmdbcache:x": "1", "default_language": "fr"}})
    assert r.status_code == 200
    assert any("session_secret" in w for w in r.json()["warnings"])
    db = app.m["db"]
    assert db.get_setting("session_secret") in (None, "") or db.get_setting("session_secret") != "owned"
    assert db.get_setting("_tmdbcache:x") is None
    assert db.get_setting("default_language") == "fr"
    r = admin.patch("/api/settings", json={"values": {"default_language": "<x>",
                                                      "default_profile_id": 999}})
    assert len(r.json()["warnings"]) == 2


def test_tmdb_key_is_write_only(app):
    admin = app.client("admin", "adminpass123")
    admin.patch("/api/settings", json={"values": {"tmdb_key": "abcdef0123456789"}})
    body = admin.get("/api/settings").json()
    assert "tmdb_key" not in body["db"] and body["env"]["TMDB_KEY_SET"] is True
    assert "abcdef0123456789" not in admin.get("/api/settings").text


# ── S5: headers ─────────────────────────────────────────────────────────────

def test_security_headers_everywhere(app):
    anon = app.client()
    for path in ("/login", "/api/series", "/"):
        r = anon.get(path)
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"], path
        assert r.headers["x-content-type-options"] == "nosniff"


# ── S6: first-run race ──────────────────────────────────────────────────────

def test_concurrent_first_registrations_make_one_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    for m in ("config", "db", "auth"):
        importlib.reload(importlib.import_module("faucet." + m))
    from faucet import auth, db
    db.init()
    barrier = threading.Barrier(6)
    errors = []

    def go(i):
        barrier.wait()
        try:
            auth.create_user(f"racer{i}", "longenough1")
        except Exception as e:                   # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert not errors
    with db.connect() as c:
        roles = [r["role"] for r in c.execute("SELECT role FROM users")]
    assert len(roles) == 6 and roles.count("admin") == 1


# ── S7: settings test can't exfiltrate saved credentials ─────────────────────

def test_settings_test_withholds_saved_key_from_new_url(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.m["app"].searchmod, "search",
                        lambda url, key, *a, **k: calls.append((url, key)) or [])
    admin = app.client("admin", "adminpass123")
    r = admin.post("/api/settings/test", json={"jackett_url": "http://attacker.example"})
    assert r.json()["indexer"]["ok"] is False and calls == []
    admin.post("/api/settings/test", json={})
    assert calls == [("http://jackett.saved:9117", "savedkey123")]


# ── S8: members don't see paths ─────────────────────────────────────────────

def test_movie_detail_hides_path_from_members(app, monkeypatch):
    from faucet import movies
    monkeypatch.setattr(movies, "movie_detail", lambda mid: {
        "movie": {"id": mid}, "meta": {},
        "file": {"quality": "1080p", "size": 1, "path": "/library/movies/X/X.mkv"}})
    bob = _member(app)
    assert "path" not in bob.get("/api/movies/1/detail").json()["file"]
    admin = app.client("admin", "adminpass123")
    assert admin.get("/api/movies/1/detail").json()["file"]["path"].endswith("X.mkv")

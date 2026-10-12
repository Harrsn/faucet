"""Installable app: manifest, icons, service worker."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("SESSION_SECRET", "t")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m)) for m in ("config", "db", "auth", "auth_routes", "app")}
    mods["db"].init()
    from fastapi.testclient import TestClient
    c = TestClient(mods["app"].app, follow_redirects=False)
    c.app_mod = mods["app"]
    return c


def test_manifest_is_public_and_points_at_real_icons(client):
    r = client.get("/manifest.webmanifest")                  # no session: the sign-in page needs it too
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert (m["name"], m["display"], m["start_url"], m["scope"]) == ("Faucet", "standalone", "/", "/")
    assert {i["purpose"] for i in m["icons"]} == {"any", "maskable"}
    assert any(i["sizes"] == "512x512" for i in m["icons"]) and any(i["sizes"] == "192x192" for i in m["icons"])
    v = client.app_mod.ASSET_VERSION
    for i in m["icons"]:
        assert i["src"].startswith(f"/app/{v}/icons/")
        img = client.get(i["src"])
        assert img.status_code == 200 and img.headers["content-type"] == "image/png"
        assert img.content[:8] == b"\x89PNG\r\n\x1a\n" and "immutable" in img.headers["cache-control"]


def test_service_worker(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "no-cache" and r.headers["service-worker-allowed"] == "/"
    assert f'const VERSION = "{client.app_mod.ASSET_VERSION}"' in r.text and "__ASSET_VERSION__" not in r.text
    assert "/api/" not in r.text.split("const OFFLINE")[0].replace("// ", "")   # never caches the API


def test_pages_declare_the_manifest(client):
    v = client.app_mod.ASSET_VERSION
    page = client.get("/login").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    assert f'<link rel="apple-touch-icon" href="/app/{v}/icons/apple-touch-icon.png">' in page
    assert 'name="theme-color" content="#0E1013"' in page

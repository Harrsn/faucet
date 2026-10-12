"""Preferred-format rules in quality profiles: codec, HDR, release groups and
never-words."""
from __future__ import annotations

import pytest

from faucet import profiles as P
from faucet.search import parse_badges, release_group


@pytest.mark.parametrize("name,group", [
    ("Family.Guy.S23E04.1080p.WEB.h264-ETHEL[eztv.re].mkv", "ETHEL"),
    ("Severance.S02E01.2160p.ATVP.WEB-DL.DDP5.1.DV.HDR.H.265-FLUX", "FLUX"),
    ("Rags.2012.1080p.WEBRip.x264-YIFY", "YIFY"),
    ("Some Show S01E01 1080p WEB-DL", None),                 # 'DL' isn't a group
    ("Movie.2020.BluRay.1080p", None),
    ("Bluey.S03E01.720p.HEVC.x265-MeGusta", "MeGusta"),
])
def test_release_group(name, group):
    assert release_group(name) == group


def test_codec_and_hdr_badges():
    assert parse_badges("Show.S01E01.1080p.WEB.H.265-X")["codec"] == "HEVC"
    assert parse_badges("Show.S01E01.1080p.WEB.x264-X")["codec"] == "H.264"
    assert parse_badges("Show S01E01 1080p AV1 10bit")["codec"] == "AV1"
    assert parse_badges("Show.2160p.DV.HDR10+.WEB")["hdr"] is True
    assert parse_badges("Show.S01E01.1080p.WEB.x264")["hdr"] is False


def rel(title, seeders=50):
    return {"title": title, "seeders": seeders, "size": 2 * 1024 ** 3, "badges": parse_badges(title)}


BASE = {"min_seeders": 1, "resolutions": ["1080p", "720p"], "sources": ["WEB-DL", "WEBRip", "HDTV"], "language": "any"}


def prof(**formats):
    return {**BASE, "formats": formats}


def test_never_rules_refuse_with_a_reason():
    p = prof(never_groups=["YIFY"], never_words=["HC", "KORSUB"], hdr="never")
    assert P.passes(rel("Movie.2020.1080p.WEBRip.x264-YIFY"), p) == (False, "format: release group YIFY is on the never list")
    assert P.passes(rel("Movie.2020.1080p.HC.WEBRip.x264-ABC"), p)[1] == "format: contains 'HC'"
    assert P.passes(rel("Movie.2020.1080p.KORSUB.WEBRip-ABC"), p)[0] is False
    assert P.passes(rel("Movie.2020.1080p.HCL.WEBRip-ABC"), p)[0] is True       # whole words only
    assert P.passes(rel("Movie.2020.1080p.WEB-DL.DV.HDR.H.265-ABC"), p)[1] == "format: HDR"
    assert P.passes(rel("Movie.2020.1080p.WEB-DL.H.264-ABC"), p) == (True, "")


def test_only_codec():
    p = prof(codec="only-h264")
    assert P.passes(rel("Show.S01E01.1080p.WEB.x265-A"), p)[0] is False
    assert P.passes(rel("Show.S01E01.1080p.WEB.x264-A"), p)[0] is True
    assert P.passes(rel("Show.S01E01.1080p.WEB-A"), p)[1].startswith("format: unknown codec")


def test_preferences_rank_but_never_beat_resolution():
    p = prof(codec="prefer-h264", prefer_groups=["NTb"])
    hevc = rel("Show.S01E01.1080p.WEB-DL.x265-A")
    h264 = rel("Show.S01E01.1080p.WEBRip.x264-A")                              # worse source…
    assert P.score(h264, p) > P.score(hevc, p)                                   # …codec preference wins
    lower_res = rel("Show.S01E01.720p.WEB-DL.x264-NTb")
    assert P.score(rel("Show.S01E01.1080p.WEB-DL.x265-A"), p) > P.score(lower_res, p)
    assert P.score(rel("Show.S01E01.1080p.WEB-DL.x265-NTb"), p) - P.score(rel("Show.S01E01.1080p.WEB-DL.x265-A"), p) == 30
    avoid = prof(hdr="avoid")
    assert P.score(rel("M.2160p.WEB-DL.HDR.x265-A"), avoid) < P.score(rel("M.2160p.WEB-DL.x265-A"), avoid)


def test_formats_are_normalised_and_old_profiles_unaffected():
    assert P.formats({}) == {"codec": "", "hdr": "", "prefer_groups": [], "never_groups": [], "never_words": []}
    assert P.formats({"formats": '{"codec": "bogus", "never_words": ["  HC ", ""]}'})["never_words"] == ["HC"]
    assert P.passes(rel("Movie.2020.1080p.WEBRip.x264-YIFY"), dict(BASE)) == (True, "")


def test_hunter_counts_format_refusals(monkeypatch):
    from faucet import scheduler
    reasons = {}
    monkeypatch.setattr(scheduler, "_tracker_penalties", lambda: {})
    kept = scheduler._rank_for_hunt([rel("Show.S01E01.1080p.WEB.x264-YIFY"), rel("Show.S01E01.1080p.WEB.x264-OK")],
                                    prof(never_groups=["yify"]), reasons)
    assert [k["title"] for k in kept] == ["Show.S01E01.1080p.WEB.x264-OK"] and reasons == {"format": 1}


def test_profile_api(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "config" / "events.jsonl"))
    monkeypatch.setenv("SESSION_SECRET", "t")
    mods = {m: importlib.reload(importlib.import_module("faucet." + m)) for m in ("config", "db", "auth", "auth_routes", "app")}
    mods["db"].init()
    from fastapi.testclient import TestClient
    c = TestClient(mods["app"].app, follow_redirects=False)
    c.headers["X-Faucet-Auth"] = "2"
    c.post("/api/auth/register", json={"username": "admin", "password": "adminpass123"})
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    c.headers["X-CSRF-Token"] = c.cookies.get("faucet_csrf")
    body = {"name": "Mamaw", "resolutions": ["1080p", "720p"], "sources": ["WEB-DL"],
            "formats": {"codec": "prefer-h264", "hdr": "never", "never_words": ["HC"]}}
    pid = c.post("/api/profiles", json=body).json()["id"]
    got = next(p for p in c.get("/api/profiles").json()["profiles"] if p["id"] == pid)
    assert got["formats"]["codec"] == "prefer-h264" and got["formats"]["never_words"] == ["HC"]
    assert c.post("/api/profiles", json={**body, "formats": {"codec": "x266"}}).status_code == 422
    assert c.put(f"/api/profiles/{pid}", json={**body, "formats": {}}).status_code == 200
    assert next(p for p in c.get("/api/profiles").json()["profiles"] if p["id"] == pid)["formats"]["codec"] == ""

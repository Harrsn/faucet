"""Pages read the download client's list from a background poller and never
wait on a slow client."""
from __future__ import annotations

import importlib
import threading
import time
from types import SimpleNamespace

import pytest


def T(i, name="x", status="downloading", pct=50.0, rate=1000):
    return SimpleNamespace(id=i, name=name, status=status, percent=pct, down_rate=rate,
                           up_rate=0, eta=60, size=10, error="", ratio=0.0, done=pct >= 100)


@pytest.fixture
def tr(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("TRANSFERS_POLL_SECONDS", "1")
    importlib.reload(importlib.import_module("faucet.config"))
    m = importlib.reload(importlib.import_module("faucet.transfers"))
    m._reset_for_tests()
    return m


def _client(monkeypatch, tr, fn):
    class C:
        timeout = 90

        def list_transfers(self):
            assert self.timeout == 10                # polls never wait 90s
            return fn()
    tr.configure(lambda: C())


def test_snapshot_returns_without_waiting_on_a_hung_client(tr, monkeypatch):
    calls = {"n": 0}
    hang = threading.Event()

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            return [T(1, "A")]
        hang.wait(30)                            # the second poll hangs
        return []
    _client(monkeypatch, tr, fn)
    s = tr.snapshot()
    assert [t.name for t in s["items"]] == ["A"] and s["ok"] is True and s["stale"] is False
    time.sleep(1.3)                              # the next poll is now stuck
    t0 = time.monotonic()
    s = tr.snapshot()
    assert time.monotonic() - t0 < 0.2 and [t.name for t in s["items"]] == ["A"]
    hang.set()


def test_failed_poll_keeps_the_last_good_list(tr, monkeypatch):
    seq = iter([[T(1, "A")], RuntimeError("Read timed out"), RuntimeError("Read timed out")])

    def fn():
        x = next(seq, RuntimeError("down"))
        if isinstance(x, Exception):
            raise x
        return x
    _client(monkeypatch, tr, fn)
    assert tr.snapshot()["ok"] is True
    tr.refresh_soon()
    time.sleep(0.3)
    s = tr.snapshot()
    assert s["ok"] is False and "timed out" in s["error"] and [t.name for t in s["items"]] == ["A"]


def test_cold_cache_with_dead_client(tr, monkeypatch):
    def fn():
        raise ConnectionError("refused")
    _client(monkeypatch, tr, fn)
    s = tr.snapshot()
    assert s["ok"] is False and s["items"] == [] and s["stale"] is True


def test_cold_cache_wakes_a_sleeping_poller(tr, monkeypatch):
    """CI caught this: after a reset the poller could sleep out its interval
    while the first reader gave up after 3s and showed an empty list."""
    monkeypatch.setenv("TRANSFERS_POLL_SECONDS", "30")
    _client(monkeypatch, tr, lambda: [T(1, "A")])
    assert [t.name for t in tr.snapshot()["items"]] == ["A"]   # thread now sleeping 30s
    tr.configure(tr._factory)                                  # cold again (a reload, a restart)
    _client(monkeypatch, tr, lambda: [T(2, "B")])
    t0 = time.monotonic()
    assert [t.name for t in tr.snapshot()["items"]] == ["B"]
    assert time.monotonic() - t0 < 1.5

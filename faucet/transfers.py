"""The download client's transfer list, polled in the background.

Home, Activity and the activity indicator used to call the client inside the
request. Transmission answers RPC on the same thread that does its disk I/O,
so while a completed season pack was being copied onto the NAS a page could
hang for the whole client timeout (90s at the time). Now one thread polls the
client with a short timeout and pages read the last good list instantly — if
the client is slow, they show that list with its age and a note, never a
spinner.

The poller sleeps when nobody has looked for a minute, so an idle Faucet
doesn't poll forever.

Env (read at each poll): TRANSFERS_POLL_SECONDS (default 5).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

log = logging.getLogger("faucet.transfers")

_IDLE_AFTER = 60.0          # stop polling this long after the last reader
_FIRST_WAIT = 3.0           # a cold cache waits this long for the first poll
_CLIENT_TIMEOUT_CAP = 10    # a poll never waits longer than this

_lock = threading.Lock()
_wake = threading.Event()
_ready = threading.Event()
_state = {"items": [], "ok": None, "error": None, "ts": None, "mono": None,
          "last_read": None, "thread": None}


def _interval() -> float:
    try:
        return max(1.0, float(os.environ.get("TRANSFERS_POLL_SECONDS", "5")))
    except ValueError:
        return 5.0


_factory = None


def configure(factory) -> None:
    """The app hands over how to build its client (so the poller uses the
    same configuration — and tests' fakes); also starts from a clean list."""
    global _factory
    _factory = factory
    _reset_for_tests()


def _client():
    if _factory is not None:
        cl = _factory()
    else:
        from . import config
        from .clients import make_client
        c = config.config
        cl = make_client(c.client_kind, c.client_url, c.client_user, c.client_pass,
                         c.request_timeout)
    if isinstance(getattr(cl, "timeout", None), (int, float)):
        cl.timeout = max(2, min(cl.timeout, _CLIENT_TIMEOUT_CAP))
    return cl


def _poll_once() -> None:
    from . import config
    try:
        items = _client().list_transfers()
    except Exception as e:                       # noqa: BLE001 - recorded, never raised
        from .redact import redact
        with _lock:
            _state.update(ok=False, error=redact(e, config.config.client_pass))
        _ready.set()
        return
    with _lock:
        _state.update(items=list(items), ok=True, error=None,
                      ts=datetime.now().isoformat(timespec="seconds"), mono=time.monotonic())
    _ready.set()


def _idle() -> bool:
    last = _state["last_read"]
    return last is None or time.monotonic() - last > _IDLE_AFTER


def _loop() -> None:
    while True:
        with _lock:
            idle = _idle()
        if idle:
            _wake.wait()                         # until someone reads again
        _wake.clear()
        _poll_once()
        _wake.wait(_interval())


def _ensure_thread() -> None:
    with _lock:
        t = _state["thread"]
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=_loop, name="transfers-poll", daemon=True)
        _state["thread"] = t
    t.start()


def snapshot() -> dict:
    """The latest transfer list without touching the client: {items, ok,
    error, as_of, age, stale}. `ok` is about the latest poll; `items` is the
    last list that succeeded."""
    with _lock:
        idle_before = _idle()
        _state["last_read"] = time.monotonic()
    _ensure_thread()
    cold = not _ready.is_set()
    if idle_before or cold:
        _wake.set()                              # poll now, don't sleep out the interval
    if cold:
        _ready.wait(_FIRST_WAIT)
    with _lock:
        age = None if _state["mono"] is None else round(time.monotonic() - _state["mono"], 1)
        return {"items": list(_state["items"]), "ok": _state["ok"], "error": _state["error"],
                "as_of": _state["ts"], "age": age,
                "stale": age is None or age > 3 * _interval()}


def refresh_soon() -> None:
    """After pause / resume / remove / add: poll now rather than in 5s."""
    _wake.set()


def _reset_for_tests() -> None:
    with _lock:
        _state.update(items=[], ok=None, error=None, ts=None, mono=None, last_read=None)
    _ready.clear()

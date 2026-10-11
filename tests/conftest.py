"""Make the in-repo `faucet` package importable for every test module.

CI runs plain `pytest`, which (unlike `python -m pytest`) doesn't put the repo
root on sys.path. test_faucet.py used to do this itself, so any test file that
sorted before it and imported `faucet` at module level failed to collect.
conftest.py is loaded before any test module, whatever the order.
"""
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_login_throttle():
    """Login/registration throttling is in-process state keyed by client IP,
    and every TestClient request comes from the same address — without a
    reset, one test's registrations would throttle the next test's."""
    from faucet import netutil
    netutil.reset_state()
    yield
    netutil.reset_state()


@pytest.fixture(autouse=True)
def _restore_environ():
    """config._load_persisted() writes the persisted settings file straight
    into os.environ, which monkeypatch can't undo; a JACKETT_INDEXER (or any
    other key) saved by one test used to leak into every later one."""
    import os
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _admin_2fa_optional_in_tests(monkeypatch, request):
    """Production requires two-factor for admins; the suite's admins sign in
    with a password unless a test opts in (marker `requires_admin_2fa`)."""
    if request.node.get_closest_marker("requires_admin_2fa") is None:
        monkeypatch.setenv("FAUCET_REQUIRE_ADMIN_2FA", "0")
    yield

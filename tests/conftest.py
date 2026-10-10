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

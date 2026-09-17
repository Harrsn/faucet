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

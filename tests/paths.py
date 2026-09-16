"""Stable filesystem roots for the test suite.

Nested test modules must not compute fixture or repo paths from
``Path(__file__).parents[N]``; that depth changes when files move.
"""

from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
REPO_ROOT = TESTS_DIR.parent

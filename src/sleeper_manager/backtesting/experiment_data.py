"""Compatibility alias for historical experiment data."""

import sys

from sleeper_manager.backtesting.experiments import data as _implementation

sys.modules[__name__] = _implementation

"""Compatibility alias for lock-in policy experiments."""

import sys

from sleeper_manager.backtesting.experiments import lock_in as _implementation

sys.modules[__name__] = _implementation

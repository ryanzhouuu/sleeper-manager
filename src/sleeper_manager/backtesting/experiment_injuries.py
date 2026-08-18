"""Compatibility alias for historical injury archives."""

import sys

from sleeper_manager.backtesting.experiments import injuries as _implementation

sys.modules[__name__] = _implementation

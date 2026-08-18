"""Compatibility alias for experiment serialization helpers."""

import sys

from sleeper_manager.backtesting.experiments import io as _implementation

sys.modules[__name__] = _implementation

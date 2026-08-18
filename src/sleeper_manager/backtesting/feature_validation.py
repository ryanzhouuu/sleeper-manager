"""Compatibility alias for feature validation experiments."""

import sys

from sleeper_manager.backtesting.experiments import feature_validation as _implementation

sys.modules[__name__] = _implementation

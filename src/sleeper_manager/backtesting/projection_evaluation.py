"""Compatibility alias for projection evaluation experiments."""

import sys

from sleeper_manager.backtesting.experiments import projection_evaluation as _implementation

sys.modules[__name__] = _implementation

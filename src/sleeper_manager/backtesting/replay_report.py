"""Compatibility alias for replay reports."""

import sys

from sleeper_manager.backtesting.replay import report as _implementation

sys.modules[__name__] = _implementation

"""Keep the deployed Worker import path clear of excluded research dependencies."""

import subprocess
import sys


def test_worker_import_avoids_excluded_dependencies() -> None:
    """Worker startup must not import packages excluded from its bundle."""

    script = (
        "import sys\n"
        'sys.modules["httpx"] = None\n'
        'sys.modules["pydantic_settings"] = None\n'
        "from sleeper_manager.cloudflare.worker import Default\n"
        "from sleeper_manager.workflows.forecast_collection import CaptureOnlyForecastPolicy\n"
        "CaptureOnlyForecastPolicy()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

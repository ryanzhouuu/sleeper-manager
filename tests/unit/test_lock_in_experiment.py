from pathlib import Path

from sleeper_manager.backtesting.lock_in_experiment import (
    LockInExperimentError,
    run_lock_in_policy_validation,
)


def test_lock_in_experiment_fails_closed_without_cached_archives(tmp_path: Path) -> None:
    try:
        run_lock_in_policy_validation(
            tmp_path,
            current_league_id="current",
            historical_league_id="historical",
            stress_league_id="stress",
        )
    except LockInExperimentError as error:
        assert "Missing cached Sleeper artifact" in str(error)
    else:
        raise AssertionError("missing cached archives must fail closed")

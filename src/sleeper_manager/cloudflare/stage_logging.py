"""Emit stage entry before expensive work; CPU termination may leave no completion event."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter


@contextmanager
def timed_stage(name: str, *, correlation_id: str | None = None) -> Iterator[None]:
    """Measure wall time, not CPU; exception events expose only the exception type."""
    fields = {"event": "scheduled_stage", "stage": name}
    if correlation_id is not None:
        fields["correlation_id"] = correlation_id
    started = perf_counter()
    print(json.dumps({**fields, "outcome": "started"}), flush=True)
    try:
        yield
    except BaseException as error:
        print(
            json.dumps(
                {
                    **fields,
                    "outcome": "failed",
                    "wall_ms": round((perf_counter() - started) * 1000, 3),
                    "error_type": type(error).__name__,
                }
            ),
            flush=True,
        )
        raise
    else:
        print(
            json.dumps(
                {
                    **fields,
                    "outcome": "completed",
                    "wall_ms": round((perf_counter() - started) * 1000, 3),
                }
            ),
            flush=True,
        )

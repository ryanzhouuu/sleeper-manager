"""Wake the existing queue after a durable user-requested replan has been saved."""

from datetime import datetime
from typing import Any

from sleeper_manager.cloudflare.planning_queue import PlanningWake


async def publish_planning_wake(env: Any, now: datetime) -> bool:
    """A transport failure leaves saved D1 work available to the next cron wake."""
    queue = getattr(env, "PLANNING_QUEUE", None)
    if queue is None:
        return False
    try:
        await queue.send(PlanningWake(now).to_body(), contentType="text")
        return True
    except Exception as error:
        print(f"Manager replan wake failed: {type(error).__name__}")
        return False

"""Status and JSON payload returned by Worker route handlers."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RouteResponse:
    status_code: int
    payload: dict[str, Any]

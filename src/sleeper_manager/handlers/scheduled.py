import json
from typing import Any


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """AWS Lambda entry point; workflow wiring is added in the deployment milestone."""

    del context
    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "status": "initialized",
                "source": event.get("source", "manual"),
            }
        ),
    }

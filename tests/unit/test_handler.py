import json

from sleeper_manager.handlers.scheduled import handler


def test_scheduled_handler_is_invokable() -> None:
    response = handler({"source": "test"}, object())

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "initialized", "source": "test"}

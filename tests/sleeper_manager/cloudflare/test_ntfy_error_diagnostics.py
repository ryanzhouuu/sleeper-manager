"""Provider diagnostics identify the limiter without retaining private response contents."""

import asyncio
import json

import pytest

from sleeper_manager.cloudflare.notifications import CloudflareNtfySender
from tests.sleeper_manager.cloudflare.test_cloudflare_notifications import notification


class RejectedResponse:
    ok = False
    status = 429

    def __init__(self, *, payload: object = None, length: str | None = None) -> None:
        self.body = json.dumps(payload).encode()
        self.headers = {
            "content-type": "application/json",
            "content-length": str(len(self.body)) if length is None else length,
            "retry-after": "300",
        }
        self.reads = 0

    async def bytes(self) -> bytes:
        self.reads += 1
        return self.body


def send_error(response: RejectedResponse) -> str:
    """Exercise the production sender rather than testing the parser in isolation."""

    async def fetch(*args, **kwargs):  # type: ignore[no-untyped-def]
        return response

    sender = CloudflareNtfySender(
        "private-topic", base_url="https://ntfy.sh", access_token="secret", fetcher=fetch
    )
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(sender.send(notification()))
    return str(raised.value)


def test_ntfy_code_and_retry_delay_survive_without_private_response_fields() -> None:
    response = RejectedResponse(
        payload={"code": 42908, "error": "private-topic secret https://private.example/token"}
    )
    error = send_error(response)
    assert "HTTP 429" in error
    assert "ntfy_code=42908" in error
    assert "retry_after_seconds=300" in error
    assert "private" not in error
    assert "secret" not in error
    assert response.reads == 1


@pytest.mark.parametrize("code", [True, "42908", -1, 999999, None])
def test_untrusted_codes_are_omitted(code: object) -> None:
    error = send_error(RejectedResponse(payload={"code": code}))
    assert "ntfy_code=" not in error
    assert "HTTP 429" in error


@pytest.mark.parametrize("length", ["8193", "-1", "invalid", ""])
def test_unbounded_or_invalid_response_sizes_are_not_read(length: str) -> None:
    response = RejectedResponse(payload={"code": 42908}, length=length)
    assert "HTTP 429" in send_error(response)
    assert response.reads == 0


def test_html_or_malformed_body_cannot_replace_the_original_http_failure() -> None:
    response = RejectedResponse(payload={"code": 42908})
    response.headers["content-type"] = "text/html"
    assert "response_type=html" in send_error(response)
    assert response.reads == 0
    response.headers["content-type"] = "application/json"
    response.body = b"not JSON"
    assert "HTTP 429" in send_error(response)


def test_invalid_retry_header_is_not_echoed() -> None:
    response = RejectedResponse(payload={"code": 42908})
    response.headers["retry-after"] = "private-topic secret"
    error = send_error(response)
    assert "retry_after" not in error
    assert "private" not in error

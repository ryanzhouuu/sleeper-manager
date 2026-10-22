"""Small server-rendered forms shared by owner controls and scoped confirmations."""

from dataclasses import dataclass, field
from html import escape
from typing import Any
from urllib.parse import parse_qs, urlparse

_STYLE = """:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#11161d;
color:#e5eaf0;font:15px/1.5 ui-monospace,monospace}main{max-width:48rem;margin:auto;
padding:2rem 1rem}section{background:#1c242e;padding:1rem;margin:1rem 0}h1,h2,p{
font-size:inherit;margin:0 0 .75rem}h1,h2,a{color:#8baac6}form{margin:.75rem 0}
label{display:block;margin:.5rem 0}button,input,select{font:inherit;color:inherit;
background:#11161d;border:1px solid #8baac6;padding:.6rem;max-width:100%}button{
cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible{
outline:2px solid #e5eaf0;outline-offset:3px}small{color:#b2c0cd}pre{white-space:pre-wrap}
.notice{border-left:3px solid #8baac6;padding:.75rem}.actions{display:flex;gap:.75rem;
flex-wrap:wrap}details{margin-top:1rem}li{margin:.5rem 0}
"""


@dataclass(frozen=True)
class FormPage:
    """Carry HTML and headers independently of the Workers SDK."""

    body: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)


def page(title: str, content: str, *, status: int = 200) -> FormPage:
    """Content is already escaped markup; never cache credentials or decision forms."""
    return FormPage(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style><main>"
        f"<h1>{escape(title)}</h1>{content}</main></html>",
        status,
        {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store",
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
            "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        },
    )


def hidden(name: str, value: object) -> str:
    return f'<input type="hidden" name="{escape(name)}" value="{escape(str(value))}">'


def same_origin(request: Any) -> bool:
    """Reject cross-site or origin-less browser writes, including login forms."""
    parsed = urlparse(str(request.url))
    return bool(request.headers.get("origin") == f"{parsed.scheme}://{parsed.netloc}")


async def form_values(request: Any) -> dict[str, str]:
    """Accept bounded URL-encoded forms, rejecting duplicate field names."""
    content_type = str(request.headers.get("content-type") or "").split(";")[0]
    if content_type != "application/x-www-form-urlencoded":
        raise ValueError("Expected form data")
    length = request.headers.get("content-length")
    if length is not None and int(length) > 4096:
        raise ValueError("Form too large")
    reader = request.body.getReader()
    chunks = bytearray()
    try:
        while True:
            part = await reader.read()
            if part.done:
                break
            chunks.extend(bytes(part.value.to_py()))
            if len(chunks) > 4096:
                await reader.cancel()
                raise ValueError("Form too large")
    finally:
        reader.releaseLock()
    values = parse_qs(chunks.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
    if any(len(items) != 1 for items in values.values()):
        raise ValueError("Duplicate fields")
    return {key: items[0] for key, items in values.items()}

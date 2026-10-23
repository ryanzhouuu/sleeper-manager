"""Small server-rendered forms shared by owner controls and scoped confirmations."""

import json
from dataclasses import dataclass, field, replace
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
flex-wrap:wrap}details{margin-top:1rem}li{margin:.5rem 0}fieldset{border:0;padding:0;margin:0}
.choice{display:inline-flex;align-items:center;gap:.4rem;margin-right:1rem}
.icon{width:1rem;height:1rem;flex:none;vertical-align:middle}.muted{color:#94a5b6}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
clip:rect(0,0,0,0);white-space:nowrap;border:0}.manager-head{display:flex;
justify-content:space-between;align-items:center;margin:0 0 1.5rem}.manager-head h1{
font-size:1.4rem;margin:0;color:#edf3f8}.manager-head p{margin:0;font-size:.8rem}
.game-card{border:1px solid #2d3946;border-radius:10px;margin:.75rem 0;padding:1rem;
background:#19222c}.game-head,.game-state{display:flex;justify-content:space-between;
align-items:center;gap:.75rem}.game-head{align-items:flex-start}.game-head h2{color:#edf3f8;
font-size:1rem;margin:0 0 .2rem}.game-meta{color:#94a5b6;font-size:.75rem;margin:0}
.game-score{text-align:right;font-size:1.65rem;line-height:1.15;font-variant-numeric:tabular-nums}
.game-score small{display:block;font-size:.65rem;letter-spacing:.08em;color:#94a5b6}
.game-state{margin:1rem 0 .75rem;font-size:.75rem}.state-group{display:flex;align-items:center;
gap:.4rem}.chip{display:inline-flex;gap:.3rem;align-items:center;font-size:.75rem;
padding:.18rem .4rem;border-radius:4px;background:#253140}.chip--wait{color:#a8bfda}
.chip--lock{color:#8bd6b0;background:#20372f}.chip--pass{color:#e4bd7c;background:#382f24}
.chip--alert{color:#d6a4a4;background:#372b30}.choices{display:flex;gap:.4rem}
.choices .choice{position:relative;display:flex;flex:1;margin:0;min-width:0}
.choices input{position:absolute;opacity:0;width:1px;height:1px;padding:0;border:0}
.choices .option{display:flex;align-items:center;justify-content:center;gap:.45rem;
min-height:44px;width:100%;border:1px solid #344252;border-radius:6px;color:#9dadbe;
background:#141c25;cursor:pointer;font-size:.8rem}.choices .option:hover{border-color:#70849a}
.choices input:checked+.option{border-color:#a8bfda;color:#e3edf7;background:#29394b}
.choices input[value=locked]:checked+.option{border-color:#8bd6b0;color:#8bd6b0;background:#20372f}
.choices input[value=passed]:checked+.option{border-color:#e4bd7c;color:#e4bd7c;background:#382f24}
.choices input:focus-visible+.option{outline:2px solid #edf3f8;outline-offset:3px}
.game-deadline{display:flex;align-items:center;gap:.35rem;margin:.7rem 0 0;color:#94a5b6;
font-size:.7rem}.plan-details{margin:0 0 1.2rem;border-bottom:1px solid #2d3946;padding-bottom:1rem}
summary{cursor:pointer;min-height:32px}summary:hover{color:#edf3f8}.plan-details pre{
font-size:.8rem;color:#b8c5d2;max-height:26rem;overflow:auto}.manager-help{font-size:.8rem;
color:#94a5b6}.manager-help p{margin:.5rem 0}.save-bar{position:sticky;bottom:0;
padding:.8rem 0 .5rem;background:#11161df5;display:flex;gap:1rem;align-items:center;
justify-content:space-between;border-top:1px solid #2d3946;margin-top:1rem}.save-bar button{
display:inline-flex;align-items:center;gap:.6rem;background:#b7d7ec;color:#12212d;
border-color:#b7d7ec;border-radius:6px;font-weight:600;min-height:44px;padding:.65rem 1rem}
.save-bar small{font-size:.7rem}.section-heading{display:flex;gap:.5rem;align-items:center;
margin:0 0 .5rem;font-size:.8rem;color:#94a5b6}.count{display:inline-flex;align-items:center;
justify-content:center;min-width:1.3rem;height:1.3rem;border-radius:4px;background:#293746;
color:#d8e6f2;font-size:.7rem}.manager-footer{display:flex;justify-content:space-between;
align-items:center;margin-top:1.5rem;font-size:.75rem}.manager-footer form{margin:0}
.manager-footer button{border:0;background:none;color:#94a5b6;padding:.5rem}
.player-heading{display:flex;align-items:center;gap:.65rem;min-width:0}
.player-portrait{width:44px;height:44px;border-radius:50%;object-fit:cover;object-position:top;
background:#283543;flex:none}.team-mark{width:18px;height:18px;object-fit:contain;flex:none}
.game-meta{display:flex;align-items:center;gap:.35rem;flex-wrap:wrap}.game-score{flex:none}
@media(max-width:420px){main{padding:1.2rem .8rem}.game-card{padding:.8rem}.game-state{
gap:.4rem}.game-head h2{font-size:.9rem}.save-bar{gap:.5rem}.save-bar small{max-width:8rem}}
"""


@dataclass(frozen=True)
class FormPage:
    """Carry HTML and headers independently of the Workers SDK."""

    body: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    data: dict[str, object] = field(default_factory=dict)


def page(
    title: str,
    content: str,
    *,
    status: int = 200,
    heading: bool = True,
    data: dict[str, object] | None = None,
) -> FormPage:
    """Content is already escaped markup; never cache credentials or decision forms."""
    return FormPage(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style><main>"
        + (f"<h1>{escape(title)}</h1>" if heading else "")
        + f"{content}</main></html>",
        status,
        {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store",
            # no-referrer turns browser form POST origins into "null".
            "referrer-policy": "same-origin",
            "x-content-type-options": "nosniff",
            "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; "
            "img-src https://a.espncdn.com; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        },
        data if data is not None else {"status": "message", "title": title},
    )


def json_page(form: FormPage) -> FormPage:
    """Expose the explicit document rather than HTML to the same authenticated route callers."""
    return replace(
        form,
        body=json.dumps(form.data, allow_nan=False),
        headers={**form.headers, "content-type": "application/json; charset=utf-8"},
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
    values = parse_qs(chunks.decode("utf-8"), keep_blank_values=True, max_num_fields=70)
    if any(len(items) != 1 for items in values.values()):
        raise ValueError("Duplicate fields")
    return {key: items[0] for key, items in values.items()}

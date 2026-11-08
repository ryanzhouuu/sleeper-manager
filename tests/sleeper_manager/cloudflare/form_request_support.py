"""Workers-style request doubles for form routes."""

from types import SimpleNamespace
from urllib.parse import urlencode


class Reader:
    def __init__(self, body: bytes):  # type: ignore[no-untyped-def]
        self.body = body

    async def read(self):  # type: ignore[no-untyped-def]
        body, self.body = self.body, b""
        return SimpleNamespace(done=not body, value=SimpleNamespace(to_py=lambda: body))

    async def cancel(self):  # type: ignore[no-untyped-def]
        pass

    def releaseLock(self):  # type: ignore[no-untyped-def]
        pass


def request(method="GET", origin="https://worker.test", values=None):  # type: ignore[no-untyped-def]
    body = urlencode(values or {}).encode()
    return SimpleNamespace(
        method=method,
        url="https://worker.test/manage",
        headers={"origin": origin, "content-type": "application/x-www-form-urlencoded"},
        body=SimpleNamespace(getReader=lambda: Reader(body)),
    )

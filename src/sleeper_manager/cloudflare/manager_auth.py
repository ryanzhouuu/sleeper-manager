"""Owner cookies and CSRF proofs are scoped to the manager editor, never public reads."""

import hashlib
import hmac
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from secrets import token_hex

COOKIE_NAME = "manager_session"
SESSION_SECONDS = 30 * 24 * 60 * 60


def _sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def password_matches(provided: str, secret: str) -> bool:
    """Hash both values before comparison; an unconfigured key fails closed."""
    return bool(secret) and hmac.compare_digest(
        hashlib.sha256(provided.encode()).digest(), hashlib.sha256(secret.encode()).digest()
    )


def issue_session(secret: str, now: datetime) -> str:
    """Fresh nonces keep sessions and their CSRF proofs independent."""
    payload = f"{int(now.timestamp()) + SESSION_SECONDS}.{token_hex(16)}"
    return f"{payload}.{_sign(secret, 'session:' + payload)}"


def read_session(cookie: str, secret: str, now: datetime) -> str | None:
    """A password rotation invalidates every prior cookie and CSRF proof."""
    if not secret or len(cookie) > 4096:
        return None
    try:
        cookies = SimpleCookie()
        cookies.load(cookie)
        session = cookies[COOKIE_NAME].value
        expires, nonce, signature = session.split(".")
        if not 0 < int(expires) - int(now.timestamp()) <= SESSION_SECONDS:
            return None
        if len(nonce) != 32 or not hmac.compare_digest(
            signature.encode(), _sign(secret, f"session:{expires}.{nonce}").encode()
        ):
            return None
        return session
    except (KeyError, ValueError, CookieError):
        return None


def csrf_proof(secret: str, session: str) -> str:
    return _sign(secret, "csrf:" + session)


def valid_csrf(provided: str, secret: str, session: str) -> bool:
    return hmac.compare_digest(provided.encode(), csrf_proof(secret, session).encode())


def session_cookie(session: str, *, expired: bool = False) -> str:
    return (
        f"{COOKIE_NAME}={session}; Path=/manage; HttpOnly; Secure; SameSite=Strict; "
        f"Max-Age={0 if expired else SESSION_SECONDS}"
    )

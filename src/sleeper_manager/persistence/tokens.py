import hashlib
import secrets


def generate_action_token() -> str:
    return secrets.token_urlsafe(32)


def hash_action_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

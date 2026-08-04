import re
import unicodedata

_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv)\b$")
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def normalize_player_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    normalized = _NON_ALPHANUMERIC.sub(" ", ascii_name).strip()
    return _SUFFIX.sub("", normalized).strip()

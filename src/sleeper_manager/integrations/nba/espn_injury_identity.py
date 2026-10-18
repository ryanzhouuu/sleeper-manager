"""Resolve injury athletes from explicit IDs or ESPN's canonical player-profile URLs."""

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

_PROFILE_PATH = re.compile(r"/nba/player/_/id/([1-9][0-9]*)(?:/[^/]+)?/?")


def resolve_injury_player_id(athlete: Mapping[str, Any]) -> str:
    """Reject missing or conflicting identities; never substitute an injury or team ID."""
    explicit = athlete.get("id")
    if explicit is not None and (isinstance(explicit, bool) or not isinstance(explicit, str | int)):
        raise ValueError("ESPN injury athlete identity has an invalid explicit ID")
    player_id = str(explicit).strip() if explicit is not None else ""
    candidates = {player_id} if player_id else set()
    links = athlete.get("links")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, Mapping) and (linked_id := _profile_player_id(link.get("href"))):
                candidates.add(linked_id)
    if len(candidates) > 1:
        raise ValueError("ESPN injury athlete identity has conflicting player IDs")
    if not candidates:
        raise ValueError("ESPN injury athlete identity requires an ID or valid player-profile link")
    return candidates.pop()


def _profile_player_id(href: object) -> str | None:
    """Accept only canonical HTTPS ESPN NBA player paths, without following any links."""
    if not isinstance(href, str):
        return None
    try:
        url = urlsplit(href)
    except ValueError:
        return None
    if url.scheme != "https" or url.netloc.lower() not in {"espn.com", "www.espn.com"}:
        return None
    match = _PROFILE_PATH.fullmatch(url.path)
    return match.group(1) if match else None

import re
import unicodedata

_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv)\b$")
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")

_TEAM_ALIASES = {
    "atlanta": "atl",
    "atl": "atl",
    "boston": "bos",
    "bos": "bos",
    "brooklyn": "bkn",
    "bkn": "bkn",
    "charlotte": "cha",
    "cha": "cha",
    "chicago": "chi",
    "chi": "chi",
    "cleveland": "cle",
    "cle": "cle",
    "dallas": "dal",
    "dal": "dal",
    "denver": "den",
    "den": "den",
    "detroit": "det",
    "det": "det",
    "goldenstate": "gsw",
    "gs": "gsw",
    "gsw": "gsw",
    "houston": "hou",
    "hou": "hou",
    "indiana": "ind",
    "ind": "ind",
    "lac": "lac",
    "laclippers": "lac",
    "losangelesclippers": "lac",
    "lal": "lal",
    "lalakers": "lal",
    "losangeleslakers": "lal",
    "memphis": "mem",
    "mem": "mem",
    "miami": "mia",
    "mia": "mia",
    "milwaukee": "mil",
    "mil": "mil",
    "minnesota": "min",
    "min": "min",
    "neworleans": "nop",
    "no": "nop",
    "nop": "nop",
    "newyork": "nyk",
    "ny": "nyk",
    "nyk": "nyk",
    "oklahomacity": "okc",
    "okc": "okc",
    "orlando": "orl",
    "orl": "orl",
    "philadelphia": "phi",
    "phi": "phi",
    "phoenix": "phx",
    "phx": "phx",
    "portland": "por",
    "por": "por",
    "sacramento": "sac",
    "sac": "sac",
    "sanantonio": "sas",
    "sa": "sas",
    "sas": "sas",
    "toronto": "tor",
    "tor": "tor",
    "utah": "uta",
    "uta": "uta",
    "washington": "was",
    "wsh": "was",
    "was": "was",
}


def normalize_player_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    normalized = _NON_ALPHANUMERIC.sub(" ", ascii_name).strip()
    return _SUFFIX.sub("", normalized).strip()


def normalize_report_player_name(name: str) -> str:
    """Convert the official report's ``Last, First`` display form to a name key."""
    parts = [part.strip() for part in name.split(",", maxsplit=1)]
    if len(parts) == 2:
        name = f"{parts[1]} {parts[0]}"
    return normalize_player_name(name)


def normalize_team(value: str | None) -> str | None:
    if not value:
        return None
    normalized = "".join(character for character in value.casefold() if character.isalnum())
    return _TEAM_ALIASES.get(normalized, normalized)


NBA_TEAM_NAMES: dict[str, str] = {
    "atl": "Atlanta Hawks",
    "bos": "Boston Celtics",
    "bkn": "Brooklyn Nets",
    "cha": "Charlotte Hornets",
    "chi": "Chicago Bulls",
    "cle": "Cleveland Cavaliers",
    "dal": "Dallas Mavericks",
    "den": "Denver Nuggets",
    "det": "Detroit Pistons",
    "gsw": "Golden State Warriors",
    "hou": "Houston Rockets",
    "ind": "Indiana Pacers",
    "lac": "LA Clippers",
    "lal": "Los Angeles Lakers",
    "mem": "Memphis Grizzlies",
    "mia": "Miami Heat",
    "mil": "Milwaukee Bucks",
    "min": "Minnesota Timberwolves",
    "nop": "New Orleans Pelicans",
    "nyk": "New York Knicks",
    "okc": "Oklahoma City Thunder",
    "orl": "Orlando Magic",
    "phi": "Philadelphia 76ers",
    "phx": "Phoenix Suns",
    "por": "Portland Trail Blazers",
    "sac": "Sacramento Kings",
    "sas": "San Antonio Spurs",
    "tor": "Toronto Raptors",
    "uta": "Utah Jazz",
    "was": "Washington Wizards",
}

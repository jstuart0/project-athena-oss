"""The household's favourite teams: model, validation and the stored shape.

One ordered list (at most ``MAX_FAVORITE_TEAMS``), each team with an optional
league. admin-backend validates and stores it as a single ``system_settings``
JSON value; the orchestrator reads it back through ``AdminConfigClient``.
Both sides use the types here, so the rules live in one place.

Stored shape (version 1)::

    {"v": 1, "revision": <int>, "teams": [{"name": "...", "league": "nfl" | null}, ...]}

Stdlib plus pydantic only.
"""
from __future__ import annotations

import json
import logging
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger(__name__)

SETTING_KEY = "sports.favorite_teams"
SETTING_CATEGORY = "sports"
STORED_VERSION = 1
MAX_FAVORITE_TEAMS = 8
MAX_NAME_LENGTH = 64

# The league codes the sports RAG accepts (LEAGUE_TO_ESPN_PATH), in its order.
# tests/unit/test_favorite_teams_model.py keeps the two lists equal.
LEAGUE_CODES: Tuple[str, ...] = (
    "nfl",
    "college-football",
    "nba",
    "college-basketball",
    "wnba",
    "mlb",
    "nhl",
    "mls",
    "premier-league",
    "la-liga",
    "bundesliga",
    "serie-a",
    "ligue-1",
    "liga-mx",
    "champions-league",
    "europa-league",
    "international",
)

LEAGUE_LABELS: Dict[str, str] = {
    "nfl": "NFL",
    "college-football": "College football",
    "nba": "NBA",
    "college-basketball": "College basketball",
    "wnba": "WNBA",
    "mlb": "MLB",
    "nhl": "NHL",
    "mls": "MLS",
    "premier-league": "Premier League",
    "la-liga": "La Liga",
    "bundesliga": "Bundesliga",
    "serie-a": "Serie A",
    "ligue-1": "Ligue 1",
    "liga-mx": "Liga MX",
    "champions-league": "Champions League",
    "europa-league": "Europa League",
    "international": "International (national teams)",
}

LEAGUE_SPORT: Dict[str, str] = {
    "nfl": "Football",
    "college-football": "Football",
    "nba": "Basketball",
    "college-basketball": "Basketball",
    "wnba": "Basketball",
    "mlb": "Baseball",
    "nhl": "Hockey",
    "mls": "Soccer",
    "premier-league": "Soccer",
    "la-liga": "Soccer",
    "bundesliga": "Soccer",
    "serie-a": "Soccer",
    "ligue-1": "Soccer",
    "liga-mx": "Soccer",
    "champions-league": "Soccer",
    "europa-league": "Soccer",
    "international": "Soccer",
}

NAME_RULE_TEXT = "Use letters, numbers, spaces and . - ' only"


def normalize_name(value: str) -> str:
    """Strip and NFC-normalize. Validation runs on this form, so a name typed
    with combining marks ("u" + diaeresis) is judged as the composed letter."""
    return unicodedata.normalize("NFC", value.strip())


def valid_team_name(value: object) -> bool:
    """The one team-name rule: 1 to 64 characters, each a letter or digit
    (``str.isalnum``, so non-English names pass) or one of space . - '.
    Control, format, bidi and markup characters all fail it."""
    if not isinstance(value, str):
        return False
    return 0 < len(value) <= MAX_NAME_LENGTH and all(c.isalnum() or c in " .-'" for c in value)


class FavoriteTeam(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    league: Optional[str] = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: object) -> str:
        if not isinstance(v, str):
            raise ValueError("name must be text")
        name = normalize_name(v)
        if not valid_team_name(name):
            raise ValueError(NAME_RULE_TEXT)
        return name

    @field_validator("league", mode="before")
    @classmethod
    def _league(cls, v: object) -> Optional[str]:
        if v is None or v == "":
            return None
        if not isinstance(v, str) or v not in LEAGUE_CODES:
            raise ValueError("unknown league")
        return v


def _duplicate_free(teams: List[FavoriteTeam]) -> bool:
    """A team is a (name, league) pair: "Michigan" in college football and in
    college basketball are two teams; the same name and league twice is not."""
    seen = [(t.name.casefold(), t.league) for t in teams]
    return len(seen) == len(set(seen))


class FavoriteTeamsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    teams: List[FavoriteTeam] = Field(max_length=MAX_FAVORITE_TEAMS)
    revision: int = Field(ge=0)

    @model_validator(mode="after")
    def _unique(self) -> "FavoriteTeamsUpdate":
        if not _duplicate_free(self.teams):
            raise ValueError("duplicate team")
        return self


def stored_value(teams: Tuple[FavoriteTeam, ...] | List[FavoriteTeam], revision: int) -> str:
    """The text written to ``system_settings.value``."""
    return json.dumps(
        {
            "v": STORED_VERSION,
            "revision": revision,
            "teams": [{"name": t.name, "league": t.league} for t in teams],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_teams(items: Any, *, source: str = "stored") -> Tuple[FavoriteTeam, ...]:
    """Tolerant read of a list of ``{"name", "league"}`` objects: an invalid or
    duplicate item is dropped (logged by index), and the list is cut at the cap.
    ``source`` names the origin in the log line."""
    if not isinstance(items, list):
        return ()
    teams: List[FavoriteTeam] = []
    for index, item in enumerate(items):
        try:
            team = FavoriteTeam.model_validate(item)
        except ValidationError:
            logger.error("favorite_team_invalid: source=%s index=%d dropped", source, index)
            continue
        if not _duplicate_free([*teams, team]):
            logger.error("favorite_team_duplicate: source=%s index=%d dropped", source, index)
            continue
        if len(teams) >= MAX_FAVORITE_TEAMS:
            logger.error("favorite_team_over_cap: source=%s index=%d dropped", source, index)
            continue
        teams.append(team)
    return tuple(teams)


def parse_stored(raw: Optional[str]) -> Tuple[Tuple[FavoriteTeam, ...], int]:
    """(teams, revision) from the stored text. Missing gives ``((), 0)``; an
    unreadable blob gives ``((), 0)`` with an ERROR carrying its length only."""
    if raw is None:
        return (), 0
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.error("favorite_teams_unreadable: length=%d", len(raw) if isinstance(raw, str) else 0)
        return (), 0
    if not isinstance(data, dict):
        logger.error("favorite_teams_unreadable: length=%d", len(raw))
        return (), 0
    revision = data.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        revision = 0
    return parse_teams(data.get("teams"), source="stored"), revision


def league_catalog() -> List[Dict[str, str]]:
    """The league list the admin UI offers: code, label and sport (for grouping)."""
    return [{"code": c, "label": LEAGUE_LABELS[c], "sport": LEAGUE_SPORT[c]} for c in LEAGUE_CODES]

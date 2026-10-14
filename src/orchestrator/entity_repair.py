"""Repair of misheard or mistyped entity words, for routing only.

``repair_for_routing`` fixes a word that is a near miss of a name from a known
vocabulary (team names, sports nouns, room words, device words) so the intent
classifier can see what was meant ("whats the scor of the ravens game").

The output is for classification and nothing else. It feeds the pattern
classifier, the classifier prompt and the sports team entity. It is never
stored as the query, never reaches the controller or the write-cue check, and
this module is not imported by ``smart_home_controller``, ``access_target`` or
``route_control`` (``tests/unit/test_entity_repair.py`` pins that). A repaired
word therefore can't create a write cue, and the controller's own typo table
stays a separate, exact table.

A turn the fast path answered never gets here: ``classify_node`` runs after it.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, Tuple

from orchestrator.access_target import TEAM_WORDS
from orchestrator.room_context import COMMON_ROOM_WORDS

CUTOFF = 0.85
MAX_QUERY_CHARS = 200
MAX_TOKENS = 20
MIN_TOKEN_CHARS = 4

SPORTS_NOUNS: FrozenSet[str] = frozenset({
    "score", "scores", "game", "games", "standings", "playoffs", "playoff", "schedule",
    "football", "baseball", "basketball", "hockey", "soccer", "championship",
})
DEVICE_WORDS: FrozenSet[str] = frozenset({
    "lights", "light", "lamp", "lamps", "thermostat", "garage", "blinds", "shades", "television", "temperature",
})
ROOM_WORDS: FrozenSet[str] = frozenset(
    w for phrase in COMMON_ROOM_WORDS for w in phrase.split() if len(w) >= MIN_TOKEN_CHARS and w.isalpha()
)

KIND_TEAM = "team"
KIND_SPORTS = "sports"
KIND_ROOM = "room"
KIND_DEVICE = "device"

_VOCABULARIES: Tuple[Tuple[str, FrozenSet[str]], ...] = (
    (KIND_TEAM, TEAM_WORDS),
    (KIND_SPORTS, SPORTS_NOUNS),
    (KIND_ROOM, ROOM_WORDS),
    (KIND_DEVICE, DEVICE_WORDS),
)
_KNOWN: FrozenSet[str] = frozenset().union(*(words for _, words in _VOCABULARIES))
_CHOICES: Dict[str, Tuple[str, ...]] = {kind: tuple(sorted(words)) for kind, words in _VOCABULARIES}
_WORD_RE = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True)
class Repair:
    text: str
    changes: Tuple[Tuple[str, str, str], ...] = ()  # (heard, meant, kind)

    @property
    def team(self) -> str:
        """The team word a repair produced, if any."""
        return next((meant for _, meant, kind in self.changes if kind == KIND_TEAM), "")


def _is_inflection_of_known(word: str) -> bool:
    """"brown" beside the team word "browns": a singular of a known plural is a word, not a near miss."""
    return word + "s" in _KNOWN


def _closest(word: str) -> Tuple[str, str]:
    """(kind, vocabulary word) for the best near miss of ``word``, or ("", "")."""
    for kind, _ in _VOCABULARIES:
        match = difflib.get_close_matches(word, _CHOICES[kind], n=1, cutoff=CUTOFF)
        if match:
            return kind, match[0]
    return "", ""


def repair_for_routing(query: str) -> Repair:
    """The query with near-miss entity words corrected. Unchanged when the
    query is over the caps or nothing is a near miss.

    A sports noun is only repaired next to a team name or another sports noun,
    so "core values" stays as spoken."""
    if not query or len(query) > MAX_QUERY_CHARS:
        return Repair(query or "")
    words = _WORD_RE.findall(query)
    if len(query.split()) > MAX_TOKENS:
        return Repair(query)
    lowered = {w.lower() for w in words}
    sports_context = bool(lowered & (TEAM_WORDS | SPORTS_NOUNS))
    changes = []

    def fix(match: "re.Match[str]") -> str:
        word = match.group(0)
        low = word.lower()
        if len(low) < MIN_TOKEN_CHARS or low in _KNOWN or _is_inflection_of_known(low):
            return word
        kind, meant = _closest(low)
        if not kind or (kind == KIND_SPORTS and not sports_context):
            return word
        changes.append((low, meant, kind))
        return meant

    repaired = _WORD_RE.sub(fix, query)
    return Repair(repaired, tuple(changes)) if changes else Repair(query)

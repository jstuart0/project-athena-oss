"""Repair of misheard or mistyped sports words, for routing only.

``repair_for_routing`` fixes a word that is a near miss of a team name or a
sports noun, and only in an utterance that already talks about sports, so the
intent classifier can see what was meant ("how did the falons do"). Rooms and
devices are never repaired: their near misses are ordinary words ("flight" and
"light", "night" and "knight", "officer" and "office").

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
from typing import FrozenSet, Tuple

from orchestrator.access_target import TEAM_WORDS

CUTOFF = 0.85
MAX_QUERY_CHARS = 200
MAX_TOKENS = 20
MIN_TOKEN_CHARS = 4

SPORTS_NOUNS: FrozenSet[str] = frozenset({
    "score", "scores", "game", "games", "standings", "playoffs", "playoff", "schedule",
    "football", "baseball", "basketball", "hockey", "soccer", "championship",
})
# Words that say the utterance is about a contest. A team name is also context.
SPORTS_CONTEXT_WORDS: FrozenSet[str] = SPORTS_NOUNS | frozenset({
    "won", "win", "wins", "lost", "lose", "beat", "play", "plays", "played", "playing", "vs", "versus",
    "match", "record", "team", "teams",
})
_FRAME_RE = re.compile(r"\bhow (?:did|are|do|have) (?:the )?[a-z]+ (?:do|doing|done|play|playing|look|looking)\b")
# Ordinary words that sit one edit from a team name or a sports noun. Never repaired.
COMMON_WORDS: FrozenSet[str] = frozenset({
    "flight", "flights", "night", "nights", "place", "places", "lighting", "texas", "charges", "charge",
    "supper", "officer", "office", "light", "lights", "bear", "bears", "store", "stores",
    "scare", "scared", "came", "gamer", "plays", "state", "states", "giant", "heat", "kings", "king",
})

KIND_TEAM = "team"
KIND_SPORTS = "sports"

_TEAM_CHOICES: Tuple[str, ...] = tuple(sorted(TEAM_WORDS))
_SPORTS_CHOICES: Tuple[str, ...] = tuple(sorted(SPORTS_NOUNS))
_KNOWN: FrozenSet[str] = TEAM_WORDS | SPORTS_NOUNS
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
    for kind, choices in ((KIND_TEAM, _TEAM_CHOICES), (KIND_SPORTS, _SPORTS_CHOICES)):
        match = difflib.get_close_matches(word, choices, n=1, cutoff=CUTOFF)
        if match:
            return kind, match[0]
    return "", ""


def _sports_context(lowered_words: set, lowered_query: str) -> bool:
    return bool(lowered_words & (TEAM_WORDS | SPORTS_CONTEXT_WORDS)) or bool(_FRAME_RE.search(lowered_query))


def repair_for_routing(query: str) -> Repair:
    """The query with near-miss team and sports words corrected. Unchanged when
    the query is over the caps, has no sports context, or nothing is a near miss."""
    if not query or len(query) > MAX_QUERY_CHARS:
        return Repair(query or "")
    if len(query.split()) > MAX_TOKENS:
        return Repair(query)
    lowered = query.lower()
    if not _sports_context({w.lower() for w in _WORD_RE.findall(query)}, lowered):
        return Repair(query)
    changes = []

    def fix(match: "re.Match[str]") -> str:
        word = match.group(0)
        low = word.lower()
        if len(low) < MIN_TOKEN_CHARS or low in _KNOWN or low in COMMON_WORDS or _is_inflection_of_known(low):
            return word
        kind, meant = _closest(low)
        if not kind:
            return word
        changes.append((low, meant, kind))
        return meant

    repaired = _WORD_RE.sub(fix, query)
    return Repair(repaired, tuple(changes)) if changes else Repair(query)

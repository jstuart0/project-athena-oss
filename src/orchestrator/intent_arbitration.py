"""Arbitration between the LLM classifier's intent and the pattern classifier's.

``arbitrate_intent`` is pure: it takes both answers and returns the one the
turn keeps, with the name of the rule that decided. ``classify_node`` applies
the result and logs it (``intent_arbitration``).

The rules, in order (the first that applies decides):

1. Indoor-temperature questions are control, not weather.
2. LLM ``unknown``: take the pattern answer when it is specific.
3. The explicit overrides (dining over directions, a web-search request,
   now-playing, colour and scene words, problem and instructional queries,
   strong general knowledge, implicit brightness and temperature, colour
   requests).
4. An LLM ``general_info`` against a trusted, specific pattern answer
   (>= ``SPECIFIC_PATTERN``) takes the pattern answer, whatever the LLM's own
   confidence. This is the rule the old 0.75 ceiling left a dead zone in.
5. When the two agree and the LLM is unsure (< 0.7), the pattern answer's
   confidence is borrowed. A boost never changes the intent and never applies
   when they disagree.
6. When they disagree and the LLM is unsure (<= 0.75), a specific pattern
   answer wins.

Rule 4 trusts only pattern answers whose evidence is a whole word of that
domain (``_PATTERN_EVIDENCE``). The pattern classifier matches substrings such
as "game" and "rain", so "game of thrones" or "what is a train" is not
sports or weather on that evidence alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

import structlog

from orchestrator.access_target import TEAM_WORDS
from orchestrator.state import IntentCategory
from orchestrator.utterance_kind import is_indoor_temperature_question
from shared.fast_path_vocab import SCENE_OVERRIDE_PHRASES

logger = structlog.get_logger(__name__)

SPECIFIC_PATTERN = 0.8
LLM_UNSURE = 0.7
LLM_MODERATE = 0.75
OVERRIDE_CONFIDENCE = 0.9
WEBSEARCH_CONFIDENCE = 0.95

_UNSPECIFIC = (IntentCategory.GENERAL_INFO, IntentCategory.UNKNOWN)

_SPORTS_EVIDENCE = re.compile(
    r"\b(?:scores?|standings|playoffs?|championship|nfl|nba|mlb|nhl|mls|ncaa|football|soccer|basketball|baseball|hockey)\b"
)
_WEATHER_EVIDENCE = re.compile(
    r"\b(?:weather|forecast|rain|raining|snow|snowing|sleet|hail|storms?|"
    r"(?:cold|hot|warm|temperature|temp)\s+(?:is it\s+)?outside|outside\s+temp(?:erature)?|outdoors?\s+temp(?:erature)?)\b"
)
_WEATHER_NAMED = re.compile(r"\b(?:weather|forecast)\b")  # naming the service outranks a media word
# Music, film and show titles share these words ("the rain album").
_MEDIA_WORDS = re.compile(r"\b(?:album|song|songs|track|band|lyrics|movie|film|series|podcast|playlist)\b")
_PATTERN_EVIDENCE: Mapping[IntentCategory, "re.Pattern[str]"] = {
    IntentCategory.SPORTS: _SPORTS_EVIDENCE,
    IntentCategory.WEATHER: _WEATHER_EVIDENCE,
}

_IMPLICIT_BRIGHTNESS = (
    "take it easy on my eyes", "easy on my eyes", "easy on the eyes",
    "darken it up", "tone down", "tone it down",
    "too bright", "too dark", "make it cozy",
    "can't see", "cant see", "hard to see",
    "light me up", "lights please",
)
_INDOOR_TEMP_COMPLAINTS = (
    "mad cold", "mad hot", "hella cold", "hella hot",
    "its cold", "it's cold", "its hot", "it's hot",
    "so cold", "so hot", "chilly", "freezing", "too warm",
    "drop the temp", "drop that temp", "raise the temp", "raise that temp",
    "warmer inside", "colder inside", "hotter inside", "cooler inside",
    "inside than outside", "how much warmer is it inside",
    "indoor vs outdoor", "temp difference",
)
_COLOR_REQUEST_PHRASES = (
    "random colors", "gimme random", "give me random",
    "hit me with some", "throw some", "gimme some",
)
_COLOR_REQUEST_WORDS = ("blue", "red", "green", "purple", "color", "colors", "random")
_NOW_PLAYING = (
    "whats playing", "what's playing", "whats playin", "what's playin",
    "playin rn", "playing rn", "what song", "song is this",
    "who sings", "whos singing", "who's singing", "artist is this",
)
_COLOR_WORDS = (
    "blue", "red", "green", "yellow", "orange", "purple", "pink",
    "cyan", "magenta", "white", "warm", "cool", "sunset", "rainbow",
    "random color", "random colors", "christmas", "christmas colors",
)
_SCENE_PLANNING_EXCLUSIONS = (
    "help me", "plan a", "plan my", "planning", "ideas for",
    "something ", "but also", "is that", "is it possible",
    "what should", "where should", "recommend", "any ",
)
_INDOOR_TEMP_INDICATORS = (
    "temperature inside", "temperature in the house", "temperature in here",
    "temp inside", "inside temp", "indoor temp", "indoors",
    "how cold is it in", "how hot is it in", "how warm is it in",
    "temp we at", "what temp we at", "temperature we at",
    "temp in here", "what is the temp in here",
)
_PROBLEM_PATTERNS = (
    "won't turn on", "wont turn on", "won't turn off", "wont turn off",
    "not working", "isn't working", "isnt working", "doesn't work", "doesnt work",
    "stopped working", "quit working", "broken", "not responding",
    "won't respond", "wont respond", "is broken", "seems broken",
    "having trouble with", "trouble with the", "problem with the",
    "issue with the", "can't get", "cant get", "won't work", "wont work",
    "not turning on", "not turning off",
    "not getting hot", "not getting cold", "not heating", "not cooling",
    "black screen", "blank screen", "isnt showing", "isn't showing",
    "not showing", "nothing on", "no picture", "no sound", "no audio",
    "keeps turning off", "keeps shutting", "keeps restarting",
    "stuck on", "frozen", "unresponsive", "no response",
)
_INSTRUCTIONAL_PATTERNS = (
    "how do i use", "how do you use", "how to use", "how does the",
    "how does this", "how do i work", "how to work the", "how to operate",
    "how do i operate", "what's the best way to", "whats the best way to",
    "can you explain how", "show me how", "instructions for",
)
_GENERAL_STRONG = (
    "what time", "time is it", "current time", "what date", "today's date",
    "how do", "how does", "how can", "how to", "why do", "why does",
    "tell me about", "what is", "who is", "explain", "describe",
    "definition of", "meaning of",
)
_SERVICE_INTENTS = (
    IntentCategory.WEATHER, IntentCategory.SPORTS, IntentCategory.AIRPORTS,
    IntentCategory.FLIGHTS, IntentCategory.EVENTS, IntentCategory.STREAMING,
    IntentCategory.NEWS, IntentCategory.STOCKS, IntentCategory.RECIPES,
    IntentCategory.DINING,
)
_FINAL_INDOOR_TEMP = (
    "temp we at", "what temp we at", "temperature we at",
    "temp in here", "what is the temp in here", "whats the temp in here",
    "indoor temp", "inside temp", "thermostat",
    "warmer inside", "colder inside", "hotter inside", "cooler inside",
    "inside than outside", "how much warmer is it inside",
    "indoor vs outdoor", "temp difference",
)
_INDOOR_ENTITIES: Mapping[str, str] = MappingProxyType({"device_type": "climate", "action": "get_status"})
_NO_ENTITIES: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class Arbitration:
    intent: IntentCategory
    confidence: float
    rule: str
    entities_update: Mapping[str, str]


def _any(query: str, phrases) -> bool:
    return any(p in query for p in phrases)


def _has_evidence(intent: IntentCategory, query: str) -> bool:
    pattern = _PATTERN_EVIDENCE.get(intent)
    if pattern is None or (
        intent == IntentCategory.WEATHER and _MEDIA_WORDS.search(query) and not _WEATHER_NAMED.search(query)
    ):
        return False
    if pattern.search(query):
        return True
    return intent == IntentCategory.SPORTS and bool(set(re.findall(r"[a-z]+", query)) & TEAM_WORDS)


def _chain(
    query: str,
    llm: IntentCategory,
    llm_conf: float,
    pat: IntentCategory,
    pat_conf: float,
) -> Optional[Arbitration]:
    """The explicit rules, in the order the old elif chain applied them. None
    means no rule decided and the LLM answer stands. A rule that names its
    two intents but whose phrase check fails still ends the chain, as before."""

    def take(intent: IntentCategory, conf: float, rule: str, update: Mapping[str, str] = _NO_ENTITIES) -> Arbitration:
        return Arbitration(intent, conf, rule, update)

    if llm == IntentCategory.WEATHER and is_indoor_temperature_question(query):
        return take(IntentCategory.CONTROL, OVERRIDE_CONFIDENCE, "indoor_temperature_question", _INDOOR_ENTITIES)
    if llm == IntentCategory.UNKNOWN:
        if pat != IntentCategory.GENERAL_INFO or pat_conf >= SPECIFIC_PATTERN:
            return take(pat, pat_conf, "unknown_takes_pattern")
        return None
    if llm == IntentCategory.DIRECTIONS and pat == IntentCategory.DINING:
        return take(IntentCategory.DINING, OVERRIDE_CONFIDENCE, "dining_over_directions")
    if pat == IntentCategory.WEBSEARCH:
        return take(IntentCategory.WEBSEARCH, WEBSEARCH_CONFIDENCE, "explicit_websearch")
    if llm == IntentCategory.EVENTS and pat == IntentCategory.MUSIC_CONTROL:
        if _any(query, _NOW_PLAYING):
            return take(IntentCategory.MUSIC_CONTROL, OVERRIDE_CONFIDENCE, "now_playing")
        return None
    if llm == IntentCategory.MUSIC_PLAY and pat == IntentCategory.CONTROL:
        if _any(query, _COLOR_WORDS):
            return take(IntentCategory.CONTROL, OVERRIDE_CONFIDENCE, "color_not_music")
        if _any(query, SCENE_OVERRIDE_PHRASES) and not _any(query, _SCENE_PLANNING_EXCLUSIONS):
            return take(IntentCategory.CONTROL, OVERRIDE_CONFIDENCE, "scene_not_music")
        return None
    if llm == IntentCategory.WEATHER and pat == IntentCategory.CONTROL:
        if _any(query, _INDOOR_TEMP_INDICATORS):
            return take(IntentCategory.CONTROL, OVERRIDE_CONFIDENCE, "indoor_temperature_not_weather")
        return None
    if llm == IntentCategory.CONTROL and pat == IntentCategory.GENERAL_INFO and pat_conf >= SPECIFIC_PATTERN:
        if _any(query, _PROBLEM_PATTERNS):
            return take(pat, pat_conf, "problem_not_command")
        if _any(query, _INSTRUCTIONAL_PATTERNS):
            return take(pat, pat_conf, "instructional_not_command")
        return None
    if pat == IntentCategory.GENERAL_INFO and pat_conf >= SPECIFIC_PATTERN and llm in _SERVICE_INTENTS:
        if _any(query, _GENERAL_STRONG):
            return take(pat, pat_conf, "strong_general_info")
        return None
    if llm == IntentCategory.GENERAL_INFO and pat == IntentCategory.CONTROL and pat_conf >= SPECIFIC_PATTERN:
        if _any(query, _IMPLICIT_BRIGHTNESS):
            return take(pat, pat_conf, "implicit_brightness")
        if _any(query, _INDOOR_TEMP_COMPLAINTS):
            return take(pat, pat_conf, "indoor_temperature_complaint")
        if _any(query, _COLOR_REQUEST_PHRASES) and _any(query, _COLOR_REQUEST_WORDS):
            return take(pat, pat_conf, "color_request")
        return None
    if (
        llm == IntentCategory.GENERAL_INFO
        and pat not in (*_UNSPECIFIC, IntentCategory.CONTROL)
        and pat_conf >= SPECIFIC_PATTERN
        and _has_evidence(pat, query)
    ):
        return take(pat, pat_conf, "pattern_over_general_info")
    if llm == pat:
        if llm_conf < LLM_UNSURE and pat_conf >= SPECIFIC_PATTERN:
            return take(llm, pat_conf, "agreement_boost")
        return None
    if llm_conf <= LLM_MODERATE and pat not in _UNSPECIFIC:
        if llm == IntentCategory.GENERAL_INFO and pat in _PATTERN_EVIDENCE and not _has_evidence(pat, query):
            return None  # "game of thrones": a substring hit alone is not sports
        return take(pat, pat_conf, "pattern_over_unsure_llm")
    return None


def arbitrate_intent(
    *,
    query: str,
    llm_intent: IntentCategory,
    llm_confidence: float,
    pattern_intent: IntentCategory,
    pattern_confidence: float,
) -> Arbitration:
    """The intent and confidence the turn keeps. Pure; logs ``intent_arbitration``
    (a query length, never the query)."""
    lower = query.lower()
    decided = _chain(lower, llm_intent, llm_confidence, pattern_intent, pattern_confidence)
    outcome = decided or Arbitration(llm_intent, llm_confidence, "llm_stands", _NO_ENTITIES)
    if outcome.intent == IntentCategory.WEATHER and _any(lower, _FINAL_INDOOR_TEMP):
        outcome = Arbitration(IntentCategory.CONTROL, OVERRIDE_CONFIDENCE, "final_indoor_temperature", _NO_ENTITIES)
    logger.info(
        "intent_arbitration",
        rule=outcome.rule,
        llm_intent=llm_intent.value,
        llm_confidence=round(llm_confidence, 2),
        pattern_intent=pattern_intent.value,
        pattern_confidence=round(pattern_confidence, 2),
        intent=outcome.intent.value,
        confidence=round(outcome.confidence, 2),
        query_len=len(query),
    )
    return outcome

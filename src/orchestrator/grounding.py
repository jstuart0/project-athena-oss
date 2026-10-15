"""The grounding contract: which intents a tool owns, whether a turn holds tool
data, and when an answer claims specifics the turn never fetched.

Pure and in-process: no I/O, no config except the sports default team (read at
call time), and nothing imported from ``orchestrator.main``. The callers are
``tool_call_node`` (forced calls, the decline), ``validate_node`` (support for
specific facts), the answer routes (the local-business cue) and the semantic
cache (what may be stored).

``grounded`` answers one question: did this turn get at least one successful
tool result (or, on the retrieve path, a non-empty result)? An error object is
never data.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional

from orchestrator.model_safe_errors import is_error_object
from orchestrator.state import IntentCategory
from shared.favorite_teams import FavoriteTeam

DECLINE_TEXT = "I couldn't look that up right now."

# Intents whose answer a tool owns: without tool data the model has nothing
# true to say about them. Stocks and directions are owned but never forced here
# (stocks needs a symbol; directions has its own forced path in tool_call_node).
TOOL_OWNED: FrozenSet[IntentCategory] = frozenset({
    IntentCategory.DINING,
    IntentCategory.SPORTS,
    IntentCategory.WEATHER,
    IntentCategory.NEWS,
    IntentCategory.EVENTS,
    IntentCategory.RECIPES,
    IntentCategory.STREAMING,
    IntentCategory.AIRPORTS,
    IntentCategory.FLIGHTS,
    IntentCategory.STOCKS,
    IntentCategory.DIRECTIONS,
})

# The tool a forced call uses, per intent. Stocks aren't forced (a symbol is
# needed) and directions have their own forced path in tool_call_node.
FORCED_TOOL: Mapping[IntentCategory, str] = {
    IntentCategory.DINING: "search_restaurants",
    IntentCategory.SPORTS: "get_sports_scores",
    IntentCategory.WEATHER: "get_weather",
    IntentCategory.NEWS: "get_news",
    IntentCategory.EVENTS: "search_events",
    IntentCategory.RECIPES: "search_recipes",
    IntentCategory.STREAMING: "search_streaming",
    IntentCategory.AIRPORTS: "get_airport_info",
    IntentCategory.FLIGHTS: "search_flights",
}
LOCAL_BUSINESS_TOOL = "search_restaurants"

EVIDENCE_LIMIT = 4000


def _field(state: Any, name: str, default: Any = None) -> Any:
    if isinstance(state, Mapping):
        return state.get(name, default)
    return getattr(state, name, default)


def request_text(state: Any) -> str:
    """The text a model-facing reader works from: the resolved request of a
    follow-up when the turn has one, else the user's own words."""
    return _field(state, "resolved_request") or _field(state, "query") or ""


def _is_data(result: Any) -> bool:
    if result is None or result == {} or result == [] or result == "":
        return False
    return not is_error_object(result)


def grounded(state: Any) -> bool:
    """At least one successful tool result, or a non-empty retrieve-path result."""
    data = _field(state, "retrieved_data")
    if not isinstance(data, dict) or not data:
        return False
    if "tool_results" in data:
        results = data.get("tool_results")
        return isinstance(results, dict) and any(_is_data(r) for r in results.values())
    if "tool_calls" in data and len(data) == 1:
        return False
    if data.get("type") == "conversation_context" or data.get("limitation"):
        return False
    return _is_data(data)


def prior_tool_context(state: Any) -> bool:
    """The previous exchange holds this intent's tool results: a follow-up on it
    isn't forced into a fresh call, and what it says is supported by that turn."""
    intent = _field(state, "intent")
    previous = _field(state, "prev_context")
    if not intent or not isinstance(previous, dict):
        return False
    if _field(state, "resolved_request"):
        return False  # a resumed task is a new search ("yes" to "want outdoor seating?"), not a question about the last one
    if previous.get("intent") != getattr(intent, "value", intent):
        return False
    parameters = previous.get("parameters") or {}
    # Only a turn that held data counts: a failed or declined search saved
    # ``grounded`` False, and a context saved before the marker existed isn't trusted.
    return parameters.get("grounded") is True and not parameters.get("direct_answer")


def evidence_summary(state: Any, limit: int = EVIDENCE_LIMIT) -> str:
    """The tool names plus truncated successful results, bounded at ``limit``
    characters: what the fact-check model is shown instead of the whole payload."""
    data = _field(state, "retrieved_data")
    if not isinstance(data, dict) or not data:
        return ""
    if "tool_results" in data:
        names = [n for n in (data.get("tool_calls") or []) if isinstance(n, str)]
        results = [r for r in (data.get("tool_results") or {}).values() if _is_data(r)]
        if not results:
            return ""
        header = f"Tools used: {', '.join(names)}\n" if names else ""
        share = max(200, (limit - len(header)) // max(1, len(results)))
        body = "\n".join(json.dumps(r, default=str, ensure_ascii=False)[:share] for r in results)
        return (header + body)[:limit]
    return json.dumps(data, default=str, ensure_ascii=False)[:limit]


# --- the local-business cue ---------------------------------------------------

_PROXIMITY = re.compile(r"\b(?:near me|nearby|around here|close by|in my area|around me|closest|nearest|in town|in the area)\b")
_VENUE = re.compile(
    r"\b(?:restaurants?|cafes?|coffee shops?|diners?|bakery|bakeries|pizzerias?|pizza places?|"
    r"breweries|brewery|steakhouses?|sushi|brunch|takeout|take-out|bistro|pubs?|taverns?|"
    r"places? to (?:eat|drink|grab)|somewhere to eat|something to eat|(?:sports|wine|cocktail|dive) bars?|"
    r"happy hour)\b"
)
_PLACE_WORD = re.compile(r"\b(?:place|spot|food|eat|drink|shop|store|bar)s?\b")
# A request to find, pick or book something: without one a venue word is a topic.
_FIND_INTENT = re.compile(
    r"\b(?:recommend|suggest|find|looking for|look for|any|is there|are there|where (?:can|should|to|do)|"
    r"book|reserve|reservations?|open (?:now|late|today|tonight|tomorrow)|best|good|great|top|"
    r"take me|get me|hungry)\b"
)
# Knowledge about a venue word: recipes, origins, meanings, history.
_TOPIC_QUESTION = re.compile(
    r"\b(?:recipes?|originat\w*|origins?|history|invented|ingredients?|calories|nutrition|etymology|meaning|"
    r"how (?:do|to|does|did) (?:i |you |we )?(?:make|cook|bake|prepare)|why)\b"
    r"|^\s*(?:what|who|when)(?:'s| is| are| was| were)\s+(?:a |an |the )?(?:happy hour|brunch|sushi|bistro|diner|pub)\b"
)


def local_business_cue(query: Optional[str]) -> bool:
    """The request asks to find, pick or book a local business or place: a venue
    word with a find-or-recommend intent or a nearness phrase, or a nearness
    phrase with a place word. A knowledge question that mentions a venue word
    ("where did sushi originate", "a good brunch recipe", "what is happy hour")
    is not one."""
    text = (query or "").lower()
    if not text or _TOPIC_QUESTION.search(text):
        return False
    proximity = bool(_PROXIMITY.search(text))
    if _VENUE.search(text) and (proximity or _FIND_INTENT.search(text)):
        return True
    return proximity and bool(_PLACE_WORD.search(text))


_KNOWLEDGE_QUESTION = re.compile(
    r"^\s*(?:what(?:'s| is| are| does| do)\s+(?:a|an)\s+\w+|what (?:does|do)\b.+\bmean|define\b|explain\b|"
    r"how (?:does|do|is|are)\b.+\b(?:work|works|calculated|determined|made|different)|"
    r"why (?:is|are|do|does|did)\b|what(?:'s| is) the (?:difference|meaning)\b|"
    r"(?:tell me )?(?:the )?difference between\b|what (?:is|are) .+ (?:for|used for)\b)",
    re.IGNORECASE,
)


# A question about now, today or a price is about the world's current state, not a concept.
_CURRENT_WORDS = re.compile(
    r"\b(?:today|now|tonight|tomorrow|yesterday|last night|this (?:week|weekend|morning|afternoon|evening|month|season|year)|"
    r"latest|current(?:ly)?|right now|recent(?:ly)?|score|scores|price of)\b",
    re.IGNORECASE,
)


def knowledge_question(query: Optional[str]) -> bool:
    """A definition, how-it-works or why question: its answer may carry worked
    examples (a company and a price) that no tool could supply. A request to find
    a business is never one."""
    text = (query or "").strip()
    return (
        bool(text)
        and bool(_KNOWLEDGE_QUESTION.search(text))
        and not _CURRENT_WORDS.search(text)
        and not local_business_cue(text)
    )


# --- location -------------------------------------------------------------------

_RELATIVE_PHRASES = re.compile(
    r"\b(?:nearby|near me|near|here|around|close|closest|outdoor|outdoors|indoor|indoors|"
    r"seating|patio|rooftop|happy hour)\b"
)


def plausible_location(text: Any, query: Optional[str]) -> bool:
    """True when ``text`` could be a place name from the user's own words: it
    appears in the query and isn't a relative or amenity phrase ("nearby",
    "outdoor seating") or "downtown" alone."""
    if not isinstance(text, str) or not text.strip():
        return False
    candidate = text.strip().lower()
    if candidate not in (query or "").lower():
        return False
    if _RELATIVE_PHRASES.search(candidate):
        return False
    return candidate not in {"downtown", "the downtown", "town", "the area"}


def _override_text(override: Any) -> Optional[str]:
    """The place a ``location_override`` names: coordinates (device GPS) or an address."""
    if not isinstance(override, dict):
        return None
    if override.get("use_device_location"):
        if override.get("latitude") and override.get("longitude"):
            return f"{override['latitude']:.4f}, {override['longitude']:.4f}"
        return None
    if override.get("address"):
        return override["address"]
    if override.get("latitude") and override.get("longitude"):
        return f"{override['latitude']:.4f}, {override['longitude']:.4f}"
    return None


def resolve_search_location(state: Any, default: str) -> "tuple[str, str]":
    """Where a search is near, and which source said so (a fixed word for logs).

    A ``location_override`` (the device's position, or a place the user gave as a
    correction) wins. Otherwise the entities' location is used when the user's
    own words name a place (``plausible_location``) or when it is the request's
    own browser location (``request_location``). Anything else the classifier read
    as a place ("outdoor seating", a name that isn't in the words) is dropped.
    Otherwise ``default`` (the home address)."""
    override = _override_text((_field(state, "context") or {}).get("location_override"))
    if override:
        return override, "location_override"
    entity = (_field(state, "entities") or {}).get("location")
    if isinstance(entity, str) and entity.strip():
        request_location = _field(state, "request_location")
        if isinstance(request_location, str) and entity.strip() == request_location.strip():
            return entity, "request_entities"
        if plausible_location(entity, request_text(state)):
            return entity, "classifier_entity"
    return default, "default"


# --- forced calls ---------------------------------------------------------------

_LEAD_FILLER = re.compile(
    r"^(?:(?:hey|ok|okay|so|um|well)\s+)*"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
    r"(?:please\s+)?"
    r"(?:(?:i\s+(?:want|need|would like|'d like)\s+(?:to\s+(?:find|get|see|know)\s+)?)|"
    r"(?:(?:recommend|suggest|find|show|get|give|tell|look up|search for|look for)\s+(?:me\s+)?))?"
    r"(?:(?:a|an|some|the|any)\s+)?(?:(?:good|great|nice|best|decent)\s+)?"
)
_TRAIL_FILLER = re.compile(r"\s+(?:near me|nearby|around here|close by|in my area|please|right now)$")


def search_term(text: str) -> str:
    """The request without its polite lead-in or trailing nearness phrase; the
    whole text when nothing is left."""
    cleaned = re.sub(r"[^\w\s'-]", " ", (text or "").lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    stripped = _LEAD_FILLER.sub("", cleaned, count=1).strip()
    while True:
        shorter = _TRAIL_FILLER.sub("", stripped).strip()
        if shorter == stripped:
            break
        stripped = shorter
    return stripped or cleaned


def _tool_call(index: int, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": f"forced_{index}_{name}", "type": "function", "function": {"name": name, "arguments": arguments}}


def _sports_team(state: Any) -> Optional[str]:
    """The team the request names; None when it names none."""
    entities = _field(state, "entities") or {}
    team = entities.get("team")
    if isinstance(team, str) and team.strip():
        return team.strip()
    return None


# The leagues get_sports_scores accepts (rag_tools.py); a favourite in another
# league is passed by name only.
_SPORTS_TOOL_LEAGUES = frozenset({
    "nfl", "college-football", "nba", "college-basketball", "wnba", "mlb", "nhl", "mls",
    "premier-league", "la-liga", "bundesliga", "serie-a", "champions-league", "international",
})


def _sports_arguments(state: Any, default_team: Any) -> Optional[Dict[str, Any]]:
    named = _sports_team(state)
    if named:
        return {"team": named}
    if default_team is None:
        return None
    arguments: Dict[str, Any] = {"team": default_team.name}
    if default_team.league in _SPORTS_TOOL_LEAGUES:
        arguments["league"] = default_team.league
    return arguments


def _forced_arguments(
    tool: str, state: Any, text: str, search_location: str, default_team: Any = None
) -> Optional[Dict[str, Any]]:
    term = search_term(text)
    if tool == "search_restaurants":
        return {"location": search_location, "term": term} if search_location else None
    if tool == "get_weather":
        return {"location": search_location} if search_location else None
    if tool == "get_sports_scores":
        return _sports_arguments(state, default_team)
    if tool == "get_news":
        return {"query": term}
    if tool == "search_events":
        return {"keyword": term}
    if tool in ("search_recipes", "search_streaming", "get_airport_info", "search_flights"):
        return {"query": term}
    return None


def forced_tool_calls(
    state: Any, *, offered: Iterable[str], search_location: str, default_team: Optional[FavoriteTeam] = None,
) -> List[Dict[str, Any]]:
    """The tool call a data turn makes when the model made none.

    Built only when the mapped tool is in ``offered`` (the tool names left after
    the public, guest and off-stay narrowing and the per-intent filter), so a
    caller can never be sent to a tool it isn't offered. A follow-up on this
    intent's earlier tool results is not forced."""
    intent = _field(state, "intent")
    text = request_text(state)
    tool = FORCED_TOOL.get(intent)
    if tool is None and intent in (IntentCategory.GENERAL_INFO, IntentCategory.UNKNOWN) and local_business_cue(text):
        tool = LOCAL_BUSINESS_TOOL
    if tool is None or tool not in set(offered) or prior_tool_context(state):
        return []
    if knowledge_question(text):  # a definition is answered, not looked up
        return []
    arguments = _forced_arguments(tool, state, text, search_location, default_team)
    return [_tool_call(0, tool, arguments)] if arguments else []


# --- ungrounded answers -----------------------------------------------------------

_EXPERIENCE_CLAIM = re.compile(
    r"\bI(?:'ve| have)?\s+(?:personally\s+)?(?:been (?:there|here|to)|eaten|tried (?:(?:the|a|an|their|some|that|this) [\w'-]+(?: [\w'-]+){0,3}|it) (?:there|here)|(?:visited|gone)(?: to)? (?:there|here)|dined|stayed at)\b"
    r"|\bI\s+(?:personally\s+)?(?:ate|went|dined|visited|stayed|got)\s+(?:to |at )?(?:there|here)\b"
    r"|\bI\s+(?:personally\s+)?(?:ate at|dined at|stayed at)\b"
    r"|\bI(?:'ve| have)? had (?:a|an|the|some)\b[^.?!]{0,40}\b(?:there|here)\b"
    r"|\b(?:when|last time|the time) I (?:visited(?=\s*[,.]|\s+(?:there|here)\b)|ate|tried|was there)\b"
    r"|\bI (?:love|enjoy) (?:going|eating|ordering) (?:there|at)\b",
    re.IGNORECASE,
)
_CREATIVE_REQUEST = re.compile(
    r"\b(?:story|stories|poem|poetry|joke|song|lyrics|riddle|haiku|limerick|fiction|roleplay|role-play|"
    r"pretend|imagine|write|compose|narrate)\b",
    re.IGNORECASE,
)
_VENUE_NAME = re.compile(r"\b[A-Z][\w'’-]*(?:\s+(?:&|and|of|the|de|la|at)\s+|\s+)[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)*")
_NOT_VENUE_WORDS = frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "the", "this", "that", "it", "they", "their", "sure", "yes", "here", "there",
    "open", "try", "based", "check", "some", "another", "i", "i'm", "i've", "you", "your", "we",
    "italian", "mexican", "chinese", "japanese", "thai", "indian", "french", "american", "greek",
})
_SPECIFIC_DETAIL = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)\b"
    r"|\$\s?\d"
    r"|\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"
    r"|\b\d{1,5}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Pike)\b"
    r"|\b(?:open|opens|closes|closed)\b[^.?!]{0,30}\b(?:until|till|from|at|daily)\b",
    re.IGNORECASE,
)


def experience_claim(answer: Optional[str], query: Optional[str] = None) -> bool:
    """The answer speaks as someone who has been, eaten or tried: not true of an
    assistant. A creative request (a story told in the first person) is exempt."""
    if not answer or (query and _CREATIVE_REQUEST.search(query)):
        return False
    return bool(_EXPERIENCE_CLAIM.search(answer))


def _has_venue_with_detail(answer: str) -> bool:
    for match in _VENUE_NAME.finditer(answer):
        words = [w.strip("'’").lower() for w in re.split(r"\s+", match.group(0))]
        significant = [w for w in words if w not in _NOT_VENUE_WORDS]
        if len(significant) >= 2:
            return bool(_SPECIFIC_DETAIL.search(answer))
    return False


# Intents whose answer names businesses or venues: only these (and a request to
# find a local business) are judged for an invented venue with details.
VENUE_INTENTS: FrozenSet[IntentCategory] = frozenset({IntentCategory.DINING, IntentCategory.EVENTS})


def _is_venue_intent(intent: Any) -> bool:
    """By value, so an intent that arrives as a string or a stand-in with a
    ``.value`` (not hashable) is judged like the enum."""
    value = getattr(intent, "value", intent)
    return isinstance(value, str) and value in {i.value for i in VENUE_INTENTS}


def ungrounded_answer(answer: Optional[str], intent: Any, query: Optional[str], is_grounded: bool) -> bool:
    """True when the turn holds no tool data and the answer still claims what
    only data could give: a first-person experience, or (for a venue intent or a
    local-business request) a named venue with hours, a time, a price, an address
    or a phone number. A knowledge answer never matches: a definition or how-it-works
    question may carry a worked example (a company and a price) whatever its intent."""
    if is_grounded or not answer:
        return False
    if experience_claim(answer, query):
        return True
    if knowledge_question(query):
        return False
    if _is_venue_intent(intent) or local_business_cue(query):
        return _has_venue_with_detail(answer)
    return False


# --- synthesis rules shared by synthesize_node and the streaming prompt builder ---

SPORTS_STATUS_RULE = (
    "7. SPORTS STATUS: the data's status object says where the game stands. If status.state is "
    '"live", say the game is in progress and give the score; never call it the last result, a win '
    'or a loss. If status.state is "final", the winner is status.winner (a tie when status.tie is true). '
    'If it is "upcoming", say when it starts.'
)


def _has_sports_status(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    status = data.get("status")
    if isinstance(status, dict) and status.get("state"):
        return True
    results = data.get("tool_results")
    return isinstance(results, dict) and any(_has_sports_status(r) for r in results.values())


def sports_status_rule(state: Any) -> str:
    """The extra anti-hallucination line for a turn whose data carries a sports
    status; empty otherwise."""
    return ("\n" + SPORTS_STATUS_RULE) if _has_sports_status(_field(state, "retrieved_data")) else ""


def tool_owned_without_data(state: Any) -> bool:
    """A data intent with nothing fetched: its synthesis must not use the
    built-in-knowledge prompt, which permits specifics the turn never had."""
    return _field(state, "intent") in TOOL_OWNED and not grounded(state) and not prior_tool_context(state)


def no_data_prompt(query: str) -> str:
    return f"""Question: {query}

CRITICAL: You do NOT have access to current or specific information to answer this question.

You must respond with:
1. Acknowledge you don't have current/specific information
2. Suggest where the user can find this information
3. NEVER make up specific facts, dates, names, numbers, or events

Respond honestly about your limitations.

Response:"""

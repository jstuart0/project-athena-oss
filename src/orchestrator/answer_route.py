"""Where a classified turn goes next: one routing decision for the graph and
the streaming runner.

``handler_route`` picks the turns a dedicated node owns (a preset answer, a
media request, home control, SMS, notification preferences, music, TV).
``decide`` picks among the information routes and takes the two async
facts (the tool-calling trigger, the ambient-fragment gate) as arguments, so
the function stays synchronous and testable. Both callers map a ``Route`` to
their own node call; neither keeps a routing rule of its own.

The routes the two paths used to disagree on, and the side chosen:

(a) media-request keywords: the graph's rule (a media request goes to the
    tool node, owner mode only, ahead of home control) now applies to both.
(b) ``GENERAL_INFO`` with no trigger: synthesis (the graph node, or the
    streamed synthesis). Equivalent; kept.
(c) a referential question about the conversation, asked of a data intent:
    answered from history in both paths (the intent becomes ``GENERAL_INFO``).
(d) ``GENERAL_INFO`` or ``UNKNOWN`` that asks for a local business: the tool
    node in both (the streaming runner used to skip it).
(e) a preset answer (``skip_synthesis`` with an answer): ``FINALIZE``, the
    graph's finalize or the streamed precomputed answer.
"""
from __future__ import annotations

import enum
import logging
import re

from orchestrator.context import is_conversational_reference
from orchestrator.grounding import local_business_cue, request_text
from orchestrator.state import IntentCategory, OrchestratorState

logger = logging.getLogger(__name__)


class Route(str, enum.Enum):
    TOOL_CALL = "tool_call"
    SYNTHESIZE = "synthesize"
    FINALIZE = "finalize"
    AMBIENT = "ambient"
    ROUTE_INFO = "route_info"
    CONTROL = "route_control"
    MUSIC = "route_music"
    TV = "route_tv"
    SMS = "send_sms"
    NOTIFICATION_PREF = "notification_pref"


# A phrase that can only be a request to the media library.
MEDIA_REQUEST_KEYWORDS = (
    "request movie", "request show", "request the movie", "request the show",
    "add movie", "add the movie", "add show", "add the show", "add to plex", "add to jellyfin",
    "download movie", "download show", "download the movie", "download the show",
    "my requests", "media requests", "pending requests", "my pending",
    "is available on plex", "is available on jellyfin",
    "overseerr", "request status", "movie request", "show request", "tv request",
)
# Phrases that are also how a viewer or listener asks a device to play something.
AMBIGUOUS_MEDIA_KEYWORDS = ("want to watch", "in the library", "on plex", "on jellyfin")
# A device, an app or a room named in the request: a command, not a library request.
_PLAYBACK_TARGET = re.compile(
    r"\b(?:in|on|at) (?:the|my|our) [a-z][a-z ]{1,24}\b"
    r"|\b(?:netflix|hulu|disney\+?|youtube|prime video|hbo|max|apple tv|roku|spotify|apple music|the tv|tv)\b"
)
_PLAYBACK_INTENTS = frozenset({IntentCategory.TV_CONTROL, IntentCategory.MUSIC_PLAY, IntentCategory.MUSIC_CONTROL})

# Data intents the tool node answers directly (the rest go through route_info).
_TOOL_NODE_INTENTS = frozenset({
    IntentCategory.DINING, IntentCategory.RECIPES, IntentCategory.EVENTS,
    IntentCategory.STREAMING, IntentCategory.NEWS, IntentCategory.STOCKS,
    IntentCategory.FLIGHTS, IntentCategory.DIRECTIONS,
})

_HANDLER_ROUTES = {
    IntentCategory.CONTROL: Route.CONTROL,
    IntentCategory.NOTIFICATION_PREF: Route.NOTIFICATION_PREF,
    IntentCategory.TEXT_ME_THAT: Route.SMS,
    IntentCategory.MUSIC_PLAY: Route.MUSIC,
    IntentCategory.MUSIC_CONTROL: Route.MUSIC,
    IntentCategory.TV_CONTROL: Route.TV,
}


def is_media_request(state: OrchestratorState) -> bool:
    """A media-library request, which only an owner-mode turn may make. A phrase
    that is also how one asks a device to play ("I want to watch Netflix in the
    den", "play it on plex in the living room") is a request only when no device,
    app or room is named and the turn isn't already a playback command."""
    if state.mode == "guest":
        return False
    query = (state.query or "").lower()
    if any(keyword in query for keyword in MEDIA_REQUEST_KEYWORDS):
        return True
    if not any(keyword in query for keyword in AMBIGUOUS_MEDIA_KEYWORDS):
        return False
    for keyword in AMBIGUOUS_MEDIA_KEYWORDS:  # the phrase itself ("in the library") names no target
        query = query.replace(keyword, " ")
    return state.intent not in _PLAYBACK_INTENTS and not _PLAYBACK_TARGET.search(query)


def has_conversation(state: OrchestratorState) -> bool:
    return bool(state.conversation_history or state.history_summary)


def handler_route(state: OrchestratorState) -> Route | None:
    """The route of a turn a dedicated node owns, or None when the information
    routes decide. The order is the rule: a preset answer ends the turn, a media
    request beats home control (a pending-request phrase can read as a command)."""
    if (state.identity_answer or state.skip_synthesis) and state.answer:
        return Route.FINALIZE
    if is_media_request(state):
        return Route.TOOL_CALL
    return _HANDLER_ROUTES.get(state.intent)


def _asks_about_the_conversation(state: OrchestratorState) -> bool:
    if not has_conversation(state):
        return False
    ref_info = state.context_ref_info or {}
    return bool(
        is_conversational_reference(state.query, True)
        or ref_info.get("has_context_ref", False)
    )


def decide(state: OrchestratorState, *, tool_trigger: bool, ambient: bool) -> Route:
    """The information route of a turn no handler owns."""
    if ambient:
        return Route.AMBIENT
    intent = state.intent
    wants_local_business = (
        intent in (IntentCategory.GENERAL_INFO, IntentCategory.UNKNOWN)
        and local_business_cue(request_text(state))
        and not _asks_about_the_conversation(state)
    )
    if tool_trigger or wants_local_business:
        return Route.TOOL_CALL

    if intent == IntentCategory.GENERAL_INFO:
        return Route.SYNTHESIZE

    if intent in _TOOL_NODE_INTENTS:
        if _asks_about_the_conversation(state):
            logger.info("answer_route_history_redirect", extra={"intent": intent.value})
            state.intent = IntentCategory.GENERAL_INFO
            return Route.SYNTHESIZE
        return Route.TOOL_CALL

    if intent == IntentCategory.UNKNOWN:
        continuing = bool((state.context_ref_info or {}).get("is_continuation", False))
        if continuing and state.conversation_history:
            return Route.SYNTHESIZE
        return Route.FINALIZE

    return Route.ROUTE_INFO

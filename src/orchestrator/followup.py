"""Follow-ups keep their subject: a bare reply resumes the task it answers.

"Yes." after "Want me to look for outdoor seating?" means *search for outdoor
seating*, not a new topic. The task is derived from state the entry point
already holds -- the session's last user and assistant messages
(`last_exchange`) and the stored context's intent (`prev_intent`) -- so no new
key and no extra round trip.

`open_task` says whether the last exchange left something open (a question or
an offer, on an information intent). `resolve` says what a reply does with it:
`Affirm` (do it), `Decline` (drop it), `Slot` (a short answer that fills a
blank in it) or `None` (a new turn). Pure; the caller holds the state.

A reply that something else owns resolves to None first: a Home Assistant
pending (it owns "yes" and "no"), a command, words that name a device, a
question.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Union

from shared.fast_path_vocab import (
    AFFIRMATION_WORDS,
    BARE_AFFIRMATION_RE,
    GRATITUDE_WORDS,
    BARE_NEGATION_RE,
    POLITE_SUFFIX_WORDS,
    normalize_reply,
)

from orchestrator import access_target
from orchestrator.fast_path import (
    _ends_with_question,
    ends_with_promise,
    is_generic_closing,
    last_assistant_text,
    last_sentence,
)
from orchestrator.session_keys import id_class
from orchestrator.state import IntentCategory
from orchestrator.utterance_kind import UtteranceClassification, UtteranceKind

# The exchange is too old to be what the speaker is answering.
STALE_AFTER_SECONDS = 600.0
SLOT_MAX_WORDS = 6
_REQUEST_MAX_CHARS = 300
_OFFER_MAX_CHARS = 200

# Information intents only. A control, music, TV, SMS or notification turn is
# never resumed from words: its own pending (if any) owns the reply.
INFO_INTENTS = frozenset(
    intent.value
    for intent in (
        IntentCategory.WEATHER, IntentCategory.AIRPORTS, IntentCategory.SPORTS, IntentCategory.FLIGHTS,
        IntentCategory.EVENTS, IntentCategory.STREAMING, IntentCategory.NEWS, IntentCategory.STOCKS,
        IntentCategory.RECIPES, IntentCategory.DINING, IntentCategory.DIRECTIONS, IntentCategory.WEBSEARCH,
        IntentCategory.GENERAL_INFO,
    )
)

HA_PENDING_KEYS = ("pending_write_confirmation", "awaiting_room_choice", "awaiting_state_question")
_AWAITING_PREFIX = "awaiting_"

# What the pipeline says when it has nothing: not a task to resume.
_NON_TASK_REPLIES = (
    "i'm not sure how to help",
    "i couldn't look that up",
    "i'm not confident in my response",
    "i encountered an issue processing",
    "i don't have current information to answer",
    "i couldn't process that request",
)

_QUESTION_START = re.compile(
    r"^(?:who|whom|whose|what|when|where|why|how|which|is|are|was|were|am|do|does|did|can|could|will|would"
    r"|should|shall|have|has|had|may|might)\b"
)
_SPOKEN_AFFIRMATION = frozenset(
    AFFIRMATION_WORDS + ("please", "thanks", "thank", "you", "definitely", "absolutely", "totally")
) | frozenset(word for phrase in POLITE_SUFFIX_WORDS for word in phrase.split())
_SPOKEN_AFFIRMATION_MAX_WORDS = 3


@dataclass(frozen=True)
class OpenTask:
    request: str
    offer: str
    kind: str  # "question" | "promise"
    intent: str


@dataclass(frozen=True)
class Affirm:
    resolved_request: str


@dataclass(frozen=True)
class Slot:
    resolved_request: str


@dataclass(frozen=True)
class Decline:
    pass


Resolution = Union[Affirm, Slot, Decline]


def _epoch(timestamp: Any) -> Optional[float]:
    if isinstance(timestamp, (int, float)):
        return float(timestamp)
    try:
        parsed = datetime.fromisoformat(str(timestamp))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # the session stores UTC
    return parsed.timestamp()


def last_exchange_from_session(session: Any) -> Optional[dict]:
    """The session's last user message and the assistant reply to it, or None.

    Read from the session the entry point already loaded. A reply the fast path
    wrote ("Hello. How can I help?") is not an exchange to resume."""
    assistant = last_assistant_text(session)
    if not assistant:
        return None
    messages = getattr(session, "messages", None) or []
    reply_index = max(
        (i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=None
    )
    if reply_index is None:
        return None
    user = next(
        (m.get("content") for m in reversed(messages[:reply_index]) if m.get("role") == "user"), None
    )
    if not isinstance(user, str) or not user.strip():
        return None
    return {
        "user": user,
        "assistant": assistant,
        "at": _epoch(messages[reply_index].get("timestamp")),
        "session_class": id_class(getattr(session, "session_id", None)),
    }


def _asked(last_exchange: Optional[Mapping[str, Any]], session_class: str, now: Optional[float]) -> Optional[tuple]:
    """(user, assistant, kind) when the last exchange ended by asking or offering
    something and is fresh, same-class and not the pipeline's own fallback."""
    if not last_exchange:
        return None
    user, assistant = last_exchange.get("user"), last_exchange.get("assistant")
    if not isinstance(user, str) or not isinstance(assistant, str) or not user.strip():
        return None
    if last_exchange.get("session_class") != session_class:
        return None
    at = _epoch(last_exchange.get("at"))
    clock = time.time() if now is None else now
    if at is None or clock - at > STALE_AFTER_SECONDS:
        return None
    lowered = assistant.lower().replace("’", "'")
    if any(phrase in lowered for phrase in _NON_TASK_REPLIES) or is_generic_closing(assistant):
        return None
    # A device offer tacked on an answer ("want me to turn on the lights?") is
    # for control to take: it is not the information task.
    if access_target.names_device(last_sentence(assistant)):
        return None
    if _ends_with_question(assistant):
        return user, assistant, "question"
    if ends_with_promise(assistant):
        return user, assistant, "promise"
    return None


def open_task(
    last_exchange: Optional[Mapping[str, Any]],
    prev_intent: Optional[str],
    prev_query: Optional[str] = None,
    *,
    session_class: str,
    now: Optional[float] = None,
) -> Optional[OpenTask]:
    """What the last exchange left open, or None.

    `prev_intent` is the stored context's intent (`state.prev_context["intent"]`);
    only an information intent has a task to resume. `prev_query` is that
    context's query: when the last user message was itself a reply (so the
    context holds the request it resumed), it is the request to carry."""
    if prev_intent not in INFO_INTENTS:
        return None
    asked = _asked(last_exchange, session_class, now)
    if asked is None:
        return None
    user, assistant, kind = asked
    request = user
    if prev_query and normalize_reply(user) and normalize_reply(user) in normalize_reply(prev_query):
        request = prev_query
    return OpenTask(
        request=request.strip()[:_REQUEST_MAX_CHARS],
        offer=last_sentence(assistant)[:_OFFER_MAX_CHARS],
        kind=kind,
        intent=prev_intent,
    )


def _pending_owns_reply(prev_context: Optional[Mapping[str, Any]]) -> bool:
    parameters = (prev_context or {}).get("parameters")
    if not isinstance(parameters, Mapping):
        return False
    if any(parameters.get(key) for key in HA_PENDING_KEYS):
        return True
    return any(isinstance(k, str) and k.startswith(_AWAITING_PREFIX) and v for k, v in parameters.items())


_GRATITUDE_TOKENS = frozenset(word for phrase in GRATITUDE_WORDS for word in phrase.split())


def _is_affirmation(reply: str, task_kind: str = "question") -> bool:
    if task_kind == "promise" and _GRATITUDE_TOKENS & set(reply.split()):
        return False  # "ok thanks" after an offer closes the topic; it doesn't accept
    if BARE_AFFIRMATION_RE.match(reply):
        return True
    words = reply.split()
    return (
        0 < len(words) <= _SPOKEN_AFFIRMATION_MAX_WORDS
        and words[0] in AFFIRMATION_WORDS
        and all(word in _SPOKEN_AFFIRMATION for word in words)
    )


def has_question_form(query: str) -> bool:
    text = (query or "").strip()
    return text.endswith(("?", "？")) or bool(_QUESTION_START.match(normalize_reply(text)))


def _slot_shaped(query: str, uk: Optional[UtteranceClassification]) -> bool:
    """A short statement that is no command, no question and names no device."""
    reply = normalize_reply(query)
    if not reply or len(reply.split()) > SLOT_MAX_WORDS:
        return False
    if uk is not None and uk.kind in (UtteranceKind.IMPERATIVE, UtteranceKind.STATE_QUESTION):
        return False
    return not has_question_form(query) and not access_target.names_device(query)


def slot_candidate(
    query: str,
    last_exchange: Optional[Mapping[str, Any]],
    uk: Optional[UtteranceClassification],
    *,
    session_class: str,
    now: Optional[float] = None,
) -> bool:
    """Whether the words could fill a blank in a question just asked. Needs no
    stored context, so the semantic cache can ask it before the graph runs."""
    asked = _asked(last_exchange, session_class, now)
    return asked is not None and asked[2] == "question" and _slot_shaped(query, uk)


_INSTRUCTION_LIKE = re.compile(
    r"\b(?:ignore|disregard|forget|override)\b.{0,40}\b(?:instructions?|rules|prompt|guidelines)\b"
    r"|\bsystem prompt\b|\byou (?:must|are now|should now)\b|\bnew instructions\b",
    re.IGNORECASE,
)


def _quote(offer: str) -> str:
    """The assistant's own earlier sentence as quoted data: one line, no quote
    marks, and nothing that reads as an instruction to the model."""
    text = " ".join(offer.replace('"', "'").split())
    return "an earlier offer" if _INSTRUCTION_LIKE.search(text) else text


def _clean(text: str) -> str:
    return text.strip().rstrip(".!?, ")


def resolve(
    query: str,
    task: Optional[OpenTask],
    uk: Optional[UtteranceClassification],
    prev_context: Optional[Mapping[str, Any]] = None,
) -> Optional[Resolution]:
    """What a reply does with the open task, or None when it is a new turn."""
    if task is None or _pending_owns_reply(prev_context):
        return None
    if uk is not None and uk.kind == UtteranceKind.IMPERATIVE:
        return None
    if access_target.names_device(query):
        return None
    reply = normalize_reply(query)
    if not reply:
        return None
    if BARE_NEGATION_RE.match(reply):
        return Decline()
    if _is_affirmation(reply, task.kind):
        return Affirm(
            f'{_clean(task.request)}. You asked: "{_quote(task.offer)}" The user answered: {reply}.'
        )
    if task.kind == "question" and _slot_shaped(query, uk):
        return Slot(f"{_clean(task.request)}, {_clean(query)}")
    return None

"""Deterministic fast path: which turns need no model, and when to defer.

`fast_path_reply` is the vocabulary lookup (pure, exact match, local clock).
`fast_path_open_question` is the guard that keeps the fast path from
answering a reply to an open question: a stored confirmation or clarification
context, or an assistant message that ended in a question. It reads the
conversation context once, tells "no context" from "read failed", and fails
closed (a read failure defers to the full pipeline).

R2-C3: imports `nodes._runtime`, `session_keys`, `shared.*` -- never
`orchestrator.main`.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Tuple

from shared.assistant_profile import ambient_fragment_gate_enabled, get_guardrails
from shared.fast_path_vocab import AMBIENT_REPLY, normalize, reply_for
from shared.local_time import local_now
from shared.logging_config import configure_logging
from shared.output_channel import OutputChannel, channel_for_interface_type

from orchestrator.nodes import _runtime
from orchestrator.session_keys import context_storage_key
from orchestrator.utterance_kind import UtteranceKind, classify_utterance

logger = configure_logging("orchestrator.fast_path")

CONTEXT_READ_TIMEOUT_SECONDS = 2.0

REASON_PENDING_CONFIRMATION = "pending_confirmation"
REASON_AWAITING_CONTEXT = "awaiting_context"
REASON_OPEN_QUESTION = "open_question"
REASON_CONTEXT_UNREADABLE = "context_unreadable"

_AWAITING_PREFIX = "awaiting_"
_PENDING_KEY = "pending_write_confirmation"
_TRAILING_CLOSERS = "\"')]}”’»"
_QUESTION_MARKS = ("?", "？")


@dataclass(frozen=True)
class FastPathReply:
    kind: str
    text: str


def fast_path_reply(query: Optional[str]) -> Optional[FastPathReply]:
    """The deterministic reply for an exact-match trivial turn, else None.
    Never raises."""
    try:
        found = reply_for(query, local_now())
    except Exception:
        return None
    return FastPathReply(kind=found[0], text=found[1]) if found else None


def last_assistant_text(session: Any) -> Optional[str]:
    """The text of the session's last assistant message, or None.

    A message the fast path itself wrote ("Hello. How can I help?") is not an
    open question: it carries `metadata.fast_path` and reads as no question.
    """
    for message in reversed(getattr(session, "messages", None) or []):
        if message.get("role") != "assistant":
            continue
        if (message.get("metadata") or {}).get("fast_path"):
            return None
        content = message.get("content")
        return content if isinstance(content, str) else None
    return None


def _ends_with_question(text: Optional[str]) -> bool:
    if not text:
        return False
    return text.rstrip().rstrip(_TRAILING_CLOSERS).rstrip().endswith(_QUESTION_MARKS)


def _context_reason(parameters: Mapping[str, Any]) -> Optional[str]:
    if parameters.get(_PENDING_KEY):
        return REASON_PENDING_CONFIRMATION
    for key, value in parameters.items():
        if isinstance(key, str) and key.startswith(_AWAITING_PREFIX) and value:
            return REASON_AWAITING_CONTEXT
    return None


async def _redis_context_parameters(session_id: str) -> Optional[Mapping[str, Any]]:
    """The stored context's parameters; None when none is stored. Raises when
    the store can't be read."""
    cache = _runtime.get_cache_client()
    if not (cache and getattr(cache, "client", None)):
        return None
    raw = await asyncio.wait_for(
        cache.client.get(context_storage_key(session_id)), timeout=CONTEXT_READ_TIMEOUT_SECONDS
    )
    if not raw:
        return None
    data = json.loads(raw)
    parameters = data.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ValueError("context parameters are not a mapping")
    return parameters


def _memory_context_parameters(session_id: str) -> Mapping[str, Any]:
    entry = _runtime.get_memory_context().get(session_id)
    if not entry or entry.get("expires_at", 0) <= time.time():
        return {}
    return getattr(entry.get("context"), "parameters", None) or {}


async def fast_path_open_question(session_id: Optional[str], last_assistant: Optional[str]) -> Optional[str]:
    """Why the fast path must defer, or None when the turn can be answered.

    Reasons: `pending_confirmation` (any caller's pending write confirmation),
    `awaiting_context` (any `awaiting_*` clarification), `open_question` (the
    last assistant message ended with a question), `context_unreadable`
    (the store failed or held malformed data). Never raises.
    """
    try:
        if session_id:
            try:
                stored = await _redis_context_parameters(session_id)
            except Exception:
                return REASON_CONTEXT_UNREADABLE
            for parameters in (stored or {}, _memory_context_parameters(session_id)):
                reason = _context_reason(parameters)
                if reason:
                    return reason
        if _ends_with_question(last_assistant):
            return REASON_OPEN_QUESTION
        return None
    except Exception:
        return REASON_CONTEXT_UNREADABLE


# --- ambient fragments ---------------------------------------------------------------
#
# An opt-in gate (guardrails.voice_response.ambient_fragment_gate, default off)
# that answers an overheard spoken fragment with AMBIENT_REPLY instead of
# paying for tool selection. It is deliberately narrow: a turn is gated only
# when NO guard blocks it, and `ambient_block_reason` names the first guard that
# does, so a test (or a log line) can say why a turn was not gated.

AMBIENT_MIN_WORDS = 3
AMBIENT_MAX_WORDS = 8
AMBIENT_CONFIDENCE_CEILING = 0.30
# The pattern classifier's no-match result is GENERAL_INFO at 0.5; a specific
# match is 0.85. Anything at or above this ceiling is a real match.
PATTERN_FALLBACK_CONFIDENCE_CEILING = 0.85

GUARD_CHANNEL = "channel"
GUARD_OPEN_QUESTION = "open_question"
GUARD_INTENT = "intent"
GUARD_CONFIDENCE = "confidence"
GUARD_SAFETY_WORD = "safety_word"
GUARD_WORD_COUNT = "word_count"
GUARD_FIRST_WORD = "first_word"
GUARD_MEDIA_WORD = "media_command_word"
GUARD_FIRST_PERSON = "first_person"
GUARD_REFERENCE_WORD = "reference_word"
GUARD_DEVICE_WORD = "device_word"
GUARD_CONFIRMATION_WORD = "confirmation_word"
GUARD_CONTINUATION = "continuation"
GUARD_UTTERANCE_KIND = "utterance_kind"
GUARD_PATTERN = "pattern_classifier"
GUARD_ERROR = "error"

# Never gated, at any confidence, anywhere in the utterance.
SAFETY_WORDS = frozenset(
    "help emergency fire police ambulance 911 alarm smoke intruder burglar danger hurt gas cancel stop".split()
)
# A first word that makes a short utterance a question, an auxiliary-led request, a command or a repeat/feedback request.
_FIRST_WORDS = frozenset(
    ("what who where when why how which is are am can could would will do does did should tell set turn "
     "repeat say go be please sorry pardon back make put open close call find show give let lets text send start read remind").split()
)
# A media/command word anywhere.
_MEDIA_COMMAND_WORDS = frozenset(
    ("stop pause next skip previous resume play mute unmute louder quieter volume cancel off on up down again repeat back "
     "quiet silence brighter dimmer darker warmer cooler colder hotter higher lower faster slower").split()
)
# First/second-person statements are often requests ("I'm cold", "I'm home", "you're too loud").
_FIRST_PERSON_WORDS = frozenset("i im ive id ill my me mine we us our you youre your".split())
# Words that point back at an earlier question or an option list ("the kitchen one", "all of them").
_REFERENCE_WORDS = frozenset(
    ("the a an all both that this those these it its them they one ones first second third last other another same there here").split()
)
# A device, room or house-system noun: the utterance is probably about the house.
_DEVICE_WORDS = frozenset(
    ("light lights lamp lamps bulb bulbs door doors garage lock locks thermostat fan fans tv television window windows "
     "blind blinds shade shades curtain curtains heat heater heating ac temperature switch plug outlet music song songs "
     "speaker speakers kitchen bedroom bathroom office living porch basement attic hallway patio").split()
)
_CONFIRMATION_WORDS = frozenset("yes yeah yep yup no nope nah ok okay sure please thanks thank".split())

_PatternResult = Tuple[Any, float]
_pattern_classifier: Optional[Callable[..., _PatternResult]] = None


def register_pattern_classifier(classifier: Callable[..., _PatternResult]) -> None:
    """Register the orchestrator's pattern classifier (`(query, return_confidence=True)
    -> (intent, confidence)`). It lives in `main.py`, which this module must not
    import (R2-C3); main registers it at import. Until it is registered the
    gate never applies."""
    global _pattern_classifier
    _pattern_classifier = classifier


def _pattern_confirms_no_match(pattern: Optional[_PatternResult]) -> bool:
    if not pattern:
        return False
    intent, confidence = pattern
    return getattr(intent, "value", intent) in ("unknown", "general_info") and confidence < PATTERN_FALLBACK_CONFIDENCE_CEILING


def ambient_block_reason(
    query: Optional[str],
    intent: Any,
    confidence: Optional[float],
    open_question: Optional[str],
    conversation_history: Any,
    ref_info: Optional[Mapping[str, Any]],
    channel: OutputChannel,
    pattern: Optional[_PatternResult] = None,
) -> Optional[str]:
    """The first guard that stops this turn from being gated, or None when it
    is an ambient fragment. Guard order is the order of the GUARD_* constants.
    `pattern` is the pattern classifier's `(intent, confidence)`; without it the
    turn is never gated. Never raises."""
    try:
        if channel is not OutputChannel.SPEECH:
            return GUARD_CHANNEL
        if open_question:
            return GUARD_OPEN_QUESTION
        if getattr(intent, "value", intent) != "unknown":
            return GUARD_INTENT
        if confidence is None or not confidence < AMBIENT_CONFIDENCE_CEILING:
            return GUARD_CONFIDENCE
        words = normalize(query).split()
        if SAFETY_WORDS.intersection(words):
            return GUARD_SAFETY_WORD
        if not AMBIENT_MIN_WORDS <= len(words) <= AMBIENT_MAX_WORDS:
            return GUARD_WORD_COUNT
        if words[0] in _FIRST_WORDS:
            return GUARD_FIRST_WORD
        if _MEDIA_COMMAND_WORDS.intersection(words):
            return GUARD_MEDIA_WORD
        if _FIRST_PERSON_WORDS.intersection(words):
            return GUARD_FIRST_PERSON
        if _REFERENCE_WORDS.intersection(words):
            return GUARD_REFERENCE_WORD
        if _DEVICE_WORDS.intersection(words):
            return GUARD_DEVICE_WORD
        if _CONFIRMATION_WORDS.intersection(words):
            return GUARD_CONFIRMATION_WORD
        if (ref_info or {}).get("is_continuation") and conversation_history:
            return GUARD_CONTINUATION
        utterance = classify_utterance(query)
        if utterance.kind is not UtteranceKind.UNKNOWN or utterance.device_type is not None:
            return GUARD_UTTERANCE_KIND
        if not _pattern_confirms_no_match(pattern):
            return GUARD_PATTERN
        return None
    except Exception:
        return GUARD_ERROR


def is_ambient_fragment(
    query: Optional[str],
    intent: Any,
    confidence: Optional[float],
    open_question: Optional[str],
    conversation_history: Any,
    ref_info: Optional[Mapping[str, Any]],
    channel: OutputChannel,
    pattern: Optional[_PatternResult] = None,
) -> bool:
    """True only for a spoken fragment no guard blocks. See `ambient_block_reason`."""
    return ambient_block_reason(
        query, intent, confidence, open_question, conversation_history, ref_info, channel, pattern
    ) is None


def _last_assistant_in_history(history: Any) -> Optional[str]:
    for message in reversed(history or []):
        if isinstance(message, dict) and message.get("role") == "assistant":
            content = message.get("content")
            return content if isinstance(content, str) else None
    return None


def _classify_by_pattern(query: Optional[str]) -> Optional[_PatternResult]:
    if _pattern_classifier is None:
        return None
    try:
        return _pattern_classifier(query, return_confidence=True)
    except Exception:
        return None


async def ambient_fragment_applies(state: Any) -> bool:
    """`is_ambient_fragment` for a classified turn, with the guardrail switch
    and the open-question read (the same read and the same fail-closed rule
    the fast path uses). The cheap checks run first, so only a real candidate
    pays for the guardrail and context reads. Never raises."""
    try:
        channel = channel_for_interface_type(getattr(state, "interface_type", None))
        args = (state.query, state.intent, state.confidence)
        rest = (state.conversation_history, state.context_ref_info, channel)
        pattern = _classify_by_pattern(state.query)
        if not is_ambient_fragment(*args, None, *rest, pattern):
            return False
        if not ambient_fragment_gate_enabled(await get_guardrails()):
            return False
        reason = await fast_path_open_question(
            state.session_id, _last_assistant_in_history(state.conversation_history)
        )
        return is_ambient_fragment(*args, reason, *rest, pattern)
    except Exception:
        return False

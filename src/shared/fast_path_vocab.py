"""The deterministic-reply vocabulary, and everything that must never match it.

One table answers trivial turns (greetings, thanks, time, date, short
acknowledgements) without a model. The exclusions live beside it so the
orchestrator and the gateway apply the same rule: a scene or routine phrase,
or a bare yes/no reply to a question, is never a fast-path candidate.

Stdlib only: the gateway asks `is_fast_path_candidate` and the orchestrator
asks `reply_for`, from the same table. No I/O, no config, no clock reads --
callers pass `now`.

The confirmation vocabulary (word tuples, `normalize_reply`, the two bare
reply patterns) is defined here and re-exported by
`orchestrator.context.detector` and `orchestrator.write_fanout`, so the
anaphora tag, the confirmation reply and this table share one source.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Optional, Tuple

# --- confirmation vocabulary (moved from orchestrator.context.detector) ------

AFFIRMATION_WORDS = ("yes", "yeah", "yep", "yup", "ok", "okay", "sure")
NEGATION_WORDS = ("no", "nope", "nah")
GRATITUDE_WORDS = ("thanks", "thank you")
POLITE_SUFFIX_WORDS = ("please",) + GRATITUDE_WORDS
PROCEED_PHRASES = ("do it", "go ahead")


def _alternation(words: Iterable[str]) -> str:
    return "|".join(re.escape(w) for w in words)


# Matched against normalize_reply() output. Anything with more content than
# these ("yes, just the desk lamp") is a new utterance, never a confirmation.
BARE_AFFIRMATION_RE = re.compile(
    rf"^(?:{_alternation(AFFIRMATION_WORDS)})"
    rf"(?:\s+(?:{_alternation(POLITE_SUFFIX_WORDS + PROCEED_PHRASES)}))?$"
    rf"|^(?:{_alternation(PROCEED_PHRASES)})$"
)
BARE_NEGATION_RE = re.compile(
    rf"^(?:{_alternation(NEGATION_WORDS)})(?:\s+(?:{_alternation(GRATITUDE_WORDS)}))?$"
)


def normalize_reply(query: Optional[str]) -> str:
    """Lowercase, strip trailing punctuation, remove commas, collapse
    whitespace -- STT emits "Yes, please." for a bare yes."""
    q = (query or "").strip().lower()
    q = q.rstrip(".!?").replace(",", "")  # rstrip: linear on any input
    return re.sub(r"\s+", " ", q).strip()


# --- scene and routine phrases (lifted from classify_node) -------------------

# A query containing one of these is a scene/routine command for Home
# Assistant; classify_node routes it to CONTROL.
SCENE_TRIGGER_PHRASES = (
    "movie mode", "movie time", "watch a movie",
    "good night", "goodnight", "bedtime", "night mode", "time for bed",
    "good morning", "morning mode", "wake up",
    "i am leaving", "i'm leaving", "im leaving", "goodbye", "leaving home", "heading out",
    "i am home", "i'm home", "im home", "i'm back", "im back", "home now",
    "romantic mode", "date night",
    "relax mode", "chill mode",
    "party mode", "party time",
    "vibes for my girl", "my girl comes over", "girlfriend coming",
    "romantic vibes", "vibes for when", "set the mood",
)

# The MUSIC_PLAY -> CONTROL override list: scene phrases an LLM may have
# misread as a music request.
SCENE_OVERRIDE_PHRASES = (
    "party vibes", "party vibe", "party mode", "party time",
    "movie mode", "movie time", "chill mode", "relax mode",
    "romantic mode", "date night", "set the mood",
    "vibes for my girl", "my girl comes over", "girlfriend coming",
    "romantic vibes", "vibes for when",
)


# --- the table ----------------------------------------------------------------

def normalize(query: Optional[str]) -> str:
    """Lowercase with punctuation removed, for exact matching."""
    return re.sub(r"[^a-z0-9\s]", "", (query or "").lower()).strip()


_GREETING_REPLY = {
    "hello": "Hello. How can I help?",
    "hi": "Hi. How can I help?",
    "hey": "Hey. How can I help?",
    "good afternoon": "Good afternoon. How can I help?",
    "good evening": "Good evening. How can I help?",
}
_SMALLTALK_REPLY = {
    "how are you": "I'm doing well. How can I help?",
    "hows it going": "I'm here and ready to help.",
}
_THANKS_PHRASES = GRATITUDE_WORDS + (
    "thanks a lot", "thanks so much", "thank you very much", "thank you so much", "many thanks",
)
_FAREWELL_REPLY = {
    "bye": "Goodbye.",
    "bye bye": "Goodbye.",
    "see you": "See you later.",
    "see you later": "See you later.",
}
_ACK_PHRASES = (
    "got it", "cool", "great", "perfect", "nice", "awesome",
    "never mind", "nevermind", "thats all", "thats it",
)
TIME_PHRASES = frozenset({
    "what time is it", "whats the time", "what is the time", "current time",
    "tell me the time", "what time is it now",
})
DATE_PHRASES = frozenset({
    "what date is it", "whats the date", "what is todays date", "whats todays date",
    "current date", "what day is it", "what is the date", "what day is it today",
})

THANKS_REPLY = "You're welcome."
ACK_REPLY = "Okay."

# normalized phrase -> (kind, fixed text). The time and date kinds carry no
# fixed text: reply_for renders them from `now`.
REPLIES: dict = {
    **{p: ("greeting", t) for p, t in _GREETING_REPLY.items()},
    **{p: ("smalltalk", t) for p, t in _SMALLTALK_REPLY.items()},
    **{p: ("thanks", THANKS_REPLY) for p in _THANKS_PHRASES},
    **{p: ("farewell", t) for p, t in _FAREWELL_REPLY.items()},
    **{p: ("ack", ACK_REPLY) for p in _ACK_PHRASES},
    **{p: ("time", "") for p in TIME_PHRASES},
    **{p: ("date", "") for p in DATE_PHRASES},
}


def _is_excluded(query: str) -> bool:
    lowered = query.lower()
    if any(p in lowered for p in SCENE_TRIGGER_PHRASES) or any(p in lowered for p in SCENE_OVERRIDE_PHRASES):
        return True
    reply = normalize_reply(query)
    return bool(BARE_AFFIRMATION_RE.match(reply) or BARE_NEGATION_RE.match(reply))


def _lookup(query: Optional[str]) -> Optional[Tuple[str, str]]:
    if not query:
        return None
    normalized = normalize(query)
    entry = REPLIES.get(normalized)
    if entry is None or _is_excluded(query):
        return None
    return entry


def _clock_text(now: datetime) -> str:
    hour12 = now.hour % 12 or 12
    return f"{hour12}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}"


def reply_for(query: Optional[str], now: datetime) -> Optional[Tuple[str, str]]:
    """(kind, text) for an exact-match trivial turn, else None.

    None for a scene or routine phrase and for a bare yes/no, even when the
    table would otherwise match. `now` should be the property's local time.
    """
    entry = _lookup(query)
    if entry is None:
        return None
    kind, text = entry
    if kind == "time":
        return kind, f"It's {_clock_text(now)}."
    if kind == "date":
        return kind, f"Today is {now.strftime('%A, %B')} {now.day}, {now.year}."
    return kind, text


def is_fast_path_candidate(query: Optional[str]) -> bool:
    """True when `reply_for` would answer this query (no clock needed)."""
    return _lookup(query) is not None

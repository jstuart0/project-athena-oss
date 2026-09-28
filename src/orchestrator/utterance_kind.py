"""ATHENA-128 Phase 2 -- pure utterance classifier (D2, D3, D12).

Classifies a natural-language smart-home utterance into IMPERATIVE,
STATE_QUESTION, or UNKNOWN, without any I/O, LLM call, or knowledge of
Home Assistant entities. Consulted once per turn by
`orchestrator.nodes.route_control` and by `orchestrator.write_fanout`
(via `PermissionScope.utterance`).

R2-C3: stdlib only. Never import `orchestrator.main` or any sibling
module -- this file must be importable standalone.

Invariants: pure, never raises, and `None`/empty input classifies as
UNKNOWN.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class UtteranceKind(str, Enum):
    IMPERATIVE = "imperative"
    STATE_QUESTION = "state_question"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class UtteranceClassification:
    kind: UtteranceKind
    device_type: Optional[str] = None
    room: Optional[str] = None
    target_state: Optional[str] = None
    needs_referent: bool = False
    rule: Optional[str] = None


UNKNOWN_CLASSIFICATION = UtteranceClassification(kind=UtteranceKind.UNKNOWN, rule="kill_switch")


# ---------------------------------------------------------------------------
# Vocabulary (D2, 2.2 rules 2-5)
# ---------------------------------------------------------------------------

_LEADING_FILLER_RE = re.compile(r"^(?:hey|ok|okay|so|um|uh|athena|jarvis)\b[\s,]*", re.I)
_FILLER_ADVERB_RE = re.compile(r"\b(?:currently|right now|at the moment|still|now)\b", re.I)
_WS_RE = re.compile(r"\s+")
_CLAUSE_SPLIT_RE = re.compile(r"\?|\.|,| and | then ")

_REQUEST_PREFIXES = (
    "is it possible to", "would it be possible to", "are you able to",
    "can you make sure", "go ahead and",
    "i want you to", "i need you to",
    "can you", "could you", "would you", "will you",
    "let us", "let's", "lets",
    "make sure",
    "please",
)

_CONTROL_VERBS = (
    "turn|switch|set|make|dim|brighten|lock|unlock|open|close|shut|start|"
    "stop|play|pause|resume|kill|cut|raise|lower|put|leave|keep|activate|"
    "deactivate|enable|disable|warm|heat|cool"
)
_CONTROL_VERB_RE = re.compile(rf"^(?:{_CONTROL_VERBS})\b", re.I)

_GERUND_VERBS = (
    "turning|switching|setting|locking|unlocking|opening|closing|shutting|"
    "dimming|playing|pausing|stopping|starting"
)
_GERUND_FRAME_RE = re.compile(rf"^(?:do you mind|would you mind)\s+(?:{_GERUND_VERBS})\b", re.I)
_ENSURE_STATE_RE = re.compile(r"\b(?:is|are)\b.*" + r"(?:on|off|open|closed|locked|unlocked)\b", re.I)
_COMPOUND_CONNECTOR_RE = re.compile(r"^(?:if so|so|then)\s+", re.I)

_FIXED_IMPERATIVE_IDIOMS = {"lock up", "lock it down", "lights out"}

_DEVICE_NOUN = (
    r"(?:lights?|lamp(?:s|ing)?|switch(?:es)?|plug(?:s)?|outlet(?:s)?|"
    r"lock(?:s)?|deadbolt(?:s)?|garage(?: door)?|blinds|shades|curtains|door(?:s)?|"
    r"thermostat(?:s)?|heat(?:ing)?|\bac\b|a/c|air conditioning|hvac|furnace|"
    r"tv|television|speaker(?:s)?|music|media|fan(?:s)?)"
)
_DEVICE_NOUN_RE = re.compile(_DEVICE_NOUN, re.I)

_STATE_WORDS = (
    r"(?:on|off|open|opened|closed|locked|unlocked|playing|running|paused|"
    r"idle|heating|cooling|lit|back on|back off)"
)
_STATE_WORD_RE = re.compile(_STATE_WORDS, re.I)

_BARE_COMMAND_EXCLUDED = re.compile(r"\b(?:still|left|any|anything|currently)\b", re.I)
_BARE_COMMAND_RE = re.compile(
    rf"^(?:the )?(?:[a-z]+ )?{_DEVICE_NOUN} (on|off)(?: in the [a-z ]+)?$", re.I
)

_AUX_INITIAL_RE = re.compile(r"^(?:is|are|was|were|did|do|does|has|have)\b", re.I)

_WH_STATE_RE = re.compile(
    r"what'?(?:s| is| are)? the (?:state|status) of"
    r"|(?:what|which)\b.*\b(?:is|are)\b.*" + _STATE_WORDS
    + r"|how many\b.*\b(?:is|are)\b.*" + _STATE_WORDS,
    re.I,
)
_NOUN_FIRST_STATUS_RE = re.compile(rf"{_DEVICE_NOUN}(?:\s+\w+)?\s+(?:state|status)$", re.I)

_EMBEDDED_READ_RE = re.compile(
    r"\b(?:check|tell me|let me know|do you know|find out|see)\b\s*(?:if|whether|that)\b",
    re.I,
)

_ELLIPTIC_PATTERNS = (
    re.compile(r"\bany\s*(?:thing)?\s*(?:lights?)?\s*(?:left|still)\s+on\b", re.I),
    re.compile(r"\blights?\s+still\s+on\b", re.I),
    re.compile(r"what'?s\s+lit\b", re.I),
)

_PRONOUN_SUBJECT_RE = re.compile(r"\b(?:it|they|those|these|them)\b", re.I)

_NOUN_MAP = (
    (re.compile(r"\b(?:lights?|lamps?|lighting)\b", re.I), "light"),
    (re.compile(r"\b(?:switch(?:es)?|plugs?|outlets?)\b", re.I), "switch"),
    (re.compile(r"\b(?:lock(?:s)?|deadbolts?)\b", re.I), "lock"),
    (re.compile(r"\bdoors?\b.*\b(?:locked|unlocked)\b", re.I), "lock"),
    (
        re.compile(r"\b(?:garage|blinds|shades|curtains)\b", re.I),
        "cover",
    ),
    (re.compile(r"\bdoors?\b.*\b(?:open|opened|closed)\b", re.I), "cover"),
    (
        re.compile(
            r"\b(?:thermostat|heat|heating|ac|a/c|air conditioning|hvac|furnace)\b", re.I
        ),
        "climate",
    ),
    (re.compile(r"\b(?:tv|television|speaker|music|media|playing)\b", re.I), "media_player"),
    (re.compile(r"\bfan\b", re.I), "fan"),
)

_ROOM_RE = re.compile(
    rf"\bin the ([a-z]+(?:\s[a-z]+)?)\s+(?:{_DEVICE_NOUN})\b"
    rf"|\bthe ([a-z]+(?:\s[a-z]+)?)\s+(?:{_DEVICE_NOUN})\b"
    rf"|\b([a-z]+(?:\s[a-z]+)?)\s+(?:{_DEVICE_NOUN})\b"
    rf"|\bin the ([a-z]+(?:\s[a-z]+)?)$",
    re.I,
)
_ROOM_STOPWORDS = {"the", "a", "an", "any", "all", "some"}


def _normalize(query: str) -> tuple[str, bool]:
    q = (query or "").strip().lower()
    had_q = q.endswith("?")
    q = q.rstrip("?.! ").strip()
    while True:
        m = _LEADING_FILLER_RE.match(q)
        if not m:
            break
        q = q[m.end():]
    q = _WS_RE.sub(" ", q).strip()
    return q, had_q


def _strip_filler_adverbs(q: str) -> str:
    q = _FILLER_ADVERB_RE.sub("", q)
    return _WS_RE.sub(" ", q).strip()


def _split_clauses(q: str) -> list:
    parts = _CLAUSE_SPLIT_RE.split(q)
    return [p.strip() for p in parts if p.strip()]


def _strip_request_prefixes(clause: str) -> Optional[str]:
    """Strip one or more repeated request prefixes from the start of a
    clause. Returns the remainder if at least one prefix was stripped and
    the remainder begins with a control verb, else None."""
    remainder = clause
    stripped_any = False
    last_prefix = None
    changed = True
    while changed:
        changed = False
        for prefix in _REQUEST_PREFIXES:
            if remainder == prefix or remainder.startswith(prefix + " "):
                remainder = remainder[len(prefix):].strip()
                stripped_any = True
                last_prefix = prefix
                changed = True
                break
    if not stripped_any:
        return None
    if _CONTROL_VERB_RE.match(remainder):
        return remainder
    # "(can you) make sure X is/are <state>" (D2 ensure-state imperatives)
    # has no separate control verb -- the verb is the copula + state
    # predicate. Restricted to the "make sure" family: a generic "check
    # if X is locked" prefix must stay a read frame (D2 hard negative).
    if last_prefix in ("make sure", "can you make sure") and _ENSURE_STATE_RE.search(remainder):
        return remainder
    return None


def _clause_is_imperative_open(clause: str) -> bool:
    if _strip_request_prefixes(clause) is not None:
        return True
    if _GERUND_FRAME_RE.match(clause):
        return True
    if _CONTROL_VERB_RE.match(clause):
        return True
    return False


def _device_type_for(text: str) -> Optional[str]:
    for pattern, device_type in _NOUN_MAP:
        if pattern.search(text):
            return device_type
    return None


def _room_for(text: str) -> Optional[str]:
    m = _ROOM_RE.search(text)
    if not m:
        return None
    for group in m.groups():
        if group and group.strip() not in _ROOM_STOPWORDS:
            return group.strip()
    return None


def _target_state_for(text: str) -> Optional[str]:
    has_on = re.search(r"\bon\b", text) is not None
    has_off = re.search(r"\boff\b", text) is not None
    if has_on and not has_off:
        return "on"
    if has_off and not has_on:
        return "off"
    return None


def _needs_referent(text: str) -> bool:
    return bool(_PRONOUN_SUBJECT_RE.search(text)) and _DEVICE_NOUN_RE.search(text) is None


def classify_utterance(query: Optional[str]) -> UtteranceClassification:
    """Pure classifier. Never raises; None/empty -> UNKNOWN."""
    try:
        if not query or not query.strip():
            return UtteranceClassification(kind=UtteranceKind.UNKNOWN, rule="empty")

        q, had_q = _normalize(query)
        if not q:
            return UtteranceClassification(kind=UtteranceKind.UNKNOWN, rule="empty")

        # Rule 1a: elliptic existential, evaluated before filler-adverb
        # stripping and before rule 2 (bob r2 (3)).
        for pattern in _ELLIPTIC_PATTERNS:
            if pattern.search(q):
                return UtteranceClassification(
                    kind=UtteranceKind.STATE_QUESTION,
                    device_type=_device_type_for(q) or "light",
                    room=_room_for(q),
                    needs_referent=_needs_referent(q),
                    rule="elliptic_existential",
                )

        q2 = _strip_filler_adverbs(q)
        clauses = _split_clauses(q2) or [q2]
        first_clause = clauses[0]

        first_is_question_open = (
            _AUX_INITIAL_RE.match(first_clause) is not None
            and _strip_request_prefixes(first_clause) is None
            and _GERUND_FRAME_RE.match(first_clause) is None
        )

        # Rule 2: IMPERATIVE.
        if not had_q and not first_is_question_open and _is_bare_command(q2):
            return UtteranceClassification(
                kind=UtteranceKind.IMPERATIVE,
                device_type=_device_type_for(q2),
                room=_room_for(q2),
                target_state=_target_state_for(q2),
                rule="bare_command",
            )

        imperative_clause_hit = None
        if not first_is_question_open:
            for clause in clauses:
                if _clause_is_imperative_open(clause):
                    imperative_clause_hit = clause
                    break

        if imperative_clause_hit is not None:
            return UtteranceClassification(
                kind=UtteranceKind.IMPERATIVE,
                device_type=_device_type_for(q2),
                room=_room_for(q2),
                target_state=_target_state_for(q2),
                rule="imperative_clause",
            )

        # Rule 4: compound question + command -> UNKNOWN.
        if first_is_question_open and len(clauses) > 1:
            for clause in clauses[1:]:
                bare = _COMPOUND_CONNECTOR_RE.sub("", clause)
                if _CONTROL_VERB_RE.match(bare):
                    return UtteranceClassification(kind=UtteranceKind.UNKNOWN, rule="compound")

        # Rule 3: STATE_QUESTION.
        if _EMBEDDED_READ_RE.search(q2) and _STATE_WORD_RE.search(q2):
            return UtteranceClassification(
                kind=UtteranceKind.STATE_QUESTION,
                device_type=_device_type_for(q2),
                room=_room_for(q2),
                needs_referent=_needs_referent(q2),
                rule="embedded_read_frame",
            )

        if _WH_STATE_RE.search(q2) or _NOUN_FIRST_STATUS_RE.search(q2):
            return UtteranceClassification(
                kind=UtteranceKind.STATE_QUESTION,
                device_type=_device_type_for(q2),
                room=_room_for(q2),
                needs_referent=_needs_referent(q2),
                rule="wh_frame",
            )

        if first_is_question_open and _STATE_WORD_RE.search(q2):
            return UtteranceClassification(
                kind=UtteranceKind.STATE_QUESTION,
                device_type=_device_type_for(q2),
                room=_room_for(q2),
                needs_referent=_needs_referent(q2),
                rule="aux_initial",
            )

        if had_q and _DEVICE_NOUN_RE.search(q2) and _STATE_WORD_RE.search(q2) and not _CONTROL_VERB_RE.search(q2):
            return UtteranceClassification(
                kind=UtteranceKind.STATE_QUESTION,
                device_type=_device_type_for(q2),
                room=_room_for(q2),
                needs_referent=_needs_referent(q2),
                rule="ends_in_question_mark",
            )

        return UtteranceClassification(kind=UtteranceKind.UNKNOWN, rule="fallthrough")
    except Exception:
        return UtteranceClassification(kind=UtteranceKind.UNKNOWN, rule="error")


def _is_bare_command(q2: str) -> bool:
    if q2 in _FIXED_IMPERATIVE_IDIOMS:
        return True
    if _BARE_COMMAND_EXCLUDED.search(q2):
        return False
    return bool(_BARE_COMMAND_RE.match(q2))

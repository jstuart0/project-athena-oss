"""ATHENA-128 Phase 2 -- pure utterance classifier (D2, D3, D12).

Classifies a natural-language smart-home utterance into IMPERATIVE,
STATE_QUESTION, or UNKNOWN, without any I/O, LLM call, or knowledge of
Home Assistant entities. Consulted once per turn by
`orchestrator.nodes.route_control` and by `orchestrator.write_fanout`
(via `PermissionScope.utterance`).

R2-C3: stdlib only. Never import `orchestrator.main` or any sibling
module -- this file must be importable standalone. (The one exception is the
generic room vocabulary, read lazily from `room_context` and optional.)

Invariants: pure, never raises, and `None`/empty input classifies as
UNKNOWN.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, List, NamedTuple, Optional, Sequence, Tuple


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
    # The imperative clauses of the utterance, each running from its start to
    # the next clause boundary (None when there are none). Narration around a
    # command is not in it. See ``analyze_clauses``.
    command_text: Optional[str] = None
    # The utterance without leading okay/hey/name and a trailing name: what a
    # scene phrase is compared with as a whole utterance.
    core_text: Optional[str] = None
    # No command for now, but a command for later or on a condition ("in 5
    # minutes lock the door", "if it gets dark, turn on the porch light").
    deferred: bool = False


UNKNOWN_CLASSIFICATION = UtteranceClassification(kind=UtteranceKind.UNKNOWN, rule="kill_switch")

# Admin feature flag (inverted, seeded disabled): when enabled, the nodes
# that open an HA permission scope route as if every utterance were
# UNKNOWN (no read-only scope, legacy status gating).
STATE_QUESTION_KILL_SWITCH_FLAG = "state_question_routing_kill_switch"


# ---------------------------------------------------------------------------
# Vocabulary (D2, 2.2 rules 2-5)
# ---------------------------------------------------------------------------

# Cost bound (the classifier runs on the event loop before any permission
# check): only a prefix is classified, and every gap between two frame
# words is a bounded, clause-local run -- no unbounded `.*` chains.
MAX_CLASSIFIED_CHARS = 1000  # = room_context.SMART_HOME_MAX_QUERY_BYTES: the analysis sees all the extractor does
_GAP = r"[^?.,]{0,80}?"

_FILLER_WORDS = ("hey", "ok", "okay", "so", "um", "uh")
# The shipped assistant names; a deployment's configured name is added by
# the caller (route_control reads it from the assistant profile).
DEFAULT_ASSISTANT_NAMES = ("athena", "jarvis")
_FILLER_ADVERB_RE = re.compile(r"\b(?:currently|right now|at the moment|still|now)\b", re.I)
_WS_RE = re.compile(r"\s+")
_CLAUSE_SPLIT_RE = re.compile(r"\?|\.|,| and | then ")

_REQUEST_PREFIXES = (
    "is it possible to", "would it be possible to", "are you able to",
    "do you think you could", "do you think you can", "would you be able to",
    "can i get you to", "could i get you to",
    "i'd like you to", "i would like you to",
    "can you make sure", "go ahead and",
    "i want you to", "i need you to", "i said", "why don't you", "why dont you",
    "can you", "could you", "would you", "will you",
    "can we", "could we", "can i", "could i", "can u", "could u", "would u", "will u",
    "let us", "let's", "lets",
    "make sure",
    "please",
)

_CONTROL_VERBS = (
    "turn|switch|set|make|dim|brighten|lock|unlock|open|close|shut|start|"
    "stop|play|pause|resume|kill|cut|raise|lower|put|leave|keep|activate|run|"
    "deactivate|enable|disable|warm|heat|cool"
)
_CONTROL_VERB_RE = re.compile(rf"^(?:{_CONTROL_VERBS})\b", re.I)

_GERUND_VERBS = (
    "turning|switching|setting|locking|unlocking|opening|closing|shutting|"
    "dimming|playing|pausing|stopping|starting"
)
_GERUND_FRAME_RE = re.compile(rf"^(?:do you mind|would you mind)\s+(?:{_GERUND_VERBS})\b", re.I)
_ENSURE_STATE_RE = re.compile(
    rf"\b(?:is|are)\b{_GAP}\b(?:on|off|open|closed|locked|unlocked)\b", re.I
)
_COMPOUND_CONNECTOR_RE = re.compile(r"^(?:if so|so|then)\s+", re.I)

_FIXED_IMPERATIVE_IDIOMS = {"lock up", "lock it down", "lights out"}

_DEVICE_NOUN = (
    r"(?:lights?|lamp(?:s|ing)?|switch(?:es)?|plug(?:s)?|outlet(?:s)?|"
    r"lock(?:s)?|deadbolt(?:s)?|garage(?: door)?|blinds|shades|curtains|door(?:s)?|"
    r"thermostat(?:s)?|heat(?:ing)?|\bac\b|a/c|air conditioning|hvac|furnace|"
    r"tvs?|televisions?|speaker(?:s)?|music|media|fan(?:s)?)"
)
_DEVICE_NOUN_RE = re.compile(rf"\b{_DEVICE_NOUN}\b", re.I)  # whole words: "locked" is not a lock

_STATE_WORDS = (
    r"\b(?:on|off|open|opened|closed|locked|unlocked|playing|running|paused|"
    r"idle|heating|cooling|lit|back on|back off)\b"
)
_STATE_WORD_RE = re.compile(_STATE_WORDS, re.I)

_BARE_COMMAND_EXCLUDED = re.compile(r"\b(?:still|left|any|anything|currently)\b", re.I)
_BARE_COMMAND_RE = re.compile(
    rf"^(?:the )?(?:[a-z]+ )?{_DEVICE_NOUN} (on|off)(?: in the [a-z ]+)?$", re.I
)

_AUX_INITIAL_RE = re.compile(r"^(?:is|are|was|were|did|do|does|has|have)\b", re.I)
# Rule 3(a), past-tense check: "did I lock the back door" asks about a
# lock/cover state without a state word.
_PAST_ACTION_CHECK_RE = re.compile(
    r"^(?:did|have|has)\s+(?:i|you|we|they|someone|somebody|anyone|anybody)\s+"
    r"(?:lock|unlock|close|shut|open)\b",
    re.I,
)

_WH_STATE_RE = re.compile(
    r"what'?(?:s| is| are)? the (?:state|status) of"
    rf"|(?:what|which)\b{_GAP}\b(?:is|are)\b{_GAP}{_STATE_WORDS}"
    rf"|how many\b{_GAP}\b(?:is|are)\b{_GAP}{_STATE_WORDS}",
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
    (re.compile(rf"\bdoors?\b{_GAP}\b(?:locked|unlocked)\b", re.I), "lock"),
    (
        re.compile(r"\b(?:garage|blinds|shades|curtains)\b", re.I),
        "cover",
    ),
    (re.compile(rf"\bdoors?\b{_GAP}\b(?:open|opened|closed)\b", re.I), "cover"),
    (
        re.compile(
            r"\b(?:thermostat|heat|heating|ac|a/c|air conditioning|hvac|furnace)\b", re.I
        ),
        "climate",
    ),
    (re.compile(r"\b(?:tvs?|televisions?|speakers?|music|media|playing)\b", re.I), "media_player"),
    (re.compile(r"\bfan\b", re.I), "fan"),
)

# Rule 5, fallback: with no device noun, a lock/cover verb names the device
# ("did you unlock the front door", "did you close the front door").
_VERB_DEVICE_MAP = (
    (re.compile(r"\b(?:lock|unlock|locked|unlocked)\b", re.I), "lock"),
    (re.compile(r"\b(?:open|opened|close|closed|shut)\b", re.I), "cover"),
)

# Words that are both a room and a device noun ("the garage lights", "the
# media room lights"): when another device noun (or "room") follows, they
# name the room, not the device.
_ROOM_NOUN_MODIFIER_RE = re.compile(
    r"\b(?:media|music|tv|television)\s+rooms?\b"
    rf"|\bgarage\b(?=\s+(?!door\b){_DEVICE_NOUN}\b)",
    re.I,
)
_ROOM_CAPABLE_NOUNS = frozenset({"garage"})

# Rule 6 (room): the words between the utterance start or a determiner and
# a device noun, or after "in the". Candidates are filtered: leading
# determiners / possessives / quantifiers / wh-words / verbs are dropped
# ("any kitchen" -> "kitchen", "what" -> none) and so is a trailing door
# qualifier or device noun ("front door" -> none).
_ROOM_DETERMINERS = (
    "the", "a", "an", "any", "all", "some", "every", "each", "both",
    "my", "our", "your", "his", "her", "their", "this", "that", "these", "those",
)
_HEAD_NOUN = rf"{_DEVICE_NOUN}\b(?!\s+(?:{_DEVICE_NOUN}|rooms?)\b)"
_ROOM_FRAME_RES = (
    re.compile(rf"^((?:[a-z]+\s+){{1,2}}?){_HEAD_NOUN}", re.I),
    re.compile(
        rf"\b(?:{'|'.join(_ROOM_DETERMINERS)})\s+((?:[a-z]+\s+){{0,2}}?){_HEAD_NOUN}", re.I
    ),
    re.compile(rf"\bin the ([a-z]+(?:\s[a-z]+)?)(?:\s+{_DEVICE_NOUN}\b|$)", re.I),
)
_ROOM_STOPWORDS = frozenset(_ROOM_DETERMINERS) | {
    "what", "which", "whose", "how", "many", "much", "is", "are", "was", "were",
    "did", "do", "does", "has", "have", "i", "you", "we", "they", "it",
    "on", "off", "in", "of", "to", "up", "down", "left", "still", "other", "same",
} | set(_CONTROL_VERBS.split("|"))
_ROOM_TRAILING_QUALIFIERS = frozenset({"front", "back", "side", "main", "rear"})
_DEVICE_NOUN_WORD_RE = re.compile(_DEVICE_NOUN, re.I)


@functools.lru_cache(maxsize=32)
def _leading_filler_re(assistant_names: Tuple[str, ...]) -> "re.Pattern[str]":
    names = {n.strip().lower() for n in DEFAULT_ASSISTANT_NAMES + assistant_names if n and n.strip()}
    words = sorted(set(_FILLER_WORDS) | names, key=len, reverse=True)
    return re.compile(r"^(?:" + "|".join(re.escape(w) for w in words) + r")(?=[\s,]|$)[\s,]*", re.I)


def _bound(query: str) -> str:
    q = query.strip()
    if len(q) <= MAX_CLASSIFIED_CHARS:
        return q
    head = q[:MAX_CLASSIFIED_CHARS]
    return head.rsplit(" ", 1)[0] if " " in head else head


def _normalize(query: str, assistant_names: Tuple[str, ...] = ()) -> tuple[str, bool]:
    q = _bound(query).lower()
    had_q = q.endswith("?")
    q = q.rstrip("?.! ").strip()
    filler_re = _leading_filler_re(assistant_names)
    while True:
        m = filler_re.match(q)
        if not m or m.end() == 0:
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


# "can I get the lights off": a first-person request with "get" as its verb.
_GET_REQUEST_RE = re.compile(r"^(?:can|could|may) (?:i|we|u) (?:please )?get\b.*\b(?:on|off|up|down|brighter|dimmer)\b")
# "have the lights turn on at sunset": a request that someone or something do it, not a question.
_CAUSATIVE_RE = re.compile(
    r"^have (?:the|my|our|all|every)\b.*\b(?:turn|come|go|switch|run|dim|lock|unlock|open|close)\b"
)


def _clause_is_imperative_open(clause: str) -> bool:
    if _strip_request_prefixes(clause) is not None or _GET_REQUEST_RE.match(clause):
        return True
    if _GERUND_FRAME_RE.match(clause):
        return True
    if _CONTROL_VERB_RE.match(clause):
        return True
    return False


def _device_type_for(text: str) -> Optional[str]:
    nouns_text = _ROOM_NOUN_MODIFIER_RE.sub(" ", text)
    for pattern, device_type in _NOUN_MAP:
        if pattern.search(nouns_text):
            return device_type
    for pattern, device_type in _VERB_DEVICE_MAP:
        if pattern.search(nouns_text):
            return device_type
    return None


def _room_candidate(words: str) -> Optional[str]:
    tokens = words.split()
    while tokens and tokens[0] in _ROOM_STOPWORDS:
        tokens.pop(0)
    while tokens and (
        tokens[-1] in _ROOM_TRAILING_QUALIFIERS
        or (_DEVICE_NOUN_WORD_RE.fullmatch(tokens[-1]) and tokens[-1] not in _ROOM_CAPABLE_NOUNS)
    ):
        tokens.pop()
    if not tokens or any(t in _ROOM_STOPWORDS for t in tokens):
        return None
    return " ".join(tokens)


def _room_for(text: str) -> Optional[str]:
    for pattern in _ROOM_FRAME_RES:
        for m in pattern.finditer(text):
            words = m.group(1) or ""
            # "garage door" is one device, not a room plus a door.
            if text[m.end(1):m.end()].lower().startswith("door") and words.split()[-1:] == ["garage"]:
                words = words.rsplit("garage", 1)[0]
            room = _room_candidate(words)
            if room:
                return room
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


# ---------------------------------------------------------------------------
# Clause analysis (T3): which words of an utterance are a command
#
# Speech reaches the assistant with background talk around the request. The
# analysis cuts the text into clauses at fixed boundaries, decides for each
# clause whether it is a command (its first content word is a control verb and
# no guard applies), narration (a statement about someone or something) or a
# condition, and keeps the command clauses as ``command_text``. A cue for a
# write is read from those words only. Pure, stdlib only, never raises.
# ---------------------------------------------------------------------------

_HARD_BOUNDARIES = frozenset({".", "!", "?", ";", "\n"})
_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?%?|[a-z][a-z0-9]*(?:['’][a-z]+)*|[,.;!?\n]|[\"“”]")
_QUOTE_SPAN_RE = re.compile(r"\"([^\"]*)\"|“([^”]*)”|(?<!\w)'([^']*\s[^']*)'(?!\w)")
_I_SAID_BEFORE_RE = re.compile(r"\bi said[,:]?\s*$")
_WORD_RE = re.compile(r"\w+")

_MARKER_WORDS = frozenset({"okay", "ok", "alright", "hey"})
# "is it ok to turn it off", "would it be alright": ok after these is part of the question.
_BEFORE_OK_WORDS = frozenset({"is", "be", "it", "it's", "are", "was", "not", "am"})
# "but" is bound to a name so no collection here lists a word of the exclusion
# vocabulary (tests/unit/test_exclusion_vocabulary.py); it is a clause joiner here.
_BUT = "but"
_CONJUNCTIONS = frozenset({"and", "then", "also", "plus", _BUT})
_LEADING_FILLERS = frozenset({
    "just", "now", "kindly", "also", "then", "so", _BUT, "and", "plus", "well", "oh", "um", "uh",
    "yes", "yeah", "yep", "sure", "thanks", "actually",
})
_NEGATOR_STARTS = frozenset({"don't", "dont", "never", "no", "not", "won't", "wont", "can't", "cannot", "shouldn't"})
_SUBORDINATORS = frozenset({
    "if", "when", "whenever", "because", "that", "while", "unless", "what", "how", "where", "why",
    "once", "since", "although", "though", "whether",
})
_CONDITIONAL_LEADS = frozenset({"if", "when", "whenever", "once", "unless"})
_TIME_SUBORDINATORS = frozenset({"before", "after", "until"})
_REPORT_VERBS = frozenset({
    "say", "says", "said", "saying", "tell", "tells", "told", "ask", "asks", "asked",
    "yell", "yells", "yelled", "shout", "shouts", "shouted", "whisper", "whispers", "whispered",
    "reply", "replies", "replied", "announce", "announces", "announced", "repeat", "repeats", "repeated",
    "read", "reads", "wrote", "write", "writes", "sing", "sings", "sang", "goes", "went",
})
_QUOTATIVE_LIKE_PRECEDERS = frozenset({"was", "were", "is", "am", "are"})
_COMPOUND_SUBJECT_JOINERS = frozenset({"and", "or", "with", "&"})
_PRONOUN_SUBJECTS = frozenset({
    "i", "we", "he", "she", "they", "it", "you", "someone", "somebody", "everyone", "everybody",
    "people", "anyone", "anybody", "nobody",
})
_NARRATIVE_SUBJECTS = frozenset({
    "we", "he", "she", "they", "you", "someone", "somebody", "everyone", "everybody", "people",
    "anyone", "anybody", "nobody",
})
_DETERMINERS = frozenset(_ROOM_DETERMINERS) - {"any", "all", "some", "every", "each", "both"}
_FINITE_VERBS = frozenset({
    "is", "are", "was", "were", "keep", "keeps", "leave", "leaves", "left", "set", "sets", "turn", "turns",
    "turned", "has", "have", "had", "will", "usually", "always", "never", "get", "gets", "got", "say",
    "says", "said", "want", "wants", "need", "needs", "love", "loves", "look", "looks", "seem", "seems",
    "think", "thinks", "come", "comes", "go", "goes", "went", "makes", "like", "likes",
})
_DETERMINER_VERB_WINDOW = 6
_MODAL_STARTERS = frozenset({"can", "could", "will", "would", "should"})
# "can we dim the lights", "could I get the lights off": the speaker asks, so it is no question about others.
_MODAL_ASKERS = frozenset({"you", "u", "ya", "we", "i", "me", "us"})
_QUESTION_STARTERS = frozenset({
    "is", "are", "was", "were", "did", "do", "does", "has", "have", "can", "could", "will", "would", "should",
})
_TIME_LEAD_WORDS = frozenset({
    "every", "each", "all", "night", "nights", "day", "days", "morning", "evening", "afternoon", "tonight",
    "today", "yesterday", "usually", "always", "sometimes", "often", "normally", "later", "earlier", "last",
    "this", "next", "when",
})
_COPULAS = frozenset({"is", "are", "was", "were", "am"})
_COPULA_CONTRACTION_BASES = frozenset({"it", "that", "there", "here", "he", "she", "what", "who", "how", "where"})
_EVALUATION_WORDS = frozenset({"nice", "great", "love", "loves", "looks", "seems", "pretty", "cool", "lovely"})
_COLOUR_BRIGHTNESS_WORDS = frozenset({
    "red", "orange", "yellow", "green", "blue", "purple", "pink", "white", "warm", "cool", "cyan",
    "magenta", "violet", "gold", "amber", "teal", "turquoise", "bright", "brighter", "brightest", "dim",
    "dimmer", "dimmest", "low", "lower", "high", "higher", "full", "max", "maximum", "minimum",
})
_ENVIRONMENT_WORDS = frozenset({"dark", "bright", "dim", "cold", "hot", "warm", "chilly", "stuffy", "freezing"})
_ENVIRONMENT_ADVERBS = frozenset({
    "too", "so", "really", "very", "kind", "of", "a", "bit", "little", "quite", "rather", "pretty",
})
_DESIRE_VERBS = frozenset({"want", "need"})
# "I want to watch Netflix": a request for media, though not a control verb.
_MEDIA_REQUEST_VERBS = frozenset({"watch", "listen"})
_REQUEST_OBJECT_WORDS = frozenset({"on", "off", "it", "them", "everything", "all", "some"})
_STRONG_VERBS = frozenset({
    "turn", "switch", "dim", "brighten", "lock", "unlock", "shut", "kill", "raise", "lower", "activate",
    "deactivate", "enable", "disable", "start", "stop", "pause", "resume", "play",
})
_SUBJECT_PARTICLES = frozenset({"and", "then", "also", "plus", "please", "to", "off", "on", "up", "down", "them", "it"})


class _Tok(NamedTuple):
    text: str
    start: int
    end: int
    quoted: bool


class Clause(NamedTuple):
    """One clause of an utterance. ``imperative``: a command. ``narration``:
    a statement about someone or something, or a would-be command a guard
    refuses (reported speech, quotation, a narrative lead-in, a subject before
    the verb). ``conditional``: a condition ("if it gets dark") or a clause
    that follows one. ``cue_ok``: its words may count as the cue for a write."""

    start: int
    end: int
    words: Tuple[str, ...]
    sep: str
    imperative: bool
    narration: bool
    conditional: bool
    negated: bool
    empty: bool
    cue_ok: bool
    # A command that is not for now: a command after a conditional lead-in, or
    # a condition that carries the command ("when it gets dark turn on the
    # porch light"). Not narration, but not an immediate request either.
    deferred: bool = False
    # A command led by a time ("in 5 minutes lock the door", "at 11 turn off the
    # lights"): never for now, so never an immediate write; a sequence plans it.
    scheduled: bool = False


class ClauseAnalysis(NamedTuple):
    text: str
    clauses: Tuple[Clause, ...]
    command_text: Optional[str]


def _tokenize(low: str) -> List[_Tok]:
    spans = []
    for m in _QUOTE_SPAN_RE.finditer(low):
        inner = next((g for g in m.groups() if g is not None), "")
        if len(_WORD_RE.findall(inner)) >= 2 and not _I_SAID_BEFORE_RE.search(low[:m.start()]):
            spans.append((m.start(), m.end()))
    out: List[_Tok] = []
    for m in _TOKEN_RE.finditer(low):
        text = m.group().replace("’", "'")
        if text in ('"', "“", "”"):
            continue
        out.append(_Tok(text, m.start(), m.end(), any(a <= m.start() < b for a, b in spans)))
    return out


def _sentences(toks: Sequence[_Tok]) -> List[List[_Tok]]:
    out: List[List[_Tok]] = [[]]
    for t in toks:
        if t.text in _HARD_BOUNDARIES:
            out.append([])
        else:
            out[-1].append(t)
    return [s for s in out if s]


@functools.lru_cache(maxsize=32)
def _name_sequences(names: Tuple[str, ...]) -> Tuple[Tuple[str, ...], ...]:
    seqs = {tuple(_TOKEN_RE.findall(n.strip().lower())) for n in DEFAULT_ASSISTANT_NAMES + names if n and n.strip()}
    return tuple(sorted((s for s in seqs if s), key=len, reverse=True))


def _filler_count(words: Sequence[str]) -> int:
    """How many leading words are interjections or joiners ("yeah", "thanks",
    "thank you", "actually", "and then")."""
    i = 0
    while i < len(words):
        if words[i] in _LEADING_FILLERS:
            i += 1
        elif words[i] == "thank" and list(words[i + 1:i + 2]) == ["you"]:
            i += 2
        else:
            break
    return i


def _content(words: Sequence[str]) -> List[str]:
    return list(words[_filler_count(words):])


_SCHEDULE_LEADS = frozenset({"at", "in", "by"})  # "in 5 minutes lock the door", "at 11 turn off the lights"


def _unwrap_time_phrase(content: List[str]) -> Tuple[List[str], bool]:
    """"before bed turn off the lights", "in 5 minutes lock the door": a time
    phrase of up to three words before a control verb is part of the command. Returns the words from the
    verb on and whether a time phrase was there."""
    if content and (content[0] in _TIME_SUBORDINATORS or content[0] in _SCHEDULE_LEADS):
        for k in (2, 3, 4):
            if len(content) > k and _CONTROL_VERB_RE.match(content[k]):
                return content[k:], True
    return content, False


def _starts_negated(content: Sequence[str]) -> bool:
    if not content:
        return False
    return content[0] in _NEGATOR_STARTS or (content[0] == "do" and content[1:2] == ["not"])


def _imperative_shape(content: Sequence[str]) -> bool:
    """Whether the clause's first content word is a control verb (after the
    request openers and the time phrase). No guard is applied here."""
    core, _ = _unwrap_time_phrase(list(content))
    return bool(core) and (_clause_is_imperative_open(" ".join(core)) or _room_first_command(core))


_room_words_cache: Optional[frozenset] = None
_room_words_failure_logged = False


def _room_words() -> frozenset:
    """The words of the generic room vocabulary (``room_context.COMMON_ROOM_WORDS``,
    the one place room words live), read at call time so this module keeps
    importing on its own. A failed read is logged once (the error type only) and
    retried on the next call, never cached: with the vocabulary unavailable no
    word names a room, which refuses a room-first command rather than guessing
    one. A house's own room names are not here; a short phrase ending in a
    device word stands in for them (see ``_leading_phrase_lengths``)."""
    global _room_words_cache, _room_words_failure_logged
    if _room_words_cache is not None:
        return _room_words_cache
    try:
        from orchestrator.room_context import COMMON_ROOM_WORDS
    except Exception as exc:
        if not _room_words_failure_logged:
            _room_words_failure_logged = True
            import logging
            logging.getLogger(__name__).warning("room_vocabulary_unavailable error_type=%s", type(exc).__name__)
        return frozenset()
    _room_words_cache = frozenset(w for phrase in COMMON_ROOM_WORDS for w in phrase.split())
    return _room_words_cache


_ROOM_FIRST_VERBS = frozenset({"turn", "switch", "dim", "brighten", "set"})
_ROOM_FIRST_PARTICLES = frozenset({"on", "off", "up", "down"})
_ROOM_FIRST_JOINERS = frozenset({"and", "then"})  # "porch light on and set it to 50%"
# Anything that makes the clause a schedule, a condition or a thing that happens by itself.
_SCHEDULE_OR_CONDITION_RE = re.compile(
    r"\b(?:when|whenever|after|before|because|if|at|every|until|while|once|unless|"
    r"by itself|by themselves|on its own|on their own|all night|all day)\b"
)
_SELF_ACTING_RE = re.compile(
    r"\b(?:by itself|by themselves|on its own|on their own|all night|every night|every day|at night|"
    r"all day|every morning|every evening)\b"
)
_LEVEL_RE = re.compile(r"\d{1,3}%?")
_OBJECT_DETERMINERS = frozenset({"the", "all", "my", "our", "those", "these", "it", "them"})


def _is_device_word(w: str) -> bool:
    return w in _room_words() or w in _COLOUR_BRIGHTNESS_WORDS or _DEVICE_NOUN_RE.fullmatch(w) is not None


def _room_noun_phrase_length(ws: Sequence[str]) -> int:
    """How many leading words are a room, a device or a colour with no
    determiner ("kitchen lights", "living room lamp", "porch light")."""
    n = 0
    while n < min(len(ws), 4) and _is_device_word(ws[n]):
        n += 1
    return n


def _plain_name_word(w: str) -> bool:
    """A word that could be part of a house's own room name: no possessive, no
    pronoun or determiner, nothing that reads as a person doing something."""
    return (
        "'" not in w and not w[0].isdigit() and w not in _PRONOUN_SUBJECTS and w not in _DETERMINERS
        and w not in _NARRATIVE_SUBJECTS and w not in _FINITE_VERBS and w not in _THIRD_PERSON_CONTROL_FORMS
        and w not in _LEADING_FILLERS and w not in _SUBORDINATORS and not _CONTROL_VERB_RE.match(w)
    )


def _leading_phrase_lengths(ws: Sequence[str]) -> List[int]:
    """The lengths a leading room phrase may have: the generic vocabulary's, and
    a house's own name, a plain phrase of at most three words that ends in a
    device word ("passage light", "master closet light")."""
    lengths = []
    n = _room_noun_phrase_length(ws)
    if n:
        lengths.append(n)
    for k in (1, 2, 3):
        if k <= len(ws) and _DEVICE_NOUN_RE.fullmatch(ws[k - 1]) and all(_plain_name_word(w) for w in ws[:k - 1]):
            if k not in lengths:
                lengths.append(k)
    return lengths


def _skip_object(rest: List[str]) -> List[str]:
    """An optional object after "turn off": "the lights", "all the kitchen lights", "it"."""
    if rest and rest[0] in _OBJECT_DETERMINERS:
        rest = rest[1:]
        if rest and rest[0] in _OBJECT_DETERMINERS:
            rest = rest[1:]
    k = 0
    while k < min(len(rest), 3) and _is_device_word(rest[k]):
        k += 1
    return rest[k:]


def _closed_command_tail(rest: List[str]) -> Optional[List[str]]:
    """What follows a room phrase's command, or None when it is not one of the
    closed forms: turn/switch + on|off|up|down; set + to + a number, N% or a
    colour; dim/brighten + optional up|down|it|them|to N."""
    verb, tail = rest[0], rest[1:]
    if verb in ("turn", "switch"):
        return _skip_object(tail[1:]) if tail[:1] and tail[0] in _ROOM_FIRST_PARTICLES else None
    if verb == "set":
        if tail[:1] == ["to"] and len(tail) > 1 and (_LEVEL_RE.fullmatch(tail[1]) or tail[1] in _COLOUR_BRIGHTNESS_WORDS):
            tail = tail[2:]
            return tail[1:] if tail[:1] == ["percent"] else tail
        return None
    if verb in ("dim", "brighten"):
        if tail[:1] and tail[0] in ("up", "down", "it", "them"):
            return _skip_object(tail[1:])
        if tail[:1] == ["to"] and len(tail) > 1 and _LEVEL_RE.fullmatch(tail[1]):
            tail = tail[2:]
            return tail[1:] if tail[:1] == ["percent"] else tail
        return _skip_object(tail)
    return None


def _closed_rest(tail: Sequence[str]) -> bool:
    """Nothing left, "please" or "now", or and/then and another command."""
    if not tail or list(tail) in (["please"], ["now"], ["now", "please"]):
        return True
    return tail[0] in _ROOM_FIRST_JOINERS and len(tail) > 1 and _imperative_shape(list(tail[1:]))


def _room_first_command(ws: Sequence[str]) -> bool:
    """A command that names its room or device first: "kitchen lights set to
    50 percent", "bedroom lights turn off", "kitchen turn off the lights",
    "living room lamp turn on", "passage light turn on", "porch light on and
    set it to 50%". The tail is closed (see ``_closed_command_tail``); a time,
    a condition or "by itself" anywhere in the clause makes it something
    else. "The kitchen lights keep turning on" (a determiner) is narration, and
    "lights off at ten" (a schedule) is left as it is."""
    text = " ".join(ws)
    if _SCHEDULE_OR_CONDITION_RE.search(text):
        return False
    for n in _leading_phrase_lengths(ws):
        if n >= len(ws):
            continue
        rest = list(ws[n:])
        if rest[0] in ("on", "off"):
            # Only as the first half of two commands. "Office lights off please" stays
            # the non-imperative request it always was (its fan-out limits still apply).
            if len(rest) > 2 and rest[1] in _ROOM_FIRST_JOINERS and _imperative_shape(rest[2:]):
                return True
            continue
        if rest[0] in _ROOM_FIRST_VERBS:
            tail = _closed_command_tail(rest)
            if tail is not None and _closed_rest(tail):
                return True
    return False


def _device_first_schedule_request(ws: Sequence[str]) -> bool:
    """"porch light turn on whenever someone walks by", "upstairs lights turn off
    at ten": a room or device first, a command verb, and a schedule or condition.
    An automation to set up, not narration and not for now."""
    text = " ".join(ws)
    if not _SCHEDULE_OR_CONDITION_RE.search(text) or _SELF_ACTING_RE.search(text):
        return False
    return any(n < len(ws) and ws[n] in _ROOM_FIRST_VERBS for n in _leading_phrase_lengths(ws))


def _split_sentence(toks: Sequence[_Tok], seqs: Tuple[Tuple[str, ...], ...]) -> List[Tuple[List[_Tok], str]]:
    """The sentence's clauses with how each began (``start``, ``conj``,
    ``comma``, ``marker`` or ``named``: a marker followed by an assistant name)."""
    rank = {"start": 0, "conj": 1, "comma": 2, "marker": 3, "named": 4}
    out: List[Tuple[List[_Tok], str]] = []
    cur: List[_Tok] = []
    cur_sep = "start"
    next_sep = "start"
    after_marker = False

    def close() -> None:
        nonlocal cur
        if cur:
            out.append((cur, cur_sep))
        cur = []

    def boundary(kind: str) -> None:
        nonlocal next_sep
        close()
        next_sep = kind if rank[kind] >= rank[next_sep] or next_sep == "start" else next_sep

    words = [t.text for t in toks]
    i, n = 0, len(toks)
    while i < n:
        w = words[i]
        if w == ",":
            boundary("comma")
            after_marker = False
            i += 1
            continue
        matched = next((len(s) for s in seqs if tuple(words[i:i + len(s)]) == s), 0)
        if matched:
            boundary("named" if after_marker else "marker")
            after_marker = False
            i += matched
            continue
        if (w in _MARKER_WORDS and not (cur and words[i - 1] in _BEFORE_OK_WORDS)) or (
            w == "all" and words[i + 1:i + 2] == ["right"]
        ):
            boundary("marker")
            after_marker = True
            i += 2 if w == "all" else 1
            continue
        if w in _CONJUNCTIONS or (w == "so" and not cur):
            if not cur:
                i += 1
                continue
            if w != "so" and _imperative_shape(_content([t.text for t in cur])):
                boundary("conj")
                i += 2 if (w == "and" and words[i + 1:i + 2] == ["then"]) else 1
                after_marker = False
                continue
        if not cur:
            cur_sep = next_sep
            next_sep = "start"
        cur.append(toks[i])
        after_marker = False
        i += 1
    close()
    return out


def _is_subject_i(words: Sequence[str], index: int) -> bool:
    """Whether the word before ``words[index]`` is exactly the speaker, "I"."""
    if index < 1 or words[index - 1] != "i":
        return False
    return not (index >= 2 and words[index - 2] in _COMPOUND_SUBJECT_JOINERS)


def _reports_speech(words: Sequence[str]) -> bool:
    """G3: a report or quotative verb whose subject is not exactly "I", anywhere
    in the sentence. A verb with no subject before it is an instruction ("tell
    me a joke") and reports nothing."""
    for i, w in enumerate(words):
        quotative_like = w == "like" and i > 0 and (
            words[i - 1] in _QUOTATIVE_LIKE_PRECEDERS or words[i - 1].endswith(("'s", "'m", "'re"))
        )
        if w not in _REPORT_VERBS and not quotative_like:
            continue
        prev = words[i - 1] if i else None
        if prev is None or prev in _LEADING_FILLERS or prev in _MARKER_WORDS or prev in ("please", "to", ","):
            continue
        if prev == "you" and i >= 2 and words[i - 2] in ("can", "could", "would", "will"):
            continue
        if _is_subject_i(words, i):
            continue
        return True
    return False


def _environment_form(ws: Sequence[str]) -> bool:
    """"it's too dark in here", "I'm cold", "we are freezing": an environment
    word after a copula. Not narration; it keeps the cue rules it always had."""
    if not ws:
        return False
    first = ws[0]
    base = first.split("'")[0]
    if base not in ("it", "i", "we"):
        return False
    rest = list(ws[1:])
    if "'" not in first:
        if not rest or rest[0] not in _COPULAS:
            return False
        rest = rest[1:]
    elif first.split("'", 1)[1] not in ("s", "m", "re"):
        return False
    while rest and rest[0] in _ENVIRONMENT_ADVERBS:
        rest = rest[1:]
    return bool(rest) and rest[0] in _ENVIRONMENT_WORDS


def _request_form(ws: Sequence[str]) -> bool:
    """"I want the lights blue", "we'd like the kitchen on", "I need to turn
    off the lights": a first-person request with an object, or "to" and a
    control verb. "Wish", and "want to know/see/check", are not requests."""
    base = ws[0].split("'")[0] if ws else ""
    if base not in ("i", "we"):
        return False
    rest = list(ws[1:])
    if "'" in ws[0]:
        suffix = ws[0].split("'", 1)[1]
        if suffix not in ("d", "ll"):
            return False
    elif rest and rest[0] == "would":
        rest = rest[1:]
    # "like" is a request only as "I'd like" / "I would like"; "I like the lights blue" is a taste.
    would_like = "'d" in ws[0] or (len(ws) > 1 and ws[1] == "would")
    if not rest or not (rest[0] in _DESIRE_VERBS or (rest[0] == "like" and would_like)):
        return False
    rest = rest[1:]
    if not rest:
        return False
    if rest[0] == "to":
        return len(rest) > 1 and (bool(_CONTROL_VERB_RE.match(rest[1])) or rest[1] in _MEDIA_REQUEST_VERBS)
    obj = rest[0]
    return (
        obj in _DETERMINERS or obj in _REQUEST_OBJECT_WORDS or obj in _COLOUR_BRIGHTNESS_WORDS
        or _DEVICE_NOUN_RE.fullmatch(obj) is not None
    )


_FIRST_PERSON_NEGATORS = frozenset({
    "can't", "cannot", "don't", "dont", "won't", "doesn't", "didn't", "wouldn't", "couldn't",
})


def _negated_first_person(ws: Sequence[str]) -> bool:
    """"I can't see", "I can't sleep turn off the lights", "you don't need to
    wait turn on the lights": a first-person statement of need. It is no
    narration; the negation rules of the write cue decide what it still asks."""
    base = ws[0].split("'")[0] if ws else ""
    if base not in ("i", "we", "you") or len(ws) < 2:
        return False
    return ws[1] in _FIRST_PERSON_NEGATORS or (ws[1] == "do" and ws[2:3] == ["not"])


def _copula_lead(ws: Sequence[str]) -> bool:
    """The clause's verb is a copula: "we're home", "you are right", "I'm back"."""
    first = ws[0]
    if "'" in first and first.split("'", 1)[1] in ("s", "m", "re"):
        return True
    return len(ws) > 1 and ws[1] in _COPULAS


_THIRD_PERSON_CONTROL_FORMS = frozenset({
    "turns", "switches", "dims", "puts", "sets", "plays", "starts", "stops", "locks", "opens", "closes",
    "leaves", "keeps", "makes", "wants", "needs", "likes", "watches", "pauses", "raises", "lowers",
})
_BARE_SUBJECT_WINDOW = 4


def _bare_subject_narration(ws: Sequence[str]) -> bool:
    """A name or a bare plural before a finite verb ("Sarah wants the lights
    blue", "Mike turns the lights off", "kids keep the lights on"). A first word
    that is a device, a colour, an on/off word, a filler, a time or a control
    verb is not a subject."""
    first = ws[0]
    if (
        first in _LEADING_FILLERS or first == "please" or first in _COLOUR_BRIGHTNESS_WORDS
        or first in _TIME_WORDS or first[0].isdigit()
        or first in ("on", "off") or _DEVICE_NOUN_RE.fullmatch(first) or _CONTROL_VERB_RE.match(first)
    ):
        return False
    return any(
        (w in _FINITE_VERBS and w not in _COPULAS) or w in _THIRD_PERSON_CONTROL_FORMS
        for w in ws[1:1 + _BARE_SUBJECT_WINDOW]
    )


_SCHEDULE_MARKERS = frozenset({
    "at", "until", "by", "when", "whenever", "every", "sunset", "sunrise", "dusk", "dawn", "after", "before",
})
_REQUEST_MODALS = frozenset({"should", "must", "will", "needs", "ought"})


def _device_subject_request(ws: Sequence[str]) -> bool:
    """"the porch light should come on at sunset": a device (not a person) as
    the subject of a modal, with a schedule or condition. A request to set up
    an automation, not narration; and not for now."""
    if not ws or ws[0] not in _DETERMINERS:
        return False
    n = _room_noun_phrase_length(ws[1:])
    if n == 0 or len(ws) <= n + 1 or ws[n + 1] not in _REQUEST_MODALS:
        return False
    return any(w in _SCHEDULE_MARKERS for w in ws[n + 2:])


_DEVICE_FIRST_VERBS = _FINITE_VERBS | _THIRD_PERSON_CONTROL_FORMS | frozenset({"go", "goes", "come", "comes"})


def _device_first_narration(ws: Sequence[str]) -> bool:
    """A room or device first, then a present-tense verb that is no command
    ("lights go off at ten", "tv turns on by itself"). "Lights off at ten" has
    no verb and is a schedule."""
    n = _room_noun_phrase_length(ws)
    if n == 0:
        return False
    return any(w in _DEVICE_FIRST_VERBS and w not in _COPULAS for w in ws[n:n + 3])


def _determiner_narration(ws: Sequence[str]) -> bool:
    if not ws or ws[0] not in _DETERMINERS:
        return False
    window = ws[1:1 + _DETERMINER_VERB_WINDOW]
    return any(w in _FINITE_VERBS or w.endswith("'ll") for w in window)


def _evaluation_narration(ws: Sequence[str]) -> bool:
    for i, w in enumerate(ws):
        if w in _EVALUATION_WORDS:
            nxt = ws[i + 1] if i + 1 < len(ws) else ""
            if nxt not in _COLOUR_BRIGHTNESS_WORDS:
                return True
    return False


_TIME_WORDS = frozenset({
    "at", "in", "by", "around", "for", "every", "each", "tomorrow", "tonight", "later", "noon", "midnight",
    "am", "pm", "a", "an", "minute", "minutes", "second", "seconds", "hour", "hours", "morning", "evening",
    "afternoon", "night", "sunset", "sunrise", "o'clock", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "next", "another",
})


def _subject_before_strong_verb(ws: Sequence[str]) -> bool:
    """A word or two before a verb that opens a command ("kids turn off the
    lights"): a subject, so not a command. A time or delay ("at 11 turn off",
    "in five minutes dim") is no subject."""
    for i in (1, 2, 3):
        if i < len(ws) and ws[i] in _STRONG_VERBS:
            before = ws[:i]
            if any(w in _SUBJECT_PARTICLES for w in before):
                return False
            return any(not (w in _TIME_WORDS or w[0].isdigit()) for w in before)
    return False


def _statement_kind(content: Sequence[str]) -> Tuple[bool, bool, bool]:
    """The statement table for a clause that is not a command:
    ``(narration, conditional, narrative_lead)``. Narration is a statement
    about someone or something; conditional is a subordinate clause;
    ``narrative_lead`` says the narration has a subject other than I, it or
    there with a finite verb (G4)."""
    ws = [w for w in content]
    if not ws:
        return False, False, False
    first = ws[0]
    if first in _SUBORDINATORS or (first in _TIME_SUBORDINATORS and not _unwrap_time_phrase(ws)[1]):
        return False, True, False
    if first in _MODAL_STARTERS and len(ws) > 1 and ws[1] not in _MODAL_ASKERS:
        return True, False, False  # "can the kids turn off the lights": a question about others
    if first in _QUESTION_STARTERS:
        return False, False, False  # a question: the state-question rules own it
    if _reports_speech(ws):
        return True, False, False
    lead_in = 0
    while lead_in < 3 and lead_in < len(ws) - 1 and ws[lead_in] in _TIME_LEAD_WORDS:
        lead_in += 1
    if lead_in:
        ws, first = ws[lead_in:], ws[lead_in]
        if first.split("'")[0] not in _NARRATIVE_SUBJECTS:
            ws, first = [w for w in content], content[0]
    base = first.split("'")[0]
    if base in _PRONOUN_SUBJECTS:
        if _environment_form(ws) or _request_form(ws) or _negated_first_person(ws):
            return False, False, False
        # A copula lead-in ("we're home, turn on the lights") is no narrative lead (G4).
        return True, False, base in _NARRATIVE_SUBJECTS and not _copula_lead(ws)
    if _device_first_schedule_request(ws):
        return False, True, False  # "porch light turn on whenever someone walks by": an automation to set up
    if _device_subject_request(ws):
        return False, True, False  # "the porch light should come on at sunset": an automation to set up
    if _determiner_narration(ws):
        # A copula lead-in ("the porch is lit, turn on the hall light") is no
        # narrative lead (G4); a finite verb of doing is.
        window = ws[1:1 + _DETERMINER_VERB_WINDOW]
        return True, False, any(
            (w in _FINITE_VERBS and w not in _COPULAS) or w.endswith("'ll") for w in window
        )
    # A copula first is a question ("are the lights on"): the state-question
    # rules own it, not the narration rules.
    if any(w in _COPULAS for w in ws[1:]) or any(
        w.endswith("'s") and w.split("'")[0] in _COPULA_CONTRACTION_BASES for w in ws
    ):
        return True, False, False
    if _evaluation_narration(ws):
        return True, False, False
    if _subject_before_strong_verb(ws):
        return True, False, True
    if _bare_subject_narration(ws) or _device_first_narration(ws):
        return True, False, True
    return False, False, False


_QUESTION_WORDS = frozenset({"what", "how", "where", "why", "whether"})


def _automation_shaped(content: Sequence[str]) -> bool:
    """A condition with its command in it ("when motion is detected in the hall
    turn on the lights"): a control verb after the condition, and no subject
    and verb after that command ("if I turn off the lights it flickers" is a
    statement about the lights, and a question word opens a question)."""
    if _device_subject_request(content) or _device_first_schedule_request(content):
        return True
    if not content or content[0] in _QUESTION_WORDS:
        return False
    verbs = [i for i, w in enumerate(content) if i >= 2 and _CONTROL_VERB_RE.match(w)]
    if not verbs:
        return False
    return not any(w.split("'")[0] in _PRONOUN_SUBJECTS for w in content[verbs[-1] + 1:])


def statement_clause(tokens: Sequence[str]) -> bool:
    """Whether the clause's words are a statement (narration or a subordinate
    clause) and not a command or a first-person request. Only meaningful for
    one clause; pure."""
    try:
        content = _content([str(t).lower() for t in tokens])
        if _imperative_shape(content):
            return False
        narration, conditional, _ = _statement_kind(content)
        return narration or conditional
    except Exception:
        return False


def _without_names(words: Sequence[str], seqs: Tuple[Tuple[str, ...], ...]) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(words):
        matched = next((len(s) for s in seqs if tuple(words[i:i + len(s)]) == s), 0)
        if matched:
            i += matched
            continue
        out.append(words[i])
        i += 1
    return out


def _analyze_sentence(
    stoks: Sequence[_Tok], seqs: Tuple[Tuple[str, ...], ...],
) -> List[Clause]:
    words_all = [t.text for t in stoks]
    reported = _reports_speech(_without_names(words_all, seqs))
    cond = False
    narrative = False
    out: List[Clause] = []
    for toks, sep in _split_sentence(stoks, seqs):
        words = tuple(t.text for t in toks)
        content = _content(words)
        quoted = any(t.quoted for t in toks)
        if sep == "named":
            narrative = False
        start, end = toks[_filler_count(words) if content else 0].start, toks[-1].end
        if not content:
            out.append(Clause(start, end, words, sep, False, False, False, False, True, True, False))
            continue
        negated = _starts_negated(content)
        if content[0] in _CONDITIONAL_LEADS or content[:3] == ["as", "soon", "as"]:
            cond = True
        shape = _imperative_shape(content)
        deferred = False
        scheduled = False
        if shape:
            narration = reported or quoted or narrative
            scheduled = (
                not narration and content[0] in _SCHEDULE_LEADS and _unwrap_time_phrase(list(content))[1]
            )
            conditional = cond or scheduled
            imperative = not (narration or conditional)
            deferred = conditional and not narration
        else:
            narration, conditional, lead = _statement_kind(content)
            narration = narration or reported or quoted or narrative
            deferred = conditional and not narration and _automation_shaped(content)
            conditional = conditional or cond
            imperative = False
            if lead and not negated:
                narrative = True
        cue_ok = imperative or negated or not (narration or conditional)
        out.append(Clause(
            start, end, words, sep, imperative, narration, conditional, negated, False, cue_ok, deferred, scheduled,
        ))
    return out


def _command_span_text(src: str, clauses: Sequence[Clause]) -> Optional[str]:
    groups: List[List[int]] = []  # [start, end, next clause index]
    i, n = 0, len(clauses)
    while i < n:
        c = clauses[i]
        if not c.imperative:
            i += 1
            continue
        start, end = c.start, c.end
        j = i + 1
        while j < n:
            nxt = clauses[j]
            if nxt.imperative:
                end = nxt.end
            elif nxt.sep in ("conj", "comma") and not nxt.narration and not nxt.conditional and not nxt.negated:
                end = nxt.end
            else:
                break
            j += 1
        groups.append([start, end, j])
        i = j
    if not groups:
        return None
    parts = [re.sub(r"[\"“”]", "", src[a:b]).strip(" ,;.!?\n") for a, b, _ in groups]
    return ", ".join(p for p in parts if p) or None


@functools.lru_cache(maxsize=64)
def _analyze_cached(text: str, names: Tuple[str, ...]) -> ClauseAnalysis:
    low = text.lower()
    src = low if len(low) != len(text) else text
    seqs = _name_sequences(names)
    clauses: List[Clause] = []
    for stoks in _sentences(_tokenize(low)):
        clauses.extend(_analyze_sentence(stoks, seqs))
    return ClauseAnalysis(src, tuple(clauses), _command_span_text(src, clauses))


def _analyze_or_none(query: Optional[str], assistant_names: Iterable[str]) -> Optional[ClauseAnalysis]:
    try:
        if not query or not query.strip():
            return ClauseAnalysis("", (), None)
        names = tuple(sorted({str(n).strip().lower() for n in assistant_names if n and str(n).strip()}))
        return _analyze_cached(_bound(query), names)
    except Exception:
        return None


def analyze_clauses(query: Optional[str], assistant_names: Iterable[str] = ()) -> ClauseAnalysis:
    """The utterance's clauses and its command text (the imperative clauses,
    joined in order). Pure; never raises (a failure analyses as no clauses)."""
    return _analyze_or_none(query, assistant_names) or ClauseAnalysis(query or "", (), None)


def cue_text(
    query: Optional[str], assistant_names: Iterable[str] = (), *, allow_scheduled: bool = False,
) -> Optional[str]:
    """The words that may carry the cue for a write. When the utterance has a
    command, only the command's words; else the clauses that are not
    narration or a condition, and the text itself when nothing is dropped.
    Fails closed: an analysis that fails gives no words. ``allow_scheduled``
    lets a time-led command count (a sequence step is run later, not now)."""
    if query is None:
        return None
    analysis = _analyze_or_none(query, assistant_names)
    if analysis is None:
        return ""
    if allow_scheduled and any(c.scheduled for c in analysis.clauses):
        # A sequence plans a timed step: its time-led command is the cue.
        return ", ".join(analysis.text[c.start:c.end] for c in analysis.clauses if c.imperative or c.scheduled)
    if analysis.command_text is not None:
        return analysis.command_text
    if not query.strip():
        return query
    if not analysis.clauses:
        return ""
    if all(c.cue_ok for c in analysis.clauses):
        return query
    return ", ".join(analysis.text[c.start:c.end] for c in analysis.clauses if c.cue_ok and not c.empty)


def _live(analysis: ClauseAnalysis) -> List[Clause]:
    return [c for c in analysis.clauses if not c.empty and not c.negated]


def is_narration(query: Optional[str], assistant_names: Iterable[str] = ()) -> bool:
    """Whether every clause that is not negated or empty is narration or a
    bare condition: the words talk about someone or something and ask for
    nothing. A command that is not for now ("whenever someone comes in, turn
    on the lights") is a request to the automation agent, so it is not
    narration. Fails closed: an analysis that fails is narration."""
    analysis = _analyze_or_none(query, assistant_names)
    if analysis is None:
        return True
    live = _live(analysis)
    return bool(live) and all(c.narration or (c.conditional and not c.deferred) for c in live)


def has_narration(query: Optional[str], assistant_names: Iterable[str] = ()) -> bool:
    """Whether any clause that is not negated or empty is narration. Fails closed."""
    analysis = _analyze_or_none(query, assistant_names)
    return True if analysis is None else any(c.narration for c in _live(analysis))


def imperative_text(query: Optional[str], assistant_names: Iterable[str] = ()) -> str:
    """The imperative clauses alone, without the fragments that follow them
    ("turn on the hall light, heading out" is "turn on the hall light")."""
    analysis = analyze_clauses(query, assistant_names)
    return ", ".join(
        re.sub(r"[\"\u201c\u201d]", "", analysis.text[c.start:c.end]).strip() for c in analysis.clauses if c.imperative
    )


def command_query(uk: "UtteranceClassification", query: str) -> str:
    """The words to extract an intent from and to execute: the command text
    when the utterance has one, with the room an earlier clause named when the
    command names none; else the utterance itself."""
    cmd = uk.command_text
    if not cmd or len(query.encode("utf-8")) > MAX_CLASSIFIED_CHARS:
        # Only a prefix is analysed: a longer utterance goes on whole, to
        # be refused by the size cap that guards the extractor.
        return query
    if uk.room and uk.room.lower() not in cmd.lower():
        return f"{cmd} in the {uk.room}"
    return cmd


def _earlier_room(analysis: ClauseAnalysis) -> Optional[str]:
    """The room an earlier clause that is not narration named ("In the
    kitchen. Turn on the lights.")."""
    for c in analysis.clauses:
        if c.imperative:
            return None
        if c.cue_ok and not c.empty and not c.negated:
            room = _room_for(analysis.text[c.start:c.end])
            if room:
                return room
    return None


def _core_text(q: str, names: Tuple[str, ...]) -> str:
    """``q`` (already stripped of leading okay/hey/name) without a trailing
    assistant name ("goodbye, nebu")."""
    core = q
    words = core.split(" ")
    while len(words) > 1 and (words[0] in _LEADING_FILLERS or words[0] == "please"):
        words.pop(0)
    if len(words) > 1 and words[-1] == "please":
        words.pop()
    core = " ".join(words).rstrip(" ,")
    seqs = ["".join(s) if len(s) == 1 else " ".join(s) for s in _name_sequences(names)]
    changed = True
    while changed:
        changed = False
        for n in seqs:
            for tail in (", " + n, " " + n):
                if core.endswith(tail):
                    core = core[:-len(tail)].rstrip(" ,")
                    changed = True
    return core


def classify_utterance(
    query: Optional[str], assistant_names: Iterable[str] = ()
) -> UtteranceClassification:
    """Pure classifier. Never raises; None/empty -> UNKNOWN.

    `assistant_names` adds the deployment's configured assistant name(s) to
    the leading vocative fillers stripped before classification ("Friday,
    are the lights on"); DEFAULT_ASSISTANT_NAMES are always stripped. Only
    the first MAX_CLASSIFIED_CHARS characters are classified.

    An IMPERATIVE result carries ``command_text``: the command clauses only,
    without the narration around them (see ``analyze_clauses``)."""
    try:
        names = tuple(str(n) for n in assistant_names if n)
        result = _classify(query, names)
        if not query or not query.strip():
            return result
        q, _ = _normalize(query, names)
        return replace(result, core_text=_core_text(q, names) or None)
    except Exception:
        return UtteranceClassification(kind=UtteranceKind.UNKNOWN, rule="error")


def _classify(query: Optional[str], names: Tuple[str, ...]) -> UtteranceClassification:
    try:
        if not query or not query.strip():
            return UtteranceClassification(kind=UtteranceKind.UNKNOWN, rule="empty")

        q, had_q = _normalize(query, names)
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
            and _CAUSATIVE_RE.match(first_clause) is None
        )

        # Rule 2: IMPERATIVE.
        if not had_q and not first_is_question_open and _is_bare_command(q2):
            return UtteranceClassification(
                kind=UtteranceKind.IMPERATIVE,
                device_type=_device_type_for(q2),
                room=_room_for(q2),
                target_state=_target_state_for(q2),
                rule="bare_command",
                command_text=q2,
            )

        analysis = analyze_clauses(query, names)
        if not first_is_question_open and analysis.command_text is not None:
            basis = _strip_filler_adverbs(analysis.command_text.lower())
            return UtteranceClassification(
                kind=UtteranceKind.IMPERATIVE,
                device_type=_device_type_for(basis),
                room=_room_for(basis) or _earlier_room(analysis),
                target_state=_target_state_for(basis),
                rule="imperative_clause",
                command_text=analysis.command_text,
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

        if first_is_question_open and (
            _STATE_WORD_RE.search(q2) or _PAST_ACTION_CHECK_RE.match(first_clause)
        ):
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

        return UtteranceClassification(
            kind=UtteranceKind.UNKNOWN, rule="fallthrough",
            deferred=any(c.deferred for c in analysis.clauses) and not first_is_question_open,
        )
    except Exception:
        return UtteranceClassification(kind=UtteranceKind.UNKNOWN, rule="error")


def _is_bare_command(q2: str) -> bool:
    if q2 in _FIXED_IMPERATIVE_IDIOMS:
        return True
    if _BARE_COMMAND_EXCLUDED.search(q2):
        return False
    return bool(_BARE_COMMAND_RE.match(q2))


_INDOOR_TEMP_WORD_RE = re.compile(r"\b(?:warm|hot|cold|chilly|temperature|temp)\b")
_INDOOR_PLACE_RE = re.compile(r"\b(?:in here|inside|indoors)\b")
_INDOOR_ADJUST_RE = re.compile(r"\b(?:warmer|cooler|colder|hotter|turn (?:up|down)|raise|lower|drop|set|make)\b")


def is_indoor_temperature_question(query: Optional[str]) -> bool:
    """A question about how warm it is in here, not outside: a temperature
    word with "in here", "inside" or "indoors", and no wording that asks for
    a change. Independent of any other classifier's verdict."""
    q = (query or "").lower()
    return bool(
        _INDOOR_TEMP_WORD_RE.search(q) and _INDOOR_PLACE_RE.search(q) and not _INDOOR_ADJUST_RE.search(q)
    )

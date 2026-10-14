"""The deterministic answer to "what's my name" / "where do I live".

When the server can't say who is speaking (no proven owner, no named guest,
no signed-in member), the answer to a first-person identity question is a
fixed sentence, not a model call: the model sees the owner's rows and would
read them out as the speaker's. Named callers keep their own paths.

Pure and stdlib-only apart from `shared.fast_path_vocab`; no I/O, no config.
`identity_question` is the one vocabulary for the classify-time answer, the
named-caller fast path and the semantic-cache skip.

Questions about the house ("what's the address here", "who lives here",
"who's the owner") are not identity questions: they follow the owner's
tiers, as configured.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from shared.fast_path_vocab import normalize_reply

NAME = "name"
HOME = "home"

NAME_REPLY = "I can't tell who's speaking, so I don't know your name."
HOME_REPLY = "I can't tell who's speaking, so I can't say where you live."
_REPLIES = {NAME: NAME_REPLY, HOME: HOME_REPLY}

MAX_WORDS = 12
DEFAULT_NAMES = ("athena", "jarvis")

_WHAT = r"(?:what is|what's|whats|what)"
_NAME_FRAMES = (
    rf"{_WHAT} my (?:full |first |last |real )?name(?: again)?",
    r"(?:say|tell me|state|give me|repeat) my (?:full |first |last )?name(?: again)?",
    r"who am i(?: again)?",
    r"(?:tell me|say) who i am",
    r"who i am",
    r"(?:do|can) you (?:know|remember|recognize) (?:me|who i am|my name)",
    r"is my name [\w.'-]+(?: [\w.'-]+)?",
    r"what (?:do|did|would) (?:you|people|they|everyone|everybody|folks|others) call me",
    r"what am i called",
)
_HOME_FRAMES = (
    r"where do i live",
    r"(?:tell me|say|do you know) where i live",
    r"(?:do|can) you (?:know|remember) my (?:home |house |street )?address",
    r"my (?:home |house |street )?address",
    rf"{_WHAT} my (?:home |house |street |mailing |current |full )?address(?: again)?",
    r"(?:tell me|say|give me|state) my (?:home |house |street )?address",
    r"(?:where is|where's|wheres) my (?:home|house|place|apartment)",
    rf"{_WHAT} the address (?:of|for) my (?:home|house|place)",
)
_NAME_RE = re.compile("|".join(f"(?:{f})" for f in _NAME_FRAMES))
_HOME_RE = re.compile("|".join(f"(?:{f})" for f in _HOME_FRAMES))

_LEAD_FILLER = (
    "hey there", "hey", "hi", "hello", "okay", "ok", "so", "and", "um", "uh", "well", "please", "just", "now",
    "can you", "could you", "would you", "will you",
)
_TRAIL_FILLER = ("please", "right now", "now", "thanks", "thank you", "for me")
_LEAD_FILLER_RE = re.compile(r"^(?:%s) " % "|".join(re.escape(w) for w in _LEAD_FILLER))
_TRAIL_FILLER_RE = re.compile(r" (?:%s)$" % "|".join(re.escape(w) for w in _TRAIL_FILLER))

# A cheap pre-check so callers resolve the configured assistant names (a
# bounded lookup) only for a question that may be an identity question.
_MAY_BE_IDENTITY_RE = re.compile(
    r"\bmy (?:\w+ )?(?:name|address)\b|\baddress (?:of|for) my\b|\bwho (?:am )?i\b|\bwhere (?:do )?i live\b|\bcall me\b"
    r"|\b(?:know|recognize|remember) me\b|\bwhere(?:'s| is) my\b|\bam i called\b|\bwho i am\b|\bis my name\b"
)

_FOLD_RE = re.compile(r"[\W_]+")
_SELF_REFERENCE_RE = re.compile(r"\b(?:my|me|i|i'm|myself)\b")
_IDENTITY_WORD_RE = re.compile(r"\b(?:name|called|call|calls|who|address|live|lives|living)\b")
# Asking for a name or where one lives, in a question of any length. Unlike
# the backstop's word list it has no bare "who": "who won the game I watched"
# is not about the speaker.
_SENSITIVE_RE = re.compile(
    r"\b(?:names?|address|lives?|living|called|call me|who am i|who i am|(?:know|recognize|remember) me)\b"
)
_NAME_ASKING_RE = re.compile(r"\b(?:names?|called|call me|who am i|who i am|(?:know|recognize|remember) me)\b")
_STREET_ABBREVIATIONS = {
    "st": "street", "ave": "avenue", "rd": "road", "dr": "drive", "blvd": "boulevard", "ln": "lane", "ct": "court",
}
# First names that are also ordinary words: matched on their own only in the
# phrasing of a name statement ("your name is Will"), never anywhere in an answer.
_COMMON_WORD_NAMES = frozenset({
    "will", "mark", "may", "grace", "bill", "rich", "hope", "joy", "art", "pat", "ray", "rose", "dawn", "faith",
    "june", "april", "august", "jack", "jean", "lane", "drew", "chase", "summer", "sky", "page", "grant", "dean",
    "frank", "guy", "max", "miles", "bay", "cliff", "hunter", "rock", "star", "sue", "tom", "wade", "ward", "bob",
    "case", "gene", "ginger", "heather", "holly", "ivy", "lily", "mary", "pearl", "penny", "ruby", "sandy", "violet",
})
_NAME_STATEMENT_RE = r"(?:name is|you are|you re|call you|called|your name s|i m)"


def _normalize(query: Optional[str]) -> str:
    q = normalize_reply(query).replace("’", "'")
    q = re.sub(r"[^\w' ]+", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def _strip_names(q: str, names: Iterable[str]) -> str:
    cleaned = sorted({n for n in (_normalize(x) for x in (*DEFAULT_NAMES, *names)) if n}, key=len, reverse=True)
    if not cleaned:
        return q
    alternation = "|".join(re.escape(n) for n in cleaned)
    q = re.sub(rf"^(?:(?:hey|hi|hello|okay|ok) )?(?:{alternation}) ", "", q)
    return re.sub(rf" (?:{alternation})$", "", q)


def may_be_identity_question(query: Optional[str]) -> bool:
    q = _normalize(query)
    return 0 < len(q.split()) <= MAX_WORDS and bool(_MAY_BE_IDENTITY_RE.search(q))


def identity_question(query: Optional[str], assistant_names: Iterable[str] = ()) -> Optional[str]:
    """NAME, HOME or None for a first-person identity question of at most
    12 words, ignoring vocatives (an assistant name), fillers and punctuation."""
    q = _normalize(query)
    if not q or len(q.split()) > MAX_WORDS:
        return None
    for _ in range(4):
        stripped = _strip_names(q, assistant_names)
        stripped = _LEAD_FILLER_RE.sub("", stripped, count=1)
        stripped = _TRAIL_FILLER_RE.sub("", stripped, count=1)
        if stripped == q:
            break
        q = stripped
    if _NAME_RE.fullmatch(q):
        return NAME
    if _HOME_RE.fullmatch(q):
        return HOME
    return None


def reply_for(
    query: Optional[str], addressee_kind: Optional[str], assistant_names: Iterable[str] = ()
) -> Optional[str]:
    """The fixed reply, only when nobody is addressed (``addressee_kind is None``)."""
    if addressee_kind is not None:
        return None
    kind = identity_question(query, assistant_names)
    return _REPLIES[kind] if kind else None


def mentions_identity(query: Optional[str]) -> bool:
    """A first-person reference together with a name or address word, in a
    question of any length ("hey so could you tell me what is my name",
    "what's my name and where do I live"). Broader than `identity_question`:
    it decides what the prompt must leave out, not what is answered."""
    q = _normalize(query)
    return bool(_SELF_REFERENCE_RE.search(q) and _SENSITIVE_RE.search(q))


def _fold(text: Optional[str]) -> str:
    return _FOLD_RE.sub(" ", str(text or "").casefold()).strip()


def _fold_address(text: Optional[str]) -> str:
    return " ".join(_STREET_ABBREVIATIONS.get(t, t) for t in _fold(text).split())


def _contains(folded_answer: str, folded_needle: str) -> bool:
    return bool(folded_needle) and f" {folded_needle} " in f" {folded_answer} "


def _name_in(folded_answer: str, owner_name: Optional[str], *, asking_name: bool) -> bool:
    """The full name always counts. A single name token (3+ letters) counts only
    when the question asks for a name, and a token that is also an ordinary
    word only in the phrasing of a name statement."""
    folded = _fold(owner_name)
    if not folded:
        return False
    if _contains(folded_answer, folded):
        return True
    if not asking_name:
        return False
    for token in folded.split():
        if len(token) < 3:
            continue
        if token in _COMMON_WORD_NAMES:
            if re.search(rf"\b{_NAME_STATEMENT_RE} {re.escape(token)}\b", folded_answer):
                return True
        elif _contains(folded_answer, token):
            return True
    return False


def _address_in(folded_answer: str, home_address: Optional[str]) -> bool:
    folded = _fold_address(home_address)
    if not folded:
        return False
    answer = _fold_address(folded_answer)
    street = _fold_address(str(home_address).split(",")[0])
    street_is_specific = len(street.split()) >= 2 and any(ch.isdigit() for ch in street)
    return _contains(answer, folded) or (street_is_specific and _contains(answer, street))


def backstop(
    query: Optional[str],
    answer: Optional[str],
    addressee_kind: Optional[str],
    owner_name: Optional[str],
    home_address: Optional[str] = None,
) -> Optional[str]:
    """A model answer to nobody that reads out the visible owner name or home
    address in reply to a first-person question about names or where one
    lives is replaced by the matching fixed reply; otherwise None."""
    if addressee_kind is not None or not answer:
        return None
    q = _normalize(query)
    if not (_SELF_REFERENCE_RE.search(q) and _IDENTITY_WORD_RE.search(q)):
        return None
    folded_answer = _fold(answer)
    if _name_in(folded_answer, owner_name, asking_name=bool(_NAME_ASKING_RE.search(q))):
        return NAME_REPLY
    if _address_in(folded_answer, home_address):
        return HOME_REPLY
    return None

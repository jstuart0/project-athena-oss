"""Which door a command names: locks and the covers that let someone in.

Pure. Nothing here does I/O, logs, reads a runtime singleton or touches
permissions. The controller hands in the entities the request MAY use
(``entities``: id -> spoken name), the utterance, and what a model guessed;
``resolve`` says what the words settle.

The rule this module exists for: an opening (an unlock, or opening a garage,
gate or door cover) acts on exactly one entity, and only when the user's own
words name it. A room or an area never names a door; it only narrows the
doors a question offers (``InArea``). A model's guess counts only through the
same word test, and only for words the utterance really contains.

Lock, close and status commands may use the same outcomes more loosely; the
policy table lives with the writers (``SmartHomeController``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from orchestrator import room_context
from shared.ha_registry import tokens

# How an entity came to be the one named (``Named.named_by``).
CARRIED = "carried"
TOKENS_AND_NOUN = "tokens+noun"
TOKENS = "tokens"
BARE_NOUN = "bare_noun"
# The sources that may write an opening. ``TOKENS`` (a name without a door
# noun) only ever gives a hint.
OPENING_SOURCES: FrozenSet[str] = frozenset({CARRIED, TOKENS_AND_NOUN, BARE_NOUN})

DOMAINS = ("lock", "cover")
# Domains whose services can let someone in: a call with no concrete entity
# in one of them is never aimed at a room.
WAY_IN_DOMAINS: FrozenSet[str] = frozenset({"lock", "cover", "homeassistant"})

_NOUNS = {
    "lock": frozenset({"lock", "locks", "door", "doors", "deadbolt", "deadbolts"}),
    "cover": frozenset({"garage", "garages", "gate", "gates", "door", "doors", "opener", "openers"}),
}
_PLURAL_NOUNS = {
    "lock": frozenset({"locks", "doors", "deadbolts"}),
    "cover": frozenset({"doors", "gates", "openers", "garages"}),
}
_ALL_NOUNS = _NOUNS["lock"] | _NOUNS["cover"]
_GENERIC_NOUNS = frozenset({"door", "doors", "lock", "locks"})

# A cover is NOT an access cover only when its device class says so. Anything
# else, including a missing class, is treated as a way in.
NON_ACCESS_DEVICE_CLASSES: FrozenSet[str] = frozenset({
    "awning", "blind", "curtain", "damper", "shade", "shutter", "window",
})
# Words that mean a non-access cover when a person says them.
NON_ACCESS_COVER_WORDS: FrozenSet[str] = frozenset({
    "blind", "blinds", "shade", "shades", "curtain", "curtains", "shutter", "shutters",
    "awning", "awnings", "window", "windows", "drape", "drapes", "damper", "dampers",
})

# Services and actions that let someone in, per domain (D10/D14). ``toggle``
# can open, and HA's own ``homeassistant`` mapping turns a lock off to unlock
# and a cover on to open.
_OPENING_ACTIONS = {
    "lock": frozenset({"unlock", "open", "turn_off", "toggle"}),
    "cover": frozenset({"open", "open_cover", "turn_on", "toggle", "set_cover_position"}),
}

_QUANTIFIERS = frozenset({"all", "every", "each", "both"})
_UNIVERSAL_ANYWHERE = frozenset({"everything", "everywhere"})
_PLURAL_DETERMINERS = frozenset({"the", "my", "our", "your", "these", "those"})
_AREA_WORDS = frozenset({"area", "areas", "room", "rooms", "zone", "zones"})

_FILLER = frozenset({
    "the", "a", "an", "my", "our", "your", "please", "to", "for", "of", "in", "on", "off", "at", "up",
    "down", "it", "that", "this", "these", "those", "and", "or", "now", "can", "could", "would",
    "will", "you", "hey", "i", "me", "just", "also", "then", "is", "are", "be", "do", "does", "did",
    "let", "get", "go", "ahead", "with", "from", "by", "so", "if", "not", "no", "yes", "what",
    "whats", "s", "status", "there", "here", "any", "one", "ones", "thing", "things",
})
_VERBS = frozenset({
    "unlock", "unlocks", "unlocked", "lock", "locks", "locked", "locking", "open", "opens", "opened",
    "opening", "close", "closes", "closed", "closing", "shut", "release", "released", "undo", "unbolt",
    "bolt", "secure", "raise", "lower", "check", "disarm", "arm", "need", "want", "like", "leave",
    "turn", "toggle", "activate", "set",
})
_NOT_MODIFIERS = _FILLER | _VERBS | _ALL_NOUNS | _AREA_WORDS

_REPLY_FILLER = frozenset({"the", "one", "please", "a", "an", "um", "uh", "yes", "yeah", "that", "this", "my"})


@dataclass(frozen=True)
class Ref:
    entity_id: str
    label: str


@dataclass(frozen=True)
class Named:
    """Exactly one entity. ``named_by`` says how, and so whether it may be
    opened (``OPENING_SOURCES``)."""
    refs: Tuple[Ref, ...]
    named_by: str


@dataclass(frozen=True)
class Every:
    refs: Tuple[Ref, ...]


@dataclass(frozen=True)
class Ambiguous:
    refs: Tuple[Ref, ...]
    phrase: str


@dataclass(frozen=True)
class InArea:
    """The doors in a room or area the utterance named. Narrows a question;
    never names a door to open."""
    refs: Tuple[Ref, ...]
    phrase: str


@dataclass(frozen=True)
class Unnamed:
    refs: Tuple[Ref, ...]


@dataclass(frozen=True)
class NotFound:
    """``phrase`` is built from the utterance's own words, at most three."""
    phrase: str


Outcome = Union[Named, Every, Ambiguous, InArea, Unnamed, NotFound]


@dataclass(frozen=True)
class Choice:
    kind: str
    ref: Optional[Ref] = None
    matches: Tuple[Ref, ...] = ()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def is_access_cover(entity_id: str, attributes: Optional[Mapping]) -> bool:
    """Whether a cover is a way in. Fails closed: only a device class naming
    something that is not a door makes it a plain cover."""
    device_class = attributes.get("device_class") if isinstance(attributes, Mapping) else None
    if isinstance(device_class, str) and device_class.strip().lower() in NON_ACCESS_DEVICE_CLASSES:
        return False
    return True


def entity_domain(entity_id: Any) -> Optional[str]:
    if not isinstance(entity_id, str) or "." not in entity_id:
        return None
    return entity_id.split(".", 1)[0].strip().lower() or None


def opening_domain(entity_id: Any, device_type: Any) -> Optional[str]:
    """``lock`` or ``cover`` when a step or call targets one: the id's own
    domain when it has one (a light-typed step carrying ``lock.x`` is a
    lock), else the device type."""
    domain = entity_domain(entity_id)
    if domain is None and isinstance(device_type, str):
        domain = device_type.strip().lower()
    return domain if domain in DOMAINS else None


def is_opening_action(domain: Optional[str], action: Any) -> bool:
    if domain not in _OPENING_ACTIONS or not isinstance(action, str):
        return False
    return action.strip().lower() in _OPENING_ACTIONS[domain]


def noun_present(query: Optional[str], domain: str) -> bool:
    """Whether the utterance says an access noun for the domain."""
    return any(t in _NOUNS[domain] for t in tokens(query or ""))


def mentions_non_access_cover(query: Optional[str]) -> bool:
    return any(t in NON_ACCESS_COVER_WORDS for t in tokens(query or ""))


_STRICT_COVER_NOUNS = frozenset({"door", "doors", "gate", "gates", "opener", "openers"})


def cover_intent_kind(query: Optional[str]) -> Optional[str]:
    """``plain`` when the words are about blinds and the like and say no
    door, gate or opener ("the garage blinds"); ``access`` when they name an
    access noun; None when they say neither."""
    spoken = tokens(query or "")
    if any(t in NON_ACCESS_COVER_WORDS for t in spoken) and not any(t in _STRICT_COVER_NOUNS for t in spoken):
        return "plain"
    return "access" if any(t in _NOUNS["cover"] for t in spoken) else None


def hint_phrase(label: str, domain: str) -> str:
    """What to say to name this door so the opening test passes: its name,
    with an access noun added when the name has none ("Front Entry" ->
    "Front Entry door")."""
    return label if any(t in _NOUNS[domain] for t in tokens(label)) else f"{label} door"


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

def _distinguishing(label: str, domain: str) -> FrozenSet[str]:
    """The words of an entity's name that tell it from its siblings: its name
    less the generic nouns and filler ("Front Door Lock" -> front)."""
    return frozenset(t for t in tokens(label) if t not in _NOUNS[domain] and t not in _FILLER and t not in _VERBS)


def _modifiers(toks: Sequence[str], domain: str) -> List[str]:
    """The words the speaker put before an access noun to say which one
    ("side" in "the side door"), in order."""
    nouns = _NOUNS[domain]
    found: List[str] = []
    for i, tok in enumerate(toks):
        if tok not in nouns:
            continue
        j = i - 1
        run: List[str] = []
        while j >= 0:
            prev = toks[j]
            if prev in nouns:
                j -= 1
                continue
            if prev in _NOT_MODIFIERS:
                break
            run.append(prev)
            j -= 1
        found.extend(reversed(run))
    return list(dict.fromkeys(found))


def _is_universal(toks: Sequence[str], domain: str) -> bool:
    nouns = _NOUNS[domain]
    plural = _PLURAL_NOUNS[domain]
    for i, tok in enumerate(toks):
        if tok in _UNIVERSAL_ANYWHERE:
            return True
        if tok in _QUANTIFIERS and any(w in nouns for w in toks[i + 1:i + 4]):
            return True
        if tok == "lock" and toks[i + 1:i + 2] in (["up"], ["down"]):
            return True
        if tok == "lock" and toks[i + 1:i + 3] in (["it", "all"], ["it", "up"], ["it", "down"]):
            return True
        if tok in plural and i > 0 and toks[i - 1] in _PLURAL_DETERMINERS:
            return True
    return False


def _is_bare_referent(toks: Sequence[str], label: str, domain: str) -> bool:
    """Whether the words say nothing but "it" for the carried door: no name
    word (blinds included) or access noun that isn't part of its own name,
    and no plural or universal wording."""
    if not toks:
        return True
    if _is_universal(toks, domain):
        return False
    own = frozenset(tokens(label))
    if any(t not in _NOT_MODIFIERS and t not in own for t in toks):
        return False
    specific = _NOUNS[domain] - _GENERIC_NOUNS
    return not any(t in specific and t not in own for t in toks)


def _pick(matches: Sequence[Ref], named_by: str, phrase: str) -> Outcome:
    if len(matches) == 1:
        return Named((matches[0],), named_by)
    return Ambiguous(tuple(matches), phrase)


def _query_matches(refs: Sequence[Ref], dist: Mapping[str, FrozenSet[str]], qset: FrozenSet[str]) -> List[Ref]:
    """The entities whose every distinguishing word the speaker said; if
    several, only the most specific."""
    full = [r for r in refs if dist[r.entity_id] and dist[r.entity_id] <= qset]
    if not full:
        return []
    most = max(len(dist[r.entity_id]) for r in full)
    return [r for r in full if len(dist[r.entity_id]) == most]


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

def resolve(
    query: Optional[str],
    *,
    domain: str,
    model_target: Any,
    carried: Optional[str],
    entities: Mapping[str, str],
    area_entities: Optional[Mapping[str, Sequence[str]]] = None,
    directory: Optional[room_context.RoomDirectory] = None,
    carried_trusted: bool = False,
) -> Outcome:
    """What the utterance settles about which of ``entities`` is meant.

    ``entities`` is the PERMITTED subset only (id -> spoken name), so an
    outcome can never name a door the request may not use. ``area_entities``
    maps a room key to the ids in that room's area. Rules, in order:

    1. ``carried`` (a server decision), only for a bare referent ("it",
       "that", "the door") or the answer to "which door?"
       (``carried_trusted``). Any word that isn't the carried door's own
       drops the carry.
    2. Universal wording: ``Every``.
    3. The speaker's words match an entity's distinguishing words: ``Named``
       when exactly one (the most specific), else ``Ambiguous``; a name said
       only in part is ``Ambiguous``.
    4. A room the utterance names whose area holds some of them: ``InArea``,
       never ``Named``.
    5. A model's target, only for words the utterance contains.
    6. A bare noun ("the lock", "my garage") with exactly one entity;
       an unmatched modifier ("the side door") is ``NotFound``.
    7. Otherwise ``Unnamed``.

    With no utterance (a server caller) only rules 1 and 7 apply. An unknown
    domain resolves nothing.
    """
    if domain not in DOMAINS:
        return Unnamed(())
    refs = tuple(Ref(eid, label) for eid, label in entities.items())
    dist = {r.entity_id: _distinguishing(r.label, domain) for r in refs}
    toks = list(tokens(query)) if query else []
    qset = frozenset(toks)
    named_by = TOKENS_AND_NOUN if noun_present(query, domain) else TOKENS

    if isinstance(carried, str) and carried in entities:
        if carried_trusted or _is_bare_referent(toks, entities[carried], domain):
            return Named((Ref(carried, entities[carried]),), CARRIED)
    if not toks:
        return Unnamed(refs)

    if _is_universal(toks, domain):
        return Every(refs)

    full = _query_matches(refs, dist, qset)
    if full:
        return _pick(full, named_by, "door")
    said_words = frozenset(t for t in toks if t not in _NOT_MODIFIERS)
    if said_words:
        partial = [r for r in refs if dist[r.entity_id] & said_words]
        if partial:
            return Ambiguous(tuple(partial), "door")

    in_area = _in_area(toks, query, domain, refs, area_entities or {}, directory)
    if in_area is not None:
        return in_area

    target = _target_tokens(model_target, domain)
    if target and frozenset(target) <= qset:
        matched = [r for r in refs if frozenset(target) <= dist[r.entity_id]]
        if not matched:
            return NotFound(" ".join(target[:3]))
        return _pick(matched, named_by, "door")

    if noun_present(query, domain):
        modifiers = _modifiers(toks, domain)
        if modifiers:
            return NotFound(" ".join(modifiers[:3]))
        # "my garage": the entities whose own name says the noun the speaker
        # said (door and lock are in every name, so they narrow nothing).
        specific = {t for t in toks if t in _NOUNS[domain] and t not in _GENERIC_NOUNS}
        pool = [r for r in refs if specific & frozenset(tokens(r.label))] if specific else []
        pool = tuple(pool) if pool else refs
        if len(pool) == 1:
            return Named(pool, BARE_NOUN)
        return Unnamed(pool)
    return Unnamed(refs)


def _in_area(
    toks: Sequence[str], query: Optional[str], domain: str, refs: Sequence[Ref],
    area_entities: Mapping[str, Sequence[str]], directory: Optional[room_context.RoomDirectory],
) -> Optional[InArea]:
    if not area_entities:
        return None
    mention = room_context.find_room_mention(query, directory)
    if mention.kind == "none" or mention.in_exclusion:
        return None
    phrase_words = toks[mention.start:mention.end]
    after = toks[mention.end:mention.end + 1]
    says_area = bool(after) and after[0] in _AREA_WORDS
    if phrase_words and all(w in _NOUNS[domain] or w in _FILLER for w in phrase_words) and not says_area:
        # "the garage" for a cover is the noun, not a room.
        return None
    allowed = {r.entity_id: r for r in refs}
    found: List[Ref] = []
    for candidate in mention.candidates:
        for entity_id in area_entities.get(candidate.key, ()):
            ref = allowed.get(entity_id)
            if ref is not None and ref not in found:
                found.append(ref)
    return InArea(tuple(found), mention.phrase) if found else None


def narrow_to_target(query: Optional[str], model_target: Any, domain: str) -> Optional[str]:
    """For one step of a sequence: the words that name this step's door.

    A sequence's utterance names several doors, so each step is read against
    its own target: the model's target, when every distinguishing word of it
    is in the utterance (they are then the speaker's words), plus the access
    noun the speaker used. A target the utterance does not contain narrows
    nothing and the utterance itself is used."""
    wanted = _target_tokens(model_target, domain)
    spoken = list(tokens(query or ""))
    if not wanted or not frozenset(wanted) <= frozenset(spoken):
        return query
    noun = next((t for t in spoken if t in _NOUNS[domain]), "")
    return " ".join([*wanted, noun]).strip()


def _target_tokens(model_target: Any, domain: str) -> List[str]:
    """The distinguishing words of a model's target, or [] (a target that is
    not text, or that says only "door")."""
    if not isinstance(model_target, str):
        return []
    if re.match(r"^[a-z_]+\.", model_target.strip().lower()):
        model_target = model_target.split(".", 1)[1]
    return [
        t for t in tokens(model_target)
        if t not in _NOUNS[domain] and t not in _FILLER and t not in _VERBS and t not in _AREA_WORDS
    ]


# ---------------------------------------------------------------------------
# The answer to "which door?"
# ---------------------------------------------------------------------------

def match_reply(reply: Optional[str], candidates: Sequence[Ref]) -> Choice:
    """Read the answer to "which door?". The reply's words, less filler and
    the generic nouns ("the front door" -> front), must all belong to one
    candidate's name. A reply made only of generic nouns ("the deadbolt") is
    read against the names whole. A command word makes it no answer."""
    raw = [t for t in tokens(reply or "") if t not in _REPLY_FILLER]
    if not raw or raw[0] in room_context._COMMAND_VERBS:
        return Choice("none")
    # "front door lock" repeats the name; a verb anywhere else fits no name.
    words = [t for t in raw if t not in _ALL_NOUNS and t not in _AREA_WORDS]
    wanted = frozenset(words or raw)
    fits = tuple(c for c in candidates if wanted <= frozenset(tokens(c.label)))
    if len(fits) == 1:
        return Choice("one", fits[0], fits)
    if len(fits) > 1:
        return Choice("several", None, fits)
    return Choice("none")

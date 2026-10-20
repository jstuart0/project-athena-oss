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

from orchestrator import light_effects, room_context
from orchestrator.utterance_kind import (  # noqa: F401  (statement_clause is re-exported for the cue callers)
    UtteranceKind,
    analyze_clauses,
    classify_utterance,
    cue_text,
    statement_clause,
)
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

# Words said of a cover that is plain only by device class: an unclassified
# "Bay Window" or "Workshop Shutter" can be a door, so these never make a
# cover plain by name. The wording still treats them as window coverings.
DEVICE_CLASS_ONLY_COVER_WORDS: FrozenSet[str] = frozenset({"shutter", "shutters", "window", "windows"})
# Words that make an unclassified cover plain by its NAME: the wording set
# less the device-class-only words, so wording and classification are one set.
_NAME_PLAIN_WORDS: FrozenSet[str] = NON_ACCESS_COVER_WORDS - DEVICE_CLASS_ONLY_COVER_WORDS

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
    """Whether a cover is a way in. Fails closed: a device class naming
    something that is not a door makes it a plain cover, and so does an
    unclassified cover whose name says blinds, shades and the like and no
    door, gate, opener or garage ("Living Room Blinds"). Anything else,
    "Bay 1" included, is a way in."""
    attrs = attributes if isinstance(attributes, Mapping) else {}
    device_class = attrs.get("device_class")
    if isinstance(device_class, str) and device_class.strip().lower() in NON_ACCESS_DEVICE_CLASSES:
        return False
    if isinstance(device_class, str) and device_class.strip():
        return True
    name = attrs.get("friendly_name")
    if not isinstance(name, str) or not name.strip():
        name = entity_id.split(".", 1)[-1] if isinstance(entity_id, str) else ""
    spoken = tokens(name)
    return not (
        any(t in _NAME_PLAIN_WORDS for t in spoken)
        and not any(t in _NOUNS["cover"] for t in spoken)
    )


# A lock that is not a door: a child lock on an appliance, a keypad, a safe.
NON_DOOR_LOCK_DEVICE_CLASSES: FrozenSet[str] = frozenset({"child_lock", "keypad", "panel", "safe"})
NON_DOOR_LOCK_WORDS: FrozenSet[str] = frozenset({
    "child", "kid", "kids", "keypad", "panel", "safe", "minisplit", "display", "buttons",
})


# A door, gate or entry word in a lock's name or id makes it a door whatever
# else the name says ("Front Door Keypad", "Kids Room Door").
DOOR_LOCK_WORDS: FrozenSet[str] = frozenset({
    "door", "doors", "gate", "gates", "entry", "front", "back", "garage", "deadbolt", "deadbolts",
})


def is_access_lock(entity_id: str, attributes: Optional[Mapping]) -> bool:
    """Whether a lock is a door. A door, gate, entry, front, back, garage or
    deadbolt word in its name or id always makes it one. Otherwise a device
    class or a name saying child lock, keypad, panel, safe or a mini-split's
    lock makes it something else, and an unnamed or unclassified lock is a
    door. A summary of "all the doors" and a door-scoped write use only
    these; a read of a lock the user names does not need this test."""
    attrs = attributes if isinstance(attributes, Mapping) else {}
    name = attrs.get("friendly_name")
    object_id = entity_id.split(".", 1)[-1] if isinstance(entity_id, str) else ""
    spoken = tokens(name if isinstance(name, str) else "") + tokens(object_id)
    if any(t in DOOR_LOCK_WORDS for t in spoken):
        return True
    device_class = attrs.get("device_class")
    if isinstance(device_class, str) and device_class.strip().lower() in NON_DOOR_LOCK_DEVICE_CLASSES:
        return False
    if "mini" in spoken and "split" in spoken:
        return False
    for i, word in enumerate(spoken):
        if word in NON_DOOR_LOCK_WORDS and not (word == "safe" and spoken[i + 1:i + 2] == ("room",)):
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


def is_window_door(entity_id: str, attributes: Optional[Mapping]) -> bool:
    """An access cover (by the classification above) whose own name says
    window or shutter ("Bay Window" with no device class). Window wording
    still reaches it to close it or read it; opening it goes through the
    door path, which asks for its name."""
    if not is_access_cover(entity_id, attributes):
        return False
    attrs = attributes if isinstance(attributes, Mapping) else {}
    name = attrs.get("friendly_name")
    if not isinstance(name, str) or not name.strip():
        name = entity_id.split(".", 1)[-1] if isinstance(entity_id, str) else ""
    return any(t in DEVICE_CLASS_ONLY_COVER_WORDS for t in tokens(name))


def named_by_words(label: str, query: Optional[str]) -> bool:
    """Whether the utterance says every distinguishing word of a cover's name
    ("bay" of "Bay Window"), and the name has one."""
    wanted = frozenset(
        t for t in tokens(label)
        if t not in NON_ACCESS_COVER_WORDS and t not in _NOUNS["cover"] and t not in _FILLER and t not in _VERBS
    )
    return bool(wanted) and wanted <= frozenset(tokens(query or ""))


# The words that say a device type is what a clause is about. One table for
# every sequence step's device type; the cover and lock words are this
# module's own vocabularies.
DEVICE_WORDS: dict = {
    "light": frozenset({"light", "lights", "lamp", "lamps", "bulb", "bulbs", "lighting"}),
    "fan": frozenset({"fan", "fans"}),
    "switch": frozenset({"switch", "switches", "plug", "plugs", "outlet", "outlets"}),
    "media": frozenset({
        "tv", "television", "speaker", "speakers", "music", "play", "playing", "volume", "stereo", "radio",
        "song", "songs", "playlist", "mute", "media", "shield",
    }),
    "climate": frozenset({
        "thermostat", "temperature", "heat", "heating", "cooling", "degrees", "degree", "ac", "hvac",
        "conditioning", "warmer", "colder", "cooler",
    }),
    "cover": NON_ACCESS_COVER_WORDS | frozenset({"garage", "garages", "gate", "gates", "door", "doors", "opener", "openers"}),
    "lock": frozenset({
        "lock", "locks", "unlock", "unlocks", "locking", "locked", "unlocked", "deadbolt", "deadbolts", "door", "doors",
    }),
}
# The actions that only read. One set for the planner (which skips an unbacked
# status check) and the executor (which never sends a service call for one).
READ_ONLY_ACTIONS: FrozenSet[str] = frozenset({"get_status", "check_status", "status", "check", "read"})
_DEVICE_KIND_ALIASES = {"media_player": "media", "tv": "media", "speaker": "media"}
_LIGHT_ACTIONS = frozenset({"set_color", "set_brightness"})


def device_kind(device_type: Any) -> Optional[str]:
    """The table key for a step's device type, or None for a type the table
    does not know (a scene is grounded separately)."""
    if not isinstance(device_type, str):
        return None
    kind = canonical_device_type(device_type).strip().lower()
    kind = _DEVICE_KIND_ALIASES.get(kind, kind)
    return kind if kind in DEVICE_WORDS else None


def _says_a_temperature(toks: Sequence[str]) -> bool:
    """"set the study to 70": a set command to a plausible indoor
    temperature, in a clause that names no other device."""
    return "set" in toks and any(
        toks[i] == "to" and toks[i + 1].isdigit() and 50 <= int(toks[i + 1]) <= 90 for i in range(len(toks) - 1)
    )


# Phrases the fast path (and the intent extractor) recognise on their own. One
# definition: the controller uses them to route, and the write-cue check counts
# a hit as a cue before negation is applied.
MOTION_CONTROL_PATTERNS: Tuple[str, ...] = (
    'leave the lights on',
    'keep the lights on',
    'lights stay on',
    "don't turn off the lights",
    'stop turning off the lights',
    'leave lights on',
    'keep lights on',
    'leave the lights off',
    'keep the lights off',
    'lights stay off',
    "don't turn on the lights",
    'stop turning on the lights',
    'leave lights off',
    'keep lights off',
    'taking a nap',
    'gonna nap',
    'going to nap',
    'nap time',
    'disable motion',
    'turn off motion',
    'no motion',
    'motion off',
    'disable the motion',
    'stop motion detection',
    'pause motion',
    'disable motion detection',
    'turn off motion detection',
    'enable motion',
    'turn on motion',
    'motion on',
    'resume motion',
    'enable the motion',
    'start motion detection',
    'resume normal',
    'turn motion back on',
    'motion detection on',
    'normal motion',
    'reset motion',
    'reset the motion',
    'keep it at this brightness',
    'lock the brightness',
    'stay at this brightness',
    'keep this brightness',
    'maintain this brightness',
    'brightness override',
    'lock brightness',
    'set motion brightness',
    'override brightness',
    'keep the brightness',
    'stay this bright',
    "don't change brightness",
)

# Typo corrections that are about a stray letter inside a short word ("onn",
# "oon", "oof", "offf") apply to the whole word only: as a substring "onn" turned
# "gonna" into "gona" and so broke "gonna nap".
WHOLE_WORD_TYPOS: FrozenSet[str] = frozenset({"onn", "oon", "oof", "offf"})


def correct_typo(text: str, typo: str, correction: str) -> str:
    """``text`` with ``typo`` replaced by ``correction``: everywhere, or as a
    whole word for the typos in ``WHOLE_WORD_TYPOS``."""
    if typo in WHOLE_WORD_TYPOS:
        return re.sub(rf"\b{re.escape(typo)}\b", correction, text)
    return text.replace(typo, correction)


# A motion condition ("when there's motion in the hall, ...") sets up an
# automation; "disable motion in the garage" is a motion-flow control and "is
# there motion" a question. Only a condition word before the motion word counts.
_MOTION_TRIGGER_RE = re.compile(
    r"\b(?:when|whenever|if|once|as soon as|any ?time)\b[^.;!?]{0,40}?\b(?:motion|movement)\b"
)


def is_motion_trigger_form(query: str) -> bool:
    return bool(_MOTION_TRIGGER_RE.search((query or "").lower()))


_QUESTION_LEAD = re.compile(r"\s*(?:is|are|was|were|does|do|did|has|have|any|anyone|anybody|when|where|what|which|who|whose|how)\b")


_QUESTION_FRAMING = re.compile(r"\bwhether\b|\btell me\b|\blet me know\b|\bi wonder\b|\bi want to know\b|\bfind out\b|\bcheck (?:if|whether)\b")


def is_motion_flow_command(query: str) -> bool:
    """A command that controls a room's motion flow ("resume motion in beta",
    "leave the lights on"): it holds a motion phrase, is not a motion
    condition ("when there's motion, ...") and is not a question about the
    state ("is motion on in beta", "is there no motion in the hall")."""
    lower = (query or "").lower()
    return (
        any(p in lower for p in MOTION_CONTROL_PATTERNS)
        and not is_motion_trigger_form(lower)
        and not _QUESTION_LEAD.match(lower)
        and not _QUESTION_FRAMING.search(lower)
        and not lower.rstrip().endswith("?")
        and classify_utterance(query).kind != UtteranceKind.STATE_QUESTION
    )


# "for 20 minutes", "for an hour", "for half an hour": the duration of a hold.
_NUMBER_OR_ARTICLE = (
    r"\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty[- ]five|sixty|few|couple of"
)
_HOLD_DURATION = (
    rf"(?:about |around )?(?:half an? |(?:{_NUMBER_OR_ARTICLE}) )(?:seconds?|minutes?|mins?|hours?|hrs?)"
)

_PLAIN_MOTION_COMMAND = re.compile(
    r"(?:please )?(?P<phrase>" + "|".join(re.escape(p) for p in sorted(MOTION_CONTROL_PATTERNS, key=len, reverse=True)) + r")"
    r"(?: in (?:the )?(?P<room>(?!please\b|for\b)[a-z0-9]+(?: (?!please\b|for\b)[a-z0-9]+){0,3}))?"
    rf"(?: for (?P<duration>{_HOLD_DURATION}))?"
    r"(?: please)?",
)


def _normalized_utterance(query: str) -> str:
    text = " ".join((query or "").lower().split())
    return text[:-1] if text.endswith(".") else text


def is_plain_motion_command(query: str, directory: Optional[room_context.RoomDirectory]) -> bool:
    """True only when the whole utterance is exactly

        [please ]<motion phrase>[ in [the ]<room>][ for <duration>][ please]

    (lowercased, whitespace collapsed, one trailing period dropped). It is an
    allowlist: any other word, any punctuation, a second room, a question, a
    negator, a conjunction or a stray "only" fails the full match, and the
    query takes the path it always took. The room, when named, must be one
    room the room parser resolves exactly."""
    match = _PLAIN_MOTION_COMMAND.fullmatch(_normalized_utterance(query))
    if match is None:
        return False
    room = match.group("room")
    if room is None:
        return True
    mentions = room_context.find_room_mentions(room, directory)
    return (
        len(mentions) == 1 and mentions[0].kind == "exact" and len(mentions[0].candidates) == 1
        and mentions[0].start == 0 and mentions[0].end == len(room_context.tokens(room))
    )


IMPLICIT_BRIGHTER: Tuple[str, ...] = (
    "can't see",
    'cant see',
    'cannot see',
    'too dark',
    'hard to see',
    'brighter',
    'more light',
    'brighten',
    'bump up',
    'bump it up',
    'brightness higher',
    'set brightness higher',
    'raise the lights',
    'lights higher',
    'higher brightness',
    'getting dark',
    'need some light',
    'need light',
    'need more light',
    'give me some light',
    'some light over here',
    'some light please',
    'bring up the lights',
    'bring the lights up',
    'kinda dim',
    'looking dim',
    'its dim',
    "it's dim",
    'bit dim',
    'pretty dim',
    'bring them back up',
    'bring it back up',
    'back up',
    'bring em back up',
    'lights weak',
    'weak af',
    'not bright enough',
    'need it brighter',
    'super bright',
    'really bright',
    'extra bright',
    'crazy bright',
)

IMPLICIT_DIMMER: Tuple[str, ...] = (
    'too bright',
    'dimmer',
    'less light',
    'darker',
    'softer light',
    'make it cozy',
    'lower the lights',
    'lower lights',
    'way down',
    'turn it down',
    'brightness lower',
    'set brightness lower',
    'bring the lights down',
    'bring down the lights',
    'lights lower',
    'lower brightness',
    'too much light',
    'thats too much',
    'not so bright',
    'a bit less',
    'little less light',
    'darken it up',
    'tone down',
    'tone it down',
    'take it easy on my eyes',
    'easy on my eyes',
    'fade the lights',
    'fade lights',
    'fade down',
    'fade it down',
    'fade them down',
    'fade out',
    'fading',
)

TURN_OFF_PHRASES: Tuple[str, ...] = (
    'lights out',
    'kill the light',
    'kill the lights',
    'cut the lights',
    'cut the light',
    'no more lights',
    'no more light',
    'shut it off',
    'shut off the lights',
    'shut them off',
    'forget the lights',
    'forget the light',
    'off with the lights',
    'off with the light',
    'every light off',
    'every light out',
    'kill all the lights',
    'kill all lights',
    'kill all',
    'everything off',
    'all off',
    'yo everything off',
)

BRIGHTER_PATTERNS: Tuple[str, ...] = (
    'brighter',
    'more light',
    'brighten',
    'too dark',
    'it is dark',
    'need more light',
    'can barely see',
    'increase brightness',
    'increase light',
    "can't see",
    'cant see',
    'cannot see',
    'hard to see',
    'difficult to see',
)

DIMMER_PATTERNS: Tuple[str, ...] = (
    'dimmer',
    'less light',
    'too bright',
    'it is too bright',
    'decrease brightness',
    'decrease light',
    'softer light',
    'make it cozy',
    'dim a little',
    'dim a bit',
    'a little dimmer',
    'a bit dimmer',
    'darker',
    'make it darker',
    'can you dim',
    'could you dim',
)

TEAM_TABLE = """SPORTS TEAM COLORS (use these hue values for team color requests):
NFL: Ravens(270,45), Steelers(50,0), Browns(30,0), Bengals(30,0), Chiefs(0,50), Bills(240,0), Dolphins(175,30), Patriots(220,0), Jets(120,0), Raiders(0,0), Broncos(30,240), Chargers(50,210), Cowboys(220,0), Eagles(180,0), Giants(240,0), Commanders(45,0), Packers(120,50), Bears(30,220), Lions(210,0), Vikings(270,50), 49ers(0,50), Seahawks(120,220), Cardinals(0,0), Rams(240,50), Falcons(0,0), Panthers(210,0), Saints(50,0), Buccaneers(0,0), Colts(240,0), Titans(210,0), Texans(220,0), Jaguars(175,50)
MLB: Orioles(30), Yankees(220), Red Sox(0), Blue Jays(240), Rays(210), Mets(240,30), Phillies(0), Braves(220,0), Marlins(210), Nationals(0), Cubs(240,0), Cardinals(0), Brewers(50,220), Reds(0), Pirates(50,0), Dodgers(240), Giants(30,0), Padres(50,45), Diamondbacks(0,175), Rockies(270,0), Astros(30,220), Rangers(240,0), Mariners(175,220), Angels(0), Athletics(120,50), Twins(220,0), White Sox(0,0), Tigers(30,220), Royals(240), Indians/Guardians(0,220)
NBA: Lakers(270,50), Celtics(120), Warriors(50,240), Bulls(0), Heat(0,0), Nets(0,0), Knicks(240,30), 76ers(240,0), Suns(30,270), Bucks(120), Hawks(0), Spurs(0,0), Mavericks(220,0), Nuggets(50,220), Clippers(0,240), Kings(270), Blazers(0,0), Jazz(50,120,220), Pacers(50,220), Cavaliers(45,0), Pistons(0,240), Magic(240,0), Hornets(175,270), Wizards(220,0), Grizzlies(210), Pelicans(50,0,220), Timberwolves(220,120), Thunder(240,30), Raptors(0,0)
NHL: Capitals(0,220), Penguins(50,0), Flyers(30,0), Rangers(240,0), Bruins(50,0), Canadiens(0,240), Maple Leafs(240), Red Wings(0), Blackhawks(0,0), Blues(240,50), Avalanche(45,240), Golden Knights(50,0), Kraken(175,0), Sharks(175), Ducks(30,50), Kings(0,0), Flames(0,50), Oilers(30,220), Canucks(120,240), Jets(220), Wild(120,0), Stars(120,0), Predators(50,220), Lightning(240), Panthers(0,220), Devils(0,0), Islanders(30,240), Hurricanes(0,0), Blue Jackets(220,0), Senators(0,50), Sabres(50,220)
College: Michigan(50,240), Ohio State(0), Penn State(220), Alabama(45,0), Georgia(0,0), Clemson(30,270), LSU(50,270), Notre Dame(50,240), Oklahoma(45,0), Texas(210,30), USC(45,0), UCLA(50,240), Florida(30,240), Auburn(30,220), Tennessee(30,0), Wisconsin(0,0), Oregon(120,50), Washington(270,50), Florida State(45,0), Miami(30,120), NC State(0,0), Duke(240), UNC(210), Kentucky(240), Kansas(240,0), Gonzaga(220,0), Villanova(220,0), Syracuse(30), Army(50,0), Navy(220,50), Air Force(240,0), West Point(50,0)
Soccer/MLS: LAFC(50,0), LA Galaxy(220,50), Atlanta United(0,50), Seattle Sounders(120,240), NYCFC(210,30), Inter Miami(0,270), Austin FC(120,0), Nashville SC(50,0), Portland Timbers(120,50), Philadelphia Union(50,220), Columbus Crew(50,0), FC Cincinnati(240,30), Toronto FC(0), Vancouver Whitecaps(220,240), Montreal(0,220)
Premier League: Manchester United(0), Manchester City(210), Liverpool(0), Chelsea(240), Arsenal(0,0), Tottenham(220,0), Newcastle(0,0), Brighton(240), Aston Villa(45,210), West Ham(45,210), Everton(240), Crystal Palace(0,240), Wolves(45,0), Leicester(240), Fulham(0,0), Bournemouth(0,0), Brentford(0,0), Nottingham Forest(0)
World: Barcelona(45,0), Real Madrid(0,0), Bayern Munich(0), Juventus(0,0), PSG(220,0), AC Milan(0,0), Inter Milan(240,0), Dortmund(50,0), Ajax(0,0), Celtic(120,0), Rangers(240)"""

# A command that changes a device must carry a cue in the user's words, and the
# cue must name this kind of device (or no device at all): the server never
# infers a write from a room, a speaker position or a model's guess alone.
# Every device kind is judged except reads, locks, scenes (a door has its own
# rules: an opening verb plus a name), the read-only appliance kinds and a
# cover's opening; a kind with no table is refused (fail closed).
WRITE_CUE_ACTIONS: FrozenSet[str] = frozenset({"set_color", "set_brightness", "set_level", "increase", "decrease"})
_ON_OFF_WORDS = frozenset({
    "on", "off", "lit", "kill", "dim", "dimmer", "brighten", "brighter", "dark", "darker", "darken",
})
# Evidence in the words for a colour or brightness action: the action's name
# alone is a model's guess.
COLOR_WORDS = frozenset({
    "red", "orange", "yellow", "green", "blue", "white", "purple", "pink", "violet", "cyan", "teal", "magenta",
    "gold", "amber", "warm", "cool", "rainbow", "sunset", "sunrise", "random", "christmas", "color", "colors",
    "colour", "colours", "ravens", "orioles", "steelers", "team",
    "ocean", "vibe", "vibes", "mood", "theme", "party", "disco", "gradient", "pastel", "neon", "candle", "fire",
    "forest", "sky", "sea", "lava", "romantic", "relax", "cozy", "calm", "wolverines", "maize", "glow", "glowing",
    "lavender", "lilac", "indigo", "turquoise", "crimson", "scarlet", "coral", "peach", "mint", "lime", "navy",
    "brown", "grey", "gray", "silver", "beige", "maroon", "cream", "aqua", "salmon", "rose", "ruby", "emerald",
})
# Team names from the extractor prompt's own table (``TEAM_TABLE``), less the
# words that are ordinary English.
_TEAM_STOPWORDS = frozenset({
    "state", "city", "united", "new", "fc", "sc", "wild", "heat", "magic", "stars", "kings", "suns", "jazz", "nets",
    "blues", "reds", "jets", "wolves",
})


def _team_words() -> FrozenSet[str]:
    names = re.findall(r"([A-Za-z0-9][A-Za-z0-9 .]*?)\(", TEAM_TABLE)
    out = set()
    for name in names:
        for tok in tokens(name):
            if len(tok) > 2 and tok not in _TEAM_STOPWORDS:
                out.add(tok)
    return frozenset(out)


TEAM_WORDS: FrozenSet[str] = _team_words()
BRIGHTNESS_WORDS = frozenset({
    "bright", "brighter", "brightness", "brighten", "dim", "dimmer", "dimmed", "dark", "darker", "darken",
    "level", "percent", "max", "maximum", "full", "half", "low", "lower", "high", "higher", "more", "less",
    "up", "down", "warmer", "cooler", "softer", "louder", "quieter",
})
_CLIMATE_CUE_WORDS = frozenset({
    "warm", "cool", "hot", "hotter", "colder", "cold", "chilly", "freezing", "toasty", "temp", "crank",
})
_MEDIA_CUE_WORDS = frozenset({
    "pause", "resume", "stop", "skip", "next", "previous", "louder", "quieter", "unmute", "mute",
    "watch", "channel", "volume", "up", "down", "game", "show", "movie",
})
_CUE_WORDS = {
    "light": DEVICE_WORDS["light"],
    "fan": DEVICE_WORDS["fan"],
    "switch": DEVICE_WORDS["switch"],
    "media": DEVICE_WORDS["media"] | _MEDIA_CUE_WORDS,
    "climate": DEVICE_WORDS["climate"] | _CLIMATE_CUE_WORDS,
    "cover": DEVICE_WORDS["cover"] | frozenset({"close", "shut", "lower", "raise"}),
    "bed": frozenset({"bed", "mattress", "warmer", "pad", "blanket"}),
}
# The device_type a model writes, normalised before the check; anything else
# stays unknown (refused).
_CUE_KINDS = {
    "light": "light", "lights": "light", "lamp": "light", "lamps": "light", "light_group": "light",
    "fan": "fan", "fans": "fan",
    "switch": "switch", "switches": "switch", "plug": "switch", "outlet": "switch",
    "climate": "climate", "thermostat": "climate", "hvac": "climate", "ac": "climate",
    "cover": "cover", "covers": "cover", "blinds": "cover", "shades": "cover", "curtains": "cover",
    "media": "media", "media_player": "media", "tv": "media", "television": "media", "speaker": "media",
    "speakers": "media",
    "bed_warmer": "bed", "motion_control": "motion", "motion": "motion",
}
# The one name for each device type the dispatch, the planner and the write
# precheck understand. A model may write any alias; everything downstream sees
# only these.
_CANONICAL_TYPES = {
    "light": "light", "lights": "light", "lamp": "light", "lamps": "light", "light_group": "light",
    "fan": "fan", "fans": "fan",
    "switch": "switch", "switches": "switch", "plug": "switch", "plugs": "switch", "outlet": "switch",
    "outlets": "switch",
    "climate": "climate", "thermostat": "climate", "hvac": "climate", "ac": "climate",
    "cover": "cover", "covers": "cover", "blind": "cover", "blinds": "cover", "shades": "cover",
    "curtains": "cover",
    "media": "media_player", "media_player": "media_player", "tv": "media_player", "television": "media_player",
    "speaker": "media_player", "speakers": "media_player",
    "bed_warmer": "bed_warmer", "motion_control": "motion_control", "motion": "motion_control",
    "lock": "lock", "scene": "scene", "script": "script", "oven": "oven", "fridge": "fridge",
    "freezer": "freezer", "appliance": "appliance", "sensor": "sensor",
}


def canonical_device_type(device_type: Any) -> Any:
    """The canonical name of a device type ("television" is ``media_player``,
    "thermostat" is ``climate``), or the value unchanged when it is not a type
    this module knows (the cue check refuses an unknown type)."""
    if not isinstance(device_type, str):
        return device_type
    return _CANONICAL_TYPES.get(device_type.strip().lower(), device_type)


# Kinds a cue is not asked of: doors (own rules), scenes (grounded), and the
# kinds whose handlers only read.
_CUE_EXEMPT_TYPES = frozenset({"lock", "oven", "fridge", "freezer", "appliance", "sensor"})
# A scene or script runs a routine: it needs a command to run it (or the
# routine's own phrase as the whole utterance), not a mention of it.
_ROUTINE_TYPES = frozenset({"scene", "script"})
# A clause that starts with one of these is a command about a device.
_CONTROL_CUE_VERBS = frozenset({
    "turn", "switch", "dim", "brighten", "start", "stop", "play", "pause", "resume", "kill", "cut", "raise",
    "lower", "activate", "deactivate", "enable", "disable", "warm", "heat", "cool", "run",
})
_CONTROL_CUE_VERBS_BY_KIND = {"media": frozenset({"watch", "change"})}
_LEADING_FILLER = frozenset({
    "please", "hey", "ok", "okay", "can", "could", "would", "will", "you", "just", "go", "ahead", "lets", "let",
    "us", "me", "i", "want", "need", "like", "to", "now", "then", "also",
})
# What may follow a control verb without being a thing it acts on.
_OBJECT_FILLER = frozenset({
    "the", "a", "an", "my", "it", "them", "that", "this", "those", "these", "up", "down", "on", "off", "again",
    "please", "now", "back", "for", "me", "to", "all", "everything", "everywhere", "in", "here", "there",
    "around", "of", "some", "more", "less", "louder", "quieter", "higher", "lower", "thanks", "thank", "you",
})
_NEGATORS = frozenset({
    "not", "never", "dont", "wont", "cant", "without", "don", "doesn", "didn", "won", "couldn", "shouldn",
    "wouldn", "isn", "aren",
})
# A negator that does not negate the command after it: "don't forget to ...",
# "why don't you ...", "won't you ...", "can't sleep/see ...".
_NON_NEGATING_NEXT = frozenset({"forget", "you", "sleep", "see", "stand", "wait"})
_NEGATION_BRIDGE = frozenset({"t", "ever", "really", "just", "please", "you", "to", "even", "want", "need", "be", "go", "ever"})
_CUE_CLAUSE_SPLIT = re.compile(r"[,;.!?]|\b(?:and|then|also|plus)\b")
_TEMPERATURE_VERBS = frozenset({"set", "make", "put", "keep", "bring", "need", "want", "turn"})
_TEMPERATURE_UNITS = frozenset({"degrees", "degree", "celsius", "fahrenheit", "c", "f", "in"})
_ALL_CONTROL_VERBS = _CONTROL_CUE_VERBS | _CONTROL_CUE_VERBS_BY_KIND["media"] | frozenset(
    {"make", "set", "put", "keep", "leave", "open", "close", "touch", "change"}
)


def has_negator(text: Optional[str]) -> bool:
    """Whether the words hold a negator that negates a command after it."""
    return any(_negation_cut(list(tokens(c)))[1] for c in _CUE_CLAUSE_SPLIT.split((text or "").lower()))


def _negation_cut(toks: List[str]) -> Tuple[List[str], bool]:
    """A clause's tokens with every negated stretch removed, and whether any
    was. A negator negates the command that follows it, up to the next control
    verb that starts a new command: "don't turn on the lights" is negated
    whole; "I can't sleep turn off the lights" keeps its second command."""
    out: List[str] = []
    negated = False
    i = 0
    while i < len(toks):
        tok = toks[i]
        is_neg = tok in _NEGATORS or (tok == "can" and tuple(toks[i + 1:i + 2]) == ("t",))
        if not is_neg:
            out.append(tok)
            i += 1
            continue
        after = i + 1
        while after < len(toks) and toks[after] == "t":
            after += 1
        if (after < len(toks) and toks[after] in _NON_NEGATING_NEXT) or (i and toks[i - 1] == "why"):
            i = after  # "don't forget", "why don't you", "can't sleep": not a negation
            continue
        negated = True
        probe = after
        while probe < len(toks) and toks[probe] in _NEGATION_BRIDGE:
            probe += 1
        if probe < len(toks) and toks[probe] in _ALL_CONTROL_VERBS:
            break  # the command right after the negator is the negated one: nothing after it counts
        verb = next((j for j in range(after, len(toks)) if toks[j] in _CONTROL_CUE_VERBS), None)
        if verb is None:
            break
        i = verb  # content words came first: the negation ends where the next command starts
    return out, negated


def _positive_clauses(query: str, *, negation: bool) -> List[List[str]]:
    """The query's clauses as token lists, negated stretches removed."""
    out: List[List[str]] = []
    for clause in _CUE_CLAUSE_SPLIT.split((query or "").lower()):
        toks = list(tokens(clause))
        if negation:
            toks = _negation_cut(toks)[0]
        if toks:
            out.append(toks)
    return out


def _temperature_in(toks: Sequence[str]) -> bool:
    """A plausible indoor temperature (50-90 Fahrenheit or 10-32 Celsius) said
    as a setting: "to 70", "make it 72", "72 degrees", "72 in the study", or a
    number beside set/make/put/keep/bring."""
    for i, tok in enumerate(toks):
        if not tok.isdigit() or not (50 <= int(tok) <= 90 or 10 <= int(tok) <= 32):
            continue
        before = toks[i - 1] if i else ""
        after = toks[i + 1] if i + 1 < len(toks) else ""
        if before == "to" or after in _TEMPERATURE_UNITS or any(v in toks for v in _TEMPERATURE_VERBS):
            return True
    return False


def _has_brightness_parameter(parameters: Any) -> bool:
    return isinstance(parameters, Mapping) and any(
        parameters.get(k) is not None for k in ("brightness", "brightness_percent", "level")
    )


def _builtin_phrase(query: str) -> bool:
    """A phrase the fast path recognises on its own ("I can't see", "too
    much light", "fade the lights"). It is a cue however the sentence is
    negated, because the fast path would have acted on it."""
    lower = query.lower()
    return any(p in lower for p in IMPLICIT_BRIGHTER + IMPLICIT_DIMMER + TURN_OFF_PHRASES + BRIGHTER_PATTERNS + DIMMER_PATTERNS)


def _verb_leads(clauses: List[List[str]], verbs: FrozenSet[str], *, object_ok: bool) -> bool:
    """Whether a clause starts with a control verb (after filler). With
    ``object_ok`` False the verb must stand alone or act on a pronoun or
    particle ("turn it up", "pause"): "start the coffee maker" needs the
    thing's name to be a known device."""
    for toks in clauses:
        lead = [t for t in toks if t not in _LEADING_FILLER]
        if lead and lead[0] in verbs:
            if object_ok or not [t for t in lead[1:] if t not in _OBJECT_FILLER]:
                return True
    return False


WRONG_DEVICE = "wrong_device"
NO_CUE = "no_cue"

# The actions that set or clear a light's effect. Not in WRITE_CUE_ACTIONS: their
# cue is the effect itself, said in the words, never a generic light word, on/off
# word or control verb.
EFFECT_ACTIONS = frozenset({"set_effect", "clear_effect"})
_EFFECT_WORDS = frozenset({"effect", "effects"})
# The words that turn an effect off. "mode" also counts when the effect is named
# ("candlelight mode off", "turn off candlelight mode").
_CLEAR_WORDS = frozenset({"off", "stop", "clear", "cancel", "end", "remove", "disable", "kill"})
# A named effect with neither "effect" nor "mode" said ("stop the twinkle"): bare
# "off" is not enough, or "turn off the kitchen lights" would clear an effect
# the model called "kitchen".
_STOP_WORDS = _CLEAR_WORDS - {"off"}
# "no more effects", the one clear that has no verb.
_NO_EFFECTS_CLAUSES = frozenset({("no", "effects"), ("no", "more", "effects")})
# Words that can't make an effect "named": an effect made only of these would
# be backed by the on/off or filler words of any command.
_EFFECT_FILLER = _LEADING_FILLER | _OBJECT_FILLER | _ON_OFF_WORDS | frozenset({"effect", "effects", "mode", "light", "lights"})


def _effect_name(effect: Any) -> Optional[Sequence[str]]:
    """The words an effect is said with, or None when it can't be named in words:
    not a string, no words, a clear alias, or only on/off and filler words."""
    if not isinstance(effect, str):
        return None
    words = light_effects.tokens(effect)
    if not words or light_effects.is_clear_alias(effect) or all(w in _EFFECT_FILLER for w in words):
        return None
    return words


def _effect_named(parameters: Any, clauses: Any) -> Optional[Tuple[Sequence[str], Tuple[int, int]]]:
    """The first clause that says the model's effect, with where."""
    effect = parameters.get("effect") if isinstance(parameters, dict) else None
    if _effect_name(effect) is None:
        return None
    for toks in clauses:
        span = light_effects.spoken_span(effect, toks)
        if span is not None:
            return toks, span
    return None


def _outside(toks: Sequence[str], span: Tuple[int, int], words: FrozenSet[str]) -> bool:
    return any(t in words for i, t in enumerate(toks) if not span[0] <= i < span[1])


def _device_words_spoken(
    act: str, spoken: FrozenSet[str], clauses: Any, parameters: Any, effect_listed: bool = False,
) -> FrozenSet[str]:
    """The spoken words that can name a device. For an effect, only the matched
    words of its own name ("tv simulator") are set aside, and only when the
    words also say "effect" or a light word (an effect called "Fan" doesn't turn
    "turn on the fan" into a light request). "play" is a verb, not a media
    request, only in a clause that says "effect"."""
    if act not in EFFECT_ACTIONS:
        return spoken
    found = _effect_named(parameters, clauses) if (
        effect_listed or spoken & (_EFFECT_WORDS | DEVICE_WORDS["light"])
    ) else None
    remaining = set()
    for toks in clauses:
        drop = set()
        if found is not None and found[0] is toks:
            drop.update(range(*found[1]))
        if set(toks) & _EFFECT_WORDS:
            drop.update(i for i, t in enumerate(toks) if t in ("play", "playing"))
        remaining.update(t for i, t in enumerate(toks) if i not in drop)
    return frozenset(remaining)


def _effect_cue(act: str, clauses: Any, spoken: FrozenSet[str], parameters: Any) -> Optional[str]:
    """None when the words themselves ask for the effect.

    Setting needs the effect named in the words (the model's guess counts only
    through them), with no clear word beside it. Clearing needs, in ONE clause,
    the word "effect" with a clear word ("turn off the effect"), or a named
    effect with the word "mode" and a clear word ("candlelight mode off"), or a
    named effect with a stop word ("stop the twinkle"; bare "off" is not
    enough). The word "effect" alone backs only a clear, and "that had no
    effect" backs nothing."""
    found = _effect_named(parameters, clauses)
    if act == "set_effect":
        return None if found is not None and not _outside(*found, _CLEAR_WORDS) else NO_CUE
    for toks in clauses:
        words = set(toks)
        if tuple(toks) in _NO_EFFECTS_CLAUSES or (words & _EFFECT_WORDS and words & _CLEAR_WORDS):
            return None
    if found is not None:
        toks, span = found
        moded = _outside(toks, span, frozenset({"mode"}))
        return None if _outside(toks, span, _CLEAR_WORDS if moded else _STOP_WORDS) else NO_CUE
    return NO_CUE


def write_cue_problem(
    device_type: Any, action: Any, query: Optional[str], parameters: Any = None, *, named_entity: bool = False,
    kind_hint: Optional[UtteranceKind] = None, whole_scene_phrase: bool = False,
    assistant_names: Iterable[str] = (), allow_scheduled: bool = False, effect_listed: bool = False,
) -> Optional[str]:
    """None when a device-changing command is backed by the words; else
    ``NO_CUE`` (nothing in the words asks for a change) or ``WRONG_DEVICE``
    (the words name a different kind of device).

    A cue is a device word for the command's kind, a built-in fast-path
    phrase, an on/off word, a control verb that starts a clause, whole-house
    wording (lights), a temperature (climate), or, for a colour or brightness
    action, a colour, brightness or team word. Generic cues count only when
    the words name this kind of device or none at all; a cue after a negator
    does not count. ``named_entity`` says the words call one of the house's
    devices of this kind by its name; it lets a control verb act on that
    named thing ("start the coffee maker") and is no cue on its own.

    Only the words that are a request count: when the utterance holds a
    command clause, the command's words alone; else every clause that is not
    narration ("they turn the lights off at night") or a condition ("if it
    gets dark"). ``kind_hint`` is the classifier's verdict on the utterance:
    a scene or script runs for an IMPERATIVE utterance, or for its own phrase
    as the whole utterance (``whole_scene_phrase``); with no hint it is not
    judged. A read is never a problem, a lock is judged elsewhere, a cover's
    opening by the door rules, and a command with no words (a sequence step,
    checked per clause) is not judged."""
    act = action.strip().lower() if isinstance(action, str) else ""
    canonical = canonical_device_type(device_type)
    dtype = canonical.strip().lower() if isinstance(canonical, str) else None
    if act in EFFECT_ACTIONS and (query is None or dtype != "light"):
        return NO_CUE  # an effect is backed only by words, and only for a light
    if query is None:
        return None
    if act in READ_ONLY_ACTIONS:
        return None
    if dtype in _ROUTINE_TYPES:
        if kind_hint is None or kind_hint is UtteranceKind.IMPERATIVE or whole_scene_phrase:
            return None
        return NO_CUE
    if dtype in _CUE_EXEMPT_TYPES:
        return None
    kind = _CUE_KINDS.get(dtype or "")
    if kind is None:
        return NO_CUE
    if kind == "cover" and is_opening_action("cover", act):
        return None
    query = cue_text(query, assistant_names, allow_scheduled=allow_scheduled)
    if not query or not query.strip():
        return NO_CUE  # everything said was narration or a condition
    if kind == "motion":
        lower = query.lower()
        return None if any(p in lower for p in MOTION_CONTROL_PATTERNS) else NO_CUE
    clauses = _positive_clauses(query, negation=True)
    spoken = frozenset(t for toks in clauses for t in toks)
    # The words name another kind of device and none of this kind's: wrong
    # device, whatever else they say ("make the tv brighter" for a light).
    # Words that are also colour or brightness words ("warmer") name no device,
    # and whole-house wording is the scope of a light command.
    own_named = bool(spoken & DEVICE_WORDS.get(kind, _CUE_WORDS.get(kind, frozenset()))) or (
        kind == "light" and room_context.is_explicit_whole_house(" ".join(" ".join(t) for t in clauses))
    )
    device_spoken = _device_words_spoken(act, spoken, clauses, parameters, effect_listed)
    if not own_named and any(
        device_spoken & (DEVICE_WORDS[k] - COLOR_WORDS - BRIGHTNESS_WORDS) for k in DEVICE_WORDS if k != kind
    ):
        return WRONG_DEVICE
    if act in EFFECT_ACTIONS:
        return _effect_cue(act, clauses, spoken, parameters) if clauses else NO_CUE
    if kind == "light" and _builtin_phrase(query):
        return None
    if not clauses:
        return NO_CUE  # everything said was negated
    if spoken & _CUE_WORDS[kind]:
        return None
    if kind == "light" and own_named:
        return None
    colour_evidence = bool(spoken & (COLOR_WORDS | BRIGHTNESS_WORDS | TEAM_WORDS)) or "%" in query
    verbs = _CONTROL_CUE_VERBS | _CONTROL_CUE_VERBS_BY_KIND.get(kind, frozenset())
    if act in WRITE_CUE_ACTIONS:
        if colour_evidence or _verb_leads(clauses, verbs, object_ok=True):
            return None
        return NO_CUE
    if _has_brightness_parameter(parameters) and (colour_evidence or "%" in query):
        return None
    if spoken & _ON_OFF_WORDS or _verb_leads(clauses, verbs, object_ok=named_entity):
        return None
    if kind == "climate" and any(_temperature_in(toks) for toks in clauses):
        return None
    return NO_CUE


def lacks_write_cue(
    device_type: Any, action: Any, query: Optional[str], parameters: Any = None, *, named_entity: bool = False,
    kind_hint: Optional[UtteranceKind] = None, whole_scene_phrase: bool = False,
    assistant_names: Iterable[str] = (), allow_scheduled: bool = False, effect_listed: bool = False,
) -> bool:
    """``write_cue_problem`` as a yes/no."""
    return write_cue_problem(
        device_type, action, query, parameters, named_entity=named_entity, kind_hint=kind_hint,
        whole_scene_phrase=whole_scene_phrase, assistant_names=assistant_names, allow_scheduled=allow_scheduled,
        effect_listed=effect_listed,
    ) is not None


def media_write_allowed(
    kind: UtteranceKind, query: Optional[str], assistant_names: Iterable[str] = (),
) -> bool:
    """Whether the words may start a music or TV write: an IMPERATIVE
    utterance, or an UNKNOWN one with a clause that is a request (not
    narration, a condition or a negation). A question never writes."""
    if kind is UtteranceKind.IMPERATIVE:
        return True
    if kind is UtteranceKind.STATE_QUESTION:
        return False
    clauses = analyze_clauses(query, assistant_names).clauses
    return any(c.cue_ok and not c.empty and not c.negated for c in clauses)


def names_device(text: Optional[str]) -> bool:
    """Whether the words name a device of any kind in the table. Words that
    are also colour or brightness words ("warmer") name no device."""
    spoken = frozenset(tokens(text or ""))
    return any(spoken & (words - COLOR_WORDS - BRIGHTNESS_WORDS) for words in DEVICE_WORDS.values())


def device_backed(
    device_type: Any, clause: Optional[str], query: Optional[str], action: Any = None,
    antecedent: Optional[str] = None,
) -> bool:
    """Whether the step's own words ask for this kind of device.

    A word of the type's table in the clause backs it. A clause that names
    another device does not (a colour or brightness action on a light
    excepted). A clause that names no device at all ("then raise it") is a
    referent and takes its antecedent's device: ``antecedent`` is the nearest
    earlier clause that names one, and the step's type must be one of its
    kinds. With no antecedent, the utterance backs it, and a light is the
    default only when the utterance names no device anywhere. A type the table
    does not know is not backed."""
    kind = device_kind(device_type)
    if kind is None:
        return False

    def words(text: Optional[str]) -> frozenset:
        return frozenset(tokens(text or ""))

    clause_words = words(clause)
    own = bool(clause_words & DEVICE_WORDS[kind])
    # A clause that names another kind of device and none of this kind's does
    # not back the step, whatever the action ("make the tv brighter" is not a
    # light step). Words that are also colour or brightness words name no device.
    if not own and any(
        clause_words & (DEVICE_WORDS[k] - COLOR_WORDS - BRIGHTNESS_WORDS) for k in DEVICE_WORDS if k != kind
    ):
        return False
    if own:
        return True
    if kind == "climate" and _says_a_temperature(tokens(clause or "")):
        return True
    if antecedent is not None:
        return bool(words(antecedent) & DEVICE_WORDS[kind])
    if not names_device(query):
        return kind == "light"
    return bool(words(query) & DEVICE_WORDS[kind])


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


_NEGATING = frozenset({"not", "never", "without"})


def _without_negated(query: Optional[str]) -> Optional[str]:
    """The utterance less what a negation rules out: in each clause, the
    words from "not" on ("unlock the front door, not the back door" names the
    front door only)."""
    if not query:
        return query
    kept = []
    for clause in re.split(r"[,;.]", query):
        words = list(tokens(clause))
        cut = next((i for i, w in enumerate(words) if w in _NEGATING), len(words))
        kept.extend(words[:cut])
    return " ".join(kept)


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
    query = _without_negated(query)
    refs = tuple(Ref(eid, label) for eid, label in entities.items())
    dist = {r.entity_id: _distinguishing(r.label, domain) for r in refs}
    toks = list(tokens(query)) if query else []
    qset = frozenset(toks)
    named_by = TOKENS_AND_NOUN if noun_present(query, domain) else TOKENS

    if isinstance(carried, str) and carried in entities:
        if carried_trusted or (
            _is_bare_referent(toks, entities[carried], domain)
            and all(r.entity_id == carried for r in _query_matches(refs, dist, qset))
        ):
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


def target_nouns(model_target: Any, domain: str) -> FrozenSet[str]:
    """The access nouns a model's target itself says (``garage``, ``gate``):
    the words ``_target_tokens`` leaves out."""
    if not isinstance(model_target, str):
        return frozenset()
    if re.match(r"^[a-z_]+\.", model_target.strip().lower()):
        model_target = model_target.split(".", 1)[1]
    return frozenset(t for t in tokens(model_target) if t in _NOUNS[domain])


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

"""Which room an utterance means: the vocabulary, the mentions in a sentence,
and the decision.

Pure. Nothing here does I/O, reads a runtime singleton, or touches
permissions. Callers (the controller, the classify node) hand in a
``RoomDirectory`` and the speaker's room and get a ``RoomDecision`` back; the
write path then authorizes whatever it names.

The vocabulary (``RoomDirectory``) is built from Home Assistant's area
registry and the deployment's own configuration, never from a list of house
names. ``COMMON_ROOM_WORDS`` (generic nouns) only lets an utterance's room
word be *recognized* when the directory has nothing for it; it never makes a
speaker's room count and never chooses targets.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from shared.ha_registry import room_key, tokens

# ---------------------------------------------------------------------------
# Whole-house wording
# ---------------------------------------------------------------------------

# The write fan-out gate proceeds without a confirmation when the utterance
# carries one of these words. One definition: write_fanout imports it.
SCOPE_CUE_RE = re.compile(r"\b(?:all|every|everything|everywhere|whole|entire|house)\b", re.IGNORECASE)

# "all the lights", "every light", "everything": these quantify lights, so a
# room named in the same sentence wins. The lookbehind stops the speech-to-
# text homophone "the all lights" (for "hall lights").
WHOLE_HOUSE_LIGHT_RE = re.compile(
    r"(?<!\bthe )\ball (?:of )?(?:the )?lights\b|\bevery light\b|\beverything\b",
    re.IGNORECASE,
)
# These quantify rooms, so they beat a room named in the sentence.
WHOLE_HOUSE_ROOM_RE = re.compile(
    r"\b(?:the )?whole house\b|\bentire house\b|\bevery room\b|\ball (?:the )?rooms\b"
    r"|\beverywhere\b|\b(?:throughout|all over) the house\b",
    re.IGNORECASE,
)


def is_explicit_whole_house(query: Optional[str]) -> bool:
    """True when the wording itself asks for the whole house. Says nothing
    about a room named in the same sentence; ``decide_room`` weighs that."""
    text = query or ""
    return bool(WHOLE_HOUSE_ROOM_RE.search(text) or WHOLE_HOUSE_LIGHT_RE.search(text))


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Generic room nouns, enough to recognize "the kitchen lights" when neither
# Home Assistant nor the configuration names the room. Never a house's own
# names, and never used to decide which lights a command writes.
COMMON_ROOM_WORDS: Tuple[str, ...] = (
    "office", "kitchen", "bedroom", "living room", "livingroom", "bathroom",
    "master bedroom", "master bathroom", "master bath", "guest room", "hallway", "hall",
    "basement", "attic", "garage", "porch", "deck", "patio", "dining room",
    "den", "family room", "study", "library", "laundry room",
    "first floor", "1st floor", "main floor", "ground floor", "downstairs",
    "second floor", "2nd floor", "upstairs", "upper floor", "lower level", "cellar",
)

# Words that never name a room on their own, so a room named "On" or "Lights"
# can't be matched, and a piece of a longer name made only of these ("room"
# from "Living Room") isn't a partial match.
_NON_ROOM_TOKENS = frozenset({
    "the", "a", "an", "of", "in", "on", "off", "at", "to", "and", "or", "my", "our", "your",
    "it", "them", "that", "this", "please", "light", "lights", "lamp", "lamps", "room", "rooms",
    "turn", "switch", "set", "make", "dim", "brighten", "all", "every", "everything",
    "everywhere", "house", "whole", "entire", "both", "each", "one", "up", "down", "mode",
    "scene", "color", "colour", "level", "percent", "bright", "brightness", "status",
    "is", "are", "what", "which", "any", "some", "with", "for", "lit", "group", "area",
})

EXCLUSION_PHRASES: Tuple[Tuple[str, ...], ...] = (
    ("except",), ("excepting",), ("but", "not"), ("other", "than"), ("excluding",),
    ("apart", "from"), ("aside", "from"), ("besides",), ("not", "including"), ("not", "the"),
    ("but",),
)
# Words that often carry an exclusion without being one of the phrases above;
# a fast path that sees one and parsed no excluded room leaves it to the model.
EXCLUSION_DEFERRAL_WORDS = frozenset({"without", "unless", "minus", "skip", "leave", "keep"})
_UNIVERSAL_QUANTIFIERS = frozenset({"all", "both", "every", "each"})
_LIGHT_NOUNS = frozenset({"light", "lights", "lamp", "lamps"})
_STRIP_DEVICE_NOUNS = frozenset({"light", "lights", "lamp", "lamps", "group", "all"})
_NUMBER_IN_PARENS = re.compile(r"\((\d+)\)")


def spoken_label(name: str) -> str:
    """How a room is said aloud: its name lower-cased, ``(2)`` read as ``2``."""
    return " ".join(_NUMBER_IN_PARENS.sub(r"\1", name or "").lower().split())


@dataclass(frozen=True)
class RoomRef:
    key: str
    label: str


@dataclass(frozen=True)
class _Entry:
    key: str
    label: str
    tokens: Tuple[str, ...]
    phrases: Tuple[Tuple[str, ...], ...]
    members: Tuple[str, ...] = ()
    matchable: bool = True

    def ref(self) -> RoomRef:
        return RoomRef(self.key, self.label)


def _matchable(phrase: Tuple[str, ...]) -> bool:
    return bool(phrase) and not all(t in _NON_ROOM_TOKENS or t.isdigit() for t in phrase)


def _strip_device_nouns(name: str) -> str:
    parts = list(tokens(name))
    while parts and parts[-1] in _STRIP_DEVICE_NOUNS:
        parts.pop()
    return " ".join(parts)


class RoomDirectory:
    """The rooms a deployment has, and the phrases that name them.

    Build it with ``build``. An entry is keyed by ``room_key`` of its name;
    two sources naming the same key are one room. A key shared by several
    registry areas (``Living-Room`` and ``Living Room``) can't be told apart
    by speech, so it matches no phrase, but a speaker's room of that key
    still resolves.
    """

    def __init__(self, entries: Mapping[str, _Entry], *, unmatchable: int = 0, collisions: int = 0):
        self._entries = dict(entries)
        self.unmatchable_count = unmatchable
        self.collision_count = collisions
        self._exact: Dict[Tuple[str, ...], List[str]] = {}
        self._partial: Dict[Tuple[str, ...], List[str]] = {}
        self._common: Dict[Tuple[str, ...], RoomRef] = {}
        self.max_phrase_len = 1
        for entry in self._entries.values():
            if not entry.matchable:
                continue
            for phrase in entry.phrases:
                if _matchable(phrase):
                    self._index(self._exact, phrase, entry.key)
            for width in range(1, len(entry.tokens)):
                for start in range(len(entry.tokens) - width + 1):
                    part = entry.tokens[start:start + width]
                    if _matchable(part):
                        self._index(self._partial, part, entry.key)
        for word in COMMON_ROOM_WORDS:
            phrase = tokens(word)
            if _matchable(phrase):
                self._common[phrase] = RoomRef(room_key(word), spoken_label(word))
                self.max_phrase_len = max(self.max_phrase_len, len(phrase))

    def _index(self, table: Dict[Tuple[str, ...], List[str]], phrase: Tuple[str, ...], key: str) -> None:
        bucket = table.setdefault(phrase, [])
        if key not in bucket:
            bucket.append(key)
        self.max_phrase_len = max(self.max_phrase_len, len(phrase))

    # -- construction -----------------------------------------------------

    @classmethod
    def empty(cls) -> "RoomDirectory":
        return cls({})

    @classmethod
    def build(
        cls,
        *,
        areas: Iterable = (),
        light_group_keys: Iterable[str] = (),
        room_groups: Iterable[Mapping] = (),
        satellite_rooms: Iterable[str] = (),
        group_friendly_names: Iterable[str] = (),
    ) -> "RoomDirectory":
        """``areas``: registry areas (``area_id``, ``name``, ``aliases``, ``key``).
        ``light_group_keys``: the keys of ``HA_LIGHT_GROUPS``. ``room_groups``:
        admin room groups (``name``, ``display_name``, ``aliases``, ``members``
        of ``room_name``/``display_name``). ``satellite_rooms``: the values of
        ``HA_SATELLITE_ROOM_MAP``. ``group_friendly_names``: light-group names,
        passed only while the registry is unavailable."""
        pending: Dict[str, dict] = {}

        def add(name: str, *, aliases: Iterable[str] = (), members: Iterable[str] = (), area: bool = False) -> None:
            key = room_key(name)
            if not key:
                return
            slot = pending.setdefault(key, {
                "label": spoken_label(name), "phrases": [], "members": [], "areas": 0, "tokens": tokens(name),
            })
            for text in (name, *aliases):
                phrase = tokens(text)
                if phrase and phrase not in slot["phrases"]:
                    slot["phrases"].append(phrase)
            for member in members:
                if member not in slot["members"]:
                    slot["members"].append(member)
            if area:
                slot["areas"] += 1

        for area in areas:
            add(area.name, aliases=area.aliases, area=True)
        for key in light_group_keys:
            add(key.replace("_", " "))
        for group in room_groups:
            member_keys = []
            for member in group.get("members") or []:
                member_name = member.get("display_name") or member.get("room_name") or ""
                add(member_name)
                member_keys.append(room_key(member.get("room_name") or member_name))
            add(
                group.get("display_name") or group.get("name") or "",
                aliases=[group.get("name") or "", *(group.get("aliases") or [])],
                members=member_keys,
            )
        for room in satellite_rooms:
            add(str(room).replace("_", " "))
        for friendly in group_friendly_names:
            add(_strip_device_nouns(friendly))

        entries: Dict[str, _Entry] = {}
        unmatchable = collisions = 0
        for key, slot in pending.items():
            collided = slot["areas"] > 1
            full = slot["tokens"]
            usable = _matchable(full) and not collided
            if collided:
                collisions += 1
            elif not _matchable(full):
                unmatchable += 1
            entries[key] = _Entry(
                key=key,
                label=slot["label"],
                tokens=full,
                phrases=tuple(slot["phrases"]),
                members=tuple(slot["members"]),
                matchable=usable,
            )
        return cls(entries, unmatchable=unmatchable, collisions=collisions)

    # -- lookups ----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def keys(self) -> Tuple[str, ...]:
        return tuple(sorted(self._entries))

    def resolves(self, room: Optional[str]) -> Optional[RoomRef]:
        """The room a speaker's room string names: a known key or alias. A
        common word never counts, and neither does an unknown string such as
        a service's own name."""
        key = room_key(room or "")
        if not key:
            return None
        entry = self._entries.get(key)
        if entry is not None:
            return entry.ref()
        phrase = tokens(room or "")
        owners = self._exact.get(phrase, [])
        return self._entries[owners[0]].ref() if len(owners) == 1 else None

    def ref(self, key: str) -> Optional[RoomRef]:
        entry = self._entries.get(key)
        return entry.ref() if entry else None

    def members_of(self, key: str) -> Tuple[str, ...]:
        entry = self._entries.get(key)
        return entry.members if entry else ()

    def _lookup(self, gram: Tuple[str, ...]) -> Optional[Tuple[str, Tuple[RoomRef, ...]]]:
        exact = self._exact.get(gram)
        if exact:
            kind = "exact" if len(exact) == 1 else "partial"
            return kind, tuple(self._entries[k].ref() for k in exact)
        partial = self._partial.get(gram)
        if partial:
            return "partial", tuple(self._entries[k].ref() for k in partial)
        return None

    def _lookup_common(self, gram: Tuple[str, ...]) -> Optional[RoomRef]:
        return self._common.get(gram)


_EMPTY = RoomDirectory.empty()


# ---------------------------------------------------------------------------
# Mentions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoomMention:
    """A room phrase in an utterance. ``kind``: ``exact`` (one room's whole
    name or alias), ``partial`` (part of several names, or part of one),
    ``plural`` ("all the bathrooms"), or ``none``."""
    phrase: str
    kind: str
    candidates: Tuple[RoomRef, ...] = ()
    in_exclusion: bool = False
    start: int = -1
    end: int = -1


NO_MENTION = RoomMention("", "none")


def _singular(token: str) -> Tuple[str, ...]:
    forms = []
    if token.endswith("es") and len(token) > 3:
        forms.append(token[:-2])
    if token.endswith("s") and len(token) > 2:
        forms.append(token[:-1])
    return tuple(forms)


def _exclusion_start(toks: Sequence[str]) -> int:
    for i in range(len(toks)):
        for keyword in EXCLUSION_PHRASES:
            if tuple(toks[i:i + len(keyword)]) == keyword:
                return i
    return len(toks)


def has_exclusion(query: Optional[str]) -> bool:
    """True when the utterance contains an exclusion phrase as whole tokens
    ("button", "butler" and "about" do not contain "but")."""
    toks = tokens(query or "")
    return _exclusion_start(toks) < len(toks)


def has_exclusion_deferral_word(query: Optional[str]) -> bool:
    """True when the utterance contains a word that may carry an exclusion
    ("without", "unless", "minus", "skip", "leave", "keep")."""
    return any(t in EXCLUSION_DEFERRAL_WORDS for t in tokens(query or ""))


def find_room_mentions(query: Optional[str], directory: Optional[RoomDirectory]) -> List[RoomMention]:
    """Every room phrase in the utterance, left to right, non-overlapping.
    The longest phrase wins where two overlap ("master bathroom" beats
    "bathroom"); at equal length an exact name beats a partial one."""
    directory = directory if directory is not None else _EMPTY
    toks = tokens(query or "")
    if not toks:
        return []
    found: List[tuple] = []
    for i in range(len(toks)):
        for j in range(min(len(toks), i + directory.max_phrase_len), i, -1):
            gram = toks[i:j]
            hit = directory._lookup(gram)
            plural = False
            if hit is None:
                for single in _singular(gram[-1]):
                    hit = directory._lookup(gram[:-1] + (single,))
                    if hit is not None:
                        plural = True
                        break
            if hit is not None:
                kind, candidates = hit
                found.append((i, j, kind, candidates, plural))
    taken = [False] * len(toks)
    chosen: List[tuple] = []
    for item in sorted(found, key=lambda f: (-(f[1] - f[0]), 0 if f[2] == "exact" else 1, f[0])):
        i, j = item[0], item[1]
        if any(taken[i:j]):
            continue
        for k in range(i, j):
            taken[k] = True
        chosen.append(item)
    for i in range(len(toks)):
        for j in range(min(len(toks), i + directory.max_phrase_len), i, -1):
            if any(taken[i:j]):
                continue
            gram = toks[i:j]
            ref = directory._lookup_common(gram)
            if ref is None:
                for single in _singular(gram[-1]):
                    ref = directory._lookup_common(gram[:-1] + (single,))
                    if ref is not None:
                        break
            if ref is not None:
                for k in range(i, j):
                    taken[k] = True
                chosen.append((i, j, "exact", (ref,), False))
    chosen.sort(key=lambda c: c[0])
    cutoff = _exclusion_start(toks)
    mentions = []
    for i, j, kind, candidates, plural in chosen:
        before = toks[max(0, i - 3):i]
        after = toks[j:j + 1]
        universal = bool(set(before) & _UNIVERSAL_QUANTIFIERS) and not (after and after[0] in _LIGHT_NOUNS)
        if (plural or universal) and len(candidates) > 1:
            kind = "plural"
        mentions.append(RoomMention(
            phrase=" ".join(toks[i:j]),
            kind=kind,
            candidates=candidates,
            in_exclusion=i >= cutoff,
            start=i,
            end=j,
        ))
    return mentions


def find_room_mention(query: Optional[str], directory: Optional[RoomDirectory]) -> RoomMention:
    """The first room phrase outside an exclusion clause, or ``NO_MENTION``."""
    for mention in find_room_mentions(query, directory):
        if not mention.in_exclusion:
            return mention
    return NO_MENTION


def ground_llm_room(
    llm_room: Optional[str], query: Optional[str], directory: Optional[RoomDirectory],
) -> Optional[str]:
    """A room a language model named, kept only when the utterance itself
    names it (whole tokens: "den" is not in "garden"). ``whole_house`` counts
    only for whole-house wording. Anything else is None."""
    if not llm_room:
        return None
    if llm_room == "whole_house":
        return "whole_house" if is_explicit_whole_house(query) else None
    claimed = tokens(llm_room)
    spoken = tokens(query or "")
    if not claimed:
        return None
    width = len(claimed)
    if any(spoken[i:i + width] == claimed for i in range(len(spoken) - width + 1)):
        named = find_room_mention(llm_room, directory)
        if named.kind != "none" and len(named.candidates) == 1:
            return named.candidates[0].key
        return room_key(llm_room)
    keys = {c.key for m in find_room_mentions(query, directory) if not m.in_exclusion for c in m.candidates}
    claimed_key = room_key(llm_room)
    return claimed_key if claimed_key in keys else None


# Words that can sit before "lights" without naming a room: colours, levels,
# fixture kinds, quantifiers and pointers. A phrase made only of these (or of
# _NON_ROOM_TOKENS) is never a room.
_NOT_A_ROOM_WORD = frozenset({
    "red", "orange", "yellow", "green", "blue", "purple", "pink", "violet", "white", "teal", "cyan",
    "warm", "cool", "cold", "hot", "soft", "bright", "brighter", "dim", "dimmer", "low", "high",
    "full", "half", "max", "min", "maximum", "minimum", "medium", "natural", "daylight",
    "ceiling", "overhead", "table", "floor", "desk", "bedside", "night", "accent", "led", "smart",
    "ambient", "pendant", "wall", "strip", "string", "fairy", "reading", "mood", "lava", "main",
    "other", "same", "these", "those", "here", "there", "that", "this", "home", "outside", "inside",
    "its", "their", "his", "her", "last", "next", "previous", "first", "second", "third",
})
_DEVICE_NOUNS = frozenset({
    "light", "lights", "lamp", "lamps", "fan", "fans", "switch", "switches", "plug", "plugs",
    "tv", "television", "speaker", "speakers",
})
_DETERMINERS = frozenset({"the", "my", "our", "a"})
_PLACE_WORDS = frozenset({"in"})
_PHRASE_END = frozenset({
    "and", "or", "to", "please", "except", "but", "when", "by", "so", "if", "then", "on", "off",
    "with", "until", "for", "at", "from", "after", "before", "while", "now",
    "apart", "aside", "besides", "excluding", "including",
})
_MAX_ROOM_WORDS = 3


def _room_like(words: Sequence[str]) -> bool:
    return bool(words) and not any(
        w in _NON_ROOM_TOKENS or w in _NOT_A_ROOM_WORD or w in _DEVICE_NOUNS or w.isdigit() for w in words
    )


def unknown_room_phrase(query: Optional[str], directory: Optional[RoomDirectory]) -> str:
    """A phrase that is shaped like a room ("the sunroom lights", "lights in
    the sunroom", "turn on sunroom lights") but names none the directory
    knows; "" when there is none. Colours, levels, fixture kinds and pointers
    ("the red lights", "dim lights", "the ceiling lights", "lights in here")
    are not rooms. Used before any default: a room the speaker named and
    Athena doesn't have must not become the speaker's room."""
    toks = tokens(query or "")
    if not toks:
        return ""
    known = set()
    for mention in find_room_mentions(query, directory):
        known.update(range(mention.start, mention.end))
    n = len(toks)
    for i, tok in enumerate(toks):
        # "<noun> in|at|for the X": the words after the place word.
        if tok in _PLACE_WORDS and any(t in _DEVICE_NOUNS for t in toks[:i]):
            j = i + 1
            if j < n and toks[j] in _DETERMINERS:
                j += 1
            end = j
            while end < n and end - j < _MAX_ROOM_WORDS and toks[end] not in _PHRASE_END and toks[end] not in _PLACE_WORDS:
                end += 1
            words = toks[j:end]
            if _room_like(words) and not known.intersection(range(j, end)):
                return " ".join(words)
        # "the|my X <noun>" and "on|off X <noun>": the words before the noun.
        if tok in _DEVICE_NOUNS and i > 0:
            start = i
            while start > 0 and i - start < _MAX_ROOM_WORDS and toks[start - 1] not in _DETERMINERS \
                    and toks[start - 1] not in _PHRASE_END and toks[start - 1] not in _PLACE_WORDS \
                    and toks[start - 1] not in _DEVICE_NOUNS:
                start -= 1
            words = toks[start:i]
            introduced = start > 0 and (toks[start - 1] in _DETERMINERS or toks[start - 1] in ("on", "off"))
            if introduced and _room_like(words) and not known.intersection(range(start, i)):
                return " ".join(words)
    return ""


_JOINERS = frozenset({"and", "the", "my", "also", "plus", "light", "lights", "lamp", "lamps"})


def room_values(mention: RoomMention) -> Tuple[str, ...]:
    """The room keys a mention stands for: every candidate of a plural
    phrase, the one room of an exact or single-candidate mention, and, for
    an ambiguous partial phrase, the phrase itself (the caller asks)."""
    if mention.kind == "none":
        return ()
    if mention.kind == "plural":
        return tuple(c.key for c in mention.candidates)
    if len(mention.candidates) == 1:
        return (mention.candidates[0].key,)
    return (room_key(mention.phrase),)


def joined_room_mentions(query: Optional[str], directory: Optional[RoomDirectory]) -> List[RoomMention]:
    """Rooms joined by "and" ("the kitchen and the living room lights"), in
    order; empty unless at least two are chained by an "and" with nothing but
    articles and light words between them."""
    mentions = [m for m in find_room_mentions(query, directory) if not m.in_exclusion]
    if len(mentions) < 2:
        return []
    toks = tokens(query or "")
    chain = [mentions[0]]
    saw_and = False
    for previous, following in zip(mentions, mentions[1:]):
        between = toks[previous.end:following.start]
        if not all(t in _JOINERS for t in between):
            break
        saw_and = saw_and or "and" in between
        chain.append(following)
    return chain if len(chain) >= 2 and saw_and else []


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Room:
    key: str
    label: str


@dataclass(frozen=True)
class Rooms:
    keys: Tuple[str, ...]
    labels: Tuple[str, ...]


@dataclass(frozen=True)
class WholeHouse:
    excluded: Tuple[RoomRef, ...] = ()


@dataclass(frozen=True)
class AskWhich:
    candidates: Tuple[RoomRef, ...]
    phrase: str


@dataclass(frozen=True)
class AskRoom:
    pass


RoomDecision = Union[Room, Rooms, WholeHouse, AskWhich, AskRoom]


def _excluded_rooms(query: str, directory: Optional[RoomDirectory]) -> Tuple[RoomRef, ...]:
    """Rooms named inside an exclusion clause. An ambiguous name excludes
    every room it could mean: that writes less, never more."""
    seen: Dict[str, RoomRef] = {}
    for mention in find_room_mentions(query, directory):
        if mention.in_exclusion:
            for candidate in mention.candidates:
                seen.setdefault(candidate.key, candidate)
    return tuple(seen.values())


def explicit_whole_house(query: Optional[str], directory: Optional[RoomDirectory] = None) -> Optional[WholeHouse]:
    """``WholeHouse`` when the wording asks for it: a room-quantifying cue
    ("the whole house") always; a light-quantifying cue ("all the lights")
    unless a room is named outside an exclusion clause. None otherwise."""
    text = query or ""
    if WHOLE_HOUSE_ROOM_RE.search(text):
        return WholeHouse(_excluded_rooms(text, directory))
    if WHOLE_HOUSE_LIGHT_RE.search(text) and find_room_mention(text, directory).kind == "none":
        return WholeHouse(_excluded_rooms(text, directory))
    return None


def decide_room(
    mention: RoomMention,
    *,
    query: Optional[str],
    speaker_room: Optional[str],
    prev_decision: Optional[RoomDecision] = None,
    referent_followup: bool = False,
    directory: Optional[RoomDirectory] = None,
    allow_whole_house: bool = True,
) -> RoomDecision:
    """The room a command acts on.

    1. Whole-house wording (``explicit_whole_house``).
    2. A named room: exact, or the only candidate, or the speaker's room if
       it is among several, else ask which. A plural or universal phrase
       ("all the bathrooms") is all its candidates.
    3. A referent follow-up ("turn them off") repeats the previous decision.
    4. The speaker's room, if the directory knows it.
    5. Ask which room.
    """
    whole = explicit_whole_house(query, directory) if allow_whole_house else None
    if whole is not None:
        return whole
    speaker = (directory if directory is not None else _EMPTY).resolves(speaker_room)
    if mention.kind != "none" and not mention.in_exclusion:
        return _decide_mention(mention, speaker)
    if referent_followup and prev_decision is not None:
        return prev_decision
    if speaker is not None:
        return Room(speaker.key, speaker.label)
    return AskRoom()


def _decide_mention(mention: RoomMention, speaker: Optional[RoomRef]) -> RoomDecision:
    candidates = mention.candidates
    if mention.kind == "plural" and len(candidates) > 1:
        return Rooms(tuple(c.key for c in candidates), tuple(c.label for c in candidates))
    if len(candidates) == 1:
        return Room(candidates[0].key, candidates[0].label)
    if speaker is not None and speaker in candidates:
        return Room(speaker.key, speaker.label)
    return AskWhich(candidates, mention.phrase)


# ---------------------------------------------------------------------------
# Asking, and reading the answer
# ---------------------------------------------------------------------------

MAX_NAMED_CANDIDATES = 4


def _distinguishing(candidate: RoomRef, phrase: Tuple[str, ...]) -> str:
    own = [t for t in tokens(candidate.label) if t not in phrase and t not in _NON_ROOM_TOKENS]
    if not own or all(t.isdigit() for t in own):
        return candidate.label
    return " ".join(own)


def _join_or(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " or " + items[-1]


def ask_text(decision: RoomDecision) -> str:
    """The question for an ``AskWhich`` or ``AskRoom``. Names no rooms the
    caller wasn't already told about: the candidates were filtered first."""
    if isinstance(decision, AskWhich):
        phrase = tokens(decision.phrase)
        noun = decision.phrase or "room"
        if len(decision.candidates) > MAX_NAMED_CANDIDATES:
            return f"Which {noun} do you mean?"
        names = [_distinguishing(c, phrase) for c in decision.candidates]
        return f"Which {noun}: {_join_or(names)}?"
    return "Which room?"


_REPLY_FILLER = frozenset({"the", "one", "please", "in", "room", "light", "lights", "yes", "um", "uh", "a"})
_COMMAND_VERBS = frozenset({
    "turn", "switch", "set", "dim", "make", "play", "open", "close", "lock", "unlock", "brighten",
})


@dataclass(frozen=True)
class MatchResult:
    kind: str
    room: Optional[RoomRef] = None
    matches: Tuple[RoomRef, ...] = ()


def match_reply(reply: Optional[str], candidates: Sequence[RoomRef]) -> MatchResult:
    """Read the answer to "which one?". The reply's words, less filler, must
    all belong to one candidate's name; a command verb or any other word
    makes it no answer at all. Two candidates fitting is ``several``."""
    words = [t for t in tokens(reply or "") if t not in _REPLY_FILLER]
    if not words or any(t in _COMMAND_VERBS for t in words):
        return MatchResult("none")
    fits = tuple(c for c in candidates if set(words) <= set(tokens(c.label)))
    if len(fits) == 1:
        return MatchResult("one", fits[0], fits)
    if len(fits) > 1:
        return MatchResult("several", None, fits)
    return MatchResult("none")

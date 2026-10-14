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
# Words that often carry an exclusion without being one of the phrases above
# ("exclude the galley", "with the galley excepted", "save for", ASR "accept");
# a fast path that sees one and parsed no excluded room leaves it to the model.
EXCLUSION_DEFERRAL_WORDS = frozenset({
    "without", "unless", "minus", "skip", "leave", "keep",
    "exclude", "excluded", "excepted", "accept", "save", "spare", "ignoring", "outside",
})
_UNIVERSAL_QUANTIFIERS = frozenset({"all", "both", "every", "each"})
_LIGHT_NOUNS = frozenset({"light", "lights", "lamp", "lamps"})
_STRIP_DEVICE_NOUNS = frozenset({"light", "lights", "lamp", "lamps", "group", "all"})
_NUMBER_IN_PARENS = re.compile(r"\((\d+)\)")


# What only the server may write onto an intent or a sequence step target: the
# room decision, the door decision, and the "could not be processed" marker.
# Model output never carries them (``strip_server_keys`` drops them right after
# every parse), so a model cannot decide where, or which door, a command acts.
SERVER_DECISION_KEYS: Tuple[str, ...] = (
    "decided_room", "decided_rooms", "decided_excluded", "room_label", "referent_followup",
    "decided_access", "decided_access_fp", "decided_access_aud", "decided_access_src", "decided_access_at",
    "unprocessable", "unprocessable_reason",
    "decided_entity", "write_cue",
)

# A smart-home command longer than this many UTF-8 bytes is refused before any
# model call: no device command is that long, and the model prompt is sized for
# one that isn't. Bytes, not characters: the context estimate is per byte, so
# 4-byte text must not plan a larger context than ASCII.
SMART_HOME_MAX_QUERY_BYTES = 1000
QUERY_TOO_LONG_TEXT = "That's too long for a device command."
UNPROCESSABLE_TEXT = "Sorry, I couldn't work out that command."
# Why a model call gave nothing to act on: the query was over the cap, the
# context budget could not hold the whole prompt, the answer hit its token
# limit, the JSON was not an object, or (a backend that can't promise the whole
# prompt was read) it did not parse, or a command came back as a status read
# or without an explicit on/off.
UNPROCESSABLE_REASONS = ("too_long", "budget", "length", "not_object", "unparseable", "unverified", "imperative_unresolved")


def utf8_len(text: object) -> int:
    return len(text.encode("utf-8")) if isinstance(text, str) else 0


def cut_bytes(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` UTF-8 bytes, on a character boundary."""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def unprocessable_intent(reason: str) -> dict:
    """The result of a model call that can't be acted on: no device, no action,
    and a closed ``reason``. Every consumer answers it with
    ``unprocessable_text`` and writes nothing."""
    return {"unprocessable": True, "unprocessable_reason": reason}


def unprocessable_text(intent: object) -> Optional[str]:
    """The fixed spoken answer for an unprocessable result, else None."""
    if not isinstance(intent, dict) or not intent.get("unprocessable"):
        return None
    return QUERY_TOO_LONG_TEXT if intent.get("unprocessable_reason") == "too_long" else UNPROCESSABLE_TEXT


_REFUSALS: set = set()
NO_WRITE_CUE_MARK: str = ""  # set by the controller to its no-cue phrase


def register_refusals(*texts: str) -> None:
    """The controller registers each fixed answer that means "nothing was
    changed and this was not a command to remember"."""
    _REFUSALS.update(t for t in texts if isinstance(t, str))


def set_no_write_cue_text(text: str) -> None:
    global NO_WRITE_CUE_MARK
    NO_WRITE_CUE_MARK = text


def is_refusal_text(text: object) -> bool:
    """Whether an answer is one of the registered fixed refusals. Matched
    exactly, never by a word in the text."""
    return isinstance(text, str) and text in _REFUSALS


def strip_server_keys(obj: object) -> Optional[dict]:
    """A model's parsed JSON with every ``SERVER_DECISION_KEYS`` entry removed,
    from the object itself and from each step target of a sequence. Anything
    that is not a JSON object (a list, a scalar) gives None: an unparseable
    result, never a crash. Non-object steps are dropped and a non-object
    target becomes empty."""
    if not isinstance(obj, dict):
        return None
    for key in SERVER_DECISION_KEYS:
        obj.pop(key, None)
    steps = obj.get("steps")
    if "steps" in obj:
        if not isinstance(steps, list):
            obj["steps"] = []
        else:
            kept = []
            for step in steps:
                if not isinstance(step, dict):
                    continue
                for key in SERVER_DECISION_KEYS:
                    step.pop(key, None)
                target = step.get("target")
                if isinstance(target, dict):
                    for key in SERVER_DECISION_KEYS:
                        target.pop(key, None)
                else:
                    step["target"] = {}
                kept.append(step)
            obj["steps"] = kept
    return obj


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

    def name_keys(self, key: str) -> Tuple[str, ...]:
        """The ``room_key`` form of every name a room answers to (its own
        and each alias), for matching a room to an id slug."""
        entry = self._entries.get(key)
        if entry is None:
            return ()
        return tuple(dict.fromkeys(["_".join(phrase) for phrase in entry.phrases] + [entry.key]))

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


# Whether a phrase starts an exclusion is decided here, from the words alone.
# It does when it follows a scope (a quantifier, a whole-house word, a light
# noun) or a room, or when it leads the utterance, names a room, and a
# whole-house cue ("every light", "the whole house") appears anywhere:
# "except the galley, turn off all the lights". Otherwise it is an opener
# ("aside from that, ..." or "besides the den, turn on the kitchen fan"), and
# the room it introduces is neither a target nor an exclusion.
_SCOPE_WORDS = _UNIVERSAL_QUANTIFIERS | _LIGHT_NOUNS | frozenset({
    "everything", "everywhere", "house", "whole", "entire", "rooms", "rest", "others",
})
_OBJECT_FILLER = frozenset({"the", "a", "an", "my", "our", "your", "for"})
_STATE_WORDS = frozenset({"on", "off", "lit", "dark"})
_LIST_JOINERS = frozenset({"and", "or"})
_PLACE_PREPOSITIONS = frozenset({"in", "inside", "at"})
_TRAILING_DEFERRAL_WORDS = frozenset({"excluded", "excepted"})
_SKIPPED_AFTER_DEFERRAL = (
    _OBJECT_FILLER | _LIGHT_NOUNS | _PLACE_PREPOSITIONS | frozenset({"ones", "one", "those", "them", "it"})
)
_PLAIN_WORDS = _OBJECT_FILLER | _SCOPE_WORDS | _NON_ROOM_TOKENS | frozenset({"it", "them", "that"})


def _phrase_at(toks: Sequence[str], i: int) -> int:
    """Length of the exclusion phrase starting at ``i``, or 0."""
    for keyword in EXCLUSION_PHRASES:
        if tuple(toks[i:i + len(keyword)]) == keyword:
            return len(keyword)
    return 0


def _has_whole_house_cue(toks: Sequence[str]) -> bool:
    text = " ".join(toks)
    return bool(WHOLE_HOUSE_ROOM_RE.search(text) or WHOLE_HOUSE_LIGHT_RE.search(text))


def _object_chain(toks: Sequence[str], spans: Sequence[Tuple[int, int]], end: int) -> List[Tuple[int, int]]:
    """The rooms a phrase ending at ``end`` introduces: each room separated
    from the previous one (or the phrase) only by filler or "and"/"or"
    ("besides the den and the study")."""
    chain: List[Tuple[int, int]] = []
    previous = end
    for start, stop in sorted(spans):
        if start >= previous and all(t in _OBJECT_FILLER or t in _LIST_JOINERS for t in toks[previous:start]):
            chain.append((start, stop))
            previous = stop
    return chain


def _names_scope_room(toks: Sequence[str], spans: Sequence[Tuple[int, int]],
                      objects: Sequence[Tuple[int, int]]) -> bool:
    """True when a room outside ``objects`` follows a place word that comes
    after a scope or light noun ("all the lights in the kitchen"): that room
    is the target, so a leading phrase before it is an opener."""
    for start, stop in spans:
        if (start, stop) in objects:
            continue
        q = start - 1
        while q >= 0 and toks[q] in _OBJECT_FILLER:
            q -= 1
        if q >= 0 and toks[q] in _PLACE_PREPOSITIONS and any(t in _SCOPE_WORDS for t in toks[:q]):
            return True
    return False


def _scan_exclusions(
    toks: Sequence[str], spans: Sequence[Tuple[int, int]], directory: Optional[RoomDirectory] = None,
) -> Tuple[int, List[Tuple[int, int]]]:
    """(index where the exclusion clause starts, or ``len(toks)``; the token
    ranges of rooms an opener introduced)."""
    introduced: List[Tuple[int, int]] = []
    cue = _has_whole_house_cue(toks)
    i = 0
    while i < len(toks):
        length = _phrase_at(toks, i)
        if not length:
            i += 1
            continue
        end = i + length
        if (
            tuple(toks[i:end]) == ("but",) and toks[end:end + 1] and toks[end] in _NON_LIGHT_STEP_VERBS
            and not _starts_a_room(" ".join(toks[end:]), directory)
        ):
            i = end  # "all the lights but lock the front door": a second command
            continue
        objects = _object_chain(toks, spans, end)
        if any(t in _SCOPE_WORDS for t in toks[:i]) or any(e <= i for _, e in spans):
            return i, introduced
        if cue and objects and not _names_scope_room(toks, spans, objects):
            return i, introduced
        introduced.extend(objects)
        i = end
    return len(toks), introduced


def has_exclusion(query: Optional[str], directory: Optional[RoomDirectory] = None) -> bool:
    """True when the utterance contains an exclusion (see above): a phrase as
    whole tokens ("button", "butler" and "about" do not contain "but").
    Pass the directory so a room counts."""
    toks = tokens(query or "")
    spans = [(m.start, m.end) for m in _chosen_mentions(toks, directory)]
    return _scan_exclusions(toks, spans, directory)[0] < len(toks)


def has_exclusion_wording(query: Optional[str], directory: Optional[RoomDirectory] = None) -> bool:
    """True when the utterance may carry an exclusion in any wording: an
    exclusion phrase, a deferral word that is followed by a room or by what
    reads as one ("leave the porch on"), or "not <room>". The server cannot
    always tell which room is left out, so callers treat this as 'do not act
    on the whole house unasked'."""
    if has_exclusion(query, directory) or has_exclusion_deferral_word(query, directory):
        return True
    toks = tokens(query or "")
    return any(m.start >= 1 and toks[m.start - 1] == "not" for m in find_room_mentions(query, directory))


def has_exclusion_deferral_word(query: Optional[str], directory: Optional[RoomDirectory] = None) -> bool:
    """True when a deferral word ("without", "leave", "keep", "spare",
    "exclude"...) is followed within three tokens by a room, or by a noun
    phrase that ends in on, off, lit or dark and holds a word that is not a
    scope or filler ("leave the porch on"). "Before we leave", "keep all the
    lights off" and "save power" are not exclusions. A word that is itself a
    room's name ("outside") is a room."""
    toks = tokens(query or "")
    spans = [(m.start, m.end) for m in _chosen_mentions(toks, directory)]
    for i, tok in enumerate(toks):
        if tok not in EXCLUSION_DEFERRAL_WORDS or any(start <= i < stop for start, stop in spans):
            continue
        if any(i < start <= i + 3 for start, _ in spans):
            return True
        if tok in _TRAILING_DEFERRAL_WORDS and any(i - 3 <= stop <= i for _, stop in spans):
            return True  # "with the galley excluded"
        # "leave the lights in the hall on": skip the filler, light nouns and
        # place words, then look three tokens on.
        j = i + 1
        while j < len(toks) and toks[j] in _SKIPPED_AFTER_DEFERRAL:
            j += 1
        if any(j <= start < j + 3 for start, _ in spans):
            return True
        window = toks[j:j + 3]
        for k, word in enumerate(window):
            if word in _STATE_WORDS:
                if any(w not in _PLAIN_WORDS for w in window[:k]):
                    return True
                break
    return False


def _chosen_mentions(toks: Sequence[str], directory: Optional[RoomDirectory]) -> List[RoomMention]:
    """The room phrases in ``toks`` before exclusion is decided."""
    return [
        RoomMention(phrase=" ".join(toks[i:j]), kind=kind, candidates=candidates, start=i, end=j)
        for i, j, kind, candidates, _ in _choose_spans(toks, directory)
    ]


def find_room_mentions(query: Optional[str], directory: Optional[RoomDirectory]) -> List[RoomMention]:
    """Every room phrase in the utterance, left to right, non-overlapping.
    The longest phrase wins where two overlap ("master bathroom" beats
    "bathroom"); at equal length an exact name beats a partial one. A room an
    opener introduces ("besides the den, ...") is left out: it is neither a
    target nor an exclusion."""
    toks = tokens(query or "")
    if not toks:
        return []
    chosen = _choose_spans(toks, directory)
    cutoff, introduced = _scan_exclusions(toks, [(c[0], c[1]) for c in chosen], directory)
    chosen = [c for c in chosen if (c[0], c[1]) not in introduced]
    return _mentions_from(toks, chosen, cutoff)


def _choose_spans(toks: Sequence[str], directory: Optional[RoomDirectory]) -> List[tuple]:
    directory = directory if directory is not None else _EMPTY
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
    return chosen


def _mentions_from(toks: Sequence[str], chosen: Sequence[tuple], cutoff: int) -> List[RoomMention]:
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


def names_entity(entity_id: str, friendly_name: Optional[str], text: Optional[str]) -> bool:
    """Whether the words call an entity by its name: every distinguishing word
    of its friendly name (else its object id) is in ``text``, and there is at
    least one. Device nouns, quantifiers, filler and bare numbers ("light",
    "all", "room", "2") never distinguish: "the office light" may be any of
    "Office Light 1..3", and the room it names is what the words settle."""
    name = friendly_name if isinstance(friendly_name, str) and friendly_name.strip() else (
        entity_id.split(".", 1)[-1] if isinstance(entity_id, str) else ""
    )
    wanted = {t for t in tokens(name) if t not in _NON_ROOM_TOKENS and t not in _DEVICE_NOUNS and not t.isdigit()}
    return bool(wanted) and wanted <= set(tokens(text or ""))


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


# A room given as a place relative to the speaker names no room of its own;
# the speaker's room answers it (decide_room).
_SPEAKER_RELATIVE_PHRASES = frozenset({
    ("my", "room"), ("this", "room"), ("the", "room"), ("here",), ("in", "here"), ("my", "current", "room"),
})


def is_speaker_relative(phrase: Optional[str]) -> bool:
    """Whether a model-supplied room is "my room" or "here" rather than a name."""
    return tokens(phrase or "") in _SPEAKER_RELATIVE_PHRASES


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


# "leave the galley on but turn off the rest": the room named first is kept,
# every other room is turned off.
LEAVE_ON_RE = re.compile(
    r"leave\s+(?:the\s+)?(\w+(?:\s+\w+)?)\s+(?:light(?:s)?|on)\s+(?:on\s+)?(?:but|and|then)\s+turn\s+off",
    re.IGNORECASE,
)


def leave_on_exclusion(query: Optional[str], directory: Optional[RoomDirectory] = None) -> Tuple[RoomRef, ...]:
    """The rooms kept by the "leave X on but turn off the rest" form, or ().
    The form counts only when no other exclusion wording remains once its
    clause is removed. An ambiguous name keeps every room it could mean."""
    text = query or ""
    match = LEAVE_ON_RE.search(text)
    if match is None or has_exclusion_wording(LEAVE_ON_RE.sub(" ", text), directory):
        return ()
    kept = find_room_mention(match.group(1), directory)
    return tuple(kept.candidates) if kept.kind != "none" else ()


def explicit_whole_house(query: Optional[str], directory: Optional[RoomDirectory] = None) -> Optional[WholeHouse]:
    """``WholeHouse`` when the wording asks for it: a room-quantifying cue
    ("the whole house") always; a light-quantifying cue ("all the lights")
    unless a room is named outside an exclusion clause; the leave-on form.
    None otherwise."""
    text = query or ""
    kept = leave_on_exclusion(text, directory)
    if kept:
        return WholeHouse(kept)
    if WHOLE_HOUSE_ROOM_RE.search(text):
        return WholeHouse(_excluded_rooms(text, directory))
    if WHOLE_HOUSE_LIGHT_RE.search(text):
        # A room named in another command ("... but play music in the
        # kitchen") is not this cue's room: only the cue's own clause counts.
        # A fragment with no step of its own ("In the kitchen.") belongs to
        # the cue's clause.
        clauses = step_clauses(text, directory)
        cues = [c for c in clauses if WHOLE_HOUSE_LIGHT_RE.search(c)]
        fragments = [
            c for c in clauses
            if not WHOLE_HOUSE_LIGHT_RE.search(c) and not any(t in _STEP_VERBS for t in tokens(c))
        ]
        cue_clauses = [" ".join([c, *fragments]) for c in cues] or [text]
        if all(find_room_mention(c, directory).kind == "none" for c in cue_clauses):
            return WholeHouse(_excluded_rooms(text, directory))
    return None


def grounded_exclusion(
    query: Optional[str], model_excluded: object, directory: Optional[RoomDirectory],
) -> Optional[Tuple[RoomRef, ...]]:
    """The rooms a model's ``excluded_rooms`` leave out, or None when the list
    is not safe to use: every name must be a room the directory knows, and
    the list must cover every room the utterance names (an ambiguous name by
    at least one of its rooms, and then by all of them). Anything less would
    write a room the speaker meant to spare."""
    directory = directory if directory is not None else _EMPTY
    if not isinstance(model_excluded, list) or not model_excluded:
        return None
    refs: Dict[str, RoomRef] = {}
    for name in model_excluded:
        if not isinstance(name, str) or not name.strip():
            return None
        ref = directory.ref(room_key(name))
        if ref is None:
            return None
        refs[ref.key] = ref
    for mention in find_room_mentions(query, directory):
        if not any(c.key in refs for c in mention.candidates):
            return None
        for candidate in mention.candidates:
            refs.setdefault(candidate.key, candidate)
    return tuple(refs.values())


def whole_house_decision(
    query: Optional[str], directory: Optional[RoomDirectory] = None,
) -> Optional[Union[WholeHouse, AskRoom]]:
    """What the wording says about the whole house: ``WholeHouse`` (with the
    rooms it leaves out), ``AskRoom`` when it carries an exclusion this parser
    does not read, or None when it does not ask for the whole house."""
    if (
        is_explicit_whole_house(query)
        and not has_exclusion(query, directory) and has_exclusion_wording(query, directory)
    ):
        # "all the lights without the galley", "...spare the galley": the
        # wording is an exclusion this parser does not read, so the room it
        # names is not a target and the house is not the answer.
        return AskRoom()
    if LEAVE_ON_RE.search(query or "") and not leave_on_exclusion(query, directory):
        # The leave-on form with more exclusion wording in it: which rooms
        # stay on is not something the words settle.
        return AskRoom()
    whole = explicit_whole_house(query, directory)
    if whole is None:
        return None
    if not whole.excluded and has_exclusion_wording(query, directory):
        # An exclusion that names no room we know: every light would be
        # written. Ask instead.
        return AskRoom()
    return whole


# The words that end one step of a sequence and start the next. A comma does
# not: it separates the rooms of an exclusion list.
_HARD_STEP_SPLIT = re.compile(r"[;.!?]|\b(?:and then|then|after that|afterwards|later)\b", re.IGNORECASE)
_SOFT_STEP_SPLIT = re.compile(r"\b(and|but)\b\s+(\w+)", re.IGNORECASE)
# "and <verb>" starts a new step. "but <verb>" does too, but only for verbs
# that are not about lights ("but lock the front door"): "all the lights on
# but turn off the kitchen" still reads as an exclusion.
_STEP_VERBS = frozenset({
    "turn", "switch", "set", "dim", "brighten", "make", "open", "close", "lock", "unlock", "play", "start",
    "stop", "pause",
})
_NON_LIGHT_STEP_VERBS = frozenset({"open", "close", "lock", "unlock", "play", "start", "stop", "pause"})


def _starts_a_room(rest: str, directory: Optional[RoomDirectory]) -> bool:
    """Whether the words begin with a room's name ("play room")."""
    return any(m.start == 0 and m.kind != "partial" for m in _chosen_mentions(tokens(rest), directory))


def step_clauses(text: str, directory: Optional[RoomDirectory] = None) -> List[str]:
    """The utterance cut into the clauses of its steps. A verb that is the
    first word of a room's name ("and play room") does not start a step."""
    parts: List[str] = []
    for chunk in _HARD_STEP_SPLIT.split(text or ""):
        start = 0
        for match in _SOFT_STEP_SPLIT.finditer(chunk):
            joiner, verb = match.group(1).lower(), match.group(2).lower()
            if verb not in (_NON_LIGHT_STEP_VERBS if joiner == "but" else _STEP_VERBS):
                continue
            if _starts_a_room(chunk[match.start(2):], directory):
                continue
            parts.append(chunk[start:match.start()])
            start = match.end(1)
        parts.append(chunk[start:])
    return [p for p in parts if p.strip()]


def sequence_whole_house(
    query: Optional[str], directory: Optional[RoomDirectory] = None,
) -> Union[AskRoom, Tuple[WholeHouse, ...]]:
    """The whole-house decisions of a sequence, one per clause that says the
    whole house, in order: each is the single-command decision
    (``whole_house_decision``) read on its own clause, so a room spared in one
    step is not spared in another. ``AskRoom`` when any such clause is not a
    plain whole house (a room named outside an exclusion, or exclusion wording
    this parser does not read), when no clause says it, or when a clause with
    no step of its own carries exclusion wording ("turn off all the lights,
    then keep the hall lit"): a step never writes a room the speaker spared."""
    decisions: List[WholeHouse] = []
    for clause in step_clauses(query or "", directory):
        if is_explicit_whole_house(clause):
            decision = whole_house_decision(clause, directory)
            if not isinstance(decision, WholeHouse):
                return AskRoom()
            decisions.append(decision)
        elif has_exclusion_wording(clause, directory) and not any(t in _STEP_VERBS for t in tokens(clause)):
            return AskRoom()
    return tuple(decisions) if decisions else AskRoom()


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
    whole = whole_house_decision(query, directory) if allow_whole_house else None
    if whole is not None:
        return whole
    speaker = (directory if directory is not None else _EMPTY).resolves(speaker_room)
    if mention.kind != "none" and not mention.in_exclusion:
        return _decide_mention(mention, speaker)
    if _excluded_rooms(query or "", directory):
        # "the lights on except the galley": the speaker's room may be the
        # one excluded, so it is never the default here.
        return AskRoom()
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

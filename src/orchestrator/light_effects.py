"""Light effects named in words: what the words ask for, and what a light can do.

Pure. Nothing here does I/O, logs, reads the environment or the clock, or
touches permissions. The effect vocabulary is never written down here: it is
read from the ``effect_list`` each light reports (``EffectIndex``), and an
effect only ever leaves this module as a member of the list it was matched
against, byte for byte.

``has_effect_shape`` is the cheap structural gate (tokens only, no
vocabulary). ``recognise`` reads an utterance that passed it and says whether
it asks to set or clear an effect. The callers hand in what only they know:
which phrases an existing deterministic path already claims, and whether a
span of words names a room or a light. ``effect_problem`` and
``effect_carriers`` are the structural checks the automation agent applies
before an ``effect`` reaches Home Assistant.

Import direction: ``access_target -> light_effects -> shared``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from shared.ha_registry import tokens

# ---------------------------------------------------------------------------
# Names: normalising, sanitising, charset
# ---------------------------------------------------------------------------

# An effect name sent to Home Assistant, shown in a reply or stored in an
# automation: word characters and a little punctuation. No angle brackets,
# quotes, backticks, braces, hashes, modulo signs or control characters, so a
# name can't carry markup or a template anywhere it travels.
# \A...\Z, not ^...$: "$" also matches before a trailing newline.
EFFECT_CHARSET = re.compile(r"\A[\w .,'&()+:/!\-]+\Z")
MAX_EFFECT_NAME_CHARS = 64
MAX_EFFECT_NAMES = 512
# A list longer than this is read no further.
_RAW_SCAN_CAP = 2048
# The value that means "no effect", in the order a light's own list is tried.
CLEAR_ALIASES: Tuple[str, ...] = ("off", "none", "solid")
# ``LightEntityFeature.EFFECT``.
EFFECT_FEATURE_BIT = 4


def normalize(s: Any) -> str:
    """The words of ``s``, case-folded and joined by single spaces."""
    return " ".join(tokens(s)) if isinstance(s, str) else ""


def compact(s: Any) -> str:
    """The words of ``s`` run together: "Fire 2012" and "fire2012" agree."""
    return "".join(tokens(s)) if isinstance(s, str) else ""


@lru_cache(maxsize=4096)
def _forms(name: str) -> Tuple[str, str]:
    toks = tokens(name)
    return " ".join(toks), "".join(toks)


def _sanitise(items: Tuple[Any, ...]) -> Tuple[str, ...]:
    seen = set()
    kept: List[str] = []
    for item in items:
        if not isinstance(item, str) or not 1 <= len(item) <= MAX_EFFECT_NAME_CHARS:
            continue
        if not EFFECT_CHARSET.fullmatch(item):
            continue
        norm = _forms(item)[0]
        if not norm or norm in seen:
            continue
        seen.add(norm)
        kept.append(item)
        if len(kept) >= MAX_EFFECT_NAMES:
            break
    return tuple(kept)


_sanitise_cached = lru_cache(maxsize=32)(_sanitise)


def sanitize_effect_list(raw: Any) -> Tuple[str, ...]:
    """The usable names in a light's reported ``effect_list``: strings of 1-64
    characters inside ``EFFECT_CHARSET``, one per normalised form (the first
    spelling wins), at most 512. Anything that isn't a list gives ``()``."""
    if not isinstance(raw, (list, tuple)):
        return ()
    items = tuple(raw[:_RAW_SCAN_CAP])
    try:
        return _sanitise_cached(items)
    except TypeError:  # an unhashable member (a nested list): not worth caching
        return _sanitise(items)


def _attributes(state: Any) -> Mapping[str, Any]:
    attrs = state.get("attributes") if isinstance(state, dict) else None
    return attrs if isinstance(attrs, dict) else {}


def _has_effect_bit(attrs: Mapping[str, Any]) -> bool:
    features = attrs.get("supported_features")
    return isinstance(features, int) and not isinstance(features, bool) and bool(features & EFFECT_FEATURE_BIT)


def supports_effects(state: Any) -> bool:
    """Whether a light's state says it takes effects: the EFFECT feature bit
    and a non-empty usable list. A list without the bit counts as none."""
    attrs = _attributes(state)
    return _has_effect_bit(attrs) and bool(sanitize_effect_list(attrs.get("effect_list")))


# ---------------------------------------------------------------------------
# Matching a phrase against one list
# ---------------------------------------------------------------------------

FUZZY_MIN_RATIO = 0.85
FUZZY_MIN_MARGIN = 0.05
FUZZY_MIN_CHARS = 5
FUZZY_MAX_CHARS = 48
# A candidate whose ratio can't reach this can't change the answer: it is
# below the minimum and, as a runner-up, more than the margin under it.
_FUZZY_FLOOR = FUZZY_MIN_RATIO - FUZZY_MIN_MARGIN


@lru_cache(maxsize=64)
def _table(names: Tuple[str, ...]) -> Tuple[Dict[str, str], Dict[str, str], Tuple[Tuple[str, str], ...]]:
    by_norm: Dict[str, str] = {}
    by_compact: Dict[str, str] = {}
    pairs: List[Tuple[str, str]] = []
    for name in names:
        if len(name) > MAX_EFFECT_NAME_CHARS or not EFFECT_CHARSET.fullmatch(name):
            continue
        norm, comp = _forms(name)
        if not norm or norm in by_norm:
            continue
        by_norm[norm] = name
        by_compact.setdefault(comp, name)
        pairs.append((norm, name))
        if len(pairs) >= MAX_EFFECT_NAMES:
            break
    return by_norm, by_compact, tuple(pairs)


def _list_key(effects: Iterable[Any]) -> Tuple[str, ...]:
    if not isinstance(effects, (list, tuple)):
        return ()
    return tuple(e for e in effects[:_RAW_SCAN_CAP] if isinstance(e, str))


def match(phrase: Any, effects: Sequence[str], *, fuzzy: bool = False) -> Optional[str]:
    """The member of ``effects`` the phrase names, byte for byte, or None.

    Exact first (normalised or compact form). With ``fuzzy``, the closest
    member when its ratio is at least 0.85, it leads the runner-up by at least
    0.05, and the phrase is 5-48 characters with no digit. A tie is no match.
    The caller keeps phrases an existing path claims out of the fuzzy step.
    """
    norm, comp = _forms(phrase) if isinstance(phrase, str) and len(phrase) <= 256 else ("", "")
    if not norm:
        return None
    by_norm, by_compact, pairs = _table(_list_key(effects))
    hit = by_norm.get(norm) or by_compact.get(comp)
    if hit is not None:
        return hit
    if not fuzzy or not FUZZY_MIN_CHARS <= len(norm) <= FUZZY_MAX_CHARS or any(c.isdigit() for c in norm):
        return None
    matcher = SequenceMatcher(None)
    matcher.set_seq2(norm)
    best, best_ratio, runner_up = None, 0.0, 0.0
    for cand_norm, name in pairs:
        matcher.set_seq1(cand_norm)
        if matcher.real_quick_ratio() < _FUZZY_FLOOR or matcher.quick_ratio() < _FUZZY_FLOOR:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best, best_ratio, runner_up = name, ratio, best_ratio
        elif ratio > runner_up:
            runner_up = ratio
    if best is not None and best_ratio >= FUZZY_MIN_RATIO and best_ratio - runner_up >= FUZZY_MIN_MARGIN:
        return best
    return None


_CLEAR_COMPACT = frozenset("".join(tokens(alias)) for alias in CLEAR_ALIASES)


def is_clear_alias(name: Any) -> bool:
    """Whether a name means "no effect" in any spelling that is said or reported the
    same way: compared run together, so "off", "O F F" and "Of-f" are one. A set that
    resolves to such a member is a clear, and is never written as a set."""
    return isinstance(name, str) and compact(name) in _CLEAR_COMPACT


def clear_value(effects: Sequence[str]) -> Optional[str]:
    """The member of ``effects`` that means "no effect": the first of
    ``CLEAR_ALIASES`` (in that order) the list holds, spelled as the list has it."""
    by_norm, by_compact, _ = _table(_list_key(effects))
    for alias in CLEAR_ALIASES:
        hit = by_norm.get(alias) or by_compact.get(alias)
        if hit is not None:
            return hit
    return None


def active_effect(state: Any) -> Optional[str]:
    """The effect an ``on`` light reports running, or None: the light isn't on,
    reports none, or it is one of ``CLEAR_ALIASES`` (nothing to clear).

    For comparing by normalised form only; it is never sent anywhere. It need not
    be in the light's sanitised list: a running "Sparkle+" is clearable though
    the list kept only "Sparkle" for that spelling.
    """
    if not isinstance(state, dict) or state.get("state") != "on":
        return None
    current = _attributes(state).get("effect")
    if not isinstance(current, str) or not 1 <= len(current) <= MAX_EFFECT_NAME_CHARS:
        return None
    return current if _forms(current)[0] and not is_clear_alias(current) else None


def suggestions(phrase: Any, effect_lists: Iterable[Sequence[str]], k: int = 3) -> List[str]:
    """Up to ``k`` names from the given lists closest to the phrase, nearest
    first; a name appears once however many lists hold it. The phrase is cut to
    48 characters, and candidates that can't reach the current k-th best are
    never scored exactly."""
    norm = normalize(phrase)[:FUZZY_MAX_CHARS]
    if not norm or k <= 0:
        return []
    matcher = SequenceMatcher(None)
    matcher.set_seq2(norm)
    seen: Dict[str, str] = {}
    for effects in effect_lists:
        for cand_norm, name in _table(_list_key(effects))[2]:
            seen.setdefault(cand_norm, name)
    bounded = []
    for cand_norm, name in seen.items():
        matcher.set_seq1(cand_norm)
        bounded.append((matcher.quick_ratio(), cand_norm, name))
    bounded.sort(key=lambda item: (-item[0], item[1]))
    best: List[Tuple[float, str, str]] = []
    for upper, cand_norm, name in bounded:
        if len(best) >= k and upper < best[-1][0]:
            break
        matcher.set_seq1(cand_norm)
        best.append((matcher.ratio(), cand_norm, name))
        best.sort(key=lambda item: (-item[0], item[1]))
        del best[k:]
    return [name for _, _, name in best]


def spoken_span(effect: Any, spoken_tokens: Sequence[str]) -> Optional[Tuple[int, int]]:
    """Where the effect's own words appear, in order and side by side, in the
    spoken tokens, as the first ``(start, end)`` token range (or run together
    they spell it: "fire 2012" for "Fire2012"); None when they don't."""
    if not isinstance(effect, str) or not spoken_tokens:
        return None
    want = tokens(effect)
    if not want:
        return None
    spoken = tuple(spoken_tokens)
    width = len(want)
    for start in range(len(spoken) - width + 1):
        if spoken[start:start + width] == want:
            return start, start + width
    target = "".join(want)
    for start in range(len(spoken)):
        joined = ""
        for end in range(start, min(len(spoken), start + 8)):
            joined += spoken[end]
            if joined == target:
                return start, end + 1
            if len(joined) >= len(target):
                break
    return None


def spoken_in(effect: Any, spoken_tokens: Sequence[str]) -> bool:
    """Whether the effect's own words appear in the spoken tokens (see ``spoken_span``)."""
    return spoken_span(effect, spoken_tokens) is not None


MAX_SPOKEN_NAME_CHARS = 32


_NOT_SPEECH = re.compile(r"[^\w '\-]|_")


def spoken_name(effect: Any) -> str:
    """An effect name as a reply says it: lower case, only letters, digits,
    spaces, apostrophes and hyphens (anything else, such as the "+" in a name,
    becomes a space), single spaces, at most 32 characters (cut at a word
    where it can be)."""
    if not isinstance(effect, str):
        return ""
    text = " ".join(_NOT_SPEECH.sub(" ", effect.casefold()).split())
    if len(text) <= MAX_SPOKEN_NAME_CHARS:
        return text
    cut = text[:MAX_SPOKEN_NAME_CHARS]
    head, sep, _ = cut.rpartition(" ")
    return (head if sep and head else cut).rstrip()


# ---------------------------------------------------------------------------
# The vocabulary lights report
# ---------------------------------------------------------------------------


class EffectIndex:
    """The effects the lights in a set of states report.

    Build once per cache generation with ``for_states``. A light is in the index
    only when ``supports_effects`` holds for it. Identical lists are
    sanitised and normalised once however many lights share them.
    """

    _memo: Optional[Tuple[Any, int, "EffectIndex"]] = None

    def __init__(self, states: Optional[Mapping[str, Any]]) -> None:
        self._lists: List[Tuple[str, ...]] = []
        self._forms: List[FrozenSet[str]] = []
        self._list_of: Dict[str, int] = {}
        by_raw: Dict[Any, int] = {}
        by_sanitised: Dict[Tuple[str, ...], int] = {}
        for entity_id, state in (states or {}).items():
            if not isinstance(entity_id, str) or not entity_id.startswith("light."):
                continue
            attrs = _attributes(state)
            raw = attrs.get("effect_list")
            if not _has_effect_bit(attrs) or not isinstance(raw, (list, tuple)):
                continue
            items = tuple(raw[:_RAW_SCAN_CAP])
            try:
                slot = by_raw.get(items)
            except TypeError:
                items, slot = None, None
            if slot is None:
                names = sanitize_effect_list(raw)
                if not names:
                    continue
                slot = by_sanitised.get(names)
                if slot is None:
                    slot = len(self._lists)
                    by_sanitised[names] = slot
                    self._lists.append(names)
                    self._forms.append(frozenset(f for n in names for f in _forms(n)))
                if items is not None:
                    by_raw[items] = slot
            self._list_of[entity_id] = slot
        self._all = frozenset().union(*self._forms) if self._forms else frozenset()

    @classmethod
    def for_states(cls, states: Optional[Mapping[str, Any]], generation: int) -> "EffectIndex":
        """The index for ``states``, reused while the same states object is still
        at the same ``generation`` (``HAEntityManager.cache_generation()``)."""
        memo = cls._memo
        if memo is not None and memo[0] is states and memo[1] == generation:
            return memo[2]
        index = cls(states)
        cls._memo = (states, generation, index)
        return index

    def vocab(self, ids: Optional[Iterable[str]] = None) -> FrozenSet[str]:
        """The normalised and compact forms of every effect the given lights
        (all of them when ``ids`` is None) report."""
        if ids is None:
            return self._all
        slots = {self._list_of[e] for e in ids if e in self._list_of}
        if not slots:
            return frozenset()
        return frozenset().union(*(self._forms[s] for s in slots))

    def effects(self, entity_id: str) -> Tuple[str, ...]:
        """The usable effect names one light reports; ``()`` when it takes none."""
        slot = self._list_of.get(entity_id)
        return self._lists[slot] if slot is not None else ()


# ---------------------------------------------------------------------------
# Recognising a request
# ---------------------------------------------------------------------------

_MAX_TOKENS = 32
# An utterance longer than this is never a request; checked before tokenising
# (NFKC can expand text, so the cost of ``tokens`` isn't bounded by the input).
_MAX_TEXT_CHARS = 256
_MAX_PHRASE_TOKENS = 6
_MAX_PHRASE_CHARS = 48

_SET_VERBS = frozenset({"set", "put", "change", "switch", "turn"})
_CLEAR_VERBS = frozenset({"stop", "clear", "cancel", "end", "remove", "disable", "kill"})
_ACTIVATE_VERBS: Tuple[Tuple[str, ...], ...] = (("turn", "on"), ("start",), ("run",), ("use",), ("play",), ("activate",))
_LEADING_VERBS = _SET_VERBS | _CLEAR_VERBS | {"make"}
_EFFECT_WORDS = frozenset({"effect", "effects"})
_TAIL_WORDS = frozenset({"on", "in", "for"})
_ARTICLES = frozenset({"the", "a", "an"})
_FILLERS: Tuple[Tuple[str, ...], ...] = (
    ("please",), ("can", "you"), ("could", "you"), ("would", "you"), ("will", "you"),
    ("go", "ahead", "and"), ("hey",), ("ok",), ("okay",),
)


@dataclass(frozen=True)
class EffectRequest:
    """What an utterance asks for.

    ``clear``: turn the effect off rather than set one. ``phrase``: the
    effect named (normalised words); empty for a clear that names none.
    ``span``: the token range (into ``tokens(text)``) of the effect words, for
    removing them before names are looked for. ``explicit``: the word
    "effect" or "effects" was said. ``mode_word``: the word "mode" was said.
    ``target_text``: the words naming where, or "" when none did.
    """

    clear: bool
    phrase: str
    span: Tuple[int, int]
    explicit: bool
    mode_word: bool
    target_text: str


def _window(toks: Sequence[str]) -> Tuple[int, int]:
    lo, hi = 0, len(toks)
    moved = True
    while moved and lo < hi:
        moved = False
        for filler in _FILLERS:
            if tuple(toks[lo:lo + len(filler)]) == filler:
                lo += len(filler)
                moved = True
                break
    while hi > lo and toks[hi - 1] == "please":
        hi -= 1
    return lo, hi


def has_effect_shape(text: Any) -> bool:
    """Whether the words could be a request to set or clear an effect, judged
    on their structure alone: no vocabulary, no light or room names. Most
    commands fail here; "turn on the kitchen light" and "dim the lights" do."""
    if not isinstance(text, str) or not text or len(text) > _MAX_TEXT_CHARS:
        return False
    toks = tokens(text)
    if len(toks) > _MAX_TOKENS:
        return False
    lo, hi = _window(toks)
    body = toks[lo:hi]
    n = len(body)
    if n < 2:
        return False
    if "mode" in body or any(w in body for w in _EFFECT_WORDS):
        return True
    first = body[0]
    if first in _SET_VERBS:
        connectors = ("to", "into", "on") if first in ("set", "put") else ("to", "into")
        return any(body[i] in connectors for i in range(2, n - 1))
    if first == "make":
        return n >= 3
    if first in _CLEAR_VERBS:
        return any(body[i] in _TAIL_WORDS for i in range(2, n - 1))
    return False


_NameTest = Callable[[str], bool]


class _Parse:
    """One utterance read against the grammar. Token indexes in results are
    into the full ``tokens(text)``; ``body`` is that without the filler."""

    def __init__(self, toks: Sequence[str], lo: int, hi: int, index: Optional[EffectIndex],
                 ids: Optional[Iterable[str]], claimed: _NameTest, names_target: _NameTest,
                 names_light: _NameTest) -> None:
        self.body = list(toks[lo:hi])
        self.off = lo
        self.index = index
        self._ids = ids
        self._vocab: Optional[FrozenSet[str]] = None
        self.claimed = claimed
        self.names_target = names_target
        self.names_light = names_light

    # -- pieces ------------------------------------------------------------

    @property
    def vocab(self) -> FrozenSet[str]:
        """The effect names in play, read from the index the first time a phrase
        survives the claimed check (a request an existing path owns never gets here)."""
        if self._vocab is None:
            self._vocab = self.index.vocab(self._ids) if self.index is not None else frozenset()
        return self._vocab

    def in_vocab(self, ts: Sequence[str]) -> bool:
        return bool(ts) and (" ".join(ts) in self.vocab or "".join(ts) in self.vocab)

    @staticmethod
    def strip_articles(ts: Sequence[str]) -> List[str]:
        i = 0
        while i < len(ts) and ts[i] in _ARTICLES:
            i += 1
        return list(ts[i:])

    @staticmethod
    def phrase_ok(ts: Sequence[str]) -> bool:
        return bool(ts) and len(ts) <= _MAX_PHRASE_TOKENS and len(" ".join(ts)) <= _MAX_PHRASE_CHARS

    def is_claimed(self, ts: Sequence[str], mode: bool) -> bool:
        bare = " ".join(ts)
        return bool(self.claimed(bare) or (mode and self.claimed(bare + " mode")))

    def phrase_at(self, ts: Sequence[str], start: int, *, need_vocab: bool) -> Optional[Tuple[List[str], Tuple[int, int], bool, bool]]:
        """A phrase in ``ts`` (which starts at body index ``start``): leading
        articles dropped, a closing "effect", "effects" or "mode" noted. None
        when it isn't a usable phrase."""
        ts = list(ts)
        lead = 0
        while lead < len(ts) and ts[lead] in _ARTICLES:
            lead += 1
        end = len(ts)
        explicit = mode = False
        if end > lead and ts[end - 1] in _EFFECT_WORDS:
            explicit, end = True, end - 1
        elif end > lead and ts[end - 1] == "mode":
            mode, end = True, end - 1
        phrase = ts[lead:end]
        if not self.phrase_ok(phrase):
            return None
        if not explicit and self.is_claimed(phrase, mode):
            return None
        exact = self.in_vocab(phrase)
        if (phrase[0].isdigit() or need_vocab) and not exact:
            return None
        span = (self.off + start + lead, self.off + start + len(ts))
        return phrase, span, explicit, mode

    def _ends_in_claimed_phrase(self, ts: Sequence[str]) -> bool:
        """Whether the words end in a phrase an existing path owns, said without
        "effect". Checked before any vocabulary is read."""
        if not ts or ts[-1] in _EFFECT_WORDS:
            return False
        mode = ts[-1] == "mode"
        words = list(ts[:-1] if mode else ts)
        return any(self.is_claimed(words[-n:], mode) for n in range(1, min(3, len(words)) + 1))

    def tail(self, rem: Sequence[str], *, light: bool = False) -> Optional[str]:
        """The words after "on", "in" or "for" ("" when there are none); None
        when something else follows or they name nothing the caller knows."""
        if not rem:
            return ""
        if rem[0] in _TAIL_WORDS and len(rem) > 1:
            text = " ".join(self.strip_articles(rem[1:]))
            if text and (self.names_light if light else self.names_target)(text):
                return text
        return None

    def request(self, clear: bool, phrase: Sequence[str], span: Tuple[int, int], explicit: bool,
                mode: bool, target_text: str) -> EffectRequest:
        return EffectRequest(clear, " ".join(phrase), span, explicit, mode, target_text)

    # -- the forms ---------------------------------------------------------

    def set_form(self) -> Optional[EffectRequest]:
        """T1: set|put|change|switch|turn <target> to|into <effect>; set|put <target> on <effect>."""
        b = self.body
        if len(b) < 4 or b[0] not in _SET_VERBS or self.index is None:
            return None
        for i in range(2, len(b) - 1):
            if b[i] in ("to", "into") or (b[i] == "on" and b[0] in ("set", "put")):
                found = self.phrase_at(b[i + 1:], i + 1, need_vocab=True)
                prefix = self.strip_articles(b[1:i])
                if found and prefix and self.names_target(" ".join(prefix)):
                    phrase, span, explicit, mode = found
                    return self.request(False, phrase, span, explicit, mode, " ".join(prefix))
        return None

    def make_form(self) -> Optional[EffectRequest]:
        """T2: make <target> <effect>, split at the longest suffix that is an effect."""
        b = self.body
        if len(b) < 3 or b[0] != "make" or self.index is None:
            return None
        rest = b[1:]
        if self._ends_in_claimed_phrase(rest):
            return None
        for j in range(1, len(rest)):
            if len(rest) - j > _MAX_PHRASE_TOKENS + 1:
                continue
            found = self.phrase_at(rest[j:], 1 + j, need_vocab=True)
            prefix = self.strip_articles(rest[:j])
            if found and prefix and self.names_target(" ".join(prefix)):
                phrase, span, explicit, mode = found
                return self.request(False, phrase, span, explicit, mode, " ".join(prefix))
        return None

    def activate_form(self) -> Optional[EffectRequest]:
        """T3: <effect> mode|effect [on|in|for <target>], with or without a leading verb."""
        b = self.body
        i = 0
        for verb in _ACTIVATE_VERBS:
            if tuple(b[:len(verb)]) == verb:
                i = len(verb)
                break
        while i < len(b) and b[i] in _ARTICLES:
            i += 1
        k = next((x for x in range(i, len(b)) if b[x] in _EFFECT_WORDS or b[x] == "mode"), None)
        if k is None or k == i or b[i] in _LEADING_VERBS:
            return None
        found = self.phrase_at(b[i:k + 1], i, need_vocab=False)
        if found is None:
            return None
        phrase, span, explicit, mode = found
        tail = self.tail(b[k + 1:])
        if tail is None:
            return None
        if mode:
            if not self.in_vocab(phrase):
                return None
        elif not self.in_vocab(phrase) and not (tail and self.names_light(tail)):
            return None
        return self.request(False, phrase, span, explicit, mode, tail)

    def clear_form(self) -> Optional[EffectRequest]:
        """T4: turn the effect off, in the bare, named and mode forms."""
        b = self.body
        n = len(b)
        if b[:1] == ["no"] and b[-1:] and b[-1] in _EFFECT_WORDS and b[1:-1] in ([], ["more"]):
            return self.request(True, [], (self.off + n - 1, self.off + n), True, False, "")
        if b[:2] == ["turn", "off"]:
            rest, start, need_off, stop = b[2:], 2, False, False
        elif b[0] in _CLEAR_VERBS:
            rest, start, need_off, stop = b[1:], 1, False, b[0] == "stop"
        elif b[0] == "turn":
            rest, start, need_off, stop = b[1:], 1, True, False
        else:
            rest, start, need_off, stop = b, 0, True, False
        i = 0
        while i < len(rest) and rest[i] in _ARTICLES:
            i += 1
        if rest[i:i + 1] in (["light"], ["lights"]) and rest[i + 1:i + 2] and rest[i + 1] in _EFFECT_WORDS:
            i += 1
        k = next((x for x in range(i, len(rest)) if rest[x] in _EFFECT_WORDS or rest[x] == "mode"), None)
        if k is None:
            return self._stop_form(rest, start, i) if stop else None
        phrase, word, after = rest[i:k], rest[k], rest[k + 1:]
        if after[:1] == ["off"]:
            after = after[1:]
        elif need_off:
            return None
        tail = self.tail(after)
        if tail is None:
            return None
        word_at = self.off + start + k
        explicit, mode = word in _EFFECT_WORDS, word == "mode"
        if not phrase:
            return None if mode else self.request(True, [], (word_at, word_at + 1), True, False, tail)
        phrase_span = (self.off + start + i, word_at + 1)
        if self.phrase_ok(phrase) and (explicit or not self.is_claimed(phrase, True)) and self.in_vocab(phrase):
            return self.request(True, phrase, phrase_span, explicit, mode, tail)
        if explicit and not tail and self.names_light(" ".join(phrase)):
            return self.request(True, [], (word_at, word_at + 1), True, False, " ".join(phrase))
        return None

    def _stop_form(self, rest: List[str], start: int, i: int) -> Optional[EffectRequest]:
        """stop <effect> on|in|for <light>: only when a light is named."""
        t = next((x for x in range(i + 1, len(rest) - 1) if rest[x] in _TAIL_WORDS), None)
        if t is None:
            return None
        phrase = rest[i:t]
        tail = self.tail(rest[t:], light=True)
        if not tail or not self.phrase_ok(phrase) or self.is_claimed(phrase, False):
            return None
        return self.request(True, phrase, (self.off + start + i, self.off + start + t), False, False, tail)


def recognise(
    text: Any,
    index: Optional[EffectIndex],
    ids: Optional[Iterable[str]],
    *,
    claimed: _NameTest,
    names_target: _NameTest,
    names_light: _NameTest,
) -> Optional[EffectRequest]:
    """The request ``text`` makes, or None. ``text`` is the command words.

    ``index`` and ``ids`` say which effects exist among the lights the speaker
    may use; ``index=None`` means that isn't known yet, and then only a bare
    clear and "<phrase> effect on <light>" can match. ``claimed(phrase)`` says an
    existing path owns the phrase (it then counts only with the word "effect");
    ``names_target(words)`` and ``names_light(words)`` say whether the words name
    a room or light, or a light.
    """
    if not isinstance(text, str) or not text or len(text) > _MAX_TEXT_CHARS:
        return None
    toks = tokens(text)
    if len(toks) > _MAX_TOKENS:
        return None
    lo, hi = _window(toks)
    if hi - lo < 2:
        return None
    parse = _Parse(toks, lo, hi, index, ids, claimed, names_target, names_light)
    return parse.clear_form() or parse.activate_form() or parse.set_form() or parse.make_form()


# ---------------------------------------------------------------------------
# What the automation agent may let through
# ---------------------------------------------------------------------------

PROBLEM_CARRIER = "carrier"
PROBLEM_SERVICE = "service"
PROBLEM_VALUE = "value"
PROBLEM_TARGET = "target"

_EFFECT_SERVICE = "light.turn_on"
_LIGHT_ENTITY_ID = re.compile(r"light\.[a-z0-9_]+")
_WIDE_TARGET_KEYS = ("area_id", "device_id", "label_id", "floor_id")
_MAX_DEPTH = 16
_CONTAINER_KEYS = ("sequence", "default", "then", "else", "parallel", "choose", "repeat", "if")


def _count_effect_keys(node: Any, depth: int = 0) -> int:
    """How many times the key ``effect`` appears anywhere inside. Too deep counts as many."""
    if depth > _MAX_DEPTH:
        return _MAX_DEPTH
    if isinstance(node, dict):
        return sum((1 if k == "effect" else 0) + _count_effect_keys(v, depth + 1) for k, v in node.items())
    if isinstance(node, (list, tuple)):
        return sum(_count_effect_keys(v, depth + 1) for v in node)
    return 0


_TEMPLATE_MARKERS = ("{{", "{%", "{#")
# What `light.turn_on` (and the other light services) take, plus the wide-target
# keys, which are in the set so that they are refused as targets, not as strangers.
_DATA_KEYS = frozenset({
    "entity_id", "brightness", "brightness_pct", "brightness_step", "brightness_step_pct", "color_name",
    "color_temp", "color_temp_kelvin", "kelvin", "hs_color", "rgb_color", "rgbw_color", "rgbww_color",
    "xy_color", "white", "profile", "transition", "flash", "effect",
}) | frozenset(_WIDE_TARGET_KEYS)
# The keys a service step may carry and still be judged; anything else
# (``data_template``, ``service_data``, ``service_template``, ``variables``, a
# container key) hides content this module can't see.
_STEP_KEYS = frozenset({
    "service", "action", "target", "data", "entity_id", "alias", "enabled", "continue_on_error", "id", "metadata",
})
_LIGHT_SERVICE_PREFIXES = ("light.", "homeassistant.")
_OPAQUE_VALUE_KEYS = frozenset({"data", "target", "data_template", "service_data"})


def _has_template(node: Any, depth: int = 0) -> bool:
    """Whether a Jinja marker appears anywhere inside, in a key or a value. Too deep counts as yes."""
    if depth > _MAX_DEPTH:
        return True
    if isinstance(node, str):
        return any(m in node for m in _TEMPLATE_MARKERS)
    if isinstance(node, dict):
        return any(_has_template(k, depth + 1) or _has_template(v, depth + 1) for k, v in node.items())
    if isinstance(node, (list, tuple)):
        return any(_has_template(v, depth + 1) for v in node)
    return False


def _touches_lights(service: Any) -> bool:
    """Whether a call could reach a light: a light or homeassistant service, or
    one that can't be resolved (not a plain string, or a template)."""
    if service is None:
        return False
    if not isinstance(service, str) or _has_template(service):
        return True
    return service.strip().lower().startswith(_LIGHT_SERVICE_PREFIXES)


def effect_target_ids(*specs: Any) -> Optional[List[str]]:
    """The concrete light ids the given target values name, or None.

    Each spec is a string (comma separated), a list of strings, a dict holding
    only ``entity_id``, or None (skipped). Anything else (a template, an area,
    "all", a non-string, an empty part, a non-light id) gives None, as does a
    call that names no light at all. The caller permission-checks exactly the
    list this returns.

    Stricter than ``mode_permission._split_entity_ids``, which drops empty and
    non-string parts: a part dropped there is a part nobody permission-checked
    here, so here it refuses.
    """
    ids: List[str] = []
    for spec in specs:
        if spec is None:
            continue
        if isinstance(spec, dict):
            if set(spec) - {"entity_id"}:
                return None
            spec = spec.get("entity_id")
            if spec is None:
                continue
        if isinstance(spec, str):
            parts = [p.strip() for p in spec.split(",")]
        elif isinstance(spec, (list, tuple)):
            parts = []
            for item in spec:
                if not isinstance(item, str):
                    return None
                parts.extend(p.strip() for p in item.split(","))
        else:
            return None
        if not parts or not all(_LIGHT_ENTITY_ID.fullmatch(p) for p in parts):
            return None
        ids.extend(p for p in parts if p not in ids)
    return ids or None


def effect_problem(service: Any, data: Any, targets: Any, entity_id: Any = None) -> Optional[str]:
    """Why a service call mustn't be sent, or None.

    Judges a call that carries an ``effect`` anywhere, or that could reach a
    light (a light or homeassistant service, or one that can't be resolved);
    any other call is not this function's concern. Such a call must have
    ``data`` and ``target`` that are dicts, only literal keys from the fixed
    light parameter set, and no template anywhere (keys included). An effect
    may only be a literal clean string at ``data.effect`` of a literal
    ``light.turn_on``, aimed at the concrete ``light.*`` ids of
    ``effect_target_ids`` and nothing wider. ``entity_id`` is the step's own.
    Returns one of the ``PROBLEM_*`` codes.
    """
    effects = _count_effect_keys((data, targets, entity_id))
    if not effects and not _touches_lights(service):
        return None
    if data is not None and not isinstance(data, dict):
        return PROBLEM_CARRIER
    if targets is not None and not isinstance(targets, (dict, str, list, tuple)):
        return PROBLEM_CARRIER
    visible = {k: v for k, v in data.items() if k != "effect"} if isinstance(data, dict) else None
    if _has_template((service, visible, targets, entity_id)):
        return PROBLEM_CARRIER
    if isinstance(data, dict) and any(not isinstance(k, str) or k not in _DATA_KEYS for k in data):
        return PROBLEM_CARRIER
    if not effects:
        return None
    if effects != 1 or not (isinstance(data, dict) and "effect" in data):
        return PROBLEM_CARRIER
    if service != _EFFECT_SERVICE:
        return PROBLEM_SERVICE
    effect = data["effect"]
    if (not isinstance(effect, str) or not 1 <= len(effect) <= MAX_EFFECT_NAME_CHARS
            or not EFFECT_CHARSET.fullmatch(effect) or not normalize(effect)):
        return PROBLEM_VALUE
    for container in (data, targets):
        if isinstance(container, dict) and any(k in container for k in _WIDE_TARGET_KEYS):
            return PROBLEM_TARGET
    if effect_target_ids(data.get("entity_id"), targets, entity_id) is None:
        return PROBLEM_TARGET
    return None


def effect_step_problem(step: Any) -> Optional[str]:
    """``effect_problem`` for a whole automation step: the step's own keys are
    judged too, so an ``effect`` (or anything unverifiable) in ``data_template``,
    ``service_data``, a template key or a container key is refused."""
    if not isinstance(step, dict):
        return PROBLEM_CARRIER
    service = step.get("service") if "service" in step else step.get("action")
    if "service" in step and "action" in step and step["service"] != step["action"]:
        return PROBLEM_CARRIER
    if not _count_effect_keys(step) and not _touches_lights(service):
        return None
    if any(not isinstance(k, str) or k not in _STEP_KEYS for k in step):
        return PROBLEM_CARRIER
    return effect_problem(service, step.get("data"), step.get("target"), step.get("entity_id"))


def effect_carriers(actions: Any, walk: Callable[[Any], Iterable[Any]]) -> List[Tuple[Tuple[Any, ...], Optional[Dict[str, Any]]]]:
    """Every place an ``effect`` key appears anywhere in a normalised action
    list, and every place content hides from this module: a templated key, or a
    ``data`` or ``target`` that is a template string.

    ``walk`` yields the leaf steps of an action tree (the caller passes
    ``mode_permission._walk_automation_steps``). Each result is ``(path, step)``.
    ``step`` is the step only when the key is the ``data.effect`` of a
    top-level ``service:`` leaf step in a list that is not also a container; it
    is None for every other place (``data_template``, ``service_data``,
    ``target``, a step inside a container, a container itself, a condition, a
    nested value, a bare dict, the ``action:`` form), which a caller must refuse.
    """
    bare = isinstance(actions, dict)
    steps = [actions] if bare else (actions if isinstance(actions, (list, tuple)) else [])
    leaf_ids = {id(s) for s in walk(steps)}
    found: List[Tuple[Tuple[Any, ...], Optional[Dict[str, Any]]]] = []

    def direct(step: Optional[Dict[str, Any]], holder: Dict[str, Any], holder_path: Tuple[Any, ...]) -> Optional[Dict[str, Any]]:
        if (not bare and step is not None and len(holder_path) == 2 and holder_path[1] == "data"
                and id(step) in leaf_ids and step.get("data") is holder and isinstance(step.get("service"), str)
                and not any(k in step for k in _CONTAINER_KEYS)):
            return step
        return None

    def visit(node: Any, path: Tuple[Any, ...], step: Optional[Dict[str, Any]], depth: int) -> None:
        if depth > _MAX_DEPTH:
            if _count_effect_keys(node) or _has_template(node):
                found.append((path, None))
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "effect":
                    found.append((path + (key,), direct(step, node, path)))
                elif _has_template(key) or (key in _OPAQUE_VALUE_KEYS and isinstance(value, str) and _has_template(value)):
                    found.append((path + (key,), None))
                visit(value, path + (key,), step, depth + 1)
        elif isinstance(node, (list, tuple)):
            for position, value in enumerate(node):
                visit(value, path + (position,), value if isinstance(value, dict) else step, depth + 1)

    visit(steps, (), None, 0)
    return found

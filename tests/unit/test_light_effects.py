"""light_effects: the pure module that reads effect requests and effect lists.

Vocabulary comes only from the contract fixtures in tests/fixtures/light_effects
(upstream-shaped, fictional ids): they enter through a fake ``get_states`` into
the real ``HAEntityManager.refresh_entities()``. Rooms and lights are fictional.
Randomised loops are seeded; the seed is in every failure message.
"""
from __future__ import annotations

import asyncio
import copy
import json
import random
import re
import statistics
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator import light_effects as le  # noqa: E402
from orchestrator.ha_entity_manager import HAEntityManager  # noqa: E402
from shared.ha_registry import tokens  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "light_effects"
MODULE = ROOT / "src" / "orchestrator" / "light_effects.py"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


FIXTURE_NAMES = ("govee_local", "wled", "no_effects", "effect_list_without_bit", "null_effect_list", "unavailable", "malformed")
GOVEE = load("govee_local")
WLED = load("wled")
GOVEE_ID, WLED_ID = GOVEE["entity_id"], WLED["entity_id"]
WLED_NAMES = WLED["attributes"]["effect_list"]


class FakeHA:
    def __init__(self, states):
        self.states = {s["entity_id"]: s for s in states}

    async def get_states(self):
        return [copy.deepcopy(s) for s in self.states.values()]

    async def get_state(self, entity_id, **_):
        return copy.deepcopy(self.states[entity_id])


def manager_for(*names, extra=()):
    """The real entity manager, refreshed from fixture states through a fake client."""
    states = [load(n) for n in names] + list(extra)
    manager = HAEntityManager(FakeHA(states))
    asyncio.run(manager.refresh_entities())
    return manager


@pytest.fixture(scope="module")
def house():
    manager = manager_for("govee_local", "wled")
    states = manager.cached_entities()
    return manager, states, le.EffectIndex.for_states(states, manager.cache_generation()), [GOVEE_ID, WLED_ID]


ROOMS = ("study", "living room", "bedroom 2", "kitchen", "master bedroom", "hallway")
LIGHT_NAMES = ("study prism", "prism", "hexagon", "study light")
CLAIMED_TABLE = {"movie mode", "romantic mode", "night mode", "good night", "sunrise", "sunset", "bedtime"}
COLOUR_WORDS = {"blue", "red", "green", "warm", "white"}


def names_light(words):
    text = le.normalize(words)
    return any(name in text for name in LIGHT_NAMES)


def names_target(words):
    text = le.normalize(words)
    return names_light(text) or any(room in text for room in ROOMS)


def claimed(phrase):
    """A stand-in for the deterministic tables: a table phrase, one with a closing
    "mode" removed, or a single colour word."""
    text = le.normalize(phrase)
    return text in CLAIMED_TABLE or f"{text} mode" in CLAIMED_TABLE or text in COLOUR_WORDS


def read(text, index, ids=None):
    return le.recognise(text, index, ids, claimed=claimed, names_target=names_target, names_light=names_light)


def describe(request):
    if request is None:
        return None
    return ("clear:" if request.clear else "") + request.phrase


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SOURCE_PINS = {
    "govee_local": ("govee_light_local/light.py", "2026.10.2", "govee-local-api/blob/v3.1.0"),
    "wled": ("wled/light.py", "2026.10.2", "wled/WLED/blob/v0.15.5/wled00/FX.cpp"),
    "no_effects": ("synthetic:", "2026.10.2"),
    "effect_list_without_bit": ("synthetic:", "2026.10.2"),
    "null_effect_list": ("synthetic:", "2026.10.2"),
    "unavailable": ("synthetic:", "2026.10.2"),
    "malformed": ("synthetic:", "hostile"),
}
REAL_ATTRIBUTES = {"friendly_name", "supported_features", "supported_color_modes", "color_mode", "brightness", "effect", "effect_list"}


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixtures_carry_source_and_real_attributes(name):
    state = load(name)
    assert isinstance(state["_source"], str) and state["_source"]
    for pin in SOURCE_PINS[name]:
        assert pin in state["_source"], (name, pin)
    assert {"entity_id", "state", "attributes"} <= set(state)
    assert state["entity_id"].startswith("light.example_")
    if name in ("govee_local", "wled"):
        assert REAL_ATTRIBUTES <= set(state["attributes"])
        assert state["attributes"]["supported_features"] & le.EFFECT_FEATURE_BIT


def test_fixture_populations_match_the_plan():
    assert len(GOVEE["attributes"]["effect_list"]) == 12 and GOVEE["attributes"]["effect_list"][0] == "none"
    assert len(WLED_NAMES) >= 100
    without_bit = load("effect_list_without_bit")["attributes"]
    assert without_bit["effect_list"] and not without_bit["supported_features"] & le.EFFECT_FEATURE_BIT
    assert load("null_effect_list")["attributes"]["effect_list"] is None
    assert len(load("malformed")["attributes"]["effect_list"]) == 600


SUPPORTS = {
    "govee_local": True, "wled": True, "no_effects": False, "effect_list_without_bit": False,
    "null_effect_list": False, "unavailable": False, "malformed": True,
}


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_supports_effects_needs_the_bit_and_a_usable_list(name):
    assert le.supports_effects(load(name)) is SUPPORTS[name]


def test_the_fixture_with_a_list_but_no_bit_takes_no_effects_through_the_manager():
    manager = manager_for("effect_list_without_bit", "null_effect_list", "no_effects", "unavailable")
    index = le.EffectIndex.for_states(manager.cached_entities(), manager.cache_generation())
    assert index.vocab() == frozenset()
    assert all(index.effects(e) == () for e in manager.cached_entities())


def test_the_malformed_fixture_yields_only_clean_names():
    names = le.sanitize_effect_list(load("malformed")["attributes"]["effect_list"])
    assert 0 < len(names) <= le.MAX_EFFECT_NAMES
    assert all(le.EFFECT_CHARSET.fullmatch(n) for n in names)
    assert not any(bad in n for n in names for bad in ("<", "{{", "`", "\x07"))
    assert "Valid Name" in names and "valid name" not in names


# ---------------------------------------------------------------------------
# The module carries no vocabulary of its own
# ---------------------------------------------------------------------------

_VOCAB_WORDS = re.compile(
    r"\b(candlelight|twinkle|snowflake|breathe|dating|energetic|crossing|romantic|movie|sunrise|sunset|fade|percent|stream|rain)\b",
    re.IGNORECASE,
)


def test_the_module_has_no_effect_vocabulary_and_no_logger():
    source = MODULE.read_text(encoding="utf-8")
    assert not _VOCAB_WORDS.search(source)
    assert "logging" not in source and "structlog" not in source and "logger" not in source


def test_an_effect_only_a_test_state_has_is_recognised():
    state = {"entity_id": "light.example_zebra", "state": "on",
             "attributes": {"supported_features": 4, "effect_list": ["Zebra Drift", "Candy Cane"]}}
    index = le.EffectIndex.for_states({state["entity_id"]: state}, 1)
    request = read("set the study prism to zebra drift", index)
    assert describe(request) == "zebra drift"
    assert le.match(request.phrase, index.effects("light.example_zebra")) == "Zebra Drift"


# ---------------------------------------------------------------------------
# The parse table
# ---------------------------------------------------------------------------

PARSE_ROWS = [
    # set / put / change / switch / turn <target> to|into|on <effect>
    ("set the study prism to candlelight", "candlelight"),
    ("set the study prism to the candlelight", "candlelight"),
    ("set the study prism to candlelight mode", "candlelight"),
    ("set the study prism to the candlelight effect", "candlelight"),
    ("put the study prism on candlelight", "candlelight"),
    ("change the study prism to twinkle", "twinkle"),
    ("switch the study prism into twinkle", "twinkle"),
    ("turn the study prism to snowflake", "snowflake"),
    ("please set the study prism to candlelight", "candlelight"),
    ("can you set the study prism to candlelight please", "candlelight"),
    ("set the hexagon to energetic", "energetic"),
    ("set the living room to breathe", "breathe"),
    ("set the living room to dating", "dating"),
    ("set the study to crossing", "crossing"),
    ("set the study to fire 2012", "fire 2012"),
    ("set the study to fire2012", "fire2012"),
    ("set bedroom 2 to candlelight", "candlelight"),
    ("set the kitchen to zigzag", None),
    ("set the study to blue", None),
    ("set the study to warm white", None),
    ("set the thermostat to candlelight", None),
    ("set the study lights to 50 percent", None),
    ("set the study lights to 50", None),
    ("set the study to sunset", None),
    ("set the study to romantic", None),
    ("set the study to the sunset effect", "sunset"),
    ("put the study on fade", "fade"),
    ("set the study prism on candlelight", "candlelight"),
    ("turn the study prism on candlelight", None),
    ("make the study prism sparkle dark", "sparkle dark"),
    ("make the study prism sparkle", "sparkle"),
    # make <target> <effect>
    ("make the living room twinkle", "twinkle"),
    ("make the study prism twinkle", "twinkle"),
    ("make the hexagon candlelight", "candlelight"),
    ("make bedroom 2 candlelight", "candlelight"),
    ("make the study prism candlelight effect", "candlelight"),
    ("make the study prism sunset", None),
    ("make the study brighter", None),
    ("make it twinkle", None),
    ("make the kitchen coffee", None),
    # <effect> mode|effect [on|in|for <target>] and the verb-led forms
    ("candlelight mode on the study prism", "candlelight"),
    ("candlelight mode in the living room", "candlelight"),
    ("candlelight mode for the hexagon", "candlelight"),
    ("candlelight mode", "candlelight"),
    ("twinkle effect on the study prism", "twinkle"),
    ("turn on the candlelight effect on the study prism", "candlelight"),
    ("start the twinkle effect in the living room", "twinkle"),
    ("run the snowflake mode on the hexagon", "snowflake"),
    ("use the breathe effect for the study prism", "breathe"),
    ("play the candle effect on the study prism", "candle"),
    ("activate the tv simulator effect on the study prism", "tv simulator"),
    ("turn on the tv simulator effect on the study prism", "tv simulator"),
    ("sunset effect on the study prism", "sunset"),
    ("turn on the sound effect", None),
    ("turn on the sound effect in the study", None),
    ("play the rain sound effect in the living room", None),
    ("candlelight mode on the garage door", None),
    ("candlelight effect on the tv", None),
    ("romantic mode", None),
    ("night mode on the study prism", None),
    ("movie mode in the living room", None),
    ("zigzag mode on the study prism", None),
    # clearing
    ("turn off the effect", "clear:"),
    ("remove the effect", "clear:"),
    ("disable the effects", "clear:"),
    ("kill the effect", "clear:"),
    ("effect off", "clear:"),
    ("turn off the effects", "clear:"),
    ("turn the effect off", "clear:"),
    ("stop the effect", "clear:"),
    ("cancel the effects", "clear:"),
    ("end the light effect", "clear:"),
    ("clear the effect on the study prism", "clear:"),
    ("turn off the effect in the living room", "clear:"),
    ("effects off", "clear:"),
    ("no more effects", "clear:"),
    ("no effects", "clear:"),
    ("turn off the candlelight effect", "clear:candlelight"),
    ("turn off candlelight mode", "clear:candlelight"),
    ("candlelight mode off", "clear:candlelight"),
    ("stop the twinkle on the study prism", "clear:twinkle"),
    ("turn off the study prism effect", "clear:"),
    ("turn off the effects on the tv", None),
    ("turn off the effect on the garage", None),
    ("turn off the study effect", None),
    ("turn on the 50 percent effect on the study prism", None),
    ("play the candlelite effect on the study prism", "candlelite"),
    ("candlelite mode on the study prism", None),
    ("stop the twinkle", None),
    ("stop the twinkle on the tv", None),
    ("stop the twinkle in the living room", None),
    ("stop the twinkle on the study", None),
    ("is the prism on candlelight", None),
    ("what effect is the study prism on", None),
    ("the effect of the light was nice", None),
    ("tell me about candlelight", None),
    ("fade the lights", None),
    ("turn on the kitchen light", None),
]
NAMED_PARSE_ROW = ("set the study prism to candlelight", "candlelight")


def test_parse_table_population_and_named_member():
    assert len(PARSE_ROWS) >= 60
    assert NAMED_PARSE_ROW in PARSE_ROWS


@pytest.mark.parametrize("text,expected", PARSE_ROWS, ids=[r[0] for r in PARSE_ROWS])
def test_parse_table(house, text, expected):
    _, _, index, ids = house
    assert describe(read(text, index, ids)) == expected


def test_every_positive_parse_row_passes_the_shape_gate():
    positives = [t for t, e in PARSE_ROWS if e is not None]
    assert len(positives) >= 40
    assert [t for t in positives if not le.has_effect_shape(t)] == []


def _phrase_of(tokens_count, chars):
    """Words that read as ``tokens_count`` tokens and ``chars`` characters joined by spaces."""
    letters = chars - (tokens_count - 1)
    base, extra = divmod(letters, tokens_count)
    return " ".join("a" * (base + (1 if i < extra else 0)) for i in range(tokens_count))


@pytest.mark.parametrize("count,chars,accepted", [(6, 48, True), (6, 49, False), (7, 40, False), (5, 48, True), (1, 48, True), (1, 49, False)])
def test_an_unknown_phrase_with_a_named_light_is_capped_at_six_tokens_and_48_characters(house, count, chars, accepted):
    _, _, index, ids = house
    phrase = _phrase_of(count, chars)
    assert len(phrase.split()) == count and len(phrase) == chars
    assert (read(f"play the {phrase} effect on the study prism", index, ids) is not None) is accepted


def test_leading_articles_do_not_count_against_the_phrase_cap(house):
    _, _, index, ids = house
    phrase = _phrase_of(6, 40)
    assert read(f"play the a an {phrase} effect on the study prism", index, ids) is not None


T4_NEGATIVES = (
    "turn off guest mode", "turn off away mode", "turn off sleep mode", "game mode off",
    "turn off the sound effects", "stop the stream", "stop the rain",
    "stop the washing machine", "stop the noise",
)


@pytest.mark.parametrize("text", T4_NEGATIVES)
def test_t4_negatives_are_not_requests_with_the_full_vocabulary(house, text):
    _, _, index, ids = house
    assert read(text, index, ids) is None
    assert read(text, None, None) is None


def test_t4_negative_population_and_named_member():
    assert len(T4_NEGATIVES) >= 9 and "stop the washing machine" in T4_NEGATIVES


def test_the_reported_request_carries_its_pieces(house):
    _, _, index, ids = house
    request = read("set the study prism to candlelight mode", index, ids)
    assert request == le.EffectRequest(False, "candlelight", (5, 7), False, True, "study prism")
    assert tokens("set the study prism to candlelight mode")[5:7] == ("candlelight", "mode")


def test_a_clear_names_its_target_and_says_effect(house):
    _, _, index, ids = house
    request = read("turn off the effect in the living room", index, ids)
    assert request.clear and request.explicit and request.phrase == "" and request.target_text == "living room"


def test_a_tail_that_names_nothing_is_no_request_and_never_the_speakers_room(house):
    _, _, index, ids = house
    for text in ("turn off the effects on the tv", "twinkle effect on the garage", "stop the twinkle on the fridge"):
        assert read(text, index, ids) is None, text


def test_the_filler_words_do_not_move_the_span(house):
    _, _, index, ids = house
    request = read("please set the study prism to candlelight", index, ids)
    assert tokens("please set the study prism to candlelight")[request.span[0]:request.span[1]] == ("candlelight",)


def test_a_cold_index_matches_only_a_bare_clear_and_an_effect_with_a_light():
    assert describe(read("turn off the effect", None)) == "clear:"
    assert describe(read("play the candle effect on the study prism", None)) == "candle"
    assert describe(read("effects off", None)) == "clear:"
    for text in ("set the study prism to candlelight", "make the living room twinkle", "candlelight mode on the study prism",
                 "turn off candlelight mode", "twinkle effect in the living room"):
        assert read(text, None) is None, text


def test_the_vocabulary_is_limited_to_the_permitted_ids(house):
    _, _, index, _ = house
    assert read("set the study prism to candlelight", index, [WLED_ID]) is None
    assert describe(read("set the study prism to candlelight", index, [GOVEE_ID])) == "candlelight"
    assert read("set the study prism to candlelight", index, []) is None


def test_fade_asked_of_lights_that_lack_it_is_recognised_then_honestly_unmatched(house):
    """F85: a permitted light elsewhere has the effect, no light in the room does."""
    _, _, index, ids = house
    assert describe(read("put the study on fade", index, ids)) == "fade"
    govee_list = index.effects(GOVEE_ID)
    assert le.match("fade", govee_list, fuzzy=True) is None
    offered = le.suggestions("fade", [govee_list])
    assert 0 < len(offered) <= 3 and all(o in govee_list for o in offered)
    assert le.match("fade", index.effects(WLED_ID)) == "Fade"


# ---------------------------------------------------------------------------
# has_effect_shape
# ---------------------------------------------------------------------------

SHAPE_TRUE = (
    "set the study prism to candlelight", "put the study on fade", "make the living room twinkle", "candlelight mode",
    "turn off the effect", "effects off", "no more effects", "stop the twinkle on the prism", "set the study to blue",
    "set the study lights to 50 percent", "turn off guest mode", "play the candle effect on the prism",
    "change the lamp into candlelight", "please set the study to blue",
)
SHAPE_FALSE = (
    "turn on the kitchen light", "turn off the kitchen light", "fade the lights", "is the prism on candlelight",
    "what time is it", "stop the stream", "stop the washing machine", "dim the study to 20", "", "   ", "lights",
    "open the garage", "what is the weather", "make", "set",
)


@pytest.mark.parametrize("text", SHAPE_TRUE)
def test_shape_passes(text):
    assert le.has_effect_shape(text) is True


@pytest.mark.parametrize("text", SHAPE_FALSE)
def test_shape_fails(text):
    assert le.has_effect_shape(text) is False


def test_shape_rejects_non_strings_and_oversize_utterances():
    assert not any(le.has_effect_shape(x) for x in (None, 5, b"set the study to blue", ["set"]))
    assert le.has_effect_shape("set the study to " + "blue " * 40) is False


# ---------------------------------------------------------------------------
# normalising, sanitising, matching
# ---------------------------------------------------------------------------


def test_normalize_and_compact_agree_on_spelling():
    assert le.normalize("Fire  2012") == "fire 2012" == le.normalize("fire-2012")
    assert le.compact("Fire 2012") == "fire2012" == le.compact("FIRE2012")
    assert le.normalize(None) == "" and le.compact(5) == ""


@pytest.mark.parametrize("count,kept", [(511, 511), (512, 512), (513, 512)])
def test_sanitize_caps_the_list_at_512(count, kept):
    assert len(le.sanitize_effect_list([f"Name {i}" for i in range(count)])) == kept


@pytest.mark.parametrize("length,kept", [(64, 1), (65, 0), (1, 1)])
def test_sanitize_caps_a_name_at_64_characters(length, kept):
    assert len(le.sanitize_effect_list(["a" * length])) == kept


@pytest.mark.parametrize("char", list("<>\"`{}#%\n\t\x00\x07\\;|$@^~[]=?*"))
def test_sanitize_drops_every_forbidden_character(char):
    assert le.sanitize_effect_list([f"Ab{char}cd", "Plain"]) == ("Plain",)


def test_the_charset_pattern_does_not_accept_a_trailing_newline_even_with_match():
    assert le.EFFECT_CHARSET.match("Fire\n") is None and le.EFFECT_CHARSET.match("Fire") is not None


@pytest.mark.parametrize("char", list(" .,'&()+:/!-_"))
def test_sanitize_keeps_every_allowed_punctuation(char):
    name = f"Ab{char}cd"
    assert le.sanitize_effect_list([name]) == (name,)


def test_sanitize_dedupes_by_normalised_form_and_keeps_the_first_spelling():
    assert le.sanitize_effect_list(["Fire 2012", "fire-2012", "FIRE  2012", "Other"]) == ("Fire 2012", "Other")


@pytest.mark.parametrize("raw", [None, "Fire", 5, {"a": 1}, [], [None, 3, ["x"], {}, ""], ("only", "tuple")])
def test_sanitize_never_raises_and_returns_a_tuple(raw):
    result = le.sanitize_effect_list(raw)
    assert isinstance(result, tuple) and all(isinstance(n, str) for n in result)


def test_match_is_exact_by_normalised_or_compact_form_and_returns_the_list_spelling():
    assert le.match("candlelight", GOVEE["attributes"]["effect_list"]) == "candlelight"
    assert le.match("fire 2012", WLED_NAMES) == "Fire 2012"
    assert le.match("FIRE2012", WLED_NAMES) == "Fire 2012"
    assert le.match("sparkle", WLED_NAMES) == "Sparkle"
    assert le.match("zigzag", WLED_NAMES) is None
    assert le.match("", WLED_NAMES) is None and le.match(None, WLED_NAMES) is None


def test_match_ignores_unusable_lists():
    assert le.match("x", None) is None and le.match("x", "abc") is None and le.match("x", []) is None
    assert le.match("fire", [None, 3, "Fire"]) == "Fire"


def _ratio(a, b):
    return SequenceMatcher(None, le.normalize(b), le.normalize(a)).ratio()


def _expected_fuzzy(phrase, names):
    """The spec, from difflib alone: >= 0.85, margin >= 0.05, 5-48 characters, no digit."""
    norm = le.normalize(phrase)
    if not 5 <= len(norm) <= 48 or any(c.isdigit() for c in norm):
        return None
    scored = sorted(((_ratio(norm, n), n) for n in names), reverse=True)
    best, runner_up = scored[0][0], scored[1][0] if len(scored) > 1 else 0.0
    return scored[0][1] if best >= 0.85 and best - runner_up >= 0.05 else None


def _mutations(name):
    out = set()
    for i in range(len(name)):
        out.add(name[:i] + name[i + 1:])
    for i in range(len(name) - 1):
        out.add(name[:i] + name[i + 1] + name[i] + name[i + 2:])
    return sorted(m for m in out if m and m != name)


@pytest.fixture(scope="module")
def scanned():
    """(phrase, best ratio, margin) for mutated WLED names against the full list."""
    norms = sorted({le.normalize(n) for n in WLED_NAMES if len(n) >= 6 and not any(c.isdigit() for c in n)})[::3]
    rows = []
    for name in norms:
        for phrase in _mutations(name):
            if not 5 <= len(phrase) <= 48 or "  " in phrase:
                continue
            ratios = sorted((_ratio(phrase, n) for n in WLED_NAMES), reverse=True)
            rows.append((phrase, ratios[0], ratios[0] - ratios[1]))
    return rows


def test_fuzzy_boundary_population(scanned):
    assert len(scanned) >= 200


def test_fuzzy_just_below_the_ratio_is_refused(scanned):
    below = [r for r in scanned if 0.82 <= r[1] < 0.85]
    assert below, "no pair found just below 0.85"
    for phrase, ratio, _ in below[:25]:
        assert le.match(phrase, WLED_NAMES, fuzzy=True) is None, (phrase, ratio)


def test_fuzzy_at_or_just_above_the_ratio_is_accepted(scanned):
    at = [r for r in scanned if 0.85 <= r[1] < 0.87 and r[2] >= 0.05]
    assert at, "no pair found at 0.85"
    for phrase, ratio, _ in at[:25]:
        assert le.match(phrase, WLED_NAMES, fuzzy=True) is not None, (phrase, ratio)


@pytest.fixture(scope="module")
def families():
    """(phrase, list, best ratio, margin): mutated names against the family of WLED
    names sharing their first four letters, where the runner-up is close."""
    groups = {}
    for name in WLED_NAMES:
        norm = le.normalize(name)
        if len(norm) >= 6 and not any(c.isdigit() for c in norm):
            groups.setdefault(norm[:4], []).append(name)
    rows = []
    for group in (g for g in groups.values() if len(g) >= 2):
        for member in group:
            for phrase in _mutations(le.normalize(member)):
                if not 5 <= len(phrase) <= 48 or "  " in phrase:
                    continue
                ratios = sorted((_ratio(phrase, n) for n in group), reverse=True)
                rows.append((phrase, group, ratios[0], ratios[0] - ratios[1]))
    return rows


def test_fuzzy_margin_at_or_just_above_five_hundredths_is_accepted(families):
    at = sorted((r for r in families if r[2] >= 0.85 and r[3] >= 0.05), key=lambda r: r[3])[:10]
    assert at and at[0][3] < 0.06, "no pair found at a 0.05 margin"
    for phrase, group, _, margin in at:
        assert le.match(phrase, group, fuzzy=True) is not None, (phrase, margin)


def test_fuzzy_margin_just_below_five_hundredths_is_refused(families):
    below = sorted((r for r in families if r[2] >= 0.85 and r[3] < 0.05), key=lambda r: -r[3])[:10]
    assert below and below[0][3] > 0.045, "no pair found just under a 0.05 margin"
    for phrase, group, _, margin in below:
        assert le.match(phrase, group, fuzzy=True) is None, (phrase, margin)


def test_fuzzy_agrees_with_the_spec_on_every_scanned_phrase(scanned):
    for phrase, _, _ in scanned:
        assert le.match(phrase, WLED_NAMES, fuzzy=True) == _expected_fuzzy(phrase, WLED_NAMES), phrase


def _near(base, keep, tail="xyz"):
    return base[:keep] + tail


def test_fuzzy_exact_085_is_accepted_and_just_under_is_refused():
    """2*17/40 is exactly 0.85; 2*16/38 is 0.842."""
    name_a, name_b = "abcdefghijklmnopqrst", "abcdefghijklmnopqrs"
    exactly = _near(name_a, 17)
    assert len(exactly) == 20 and SequenceMatcher(None, name_a, exactly).ratio() == 0.85
    assert le.match(exactly, [name_a], fuzzy=True) == name_a
    under = _near(name_b, 16)
    assert 0.84 < SequenceMatcher(None, name_b, under).ratio() < 0.85
    assert le.match(under, [name_b], fuzzy=True) is None


def test_fuzzy_closest_below_rows_from_the_scan_are_refused(scanned):
    below = sorted((r for r in scanned if r[1] < 0.85), key=lambda r: -r[1])[:5]
    assert below and below[0][1] > 0.8
    for phrase, ratio, _ in below:
        assert le.match(phrase, WLED_NAMES, fuzzy=True) is None, (phrase, ratio)


def test_fuzzy_four_character_phrase_is_refused_though_it_clears_the_ratio():
    assert SequenceMatcher(None, "fires", "fire").ratio() >= 0.85
    assert le.match("fire", ["fires"], fuzzy=True) is None
    assert le.match("fires", ["firest"], fuzzy=True) == "firest"


def test_fuzzy_is_off_unless_asked():
    assert le.match("candlelite", GOVEE["attributes"]["effect_list"]) is None
    assert le.match("candlelite", GOVEE["attributes"]["effect_list"], fuzzy=True) == "candlelight"


@pytest.mark.parametrize("phrase,length", [("candl", 5), ("cand", 4)])
def test_fuzzy_phrase_length_floor(phrase, length):
    assert len(phrase) == length
    names = ["candle"]
    assert (le.match(phrase, names, fuzzy=True) == "candle") is (length >= 5 and _ratio(phrase, "candle") >= 0.85)


def test_fuzzy_phrase_length_cap_is_48():
    long_name = "a" * 48
    near = "a" * 47 + "b"
    assert le.match(near, [long_name, "zzz"], fuzzy=True) == long_name
    assert le.match(near + "a", [long_name + "a", "zzz"], fuzzy=True) is None  # 49 characters


def test_fuzzy_refuses_a_phrase_with_a_digit():
    assert le.match("fire 2013", WLED_NAMES, fuzzy=True) is None


def test_fuzzy_tie_is_no_match():
    assert le.match("candlelighx", ["candlelighta", "candlelightb"], fuzzy=True) is None


def test_fuzzy_never_reaches_a_name_outside_the_given_list(house):
    _, _, index, _ = house
    assert le.match("candlelite", index.effects(WLED_ID), fuzzy=True) is None
    assert le.match("candlelite", index.effects(GOVEE_ID), fuzzy=True) == "candlelight"


def test_clear_value_prefers_off_then_none_then_solid_in_the_lists_own_spelling():
    assert le.clear_value(["Fire", "OFF", "none", "Solid"]) == "OFF"
    assert le.clear_value(["Fire", "none", "Solid"]) == "none"
    assert le.clear_value(["Fire", "Solid"]) == "Solid"
    assert le.clear_value(["Fire"]) is None
    assert le.clear_value(GOVEE["attributes"]["effect_list"]) == "none"
    assert le.clear_value(WLED_NAMES) == "Solid"
    assert le.clear_value(None) is None


def _lamp(state="on", effect="Fire 2012", names=None, features=36):
    return {"entity_id": "light.example_lamp", "state": state,
            "attributes": {"supported_features": features, "effect": effect,
                           "effect_list": list(names if names is not None else WLED_NAMES)}}


def test_active_effect_is_the_reported_effect_of_an_on_light_running_one():
    assert le.active_effect(_lamp()) == "Fire 2012"


@pytest.mark.parametrize("effect", ["Sparkle+", "Not In The List", "fire-2012"])
def test_active_effect_needs_no_membership_in_the_sanitised_list(effect):
    """D4/D14: a running "Sparkle+" is clearable though the list kept only "Sparkle"."""
    assert le.active_effect(_lamp(effect=effect)) == effect
    assert "Sparkle+" not in le.sanitize_effect_list(WLED_NAMES)
    assert le.normalize("Sparkle+") == le.normalize("Sparkle")


@pytest.mark.parametrize("state", [
    _lamp(state="off"), _lamp(state="unavailable"), _lamp(effect=None), _lamp(effect=""), _lamp(effect="Solid"),
    _lamp(effect="none", names=["none", "Fire"]), _lamp(effect=7), _lamp(effect="x" * 65), _lamp(effect="..."), None, "on", {},
])
def test_active_effect_is_none_when_there_is_nothing_to_clear(state):
    assert le.active_effect(state) is None


def test_sparkle_and_sparkle_plus_collide_and_the_first_spelling_is_kept():
    """Indistinguishable by voice (D14): keep the first, in either order."""
    assert le.sanitize_effect_list(["Sparkle", "Sparkle+"]) == ("Sparkle",)
    assert le.sanitize_effect_list(["Sparkle+", "Sparkle"]) == ("Sparkle+",)
    assert "Sparkle" in WLED_NAMES and "Sparkle+" in WLED_NAMES
    index = le.EffectIndex({WLED_ID: WLED})
    assert "Sparkle" in index.effects(WLED_ID) and "Sparkle+" not in index.effects(WLED_ID)
    assert le.match("sparkle", index.effects(WLED_ID)) == "Sparkle"


def test_suggestions_are_real_distinct_names_nearest_first():
    offered = le.suggestions("candlelite", [GOVEE["attributes"]["effect_list"], GOVEE["attributes"]["effect_list"]])
    assert offered[0] == "candlelight" and len(offered) == len(set(offered)) <= 3
    assert le.suggestions("", [WLED_NAMES]) == [] and le.suggestions("x", [WLED_NAMES], k=0) == []


@pytest.mark.parametrize("effect,spoken,expected", [
    ("Candlelight", ("set", "the", "prism", "to", "candlelight"), True),
    ("Fire 2012", ("fire", "2012"), True),
    ("Fire 2012", ("fire2012",), True),
    ("Fire 2012", ("fire", "2013"), False),
    ("Sparkle Dark", ("sparkle",), False),
    ("Twinkle", ("twinkles",), False),
    ("", ("anything",), False),
    ("Fire", (), False),
    (None, ("fire",), False),
])
def test_spoken_in(effect, spoken, expected):
    assert le.spoken_in(effect, spoken) is expected


@pytest.mark.parametrize("effect,spoken,expected", [
    ("Fire 2012", ("set", "fire", "2012", "now"), (1, 3)),
    ("Fire 2012", ("set", "fire2012"), (1, 2)),
    ("Twinkle", ("twinkle", "twinkle"), (0, 1)),
    ("Fire 2012", ("fire", "2013"), None),
    ("", ("a",), None), (None, ("a",), None), ("Fire", (), None),
])
def test_spoken_span_is_the_first_matched_token_range(effect, spoken, expected):
    assert le.spoken_span(effect, spoken) == expected
    assert le.spoken_in(effect, spoken) is (expected is not None)


def test_spoken_name_is_short_lower_case_and_clean():
    assert le.spoken_name("Fire  2012") == "fire 2012"
    long_name = "Chase Flash Rnd " * 4
    assert len(le.spoken_name(long_name)) <= 32 and le.spoken_name(long_name) == "chase flash rnd chase flash rnd"
    assert len(le.spoken_name("x" * 90)) == 32 and le.spoken_name(None) == ""


# ---------------------------------------------------------------------------
# EffectIndex and the cache generation
# ---------------------------------------------------------------------------


def test_index_lists_only_lights_that_take_effects(house):
    _, states, index, _ = house
    assert set(states) == {GOVEE_ID, WLED_ID}
    assert index.effects(GOVEE_ID) == tuple(GOVEE["attributes"]["effect_list"])
    assert index.effects("light.nonexistent") == ()
    assert "candlelight" in index.vocab() and "fire 2012" in index.vocab() and "fire2012" in index.vocab()
    assert "candlelight" not in index.vocab([WLED_ID]) and index.vocab(["light.nonexistent"]) == frozenset()


def test_index_ignores_entities_that_are_not_lights():
    switch = {"entity_id": "switch.example", "state": "on", "attributes": {"supported_features": 4, "effect_list": ["Fire"]}}
    assert le.EffectIndex({switch["entity_id"]: switch}).vocab() == frozenset()


def test_the_manager_counts_generations_through_a_bulk_refresh_and_a_single_read():
    manager = manager_for("govee_local")
    g0 = manager.cache_generation()
    asyncio.run(manager.refresh_entities())
    assert manager.cache_generation() == g0 + 1
    manager.invalidate([GOVEE_ID])
    asyncio.run(manager.get_entities())
    assert manager.cache_generation() == g0 + 2


def test_index_rebuilds_after_an_in_place_entity_patch():
    manager = manager_for("govee_local")
    states = manager.cached_entities()
    first = le.EffectIndex.for_states(states, manager.cache_generation())
    assert le.EffectIndex.for_states(states, manager.cache_generation()) is first
    patched = copy.deepcopy(GOVEE)
    patched["attributes"]["effect_list"] = ["none", "zebra drift"]
    manager._ha.states[GOVEE_ID] = patched
    manager.invalidate([GOVEE_ID])
    asyncio.run(manager.get_entities())
    assert manager.cached_entities() is states, "patched in place, not replaced"
    second = le.EffectIndex.for_states(states, manager.cache_generation())
    assert second is not first
    assert second.effects(GOVEE_ID) == ("none", "zebra drift") and first.effects(GOVEE_ID) != second.effects(GOVEE_ID)


def test_the_memo_does_not_cross_between_different_states_at_the_same_generation():
    a = manager_for("govee_local")
    b = manager_for("wled")
    assert a.cache_generation() == b.cache_generation()
    ia = le.EffectIndex.for_states(a.cached_entities(), a.cache_generation())
    ib = le.EffectIndex.for_states(b.cached_entities(), b.cache_generation())
    assert ia is not ib and "candlelight" in ia.vocab() and "candlelight" not in ib.vocab()


@pytest.fixture
def builds(monkeypatch):
    count = {"n": 0}
    original = le.EffectIndex.__init__

    def counting(self, states):
        count["n"] += 1
        original(self, states)

    monkeypatch.setattr(le.EffectIndex, "__init__", counting)
    le.EffectIndex._memo = None
    return count


def _pipeline(text, states, generation, ids):
    """What a caller does: the shape gate first, the index only after it."""
    if not le.has_effect_shape(text):
        return None
    return read(text, le.EffectIndex.for_states(states, generation), ids)


NON_EFFECT_VERBS = ("turn on", "turn off", "dim", "brighten", "open", "close", "lock", "unlock", "what is", "play", "pause", "how many")
NON_EFFECT_OBJECTS = ("the kitchen light", "the study", "the living room lights", "the garage door", "the front lock", "the tv",
                      "the hallway fan", "bedroom 2", "the thermostat", "the study prism", "the hexagon", "the porch light",
                      "the music", "the blinds", "the master bedroom", "the weather", "the time", "the stove", "the oven", "a timer")


def test_no_shape_corpus_builds_no_index(house, builds):
    _, states, _, ids = house
    corpus = [f"{verb} {obj}" for verb in NON_EFFECT_VERBS for obj in NON_EFFECT_OBJECTS]
    assert len(corpus) >= 200
    assert not any(le.has_effect_shape(t) for t in corpus)
    for text in corpus:
        assert _pipeline(text, states, 1, ids) is None
    assert builds["n"] == 0


def test_shape_passing_control_commands_build_at_most_once_per_generation(house, builds):
    _, states, _, ids = house
    corpus = [f"set the {room} to {colour}" for room in ("study", "kitchen", "hallway", "living room", "bedroom 2", "master bedroom")
              for colour in ("blue", "red", "green", "warm white", "purple", "orange", "pink", "yellow", "cyan", "teal")]
    corpus += [f"set the {room} lights to {n} percent" for room in ("study", "kitchen", "hallway") for n in (10, 40, 70, 100)]
    assert len(corpus) >= 50 and all(le.has_effect_shape(t) for t in corpus)
    timings = []
    for text in corpus:
        started = time.perf_counter()
        assert _pipeline(text, states, 7, ids) is None, text
        timings.append(time.perf_counter() - started)
    assert builds["n"] == 1
    assert statistics.median(timings) < 0.02  # 2 ms budget, 10x headroom
    _pipeline(corpus[0], states, 8, ids)
    assert builds["n"] == 2


def test_claimed_phrases_reject_before_the_index(house, monkeypatch):
    _, _, index, ids = house
    reads = {"n": 0}
    original = le.EffectIndex.vocab

    def spy(self, ids=None):
        reads["n"] += 1
        return original(self, ids)

    monkeypatch.setattr(le.EffectIndex, "vocab", spy)
    for text in ("set the study to sunset", "set the study to romantic mode", "make the living room sunrise",
                 "put the study on movie", "turn off night mode", "stop the movie on the study prism"):
        assert read(text, index, ids) is None, text
    assert reads["n"] == 0
    assert describe(read("set the study to the sunset effect", index, ids)) == "sunset" and reads["n"] == 1


def test_worst_case_long_utterance_shape_gate_under_one_ms():
    text = ("set the study to " + "word " * 250)[:1000]
    assert len(text.encode()) == 1000
    timings = []
    for _ in range(20):
        started = time.perf_counter()
        le.has_effect_shape(text)
        timings.append(time.perf_counter() - started)
    assert min(timings) < 0.1  # a loose ceiling (budget 1 ms); the length cap is what is pinned, below
    short = []
    for _ in range(20):
        started = time.perf_counter()
        le.has_effect_shape("turn on the kitchen light")
        short.append(time.perf_counter() - started)
    assert statistics.median(short) < 0.05  # a loose ceiling (budget 0.05 ms)


def test_a_text_over_the_length_cap_is_rejected_before_it_is_tokenised(monkeypatch):
    seen = {"n": 0}
    original = le.tokens

    def spy(text):
        seen["n"] += 1
        return original(text)

    monkeypatch.setattr(le, "tokens", spy)
    long_text = "set the study to candlelight " + "x" * 300
    assert len(long_text) > 256
    assert le.has_effect_shape(long_text) is False
    assert le.recognise(long_text, None, None, claimed=claimed, names_target=names_target, names_light=names_light) is None
    assert seen["n"] == 0
    assert le.has_effect_shape("set the study to candlelight") is True and seen["n"] == 1


def test_identical_effect_lists_normalise_once(monkeypatch):
    calls = {"n": 0}
    original = le.sanitize_effect_list

    def counting(raw):
        calls["n"] += 1
        return original(raw)

    monkeypatch.setattr(le, "sanitize_effect_list", counting)
    states = {}
    for i in range(60):
        lamp = copy.deepcopy(WLED)
        lamp["entity_id"] = f"light.example_wled_{i}"
        states[lamp["entity_id"]] = lamp
    index = le.EffectIndex(states)
    assert calls["n"] == 1
    assert len(index._lists) == 1 and len(index._list_of) == 60


def test_recognise_sixty_wled_lights_within_budget():
    states = {}
    for i in range(60):
        lamp = copy.deepcopy(WLED)
        lamp["entity_id"] = f"light.example_wled_{i}"
        states[lamp["entity_id"]] = lamp
    colds = []
    for generation in range(5):
        for cache in (le._forms, le._sanitise_cached, le._table):
            cache.cache_clear()
        started = time.perf_counter()
        index = le.EffectIndex.for_states(states, generation + 1)
        colds.append(time.perf_counter() - started)
    assert min(colds) < 0.5  # a loose ceiling (budget 15 ms), caches cleared each time
    ids = list(states)
    timings = []
    for _ in range(20):
        started = time.perf_counter()
        request = read("set the study prism to twinkle", index, ids)
        timings.append(time.perf_counter() - started)
    assert describe(request) == "twinkle" and statistics.median(timings) < 0.2  # a loose ceiling (budget 2 ms)
    matches = []
    for _ in range(5):
        started = time.perf_counter()
        hits = [le.match("twinkle", index.effects(i)) for i in ids]
        matches.append(time.perf_counter() - started)
    assert min(matches) < 0.5 and set(hits) == {"Twinkle"}  # a loose ceiling (budget 5 ms)


def test_caches_do_the_work_once_not_once_per_light():
    """The deterministic guard behind the timing ceilings."""
    states = {}
    for i in range(60):
        lamp = copy.deepcopy(WLED)
        lamp["entity_id"] = f"light.example_wled_{i}"
        states[lamp["entity_id"]] = lamp
    for cache in (le._forms, le._sanitise_cached, le._table):
        cache.cache_clear()
    index = le.EffectIndex(states)
    assert le._sanitise_cached.cache_info().misses == 1
    assert le._forms.cache_info().misses == len(WLED_NAMES)
    ids = list(states)
    assert {le.match("twinkle", index.effects(i)) for i in ids} == {"Twinkle"}
    info = le._table.cache_info()
    assert info.misses == 1 and info.hits == len(ids) - 1


def test_match_scores_only_candidates_the_prefilter_lets_through(monkeypatch):
    calls = {"n": 0}

    class Counting(SequenceMatcher):
        def ratio(self):
            calls["n"] += 1
            return super().ratio()

    monkeypatch.setattr(le, "SequenceMatcher", Counting)
    assert le.match("twinkle", WLED_NAMES, fuzzy=True) == "Twinkle" and calls["n"] == 0
    assert le.match("qqqqqqqq", WLED_NAMES, fuzzy=True) is None and calls["n"] == 0
    assert le.match("twinkel", WLED_NAMES, fuzzy=True) == "Twinkle"
    assert 0 < calls["n"] < len(WLED_NAMES) // 4
    calls["n"] = 0
    assert le.suggestions("twinkel", [WLED_NAMES]) and 0 < calls["n"] <= 10


def test_a_table_never_holds_more_than_the_name_cap():
    names = tuple(f"Name {i}" for i in range(le.MAX_EFFECT_NAMES + 50))
    assert len(le._table(names)[2]) == le.MAX_EFFECT_NAMES
    assert le.match("Name 600", names) is None and le.match("Name 3", names) == "Name 3"


def test_suggestions_cut_a_long_phrase_and_stay_nearest_first(monkeypatch):
    seen = []

    class Recording(SequenceMatcher):
        def set_seq2(self, b):
            seen.append(len(b))
            super().set_seq2(b)

    monkeypatch.setattr(le, "SequenceMatcher", Recording)
    offered = le.suggestions("twinkel " * 40, [WLED_NAMES])
    assert len(offered) <= 3 and max(seen) == 48 and seen[-1] == 48
    monkeypatch.undo()
    offered = le.suggestions("twinkel", [WLED_NAMES])
    ratios = [SequenceMatcher(None, "twinkel", le.normalize(n)).ratio() for n in offered]
    assert offered[0] == "Twinkle" and ratios == sorted(ratios, reverse=True)
    best_outside = max(SequenceMatcher(None, "twinkel", le.normalize(n)).ratio() for n in WLED_NAMES if n not in offered)
    assert best_outside <= ratios[-1]


@pytest.mark.parametrize("scanned_names,kept", [(2047, ["Valid"]), (2048, [])])
def test_names_past_the_raw_scan_cap_are_never_read(scanned_names, kept):
    assert le.sanitize_effect_list(["<"] * scanned_names + ["Valid"]) == tuple(kept)


def test_the_malformed_fixture_goes_through_the_manager_and_index_cleanly():
    manager = manager_for("malformed")
    index = le.EffectIndex.for_states(manager.cached_entities(), manager.cache_generation())
    names = index.effects(load("malformed")["entity_id"])
    assert 0 < len(names) <= le.MAX_EFFECT_NAMES and all(le.EFFECT_CHARSET.fullmatch(n) for n in names)
    assert "<" not in "".join(index.vocab()) and "{" not in "".join(index.vocab())
    assert "pattern 5" in index.vocab()


def _running(name, effect, state="on"):
    lamp = load(name)
    lamp["state"] = state
    lamp["attributes"]["effect"] = effect
    return lamp


@pytest.mark.parametrize("name,effect,state,expected", [
    ("wled", "Fire 2012", "on", "Fire 2012"),
    ("wled", "Solid", "on", None),
    ("wled", "Fire 2012", "off", None),
    ("govee_local", "candlelight", "on", "candlelight"),
    ("govee_local", "none", "on", None),
    ("govee_local", None, "on", None),
    ("wled", "Sparkle+", "on", "Sparkle+"),
])
def test_active_effect_over_running_fixture_variants(name, effect, state, expected):
    assert le.active_effect(_running(name, effect, state)) == expected


# ---------------------------------------------------------------------------
# effect_problem and effect_carriers
# ---------------------------------------------------------------------------

LIGHT = "light.example_lamp"
OK_TARGETS = [LIGHT, f"{LIGHT}, light.example_two", [LIGHT, "light.example_two"], {"entity_id": LIGHT}, {"entity_id": [LIGHT]}]


@pytest.mark.parametrize("targets", OK_TARGETS, ids=["str", "comma", "list", "dict", "dict-list"])
def test_effect_problem_accepts_a_literal_effect_on_light_turn_on(targets):
    assert le.effect_problem("light.turn_on", {"effect": "Fire"}, targets) is None


def test_effect_problem_accepts_the_target_inside_data():
    assert le.effect_problem("light.turn_on", {"effect": "Fire", "entity_id": LIGHT}, None) is None
    assert le.effect_problem("light.turn_on", {"effect": "Fire", "entity_id": [LIGHT]}, {}) is None


def test_effect_problem_is_silent_when_no_effect_is_carried():
    assert le.effect_problem("light.turn_off", {"brightness": 5}, "area_id") is None
    assert le.effect_problem("anything", None, None) is None


@pytest.mark.parametrize("targets", [
    {"area_id": "study"}, {"entity_id": LIGHT, "device_id": "d"}, {"label_id": "l"}, {"floor_id": "f"},
    "all", {"entity_id": "all"}, None, "", [], {}, "switch.example", "light.Example", [LIGHT, 5],
    {"entity_id": LIGHT, "extra": 1},
], ids=lambda t: repr(t)[:30])
def test_effect_problem_refuses_a_target_that_is_not_concrete_lights(targets):
    assert le.effect_problem("light.turn_on", {"effect": "Fire"}, targets) == le.PROBLEM_TARGET


@pytest.mark.parametrize("targets", ["light.{{ x }}", 5, 2.5, True])
def test_effect_problem_refuses_a_target_that_is_a_template_or_not_a_target_type(targets):
    assert le.effect_problem("light.turn_on", {"effect": "Fire"}, targets) == le.PROBLEM_CARRIER


@pytest.mark.parametrize("key", ["area_id", "device_id", "label_id", "floor_id"])
def test_effect_problem_refuses_a_wide_target_inside_data(key):
    assert le.effect_problem("light.turn_on", {"effect": "Fire", "entity_id": LIGHT, key: "x"}, LIGHT) == le.PROBLEM_TARGET


@pytest.mark.parametrize("service", ["homeassistant.turn_on", "light.turn_off", "light.toggle", None, 5, "LIGHT.turn_on", "light.turn_on ", "notify.x"])
def test_effect_problem_refuses_any_other_service(service):
    assert le.effect_problem(service, {"effect": "Fire"}, LIGHT) == le.PROBLEM_SERVICE


@pytest.mark.parametrize("service", ["{{ s }}", "{% x %}", "light.{{ s }}", "{# c #}"])
def test_effect_problem_refuses_a_templated_service(service):
    assert le.effect_problem(service, {"effect": "Fire"}, LIGHT) == le.PROBLEM_CARRIER
    assert le.effect_problem(service, {"brightness": 5}, LIGHT) == le.PROBLEM_CARRIER


@pytest.mark.parametrize("data,targets", [
    ({"{{ 'eff' ~ 'ect' }}": "Fire"}, LIGHT),
    ({"effect": "Fire", "{{ 'area_' ~ 'id' }}": "study"}, LIGHT),
    ({"effect": "Fire"}, {"{{ 'area_' ~ 'id' }}": "study"}),
    ("{{ {'effect': 'Fire'} }}", LIGHT),
    ("plain string", LIGHT),
    ([("effect", "Fire")], LIGHT),
    ({"effect": "Fire", "entity_id": "{{ x }}"}, LIGHT),
    ({"brightness": "{{ 5 }}"}, LIGHT),
    ({"unknown_key": 1}, LIGHT),
    ({1: "x"}, LIGHT),
], ids=["templated-effect-key", "templated-area-key-in-data", "templated-area-key-in-target", "template-data", "string-data",
        "list-data", "templated-entity", "templated-value", "unknown-key", "int-key"])
def test_effect_problem_is_an_allowlist_for_any_call_that_could_reach_a_light(data, targets):
    """F91: it doesn't take a literal "effect" key to be refused."""
    for service in ("light.turn_on", "light.turn_off", "homeassistant.turn_on", "{{ s }}"):
        assert le.effect_problem(service, data, targets) is not None, (service, data)


def test_effect_problem_leaves_a_plain_call_to_another_integration_alone():
    assert le.effect_problem("notify.mobile", {"message": "{{ now() }}", "extra": 1}, "{{ x }}") is None
    assert le.effect_problem("light.turn_off", {"transition": 2, "brightness_pct": 5}, LIGHT) is None


def test_effect_problem_judges_the_steps_own_entity_id():
    """F92."""
    assert le.effect_problem("light.turn_on", {"effect": "Fire"}, None, LIGHT) is None
    assert le.effect_problem("light.turn_on", {"effect": "Fire"}, None, "all") == le.PROBLEM_TARGET
    assert le.effect_problem("light.turn_on", {"effect": "Fire"}, LIGHT, "switch.x") == le.PROBLEM_TARGET
    assert le.effect_problem("light.turn_on", {"effect": "Fire"}, None, None) == le.PROBLEM_TARGET


@pytest.mark.parametrize("step,expected", [
    ({"service": "light.turn_on", "entity_id": LIGHT, "data": {"effect": "Fire"}}, None),
    ({"service": "light.turn_on", "target": {"entity_id": LIGHT}, "data": {"effect": "Fire"}, "alias": "x"}, None),
    ({"service": "light.turn_on", "entity_id": "all", "data": {"effect": "Fire"}}, le.PROBLEM_TARGET),
    ({"service": "light.turn_on", "target": {"entity_id": LIGHT}, "data_template": {"effect": "Fire"}}, le.PROBLEM_CARRIER),
    ({"service": "light.turn_on", "entity_id": LIGHT, "service_data": {"effect": "Fire"}}, le.PROBLEM_CARRIER),
    ({"service": "light.turn_on", "entity_id": LIGHT, "data": {"effect": "Fire"}, "service_template": "x"}, le.PROBLEM_CARRIER),
    ({"service": "light.turn_on", "entity_id": LIGHT, "data": {"effect": "Fire"}, "sequence": []}, le.PROBLEM_CARRIER),
    ({"service": "light.turn_on", "action": "homeassistant.turn_on", "entity_id": LIGHT, "data": {"effect": "Fire"}}, le.PROBLEM_CARRIER),
    ({"action": "light.turn_on", "entity_id": LIGHT, "data": {"effect": "Fire"}}, None),
    ({"service": "light.turn_off", "entity_id": LIGHT, "variables": {"x": 1}}, le.PROBLEM_CARRIER),
    ({"service": "notify.x", "data": {"message": "{{ x }}"}}, None),
    ({"delay": 5}, None),
    ("not a step", le.PROBLEM_CARRIER),
])
def test_effect_step_problem(step, expected):
    assert le.effect_step_problem(step) == expected


@pytest.mark.parametrize("specs,expected", [
    ((LIGHT,), [LIGHT]),
    ((f"{LIGHT}, light.example_two",), [LIGHT, "light.example_two"]),
    (([LIGHT, "light.example_two, light.example_three"],), [LIGHT, "light.example_two", "light.example_three"]),
    (({"entity_id": [LIGHT]}, LIGHT), [LIGHT]),
    ((None, LIGHT, None), [LIGHT]),
    ((), None), ((None,), None), (("",), None), ((f"{LIGHT},,light.example_two",), None), ((f"{LIGHT},",), None),
    (({"entity_id": LIGHT, "area_id": "x"},), None), (("all",), None), (([LIGHT, 5],), None), ((5,), None),
    (("switch.x",), None), (("light.{{ x }}",), None), ((LIGHT, "all"), None),
])
def test_effect_target_ids(specs, expected):
    assert le.effect_target_ids(*specs) == expected


def test_effect_problem_and_effect_target_ids_agree_on_every_target():
    """F93: whatever clears effect_problem is exactly what effect_target_ids returns."""
    rng = random.Random(SEED + 7)
    pool = [LIGHT, "light.example_two", "all", "", 5, None, "switch.x", "light.A", f"{LIGHT}, light.example_two",
            [LIGHT], [LIGHT, 5], {"entity_id": LIGHT}, {"entity_id": [LIGHT], "area_id": "a"}, "light.{{ x }}"]
    cleared = 0
    for case in range(CASES):
        data_id, target, step_id = rng.choice(pool), rng.choice(pool), rng.choice(pool)
        data = {"effect": "Fire"}
        if data_id is not None:
            data["entity_id"] = data_id
        where = f"seed={SEED + 7} case={case} {data!r} {target!r} {step_id!r}"
        problem = le.effect_problem("light.turn_on", data, target, step_id)
        ids = le.effect_target_ids(data.get("entity_id"), target, step_id)
        if problem is None:
            cleared += 1
            assert ids and all(i.startswith("light.") for i in ids), where
        else:
            assert problem in (le.PROBLEM_TARGET, le.PROBLEM_CARRIER) and ids is None, where
    assert cleared > 10


@pytest.mark.parametrize("effect", [5, None, True, "", ["Fire"], {"a": 1}, "x" * 65, "{{ states('x') }}", "a<b", 'a"b', "a`b", "a#b", "a%b", "a\nb", "   .", "a{b}"])
def test_effect_problem_refuses_an_effect_that_is_not_a_clean_string(effect):
    assert le.effect_problem("light.turn_on", {"effect": effect}, LIGHT) in (le.PROBLEM_VALUE, le.PROBLEM_CARRIER)
    if isinstance(effect, str):
        assert le.effect_problem("light.turn_on", {"effect": effect}, LIGHT) == le.PROBLEM_VALUE


@pytest.mark.parametrize("data,targets", [
    ({"data_template": {"effect": "Fire"}}, LIGHT),
    ({"service_data": {"effect": "Fire"}}, LIGHT),
    ({"nested": {"effect": "Fire"}}, LIGHT),
    ({"effect": "Fire", "nested": {"effect": "Other"}}, LIGHT),
    ({"effect": "Fire"}, {"entity_id": LIGHT, "effect": "x"}),
    ({"items": [{"effect": "Fire"}]}, LIGHT),
], ids=["data_template", "service_data", "nested", "double", "target", "list"])
def test_effect_problem_refuses_an_effect_anywhere_but_data_effect(data, targets):
    assert le.effect_problem("light.turn_on", data, targets) == le.PROBLEM_CARRIER


def _walk(steps):
    from orchestrator.mode_permission import _walk_automation_steps
    return _walk_automation_steps(steps)


LEAF = {"service": "light.turn_on", "target": {"entity_id": LIGHT}, "data": {"effect": "Fire"}}


def test_carriers_accept_only_a_plain_leaf_data_effect():
    found = le.effect_carriers([copy.deepcopy(LEAF), {"service": "light.turn_off", "entity_id": LIGHT}], _walk)
    assert len(found) == 1 and found[0][1] is not None and found[0][0][-2:] == ("data", "effect")
    assert le.effect_carriers([], _walk) == [] and le.effect_carriers(None, _walk) == []
    assert le.effect_carriers([{"service": "light.turn_off"}], _walk) == []


def test_a_bare_step_dict_carrying_an_effect_fails_closed():
    found = le.effect_carriers(copy.deepcopy(LEAF), _walk)
    assert len(found) == 1 and found[0][1] is None


def test_the_action_form_carrying_an_effect_fails_closed():
    step = {"action": "light.turn_on", "target": {"entity_id": LIGHT}, "data": {"effect": "Fire"}}
    found = le.effect_carriers([step], _walk)
    assert len(found) == 1 and found[0][1] is None


@pytest.mark.parametrize("step", [
    {"service": "light.turn_on", "target": {"entity_id": LIGHT}, "data": {"{{ 'eff' ~ 'ect' }}": "Fire"}},
    {"service": "light.turn_on", "target": {"{{ 'area_' ~ 'id' }}": "study"}, "data": {"effect": "Fire"}},
    {"service": "light.turn_on", "target": {"entity_id": LIGHT}, "data": "{{ {'effect': 'Fire'} }}"},
    {"service": "light.turn_on", "target": "{% if x %}{% endif %}", "data": {}},
    {"service": "light.turn_on", "data": {"{# c #}": 1}},
], ids=["templated-effect-key", "templated-area-key", "string-data", "string-target", "comment-key"])
def test_carriers_see_content_hidden_by_a_template(step):
    found = le.effect_carriers([step], _walk)
    assert any(s is None for _, s in found), found


def _containers(leaf):
    leaf = copy.deepcopy(leaf)
    return {
        "choose": {"choose": [{"conditions": [], "sequence": [leaf]}]},
        "repeat": {"repeat": {"count": 2, "sequence": [leaf]}},
        "parallel": {"parallel": [leaf]},
        "then": {"if": [], "then": [leaf]},
        "else": {"if": [], "else": [leaf]},
        "sequence": {"sequence": [leaf]},
        "default": {"choose": [], "default": [leaf]},
    }


@pytest.mark.parametrize("kind", sorted(_containers(LEAF)))
def test_a_leaf_inside_any_container_has_no_step_so_a_caller_refuses(kind):
    found = le.effect_carriers([_containers(LEAF)[kind]], _walk)
    assert len(found) == 1 and found[0][1] is None and found[0][0][-2:] == ("data", "effect"), kind


@pytest.mark.parametrize("step", [
    {"service": "light.turn_on", "target": {"entity_id": LIGHT}, "data_template": {"effect": "Fire"}},
    {"service": "light.turn_on", "target": {"entity_id": LIGHT}, "service_data": {"effect": "Fire"}},
    {"service": "light.turn_on", "target": {"entity_id": LIGHT, "effect": "Fire"}},
    {"action": "light.turn_on", "entity_id": LIGHT, "effect": "Fire"},
    {"service": "light.turn_on", "target": {"entity_id": LIGHT}, "data": {"effect": "Fire"}, "sequence": [{"service": "x"}]},
    {"condition": "state", "effect": "x"},
], ids=["data_template", "service_data", "target", "bare-key", "container-and-service", "condition"])
def test_carriers_outside_data_effect_of_a_leaf_have_no_step(step):
    found = le.effect_carriers([step], _walk)
    assert found and all(s is None for _, s in found)


def test_carriers_see_an_effect_on_the_container_itself():
    found = le.effect_carriers([{"sequence": [{"service": "light.turn_off"}], "effect": "Fire"}], _walk)
    assert len(found) == 1 and found[0][1] is None


def test_carriers_fail_closed_beyond_the_depth_limit():
    deep = {"effect": "Fire"}
    for _ in range(40):
        deep = {"nest": deep}
    found = le.effect_carriers([{"service": "light.turn_on", "data": deep}], _walk)
    assert found and all(s is None for _, s in found)


# ---------------------------------------------------------------------------
# Seeded invariant loops
# ---------------------------------------------------------------------------

CASES = 500
SEED = 20261018
ALPHABET = list("abcdefghij kLMNOP0123456789-+&'.,()/!_<>{}#%\"`\n\t") + ["é", "ß", "Ａ", "中", "́"]


def _junk(rng, limit=70):
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, limit)))


def _junk_list(rng):
    pool = [lambda: _junk(rng), lambda: rng.choice(WLED_NAMES), lambda: rng.choice([None, 3, 2.5, True, [], {}, ()]),
            lambda: rng.choice(["none", "off", "Solid", "OFF", "Fire 2012"])]
    return [rng.choice(pool)() for _ in range(rng.randint(0, 40))]


def _clean_member(name):
    return isinstance(name, str) and 1 <= len(name) <= 64 and le.EFFECT_CHARSET.fullmatch(name) is not None


def test_seeded_sanitize_effect_list():
    rng = random.Random(SEED)
    for case in range(CASES):
        raw = _junk_list(rng)
        out = le.sanitize_effect_list(raw)
        where = f"seed={SEED} case={case} raw={raw!r}"
        assert isinstance(out, tuple) and len(out) <= le.MAX_EFFECT_NAMES, where
        assert all(_clean_member(n) for n in out), where
        assert len({le.normalize(n) for n in out}) == len(out), where
        assert all(n in raw for n in out), where


def test_seeded_match_returns_only_members_of_the_given_list():
    rng = random.Random(SEED + 1)
    for case in range(CASES):
        names = [n for n in _junk_list(rng) if isinstance(n, str)]
        phrase = rng.choice([_junk(rng), rng.choice(WLED_NAMES), rng.choice(WLED_NAMES).lower()[:-1]])
        for fuzzy in (False, True):
            out = le.match(phrase, names, fuzzy=fuzzy)
            where = f"seed={SEED + 1} case={case} fuzzy={fuzzy} phrase={phrase!r} names={names!r}"
            assert out is None or (out in names and _clean_member(out)), where


def test_seeded_clear_value_and_active_effect_hold_their_invariants():
    rng = random.Random(SEED + 2)
    for case in range(CASES):
        names = [n for n in _junk_list(rng) if isinstance(n, str)]
        where = f"seed={SEED + 2} case={case} names={names!r}"
        out = le.clear_value(names)
        assert out is None or (out in names and le.normalize(out) in le.CLEAR_ALIASES), where
        current = rng.choice(names) if names and rng.random() < 0.7 else _junk(rng)
        state = {"state": rng.choice(["on", "off", "unavailable"]),
                 "attributes": {"supported_features": 4, "effect": current, "effect_list": names}}
        active = le.active_effect(state)
        assert active is None or (state["state"] == "on" and active == current and 1 <= len(active) <= 64
                                  and le.normalize(active) not in le.CLEAR_ALIASES), where


def test_seeded_recognise_never_raises_and_returns_a_consistent_request(house):
    _, _, index, ids = house
    rng = random.Random(SEED + 3)
    words = ["set", "put", "make", "turn", "off", "on", "the", "study", "prism", "to", "into", "effect", "effects", "mode", "stop",
             "candlelight", "twinkle", "fire", "2012", "living", "room", "in", "for", "no", "more", "please", "a", "hexagon", "zzz", "50"]
    for case in range(CASES):
        text = " ".join(rng.choice(words) for _ in range(rng.randint(1, 12)))
        if rng.random() < 0.15:
            text = _junk(rng, 120)
        where = f"seed={SEED + 3} case={case} text={text!r}"
        for idx, id_list in ((index, ids), (None, None)):
            request = read(text, idx, id_list)
            if request is None:
                continue
            toks = tokens(text)
            assert 0 <= request.span[0] < request.span[1] <= len(toks), where
            assert le.has_effect_shape(text), where
            assert not request.explicit or any(w in toks for w in ("effect", "effects")), where
            assert not request.mode_word or "mode" in toks, where
            assert len(request.phrase.split()) <= 6 and len(request.phrase) <= 48, where


def _random_json(rng, depth=0):
    kind = rng.random()
    if depth > 3 or kind < 0.3:
        return rng.choice(["Fire", "light.example_lamp", "all", "{{ x }}", 5, None, True, "", _junk(rng, 12)])
    if kind < 0.6:
        return [_random_json(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    keys = ["effect", "entity_id", "area_id", "data", "target", "x", "device_id"]
    return {rng.choice(keys): _random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def test_seeded_effect_problem_only_clears_a_clean_literal_call():
    rng = random.Random(SEED + 4)
    cleared = 0
    for case in range(CASES * 2):
        service = rng.choice(["light.turn_on", "light.turn_on", "light.turn_off", "{{ s }}", None])
        if case % 5 == 0:
            service = "light.turn_on"
        data = rng.choice([{"effect": _random_json(rng)}, {"effect": rng.choice(WLED_NAMES), "entity_id": rng.choice(OK_TARGETS)},
                           _random_json(rng)])
        targets = rng.choice([None, rng.choice(OK_TARGETS), _random_json(rng)])
        where = f"seed={SEED + 4} case={case} {service!r} {data!r} {targets!r}"
        problem = le.effect_problem(service, data, targets)
        assert problem is None or problem in (le.PROBLEM_CARRIER, le.PROBLEM_SERVICE, le.PROBLEM_VALUE, le.PROBLEM_TARGET), where
        if problem is None and (le._count_effect_keys(data) or le._count_effect_keys(targets)):
            cleared += 1
            assert service == "light.turn_on" and isinstance(data, dict) and _clean_member(data["effect"]), where
            assert le.effect_target_ids(data.get("entity_id"), targets) is not None, where
    assert cleared > 20, "the loop never exercised an allowed call"


def test_seeded_effect_carriers_never_raise_and_flag_every_effect_key():
    rng = random.Random(SEED + 5)
    for case in range(CASES):
        actions = [_random_json(rng) for _ in range(rng.randint(0, 3))]
        where = f"seed={SEED + 5} case={case} actions={actions!r}"
        found = le.effect_carriers(actions, _walk)
        assert all(isinstance(path, tuple) for path, _ in found), where
        assert len(found) >= le._count_effect_keys(actions), where


def test_seeded_spoken_name_is_short_and_a_string():
    rng = random.Random(SEED + 6)
    for case in range(CASES):
        source = rng.choice([_junk(rng, 200), rng.choice(WLED_NAMES), None, 5])
        out = le.spoken_name(source)
        assert isinstance(out, str) and len(out) <= le.MAX_SPOKEN_NAME_CHARS, f"seed={SEED + 6} case={case} {source!r}"

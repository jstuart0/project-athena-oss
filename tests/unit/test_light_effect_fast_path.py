"""Light effects: the controller's fast path (plan Phase 3).

A spoken effect request becomes an intent with no model call, from the entity
manager's cached states alone. The shape gate runs before the index, a clear is
``clear_effect`` (never a turn off), and everything an existing path owns keeps
its answer. Rooms and lights are fictional; effect names come from the contract
fixtures through the real ``HAEntityManager.refresh_entities()``.
"""
from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "orchestrator"))

from . import _public_audience_harness as h  # noqa: E402
from . import test_light_effects as fx  # noqa: E402
from ._light_effect_corpus import NEGATIVES, OTHER_NEGATIVES, PARITY_CORPUS, POSITIVES, T4_NEGATIVES  # noqa: E402
from orchestrator import access_target, light_effects, mode_permission as mp  # noqa: E402
from orchestrator.smart_home_controller import (  # noqa: E402
    AMBIENT_COLOR_COMMANDS, BASIC_COLORS, LIGHT_STATUS_PATTERNS, SCENE_PHRASES, TURN_ON_PHRASES,
    WHOLE_HOUSE_LIGHT_PATTERNS, SmartHomeController,
)
from orchestrator.utterance_kind import classify_utterance  # noqa: E402
from shared import household  # noqa: E402

EFFECT_ACTIONS = access_target.EFFECT_ACTIONS


def _light(entity_id, name, base="govee_local", **attrs):
    state = copy.deepcopy(fx.load(base))
    state["entity_id"] = entity_id
    state["attributes"]["friendly_name"] = name
    state["attributes"].update(attrs)
    return state


def _manager(*extra, bases=("govee_local", "wled")):
    return fx.manager_for(*bases, extra=[_light("light.study_prism", "Study Prism"), *extra])


class FailingLLM:
    def __getattr__(self, name):
        async def _no(*a, **k):
            raise AssertionError(f"the model was called: {name}")
        return _no


@pytest.fixture(autouse=True)
def _runtime():
    h.reset_runtime()
    household.install_for_test()
    yield
    h.reset_runtime()


def controller(manager):
    return SmartHomeController(entity_manager=manager, llm_router=FailingLLM())


def fast(ctrl, query, utterance="__classify__"):
    verdict = classify_utterance(query) if utterance == "__classify__" else utterance
    lowered = query.lower()
    return asyncio.run(ctrl._fast_path_intent(
        query, lowered, None, "turn on" in lowered, "turn off" in lowered, False, verdict))


@pytest.fixture(scope="module")
def full():
    h.reset_runtime()
    household.install_for_test()
    return controller(_manager())


@pytest.fixture(scope="module")
def empty():
    h.reset_runtime()
    household.install_for_test()
    return controller(fx.manager_for("no_effects"))


def is_effect(intent):
    return bool(intent) and intent.get("action") in EFFECT_ACTIONS


# -- parity with the shared corpus ---------------------------------------------------

def test_the_corpus_population_and_named_members():
    assert len(PARITY_CORPUS) >= 30
    assert ("set the study prism to candlelight", True) in PARITY_CORPUS and ("turn off guest mode", False) in PARITY_CORPUS
    assert len(POSITIVES) + len(NEGATIVES) == len(PARITY_CORPUS) and len(T4_NEGATIVES) >= 9


@pytest.mark.parametrize("query,expected", PARITY_CORPUS, ids=[q for q, _ in PARITY_CORPUS])
def test_the_fast_path_takes_a_query_exactly_when_the_corpus_says_it_is_an_effect_request(full, query, expected):
    h.reset_runtime()
    household.install_for_test()
    assert is_effect(fast(full, query)) is expected


@pytest.mark.parametrize("query", POSITIVES)
def test_the_intent_carries_the_action_and_a_phrase_the_words_back(full, query):
    intent = fast(full, query)
    assert intent["device_type"] == "light" and intent["room"] is None and intent["color_description"] is None
    clears = intent["parameters"]["clear"]
    assert intent["action"] == ("clear_effect" if clears else "set_effect")
    assert access_target.write_cue_problem("light", intent["action"], query, intent["parameters"]) is None, intent
    assert set(intent) == {"device_type", "room", "action", "target_scope", "parameters", "color_description"}


# -- clears are clears ------------------------------------------------------------

CLEARS = ["turn off the effect", "turn off the effects", "turn the effect off", "stop the effect", "cancel the effects",
          "end the light effect", "remove the effect", "disable the effects", "kill the effect", "effects off", "no more effects",
          "turn off the candlelight effect", "candlelight mode off", "stop the twinkle on the study prism"]


@pytest.mark.parametrize("query", CLEARS)
def test_clear_variants_never_reach_turn_off(full, query):
    intent = fast(full, query)
    assert intent is not None and intent["action"] == "clear_effect" and intent["parameters"]["clear"] is True, query
    assert intent["action"] not in ("turn_off", "turn_on")


def test_turn_off_the_effect_clears_and_never_turns_lights_off(full):
    assert fast(full, "turn off the effect")["action"] == "clear_effect"
    assert fast(full, "turn off the lights")["action"] == "turn_off", "an ordinary turn off keeps its meaning"


# -- everything else keeps its answer ----------------------------------------------

def test_t4_negatives_keep_their_existing_intent(full, empty):
    """With the whole vocabulary loaded or none, a negative gets the same answer."""
    for query in T4_NEGATIVES:
        assert fast(full, query) == fast(empty, query), query
        assert not is_effect(fast(full, query)), query


def _table_phrases():
    out = []
    for source in (SCENE_PHRASES, AMBIENT_COLOR_COMMANDS, BASIC_COLORS, [p for p, _ in WHOLE_HOUSE_LIGHT_PATTERNS],
                   LIGHT_STATUS_PATTERNS, TURN_ON_PHRASES, access_target.TURN_OFF_PHRASES, access_target.IMPLICIT_BRIGHTER,
                   access_target.IMPLICIT_DIMMER, access_target.BRIGHTER_PATTERNS, access_target.DIMMER_PATTERNS):
        out.extend(source)
    return list(dict.fromkeys(out))


def test_existing_command_tables_are_unchanged_by_a_full_vocabulary(full, empty):
    phrases = _table_phrases()
    queries = []
    for phrase in phrases:
        queries += [phrase, f"turn on {phrase}", f"{phrase} please", f"set the study to {phrase}", f"make the study {phrase}",
                    f"{phrase} mode", f"put the study on {phrase}"]
    queries = list(dict.fromkeys(queries + ["romantic mode", "set the study to romantic mode", "romantic mode on the study prism"]))
    assert len(queries) >= 150 and "romantic mode" in queries
    differing = [q for q in queries if fast(full, q) != fast(empty, q)]
    assert differing == [], differing[:10]


@pytest.mark.parametrize("word", ["Guest", "Away", "Sleep", "Game", "Home", "Night", "Party", "Quiet"])
def test_house_modes_stay_off_the_effect_path_even_when_a_light_has_that_effect(word):
    """D19."""
    manager = fx.manager_for("no_effects", extra=[_light("light.study_prism", "Study Prism", effect_list=["none", word, "Fire"])])
    ctrl = controller(manager)
    lower = word.lower()
    for query in (f"turn off {lower} mode", f"{lower} mode off", f"set the study prism to {lower} mode"):
        assert not is_effect(fast(ctrl, query)), query
    assert is_effect(fast(ctrl, f"set the study prism to the {lower} effect")), "the word effect still asks for it"


# -- tails and targets (F79/F83) --------------------------------------------------------

@pytest.mark.parametrize("query", [
    "turn off the effects on the tv", "turn off the effects on the guest", "turn off the effects for the neighbours",
    "stop the twinkle on the tv", "candlelight mode on the toaster", "turn off the effect in the cupboard",
])
def test_a_tail_that_names_nothing_is_no_request(full, query):
    assert not is_effect(fast(full, query)), query


def test_a_tail_naming_a_room_or_a_light_is_a_request(full):
    for query in ("turn off the effects in the study", "turn off the effects on the guest room", "stop the twinkle on the study prism"):
        assert is_effect(fast(full, query)), query


def test_a_caller_who_cannot_use_the_named_light_gets_no_effect_request():
    ctrl = controller(_manager())
    guest = mp.apply_guest_baseline({"mode": "guest", "restricted_entities": [r"^light\."]})
    for query in ("set the study prism to candlelight", "stop the twinkle on the study prism", "make the study twinkle"):
        with mp.ha_permission_scope(guest, mode="guest", utterance=classify_utterance(query)):
            assert not is_effect(fast(ctrl, query)), query
        assert is_effect(fast(ctrl, query)), query


# -- questions ----------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "is the candlelight effect on the study prism", "is the twinkle effect on the study prism", "is the effect on the study prism",
    "what effect is on the study prism",
])
def test_a_state_question_is_never_an_effect_request(full, query):
    assert not is_effect(fast(full, query))


# -- no model, no I/O, shape first --------------------------------------------------------

def test_the_reported_shape_sets_the_effect_with_no_model_call():
    ctrl = controller(_manager())
    query = "set the study prism to candlelight"
    intent = asyncio.run(ctrl.extract_intent(query, device_room=None))
    assert intent["action"] == "set_effect" and intent["parameters"]["effect"] == "candlelight"
    assert intent["device_type"] == "light"


def test_the_fast_path_reads_cached_states_only(monkeypatch):
    manager = _manager()
    for name in ("get_entities", "get_entity", "refresh_entities", "find_lights_by_room"):
        monkeypatch.setattr(manager, name, AsyncMock(side_effect=AssertionError(f"I/O: {name}")))
    manager._ha = MagicMock(side_effect=AssertionError("ha client touched"))
    ctrl = controller(manager)
    assert is_effect(fast(ctrl, "set the study prism to candlelight"))
    assert is_effect(fast(ctrl, "turn off the effect"))


def test_a_cold_cache_judges_only_what_needs_no_vocabulary():
    manager = _manager()
    manager._entities_cache = None
    ctrl = controller(manager)
    assert ctrl.entity_manager.cached_entities() is None
    assert fast(ctrl, "turn off the effect")["action"] == "clear_effect"
    assert not is_effect(fast(ctrl, "set the study prism to candlelight"))
    assert not is_effect(fast(ctrl, "make the study twinkle"))


@pytest.fixture
def index_builds(monkeypatch):
    count = {"n": 0}
    original = light_effects.EffectIndex.__init__

    def counting(self, states):
        count["n"] += 1
        original(self, states)

    monkeypatch.setattr(light_effects.EffectIndex, "__init__", counting)
    light_effects.EffectIndex._memo = None
    return count


NON_EFFECT_VERBS = ("turn on", "turn off", "dim", "brighten", "open", "close", "lock", "unlock", "what is", "play", "pause", "how many")
NON_EFFECT_OBJECTS = ("the kitchen light", "the study", "the living room lights", "the garage door", "the front lock", "the tv",
                      "the hallway fan", "bedroom 2", "the thermostat", "the study prism", "the porch light", "the music",
                      "the blinds", "the weather", "the time", "the stove", "the oven", "a timer", "the guest room", "the fan")


def test_commands_with_no_effect_shape_build_no_index_at_the_real_call_site(full, index_builds):
    corpus = [f"{verb} {obj}" for verb in NON_EFFECT_VERBS for obj in NON_EFFECT_OBJECTS]
    assert len(corpus) >= 200 and not any(light_effects.has_effect_shape(q) for q in corpus)
    for query in corpus:
        fast(full, query)
    assert index_builds["n"] == 0


def test_shape_passing_commands_build_the_index_once_per_generation(index_builds):
    ctrl = controller(_manager())
    corpus = [f"set the {room} to {colour}" for room in ("study", "kitchen", "hallway", "living room", "bedroom 2", "guest room")
              for colour in ("blue", "red", "green", "warm white", "purple", "orange", "pink", "yellow", "cyan", "teal")]
    assert len(corpus) >= 50 and all(light_effects.has_effect_shape(q) for q in corpus)
    for query in corpus:
        assert not is_effect(fast(ctrl, query)), query
    assert index_builds["n"] == 1
    ctrl.entity_manager._generation += 1
    fast(ctrl, corpus[0])
    assert index_builds["n"] == 2


def test_an_in_place_state_patch_is_seen_by_the_next_request():
    manager = _manager()
    ctrl = controller(manager)
    assert not is_effect(fast(ctrl, "set the study prism to zebra drift"))
    manager._entities_cache["light.study_prism"]["attributes"]["effect_list"] = ["none", "zebra drift"]
    manager._generation += 1
    assert is_effect(fast(ctrl, "set the study prism to zebra drift"))


# -- structure ----------------------------------------------------------------------------

def test_route_control_opens_the_scope_before_extract_intent():
    """The permitted lights come from the scope, so the scope must exist first."""
    source = (Path(__file__).resolve().parents[2] / "src" / "orchestrator" / "nodes" / "route_control.py").read_text()
    assert source.index("ha_permission_scope(") < source.index("smart_controller.extract_intent(")


def test_the_effect_branch_sits_before_the_tv_branch_and_after_the_window_branch():
    import inspect
    text = inspect.getsource(SmartHomeController._fast_path_intent)
    assert text.index("window sensor query") < text.index("self._effect_fast_intent(") < text.index("FAST PATH: TV/Media Player")


def test_the_fast_path_branch_has_no_await_and_no_model_call():
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(SmartHomeController._effect_fast_intent)))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Await, ast.AsyncFunctionDef))]
    text = inspect.getsource(SmartHomeController._effect_fast_intent)
    assert "llm_router" not in text and "get_entities" not in text and "get_state" not in text


@pytest.mark.parametrize("spaced", ["o f f", "n o n e", "s o l i d"])
def test_a_spaced_clear_alias_said_as_a_set_is_no_effect_request(full, spaced):
    """A set that is a clear in disguise would skip the clear rules."""
    assert not is_effect(fast(full, f"set the study prism to {spaced}"))


def test_a_fast_path_failure_falls_back_instead_of_failing_the_turn(monkeypatch):
    """A manager that returns nonsense (or a recogniser that raises) leaves the turn to the ordinary path."""
    ctrl = controller(MagicMock())
    assert fast(ctrl, "set the study prism to candlelight") is None
    ctrl = controller(_manager())
    monkeypatch.setattr(light_effects, "recognise", MagicMock(side_effect=RuntimeError("boom")))
    assert fast(ctrl, "set the study prism to candlelight") is None

"""Light effects: the deterministic route in ``classify_node`` (plan Phase 4).

A spoken effect request is classified CONTROL with no intent-cache read or
write and no model call, from the entity manager's cached states alone. Rooms
and lights are fictional; effect names come from the contract fixtures in
tests/fixtures/light_effects, entering through the real
``HAEntityManager.refresh_entities()``.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

# classify_node does `from semantic_cache import ...` (same-directory import).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "orchestrator"))

from . import _public_audience_harness as h  # noqa: E402
from . import test_light_effects as fx  # noqa: E402
from ._fast_path_harness import RaisingLLM  # noqa: E402
from shared import assistant_profile, household  # noqa: E402
from orchestrator import light_effects, mode_permission, room_context  # noqa: E402
from orchestrator.ha_entity_manager import HAEntityManager  # noqa: E402
from orchestrator.nodes import _runtime, route_control  # noqa: E402
from orchestrator.room_directory import current_directory  # noqa: E402
from orchestrator.search_providers.intent_classifier import IntentClassifier  # noqa: E402
from orchestrator.smart_home_controller import effect_word_claimed  # noqa: E402
from orchestrator.state import IntentCategory, OrchestratorState  # noqa: E402
from orchestrator.utterance_kind import UtteranceKind, classify_utterance  # noqa: E402

main = h.main
SENTINEL = {"intent": "general_info", "confidence": 0.5, "entities": {}, "complexity": "simple"}
BUDGET_MS = 3.0

# The utterances that must classify CONTROL through the effect route.
POSITIVES = [
    "set the study prism to candlelight",
    "set the study to candlelight",
    "make the study twinkle",
    "turn off the effect",
    "turn off the candlelight effect",
    "turn the effect off",
    "candlelight mode off",
    "candlelight mode on the study prism",
    "stop the twinkle on the study prism",
    "play the candle effect on the study prism",
    "put the study on fade",
    "set the study to fire 2012",
    "turn off the effects in the study",
    "turn off the effects on the guest room",
    "no more effects",
    "sunset effect on the study prism",
    "can you turn off the effect",
    "set bedroom 2 to candlelight",
]

# The F67 T4 negatives (nothing here is an effect request, with the full
# vocabulary loaded) and the other commands the effect grammar must not take.
T4_NEGATIVES = [
    "turn off guest mode",
    "turn off away mode",
    "turn off sleep mode",
    "game mode off",
    "turn off the sound effects",
    "stop the stream",
    "stop the rain",
    "stop the washing machine",
    "stop the noise",
]
OTHER_NEGATIVES = [
    "turn on the sound effect",
    "play the rain sound effect in the living room",
    "romantic mode",
    "night mode on the study prism",
    "set the study lights to 50 percent",
    "turn on the kitchen light",
    "fade the lights",
    "set the study to blue",
    "turn off the effects on the tv",
    "turn off the effects on the guest",
    "turn off the effects for the neighbours",
    "is the candlelight effect on the study prism",
    "what effect is on the study prism",
]
NEGATIVES = T4_NEGATIVES + OTHER_NEGATIVES

PARITY_CORPUS = [(q, True) for q in POSITIVES] + [(q, False) for q in NEGATIVES]


def _light(entity_id, name, base="govee_local"):
    state = copy.deepcopy(fx.load(base))
    state["entity_id"] = entity_id
    state["attributes"]["friendly_name"] = name
    return state


def _house(*, wled_lights=0):
    extra = [_light("light.study_prism", "Study Prism")]
    extra += [_light(f"light.living_room_strip_{i}", f"Living Room Strip {i}", "wled") for i in range(wled_lights)]
    return fx.manager_for("govee_local", "wled", extra=extra)


class _Rig:
    """classify_node over a real entity manager, with the config and model I/O
    the non-effect paths reach stubbed, and a guard that records (and fails on)
    any network call."""

    def __init__(self, monkeypatch, manager, cached):
        h.reset_runtime()
        household.install_for_test()
        self.cache = SimpleNamespace(get=AsyncMock(return_value=cached), set=AsyncMock())
        self.llm = RaisingLLM()
        self.network = []
        _runtime.set_cache_client(self.cache)
        _runtime.set_llm_router(self.llm)
        _runtime.set_intent_classifier(IntentClassifier())
        if manager is not None:
            _runtime.set_entity_manager(manager)
        monkeypatch.setattr(main, "get_conversation_context", AsyncMock(return_value=None))
        monkeypatch.setattr(main, "get_feature_config", AsyncMock(return_value={"enabled": False, "config": {}}))
        monkeypatch.setattr(main, "get_component_config", AsyncMock(return_value={"model_name": "stub", "system_prompt": ""}))
        monkeypatch.setattr(main, "_component_system_prompt", lambda config: "")

        def reached(kind):
            async def _send(client, request, *a, **k):
                self.network.append((kind, str(request.url.host)))
                raise AssertionError(f"network reached: {kind} {request.url.host}")
            return _send

        monkeypatch.setattr(httpx.AsyncClient, "send", reached("async"))
        monkeypatch.setattr(httpx.Client, "send", lambda client, request, *a, **k: reached("sync")(client, request))
        self.manager = manager

    def classify(self, query):
        state = OrchestratorState(query=query)
        state.session_id = "sess-1"
        state.mode = "owner"
        state.node_timings = {}
        return asyncio.run(main.classify_node(state))


@pytest.fixture(autouse=True)
def _reset_runtime_after():
    yield
    _runtime.reset_for_test()


@pytest.fixture
def rig(monkeypatch):
    made = _Rig(monkeypatch, _house(), SENTINEL)
    yield made
    assert made.network == [], "a test reached the network"


def _took_the_effect_route(rig, query):
    """CONTROL with no intent-cache read or write and no model call: what the
    route does and what no other path in the node does for these queries."""
    rig.cache.get.reset_mock()
    rig.cache.set.reset_mock()
    rig.llm.calls.clear()
    state = rig.classify(query)
    return (
        state.intent == IntentCategory.CONTROL
        and rig.cache.get.await_count == 0
        and rig.cache.set.await_count == 0
        and rig.llm.calls == []
    )


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def test_classify_routes_effect_requests_to_control_without_the_llm(rig):
    assert len(POSITIVES) >= 15 and "set the study prism to candlelight" in POSITIVES
    for query in POSITIVES:
        rig.cache.get.reset_mock()
        state = rig.classify(query)
        assert state.intent == IntentCategory.CONTROL, query
        assert state.confidence == 0.9, query
        assert state.complexity in ("simple", "complex", "super_complex"), query
        assert rig.cache.get.await_count == 0 and rig.cache.set.await_count == 0, query
        assert rig.llm.calls == [], query
        assert state.node_timings.get("classify") is not None, query


def test_the_route_sets_the_complexity_the_other_control_routes_use(rig):
    query = "set the study prism to candlelight"
    state = rig.classify(query)
    from orchestrator.complexity_detector import determine_complexity

    assert state.complexity == determine_complexity(query, "control", log_features=False)


def test_a_stale_cached_general_info_does_not_suppress_the_effect_route(rig):
    stale = {"intent": "general_info", "confidence": 0.99, "entities": {}, "complexity": "simple"}
    rig.cache.get.return_value = stale
    for query in ("set the study prism to candlelight", "turn off the effect"):
        state = rig.classify(query)
        assert state.intent == IntentCategory.CONTROL, query
    assert rig.cache.get.await_count == 0
    assert rig.cache.set.await_count == 0


def test_the_route_never_writes_the_intent_cache(rig):
    rig.cache.get.return_value = None
    for query in POSITIVES:
        rig.classify(query)
    rig.cache.set.assert_not_awaited()
    rig.cache.get.assert_not_awaited()


@pytest.mark.parametrize("query", NEGATIVES)
def test_negatives_do_not_take_the_effect_route(rig, query):
    assert not _took_the_effect_route(rig, query), query


def test_t4_negatives_do_not_take_the_effect_route(rig):
    assert len(T4_NEGATIVES) == 9 and "turn off guest mode" in T4_NEGATIVES and "stop the washing machine" in T4_NEGATIVES
    assert light_effects.has_effect_shape("turn off guest mode")
    for query in T4_NEGATIVES:
        assert not _took_the_effect_route(rig, query), query


def test_a_tail_that_names_neither_a_room_nor_a_light_is_no_request(rig):
    for query in ("turn off the effects on the tv", "turn off the effects on the guest", "stop the effect for the neighbours"):
        assert not _took_the_effect_route(rig, query), query
    assert _took_the_effect_route(rig, "turn off the effects on the guest room")
    assert _took_the_effect_route(rig, "turn off the effects on the study prism")


def test_play_the_candle_effect_on_the_prism_is_control_not_music(rig):
    state = rig.classify("play the candle effect on the study prism")
    assert state.intent == IntentCategory.CONTROL
    assert rig.cache.get.await_count == 0


def test_non_effect_queries_keep_their_routing(rig, monkeypatch):
    """Whatever classify decided for a query before this route is what it decides
    with the route in place, including the paths below the cache read."""
    queries = [
        "change the office to white",
        "turn on the kitchen light at 8pm",
        "set the study to blue",
        "turn on the kitchen light",
        "what is the weather",
        "turn off guest mode",
        "stop the rain",
    ]
    assert len(queries) >= 7 and "change the office to white" in queries

    real_route = main._light_effect_request

    def run(route_on):
        rig.cache.get.return_value = None
        monkeypatch.setattr(main, "_light_effect_request", real_route if route_on else (lambda q: None))
        results = {}
        for query in queries:
            rig.cache.get.reset_mock()
            try:
                state = rig.classify(query)
            except AssertionError:
                results[query] = "reached the model"
                continue
            results[query] = (state.intent, state.confidence, state.complexity, state.entities, rig.cache.get.await_count)
        monkeypatch.setattr(main, "_light_effect_request", real_route)
        return results

    with_route = run(True)
    without_route = run(False)
    assert with_route == without_route
    assert with_route["change the office to white"][0] == IntentCategory.CONTROL


def test_classify_takes_the_effect_route_exactly_when_the_helper_finds_a_request(rig):
    """classify's decision is the helper's, over a corpus whose expected
    answers are written out. This is not fast-path parity: Phase 3 adds that
    comparison (``_fast_path_intent`` over PARITY_CORPUS) when the branch exists."""
    assert len(PARITY_CORPUS) >= 30 and ("set the study prism to candlelight", True) in PARITY_CORPUS
    for query, expected in PARITY_CORPUS:
        assert (main._light_effect_request(query) is not None) is expected, f"corpus expectation drifted for {query!r}"
        assert _took_the_effect_route(rig, query) is expected, query


QUESTIONS_THE_GRAMMAR_ALONE_WOULD_TAKE = (
    "is the candlelight effect on the study prism",
    "is the twinkle effect on the study prism",
    "is the sunset effect on the study prism",
    "is the effect on the study prism",
)


@pytest.mark.parametrize("query", QUESTIONS_THE_GRAMMAR_ALONE_WOULD_TAKE)
def test_a_state_question_is_never_an_effect_request(rig, query):
    """The recogniser reads these as requests; the utterance classifier's verdict
    that they ask about state is what keeps them off the route."""
    states = rig.manager.cached_entities()
    index = light_effects.EffectIndex.for_states(states, rig.manager.cache_generation())
    raw = light_effects.recognise(
        query, index, None, claimed=effect_word_claimed, names_target=lambda w: True, names_light=lambda w: True,
    )
    assert raw is not None and classify_utterance(query).kind == UtteranceKind.STATE_QUESTION
    assert not _took_the_effect_route(rig, query)


HOUSE_MODE_EFFECTS = ["none", "Guest", "Away", "Sleep", "Game"]


@pytest.mark.xfail(strict=True, reason="D19 lands in 2c")
def test_house_mode_words_stay_off_the_route_even_when_a_light_has_an_effect_of_that_name(monkeypatch):
    states = [_light("light.study_prism", "Study Prism")]
    states[0]["attributes"]["effect_list"] = HOUSE_MODE_EFFECTS
    modes = _Rig(monkeypatch, fx.manager_for("no_effects", extra=states), SENTINEL)
    index = light_effects.EffectIndex.for_states(modes.manager.cached_entities(), modes.manager.cache_generation())
    assert {"guest", "away", "sleep", "game"} <= index.vocab()
    for query in ("turn off guest mode", "turn off away mode", "turn off sleep mode", "game mode off"):
        assert not _took_the_effect_route(modes, query), query


# ---------------------------------------------------------------------------
# Cold cache, no manager, a failing read
# ---------------------------------------------------------------------------


def _cold_manager():
    return HAEntityManager(fx.FakeHA([]))


def test_cold_cache_routes_only_bare_clear_and_explicit_effect_shapes(monkeypatch):
    cold = _Rig(monkeypatch, _cold_manager(), SENTINEL)
    assert cold.manager.cached_entities() is None
    for query in ("turn off the effect", "turn the effect off", "no more effects", "turn off the effects in the study", "can you turn off the effect"):
        assert _took_the_effect_route(cold, query), query
    for query in (
        "set the study prism to candlelight",
        "make the study twinkle",
        "candlelight mode off",
        "put the study on fade",
        "turn off the candlelight effect",
        "play the candle effect on the study prism",
        "stop the twinkle on the study prism",
    ):
        assert not _took_the_effect_route(cold, query), query
    for query in T4_NEGATIVES:
        assert not _took_the_effect_route(cold, query), query


def test_lights_that_take_no_effects_still_route_an_explicit_effect_on_a_named_light(monkeypatch):
    plain = fx.manager_for("no_effects", extra=[_light("light.study_prism", "Study Prism", "no_effects")])
    warm = _Rig(monkeypatch, plain, SENTINEL)
    assert light_effects.EffectIndex.for_states(plain.cached_entities(), plain.cache_generation()).vocab() == frozenset()
    assert _took_the_effect_route(warm, "play the candle effect on the study prism")
    assert _took_the_effect_route(warm, "turn off the effect")
    assert not _took_the_effect_route(warm, "set the study prism to candlelight")
    assert not _took_the_effect_route(warm, "make the study twinkle")


def test_with_no_entity_manager_a_bare_clear_still_routes(monkeypatch):
    nobody = _Rig(monkeypatch, None, SENTINEL)
    assert _runtime.get_entity_manager() is None
    assert _took_the_effect_route(nobody, "turn off the effect")
    assert not _took_the_effect_route(nobody, "set the study prism to candlelight")


def test_a_failure_in_the_route_falls_back_to_ordinary_classification(monkeypatch):
    class Broken:
        def cached_entities(self):
            raise RuntimeError("boom: light.study_prism candlelight")

        def cache_generation(self):
            return 0

    broken = _Rig(monkeypatch, Broken(), SENTINEL)
    records = _record_logger(monkeypatch, main)
    assert not _took_the_effect_route(broken, "set the study prism to candlelight")
    failed = [r for r in records if r[1] == "light_effect_route_failed"]
    assert failed and failed[0][2] == {"error": "RuntimeError"}


def test_an_in_place_patch_rebuilds_the_classify_index(monkeypatch):
    manager = _house()
    patched = _Rig(monkeypatch, manager, SENTINEL)
    states = manager.cached_entities()
    query = "set the study prism to zebra drift"
    assert not _took_the_effect_route(patched, query)
    changed = copy.deepcopy(fx.load("govee_local"))
    changed["entity_id"] = "light.study_prism"
    changed["attributes"]["friendly_name"] = "Study Prism"
    changed["attributes"]["effect_list"] = ["none", "zebra drift"]
    manager._ha.states["light.study_prism"] = changed
    manager.invalidate(["light.study_prism"])
    asyncio.run(manager.get_entities())
    assert manager.cached_entities() is states, "patched in place, not replaced"
    assert _took_the_effect_route(patched, query)


# ---------------------------------------------------------------------------
# Permission and logging (F80, Q4)
# ---------------------------------------------------------------------------


class Tripwire:
    """Records every attribute use and fails the test on it."""

    def __init__(self):
        self.used = []

    def __getattr__(self, name):
        self.used.append(name)
        raise AssertionError(f"touched {name}")

    def __bool__(self):
        return True


def _control_state(query, permissions):
    state = OrchestratorState(query=query)
    state.session_id = "sess-1"
    state.mode = permissions.get("mode", "owner")
    state.permissions = permissions
    state.intent = IntentCategory.CONTROL
    state.node_timings = {}
    return state


DENIED = {"mode": "guest", "restricted_intents": ["control"]}


def _record_logger(monkeypatch, module):
    records = []
    monkeypatch.setattr(module, "logger", SimpleNamespace(**{
        lvl: (lambda msg=None, *a, _l=lvl, **kw: records.append((_l, msg, kw)))
        for lvl in ("debug", "info", "warning", "error", "exception")
    }))
    return records


def test_a_caller_without_control_permission_gets_the_existing_refusal_with_no_reads_or_writes(rig, monkeypatch):
    query = "set the study prism to candlelight"
    classified = rig.classify(query)
    assert classified.intent == IntentCategory.CONTROL, "classify has no per-caller identity; the gate is route_control"
    assert mode_permission.check_intent_permission(IntentCategory.CONTROL, DENIED) is False
    rig.cache.get.reset_mock()
    rig.cache.set.reset_mock()
    rig.llm.calls.clear()
    records = _record_logger(monkeypatch, route_control)

    trip = {"ha": Tripwire(), "em": Tripwire(), "sc": Tripwire(), "se": Tripwire(), "aa": Tripwire()}
    _runtime.set_ha_client(trip["ha"])
    _runtime.set_entity_manager(trip["em"])
    _runtime.set_smart_controller(trip["sc"])
    _runtime.set_sequence_executor(trip["se"])
    _runtime.set_automation_agent(trip["aa"])
    state = asyncio.run(route_control.route_control_node(_control_state(query, DENIED)))
    assert state.answer == mode_permission.intent_refusal_message(DENIED)
    assert state.error == "permission_denied"
    assert all(t.used == [] for t in trip.values()), {k: t.used for k, t in trip.items()}
    rig.cache.get.assert_not_awaited()
    rig.cache.set.assert_not_awaited()
    assert rig.llm.calls == []
    assert [r[1] for r in records] == ["control_request_denied"]
    emitted = json.dumps(records, default=str).lower()
    assert "candlelight" not in emitted and "study" not in emitted and "light." not in emitted


def test_the_refusal_check_above_can_see_a_read(rig, monkeypatch):
    """Positive control: the same rig, with the owner's permissions, does touch
    the controller, so the zero-use assertion above can fail."""
    trip = {"ha": Tripwire(), "em": Tripwire(), "sc": Tripwire()}
    _runtime.set_ha_client(trip["ha"])
    _runtime.set_entity_manager(trip["em"])
    _runtime.set_smart_controller(trip["sc"])
    monkeypatch.setattr(route_control, "get_feature_config", AsyncMock(return_value={"enabled": False, "config": {}}))
    monkeypatch.setattr(route_control, "get_automation_system_mode", AsyncMock(return_value="off"))
    monkeypatch.setattr(assistant_profile, "get_assistant_config", AsyncMock(return_value={}))
    try:
        asyncio.run(route_control.route_control_node(_control_state("set the study prism to candlelight", {"mode": "owner"})))
    except AssertionError:
        pass
    assert any(t.used for t in trip.values())


def test_the_routing_path_logs_no_names_or_ids(rig, monkeypatch, capsys):
    records = _record_logger(monkeypatch, main)
    states = rig.manager.cached_entities()
    index = light_effects.EffectIndex.for_states(states, rig.manager.cache_generation())
    secrets = {"candlelight", "study prism", "twinkle", "zebra drift", "light.study_prism", fx.GOVEE_ID, fx.WLED_ID}
    secrets |= {e.lower() for e in index.effects(fx.GOVEE_ID)[1:]} | {e.lower() for e in list(index.effects(fx.WLED_ID))[:40]}
    for query in ("set the study prism to candlelight", "turn off the candlelight effect", "stop the twinkle on the study prism"):
        rig.classify(query)
    routed = [r for r in records if r[1] == "light_effect_route"]
    assert len(routed) == 3, "positive control: the route logs once per request"
    assert all(r[2] == {"query_len": len(q), "clear": c} for r, q, c in zip(
        routed,
        ("set the study prism to candlelight", "turn off the candlelight effect", "stop the twinkle on the study prism"),
        (False, True, True),
    ))
    emitted = json.dumps(records, default=str).lower() + capsys.readouterr().out.lower()
    for secret in secrets:
        assert secret not in emitted, secret


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


NO_SHAPE_CORPUS = (
    [f"what is the weather in city number {i}" for i in range(120)]
    + [f"turn on the lamp number {i}" for i in range(60)]
    + ["play some music", "what time is it", "who won the game", "how tall is the tower", "tell me a story", "what's on my calendar"]
    + ["dim the lights", "turn on the kitchen light", "lights on"]
    + [f"remind me to call number {i}" for i in range(40)]
)


def test_no_shape_corpus_builds_no_index(rig, monkeypatch):
    assert len(NO_SHAPE_CORPUS) >= 200 and not any(light_effects.has_effect_shape(q) for q in NO_SHAPE_CORPUS)
    builds = []
    real = light_effects.EffectIndex.for_states.__func__
    monkeypatch.setattr(light_effects.EffectIndex, "for_states", classmethod(lambda cls, *a: builds.append(a) or real(cls, *a)))
    for query in NO_SHAPE_CORPUS:
        assert main._light_effect_request(query) is None
    assert builds == []
    assert main._light_effect_request("turn off the effect") is not None and len(builds) == 1


def test_classify_node_added_cost_within_budget(monkeypatch):
    extra = [_light("light.hexagon_panel", "Hexagon Panel")]
    extra += [_light(f"light.living_room_strip_{i}", f"Living Room Strip {i}", "wled") for i in range(60)]
    big = _Rig(monkeypatch, fx.manager_for("govee_local", "wled", extra=extra), SENTINEL)
    states = big.manager.cached_entities()
    lights = [e for e in states if e.startswith("light.")]
    assert sum(1 for e in states if e.startswith("light.living_room_strip_")) == 60 and len(lights) == 63
    builds = []
    real_init = light_effects.EffectIndex.__init__

    def counting(self, *a, **k):
        builds.append(1)
        real_init(self, *a, **k)

    monkeypatch.setattr(light_effects.EffectIndex, "__init__", counting)
    light_effects.EffectIndex._memo = None
    scans = []
    real_names = room_context.names_entity
    monkeypatch.setattr(room_context, "names_entity", lambda *a, **k: scans.append(1) or real_names(*a, **k))

    def timed(fn):
        t0 = time.perf_counter()
        fn()
        return (time.perf_counter() - t0) * 1000

    rows = (
        ("set the living room to candlelight", True, False),
        ("set the hexagon panel to candlelight", True, True),
        ("turn off the effects on the neighbours", False, True),
        ("set the study to blue", False, False),
    )
    for query, expect, scans_lights in rows:
        scans.clear()
        assert (main._light_effect_request(query) is not None) is expect, query
        assert len(scans) <= len(lights), "names_light passes over the lights at most once per request"
        if scans_lights:
            assert len(scans) >= 1, "this row must exercise the scan"
        helper = [timed(lambda: main._light_effect_request(query)) for _ in range(20)]
        assert statistics.median(helper) <= BUDGET_MS, (query, statistics.median(helper))
    assert len(builds) == 1, "one index build for the generation, however many queries"

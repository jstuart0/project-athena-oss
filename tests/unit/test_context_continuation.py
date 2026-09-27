"""Red/green contract for ATHENA-88 phase 5 (F16, D8/D13): conservative
short-turn context continuation and the single-writer context_ref_view for
downstream routing.

Plan: .mozart/plans/active/2026-09-26-deliver-athena-voice-intent-defects.md,
Phase 5. Test contract: same directory,
2026-09-26-deliver-athena-voice-intent-defects.test-contract.md, Phase 5
C1-C13 (r1/r2/r3 amendments).
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "src")
# main.py does `from semantic_cache import UNCACHEABLE_PATTERNS` (unqualified,
# same-directory import) inside classify_node -- needs src/orchestrator on
# the path directly, not just src.
sys.path.insert(0, os.path.join("src", "orchestrator"))

# Stub heavy/absent deps before any orchestrator import (same pattern as
# test_health_probes.py / test_openai_session_key.py). orchestrator.nodes
# must be imported before orchestrator.helpers -- see helpers.py's "Import
# contract" docstring.
for _mod in ("langgraph", "langgraph.graph", "prometheus_client"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()
os.environ.setdefault("SERVICE_API_KEY", "test-key-context-continuation")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

from shared.config import get_config as _shared_get_config  # noqa: E402

_config_loader_mock = mock.MagicMock()
_config_loader_mock.get_config = _shared_get_config
_config_loader_mock.ADMIN_API_URL = os.environ["ADMIN_API_URL"]
_config_loader_mock.get_feature_flag = mock.AsyncMock(return_value=False)
_config_loader_mock.get_feature_flags = mock.AsyncMock(return_value={})
_config_loader_mock.clear_cache = mock.AsyncMock()
sys.modules.setdefault("orchestrator.config_loader", _config_loader_mock)

import orchestrator.nodes  # noqa: E402,F401

import orchestrator.main as main_module  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.state import OrchestratorState, ConversationContext, IntentCategory  # noqa: E402
from orchestrator.context.detector import (  # noqa: E402
    detect_context_reference,
    decide_context_continuation,
    context_ref_view,
)
from orchestrator.search_providers.intent_classifier import IntentClassifier  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "src" / "orchestrator" / "main.py"

TURN2 = "what plae have happy our and outdoor seating?"


@pytest.fixture(autouse=True)
def _reset_runtime_between_tests():
    _runtime.reset_for_test()
    yield
    _runtime.reset_for_test()


def _make_fake_cache(sentinel_intent="general_info", sentinel_confidence=0.5):
    cache = SimpleNamespace()
    cache.get = AsyncMock(
        return_value={
            "intent": sentinel_intent,
            "confidence": sentinel_confidence,
            "entities": {},
            "complexity": "simple",
        }
    )
    return cache


def _install_classify_node_runtime(sentinel_intent="general_info", sentinel_confidence=0.5):
    fake_cache = _make_fake_cache(sentinel_intent, sentinel_confidence)
    _runtime.set_cache_client(fake_cache)
    _runtime.set_llm_router(MagicMock())
    _runtime.set_intent_classifier(IntentClassifier())
    return fake_cache


def _make_classify_state(query: str, session_id: str = "sess-1") -> OrchestratorState:
    state = OrchestratorState(query=query)
    state.session_id = session_id
    state.mode = "owner"
    state.node_timings = {}
    return state


# ---------------------------------------------------------------------------
# 1. test_anaphora_types_table
# ---------------------------------------------------------------------------

ANAPHORA_TABLE = [
    (TURN2, set()),  # named member: today's follow_up false positive from "and"
    ("and the kitchen?", {"follow_up", "room_only"}),
    ("what about tomorrow", {"follow_up"}),
    ("turn it off", {"pronoun"}),
    ("make them brighter", {"device_modifier", "pronoun"}),
    ("set it to level 2", {"incomplete_command", "pronoun"}),
    ("set the lights to 50 percent", {"incomplete_command"}),
    ("yes please", {"yes_no"}),
    ("do that again", {"repeat"}),
    ("turn up", {"incomplete_command"}),
    ("higher", {"incomplete_command"}),
    ("turn them too", {"follow_up", "pronoun"}),
    ("and the office", {"follow_up", "room_only"}),
    ("louder", {"device_modifier"}),
    ("warmer", {"device_modifier"}),
    ("the same one", {"pronoun"}),
    ("sure", {"yes_no"}),
    ("was it", {"inquiry", "pronoun"}),
]


def test_anaphora_table_population_floor():
    assert len(ANAPHORA_TABLE) == 18


@pytest.mark.parametrize("query,expected", ANAPHORA_TABLE, ids=[q for q, _ in ANAPHORA_TABLE])
def test_anaphora_types_table(query, expected):
    result = detect_context_reference(query)
    assert set(result["anaphora_types"]) == expected


# ---------------------------------------------------------------------------
# 2. test_legacy_ref_fields_unchanged
# ---------------------------------------------------------------------------

# Recorded snapshot of the pre-5b legacy fields for the same 18 strings
# (measured against the committed detect_context_reference before this
# phase's anaphora_types addition -- the addition is purely additive, so
# these values must still hold afterward).
LEGACY_SNAPSHOT = {
    TURN2: {
        "has_context_ref": True, "ref_types": ["follow_up"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "and the kitchen?": {
        "has_context_ref": True, "ref_types": ["follow_up", "implicit_location"], "suggested_intent": None,
        "has_room_indicator": True, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "what about tomorrow": {
        "has_context_ref": True, "ref_types": ["follow_up", "temporal"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": True, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "turn it off": {
        "has_context_ref": True, "ref_types": ["pronoun"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "make them brighter": {
        "has_context_ref": True, "ref_types": ["pronoun", "modifier"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": True,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "set it to level 2": {
        "has_context_ref": True, "ref_types": ["pronoun", "incomplete_command"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "set the lights to 50 percent": {
        "has_context_ref": True, "ref_types": ["incomplete_command"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "yes please": {
        "has_context_ref": True, "ref_types": ["continuation"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": True,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "do that again": {
        "has_context_ref": True, "ref_types": ["action"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "turn up": {
        "has_context_ref": True, "ref_types": ["incomplete_command"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "higher": {
        "has_context_ref": True, "ref_types": ["incomplete_command"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "turn them too": {
        "has_context_ref": True, "ref_types": ["pronoun", "follow_up"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "and the office": {
        "has_context_ref": True, "ref_types": ["follow_up", "implicit_location"], "suggested_intent": None,
        "has_room_indicator": True, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "louder": {
        "has_context_ref": True, "ref_types": ["modifier"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": True,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "warmer": {
        "has_context_ref": True, "ref_types": ["modifier"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": True,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "the same one": {
        "has_context_ref": True, "ref_types": ["pronoun"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "sure": {
        "has_context_ref": True, "ref_types": ["continuation"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": False, "is_continuation": True,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
    "was it": {
        "has_context_ref": True, "ref_types": ["pronoun", "inquiry"], "suggested_intent": None,
        "has_room_indicator": False, "has_temporal_ref": False, "has_modifier": False,
        "is_short_query": True, "is_inquiry": True, "is_continuation": False,
        "is_meta_inquiry": False, "is_conversation_breaker": False,
    },
}


@pytest.mark.parametrize("query", [q for q, _ in ANAPHORA_TABLE], ids=[q for q, _ in ANAPHORA_TABLE])
def test_legacy_ref_fields_unchanged(query):
    result = detect_context_reference(query)
    legacy = {k: v for k, v in result.items() if k != "anaphora_types"}
    assert legacy == LEGACY_SNAPSHOT[query]


# ---------------------------------------------------------------------------
# 3. test_follow_ups_continue
# ---------------------------------------------------------------------------

# (query, prev_intent, expected_reason)
FOLLOW_UP_ROWS = [
    ("and the kitchen?", "control", "anaphora"),
    ("what about tomorrow", "weather", "anaphora"),
    ("turn it off", "control", "anaphora"),
    ("make them brighter", "control", "anaphora"),
    ("set it to level 2", "control", "ellipsis"),
    ("set the lights to 50 percent", "control", "ellipsis"),
    ("yes please", "dining", "ellipsis"),
    ("do that again", "control", "ellipsis"),
    ("turn up", "control", "ellipsis"),
    ("higher", "control", "ellipsis"),
    ("turn them too", "control", "anaphora"),
    ("and the office", "control", "anaphora"),
    ("louder", "music_play", "anaphora"),  # named member: music family
    ("turn it up", "weather", "ellipsis"),  # ellipsis wins over a different-family fresh intent
    ("warmer", "control", "anaphora"),
    ("the same one", "dining", "anaphora"),
    ("sure", "dining", "ellipsis"),
    ("was it", "control", "anaphora"),
    ("in the kitchen", "control", "anaphora"),
    # Boundary/rule-order row: an ellipsis continues even when the fresh
    # intent is confident, specific, and in a DIFFERENT family than prev.
    ("set it to 50 percent", "music_play", "ellipsis"),
]


def test_follow_up_rows_population_floor():
    assert len(FOLLOW_UP_ROWS) == 20


@pytest.mark.parametrize(
    "query,prev_intent,expected_reason", FOLLOW_UP_ROWS,
    ids=[f"{q}|{p}" for q, p, _ in FOLLOW_UP_ROWS],
)
def test_follow_ups_continue(query, prev_intent, expected_reason):
    ref_info = detect_context_reference(query)
    fresh_intent, fresh_confidence = main_module._pattern_based_classification(
        query, return_confidence=True
    )
    should_continue, reason = decide_context_continuation(
        ref_info, prev_intent, fresh_intent, fresh_confidence
    )
    assert should_continue is True
    assert reason == expected_reason


# ---------------------------------------------------------------------------
# 4. test_topic_switches_classify_fresh
# ---------------------------------------------------------------------------

TOPIC_SWITCH_ROWS = [
    (TURN2, "recipes"),  # named member: turn 2 after recipes
    ("how are the ravens doing", "weather"),
    ("turn on the lights", "weather"),
    ("play some jazz", "control"),
    ("what is the score of the game", "dining"),
    ("what is the score of the ravens game", "control"),
    ("how to make a chocolate cake", "dining"),
    ("what movies are streaming tonight", "sports"),
    ("what is the weather forecast for tomorrow", "music_play"),
]


def test_topic_switch_rows_population_floor():
    assert len(TOPIC_SWITCH_ROWS) == 9


@pytest.mark.parametrize(
    "query,prev_intent", TOPIC_SWITCH_ROWS, ids=[f"{q}|{p}" for q, p in TOPIC_SWITCH_ROWS]
)
def test_topic_switches_classify_fresh(query, prev_intent):
    ref_info = detect_context_reference(query)
    fresh_intent, fresh_confidence = main_module._pattern_based_classification(
        query, return_confidence=True
    )
    decision = decide_context_continuation(ref_info, prev_intent, fresh_intent, fresh_confidence)
    assert decision == (False, "fresh_intent")


# ---------------------------------------------------------------------------
# 5. test_long_query_without_anaphora_not_continued
# ---------------------------------------------------------------------------

def test_long_query_without_anaphora_not_continued():
    query = "I was wondering and hoping you could tell me something new"
    assert len(query.split()) == 11
    ref_info = detect_context_reference(query)
    assert ref_info["anaphora_types"] == []
    fresh_intent, fresh_confidence = main_module._pattern_based_classification(
        query, return_confidence=True
    )
    decision = decide_context_continuation(ref_info, "dining", fresh_intent, fresh_confidence)
    assert decision == (False, "no_reference")


# ---------------------------------------------------------------------------
# 6. test_classify_node_replays_incident_transcript (end-to-end)
# ---------------------------------------------------------------------------

def test_classify_node_replays_incident_transcript():
    # Turn 2: prev intent recipes, garbled ASR dining query -> DINING, no cache.
    fake_cache = _install_classify_node_runtime()
    prev_ctx_recipes = ConversationContext(
        intent="recipes", query="give me a recipe for chicken parmesan",
        entities={}, parameters={}, response="Here is a recipe...",
    )
    main_module.get_conversation_context = AsyncMock(return_value=prev_ctx_recipes)
    state2 = _make_classify_state(TURN2)
    result2 = mock_run(main_module.classify_node(state2))
    assert result2.intent == IntentCategory.DINING
    assert fake_cache.get.await_count == 0

    # Turn 3: prev intent dining (from turn 2), "yes please" -> continues DINING, no cache.
    fake_cache2 = _install_classify_node_runtime()
    prev_ctx_dining = ConversationContext(
        intent="dining", query=TURN2, entities={}, parameters={},
        response="Would you like me to find some places?",
    )
    main_module.get_conversation_context = AsyncMock(return_value=prev_ctx_dining)
    state3 = _make_classify_state("yes please")
    result3 = mock_run(main_module.classify_node(state3))
    assert result3.intent == IntentCategory.DINING
    assert fake_cache2.get.await_count == 0

    # A genuine topic switch after weather reaches the cache (fresh path)
    # and does not stay WEATHER. "what about the ravens" itself trips the
    # semantic-cache skip pattern (`^what\s+about\s+`), so this uses an
    # equivalent sports query that doesn't.
    fake_cache3 = _install_classify_node_runtime(sentinel_intent="general_info", sentinel_confidence=0.5)
    prev_ctx_weather = ConversationContext(
        intent="weather", query="what is the weather", entities={}, parameters={}, response="Sunny today.",
    )
    main_module.get_conversation_context = AsyncMock(return_value=prev_ctx_weather)
    state4 = _make_classify_state("how are the ravens doing")
    result4 = mock_run(main_module.classify_node(state4))
    assert fake_cache3.get.await_count == 1
    assert result4.intent != IntentCategory.WEATHER


# ---------------------------------------------------------------------------
# 7. test_classify_node_follow_up_chain_still_continues (end-to-end, regression)
# ---------------------------------------------------------------------------

FOLLOW_UP_CHAIN_ROWS = [
    ("turn it off", "control", IntentCategory.CONTROL),
    ("make them brighter", "control", IntentCategory.CONTROL),
    ("yes please", "dining", IntentCategory.DINING),
    ("do that again", "control", IntentCategory.CONTROL),
    ("warmer", "control", IntentCategory.CONTROL),
    ("higher", "control", IntentCategory.CONTROL),
]


@pytest.mark.parametrize(
    "query,prev_intent,expected_intent", FOLLOW_UP_CHAIN_ROWS,
    ids=[f"{q}|{p}" for q, p, _ in FOLLOW_UP_CHAIN_ROWS],
)
def test_classify_node_follow_up_chain_still_continues(query, prev_intent, expected_intent):
    fake_cache = _install_classify_node_runtime()
    prev_ctx = ConversationContext(
        intent=prev_intent, query="prior query", entities={}, parameters={}, response="prior response",
    )
    main_module.get_conversation_context = AsyncMock(return_value=prev_ctx)
    state = _make_classify_state(query)
    result = mock_run(main_module.classify_node(state))
    assert result.intent == expected_intent
    assert fake_cache.get.await_count == 0
    assert result.continuation_decision["decision"] == "continued"


# ---------------------------------------------------------------------------
# 8. test_context_ref_view_modes
# ---------------------------------------------------------------------------

def test_context_ref_view_modes():
    turn2_raw = detect_context_reference(TURN2)

    continued = context_ref_view(turn2_raw, "continued")
    assert continued["has_context_ref"] == turn2_raw["has_context_ref"]
    assert continued["ref_types"] == turn2_raw["ref_types"]
    assert continued["raw"] == turn2_raw

    declined = context_ref_view(turn2_raw, "declined")
    assert declined["has_context_ref"] is False
    assert declined["is_continuation"] is False
    assert declined["is_inquiry"] is False
    assert declined["ref_types"] == []
    assert declined["anaphora_types"] == []
    assert declined["raw"] == turn2_raw

    yes_please_raw = detect_context_reference("yes please")
    not_consulted = context_ref_view(yes_please_raw, "not_consulted")
    assert not_consulted["has_context_ref"] is True  # anaphora_types == ["yes_no"]
    assert not_consulted["is_continuation"] is True
    assert not_consulted["is_inquiry"] is False
    assert not_consulted["ref_types"] == ["yes_no"]
    assert not_consulted["raw"] == yes_please_raw

    meta_raw = detect_context_reference("what happened")
    meta_raw["prev_error_context"] = {"intent": "control", "response": "It failed."}
    meta_declined = context_ref_view(meta_raw, "declined")
    assert meta_declined["prev_error_context"] == {"intent": "control", "response": "It failed."}
    assert meta_declined["has_context_ref"] is False


# ---------------------------------------------------------------------------
# 9. test_classify_node_writes_continuation_decision (end-to-end)
# ---------------------------------------------------------------------------

def test_classify_node_writes_continuation_decision_turn2_declined_strong_intent():
    _install_classify_node_runtime()
    prev_ctx = ConversationContext(
        intent="recipes", query="give me a recipe", entities={}, parameters={}, response="ok",
    )
    main_module.get_conversation_context = AsyncMock(return_value=prev_ctx)
    state = _make_classify_state(TURN2)
    result = mock_run(main_module.classify_node(state))
    assert result.continuation_decision == {"decision": "declined", "reason": "strong_intent"}
    assert result.context_ref_info["has_context_ref"] is False


def test_classify_node_writes_continuation_decision_declined_fresh_intent():
    _install_classify_node_runtime()
    prev_ctx = ConversationContext(
        intent="weather", query="what is the weather", entities={}, parameters={}, response="Sunny.",
    )
    main_module.get_conversation_context = AsyncMock(return_value=prev_ctx)
    state = _make_classify_state("how are the ravens doing")
    result = mock_run(main_module.classify_node(state))
    assert result.continuation_decision == {"decision": "declined", "reason": "fresh_intent"}


def test_classify_node_writes_continuation_decision_continued():
    _install_classify_node_runtime()
    prev_ctx = ConversationContext(
        intent="control", query="prior query", entities={}, parameters={}, response="prior response",
    )
    main_module.get_conversation_context = AsyncMock(return_value=prev_ctx)
    state = _make_classify_state("turn it off")
    result = mock_run(main_module.classify_node(state))
    assert result.continuation_decision["decision"] == "continued"
    assert result.context_ref_info["has_context_ref"] is True


def test_classify_node_writes_continuation_decision_not_consulted():
    _install_classify_node_runtime()
    main_module.get_conversation_context = AsyncMock(return_value=None)
    state = _make_classify_state(TURN2)
    result = mock_run(main_module.classify_node(state))
    assert result.continuation_decision["decision"] == "not_consulted"
    assert result.context_ref_info["has_context_ref"] is False


# ---------------------------------------------------------------------------
# 10. test_route_after_classify_honours_the_view (codex F16)
# ---------------------------------------------------------------------------

def _make_route_state(context_ref_info, query="tell me more about the wings I mentioned"):
    state = OrchestratorState(query=query)
    state.intent = IntentCategory.DINING
    state.confidence = 0.9
    state.mode = "owner"
    state.conversation_history = [
        {"role": "user", "content": "find dining"}, {"role": "assistant", "content": "ok"}
    ]
    state.context_ref_info = context_ref_info
    return state


def test_route_after_classify_declined_view_reaches_tool_call(monkeypatch):
    monkeypatch.setattr(main_module, "should_use_tool_calling", AsyncMock(return_value=False))
    declined_view = {
        "has_context_ref": False, "is_continuation": False, "is_inquiry": False,
        "ref_types": [], "anaphora_types": [],
    }
    state = _make_route_state(declined_view, query="any good bars around here")
    route = mock_run(main_module.route_after_classify(state))
    assert route == "tool_call"


def test_route_after_classify_raw_ref_still_demotes(monkeypatch):
    # Named member: the gate still reads has_context_ref -- proves it's the
    # writer (classify_node), not this reader, that fixes F16.
    monkeypatch.setattr(main_module, "should_use_tool_calling", AsyncMock(return_value=False))
    raw_ref = {"has_context_ref": True, "ref_types": ["follow_up"], "is_continuation": False, "is_inquiry": False}
    state = _make_route_state(raw_ref, query="any good bars around here")
    route = mock_run(main_module.route_after_classify(state))
    assert route == "synthesize"


def test_route_after_classify_genuine_conversational_reference_demotes(monkeypatch):
    monkeypatch.setattr(main_module, "should_use_tool_calling", AsyncMock(return_value=False))
    declined_view = {
        "has_context_ref": False, "is_continuation": False, "is_inquiry": False,
        "ref_types": [], "anaphora_types": [],
    }
    state = _make_route_state(declined_view, query="tell me more about the wings I mentioned")
    route = mock_run(main_module.route_after_classify(state))
    assert route == "synthesize"


# ---------------------------------------------------------------------------
# 12. test_dick_turn2_end_to_end_reaches_dining_tools (codex F24)
# ---------------------------------------------------------------------------

def _extract_intent_to_tools() -> dict:
    tree = ast.parse(MAIN_PY.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "tool_call_node":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "intent_to_tools" for t in inner.targets
                ):
                    return ast.literal_eval(inner.value)
    raise AssertionError("intent_to_tools literal not found in tool_call_node")


def test_dick_turn2_end_to_end_reaches_dining_tools(monkeypatch):
    monkeypatch.setattr(main_module, "should_use_tool_calling", AsyncMock(return_value=False))
    _install_classify_node_runtime()
    prev_ctx = ConversationContext(
        intent="recipes", query="give me a recipe", entities={}, parameters={}, response="ok",
    )
    main_module.get_conversation_context = AsyncMock(return_value=prev_ctx)
    state = _make_classify_state(TURN2)
    state.conversation_history = [
        {"role": "user", "content": "give me a recipe"}, {"role": "assistant", "content": "ok"}
    ]
    classified = mock_run(main_module.classify_node(state))
    assert classified.intent == IntentCategory.DINING

    route = mock_run(main_module.route_after_classify(classified))
    assert route == "tool_call"

    intent_to_tools = _extract_intent_to_tools()
    assert len(intent_to_tools) >= 10
    assert "search_restaurants" in intent_to_tools["dining"]


# ---------------------------------------------------------------------------
# 13. test_single_writer_of_context_ref_info (AST guard)
# ---------------------------------------------------------------------------

def test_single_writer_of_context_ref_info():
    tree = ast.parse(MAIN_PY.read_text())

    classify_node_fn = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "classify_node":
            classify_node_fn = node
            break
    assert classify_node_fn is not None
    classify_node_ids = {id(n) for n in ast.walk(classify_node_fn)}

    assigns = []
    subscript_writes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "context_ref_info"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "state"
                ):
                    assigns.append(node)
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "context_ref_info"
                and isinstance(value.value, ast.Name)
                and value.value.id == "state"
            ):
                subscript_writes.append(node)

    assert assigns, "expected at least one state.context_ref_info assignment"
    assert subscript_writes == []

    for assign in assigns:
        assert id(assign) in classify_node_ids, (
            f"state.context_ref_info assigned outside classify_node at line {assign.lineno}"
        )
        assert isinstance(assign.value, ast.Call) and isinstance(assign.value.func, ast.Name) \
            and assign.value.func.id == "context_ref_view", (
            f"state.context_ref_info assignment at line {assign.lineno} is not a context_ref_view(...) call"
        )


def mock_run(coro):
    import asyncio
    return asyncio.run(coro)

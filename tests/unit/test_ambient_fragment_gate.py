"""Spoken low-information fragments skip tool selection.

The predicate (`fast_path.is_ambient_fragment`), its I/O wrapper
(`ambient_fragment_applies`: guardrail switch plus the same open-question read
and fail-closed rule the fast path uses), and the three places that act on it:
`route_after_classify`, `run_orchestrator_for_streaming` and `finalize_node`.
The command corpus runs through the real pattern classifier and the real router
with the classifier's LLM result forced to the worst case (UNKNOWN, 0.1).
"""
from __future__ import annotations

import asyncio
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.assistant_profile import DEFAULT_GUARDRAILS, ambient_fragment_gate_enabled
from shared.output_channel import OutputChannel

from . import _fast_path_harness as fp
from . import _public_audience_harness as h
from orchestrator.fast_path import ambient_block_reason, ambient_fragment_applies, is_ambient_fragment
from orchestrator.utterance_kind import classify_utterance
from shared.fast_path_vocab import AMBIENT_REPLY
from orchestrator.state import IntentCategory, OrchestratorState

SPEECH, TEXT = OutputChannel.SPEECH, OutputChannel.TEXT
U = IntentCategory.UNKNOWN
FALLBACK = (IntentCategory.GENERAL_INFO, 0.5)     # what the pattern classifier says when nothing matches
SWITCH_ON = {"voice_response": {"ambient_fragment_gate": True}}

# Overheard speech nothing in the house can place.
FRAGMENTS = ["hmm interesting point", "huh well alright then", "wow big surprise ending"]

# Real utterances through the real classify_utterance and the real
# _pattern_based_classification, with the LLM classifier forced to its worst
# case (UNKNOWN, 0.1). None may be gated when the switch is on; each names the
# guard that stops it. Short ones are stopped by the word-count floor; the
# longer variants below prove the content guards on their own.
NEVER_GATED = {
    "I'm cold": "word_count",
    "it's too dark in here": "reference_word",
    "goodnight": "word_count",
    "brighter": "word_count",
    "I'm home": "word_count",
    "repeat that": "word_count",
    "say that again": "first_word",
    "sorry what": "word_count",
    "go back": "word_count",
    "be quiet": "word_count",
    "quiet": "word_count",
    "the kitchen one": "reference_word",
    "yes please": "word_count",
    "all of them": "reference_word",
    "the first one": "reference_word",
    "bedroom": "word_count",
    "yes": "word_count",
    "no": "word_count",
    # longer variants, so a guard other than the word-count floor must fire
    "it is way too cold in here": "reference_word",
    "i am really cold right now": "first_person",
    "say that one more time": "first_word",
    "sorry could you say that": "first_word",
    "bring the lights up slowly": "media_command_word",
    "yeah that works for me": "first_person",
    "okay sounds good enough then": "confirmation_word",
    "no thanks not right now": "confirmation_word",
    "kitchen lights blue please": "device_word",
    "garage door status today": "device_word",
    "dim kitchen lights slowly": "device_word",
    "please quiet down kitchen": "first_word",
    "louder in the bedroom now": "media_command_word",
    "it needs to be warmer": "media_command_word",
}
# Words that must never be gated: asked for help, or an alarm.
SAFETY = "help emergency fire police ambulance 911 alarm smoke intruder cancel stop".split()
# Phrases the real pattern classifier maps to a specific intent.
CLASSIFIED = ["what's the weather", "turn on the kitchen lights", "play some jazz"]


def _reason(query="hmm interesting point", *, intent=U, confidence=0.1, open_question=None, history=(), ref=None,
            channel=SPEECH, pattern=FALLBACK):
    return ambient_block_reason(query, intent, confidence, open_question, list(history), ref, channel, pattern)


def _gated(query="hmm interesting point", **kwargs):
    return _reason(query, **kwargs) is None


# --- the predicate ----------------------------------------------------------------------


@pytest.mark.parametrize("query", FRAGMENTS)
def test_observed_fragments_are_gated_on_speech_only(query):
    assert _gated(query) is True
    assert _reason(query, channel=TEXT) == "channel"


@pytest.mark.parametrize("query", FRAGMENTS)
def test_the_fragments_are_what_the_real_classifiers_cannot_place(query):
    pattern = h.main._pattern_based_classification(query, return_confidence=True)
    assert pattern == FALLBACK
    assert classify_utterance(query).kind.value == "unknown"


def test_word_count_boundaries():
    assert _reason("hmm " + "okay-ish " * 0 + "interesting point") is None          # 3 words: the floor
    assert _reason("hmm interesting") == "word_count"                               # 2
    assert _reason("hmm " + "interesting " * 6 + "point") is None                   # 8
    assert _reason("hmm " + "interesting " * 7 + "point") == "word_count"           # 9
    assert _reason("") == "word_count"


def test_confidence_boundary_029_gated_030_not():
    assert _gated(confidence=0.29) is True
    assert _reason(confidence=0.30) == "confidence"
    assert _reason(confidence=None) == "confidence"


def test_only_unknown_is_gated():
    for intent in IntentCategory:
        assert (_reason(intent=intent) is None) is (intent is U), intent


@pytest.mark.parametrize("query,guard", list(NEVER_GATED.items()))
def test_real_utterances_are_never_gated_and_the_named_guard_fires(query, guard):
    pattern = h.main._pattern_based_classification(query, return_confidence=True)
    assert _reason(query, pattern=pattern) == guard


def test_the_corpus_is_large_and_hits_every_content_guard():
    assert len(NEVER_GATED) >= 25
    assert {"word_count", "first_word", "first_person", "reference_word", "device_word", "confirmation_word", "media_command_word"} <= set(NEVER_GATED.values())


@pytest.mark.parametrize("word", SAFETY)
@pytest.mark.parametrize("confidence", [0.0, 0.1, 0.29])
def test_a_safety_word_is_never_gated_at_low_confidence(word, confidence):
    assert _reason(word, confidence=confidence) == "safety_word"
    assert _reason(f"hmm there is {word} again", confidence=confidence) == "safety_word"
    assert _reason(f"{word.upper()}!", confidence=confidence) == "safety_word"


def test_the_pattern_classifier_must_confirm_no_match():
    assert _reason(pattern=None) == "pattern_classifier"
    assert _reason(pattern=(IntentCategory.CONTROL, 0.85)) == "pattern_classifier"
    assert _reason(pattern=(IntentCategory.GENERAL_INFO, 0.85)) == "pattern_classifier"
    assert _reason(pattern=(IntentCategory.WEATHER, 0.5)) == "pattern_classifier"
    assert _reason(pattern=(U, 0.5)) is None
    assert _reason(pattern=FALLBACK) is None


def test_an_open_question_is_never_gated():
    for reason in ("pending_confirmation", "awaiting_context", "open_question", "context_unreadable"):
        assert _reason(open_question=reason) == "open_question", reason


def test_a_continuation_with_history_is_never_gated():
    history = [{"role": "assistant", "content": "It's sunny."}]
    ref = {"is_continuation": True}
    assert _reason(history=history, ref=ref) == "continuation"
    assert _gated(history=[], ref=ref) is True            # no history to continue
    assert _gated(history=history, ref={"is_continuation": False}) is True


def test_the_utterance_classifier_is_its_own_guard(monkeypatch):
    """A phrase no word guard stops, but that the classifier reads as a command or question."""
    from orchestrator import fast_path
    from orchestrator.utterance_kind import UtteranceClassification, UtteranceKind

    for kind in (UtteranceKind.IMPERATIVE, UtteranceKind.STATE_QUESTION):
        monkeypatch.setattr(fast_path, "classify_utterance", lambda q, kind=kind: UtteranceClassification(kind=kind))
        assert _reason() == "utterance_kind"
    monkeypatch.setattr(
        fast_path, "classify_utterance",
        lambda q: UtteranceClassification(kind=UtteranceKind.UNKNOWN, device_type="light"),
    )
    assert _reason() == "utterance_kind"


def test_the_predicate_never_raises():
    assert ambient_block_reason(object(), "unknown", "x", None, None, None, SPEECH) == "error"
    assert is_ambient_fragment(object(), "unknown", "x", None, None, None, SPEECH) is False


# --- the guardrail switch value (default OFF) --------------------------------------------------


@pytest.mark.parametrize("guardrails,expected", [
    (None, False), ({}, False), (DEFAULT_GUARDRAILS, False),
    ({"voice_response": {}}, False), ({"voice_response": None}, False), ({"voice_response": "on"}, False),
    ({"voice_response": {"ambient_fragment_gate": True}}, True),
    ({"voice_response": {"ambient_fragment_gate": False}}, False),
    ({"voice_response": {"ambient_fragment_gate": "true"}}, True),
    ({"voice_response": {"ambient_fragment_gate": "On"}}, True),
    ({"voice_response": {"ambient_fragment_gate": "false"}}, False),
    ({"voice_response": {"ambient_fragment_gate": 1}}, False),     # only a real true counts
    ({"voice_response": {"ambient_fragment_gate": None}}, False),
    ({"voice_response": {"ambient_fragment_gate": "garbage"}}, False),
])
def test_switch_values(guardrails, expected):
    assert ambient_fragment_gate_enabled(guardrails) is expected


def test_the_default_guardrails_ship_the_switch_off_in_both_default_dicts():
    from pathlib import Path

    assert DEFAULT_GUARDRAILS["voice_response"]["ambient_fragment_gate"] is False
    admin_defaults = (Path(__file__).resolve().parents[2] / "admin/backend/app/routes/settings.py").read_text(encoding="utf-8")
    assert admin_defaults.count('"ambient_fragment_gate": False') == 1
    assert "ambient_fragment_gate\": True" not in admin_defaults


# --- the wrapper and the actors --------------------------------------------------------------


class SimpleRig:
    def __init__(self, cache, guardrails, selection, counter):
        self.cache, self.guardrails, self.selection, self.counter = cache, guardrails, selection, counter


@pytest.fixture(autouse=True)
def rig(monkeypatch):
    h.reset_runtime()
    cache = fp.FakeCache()
    h._runtime.set_cache_client(cache)
    guardrails = AsyncMock(return_value=SWITCH_ON)          # the switch is ON unless a test says otherwise
    monkeypatch.setattr("orchestrator.fast_path.get_guardrails", guardrails)
    selection = AsyncMock(return_value=False)
    monkeypatch.setattr(h.main, "should_use_tool_calling", selection)
    counter = MagicMock()
    monkeypatch.setattr(h.main, "ambient_fragment_gated_total", counter)
    yield SimpleRig(cache, guardrails, selection, counter)
    h.reset_runtime()


def _state(query="hmm interesting point", *, interface_type="voice", intent=U, confidence=0.1, history=None, session_id="amb-1"):
    state = OrchestratorState(query=query)
    state.intent, state.confidence = intent, confidence
    state.interface_type = interface_type
    state.session_id = session_id
    state.conversation_history = history or []
    state.permissions = {"mode": "owner"}
    state.mode = "owner"
    return state


def _route(state):
    return asyncio.run(h.main.route_after_classify(state))


def test_the_real_pattern_classifier_is_registered_by_main():
    from orchestrator import fast_path

    assert fast_path._pattern_classifier is h.main._pattern_based_classification


def test_a_fragment_skips_tool_selection_and_reaches_finalize(rig):
    assert _route(_state()) == "finalize"
    rig.selection.assert_not_awaited()
    rig.counter.labels.assert_called_once_with(route="graph")


def test_without_a_registered_pattern_classifier_nothing_is_gated(rig, monkeypatch):
    monkeypatch.setattr("orchestrator.fast_path._pattern_classifier", None)
    assert asyncio.run(ambient_fragment_applies(_state())) is False
    _route(_state())
    rig.selection.assert_awaited_once()


def test_the_default_guardrails_leave_the_gate_off(rig):
    rig.guardrails.return_value = DEFAULT_GUARDRAILS
    _route(_state())
    rig.selection.assert_awaited_once()
    rig.counter.labels.assert_not_called()


def test_text_keeps_tool_selection(rig):
    _route(_state(interface_type="text"))
    rig.selection.assert_awaited_once()
    rig.counter.labels.assert_not_called()


def test_the_switch_off_restores_tool_selection(rig):
    rig.guardrails.return_value = {"voice_response": {"ambient_fragment_gate": False}}
    _route(_state())
    rig.selection.assert_awaited_once()
    rig.counter.labels.assert_not_called()


def test_an_unexpected_error_in_the_wrapper_does_not_gate(rig):
    rig.guardrails.side_effect = RuntimeError("admin down in an unexpected way")
    assert asyncio.run(ambient_fragment_applies(_state())) is False
    _route(_state())
    rig.selection.assert_awaited_once()


def test_confidence_boundary_through_the_router(rig):
    _route(_state(confidence=0.29))
    rig.selection.assert_not_awaited()
    _route(_state(confidence=0.30))
    rig.selection.assert_awaited_once()


def test_nine_words_keep_tool_selection(rig):
    _route(_state("hmm " + "interesting " * 7 + "point"))
    rig.selection.assert_awaited_once()


# --- the same open-question read as the fast path, same fail-closed rule ------------------------

from .test_fast_path_pending_defer import OPENERS  # noqa: E402  (real stored contexts, as the fast path sees them)


@pytest.mark.parametrize("opener", ["pending_confirmation", "foreign_pending", "awaiting_state_question"])
def test_a_stored_open_question_is_not_gated(rig, opener):
    async def scenario():
        await OPENERS[opener](rig, "amb-1")
        return await h.main.route_after_classify(_state())

    asyncio.run(scenario())
    rig.selection.assert_awaited_once()
    rig.counter.labels.assert_not_called()


def test_an_assistant_question_in_history_is_not_gated(rig):
    history = [{"role": "user", "content": "turn on a light"}, {"role": "assistant", "content": "Which light do you mean?"}]
    _route(_state(history=history))
    rig.selection.assert_awaited_once()


def test_a_context_read_failure_does_not_gate(rig):
    rig.cache.client.fail_reads = True
    _route(_state())
    rig.selection.assert_awaited_once()
    rig.counter.labels.assert_not_called()


def test_an_ordinary_stored_context_does_not_stop_the_gate(rig):
    from orchestrator.helpers import store_conversation_context

    async def scenario():
        await store_conversation_context(
            session_id="amb-1", intent="control", query="turn on the lamp", entities={},
            parameters={"action": "turn_on"}, response="Done.",
        )
        return await h.main.route_after_classify(_state())

    assert asyncio.run(scenario()) == "finalize"
    rig.selection.assert_not_awaited()


def test_only_a_candidate_pays_for_the_reads(rig):
    _route(_state("turn on the lights", confidence=0.1))
    rig.guardrails.assert_not_awaited()


# --- the real-utterance corpus, with the switch ON, through the real router ----------------------------


@pytest.mark.parametrize("query", list(NEVER_GATED))
def test_real_utterances_reach_tool_selection_even_when_the_llm_says_unknown(rig, query):
    state = _state(query)             # the worst case: the LLM classifier fell through to UNKNOWN, 0.1
    assert asyncio.run(ambient_fragment_applies(state)) is False
    _route(state)
    rig.counter.labels.assert_not_called()
    rig.selection.assert_awaited_once()


@pytest.mark.parametrize("word", SAFETY)
def test_safety_words_reach_tool_selection_at_zero_confidence(rig, word):
    _route(_state(word, confidence=0.0))
    rig.counter.labels.assert_not_called()
    rig.selection.assert_awaited_once()


@pytest.mark.parametrize("query", ["yes", "no", "the kitchen one"])
def test_bare_replies_with_no_stored_context_are_not_gated(rig, query):
    """Pinned: with the switch on and nothing stored, a bare yes/no or an option reply still
    goes to the normal pipeline, never to "Sorry, I didn't catch that."."""
    assert asyncio.run(rig.cache.client.get("nothing")) is None
    state = _state(query)
    assert asyncio.run(ambient_fragment_applies(state)) is False
    _route(state)
    rig.selection.assert_awaited_once()


def test_stop_with_music_playing_is_not_gated(rig):
    history = [{"role": "user", "content": "play some jazz"}, {"role": "assistant", "content": "Playing jazz in the kitchen."}]
    assert asyncio.run(ambient_fragment_applies(_state("stop", history=history))) is False


@pytest.mark.parametrize("query", CLASSIFIED)
def test_phrases_the_real_pattern_classifier_places_are_never_gated(rig, query):
    intent, confidence = h.main._pattern_based_classification(query, return_confidence=True)
    assert intent is not U
    assert asyncio.run(ambient_fragment_applies(_state(query, intent=intent, confidence=confidence))) is False
    # and even with the LLM at its worst, the pattern classifier (or a word guard) stops it
    assert asyncio.run(ambient_fragment_applies(_state(query))) is False


# --- finalize, the streaming runner, and the real graph ---------------------------------------------------


def test_finalize_answers_a_gated_fragment_with_the_ambient_reply(rig):
    from orchestrator.nodes import finalize_node

    state = _state()
    state.answer = ""
    out = asyncio.run(finalize_node(state))
    assert out.answer == AMBIENT_REPLY
    assert out.is_fallback is False


def test_finalize_keeps_the_generic_fallback_for_text(rig):
    from orchestrator.nodes import finalize_node

    state = _state(interface_type="text")
    state.answer = ""
    out = asyncio.run(finalize_node(state))
    assert out.answer != AMBIENT_REPLY and out.is_fallback is True


def test_finalize_keeps_the_generic_fallback_when_the_switch_is_off(rig):
    from orchestrator.nodes import finalize_node

    rig.guardrails.return_value = {"voice_response": {"ambient_fragment_gate": False}}
    state = _state()
    state.answer = ""
    assert asyncio.run(finalize_node(state)).answer != AMBIENT_REPLY


def test_router_then_finalize_with_a_raising_tool_selection(rig):
    from orchestrator.nodes import finalize_node

    rig.selection.side_effect = AssertionError("tool selection reached")
    state = _state()
    assert _route(state) == "finalize"
    state.answer = ""
    assert asyncio.run(finalize_node(state)).answer == AMBIENT_REPLY


_REAL_GRAPH_SCRIPT = """
import asyncio, os, sys
from unittest import mock
os.environ.setdefault("SERVICE_API_KEY", "test-key")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")
sys.modules["prometheus_client"] = mock.MagicMock()
from shared.config import get_config
cl = mock.MagicMock()
cl.get_config = get_config
cl.ADMIN_API_URL = "http://localhost:8080"
cl.get_feature_flag = mock.AsyncMock(return_value=False)
cl.get_feature_flags = mock.AsyncMock(return_value={})
cl.clear_cache = mock.AsyncMock()
sys.modules["orchestrator.config_loader"] = cl
import langgraph.graph
assert isinstance(langgraph.graph.StateGraph, type), "real langgraph expected"
import orchestrator.nodes
import orchestrator.main as main
from orchestrator.nodes import _runtime
from orchestrator.state import IntentCategory, OrchestratorState

query = sys.argv[1]
selection_reached = []

async def classify(state):
    state.intent, state.confidence = IntentCategory.UNKNOWN, 0.1
    return state

async def selection(*a, **k):
    selection_reached.append(True)
    return False

class Cache:
    client = None
    async def set(self, *a, **k): return True
    async def get(self, *a, **k): return None

main.classify_node = classify
main.should_use_tool_calling = selection
_runtime.set_cache_client(Cache())
async def guardrails(): return {"voice_response": {"ambient_fragment_gate": True}}
import orchestrator.fast_path as fp
fp.get_guardrails = guardrails
state = OrchestratorState(query=query)
state.interface_type = "voice"
state.session_id = "g-1"
state.permissions = {"mode": "owner"}
state.mode = "owner"
result = asyncio.run(main.create_orchestrator_graph().ainvoke(state))
print("ANSWER=" + result["answer"])
print("SELECTION=" + str(bool(selection_reached)))
"""


def _run_real_graph(query):
    import os
    import subprocess
    import sys
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[2] / "src")
    proc = subprocess.run(
        [sys.executable, "-c", _REAL_GRAPH_SCRIPT, query],
        capture_output=True, text=True, timeout=120, env={**os.environ, "PYTHONPATH": src},
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if line.startswith(("ANSWER=", "SELECTION=")))
    return out["ANSWER"], out["SELECTION"] == "True"


def test_the_real_graph_gates_a_fragment_with_the_switch_on():
    """Run in a fresh interpreter: other test modules stub langgraph in-process."""
    answer, selection_reached = _run_real_graph("hmm interesting point")
    assert answer == AMBIENT_REPLY
    assert selection_reached is False


def test_the_real_graph_sends_a_spoken_command_to_selection_with_the_switch_on():
    answer, selection_reached = _run_real_graph("turn on the kitchen lights")
    assert selection_reached is True      # it reached tool selection, not the ambient finalize
    assert answer != AMBIENT_REPLY


def _stub_stream_runner(monkeypatch):
    async def classify(state):
        state.intent, state.confidence = U, 0.1
        return state

    monkeypatch.setattr(h.main, "classify_node", classify)


def test_the_streaming_runner_answers_a_fragment_without_tool_selection(rig, monkeypatch):
    _stub_stream_runner(monkeypatch)
    rig.selection.side_effect = AssertionError("tool selection reached")
    out = asyncio.run(h.main.run_orchestrator_for_streaming(_state()))
    assert out.answer == AMBIENT_REPLY
    rig.counter.labels.assert_called_once_with(route="stream")


def test_the_streaming_runner_keeps_tool_selection_for_text_and_switch_off(rig, monkeypatch):
    _stub_stream_runner(monkeypatch)
    asyncio.run(h.main.run_orchestrator_for_streaming(_state(interface_type="text")))
    assert rig.selection.await_count == 1
    rig.guardrails.return_value = {"voice_response": {"ambient_fragment_gate": False}}
    out = asyncio.run(h.main.run_orchestrator_for_streaming(_state()))
    assert rig.selection.await_count == 2
    assert out.answer != AMBIENT_REPLY


@pytest.mark.parametrize("query", ["stop", "I'm cold", "the kitchen one", "help"])
def test_the_streaming_runner_does_not_gate_real_utterances(rig, monkeypatch, query):
    _stub_stream_runner(monkeypatch)
    out = asyncio.run(h.main.run_orchestrator_for_streaming(_state(query)))
    assert out.answer != AMBIENT_REPLY
    rig.selection.assert_awaited_once()

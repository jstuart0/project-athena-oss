"""`/ha/conversation` with `ha_intent_prerouting` on skips the pre-router for fast-path candidates.

The classifier (`classify_intent`) and the simple-intent model
(`handle_simple_intent`) are two LLM calls ahead of the orchestrator. For a
turn the orchestrator answers deterministically ("what time is it") they add
latency and nothing else, so those turns go straight to `/query`. Scene
phrases and bare yes/no replies are not candidates and pre-route as before.
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, "src")

sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-gateway-prerouting")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

import gateway.main as gw  # noqa: E402


@pytest.fixture
def rig(monkeypatch):
    flags = {"ha_intent_prerouting": True}

    async def _flag(name, default=False):
        return flags.get(name, default)

    classify = mock.AsyncMock(return_value="COMPLEX")
    simple = mock.AsyncMock(return_value=None)
    answer = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="From the orchestrator."))])
    orchestrate = mock.AsyncMock(return_value=(answer, "sess-1"))
    skipped = mock.MagicMock()
    sessions = SimpleNamespace(
        get_session_for_device=mock.AsyncMock(return_value=None),
        update_session_for_device=mock.AsyncMock(),
    )
    monkeypatch.setattr(gw, "get_feature_flag", _flag)
    monkeypatch.setattr(gw, "classify_intent", classify)
    monkeypatch.setattr(gw, "handle_simple_intent", simple)
    monkeypatch.setattr(gw, "route_to_orchestrator", orchestrate)
    monkeypatch.setattr(gw, "prerouting_skipped_total", skipped)
    monkeypatch.setattr(gw, "device_session_mgr", sessions)
    monkeypatch.setattr(gw, "_detect_room_from_active_satellite", mock.AsyncMock(return_value="kitchen"))
    return SimpleNamespace(flags=flags, classify=classify, simple=simple, orchestrate=orchestrate, skipped=skipped)


def _ask(text):
    return asyncio.run(gw.ha_conversation(gw.HAConversationRequest(text=text, device_id="kitchen")))


@pytest.mark.parametrize("text", [
    "what time is it", "What's the date?", "hello", "thanks", "how are you", "got it", "bye",
])
def test_candidates_skip_both_llm_calls_and_go_to_the_orchestrator(rig, text):
    response = _ask(text)
    rig.classify.assert_not_awaited()
    rig.simple.assert_not_awaited()
    rig.orchestrate.assert_awaited_once()
    rig.skipped.inc.assert_called_once_with()
    assert response.conversation_id == "sess-1"


def test_a_real_question_still_pre_routes(rig):
    _ask("tell me a joke")
    rig.classify.assert_awaited_once_with("tell me a joke")
    rig.skipped.inc.assert_not_called()


@pytest.mark.parametrize("text", ["good morning", "okay", "goodbye", "no thanks", "yes please", "good night"])
def test_scene_and_confirmation_phrases_pre_route_exactly_as_before(rig, text):
    _ask(text)
    rig.classify.assert_awaited_once_with(text)
    rig.skipped.inc.assert_not_called()


def test_simple_intent_answer_is_unchanged_for_non_candidates(rig):
    rig.classify.return_value = "SIMPLE"
    rig.simple.return_value = "A short chat answer."
    response = _ask("tell me a joke")
    rig.simple.assert_awaited_once_with("tell me a joke")
    rig.orchestrate.assert_not_awaited()
    assert response.conversation_id == "prerouted"


def test_flag_off_never_counts_a_skip(rig):
    rig.flags["ha_intent_prerouting"] = False
    _ask("what time is it")
    rig.classify.assert_not_awaited()
    rig.skipped.inc.assert_not_called()
    rig.orchestrate.assert_awaited_once()


@pytest.mark.parametrize("text", ["good morning", "okay", "no thanks"])
def test_the_gateway_applies_the_shared_exclusions_not_just_table_membership(rig, monkeypatch, text):
    """Even if the table listed a scene or confirmation phrase, the gateway
    still pre-routes it, because the exclusion rule is the vocabulary's."""
    from shared import fast_path_vocab

    monkeypatch.setitem(fast_path_vocab.REPLIES, text, ("greeting", "x"))
    _ask(text)
    rig.classify.assert_awaited_once_with(text)
    rig.skipped.inc.assert_not_called()

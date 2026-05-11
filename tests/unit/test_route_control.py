"""Unit tests for orchestrator.nodes.route_control.route_control_node.

Covers major routing branches:

 1.  Sensor fast-path via state.entities device_type=="sensor".
 2.  Presence/occupancy pattern-match fast path.
 3.  Status-query detection + skip-synthesis optimised return.
 4.  Status-query detection + bulk-loaded data (fall-through).
 5.  Status-query optimisation exception — falls back silently.
 6.  Dynamic-agent route (automation_mode=="dynamic_agent" + should_use_automation_agent).
 7.  Sequence intent detected + sequence_executor executes.
 8.  Sequence detected but no steps — falls through to normal extraction.
 9.  Sequence detected but sequence_executor is None — fallback message.
10.  Inquiry follow-up (is_inquiry) — answer from prev_context.
11.  Context continuation (has_context_ref) — previous intent merged, new room applied.
12.  Context reversal "back on" in query.
13.  Brightness modifier "brighter" applied.
14.  Normal intent extraction — no context.
15.  store_conversation_context called on success with session_id.
16.  Fallback branch (no smart_controller) — turn-on pattern.
17.  Fallback branch — check_entity_permission denied.
18.  Outer exception handler — error set, fallback answer returned.
19.  timing_tracker.track_sync called after node completes.
20.  node_timings["route_control"] set on every return path.

Patching strategy:
- smart_controller / sequence_executor / automation_agent / ha_client / entity_manager:
  install fakes via orchestrator.nodes._runtime.set_*() — the proxy shims delegate
  to _runtime at call time.
- Helpers patched at their source modules (lazy-import safe):
  orchestrator.helpers.get_feature_config
  orchestrator.helpers.get_automation_system_mode
  orchestrator.helpers.store_conversation_context
  orchestrator.ha_status_optimizer.detect_status_query_type
  orchestrator.ha_status_optimizer.optimize_status_query
  orchestrator.ha_status_optimizer.should_skip_synthesis
  orchestrator.automation_agent.should_use_automation_agent
  orchestrator.mode_permission.check_entity_permission
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock, patch

# Stub heavy deps before any orchestrator import.
for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

from orchestrator.nodes import route_control_node
from orchestrator.nodes import _runtime
from orchestrator.state import OrchestratorState, IntentCategory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_state(
    *,
    query: str = "turn on the living room lights",
    mode: str = "owner",
    session_id: str | None = "sess-1",
    entities: dict | None = None,
    permissions: dict | None = None,
    room: str | None = "living room",
    prev_context: dict | None = None,
    context_ref_info: dict | None = None,
    timing_tracker=None,
) -> OrchestratorState:
    state = OrchestratorState(query=query)
    state.mode = mode
    state.session_id = session_id
    state.entities = entities or {}
    state.permissions = permissions or {}
    state.room = room
    state.prev_context = prev_context
    state.context_ref_info = context_ref_info
    state.timing_tracker = timing_tracker
    state.node_timings = {}
    return state


def _make_smart_controller(**overrides):
    sc = MagicMock()
    sc.detect_sequence_intent.return_value = False
    sc.extract_sequence_intent = AsyncMock(return_value=None)
    sc.extract_intent = AsyncMock(return_value={"device_type": "light", "action": "turn_on", "room": "living room"})
    sc.execute_intent = AsyncMock(return_value="Done! Lights on.")
    sc._handle_sensor_intent = AsyncMock(return_value="Sensor reading: 72°F")
    for k, v in overrides.items():
        setattr(sc, k, v)
    return sc


def _make_status_result(query_type: str = "all_lights", entities=None, raw_states=None):
    sr = MagicMock()
    sr.query_type = query_type
    sr.entities = entities or [{"entity_id": "light.kitchen", "state": "on"}]
    sr.raw_states = raw_states or {}
    return sr


# Pre-existing: disable strict mode so unset singletons don't warn.
_runtime.reset_for_test()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSensorFastPath:
    def test_device_type_sensor_routes_to_sensor_handler(self):
        sc = _make_smart_controller()
        _runtime.set_smart_controller(sc)
        state = _make_state(
            query="what is the temperature?",
            entities={"device_type": "sensor", "parameters": {"sensor_type": "temperature"}},
        )
        with patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock, return_value={"enabled": False}):
            result = _run(route_control_node(state))
        sc._handle_sensor_intent.assert_awaited_once_with(
            "sensor",
            {"sensor_type": "temperature"},
            "what is the temperature?",
        )
        assert result.answer == "Sensor reading: 72°F"
        assert "route_control" in result.node_timings

    def test_presence_pattern_routes_to_sensor_handler(self):
        sc = _make_smart_controller()
        _runtime.set_smart_controller(sc)
        state = _make_state(query="is anyone home?", entities={})
        with patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock, return_value={"enabled": False}):
            result = _run(route_control_node(state))
        sc._handle_sensor_intent.assert_awaited_once_with(
            "sensor",
            {"query_type": "presence"},
            "is anyone home?",
        )
        assert "route_control" in result.node_timings

    def test_presence_pattern_no_smart_controller_skips_sensor_handler(self):
        _runtime.set_smart_controller(None)
        state = _make_state(query="anyone home?", entities={})
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock, return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.get_automation_system_mode", new_callable=AsyncMock, return_value="pattern"),
        ):
            result = _run(route_control_node(state))
        # Without smart controller the else-branch fires; answer is set there.
        assert "route_control" in result.node_timings


class TestStatusQueryOptimisation:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_status_query_skip_synthesis_returns_templated_response(self):
        sc = _make_smart_controller()
        em = MagicMock()
        _runtime.set_smart_controller(sc)
        _runtime.set_entity_manager(em)
        state = _make_state(query="what lights are on?")
        sr = _make_status_result()
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": True, "config": {}}),
            patch("orchestrator.nodes.route_control.detect_status_query_type", return_value=True),
            patch("orchestrator.nodes.route_control.optimize_status_query",
                  new_callable=AsyncMock, return_value=sr),
            patch("orchestrator.nodes.route_control.should_skip_synthesis",
                  return_value=(True, "Kitchen light is on.")),
        ):
            result = _run(route_control_node(state))
        assert result.answer == "Kitchen light is on."
        assert result.skip_synthesis is True
        assert "route_control" in result.node_timings

    def test_status_query_bulk_loaded_falls_through(self):
        sc = _make_smart_controller()
        em = MagicMock()
        _runtime.set_smart_controller(sc)
        _runtime.set_entity_manager(em)
        _runtime.set_automation_agent(None)
        state = _make_state(query="what lights are on?")
        sr = _make_status_result()
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": True, "config": {}}),
            patch("orchestrator.nodes.route_control.detect_status_query_type", return_value=True),
            patch("orchestrator.nodes.route_control.optimize_status_query",
                  new_callable=AsyncMock, return_value=sr),
            patch("orchestrator.nodes.route_control.should_skip_synthesis",
                  return_value=(False, None)),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.store_conversation_context",
                  new_callable=AsyncMock),
        ):
            result = _run(route_control_node(state))
        # ha_status_data should be injected into state.context
        assert result.context is not None
        assert "ha_status_data" in result.context
        # Falls through to normal extraction; smart controller was called
        sc.extract_intent.assert_awaited()

    def test_status_query_optimisation_exception_falls_back(self):
        sc = _make_smart_controller()
        em = MagicMock()
        _runtime.set_smart_controller(sc)
        _runtime.set_entity_manager(em)
        _runtime.set_automation_agent(None)
        state = _make_state(query="what lights are on?")
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": True, "config": {}}),
            patch("orchestrator.nodes.route_control.detect_status_query_type", return_value=True),
            patch("orchestrator.nodes.route_control.optimize_status_query",
                  new_callable=AsyncMock, side_effect=RuntimeError("HA down")),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.store_conversation_context",
                  new_callable=AsyncMock),
        ):
            result = _run(route_control_node(state))
        # Optimisation failed; normal path executes
        sc.extract_intent.assert_awaited()
        assert "route_control" in result.node_timings


class TestDynamicAgentRouting:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_dynamic_agent_route(self):
        sc = _make_smart_controller()
        aa = MagicMock()
        aa.execute = AsyncMock(return_value="Automation executed.")
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(aa)
        _runtime.set_entity_manager(None)
        state = _make_state(query="turn on every light in the house then play jazz")
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.detect_status_query_type", return_value=False),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="dynamic_agent"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=True),
        ):
            result = _run(route_control_node(state))
        aa.execute.assert_awaited_once()
        assert result.answer == "Automation executed."
        assert "route_control" in result.node_timings


class TestSequenceRouting:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_sequence_intent_with_steps_executed(self):
        sc = _make_smart_controller()
        sc.detect_sequence_intent.return_value = True
        sc.extract_sequence_intent = AsyncMock(return_value={
            "steps": [{"action": "turn_on", "entity": "light.kitchen"}],
            "acknowledge": "On it!",
        })
        se = MagicMock()
        se.execute_sequence = AsyncMock(return_value="done")
        _runtime.set_smart_controller(sc)
        _runtime.set_sequence_executor(se)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        state = _make_state(query="turn on kitchen then dim to 50% after 1 minute")
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.detect_status_query_type", return_value=False),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
        ):
            result = _run(route_control_node(state))
        se.execute_sequence.assert_awaited_once()
        assert result.answer == "On it!"
        assert "route_control" in result.node_timings

    def test_sequence_intent_no_steps_falls_through_to_normal(self):
        sc = _make_smart_controller()
        sc.detect_sequence_intent.return_value = True
        sc.extract_sequence_intent = AsyncMock(return_value={"steps": [], "acknowledge": "Starting..."})
        _runtime.set_smart_controller(sc)
        _runtime.set_sequence_executor(None)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        state = _make_state(query="turn lights on")
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.detect_status_query_type", return_value=False),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
            patch("orchestrator.nodes.route_control.store_conversation_context", new_callable=AsyncMock),
        ):
            result = _run(route_control_node(state))
        # Falls through: extract_intent called for normal path
        sc.extract_intent.assert_awaited()

    def test_sequence_intent_executor_none_returns_unavailable_message(self):
        sc = _make_smart_controller()
        sc.detect_sequence_intent.return_value = True
        sc.extract_sequence_intent = AsyncMock(return_value={
            "steps": [{"action": "turn_on"}],
            "acknowledge": "Starting...",
        })
        _runtime.set_smart_controller(sc)
        _runtime.set_sequence_executor(None)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        state = _make_state(query="run the morning routine")
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.detect_status_query_type", return_value=False),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
        ):
            result = _run(route_control_node(state))
        assert result.answer == "Sequence executor not available."


class TestContextContinuation:
    def setup_method(self):
        _runtime.reset_for_test()

    def _setup_basic(self):
        sc = _make_smart_controller()
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        return sc

    def test_inquiry_followup_answered_from_context(self):
        sc = self._setup_basic()
        prev = {
            "response": "I turned on the lights.",
            "entities": {"room": "bedroom"},
            "parameters": {"action": "turn_on"},
        }
        state = _make_state(
            query="what did you just do?",
            prev_context=prev,
            context_ref_info={"is_inquiry": True},
        )
        with patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                   return_value={"enabled": False}):
            result = _run(route_control_node(state))
        assert "bedroom" in result.answer or "turned on" in result.answer.lower()
        assert "route_control" in result.node_timings

    def test_context_continuation_merges_previous_intent(self):
        sc = self._setup_basic()
        sc.extract_intent = AsyncMock(return_value={
            "device_type": "light",
            "action": "turn_on",
            "room": "kitchen",
        })
        sc.execute_intent = AsyncMock(return_value="Done! Kitchen lights on.")
        prev = {
            "response": "Bedroom lights off.",
            "entities": {"room": "bedroom", "device_type": "light"},
            "parameters": {"device_type": "light", "action": "turn_off"},
            "query": "turn off bedroom lights",
        }
        state = _make_state(
            query="do the same in the kitchen",
            prev_context=prev,
            context_ref_info={"has_context_ref": True},
        )
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
            patch("orchestrator.nodes.route_control.store_conversation_context", new_callable=AsyncMock),
        ):
            result = _run(route_control_node(state))
        assert result.answer == "Done! Kitchen lights on."

    def test_context_reversal_back_on(self):
        sc = self._setup_basic()
        sc.extract_intent = AsyncMock(return_value={"device_type": "light", "action": "turn_off", "room": "bedroom"})
        sc.execute_intent = AsyncMock(return_value="Done!")
        prev = {
            "response": "Bedroom off.",
            "entities": {"room": "bedroom"},
            "parameters": {"device_type": "light", "action": "turn_off"},
            "query": "turn off bedroom",
        }
        state = _make_state(
            query="turn them back on",
            prev_context=prev,
            context_ref_info={"has_context_ref": True},
        )
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
            patch("orchestrator.nodes.route_control.store_conversation_context", new_callable=AsyncMock),
        ):
            result = _run(route_control_node(state))
        # execute_intent called with intent that has action==turn_on (reversal applied)
        call_intent = sc.execute_intent.call_args[0][0]
        assert call_intent.get("action") == "turn_on"

    def test_brightness_modifier_brighter(self):
        sc = self._setup_basic()
        sc.extract_intent = AsyncMock(return_value={
            "device_type": "light", "action": "set_brightness",
            "room": "office", "parameters": {"brightness": 150},
        })
        sc.execute_intent = AsyncMock(return_value="Brighter!")
        prev = {
            "response": "Office lights at 150.",
            "entities": {"room": "office"},
            "parameters": {"device_type": "light", "action": "set_brightness", "parameters": {"brightness": 150}},
            "query": "set office lights to 150",
        }
        state = _make_state(
            query="make them brighter",
            prev_context=prev,
            context_ref_info={"has_context_ref": True, "ref_types": ["modifier"]},
        )
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
            patch("orchestrator.nodes.route_control.store_conversation_context", new_callable=AsyncMock),
        ):
            result = _run(route_control_node(state))
        call_intent = sc.execute_intent.call_args[0][0]
        # brightness should be bumped up from 150 by 50
        assert call_intent.get("parameters", {}).get("brightness", 0) == 200
        assert call_intent.get("action") == "set_brightness"


class TestNormalExtraction:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_normal_intent_extraction_no_context(self):
        sc = _make_smart_controller()
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        state = _make_state(query="turn on the kitchen lights")
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.detect_status_query_type", return_value=False),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
            patch("orchestrator.nodes.route_control.store_conversation_context", new_callable=AsyncMock),
        ):
            result = _run(route_control_node(state))
        sc.extract_intent.assert_awaited_once_with("turn on the kitchen lights", device_room="living room")
        assert result.answer == "Done! Lights on."
        assert result.retrieved_data == {"intent": {"device_type": "light", "action": "turn_on", "room": "living room"}}

    def test_store_conversation_context_called_on_success(self):
        sc = _make_smart_controller()
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        state = _make_state(query="turn on the lights", session_id="test-session")
        mock_store = AsyncMock()
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.detect_status_query_type", return_value=False),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
            patch("orchestrator.nodes.route_control.store_conversation_context", mock_store),
        ):
            _run(route_control_node(state))
        mock_store.assert_awaited_once()
        call_kwargs = mock_store.call_args[1]
        assert call_kwargs["session_id"] == "test-session"
        assert call_kwargs["intent"] == "control"


class TestFallbackBranch:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_fallback_turn_on_via_ha_client(self):
        _runtime.set_smart_controller(None)
        ha = MagicMock()
        ha.call_service = AsyncMock(return_value={"result": "ok"})
        _runtime.set_ha_client(ha)
        state = _make_state(
            query="turn on the office light",
            entities={"device": "light.office"},
        )
        with patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                   return_value={"enabled": False}):
            result = _run(route_control_node(state))
        ha.call_service.assert_awaited_once()
        assert "turned on" in result.answer.lower() or "done" in result.answer.lower()

    def test_fallback_entity_permission_denied(self):
        _runtime.set_smart_controller(None)
        ha = MagicMock()
        ha.call_service = AsyncMock(return_value={"result": "ok"})
        _runtime.set_ha_client(ha)
        state = _make_state(
            query="turn on the office light",
            entities={"device": "light.office"},
            mode="guest",
            permissions={"restricted_entities": ["light.office"]},
        )
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.check_entity_permission", return_value=False),
        ):
            result = _run(route_control_node(state))
        ha.call_service.assert_not_awaited()
        assert result.error == "permission_denied"
        assert "permission" in result.answer.lower()


class TestErrorHandlingAndMetrics:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_outer_exception_sets_error_and_fallback_answer(self):
        sc = MagicMock()
        sc.detect_sequence_intent.return_value = False
        sc.extract_intent = AsyncMock(side_effect=RuntimeError("LLM down"))
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        state = _make_state(query="turn on lights")
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.detect_status_query_type", return_value=False),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
        ):
            result = _run(route_control_node(state))
        assert result.error == "LLM down"
        assert "error" in result.answer.lower()
        assert "route_control" in result.node_timings

    def test_timing_tracker_called(self):
        sc = _make_smart_controller()
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        tracker = MagicMock()
        state = _make_state(query="turn on lights", timing_tracker=tracker)
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.detect_status_query_type", return_value=False),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
            patch("orchestrator.nodes.route_control.store_conversation_context", new_callable=AsyncMock),
        ):
            _run(route_control_node(state))
        tracker.track_sync.assert_called_once_with("graph", "route_control", mock.ANY)

    def test_node_timings_set_on_every_return_path(self):
        """Sensor fast-path exits early — node_timings must still be set."""
        sc = _make_smart_controller()
        _runtime.set_smart_controller(sc)
        state = _make_state(
            query="what is the humidity?",
            entities={"device_type": "sensor"},
        )
        with patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                   return_value={"enabled": False}):
            result = _run(route_control_node(state))
        assert isinstance(result.node_timings.get("route_control"), float)

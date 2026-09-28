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

from orchestrator import mode_permission
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
        state = _make_state(query="turn on kitchen then dim to 50% after 1 minute", permissions={"mode": "owner"})
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
        state = _make_state(query="run the morning routine", permissions={"mode": "owner"})
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

    def test_guest_sequence_with_lock_step_refused_before_scheduling(self):
        """D21: a sequence containing a denied step is refused synchronously,
        before execute_sequence is ever called -- a background sequence
        can't be scheduled with authorization it doesn't have."""
        sc = _make_smart_controller()
        sc.detect_sequence_intent.return_value = True
        sc.extract_sequence_intent = AsyncMock(return_value={
            "steps": [
                {"target": {"entity_id": "light.kitchen"}, "action": "turn_on"},
                {"target": {"entity_id": "lock.front_door"}, "action": "unlock"},
            ],
            "acknowledge": "On it!",
        })
        se = MagicMock()
        se.execute_sequence = AsyncMock(return_value="done")
        _runtime.set_smart_controller(sc)
        _runtime.set_sequence_executor(se)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        state = _make_state(
            query="turn on the kitchen lights then unlock the front door",
            mode="guest",
            permissions={"mode": "guest", "allowed_intents": ["control"]},
        )
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.detect_status_query_type", return_value=False),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
        ):
            result = _run(route_control_node(state))
        se.execute_sequence.assert_not_awaited()
        assert result.error == "permission_denied"
        assert "schedule" in result.answer.lower()
        assert "guest mode" in result.answer.lower()


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


class TestNoSmartController:
    """D12: the dead/broken 'no smart controller' fallback branch was
    deleted (route_control.py:420-459 at base) -- there is no HA call
    possible without the smart controller, so this now just says so."""

    def setup_method(self):
        _runtime.reset_for_test()

    def test_no_smart_controller_returns_not_configured(self):
        _runtime.set_smart_controller(None)
        state = _make_state(query="turn on the office light")
        with patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                   return_value={"enabled": False}):
            result = _run(route_control_node(state))
        assert result.answer == "Home automation isn't configured."
        assert result.error == "ha_not_configured"


class TestHAPermissionGating:
    """ATHENA-69 (D9/D14): the intent gate, coarse domain pre-check, and
    post-call denial surfacing wired into route_control_node's smart-
    controller dispatch."""

    def setup_method(self):
        _runtime.reset_for_test()

    def _guest_permissions(self, **overrides):
        perms = mode_permission.apply_guest_baseline({"mode": "guest"})
        perms.update(overrides)
        return perms

    def test_smart_controller_guest_lock_denied_before_execute(self):
        sc = _make_smart_controller()
        sc.extract_intent = AsyncMock(return_value={"device_type": "lock", "action": "unlock", "room": "front"})
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        state = _make_state(
            query="unlock the front door",
            mode="guest",
            # allowed_intents includes "control" so this exercises the
            # entity/domain-level coarse pre-check, not the intent gate.
            permissions=self._guest_permissions(allowed_intents=["control"]),
        )
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
        ):
            result = _run(route_control_node(state))
        sc.execute_intent.assert_not_awaited()
        assert result.error == "permission_denied"
        assert "guest mode" in result.answer.lower()
        assert "lock" in result.answer.lower()

    def test_smart_controller_owner_lock_proceeds(self):
        sc = _make_smart_controller()
        sc.extract_intent = AsyncMock(return_value={"device_type": "lock", "action": "unlock", "room": "front"})
        sc.execute_intent = AsyncMock(return_value="Done! Front door unlocked.")
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        state = _make_state(query="unlock the front door", mode="owner", permissions={"mode": "owner"})
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
            patch("orchestrator.nodes.route_control.store_conversation_context", new_callable=AsyncMock),
        ):
            result = _run(route_control_node(state))
        sc.execute_intent.assert_awaited_once()
        assert result.answer == "Done! Front door unlocked."
        assert result.error is None

    def test_guest_control_intent_denied_skips_extraction(self):
        sc = _make_smart_controller()
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        # A guest allowlist that doesn't include "control" at all (not an
        # empty list -- an empty list means baseline, not "nothing
        # allowed"; a populated allow-list without "control" is what
        # actually denies the CONTROL intent).
        state = _make_state(
            query="turn off the lights",
            mode="guest",
            permissions=self._guest_permissions(allowed_intents=["weather"]),
        )
        with patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                   return_value={"enabled": False}):
            result = _run(route_control_node(state))
        sc.extract_intent.assert_not_awaited()
        assert result.error == "permission_denied"
        assert result.answer == mode_permission.GUEST_INTENT_REFUSAL

    def test_denial_inside_execute_intent_surfaces_refusal_and_skips_context_store(self):
        sc = _make_smart_controller()
        sc.extract_intent = AsyncMock(return_value={"device_type": "light", "action": "turn_on", "room": "kitchen"})

        async def _execute_intent_with_manual_denial(intent, ha_client_arg, **kwargs):
            scope = mode_permission.current_ha_scope()
            scope.denials.append(mode_permission.HADenial(
                domain="light", service="turn_on", targets=("light.kitchen",), reason="entity_or_domain_denied",
            ))
            scope.halted = True
            return "I turned on the kitchen light."

        sc.execute_intent = AsyncMock(side_effect=_execute_intent_with_manual_denial)
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        mock_store = AsyncMock()
        state = _make_state(
            query="turn on the kitchen light",
            mode="guest",
            permissions=self._guest_permissions(allowed_intents=["control"]),
            session_id="sess-denial",
        )
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
            patch("orchestrator.nodes.route_control.store_conversation_context", mock_store),
        ):
            result = _run(route_control_node(state))
        sc.execute_intent.assert_awaited_once()
        assert result.error == "permission_denied"
        assert result.answer != "I turned on the kitchen light."
        assert "guest mode" in result.answer.lower()
        mock_store.assert_not_awaited()

    def test_bed_warmer_guest_refused_by_multi_domain_precheck(self):
        sc = _make_smart_controller()
        sc.extract_intent = AsyncMock(return_value={"device_type": "bed_warmer", "action": "warm", "room": "master"})
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        state = _make_state(
            query="warm my side of the bed",
            mode="guest",
            permissions=self._guest_permissions(allowed_intents=["control"]),
        )
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
        ):
            result = _run(route_control_node(state))
        sc.execute_intent.assert_not_awaited()
        assert result.error == "permission_denied"

    def test_degraded_owner_whole_house_lights_allowed(self):
        """D4/D14 (bob r2 M2): a degraded scope still lets an owner turn
        off all the lights -- the whole-house write is light-only, and
        light isn't in the D4 fallback's restricted floor."""
        sc = _make_smart_controller()
        sc.extract_intent = AsyncMock(return_value={
            "device_type": "light", "action": "turn_off", "room": "whole_house",
        })

        recording = MagicMock()
        recording.call_service = AsyncMock(return_value={"ok": True})
        guard = mode_permission.PermissionEnforcingHAClient(recording)

        async def _execute_intent_writes_via_guard(intent, ha_client_arg, **kwargs):
            await guard.call_service("light", "turn_off", {"entity_id": "all"})
            return "Good night! I've turned off the lights."

        sc.execute_intent = AsyncMock(side_effect=_execute_intent_writes_via_guard)
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        state = _make_state(
            query="turn off all the lights",
            mode="owner",
            permissions=mode_permission.degraded_permissions(),
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
        recording.call_service.assert_awaited_once_with("light", "turn_off", {"entity_id": "all"})
        assert result.answer == "Good night! I've turned off the lights."
        assert result.error is None

    def test_empty_permissions_are_degraded_not_owner(self):
        sc = _make_smart_controller()
        sc.extract_intent = AsyncMock(return_value={"device_type": "lock", "action": "lock", "room": "front"})
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        state = _make_state(query="lock the front door", mode="owner", permissions={})
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
        ):
            result = _run(route_control_node(state))
        # An empty permissions dict must NOT behave like unrestricted
        # owner -- the lock write is still denied (degraded floor).
        sc.execute_intent.assert_not_awaited()
        assert result.error == "permission_denied"

    def test_old_mode_service_guest_without_floor_is_floored(self):
        """A pre-ATHENA-69 mode service response with no floor patterns at
        all still gets the D8 floor applied by normalize_permissions."""
        sc = _make_smart_controller()
        sc.extract_intent = AsyncMock(return_value={"device_type": "lock", "action": "unlock", "room": "front"})
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        state = _make_state(
            query="unlock the front door",
            mode="guest",
            # allowed_intents explicitly includes "control" so this
            # exercises normalize_permissions' floor application, not the
            # separate (and also-denying) intent gate.
            permissions={"mode": "guest", "restricted_entities": [], "allowed_domains": [], "allowed_intents": ["control"]},
        )
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
        ):
            result = _run(route_control_node(state))
        sc.execute_intent.assert_not_awaited()
        assert result.error == "permission_denied"


class TestErrorHandlingAndMetrics:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_outer_exception_sets_error_and_generic_answer(self):
        """Renamed off the removed '-k fallback' test-name pattern (ATHENA-69
        Pass A deleted the unrelated dead fallback branch and its 3 tests;
        this test covers the outer try/except, a distinct concern, and is
        kept under a name outside that pattern)."""
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


class TestContextRefViewGating:
    """ATHENA-88 / F16 / D13, contract C11: route_control_node reads
    whatever context_ref_info shape it's given -- these tests prove the
    reader honours a DECLINED view (no merge, no inquiry answer) exactly as
    it would honour a raw dict with the flags unset, and that a CONTINUED
    view still merges/modifier-adjusts as today.
    """

    def setup_method(self):
        _runtime.reset_for_test()

    def _setup_basic(self):
        sc = _make_smart_controller()
        _runtime.set_smart_controller(sc)
        _runtime.set_automation_agent(None)
        _runtime.set_entity_manager(None)
        return sc

    def test_declined_view_does_not_merge_previous_context(self):
        sc = self._setup_basic()
        sc.extract_intent = AsyncMock(return_value={
            "device_type": "light", "action": "turn_on", "room": "kitchen",
        })
        sc.execute_intent = AsyncMock(return_value="Kitchen lights on.")
        prev = {
            "response": "Bedroom lights off.",
            "entities": {"room": "bedroom", "device_type": "light"},
            "parameters": {"device_type": "light", "action": "turn_off"},
            "query": "turn off bedroom lights",
        }
        declined_view = {
            "has_context_ref": False, "is_continuation": False,
            "is_inquiry": False, "ref_types": [], "anaphora_types": [],
        }
        state = _make_state(
            query="turn on the kitchen lights",
            prev_context=prev,
            context_ref_info=declined_view,
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
        # extract_intent called with NO prev_query/prev_response context —
        # a fresh command, not a follow-up merge.
        call_kwargs = sc.extract_intent.call_args.kwargs
        assert not call_kwargs.get("prev_query")
        assert result.answer == "Kitchen lights on."

    def test_declined_view_skips_inquiry_answer(self):
        sc = self._setup_basic()
        prev = {
            "response": "I turned on the lights.",
            "entities": {"room": "bedroom"},
            "parameters": {"action": "turn_on"},
        }
        declined_view = {
            "has_context_ref": False, "is_continuation": False,
            "is_inquiry": False, "ref_types": [], "anaphora_types": [],
        }
        state = _make_state(
            query="turn on the office lights",
            prev_context=prev,
            context_ref_info=declined_view,
        )
        sc.extract_intent = AsyncMock(return_value={
            "device_type": "light", "action": "turn_on", "room": "office",
        })
        sc.execute_intent = AsyncMock(return_value="Office lights on.")
        with (
            patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
                  return_value={"enabled": False}),
            patch("orchestrator.nodes.route_control.get_automation_system_mode",
                  new_callable=AsyncMock, return_value="pattern"),
            patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
            patch("orchestrator.nodes.route_control.store_conversation_context", new_callable=AsyncMock),
        ):
            result = _run(route_control_node(state))
        # Not answered from prev_context's inquiry branch.
        assert result.answer == "Office lights on."

    def test_continued_modifier_view_bumps_brightness_by_50(self):
        """[r3] 'brighter' with prev control (brightness=200) and the
        continued view -> ref_types has 'modifier', and the
        route_control.py modifier branch sets action=set_brightness,
        brightness=250. Pins that D13's view keeps ref_types for continued
        turns (reader 7 lists ref_types)."""
        sc = self._setup_basic()
        sc.extract_intent = AsyncMock(return_value={
            "device_type": "light", "action": "set_brightness",
            "room": "office", "parameters": {"brightness": 200},
        })
        sc.execute_intent = AsyncMock(return_value="Brighter!")
        prev = {
            "response": "Office at 200.",
            "entities": {"room": "office"},
            "parameters": {"device_type": "light", "action": "set_brightness", "parameters": {"brightness": 200}},
            "query": "set office to 200",
        }
        continued_view = {"has_context_ref": True, "ref_types": ["modifier"], "is_inquiry": False}
        state = _make_state(
            query="brighter",
            prev_context=prev,
            context_ref_info=continued_view,
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
        assert call_intent.get("action") == "set_brightness"
        assert call_intent.get("parameters", {}).get("brightness") == 250

    def test_declined_view_skips_modifier_branch(self):
        """The same query/prev_context as the continued-modifier row, but
        with a declined view -- the modifier branch must not fire; a fresh
        command is extracted instead."""
        sc = self._setup_basic()
        sc.extract_intent = AsyncMock(return_value={
            "device_type": "light", "action": "turn_on", "room": "kitchen",
        })
        sc.execute_intent = AsyncMock(return_value="Done")
        prev = {
            "response": "Office at 200.",
            "entities": {"room": "office"},
            "parameters": {"device_type": "light", "action": "set_brightness", "parameters": {"brightness": 200}},
            "query": "set office to 200",
        }
        declined_view = {
            "has_context_ref": False, "is_continuation": False,
            "is_inquiry": False, "ref_types": [], "anaphora_types": [],
        }
        state = _make_state(
            query="brighter",
            prev_context=prev,
            context_ref_info=declined_view,
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
        assert call_intent.get("action") == "turn_on"
        assert call_intent.get("parameters", {}).get("brightness") is None

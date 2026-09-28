"""route_control_node — extracted from orchestrator.main during Phase 3.2 (ATHENA-10).

Routes home-automation control commands through Home Assistant: sensor fast-paths,
status-query bulk optimisation, dynamic-agent / sequence / context-continuation
routing, and smart-controller intent execution.

Originally a byte-identical move; ATHENA-69 (D9, D12, D14) wraps the smart-
controller dispatch in a per-request ha_permission_scope, adds a CONTROL
intent gate and a coarse per-device-type domain pre-check before
execute_intent, surfaces any denial recorded on the scope as the answer
(D2), and deletes the dead/broken "no smart controller" fallback branch
(D12) in favor of a "not configured" answer. See
.mozart/plans/active/2026-09-28-deliver-athena-ha-permission-gap.md.
"""
from __future__ import annotations

import time
from typing import Optional

import structlog

from orchestrator.nodes._runtime import (
    get_automation_agent,
    get_entity_manager,
    get_ha_client,
    get_sequence_executor,
    get_smart_controller,
)
from orchestrator.state import IntentCategory, OrchestratorState
from orchestrator.helpers import (
    get_automation_system_mode,
    get_feature_config,
    store_conversation_context,
)
from orchestrator.mode_permission import (
    DEGRADED_INTENT_REFUSAL,
    GUEST_INTENT_REFUSAL,
    authorize_ha_write,
    authorize_sequence,
    check_intent_permission,
    ha_permission_scope,
    intent_write_domains,
    permission_refusal_message,
    sequence_refusal_message,
)
from orchestrator.ha_status_optimizer import (
    detect_status_query_type,
    optimize_status_query,
    should_skip_synthesis,
)
from orchestrator.automation_agent import should_use_automation_agent
from orchestrator.mode_permission import READ_ONLY_REFUSAL
from orchestrator.metrics import state_question_routed_total
from orchestrator.utterance_kind import (
    UtteranceKind,
    classify_utterance,
)
from orchestrator.utterance_kind import UNKNOWN_CLASSIFICATION as _KILL_SWITCH_CLASSIFICATION

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Proxy shims — forward bare-global attribute access to _runtime singletons
# ---------------------------------------------------------------------------

class _SmartControllerProxy:
    """Forward attribute access to the runtime smart controller at call time.

    route_control_node references ``smart_controller`` as a bare module global.
    The actual controller is registered in _runtime by main.py's lifespan, so
    we cannot bind it at import time.  This proxy resolves the current value on
    every attribute lookup, keeping the function body byte-identical.
    """

    def __bool__(self) -> bool:
        return get_smart_controller() is not None

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_smart_controller(), name)


class _SequenceExecutorProxy:
    """Forward attribute access to the runtime sequence executor at call time."""

    def __bool__(self) -> bool:
        return get_sequence_executor() is not None

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_sequence_executor(), name)


class _AutomationAgentProxy:
    """Forward attribute access to the runtime automation agent at call time."""

    def __bool__(self) -> bool:
        return get_automation_agent() is not None

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_automation_agent(), name)


class _HAClientProxy:
    """Forward attribute access to the runtime HA client at call time."""

    def __bool__(self) -> bool:
        return get_ha_client() is not None

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_ha_client(), name)


class _EntityManagerProxy:
    """Forward attribute access to the runtime entity manager at call time."""

    def __bool__(self) -> bool:
        return get_entity_manager() is not None

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_entity_manager(), name)


smart_controller = _SmartControllerProxy()
sequence_executor = _SequenceExecutorProxy()
automation_agent = _AutomationAgentProxy()
ha_client = _HAClientProxy()
entity_manager = _EntityManagerProxy()


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def route_control_node(state: OrchestratorState) -> OrchestratorState:
    """
    Handle home automation control commands via Home Assistant API.
    Uses LLM-based intent extraction and dynamic entity discovery.
    Supports context continuation for follow-up commands.
    """
    start = time.time()

    try:
        # FAST PATH: Check if this is a sensor/occupancy query from fast-path classification
        # If entities already indicate sensor, go directly to sensor handler
        if state.entities and state.entities.get("device_type") == "sensor":
            logger.info(f"Fast path sensor query detected, routing to sensor handler")
            if smart_controller:
                result = await smart_controller._handle_sensor_intent(
                    "sensor",
                    state.entities.get("parameters", {}),
                    state.query
                )
                state.answer = result
                state.node_timings["route_control"] = time.time() - start
                return state

        # PRESENCE/OCCUPANCY QUERY DETECTION - Pattern-based fast path
        # These queries should go to sensor handler, not LLM extraction
        query_lower = state.query.lower()
        presence_patterns = [
            "anyone home", "anybody home", "someone home", "who's home", "who is home",
            "is anyone", "is anybody", "is someone", "anyone there", "anybody there",
            # Round 15: "is there anybody in the basement"
            "anybody in", "anyone in", "someone in", "somebody in",
            "is there anybody", "is there anyone", "is there someone",
            "someone was home", "anyone was home", "anybody was home",
            "last time someone", "last time anyone", "last time somebody",
            "when was someone", "when was anyone", "when was the last",
            "last motion", "last movement", "last activity",
            "recent motion", "recent activity", "who was home", "who was here",
            "occupancy", "is the house empty", "house empty", "home empty"
        ]
        if any(p in query_lower for p in presence_patterns):
            logger.info(f"Presence/occupancy query detected via pattern matching: {state.query[:50]}...")
            if smart_controller:
                result = await smart_controller._handle_sensor_intent(
                    "sensor",
                    {"query_type": "presence"},
                    state.query
                )
                state.answer = result
                state.node_timings["route_control"] = time.time() - start
                return state

        # ATHENA-128 (D15, 3.2(a)): classify once, before the bulk block.
        # ``real_uk`` is what the gate reads (scope.utterance) -- always
        # the true classification, even when the kill switch is on, so the
        # IMPERATIVE/explicit-scope exemptions keep working. ``uk`` drives
        # every OTHER routing decision in this node and is substituted to
        # UNKNOWN when the kill switch is enabled (legacy behaviour).
        kill_switch_config = await get_feature_config("state_question_routing_kill_switch")
        state_question_kill_switch = kill_switch_config.get("enabled", False)
        real_uk = classify_utterance(state.query)
        uk = _KILL_SWITCH_CLASSIFICATION if state_question_kill_switch else real_uk

        # HA STATUS QUERY OPTIMIZATION (2026-01-12)
        # Detect status queries and use optimized bulk HA state queries
        # This saves 1-3 seconds by avoiding per-entity queries and LLM synthesis
        status_bulk_config = await get_feature_config("status_bulk_query")
        status_skip_config = await get_feature_config("status_skip_synthesis")

        # ATHENA-128 (D5, 3.2(b)): only a room-less, non-referent
        # STATE_QUESTION reaches the bulk optimizer. Legacy gating
        # (detect_status_query_type alone) returns when the kill switch is
        # on.
        if state_question_kill_switch:
            bulk_eligible = detect_status_query_type(state.query)
        else:
            bulk_eligible = (
                uk.kind == UtteranceKind.STATE_QUESTION
                and not uk.room
                and not uk.needs_referent
                and detect_status_query_type(state.query)
            )

        if status_bulk_config.get("enabled", True) and bulk_eligible:
            try:
                # Check if we have HA entity access via global entity_manager
                if entity_manager:
                    status_start = time.time()

                    # Use optimized status query handler
                    status_result = await optimize_status_query(
                        state.query,
                        entity_manager=entity_manager,
                        feature_config=status_bulk_config.get("config", {})
                    )

                    if status_result:
                        # Check if we should skip synthesis
                        skip_synthesis_enabled = status_skip_config.get("enabled", True)
                        should_skip, templated_response = should_skip_synthesis(
                            status_result,
                            feature_enabled=skip_synthesis_enabled
                        )

                        if should_skip and templated_response:
                            # Return templated response directly - skip LLM synthesis
                            state.answer = templated_response
                            state.skip_synthesis = True
                            status_duration = time.time() - status_start
                            state_question_routed_total.labels(
                                device_type=uk.device_type or "unknown", path="bulk_optimizer"
                            ).inc()

                            logger.info(
                                "status_query_optimized",
                                query=state.query[:50],
                                query_type=status_result.query_type,
                                entity_count=len(status_result.entities),
                                skip_synthesis=True,
                                duration_ms=round(status_duration * 1000, 1)
                            )

                            state.node_timings["route_control"] = time.time() - start
                            return state
                        else:
                            # Low confidence or synthesis needed - store raw states for LLM
                            state.context = state.context or {}
                            state.context["ha_status_data"] = {
                                "query_type": status_result.query_type,
                                "entities": status_result.entities,
                                "raw_states": status_result.raw_states
                            }
                            logger.info(
                                "status_query_bulk_loaded",
                                query_type=status_result.query_type,
                                entity_count=len(status_result.entities)
                            )
                            # Fall through to normal processing with pre-loaded data
            except Exception as e:
                logger.warning(f"Status query optimization failed, falling back: {e}")

        # Use smart controller for LLM-based intent extraction and execution
        if smart_controller:
            with ha_permission_scope(
                state.permissions,
                mode=state.mode,
                request_id=state.request_id,
                session_id=state.session_id,
                read_only=(uk.kind == UtteranceKind.STATE_QUESTION),
                utterance=real_uk,
            ) as scope:
                # Intent gate (D9): refuse CONTROL before any dispatch --
                # the automation agent, sequence detection, or intent
                # extraction never run for a denied intent.
                if not check_intent_permission(IntentCategory.CONTROL, scope.permissions):
                    state.answer = (
                        DEGRADED_INTENT_REFUSAL if scope.permissions.get("mode") == "degraded"
                        else GUEST_INTENT_REFUSAL
                    )
                    state.error = "permission_denied"
                    logger.warning(
                        "control_request_denied",
                        intent="control",
                        mode=scope.permissions.get("mode"),
                        request_id=state.request_id,
                        session_id=state.session_id,
                    )
                    state.node_timings["route_control"] = time.time() - start
                    return state

                # ATHENA-128 (D6, 3.2(d)): state-question dispatch. A
                # non-referent STATE_QUESTION is answered with a get_status
                # read here, under the read-only scope opened above, before
                # the automation agent / sequence / continuation / LLM
                # extraction chain ever runs. needs_referent=True questions
                # (e.g. "did those come back on?") fall through to that
                # chain unchanged -- still under the same read-only scope.
                if uk.kind == UtteranceKind.STATE_QUESTION and not uk.needs_referent:
                    state_question_intent = {
                        "device_type": uk.device_type or "light",
                        "room": uk.room,
                        "action": "get_status",
                        "target_scope": "group",
                        "parameters": {},
                    }
                    sq_denials_before = len(scope.denials)
                    sq_result = await smart_controller.execute_intent(
                        state_question_intent, ha_client, original_query=state.query, device_room=state.room
                    )
                    if len(scope.denials) > sq_denials_before and any(
                        d.reason == "read_only_scope" for d in scope.denials[sq_denials_before:]
                    ):
                        state.answer = READ_ONLY_REFUSAL
                        state.error = "state_question_write_blocked"
                        logger.error(
                            "state_question_write_blocked",
                            query=state.query[:80],
                            device_type=uk.device_type,
                            room=uk.room,
                            request_id=state.request_id,
                            session_id=state.session_id,
                        )
                        state_question_routed_total.labels(
                            device_type=uk.device_type or "unknown", path="write_blocked"
                        ).inc()
                    else:
                        state.answer = sq_result
                        state.retrieved_data = {"intent": state_question_intent}
                        state_question_routed_total.labels(
                            device_type=uk.device_type or "unknown", path="get_status_dispatch"
                        ).inc()
                        if state.session_id and "couldn't" not in sq_result.lower():
                            await store_conversation_context(
                                session_id=state.session_id,
                                intent="control",
                                query=state.query,
                                entities={"room": uk.room, "device_type": uk.device_type or "light"},
                                parameters=state_question_intent,
                                response=sq_result,
                                ttl=300,
                            )
                    state.node_timings["route_control"] = time.time() - start
                    return state

                # AUTOMATION SYSTEM MODE: Check if we should use dynamic agent vs pattern matching
                automation_mode = await get_automation_system_mode()

                # DYNAMIC AGENT: Route sequences/automations to LLM-based agent
                if automation_mode == "dynamic_agent" and automation_agent and should_use_automation_agent(state.query):
                    logger.info(f"Dynamic agent mode - routing to automation agent: {state.query[:50]}...")

                    # Build context for automation agent
                    context = {
                        "room": state.room,
                        "mode": state.mode,
                        "session_id": state.session_id,
                        "guest_name": getattr(state, 'guest_name', None),
                        "guest_session_id": getattr(state, 'guest_session_id', None),
                    }

                    # Execute via automation agent (D2: surface any denial
                    # recorded on the scope during the call as the answer,
                    # instead of whatever automation_agent.execute returned)
                    denials_before = len(scope.denials)
                    writes_before = scope.allowed_writes
                    result = await automation_agent.execute(
                        query=state.query,
                        context=context,
                        model="llama3.1:8b"  # Use capable model for automation
                    )
                    if len(scope.denials) > denials_before:
                        denied_domains = tuple(
                            d.domain for d in scope.denials[denials_before:]
                            if d.reason != "halted_after_denial"
                        )
                        state.answer = permission_refusal_message(
                            denied_domains, scope, partial=scope.allowed_writes > writes_before
                        )
                        state.error = "permission_denied"
                    else:
                        state.answer = result
                    state.node_timings["route_control"] = time.time() - start
                    return state

                # PATTERN MATCHING: Check if this is a multi-step command with delays/loops/scheduling
                if smart_controller.detect_sequence_intent(state.query):
                    logger.info(f"Sequence intent detected (pattern matching mode): {state.query[:50]}...")

                    # Extract sequence from the complex command
                    sequence_data = await smart_controller.extract_sequence_intent(
                        state.query,
                        device_room=state.room
                    )

                    if sequence_data and sequence_data.get("steps"):
                        steps = sequence_data["steps"]
                        acknowledge = sequence_data.get("acknowledge", "Starting sequence...")

                        # Sequence pre-authorization (D21): deny the whole
                        # sequence synchronously, before scheduling, when
                        # any step isn't allowed under the CURRENT scope --
                        # a background sequence otherwise fires later steps
                        # with the authorization the request had when it
                        # was scheduled, so a denied step must never be
                        # scheduled in the first place.
                        seq_decision = authorize_sequence(steps, scope.permissions)
                        if not seq_decision.allowed:
                            state.answer = sequence_refusal_message(seq_decision, scope)
                            state.error = "permission_denied"
                            state.node_timings["route_control"] = time.time() - start
                            return state

                        logger.info(f"Executing sequence with {len(steps)} steps")

                        # Execute sequence in background - return acknowledgment immediately
                        if sequence_executor:
                            denials_before = len(scope.denials)
                            writes_before = scope.allowed_writes
                            result = await sequence_executor.execute_sequence(
                                steps,
                                session_id=state.session_id,
                                background=True
                            )
                            if len(scope.denials) > denials_before:
                                denied_domains = tuple(
                                    d.domain for d in scope.denials[denials_before:]
                                    if d.reason != "halted_after_denial"
                                )
                                state.answer = permission_refusal_message(
                                    denied_domains, scope, partial=scope.allowed_writes > writes_before
                                )
                                state.error = "permission_denied"
                            else:
                                state.answer = acknowledge
                        else:
                            state.answer = "Sequence executor not available."

                        state.node_timings["route_control"] = time.time() - start
                        return state

                # Check if we have previous context from classify_node
                has_context = state.prev_context is not None
                ref_info = state.context_ref_info or {}

                # Handle inquiry follow-ups - return info about previous action instead of executing
                if has_context and ref_info.get("is_inquiry"):
                    prev = state.prev_context
                    prev_response = prev.get("response", "")
                    prev_entities = prev.get("entities", {})
                    prev_room = prev_entities.get("room", "unknown")
                    prev_action = prev.get("parameters", {}).get("action", "")

                    # Generate conversational response about what was done
                    if prev_room and prev_response:
                        state.answer = f"I {prev_action.replace('_', 'ed ').replace('turn_', 'turned ')} the {prev_room} lights. {prev_response}"
                    else:
                        state.answer = prev_response or "I performed the action you requested."

                    logger.info(f"Inquiry follow-up answered from context: room={prev_room}, action={prev_action}")
                    state.node_timings["route_control"] = time.time() - start
                    return state

                if has_context and ref_info.get("has_context_ref"):
                    # Use previous context to resolve the command
                    prev = state.prev_context
                    prev_params = prev.get("parameters", {})
                    prev_query = prev.get("query", "")
                    prev_response = prev.get("response", "")
                    prev_entities = prev.get("entities", {})

                    # Merge entities and parameters for full context
                    # prev_params is the full intent, prev_entities has room/device_type
                    prev_intent_for_llm = prev_params.copy() if prev_params else {}
                    if prev_entities:
                        prev_intent_for_llm.update(prev_entities)

                    # Extract intent with conversation context for corrections/follow-ups
                    # e.g., "no, just my side" after "Warming bed on both sides at level 3"
                    new_intent = await smart_controller.extract_intent(
                        state.query,
                        device_room=state.room,
                        prev_query=prev_query,
                        prev_response=prev_response,
                        prev_intent_entities=prev_intent_for_llm,
                        utterance=uk,
                    )
                    new_room = new_intent.get('room')

                    # Merge previous context with new info
                    # Start with previous parameters as base
                    intent = prev_params.copy() if prev_params else {}

                    # If new intent has meaningful data, merge it (preserving previous params not overwritten)
                    if new_intent.get('device_type') and new_intent.get('action'):
                        # Merge parameters: start with previous, update with new
                        prev_params_dict = intent.get('parameters', {}) if isinstance(intent.get('parameters'), dict) else {}
                        new_params_dict = new_intent.get('parameters', {}) if isinstance(new_intent.get('parameters'), dict) else {}
                        merged_params = {**prev_params_dict, **new_params_dict}

                        # Now merge the intent itself
                        intent.update(new_intent)
                        intent['parameters'] = merged_params
                        logger.info(f"LLM interpreted follow-up with context: {intent}")

                    # If new room specified, use it; otherwise keep previous room
                    if new_room:
                        intent['room'] = new_room
                        logger.info(f"Context continuation - applying previous command to new room: {new_room}")
                    elif prev.get("entities", {}).get("room"):
                        intent['room'] = prev["entities"]["room"]

                    # Handle reversal patterns - "turn them back on", "turn it back off"
                    # ATHENA-128 (3.2(e)): skipped for STATE_QUESTION -- a
                    # referent question ("did those come back on?") must
                    # never have its coerced get_status action clobbered
                    # into a write by this override.
                    query_lower = state.query.lower()
                    if uk.kind != UtteranceKind.STATE_QUESTION:
                        if "back on" in query_lower or "on again" in query_lower:
                            intent["action"] = "turn_on"
                            logger.info("Context reversal: detected 'back on' - setting action to turn_on")
                        elif "back off" in query_lower or "off again" in query_lower:
                            intent["action"] = "turn_off"
                            logger.info("Context reversal: detected 'back off' - setting action to turn_off")

                    # Handle modifier-based adjustments (ATHENA-128: also
                    # skipped for STATE_QUESTION, same rationale)
                    if uk.kind != UtteranceKind.STATE_QUESTION and "modifier" in ref_info.get("ref_types", []):
                        if "brighter" in query_lower:
                            # Increase brightness
                            current_brightness = intent.get("parameters", {}).get("brightness", 200)
                            intent.setdefault("parameters", {})["brightness"] = min(255, current_brightness + 50)
                            intent["action"] = "set_brightness"
                        elif "dimmer" in query_lower:
                            # Decrease brightness
                            current_brightness = intent.get("parameters", {}).get("brightness", 200)
                            intent.setdefault("parameters", {})["brightness"] = max(50, current_brightness - 50)
                            intent["action"] = "set_brightness"
                        elif "different color" in query_lower or "another color" in query_lower:
                            # Re-extract to get new colors with context
                            intent = await smart_controller.extract_intent(
                                state.query + " different colors",
                                device_room=state.room,
                                prev_query=prev_query,
                                prev_response=prev_response,
                                prev_intent_entities=prev_intent_for_llm,
                                utterance=uk,
                            )
                            if prev.get("entities", {}).get("room"):
                                intent['room'] = prev["entities"]["room"]
                        logger.info(f"Modifier adjustment applied: {ref_info.get('ref_types')}")

                    # Ensure we have required fields
                    if not intent.get('device_type'):
                        intent['device_type'] = prev_params.get('device_type', 'light')
                    if not intent.get('action'):
                        intent['action'] = 'get_status' if uk.kind == UtteranceKind.STATE_QUESTION else prev_params.get('action', 'set_color')

                    # ATHENA-128 (bob r2 (5)): forced after the whole merge
                    # -- covers the case where the LLM's intent lacked
                    # device_type (so the :505 merge was skipped entirely)
                    # and prev_params['action'] (a write) survived into
                    # `intent` from the :502 copy.
                    if uk.kind == UtteranceKind.STATE_QUESTION:
                        intent["action"] = "get_status"
                else:
                    # Normal extraction - no context continuation
                    # Pass device room for context when query doesn't specify room
                    intent = await smart_controller.extract_intent(state.query, device_room=state.room, utterance=uk)

                logger.info(f"Extracted intent: {intent}")

                # Execute the intent with permission checking
                device_type = intent.get('device_type', 'light')
                room = intent.get('room')

                # Coarse domain pre-check (D14): refuse before execute_intent
                # when the intent's device_type would write a domain this
                # scope isn't authorized for. This is defense-in-depth --
                # per-entity patterns still pass this coarse check and are
                # enforced by the guard's authorize_ha_write once it's wired
                # into lifespan (Pass B).
                expected_domains = intent_write_domains(intent)
                precheck_denied_domains = [
                    d for d in expected_domains
                    if not authorize_ha_write(d, "_precheck", None, scope.permissions).allowed
                ]
                if precheck_denied_domains:
                    state.answer = permission_refusal_message(precheck_denied_domains, scope)
                    state.error = "permission_denied"
                    state.node_timings["route_control"] = time.time() - start
                    return state

                # Execute the command (pass original query for fallback room extraction, and device_room for context)
                denials_before = len(scope.denials)
                writes_before = scope.allowed_writes
                result = await smart_controller.execute_intent(intent, ha_client, original_query=state.query, device_room=state.room)

                if len(scope.denials) > denials_before:
                    denied_domains = tuple(
                        d.domain for d in scope.denials[denials_before:]
                        if d.reason != "halted_after_denial"
                    )
                    state.answer = permission_refusal_message(
                        denied_domains, scope, partial=scope.allowed_writes > writes_before
                    )
                    # ATHENA-128 (3.2(g)): a referent question (e.g. "did
                    # those come back on?") is dispatched through this same
                    # extraction chain under the read-only scope opened
                    # above -- if it was denied for read_only_scope, use
                    # the write_blocked classification/metric instead of
                    # the generic permission_denied one.
                    if any(d.reason == "read_only_scope" for d in scope.denials[denials_before:]):
                        state.error = "state_question_write_blocked"
                        state_question_routed_total.labels(
                            device_type=uk.device_type or "unknown", path="write_blocked"
                        ).inc()
                    else:
                        state.error = "permission_denied"
                else:
                    state.answer = result
                    state.retrieved_data = {"intent": intent}

                    logger.info(f"Smart control executed: {intent.get('action')} on {device_type} in {room}")

                    # Store context for future reference using new context system
                    if state.session_id and "couldn't" not in result.lower():
                        await store_conversation_context(
                            session_id=state.session_id,
                            intent="control",
                            query=state.query,
                            entities={"room": room, "device_type": device_type},
                            parameters=intent,
                            response=result,
                            ttl=300  # 5 minutes
                        )

        else:
            # D12: the old "no smart controller" fallback pattern-matched
            # turn_on/turn_off and called ha_client directly, but it was
            # both unreachable in production and broken -- ha_client is
            # only constructed when smart_controller is (main.py's
            # lifespan), so this branch's ha_client.call_service would
            # always dereference None. No HA call is possible without the
            # smart controller; say so rather than execute a dead path.
            state.answer = "Home automation isn't configured."
            state.error = "ha_not_configured"

    except Exception as e:
        logger.error(f"Control execution error: {e}", exc_info=True)
        state.answer = "I encountered an error while trying to control that device. Please try again."
        state.error = str(e)

    route_control_duration = time.time() - start
    state.node_timings["route_control"] = route_control_duration
    # Track node time to Prometheus
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "route_control", route_control_duration)
    return state

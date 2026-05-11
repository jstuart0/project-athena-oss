"""route_control_node — extracted from orchestrator.main during Phase 3.2 (ATHENA-10).

Routes home-automation control commands through Home Assistant: sensor fast-paths,
status-query bulk optimisation, dynamic-agent / sequence / context-continuation
routing, and simple pattern-match fallback when the smart controller is absent.

Byte-identical move. See thoughts/shared/plans/2026-05-06-deliver-orchestrator-refactor.md.
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
from orchestrator.state import OrchestratorState
from orchestrator.helpers import (
    get_automation_system_mode,
    get_feature_config,
    store_conversation_context,
)
from orchestrator.mode_permission import check_entity_permission
from orchestrator.ha_status_optimizer import (
    detect_status_query_type,
    optimize_status_query,
    should_skip_synthesis,
)
from orchestrator.automation_agent import should_use_automation_agent

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

        # HA STATUS QUERY OPTIMIZATION (2026-01-12)
        # Detect status queries and use optimized bulk HA state queries
        # This saves 1-3 seconds by avoiding per-entity queries and LLM synthesis
        status_bulk_config = await get_feature_config("status_bulk_query")
        status_skip_config = await get_feature_config("status_skip_synthesis")

        if status_bulk_config.get("enabled", True) and detect_status_query_type(state.query):
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

                # Execute via automation agent
                result = await automation_agent.execute(
                    query=state.query,
                    context=context,
                    model="llama3.1:8b"  # Use capable model for automation
                )
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

                    logger.info(f"Executing sequence with {len(steps)} steps")

                    # Execute sequence in background - return acknowledgment immediately
                    if sequence_executor:
                        result = await sequence_executor.execute_sequence(
                            steps,
                            session_id=state.session_id,
                            background=True
                        )
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
                    prev_intent_entities=prev_intent_for_llm
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
                query_lower = state.query.lower()
                if "back on" in query_lower or "on again" in query_lower:
                    intent["action"] = "turn_on"
                    logger.info("Context reversal: detected 'back on' - setting action to turn_on")
                elif "back off" in query_lower or "off again" in query_lower:
                    intent["action"] = "turn_off"
                    logger.info("Context reversal: detected 'back off' - setting action to turn_off")

                # Handle modifier-based adjustments
                if "modifier" in ref_info.get("ref_types", []):
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
                            prev_intent_entities=prev_intent_for_llm
                        )
                        if prev.get("entities", {}).get("room"):
                            intent['room'] = prev["entities"]["room"]
                    logger.info(f"Modifier adjustment applied: {ref_info.get('ref_types')}")

                # Ensure we have required fields
                if not intent.get('device_type'):
                    intent['device_type'] = prev_params.get('device_type', 'light')
                if not intent.get('action'):
                    intent['action'] = prev_params.get('action', 'set_color')
            else:
                # Normal extraction - no context continuation
                # Pass device room for context when query doesn't specify room
                intent = await smart_controller.extract_intent(state.query, device_room=state.room)

            logger.info(f"Extracted intent: {intent}")

            # Execute the intent with permission checking
            device_type = intent.get('device_type', 'light')
            room = intent.get('room')

            # Execute the command (pass original query for fallback room extraction, and device_room for context)
            result = await smart_controller.execute_intent(intent, ha_client, original_query=state.query, device_room=state.room)
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
            # Fallback to simple pattern matching if smart controller not available
            device = state.entities.get("device")
            query_lower = state.query.lower()

            # Simple pattern matching for common commands
            if "turn on" in query_lower:
                action = "turn_on"
            elif "turn off" in query_lower:
                action = "turn_off"
            else:
                action = None

            if not device:
                device = "light.office"  # Default

            if device and action:
                # Phase 2: Check entity permission before executing command
                if not check_entity_permission(device, state.permissions):
                    logger.warning(
                        "entity_blocked_by_guest_mode",
                        entity_id=device,
                        mode=state.mode
                    )
                    state.answer = f"I'm sorry, you don't have permission to control {device.replace('_', ' ').replace('.', ' ')} in {state.mode} mode."
                    state.error = "permission_denied"
                    return state

                # Call Home Assistant service
                domain = device.split(".")[0]
                result = await ha_client.call_service(
                    domain=domain,
                    service=action,
                    service_data={"entity_id": device}
                )

                state.answer = f"Done! I've turned {'on' if action == 'turn_on' else 'off'} the {device.replace('_', ' ').replace('.', ' ')}."
                state.retrieved_data = {"ha_response": result}

            else:
                state.answer = "I understand you want to control something, but I need more details."

            logger.info(f"Fallback control executed: {device} - {action}")

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

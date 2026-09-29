"""The graph's terminal node for an intent the caller isn't allowed.

route_after_classify sends a request here when intent_gate_refusal refuses
its intent, so no routing, retrieval or tool node ever runs for it. Goes
straight to finalize.
"""

import time

from orchestrator.mode_permission import intent_gate_refusal, intent_refusal_message, record_intent_gate_refusal
from orchestrator.state import OrchestratorState


async def intent_refused_node(state: OrchestratorState) -> OrchestratorState:
    start = time.time()
    state.answer = intent_gate_refusal(state.intent, state.permissions) or intent_refusal_message(state.permissions)
    state.error = "permission_denied"
    record_intent_gate_refusal(state.intent, state.permissions)
    duration = time.time() - start
    state.node_timings["intent_refused"] = duration
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "intent_refused", duration)
    return state

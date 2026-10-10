"""Prometheus metrics for the Athena orchestrator.

These 7 metric objects were previously declared in main.py (lines 840–879).
An 8th, ``ha_write_denied_total``, was added directly here for ATHENA-69 --
it has no main.py predecessor.
They are moved here so that sibling modules (e.g. nodes/validate.py) can
import them without crossing the orchestrator.main boundary.

Metric objects register with the global prometheus_client.REGISTRY exactly
once at module-import time. Moving the declaration site does not change
runtime behavior: the new import path declares; main.py imports the same
singleton objects.

main.py keeps `from prometheus_client import Counter, Histogram, generate_latest`
because generate_latest is still used directly by the /metrics route handler.
"""
from prometheus_client import Counter, Histogram

# ---------------------------------------------------------------------------
# Orchestrator-level metrics
# ---------------------------------------------------------------------------

request_counter = Counter(
    'orchestrator_requests_total',
    'Total requests to orchestrator',
    ['intent', 'status']
)

request_duration = Histogram(
    'orchestrator_request_duration_seconds',
    'Request duration in seconds',
    ['intent']
)

node_duration = Histogram(
    'orchestrator_node_duration_seconds',
    'Node execution duration in seconds',
    ['node']
)

tool_call_breakdown = Histogram(
    'athena_tool_call_phase_seconds',
    'Tool call node phase breakdown in seconds',
    ['phase'],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]
)

# ---------------------------------------------------------------------------
# Validation and hallucination metrics
# ---------------------------------------------------------------------------

validation_counter = Counter(
    'athena_validation_total',
    'Total validation outcomes',
    ['passed', 'reason']  # passed: true/false, reason: too_short, too_long, hallucination, etc.
)

hallucination_counter = Counter(
    'athena_hallucinations_detected_total',
    'Hallucinations detected by detection layer',
    ['layer', 'type']  # layer: pattern_detection, llm_fact_check, tool_filter; type: date, time, money, phone, tool_name
)

validation_layer_duration = Histogram(
    'athena_validation_duration_seconds',
    'Validation node duration in seconds',
    ['layer'],  # layer: basic, pattern, llm_fact_check
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
)

# ---------------------------------------------------------------------------
# HA write authorization (ATHENA-69)
# ---------------------------------------------------------------------------

ha_write_denied_total = Counter(
    'athena_ha_write_denied_total',
    'Home Assistant writes denied by the permission-enforcing guard',
    ['domain', 'scope_mode']  # domain: HA service domain; scope_mode: owner/guest/degraded/system
)

# ---------------------------------------------------------------------------
# State-question routing and write fan-out confirmation (ATHENA-128)
# ---------------------------------------------------------------------------

state_question_routed_total = Counter(
    'athena_state_question_routed_total',
    'State-question utterances routed by path',
    ['device_type', 'path']
    # device_type: light|switch|lock|cover|climate|media_player|fan|unknown
    # path: bulk_optimizer|get_status_dispatch|llm_coerced|json_fallback|write_blocked
)

ha_write_fanout_confirm_total = Counter(
    'athena_ha_write_fanout_confirm_total',
    'Write fan-out confirmation gate outcomes',
    ['domain', 'outcome']
    # outcome: exempt_scope|exempt_imperative|requested|reworded|confirmed|
    #          declined|reasked|cross_identity|replay_claim_lost
)

# ---------------------------------------------------------------------------
# Intent gate (one refusal rule before routing, on every entry path)
# ---------------------------------------------------------------------------

intent_gate_refused_total = Counter(
    'athena_intent_gate_refused_total',
    'Requests refused by the intent gate before any routing',
    ['audience', 'intent']  # audience: public|guest|degraded|<mode>
)

# ---------------------------------------------------------------------------
# Deterministic fast path (no model for trivial turns)
# ---------------------------------------------------------------------------

fast_path_answered_total = Counter(
    'athena_fast_path_answered_total',
    'Turns answered by the deterministic fast path with no model call',
    ['route', 'kind']  # kind: greeting|smalltalk|thanks|farewell|ack|time|date
)

fast_path_deferred_total = Counter(
    'athena_fast_path_deferred_total',
    'Fast-path candidates sent to the full pipeline because the session has an open question',
    ['route', 'reason']  # reason: pending_confirmation|awaiting_context|open_question|context_unreadable
)

ambient_fragment_gated_total = Counter(
    'athena_ambient_fragment_gated_total',
    'Spoken low-information fragments answered without tool selection',
    ['route']  # route: graph|stream
)

fast_path_seconds = Histogram(
    'athena_fast_path_seconds',
    'Handler start to fast-path answer, by route',
    ['route'],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5]
)

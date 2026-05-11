"""Prometheus metrics for the Athena orchestrator.

These 7 metric objects were previously declared in main.py (lines 840–879).
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

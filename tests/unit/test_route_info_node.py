"""Unit tests for orchestrator.nodes.route_info.route_info_node.

Covers the three complexity branches (super_complex / complex / simple) and
verifies that node_timings["route_info"] is populated after each call.

Note: route_info_node is async; tests use asyncio.run() directly to avoid
requiring pytest-asyncio (consistent with test_runtime.py pattern).
"""
import asyncio
import sys
from unittest.mock import MagicMock

import pytest

# Insert src/ so `orchestrator.*` imports resolve without the package installed.
sys.path.insert(0, "src")

from orchestrator.nodes import route_info_node
from orchestrator.state import OrchestratorState, ModelTier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(complexity: str) -> OrchestratorState:
    """Build a minimal OrchestratorState with the given complexity string."""
    return OrchestratorState(query="test query", complexity=complexity)


# ---------------------------------------------------------------------------
# Complexity branch: super_complex → LARGE / tool_calling_super_complex
# ---------------------------------------------------------------------------

def test_super_complex_selects_large_tier():
    state = _make_state("super_complex")
    result = asyncio.run(route_info_node(state))
    assert result.model_tier == ModelTier.LARGE


def test_super_complex_sets_component():
    state = _make_state("super_complex")
    result = asyncio.run(route_info_node(state))
    assert result.model_component == "tool_calling_super_complex"


def test_super_complex_records_timing():
    state = _make_state("super_complex")
    result = asyncio.run(route_info_node(state))
    assert "route_info" in result.node_timings
    assert result.node_timings["route_info"] >= 0.0


# ---------------------------------------------------------------------------
# Complexity branch: complex → MEDIUM / tool_calling_complex
# ---------------------------------------------------------------------------

def test_complex_selects_medium_tier():
    state = _make_state("complex")
    result = asyncio.run(route_info_node(state))
    assert result.model_tier == ModelTier.MEDIUM


def test_complex_sets_component():
    state = _make_state("complex")
    result = asyncio.run(route_info_node(state))
    assert result.model_component == "tool_calling_complex"


def test_complex_records_timing():
    state = _make_state("complex")
    result = asyncio.run(route_info_node(state))
    assert "route_info" in result.node_timings
    assert result.node_timings["route_info"] >= 0.0


# ---------------------------------------------------------------------------
# Complexity branch: simple (default / else) → SMALL / tool_calling_simple
# ---------------------------------------------------------------------------

def test_simple_selects_small_tier():
    state = _make_state("simple")
    result = asyncio.run(route_info_node(state))
    assert result.model_tier == ModelTier.SMALL


def test_simple_sets_component():
    state = _make_state("simple")
    result = asyncio.run(route_info_node(state))
    assert result.model_component == "tool_calling_simple"


def test_simple_records_timing():
    state = _make_state("simple")
    result = asyncio.run(route_info_node(state))
    assert "route_info" in result.node_timings
    assert result.node_timings["route_info"] >= 0.0


# ---------------------------------------------------------------------------
# None complexity falls through to the else (simple) branch
# ---------------------------------------------------------------------------

def test_none_complexity_falls_through_to_simple():
    """complexity=None triggers the else branch (same as simple)."""
    state = _make_state(None)  # type: ignore[arg-type]
    result = asyncio.run(route_info_node(state))
    assert result.model_tier == ModelTier.SMALL
    assert result.model_component == "tool_calling_simple"


# ---------------------------------------------------------------------------
# timing_tracker integration: track_sync called when tracker present
# ---------------------------------------------------------------------------

def test_timing_tracker_is_called_when_present():
    state = _make_state("simple")
    fake_tracker = MagicMock()
    state.timing_tracker = fake_tracker

    asyncio.run(route_info_node(state))

    fake_tracker.track_sync.assert_called_once()
    call_args = fake_tracker.track_sync.call_args[0]
    assert call_args[0] == "graph"
    assert call_args[1] == "route_info"
    assert call_args[2] >= 0.0


def test_timing_tracker_not_called_when_absent():
    """No AttributeError when timing_tracker is None (default)."""
    state = _make_state("complex")
    assert state.timing_tracker is None
    # Should complete without raising
    asyncio.run(route_info_node(state))


# ---------------------------------------------------------------------------
# Return type: always returns the same OrchestratorState instance
# ---------------------------------------------------------------------------

def test_returns_same_state_instance():
    state = _make_state("simple")
    result = asyncio.run(route_info_node(state))
    assert result is state

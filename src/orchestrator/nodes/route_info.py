"""Model-tier selection by complexity for information routing."""

import logging
import time

from orchestrator.state import OrchestratorState, ModelTier

logger = logging.getLogger(__name__)


async def route_info_node(state: OrchestratorState) -> OrchestratorState:
    """
    Select appropriate model tier for information queries.
    Uses complexity determined by classify_node's feature-based detection.
    """
    start = time.time()

    # Use complexity from classification (feature-based detection)
    # This properly routes complex queries to more capable models
    if state.complexity == "super_complex":
        state.model_tier = ModelTier.LARGE
        state.model_component = "tool_calling_super_complex"
    elif state.complexity == "complex":
        state.model_tier = ModelTier.MEDIUM
        state.model_component = "tool_calling_complex"
    else:  # simple
        state.model_tier = ModelTier.SMALL
        state.model_component = "tool_calling_simple"

    # Log model selection decision
    logger.info(
        f"Model selection: complexity={state.complexity} -> "
        f"tier={state.model_tier.value}, component={state.model_component}"
    )

    route_info_duration = time.time() - start
    state.node_timings["route_info"] = route_info_duration
    # Track node time to Prometheus
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "route_info", route_info_duration)
    return state

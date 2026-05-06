"""finalize_node — extracted from orchestrator.main during Phase 2.6 (ATHENA-10).

Byte-identical move. See thoughts/shared/plans/2026-05-06-deliver-orchestrator-refactor.md.
Last node extraction in Phase 2.
"""
from __future__ import annotations

import time

import structlog

from orchestrator.nodes._runtime import get_cache_client
from orchestrator.state import OrchestratorState
from orchestrator.helpers import (
    maybe_post_synthesis_fallback,
    _strip_hallucinated_continuation,
)

logger = structlog.get_logger(__name__)


class _CacheClientProxy:
    """Forward attribute access to the runtime cache client at call time.

    finalize_node references ``cache_client`` as a bare module global.
    The actual client is registered in _runtime by main.py's lifespan, so we
    cannot bind it at import time.  This proxy resolves the current value on
    every attribute lookup, keeping the function body byte-identical.
    """

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_cache_client(), name)


cache_client = _CacheClientProxy()


async def finalize_node(state: OrchestratorState) -> OrchestratorState:
    """
    Prepare final response with fallbacks for validation failures.
    Also handles multi-intent result aggregation.
    """
    start = time.time()

    # MULTI-INTENT HANDLING
    if state.is_multi_intent:
        # Store current result
        current_result = {
            "query": state.query,
            "intent": state.intent.value if state.intent else "unknown",
            "answer": state.answer,
            "data_source": state.data_source
        }
        state.intent_results.append(current_result)
        logger.info(
            f"Multi-intent result {state.current_intent_index + 1}/{len(state.intent_parts)}: {state.intent}",
            extra={"answer_preview": state.answer[:100] if state.answer else None}
        )

        # Check if there are more intents to process
        if state.current_intent_index < len(state.intent_parts) - 1:
            # More intents to process - set up next intent
            state.current_intent_index += 1
            state.query = state.intent_parts[state.current_intent_index]
            # Reset state for next intent processing
            state.intent = None
            state.confidence = 0.0
            state.answer = None
            state.retrieved_data = {}
            state.data_source = None
            state.validation_passed = True
            state.validation_reason = None
            logger.info(f"Preparing next intent ({state.current_intent_index + 1}/{len(state.intent_parts)}): '{state.query}'")
            finalize_duration = time.time() - start
            state.node_timings["finalize"] = finalize_duration
            if state.timing_tracker:
                state.timing_tracker.track_sync("graph", "finalize", finalize_duration)
            return state  # Will route back to classify via conditional edge

        # All intents processed - combine results
        combined_answers = []
        for i, result in enumerate(state.intent_results):
            if result.get("answer"):
                # Add a transition phrase for subsequent answers
                if i > 0:
                    combined_answers.append("")  # Add spacing
                combined_answers.append(result["answer"])

        state.answer = "\n\n".join(combined_answers)
        logger.info(
            f"Multi-intent complete: {len(state.intent_results)} intents processed",
            extra={"intents": [r.get("intent") for r in state.intent_results]}
        )

    # POST-SYNTHESIS FALLBACK: Check if response indicates insufficient data
    # and retry with web search if enabled
    if state.answer and state.validation_passed:
        try:
            fallback_triggered = await maybe_post_synthesis_fallback(state)
            if fallback_triggered:
                logger.info(
                    "post_synthesis_fallback_completed_in_finalize",
                    request_id=state.request_id
                )
        except Exception as fallback_err:
            logger.warning(f"post_synthesis_fallback_error: {fallback_err}")

    if not state.validation_passed:
        # Provide fallback response based on validation failure reason
        logger.warning(f"Validation failed, providing fallback response: {state.validation_reason}")

        if "hallucination" in state.validation_reason.lower():
            # Hallucination detected - provide helpful fallback
            state.answer = f"I don't have current information to answer that accurately. I recommend checking reliable sources for up-to-date information about {state.query.lower()}."
        elif state.error:
            state.answer = "I encountered an issue processing your request. Please try rephrasing your question."
        else:
            state.answer = "I'm not confident in my response. Could you please rephrase your question?"

    # Safety net: never return an empty answer
    if not state.answer:
        logger.warning(
            "finalize_node_empty_answer_fallback",
            intent=state.intent.value if state.intent else None,
            error=state.error,
            request_id=state.request_id
        )
        state.answer = "I'm not sure how to help with that. Could you rephrase your question?"
        state.is_fallback = True

    # Strip any hallucinated role-continuation text (e.g. "\nUser:", "\nHuman:")
    state.answer = _strip_hallucinated_continuation(state.answer)

    # Calculate total processing time
    total_time = time.time() - state.start_time
    logger.info(
        f"Request {state.request_id} completed in {total_time:.2f}s",
        extra={
            "request_id": state.request_id,
            "intent": state.intent,
            "total_time": total_time,
            "node_timings": state.node_timings
        }
    )

    # Cache conversation context for follow-ups
    try:
        await cache_client.set(
            f"conversation:{state.request_id}",
            {
                "query": state.query,
                "intent": state.intent.value if state.intent else None,
                "answer": state.answer,
                "timestamp": time.time()
            },
            ttl=3600  # 1 hour TTL
        )
    except Exception as cache_err:
        logger.warning(f"Failed to cache conversation context: {cache_err}")

    finalize_duration = time.time() - start
    state.node_timings["finalize"] = finalize_duration
    # Track node time to Prometheus
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "finalize", finalize_duration)
    return state

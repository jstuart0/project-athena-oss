"""
helpers.py — Shared utility functions for the Athena orchestrator pipeline.

Moved from main.py (Phase 2.1, ATHENA-10) — pure refactor, no behavior change.

All 17 helpers are function definitions only. No module-level service state.
Runtime singletons are accessed via _runtime.get_X() at call time (Pattern 1).
Helpers that take all dependencies as arguments are Pattern 2.
Pure helpers with no runtime dependencies are Pattern 1 PURE.

Import contract:
  - orchestrator.state  — type definitions
  - orchestrator.urls   — URL constants
  - orchestrator.nodes._runtime — call-time singleton accessors
  - shared.admin_config, shared.service_registry — sibling-already modules
  - Standard library + third-party (httpx, re, etc.)
  - NO `from orchestrator.main import ...` lines (three-pattern rule, r1c:R3-H2)
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from shared.logging_config import configure_logging

from orchestrator.nodes import _runtime
from orchestrator.state import ConversationContext
from orchestrator.urls import (
    AIRPORTS_SERVICE_URL,
    DINING_SERVICE_URL,
    DIRECTIONS_SERVICE_URL,
    EVENTS_SERVICE_URL,
    FLIGHTS_SERVICE_URL,
    NEWS_SERVICE_URL,
    ONECALL_SERVICE_URL,
    RECIPES_SERVICE_URL,
    SPORTS_SERVICE_URL,
    STOCKS_SERVICE_URL,
    STREAMING_SERVICE_URL,
    WEATHER_SERVICE_URL,
    WEBSEARCH_SERVICE_URL,
)
from shared.admin_config import get_admin_client
from shared.service_registry import get_service_url as registry_get_service_url
from orchestrator.config_loader import ADMIN_API_URL
from orchestrator.utils.constants import DEFAULT_LOCATION, DEFAULT_TIMEZONE

logger = configure_logging("orchestrator.helpers")

# ---------------------------------------------------------------------------
# Module-level constants (no runtime state)
# ---------------------------------------------------------------------------

# Feature-flag cache TTL (seconds); mirrors the value in main.py
_orch_feature_flag_cache_ttl: float = 60.0

# Post-synthesis fallback cache (local to this module — only used by the two
# helpers below).  Mirrors the dict + TTL previously in main.py.
_post_synthesis_fallback_cache: Dict[str, Tuple[float, Dict]] = {}
_post_synthesis_fallback_cache_ttl: float = 60.0

# Component-model cache timestamp lives here because _runtime only stores the
# dict itself.  get_component_model_cache() returns that dict by reference; we
# update it in-place (clear + update) rather than rebinding the local name.
_component_model_cache_time: float = 0.0
COMPONENT_MODEL_CACHE_TTL: int = 300  # 5 minutes

# Default model + fallback table (mirrors main.py verbatim)
_DEFAULT_MODEL: str = os.getenv("ATHENA_DEFAULT_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
FALLBACK_MODELS: Dict[str, str] = {
    "intent_classifier": os.getenv("ATHENA_FALLBACK_MODEL_INTENT_CLASSIFIER", _DEFAULT_MODEL),
    "tool_calling_simple": os.getenv("ATHENA_FALLBACK_MODEL_TOOL_CALLING_SIMPLE", _DEFAULT_MODEL),
    "tool_calling_complex": os.getenv("ATHENA_FALLBACK_MODEL_TOOL_CALLING_COMPLEX", _DEFAULT_MODEL),
    "tool_calling_super_complex": os.getenv("ATHENA_FALLBACK_MODEL_TOOL_CALLING_SUPER_COMPLEX", _DEFAULT_MODEL),
    "response_synthesis": os.getenv("ATHENA_FALLBACK_MODEL_RESPONSE_SYNTHESIS", _DEFAULT_MODEL),
    "response_synthesis_complex": os.getenv("ATHENA_FALLBACK_MODEL_RESPONSE_SYNTHESIS_COMPLEX", _DEFAULT_MODEL),
    "fact_check_validation": os.getenv("ATHENA_FALLBACK_MODEL_FACT_CHECK_VALIDATION", _DEFAULT_MODEL),
    "conversation_summarizer": os.getenv("ATHENA_FALLBACK_MODEL_CONVERSATION_SUMMARIZER", _DEFAULT_MODEL),
}

# Regex used by _strip_hallucinated_continuation; compiled once at module load.
_CONTINUATION_PATTERN = re.compile(
    r'\n\s*(User|Human|Jarvis|Assistant)\s*:',
    re.IGNORECASE,
)


# =============================================================================
# Feature Flag Helpers (Pattern 1 — use _runtime.get_orch_feature_flag_cache())
# =============================================================================

async def get_feature_config(flag_name: str) -> Dict[str, Any]:
    """
    Get feature flag with full config dict (not just enabled/disabled).

    Args:
        flag_name: Name of the feature flag

    Returns:
        Dict with enabled status and config, or empty dict if not found
    """
    import time

    cache_key = f"{flag_name}_config"
    _feature_flag_cache = _runtime.get_orch_feature_flag_cache()

    # Check cache first
    if cache_key in _feature_flag_cache:
        cached_time, cached_value = _feature_flag_cache[cache_key]
        if time.time() - cached_time < _orch_feature_flag_cache_ttl:
            return cached_value

    # Fetch from admin API
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{ADMIN_API_URL}/api/features/public",
                params={"name": flag_name}
            )
            if response.status_code == 200:
                flags = response.json()
                for flag in flags:
                    if flag.get("name") == flag_name:
                        result = {
                            "enabled": flag.get("enabled", False),
                            "config": flag.get("config", {}),
                        }
                        _feature_flag_cache[cache_key] = (time.time(), result)
                        return result
    except Exception as e:
        logger.warning("feature_config_fetch_failed", flag=flag_name, error=str(e))

    return {"enabled": False, "config": {}}


async def get_automation_system_mode() -> str:
    """
    Get the automation system mode from feature flag config.

    Returns:
        "pattern_matching" or "dynamic_agent"
    """
    import time

    flag_name = "automation_system_mode"
    _feature_flag_cache = _runtime.get_orch_feature_flag_cache()

    # Check cache first
    if flag_name in _feature_flag_cache:
        cached_time, cached_value = _feature_flag_cache[flag_name]
        if time.time() - cached_time < _orch_feature_flag_cache_ttl:
            return cached_value

    # Fetch from admin API
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{ADMIN_API_URL}/api/features/public",
                params={"name": flag_name}
            )
            if response.status_code == 200:
                flags = response.json()
                for flag in flags:
                    if flag.get("name") == flag_name:
                        config = flag.get("config", {})
                        mode = config.get("mode", "pattern_matching")
                        _feature_flag_cache[flag_name] = (time.time(), mode)
                        return mode
    except Exception as e:
        logger.warning("automation_mode_fetch_failed", error=str(e))

    return "pattern_matching"  # Default to pattern matching


async def get_weather_provider_mode() -> str:
    """
    Get the weather provider mode from feature flag config.

    Returns:
        "standard" (free tier) or "onecall" (OneCall 3.0)
    """
    import time

    flag_name = "weather_provider"
    _feature_flag_cache = _runtime.get_orch_feature_flag_cache()

    # Check cache first
    if flag_name in _feature_flag_cache:
        cached_time, cached_value = _feature_flag_cache[flag_name]
        if time.time() - cached_time < _orch_feature_flag_cache_ttl:
            return cached_value

    # Fetch from admin API
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{ADMIN_API_URL}/api/features/public",
                params={"name": flag_name}
            )
            if response.status_code == 200:
                flags = response.json()
                for flag in flags:
                    if flag.get("name") == flag_name:
                        config = flag.get("config", {})
                        mode = config.get("mode", "standard")
                        _feature_flag_cache[flag_name] = (time.time(), mode)
                        return mode
    except Exception as e:
        logger.warning("weather_provider_mode_fetch_failed", error=str(e))

    return "standard"  # Default to standard weather service


# =============================================================================
# Post-Synthesis Web Search Fallback
# =============================================================================

async def get_post_synthesis_fallback_config() -> Dict[str, Any]:
    """
    Get post-synthesis fallback configuration from feature flags.

    Returns:
        Dict with 'enabled' and 'config' keys
    """
    cache_key = "post_synthesis_fallback"

    # Check cache
    if cache_key in _post_synthesis_fallback_cache:
        cached_time, cached_config = _post_synthesis_fallback_cache[cache_key]
        if time.time() - cached_time < _post_synthesis_fallback_cache_ttl:
            return cached_config

    # Fetch from admin API
    try:
        config = await get_feature_config("post_synthesis_fallback")
        _post_synthesis_fallback_cache[cache_key] = (time.time(), config)
        return config
    except Exception as e:
        logger.warning("post_synthesis_fallback_config_fetch_failed", error=str(e))

    return {"enabled": False, "config": {}}


# =============================================================================
# Pattern 1 PURE helpers
# =============================================================================

def enhance_query_with_year(query: str) -> str:
    """
    Enhance a search query with the current year for better relevance.

    Adds the current year to queries that ask about current/recent information
    but don't already contain a year reference.

    Args:
        query: The original search query

    Returns:
        Enhanced query with year appended if applicable
    """
    import re
    from datetime import datetime

    current_year = datetime.now().year
    query_lower = query.lower()

    # Skip if query already contains a recent year (2020-2030)
    if re.search(r'\b20[2-3]\d\b', query):
        return query

    # Keywords that indicate the user wants current/recent information
    current_indicators = [
        "current", "latest", "now", "today", "this season", "this year",
        "right now", "at the moment", "standings", "playoff", "rankings",
        "score", "results", "schedule", "upcoming", "recent", "new"
    ]

    # Check if query contains current-info indicators
    needs_year = any(indicator in query_lower for indicator in current_indicators)

    # Also add year for sports/news queries that typically need fresh data
    sports_news_keywords = [
        "nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball",
        "baseball", "hockey", "game", "match", "championship", "super bowl",
        "world series", "finals", "news", "election", "price", "stock"
    ]
    if any(kw in query_lower for kw in sports_news_keywords):
        needs_year = True

    if needs_year:
        return f"{query} {current_year}"

    return query


def detect_insufficient_response(response: str, config: Dict[str, Any]) -> Optional[str]:
    """
    Detect if LLM response indicates it couldn't find the requested information.

    Args:
        response: The LLM response to check
        config: Feature config with detection_patterns

    Returns:
        The matched pattern if found, None otherwise
    """
    if not response:
        return "empty_response"

    # Check minimum response length
    min_length = config.get("min_response_length", 10)
    if len(response) < min_length:
        return "response_too_short"

    patterns = config.get("detection_patterns", [
        "couldn't find",
        "could not find",
        "don't have information",
        "no information available",
        "unable to find",
        "I don't know",
        "I'm not sure",
        "I cannot find",
        "no data available",
        "not able to find"
    ])

    response_lower = response.lower()
    for pattern in patterns:
        if pattern.lower() in response_lower:
            return pattern

    return None


# =============================================================================
# maybe_post_synthesis_fallback (Pattern 1 — uses _runtime at call time)
# =============================================================================

async def maybe_post_synthesis_fallback(state: 'Any') -> bool:
    """
    Check if synthesis produced insufficient response and retry with web search.

    This function is called after the normal synthesis completes. If the response
    matches detection patterns (e.g., "I couldn't find information"), it triggers
    a web search to try to find the answer.

    Args:
        state: The current orchestrator state

    Returns:
        True if fallback was triggered and succeeded, False otherwise
    """
    # Get feature config
    fallback_config = await get_post_synthesis_fallback_config()

    if not fallback_config.get("enabled", False):
        return False

    config = fallback_config.get("config", {})

    # Check excluded intents
    excluded_intents = config.get("excluded_intents", ["control", "automation", "scene", "timer", "reminder"])
    if state.intent:
        intent_value = state.intent.value.lower() if hasattr(state.intent, 'value') else str(state.intent).lower()
        if intent_value in [e.lower() for e in excluded_intents]:
            logger.debug(
                "post_synthesis_fallback_skipped_excluded_intent",
                intent=intent_value
            )
            return False

    # Check if response indicates insufficient data
    matched_pattern = detect_insufficient_response(state.answer, config)
    if not matched_pattern:
        return False

    logger.info(
        "post_synthesis_fallback_triggered",
        matched_pattern=matched_pattern,
        original_response_preview=state.answer[:100] if state.answer else None,
        intent=state.intent.value if state.intent and hasattr(state.intent, 'value') else str(state.intent),
        query_preview=state.query[:50] if state.query else None
    )

    # Execute web search fallback
    try:
        fallback_start = time.time()

        # Build enhanced search query with year for relevance
        search_query = enhance_query_with_year(state.query)
        if state.entities:
            location = state.entities.get("location")
            if location:
                search_query = f"{search_query} {location}"

        logger.info(f"post_synthesis_fallback_search_query: '{search_query}'")

        # Execute web search
        parallel_search_engine = _runtime.get_parallel_search_engine()
        if not parallel_search_engine:
            logger.warning("post_synthesis_fallback_no_search_engine")
            return False

        intent, search_results = await parallel_search_engine.search(
            query=search_query,
            location=DEFAULT_LOCATION,
            limit_per_provider=10,
            force_search=True
        )

        if not search_results:
            logger.warning("post_synthesis_fallback_no_results", query=search_query)
            return False

        # Check latency budget
        max_latency_ms = config.get("max_latency_ms", 5000)
        elapsed_ms = (time.time() - fallback_start) * 1000
        if elapsed_ms > max_latency_ms:
            logger.warning(
                "post_synthesis_fallback_latency_exceeded",
                elapsed_ms=elapsed_ms,
                max_latency_ms=max_latency_ms
            )
            # Still continue - we have results

        # Build context from search results
        context_parts = []
        for result in search_results[:8]:
            if hasattr(result, 'snippet') and result.snippet:
                context_parts.append(result.snippet)
            elif hasattr(result, 'to_dict'):
                rd = result.to_dict()
                if rd.get('snippet'):
                    context_parts.append(rd['snippet'])

        if not context_parts:
            logger.warning("post_synthesis_fallback_no_context")
            return False

        context = "\n\n".join(context_parts[:5])

        # Synthesize response from web search results
        synthesis_prompt = f"""The user asked: "{state.query}"

The original response could not find the requested information. Here are web search results that may help:

{context}

Based on these search results, provide a helpful, accurate answer to the user's question. Be concise but informative."""

        try:
            synthesis_config = await get_component_config("response_synthesis")
            synthesis_model = synthesis_config["model_name"]
            synthesis_start = time.time()

            llm_router = _runtime.get_llm_router()
            synthesis_result = await llm_router.generate(
                model=synthesis_model,
                prompt=synthesis_prompt,
                temperature=0.7,
                system_prompt=_component_system_prompt(synthesis_config),
                request_id=state.request_id,
                session_id=state.session_id,
                stage="post_synthesis_fallback"
            )

            synthesis_duration = time.time() - synthesis_start

            # Track LLM call for metrics
            if state.timing_tracker:
                tokens = synthesis_result.get("eval_count", 0)
                state.timing_tracker.record_llm_call(
                    "post_synthesis_fallback", synthesis_model, tokens, int(synthesis_duration * 1000), "synthesis"
                )

            new_response = synthesis_result.get("response", "")
            if new_response and len(new_response) > 20:
                # Store original response for debugging
                original_response = state.answer
                state.answer = new_response
                state.data_source = f"Web Search (post-synthesis fallback)"
                state.node_timings["post_synthesis_fallback"] = time.time() - fallback_start

                if config.get("log_triggers", True):
                    logger.info(
                        "post_synthesis_fallback_succeeded",
                        new_response_preview=new_response[:100],
                        original_response_preview=original_response[:100] if original_response else None,
                        results_count=len(search_results),
                        latency_ms=elapsed_ms
                    )

                return True
            else:
                logger.warning("post_synthesis_fallback_empty_synthesis")
                return False

        except Exception as synth_err:
            logger.error(f"post_synthesis_fallback_synthesis_failed: {synth_err}")
            return False

    except Exception as e:
        logger.error(f"post_synthesis_fallback_failed: {e}")
        return False


# =============================================================================
# store_conversation_context (Pattern 1 — uses _runtime at call time)
# =============================================================================

async def store_conversation_context(
    session_id: str,
    intent: str,
    query: str,
    entities: Dict[str, Any],
    parameters: Dict[str, Any],
    response: str,
    ttl: int = 300  # 5 minute default TTL
) -> bool:
    """Store conversation context in Redis (with in-memory fallback)."""
    if not session_id:
        return False

    context = ConversationContext(
        intent=intent,
        query=query,
        entities=entities,
        parameters=parameters,
        response=response,
        timestamp=time.time()
    )

    cache_client = _runtime.get_cache_client()
    memory_context = _runtime.get_memory_context()

    # Try Redis first with timeout to prevent hanging on dead Redis
    redis_success = False
    if cache_client and cache_client.client:
        try:
            context_key = f"athena:context:{session_id}"
            await asyncio.wait_for(
                cache_client.client.setex(context_key, ttl, context.model_dump_json()),
                timeout=2.0  # 2 second timeout
            )
            logger.info(f"Stored context in Redis for session {session_id[:8]}...: intent={intent}")
            redis_success = True
        except asyncio.TimeoutError:
            logger.warning(f"Redis context storage timed out, using memory fallback")
        except Exception as e:
            logger.warning(f"Redis context storage failed, using memory fallback: {e}")

    # Always store in memory as fallback (ensures context works even if Redis fails later)
    memory_context[session_id] = {
        "context": context,
        "expires_at": time.time() + ttl
    }

    # Periodic cleanup (every ~10 stores)
    if len(memory_context) > 0 and len(memory_context) % 10 == 0:
        _cleanup_expired_memory_contexts(memory_context)

    if not redis_success:
        logger.info(f"Stored context in memory for session {session_id[:8]}...: intent={intent}")

    return True


def _cleanup_expired_memory_contexts(memory_context: Dict[str, Any]) -> None:
    """Remove expired contexts from the in-memory dict (called periodically)."""
    now = time.time()
    expired = [sid for sid, data in memory_context.items() if data.get("expires_at", 0) < now]
    for sid in expired:
        del memory_context[sid]
    if expired:
        logger.debug(f"Cleaned up {len(expired)} expired contexts from memory")


# =============================================================================
# Component model helpers (Pattern 1 — uses _runtime.get_component_model_cache())
# =============================================================================

async def get_model_for_component(component_name: str) -> str:
    """Get model for a component with caching to reduce database calls.

    Uses a 5-minute TTL cache to avoid per-request database lookups.
    Falls back to FALLBACK_MODELS if cache refresh fails.
    """
    global _component_model_cache_time

    now = time.time()
    component_model_cache = _runtime.get_component_model_cache()

    # Refresh cache if expired
    if now - _component_model_cache_time > COMPONENT_MODEL_CACHE_TTL:
        try:
            admin_client = get_admin_client()
            all_models = await admin_client.get_all_component_models()
            component_model_cache.clear()
            component_model_cache.update({m['component_name']: m for m in all_models})
            _component_model_cache_time = now
            logger.debug("component_model_cache_refreshed", count=len(component_model_cache))
        except Exception as e:
            logger.warning(f"Failed to refresh component model cache: {e}")
            # If cache is completely empty, try single lookup as fallback
            if not component_model_cache:
                try:
                    admin_client = get_admin_client()
                    config = await admin_client.get_component_model(component_name)
                    if config and config.get("enabled"):
                        return config.get("model_name")
                except Exception as e:
                    logger.warning("get_model_for_component_failed", error=str(e))

    # Return from cache or fallback
    if component_name in component_model_cache:
        config = component_model_cache[component_name]
        if config.get("enabled"):
            return config.get("model_name")

    return FALLBACK_MODELS.get(component_name, _DEFAULT_MODEL)


async def get_component_config(component_name: str) -> dict:
    """Get full component configuration including model settings and backend type.

    Uses the same cache as get_model_for_component for efficiency.

    Returns dict with:
        - model_name: The LLM model to use
        - backend_type: ollama, mlx, openai, etc.
        - temperature: Temperature setting (optional)
        - max_tokens: Max tokens limit (optional)
        - disable_thinking: If True, prepend /no_think to disable Qwen3 thinking mode
        - enabled: If component is enabled
    """
    global _component_model_cache_time

    now = time.time()
    component_model_cache = _runtime.get_component_model_cache()

    # Refresh cache if expired
    if now - _component_model_cache_time > COMPONENT_MODEL_CACHE_TTL:
        try:
            admin_client = get_admin_client()
            all_models = await admin_client.get_all_component_models()
            component_model_cache.clear()
            component_model_cache.update({m['component_name']: m for m in all_models})
            _component_model_cache_time = now
            logger.debug("component_model_cache_refreshed", count=len(component_model_cache))
        except Exception as e:
            logger.warning(f"Failed to refresh component model cache: {e}")

    # Return from cache or fallback
    if component_name in component_model_cache:
        config = component_model_cache[component_name]
        if config.get("enabled"):
            return {
                "model_name": config.get("model_name"),
                "backend_type": config.get("backend_type", "ollama"),
                "temperature": config.get("temperature"),
                "max_tokens": config.get("max_tokens"),
                "disable_thinking": config.get("disable_thinking", False),
                "enabled": config.get("enabled", True),
            }

    # Return fallback with defaults
    return {
        "model_name": FALLBACK_MODELS.get(component_name, _DEFAULT_MODEL),
        "backend_type": "ollama",
        "temperature": None,
        "max_tokens": None,
        "disable_thinking": False,
        "enabled": True,
    }


def _component_system_prompt(component_config: Optional[dict]) -> Optional[str]:
    """Return a system prompt that disables reasoning/thinking when configured."""
    if component_config and component_config.get("disable_thinking"):
        return "/no_think"
    return None


# =============================================================================
# get_rag_service_url (Pattern 1 — uses sibling-already get_admin_client)
# =============================================================================

async def get_rag_service_url(intent: str) -> Optional[str]:
    """
    Get RAG service URL for intent from service registry.

    Priority order:
    1. Intent routing table (for custom routing overrides)
    2. Service registry (source of truth for service URLs)
    3. Environment variables (last resort fallback)

    Args:
        intent: Intent category (e.g., "weather", "sports", "airports")

    Returns:
        RAG service URL or None
    """
    # 1. First try intent routing table (for custom routing)
    try:
        client = get_admin_client()
        routing_config = await client.get_intent_routing()

        if routing_config and intent in routing_config:
            url = routing_config[intent].get("rag_service_url")
            if url:
                logger.info(f"Using intent routing URL for '{intent}': {url}")
                return url
    except Exception as e:
        logger.debug(f"Intent routing lookup failed for '{intent}': {e}")

    # 2. Query service registry (source of truth)
    try:
        registry_url = await registry_get_service_url(intent)
        if registry_url:
            logger.info(f"Using service registry URL for '{intent}': {registry_url}")
            return registry_url
    except Exception as e:
        logger.warning(f"Service registry lookup failed for '{intent}': {e}")

    # 3. Last resort: environment variable fallback
    env_var_map = {
        "weather": WEATHER_SERVICE_URL,
        "onecall": ONECALL_SERVICE_URL,
        "airports": AIRPORTS_SERVICE_URL,
        "sports": SPORTS_SERVICE_URL,
        "flights": FLIGHTS_SERVICE_URL,
        "events": EVENTS_SERVICE_URL,
        "streaming": STREAMING_SERVICE_URL,
        "news": NEWS_SERVICE_URL,
        "stocks": STOCKS_SERVICE_URL,
        "websearch": WEBSEARCH_SERVICE_URL,
        "dining": DINING_SERVICE_URL,
        "recipes": RECIPES_SERVICE_URL,
        "directions": DIRECTIONS_SERVICE_URL,
    }
    fallback_url = env_var_map.get(intent)
    if fallback_url:
        logger.warning(f"Using env var fallback URL for '{intent}': {fallback_url} (service registry unavailable)")
    return fallback_url


# =============================================================================
# _fallback_to_web_search (Pattern 1 — uses _runtime at call time)
# R3-H2: two callers — retrieve_node (Phase 3.5) + tool_call_node (deferred 1.3)
# =============================================================================

async def _fallback_to_web_search(state: 'Any', rag_service: str, error_msg: str):
    """
    Enhanced fallback with 3-tier content retrieval system.

    Tier 1: Structured data extraction (fastest - no fetching)
    Tier 2: Selective content fetching (1-2 high-value URLs)
    Tier 3: Multi-snippet aggregation (current fallback)

    Args:
        state: Current orchestrator state
        rag_service: Name of the failed RAG service (for logging)
        error_msg: Error message from the failed service
    """
    logger.warning(f"{rag_service} RAG service failed ({error_msg}), falling back to web search")

    try:
        # Location-sensitive RAG services need location in query (search providers ignore location param)
        location_sensitive_services = ["weather", "dining", "events", "flights", "airports"]
        user_location = state.entities.get("location", DEFAULT_LOCATION) if state.entities else DEFAULT_LOCATION

        if rag_service.lower() in location_sensitive_services:
            search_query = f"{enhance_query_with_year(state.query)} {user_location}"
        else:
            search_query = enhance_query_with_year(state.query)

        parallel_search_engine = _runtime.get_parallel_search_engine()

        # STEP 1: Execute parallel search with increased result limit
        intent, search_results = await parallel_search_engine.search(
            query=search_query,
            location=DEFAULT_LOCATION,
            limit_per_provider=10,  # Increased from 5 for better coverage
            force_search=True  # CRITICAL: Force web search even for RAG intents
        )

        logger.info(f"Fallback search intent classified as: '{intent}', {len(search_results)} total results")

        if not search_results:
            # No search results at all - use LLM knowledge
            state.retrieved_data = {}
            state.data_source = "LLM knowledge (web search returned no results)"
            logger.warning(f"Fallback web search returned no results, using LLM knowledge")
            return

        # STEP 2: Check if content fetching would be beneficial (TIER 1 check)
        from search_providers.structured_data import (
            should_fetch_content,
            get_fetch_priority_urls,
            estimate_fetch_benefit
        )

        fetch_benefit = estimate_fetch_benefit(state.query, search_results)
        should_fetch = should_fetch_content(state.query, search_results)

        logger.info(f"Content fetch benefit: {fetch_benefit}, should_fetch: {should_fetch}")

        fetched_content = None

        # STEP 3: TIER 2 - Selective Content Fetching (if beneficial)
        if should_fetch:
            priority_urls = get_fetch_priority_urls(search_results, state.query, max_urls=2)

            if priority_urls:
                logger.info(f"Attempting content fetch from {len(priority_urls)} high-value URLs")

                from shared.content_fetcher import ContentFetcher

                fetcher = ContentFetcher(timeout=2.0, max_concurrent=2)

                try:
                    # Fetch URLs in parallel
                    results = await fetcher.fetch_multiple_urls(priority_urls)

                    # Use first successful result
                    for result in results:
                        if result:
                            fetched_content = result
                            logger.info(
                                f"Content fetch successful: {result['type']} from {result['source_url']} "
                                f"({result['extraction_time_ms']:.0f}ms)"
                            )
                            break

                except Exception as e:
                    logger.warning(f"Content fetching failed: {e}")

                finally:
                    await fetcher.close()

        # STEP 4: Format response based on what we retrieved
        if fetched_content:
            # TIER 2 SUCCESS - We got comprehensive data from fetching
            state.retrieved_data = {
                "intent": intent,
                "fetched_content": fetched_content,
                "search_results": [r.to_dict() for r in search_results[:5]],
                "sources": [fetched_content["source_url"]],
                "data_type": fetched_content["type"],
                "extraction_time_ms": fetched_content.get("extraction_time_ms", 0),
                "fallback_note": f"Comprehensive data retrieved via {fetched_content['type']} extraction"
            }
            state.data_source = f"Content Fetch ({fetched_content['type']}): {fetched_content['source_url']}"
            state.citations.append(f"Content extracted from {fetched_content['source_url']}")
            state.citations.append(f"Extraction method: {fetched_content['type']}")
            logger.info(
                f"Using Tier 2 (content fetch): {fetched_content['type']} "
                f"in {fetched_content['extraction_time_ms']:.0f}ms"
            )

        else:
            # TIER 3 - Fall back to multi-snippet aggregation
            result_fusion = _runtime.get_result_fusion()
            fused_results = result_fusion.get_top_results(
                results=search_results,
                query=state.query,
                intent=intent,
                limit=10  # Increased from 5 for better coverage
            )

            logger.info(f"Fallback to Tier 3 (snippets): {len(fused_results)} fused results")

            search_data = {
                "intent": intent,
                "results": [r.to_dict() for r in fused_results],
                "sources": list(set(r.source for r in fused_results)),
                "total_results": len(search_results),
                "fused_results": len(fused_results),
                "fallback_note": f"Data aggregated from search snippets (primary {rag_service} service unavailable)"
            }

            state.retrieved_data = search_data
            state.data_source = f"Web Search Snippets ({intent}): {', '.join(search_data['sources'])}"
            state.citations.extend([f"Search result from {r.source}" for r in fused_results])
            logger.info(f"Using Tier 3 (snippets): intent={intent}, sources={search_data['sources']}")

        # Add note about fallback
        state.citations.append(f"Note: {rag_service} service unavailable, used web search fallback")

    except Exception as e:
        logger.error(f"Fallback web search failed: {e}", exc_info=True)
        state.retrieved_data = {}
        state.data_source = "LLM knowledge (RAG and web search failed)"


# =============================================================================
# summarize_conversation_history (Pattern 1 — uses _runtime at call time)
# =============================================================================

async def summarize_conversation_history(
    history: List[Dict[str, str]],
    current_query: str,
    request_id: str = None,
    previous_summary: str = ""
) -> str:
    """
    Summarize conversation history into a brief context statement.

    Uses a rolling accumulation strategy: if a previous summary exists,
    it is included so that facts from earlier turns are preserved even
    after they fall out of the message history window.

    Args:
        history: List of previous messages [{"role": "user/assistant", "content": "..."}]
        current_query: The current user query (for relevance)
        request_id: Optional request ID for logging
        previous_summary: Optional previous summary to accumulate facts from

    Returns:
        A brief summary string (e.g., "Previous context: User is Sarah, a software engineer...")
    """
    if not history:
        return previous_summary or ""

    # SHORT HISTORY PATH (1-2 messages):
    # Skip the LLM entirely. Small models struggle to "summarize" when there's
    # barely anything to compress — they often respond with "no relevant context"
    # or hallucinate filler. For 1-2 messages, the raw user content IS the best
    # summary. At 3+ messages the LLM adds value by compressing and extracting
    # patterns across turns.
    if len(history) <= 2:
        user_messages = [m["content"] for m in history if m["role"] == "user"]
        if user_messages:
            raw_context = " ".join(msg[:300] for msg in user_messages)
            if previous_summary:
                clean_prev = previous_summary.replace("Previous context: ", "", 1)
                result = f"Previous context: {clean_prev} {raw_context}"
            else:
                result = f"Previous context: {raw_context}"
            logger.info(f"Short history path: {len(history)} messages -> {len(result)} chars (no LLM)")
            return result

    # LONG HISTORY PATH (3+ messages): Use LLM summarizer
    # Format history for summarization — use all available history
    history_text = "\n".join([
        f"{msg['role'].capitalize()}: {msg['content'][:300]}"
        for msg in history[-12:]  # Use up to last 12 messages for better coverage
    ])

    # If we have a previous summary, include it so facts accumulate across turns
    prior_context_section = ""
    if previous_summary:
        # Strip the "Previous context: " prefix if present
        clean_prev = previous_summary.replace("Previous context: ", "", 1)
        prior_context_section = f"""
Previously known facts about the user (MUST be preserved in your summary):
{clean_prev}

"""

    prompt = f"""Extract ALL user facts and context from this conversation. Preserve every detail the user stated about themselves.
{prior_context_section}Recent conversation:
{history_text}

Write a concise summary (2-5 sentences) that captures:
1. ALL facts the user stated about themselves (name, location, occupation, pets, family, preferences, etc.) — include facts from the "previously known" section above
2. Key topics discussed and any decisions or corrections made (corrections override old facts)
3. Any context relevant to this new query: "{current_query}"

IMPORTANT: Never drop user-stated facts even if they seem unrelated to the new query. The user expects you to remember everything they told you. If a user corrected a fact (e.g., "actually Mochi is 4 now"), use the corrected value.
You MUST produce a summary. Every user message contains information worth preserving for conversation continuity.

Summary:"""

    try:
        # Get summarizer model from database config
        summarizer_config = await get_component_config("conversation_summarizer")
        summarizer_model = summarizer_config["model_name"]

        # Use configurable model for summarization
        summarize_start = time.time()
        llm_router = _runtime.get_llm_router()
        result = await llm_router.generate(
            model=summarizer_model,
            prompt=prompt,
            temperature=0.3,
            system_prompt=_component_system_prompt(summarizer_config),
            request_id=request_id,
            stage="summarize"
        )
        summarize_duration = time.time() - summarize_start

        # Record LLM call for metrics
        from shared.metrics import LLM_CALL_DURATION, LLM_TOKENS_GENERATED
        tokens = result.get("eval_count", 0)
        LLM_CALL_DURATION.labels(
            stage="summarize",
            model=summarizer_model,
            call_type="inference"
        ).observe(summarize_duration)
        if tokens > 0:
            LLM_TOKENS_GENERATED.labels(stage="summarize", model=summarizer_model).inc(tokens)

        summary = result.get("response", "").strip()

        # Validate summary isn't too long or empty
        if summary and len(summary) < 1000 and "No relevant prior context" not in summary:
            logger.info(f"History summarized: {len(history)} messages -> {len(summary)} chars")
            return f"Previous context: {summary}"

        return ""

    except Exception as e:
        logger.warning(f"History summarization failed: {e}")
        return ""


# =============================================================================
# Pattern 1 PURE helpers — no runtime dependencies
# =============================================================================

def _normalized_general_info_query(query: str) -> str:
    """Lowercase query with punctuation removed for low-latency fast-path matching."""
    return re.sub(r"[^a-z0-9\s]", "", (query or "").lower()).strip()


def _direct_general_info_response(query: str) -> Optional[str]:
    """Return deterministic responses for trivial chat and local time/date requests."""
    normalized = _normalized_general_info_query(query)
    if not normalized:
        return None

    direct_responses = {
        "hello": "Hello. How can I help?",
        "hi": "Hi. How can I help?",
        "hey": "Hey. How can I help?",
        "good morning": "Good morning. How can I help?",
        "good afternoon": "Good afternoon. How can I help?",
        "good evening": "Good evening. How can I help?",
        "how are you": "I'm doing well. How can I help?",
        "hows it going": "I'm here and ready to help.",
        "thanks": "You're welcome.",
        "thank you": "You're welcome.",
        "bye": "Good night.",
        "goodbye": "Goodbye.",
        "see you": "See you later.",
    }
    if normalized in direct_responses:
        return direct_responses[normalized]

    if normalized in {
        "what time is it",
        "whats the time",
        "what is the time",
        "current time",
        "tell me the time",
    }:
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime, timezone as tz
            local_now = datetime.now(tz.utc).astimezone(ZoneInfo(DEFAULT_TIMEZONE))
            return f"It's {local_now.strftime('%-I:%M %p')}."
        except Exception as e:
            logger.warning("direct_time_response_failed", error=str(e))
            return None

    if normalized in {
        "what date is it",
        "whats the date",
        "what is todays date",
        "whats todays date",
        "current date",
        "what day is it",
    }:
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime, timezone as tz
            local_now = datetime.now(tz.utc).astimezone(ZoneInfo(DEFAULT_TIMEZONE))
            return f"Today is {local_now.strftime('%A, %B %-d, %Y')}."
        except Exception as e:
            logger.warning("direct_date_response_failed", error=str(e))
            return None

    return None


def _strip_hallucinated_continuation(text: str) -> str:
    """
    Strip LLM-hallucinated role-continuation text from a response.

    Truncates at the first line that looks like a new role turn (e.g. "\\nUser:",
    "\\nHuman:", "\\nAssistant:", "\\nJarvis:"). These occur when the LLM starts
    generating the next conversation turn instead of stopping after its response.

    Also detects paragraph-level repetition (e.g. thinking-mode leak that causes
    the model to repeat the same block multiple times) and truncates before the
    first repeated paragraph.
    """
    if not text:
        return text
    match = _CONTINUATION_PATTERN.search(text)
    if match:
        text = text[:match.start()].rstrip()

    # Paragraph-level repetition detector: if the same paragraph (first 120 chars)
    # appears more than once, truncate before the second occurrence.
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) >= 3:
        seen: Dict[str, int] = {}
        for i, para in enumerate(paragraphs):
            key = para[:120]
            if key in seen:
                text = "\n\n".join(paragraphs[:i]).rstrip()
                break
            seen[key] = i

    return text

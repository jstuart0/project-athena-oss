"""
Project Athena Orchestrator Service

LangGraph-based state machine that coordinates between:
- Intent classification
- Home Assistant control
- RAG services for information retrieval
- LLM synthesis
- Response validation
"""

import os

# Load environment variables from .env file BEFORE any other imports
from dotenv import load_dotenv
load_dotenv()

import json
import time
import hashlib
import asyncio
import subprocess
import signal
import re
from typing import Dict, Any, Optional, List, Literal, Tuple
from contextlib import asynccontextmanager
from enum import Enum

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, END
from prometheus_client import Counter, Histogram, generate_latest
from starlette.responses import Response

# Add to Python path for imports
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.logging_config import configure_logging
from shared.ha_client import HomeAssistantClient
from shared.llm_router import get_llm_router, LLMRouter
from shared.cache import CacheClient
from shared.admin_config import get_admin_client
from shared.base_knowledge_utils import get_knowledge_context_for_user, get_home_address_for_user
from shared.tracing import RequestTracingMiddleware, get_tracing_headers
from shared.errors import register_exception_handlers, RateLimitError, ServiceUnavailableError
from shared.service_registry import get_service_url as registry_get_service_url
from shared.metrics import record_tool_execution, get_metrics_text, record_timing_metrics

# Parallel search imports
from orchestrator.search_providers.parallel_search import ParallelSearchEngine
from orchestrator.search_providers.result_fusion import ResultFusion

# Session manager imports
from orchestrator.session_manager import (
    get_session_manager, SessionManager,
    get_session_summary, update_session_summary
)
from orchestrator.config_loader import get_config
from orchestrator.timing import TimingTracker
from orchestrator.tts_normalizer import normalize_for_tts

# RAG validation imports
from orchestrator.rag_validator import validator, ValidationResult
from orchestrator.search_providers.intent_classifier import IntentClassifier

# Complexity detection
from orchestrator.complexity_detector import determine_complexity, get_complexity_with_override

# Smart home control imports
from orchestrator.ha_entity_manager import HAEntityManager
from orchestrator.smart_home_controller import SmartHomeController
from orchestrator.sequence_executor import SequenceExecutor, detect_sequence_intent
from orchestrator.automation_agent import AutomationAgent, should_use_automation_agent

# Music playback imports
from orchestrator.music_handler import MusicHandler, get_music_handler

# TV control imports
from orchestrator.tv_handler import AppleTVHandler, get_tv_handler

# Follow-me audio imports
from orchestrator.follow_me_audio import (
    FollowMeAudioService, FollowMeConfig, FollowMeMode,
    initialize_follow_me, get_follow_me_service
)

# Resilience pattern imports
from orchestrator.rag_client import get_rag_client, initialize_rag_client
from orchestrator.circuit_breaker import get_circuit_breaker_registry
from orchestrator.rate_limiter import get_rate_limiter_registry

# Semantic query caching for latency optimization
from orchestrator.semantic_cache import get_cached_response, cache_response, extract_semantic_intent

# Sentence buffering for LLM streaming pipeline
from orchestrator.sentence_buffer import SentenceBuffer, stream_with_sentence_buffering

# Privacy filter for cloud LLM routing
from shared.privacy_filter import (
    get_privacy_filter, configure_privacy_filter,
    filter_for_cloud, should_block_for_cloud
)

# Modular context imports
from orchestrator.context import (
    detect_context_reference,
    detect_strong_intent,
    detect_location_correction,
    CONTEXT_REF_PATTERNS,
    ROOM_INDICATORS,
)
from orchestrator.utils.constants import DEFAULT_LOCATION, DEFAULT_CITY, CITY_STATE_MAP

# SMS integration imports
from sms.content_detector import detect_textable_content, extract_sms_content
from sms.text_me_that import is_text_me_that_request
from sms.service import get_sms_service

# Intent discovery imports
from orchestrator.intent_discovery import discover_intent, record_intent_metric, INTENT_DISCOVERY_CONFIG
from orchestrator.config_loader import ADMIN_API_URL

# Memory manager imports
from orchestrator.memory_manager import get_memory_manager, MemoryManager

# Performance optimization imports (2026-01-12)
from orchestrator.airport_lookup import resolve_flight_parameters, is_airport_code
from orchestrator.search_preclassifier import preclassify_query, IntentMatch
from orchestrator.ha_status_optimizer import (
    optimize_status_query, should_skip_synthesis, detect_status_query_type
)

# Self-building tools imports
from orchestrator.self_building_tools import (
    SelfBuildingToolsFactory,
    generate_tool_from_request
)

# Event system imports for real-time pipeline monitoring
try:
    from shared.events import (
        EventEmitterFactory,
        emit_session_start,
        emit_session_end,
        emit_intent_classified,
        emit_tool_selected,
        emit_tool_executing,
        emit_tool_complete,
        emit_tool_error,
        emit_llm_generating,
        emit_llm_complete,
        emit_response_ready,
    )
    EVENTS_AVAILABLE = True
except ImportError:
    EVENTS_AVAILABLE = False

# Tool registry for unified static + dynamic tools
try:
    from shared.tool_registry import ToolRegistryFactory, ToolSource
    TOOL_REGISTRY_AVAILABLE = True
except ImportError:
    TOOL_REGISTRY_AVAILABLE = False

# Configure logging
logger = configure_logging("orchestrator")

if not EVENTS_AVAILABLE:
    logger.warning("Event system not available - Admin Jarvis monitoring disabled")


# =============================================================================
# Feature Flag Helper
# =============================================================================

# Feature flag cache for orchestrator
_orch_feature_flag_cache: dict = {}
_orch_feature_flag_cache_ttl = 60.0  # seconds


async def get_feature_flag(flag_name: str, default: bool = False) -> bool:
    """
    Get feature flag value with caching.

    Args:
        flag_name: Name of the feature flag
        default: Default value if flag not found or on error

    Returns:
        Boolean flag value
    """
    import time

    # Check cache first
    if flag_name in _orch_feature_flag_cache:
        cached_time, cached_value = _orch_feature_flag_cache[flag_name]
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
                        value = flag.get("enabled", default)
                        _orch_feature_flag_cache[flag_name] = (time.time(), value)
                        return value
    except Exception as e:
        logger.warning("feature_flag_fetch_failed", flag=flag_name, error=str(e))

    return default


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

    # Check cache first
    if cache_key in _orch_feature_flag_cache:
        cached_time, cached_value = _orch_feature_flag_cache[cache_key]
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
                        _orch_feature_flag_cache[cache_key] = (time.time(), result)
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

    # Check cache first
    if flag_name in _orch_feature_flag_cache:
        cached_time, cached_value = _orch_feature_flag_cache[flag_name]
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
                        _orch_feature_flag_cache[flag_name] = (time.time(), mode)
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

    # Check cache first
    if flag_name in _orch_feature_flag_cache:
        cached_time, cached_value = _orch_feature_flag_cache[flag_name]
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
                        _orch_feature_flag_cache[flag_name] = (time.time(), mode)
                        return mode
    except Exception as e:
        logger.warning("weather_provider_mode_fetch_failed", error=str(e))

    return "standard"  # Default to standard weather service


# =============================================================================
# Post-Synthesis Web Search Fallback
# =============================================================================

_post_synthesis_fallback_cache: Dict[str, Tuple[float, Dict]] = {}
_post_synthesis_fallback_cache_ttl = 60.0  # 60 second cache


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


async def maybe_post_synthesis_fallback(state: 'OrchestratorState') -> bool:
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
        global parallel_search_engine
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
            # get_model_for_component is defined in this file
            synthesis_model = await get_model_for_component("response_synthesis")
            synthesis_start = time.time()

            synthesis_result = await llm_router.generate(
                model=synthesis_model,
                prompt=synthesis_prompt,
                temperature=0.7,
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
# Intent Routing Strategy (Cascading Fallback System)
# =============================================================================

_intent_routing_cache: Dict[str, Tuple[float, str]] = {}
_intent_routing_cache_ttl = 60.0  # 60 second cache


async def get_intent_routing_strategy(intent_name: str) -> str:
    """
    Get routing strategy for an intent from admin API.

    Returns:
        'cascading' - Direct RAG first, fallback to tool calling on failure (default)
        'always_tool_calling' - Skip direct RAG, always use LLM tool selection
        'direct_only' - Never fall back to tool calling
    """
    cache_key = intent_name.lower()

    # Check cache
    if cache_key in _intent_routing_cache:
        cached_time, cached_strategy = _intent_routing_cache[cache_key]
        if time.time() - cached_time < _intent_routing_cache_ttl:
            return cached_strategy

    # Fetch from admin API
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{ADMIN_API_URL}/api/intent-routing/strategy/configs/{cache_key}"
            )
            if response.status_code == 200:
                config = response.json()
                strategy = config.get("routing_strategy", "cascading")
                _intent_routing_cache[cache_key] = (time.time(), strategy)
                logger.debug(
                    "intent_routing_strategy_fetched",
                    intent=intent_name,
                    strategy=strategy
                )
                return strategy
    except Exception as e:
        logger.warning(
            "intent_routing_strategy_fetch_failed",
            intent=intent_name,
            error=str(e)
        )

    # Default to cascading
    return "cascading"


# =============================================================================
# Tool Creation Intent Detection
# =============================================================================

TOOL_CREATION_PATTERNS = [
    "create a tool",
    "create tool",
    "make a tool",
    "build a tool",
    "build tool",
    "make me a tool",
    "create an integration",
    "build an integration",
    "add a capability",
    "add capability",
    "can you create a tool",
    "can you make a tool",
    "i need a tool",
    "create a new tool",
    "make a new tool",
]


def detect_tool_creation_intent(query: str) -> bool:
    """
    Check if the user is asking to create a new tool/capability.

    Returns True if the query contains tool creation patterns.
    """
    query_lower = query.lower().strip()
    for pattern in TOOL_CREATION_PATTERNS:
        if pattern in query_lower:
            return True
    return False


async def handle_tool_creation_request(
    query: str,
    session_id: str,
    user_mode: str
) -> Optional[Dict[str, Any]]:
    """
    Handle a request to create a new tool.

    Returns a response dict if handled, None if tool creation is disabled.
    """
    # Get the self-building tools manager
    manager = SelfBuildingToolsFactory.get()

    # Check if feature is enabled
    if not await manager.check_enabled():
        logger.info("tool_creation_disabled", query=query[:50])
        return None

    # Only allow owner mode to create tools
    if user_mode != "owner":
        return {
            "answer": "Tool creation is only available in owner mode. Please switch to owner mode to create new tools.",
            "intent": "tool_creation",
            "success": False
        }

    try:
        # Get LLM router for tool generation
        llm_router = get_llm_router()

        # Generate tool definition from the request
        logger.info("generating_tool_definition", query=query[:100])

        result = await generate_tool_from_request(
            user_request=query,
            llm_router=llm_router,
            model="llama3.1:8b"
        )

        if not result.get("success"):
            return {
                "answer": f"I couldn't generate a tool definition: {result.get('error', 'Unknown error')}. Please try describing what you need more specifically.",
                "intent": "tool_creation",
                "success": False
            }

        tool_def = result.get("tool_definition", {})

        # Submit the proposal with all parameters from LLM
        proposal_result = await manager.propose_tool(
            name=tool_def.get("name", "unnamed_tool"),
            description=tool_def.get("description", ""),
            trigger_phrases=tool_def.get("trigger_phrases", [query]),
            api_url=tool_def.get("api_url"),
            api_method=tool_def.get("api_method", "GET"),
            transform_code=tool_def.get("transform_code"),
            query_params=tool_def.get("query_params"),
            required_api_key=tool_def.get("required_api_key"),
            api_key_param=tool_def.get("api_key_param"),
            created_by="llm"
        )

        if proposal_result.get("success"):
            logger.info(
                "tool_proposal_created",
                proposal_id=proposal_result.get("proposal_id"),
                name=tool_def.get("name")
            )
            return {
                "answer": (
                    f"I've created a tool proposal for '{tool_def.get('name', 'your tool')}'. "
                    f"The proposal is now pending approval in the Admin panel. "
                    f"Once approved, the tool will be available for use. "
                    f"\n\nProposal ID: {proposal_result.get('proposal_id')}"
                ),
                "intent": "tool_creation",
                "success": True,
                "proposal_id": proposal_result.get("proposal_id"),
                "tool_name": tool_def.get("name")
            }
        else:
            error_msg = proposal_result.get("error", "Unknown error")
            error_code = proposal_result.get("error_code", "")

            if error_code == "FEATURE_DISABLED":
                return None  # Let the normal flow handle it

            return {
                "answer": f"I couldn't submit the tool proposal: {error_msg}",
                "intent": "tool_creation",
                "success": False
            }

    except Exception as e:
        logger.error("tool_creation_failed", error=str(e))
        return {
            "answer": f"An error occurred while creating the tool: {str(e)}",
            "intent": "tool_creation",
            "success": False
        }

# Metrics
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

# Global clients
ha_client: Optional[HomeAssistantClient] = None
llm_router: Optional[LLMRouter] = None
cache_client: Optional[CacheClient] = None
session_manager: Optional[SessionManager] = None
rag_client: Optional[Any] = None  # Unified RAG client with circuit breakers
mode_client: Optional[httpx.AsyncClient] = None  # Phase 2: Guest mode integration
entity_manager: Optional[HAEntityManager] = None
smart_controller: Optional[SmartHomeController] = None
sequence_executor: Optional[SequenceExecutor] = None
automation_agent: Optional[AutomationAgent] = None
music_handler: Optional[MusicHandler] = None
tv_handler: Optional[AppleTVHandler] = None
follow_me_service: Optional[FollowMeAudioService] = None  # Follow-me audio
intent_classifier: Optional[IntentClassifier] = None  # Multi-intent detection

# Tool schema cache (OPTIMIZATION: Cache tool schemas to avoid regeneration)
tool_schema_cache: Dict[str, List[Dict[str, Any]]] = {}

# Tool config cache (for fallback settings - stores raw tool configs from admin API)
tool_config_cache: Dict[str, List[Dict[str, Any]]] = {}

# ============================================================================
# Conversation Context System
# ============================================================================

class ConversationContext(BaseModel):
    """
    Stores conversation context for continuity across turns.
    Allows follow-up queries like "do that again", "what about tomorrow?", "turn them off".
    """
    intent: str = Field(..., description="Last intent type (control, weather, sports, etc.)")
    query: str = Field(..., description="Original query text")
    entities: Dict[str, Any] = Field(default_factory=dict, description="Extracted entities (room, location, team, etc.)")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Action parameters (colors, brightness, etc.)")
    response: Optional[str] = Field(None, description="Last response given")
    timestamp: float = Field(default_factory=time.time, description="When context was stored")

    class Config:
        extra = "allow"

# Note: CONTEXT_REF_PATTERNS, ROOM_INDICATORS, and detect_context_reference
# are now imported from orchestrator.context module

# In-memory fallback for conversation context (used when Redis unavailable)
# Structure: {session_id: {"context": ConversationContext, "expires_at": float}}
_memory_context: Dict[str, Dict[str, Any]] = {}
CONTEXT_TTL_SECONDS = 300  # 5 minute TTL


def _cleanup_expired_contexts():
    """Remove expired contexts from memory (called periodically)."""
    now = time.time()
    expired = [sid for sid, data in _memory_context.items() if data.get("expires_at", 0) < now]
    for sid in expired:
        del _memory_context[sid]
    if expired:
        logger.debug(f"Cleaned up {len(expired)} expired contexts from memory")


async def get_conversation_context(session_id: str) -> Optional[ConversationContext]:
    """Retrieve conversation context from Redis (with in-memory fallback)."""
    if not session_id:
        return None

    # Try Redis first with timeout to prevent hanging on dead Redis
    if cache_client and cache_client.client:
        try:
            context_key = f"athena:context:{session_id}"
            # Use asyncio.wait_for to prevent hanging on dead Redis connections
            context_json = await asyncio.wait_for(
                cache_client.client.get(context_key),
                timeout=2.0  # 2 second timeout
            )
            if context_json:
                data = json.loads(context_json)
                logger.debug(f"Retrieved context from Redis for session {session_id[:8]}...")
                return ConversationContext(**data)
        except asyncio.TimeoutError:
            logger.warning(f"Redis context retrieval timed out, trying memory fallback")
        except Exception as e:
            logger.warning(f"Redis context retrieval failed, trying memory fallback: {e}")

    # Fallback to in-memory storage
    if session_id in _memory_context:
        data = _memory_context[session_id]
        if data.get("expires_at", 0) > time.time():
            logger.debug(f"Retrieved context from memory for session {session_id[:8]}...")
            return data.get("context")
        else:
            # Expired - clean it up
            del _memory_context[session_id]

    return None


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
    _memory_context[session_id] = {
        "context": context,
        "expires_at": time.time() + ttl
    }

    # Periodic cleanup (every ~10 stores)
    if len(_memory_context) > 0 and len(_memory_context) % 10 == 0:
        _cleanup_expired_contexts()

    if not redis_success:
        logger.info(f"Stored context in memory for session {session_id[:8]}...: intent={intent}")

    return True


def merge_with_context(
    new_query: str,
    new_entities: Dict[str, Any],
    context: ConversationContext,
    ref_info: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Merge new query info with previous context based on reference type.
    Returns merged entities and parameters.
    """
    merged = {
        "entities": context.entities.copy(),
        "parameters": context.parameters.copy(),
        "intent": context.intent,
        "needs_history_context": False,  # Flag for pronoun-based follow-ups
    }

    # If new query has explicit entities, use them (override context)
    for key, value in new_entities.items():
        if value:  # Only override if new value is not None/empty
            merged["entities"][key] = value

    # Handle specific reference types
    if "modifier" in ref_info["ref_types"]:
        # Modifiers adjust parameters but keep same target
        query_lower = new_query.lower()
        if "brighter" in query_lower:
            merged["parameters"]["brightness_adjust"] = "increase"
        elif "dimmer" in query_lower:
            merged["parameters"]["brightness_adjust"] = "decrease"
        elif "different color" in query_lower:
            merged["parameters"]["color_change"] = True

    if "temporal" in ref_info["ref_types"]:
        # Temporal references update the time but keep the query type
        query_lower = new_query.lower()
        if "tomorrow" in query_lower:
            merged["entities"]["time_ref"] = "tomorrow"
        elif "this weekend" in query_lower:
            merged["entities"]["time_ref"] = "this_weekend"
        elif "next week" in query_lower:
            merged["entities"]["time_ref"] = "next_week"

    # Pronoun-based follow-ups need conversation history to resolve the referent
    # Personal pronouns referring to people from previous response
    if "pronoun" in ref_info["ref_types"]:
        personal_pronouns = {"he", "she", "him", "her", "his", "they", "them", "their"}
        words = set(new_query.lower().split())
        if words & personal_pronouns:
            # Query contains personal pronouns - needs LLM to resolve from history
            merged["needs_history_context"] = True
            # Store previous response for context
            if context.response:
                merged["entities"]["previous_response"] = context.response[:500]
            merged["entities"]["previous_query"] = context.query

    return merged

# Configuration
# Phase 1 RAG Services
WEATHER_SERVICE_URL = os.getenv("RAG_WEATHER_URL", "http://localhost:8010")
ONECALL_SERVICE_URL = os.getenv("RAG_ONECALL_URL", "http://localhost:8021")
AIRPORTS_SERVICE_URL = os.getenv("RAG_AIRPORTS_URL", "http://localhost:8011")
FLIGHTS_SERVICE_URL = os.getenv("RAG_FLIGHTS_URL", "http://localhost:8012")

# Phase 2 RAG Services
EVENTS_SERVICE_URL = os.getenv("RAG_EVENTS_URL", "http://localhost:8013")
STREAMING_SERVICE_URL = os.getenv("RAG_STREAMING_URL", "http://localhost:8014")
NEWS_SERVICE_URL = os.getenv("RAG_NEWS_URL", "http://localhost:8015")
STOCKS_SERVICE_URL = os.getenv("RAG_STOCKS_URL", "http://localhost:8016")
SPORTS_SERVICE_URL = os.getenv("RAG_SPORTS_URL", "http://localhost:8017")
WEBSEARCH_SERVICE_URL = os.getenv("RAG_WEBSEARCH_URL", "http://localhost:8018")
DINING_SERVICE_URL = os.getenv("RAG_DINING_URL", "http://localhost:8019")
RECIPES_SERVICE_URL = os.getenv("RAG_RECIPES_URL", "http://localhost:8020")
DIRECTIONS_SERVICE_URL = os.getenv("RAG_DIRECTIONS_URL", "http://localhost:8030")

# Phase 2: Mode service
MODE_SERVICE_URL = os.getenv("MODE_SERVICE_URL", "http://localhost:8022")

# Notifications service (for proactive notification preferences)
NOTIFICATIONS_SERVICE_URL = os.getenv("NOTIFICATIONS_SERVICE_URL", "http://localhost:8050")

# LLM
OLLAMA_URL = os.getenv("LLM_SERVICE_URL", "http://localhost:11434")

# Intent categories
class IntentCategory(str, Enum):
    CONTROL = "control"  # Home Assistant control
    WEATHER = "weather"  # Weather information
    AIRPORTS = "airports"  # Airport/flight info
    SPORTS = "sports"  # Sports information
    FLIGHTS = "flights"  # Flight tracking (Phase 2)
    EVENTS = "events"  # Events and venues (Phase 2)
    STREAMING = "streaming"  # Movies and TV shows (Phase 2)
    NEWS = "news"  # News and headlines (Phase 2)
    STOCKS = "stocks"  # Stock market data (Phase 2)
    RECIPES = "recipes"  # Recipe search (Phase 2)
    DINING = "dining"  # Restaurant search (Phase 2)
    DIRECTIONS = "directions"  # Navigation and route planning (Phase 2)
    WEBSEARCH = "websearch"  # Explicit web search request ("search the web for X")
    TEXT_ME_THAT = "text_me_that"  # SMS: User wants info texted to them
    MUSIC_PLAY = "music_play"  # Music playback (play jazz, play Pink Floyd)
    MUSIC_CONTROL = "music_control"  # Music controls (pause, next, volume)
    TV_CONTROL = "tv_control"  # Apple TV control (open Netflix, turn on TV)
    NOTIFICATION_PREF = "notification_pref"  # Opt-out/opt-in for proactive notifications
    TESLA = "tesla"  # Tesla vehicle queries (owner mode only - blocked for guests)
    GENERAL_INFO = "general_info"  # General knowledge
    UNKNOWN = "unknown"  # Unclear intent

# Model tiers (all preloaded with keep_alive=-1)
class ModelTier(str, Enum):
    CLASSIFIER = "qwen3:4b"  # Fast classification - matches database config
    SMALL = "qwen3:4b-instruct-2507-q4_K_M"  # Fast tool calling - matches database config
    MEDIUM = "qwen3:4b-instruct-2507-q4_K_M"  # Fast for most tasks
    LARGE = "qwen3:8b"  # Complex queries - matches database config
    SYNTHESIS = "qwen3:4b-instruct-2507-q4_K_M"  # Response synthesis - matches database config


# Fallback model values if database unavailable
# These can be overridden via environment variables: ATHENA_FALLBACK_MODEL_<COMPONENT_NAME>
# IMPORTANT: Complex should use a MORE capable model than simple for meaningful escalation
FALLBACK_MODELS = {
    "intent_classifier": os.getenv("ATHENA_FALLBACK_MODEL_INTENT_CLASSIFIER", "qwen3:4b"),
    "tool_calling_simple": os.getenv("ATHENA_FALLBACK_MODEL_TOOL_CALLING_SIMPLE", "qwen3:4b-instruct-2507-q4_K_M"),
    "tool_calling_complex": os.getenv("ATHENA_FALLBACK_MODEL_TOOL_CALLING_COMPLEX", "qwen2.5:14b"),  # UPGRADED from 4b for meaningful escalation
    "tool_calling_super_complex": os.getenv("ATHENA_FALLBACK_MODEL_TOOL_CALLING_SUPER_COMPLEX", "qwen2.5:32b"),  # Top tier
    "response_synthesis": os.getenv("ATHENA_FALLBACK_MODEL_RESPONSE_SYNTHESIS", "qwen3:4b-instruct-2507-q4_K_M"),
    "response_synthesis_complex": os.getenv("ATHENA_FALLBACK_MODEL_RESPONSE_SYNTHESIS_COMPLEX", "qwen2.5:14b"),  # For complex responses
    "fact_check_validation": os.getenv("ATHENA_FALLBACK_MODEL_FACT_CHECK_VALIDATION", "qwen3:8b"),
    "conversation_summarizer": os.getenv("ATHENA_FALLBACK_MODEL_CONVERSATION_SUMMARIZER", "qwen3:4b"),
}

# Component model cache (performance optimization)
# Caches all component models to avoid per-request database lookups
_component_model_cache: Dict[str, Dict[str, Any]] = {}
_component_model_cache_time: float = 0
COMPONENT_MODEL_CACHE_TTL = 300  # 5 minutes

# Origin placeholder patterns cache (for directions override)
# Fetched from admin API, cached to avoid per-request lookups
_origin_placeholder_cache: set = set()
_origin_placeholder_cache_time: float = 0
ORIGIN_PLACEHOLDER_CACHE_TTL = 300  # 5 minutes

# Default patterns (used if API fetch fails)
DEFAULT_ORIGIN_PLACEHOLDERS = {
    "current location", "my location", "here", "current",
    "my current location", "starting point", "start",
    "user location", "your location", "origin"
}


async def get_origin_placeholder_patterns() -> set:
    """
    Get origin placeholder patterns from admin API (cached).

    These are placeholder values that LLMs use instead of real addresses.
    When detected as the origin in a get_directions call, they are replaced
    with the user's actual current location.
    """
    global _origin_placeholder_cache, _origin_placeholder_cache_time

    now = time.time()
    if _origin_placeholder_cache and (now - _origin_placeholder_cache_time < ORIGIN_PLACEHOLDER_CACHE_TTL):
        return _origin_placeholder_cache

    try:
        admin_client = get_admin_client()
        response = await admin_client.client.get(
            f"{admin_client.admin_url}/api/settings/directions-origin-placeholders"
        )
        response.raise_for_status()
        data = response.json()

        patterns = set(data.get("patterns_list", []))
        if patterns:
            _origin_placeholder_cache = patterns
            _origin_placeholder_cache_time = now
            logger.debug(f"Loaded {len(patterns)} origin placeholder patterns from admin API")
            return patterns

    except Exception as e:
        logger.warning(f"Failed to fetch origin placeholder patterns from admin API: {e}")

    # Return cached patterns if available, otherwise defaults
    if _origin_placeholder_cache:
        return _origin_placeholder_cache
    return DEFAULT_ORIGIN_PLACEHOLDERS


def extract_date_from_query(query: str) -> Optional[tuple]:
    """
    Extract a specific date from a natural language query.

    Returns tuple of (date_str_display, date_str_api) or None if no specific date found.
    - date_str_display: Human readable format like "Saturday, December 6, 2025"
    - date_str_api: API format like "2025-12-06"
    """
    import re
    from datetime import datetime, timedelta

    query_lower = query.lower()
    today = datetime.now()
    current_year = today.year

    # Month name mapping
    months = {
        'january': 1, 'jan': 1,
        'february': 2, 'feb': 2,
        'march': 3, 'mar': 3,
        'april': 4, 'apr': 4,
        'may': 5,
        'june': 6, 'jun': 6,
        'july': 7, 'jul': 7,
        'august': 8, 'aug': 8,
        'september': 9, 'sep': 9, 'sept': 9,
        'october': 10, 'oct': 10,
        'november': 11, 'nov': 11,
        'december': 12, 'dec': 12
    }

    # Pattern 1: "December 6th", "Dec 6", "December 6"
    pattern1 = r'\b(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sep|sept|october|oct|november|nov|december|dec)\s+(\d{1,2})(?:st|nd|rd|th)?\b'
    match = re.search(pattern1, query_lower)
    if match:
        month_name = match.group(1)
        day = int(match.group(2))
        month = months.get(month_name)
        if month and 1 <= day <= 31:
            # Determine year - if the date is in the past, use next year
            target_date = datetime(current_year, month, day)
            if target_date < today:
                target_date = datetime(current_year + 1, month, day)

            display_str = target_date.strftime("%A, %B %d, %Y")
            api_str = target_date.strftime("%Y-%m-%d")
            return (display_str, api_str)

    # Pattern 2: "6th of December", "6 December"
    pattern2 = r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sep|sept|october|oct|november|nov|december|dec)\b'
    match = re.search(pattern2, query_lower)
    if match:
        day = int(match.group(1))
        month_name = match.group(2)
        month = months.get(month_name)
        if month and 1 <= day <= 31:
            target_date = datetime(current_year, month, day)
            if target_date < today:
                target_date = datetime(current_year + 1, month, day)

            display_str = target_date.strftime("%A, %B %d, %Y")
            api_str = target_date.strftime("%Y-%m-%d")
            return (display_str, api_str)

    # Pattern 3: MM/DD or MM-DD
    pattern3 = r'\b(\d{1,2})[/-](\d{1,2})\b'
    match = re.search(pattern3, query_lower)
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            target_date = datetime(current_year, month, day)
            if target_date < today:
                target_date = datetime(current_year + 1, month, day)

            display_str = target_date.strftime("%A, %B %d, %Y")
            api_str = target_date.strftime("%Y-%m-%d")
            return (display_str, api_str)

    return None


async def get_model_for_component(component_name: str) -> str:
    """Get model for a component with caching to reduce database calls.

    Uses a 5-minute TTL cache to avoid per-request database lookups.
    Falls back to FALLBACK_MODELS if cache refresh fails.
    """
    global _component_model_cache, _component_model_cache_time

    now = time.time()

    # Refresh cache if expired
    if now - _component_model_cache_time > COMPONENT_MODEL_CACHE_TTL:
        try:
            admin_client = get_admin_client()
            all_models = await admin_client.get_all_component_models()
            _component_model_cache = {m['component_name']: m for m in all_models}
            _component_model_cache_time = now
            logger.debug("component_model_cache_refreshed", count=len(_component_model_cache))
        except Exception as e:
            logger.warning(f"Failed to refresh component model cache: {e}")
            # If cache is completely empty, try single lookup as fallback
            if not _component_model_cache:
                try:
                    admin_client = get_admin_client()
                    config = await admin_client.get_component_model(component_name)
                    if config and config.get("enabled"):
                        return config.get("model_name")
                except Exception:
                    pass

    # Return from cache or fallback
    if component_name in _component_model_cache:
        config = _component_model_cache[component_name]
        if config.get("enabled"):
            return config.get("model_name")

    return FALLBACK_MODELS.get(component_name, "qwen3:4b-instruct-2507-q4_K_M")


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
    global _component_model_cache, _component_model_cache_time

    now = time.time()

    # Refresh cache if expired
    if now - _component_model_cache_time > COMPONENT_MODEL_CACHE_TTL:
        try:
            admin_client = get_admin_client()
            all_models = await admin_client.get_all_component_models()
            _component_model_cache = {m['component_name']: m for m in all_models}
            _component_model_cache_time = now
            logger.debug("component_model_cache_refreshed", count=len(_component_model_cache))
        except Exception as e:
            logger.warning(f"Failed to refresh component model cache: {e}")

    # Return from cache or fallback
    if component_name in _component_model_cache:
        config = _component_model_cache[component_name]
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
        "model_name": FALLBACK_MODELS.get(component_name, "qwen3:4b-instruct-2507-q4_K_M"),
        "backend_type": "ollama",
        "temperature": None,
        "max_tokens": None,
        "disable_thinking": False,
        "enabled": True,
    }


# =========================================================================
# Hybrid Routing Logic - Phase 5
# =========================================================================

# Cloud routing configuration cache
_cloud_routing_config: Dict[str, Any] = {}
_cloud_routing_config_time: float = 0
CLOUD_ROUTING_CONFIG_TTL = 60  # Refresh every minute


async def get_cloud_routing_config() -> Dict[str, bool]:
    """Get cloud routing feature flags with caching."""
    global _cloud_routing_config, _cloud_routing_config_time

    now = time.time()
    if now - _cloud_routing_config_time < CLOUD_ROUTING_CONFIG_TTL:
        return _cloud_routing_config

    try:
        admin_client = get_admin_client()

        # Fetch cloud LLM feature flags
        cloud_enabled = await admin_client.get_feature_flag("cloud_llm_enabled")
        cloud_for_complex = await admin_client.get_feature_flag("cloud_llm_for_complex")
        cloud_fallback = await admin_client.get_feature_flag("cloud_llm_fallback")
        privacy_filter = await admin_client.get_feature_flag("cloud_llm_privacy_filter")

        _cloud_routing_config = {
            "cloud_enabled": cloud_enabled.get("enabled", False) if cloud_enabled else False,
            "cloud_for_complex": cloud_for_complex.get("enabled", False) if cloud_for_complex else False,
            "cloud_fallback": cloud_fallback.get("enabled", False) if cloud_fallback else False,
            "privacy_filter": privacy_filter.get("enabled", True) if privacy_filter else True,
        }
        _cloud_routing_config_time = now

        logger.debug(
            "cloud_routing_config_refreshed",
            config=_cloud_routing_config
        )

    except Exception as e:
        logger.warning(f"Failed to refresh cloud routing config: {e}")
        # Use cached values or defaults
        if not _cloud_routing_config:
            _cloud_routing_config = {
                "cloud_enabled": False,
                "cloud_for_complex": False,
                "cloud_fallback": False,
                "privacy_filter": True,
            }

    return _cloud_routing_config


async def select_llm_backend(
    query: str,
    complexity: str,
    intent: Optional["IntentCategory"] = None,
    component: str = "response_synthesis"
) -> tuple[str, Optional[str]]:
    """
    Select LLM backend based on query complexity and cloud routing settings.

    Returns:
        Tuple of (model_name, backend_type or None for auto)

    The routing logic:
    1. If cloud is disabled, always use local model
    2. If privacy filter blocks the query, use local
    3. If complexity is COMPLEX or SUPER_COMPLEX and cloud_for_complex is enabled, use cloud
    4. Otherwise use local model from component config
    """
    config = await get_cloud_routing_config()

    # Default to local model
    local_model = await get_model_for_component(component)

    # If cloud not enabled, always use local
    if not config.get("cloud_enabled"):
        return local_model, None

    # Check privacy filter
    if config.get("privacy_filter"):
        should_block, reason = should_block_for_cloud(query)
        if should_block:
            logger.info(
                "cloud_blocked_by_privacy",
                reason=reason,
                using_local=local_model
            )
            return local_model, None

    # Check if we should route complex queries to cloud
    if config.get("cloud_for_complex") and complexity in ("COMPLEX", "complex", "super_complex", "SUPER_COMPLEX"):
        # Get preferred cloud model
        cloud_model = await _get_preferred_cloud_model()
        if cloud_model:
            model_name, backend_type = cloud_model

            # Apply privacy filter before sending to cloud
            if config.get("privacy_filter"):
                filtered_query = filter_for_cloud(query)
                if filtered_query != query:
                    logger.info(
                        "privacy_filter_applied_to_cloud_query",
                        original_len=len(query),
                        filtered_len=len(filtered_query)
                    )

            logger.info(
                "routing_to_cloud",
                complexity=complexity,
                model=model_name,
                backend=backend_type
            )
            return model_name, backend_type

    # Default to local
    return local_model, None


async def _get_preferred_cloud_model() -> Optional[tuple[str, str]]:
    """
    Get the user's preferred cloud model if configured.

    Checks enabled cloud providers in priority order:
    1. OpenAI (most common)
    2. Anthropic
    3. Google

    Returns:
        Tuple of (model_name, backend_type) or None if no cloud provider enabled
    """
    try:
        admin_client = get_admin_client()

        # Check providers in priority order
        providers = ["openai", "anthropic", "google"]

        for provider in providers:
            try:
                # Check if provider has credentials configured
                response = await admin_client._client.get(
                    f"/api/cloud-providers/{provider}/health"
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("healthy"):
                        # Get default model for this provider
                        provider_config = await admin_client._client.get(
                            f"/api/cloud-providers/{provider}"
                        )
                        if provider_config.status_code == 200:
                            config = provider_config.json()
                            if config.get("enabled"):
                                default_model = config.get("default_model")
                                if default_model:
                                    return (default_model, provider)
            except Exception:
                continue

    except Exception as e:
        logger.warning(f"Failed to get preferred cloud model: {e}")

    return None


# Orchestrator state
class OrchestratorState(BaseModel):
    """State that flows through the LangGraph state machine."""

    # Input
    query: str = Field(..., description="User's query")
    mode: Literal["owner", "guest"] = Field("owner", description="User mode")
    room: str = Field("unknown", description="Room/zone identifier")
    temperature: float = Field(0.7, description="LLM temperature")
    session_id: Optional[str] = Field(None, description="Conversation session ID")
    interface_type: Literal["voice", "text", "chat"] = Field("voice", description="Interface type for response formatting")

    # Barge-in / Interruption context (when user interrupts previous response)
    interruption_context: Optional[Dict[str, Any]] = Field(None, description="Context when user interrupted (previous_query, interrupted_response, audio_position_ms)")

    # Conversation context
    conversation_history: List[Dict[str, str]] = Field(default_factory=list, description="Previous conversation messages")
    history_summary: str = Field("", description="Summarized conversation context (for summarized mode)")
    context_ref_info: Dict[str, Any] = Field(default_factory=dict, description="Detected context reference info")
    prev_context: Optional[Dict[str, Any]] = Field(None, description="Previous conversation context from Redis")

    # Phase 2: Guest Mode permissions
    permissions: Dict[str, Any] = Field(default_factory=dict, description="User permissions from mode service")

    # SMS Integration: Additional context (phone_number, calendar_event_id, guest_name, etc.)
    context: Dict[str, Any] = Field(default_factory=dict, description="Additional context for SMS and guest integration")

    # Classification
    intent: Optional[IntentCategory] = None
    confidence: float = 0.0
    entities: Dict[str, Any] = Field(default_factory=dict)
    complexity: Optional[str] = Field(None, description="Query complexity: simple, complex, super_complex")

    # Model selection
    model_tier: Optional[ModelTier] = None
    model_component: Optional[str] = None  # Component name for model lookup

    # Retrieved data
    retrieved_data: Dict[str, Any] = Field(default_factory=dict)
    data_source: Optional[str] = None

    # Response
    answer: Optional[str] = None
    citations: List[str] = Field(default_factory=list)
    skip_synthesis: bool = Field(False, description="Skip LLM synthesis (used by status query optimization)")
    was_truncated: bool = Field(False, description="Whether response was truncated due to token limit")

    # Validation
    validation_passed: bool = True
    validation_reason: Optional[str] = None
    validation_details: List[str] = Field(default_factory=list)

    # Metadata
    request_id: str = Field(default_factory=lambda: hashlib.md5(str(time.time()).encode()).hexdigest()[:8])
    start_time: float = Field(default_factory=time.time)
    node_timings: Dict[str, float] = Field(default_factory=dict)
    timing_tracker: Optional[Any] = Field(default=None, exclude=True)  # TimingTracker instance for granular timing
    error: Optional[str] = None

    # LLM Token Metrics (for frontend display)
    llm_tokens: int = Field(0, description="Number of tokens generated by LLM")
    llm_tokens_per_second: float = Field(0.0, description="LLM inference throughput")

    # SMS Integration
    offer_sms: bool = Field(False, description="Whether to offer SMS for this response")
    sms_content: Optional[str] = Field(None, description="Content to send via SMS if offered")
    sms_content_type: Optional[str] = Field(None, description="Type of detected SMS content")

    # Intent Discovery
    is_novel_intent: bool = Field(False, description="Whether this is a novel/discovered intent")
    emerging_intent_id: Optional[int] = Field(None, description="ID of the emerging intent if novel")
    novel_intent_name: Optional[str] = Field(None, description="Canonical name of the novel intent")

    # Memory Context
    memory_context: str = Field("", description="Relevant memories for LLM context augmentation")

    # Multi-Intent Support
    is_multi_intent: bool = Field(False, description="Whether this query contains multiple intents")
    intent_parts: List[str] = Field(default_factory=list, description="Split query parts for multi-intent")
    intent_results: List[Dict[str, Any]] = Field(default_factory=list, description="Results from each intent part")
    current_intent_index: int = Field(0, description="Current intent being processed")

    # Pronoun Resolution Support
    needs_history_context: bool = Field(False, description="Whether query needs conversation history for pronoun resolution")


async def kill_port(port: int, service_name: str = "service"):
    """Kill any process using the specified port."""
    try:
        # Find process on port using lsof
        result = subprocess.run(
            ['lsof', '-ti', f':{port}'],
            capture_output=True,
            text=True
        )

        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    logger.info(f"Killed existing {service_name} process (PID {pid}) on port {port}")
                except ProcessLookupError:
                    pass  # Process already dead
            await asyncio.sleep(2)  # Wait for port to be released (non-blocking)
        else:
            logger.info(f"No existing process found on port {port}")
    except Exception as e:
        logger.warning(f"Error checking port {port}: {e}")


# Control Agent URL for service management
CONTROL_AGENT_URL = os.getenv("CONTROL_AGENT_URL", "http://localhost:8099")
GATEWAY_PORT = 8000


async def ensure_gateway_running() -> bool:
    """
    Ensure the gateway service is running.

    Checks gateway status via Control Agent and starts it if not running.
    Controlled by START_GATEWAY environment variable (default: true).

    Returns:
        True if gateway is running (or was started), False if failed
    """
    # Check if gateway startup is disabled
    start_gateway = os.getenv("START_GATEWAY", "true").lower() in ("true", "1", "yes")
    if not start_gateway:
        logger.info("gateway_startup_disabled", reason="START_GATEWAY=false")
        return True  # Not an error, just disabled

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Check gateway status
            status_response = await client.get(f"{CONTROL_AGENT_URL}/process/status/{GATEWAY_PORT}")

            if status_response.status_code == 200:
                status = status_response.json()

                if status.get("running"):
                    logger.info("gateway_already_running", pid=status.get("pid"))
                    return True

                # Gateway not running, start it
                logger.info("gateway_not_running", action="starting")
                start_response = await client.post(f"{CONTROL_AGENT_URL}/process/start/{GATEWAY_PORT}")

                if start_response.status_code == 200:
                    result = start_response.json()
                    if result.get("success"):
                        logger.info("gateway_started", message=result.get("message"))
                        return True
                    else:
                        logger.warning("gateway_start_failed", message=result.get("message"))
                        return False
                else:
                    logger.warning("gateway_start_request_failed", status=start_response.status_code)
                    return False
            else:
                logger.warning("gateway_status_check_failed", status=status_response.status_code)
                return False

    except httpx.ConnectError:
        logger.warning("control_agent_unreachable", url=CONTROL_AGENT_URL)
        return False
    except Exception as e:
        logger.warning("gateway_startup_error", error=str(e))
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    global ha_client, llm_router, cache_client, session_manager, rag_client, parallel_search_engine, result_fusion, mode_client, entity_manager, smart_controller, sequence_executor, automation_agent, music_handler, tv_handler, intent_classifier, follow_me_service

    # Kill any existing process on orchestrator port before starting
    orchestrator_port = int(os.getenv("ORCHESTRATOR_PORT", "8001"))
    await kill_port(orchestrator_port, "Orchestrator")

    # Startup
    logger.info("Starting Orchestrator service")

    # Ensure gateway is running (default behavior, disable with START_GATEWAY=false)
    gateway_ok = await ensure_gateway_running()
    if not gateway_ok:
        logger.warning("gateway_not_available", note="Orchestrator will continue without gateway")

    # Initialize admin client
    admin_client = get_admin_client()

    # Try to fetch HA config from Admin API
    ha_url = None
    ha_token = None
    try:
        ha_config = await admin_client.get_home_assistant_config()
        if ha_config and ha_config.get("url") and ha_config.get("token"):
            ha_url = ha_config["url"]
            ha_token = ha_config["token"]
            logger.info("ha_config_from_admin", url=ha_url)
        else:
            # Fallback to environment variables
            ha_url = os.getenv("HA_URL", "http://localhost:8123")
            ha_token = os.getenv("HA_TOKEN", "")
            logger.info("ha_config_from_env", url=ha_url)
    except Exception as e:
        logger.warning("ha_config_fetch_failed", error=str(e))
        # Fallback to environment variables
        ha_url = os.getenv("HA_URL", "http://localhost:8123")
        ha_token = os.getenv("HA_TOKEN", "")
        logger.info("ha_config_from_env_fallback", url=ha_url)

    # Initialize clients
    ha_client = HomeAssistantClient(url=ha_url, token=ha_token) if ha_token else None
    if not ha_client:
        logger.warning("ha_client_not_initialized", reason="No token available")

    # Initialize LLM router with database-driven backend configuration
    llm_router = get_llm_router()
    logger.info(f"LLM Router initialized with admin API: {llm_router.admin_url}")

    # Initialize entity manager for dynamic HA entity discovery
    if ha_token:
        entity_manager = HAEntityManager(ha_url=ha_url, ha_token=ha_token)
        try:
            await entity_manager.refresh_entities()
            logger.info("Entity manager initialized with HA entities cached")
        except Exception as e:
            logger.warning("ha_entity_refresh_failed", error=str(e), msg="HA unavailable, will retry later")

        # Initialize smart home controller with LLM intent extraction
        smart_controller = SmartHomeController(entity_manager, llm_router)
        logger.info("Smart home controller initialized")

        # Initialize sequence executor for multi-step commands with delays
        sequence_executor = SequenceExecutor(smart_controller, ha_client)
        logger.info("Sequence executor initialized for multi-step commands")

        # Initialize automation agent for dynamic automation handling
        admin_client = get_admin_client()
        automation_agent = AutomationAgent(ha_client, llm_router, admin_client, entity_manager)
        logger.info("Automation agent initialized for dynamic automation handling")

        # Initialize music handler for Music Assistant integration
        admin_client = get_admin_client()
        music_handler = get_music_handler(ha_client, admin_client)
        logger.info("Music handler initialized for Music Assistant playback")

        # Initialize TV handler for Apple TV control
        tv_handler = get_tv_handler(ha_client, admin_client)
        logger.info("TV handler initialized for Apple TV control")

        # Initialize follow-me audio service (fully configurable via admin)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                fm_response = await client.get(
                    f"{admin_client.admin_url}/api/follow-me/internal/config"
                )
                follow_me_config_response = fm_response.json() if fm_response.status_code == 200 else None
            if follow_me_config_response and follow_me_config_response.get("config"):
                fm_cfg = follow_me_config_response["config"]
                room_motion_mapping = follow_me_config_response.get("room_motion_mapping", {})
                excluded_rooms = follow_me_config_response.get("excluded_rooms", [])

                # Only initialize if enabled and we have room mappings
                if fm_cfg.get("enabled", False) and room_motion_mapping:
                    follow_me_cfg = FollowMeConfig(
                        enabled=fm_cfg.get("enabled", True),
                        mode=FollowMeMode(fm_cfg.get("mode", "single")),
                        debounce_seconds=fm_cfg.get("debounce_seconds", 5.0),
                        grace_period_seconds=fm_cfg.get("grace_period_seconds", 30.0),
                        min_motion_duration_seconds=fm_cfg.get("min_motion_duration_seconds", 2.0),
                        excluded_rooms=excluded_rooms,
                        quiet_hours_start=fm_cfg.get("quiet_hours_start"),
                        quiet_hours_end=fm_cfg.get("quiet_hours_end")
                    )
                    follow_me_service = await initialize_follow_me(
                        ha_client=ha_client,
                        music_handler=music_handler,
                        room_motion_mapping=room_motion_mapping,
                        config=follow_me_cfg
                    )
                    logger.info(
                        "follow_me_initialized",
                        mode=follow_me_cfg.mode.value,
                        rooms=list(room_motion_mapping.keys())
                    )
                else:
                    logger.info("follow_me_disabled", reason="disabled in config or no room mappings")
            else:
                logger.info("follow_me_not_configured", reason="no config from admin")
        except Exception as e:
            logger.warning("follow_me_init_failed", error=str(e))
    else:
        logger.warning("entity_manager_not_initialized", reason="No HA token available")

    cache_client = CacheClient()

    # Initialize session manager
    session_manager = await get_session_manager()
    logger.info("Session manager initialized")

    # Initialize parallel search engine
    parallel_search_engine = await ParallelSearchEngine.from_environment()
    logger.info("Parallel search engine initialized")

    # Initialize result fusion
    result_fusion = ResultFusion(
        similarity_threshold=0.7,
        min_confidence=0.5
    )
    logger.info("Result fusion initialized")

    # Phase 2: Initialize mode service client for guest mode
    mode_client = httpx.AsyncClient(base_url=MODE_SERVICE_URL, timeout=10.0)
    logger.info(f"Mode service client initialized: {MODE_SERVICE_URL}")

    # Initialize unified RAG client with resilience patterns (circuit breaker, rate limiting)
    # Fetches service URLs from admin backend registry, falls back to hardcoded constants
    rag_client = await initialize_rag_client()
    logger.info(
        f"Unified RAG client initialized with {len(rag_client._service_urls)} services",
        extra={"from_registry": rag_client.urls_loaded_from_registry}
    )

    # Initialize intent classifier for multi-intent detection
    intent_classifier = IntentClassifier()
    logger.info("Intent classifier initialized for multi-intent detection")

    # Check service health via unified RAG client
    for service_name in rag_client._service_urls.keys():
        try:
            response = await rag_client.get(service_name, "/health", skip_circuit_breaker=True)
            if response.success:
                logger.info(f"RAG service {service_name} is healthy")
            else:
                logger.warning(f"RAG service {service_name} unhealthy: {response.error}")
        except Exception as e:
            logger.warning(f"RAG service {service_name} not available: {e}")

    # Check mode service health
    try:
        response = await mode_client.get("/health")
        if response.status_code == 200:
            logger.info("Mode service is healthy")
        else:
            logger.warning(f"Mode service unhealthy: {response.status_code}")
    except Exception as e:
        logger.warning(f"Mode service not available: {e}")

    yield

    # Shutdown
    logger.info("Shutting down Orchestrator service")
    if admin_client:
        await admin_client.close()
    if ha_client:
        await ha_client.close()
    if llm_router:
        await llm_router.close()
    if cache_client:
        await cache_client.close()
    if session_manager:
        await session_manager.close()
    if parallel_search_engine:
        await parallel_search_engine.close_all()
    if mode_client:
        await mode_client.aclose()
    if rag_client:
        await rag_client.close()

app = FastAPI(
    title="Athena Orchestrator",
    description="LangGraph-based request coordination for Project Athena",
    version="1.0.0",
    lifespan=lifespan
)

# Add request tracing middleware (generates X-Request-ID for all requests)
app.add_middleware(RequestTracingMiddleware, service_name="orchestrator")

# Register unified exception handlers
register_exception_handlers(app)

# ============================================================================
# Helper Functions
# ============================================================================

async def check_service_bypass(intent: str) -> Optional[Dict[str, Any]]:
    """
    Check if a service should be bypassed to cloud LLM.

    Queries the admin backend to see if the given intent has bypass
    configuration enabled. If so, returns the bypass config which includes
    the cloud provider, model, and system prompt to use.

    Args:
        intent: The intent category (e.g., "recipes", "websearch")

    Returns:
        Bypass configuration dict if enabled, None otherwise
    """
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{ADMIN_URL}/api/rag-service-bypass/public/{intent}/config"
            )
            if response.status_code == 200:
                config = response.json()
                if config.get('bypass_enabled'):
                    logger.info("service_bypass_found", intent=intent,
                               provider=config.get('cloud_provider'))
                    return config
    except Exception as e:
        logger.debug(f"Service bypass check failed for '{intent}': {e}")
    return None


async def handle_query_with_bypass(
    query: str,
    intent: str,
    bypass_config: Dict[str, Any],
    state: Optional["OrchestratorState"] = None,
    **kwargs
) -> Optional[str]:
    """
    Handle a query using cloud LLM bypass instead of RAG service.

    When a service is configured for bypass, this function routes the query
    directly to a cloud LLM with the configured system prompt, instead of
    calling the dedicated RAG service.

    Args:
        query: The user's query
        intent: The intent category
        bypass_config: Configuration from check_service_bypass()
        state: Optional orchestrator state for context
        **kwargs: Additional context (zone, room, etc.)

    Returns:
        Response string from cloud LLM, or None if bypass fails
    """
    logger.info("service_bypass_active", intent=intent,
                provider=bypass_config.get('cloud_provider'))

    try:
        # Get preferred cloud model
        model = bypass_config.get('cloud_model')
        provider = bypass_config.get('cloud_provider')

        if not model:
            # Use default cloud model from routing config
            model_info = await _get_preferred_cloud_model(provider)
            if model_info:
                model = model_info[0]
            else:
                logger.warning("no_cloud_model_for_bypass", intent=intent)
                return None  # Fall back to local RAG

        # Build context from state if available
        context_parts = []
        if state:
            if state.zone:
                context_parts.append(f"User is in {state.zone}")
            if state.user_context:
                context_parts.append(f"User context: {state.user_context}")

        # Construct the prompt with system instructions
        system_prompt = bypass_config.get('system_prompt', '')
        if context_parts:
            system_prompt = f"{system_prompt}\n\nContext: {'; '.join(context_parts)}"

        # Generate with cloud LLM
        from shared.llm_router import get_llm_router
        llm_router = get_llm_router()

        bypass_start = time.time()
        response = await llm_router.generate(
            model=model,
            prompt=query,
            system_prompt=system_prompt,
            temperature=bypass_config.get('temperature', 0.7),
            max_tokens=bypass_config.get('max_tokens', 1024),
            metadata={
                'intent': intent,
                'bypass': True,
                'zone': kwargs.get('zone', state.zone if state else None),
            },
            stage="service_bypass"
        )
        bypass_duration = time.time() - bypass_start

        if response and response.get('response'):
            # Track LLM call for metrics
            if state and state.timing_tracker:
                tokens = response.get("eval_count", 0)
                state.timing_tracker.record_llm_call(
                    "service_bypass", model, tokens, int(bypass_duration * 1000), "cloud_bypass"
                )
            return response['response']

    except Exception as e:
        logger.error("service_bypass_failed", intent=intent, error=str(e))

    return None  # Fall back to local RAG on any failure


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


# ============================================================================
# Phase 5: Tool Calling Decision Functions
# ============================================================================

async def should_use_tool_calling(state: OrchestratorState, trigger_context: str = "classify") -> bool:
    """
    Determine if LLM tool calling should be used instead of pattern-based routing.

    This implements the hybrid cascading fallback system:
    1. First checks intent routing strategy (always_tool_calling, direct_only, cascading)
    2. For 'cascading' strategy, evaluates fallback triggers:
       - Low confidence in intent classification (< 0.6)
       - Ambiguous intent (GENERAL_INFO, UNKNOWN)
       - Multi-domain keywords in query
       - Empty RAG data retrieved
       - Validation failure

    Args:
        state: Current orchestrator state
        trigger_context: Where this check is called from (classify, retrieve, validate)

    Returns:
        True if tool calling should be used, False otherwise
    """
    try:
        # Check intent routing strategy first (hybrid cascading fallback system)
        intent_name = state.intent.value if state.intent else None
        if intent_name:
            routing_strategy = await get_intent_routing_strategy(intent_name)

            if routing_strategy == "always_tool_calling":
                logger.info(
                    "tool_calling_forced_by_strategy",
                    intent=intent_name,
                    strategy=routing_strategy,
                    context=trigger_context
                )
                return True

            if routing_strategy == "direct_only":
                logger.info(
                    "tool_calling_blocked_by_strategy",
                    intent=intent_name,
                    strategy=routing_strategy,
                    context=trigger_context
                )
                return False

            # For 'cascading' strategy, continue with fallback trigger evaluation

        # Get admin client for fallback trigger configuration
        admin_client = get_admin_client()

        # Fetch fallback triggers from database
        triggers = await admin_client.get_fallback_triggers()

        # DEBUG: Log trigger fetch result
        logger.info(
            f"should_use_tool_calling called: context={trigger_context}, "
            f"triggers_count={len(triggers) if triggers else 0}, "
            f"intent={state.intent.value if state.intent else None}, "
            f"confidence={state.confidence}"
        )

        if not triggers:
            logger.info("No fallback triggers configured, skipping tool calling")
            return False

        # Check each trigger based on priority
        for trigger in triggers:
            # DEBUG: Log each trigger being evaluated
            logger.info(f"Evaluating trigger: type={trigger.get('trigger_type')}, enabled={trigger.get('enabled', True)}")

            # Skip disabled triggers
            if not trigger.get("enabled", True):
                logger.info(f"Skipping disabled trigger: {trigger.get('trigger_type')}")
                continue
            trigger_type = trigger.get("trigger_type")
            config = trigger.get("config", {})

            # 1. Low confidence trigger
            if trigger_type == "confidence" and trigger_context == "classify":
                threshold = config.get("threshold", 0.6)
                if state.confidence < threshold:
                    logger.info(
                        f"Tool calling triggered: low confidence ({state.confidence:.2f} < {threshold})",
                        extra={"trigger": "low_confidence", "confidence": state.confidence}
                    )
                    return True

            # 2. Ambiguous intent trigger
            if trigger_type == "intent" and trigger_context == "classify":
                ambiguous_intents = config.get("intents", ["GENERAL_INFO", "UNKNOWN"])

                # DEBUG: Log trigger evaluation
                logger.info(
                    f"Evaluating ambiguous_intent trigger: intent={state.intent.value if state.intent else None}, "
                    f"ambiguous_intents={ambiguous_intents}, trigger_enabled={trigger.get('enabled', True)}"
                )

                # Case-insensitive comparison (database has uppercase, enum has lowercase)
                if state.intent and state.intent.value.upper() in [i.upper() for i in ambiguous_intents]:
                    logger.info(
                        f"Tool calling triggered: ambiguous intent ({state.intent.value})",
                        extra={"trigger": "ambiguous_intent", "intent": state.intent.value}
                    )
                    return True

            # 3. Multi-domain keywords trigger
            # SKIP for CONTROL intents - commands like "red and green" are valid color combinations
            if trigger_type == "keywords" and trigger_context == "classify":
                # Don't trigger tool calling for CONTROL intents based on keywords
                # Home control commands often contain "and" (e.g., "red and green lights")
                if state.intent == IntentCategory.CONTROL:
                    logger.debug("Skipping keywords trigger for CONTROL intent")
                    continue

                keywords = config.get("keywords", [])
                min_keywords = config.get("min_keywords", 1)

                query_lower = state.query.lower()
                # Use word boundary matching to avoid false positives (e.g., "or" in "outdoor")
                matched_keywords = [
                    kw for kw in keywords
                    if re.search(r'\b' + re.escape(kw.lower()) + r'\b', query_lower)
                ]

                if len(matched_keywords) >= min_keywords:
                    logger.info(
                        f"Tool calling triggered: multi-domain keywords ({matched_keywords})",
                        extra={"trigger": "multi_domain_keywords", "keywords": matched_keywords}
                    )
                    return True

            # 4. Empty RAG data trigger
            if trigger_type == "empty_rag" and trigger_context == "retrieve":
                check_empty = config.get("check_empty", True)
                check_null = config.get("check_null", True)

                if check_null and not state.retrieved_data:
                    logger.info(
                        "Tool calling triggered: no RAG data retrieved",
                        extra={"trigger": "empty_rag_data"}
                    )
                    return True

                if check_empty and state.retrieved_data:
                    # Check if retrieved data is empty/trivial
                    if isinstance(state.retrieved_data, dict):
                        # Filter out metadata keys
                        data_keys = [k for k in state.retrieved_data.keys() if k not in ["intent", "sources", "total_results"]]
                        if not data_keys or all(not state.retrieved_data.get(k) for k in data_keys):
                            logger.info(
                                "Tool calling triggered: RAG data is empty",
                                extra={"trigger": "empty_rag_data", "data_keys": data_keys}
                            )
                            return True

            # 5. Validation failure trigger
            if trigger_type == "validation" and trigger_context == "validate":
                check_validation = config.get("check_validation_node", True)

                if check_validation and not state.validation_passed:
                    logger.info(
                        f"Tool calling triggered: validation failed ({state.validation_reason})",
                        extra={"trigger": "validation_failure", "reason": state.validation_reason}
                    )
                    return True

        # No triggers matched
        return False

    except Exception as e:
        logger.error(f"Error checking tool calling triggers: {e}", exc_info=True)
        return False


# ============================================================================
# Model Escalation Functions
# ============================================================================

# In-memory escalation state tracking (fallback when admin API is slow)
_session_escalation: Dict[str, Dict] = {}


async def check_escalation_triggers(
    state: OrchestratorState,
    context: str,  # 'response', 'tool_result', or 'user_input'
    response_text: Optional[str] = None,
    tool_results: Optional[Dict] = None
) -> Optional[str]:
    """
    Check if escalation should be triggered based on active preset rules.

    Args:
        state: Current orchestrator state
        context: Where check is called from ('response', 'tool_result', 'user_input')
        response_text: LLM response to check for clarification patterns
        tool_results: Tool execution results to check for failures

    Returns:
        'complex' or 'super_complex' if escalation triggered, None otherwise
    """
    try:
        admin_client = get_admin_client()
        preset = await admin_client.get_active_escalation_preset()

        if not preset or not preset.get("rules"):
            return None

        query_lower = state.query.lower() if state.query else ""
        response_lower = response_text.lower() if response_text else ""

        for rule in preset["rules"]:
            if not rule.get("enabled", True):
                continue

            trigger_type = rule.get("trigger_type")
            patterns = rule.get("trigger_patterns", {})
            target = rule.get("escalation_target")
            duration = rule.get("escalation_duration", 5)

            triggered = False

            # Check based on trigger type
            if trigger_type == "clarification" and context == "response" and response_text:
                # Check if response contains clarification patterns
                for pattern in patterns.get("patterns", []):
                    if pattern.lower() in response_lower:
                        triggered = True
                        logger.info(f"Escalation triggered: clarification pattern '{pattern}' in response")
                        break

            elif trigger_type == "user_correction" and context == "user_input":
                for pattern in patterns.get("patterns", []):
                    if pattern.lower() in query_lower:
                        triggered = True
                        logger.info(f"Escalation triggered: user correction pattern '{pattern}'")
                        break

            elif trigger_type == "user_frustration" and context == "user_input":
                for pattern in patterns.get("patterns", []):
                    if pattern.lower() in query_lower:
                        triggered = True
                        logger.info(f"Escalation triggered: user frustration pattern '{pattern}'")
                        break

            elif trigger_type == "explicit_request" and context == "user_input":
                for pattern in patterns.get("patterns", []):
                    if pattern.lower() in query_lower:
                        triggered = True
                        logger.info(f"Escalation triggered: explicit request pattern '{pattern}'")
                        break

            elif trigger_type == "empty_results" and context == "tool_result" and tool_results:
                if patterns.get("check_null") and not tool_results:
                    triggered = True
                    logger.info("Escalation triggered: null tool results")
                elif patterns.get("check_empty"):
                    # Check if all results are empty/error
                    all_empty = all(
                        not r or (isinstance(r, dict) and (r.get("error") or not r.get("results", r.get("data"))))
                        for r in tool_results.values()
                    )
                    if all_empty:
                        triggered = True
                        logger.info("Escalation triggered: empty tool results")

            elif trigger_type == "tool_failure" and context == "tool_result" and tool_results:
                if patterns.get("on_error"):
                    error_count = sum(1 for r in tool_results.values() if isinstance(r, dict) and r.get("error"))
                    min_failures = patterns.get("consecutive_failures", 1)
                    if error_count >= min_failures:
                        triggered = True
                        logger.info(f"Escalation triggered: {error_count} tool failures")

            elif trigger_type == "short_response" and context == "response" and response_text:
                max_length = patterns.get("max_length", 50)
                if len(response_text) < max_length:
                    triggered = True
                    logger.info(f"Escalation triggered: short response ({len(response_text)} < {max_length})")

            elif trigger_type == "short_query" and context == "user_input":
                max_words = patterns.get("max_words", 3)
                word_count = len(state.query.split()) if state.query else 0
                if word_count <= max_words:
                    triggered = True
                    logger.info(f"Escalation triggered: short query ({word_count} words)")

            elif trigger_type == "long_query" and context == "user_input":
                min_words = patterns.get("min_words", 40)
                word_count = len(state.query.split()) if state.query else 0
                if word_count >= min_words:
                    triggered = True
                    logger.info(f"Escalation triggered: long query ({word_count} >= {min_words} words)")

            elif trigger_type == "negative_sentiment" and context == "user_input":
                # Heuristic-based frustration detection - no exact patterns needed
                query = state.query or ""

                # Check for multiple exclamation marks (strong emotion)
                if query.count('!') >= 2:
                    triggered = True
                    logger.info("Escalation triggered: multiple exclamation marks detected")

                # Check for question words indicating confusion (why, what, how come)
                elif any(query.lower().startswith(w) for w in ['why ', 'why?', 'what?', 'how come', 'huh']):
                    word_count = len(query.split())
                    if word_count <= 6:  # Short confused question
                        triggered = True
                        logger.info("Escalation triggered: short confused question")

                # Check for negative indicators in short messages
                else:
                    negative_words = ['wrong', 'no', 'not', "n't", 'never', 'bad', 'stop', 'dont', "don't", 'didnt', "didn't", 'stupid', 'dumb', 'useless', 'broken']
                    word_count = len(query.split())
                    has_negative = any(neg in query.lower() for neg in negative_words)

                    if has_negative and word_count <= 10:
                        triggered = True
                        logger.info(f"Escalation triggered: negative sentiment in short message")

            elif trigger_type == "repeated_query" and context == "user_input":
                # Check conversation history for repeated similar queries
                if hasattr(state, 'conversation_history') and state.conversation_history:
                    recent_queries = [
                        msg.get("content", "").lower()
                        for msg in state.conversation_history[-5:]
                        if msg.get("role") == "user"
                    ]
                    if query_lower in recent_queries:
                        triggered = True
                        logger.info("Escalation triggered: repeated query detected")

            elif trigger_type == "always":
                triggered = True
                logger.info("Escalation triggered: always rule (Demo Mode)")

            if triggered:
                # Store escalation state
                _session_escalation[state.session_id] = {
                    "escalated_to": target,
                    "turns_remaining": duration,
                    "rule_id": rule.get("id"),
                    "rule_name": rule.get("rule_name")
                }

                # Log to audit (async, don't wait)
                asyncio.create_task(log_escalation_audit(
                    session_id=state.session_id,
                    rule_name=rule.get("rule_name"),
                    target=target,
                    trigger_type=trigger_type
                ))

                return target

        return None

    except Exception as e:
        logger.error(f"Escalation check error: {e}", exc_info=True)
        return None


def get_current_escalation(session_id: str) -> Optional[Dict]:
    """Get current escalation state for session (in-memory)."""
    state = _session_escalation.get(session_id)
    if state and state.get("turns_remaining", 0) > 0:
        return state
    return None


def decrement_escalation_turns(session_id: str) -> None:
    """Decrement turns remaining for escalated session."""
    if session_id in _session_escalation:
        _session_escalation[session_id]["turns_remaining"] -= 1
        if _session_escalation[session_id]["turns_remaining"] <= 0:
            logger.info(f"Escalation expired for session {session_id[:8]}")
            del _session_escalation[session_id]


async def log_escalation_audit(session_id: str, rule_name: str, target: str, trigger_type: str):
    """Log escalation event to audit and escalation events table (fire and forget)."""
    try:
        admin_client = get_admin_client()

        # Log to escalation events table (for metrics dashboard)
        await admin_client.client.post(
            f"{admin_client.admin_url}/api/escalation/events/internal",
            json={
                "session_id": session_id,
                "event_type": "escalation",
                "to_model": target,
                "rule_name": rule_name,
                "trigger_type": trigger_type,
                "trigger_context": {
                    "trigger_type": trigger_type,
                    "rule_name": rule_name
                }
            }
        )

        # Also log to general audit table
        await admin_client.client.post(
            f"{admin_client.admin_url}/api/audit",
            json={
                "action": "model_escalation",
                "resource_type": "session",
                "resource_id": session_id,
                "details": {
                    "rule_name": rule_name,
                    "target": target,
                    "trigger_type": trigger_type
                }
            }
        )
    except Exception:
        pass  # Don't fail main flow for audit logging


# ============================================================================
# Phase 2: Guest Mode Permission Functions
# ============================================================================

async def get_current_mode() -> Dict[str, Any]:
    """
    Get current mode from mode service (Phase 2: Guest Mode).

    Fetches mode (guest vs owner) and permission settings from the mode service.
    Falls back to owner mode if service unavailable (safe default).

    Returns:
        Dict with mode, permissions, and metadata
    """
    try:
        response = await mode_client.get("/mode")
        response.raise_for_status()
        mode_data = response.json()

        # Get permissions for current mode
        perms_response = await mode_client.get("/mode/permissions")
        perms_response.raise_for_status()
        permissions = perms_response.json()

        logger.info(
            "mode_fetched",
            mode=mode_data.get("mode", "owner"),
            override_active=mode_data.get("override_active", False)
        )

        return {
            "mode": mode_data.get("mode", "owner"),
            "permissions": permissions,
            "override_active": mode_data.get("override_active", False),
            "reason": mode_data.get("reason", "Unknown")
        }
    except Exception as e:
        logger.warning(f"Failed to get mode from mode service: {e}")
        # Default to owner mode on error (safe default)
        return {
            "mode": "owner",
            "permissions": {
                "mode": "owner",
                "allowed_intents": [],
                "restricted_entities": [],
                "allowed_domains": [],
                "max_queries_per_minute": 100
            },
            "override_active": False,
            "reason": "Mode service unavailable"
        }


# ============================================================================
# Phase 4: Voice PIN Override Detection and Handling
# ============================================================================

# Patterns for detecting owner mode commands
OWNER_MODE_PATTERNS = [
    r"\b(switch|change|enable|activate)\s+(to\s+)?owner\s*mode\b",
    r"\bowner\s*mode\b.*\bpin\b",
    r"\bpin\b.*\bowner\s*mode\b",
    r"\bi'?m\s+(the\s+)?owner\b",
    r"\bexit\s+guest\s*mode\b",
    r"\bdeactivate\s+guest\s*mode\b",
    r"\bowner\s+override\b",
]


def detect_owner_mode_command(query: str) -> bool:
    """
    Detect if the query is an owner mode command (Phase 4: Voice PIN Override).

    Args:
        query: User query string

    Returns:
        True if query appears to be an owner mode command
    """
    query_lower = query.lower()
    for pattern in OWNER_MODE_PATTERNS:
        if re.search(pattern, query_lower):
            return True
    return False


def extract_pin_from_query(query: str) -> Optional[str]:
    """
    Extract a 6-digit PIN from a query (Phase 4: Voice PIN Override).

    Handles various spoken formats:
    - "pin 123456"
    - "pin one two three four five six"
    - "code 123456"
    - "123456"

    Args:
        query: User query string

    Returns:
        6-digit PIN string or None if not found
    """
    # Word to digit mapping for spoken numbers
    word_to_digit = {
        "zero": "0", "oh": "0", "o": "0",
        "one": "1", "won": "1",
        "two": "2", "to": "2", "too": "2",
        "three": "3", "tree": "3",
        "four": "4", "for": "4", "fore": "4",
        "five": "5",
        "six": "6", "sicks": "6", "sex": "6",
        "seven": "7",
        "eight": "8", "ate": "8",
        "nine": "9", "niner": "9",
    }

    query_lower = query.lower()

    # First try to find numeric digits directly
    # Match 6 consecutive digits
    numeric_match = re.search(r'\b(\d{6})\b', query_lower)
    if numeric_match:
        return numeric_match.group(1)

    # Match 6 digits with spaces between them (e.g., "1 2 3 4 5 6")
    spaced_match = re.search(r'\b(\d)\s+(\d)\s+(\d)\s+(\d)\s+(\d)\s+(\d)\b', query_lower)
    if spaced_match:
        return ''.join(spaced_match.groups())

    # Try to find spoken digits after "pin" or "code"
    pin_context = re.search(r'(?:pin|code)\s+(.+)', query_lower)
    if pin_context:
        digit_portion = pin_context.group(1)
        digits = []

        # Split by spaces and convert words to digits
        words = digit_portion.split()
        for word in words:
            # Clean punctuation
            word_clean = re.sub(r'[^\w]', '', word)
            if word_clean.isdigit():
                digits.append(word_clean)
            elif word_clean in word_to_digit:
                digits.append(word_to_digit[word_clean])

            # Stop if we have 6 digits
            if len(digits) >= 6:
                break

        if len(digits) == 6:
            return ''.join(digits)

    return None


async def activate_owner_override(
    pin: Optional[str],
    voice_device_id: Optional[str] = None,
    timeout_minutes: Optional[int] = None
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Activate owner mode override via mode service (Phase 4: Voice PIN Override).

    Args:
        pin: 6-digit PIN or None
        voice_device_id: Optional device identifier
        timeout_minutes: Override duration

    Returns:
        Tuple of (success, message, response_data)
    """
    try:
        request_data = {
            "mode": "owner",
            "voice_pin": pin,
            "timeout_minutes": timeout_minutes,
            "voice_device_id": voice_device_id
        }

        response = await mode_client.post("/mode/override", json=request_data)

        if response.status_code == 200:
            data = response.json()
            logger.info(
                "owner_override_activated",
                expires_at=data.get("expires_at"),
                device=voice_device_id
            )
            return True, data.get("message", "Owner mode activated."), data

        elif response.status_code == 401:
            # PIN required but not provided
            logger.info("owner_override_pin_required", device=voice_device_id)
            return False, "Please provide your 6-digit owner PIN. Say 'owner mode' followed by your PIN.", None

        elif response.status_code == 403:
            # Invalid PIN
            logger.warning("owner_override_pin_invalid", device=voice_device_id)
            return False, "Invalid PIN. Access denied.", None

        elif response.status_code == 400:
            # Invalid PIN format
            detail = response.json().get("detail", "Invalid PIN format")
            logger.warning("owner_override_invalid_format", detail=detail, device=voice_device_id)
            return False, f"{detail}. Please provide a 6-digit PIN.", None

        else:
            logger.error(
                "owner_override_unexpected_error",
                status_code=response.status_code,
                device=voice_device_id
            )
            return False, "Unable to process owner mode request. Please try again.", None

    except Exception as e:
        logger.error(f"owner_override_failed: {e}")
        return False, "Mode service unavailable. Owner mode request could not be processed.", None


def check_intent_permission(intent: IntentCategory, permissions: Dict[str, Any]) -> bool:
    """
    Check if intent is allowed based on current permissions (Phase 2: Guest Mode).

    Uses a deny-list approach: intents in restricted_intents are blocked,
    everything else is allowed (unless allowed_intents is populated).

    Args:
        intent: Intent category
        permissions: Permissions from mode service

    Returns:
        True if allowed, False otherwise
    """
    mode = permissions.get("mode", "owner")

    # Owner mode: everything allowed
    if mode == "owner":
        return True

    intent_value = intent.value.lower()

    # Primary check: restricted_intents (deny list)
    restricted_intents = permissions.get("restricted_intents", [])
    if restricted_intents:
        is_restricted = intent_value in [i.lower() for i in restricted_intents]
        if is_restricted:
            logger.info(
                "intent_blocked_restricted",
                intent=intent_value,
                mode=mode,
                restricted_intents=restricted_intents
            )
            return False

    # Secondary check: allowed_intents (allow list) if populated
    allowed_intents = permissions.get("allowed_intents", [])
    if allowed_intents:
        is_allowed = intent_value in [i.lower() for i in allowed_intents]
        logger.info(
            "intent_permission_check",
            intent=intent_value,
            mode=mode,
            allowed=is_allowed,
            allowed_intents=allowed_intents
        )
        return is_allowed

    # No allow list specified - allow by default (only restricted_intents blocked)
    logger.info(
        "intent_allowed_default",
        intent=intent_value,
        mode=mode
    )
    return True


def check_entity_permission(entity_id: str, permissions: Dict[str, Any]) -> bool:
    """
    Check if entity access is allowed based on current permissions (Phase 2: Guest Mode).

    Uses regex patterns for restricted_entities (e.g., ".*tesla.*", ".*vehicle.*").
    Falls back to domain-based allow list if no match.

    Args:
        entity_id: Home Assistant entity ID (e.g., "light.bedroom")
        permissions: Permissions from mode service

    Returns:
        True if allowed, False otherwise
    """
    import re

    mode = permissions.get("mode", "owner")

    # Owner mode: everything allowed
    if mode == "owner":
        return True

    # Guest mode: check restrictions
    restricted_entities = permissions.get("restricted_entities", [])
    allowed_domains = permissions.get("allowed_domains", [])

    # Check if entity matches restricted pattern (supports regex)
    for pattern in restricted_entities:
        try:
            if re.match(pattern, entity_id, re.IGNORECASE):
                logger.info(
                    "entity_blocked_by_regex",
                    entity_id=entity_id,
                    pattern=pattern,
                    mode=mode
                )
                return False
        except re.error:
            # Invalid regex - try simple wildcard match as fallback
            if pattern.endswith("*"):
                prefix = pattern[:-1]
                if entity_id.startswith(prefix):
                    logger.info(
                        "entity_blocked_by_wildcard",
                        entity_id=entity_id,
                        pattern=pattern,
                        mode=mode
                    )
                    return False
            elif pattern == entity_id:
                logger.info(
                    "entity_blocked_exact",
                    entity_id=entity_id,
                    mode=mode
                )
                return False

    # Check if entity domain is allowed
    entity_domain = entity_id.split(".")[0] if "." in entity_id else entity_id
    if allowed_domains and entity_domain not in allowed_domains:
        logger.info(
            "entity_blocked_domain",
            entity_id=entity_id,
            domain=entity_domain,
            allowed_domains=allowed_domains,
            mode=mode
        )
        return False

    logger.info(
        "entity_allowed",
        entity_id=entity_id,
        mode=mode
    )
    return True


# ============================================================================
# Node Implementations
# ============================================================================

async def classify_node(state: OrchestratorState) -> OrchestratorState:
    """
    Classify user intent using LLM with Redis caching.
    Determines: control vs info, specific category, entities.
    Also detects and handles multi-intent queries.
    """
    start = time.time()

    # STT error correction for common Whisper transcription mistakes
    # Apply early so all classification paths use the corrected query
    stt_corrections = [
        # "play" variations - Whisper sometimes mishears "play" as "place"
        ("place a music", "play music"),
        ("place music", "play music"),
        ("place a song", "play a song"),
        ("place some music", "play some music"),
        ("play a music", "play music"),  # Grammar fix
        ("place a ", "play "),  # Generic "place a X" → "play X"
        ("place the ", "play "),
        # Other common mishearings
        ("please music", "play music"),
        ("plays music", "play music"),
    ]
    original_query = state.query
    query_lower = state.query.lower()
    for wrong, correct in stt_corrections:
        if wrong in query_lower:
            # Apply correction while preserving case where possible
            state.query = state.query.lower().replace(wrong, correct)
            logger.info("stt_correction_applied", original=original_query, corrected=state.query)
            break  # Apply only the first matching correction

    # Round 17: Typo correction for common misspellings (text input, not STT)
    # This enables recognition of "turn of the lihgts" → "turn off the lights"
    typo_corrections = {
        # Light misspellings
        'lihgts': 'lights', 'lighst': 'lights', 'ligths': 'lights', 'litghs': 'lights',
        'lghts': 'lights', 'lihgt': 'light', 'ligth': 'light', 'ligt': 'light',
        'lite': 'light', 'lites': 'lights',
        # On/off misspellings - "turn of" is extremely common
        'turn of ': 'turn off ', 'turn fo ': 'turn off ',
        'offf': 'off', 'onn': 'on',
        'trun ': 'turn ', 'tunr ': 'turn ', 'tur ': 'turn ',
        # Weather/temperature typos
        'weathr': 'weather', 'weahter': 'weather', 'wheather': 'weather',
        'teh ': 'the ', 'hte ': 'the ',  # Common article typos
        # Switch/thermostat
        'swtich': 'switch', 'swich': 'switch',
        'theromstat': 'thermostat', 'thermstat': 'thermostat',
        'temprature': 'temperature', 'tempature': 'temperature',
        # Time/tomorrow
        'tmrw': 'tomorrow', 'tmrrw': 'tomorrow', 'tomrrow': 'tomorrow', 'tomorow': 'tomorrow',
        # Slang normalizations
        'wut ': 'what ', 'wat ': 'what ', 'whats ': "what's ",
        'im ': "i'm ", 'dont ': "don't ", 'cant ': "can't ", 'wont ': "won't ",
        # Round 21-30: Price/cost slang
        'whats the damage': 'what is the price',
        "what's the damage": 'what is the price',
        'the damage': 'the price',
        # Round 21-30: Confirmation/emphasis slang
        'deadass': 'really',  # NYC slang for "seriously" or "for real"
        'no cap': 'seriously',  # Gen-Z slang for "no lie"
        'fr fr': 'for real',  # "for real for real"
        'lowkey': 'kind of',  # mild emphasis
        'highkey': 'really',  # strong emphasis
    }
    query_lower = state.query.lower()
    corrected = False
    for typo, correction in typo_corrections.items():
        if typo in query_lower:
            query_lower = query_lower.replace(typo, correction)
            corrected = True
    if corrected:
        original_for_log = state.query
        state.query = query_lower
        logger.info("typo_correction_applied", original=original_for_log, corrected=state.query)

    # Round 21-30: FALSE MEMORY CLAIM DETECTION
    # Prevent LLM hallucination when user claims we said something in a "previous session"
    query_check = state.query.lower()
    false_memory_patterns = [
        "remember that", "you mentioned last time", "you said last time",
        "you told me last time", "you told me before", "you recommended last",
        "from our last conversation", "from last session",
    ]
    if any(p in query_check for p in false_memory_patterns):
        # Check if we have actual session context first
        has_context = False
        if state.session_id:
            try:
                context = await get_conversation_context(state.session_id)
                if context and context.get("entities"):
                    has_context = True
            except Exception:
                pass
        if not has_context:
            state.intent = IntentCategory.GENERAL_INFO
            state.answer = ("I don't have memory of previous sessions. Each conversation starts fresh. "
                          "Could you tell me what you're looking for? I'd be happy to help find it now!")
            state.node_timings["classify"] = time.time() - start
            logger.info("false_memory_claim_intercepted", query=state.query[:50])
            return state

    # Round 21-30: EMOTIONAL VENTING DETECTION
    # Don't route emotional venting about weather to the weather service
    emotional_venting_patterns = [
        ("work was terrible", "I'm sorry to hear that. Would you like me to find something to help you relax, like a nice restaurant or some comfort food nearby?"),
        ("today sucked", "I'm sorry you had a rough day. Is there anything I can help with to make it better?"),
        ("bad day", None),  # Just mark, don't auto-respond
        ("now its raining", None),  # Part of venting, not weather query
        ("now it's raining", None),
    ]
    for pattern, response in emotional_venting_patterns:
        if pattern in query_check:
            # Only auto-respond if we have a specific response
            if response:
                state.intent = IntentCategory.GENERAL_INFO
                state.answer = response
                state.node_timings["classify"] = time.time() - start
                logger.info("emotional_venting_intercepted", query=state.query[:50])
                return state
            else:
                # Mark as general info to prevent weather/control routing
                state.intent = IntentCategory.GENERAL_INFO
                break

    # Round 21-30: PHONE CALL REQUEST DETECTION
    # Intercept requests to make phone calls before they go to LLM
    call_request_patterns = [
        "call them", "call him", "call her", "call the restaurant",
        "make a call", "phone call", "give them a call", "call and",
        "tell them im running late", "tell them i'm running late",
    ]
    if any(p in query_check for p in call_request_patterns):
        state.intent = IntentCategory.GENERAL_INFO
        state.answer = "I can't make phone calls, but I can help you find phone numbers or contact information for businesses."
        state.node_timings["classify"] = time.time() - start
        logger.info("phone_call_request_intercepted", query=state.query[:50])
        return state

    # Round 21-30: Ambiguous ETA/travel time questions
    # "how long to get there" without destination context - guide user
    eta_ambiguous_patterns = [
        "how long to get there", "how long will it take",
        "how long to drive", "how long until i get",
        "eta to there", "time to get there",
    ]
    # Only match if there's no specific destination mentioned
    has_specific_dest = any(word in query_check for word in ["to the", "to restaurant", "to work", "to home", "to airport"])
    if any(p in query_check for p in eta_ambiguous_patterns) and not has_specific_dest:
        state.intent = IntentCategory.GENERAL_INFO
        state.answer = "I can calculate drive time and ETA for you! Just tell me the destination - what's the address or name of the place you're heading to?"
        state.node_timings["classify"] = time.time() - start
        logger.info("eta_ambiguous_intercepted", query=state.query[:50])
        return state

    # SEQUENCE DETECTION - Check BEFORE multi-intent splitting
    # Sequence commands like "turn lights on then wait 3 seconds then turn off"
    # should NOT be split into multiple intents - they're a single sequence
    is_sequence_command = detect_sequence_intent(state.query)
    if is_sequence_command:
        logger.info(f"Sequence command detected - bypassing multi-intent split: '{state.query[:60]}...'")

    # MULTI-INTENT DETECTION
    # Check if query contains multiple intents (e.g., "turn on the lights and what's the weather")
    # Skip for sequence commands - they should be handled as a single unit
    if intent_classifier and not state.is_multi_intent and not is_sequence_command:
        intent_parts = intent_classifier.detect_multi_intent(state.query)
        if len(intent_parts) > 1:
            logger.info(
                f"Multi-intent detected: '{state.query[:50]}...' split into {len(intent_parts)} parts",
                extra={"intent_parts": intent_parts}
            )
            state.is_multi_intent = True
            state.intent_parts = intent_parts
            state.current_intent_index = 0
            # Process first intent part - subsequent parts handled in finalize_node
            state.query = intent_parts[0]
            logger.info(f"Processing first intent: '{state.query}'")

    # COMPREHENSIVE CONTEXT DETECTION
    # Detect if this query references previous conversation context
    ref_info = detect_context_reference(state.query)
    state.context_ref_info = ref_info

    # LOCATION CORRECTION DETECTION
    # Detect if user is correcting their location (e.g., "I'm not in Baltimore", "use my location")
    location_correction = detect_location_correction(state.query)
    if location_correction["is_correction"]:
        logger.info(
            f"Location correction detected: type={location_correction['correction_type']}, "
            f"location={location_correction['extracted_location']}, use_current={location_correction['use_current_location']}"
        )
        # Update state.context with the location override
        if state.context is None:
            state.context = {}

        if location_correction["extracted_location"]:
            # User specified a specific location
            state.context["location_override"] = {
                "address": location_correction["extracted_location"],
                "source": "user_correction",
                "correction_type": location_correction["correction_type"]
            }
            # Also update entities for immediate use
            if state.entities is None:
                state.entities = {}
            state.entities["location"] = location_correction["extracted_location"]
            logger.info(f"Location override set to: {location_correction['extracted_location']}")
        elif location_correction["use_current_location"]:
            # User wants their current/actual location - mark it for device lookup
            state.context["location_override"] = {
                "use_device_location": True,
                "source": "user_correction",
                "correction_type": location_correction["correction_type"]
            }
            # Clear any cached Baltimore default
            if state.entities:
                state.entities.pop("location", None)
            logger.info("Location override: will use device/GPS location")

    # ALWAYS fetch context for short queries (8 words or less) - minimal overhead (~1ms)
    # This enables natural conversation flow without requiring explicit pattern matching
    # e.g., "level 2", "set the bed to level 2", "no music" will all use previous context
    should_fetch_context = (
        state.session_id and
        (ref_info["has_context_ref"] or ref_info["is_short_query"])
    )

    if should_fetch_context:
        prev_context = await get_conversation_context(state.session_id)
        if prev_context:
            state.prev_context = prev_context.model_dump()

            # Check for strong intent indicators BEFORE doing context continuation
            # This prevents "restaurant recommendations" from routing to weather
            # just because the previous query was about weather
            strong_intent = detect_strong_intent(state.query, prev_context.intent)
            logger.info(f"DEBUG strong_intent check: query='{state.query}', prev_intent={prev_context.intent}, result={strong_intent}")
            if strong_intent["should_override_context"]:
                detected_intent_str = strong_intent["detected_intent"]
                logger.info(
                    f"Strong intent detected: '{state.query}' has {detected_intent_str} "
                    f"indicators {strong_intent['matching_keywords']} - NOT continuing {prev_context.intent} context"
                )
                # Map detected intent string to IntentCategory and use it directly
                # This ensures queries like "find supercharger" route to DINING, not GENERAL_INFO
                intent_map = {
                    "dining": IntentCategory.DINING,
                    "weather": IntentCategory.WEATHER,
                    "sports": IntentCategory.SPORTS,
                    "control": IntentCategory.CONTROL,
                    "news": IntentCategory.NEWS,
                    "directions": IntentCategory.DIRECTIONS,
                    "general": IntentCategory.GENERAL_INFO,
                    "flights": IntentCategory.FLIGHTS,
                    "streaming": IntentCategory.STREAMING,
                    "events": IntentCategory.EVENTS,
                    "stocks": IntentCategory.STOCKS,
                    "recipes": IntentCategory.RECIPES,
                    "notification_pref": IntentCategory.NOTIFICATION_PREF,
                }
                if detected_intent_str in intent_map:
                    state.intent = intent_map[detected_intent_str]
                    state.confidence = 0.90  # Strong indicators = high confidence
                    state.complexity = determine_complexity(state.query, detected_intent_str)
                    logger.info(
                        f"Fast path: strong intent override - routing '{state.query[:50]}...' "
                        f"to {state.intent} (from {detected_intent_str} indicators)"
                    )
                    state.node_timings["classify"] = time.time() - start
                    return state
                # If detected intent not in map, fall through to normal classification
            elif ref_info.get("is_meta_inquiry"):
                # Meta-inquiries about system state/errors should NOT continue previous context
                # "What happened?", "What was the error?" are asking about the system, not the topic
                logger.info(
                    f"Meta-inquiry detected: '{state.query}' is asking about system/error state "
                    f"- NOT continuing {prev_context.intent} context"
                )
                # Store the previous context info so the response can reference what happened
                state.context_ref_info = ref_info
                state.context_ref_info["prev_error_context"] = prev_context.model_dump() if prev_context else None
                # Fall through to normal classification (GENERAL or CONVERSATION intent)
            elif ref_info.get("is_conversation_breaker"):
                # Conversation breakers like "forget it", "I'm sorry", "thanks for your patience"
                # should NOT continue the previous intent - they break the task context
                logger.info(
                    f"Conversation breaker detected: '{state.query}' breaks {prev_context.intent} context "
                    f"- routing to fresh classification"
                )
                state.context_ref_info = ref_info
                # Fall through to normal classification - will be handled as conversational response
            else:
                if ref_info["has_context_ref"]:
                    logger.info(f"Context reference detected: {ref_info['ref_types']} - prev intent: {prev_context.intent}")
                else:
                    logger.info(f"Short query with context available: '{state.query}' - prev intent: {prev_context.intent}")
                    # Mark as implicit context reference for short queries
                    ref_info["has_context_ref"] = True
                    ref_info["ref_types"].append("implicit_short_query")
                    state.context_ref_info = ref_info

                # Route to the same intent as the previous context
                # This handles all intents, not just CONTROL
                try:
                    state.intent = IntentCategory(prev_context.intent)
                    state.confidence = 0.95
                    # Use complexity detector for follow-ups (may upgrade from simple)
                    state.complexity = get_complexity_with_override(
                        state.query,
                        intent=prev_context.intent,
                        is_followup=True
                    )

                    # Use merge_with_context to handle temporal refs, new entities, etc.
                    merged = merge_with_context(
                        new_query=state.query,
                        new_entities=state.entities,
                        context=prev_context,
                        ref_info=ref_info
                    )
                    state.entities = merged["entities"]

                    # Set flag for pronoun-based follow-ups that need LLM to resolve from history
                    if merged.get("needs_history_context"):
                        state.needs_history_context = True
                        logger.info(f"Pronoun follow-up detected: '{state.query}' needs conversation history for resolution")

                    # Log what we merged
                    if ref_info["has_temporal_ref"]:
                        logger.info(f"Temporal context: '{state.query}' - time_ref={merged['entities'].get('time_ref')}")

                    state.node_timings["classify"] = time.time() - start
                    logger.info(f"Context continuation for '{state.query}' - routing to {state.intent}, entities={state.entities}")
                    return state
                except ValueError:
                    # Unknown intent in context, fall through to normal classification
                    logger.warning(f"Unknown intent in context: {prev_context.intent}")
        else:
            # No previous context found - this is normal for first query in session
            if ref_info["has_context_ref"]:
                logger.info(f"Context reference detected but no previous context for session {state.session_id[:8]}...")

    # OPTIMIZATION: Check cache first (but skip for problem-reporting queries)
    # Problem queries need fresh classification each time since device state changes
    from semantic_cache import UNCACHEABLE_PATTERNS
    query_lower = state.query.lower()
    skip_cache = any(re.search(p, query_lower) for p in UNCACHEABLE_PATTERNS)

    if skip_cache:
        logger.info(f"Intent cache SKIP for problem-reporting query: '{state.query[:50]}...'")

    cache_key = f"intent:{hashlib.md5(state.query.lower().encode()).hexdigest()}"
    cache_start = time.time()

    try:
        if not skip_cache:
            cached = await cache_client.get(cache_key)
            if state.timing_tracker:
                state.timing_tracker.track_substage("graph", "classify", "cache_check", time.time() - cache_start)
            if cached:
                state.intent = IntentCategory(cached["intent"])
                state.confidence = cached.get("confidence", 0.9)
                state.complexity = cached.get("complexity", "simple")  # NEW: Load complexity from cache
                # Merge cached entities but preserve location from request
                cached_entities = cached.get("entities", {})
                preserved_location = state.entities.get("location") if state.entities else None
                state.entities = {**cached_entities}
                if preserved_location:
                    state.entities["location"] = preserved_location
                state.node_timings["classify"] = time.time() - start
                logger.info(f"Intent cache HIT for '{state.query}': {state.intent}")
                return state
        else:
            if state.timing_tracker:
                state.timing_tracker.track_substage("graph", "classify", "cache_check", time.time() - cache_start)
    except Exception as e:
        if state.timing_tracker:
            state.timing_tracker.track_substage("graph", "classify", "cache_check", time.time() - cache_start)
        logger.warning(f"Intent cache lookup failed: {e}")

    # FAST PATH: Check for automation patterns
    # Queries that should be handled by the automation agent (schedules, recurring, triggers)
    if should_use_automation_agent(state.query):
        logger.info(f"Fast path: automation pattern detected in '{state.query[:50]}...' - classifying as CONTROL (COMPLEX)")
        state.intent = IntentCategory.CONTROL
        state.confidence = 0.9
        state.complexity = "COMPLEX"  # Automations always need complex handling
        state.entities = {"automation": True}

        # Cache the classification
        try:
            await cache_client.set(cache_key, {
                "intent": state.intent.value,
                "confidence": state.confidence,
                "complexity": state.complexity,
                "entities": state.entities
            }, ttl=300)
            logger.info(f"Intent classification cached for '{state.query}'")
        except Exception as e:
            logger.warning(f"Intent cache write failed: {e}")

        state.node_timings["classify"] = time.time() - start
        return state

    # FAST PATH: Check for room+color patterns (light control without "lights" keyword)
    # This catches commands like "change office to white", "random colors in the kitchen"
    room_names = [
        'office', 'kitchen', 'bedroom', 'living room', 'bathroom',
        'master bedroom', 'master bath', 'guest room', 'hallway', 'hall',
        'basement', 'attic', 'garage', 'porch', 'deck', 'patio', 'dining room',
        'den', 'family room', 'study', 'library', 'laundry room', 'alpha', 'beta'
    ]
    color_names = [
        'red', 'blue', 'green', 'white', 'yellow', 'orange', 'purple',
        'pink', 'cyan', 'magenta', 'warm', 'cool', 'rainbow', 'sunset',
        'random colors', 'different colors', 'christmas colors', 'ocean'
    ]
    query_lower = state.query.lower()
    has_room = any(room in query_lower for room in room_names)
    has_color = any(color in query_lower for color in color_names)

    if has_room and has_color:
        # Room + color = light control command
        logger.info(f"Fast path: room+color detected in '{state.query[:50]}...' - classifying as CONTROL")
        state.intent = IntentCategory.CONTROL
        state.confidence = 0.9
        # Use complexity detector (catches "all rooms except X" patterns)
        state.complexity = determine_complexity(state.query, "control")
        state.entities = {"device": "light"}

        # Extract room and color for entity info
        for room in room_names:
            if room in query_lower:
                state.entities["room"] = room
                break
        for color in color_names:
            if color in query_lower:
                state.entities["color"] = color
                break

        # Cache the classification
        try:
            await cache_client.set(cache_key, {
                "intent": state.intent.value,
                "confidence": state.confidence,
                "complexity": state.complexity,
                "entities": state.entities
            }, ttl=300)
            logger.info(f"Intent classification cached for '{state.query}'")
        except Exception as e:
            logger.warning(f"Intent cache write failed: {e}")

        state.node_timings["classify"] = time.time() - start
        return state

    # SMS Integration: Check for "text me that" intent
    # This must be handled specially to send SMS with previous response
    if is_text_me_that_request(state.query):
        logger.info(f"Fast path: 'text me that' detected in '{state.query[:50]}...'")
        state.intent = IntentCategory.TEXT_ME_THAT
        state.confidence = 0.95
        # Text-me-that is always simple (just sending previous response)
        state.complexity = determine_complexity(state.query, "text_me_that")
        state.entities = {"action": "send_sms"}
        state.node_timings["classify"] = time.time() - start
        return state

    # FAST PATH: Scene and routine commands
    # These are special phrases that trigger specific scenes/scripts in HA
    scene_patterns = [
        "movie mode", "movie time", "watch a movie",
        "good night", "goodnight", "bedtime", "night mode", "time for bed",
        "good morning", "morning mode", "wake up",
        "i am leaving", "i'm leaving", "im leaving", "goodbye", "leaving home", "heading out",
        "i am home", "i'm home", "im home", "i'm back", "im back", "home now",
        "romantic mode", "date night",
        "relax mode", "chill mode",
        "party mode", "party time",
        # Round 17: romantic scene patterns
        "vibes for my girl", "my girl comes over", "girlfriend coming",
        "romantic vibes", "vibes for when", "set the mood"
    ]
    # Exclude planning/help/question queries from scene triggers - "help me plan a date night" is NOT lighting control
    # Also exclude conversational follow-ups like "something romantic but also fun"
    planning_exclusions = ["help me", "plan a", "plan my", "planning", "ideas for", "suggestions for",
                          "what should", "where should", "recommend", "what to do",
                          # Conversational follow-ups - asking for options, not commands
                          "something ", "but also", "is that", "is it possible", "how about", "what about",
                          "can you", "could you", "would you", "any "]
    is_planning_not_scene = any(excl in query_lower for excl in planning_exclusions)

    if any(p in query_lower for p in scene_patterns) and not is_planning_not_scene:
        logger.info(f"Fast path: scene/routine detected in '{state.query[:50]}...' - classifying as CONTROL")
        state.intent = IntentCategory.CONTROL
        state.confidence = 0.95
        state.complexity = determine_complexity(state.query, "control")
        state.entities = {"device": "scene"}

        # Cache the classification
        try:
            await cache_client.set(cache_key, {
                "intent": state.intent.value,
                "confidence": state.confidence,
                "complexity": state.complexity,
                "entities": state.entities
            }, ttl=300)
            logger.info(f"Fast path scene classification cached for '{state.query}'")
        except Exception as e:
            logger.warning(f"Intent cache write failed: {e}")

        state.node_timings["classify"] = time.time() - start
        return state

    # FAST PATH: Basic light control commands (turn on/off lights)
    # Skip LLM classification for common commands to reduce latency
    control_action_patterns = ['turn on', 'turn off', 'switch on', 'switch off']
    device_patterns = ['lights', 'light', 'lamp', 'lamps', 'fan', 'fans']
    has_control_action = any(p in query_lower for p in control_action_patterns)
    has_device = any(p in query_lower for p in device_patterns)

    if has_control_action and has_device:
        logger.info(f"Fast path: control command detected in '{state.query[:50]}...' - classifying as CONTROL")
        state.intent = IntentCategory.CONTROL
        state.confidence = 0.95
        # Use complexity detector (catches "all lights except X" patterns)
        state.complexity = determine_complexity(state.query, "control")
        state.entities = {"device": "light"}

        # Extract room if present
        for room in room_names:
            if room in query_lower:
                state.entities["room"] = room
                break

        # Cache the classification
        try:
            await cache_client.set(cache_key, {
                "intent": state.intent.value,
                "confidence": state.confidence,
                "complexity": state.complexity,
                "entities": state.entities
            }, ttl=300)
            logger.info(f"Fast path control classification cached for '{state.query}'")
        except Exception as e:
            logger.warning(f"Intent cache write failed: {e}")

        state.node_timings["classify"] = time.time() - start
        return state

    # FAST PATH: Sensor and occupancy queries
    # Route motion/occupancy questions to CONTROL intent so they hit the sensor handler
    sensor_patterns = [
        'motion', 'movement', 'occupied', 'occupancy',
        'how many people', 'anyone home', 'anybody home', 'someone home',
        'is anyone', 'is anybody', 'who is home', "who's home", 'whos home',
        'people are here', 'people are home', 'is the house empty',
        'is anyone in the house', 'based on motion', 'likely here', 'probably home',
        'last motion', 'when was motion', 'where was motion', 'where is there motion'
    ]
    is_sensor_query = any(p in query_lower for p in sensor_patterns)

    if is_sensor_query:
        logger.info(f"Fast path: sensor/occupancy query detected in '{state.query[:50]}...' - classifying as CONTROL")
        state.intent = IntentCategory.CONTROL
        state.confidence = 0.95
        # Use complexity detector (catches multi-room motion queries)
        state.complexity = determine_complexity(state.query, "control")
        state.entities = {"device": "sensor", "device_type": "sensor"}

        # Cache the classification
        try:
            await cache_client.set(cache_key, {
                "intent": state.intent.value,
                "confidence": state.confidence,
                "complexity": state.complexity,
                "entities": state.entities
            }, ttl=300)
            logger.info(f"Fast path sensor classification cached for '{state.query}'")
        except Exception as e:
            logger.warning(f"Intent cache write failed: {e}")

        state.node_timings["classify"] = time.time() - start
        return state

    # FAST PATH: Music control commands (pause, skip, volume, shuffle, repeat)
    # These should bypass LLM to avoid misclassification as CONTROL
    music_control_fast_patterns = [
        'pause', 'resume', 'stop music', 'stop the music',
        'next song', 'next track', 'skip', 'skip this', 'skip song',
        'previous song', 'previous track', 'go back',
        'play the next one', 'next one', 'play the next',  # Round 11
        'go back one song', 'back one song', 'one song back', 'play the last one',  # Round 11
        'louder', 'quieter', 'volume up', 'volume down', 'turn it up', 'turn it down',
        'shuffle', 'repeat', 'mute music', 'unmute',
        # Additional volume patterns
        'crank up', 'crank it up', 'pump up', 'pump it up', 'blast it',
        'quiet down', 'turn down', 'softer', 'too loud', 'not so loud',
        'turn up the music', 'cant hear it', "can't hear it", 'cant hear',
        "can't hear", 'i cant hear', "i can't hear", 'volume way up',
        'turn the volume way up', 'turn it way up',
        # Now playing patterns
        'song called', 'song name', 'whats that song', 'whats the song',
        'what is that song', 'whats this song', 'what is this song'
    ]
    # Single-word exact matches for music control
    single_word_music = ['next', 'stop', 'previous', 'play']
    is_music_fast_path = any(p in query_lower for p in music_control_fast_patterns)
    is_single_word_music = query_lower.strip() in single_word_music
    if is_music_fast_path or is_single_word_music:
        logger.info(f"Fast path: music control detected in '{state.query[:50]}...' - classifying as MUSIC_CONTROL")
        state.intent = IntentCategory.MUSIC_CONTROL
        state.confidence = 0.95
        state.complexity = "simple"
        state.entities = {"device": "media_player"}

        # Cache the classification
        try:
            await cache_client.set(cache_key, {
                "intent": state.intent.value,
                "confidence": state.confidence,
                "complexity": state.complexity,
                "entities": state.entities
            }, ttl=300)
            logger.info(f"Fast path music control classification cached for '{state.query}'")
        except Exception as e:
            logger.warning(f"Intent cache write failed: {e}")

        state.node_timings["classify"] = time.time() - start
        return state

    # SEARCH PRE-CLASSIFICATION (2026-01-12)
    # Use embedding similarity to skip LLM inference for high-confidence search queries
    # This saves ~1.3s on obvious queries like "restaurants near me" or "weather forecast"
    preclassify_config = await get_feature_config("search_pre_classification")
    if preclassify_config.get("enabled", True):
        try:
            preclassify_start = time.time()
            preclassify_result = await preclassify_query(
                state.query,
                feature_enabled=True,
                feature_config=preclassify_config.get("config", {})
            )
            preclassify_duration = time.time() - preclassify_start

            if state.timing_tracker:
                state.timing_tracker.track_substage("graph", "classify", "preclassify", preclassify_duration)

            if preclassify_result and preclassify_result.skip_llm:
                # High confidence pre-classification - skip LLM inference
                intent_map = {
                    "dining": IntentCategory.DINING,
                    "weather": IntentCategory.WEATHER,
                    "sports": IntentCategory.SPORTS,
                    "control": IntentCategory.CONTROL,
                    "status": IntentCategory.CONTROL,  # Status queries handled by control
                    "news": IntentCategory.NEWS,
                    "flights": IntentCategory.FLIGHTS,
                    "streaming": IntentCategory.STREAMING,
                    "events": IntentCategory.EVENTS,
                    "stocks": IntentCategory.STOCKS,
                    "notification_pref": IntentCategory.NOTIFICATION_PREF,
                }

                if preclassify_result.intent in intent_map:
                    state.intent = intent_map[preclassify_result.intent]
                    state.confidence = preclassify_result.confidence
                    state.complexity = determine_complexity(state.query, preclassify_result.intent)

                    # Cache the classification for future queries
                    try:
                        await cache_client.set(cache_key, {
                            "intent": state.intent.value,
                            "confidence": state.confidence,
                            "complexity": state.complexity,
                            "entities": state.entities or {}
                        }, ttl=300)
                    except Exception as e:
                        logger.warning(f"Pre-classification cache write failed: {e}")

                    logger.info(
                        "preclassify_skip_llm",
                        query=state.query[:50],
                        intent=state.intent.value,
                        confidence=round(preclassify_result.confidence, 3),
                        matched_template=preclassify_result.matched_template[:30],
                        preclassify_ms=round(preclassify_duration * 1000, 1)
                    )

                    state.node_timings["classify"] = time.time() - start
                    return state
            elif preclassify_result:
                # Low confidence - log but fall through to LLM
                logger.info(
                    "preclassify_low_confidence",
                    query=state.query[:50],
                    intent=preclassify_result.intent,
                    confidence=round(preclassify_result.confidence, 3),
                    threshold=preclassify_config.get("config", {}).get("confidence_threshold", 0.85)
                )
        except Exception as e:
            logger.warning(f"Pre-classification failed, falling back to LLM: {e}")

    try:
        # Build classification prompt
        classification_prompt = f"""Classify the following user query into a category, extract entities, and determine complexity.

Categories:
- control: Home automation commands (lights, switches, thermostats, scenes, fans, blinds, sensors)
- music_play: Play music, play artist/genre/playlist (e.g., "play jazz", "play rock in the kitchen", "play music downstairs")
- music_control: Control playing music (pause, skip, volume, stop music)
- weather: Weather information requests
- airports: Airport or flight information
- sports: Sports scores, games, teams
- flights: Flight tracking and schedules
- events: Events, concerts, shows
- streaming: Movies, TV shows, streaming content
- news: News and current events
- stocks: Stock market and financial data
- recipes: Recipe search and cooking
- dining: Restaurant search, food recommendations, places to eat, "near me" food queries (e.g., "find restaurants nearby", "seafood near me", "best pizza", "where to eat")
- directions: Step-by-step navigation, driving/walking/transit routes, how to get FROM point A TO point B (e.g., "how do I get to the airport", "directions to NYC", "route to work")
- text_me_that: User wants info texted/SMS'd to them
- notification_pref: User wants to change notification preferences, opt-out or opt-in to alerts/updates (e.g., "stop the morning notifications", "disable alerts", "turn off morning greetings", "pause notifications")
- general_info: Other information requests
- unknown: Unclear or ambiguous

Complexity Levels:
- simple: Single fact lookup, basic command (e.g., "What's the weather?", "Turn on lights")
- complex: Multi-step reasoning, comparisons (e.g., "Compare weather in SF and NY", "Find events this weekend")
- super_complex: Deep analysis, multiple tool coordination, complex reasoning chains

Query: "{state.query}"

Respond in JSON format:
{{
    "intent": "category_name",
    "confidence": 0.0-1.0,
    "complexity": "simple|complex|super_complex",
    "entities": {{
        "device": "optional device name",
        "location": "optional location",
        "team": "optional sports team",
        "airport": "optional airport code",
        "origin": "optional starting location for directions",
        "destination": "optional destination for directions",
        "travel_mode": "optional: driving, walking, transit, bicycling"
    }}
}}"""

        # Use CLASSIFIER model (1.5B) for fast classification
        # Combine system and user messages into a single prompt
        full_prompt = f"You are an intent classifier. Respond only with valid JSON.\n\n{classification_prompt}"

        # Get model from database or use fallback
        classifier_model = await get_model_for_component("intent_classifier")

        llm_start = time.time()
        result = await llm_router.generate(
            model=classifier_model,
            prompt=full_prompt,
            temperature=0.3,  # Lower temperature for consistent classification
            request_id=state.request_id,
            session_id=state.session_id,
            user_id=state.mode,
            zone=state.room,
            stage="classify"
        )
        llm_duration = time.time() - llm_start

        # Track LLM call timing
        if state.timing_tracker:
            state.timing_tracker.track_substage("graph", "classify", "llm_inference", llm_duration)
            tokens = result.get("eval_count", 0)
            state.timing_tracker.record_llm_call("classify", classifier_model, tokens, int(llm_duration * 1000))

        response_text = result.get("response", "")

        # Parse classification result
        try:
            result = json.loads(response_text)
            state.intent = IntentCategory(result.get("intent", "unknown"))
            state.confidence = float(result.get("confidence", 0.5))
            # Merge LLM entities with existing entities (preserve location from request)
            llm_entities = result.get("entities", {})
            existing_entities = state.entities or {}
            # LLM entities can add to or override most fields, but location from request has priority
            preserved_location = existing_entities.get("location")
            state.entities = {**llm_entities}  # Start with LLM entities
            if preserved_location:
                state.entities["location"] = preserved_location  # Preserve request location
            # Use feature-based complexity detection (more accurate than LLM output)
            state.complexity = determine_complexity(state.query, state.intent.value)

            # Pattern-based classification for validation/override
            # Now returns (IntentCategory, confidence) where confidence indicates specific vs fallback match
            pattern_intent, pattern_confidence = _pattern_based_classification(state.query, return_confidence=True)
            logger.info(f"DEBUG pattern classification: query='{state.query[:50]}', pattern_intent={pattern_intent.value}, pattern_confidence={pattern_confidence}")

            # If LLM returns "unknown", use pattern-based fallback
            if state.intent == IntentCategory.UNKNOWN:
                if pattern_intent != IntentCategory.GENERAL_INFO or pattern_confidence >= 0.8:
                    logger.info(f"LLM returned unknown, pattern-based fallback: {pattern_intent} (confidence: {pattern_confidence})")
                    state.intent = pattern_intent
                    state.confidence = pattern_confidence
            # CRITICAL FIX: If LLM says "directions" but pattern says "dining", always prefer dining
            # This catches common misclassification where "nearby restaurant" is confused with navigation
            elif state.intent == IntentCategory.DIRECTIONS and pattern_intent == IntentCategory.DINING:
                logger.info(
                    f"Dining override: LLM incorrectly classified food query as directions -> dining"
                )
                state.intent = IntentCategory.DINING
                state.confidence = 0.9  # High confidence for food-related patterns
            # WEBSEARCH override: When user explicitly says "search the web for X", always use websearch
            # This catches explicit web search requests that LLM might classify as general_info
            elif pattern_intent == IntentCategory.WEBSEARCH:
                logger.info(
                    f"WebSearch override: LLM={state.intent.value}({state.confidence:.2f}) -> websearch (explicit request)"
                )
                state.intent = IntentCategory.WEBSEARCH
                state.confidence = 0.95  # High confidence for explicit web search requests
            # Round 14: Override EVENTS when query is asking about NOW PLAYING music
            # "yo whats playin rn" is a now_playing query, not events
            elif state.intent == IntentCategory.EVENTS and pattern_intent == IntentCategory.MUSIC_CONTROL:
                query_lower = state.query.lower()
                now_playing_patterns = [
                    "whats playing", "what's playing", "whats playin", "what's playin",
                    "playin rn", "playing rn", "what song", "song is this",
                    "who sings", "whos singing", "who's singing", "artist is this"
                ]
                if any(p in query_lower for p in now_playing_patterns):
                    logger.info(
                        f"Now playing override: LLM incorrectly classified music query as events -> MUSIC_CONTROL"
                    )
                    state.intent = IntentCategory.MUSIC_CONTROL
                    state.confidence = 0.9  # High confidence for now playing patterns
            # Round 15: Override MUSIC_PLAY when query contains color words
            # "hit me with some blue" is a color request, not music
            elif state.intent == IntentCategory.MUSIC_PLAY and pattern_intent == IntentCategory.CONTROL:
                query_lower = state.query.lower()
                color_words = [
                    "blue", "red", "green", "yellow", "orange", "purple", "pink",
                    "cyan", "magenta", "white", "warm", "cool", "sunset", "rainbow",
                    "random color", "random colors",
                    "christmas", "christmas colors"  # Round 16: "throw on some christmas colors"
                ]
                if any(c in query_lower for c in color_words):
                    logger.info(
                        f"Color override: LLM incorrectly classified color query as music_play -> CONTROL"
                    )
                    state.intent = IntentCategory.CONTROL
                    state.confidence = 0.9  # High confidence for color requests
                # Round 16: Override MUSIC_PLAY for scene/vibe patterns
                else:
                    scene_patterns = ["party vibes", "party vibe", "party mode", "party time",
                                     "movie mode", "movie time", "chill mode", "relax mode",
                                     "romantic mode", "date night", "set the mood",
                                     # Round 17: romantic scene patterns
                                     "vibes for my girl", "my girl comes over", "girlfriend coming",
                                     "romantic vibes", "vibes for when"]
                    # Exclude planning/help queries from scene triggers
                    planning_excl = ["help me", "plan a", "plan my", "planning", "ideas for",
                                    "something ", "but also", "is that", "is it possible",
                                    "what should", "where should", "recommend", "any "]
                    is_planning = any(excl in query_lower for excl in planning_excl)
                    if any(s in query_lower for s in scene_patterns) and not is_planning:
                        logger.info(
                            f"Scene override: LLM incorrectly classified scene query as music_play -> CONTROL"
                        )
                        state.intent = IntentCategory.CONTROL
                        state.confidence = 0.9
            # CRITICAL FIX: Override WEATHER when query is asking about INDOOR temperature
            # "whats the temperature inside" should go to thermostat, not weather API
            elif state.intent == IntentCategory.WEATHER and pattern_intent == IntentCategory.CONTROL:
                query_lower = state.query.lower()
                indoor_temp_indicators = [
                    "temperature inside", "temperature in the house", "temperature in here",
                    "temp inside", "inside temp", "indoor temp", "indoors",
                    "how cold is it in", "how hot is it in", "how warm is it in",
                    # Round 16: slang indoor temp queries
                    "temp we at", "what temp we at", "temperature we at",
                    "temp in here", "what is the temp in here"
                ]
                if any(ind in query_lower for ind in indoor_temp_indicators):
                    logger.info(
                        f"Indoor temperature override: LLM incorrectly classified indoor query as weather -> control"
                    )
                    state.intent = IntentCategory.CONTROL
                    state.confidence = 0.9  # High confidence for indoor temperature patterns
            # CRITICAL FIX: Override CONTROL when pattern detects problem/instructional query
            # "TV won't turn on" is a PROBLEM (not a command), should be troubleshooting info
            # "How do I use the coffee maker" is INSTRUCTIONAL (not a command)
            elif (state.intent == IntentCategory.CONTROL and
                  pattern_intent == IntentCategory.GENERAL_INFO and
                  pattern_confidence >= 0.8):
                query_lower = state.query.lower()
                # Check for problem/troubleshooting patterns
                problem_patterns = [
                    "won't turn on", "wont turn on", "won't turn off", "wont turn off",
                    "not working", "isn't working", "isnt working", "doesn't work", "doesnt work",
                    "stopped working", "quit working", "broken", "not responding",
                    "won't respond", "wont respond", "is broken", "seems broken",
                    "having trouble with", "trouble with the", "problem with the",
                    "issue with the", "can't get", "cant get", "won't work", "wont work",
                    "not turning on", "not turning off",
                    # Additional problem indicators
                    "not getting hot", "not getting cold", "not heating", "not cooling",
                    "black screen", "blank screen", "isnt showing", "isn't showing",
                    "not showing", "nothing on", "no picture", "no sound", "no audio",
                    "keeps turning off", "keeps shutting", "keeps restarting",
                    "stuck on", "frozen", "unresponsive", "no response"
                ]
                # Check for instructional patterns
                instructional_patterns = [
                    "how do i use", "how do you use", "how to use", "how does the",
                    "how does this", "how do i work", "how to work the", "how to operate",
                    "how do i operate", "what's the best way to", "whats the best way to",
                    "can you explain how", "show me how", "instructions for"
                ]
                is_problem = any(p in query_lower for p in problem_patterns)
                is_instructional = any(p in query_lower for p in instructional_patterns)
                if is_problem or is_instructional:
                    reason = "problem/troubleshooting" if is_problem else "instructional"
                    logger.info(
                        f"CONTROL override: LLM={state.intent.value}({state.confidence:.2f}) -> GENERAL_INFO ({reason} query detected)"
                    )
                    state.intent = pattern_intent
                    state.confidence = pattern_confidence
            # CRITICAL FIX: Override obvious misclassifications where pattern strongly indicates GENERAL_INFO
            # but LLM returned a specific service intent (like weather for "what time is it")
            # This catches cases where LLM confuses general knowledge queries with service queries
            elif (pattern_intent == IntentCategory.GENERAL_INFO and
                  pattern_confidence >= 0.8 and
                  state.intent in [IntentCategory.WEATHER, IntentCategory.SPORTS, IntentCategory.AIRPORTS,
                                   IntentCategory.FLIGHTS, IntentCategory.EVENTS, IntentCategory.STREAMING,
                                   IntentCategory.NEWS, IntentCategory.STOCKS, IntentCategory.RECIPES,
                                   IntentCategory.DINING]):
                # Check if the query matches strong GENERAL_INFO patterns (time, knowledge questions)
                query_lower = state.query.lower()
                general_info_strong = any(p in query_lower for p in [
                    "what time", "time is it", "current time", "what date", "today's date",
                    "how do", "how does", "how can", "how to", "why do", "why does",
                    "tell me about", "what is", "who is", "explain", "describe",
                    "definition of", "meaning of"
                ])
                if general_info_strong:
                    logger.info(
                        f"Strong GENERAL_INFO override: LLM={state.intent.value}({state.confidence:.2f}) -> GENERAL_INFO (pattern matched time/knowledge query)"
                    )
                    state.intent = pattern_intent
                    state.confidence = pattern_confidence
            # CRITICAL FIX Round 11: Override GENERAL_INFO when pattern detects CONTROL (implicit brightness)
            # Queries like "take it easy on my eyes" get misclassified as general_info by LLM
            # but are actually implicit brightness commands
            elif (state.intent == IntentCategory.GENERAL_INFO and
                  pattern_intent == IntentCategory.CONTROL and
                  pattern_confidence >= 0.8):
                query_lower = state.query.lower()
                # Check for implicit brightness patterns that LLMs often misclassify
                implicit_brightness_patterns = [
                    "take it easy on my eyes", "easy on my eyes", "easy on the eyes",
                    "darken it up", "tone down", "tone it down",
                    "too bright", "too dark", "make it cozy",
                    "can't see", "cant see", "hard to see",
                    "light me up", "lights please"
                ]
                # Round 14: Indoor temperature complaints that get misclassified as general_info
                indoor_temp_complaints = [
                    "mad cold", "mad hot", "hella cold", "hella hot",
                    "its cold", "it's cold", "its hot", "it's hot",
                    "so cold", "so hot", "chilly", "freezing", "too warm",
                    "drop the temp", "drop that temp", "raise the temp", "raise that temp",
                    # Indoor vs outdoor temperature comparisons
                    "warmer inside", "colder inside", "hotter inside", "cooler inside",
                    "inside than outside", "how much warmer is it inside",
                    "indoor vs outdoor", "temp difference"
                ]
                # Round 15: Color requests that get misclassified as general_info
                color_request_patterns = [
                    "random colors", "gimme random", "give me random",
                    "hit me with some", "throw some", "gimme some"
                ]
                is_brightness = any(p in query_lower for p in implicit_brightness_patterns)
                is_temp_complaint = any(p in query_lower for p in indoor_temp_complaints)
                is_color_request = any(p in query_lower for p in color_request_patterns) and any(c in query_lower for c in ["blue", "red", "green", "purple", "color", "colors", "random"])
                if is_brightness:
                    logger.info(
                        f"Implicit brightness override: LLM={state.intent.value}({state.confidence:.2f}) -> CONTROL (implicit brightness command)"
                    )
                    state.intent = pattern_intent
                    state.confidence = pattern_confidence
                elif is_temp_complaint:
                    logger.info(
                        f"Indoor temp override: LLM={state.intent.value}({state.confidence:.2f}) -> CONTROL (temperature complaint detected)"
                    )
                    state.intent = pattern_intent
                    state.confidence = pattern_confidence
                elif is_color_request:
                    logger.info(
                        f"Color request override: LLM={state.intent.value}({state.confidence:.2f}) -> CONTROL (color request detected)"
                    )
                    state.intent = pattern_intent
                    state.confidence = pattern_confidence
            # If LLM confidence is low and pattern has a SPECIFIC match (high confidence),
            # boost the confidence to prevent spurious emerging intents
            elif state.confidence < 0.7 and pattern_confidence >= 0.8:
                # Pattern matched something specific (time queries, knowledge questions, etc.)
                # Use the pattern confidence to prevent emerging intent creation
                logger.info(
                    f"Pattern confidence boost: LLM={state.intent.value}({state.confidence:.2f}) -> pattern confidence {pattern_confidence}"
                )
                state.confidence = pattern_confidence
            # If LLM confidence is moderate (<= 0.75) and pattern-based has specific non-GENERAL_INFO match,
            # prefer pattern-based to avoid LLM misclassifications
            elif state.confidence <= 0.75 and pattern_intent not in [IntentCategory.GENERAL_INFO, IntentCategory.UNKNOWN]:
                if state.intent != pattern_intent:
                    logger.info(
                        f"Pattern-based override: LLM={state.intent.value}({state.confidence:.2f}) -> pattern={pattern_intent.value}"
                    )
                    state.intent = pattern_intent
                    state.confidence = pattern_confidence

            # Round 16: Final override - WEATHER with indoor temp patterns should be CONTROL
            if state.intent == IntentCategory.WEATHER:
                query_lower = state.query.lower()
                indoor_temp_patterns = [
                    "temp we at", "what temp we at", "temperature we at",
                    "temp in here", "what is the temp in here", "whats the temp in here",
                    "indoor temp", "inside temp", "thermostat",
                    # Indoor vs outdoor temperature comparisons
                    "warmer inside", "colder inside", "hotter inside", "cooler inside",
                    "inside than outside", "how much warmer is it inside",
                    "indoor vs outdoor", "temp difference"
                ]
                if any(p in query_lower for p in indoor_temp_patterns):
                    logger.info(
                        f"Final indoor temp override: WEATHER -> CONTROL for indoor temp query"
                    )
                    state.intent = IntentCategory.CONTROL
                    state.confidence = 0.9
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Failed to parse classification: {e}")
            # Fallback to pattern matching with confidence
            state.intent, state.confidence = _pattern_based_classification(state.query, return_confidence=True)
            # Use feature-based complexity even on parse error
            state.complexity = determine_complexity(state.query, state.intent.value)

        logger.info(f"Classified query as {state.intent} with confidence {state.confidence}")

        # INTENT DISCOVERY: Check for novel intents when confidence is low
        # This helps track user needs that don't match existing services
        discovery_start = time.time()
        try:
            if state.confidence < INTENT_DISCOVERY_CONFIG["confidence_threshold"]:
                logger.info(f"Low confidence ({state.confidence}) - triggering intent discovery")
                discovery_result = await discover_intent(
                    query=state.query,
                    current_intent=state.intent.value if state.intent else "unknown",
                    current_confidence=state.confidence,
                    llm_router=llm_router,
                    admin_api_url=ADMIN_API_URL
                )

                if discovery_result.is_novel:
                    state.is_novel_intent = True
                    state.emerging_intent_id = discovery_result.emerging_intent_id
                    state.novel_intent_name = discovery_result.canonical_name

                    # Apply confidence boost if clustered with existing intent
                    if discovery_result.confidence_boost > 0:
                        state.confidence = min(1.0, state.confidence + discovery_result.confidence_boost)
                        logger.info(f"Novel intent clustered: {discovery_result.canonical_name}, "
                                  f"confidence boosted to {state.confidence}")
                    else:
                        logger.info(f"New novel intent created: {discovery_result.canonical_name}")

            # Record intent metric for analytics (fire-and-forget to avoid blocking)
            asyncio.create_task(record_intent_metric(
                intent=state.intent.value if state.intent else "unknown",
                confidence=state.confidence,
                raw_query=state.query,
                session_id=state.session_id or "",
                mode=state.mode,
                room=state.room,
                request_id=state.request_id,
                processing_time_ms=int((time.time() - discovery_start) * 1000),
                is_novel=state.is_novel_intent,
                emerging_intent_id=state.emerging_intent_id,
                complexity=state.complexity or "simple",
                admin_api_url=ADMIN_API_URL
            ))
        except Exception as e:
            logger.warning(f"Intent discovery/metrics failed (non-blocking): {e}")

        # OPTIMIZATION: Cache the result (5 minute TTL)
        try:
            await cache_client.set(cache_key, {
                "intent": state.intent.value,
                "confidence": state.confidence,
                "complexity": state.complexity,  # NEW: Cache complexity
                "entities": state.entities
            }, ttl=300)
            logger.info(f"Intent classification cached for '{state.query}'")
        except Exception as e:
            logger.warning(f"Intent cache write failed: {e}")

    except Exception as e:
        logger.error(f"Classification error: {e}", exc_info=True)
        state.intent = IntentCategory.UNKNOWN
        state.confidence = 0.0
        state.error = f"Classification failed: {str(e)}"

    state.node_timings["classify"] = time.time() - start
    return state

def _pattern_based_classification(query: str, return_confidence: bool = False):
    """
    Fallback pattern-based classification.

    Args:
        query: The user query to classify
        return_confidence: If True, return tuple (IntentCategory, confidence)
                          where confidence indicates if this was a specific match (0.85)
                          or fallback (0.5)

    Returns:
        IntentCategory if return_confidence=False
        (IntentCategory, float) if return_confidence=True
    """
    import re
    query_lower = query.lower()

    # STT error correction for common transcription mistakes
    # Whisper sometimes mishears common phrases
    stt_corrections = [
        # "play" variations
        ("place a music", "play music"),
        ("place music", "play music"),
        ("place a song", "play a song"),
        ("place some music", "play some music"),
        ("play a music", "play music"),  # Grammar fix
        # "play [artist]" variations
        ("place a ", "play "),  # Generic "place a X" → "play X"
        ("place the ", "play "),
        # Volume variations
        ("turn up the volume", "volume up"),
        ("turn down the volume", "volume down"),
        # Other common mishearings
        ("please music", "play music"),
        ("plays music", "play music"),
    ]
    for wrong, correct in stt_corrections:
        if wrong in query_lower:
            query_lower = query_lower.replace(wrong, correct)
            logger.debug("stt_correction_applied", original=wrong, corrected=correct, query=query_lower)

    # Helper to check word boundaries (avoid matching "light" in "twilight")
    def word_match(pattern: str, text: str) -> bool:
        # For multi-word patterns, use simple substring match
        if ' ' in pattern:
            return pattern in text
        # For single words, use word boundary regex
        return bool(re.search(r'\b' + re.escape(pattern) + r'\b', text))

    def result(intent: IntentCategory, is_specific: bool = True):
        """Helper to return result with optional confidence."""
        if return_confidence:
            return (intent, 0.85 if is_specific else 0.5)
        return intent

    # =========================================================================
    # TIME/DATE QUERIES - Check early to avoid emerging intent creation
    # These are common queries that should have HIGH confidence GENERAL_INFO
    # =========================================================================
    time_patterns = [
        "what time", "current time", "what's the time", "whats the time",
        "time is it", "time now", "tell me the time",
        "what date", "today's date", "current date", "what day",
        "what month", "what year"
    ]
    if any(p in query_lower for p in time_patterns):
        return result(IntentCategory.GENERAL_INFO, is_specific=True)

    # =========================================================================
    # CONVERSATIONAL FAREWELLS/GREETINGS - Should NOT trigger RAG tools
    # Round 17: Short casual phrases like "lol ok peace" should get friendly responses
    # =========================================================================
    farewell_patterns = [
        # Casual farewells with slang
        "lol ok peace", "lol okay peace", "ok peace", "okay peace", "peace out",
        "peace", "later", "laters", "see ya", "see you", "catch you later",
        "gotta go", "got to go", "i'm out", "im out", "i'm off", "im off",
        "k bye", "ok bye", "okay bye", "alright bye", "bye bye", "byebye",
        "k thx", "k thanks", "ok thx", "ok thanks", "thx bye", "thanks bye",
        # Standard farewells
        "bye", "goodbye", "good bye", "goodnight", "good night", "nite",
        "night night", "nighty night", "sweet dreams", "take care",
        "have a good one", "have a good night", "have a good day",
        "ttyl", "talk to you later", "talk later",
        # Short thanks/appreciation (not task-related)
        "thanks for your help", "thank you for your help", "thanks for everything",
        "appreciate it", "appreciate you", "appreciate the help",
        "thanks anyway", "thank you anyway",
    ]
    # For short queries (3 words or less), these should be farewells not searches
    word_count = len(query_lower.split())
    if word_count <= 5:
        if any(p in query_lower for p in farewell_patterns):
            return result(IntentCategory.GENERAL_INFO, is_specific=True)
        # Also check if entire query is just a farewell word
        if query_lower.strip() in ["peace", "bye", "later", "goodbye", "goodnight", "thanks", "thx"]:
            return result(IntentCategory.GENERAL_INFO, is_specific=True)

    # =========================================================================
    # EXPLICIT WEB SEARCH REQUESTS - "search the web for X", "google X"
    # These should use the websearch RAG service (Brave Search)
    # =========================================================================
    websearch_patterns = [
        "search the web for", "search the web", "search the internet for",
        "search the internet", "search online for", "search online",
        "look up online", "look it up online", "google ", "google it",
        "find on the web", "find on the internet", "find online",
        "web search for", "web search", "do a web search",
        "search for information on", "search for information about",
        "can you search for", "could you search for",
        "i want you to search", "please search for"
    ]
    if any(p in query_lower for p in websearch_patterns):
        return result(IntentCategory.WEBSEARCH, is_specific=True)

    # =========================================================================
    # ENTERTAINMENT/STREAMING QUERIES - Movies, TV shows, general knowledge
    # These should NOT trigger emerging intents or control patterns
    # =========================================================================
    # Single-word patterns that need word boundary matching (avoid "show" in "shower")
    entertainment_word_boundary = [
        "movie", "film", "scene", "actor", "actress", "character",
        "book", "novel", "story", "plot", "episode", "series", "show"
    ]
    # Multi-word patterns that are safe for substring matching
    entertainment_substring = [
        "tell me about", "what is", "who is", "explain", "describe"
    ]

    # Check if this is likely an entertainment/knowledge query
    # Use word_match for single words to avoid false positives (e.g., "show" in "shower")
    is_entertainment = (
        any(word_match(p, query_lower) for p in entertainment_word_boundary) or
        any(p in query_lower for p in entertainment_substring)
    )

    # SCENE/ROUTINE patterns - these should go to CONTROL not STREAMING
    # Must be checked BEFORE streaming patterns since "movie mode" contains "movie"
    scene_patterns = [
        "movie mode", "movie time", "watch a movie",  # Note: specific phrases, not just "movie"
        "good night", "goodnight", "bedtime", "night mode", "time for bed",
        "good morning", "morning mode", "wake up",
        "i am leaving", "i'm leaving", "im leaving", "goodbye", "leaving home", "heading out",
        "i am home", "i'm home", "im home", "i'm back", "im back", "home now",
        "romantic mode", "date night",
        "relax mode", "chill mode",
        "party mode", "party time",
        # Round 17: romantic scene patterns
        "vibes for my girl", "my girl comes over", "girlfriend coming",
        "romantic vibes", "vibes for when", "set the mood"
    ]
    # Exclude planning/help/question queries from scene triggers
    planning_exclusions_scene = ["help me", "plan a", "plan my", "planning", "ideas for", "suggestions for",
                          "what should", "where should", "recommend", "what to do",
                          "something ", "but also", "is that", "is it possible", "how about", "what about",
                          "can you", "could you", "would you", "any "]
    is_planning_not_scene = any(excl in query_lower for excl in planning_exclusions_scene)
    if any(p in query_lower for p in scene_patterns) and not is_planning_not_scene:
        return result(IntentCategory.CONTROL, is_specific=True)

    # Streaming service patterns - explicit movie/TV queries
    streaming_patterns = [
        "watch", "netflix", "hulu", "disney", "prime video", "amazon prime",
        "hbo", "max", "streaming", "where can i watch", "is there a movie",
        "recommend a movie", "good movies", "what to watch"
    ]
    if any(p in query_lower for p in streaming_patterns):
        return result(IntentCategory.STREAMING, is_specific=True)

    # Movie-specific patterns that should go to GENERAL_INFO (knowledge)
    movie_knowledge_patterns = [
        "scene in", "about the movie", "in the movie", "from the movie",
        "the film", "starring", "directed by", "who played"
    ]
    if any(p in query_lower for p in movie_knowledge_patterns):
        return result(IntentCategory.GENERAL_INFO, is_specific=True)

    if is_entertainment:
        # This is likely a knowledge query, not a control command
        # Skip control pattern matching for these
        pass
    else:
        # INSTRUCTIONAL QUESTIONS: "How do I use X" / "How does X work" should be
        # informational queries, not control commands
        instructional_patterns = [
            "how do i use", "how do you use", "how to use", "how does the",
            "how does this", "how do i work", "how to work the", "how to operate",
            "how do i operate", "what's the best way to", "whats the best way to",
            "can you explain how", "show me how", "instructions for"
        ]
        if any(p in query_lower for p in instructional_patterns):
            return result(IntentCategory.GENERAL_INFO, is_specific=True)

        # PROBLEM DETECTION: If the user is describing a problem/malfunction,
        # this is NOT a control command - route to general_info for troubleshooting
        problem_patterns = [
            "won't turn on", "wont turn on", "won't turn off", "wont turn off",
            "not working", "isn't working", "isnt working", "doesn't work", "doesnt work",
            "stopped working", "quit working", "broken", "not responding",
            "won't respond", "wont respond", "is broken", "seems broken",
            "having trouble with", "trouble with the", "problem with the",
            "issue with the", "can't get", "cant get", "won't work", "wont work",
            "keeps", "not turning on", "not turning off",  # "keeps disconnecting" etc.
            # Additional problem indicators
            "not getting hot", "not getting cold", "not heating", "not cooling",
            "black screen", "blank screen", "isnt showing", "isn't showing",
            "not showing", "nothing on", "no picture", "no sound", "no audio",
            "keeps turning off", "keeps shutting", "keeps restarting",
            "stuck on", "frozen", "unresponsive", "no response"
        ]
        if any(p in query_lower for p in problem_patterns):
            return result(IntentCategory.GENERAL_INFO, is_specific=True)

        # Control patterns (includes all smart home devices)
        # Use word boundaries for ambiguous single words
        control_word_boundary = [
            "light", "lights", "fan", "lamp", "set", "dim"
        ]
        control_patterns = [
            "turn on", "turn off", "brighten", "brighter",
            "everything off", "all off", "lights off",  # Round 14: whole-house off
            "switch", "temperature", "thermostat", "scene",
            # Indoor vs outdoor temperature comparison
            "warmer inside", "colder inside", "hotter inside", "cooler inside",
            "inside than outside", "indoor vs outdoor", "temp difference",
            "inside temp compared", "how much warmer is it inside",
            "color", "colors", "random", "random colors", "gimme random",
            "give me random", "blind", "shade",
            # Round 12: Specific color names and vibe patterns
            "red", "blue", "green", "yellow", "orange", "purple", "pink",
            "cyan", "magenta", "white light", "warm light", "cool light",
            "vibe", "vibes", "mood", "christmas", "christmas colors",  # Round 16
            # Implicit brightness requests
            "too dark", "too bright", "dimmer", "darker",
            "can't see", "cant see", "cannot see", "hard to see",
            "more light", "less light", "make it cozy",
            # Round 11: additional implicit brightness phrases
            "darken it up", "tone down", "tone it down",
            "take it easy on my eyes", "easy on my eyes",
            # Round 17: fade patterns
            "fade the lights", "fade lights", "fade down", "fade it down",
            # Round 13: more brightness patterns
            "kinda dim", "looking dim", "on low", "lights on low",
            "bring them back up", "bring it back up", "back up",
            "light going", "get the light",
            # Round 16: slang and brightness patterns
            "get it lit", "get the", "super bright", "really bright",
            "any lights left on", "lights left on", "party vibes", "party vibe",
            "air moving", "air circulation", "some air",  # Round 16: fan control
            # Appliances
            "oven", "stove", "fridge", "refrigerator", "freezer",
            # Sensors
            "motion", "occupancy", "movement", "sensor", "lux", "illuminance",
            # Presence/occupancy queries (current and historical)
            "anyone home", "anybody home", "someone home", "who's home", "who is home",
            "is anyone", "is anybody", "is someone", "anyone there", "anybody there",
            "anyone in", "anybody in", "someone in", "somebody in",  # Round 15
            "is there anybody", "is there anyone", "is there someone",  # Round 15
            "someone was home", "anyone was home", "anybody was home",
            "last time someone", "last time anyone", "last time somebody",
            "when was someone", "when was anyone", "when was the last",
            "last motion", "last movement", "last activity",
            "recent motion", "recent activity", "who was home", "who was here",
            # Media
            "tv", "television", "apple tv", "homepod", "sonos", "speaker", "playing", "media",
            # Bed warmer / mattress pad
            "warm the bed", "warm up the bed", "preheat the bed", "heat the bed",
            "warm my bed", "mattress pad", "bed warmer", "warm my side",
            "warm the left", "warm the right", "warmer bed", "heat my bed",
            # Lock / door control
            "lock the door", "unlock the door", "lock the front", "unlock the front",
            "lock the back", "unlock the back", "is the door locked", "door locked",
            "check the lock", "all doors",
            # Round 11: casual lock phrases
            "lock up", "lock everything", "lock it up", "lock up the house",
            "lock it down", "lock down", "lock down for the night",  # Round 16
            # Round 13: door status queries and window sensors
            "whats the deal with the door", "what's the deal with the door",
            "whats up with the door", "what's up with the door",
            "front door", "back door", "door status", "door open",
            "hows the door", "how's the door",
            "window open", "windows open", "any windows", "check the windows",
            # Round 17: lock status queries
            "status on the locks", "status of the locks", "check the locks",
            "all the locks", "locks in the house", "any doors unlocked",
            "left any doors", "doors unlocked"
        ]
        # Check word-boundary patterns first
        if any(word_match(p, query_lower) for p in control_word_boundary):
            return result(IntentCategory.CONTROL)
        # Check substring patterns
        if any(p in query_lower for p in control_patterns):
            return result(IntentCategory.CONTROL)

    # Context reference patterns - "do that in the kitchen", "same thing upstairs"
    # These indicate a follow-up control command referencing a previous action
    context_ref_patterns = ["do that", "same thing", "do it", "same color", "that too"]
    room_indicators = ["in the", "upstairs", "downstairs", "hallway", "bedroom", "kitchen",
                       "living room", "office", "bathroom", "basement", "garage"]
    if any(p in query_lower for p in context_ref_patterns):
        if any(r in query_lower for r in room_indicators):
            return result(IntentCategory.CONTROL)

    # Music control patterns (pause, next, volume) - check before play patterns
    music_control_patterns = [
        "pause the music", "pause music", "stop the music", "stop music",
        "next song", "next track", "skip song", "skip track", "skip this", "skip",
        "previous song", "previous track", "go back",
        "volume up", "volume down", "turn it up", "turn it down",
        "louder", "quieter", "mute music", "unmute music", "resume music",
        "resume the music", "resume playing", "resume",
        "shuffle", "shuffle on", "shuffle music", "shuffle my", "enable shuffle",
        "repeat", "repeat this", "repeat song", "repeat on", "enable repeat", "loop",
        # Round 14: Now playing queries
        "whats playing", "what's playing", "whats playin", "what's playin",
        "playin rn", "playing rn", "what song", "song is this", "who sings this",
        "whos singing", "who's singing", "artist is this", "track is this",
        "music mad loud", "mad loud",  # Volume complaints
        "damn loud", "too damn loud", "so damn loud",  # Round 17
        # Round 17: "is music playing" status check queries
        "is music playing", "is anything playing", "is something playing",
        "is there music", "music on right now", "any music on", "any music playing",
        "is the music on", "anything playing right now"
    ]
    if any(p in query_lower for p in music_control_patterns):
        return result(IntentCategory.MUSIC_CONTROL)

    # Music playback patterns (play X, play music)
    music_play_patterns = [
        "play music", "play some music", "put on some music",
        "play jazz", "play rock", "play classical", "play pop",
        "play hip hop", "play country", "play electronic", "play r&b",
        "play metal", "play indie", "play blues", "play reggae",
        "play my playlist", "play workout playlist", "play chill playlist"
    ]
    if any(p in query_lower for p in music_play_patterns):
        return result(IntentCategory.MUSIC_PLAY)

    # Generic "play X" pattern - if starts with "play" and not followed by control words
    if query_lower.startswith("play "):
        # Exclude control patterns like "play/pause"
        control_words = ["pause", "stop", "tv", "movie", "video", "game"]
        remaining = query_lower[5:]  # After "play "
        if not any(remaining.startswith(cw) for cw in control_words):
            return result(IntentCategory.MUSIC_PLAY)

    # Indoor temperature queries should go to CONTROL (thermostat), not weather
    indoor_temp_patterns = [
        "temperature in the house", "temperature inside", "temperature in here",
        "temp in the house", "temp inside", "how hot is it in", "how cold is it in",
        "what's the temp in", "whats the temp in", "house temperature",
        "home temperature", "inside temperature", "check the thermostat",
        "thermostat", "set the temperature", "set temp to", "set it to",
        "crank the heat", "crank up the heat", "turn up the heat", "turn down the heat",
        "turn up the ac", "turn down the ac", "make it warmer", "make it cooler",
        # Added more explicit indoor temperature queries
        "whats the temperature inside", "what's the temperature inside",
        "how warm is it inside", "how cold is it inside", "what temp is it inside",
        # Round 14: casual temperature complaints
        "its cold", "it's cold", "mad cold", "hella cold", "so cold", "chilly", "freezing",
        "its hot", "it's hot", "mad hot", "hella hot", "so hot", "too warm",
        "drop the temp", "drop that temp", "raise the temp", "raise that temp",
        # Round 16: indoor temp queries (vs weather)
        "what temp we at", "temp we at", "what temperature we at",
        "temp in here", "indoor temp", "inside temp"
    ]
    if any(p in query_lower for p in indoor_temp_patterns):
        return result(IntentCategory.CONTROL)

    # Weather patterns (outdoor only) - must come AFTER indoor temp check
    weather_patterns = [
        "weather", "forecast", "rain", "snow", "temperature outside",
        "temp outside", "outside temp", "outside temperature",
        "cold outside", "hot outside", "warm outside",
        "how cold is it outside", "how hot is it outside", "how warm is it outside",
        "weather tomorrow", "tomorrow's weather", "weather today",
        "weather this week", "weather this weekend",
        "is it going to rain", "will it rain", "chance of rain",
        "is it going to snow", "will it snow", "chance of snow"
    ]
    if any(p in query_lower for p in weather_patterns):
        return result(IntentCategory.WEATHER)

    # Airport patterns
    if any(p in query_lower for p in ["airport", "flight", "delay", "bwi", "dca", "iad"]):
        return result(IntentCategory.AIRPORTS)

    # Sports patterns
    sports_patterns = [
        "game", "score", "ravens", "orioles", "team", "schedule",
        "football", "soccer", "basketball", "baseball", "hockey",
        "nfl", "nba", "mlb", "nhl", "mls", "ncaa",
        "playoff", "championship", "season", "match", "vs", "versus"
    ]
    if any(p in query_lower for p in sports_patterns):
        return result(IntentCategory.SPORTS)

    # Recipe patterns - MUST come BEFORE dining to avoid "highly rated" hijacking
    recipe_patterns = [
        "recipe", "recipes", "how to make", "how to cook", "how to bake",
        "how do i make", "how do you make", "cooking instructions",
        "ingredients for", "what's in", "homemade", "from scratch"
    ]
    if any(p in query_lower for p in recipe_patterns):
        return result(IntentCategory.RECIPES)

    # Dining/restaurant patterns
    # Note: "highly rated" is intentionally kept but recipe patterns above take priority
    dining_patterns = [
        "restaurant", "restaurants", "food near", "eat near", "place to eat",
        "good food", "seafood near", "italian near", "mexican near", "chinese near",
        "sushi", "steakhouse", "steak house", "brunch near", "breakfast near",
        "lunch near", "dinner near", "cafe near", "coffee shop", "bar", "pub",
        "diner", "eatery", "dining", "cuisine", "takeout", "delivery",
        "reservation", "outdoor seating", "where to eat", "good place to eat",
        "best place to eat", "highly rated restaurant", "crab near", "crab cake",
        "pizza near", "burger near", "tacos near", "where can i get",
        "good spot", "best spot", " spot near", " spot for"  # "spot" = slang for restaurant
    ]
    if any(p in query_lower for p in dining_patterns):
        return result(IntentCategory.DINING)

    # POI (Point of Interest) patterns - stores, services, places nearby
    # Route to WEBSEARCH to prevent LLM hallucinations (uses Brave Search for real-time data)
    # Note: DINING is specifically for restaurants/food, these are non-food POI
    poi_patterns = [
        "grocery store", "grocery", "supermarket", "pharmacy", "drug store", "drugstore",
        "gas station", "gas near", "fuel station", "convenience store", "liquor store",
        "hardware store", "home depot", "lowes", "target", "walmart", "costco",
        "bank near", "atm near", "post office", "dry cleaner", "laundromat",
        "hospital near", "urgent care", "doctor near", "dentist near",
        "gym near", "fitness"
    ]
    # Location-specific queries that need search (but not generic "nearest" which might be food)
    location_queries = ["where is the nearest", "where is the closest", "where can i find a"]
    has_poi = any(p in query_lower for p in poi_patterns)
    has_location_query = any(p in query_lower for p in location_queries) and has_poi
    if has_poi or has_location_query:
        return result(IntentCategory.WEBSEARCH)  # Use websearch for non-food POI

    # =========================================================================
    # KNOWLEDGE/CONVERSATIONAL QUERIES - Common questions with clear patterns
    # These should be high-confidence GENERAL_INFO, not vague emerging intents
    # =========================================================================
    knowledge_patterns = [
        # General questions
        "how do", "how does", "how can", "how to", "how is",
        "why do", "why does", "why is", "why are", "why did",
        "what does", "what are", "what was", "what were",
        "when did", "when was", "when is", "when are",
        "where is", "where are", "where was", "where did",
        # Conversational
        "can you", "could you", "would you", "will you",
        "do you know", "tell me", "i want to know", "i'd like to know",
        # Factual
        "definition of", "meaning of", "what's the difference",
        "how many", "how much", "how long", "how far", "how old"
    ]
    if any(p in query_lower for p in knowledge_patterns):
        return result(IntentCategory.GENERAL_INFO, is_specific=True)

    # Fallback - unmatched queries get low confidence to allow emerging intent discovery
    # for truly novel queries, but not common conversational patterns
    return result(IntentCategory.GENERAL_INFO, is_specific=False)

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


async def route_music_node(state: OrchestratorState) -> OrchestratorState:
    """
    Handle music playback and control commands via Music Assistant.

    Supports:
    - Playing music (artists, albums, playlists, genres)
    - Music controls (pause, next, volume)
    - Multi-room playback
    - Queue management
    - Music transfer between rooms
    - Browser playback (Jarvis Web)
    """
    start = time.time()

    try:
        if not music_handler:
            state.answer = "Music playback is not configured. Please set up Music Assistant in Home Assistant."
            state.error = "music_handler_not_initialized"
            state.node_timings["route_music"] = time.time() - start
            return state

        if state.intent == IntentCategory.MUSIC_PLAY:
            # Parse the play intent, passing interface_type for browser detection
            intent_data = await music_handler.parse_music_play_intent(
                state.query,
                room=state.room,
                interface_type=getattr(state, 'interface_type', None)
            )

            # Check for browser playback request
            play_in_browser = intent_data.get("play_in_browser", False)

            if play_in_browser:
                # Browser playback requested via "here", "on this device", etc.
                # Since direct browser streaming doesn't work for Spotify (DRM),
                # we play on the room speaker instead and show a mini player UI
                logger.info(
                    "browser_playback_requested_fallback_to_room",
                    media_type=intent_data.get("media_type"),
                    media_id=intent_data.get("media_id"),
                    room=state.room
                )

                media_id = intent_data.get("media_id", "")
                media_type = intent_data.get("media_type", "artist")

                # Play on the room speaker (fallback since browser streaming not supported)
                target_room = state.room if state.room and state.room != "jarvis_web" else "office"
                result = await music_handler.handle_play(
                    media_type=media_type,
                    media_id=media_id,
                    room=target_room,
                    radio_mode=intent_data.get("radio_mode", True)
                )

                state.answer = f"Playing {media_id} on {target_room}."
                state.retrieved_data = {
                    "music_intent": intent_data,
                    "playback_room": target_room
                }

            else:
                # Check if this is a room group request
                # parse_music_play_intent now sets is_room_group if it matched a group/alias
                is_room_group = intent_data.get("is_room_group", False)

                if is_room_group:
                    # Play to room group (synced playback)
                    result = await music_handler.handle_room_group_play(
                        group_name=intent_data.get("room"),
                        media_type=intent_data.get("media_type"),
                        media_id=intent_data.get("media_id"),
                        radio_mode=intent_data.get("radio_mode", True)
                    )
                else:
                    # Single room playback
                    result = await music_handler.handle_play(
                        media_type=intent_data.get("media_type"),
                        media_id=intent_data.get("media_id"),
                        room=intent_data.get("room"),
                        radio_mode=intent_data.get("radio_mode", True)
                    )

                state.answer = result
                state.retrieved_data = {"music_intent": intent_data}

            logger.info(
                "music_play_handled",
                media_id=intent_data.get("media_id"),
                room=intent_data.get("room"),
                browser=play_in_browser
            )

        elif state.intent == IntentCategory.MUSIC_CONTROL:
            # Parse the control intent
            intent_data = await music_handler.parse_music_control_intent(
                state.query,
                room=state.room
            )

            result = await music_handler.handle_control(
                action=intent_data.get("action"),
                room=intent_data.get("room"),
                volume_level=intent_data.get("volume_level")
            )

            state.answer = result
            state.retrieved_data = {"music_intent": intent_data}

            logger.info(
                "music_control_handled",
                action=intent_data.get("action"),
                room=intent_data.get("room")
            )

        # Store context for potential follow-up commands
        if state.session_id and state.answer and "sorry" not in state.answer.lower():
            await store_conversation_context(
                session_id=state.session_id,
                intent="music",
                query=state.query,
                entities={"room": state.room},
                parameters=state.retrieved_data.get("music_intent", {}),
                response=state.answer,
                ttl=300  # 5 minutes
            )

    except Exception as e:
        logger.error(f"Music execution error: {e}", exc_info=True)
        state.answer = "I encountered an error with music playback. Please try again."
        state.error = str(e)

    route_music_duration = time.time() - start
    state.node_timings["route_music"] = route_music_duration
    # Track node time to Prometheus
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "route_music", route_music_duration)
    return state


async def route_tv_node(state: OrchestratorState) -> OrchestratorState:
    """
    Handle Apple TV control commands.

    Supports:
    - Launching apps (Netflix, YouTube, Disney+, etc.)
    - Power control (on/off)
    - Remote navigation (up, down, left, right, select, menu, home)
    - Playback control (play, pause)
    - YouTube deep links
    - Multi-TV control ("open Netflix everywhere")
    """
    start = time.time()

    try:
        if not tv_handler:
            state.answer = "Apple TV control is not configured. Please set up the TV handler."
            state.error = "tv_handler_not_initialized"
            state.node_timings["route_tv"] = time.time() - start
            return state

        # Parse the TV intent from the query
        guest_mode = state.mode == "guest"
        intent = await tv_handler.parse_tv_intent(state.query, room=state.room, mode=state.mode)

        logger.info(
            "tv_intent_parsed",
            action=intent.action,
            app=intent.app_name,
            room=intent.room,
            all_tvs=intent.all_tvs
        )

        result = None

        if intent.action == "launch":
            if intent.all_tvs:
                result = await tv_handler.handle_launch_everywhere(
                    app_name=intent.app_name,
                    guest_mode=guest_mode
                )
            else:
                result = await tv_handler.handle_launch(
                    app_name=intent.app_name,
                    room=intent.room or state.room,
                    guest_mode=guest_mode
                )

        elif intent.action == "power":
            result = await tv_handler.handle_power(
                action=intent.power_action,
                room=intent.room or state.room
            )

        elif intent.action == "navigate":
            result = await tv_handler.handle_navigate(
                command=intent.command,
                room=intent.room or state.room
            )

        elif intent.action == "playback":
            result = await tv_handler.handle_playback(
                command=intent.command,
                room=intent.room or state.room
            )

        elif intent.youtube_video_id:
            result = await tv_handler.handle_youtube_video(
                video_id=intent.youtube_video_id,
                room=intent.room or state.room
            )

        else:
            result = {
                "success": False,
                "message": "I'm not sure what you want me to do with the TV. Try 'open Netflix' or 'turn on the TV'."
            }

        state.answer = result.get("message", "Done.")
        state.retrieved_data = {"tv_intent": intent.__dict__, "result": result}

        if not result.get("success"):
            state.error = result.get("error", "unknown_error")

        logger.info(
            "tv_command_handled",
            action=intent.action,
            success=result.get("success", False)
        )

        # Store context for potential follow-up commands
        if state.session_id and result.get("success"):
            await store_conversation_context(
                session_id=state.session_id,
                intent="tv_control",
                query=state.query,
                entities={"room": intent.room or state.room, "app": intent.app_name},
                parameters=intent.__dict__,
                response=state.answer,
                ttl=300  # 5 minutes
            )

    except Exception as e:
        logger.error(f"TV control error: {e}", exc_info=True)
        state.answer = "I encountered an error controlling the TV. Please try again."
        state.error = str(e)

    route_tv_duration = time.time() - start
    state.node_timings["route_tv"] = route_tv_duration
    # Track node time to Prometheus
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "route_tv", route_tv_duration)
    return state


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

async def _fallback_to_web_search(state: OrchestratorState, rag_service: str, error_msg: str):
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

                from search_providers.content_fetcher import ContentFetcher

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


async def retrieve_node(state: OrchestratorState) -> OrchestratorState:
    """
    Retrieve information from appropriate RAG service.
    Falls back to web search if RAG service is unavailable.
    """
    start = time.time()

    try:
        # Skip RAG for pronoun-based follow-ups that need LLM to resolve from conversation history
        # e.g., "what team does he play for now" after "who was the MVP of the Super Bowl"
        if state.needs_history_context:
            logger.info(f"Skipping RAG - query '{state.query[:50]}...' needs conversation history for pronoun resolution")
            # Use previous response as context for the LLM to answer from
            prev_response = state.entities.get("previous_response", "")
            prev_query = state.entities.get("previous_query", "")
            if prev_response or prev_query:
                state.retrieved_data = {
                    "type": "conversation_context",
                    "previous_query": prev_query,
                    "previous_response": prev_response,
                    "current_query": state.query,
                    "note": "Answer this follow-up question using the previous context"
                }
                state.data_source = "ConversationHistory"
                state.citations.append("Based on previous conversation")
            state.node_timings["retrieve"] = time.time() - start
            return state

        if state.intent == IntentCategory.WEATHER:
            # Check which weather provider to use (feature flag)
            weather_mode = await get_weather_provider_mode()
            service_name = "onecall" if weather_mode == "onecall" else "weather"

            # Get dynamic RAG service URL and update client if needed
            service_url = await get_rag_service_url(service_name)
            if not service_url:
                logger.error(f"{service_name.title()} RAG service URL not configured")
                # Fall back to web search instead of failing
                await _fallback_to_web_search(state, "Weather", "service not configured")
            else:
                try:
                    # Update RAG client URL if different from default
                    rag_client.update_service_url(service_name, service_url)

                    # Call weather service with unified RAG client (includes circuit breaker, rate limiting)
                    # Filter out temporal words that shouldn't be treated as locations
                    TEMPORAL_WORDS = {'today', 'tomorrow', 'tonight', 'yesterday', 'now', 'morning',
                                      'afternoon', 'evening', 'night', 'weekend', 'week', 'day', 'hour'}
                    raw_location = state.entities.get("location", DEFAULT_LOCATION)
                    # Use default location if extracted location is actually a temporal word
                    if raw_location and raw_location.lower().strip() in TEMPORAL_WORDS:
                        logger.info(f"Filtering temporal word '{raw_location}' from location, using default: {DEFAULT_LOCATION}")
                        location = DEFAULT_LOCATION
                    else:
                        location = raw_location or DEFAULT_LOCATION
                    time_ref = state.entities.get("time_ref")

                    # Round 17: Detect far-future weather requests beyond forecast range (7-10 days)
                    # Patterns like "3 weeks", "in 2 weeks", "next month" should acknowledge limitations
                    query_lower = state.query.lower()
                    far_future_patterns = [
                        r'\b(\d+)\s*weeks?\b',  # "3 weeks", "in 2 weeks"
                        r'\bnext\s+month\b', r'\bin\s+a\s+month\b',  # "next month"
                        r'\b(\d+)\s+days?\b',  # Check if days > 10
                    ]
                    is_far_future = False
                    import re as weather_re
                    for pattern in far_future_patterns:
                        match = weather_re.search(pattern, query_lower)
                        if match:
                            if 'week' in pattern or 'month' in pattern:
                                # Any mention of weeks or months is too far
                                num_weeks = int(match.group(1)) if match.lastindex else 1
                                if num_weeks >= 2 or 'month' in query_lower:
                                    is_far_future = True
                                    break
                            elif 'days' in pattern:
                                num_days = int(match.group(1)) if match.lastindex else 0
                                if num_days > 10:
                                    is_far_future = True
                                    break

                    if is_far_future:
                        # Return a limitation acknowledgment instead of inaccurate forecast
                        logger.info(f"Far future weather request detected: '{state.query}'")
                        state.retrieved_data = {
                            "limitation": True,
                            "message": "Weather forecasts are only reliable up to about 7-10 days out. "
                                      "Predictions beyond that become increasingly inaccurate. "
                                      "I can tell you the current weather or the forecast for the next week, "
                                      "but I can't provide reliable information that far in advance.",
                            "location": location
                        }
                        state.data_source = "Weather forecast limitation"
                        state.citations.append("Weather forecast range limitation")
                        # Skip the actual weather API call - go directly to synthesis
                    else:
                        # Normal weather processing - not a far-future request
                        # Determine which endpoint to use based on temporal context
                        if time_ref:
                            # Use forecast endpoint for temporal follow-ups
                            days = 1 if time_ref == "tomorrow" else 5 if time_ref == "this_weekend" else 7
                            # OneCall supports up to 8 days
                            if weather_mode == "onecall" and days > 5:
                                days = min(days, 8)
                            logger.info(f"Using forecast endpoint for time_ref={time_ref}, days={days}, provider={weather_mode}")
                            response = await rag_client.get(
                                service_name,
                                "/weather/forecast",
                                params={"location": location, "days": days}
                            )
                        else:
                            # Use current weather endpoint
                            response = await rag_client.get(
                                service_name,
                                "/weather/current",
                                params={"location": location}
                            )

                        if not response.success:
                            raise Exception(response.error or f"{service_name} service call failed")

                        weather_data = response.data

                        # Validate Weather RAG response quality
                        validation_result, reason, suggestions = validator.validate_weather_response(
                            weather_data, state.query
                        )

                        if validation_result == ValidationResult.VALID:
                            # Response is good, use it
                            state.retrieved_data = weather_data
                            state.data_source = "OpenWeatherMap OneCall 3.0" if weather_mode == "onecall" else "OpenWeatherMap"
                            state.citations.append(f"Weather data from {state.data_source} for {location}")
                            logger.debug(f"Weather RAG validation passed: {reason}, provider={weather_mode}")

                        elif validation_result in [ValidationResult.EMPTY, ValidationResult.INVALID]:
                            # Data is empty or invalid, trigger web search fallback
                            logger.warning(
                                f"Weather RAG validation failed: {validation_result.value} - {reason}"
                            )
                            if suggestions:
                                logger.info(f"Fallback suggestion: {suggestions}")
                            await _fallback_to_web_search(state, "Weather", reason)

                        elif validation_result == ValidationResult.NEEDS_RETRY:
                            # Data structure mismatch or missing information
                            logger.info(
                                f"Weather RAG needs retry: {reason}. Suggestions: {suggestions}"
                            )
                            # For now, fall back to web search for retry scenarios
                            await _fallback_to_web_search(state, "Weather", reason)

                except Exception as e:
                    # RAG service failed - fall back to web search
                    await _fallback_to_web_search(state, "Weather", str(e))

        elif state.intent == IntentCategory.AIRPORTS:
            # Get dynamic RAG service URL and update client if needed
            service_url = await get_rag_service_url("airports")
            if not service_url:
                logger.error("Airports RAG service URL not configured")
                await _fallback_to_web_search(state, "Airports", "service not configured")
            else:
                try:
                    # Update RAG client URL if different from default
                    rag_client.update_service_url("airports", service_url)

                    # Call airports service with unified RAG client
                    airport = state.entities.get("airport", "BWI")
                    response = await rag_client.get("airports", f"/airports/{airport}")

                    if not response.success:
                        raise Exception(response.error or "Airports service call failed")

                    airports_data = response.data

                    # Validate Airports RAG response quality
                    validation_result, reason, suggestions = validator.validate_airports_response(
                        airports_data, state.query
                    )

                    if validation_result == ValidationResult.VALID:
                        # Response is good, use it
                        state.retrieved_data = airports_data
                        state.data_source = "FlightAware"
                        state.citations.append(f"Flight data from FlightAware for {airport}")
                        logger.debug(f"Airports RAG validation passed: {reason}")

                    elif validation_result in [ValidationResult.EMPTY, ValidationResult.INVALID]:
                        # Data is empty or invalid, trigger web search fallback
                        logger.warning(
                            f"Airports RAG validation failed: {validation_result.value} - {reason}"
                        )
                        if suggestions:
                            logger.info(f"Fallback suggestion: {suggestions}")
                        await _fallback_to_web_search(state, "Airports", reason)

                    elif validation_result == ValidationResult.NEEDS_RETRY:
                        # Data structure mismatch or missing information
                        logger.info(
                            f"Airports RAG needs retry: {reason}. Suggestions: {suggestions}"
                        )
                        # For now, fall back to web search for retry scenarios
                        await _fallback_to_web_search(state, "Airports", reason)

                except Exception as e:
                    # RAG service failed - fall back to web search
                    await _fallback_to_web_search(state, "Airports", str(e))

        elif state.intent == IntentCategory.SPORTS:
            # Get dynamic RAG service URL and update client if needed
            service_url = await get_rag_service_url("sports")
            if not service_url:
                logger.error("Sports RAG service URL not configured")
                await _fallback_to_web_search(state, "Sports", "service not configured")
            else:
                try:
                    # Update RAG client URL if different from default
                    rag_client.update_service_url("sports", service_url)

                    # Call sports service with unified RAG client
                    team = state.entities.get("team", "Ravens")

                    # Search for team
                    search_response = await rag_client.get(
                        "sports",
                        "/sports/teams/search",
                        params={"query": team}
                    )
                    if not search_response.success:
                        raise Exception(search_response.error or "Sports team search failed")

                    search_data = search_response.data

                    if search_data.get("teams"):
                        team_info = search_data["teams"][0]
                        team_id = team_info["idTeam"]
                        team_full_name = team_info.get("strTeam", team)
                        team_league = team_info.get("strLeague", "")

                        # Determine league for live scores
                        league_code_map = {
                            "soccer/eng.1": "premier-league",
                            "soccer/esp.1": "la-liga",
                            "football/nfl": "nfl",
                            "basketball/nba": "nba",
                            "baseball/mlb": "mlb",
                            "hockey/nhl": "nhl",
                        }
                        live_league = league_code_map.get(team_league, "nfl")

                        # Fetch last events, next events, AND live scores in parallel
                        # This provides comprehensive data for any sports query
                        last_response, next_response, live_response = await asyncio.gather(
                            rag_client.get("sports", f"/sports/events/{team_id}/last"),
                            rag_client.get("sports", f"/sports/events/{team_id}/next"),
                            rag_client.get("sports", f"/sports/scores/live", params={"league": live_league, "team": team_full_name}),
                            return_exceptions=True
                        )

                        # Build combined response with past, upcoming, and live games
                        events_data = {
                            "team": team_full_name,
                            "team_id": team_id,
                            "league": team_league
                        }

                        # Process last games
                        if isinstance(last_response, Exception) or not last_response.success:
                            events_data["last_games"] = []
                            logger.debug(f"Last events fetch failed: {last_response}")
                        else:
                            events_data["last_games"] = last_response.data.get("events", [])

                        # Process next games (may include season_status if season ended)
                        if isinstance(next_response, Exception) or not next_response.success:
                            events_data["upcoming_games"] = []
                            logger.debug(f"Next events fetch failed: {next_response}")
                        else:
                            next_events = next_response.data.get("events", [])
                            events_data["upcoming_games"] = next_events
                            # Check if season has ended
                            if next_events and next_events[0].get("season_status") == "ended":
                                events_data["season_status"] = "ended"
                                events_data["team_record"] = next_events[0].get("team_record")
                                events_data["team_standing"] = next_events[0].get("team_standing")
                                events_data["season_message"] = next_events[0].get("message")

                        # Process live scores
                        if isinstance(live_response, Exception) or not live_response.success:
                            events_data["live_games"] = []
                        else:
                            live_data = live_response.data
                            live_games = live_data.get("games", [])
                            # Filter to only games with this team
                            team_lower = team_full_name.lower()
                            matching_live = [
                                g for g in live_games
                                if team_lower in g.get("home_team", "").lower()
                                or team_lower in g.get("away_team", "").lower()
                            ]
                            events_data["live_games"] = matching_live
                            if matching_live:
                                events_data["has_live_game"] = True
                                game = matching_live[0]
                                events_data["live_score_summary"] = (
                                    f"{game['away_team']} {game['away_score']} - "
                                    f"{game['home_score']} {game['home_team']} ({game['status']})"
                                )

                        logger.info(
                            f"Sports data fetched for {team_full_name}",
                            last_games=len(events_data.get("last_games", [])),
                            upcoming_games=len(events_data.get("upcoming_games", [])),
                            live_games=len(events_data.get("live_games", [])),
                            season_ended=events_data.get("season_status") == "ended"
                        )

                        # Validate Sports RAG response quality
                        validation_result, reason, suggestions = validator.validate_sports_response(
                            events_data, state.query
                        )

                        if validation_result == ValidationResult.VALID:
                            # Response is good, use it
                            state.retrieved_data = events_data
                            state.data_source = "TheSportsDB"
                            state.citations.append(f"Sports data from TheSportsDB for {team}")
                            logger.debug(f"Sports RAG validation passed: {reason}")

                        elif validation_result in [ValidationResult.EMPTY, ValidationResult.INVALID]:
                            # Data is empty or invalid, trigger web search fallback
                            logger.warning(
                                f"Sports RAG validation failed: {validation_result.value} - {reason}"
                            )
                            if suggestions:
                                logger.info(f"Fallback suggestion: {suggestions}")
                            await _fallback_to_web_search(state, "Sports", reason)

                        elif validation_result == ValidationResult.NEEDS_RETRY:
                            # Data structure mismatch (e.g., got schedule when query wants scores)
                            logger.info(
                                f"Sports RAG needs retry: {reason}. Suggestions: {suggestions}"
                            )
                            # For now, fall back to web search for retry scenarios
                            await _fallback_to_web_search(state, "Sports", reason)

                except Exception as e:
                    # RAG service failed - fall back to web search
                    await _fallback_to_web_search(state, "Sports", str(e))

        elif state.intent == IntentCategory.WEBSEARCH:
            # Explicit web search request - use Brave Search via websearch RAG service
            service_url = await get_rag_service_url("websearch")
            if not service_url:
                # Fall back to environment variable URL
                service_url = WEBSEARCH_SERVICE_URL

            if not service_url or service_url == "http://localhost:8018":
                logger.error("WebSearch RAG service URL not configured properly")
                # Fall back to parallel search as last resort
                state.retrieved_data = {}
                state.data_source = "LLM knowledge (websearch service unavailable)"
            else:
                try:
                    # Extract the actual search query by removing common prefixes
                    search_query = state.query.lower()
                    prefixes_to_remove = [
                        "search the web for ", "search the web ", "search the internet for ",
                        "search the internet ", "search online for ", "search online ",
                        "look up online ", "google ", "find on the web ",
                        "find on the internet ", "find online ", "web search for ",
                        "web search ", "do a web search for ", "do a web search ",
                        "search for information on ", "search for information about ",
                        "can you search for ", "could you search for ",
                        "i want you to search for ", "i want you to search ",
                        "please search for "
                    ]
                    for prefix in prefixes_to_remove:
                        if search_query.startswith(prefix):
                            search_query = state.query[len(prefix):].strip()
                            break
                    else:
                        # No prefix matched, use original query
                        search_query = state.query

                    logger.info(f"WebSearch: Searching for '{search_query}'")

                    # Update RAG client URL if different from default
                    rag_client.update_service_url("websearch", service_url)

                    # Call websearch service
                    search_response = await rag_client.get(
                        "websearch",
                        "/search",
                        params={"query": search_query, "count": 10, "safesearch": "moderate"}
                    )

                    if search_response.success and search_response.data:
                        search_data = search_response.data
                        results = search_data.get("results", [])

                        if results:
                            state.retrieved_data = {
                                "query": search_query,
                                "results": results,
                                "total_results": len(results),
                                "source": "brave_search"
                            }
                            state.data_source = "Brave Search"
                            state.citations.extend([
                                f"[{r.get('title', 'Web result')}]({r.get('url', '')})"
                                for r in results[:3] if r.get('url')
                            ])
                            logger.info(f"WebSearch: Found {len(results)} results")
                        else:
                            logger.warning("WebSearch: No results found, falling back to LLM knowledge")
                            state.retrieved_data = {}
                            state.data_source = "LLM knowledge (no web results)"
                    else:
                        error_msg = search_response.error or "Unknown error"
                        logger.warning(f"WebSearch: Service returned error: {error_msg}")
                        state.retrieved_data = {}
                        state.data_source = f"LLM knowledge (websearch error: {error_msg})"

                except Exception as e:
                    logger.error(f"WebSearch: Exception occurred: {e}", exc_info=True)
                    state.retrieved_data = {}
                    state.data_source = f"LLM knowledge (websearch exception)"

        else:
            # Use intent-based parallel web search for unknown/general queries
            logger.info("Attempting intent-based parallel search")

            # Check if query has location-sensitive keywords that need location context
            location_keywords = ["local", "near me", "nearby", "in my area", "around here", "close by", "in town"]
            query_lower = state.query.lower()
            user_location = state.entities.get("location", DEFAULT_LOCATION) if state.entities else DEFAULT_LOCATION

            if any(kw in query_lower for kw in location_keywords):
                search_query = f"{enhance_query_with_year(state.query)} {user_location}"
                logger.info(f"Location-sensitive query detected, adding location: {user_location}")
            else:
                search_query = enhance_query_with_year(state.query)

            # Execute parallel search with automatic intent classification
            intent, search_results = await parallel_search_engine.search(
                query=search_query,
                location=DEFAULT_LOCATION,
                limit_per_provider=5
            )

            logger.info(f"Search intent classified as: '{intent}'")

            if search_results:
                # Fuse and rank results based on classified intent
                fused_results = result_fusion.get_top_results(
                    results=search_results,
                    query=state.query,
                    intent=intent,
                    limit=5
                )

                logger.info(f"Parallel search returned {len(fused_results)} fused results (intent: {intent})")

                # Convert to dict format for LLM
                search_data = {
                    "intent": intent,
                    "results": [r.to_dict() for r in fused_results],
                    "sources": list(set(r.source for r in fused_results)),
                    "total_results": len(search_results),
                    "fused_results": len(fused_results)
                }

                state.retrieved_data = search_data
                state.data_source = f"Parallel Search ({intent}): {', '.join(search_data['sources'])}"
                state.citations.extend([f"Search result from {r.source}" for r in fused_results])
                logger.info(f"Parallel search completed: intent={intent}, sources={search_data['sources']}")
            else:
                # Fallback to LLM knowledge
                state.retrieved_data = {}
                state.data_source = "LLM knowledge"
                logger.info(f"Parallel search returned no results (intent: {intent}), using LLM knowledge")

        logger.info(f"Retrieved data from {state.data_source}")

    except Exception as e:
        logger.error(f"Retrieval error: {e}", exc_info=True)
        state.error = f"Retrieval failed: {str(e)}"

    retrieve_duration = time.time() - start
    state.node_timings["retrieve"] = retrieve_duration

    # Track retrieve timing in timing tracker
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "retrieve", retrieve_duration)

    return state


async def summarize_conversation_history(
    history: List[Dict[str, str]],
    current_query: str,
    request_id: str = None
) -> str:
    """
    Summarize conversation history into a brief context statement.

    Uses a fast model (qwen3:4b) to compress multiple messages into
    1-2 sentences of relevant context.

    Args:
        history: List of previous messages [{"role": "user/assistant", "content": "..."}]
        current_query: The current user query (for relevance)
        request_id: Optional request ID for logging

    Returns:
        A brief summary string (e.g., "User previously asked about Italian restaurants near home.")
    """
    if not history:
        return ""

    # Format history for summarization
    history_text = "\n".join([
        f"{msg['role'].capitalize()}: {msg['content'][:200]}"  # Truncate long messages
        for msg in history[-6:]  # Only use last 6 messages max
    ])

    prompt = f"""Summarize this conversation in ONE sentence that captures the key context relevant to the new query.

Previous conversation:
{history_text}

New query: "{current_query}"

Write a brief summary (1 sentence) of what the user was discussing. Focus on facts, preferences, or context that might be relevant to the new query. If nothing is relevant, respond with "No relevant prior context."

Summary:"""

    try:
        # Get summarizer model from database config
        summarizer_model = await get_model_for_component("conversation_summarizer")

        # Use configurable model for summarization
        summarize_start = time.time()
        result = await llm_router.generate(
            model=summarizer_model,
            prompt=prompt,
            temperature=0.3,
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
        if summary and len(summary) < 500 and "No relevant prior context" not in summary:
            logger.info(f"History summarized: {len(history)} messages -> {len(summary)} chars")
            return f"Previous context: {summary}"

        return ""

    except Exception as e:
        logger.warning(f"History summarization failed: {e}")
        return ""


async def synthesize_node(state: OrchestratorState) -> OrchestratorState:
    """
    Generate natural language response using LLM with retrieved data and conversation history.
    """
    start = time.time()

    # SKIP SYNTHESIS OPTIMIZATION (2026-01-12)
    # If skip_synthesis flag is set, we already have a templated response
    # (e.g., from status query optimization) - skip LLM synthesis entirely
    if state.skip_synthesis and state.answer:
        logger.info(
            "synthesis_skipped",
            reason="skip_synthesis_flag",
            answer_length=len(state.answer)
        )
        state.node_timings["synthesize"] = time.time() - start
        return state

    try:
        # Check if this is a continuation response (user answering a question from Athena)
        ref_info = state.context_ref_info or {}
        is_continuation = ref_info.get("is_continuation", False)

        # Build synthesis prompt based on context
        if state.retrieved_data:
            context = json.dumps(state.retrieved_data, indent=2)
            synthesis_prompt = f"""Answer the following question using ONLY the provided context.

Question: {state.query}

Context Data:
{context}

CRITICAL ANTI-HALLUCINATION INSTRUCTIONS:
1. ONLY use facts from the Context Data above - NO EXCEPTIONS
2. If the context doesn't have specific information, say "I don't have information about that"
3. NEVER INVENT OR MAKE UP:
   - Business names, restaurant names, or venue names
   - Addresses or locations
   - Phone numbers or hours
   - Prices or ratings
   - Event names or dates
   - Any specific factual details not in the context
4. If asked for recommendations but context is empty, say "I couldn't find current information for that request"
5. Be concise and only state facts that appear in the Context Data
6. If context contains errors or no results, acknowledge that honestly

Response:"""
        elif is_continuation and state.conversation_history:
            # Continuation response - user is answering Athena's question or continuing conversation
            synthesis_prompt = f"""The user is continuing a conversation with you. Their response: "{state.query}"

Based on the conversation history above, understand what the user means and respond appropriately.

INSTRUCTIONS:
1. Look at your previous question/statement in the conversation history
2. Understand what "{state.query}" means in that context
3. If they answered a question you asked, proceed with what they requested originally
4. If they declined something or said "no preference", continue with reasonable defaults
5. Be helpful and continue the task they originally requested

Your response:"""
            logger.info(f"Using continuation prompt for '{state.query}' with {len(state.conversation_history)} history messages")
        else:
            # No data retrieved - must be explicit about lack of information
            synthesis_prompt = f"""Question: {state.query}

CRITICAL: You do NOT have access to current or specific information to answer this question.

You must respond with:
1. Acknowledge you don't have current/specific information
2. Suggest where the user can find this information
3. NEVER make up specific facts, dates, names, numbers, or events

Respond honestly about your limitations.

Response:"""

        # Build prompt with system context
        system_context = """You are Jarvis, an AI assistant inspired by the Jarvis from Iron Man.

Personality:
- Sophisticated, intelligent, and efficient
- Warm but professional, with subtle dry wit when appropriate
- Calm and composed, never flustered
- Genuinely helpful and attentive

Communication style:
- Clear, concise responses
- ALWAYS ask for clarification when a request is ambiguous - NEVER just say "I can't help"
- If you're unsure what the user means, ask! Examples:
  - "peruvian spot" -> ask "Are you looking for a Peruvian restaurant?"
  - "good place" -> ask "What kind of place? Restaurant, store, or something else?"
- Never give up on a request - if you can't fulfill it directly, ask clarifying questions
- If you don't understand a request, say "I'm not sure what you mean" and suggest what you think they might want

Honesty and accuracy:
- NEVER fabricate facts, data, or information
- If you don't have information, say so clearly
- Only state things as fact when you have the data to support them
- For creative requests (stories, jokes, etc.), be imaginative - fiction is not lying

Neutrality on sensitive topics:
- You can share preferences on food, movies, music, hobbies, lifestyle choices
- STAY NEUTRAL on political opinions, religious views, and controversial social topics
- If asked about divisive issues, acknowledge multiple perspectives without taking sides

Voice-friendly formatting (CRITICAL for text-to-speech):
- NEVER use emojis in responses - they don't work with text-to-speech
- Spell out state abbreviations: "MD" -> "Maryland", "CA" -> "California"
- Spell out street abbreviations: "St" -> "Street", "Ave" -> "Avenue", "Blvd" -> "Boulevard"
- Speak zip codes as individual digits: "21117" -> "2 1 1 1 7"
- Spell out "Dr" as "Drive" for addresses, "Doctor" for people
- Say "and" instead of "&"
- Say "number" instead of "#"
- Say "at" instead of "@" in addresses
- Say "degrees Fahrenheit" instead of "°F" or just "F" after temperatures
- Say "miles per hour" instead of "mph"
- For restaurant pricing: "$" -> "budget-friendly", "$$" -> "moderate", "$$$" -> "upscale", "$$$$" -> "fine dining"
- Write times with spaces before and between letters: "10:30 AM" -> "10:30, A M", "5 PM" -> "5, P M" (comma creates pause before A/P)
- For times, use "oh" not "zero": "3:06 PM" -> "three oh six, P M" (NOT "three zero six")
- Expand common abbreviations for natural speech

When you have retrieved data, use it accurately. When you don't have data for a factual question, acknowledge it honestly rather than guessing.

"""

        # Inject base knowledge context from Admin API
        try:
            admin_client = get_admin_client()
            user_mode = state.mode if state.mode else "guest"
            knowledge_context = await get_knowledge_context_for_user(admin_client, user_mode)
            if knowledge_context:
                system_context += knowledge_context
                logger.info(f"Base knowledge context injected for mode={user_mode}")
        except Exception as e:
            logger.warning(f"Failed to fetch base knowledge context: {e}")
            # Continue without base knowledge - not critical

        # Inject guest name for personalization (multi-guest support)
        if state.context and state.context.get("guest_name"):
            guest_name = state.context["guest_name"]
            system_context += f"\nYou are speaking with {guest_name}, a guest at this property. "
            system_context += f"Address them by name when appropriate to provide a personalized experience.\n"
            logger.info(f"Guest context injected for personalization: {guest_name}")

        # Inject relevant memories for context augmentation
        if state.memory_context:
            system_context += state.memory_context
            logger.info("Memory context injected into LLM prompt")

        # Barge-in: If user interrupted previous response, acknowledge naturally
        if state.interruption_context:
            interrupted_response = state.interruption_context.get("interrupted_response", "")
            previous_query = state.interruption_context.get("previous_query", "")
            audio_position_ms = state.interruption_context.get("audio_position_ms", 0)

            # Only acknowledge if they interrupted meaningfully (not just silence detection)
            if interrupted_response:
                system_context += f"""
IMPORTANT: The user just interrupted you while you were responding.
- You were answering: "{previous_query}"
- You had said (approximately): "{interrupted_response[:200]}..."
- They interrupted around {audio_position_ms}ms into your response

Acknowledge naturally that they interrupted (e.g., "Sure, go ahead", "Yes?", "Of course")
and then address their new query. Don't repeat what you were saying unless they ask.
Keep your acknowledgment brief - don't dwell on the interruption.

"""
                logger.info("interruption_context_injected",
                           previous_query=previous_query[:30],
                           audio_position_ms=audio_position_ms)

        # Format conversation history for LLM context
        history_context = ""
        if state.history_summary:
            # Use summarized history (faster)
            history_context = f"{state.history_summary}\n\n"
            logger.info("Using summarized history context")
        elif state.conversation_history:
            # Use full history
            logger.info(f"Including {len(state.conversation_history)} previous messages in context")
            history_context = "Previous conversation:\n"
            for msg in state.conversation_history:
                role = msg["role"].capitalize()
                content = msg["content"]
                history_context += f"{role}: {content}\n"
            history_context += "\n"

        # Combine system context, history, and synthesis prompt
        full_prompt = system_context + history_context + synthesis_prompt

        # Get synthesis model from database or use fallback
        synthesis_model = await get_model_for_component("response_synthesis")

        # Emit LLM generating event for Admin Jarvis monitoring
        llm_start_time = time.time()
        if EVENTS_AVAILABLE and state.session_id:
            await emit_llm_generating(
                session_id=state.session_id,
                model=synthesis_model,
                interface=state.interface_type
            )

        result = await llm_router.generate(
            model=synthesis_model,
            prompt=full_prompt,
            temperature=state.temperature,
            request_id=state.request_id,
            session_id=state.session_id,
            user_id=state.mode,
            zone=state.room,
            intent=state.intent.value if state.intent else None,
            stage="synthesize"
        )

        state.answer = result.get("response", "")

        # Capture token metrics for frontend display
        llm_duration = time.time() - llm_start_time
        state.llm_tokens = result.get("eval_count", 0)
        if state.llm_tokens > 0 and llm_duration > 0:
            state.llm_tokens_per_second = state.llm_tokens / llm_duration
        else:
            state.llm_tokens_per_second = 0.0

        # Track LLM call in timing tracker
        if state.timing_tracker:
            state.timing_tracker.track_substage("graph", "synthesize", "llm_inference", llm_duration)
            state.timing_tracker.record_llm_call("synthesize", synthesis_model, state.llm_tokens, int(llm_duration * 1000))

        # Emit LLM complete event
        if EVENTS_AVAILABLE and state.session_id:
            llm_duration_ms = int((time.time() - llm_start_time) * 1000)
            await emit_llm_complete(
                session_id=state.session_id,
                model=synthesis_model,
                tokens=result.get("tokens", 0),
                duration_ms=llm_duration_ms,
                interface=state.interface_type
            )

        # Add data attribution
        if state.citations:
            state.answer += f"\n\n_Source: {', '.join(state.citations)}_"

        logger.info(f"Synthesized response using {state.model_tier}")

        # SMS Integration: Detect textable content in response
        # Only offer SMS for voice interface when response contains textable info
        if state.interface_type == "voice" and state.answer:
            try:
                should_offer, detected_items, reason = detect_textable_content(state.answer)
                if should_offer and detected_items:
                    state.offer_sms = True
                    state.sms_content_type = detected_items[0].content_type  # Primary content type
                    state.sms_content = extract_sms_content(state.answer, detected_items)
                    logger.info(
                        f"SMS content detected: type={state.sms_content_type}, "
                        f"reason='{reason}', offer_sms=True"
                    )
            except Exception as sms_err:
                logger.warning(f"SMS content detection failed: {sms_err}")
                # Non-critical - continue without SMS offer

        # Store conversation context for follow-up queries
        # This enables "what about tomorrow?" for weather, "how about the Lakers?" for sports, etc.
        if state.session_id and state.answer and state.intent:
            try:
                # Extract entities based on intent type
                context_entities = {}
                context_params = {}

                if state.intent == IntentCategory.WEATHER:
                    # Extract location from query or use default
                    context_entities["location"] = state.entities.get("location", DEFAULT_CITY)
                    context_entities["query_type"] = "weather"
                    if state.retrieved_data:
                        context_params["last_data"] = state.retrieved_data

                elif state.intent == IntentCategory.SPORTS:
                    # Extract team/sport info
                    context_entities["team"] = state.entities.get("team")
                    context_entities["sport"] = state.entities.get("sport")
                    context_entities["query_type"] = "sports"

                elif state.intent == IntentCategory.DINING:
                    # Extract cuisine/location preferences
                    context_entities["cuisine"] = state.entities.get("cuisine")
                    context_entities["location"] = state.entities.get("location", DEFAULT_CITY)
                    context_entities["query_type"] = "dining"

                elif state.intent == IntentCategory.NEWS:
                    # Extract topic
                    context_entities["topic"] = state.entities.get("topic")
                    context_entities["query_type"] = "news"

                elif state.intent == IntentCategory.EVENTS:
                    # Extract event type/location
                    context_entities["event_type"] = state.entities.get("event_type")
                    context_entities["location"] = state.entities.get("location", DEFAULT_CITY)
                    context_entities["query_type"] = "events"

                elif state.intent == IntentCategory.STREAMING:
                    # Extract movie/show info
                    context_entities["title"] = state.entities.get("title")
                    context_entities["query_type"] = "streaming"

                elif state.intent == IntentCategory.STOCKS:
                    # Extract stock symbol
                    context_entities["symbol"] = state.entities.get("symbol")
                    context_entities["query_type"] = "stocks"

                elif state.intent == IntentCategory.FLIGHTS:
                    # Extract flight info
                    context_entities["origin"] = state.entities.get("origin")
                    context_entities["destination"] = state.entities.get("destination")
                    context_entities["query_type"] = "flights"

                elif state.intent == IntentCategory.DIRECTIONS:
                    # Extract directions info
                    context_entities["origin"] = state.entities.get("origin")
                    context_entities["destination"] = state.entities.get("destination")
                    context_entities["travel_mode"] = state.entities.get("travel_mode", "driving")
                    context_entities["query_type"] = "directions"

                # Store context for all RAG-based intents
                await store_conversation_context(
                    session_id=state.session_id,
                    intent=state.intent.value,
                    query=state.query,
                    entities=context_entities,
                    parameters=context_params,
                    response=state.answer or "",  # Store full response for conversation continuity
                    ttl=300  # 5 minute TTL
                )
            except Exception as ctx_err:
                logger.warning(f"Failed to store synthesis context: {ctx_err}")

    except Exception as e:
        logger.error(f"Synthesis error: {e}", exc_info=True)
        state.answer = "I apologize, but I'm having trouble generating a response. Please try again."
        state.error = f"Synthesis failed: {str(e)}"

    synthesize_duration = time.time() - start
    state.node_timings["synthesize"] = synthesize_duration
    # Track node time to Prometheus
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "synthesize", synthesize_duration)
    return state

async def validate_node(state: OrchestratorState) -> OrchestratorState:
    """
    Multi-layer anti-hallucination validation.

    Layer 1: Basic checks (length, error patterns)
    Layer 2: Pattern detection (specific facts without data)
    Layer 3: LLM-based fact checking
    Layer 4: Uncertainty marker detection
    """
    start = time.time()

    # Layer 1: Basic validation
    if not state.answer or len(state.answer) < 10:
        state.validation_passed = False
        state.validation_reason = "Response too short"
        logger.warning(f"Validation failed: {state.validation_reason}")
        validate_duration = time.time() - start
        state.node_timings["validate"] = validate_duration
        if state.timing_tracker:
            state.timing_tracker.track_substage("graph", "validate", "basic_check", validate_duration)
        return state

    if len(state.answer) > 2000:
        state.validation_passed = False
        state.validation_reason = "Response too long"
        logger.warning(f"Validation failed: {state.validation_reason}")
        validate_duration = time.time() - start
        state.node_timings["validate"] = validate_duration
        if state.timing_tracker:
            state.timing_tracker.track_substage("graph", "validate", "basic_check", validate_duration)
        return state

    # Layer 2: Pattern detection for hallucinations
    # Look for specific patterns that indicate fabricated information
    import re

    # Detect specific dates (Month DD, YYYY or MM/DD/YYYY)
    date_patterns = re.findall(r'(\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b)', state.answer)

    # Detect specific times (HH:MM AM/PM)
    time_patterns = re.findall(r'\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\b', state.answer)

    # Detect specific dollar amounts
    money_patterns = re.findall(r'\$\d+(?:,\d{3})*(?:\.\d{2})?', state.answer)

    # Detect phone numbers
    phone_patterns = re.findall(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', state.answer)

    has_specific_facts = bool(date_patterns or time_patterns or money_patterns or phone_patterns)

    # Layer 3: Check if we have data to support specific facts
    has_supporting_data = bool(state.retrieved_data)

    if has_specific_facts and not has_supporting_data:
        logger.warning(f"Response contains specific facts but no supporting data retrieved")
        logger.warning(f"Dates: {date_patterns}, Times: {time_patterns}, Money: {money_patterns}, Phones: {phone_patterns}")

        # Layer 4: LLM-based fact checking
        try:
            fact_check_prompt = f"""You are a fact-checking assistant. Analyze this response for hallucinations.

Original Query: {state.query}

Retrieved Data Available: {'Yes' if state.retrieved_data else 'No'}
{f"Retrieved Data: {json.dumps(state.retrieved_data, indent=2)}" if state.retrieved_data else "No data was retrieved from external sources."}

Generated Response:
{state.answer}

Question: Does this response contain specific factual claims (dates, times, names, phone numbers, prices, events) that are NOT present in the Retrieved Data?

IMPORTANT: If no Retrieved Data is available, ANY specific factual claims are likely hallucinations.

Respond ONLY with valid JSON:
{{"contains_hallucinations": true/false, "reason": "brief explanation", "specific_claims": ["list of suspicious claims"]}}"""

            # Combine system and user prompts
            full_fact_check_prompt = f"You are a precise fact-checking assistant. Always respond with valid JSON.\n\n{fact_check_prompt}"

            # Get validation model from database or use fallback
            validation_model = await get_model_for_component("fact_check_validation")

            validation_start = time.time()
            result = await llm_router.generate(
                model=validation_model,
                prompt=full_fact_check_prompt,
                temperature=0.1,  # Low temperature for consistent checking
                request_id=state.request_id,
                session_id=state.session_id,
                user_id=state.mode,
                zone=state.room,
                intent=state.intent.value if state.intent else None,
                stage="validation"
            )
            validation_duration = time.time() - validation_start

            # Track LLM call for metrics
            if state.timing_tracker:
                tokens = result.get("eval_count", 0)
                state.timing_tracker.record_llm_call(
                    "validation", validation_model, tokens, int(validation_duration * 1000), "fact_check"
                )

            fact_check_response = result.get("response", "")

            # Parse fact check response
            try:
                # Extract JSON from response (handle markdown code blocks)
                json_match = re.search(r'\{.*\}', fact_check_response, re.DOTALL)
                if json_match:
                    fact_check_result = json.loads(json_match.group())

                    if fact_check_result.get("contains_hallucinations", False):
                        state.validation_passed = False
                        state.validation_reason = f"Hallucination detected: {fact_check_result.get('reason', 'Unknown')}"
                        state.validation_details = fact_check_result.get("specific_claims", [])
                        logger.warning(f"Hallucination detected by LLM fact checker: {state.validation_reason}")
                        logger.warning(f"Suspicious claims: {state.validation_details}")
                    else:
                        state.validation_passed = True
                        logger.info("Response passed LLM fact checking")
                else:
                    logger.warning(f"Could not parse fact check response as JSON: {fact_check_response}")
                    # Default to failing validation if we can't parse
                    state.validation_passed = False
                    state.validation_reason = "Could not verify response accuracy"

            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse fact check JSON: {e}")
                # Default to failing validation if we can't parse
                state.validation_passed = False
                state.validation_reason = "Could not verify response accuracy"

        except Exception as e:
            logger.error(f"Fact checking error: {e}", exc_info=True)
            # If fact checking fails, be conservative and fail validation
            state.validation_passed = False
            state.validation_reason = f"Validation error: {str(e)}"

    else:
        # No specific facts or we have supporting data
        state.validation_passed = True
        logger.info("Response passed validation (no specific facts or has supporting data)")

    validate_duration = time.time() - start
    state.node_timings["validate"] = validate_duration
    # Track node time to Prometheus
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "validate", validate_duration)
    return state

async def execute_tools_parallel(
    tool_calls: List[Dict[str, Any]],
    guest_mode: bool = False,
    location: str = None
) -> Dict[str, Any]:
    """
    Execute multiple tool calls in parallel.

    Args:
        tool_calls: List of tool call objects from LLM
        guest_mode: Whether to filter tools by guest mode permissions
        location: User's location for enriching local searches (e.g., "Baltimore, MD")

    Returns:
        Dict mapping tool call IDs to results
    """
    from orchestrator.rag_tools import get_tool_service_url
    from shared.admin_config import get_admin_client

    # Default location if not provided
    if not location:
        location = DEFAULT_LOCATION

    results = {}
    admin_client = get_admin_client()

    # Execute all tool calls concurrently
    async def execute_single_tool(tool_call: Dict[str, Any]) -> tuple:
        """Execute a single tool call and return (tool_call_id, result)."""
        tool_call_id = tool_call.get("id")
        function_name = tool_call.get("function", {}).get("name")
        arguments = tool_call.get("function", {}).get("arguments", {})

        # Track timing for metrics
        start_time = time.time()

        try:
            # Parse arguments if they're a JSON string
            if isinstance(arguments, str):
                arguments = json.loads(arguments)

            # Inject API keys for this tool (if configured in admin)
            # This allows tools to receive their required API keys automatically
            try:
                api_keys_to_inject = await admin_client.get_api_keys_for_tool(function_name)
                if api_keys_to_inject:
                    logger.info(f"Injecting {len(api_keys_to_inject)} API key(s) for tool {function_name}")
                    # Merge API keys into arguments (don't overwrite existing values)
                    for key, value in api_keys_to_inject.items():
                        if key not in arguments:
                            arguments[key] = value
                            logger.debug(f"Injected API key '{key}' for {function_name}")
            except Exception as inject_err:
                # Don't fail the tool call if API key injection fails
                logger.warning(f"API key injection failed for {function_name}: {inject_err}")

            # LOCATION OVERRIDE: Force user's actual location for location-sensitive tools
            # This ensures the LLM's location parameter is overridden with browser geolocation
            location_sensitive_tools = ["search_restaurants", "search_events", "get_directions"]
            if function_name in location_sensitive_tools and location:
                llm_location = arguments.get("location", "not specified")
                if llm_location != location:
                    logger.info(f"Location override: LLM suggested '{llm_location}', using user location '{location}'")
                    arguments["location"] = location
                else:
                    logger.debug(f"Location already matches user location: {location}")

            # DIRECTIONS ORIGIN OVERRIDE: For get_directions, set origin to user's current location
            # when no explicit origin is provided or when LLM uses a placeholder value
            if function_name == "get_directions" and location:
                llm_origin = arguments.get("origin", "")
                llm_destination = arguments.get("destination", "")
                llm_origin_lower = llm_origin.lower().strip() if llm_origin else ""

                # Get placeholder patterns from admin API (cached)
                placeholder_origins = await get_origin_placeholder_patterns()

                # Check if origin should be overridden:
                # 1. No origin provided
                # 2. Origin same as destination (LLM error - user is asking for directions TO somewhere)
                # 3. Origin contains "not specified"
                # 4. Origin is a placeholder value (current location, here, etc.)
                # 5. Origin is very short (less than 5 chars)
                should_override_origin = (
                    not llm_origin or
                    llm_origin_lower == llm_destination.lower().strip() or
                    "not specified" in llm_origin_lower or
                    llm_origin_lower in placeholder_origins or
                    any(p in llm_origin_lower for p in placeholder_origins) or
                    len(llm_origin) < 5
                )

                if should_override_origin:
                    logger.info(f"Directions origin override: LLM suggested '{llm_origin}', using current location '{location}'")
                    arguments["origin"] = location
                else:
                    logger.debug(f"Keeping LLM-specified origin: {llm_origin}")

            # Enrich search_web queries with location context for ambiguous local searches
            if function_name == "search_web" and "query" in arguments:
                query = arguments["query"].lower().strip()

                # Check if query looks like a local business name that needs location context
                # Patterns that suggest a local business:
                # - Short queries (1-4 words) that don't already have a location
                # - Queries starting with "the" (e.g., "the worthington")
                # - Queries asking for phone/contact/address
                query_words = query.split()
                has_location = any(loc.lower() in query for loc in ["baltimore", "maryland", "md", "dc", "washington"])
                is_short_query = len(query_words) <= 5
                looks_like_business = (
                    query.startswith("the ") or
                    "phone" in query or
                    "number" in query or
                    "contact" in query or
                    "address" in query or
                    "hours" in query
                )

                # If it looks like a local business search without location, add location
                if is_short_query and not has_location and looks_like_business:
                    original_query = arguments["query"]
                    arguments["query"] = f"{original_query} {location}"
                    logger.info(f"Enriched search query with location: '{original_query}' -> '{arguments['query']}'")

            # Get service URL from registry (try async first)
            logger.info(f"Looking up service URL for tool: {function_name}")
            from orchestrator.rag_tools import get_tool_service_url_from_registry
            service_url = await get_tool_service_url_from_registry(function_name)

            if service_url:
                logger.info(f"Service registry returned URL for {function_name}: {service_url}")
            else:
                logger.warning(f"Service registry lookup failed for {function_name}, using hardcoded URL")

            # Fallback to hardcoded if registry lookup fails
            if not service_url:
                service_url = get_tool_service_url(function_name)
                logger.info(f"Using hardcoded URL for {function_name}: {service_url}")

            if not service_url:
                logger.error(f"No service URL found for tool: {function_name}")
                return (tool_call_id, {"error": f"Tool {function_name} not configured"})

            # Special handling for get_sports_scores (requires two-step flow)
            if function_name == "get_sports_scores":
                # Update RAG client URL for sports service
                rag_client.update_service_url("sports", service_url)

                try:
                    # Step 1: Search for team to get team_id
                    team_name = arguments.get("team", "")
                    league = arguments.get("league", "")
                    logger.info(f"Calling sports API: searching for team '{team_name}' in league '{league or 'any'}'")

                    # Build search params with optional league filter
                    search_params = {"query": team_name}
                    if league:
                        search_params["league"] = league

                    search_response = await rag_client.get(
                        "sports",
                        "/sports/teams/search",
                        params=search_params
                    )
                    if not search_response.success:
                        raise Exception(search_response.error or "Sports team search failed")

                    search_data = search_response.data

                    # Check if disambiguation is needed (team exists in multiple in-season leagues)
                    if search_data.get("needs_disambiguation") and search_data.get("disambiguation_options"):
                        options = search_data["disambiguation_options"]
                        option_names = [opt["display_name"] for opt in options]
                        latency_ms = int((time.time() - start_time) * 1000)
                        logger.info(f"Sports query needs disambiguation: {team_name} -> {option_names}")
                        return (tool_call_id, {
                            "needs_clarification": True,
                            "team": team_name,
                            "question": f"Which sport are you asking about for {team_name}?",
                            "options": options,
                            "option_names": option_names,
                            "message": f"I found {team_name} in multiple sports that are currently in season: {', '.join(option_names)}. Which one are you interested in?"
                        })

                    teams = search_data.get("teams", [])
                    if not teams:
                        latency_ms = int((time.time() - start_time) * 1000)
                        asyncio.create_task(admin_client.record_tool_metric(
                            tool_name=function_name,
                            success=False,
                            latency_ms=latency_ms,
                            error_message=f"No teams found matching '{team_name}'",
                            guest_mode=guest_mode
                        ))
                        logger.warning(f"No teams found for query: {team_name}")
                        return (tool_call_id, {"error": f"No teams found matching '{team_name}'"})

                    team_id = teams[0]["idTeam"]
                    team_full_name = teams[0].get("strTeam", team_name)
                    logger.info(f"Found team: {team_full_name} (ID: {team_id})")

                    # Determine league for live scores based on team info
                    team_league = teams[0].get("strLeague", "")
                    # Map common league paths to live score league codes
                    league_code_map = {
                        "soccer/eng.1": "premier-league",
                        "soccer/esp.1": "la-liga",
                        "soccer/ger.1": "bundesliga",
                        "soccer/ita.1": "serie-a",
                        "soccer/fra.1": "ligue-1",
                        "soccer/usa.1": "mls",
                        "football/nfl": "nfl",
                        "basketball/nba": "nba",
                        "baseball/mlb": "mlb",
                        "hockey/nhl": "nhl",
                    }
                    live_league = league_code_map.get(team_league, "premier-league")

                    # Step 2: Get last events, next events, AND live scores (parallel)
                    last_response, next_response, live_response = await asyncio.gather(
                        rag_client.get("sports", f"/sports/events/{team_id}/last"),
                        rag_client.get("sports", f"/sports/events/{team_id}/next"),
                        rag_client.get("sports", f"/sports/scores/live", params={"league": live_league, "team": team_full_name}),
                        return_exceptions=True
                    )

                    # Build combined response with past, upcoming, and LIVE games
                    events_data = {"team": team_full_name, "team_id": team_id}

                    if isinstance(last_response, Exception) or not last_response.success:
                        events_data["last_games"] = []
                    else:
                        events_data["last_games"] = last_response.data.get("events", [])

                    if isinstance(next_response, Exception) or not next_response.success:
                        events_data["upcoming_games"] = []
                    else:
                        events_data["upcoming_games"] = next_response.data.get("events", [])

                    # Add live scores if available
                    if isinstance(live_response, Exception) or not live_response.success:
                        events_data["live_games"] = []
                    else:
                        live_data = live_response.data
                        live_games = live_data.get("games", [])
                        # Filter to only games with this team
                        team_lower = team_full_name.lower()
                        matching_live = [g for g in live_games if team_lower in g.get("home_team", "").lower() or team_lower in g.get("away_team", "").lower()]
                        events_data["live_games"] = matching_live
                        if matching_live:
                            # If there's a live game, highlight it
                            events_data["has_live_game"] = True
                            events_data["live_score_summary"] = f"{matching_live[0]['away_team']} {matching_live[0]['away_score']} - {matching_live[0]['home_score']} {matching_live[0]['home_team']} ({matching_live[0]['status']})"

                    # Record success metric
                    latency_ms = int((time.time() - start_time) * 1000)
                    asyncio.create_task(admin_client.record_tool_metric(
                        tool_name=function_name,
                        success=True,
                        latency_ms=latency_ms,
                        guest_mode=guest_mode
                    ))

                    logger.info(f"Tool {function_name} succeeded for team {team_full_name} in {latency_ms}ms")
                    return (tool_call_id, events_data)

                except Exception as e:
                    latency_ms = int((time.time() - start_time) * 1000)
                    error_msg = str(e)
                    asyncio.create_task(admin_client.record_tool_metric(
                        tool_name=function_name,
                        success=False,
                        latency_ms=latency_ms,
                        error_message=error_msg,
                        guest_mode=guest_mode
                    ))
                    logger.error(f"Sports API failed: {e}")
                    return (tool_call_id, {"error": error_msg})

            # Special handling for get_sports_standings (league-wide rankings)
            if function_name == "get_sports_standings":
                rag_client.update_service_url("sports", service_url)

                try:
                    league = arguments.get("league", "nfl")
                    limit = arguments.get("limit", 10)
                    logger.info(f"Fetching standings for league: {league}")

                    standings_response = await rag_client.get(
                        "sports",
                        "/sports/standings",
                        params={"league": league, "limit": limit}
                    )

                    if not standings_response.success:
                        raise Exception(standings_response.error or "Standings fetch failed")

                    standings_data = standings_response.data

                    # Record success metric
                    latency_ms = int((time.time() - start_time) * 1000)
                    asyncio.create_task(admin_client.record_tool_metric(
                        tool_name=function_name,
                        success=True,
                        latency_ms=latency_ms,
                        guest_mode=guest_mode
                    ))

                    logger.info(f"Tool {function_name} succeeded for {league} in {latency_ms}ms")
                    return (tool_call_id, standings_data)

                except Exception as e:
                    latency_ms = int((time.time() - start_time) * 1000)
                    error_msg = str(e)
                    asyncio.create_task(admin_client.record_tool_metric(
                        tool_name=function_name,
                        success=False,
                        latency_ms=latency_ms,
                        error_message=error_msg,
                        guest_mode=guest_mode
                    ))
                    logger.error(f"Standings API failed: {e}")
                    return (tool_call_id, {"error": error_msg})

            # Special handling for search_events (parallel Ticketmaster + SerpAPI + SeatGeek + Community)
            if function_name == "search_events":
                from orchestrator.rag_tools import SERPAPI_EVENTS_URL, SEATGEEK_EVENTS_URL, COMMUNITY_EVENTS_URL

                async def call_ticketmaster(args: dict) -> dict:
                    """Call Ticketmaster events API."""
                    try:
                        async with httpx.AsyncClient(base_url=service_url, timeout=20.0) as client:
                            response = await client.get("/events/search", params=args)
                            response.raise_for_status()
                            data = response.json()
                            # Add source tag
                            for event in data.get("events", []):
                                event["source"] = "ticketmaster"
                            return data
                    except Exception as e:
                        logger.warning(f"Ticketmaster events failed: {e}")
                        return {"events": [], "error": str(e)}

                async def call_serpapi(args: dict) -> dict:
                    """Call SerpAPI events API."""
                    try:
                        # Convert Ticketmaster params to SerpAPI params
                        serpapi_params = {}

                        # Map classification to better query terms
                        classification_map = {
                            "Music": "concerts",
                            "Sports": "sports games",
                            "Arts & Theatre": "theater shows",
                            "Film": "movies",
                            "Comedy": "comedy shows"
                        }

                        if args.get("keyword"):
                            serpapi_params["query"] = args["keyword"]
                        elif args.get("classification_name"):
                            serpapi_params["query"] = classification_map.get(
                                args["classification_name"], args["classification_name"].lower()
                            )
                        else:
                            serpapi_params["query"] = "events"  # SerpAPI works well with generic "events"

                        # Build location from city and state - ensure state is included
                        location_parts = []
                        city = args.get("city")
                        state = args.get("state_code")

                        if city:
                            location_parts.append(city)
                            # Add state if not provided
                            if not state:
                                state = CITY_STATE_MAP.get(city)

                        if state:
                            location_parts.append(state)

                        if location_parts:
                            serpapi_params["location"] = ", ".join(location_parts)

                        # Don't pass date - let SerpAPI return upcoming events
                        serpapi_params["size"] = args.get("size", 20)

                        async with httpx.AsyncClient(base_url=SERPAPI_EVENTS_URL, timeout=20.0) as client:
                            response = await client.get("/events/search", params=serpapi_params)
                            response.raise_for_status()
                            data = response.json()
                            return data
                    except Exception as e:
                        logger.warning(f"SerpAPI events failed: {e}")
                        return {"events": [], "error": str(e)}

                async def call_seatgeek(args: dict) -> dict:
                    """Call SeatGeek events API."""
                    try:
                        # Log received args for debugging
                        logger.info(f"SeatGeek received args: {args}")

                        # Convert Ticketmaster params to SeatGeek params
                        seatgeek_params = {}

                        # Map Ticketmaster classification to better SeatGeek queries
                        classification_map = {
                            "Music": "concerts",
                            "Sports": "sports",
                            "Arts & Theatre": "theater",
                            "Film": "movies",
                            "Comedy": "comedy",
                            "Miscellaneous": "events"
                        }

                        # Determine query: keyword > classification_name > "concerts" (better default)
                        if args.get("keyword"):
                            seatgeek_params["query"] = args["keyword"]
                        elif args.get("classification_name"):
                            seatgeek_params["query"] = classification_map.get(
                                args["classification_name"], args["classification_name"].lower()
                            )
                        else:
                            # Default to "concerts" which yields better results than generic "events"
                            seatgeek_params["query"] = "concerts"

                        # Build location from city and state - ensure state is included
                        location_parts = []
                        city = args.get("city")
                        state = args.get("state_code")

                        if city:
                            location_parts.append(city)
                            # If state not provided but city is, try to add default state
                            if not state:
                                state = CITY_STATE_MAP.get(city)

                        if state:
                            location_parts.append(state)

                        if location_parts:
                            seatgeek_params["location"] = ", ".join(location_parts)

                        # Pass date filter if provided (YYYY-MM-DD format)
                        if args.get("start_date"):
                            seatgeek_params["start_date"] = args.get("start_date")
                        if args.get("date"):
                            seatgeek_params["start_date"] = args.get("date")

                        seatgeek_params["size"] = args.get("size", 20)

                        logger.info(f"SeatGeek calling with params: {seatgeek_params}")

                        async with httpx.AsyncClient(base_url=SEATGEEK_EVENTS_URL, timeout=20.0) as client:
                            response = await client.get("/events/search", params=seatgeek_params)
                            response.raise_for_status()
                            data = response.json()
                            logger.info(f"SeatGeek returned {len(data.get('events', []))} events")
                            return data
                    except Exception as e:
                        logger.warning(f"SeatGeek events failed: {e}")
                        return {"events": [], "error": str(e)}

                async def call_community_events(args: dict) -> dict:
                    """Call Community Events API (scraped local events)."""
                    try:
                        # Convert params for community events
                        community_params = {}

                        # Pass through query/keyword for text search
                        keyword = args.get("keyword", "")
                        if keyword:
                            community_params["query"] = keyword

                        # Detect past-tense queries (historical questions)
                        past_tense_patterns = [
                            "when was", "when did", "did they have", "was there",
                            "last year", "last month", "last week", "happened",
                            "took place", "occurred", "previous", "past"
                        ]
                        keyword_lower = keyword.lower() if keyword else ""
                        include_past = any(p in keyword_lower for p in past_tense_patterns)
                        if include_past:
                            community_params["include_past"] = True
                            logger.info("Detected past-tense query, including historical events")

                        # Pass start_date if provided
                        if args.get("start_date"):
                            community_params["start_date"] = args["start_date"]
                        elif args.get("date"):
                            community_params["start_date"] = args["date"]

                        community_params["size"] = args.get("size", 20)
                        community_params["free_only"] = False  # Include all events

                        logger.info(f"Community Events calling with params: {community_params}")

                        async with httpx.AsyncClient(base_url=COMMUNITY_EVENTS_URL, timeout=20.0) as client:
                            response = await client.get("/events/search", params=community_params)
                            response.raise_for_status()
                            data = response.json()
                            logger.info(f"Community Events returned {len(data.get('events', []))} events")
                            return data
                    except Exception as e:
                        logger.warning(f"Community Events failed: {e}")
                        return {"events": [], "error": str(e)}

                # Call all four APIs in parallel
                ticketmaster_task = asyncio.create_task(call_ticketmaster(arguments))
                serpapi_task = asyncio.create_task(call_serpapi(arguments))
                seatgeek_task = asyncio.create_task(call_seatgeek(arguments))
                community_task = asyncio.create_task(call_community_events(arguments))

                ticketmaster_result, serpapi_result, seatgeek_result, community_result = await asyncio.gather(
                    ticketmaster_task, serpapi_task, seatgeek_task, community_task, return_exceptions=True
                )

                # Handle exceptions
                if isinstance(ticketmaster_result, Exception):
                    ticketmaster_result = {"events": [], "error": str(ticketmaster_result)}
                if isinstance(serpapi_result, Exception):
                    serpapi_result = {"events": [], "error": str(serpapi_result)}
                if isinstance(seatgeek_result, Exception):
                    seatgeek_result = {"events": [], "error": str(seatgeek_result)}
                if isinstance(community_result, Exception):
                    community_result = {"events": [], "error": str(community_result)}

                # Merge results from all sources
                all_events = []
                all_events.extend(ticketmaster_result.get("events", []))
                all_events.extend(serpapi_result.get("events", []))
                all_events.extend(seatgeek_result.get("events", []))
                all_events.extend(community_result.get("events", []))

                # Deduplicate events based on normalized title + venue + date
                def normalize_text(text) -> str:
                    """Normalize text for comparison - lowercase, remove punctuation, extra spaces."""
                    if not text:
                        return ""
                    # Handle dict values (some APIs return venue as dict)
                    if isinstance(text, dict):
                        text = text.get("name") or text.get("title") or str(text)
                    if not isinstance(text, str):
                        text = str(text)
                    import re
                    # Lowercase and remove common punctuation
                    text = text.lower()
                    text = re.sub(r'[^\w\s]', '', text)
                    # Remove extra whitespace
                    text = ' '.join(text.split())
                    return text

                def get_event_key(event: dict) -> str:
                    """Create a unique key for deduplication."""
                    title = normalize_text(event.get("title") or event.get("name") or "")
                    venue = normalize_text(event.get("venue") or event.get("address") or "")
                    date = event.get("date") or event.get("time") or ""
                    # Use first 30 chars of title + first 20 of venue + date for key
                    return f"{title[:30]}|{venue[:20]}|{date[:10]}"

                def event_completeness(event: dict) -> int:
                    """Score event by data completeness - prefer events with more info."""
                    score = 0
                    if event.get("title"): score += 1
                    if event.get("venue"): score += 1
                    if event.get("address"): score += 1
                    if event.get("date"): score += 1
                    if event.get("time"): score += 1
                    if event.get("link"): score += 1
                    if event.get("tickets"): score += 1
                    if event.get("thumbnail"): score += 1
                    return score

                # Deduplicate - keep event with most complete data
                seen_events = {}
                for event in all_events:
                    key = get_event_key(event)
                    if key not in seen_events:
                        seen_events[key] = event
                    else:
                        # Keep the more complete event
                        if event_completeness(event) > event_completeness(seen_events[key]):
                            seen_events[key] = event

                original_count = len(all_events)
                all_events = list(seen_events.values())
                dedup_count = original_count - len(all_events)
                if dedup_count > 0:
                    logger.info(f"Deduplicated {dedup_count} duplicate events ({original_count} -> {len(all_events)})")

                # Sort by date if available
                def get_event_date(event):
                    date_val = event.get("date") or event.get("dates", {}).get("date") or ""
                    return date_val

                all_events.sort(key=get_event_date)

                merged_result = {
                    "events": all_events,
                    "total_events": len(all_events),
                    "sources": {
                        "ticketmaster": len(ticketmaster_result.get("events", [])),
                        "serpapi": len(serpapi_result.get("events", [])),
                        "seatgeek": len(seatgeek_result.get("events", [])),
                        "community": len(community_result.get("events", []))
                    }
                }

                # Record success metric
                latency_ms = int((time.time() - start_time) * 1000)
                asyncio.create_task(admin_client.record_tool_metric(
                    tool_name=function_name,
                    success=True,
                    latency_ms=latency_ms,
                    guest_mode=guest_mode
                ))

                logger.info(f"Parallel events search completed: {merged_result['sources']} in {latency_ms}ms")
                return (tool_call_id, merged_result)

            # Map tool names to RAG service endpoints
            endpoint_map = {
                "get_weather": "/weather/current",
                "get_airport_info": "/airports/search",
                "search_flights": "/flights/search",
                "search_events": "/events/search",
                "search_streaming": "/streaming/search",
                "get_news": "/news/search",
                "get_stock_info": "/stocks/quote",
                "search_web": "/search",
                "search_restaurants": "/dining/search",
                "search_recipes": "/recipes/search",
                "get_directions": "/directions/route",
                "get_train_schedule": "/amtrak/schedule",
                "scrape_website": "/scrape",
                "scrape_webpage_bright": "/scrape",
                "compare_prices": "/search",
                "get_tesla_metrics": "/query",
                "request_media": "/query"
            }

            endpoint = endpoint_map.get(function_name, "/search")

            # Special handling for flights - convert destination to query
            if function_name == "search_flights":
                # OPTIMIZATION: Resolve city names to airport codes (2026-01-12)
                # This fixes FlightAware API 400 errors from natural language destinations
                airport_lookup_config = await get_feature_config("airport_code_lookup")
                if airport_lookup_config.get("enabled", True):
                    try:
                        arguments = await resolve_flight_parameters(
                            arguments,
                            airports_service_url="http://localhost:8011",
                            feature_enabled=True
                        )
                        logger.info("airport_lookup_applied", arguments=arguments)
                    except Exception as e:
                        logger.warning("airport_lookup_failed", error=str(e))

                # Flights API expects 'query' param, not origin/destination
                # Convert parameters for flights API
                if 'destination' in arguments and 'origin' not in arguments:
                    # Only destination provided - search for flights to this airport
                    arguments = {'query': arguments['destination']}
                elif 'destination' in arguments and 'origin' in arguments:
                    # Both provided - search by destination (more common use case)
                    arguments = {'query': arguments['destination']}
                elif 'origin' in arguments:
                    # Only origin provided - search for flights from this airport
                    arguments = {'query': arguments['origin']}
                # Note: if neither provided, let API handle the error

            # Special handling for Tesla - API expects 'q' not 'query'
            if function_name == "get_tesla_metrics":
                if 'query' in arguments:
                    arguments = {'q': arguments['query']}

            # Call the RAG service using unified client with resilience patterns
            # Map function name to RAG service name for client registration
            service_name_map = {
                "get_weather": "weather",
                "get_airport_info": "airports",
                "get_stock_info": "stocks",
                "get_news": "news",
                "search_events": "events",
                "search_flights": "flights",
                "search_web": "websearch",
                "search_restaurants": "dining",
                "search_streaming": "streaming",
                "search_recipes": "recipes",
                "get_directions": "directions",
                "get_train_schedule": "amtrak",
                "scrape_website": "site-scraper",
                "scrape_webpage_bright": "brightdata",
                "compare_prices": "price-compare",
                "get_tesla_metrics": "tesla",
            }
            rag_service_name = service_name_map.get(function_name, function_name.replace("get_", "").replace("search_", ""))

            # Update RAG client with dynamic service URL
            rag_client.update_service_url(rag_service_name, service_url)

            logger.info(f"Calling tool {function_name} via RAG client ({rag_service_name}) with args: {arguments}")

            # Determine HTTP method based on tool
            # Most RAG service endpoints use GET with query params
            get_tools = [
                "get_weather", "get_airport_info", "get_stock_info", "get_news",
                "search_events", "search_flights", "search_web", "search_restaurants",
                "search_streaming", "search_recipes", "get_directions", "get_train_schedule",
                "scrape_website", "compare_prices", "get_tesla_metrics"
            ]
            # PARALLEL SEARCH: For search_web, race Brave + SearXNG simultaneously
            if function_name == "search_web":
                try:
                    from orchestrator.parallel_search import get_parallel_search_engine
                    parallel_engine = await get_parallel_search_engine(rag_client)
                    query = arguments.get("query", "")
                    max_results = arguments.get("count", 5)

                    # Race all providers: Brave + SearXNG (with fallback to Bright Data + DuckDuckGo)
                    parallel_result = await parallel_engine.search_primary_parallel(query, max_results)

                    if parallel_result and "results" in parallel_result and len(parallel_result["results"]) > 0:
                        logger.info(f"Parallel search succeeded via {parallel_result.get('source', 'unknown')}")
                        latency_ms = int((time.time() - start_time) * 1000)
                        asyncio.create_task(admin_client.record_tool_metric(
                            tool_name=function_name,
                            success=True,
                            latency_ms=latency_ms,
                            guest_mode=guest_mode
                        ))
                        return (tool_call_id, parallel_result)
                    else:
                        raise Exception("Parallel search returned no results")
                except Exception as parallel_err:
                    logger.error(f"Parallel search failed: {parallel_err}")
                    raise Exception(f"Search failed: {parallel_err}")

            if function_name in get_tools:
                # GET request with query params
                response = await rag_client.get(rag_service_name, endpoint, params=arguments)
            else:
                # POST request with JSON body
                response = await rag_client.post(rag_service_name, endpoint, json=arguments)

            if not response.success:
                raise Exception(response.error or f"Tool {function_name} call failed")

            result_data = response.data

            # Record success metric
            latency_ms = int((time.time() - start_time) * 1000)
            asyncio.create_task(admin_client.record_tool_metric(
                tool_name=function_name,
                success=True,
                latency_ms=latency_ms,
                guest_mode=guest_mode
            ))

            # Record Prometheus metrics
            try:
                record_tool_execution(
                    tool_name=function_name,
                    source="rag",
                    success=True,
                    latency_seconds=(time.time() - start_time),
                    guest_mode=guest_mode
                )
            except Exception as metrics_err:
                logger.warning(f"Failed to record tool metrics: {metrics_err}")

            logger.info(f"Tool {function_name} succeeded in {latency_ms}ms")
            return (tool_call_id, result_data)

        except Exception as e:
            latency_ms = int((time.time() - start_time) * 1000)
            error_msg = str(e)
            asyncio.create_task(admin_client.record_tool_metric(
                tool_name=function_name,
                success=False,
                latency_ms=latency_ms,
                error_message=error_msg,
                guest_mode=guest_mode
            ))

            # Record Prometheus metrics
            try:
                record_tool_execution(
                    tool_name=function_name,
                    source="rag",
                    success=False,
                    latency_seconds=(time.time() - start_time),
                    guest_mode=guest_mode,
                    error_type=type(e).__name__
                )
            except Exception as metrics_err:
                logger.warning(f"Failed to record tool metrics: {metrics_err}")

            logger.error(f"Tool {function_name} failed: {e}", exc_info=True)
            return (tool_call_id, {"error": error_msg})

    # Execute all tools in parallel
    tasks = [execute_single_tool(tc) for tc in tool_calls]
    results_list = await asyncio.gather(*tasks, return_exceptions=True)

    # Build results dict
    for item in results_list:
        if isinstance(item, Exception):
            logger.error(f"Tool execution exception: {item}")
            continue
        tool_call_id, result = item
        results[tool_call_id] = result

    return results


async def tool_call_node(state: OrchestratorState) -> OrchestratorState:
    """
    Execute LLM-based tool calling when pattern-based routing is insufficient.

    This is the "smart path" that uses the LLM to decide which tools to call
    and orchestrates multiple tool calls if needed.

    Triggered by:
    - Low confidence in intent classification
    - Ambiguous intent (GENERAL_INFO, UNKNOWN)
    - Multi-domain keywords in query
    - Empty RAG data from retrieve node
    - Validation failure
    """
    import asyncio
    from orchestrator.rag_tools import get_rag_tools

    start = time.time()

    # Granular timing for tool_call node debugging
    timing_breakdown = {}

    # Check for user input escalation triggers (correction, frustration, explicit request)
    escalation_target = None
    try:
        escalation_target = await check_escalation_triggers(
            state,
            context="user_input"
        )
        if escalation_target:
            state.complexity = escalation_target  # Override complexity
            logger.info(f"Complexity escalated to {escalation_target} based on user input")
    except Exception as esc_err:
        logger.warning(f"Escalation check failed: {esc_err}")

    # Check existing escalation state (from previous turn)
    if state.session_id:
        existing_escalation = get_current_escalation(state.session_id)
        if existing_escalation and not escalation_target:
            state.complexity = existing_escalation.get("escalated_to", state.complexity)
            logger.info(
                f"Using existing escalation: {state.complexity} "
                f"(rule: {existing_escalation.get('rule_name')}, "
                f"turns remaining: {existing_escalation.get('turns_remaining')})"
            )
            decrement_escalation_turns(state.session_id)

    try:
        # Get admin client for tool calling settings
        admin_fetch_start = time.time()
        admin_client = get_admin_client()

        # Fetch tool calling settings
        settings = await admin_client.get_tool_calling_settings()
        timing_breakdown["admin_settings"] = time.time() - admin_fetch_start

        if not settings or not settings.get("enabled", True):
            logger.warning("Tool calling is disabled in admin settings")
            state.error = "Tool calling disabled"
            state.node_timings["tool_call"] = time.time() - start
            return state

        # Fetch enabled tools (filtered by guest mode if needed)
        guest_mode = state.mode == "guest"

        # OPTIMIZATION: Use cached tool schemas if available
        tool_load_start = time.time()
        cache_key = f"{'guest' if guest_mode else 'owner'}_tools"
        if cache_key in tool_schema_cache:
            tools = tool_schema_cache[cache_key]
            logger.info(f"Using cached tool schemas for {cache_key}")
        else:
            # Try unified tool registry first (Phase 3: self-building tools support)
            if TOOL_REGISTRY_AVAILABLE:
                try:
                    registry = await ToolRegistryFactory.create()
                    tools = registry.get_tool_schemas(guest_mode=guest_mode)
                    logger.info(f"Loaded {len(tools)} tools from unified registry (guest_mode={guest_mode})")

                    # Cache the schemas
                    tool_schema_cache[cache_key] = tools
                    # Registry doesn't have web_search_fallback_enabled per-tool, so cache empty list
                    # This means all tools will use default fallback=True when using registry
                    tool_config_cache[cache_key] = []
                except Exception as registry_err:
                    logger.warning(f"Tool registry failed, falling back to legacy: {registry_err}")
                    tools = None

            # Fallback to legacy get_rag_tools if registry failed or unavailable
            if not TOOL_REGISTRY_AVAILABLE or not tools:
                enabled_tools_db = await admin_client.get_enabled_tools(guest_mode=guest_mode)
                # Get function schemas for LLM
                tools = get_rag_tools(enabled_tools=enabled_tools_db, guest_mode=guest_mode)
                # Cache for future requests
                tool_schema_cache[cache_key] = tools
                # Also cache the raw tool configs (includes web_search_fallback_enabled)
                tool_config_cache[cache_key] = enabled_tools_db
                logger.info(f"Using legacy tool loader, cached {len(tools)} tools for {cache_key}")

        timing_breakdown["tool_loading"] = time.time() - tool_load_start

        if not tools:
            logger.warning("No tools available for tool calling")
            state.error = "No tools available"
            state.node_timings["tool_call"] = time.time() - start
            return state

        # CRITICAL: Filter tools by intent to reduce complexity for LLM
        # Qwen2.5 and other models struggle with 11 tools at once
        # Only send the 1-3 most relevant tools based on classified intent
        intent_to_tools = {
            "weather": ["get_weather"],
            "sports": ["get_sports_scores", "get_sports_standings"],
            "airports": ["get_airport_info"],
            "flights": ["search_flights", "get_train_schedule"],  # Amtrak often confused with flights
            "events": ["search_events"],
            "streaming": ["search_streaming"],
            "news": ["get_news"],
            "stocks": ["get_stock_info"],
            "websearch": ["search_web", "scrape_website", "scrape_webpage_bright"],  # Web search can include scraping
            "scraping": ["scrape_webpage_bright", "scrape_website"],  # Dedicated scraping intent
            "dining": ["search_restaurants", "scrape_website"],  # Can scrape restaurant websites for details
            "recipes": ["search_recipes"],
            "directions": ["get_directions", "search_restaurants"],  # search_restaurants can find EV chargers, gas stations, etc. along routes
            "transit": ["get_train_schedule", "get_directions"],  # Trains and transit directions
            "shopping": ["compare_prices", "search_web"],  # Price comparison for shopping queries
            "tesla": ["get_tesla_metrics"],  # Tesla vehicle queries (owner mode only)
            "media": ["request_media"],  # Media requests via Overseerr (owner mode only)
            # Multi-tool intents for complex queries
            "planning": ["get_weather", "search_events", "search_restaurants"],
            "itinerary": ["get_weather", "search_events", "search_restaurants"],
        }

        # Check for planning/itinerary keywords that need multiple tools
        query_lower = state.query.lower() if state.query else ""
        planning_keywords = [
            "itinerary", "day trip", "things to do",
            "what should we do", "whole day", "full day", "date idea",
            "day date", "fun day", "day of fun", "surprise me",
            "fun things", "good food", "represent baltimore", "baltimore experience",
            "local experience", "show me around", "take me around",
            # More specific "plan" phrases to avoid matching "party planning"
            "plan my day", "plan for today", "plan for tomorrow", "plan for the day",
            "plan the day", "plan a day", "plan our day", "plan this weekend"
        ]
        # Exclude party/event planning from triggering day itinerary
        party_exclusions = ["birthday party", "party for", "party planning", "planning a party",
                           "plan a party", "anniversary party", "surprise party", "baby shower",
                           "bridal shower", "wedding", "graduation party", "retirement party",
                           "house party", "dinner party"]
        has_party_exclusion = any(excl in query_lower for excl in party_exclusions)
        is_planning_query = any(kw in query_lower for kw in planning_keywords) and not has_party_exclusion

        # Check for website scraping keywords
        website_keywords = ["check their website", "look at their website", "check the website", "their site", "their menu", "their hours", "their happy hour", "website for"]
        is_website_query = any(kw in query_lower for kw in website_keywords)

        # Check for price comparison keywords
        price_keywords = ["cheapest", "best price", "compare price", "lowest price", "best deal", "price for", "how much does", "where can i buy"]
        is_price_query = any(kw in query_lower for kw in price_keywords)

        # Check for business phone number lookup keywords
        phone_lookup_keywords = ["phone number for", "number for the", "call the", "contact info", "phone for", "what's their number", "whats their number", "their phone"]
        is_phone_lookup = any(kw in query_lower for kw in phone_lookup_keywords)

        # Check for Tesla vehicle queries (OWNER MODE ONLY)
        # These are queries about the user's personal Tesla vehicle data from TeslaMate
        # NOT location queries like "find a Tesla supercharger" or "Tesla store near me"
        tesla_vehicle_keywords = ["my tesla", "my model 3", "model 3p", "my car", "car battery", "car charge",
                         "vehicle range", "vampire drain", "phantom drain", "tire pressure", "tpms",
                         "odometer", "miles driven", "charging history", "drive history", "my vehicle",
                         "teslamate", "software update", "mileage", "how many miles", "miles did i",
                         "how far did i drive", "trip meter", "daily miles", "weekly miles", "monthly miles",
                         "distance driven", "efficiency", "tesla battery", "car's battery", "car range",
                         "is my tesla", "is my car", "where is my car", "where is my tesla"]
        # Patterns that indicate location/place searches, NOT vehicle queries
        tesla_place_patterns = ["supercharger", "charging station", "charger near", "closest charger",
                               "nearest charger", "find.*charger", "tesla store", "tesla service",
                               "tesla dealership", "tesla showroom"]
        is_tesla_place_query = any(p in query_lower for p in tesla_place_patterns)
        is_tesla_query = any(kw in query_lower for kw in tesla_vehicle_keywords) and not guest_mode and not is_tesla_place_query

        # Check for media request queries (OWNER MODE ONLY)
        media_keywords = ["request movie", "request show", "request the movie", "request the show",
                         "add movie", "add the movie", "add show", "add the show", "add to plex", "add to jellyfin",
                         "download movie", "download show", "download the movie", "download the show",
                         "want to watch", "my requests", "media requests", "pending requests",
                         "is available on plex", "is available on jellyfin", "in the library", "on plex", "on jellyfin",
                         "overseerr", "request status", "movie request", "show request", "tv request"]
        is_media_query = any(kw in query_lower for kw in media_keywords) and not guest_mode

        if is_media_query:
            # Media request queries need request_media tool (owner mode only)
            relevant_tools = ["request_media"]
            original_count = len(tools)
            tools = [t for t in tools if t["function"]["name"] in relevant_tools]
            logger.info(f"Media request query detected - providing {len(tools)} media request tools")
        elif is_tesla_query:
            # Tesla queries need get_tesla_metrics tool (owner mode only, already filtered above)
            relevant_tools = ["get_tesla_metrics"]
            original_count = len(tools)
            tools = [t for t in tools if t["function"]["name"] in relevant_tools]
            logger.info(f"Tesla query detected - providing {len(tools)} Tesla metric tools")
        elif is_phone_lookup:
            # Phone number lookups for businesses need search_restaurants (if dining) or search_web
            # This ensures we search for business info, not people named "worthington"
            relevant_tools = ["search_restaurants", "search_web", "scrape_website"]
            original_count = len(tools)
            tools = [t for t in tools if t["function"]["name"] in relevant_tools]
            logger.info(f"Phone number lookup query detected - providing {len(tools)} business search tools")
        elif is_website_query:
            # Website scraping queries need scrape_website tool
            relevant_tools = ["scrape_website", "search_web"]
            original_count = len(tools)
            tools = [t for t in tools if t["function"]["name"] in relevant_tools]
            logger.info(f"Website scraping query detected - providing {len(tools)} tools")
        elif is_price_query:
            # Price comparison queries need compare_prices tool
            relevant_tools = ["compare_prices", "search_web"]
            original_count = len(tools)
            tools = [t for t in tools if t["function"]["name"] in relevant_tools]
            logger.info(f"Price comparison query detected - providing {len(tools)} tools")
        elif is_planning_query:
            # Give LLM access to multiple tools for planning queries
            relevant_tools = ["get_weather", "search_events", "search_restaurants"]
            original_count = len(tools)
            tools = [t for t in tools if t["function"]["name"] in relevant_tools]
            logger.info(f"Planning query detected - providing {len(tools)} tools for multi-domain query")
        # Filter tools based on intent
        elif state.intent and state.intent.value in intent_to_tools:
            relevant_tool_names = intent_to_tools[state.intent.value]
            original_count = len(tools)
            tools = [t for t in tools if t["function"]["name"] in relevant_tool_names]
            logger.info(f"Filtered tools from {original_count} to {len(tools)} based on intent '{state.intent.value}'")

        logger.info(f"Tool calling with {len(tools)} available tools (guest_mode={guest_mode})")

        # Build system content with base knowledge context
        system_content = ""
        home_address = DEFAULT_LOCATION  # Permanent home address (for "directions from home")
        search_location = DEFAULT_LOCATION  # Current location for searches (may differ from home)

        # Inject base knowledge context from Admin API
        try:
            admin_client = get_admin_client()
            user_mode = state.mode if state.mode else "guest"
            knowledge_context = await get_knowledge_context_for_user(admin_client, user_mode)
            if knowledge_context:
                system_content = knowledge_context
                logger.info(f"Base knowledge context injected for mode={user_mode} in tool_call")

            # Get permanent home address (for "directions from home" type queries)
            home_address = await get_home_address_for_user(admin_client, user_mode)
            search_location = home_address  # Default search location to home
            logger.info(f"Home address: {home_address}")

            # Check for location from request entities (browser geolocation)
            # This is set when location is passed in the QueryRequest
            if state.entities and state.entities.get("location"):
                entity_location = state.entities["location"]
                # Override SEARCH location but keep home_address unchanged
                search_location = entity_location
                logger.info(f"Search location from entities: {entity_location} (home remains: {home_address})")

            # Check for location override from context (user is somewhere else temporarily)
            # IMPORTANT: This changes the SEARCH location, not the HOME address
            # User saying "use my location" means "search near where I am now"
            # not "my home is now at this new location"
            elif state.context and state.context.get("location_override"):
                loc = state.context["location_override"]
                location_override = None

                if loc.get("use_device_location"):
                    # User requested device/GPS location - check if we have lat/long
                    if loc.get("latitude") and loc.get("longitude"):
                        location_override = f"{loc['latitude']:.4f}, {loc['longitude']:.4f}"
                    else:
                        # No GPS data available - this will need to be handled by frontend
                        logger.info("Device location requested but no GPS coordinates available")
                elif loc.get("address"):
                    location_override = loc["address"]
                elif loc.get("latitude") and loc.get("longitude"):
                    location_override = f"{loc['latitude']:.4f}, {loc['longitude']:.4f}"

                if location_override:
                    # Override SEARCH location, but keep home_address unchanged
                    logger.info(f"Search location override: {location_override} (home remains: {home_address})")
                    search_location = location_override
        except Exception as e:
            logger.warning(f"Failed to fetch base knowledge context in tool_call: {e}")
            # Continue without base knowledge - not critical

        # Inject guest name for personalization (multi-guest support)
        if state.context and state.context.get("guest_name"):
            guest_name = state.context["guest_name"]
            system_content += f"\nYou are speaking with {guest_name}, a guest at this property. "
            system_content += f"Address them by name when appropriate.\n"
            logger.info(f"Guest context injected for tool_call: {guest_name}")

        # Inject memory context for tool selection (e.g., "user's car is a Tesla")
        if state.memory_context:
            system_content += f"\n{state.memory_context}\n"
            logger.info("Memory context injected into tool_call prompt")

        # Append function calling instructions (OPTIMIZED: Reduced verbosity)
        # Use actual home address instead of hardcoded "Baltimore, MD"

        # Add special instructions for planning/itinerary queries
        if is_planning_query:
            from datetime import datetime, timedelta
            today = datetime.now()
            today_str = today.strftime("%A, %B %d, %Y")  # e.g., "Sunday, November 30, 2025"
            today_api = today.strftime("%Y-%m-%d")
            # Calculate next Saturday
            days_until_saturday = (5 - today.weekday()) % 7
            if days_until_saturday == 0:
                days_until_saturday = 7  # If today is Saturday, next Saturday is in 7 days
            next_saturday = today + timedelta(days=days_until_saturday)
            next_saturday_str = next_saturday.strftime("%A, %B %d, %Y")
            tomorrow_str = (today + timedelta(days=1)).strftime("%A, %B %d, %Y")
            tomorrow_api = (today + timedelta(days=1)).strftime("%Y-%m-%d")

            # Extract specific date from query if present
            extracted_date = extract_date_from_query(state.query)
            specific_date_instruction = ""
            if extracted_date:
                date_display, date_api = extracted_date
                specific_date_instruction = f"""
**SPECIFIC DATE DETECTED IN QUERY**: {date_display}
**USE THIS DATE FOR ALL TOOL CALLS**: {date_api}
- When calling search_events, use start_date="{date_api}"
- When calling get_weather, request forecast for {date_display}
"""
                logger.info(f"Extracted date from query: {date_display} ({date_api})")

            system_content += f"""
You are a day planner assistant. When asked to create an itinerary or plan for a day, you MUST call ALL THREE tools:

1. Call get_weather to check weather conditions for the date requested
2. Call search_events to find events/activities for that date - ALWAYS include the start_date parameter
3. Call search_restaurants to find lunch and dinner options near the user's location

You MUST call all three tools - weather, events, AND restaurants. Do not skip any.

After gathering data, create a STRUCTURED ITINERARY with specific time slots:
- Morning (8am-12pm): Activities suited for morning
- Afternoon (12pm-5pm): Lunch and afternoon activities
- Evening (5pm-10pm): Dinner and evening entertainment

Format the itinerary with specific times, venue names, addresses, and weather considerations.
If weather is poor, suggest indoor alternatives.

CRITICAL: NEVER output raw JSON in your response. Your response must always be natural language.
{specific_date_instruction}
IMPORTANT DATE CONTEXT:
- TODAY is: {today_str} (API format: {today_api})
- "tomorrow" = {tomorrow_str} (API format: {tomorrow_api})
- "next Saturday" = {next_saturday_str} (API format: {next_saturday.strftime("%Y-%m-%d")})
- CRITICAL: When calling search_events, you MUST include the start_date parameter in YYYY-MM-DD format
- Example: For December 6th, use start_date="2025-12-06"

User's HOME address: {home_address}
User's CURRENT location: {search_location}
Use CURRENT location for searches and as default origin for directions.
"""
        else:
            system_content += f"""
You are Jarvis, an intelligent AI assistant. You have access to tools for real-time information, but you can also have natural conversations.

WHEN TO USE TOOLS (mandatory):
- Weather, sports scores, flights, news, stocks, restaurants, recipes, events, airports, streaming content
- Any request for current/real-time information
- ALWAYS use the tool even if you're unsure about parameters - the tool will handle it

WHEN TO RESPOND DIRECTLY (no tools needed):
- Creative requests: stories, jokes, poems, songs, riddles
- Casual conversation: greetings, how are you, tell me about yourself
- Factual knowledge you already know: math, conversions, history, science, geography, definitions
  Examples: "how many feet in a mile", "who was the third president", "what's the capital of France"
- General explanations, how things work, concepts, tutorials
- Preferences and recommendations: food, movies, music, hobbies, lifestyle choices
- STAY NEUTRAL on: political opinions, religious views, controversial social topics - be balanced and avoid taking sides

SPORTS QUERIES: When asked about ANY game, score, team, standings, playoffs, or sports result:
- ALWAYS call get_sports_scores or get_sports_standings - NEVER answer from memory
- "Playoff picture", "playoff bracket", "who's in the playoffs" = get_sports_standings
- "Standings", "rankings", "who's leading" = get_sports_standings
- Game scores, upcoming games = get_sports_scores
- Extract the team/league from the query (e.g., "NFL playoff picture" -> league="NFL")
- If unsure about the league, the tool will auto-detect it
- IMPORTANT: Even if the question sounds definitional ("what is the playoff picture"), use tools for CURRENT data

CRITICAL: NEVER output raw JSON in your response text. Do not write tool calls as text like {{"name": "...", "parameters": {{...}}}}.
- Use the proper function calling mechanism, not text
- If the request is unclear or ambiguous, ASK FOR CLARIFICATION - don't give up
- Example: "peruvian spot" -> ask "Are you looking for a Peruvian restaurant?"
- For factual questions where no specific tool applies, use search_web
- Your response must always be natural language, never JSON

LOCATION-BASED QUERIES: When calling search_restaurants or any location tool:
- The user's HOME address is: {home_address}
- The user's CURRENT location is: {search_location}
- If user says "near me", "nearby", "around here" -> use CURRENT location "{search_location}"
- If user says "from home", "close to home", "at home" -> use HOME address "{home_address}"
- If no location specified -> use CURRENT location "{search_location}" as default
- IMPORTANT: For directions, use CURRENT location as the ORIGIN (where user is starting FROM)

DIRECTIONS: When using get_directions:
- ORIGIN should be the user's CURRENT location: {search_location}
- DESTINATION should be where the user wants to go
- If user says "directions to [place]", origin={search_location}, destination=[place]
- If user says "directions from home to [place]", origin={home_address}, destination=[place]"""

        # Build messages for LLM tool calling
        messages = [
            {
                "role": "system",
                "content": system_content
            },
            {
                "role": "user",
                "content": state.query
            }
        ]

        # Add conversation history if available
        if state.history_summary:
            # Use summarized history - prepend to system message
            messages[0]["content"] = f"{messages[0]['content']}\n\n{state.history_summary}"
            logger.info("Using summarized history in tool calling")
        elif state.conversation_history:
            # Insert full history before the current query
            messages = [messages[0]] + state.conversation_history + [messages[1]]

        # FOLLOW-UP CONTEXT: For action references like "search again", "try again", inject previous context
        # This ensures the LLM knows what "again" refers to
        ref_info = state.context_ref_info or {}
        if state.prev_context and ref_info.get("has_context_ref"):
            prev_query = state.prev_context.get("query", "")
            prev_response = state.prev_context.get("response", "")
            if prev_query and prev_response:
                # Inject previous exchange so LLM knows what "again" or "that" refers to
                context_injection = f"""
PREVIOUS CONVERSATION CONTEXT (for follow-up reference):
- User asked: "{prev_query}"
- You responded: "{prev_response[:300]}..."
- Now the user says: "{state.query}"

If the user is asking to repeat, search again, or modify the previous request, use the context above to understand what they want."""
                messages[0]["content"] = f"{messages[0]['content']}\n\n{context_injection}"
                logger.info(f"Injected prev context for follow-up: prev_query='{prev_query[:50]}...'")

        # Get LLM and backend from component config (database-configurable)
        temperature = settings.get("temperature", 0.7)
        max_tokens = 200  # OPTIMIZATION: Balanced limit for tool selection

        # For story/narrative queries on non-voice interfaces, allow much longer responses
        # since these bypass synthesis phase when no tools are needed
        query_lower = state.query.lower()

        # CONTINUATION HANDLING: For "continue" requests, reconstruct the conversation
        # with the previous response so the LLM can continue naturally
        continue_patterns = ["continue where you left off", "please continue", "keep going",
                            "continue the story", "what happens next", "go on"]
        is_continue_query = any(p in query_lower for p in continue_patterns)

        if is_continue_query and state.prev_context:
            prev_query = state.prev_context.get("query", "")
            prev_response = state.prev_context.get("response", "")
            if prev_response:
                # Reconstruct conversation: system + prev_user + prev_assistant + new_user
                # This gives the LLM natural context to continue from
                messages = [
                    messages[0],  # Keep system message as-is
                    {"role": "user", "content": prev_query or "Tell me a story"},
                    {"role": "assistant", "content": prev_response + "..."},  # Mark as incomplete
                    {"role": "user", "content": "Please continue from where you left off. Do not restart or summarize - just continue the story/content directly."}
                ]
                logger.info(f"Restructured messages for continuation ({len(prev_response)} chars prev response)")

        story_patterns = ["tell me a story", "long story", "tell me a long", "write a story",
                         "make up a story", "create a story", "story about", "once upon",
                         "another story", "different story", "new story"]
        is_story_query = any(p in query_lower for p in story_patterns)

        if is_continue_query and getattr(state, 'interface_type', 'voice') != 'voice':
            # Continuation requests always get extended tokens
            max_tokens = 500  # Good length for continuing any truncated response
            logger.info("Continue request detected, using extended max_tokens=500")
        elif is_story_query and getattr(state, 'interface_type', 'voice') != 'voice':
            max_tokens = 2000  # Extended limit for stories on non-voice interfaces
            logger.info("Story query detected in tool_call phase, using extended max_tokens=2000")

        # Select model AND backend based on query complexity
        complexity = state.complexity or "simple"
        if complexity == "simple":
            component_config = await get_component_config("tool_calling_simple")
            llm_model = component_config["model_name"]
            llm_backend = component_config["backend_type"]
            logger.info(f"Using {llm_model} ({llm_backend}) for simple query: {state.query[:50]}")
        elif complexity == "complex":
            component_config = await get_component_config("tool_calling_complex")
            llm_model = component_config["model_name"]
            llm_backend = component_config["backend_type"]
            logger.info(f"Using {llm_model} ({llm_backend}) for complex query: {state.query[:50]}")
        else:  # super_complex
            component_config = await get_component_config("tool_calling_super_complex")
            llm_model = component_config["model_name"]
            llm_backend = component_config["backend_type"]
            logger.info(f"Using {llm_model} ({llm_backend}) for super complex query: {state.query[:50]}")

        logger.info(f"Calling LLM for tool selection: model={llm_model}, backend={llm_backend}, complexity={complexity}")

        # Call LLM with tools
        llm_call_start = time.time()
        llm_response = await llm_router.generate_with_tools(
            model=llm_model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            backend=llm_backend,
            max_tokens=max_tokens,
            request_id=state.request_id,
            session_id=state.session_id,
            intent=state.intent.value if state.intent else None,
            stage="tool_selection"
        )
        llm_call_duration = time.time() - llm_call_start
        timing_breakdown["llm_tool_selection"] = llm_call_duration

        # Track LLM call for metrics
        if state.timing_tracker:
            tokens = llm_response.get("eval_count", 0)
            state.timing_tracker.record_llm_call(
                "tool_selection", llm_model, tokens, int(llm_call_duration * 1000), "tool_calling"
            )

        # Check if LLM wants to call tools
        tool_calls = llm_response.get("tool_calls")

        # VALIDATION: Filter out hallucinated tools that don't exist in our tools list
        if tool_calls:
            valid_tool_names = {t["function"]["name"] for t in tools}
            original_count = len(tool_calls)
            valid_tool_calls = []
            for tc in tool_calls:
                fn_name = tc.get("function", {}).get("name", "")
                if fn_name in valid_tool_names:
                    valid_tool_calls.append(tc)
                else:
                    logger.warning(f"Filtered hallucinated tool call: {fn_name} (not in {valid_tool_names})")

            if len(valid_tool_calls) < original_count:
                logger.info(f"Filtered {original_count - len(valid_tool_calls)} invalid tool calls")
            tool_calls = valid_tool_calls if valid_tool_calls else None

        # FORCED TOOL CALLING: For directions intent, force get_directions tool if LLM bypassed it
        # This prevents the LLM from giving generic directions from training data
        if not tool_calls and state.intent and state.intent.value == "directions":
            # Extract destination from query
            query_lower = state.query.lower()
            dest_patterns = [
                r'(?:directions?\s+to|how\s+(?:do\s+i\s+)?get\s+to|route\s+to|navigate\s+to|drive\s+to|driving\s+to|way\s+to|trip\s+to|going\s+to)\s+(.+?)(?:\?|$|from)',
                r'(?:how\s+far\s+(?:is\s+)?(?:it\s+)?to|how\s+long\s+to\s+(?:get\s+)?to)\s+(.+?)(?:\?|$|from)',
            ]
            destination = None
            for pattern in dest_patterns:
                match = re.search(pattern, query_lower)
                if match:
                    destination = match.group(1).strip()
                    # Clean up common suffixes
                    destination = re.sub(r'\s*(?:from\s+here|from\s+my\s+location|right\s+now|today).*$', '', destination)
                    break

            # Get origin from location_override or default
            origin = None
            if state.context and state.context.get("location_override"):
                loc = state.context["location_override"]
                if loc.get("address"):
                    origin = loc["address"]
                    logger.info(f"Forced directions: using location_override address as origin: {origin}")
                elif loc.get("latitude") and loc.get("longitude"):
                    origin = f"{loc['latitude']:.6f},{loc['longitude']:.6f}"
                    logger.info(f"Forced directions: using location_override coords as origin: {origin}")

            if not origin:
                # Fall back to default location from base_knowledge
                origin = DEFAULT_LOCATION
                logger.info(f"Forced directions: using default location as origin: {origin}")

            if destination and origin:
                # Execute forced directions tool call directly (bypass LLM synthesis to avoid Ollama 400 error)
                logger.info(f"Forcing get_directions tool call: {origin} -> {destination}")

                try:
                    # Call the directions RAG service directly using GET with query params
                    rag_response = await rag_client.get(
                        "directions",
                        "/directions/route",
                        params={"origin": origin, "destination": destination, "mode": "driving"}
                    )

                    if rag_response.success and rag_response.data:
                        # Extract the directions data from the RAGResponse
                        directions_result = rag_response.data

                        # Format the directions response directly
                        distance = directions_result.get("distance", "unknown distance")
                        duration = directions_result.get("duration", "unknown duration")
                        start_addr = directions_result.get("start_address", origin)
                        end_addr = directions_result.get("end_address", destination)

                        # Build response with key directions info
                        response_parts = [
                            f"From {start_addr} to {end_addr}:",
                            f"Distance: {distance}",
                            f"Estimated time: {duration}"
                        ]

                        # Add route summary if available
                        if directions_result.get("summary"):
                            response_parts.append(f"Route: via {directions_result['summary']}")

                        # Add first few steps if available
                        steps = directions_result.get("steps", [])
                        if steps:
                            response_parts.append("\nDirections:")
                            for i, step in enumerate(steps[:5], 1):
                                instruction = step.get("instruction", "").replace("<b>", "").replace("</b>", "")
                                step_dist = step.get("distance", "")
                                if instruction:
                                    response_parts.append(f"{i}. {instruction} ({step_dist})")
                            if len(steps) > 5:
                                response_parts.append(f"... and {len(steps) - 5} more steps")

                        state.answer = "\n".join(response_parts)
                        state.data_source = "Directions RAG (forced)"
                        state.citations.append("Tool: get_directions")
                        state.retrieved_data = {"directions": directions_result}

                        logger.info(f"Forced directions successful: {distance}, {duration}")
                        state.node_timings["tool_call"] = time.time() - start
                        return state
                    else:
                        error_msg = rag_response.error if rag_response else "No response"
                        logger.warning(f"Forced directions failed: {error_msg}")
                        # Fall through to let normal flow handle this

                except Exception as e:
                    logger.error(f"Forced directions execution error: {e}")
                    # Fall through to let normal flow handle this
            else:
                logger.warning(f"Could not extract destination from query: {state.query[:100]}")

        if not tool_calls:
            # LLM didn't want to call any tools, use its direct response
            content = llm_response.get("content", "")
            if content:
                state.answer = content
                state.data_source = f"LLM ({llm_model}) - no tools needed"
                # Capture token metrics from direct LLM response
                state.llm_tokens = llm_response.get("eval_count", 0)
                if state.llm_tokens > 0 and llm_call_duration > 0:
                    state.llm_tokens_per_second = state.llm_tokens / llm_call_duration
                logger.info("LLM provided direct answer without tools")

                # Check if response was truncated due to token limit
                finish_reason = llm_response.get("finish_reason", "stop")
                # Also check if we hit the max_tokens limit (some models don't set finish_reason correctly)
                # This applies to ANY query type, not just stories
                was_truncated = finish_reason == "length" or (state.llm_tokens >= max_tokens - 5)
                if was_truncated:
                    state.was_truncated = True
                    logger.info(f"Direct response was truncated (finish_reason={finish_reason}, tokens={state.llm_tokens}, max_tokens={max_tokens})")
                else:
                    state.was_truncated = False

                # Store conversation context for direct answers (enables follow-up questions)
                try:
                    await store_conversation_context(
                        session_id=state.session_id,
                        intent=state.intent.value if state.intent else "general_info",
                        query=state.query,
                        entities=state.entities or {},
                        parameters={"direct_answer": True, "model": llm_model},
                        response=state.answer or "",  # Store full response for conversation continuity
                        ttl=300  # 5 minute TTL
                    )
                except Exception as ctx_err:
                    logger.warning(f"Failed to store direct answer context: {ctx_err}")
            else:
                # LLM provided nothing - fallback to web search
                logger.warning("LLM provided neither tools nor content - attempting web search fallback")
                try:
                    await _fallback_to_web_search(
                        state,
                        state.intent.value if state.intent else "general",
                        "LLM tool selection failed"
                    )

                    # Web search sets retrieved_data but not answer - synthesize inline
                    if state.retrieved_data and not state.answer:
                        logger.info("Synthesizing answer from web search fallback data")
                        # Build a simple synthesis prompt
                        search_results = state.retrieved_data.get("results", [])
                        if search_results:
                            # Format search results for the LLM
                            context_parts = []
                            for r in search_results[:5]:
                                title = r.get("title", "")
                                snippet = r.get("snippet", "")
                                if title and snippet:
                                    context_parts.append(f"- {title}: {snippet}")

                            context = "\n".join(context_parts)
                            synthesis_prompt = f"""Answer the user's question using the search results below.
Be helpful and conversational. If the information is limited, acknowledge that.

User Question: {state.query}

Search Results:
{context}

Provide a helpful answer:"""

                            try:
                                synthesis_model = await get_model_for_component("response_synthesis")
                                fallback_start = time.time()
                                synthesis_result = await llm_router.generate(
                                    model=synthesis_model,
                                    prompt=synthesis_prompt,
                                    temperature=0.7,
                                    request_id=state.request_id,
                                    session_id=state.session_id,
                                    stage="fallback_synthesis"
                                )
                                fallback_duration = time.time() - fallback_start

                                # Track LLM call for metrics
                                if state.timing_tracker:
                                    tokens = synthesis_result.get("eval_count", 0)
                                    state.timing_tracker.record_llm_call(
                                        "fallback_synthesis", synthesis_model, tokens, int(fallback_duration * 1000), "synthesis"
                                    )

                                state.answer = synthesis_result.get("response", "")
                                state.data_source = f"Web Search Fallback ({state.data_source})"
                                logger.info("Web search fallback synthesis completed")
                            except Exception as synth_err:
                                logger.error(f"Synthesis failed: {synth_err}")
                                # Still provide the snippets as answer
                                state.answer = f"Based on web search: {context_parts[0] if context_parts else 'No relevant results found.'}"
                        else:
                            state.answer = "I couldn't find relevant information. Please try rephrasing your question."

                    if state.answer:
                        logger.info("Web search fallback succeeded after LLM tool selection failure")
                    else:
                        state.error = "Unable to process request - no tools selected and web search fallback failed"
                except Exception as fallback_error:
                    logger.error(f"Web search fallback also failed: {fallback_error}")
                    state.error = "Unable to process request - tool selection and fallback both failed"

            state.node_timings["tool_call"] = time.time() - start
            return state

        # Execute tool calls in parallel
        logger.info(f"Executing {len(tool_calls)} tool calls in parallel")

        max_parallel = settings.get("max_parallel_tools", 3)
        timeout_seconds = settings.get("tool_call_timeout_seconds", 30)

        # Limit parallelism
        tool_calls_limited = tool_calls[:max_parallel]

        # Execute tools with timeout
        # Pass user's location for local search enrichment
        # Priority: 1) explicit location in query, 2) location override from context, 3) default
        user_location = state.entities.get("location") if state.entities else None
        if not user_location:
            # Check for location override in context (user said "use my location" or specified a place)
            if state.context and state.context.get("location_override"):
                loc = state.context["location_override"]
                if loc.get("use_device_location"):
                    # User wants device/GPS location
                    if loc.get("latitude") and loc.get("longitude"):
                        user_location = f"{loc['latitude']:.4f}, {loc['longitude']:.4f}"
                        logger.info(f"Using device GPS location: {user_location}")
                    else:
                        # No GPS available yet - use address if provided, else keep checking
                        user_location = loc.get("address")
                        if not user_location:
                            # Device location requested but not available
                            # This shouldn't block - use default for now
                            logger.info("Device location requested but no coordinates available, using default")
                            user_location = DEFAULT_LOCATION
                elif loc.get("address"):
                    user_location = loc["address"]
                    logger.info(f"Using location override (address): {user_location}")
                elif loc.get("latitude") and loc.get("longitude"):
                    user_location = f"{loc['latitude']:.4f}, {loc['longitude']:.4f}"
                    logger.info(f"Using location override (coordinates): {user_location}")
            if not user_location:
                user_location = DEFAULT_LOCATION

        try:
            tool_exec_start = time.time()
            tool_results = await asyncio.wait_for(
                execute_tools_parallel(tool_calls_limited, guest_mode=guest_mode, location=user_location),
                timeout=timeout_seconds
            )
            timing_breakdown["tool_execution"] = time.time() - tool_exec_start
        except asyncio.TimeoutError:
            logger.error(f"Tool execution timed out after {timeout_seconds}s")
            state.error = f"Tool execution timeout ({timeout_seconds}s)"
            state.node_timings["tool_call"] = time.time() - start
            return state

        # Check tool results for escalation triggers
        try:
            tool_escalation = await check_escalation_triggers(
                state,
                context="tool_result",
                tool_results=tool_results
            )
            if tool_escalation and not getattr(state, '_tool_escalation_retry', False):
                logger.info(f"Tool results triggered escalation to {tool_escalation}")
                state._tool_escalation_retry = True
                state.complexity = tool_escalation
                # Note: Could re-run with better model, but for now just affects synthesis
        except Exception as tool_esc_err:
            logger.warning(f"Tool escalation check failed: {tool_esc_err}")

        # FALLBACK: Check for failed tools and trigger web search fallback
        # This handles cases where RAG services are down (e.g., dining service)
        # Respects per-tool web_search_fallback_enabled setting from admin

        # Build a map of tool_name -> web_search_fallback_enabled from cached tool configs
        tool_fallback_settings = {}
        tool_configs = tool_config_cache.get(cache_key, [])
        for tool_config in tool_configs:
            tool_name = tool_config.get("tool_name")
            if tool_name:
                # Default to True if not specified (backwards compatible)
                tool_fallback_settings[tool_name] = tool_config.get("web_search_fallback_enabled", True)

        logger.debug(f"Tool fallback settings: {tool_fallback_settings}")

        # Track web fallback timing
        web_fallback_start = time.time()
        web_fallback_occurred = False

        failed_tools = []
        for tool_call in tool_calls_limited:
            tool_call_id = tool_call.get("id")
            function_name = tool_call.get("function", {}).get("name", "unknown")
            result = tool_results.get(tool_call_id, {})

            # Check if tool returned an error
            if isinstance(result, dict) and "error" in result:
                failed_tools.append((tool_call_id, function_name, result.get("error", "Unknown error")))
                logger.warning(f"Tool '{function_name}' failed: {result.get('error')}")

        # If any tools failed, try web search fallback (if enabled for that tool)
        if failed_tools:
            logger.info(f"Checking web search fallback for {len(failed_tools)} failed tool(s)")

            for tool_call_id, function_name, error_msg in failed_tools:
                # Check if web search fallback is enabled for this tool
                fallback_enabled = tool_fallback_settings.get(function_name, True)

                if not fallback_enabled:
                    logger.info(f"Web search fallback disabled for '{function_name}', skipping")
                    continue

                # Build a search query based on the original query and tool type
                search_query = enhance_query_with_year(state.query)

                # Add context based on tool type for better search results
                tool_search_context = {
                    "search_restaurants": "restaurants dining recommendations",
                    "search_recipes": "recipes cooking",
                    "get_news": "news headlines",
                    "get_stock_info": "stock price market",
                    "search_events": "events concerts shows",
                    "search_streaming": "streaming watch movies TV",
                    "search_flights": "flights airline"
                }

                if function_name in tool_search_context:
                    # Enhance query with domain context
                    context = tool_search_context[function_name]

                    # Location-sensitive tools need location in query (search providers ignore location param)
                    location_sensitive_tools = ["search_restaurants", "search_events", "get_weather", "search_flights"]
                    user_location = state.entities.get("location", DEFAULT_LOCATION) if state.entities else DEFAULT_LOCATION

                    if function_name in location_sensitive_tools:
                        enhanced_query = f"{search_query} {context} {user_location}"
                    else:
                        enhanced_query = f"{search_query} {context}"

                    logger.info(f"Web search fallback for '{function_name}': '{enhanced_query}'")

                    try:
                        # Use parallel search engine for web search fallback
                        intent, search_results = await parallel_search_engine.search(
                            query=enhanced_query,
                            location=DEFAULT_LOCATION,
                            limit_per_provider=10,
                            force_search=True  # Force web search even for RAG intents
                        )

                        if search_results:
                            # Format web search results for LLM consumption
                            web_data = {
                                "source": "web_search_fallback",
                                "intent": intent,
                                "results": [r.to_dict() for r in search_results[:8]],
                                "query": enhanced_query,
                                "fallback_reason": f"Primary {function_name} service unavailable: {error_msg}",
                                "note": "Data retrieved via web search as fallback"
                            }

                            # Replace error result with web search data
                            tool_results[tool_call_id] = web_data
                            web_fallback_occurred = True
                            logger.info(f"Web search fallback successful for '{function_name}': {len(search_results)} results")

                            # Add citation about fallback
                            state.citations.append(f"Note: {function_name} service unavailable, used web search fallback")
                        else:
                            logger.warning(f"Web search fallback returned no results for '{function_name}'")
                            # Keep original error but add context
                            tool_results[tool_call_id] = {
                                "error": error_msg,
                                "fallback_attempted": True,
                                "fallback_result": "No web search results found",
                                "suggestion": "Please try a different query or check back later"
                            }
                    except Exception as e:
                        logger.error(f"Web search fallback failed for '{function_name}': {e}")
                        # Keep original error
                        tool_results[tool_call_id] = {
                            "error": error_msg,
                            "fallback_attempted": True,
                            "fallback_error": str(e)
                        }

        # ADDITIONAL FALLBACK: Check for empty/irrelevant results (not just errors)
        # This handles cases where a tool "succeeds" but returns no useful data
        for tool_call in tool_calls_limited:
            tool_call_id = tool_call.get("id")
            function_name = tool_call.get("function", {}).get("name", "unknown")
            result = tool_results.get(tool_call_id, {})

            # Skip if already has an error or was already replaced by fallback
            if isinstance(result, dict) and ("error" in result or result.get("source") == "web_search_fallback"):
                continue

            # Check if result is empty/irrelevant
            is_empty = False
            if isinstance(result, dict):
                # First check if result has success=true and an answer (Tesla, etc.)
                # These are considered successful even without a "results" list
                if result.get("success") and result.get("answer"):
                    is_empty = False  # Explicitly mark as not empty
                # Weather service returns {location, current, timestamp} - not empty if has current data
                elif result.get("current") or result.get("forecast"):
                    is_empty = False  # Weather data is present
                # Dining service returns {places: [...]} - not empty if has places
                elif result.get("places"):
                    is_empty = False  # Dining data is present
                # Sports service returns {last_games, upcoming_games, live_games} - not empty if any have data
                elif result.get("last_games") or result.get("upcoming_games") or result.get("live_games"):
                    is_empty = False  # Sports data is present
                # Directions service returns {origin, destination, distance, duration} - not empty if has route data
                elif result.get("origin") and result.get("destination") and (result.get("distance") or result.get("duration")):
                    is_empty = False  # Directions data is present
                else:
                    # Check for common empty result patterns
                    results_list = result.get("results", result.get("flights", result.get("events", [])))
                    message = result.get("message", "").lower()

                    if not results_list and ("no " in message or "couldn't find" in message or "not found" in message):
                        is_empty = True
                    elif isinstance(results_list, list) and len(results_list) == 0:
                        is_empty = True
            elif result is None or result == {}:
                is_empty = True

            if is_empty:
                logger.warning(f"Tool '{function_name}' returned empty/irrelevant results - triggering web search fallback")

                # Check if web search fallback is enabled for this tool
                fallback_enabled = tool_fallback_settings.get(function_name, True)
                if not fallback_enabled:
                    logger.info(f"Web search fallback disabled for '{function_name}', skipping")
                    continue

                try:
                    # Use original query enhanced with tool context
                    tool_search_context = {
                        "search_restaurants": "restaurants dining recommendations",
                        "search_recipes": "recipes cooking",
                        "get_news": "news headlines",
                        "get_stock_info": "stock price market",
                        "search_events": "events concerts shows",
                        "search_streaming": "streaming watch movies TV",
                        "search_flights": "trains flights schedules timetable"  # Added trains
                    }
                    context = tool_search_context.get(function_name, "")

                    # Location-sensitive tools need location in query (search providers ignore location param)
                    location_sensitive_tools = ["search_restaurants", "search_events", "get_weather", "search_flights"]
                    user_location = state.entities.get("location", DEFAULT_LOCATION) if state.entities else DEFAULT_LOCATION

                    if function_name in location_sensitive_tools:
                        # Include location in query for local results
                        enhanced_query = f"{enhance_query_with_year(state.query)} {context} {user_location}"
                    else:
                        enhanced_query = f"{enhance_query_with_year(state.query)} {context}"

                    logger.info(f"Web search fallback for empty '{function_name}' results: '{enhanced_query[:80]}...'")

                    intent, search_results = await parallel_search_engine.search(
                        query=enhanced_query,
                        location=DEFAULT_LOCATION,
                        limit_per_provider=10,
                        force_search=True
                    )

                    if search_results:
                        web_data = {
                            "source": "web_search_fallback",
                            "intent": intent,
                            "results": [r.to_dict() for r in search_results[:8]],
                            "query": enhanced_query,
                            "fallback_reason": f"Primary {function_name} returned no results",
                            "note": "Data retrieved via web search as fallback"
                        }
                        tool_results[tool_call_id] = web_data
                        web_fallback_occurred = True
                        logger.info(f"Web search fallback successful for empty '{function_name}': {len(search_results)} results")
                        state.citations.append(f"Note: {function_name} had no results, used web search")
                    else:
                        logger.warning(f"Web search fallback also returned no results for '{function_name}'")

                except Exception as e:
                    logger.error(f"Web search fallback for empty results failed: {e}")

        # Record web fallback timing (only if fallback occurred)
        if web_fallback_occurred:
            timing_breakdown["web_fallback"] = time.time() - web_fallback_start

        # Build response with tool results
        # Add tool results to messages and ask LLM to synthesize
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": tool_calls
        })

        # Add tool results as separate messages
        for tool_call in tool_calls:
            tool_call_id = tool_call.get("id")
            function_name = tool_call.get("function", {}).get("name", "unknown")
            result = tool_results.get(tool_call_id, {"error": "No result"})

            # Debug log to see exactly what's being passed to synthesis
            if function_name == "search_events":
                events_count = len(result.get("events", []))
                logger.info(f"DEBUG: search_events tool result has {events_count} events")
                if events_count > 0:
                    for i, event in enumerate(result.get("events", [])[:3]):  # Log first 3
                        logger.info(f"DEBUG: Event {i+1}: {event.get('title')} at {event.get('venue')}")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps(result)
            })

        # HYBRID APPROACH: Use quantized 14b for synthesis (quality matters)
        # Tool selection uses 7b (fast), synthesis uses quantized 14b (quality)
        synthesis_model = await get_model_for_component("response_synthesis")
        logger.info(f"Using {synthesis_model} for synthesis (tool selection used {llm_model})")

        # Determine max tokens based on query type
        synthesis_max_tokens = 800  # Default for regular queries (increased from 500 to prevent truncation)

        # For story/narrative queries (web interface), allow longer responses
        # Voice interface will handle this differently via interface_type
        story_patterns = ["tell me a story", "long story", "tell me a long", "write a story",
                         "make up a story", "create a story", "story about", "once upon"]
        is_story_query = any(p in query_lower for p in story_patterns)
        if is_story_query and getattr(state, 'interface_type', 'voice') != 'voice':
            synthesis_max_tokens = 2000  # Allow longer stories for text/web interface
            logger.info("Story query detected on non-voice interface, using extended max_tokens=2000")

        # For planning/itinerary queries, add explicit synthesis instructions and more tokens
        if is_planning_query:
            synthesis_max_tokens = 1500  # More tokens for detailed itineraries
            # Add synthesis instruction to guide the LLM
            # Include the target date in synthesis instruction
            # First try to extract specific date from query
            extracted_date = extract_date_from_query(state.query)
            if extracted_date:
                target_date_str = extracted_date[0]  # Use display format
            elif "saturday" in query_lower:
                target_date_str = next_saturday_str
            elif "tomorrow" in query_lower:
                target_date_str = (today + timedelta(days=1)).strftime("%A, %B %d, %Y")
            else:
                target_date_str = today_str

            # EXTRACT EVENTS FROM TOOL RESULTS TO PREVENT HALLUCINATION
            # Parse the actual event data and inject it directly into the prompt
            extracted_events = []
            for tool_call_id, result in tool_results.items():
                if isinstance(result, dict) and "events" in result:
                    for event in result.get("events", []):
                        event_title = event.get("title", "")
                        event_venue = event.get("venue", "")
                        event_time = event.get("time", "")
                        event_address = event.get("address", "")
                        event_link = event.get("link", "")
                        if event_title and event_venue:
                            extracted_events.append({
                                "title": event_title,
                                "venue": event_venue,
                                "time": event_time,
                                "address": event_address,
                                "link": event_link
                            })

            # Build event section based on extracted data
            if extracted_events:
                events_text = "\n".join([
                    f"- **Concert/Show**: {e['title']} at {e['venue']} at {e['time']}\n  Address: {e['address']}\n  Tickets: {e['link']}"
                    for e in extracted_events[:3]  # Limit to 3 events
                ])
                logger.info(f"Extracted {len(extracted_events)} events for synthesis prompt")
            else:
                events_text = "- No concerts or shows found for this date"

            synthesis_instruction = {
                "role": "user",
                "content": f"""Create a day itinerary for {target_date_str} using the tool results above.

## Weather for {target_date_str}
(Use temperature and conditions from get_weather result)

## Morning (9am-12pm)
- Free time to explore the city

## Afternoon (12pm-5pm)
- Lunch at a restaurant (pick one from search_restaurants results)

## Evening (5pm-10pm)
- Dinner at a restaurant (pick one from search_restaurants results)
{events_text}

IMPORTANT: Use the exact event information provided above. Do NOT change the concert names, venues, or times."""
            }
            messages.append(synthesis_instruction)
            logger.info("Added planning synthesis instruction with pre-extracted events")

        # Call LLM again to synthesize final response
        logger.info("Calling LLM to synthesize final response from tool results")

        synthesis_start_time = time.time()
        final_response = await llm_router.generate_with_tools(
            model=synthesis_model,
            messages=messages,
            tools=None,  # Don't provide tools during synthesis - just generate response
            temperature=temperature,
            backend=llm_backend,
            max_tokens=synthesis_max_tokens,
            request_id=state.request_id,
            session_id=state.session_id,
            intent=state.intent.value if state.intent else None,
            stage="tool_synthesis"
        )
        synthesis_duration = time.time() - synthesis_start_time
        timing_breakdown["response_synthesis"] = synthesis_duration

        # Track LLM call for metrics
        if state.timing_tracker:
            tokens = final_response.get("eval_count", 0)
            state.timing_tracker.record_llm_call(
                "tool_synthesis", synthesis_model, tokens, int(synthesis_duration * 1000), "synthesis"
            )

        # Extract final answer
        state.answer = final_response.get("content", "I couldn't generate a response from the tool results.")

        # Check if response was truncated due to token limit
        finish_reason = final_response.get("finish_reason", "stop")
        was_truncated = finish_reason == "length"
        if was_truncated:
            logger.info(f"Response was truncated (finish_reason=length, max_tokens={synthesis_max_tokens})")
            # Store truncation info in state for response metadata
            state.was_truncated = True
        else:
            state.was_truncated = False

        # Check response for clarification patterns that might trigger escalation
        try:
            response_escalation = await check_escalation_triggers(
                state,
                context="response",
                response_text=state.answer
            )
            if response_escalation and not getattr(state, '_response_escalation_retry', False):
                logger.info(f"Response triggered escalation to {response_escalation}")
                state._response_escalation_retry = True
                state.complexity = response_escalation
                # Note: For this turn, the response is already generated.
                # Escalation will affect the next turn via stored state.
        except Exception as resp_esc_err:
            logger.warning(f"Response escalation check failed: {resp_esc_err}")

        # Capture token metrics for frontend display
        state.llm_tokens = final_response.get("eval_count", 0)
        if state.llm_tokens > 0 and synthesis_duration > 0:
            state.llm_tokens_per_second = state.llm_tokens / synthesis_duration
        else:
            state.llm_tokens_per_second = 0.0
        state.retrieved_data = {
            "tool_calls": [tc.get("function", {}).get("name") for tc in tool_calls],
            "tool_results": tool_results,
            "synthesis_model": synthesis_model
        }
        state.data_source = f"Tool Calling ({llm_backend}/{synthesis_model})"
        state.citations.extend([f"Tool: {tc.get('function', {}).get('name')}" for tc in tool_calls])

        logger.info(f"Tool calling completed with {len(tool_calls)} tool(s)")

        # SMS Integration: Detect textable content in tool_call response
        # Only offer SMS for voice interface when response contains textable info
        if state.interface_type == "voice" and state.answer:
            try:
                should_offer, detected_items, reason = detect_textable_content(state.answer)
                if should_offer and detected_items:
                    state.offer_sms = True
                    state.sms_content_type = detected_items[0].content_type  # Primary content type
                    state.sms_content = extract_sms_content(state.answer, detected_items)
                    logger.info(
                        f"SMS content detected in tool_call: type={state.sms_content_type}, "
                        f"reason='{reason}', offer_sms=True"
                    )
            except Exception as sms_err:
                logger.warning(f"SMS content detection failed in tool_call: {sms_err}")
                # Non-critical - continue without SMS offer

        # Store conversation context for follow-up queries
        # This enables "what about tomorrow?" for weather, "make them brighter" for control, etc.
        if state.session_id and state.answer:
            try:
                # Determine intent for context (use classified intent or derive from tool calls)
                context_intent = state.intent.value if state.intent else "general_info"
                tool_names = [tc.get("function", {}).get("name") for tc in tool_calls]

                # Extract entities based on tools called
                context_entities = {}
                if "get_weather" in tool_names:
                    context_entities["query_type"] = "weather"
                    context_entities["location"] = home_address  # Use the determined home_address
                elif "search_restaurants" in tool_names:
                    context_entities["query_type"] = "dining"
                    context_entities["location"] = home_address
                elif "get_sports_scores" in tool_names:
                    context_entities["query_type"] = "sports"
                elif "search_events" in tool_names:
                    context_entities["query_type"] = "events"
                    context_entities["location"] = home_address

                await store_conversation_context(
                    session_id=state.session_id,
                    intent=context_intent,
                    query=state.query,
                    entities=context_entities,
                    parameters={"tool_calls": tool_names},
                    response=state.answer or "",  # Store full response for conversation continuity
                    ttl=300  # 5 minute TTL
                )
            except Exception as ctx_err:
                logger.warning(f"Failed to store tool call context: {ctx_err}")

    except Exception as e:
        logger.error(f"Tool calling error: {e}", exc_info=True)
        state.error = f"Tool calling failed: {str(e)}"

    tool_call_duration = time.time() - start
    state.node_timings["tool_call"] = tool_call_duration

    # Track tool_call timing in timing tracker
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "tool_call", tool_call_duration)

    # Record detailed breakdown to Prometheus
    for phase, duration in timing_breakdown.items():
        tool_call_breakdown.labels(phase=phase).observe(duration)

    # Log breakdown for debugging
    logger.info(
        "tool_call_breakdown",
        total_duration=f"{tool_call_duration:.2f}s",
        breakdown={k: f"{v:.3f}s" for k, v in timing_breakdown.items()}
    )

    return state


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


async def send_sms_node(state: OrchestratorState) -> OrchestratorState:
    """
    Handle "text me that" requests by sending the previous response via SMS.

    This node:
    1. Gets the previous response from conversation history
    2. Extracts textable content
    3. Queues SMS for sending via admin backend
    """
    from sms.text_me_that import handle_text_me_that

    start = time.time()

    try:
        # Get the previous assistant response from conversation history
        previous_response = None
        for msg in reversed(state.conversation_history):
            if msg.get("role") == "assistant":
                previous_response = msg.get("content", "")
                break

        if not previous_response:
            state.answer = "I don't have a previous message to text you. Could you ask me something first?"
            state.node_timings["send_sms"] = time.time() - start
            return state

        # Get guest's phone number from context
        context = state.context or {}
        phone_number = context.get("phone_number") or context.get("guest_phone")
        calendar_event_id = context.get("calendar_event_id")

        if not phone_number:
            # No phone number available - prompt user for it
            state.answer = (
                "I'd be happy to text that to you! "
                "Could you tell me your phone number? "
                "Just say it like 'four one zero, five five five, one two three four'."
            )
            send_sms_duration = time.time() - start
            state.node_timings["send_sms"] = send_sms_duration
            if state.timing_tracker:
                state.timing_tracker.track_sync("graph", "send_sms", send_sms_duration)
            return state

        # Get SMS service (will be in test mode if Twilio not configured)
        try:
            sms_service = await get_sms_service()
        except Exception as e:
            logger.warning(f"Could not initialize SMS service: {e}")
            sms_service = None

        # Use the text_me_that handler to process and send
        result = await handle_text_me_that(
            query=state.query,
            conversation_history=state.conversation_history,
            guest_phone=phone_number,
            sms_service=sms_service,
            calendar_event_id=calendar_event_id,
        )

        if result.get("success"):
            state.answer = result.get("answer", "Done! I've texted that information to you.")
        elif result.get("needs_phone"):
            state.answer = result.get("answer", "What phone number should I send it to?")
        else:
            state.answer = result.get("answer", "I'm sorry, I couldn't send the text right now. Please try again.")

    except Exception as e:
        logger.error(f"SMS send error: {e}", exc_info=True)
        state.answer = "I'm having trouble sending the text. Please try again later."
        state.error = f"SMS send failed: {str(e)}"

    send_sms_duration = time.time() - start
    state.node_timings["send_sms"] = send_sms_duration
    # Track node time to Prometheus
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "send_sms", send_sms_duration)
    return state


async def notification_pref_node(state: OrchestratorState) -> OrchestratorState:
    """
    Handle notification preference changes via voice commands.

    Examples:
    - "Stop the morning notifications" -> opt-out of morning_greeting
    - "I don't want morning updates" -> opt-out of morning_greeting
    - "Turn morning updates back on" -> opt-in to morning_greeting
    - "Enable notifications" -> opt-in to all
    - "Pause notifications" -> opt-out of all

    Uses the notifications service at NOTIFICATIONS_SERVICE_URL.
    """
    import httpx

    start = time.time()
    query_lower = state.query.lower()

    # Determine if opt-in or opt-out
    opt_out_keywords = ["stop", "disable", "turn off", "don't want", "no more", "pause", "opt out"]
    opt_in_keywords = ["start", "enable", "turn on", "resume", "opt in", "back on", "want"]

    is_opt_out = any(kw in query_lower for kw in opt_out_keywords)
    is_opt_in = any(kw in query_lower for kw in opt_in_keywords)

    # If both or neither, default based on common patterns
    if is_opt_out == is_opt_in:
        # "I want morning updates" vs "I don't want morning updates"
        if "don't" in query_lower or "not" in query_lower:
            is_opt_out = True
            is_opt_in = False
        else:
            # Ambiguous - default to opt-out since most voice requests are to stop something
            is_opt_out = True
            is_opt_in = False

    action = "opt-out" if is_opt_out else "opt-in"

    # Determine which rule(s) are affected
    rule_slugs = []
    if "morning" in query_lower or "greeting" in query_lower:
        rule_slugs.append("morning_greeting")
    if "alert" in query_lower:
        rule_slugs.append("fridge_open_alert")
        rule_slugs.append("door_unlocked_alert")
        rule_slugs.append("tesla_charge_alert")
    if "weather" in query_lower:
        rule_slugs.append("morning_greeting")  # Weather is part of morning greeting

    # If no specific rule identified, assume morning_greeting (most common)
    if not rule_slugs:
        rule_slugs = ["morning_greeting"]

    # Get room from state
    room = state.room or "office"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            results = []

            for rule_slug in rule_slugs:
                endpoint = f"{NOTIFICATIONS_SERVICE_URL}/api/preferences/{action}"
                payload = {
                    "rule_slug": rule_slug,
                    "room": room,
                    "reason": "voice_command"
                }

                logger.info(
                    "notification_pref_request",
                    action=action,
                    rule_slug=rule_slug,
                    room=room,
                    endpoint=endpoint
                )

                response = await client.post(endpoint, json=payload)

                if response.status_code == 200:
                    result = response.json()
                    results.append({
                        "rule": rule_slug,
                        "status": result.get("status"),
                        "success": True
                    })
                elif response.status_code == 404:
                    # Rule not found - might not be configured yet
                    results.append({
                        "rule": rule_slug,
                        "status": "rule_not_found",
                        "success": False
                    })
                else:
                    results.append({
                        "rule": rule_slug,
                        "status": "error",
                        "success": False,
                        "error": response.text
                    })

            # Build response message
            successful = [r for r in results if r["success"]]
            failed = [r for r in results if not r["success"]]

            if action == "opt-out":
                if successful:
                    if "morning_greeting" in [r["rule"] for r in successful]:
                        state.answer = "Okay, I've turned off the morning notifications for this room. Just say 'turn morning updates back on' whenever you'd like them again."
                    else:
                        rule_names = ", ".join([r["rule"].replace("_", " ") for r in successful])
                        state.answer = f"Done, I've disabled notifications for: {rule_names}. Let me know when you want them back."
                elif failed:
                    if any(r.get("status") == "rule_not_found" for r in failed):
                        state.answer = "I couldn't find that notification rule. The proactive notification system may still be setting up."
                    else:
                        state.answer = "I'm having trouble updating your notification preferences right now. Please try again later."
            else:  # opt-in
                if successful:
                    if "morning_greeting" in [r["rule"] for r in successful]:
                        state.answer = "Great, I've turned morning notifications back on for this room. You'll start getting them again tomorrow."
                    else:
                        rule_names = ", ".join([r["rule"].replace("_", " ") for r in successful])
                        state.answer = f"Done, I've re-enabled notifications for: {rule_names}."
                elif failed:
                    if any(r.get("status") == "already_opted_in" for r in failed):
                        state.answer = "You're already receiving those notifications."
                    elif any(r.get("status") == "rule_not_found" for r in failed):
                        state.answer = "I couldn't find that notification rule. The proactive notification system may still be setting up."
                    else:
                        state.answer = "I'm having trouble updating your notification preferences right now. Please try again later."

            logger.info(
                "notification_pref_complete",
                action=action,
                results=results,
                answer=state.answer[:100]
            )

    except httpx.ConnectError:
        logger.warning("notification_service_unreachable", url=NOTIFICATIONS_SERVICE_URL)
        state.answer = "The notification service isn't available right now. Please try again later."
        state.error = "Notifications service unreachable"

    except Exception as e:
        logger.error(f"notification_pref_error: {e}", exc_info=True)
        state.answer = "I had trouble updating your notification preferences. Please try again."
        state.error = f"Notification preference update failed: {str(e)}"

    notif_pref_duration = time.time() - start
    state.node_timings["notification_pref"] = notif_pref_duration
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "notification_pref", notif_pref_duration)

    return state


# ============================================================================
# LangGraph State Machine
# ============================================================================

def create_orchestrator_graph() -> StateGraph:
    """Create the LangGraph state machine."""

    # Initialize graph with state schema
    graph = StateGraph(OrchestratorState)

    # Add nodes
    graph.add_node("classify", classify_node)
    graph.add_node("route_control", route_control_node)
    graph.add_node("route_music", route_music_node)  # Music playback and control
    graph.add_node("route_tv", route_tv_node)  # Apple TV control
    graph.add_node("route_info", route_info_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("synthesize", synthesize_node)
    graph.add_node("validate", validate_node)
    graph.add_node("tool_call", tool_call_node)  # Phase 4: Tool calling node
    graph.add_node("send_sms", send_sms_node)  # SMS Integration: "text me that" handler
    graph.add_node("notification_pref", notification_pref_node)  # Notification opt-out/opt-in
    graph.add_node("finalize", finalize_node)

    # Define edges
    graph.set_entry_point("classify")

    # Conditional routing after classification
    async def route_after_classify(state: OrchestratorState) -> str:
        # DEBUG: Log routing function call
        logger.info(f"route_after_classify called: intent={state.intent.value if state.intent else None}, confidence={state.confidence}")

        # PRIORITY 0: Check for media queries (OWNER MODE ONLY) - must come before control check
        # because "pending requests" can be misclassified as control intent
        query_lower = state.query.lower()
        guest_mode = state.mode == "guest"
        media_keywords = ["request movie", "request show", "request the movie", "request the show",
                         "add movie", "add the movie", "add show", "add the show", "add to plex", "add to jellyfin",
                         "download movie", "download show", "download the movie", "download the show",
                         "want to watch", "my requests", "media requests", "pending requests", "my pending",
                         "is available on plex", "is available on jellyfin", "in the library", "on plex", "on jellyfin",
                         "overseerr", "request status", "movie request", "show request", "tv request"]
        is_media_query = any(kw in query_lower for kw in media_keywords) and not guest_mode
        if is_media_query:
            logger.info("Media query detected - routing to tool_call for request_media")
            return "tool_call"

        # PRIORITY: Handle CONTROL intent FIRST - route to HA control path
        if state.intent == IntentCategory.CONTROL:
            logger.info("Routing to route_control node (Home Assistant)")
            return "route_control"

        # Handle notification preferences (opt-out/opt-in for proactive notifications)
        if state.intent == IntentCategory.NOTIFICATION_PREF:
            logger.info("Routing to notification_pref node")
            return "notification_pref"

        # SMS Integration: Handle TEXT_ME_THAT intent - send SMS with previous response
        if state.intent == IntentCategory.TEXT_ME_THAT:
            logger.info("Routing to send_sms node (SMS Integration)")
            return "send_sms"

        # Music playback and control - route to music handler
        if state.intent in [IntentCategory.MUSIC_PLAY, IntentCategory.MUSIC_CONTROL]:
            logger.info(f"Routing to route_music node ({state.intent.value})")
            return "route_music"

        # Apple TV control - route to TV handler
        if state.intent == IntentCategory.TV_CONTROL:
            logger.info("Routing to route_tv node (TV Control)")
            return "route_tv"

        # Phase 5: Check if tool calling should be triggered after classification
        tool_calling_result = await should_use_tool_calling(state, trigger_context="classify")
        logger.info(f"should_use_tool_calling returned: {tool_calling_result}")

        if tool_calling_result:
            logger.info("Routing to tool_call node")
            return "tool_call"

        # OPTIMIZATION: Route Phase 2 services directly to tool_call
        # These services use tool calling, not the retrieve path
        phase2_intents = {
            IntentCategory.DINING, IntentCategory.RECIPES, IntentCategory.EVENTS,
            IntentCategory.STREAMING, IntentCategory.NEWS, IntentCategory.STOCKS,
            IntentCategory.FLIGHTS, IntentCategory.DIRECTIONS
        }

        if state.intent in phase2_intents:
            logger.info(f"Routing {state.intent.value} to tool_call node (Phase 2 service)")
            return "tool_call"

        if state.intent == IntentCategory.UNKNOWN:
            # Check if this is a continuation response (e.g., "no", "yes", "sure")
            # with conversation history - if so, route to synthesize so LLM can use context
            ref_info = state.context_ref_info or {}
            has_continuation = ref_info.get("is_continuation", False)
            has_history = len(state.conversation_history) > 0

            if has_continuation and has_history:
                logger.info(
                    f"Continuation detected with {len(state.conversation_history)} history messages - "
                    f"routing to synthesize for context-aware response"
                )
                return "synthesize"
            else:
                logger.info("Routing to finalize node (no context available)")
                return "finalize"  # Skip to finalize for unknown intents without context
        else:
            logger.info("Routing to route_info node")
            return "route_info"

    graph.add_conditional_edges(
        "classify",
        route_after_classify,
        {
            "route_control": "route_control",
            "route_music": "route_music",  # Music playback and control
            "route_tv": "route_tv",  # Apple TV control
            "route_info": "route_info",
            "tool_call": "tool_call",
            "finalize": "finalize",
            "synthesize": "synthesize",  # For continuation responses with context
            "send_sms": "send_sms",  # SMS Integration: Handle "text me that" requests
            "notification_pref": "notification_pref"  # Notification preferences (opt-out/opt-in)
        }
    )

    # Control path
    graph.add_edge("route_control", "finalize")

    # Music path
    graph.add_edge("route_music", "finalize")

    # TV control path
    graph.add_edge("route_tv", "finalize")

    # SMS Integration: send_sms goes directly to finalize
    graph.add_edge("send_sms", "finalize")

    # Notification preferences: goes directly to finalize
    graph.add_edge("notification_pref", "finalize")

    # Info path
    graph.add_edge("route_info", "retrieve")

    # Phase 5: Conditional routing after retrieve (check for empty RAG data)
    async def route_after_retrieve(state: OrchestratorState) -> str:
        if await should_use_tool_calling(state, trigger_context="retrieve"):
            return "tool_call"
        return "synthesize"

    graph.add_conditional_edges(
        "retrieve",
        route_after_retrieve,
        {
            "synthesize": "synthesize",
            "tool_call": "tool_call"
        }
    )

    graph.add_edge("synthesize", "validate")

    # Phase 5: Conditional routing after validate (check for validation failure)
    async def route_after_validate(state: OrchestratorState) -> str:
        if await should_use_tool_calling(state, trigger_context="validate"):
            return "tool_call"
        return "finalize"

    graph.add_conditional_edges(
        "validate",
        route_after_validate,
        {
            "finalize": "finalize",
            "tool_call": "tool_call"
        }
    )

    # Tool calling path (Phase 4)
    # Note: Conditional routing to tool_call will be added in Phase 5
    graph.add_edge("tool_call", "finalize")

    # Multi-intent routing after finalize
    def route_after_finalize(state: OrchestratorState) -> str:
        """Route back to classify if more intents need processing."""
        # Check if we have more intents to process by comparing results collected vs total parts
        if state.is_multi_intent and len(state.intent_results) < len(state.intent_parts):
            logger.info(f"Multi-intent: routing back to classify for intent {len(state.intent_results) + 1}/{len(state.intent_parts)}")
            return "classify"
        return END

    graph.add_conditional_edges(
        "finalize",
        route_after_finalize,
        {
            "classify": "classify",
            END: END
        }
    )

    return graph.compile()

# Create global graph instance
orchestrator_graph = None

# ============================================================================
# API Endpoints
# ============================================================================

class QueryRequest(BaseModel):
    """Request model for query endpoint."""
    query: str = Field(..., description="User's query")
    mode: Literal["owner", "guest"] = Field("owner", description="User mode")
    room: str = Field("unknown", description="Room identifier")
    temperature: float = Field(0.7, ge=0, le=2, description="LLM temperature")
    model: Optional[str] = Field(None, description="Preferred model")
    session_id: Optional[str] = Field(None, description="Conversation session ID (optional, will create new if not provided)")
    interface_type: Literal["voice", "text", "chat"] = Field("voice", description="Interface type: 'voice' for TTS (brief), 'text'/'chat' for full details")
    context: Optional[Dict[str, Any]] = Field(None, description="Additional context (phone_number, calendar_event_id, etc.)")
    device_id: Optional[str] = Field(None, description="Device fingerprint for multi-guest user identification")
    location: Optional[str] = Field(None, description="User's current location (from browser geolocation or device)")
    interruption_context: Optional[Dict[str, Any]] = Field(None, description="Context when user interrupted previous response (previous_query, interrupted_response, audio_position_ms)")

class QueryResponse(BaseModel):
    """Response model for query endpoint."""
    answer: str = Field(..., description="Generated response")
    intent: str = Field(..., description="Detected intent category")
    confidence: float = Field(..., description="Classification confidence")
    citations: List[str] = Field(default_factory=list, description="Data sources")
    request_id: str = Field(..., description="Request tracking ID")
    session_id: str = Field(..., description="Conversation session ID")
    processing_time: float = Field(..., description="Total processing time")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    # Granular execution timing
    timings: Optional[Dict[str, Any]] = Field(None, description="Hierarchical execution timing breakdown")
    # SMS Integration
    offer_sms: bool = Field(False, description="Whether SMS can be offered for this response")
    sms_content: Optional[str] = Field(None, description="Content to send via SMS if offered")
    sms_content_type: Optional[str] = Field(None, description="Type of detected SMS content")

@app.post("/query", response_model=QueryResponse)
async def process_query(request: QueryRequest) -> QueryResponse:
    """
    Process a user query through the orchestrator state machine.
    """
    global orchestrator_graph

    # Initialize graph if needed
    if orchestrator_graph is None:
        orchestrator_graph = create_orchestrator_graph()

    # Track request
    request_counter.labels(intent="unknown", status="started").inc()

    # Initialize timing tracker for granular execution time tracking
    timing_tracker = TimingTracker()

    try:
        # Multi-guest identification: Look up user by device fingerprint
        with timing_tracker.track("pre_graph", "guest_identification"):
            guest_info = None
            user_id = request.mode  # Default: use mode as user_id

            if request.device_id:
                admin_client = get_admin_client()
                guest_info = await admin_client.get_user_session_by_device(request.device_id)
                if guest_info:
                    # Use guest-specific user_id for session tracking
                    user_id = f"guest:{guest_info.get('guest_id')}"
                    logger.info(
                        "multi_guest_identified",
                        guest_id=guest_info.get("guest_id"),
                        guest_name=guest_info.get("guest_name"),
                        device_id=request.device_id[:16] + "..." if len(request.device_id) > 16 else request.device_id
                    )

        # Session management: get or create session
        with timing_tracker.track("pre_graph", "session_management"):
            logger.info(
                "session_request_received",
                request_session_id=request.session_id,
                user_id=user_id,
                zone=request.room
            )
            session = await session_manager.get_or_create_session(
                session_id=request.session_id,
                user_id=user_id,
                zone=request.room
            )

        logger.info(f"Processing query in session {session.session_id}")

        # Phase 2: Get current mode and permissions (Guest Mode)
        with timing_tracker.track("pre_graph", "mode_determination"):
            # If we identified a guest via device fingerprint, use guest mode
            # Otherwise check mode service or use request mode
            mode_info = await get_current_mode()
            if guest_info:
                # Device-identified guest: always use guest mode
                current_mode = "guest"
            else:
                current_mode = request.mode if request.mode else mode_info.get("mode", "owner")
            permissions = mode_info.get("permissions", {})

            logger.info(
                "request_mode_determined",
                mode=current_mode,
                override_active=mode_info.get("override_active", False),
                reason=mode_info.get("reason", "Unknown")
            )

        # Phase 4: Voice PIN Override - Detect and handle owner mode commands
        with timing_tracker.track("pre_graph", "pin_override_check"):
            if detect_owner_mode_command(request.query):
                logger.info(
                    "owner_mode_command_detected",
                    query=request.query[:50],
                    current_mode=current_mode
                )

                # Extract PIN if provided
                pin = extract_pin_from_query(request.query)

                # Attempt to activate owner override
                success, message, override_data = await activate_owner_override(
                    pin=pin,
                    voice_device_id=request.room,  # Use room as device identifier
                    timeout_minutes=None  # Use default from config
                )

                # Return early with owner mode response
                request_id = hashlib.md5(f"{request.query}{time.time()}".encode()).hexdigest()[:8]
                return QueryResponse(
                    answer=message,
                    intent="mode_override",
                    confidence=1.0,
                    citations=[],
                    request_id=request_id,
                    session_id=session.session_id,
                    processing_time=time.time() - timing_tracker.start_time if hasattr(timing_tracker, 'start_time') else 0.1,
                    metadata={
                        "success": success,
                        "mode": "owner" if success else current_mode,
                        "pin_provided": pin is not None,
                        "override_data": override_data
                    }
                )

        # Get conversation history for LLM context
        async with timing_tracker.track_async("pre_graph", "history_loading"):
            config = await get_config()
            conv_settings = await config.get_conversation_settings()

            # Only load history if conversation context is enabled
            conversation_history = []
            history_summary = ""

            if conv_settings.get("enabled", True) and conv_settings.get("use_context", True):
                history_mode = conv_settings.get("history_mode", "full")

                if history_mode == "none":
                    # No history - fastest mode
                    logger.info("History mode: none - skipping conversation history")

                elif history_mode == "summarized":
                    # Summarized history - balanced mode
                    precompute_enabled = await get_feature_flag("ha_precomputed_summaries", default=False)

                    if precompute_enabled:
                        # Try to use precomputed summary from session
                        precomputed = await get_session_summary(session.session_id)
                        if precomputed:
                            history_summary = precomputed
                            logger.info("History mode: summarized - using precomputed summary")
                        else:
                            # No precomputed summary, compute fresh and store it
                            max_history = conv_settings.get("max_llm_history_messages", 10)
                            raw_history = session.get_llm_history(max_history)

                            if raw_history:
                                history_summary = await summarize_conversation_history(
                                    raw_history,
                                    request.query,
                                    request_id=hashlib.md5(f"{request.query}{time.time()}".encode()).hexdigest()[:8]
                                )
                                # Store for future use
                                await update_session_summary(session.session_id, history_summary)
                                logger.info(f"History mode: summarized - computed and cached ({len(raw_history)} messages)")
                    else:
                        # Original behavior - compute fresh summary
                        max_history = conv_settings.get("max_llm_history_messages", 10)
                        raw_history = session.get_llm_history(max_history)

                        if raw_history:
                            history_summary = await summarize_conversation_history(
                                raw_history,
                                request.query,
                                request_id=hashlib.md5(f"{request.query}{time.time()}".encode()).hexdigest()[:8]
                            )
                            logger.info(f"History mode: summarized - compressed {len(raw_history)} messages")

                else:  # "full" mode (default)
                    # Full history - current behavior
                    max_history = conv_settings.get("max_llm_history_messages", 10)
                    conversation_history = session.get_llm_history(max_history)
                    logger.info(f"History mode: full - loaded {len(conversation_history)} previous messages")

        # Retrieve relevant memories from Qdrant for context augmentation
        memory_context = ""
        async with timing_tracker.track_async("pre_graph", "memory_retrieval"):
            try:
                logger.info("memory_retrieval_starting", query_preview=request.query[:50], mode=current_mode)
                memory_manager = await get_memory_manager()
                guest_session_id = None
                if guest_info:
                    # Try to get guest session ID from active session
                    active_session = await memory_manager.get_active_guest_session()
                    if active_session:
                        guest_session_id = active_session.get("id")

                memories = await memory_manager.get_relevant_memories(
                    query=request.query,
                    mode=current_mode,
                    guest_session_id=guest_session_id,
                    limit=3
                )
                logger.info("memory_retrieval_completed", found_count=len(memories) if memories else 0)

                if memories:
                    memory_context = memory_manager.format_memory_context(memories)
                    logger.info(
                        "memories_retrieved_for_context",
                        count=len(memories),
                        mode=current_mode,
                        memory_preview=memory_context[:100] if memory_context else ""
                    )
                else:
                    logger.info("memory_retrieval_empty", mode=current_mode)
            except Exception as e:
                logger.warning("memory_retrieval_skipped", error=str(e), error_type=type(e).__name__)

        # Build context with guest info (if identified via device fingerprint)
        query_context = dict(request.context) if request.context else {}
        if guest_info:
            query_context["guest_id"] = guest_info.get("guest_id")
            query_context["guest_name"] = guest_info.get("guest_name")
            query_context["device_type"] = guest_info.get("device_type", "web")
            query_context["guest_preferences"] = guest_info.get("preferences", {})

        # Create initial state with conversation history, mode, and permissions
        # Initialize entities with location if provided in request
        initial_entities = {}
        if request.location:
            initial_entities["location"] = request.location
            logger.info(f"Using location from request: {request.location}")

        initial_state = OrchestratorState(
            query=request.query,
            mode=current_mode,  # Use detected mode instead of request mode
            room=request.room,
            temperature=request.temperature,
            session_id=session.session_id,
            conversation_history=conversation_history,
            history_summary=history_summary,  # Summarized context for summarized mode
            permissions=permissions,  # Phase 2: Include permissions for entity checks
            interface_type=request.interface_type,  # SMS Integration: Pass interface type for response formatting
            context=query_context,  # SMS Integration + Multi-guest: Pass context (phone_number, calendar_event_id, guest_name, etc.)
            memory_context=memory_context,  # Memory augmentation: Relevant memories for LLM context
            timing_tracker=timing_tracker,  # Granular execution time tracking
            entities=initial_entities,  # Include location from request
            interruption_context=request.interruption_context  # Barge-in: Pass interruption context for natural acknowledgment
        )

        # Emit session start event for Admin Jarvis monitoring
        if EVENTS_AVAILABLE:
            await emit_session_start(
                session_id=session.session_id,
                interface=request.interface_type or "unknown",
                metadata={
                    "query_preview": request.query[:50],
                    "mode": current_mode,
                    "room": request.room
                }
            )

        # SEMANTIC QUERY CACHING: Check for cached response before expensive processing
        # Include location_override in cache key for location-sensitive queries (directions, dining)
        async with timing_tracker.track_async("pre_graph", "semantic_cache_check"):
            location_override = query_context.get("location_override") if query_context else None

            # Round 17 FIX: Check for strong intent BEFORE cache lookup
            # If a strong intent is detected (e.g., food keywords), we should not use
            # cached responses from a different intent (e.g., streaming) even if embeddings match
            strong_intent_result = detect_strong_intent(request.query)
            detected_strong_intent = strong_intent_result.get("detected_intent") if strong_intent_result.get("has_strong_intent") else None

            cached_response = await get_cached_response(
                query=request.query,
                room=request.room,
                mode=current_mode,
                location_override=location_override
            )

            # Skip cache if strong intent doesn't match cached intent
            if cached_response and detected_strong_intent:
                cached_intent = cached_response.get("intent", "")
                if cached_intent and cached_intent != detected_strong_intent:
                    logger.info(
                        "semantic_cache_skipped_intent_mismatch",
                        query_preview=request.query[:50],
                        detected_intent=detected_strong_intent,
                        cached_intent=cached_intent,
                        reason="Strong intent override"
                    )
                    cached_response = None  # Skip cache, process query normally

            # Round 17: Skip cache for far-future weather queries that need limitation response
            if cached_response and cached_response.get("intent") == "weather":
                query_lower = request.query.lower()
                far_future_patterns = [
                    r'\b(\d+)\s*weeks?\b',  # "3 weeks", "in 2 weeks"
                    r'\bnext\s+month\b', r'\bin\s+a\s+month\b',  # "next month"
                ]
                for pattern in far_future_patterns:
                    match = re.search(pattern, query_lower)
                    if match:
                        if 'week' in pattern:
                            num = int(match.group(1)) if match.lastindex else 1
                            if num >= 2:
                                logger.info(
                                    "semantic_cache_skipped_far_future_weather",
                                    query_preview=request.query[:50],
                                    reason=f"Far future weather request ({num} weeks)"
                                )
                                cached_response = None
                                break
                        elif 'month' in pattern:
                            logger.info(
                                "semantic_cache_skipped_far_future_weather",
                                query_preview=request.query[:50],
                                reason="Far future weather request (month)"
                            )
                            cached_response = None
                            break

            if cached_response:
                # Cache hit - return cached response immediately
                logger.info(
                    "semantic_cache_hit_returned",
                    query_preview=request.query[:50],
                    cached_intent=cached_response.get("intent"),
                    cache_category=cached_response.get("_cache_metadata", {}).get("category")
                )

                # Track cache hit metrics
                request_counter.labels(intent=cached_response.get("intent", "unknown"), status="cache_hit").inc()

                # Add messages to session even for cached responses
                session.add_message(role="user", content=request.query, metadata={"cached": True})
                session.add_message(role="assistant", content=cached_response.get("answer", ""))

                # Store conversation context for follow-up queries even on cache hit
                # This enables pronoun resolution like "what team does he play for"
                try:
                    cached_intent = cached_response.get("intent", "general_info")
                    cached_answer = cached_response.get("answer", "")
                    await store_conversation_context(
                        session_id=session.session_id,
                        intent=cached_intent,
                        query=request.query,
                        entities={},  # No entities extracted for cached responses
                        parameters={},
                        response=cached_answer,  # Store full response for conversation continuity
                        ttl=300  # 5 minute TTL
                    )
                    logger.info(f"Stored context for cached response, session {session.session_id[:8]}..., intent={cached_intent}")
                except Exception as e:
                    logger.warning(f"Failed to store context for cached response: {e}")

                # Return cached response (remove internal cache metadata)
                response_data = {k: v for k, v in cached_response.items() if not k.startswith("_cache")}
                response_data["session_id"] = session.session_id  # Update session ID
                response_data["metadata"] = {
                    **(response_data.get("metadata") or {}),
                    "cached": True,
                    "cache_hit": True,
                    "tool_exec_time": 0  # No tool execution for cached responses
                }

                return QueryResponse(**response_data)

        # Check for tool creation intent BEFORE running the state machine
        if detect_tool_creation_intent(request.query):
            logger.info("tool_creation_intent_detected", query=request.query[:50])
            tool_result = await handle_tool_creation_request(
                query=request.query,
                session_id=session.session_id,
                user_mode=current_mode
            )

            if tool_result is not None:
                # Tool creation was handled (either success or error)
                request_counter.labels(intent="tool_creation", status="success" if tool_result.get("success") else "error").inc()

                # Add to session history
                session.add_message(role="user", content=request.query, metadata={"intent": "tool_creation"})
                session.add_message(role="assistant", content=tool_result["answer"])

                # Generate a request_id for this tool creation request
                tool_request_id = hashlib.md5(f"tool_{time.time()}".encode()).hexdigest()[:8]

                return QueryResponse(
                    answer=tool_result["answer"],
                    intent="tool_creation",
                    confidence=1.0,
                    citations=[],
                    request_id=tool_request_id,
                    session_id=session.session_id,
                    processing_time=time.time() - initial_state.start_time,
                    metadata={
                        "model_used": "llama3.1:8b",
                        "tool_proposal": tool_result.get("success", False),
                        "proposal_id": tool_result.get("proposal_id"),
                        "tool_name": tool_result.get("tool_name")
                    }
                )
            # If tool_result is None, feature is disabled - continue with normal flow

        # Check for memory forget intent BEFORE running the state machine
        try:
            memory_manager = await get_memory_manager()
            if memory_manager.should_forget_memory(request.query):
                logger.info("memory_forget_intent_detected", query=request.query[:50])

                # Extract what to forget
                forget_content = memory_manager.extract_forget_content(request.query)

                if forget_content:
                    # Delete matching memories
                    result = await memory_manager.delete_memory_by_content(
                        search_query=forget_content,
                        mode=current_mode
                    )

                    deleted_count = result.get("deleted", 0)

                    if deleted_count > 0:
                        answer = f"Done! I've forgotten {deleted_count} memory{'s' if deleted_count > 1 else ''} about {forget_content}."
                    else:
                        answer = f"I don't have any memories about {forget_content} to forget."

                    # Track metrics
                    request_counter.labels(intent="memory_forget", status="success").inc()

                    # Add to session history
                    session.add_message(role="user", content=request.query, metadata={"intent": "memory_forget"})
                    session.add_message(role="assistant", content=answer)

                    # Generate request_id
                    forget_request_id = hashlib.md5(f"forget_{time.time()}".encode()).hexdigest()[:8]

                    return QueryResponse(
                        answer=answer,
                        intent="memory_forget",
                        confidence=1.0,
                        citations=[],
                        request_id=forget_request_id,
                        session_id=session.session_id,
                        processing_time=time.time() - initial_state.start_time,
                        metadata={
                            "deleted_count": deleted_count,
                            "search_query": forget_content,
                            "mode": current_mode
                        }
                    )
        except Exception as e:
            logger.warning("memory_forget_check_failed", error=str(e))

        # Run through state machine
        tool_exec_start = time.time()
        with request_duration.labels(intent="processing").time():
            final_state = await orchestrator_graph.ainvoke(initial_state)
        tool_exec_time = time.time() - tool_exec_start

        # Phase 2: Check intent permission AFTER classification
        permission_check_start = time.time()
        intent = final_state.get("intent")
        if intent and not check_intent_permission(intent, permissions):
            logger.warning(
                "intent_blocked_by_guest_mode",
                intent=intent.value if hasattr(intent, "value") else intent,
                mode=current_mode
            )
            # Return permission denied response
            return QueryResponse(
                answer="I'm sorry, that feature is not available in guest mode.",
                intent=intent.value if hasattr(intent, "value") else str(intent),
                confidence=1.0,
                session_id=session.session_id,
                model_used="permission_check",
                reasoning_path=["Permission check: Intent blocked in guest mode"],
                node_timings=final_state.get("node_timings", {}),
                total_time=time.time() - initial_state.start_time
            )

        # Track permission check timing
        if timing_tracker:
            timing_tracker.track_sync("post_graph", "permission_check", time.time() - permission_check_start)

        # Track metrics
        intent_value = final_state.get("intent")
        if intent_value and hasattr(intent_value, "value"):
            intent_str = intent_value.value
        elif isinstance(intent_value, str):
            intent_str = intent_value
        else:
            intent_str = "unknown"

        request_counter.labels(
            intent=intent_str,
            status="success"
        ).inc()

        # Track intent analytics to database for service gap analysis
        try:
            # Determine what the intent maps to in our system
            rag_mapping = {
                "weather": "weather",
                "sports": "sports",
                "airports": "airports",
                "flights": "flights",
                "events": "events",
                "streaming": "streaming",
                "news": "news",
                "stocks": "stocks",
                "websearch": "websearch",
                "dining": "dining",
                "recipes": "recipes"
            }

            system_mapping = rag_mapping.get(intent_str.lower(), "general")
            has_rag_service = intent_str.lower() in rag_mapping

            # Log to conversation_analytics (fire-and-forget to avoid blocking)
            config = await get_config()
            asyncio.create_task(config.log_analytics_event(
                session_id=session.session_id,
                event_type="query_intent",
                metadata={
                    "intent": intent_str,
                    "query": request.query,
                    "confidence": final_state.get("confidence"),
                    "has_rag_service": has_rag_service,
                    "system_mapping": system_mapping,
                    "user_id": current_mode,
                    "room": request.room
                }
            ))
        except Exception as e:
            # Don't fail request if analytics logging fails
            logger.warning(f"Intent analytics logging failed: {e}")

        # Add messages to session history
        session_update_start = time.time()
        answer = final_state.get("answer") or "I couldn't process that request."

        # Extract model_tier for session metadata
        model_tier = final_state.get("model_tier")

        # Add user message to session
        session.add_message(
            role="user",
            content=request.query,
            metadata={
                "intent": intent_str,
                "confidence": final_state.get("confidence"),
                "room": request.room
            }
        )

        # Add assistant response to session
        session.add_message(
            role="assistant",
            content=answer,
            metadata={
                "model_tier": model_tier.value if model_tier and hasattr(model_tier, "value") else str(model_tier),
                "data_source": final_state.get("data_source"),
                "validation_passed": final_state.get("validation_passed")
            }
        )

        # Save session (with trimming based on config)
        await session_manager.add_message(
            session_id=session.session_id,
            role="user",
            content=request.query,
            metadata={"intent": intent_str, "confidence": final_state.get("confidence")}
        )
        await session_manager.add_message(
            session_id=session.session_id,
            role="assistant",
            content=answer,
            metadata={"model_tier": model_tier.value if model_tier and hasattr(model_tier, "value") else str(model_tier)}
        )

        logger.info(f"Session {session.session_id} updated with {len(session.messages)} total messages")

        # Track session update timing
        if timing_tracker:
            timing_tracker.track_sync("post_graph", "session_update", time.time() - session_update_start)

        # Memory creation: Check if this conversation should create a memory
        memory_creation_start = time.time()
        try:
            memory_manager = await get_memory_manager()
            if memory_manager.should_create_memory(request.query, answer, intent_str):
                # Extract memorable content and calculate importance
                memorable_content = memory_manager.extract_memorable_fact(request.query, answer, intent_str)
                importance = memory_manager.calculate_importance(request.query, answer, intent_str)
                category = memory_manager.classify_memory_category(request.query, answer, intent_str)

                # Get guest session ID if applicable
                guest_session_id = None
                if current_mode == "guest" and guest_info:
                    active_session = await memory_manager.get_active_guest_session()
                    if active_session:
                        guest_session_id = active_session.get("id")

                # Create memory asynchronously (fire-and-forget)
                asyncio.create_task(memory_manager.create_memory(
                    content=memorable_content,
                    mode=current_mode,
                    guest_session_id=guest_session_id,
                    category=category,
                    importance=importance,
                    source_query=request.query
                ))

                logger.info(
                    "memory_creation_triggered",
                    category=category,
                    importance=importance,
                    mode=current_mode
                )
        except Exception as e:
            logger.warning("memory_creation_skipped", error=str(e))

        # Track memory creation timing
        if timing_tracker:
            timing_tracker.track_sync("post_graph", "memory_creation", time.time() - memory_creation_start)

        # Build response
        model_tier_str = model_tier.value if model_tier and hasattr(model_tier, "value") else model_tier
        total_time_ms = int((time.time() - final_state.get("start_time", time.time())) * 1000)

        # Finalize timing data and record to Prometheus
        final_timings = None
        if timing_tracker:
            final_timings = timing_tracker.finalize()
            try:
                record_timing_metrics(final_timings)
            except Exception as metrics_err:
                logger.warning("timing_metrics_error", error=str(metrics_err))

        # Emit response ready and session end events for Admin Jarvis monitoring
        if EVENTS_AVAILABLE:
            await emit_response_ready(
                session_id=session.session_id,
                response_text=answer,
                total_duration_ms=total_time_ms,
                interface=request.interface_type
            )
            await emit_session_end(
                session_id=session.session_id,
                interface=request.interface_type,
                success=True
            )

        # Normalize text for TTS (expand abbreviations for voice output)
        if request.interface_type == "voice":
            answer = normalize_for_tts(answer)

        # Build the response object
        # Include browser_playback in metadata if present (for Jarvis Web music playback)
        response_metadata = {
            "model_used": model_tier_str,
            "data_source": final_state.get("data_source"),
            "validation_passed": final_state.get("validation_passed"),
            "node_timings": final_state.get("node_timings"),
            "conversation_turns": len(session.messages) // 2,
            "tokens": final_state.get("llm_tokens", 0),
            "tokens_per_second": final_state.get("llm_tokens_per_second", 0.0),
            "tool_exec_time": tool_exec_time,
            "was_truncated": final_state.get("was_truncated", False)
        }

        # Add browser_playback and music_intent from retrieved_data if present
        retrieved_data = final_state.get("retrieved_data", {})
        if isinstance(retrieved_data, dict):
            if "browser_playback" in retrieved_data:
                response_metadata["browser_playback"] = retrieved_data["browser_playback"]
            if "music_intent" in retrieved_data:
                response_metadata["music_intent"] = retrieved_data["music_intent"]

        response = QueryResponse(
            answer=answer,
            intent=intent_str if intent_str != "unknown" else IntentCategory.UNKNOWN.value,
            confidence=final_state.get("confidence"),
            citations=final_state.get("citations"),
            request_id=final_state.get("request_id"),
            session_id=session.session_id,
            processing_time=time.time() - final_state.get("start_time", time.time()),
            metadata=response_metadata,
            # Granular execution timing (recorded to Prometheus)
            timings=final_timings,
            # SMS Integration
            offer_sms=final_state.get("offer_sms", False),
            sms_content=final_state.get("sms_content"),
            sms_content_type=final_state.get("sms_content_type")
        )

        # SEMANTIC QUERY CACHING: Store successful response for future cache hits
        # Cache asynchronously (fire-and-forget) to avoid adding latency
        # Include location_override in cache key for location-sensitive queries
        try:
            # Convert response to dict for caching
            response_dict = response.model_dump() if hasattr(response, 'model_dump') else response.dict()
            cache_location_override = query_context.get("location_override") if query_context else None
            asyncio.create_task(
                cache_response(
                    query=request.query,
                    response=response_dict,
                    room=request.room,
                    mode=current_mode,
                    location_override=cache_location_override
                )
            )
        except Exception as cache_err:
            logger.warning("semantic_cache_store_failed", error=str(cache_err))

        return response

    except Exception as e:
        logger.error(f"Query processing error: {e}", exc_info=True)
        request_counter.labels(intent="unknown", status="error").inc()

        # Emit session end event for error case
        if EVENTS_AVAILABLE and 'session' in locals():
            await emit_session_end(
                session_id=session.session_id,
                interface=request.interface_type if 'request' in locals() else None,
                success=False,
                error=str(e)
            )

        raise HTTPException(
            status_code=500,
            detail=f"Failed to process query: {str(e)}"
        )

@app.post("/query/stream")
async def process_query_stream(request: QueryRequest):
    """
    Process a user query with TRUE streaming response (Server-Sent Events).

    Streams LLM tokens as they are generated, providing much faster time-to-first-token
    compared to fake streaming that waits for full response.

    Returns results progressively:
    - Stage 1: Searching status (intent classification + RAG lookup)
    - Stage 2: Tool execution results
    - Stage 3: Final answer (TRUE streaming - tokens as generated)
    """
    async def event_generator():
        try:
            start_time = time.time()

            # Stage 1: Send initial status
            yield f"data: {json.dumps({'stage': 'searching', 'message': 'Analyzing your request...'})}\n\n"

            # Run orchestrator (tool calling only, no synthesis yet)
            global orchestrator_graph
            if orchestrator_graph is None:
                orchestrator_graph = create_orchestrator_graph()

            # Multi-guest identification: Look up user by device fingerprint
            guest_info = None
            user_id = request.mode  # Default: use mode as user_id

            if request.device_id:
                admin_client = get_admin_client()
                guest_info = await admin_client.get_user_session_by_device(request.device_id)
                if guest_info:
                    user_id = f"guest:{guest_info.get('guest_id')}"
                    logger.info(
                        "multi_guest_identified_stream",
                        guest_id=guest_info.get("guest_id"),
                        guest_name=guest_info.get("guest_name")
                    )

            # Session management
            session = await session_manager.get_or_create_session(
                session_id=request.session_id,
                user_id=user_id,
                zone=request.room
            )

            # Get mode and permissions
            mode_info = await get_current_mode()
            if guest_info:
                current_mode = "guest"  # Device-identified guest
            else:
                current_mode = mode_info.get("mode", "owner")

            # Get conversation history
            config = await get_config()
            conv_settings = await config.get_conversation_settings()
            conversation_history = []
            history_summary = ""

            if conv_settings.get("enabled", False):
                history_mode = conv_settings.get("history_mode", "full")

                if history_mode == "none":
                    logger.info("History mode: none - skipping conversation history")
                elif history_mode == "summarized":
                    precompute_enabled = await get_feature_flag("ha_precomputed_summaries", default=False)

                    if precompute_enabled:
                        # Try precomputed summary first
                        precomputed = await get_session_summary(session.session_id)
                        if precomputed:
                            history_summary = precomputed
                            logger.info("History mode: summarized - using precomputed summary")
                        else:
                            max_history = conv_settings.get("max_llm_history_messages", 10)
                            raw_history = session.get_llm_history(max_history)
                            if raw_history:
                                history_summary = await summarize_conversation_history(
                                    raw_history,
                                    request.query,
                                    request_id=hashlib.md5(f"{request.query}{time.time()}".encode()).hexdigest()[:8]
                                )
                                await update_session_summary(session.session_id, history_summary)
                                logger.info(f"History mode: summarized - computed and cached ({len(raw_history)} messages)")
                    else:
                        max_history = conv_settings.get("max_llm_history_messages", 10)
                        raw_history = session.get_llm_history(max_history)
                        if raw_history:
                            history_summary = await summarize_conversation_history(
                                raw_history,
                                request.query,
                                request_id=hashlib.md5(f"{request.query}{time.time()}".encode()).hexdigest()[:8]
                            )
                            logger.info(f"History mode: summarized - compressed {len(raw_history)} messages")
                else:  # "full" mode
                    max_history = conv_settings.get("max_llm_history_messages", 10)
                    conversation_history = session.get_llm_history(max_history)
                    logger.info(f"History mode: full - loaded {len(conversation_history)} previous messages")

            # Build context with guest info (if identified via device fingerprint)
            query_context = dict(request.context) if request.context else {}
            if guest_info:
                query_context["guest_id"] = guest_info.get("guest_id")
                query_context["guest_name"] = guest_info.get("guest_name")
                query_context["device_type"] = guest_info.get("device_type", "web")
                query_context["guest_preferences"] = guest_info.get("preferences", {})

            # Initialize state with skip_synthesis flag to get RAG data without LLM call
            request_id = hashlib.md5(f"{request.query}{time.time()}".encode()).hexdigest()[:8]
            initial_state = OrchestratorState(
                query=request.query,
                mode=current_mode,
                room=request.room,
                permissions=mode_info.get("permissions", {}),
                conversation_history=conversation_history,
                history_summary=history_summary,
                session_id=session.session_id,
                request_id=request_id,
                start_time=start_time,
                context=query_context,
                temperature=request.temperature
            )

            # Stage 2: Execute orchestrator workflow (for intent + RAG)
            yield f"data: {json.dumps({'stage': 'processing', 'message': 'Searching for information...'})}\n\n"

            tool_start_time = time.time()
            final_state = await orchestrator_graph.ainvoke(initial_state)
            tool_exec_time = time.time() - tool_start_time

            # Check if tool calling was used
            if final_state.get("tool_results"):
                tool_names = list(final_state["tool_results"].keys())
                tool_message = f"Found results using {', '.join(tool_names)}"
                yield f"data: {json.dumps({'stage': 'found', 'message': tool_message})}\n\n"

            # Stage 3: TRUE STREAMING - Stream from LLM as tokens are generated
            yield f"data: {json.dumps({'stage': 'answering', 'message': 'Generating response...'})}\n\n"
            llm_start_time = time.time()

            # Build synthesis prompt and stream directly from Ollama
            full_prompt, synthesis_model = await build_synthesis_prompt_for_streaming(final_state)

            logger.info(
                "true_streaming_started",
                request_id=request_id,
                model=synthesis_model,
                has_rag_data=bool(final_state.get("retrieved_data"))
            )

            # Stream tokens directly from Ollama
            full_answer = ""
            token_count = 0
            async for chunk in llm_router.generate_stream(
                model=synthesis_model,
                prompt=full_prompt,
                temperature=request.temperature or 0.7,
                max_tokens=2048
            ):
                token = chunk.get("token", "")
                if token:
                    token_count += 1
                    full_answer += token
                    yield f"data: {json.dumps({'stage': 'answer_chunk', 'content': token})}\n\n"

                # Check if done
                if chunk.get("done", False):
                    break

            # Update session with the streamed response
            session.add_message(role="user", content=request.query, metadata={"streaming": True})
            session.add_message(role="assistant", content=full_answer)

            # Final completion event with timing breakdown
            processing_time = time.time() - start_time
            llm_time = time.time() - llm_start_time
            tokens_per_second = token_count / llm_time if llm_time > 0 else 0

            logger.info(
                "true_streaming_complete",
                request_id=request_id,
                tokens=token_count,
                duration_ms=int(processing_time * 1000),
                tool_exec_ms=int(tool_exec_time * 1000),
                llm_time_ms=int(llm_time * 1000),
                tokens_per_second=round(tokens_per_second, 1),
                response_length=len(full_answer)
            )

            # Record streaming synthesis LLM call for metrics
            from shared.metrics import LLM_CALL_DURATION, LLM_TOKENS_GENERATED
            LLM_CALL_DURATION.labels(
                stage="stream_synthesis",
                model=synthesis_model,
                call_type="streaming"
            ).observe(llm_time)
            if token_count > 0:
                LLM_TOKENS_GENERATED.labels(stage="stream_synthesis", model=synthesis_model).inc(token_count)

            yield f"data: {json.dumps({'stage': 'complete', 'processing_time': processing_time, 'tool_exec_time': tool_exec_time, 'llm_time': llm_time, 'tokens': token_count, 'tokens_per_second': tokens_per_second})}\n\n"

        except Exception as e:
            logger.error(f"Streaming error: {e}", exc_info=True)
            yield f"data: {json.dumps({'stage': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.post("/query/stream/v2")
async def process_query_stream_v2(request: QueryRequest):
    """
    Process a user query with true LLM streaming and sentence buffering.

    This v2 endpoint streams LLM output token-by-token with sentence boundary
    detection for optimal TTS latency. Each sentence is yielded as soon as
    it's complete, enabling overlapped LLM generation and TTS synthesis.

    Expected latency savings: 1-1.5 seconds (time-to-first-audio reduction)

    SSE events:
    - {stage: 'processing', message: 'Analyzing request...'}
    - {stage: 'streaming', sentence_num: 1, sentence: 'First sentence.', is_final: false}
    - {stage: 'streaming', sentence_num: 2, sentence: 'Second sentence.', is_final: false}
    - {stage: 'complete', total_sentences: 2, full_response: '...', processing_time: 1.5}
    """
    async def sentence_event_generator():
        start_time = time.time()

        try:
            # Stage 1: Send initial status
            yield f"data: {json.dumps({'stage': 'processing', 'message': 'Analyzing your request...'})}\n\n"

            # Initialize orchestrator graph
            global orchestrator_graph
            if orchestrator_graph is None:
                orchestrator_graph = create_orchestrator_graph()

            # Session management
            session = await session_manager.get_or_create_session(
                session_id=request.session_id,
                user_id=request.mode,
                zone=request.room
            )

            # Get mode
            mode_info = await get_current_mode()
            current_mode = request.mode if request.mode else mode_info.get("mode", "owner")

            # Run orchestrator up to LLM synthesis point
            # We need intent classification and RAG data, but will stream the LLM response
            timing_tracker = TimingTracker()

            initial_state = OrchestratorState(
                query=request.query,
                mode=current_mode,
                room=request.room,
                temperature=request.temperature,
                session_id=session.session_id,
                conversation_history=[],
                history_summary="",
                permissions=mode_info.get("permissions", {}),
                interface_type=request.interface_type,
                context=dict(request.context) if request.context else {},
                memory_context="",
                timing_tracker=timing_tracker
            )

            # Run through classification and RAG nodes only (stop before synthesis)
            final_state = await orchestrator_graph.ainvoke(initial_state)

            intent_value = final_state.get("intent")
            intent_str = intent_value.value if hasattr(intent_value, "value") else str(intent_value)

            yield f"data: {json.dumps({'stage': 'classified', 'intent': intent_str})}\n\n"

            # Get the answer from the orchestrator (already synthesized)
            # For v2, we use the already-generated answer and stream it sentence by sentence
            answer = final_state.get("answer", "")

            if not answer:
                yield f"data: {json.dumps({'stage': 'error', 'message': 'No response generated'})}\n\n"
                return

            # Stream answer sentence by sentence
            sentences = []
            buffer = ""

            for char in answer:
                buffer += char

                # Check for sentence boundary
                if char in '.!?' and len(buffer) > 20:
                    # Check if next char is space or end (not mid-abbreviation)
                    sentences.append(buffer.strip())
                    yield f"data: {json.dumps({'stage': 'streaming', 'sentence_num': len(sentences), 'sentence': buffer.strip(), 'is_final': False})}\n\n"
                    buffer = ""

            # Yield any remaining content
            if buffer.strip():
                sentences.append(buffer.strip())
                yield f"data: {json.dumps({'stage': 'streaming', 'sentence_num': len(sentences), 'sentence': buffer.strip(), 'is_final': True})}\n\n"

            # Stage 4: Complete
            processing_time = time.time() - start_time
            yield f"data: {json.dumps({'stage': 'complete', 'total_sentences': len(sentences), 'full_response': answer, 'intent': intent_str, 'processing_time': processing_time})}\n\n"

            # Update session
            session.add_message(role="user", content=request.query, metadata={"streaming": True})
            session.add_message(role="assistant", content=answer)

            logger.info(
                "stream_v2_complete",
                total_sentences=len(sentences),
                processing_time=processing_time,
                intent=intent_str
            )

        except Exception as e:
            logger.error(f"Stream v2 error: {e}", exc_info=True)
            yield f"data: {json.dumps({'stage': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        sentence_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# ============================================================================
# OpenAI-Compatible API Endpoints (for Home Assistant integration)
# ============================================================================

class OpenAIChatMessage(BaseModel):
    """OpenAI chat message format."""
    role: str
    content: str

class OpenAIChatRequest(BaseModel):
    """OpenAI chat completion request format."""
    model: str = "gpt-4"
    messages: List[OpenAIChatMessage]
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    stream: bool = False  # Enable streaming responses
    extra_body: Optional[Dict[str, Any]] = None  # Extra context (room, interface_type)

class OpenAIChatResponse(BaseModel):
    """OpenAI chat completion response format."""
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[Dict[str, Any]]
    usage: Dict[str, int]

@app.get("/v1/models")
async def list_models():
    """OpenAI-compatible models endpoint - returns actual available LLM backends."""
    try:
        # Fetch available backends from admin API
        admin_url = os.getenv("ADMIN_API_URL", "http://localhost:8080")
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{admin_url}/api/llm-backends/public")
            response.raise_for_status()
            backends = response.json()

            # Return OpenAI-compatible aliases - Athena handles routing internally
            models = [
                {
                    "id": "gpt-4",
                    "object": "model",
                    "created": 1677610602,
                    "owned_by": "athena"
                },
                {
                    "id": "gpt-3.5-turbo",
                    "object": "model",
                    "created": 1677610602,
                    "owned_by": "athena"
                }
            ]

            return {
                "object": "list",
                "data": models
            }
    except Exception as e:
        logger.error("failed_to_fetch_models", error=str(e))
        # Fallback to default model if API fails
        return {
            "object": "list",
            "data": [
                {
                    "id": "gpt-4",
                    "object": "model",
                    "created": 1677610602,
                    "owned_by": "athena"
                }
            ]
        }


# ============================================================================
# True Streaming with RAG Support
# ============================================================================

async def build_synthesis_prompt_for_streaming(state: OrchestratorState) -> tuple[str, str]:
    """
    Build the synthesis prompt for streaming, using the same logic as synthesize_node.

    Returns:
        tuple of (full_prompt, synthesis_model)
    """
    # Build synthesis prompt based on context (same logic as synthesize_node)
    ref_info = state.context_ref_info or {}
    is_continuation = ref_info.get("is_continuation", False)

    if state.retrieved_data:
        context = json.dumps(state.retrieved_data, indent=2)
        synthesis_prompt = f"""Answer the following question using ONLY the provided context.

Question: {state.query}

Context Data:
{context}

CRITICAL ANTI-HALLUCINATION INSTRUCTIONS:
1. ONLY use facts from the Context Data above - NO EXCEPTIONS
2. If the context doesn't have specific information, say "I don't have information about that"
3. NEVER INVENT OR MAKE UP:
   - Business names, restaurant names, or venue names
   - Addresses or locations
   - Phone numbers or hours
   - Prices or ratings
   - Event names or dates
   - Any specific factual details not in the context
4. If asked for recommendations but context is empty, say "I couldn't find current information for that request"
5. Be concise and only state facts that appear in the Context Data
6. If context contains errors or no results, acknowledge that honestly

Response:"""
    elif is_continuation and state.conversation_history:
        synthesis_prompt = f"""The user is continuing a conversation with you. Their response: "{state.query}"

Based on the conversation history above, understand what the user means and respond appropriately.

INSTRUCTIONS:
1. Look at your previous question/statement in the conversation history
2. Understand what "{state.query}" means in that context
3. If they answered a question you asked, proceed with what they requested originally
4. If they declined something or said "no preference", continue with reasonable defaults
5. Be helpful and continue the task they originally requested

Your response:"""
    else:
        synthesis_prompt = f"""Question: {state.query}

CRITICAL: You do NOT have access to current or specific information to answer this question.

You must respond with:
1. Acknowledge you don't have current/specific information
2. Suggest where the user can find this information
3. NEVER make up specific facts, dates, names, numbers, or events

Respond honestly about your limitations.

Response:"""

    # Build system context (matches synthesize_node prompt)
    system_context = """You are Jarvis, an AI assistant inspired by the Jarvis from Iron Man.

Personality:
- Sophisticated, intelligent, and efficient
- Warm but professional, with subtle dry wit when appropriate
- Calm and composed, never flustered
- Genuinely helpful and attentive

Communication style:
- Clear, concise responses
- ALWAYS ask for clarification when a request is ambiguous - NEVER just say "I can't help"
- If you're unsure what the user means, ask! Examples:
  - "peruvian spot" -> ask "Are you looking for a Peruvian restaurant?"
  - "good place" -> ask "What kind of place? Restaurant, store, or something else?"
- Never give up on a request - if you can't fulfill it directly, ask clarifying questions
- If you don't understand a request, say "I'm not sure what you mean" and suggest what you think they might want

Honesty and accuracy:
- NEVER fabricate facts, data, or information
- If you don't have information, say so clearly
- Only state things as fact when you have the data to support them
- For creative requests (stories, jokes, etc.), be imaginative - fiction is not lying

Neutrality on sensitive topics:
- You can share preferences on food, movies, music, hobbies, lifestyle choices
- STAY NEUTRAL on political opinions, religious views, and controversial social topics
- If asked about divisive issues, acknowledge multiple perspectives without taking sides

Voice-friendly formatting (CRITICAL for text-to-speech):
- NEVER use emojis in responses - they don't work with text-to-speech
- Spell out state abbreviations: "MD" -> "Maryland", "CA" -> "California"
- Spell out street abbreviations: "St" -> "Street", "Ave" -> "Avenue", "Blvd" -> "Boulevard"
- Speak zip codes as individual digits: "21117" -> "2 1 1 1 7"
- Spell out "Dr" as "Drive" for addresses, "Doctor" for people
- Say "and" instead of "&"
- Say "number" instead of "#"
- Say "at" instead of "@" in addresses
- Say "degrees Fahrenheit" instead of "°F" or just "F" after temperatures
- Say "miles per hour" instead of "mph"
- For restaurant pricing: "$" -> "budget-friendly", "$$" -> "moderate", "$$$" -> "upscale", "$$$$" -> "fine dining"
- Write times with spaces before and between letters: "10:30 AM" -> "10:30, A M", "5 PM" -> "5, P M" (comma creates pause before A/P)
- For times, use "oh" not "zero": "3:06 PM" -> "three oh six, P M" (NOT "three zero six")
- Expand common abbreviations for natural speech

When you have retrieved data, use it accurately. When you don't have data for a factual question, acknowledge it honestly rather than guessing.

"""

    # Inject base knowledge context from Admin API
    try:
        admin_client = get_admin_client()
        user_mode = state.mode if state.mode else "guest"
        knowledge_context = await get_knowledge_context_for_user(admin_client, user_mode)
        if knowledge_context:
            system_context += knowledge_context
    except Exception as e:
        logger.warning(f"Failed to fetch base knowledge context for streaming: {e}")

    # Inject guest name for personalization
    if state.context and state.context.get("guest_name"):
        guest_name = state.context["guest_name"]
        system_context += f"\nYou are speaking with {guest_name}, a guest at this property. "
        system_context += f"Address them by name when appropriate to provide a personalized experience.\n"

    # Inject relevant memories
    if state.memory_context:
        system_context += state.memory_context

    # Format conversation history
    history_context = ""
    if state.history_summary:
        history_context = f"{state.history_summary}\n\n"
    elif state.conversation_history:
        history_context = "Previous conversation:\n"
        for msg in state.conversation_history:
            role = msg["role"].capitalize()
            content = msg["content"]
            history_context += f"{role}: {content}\n"
        history_context += "\n"

    # Combine contexts
    full_prompt = system_context + history_context + synthesis_prompt

    # Get synthesis model
    synthesis_model = await get_model_for_component("response_synthesis")

    return full_prompt, synthesis_model


async def run_orchestrator_for_streaming(state: OrchestratorState) -> OrchestratorState:
    """
    Run orchestrator through RAG collection, stopping before LLM synthesis.

    This executes:
    - classify_node (intent classification)
    - route_info_node (if needed)
    - retrieve_node (RAG data collection)

    But NOT:
    - synthesize_node (that will be streamed)
    - validate_node (not needed for streaming)

    Returns:
        OrchestratorState with retrieved_data populated
    """
    # Run classification
    state = await classify_node(state)

    # Handle special intents that don't need RAG
    if state.intent == IntentCategory.CONTROL:
        # Control commands - handled by HA, not LLM
        state = await route_control_node(state)
        return state

    if state.intent in [IntentCategory.MUSIC_PLAY, IntentCategory.MUSIC_CONTROL]:
        # Music commands - handled by music handler
        state = await route_music_node(state)
        return state

    if state.intent == IntentCategory.TV_CONTROL:
        # TV commands - handled by TV handler
        state = await route_tv_node(state)
        return state

    if state.intent == IntentCategory.TEXT_ME_THAT:
        # SMS - handled by SMS node
        state = await send_sms_node(state)
        return state

    # Check for tool calling (Phase 2 services)
    tool_calling_result = await should_use_tool_calling(state, trigger_context="classify")

    phase2_intents = {
        IntentCategory.DINING, IntentCategory.RECIPES, IntentCategory.EVENTS,
        IntentCategory.STREAMING, IntentCategory.NEWS, IntentCategory.STOCKS,
        IntentCategory.FLIGHTS, IntentCategory.DIRECTIONS
    }

    if tool_calling_result or state.intent in phase2_intents:
        # Run tool calling to get data
        state = await tool_call_node(state)
        return state

    if state.intent == IntentCategory.UNKNOWN:
        # Unknown intent - let LLM handle with context
        return state

    # For info intents, run the info routing and retrieval
    state = await route_info_node(state)
    state = await retrieve_node(state)

    # Check if tool calling needed after retrieve
    if await should_use_tool_calling(state, trigger_context="retrieve"):
        state = await tool_call_node(state)

    return state


@app.post("/v1/chat/completions")
async def chat_completions(request: OpenAIChatRequest):
    """
    OpenAI-compatible chat completions endpoint with streaming support.
    Wraps the orchestrator's /query endpoint for Home Assistant and Open WebUI compatibility.
    """
    try:
        # Extract the last user message
        user_message = None
        for msg in reversed(request.messages):
            if msg.role == "user":
                user_message = msg.content
                break

        if not user_message:
            raise HTTPException(status_code=400, detail="No user message found")

        # If streaming is requested, use SSE format
        if request.stream:
            async def openai_stream_generator():
                # Initialize state and run orchestrator
                global orchestrator_graph
                if orchestrator_graph is None:
                    orchestrator_graph = create_orchestrator_graph()

                session = await session_manager.get_or_create_session(
                    session_id="openwebui-session",
                    user_id="openwebui",
                    zone="web"
                )

                mode_info = await get_current_mode()
                config = await get_config()
                conv_settings = await config.get_conversation_settings()
                conversation_history = []
                history_summary = ""

                if conv_settings.get("enabled", False):
                    history_mode = conv_settings.get("history_mode", "full")

                    if history_mode == "none":
                        pass  # No history
                    elif history_mode == "summarized":
                        precompute_enabled = await get_feature_flag("ha_precomputed_summaries", default=False)

                        if precompute_enabled:
                            # Try precomputed summary first
                            precomputed = await get_session_summary(session.session_id)
                            if precomputed:
                                history_summary = precomputed
                            else:
                                max_history = conv_settings.get("max_llm_history_messages", 10)
                                raw_history = session.get_llm_history(max_history)
                                if raw_history:
                                    history_summary = await summarize_conversation_history(
                                        raw_history,
                                        user_message,
                                        request_id=hashlib.md5(f"{user_message}{time.time()}".encode()).hexdigest()[:8]
                                    )
                                    await update_session_summary(session.session_id, history_summary)
                        else:
                            max_history = conv_settings.get("max_llm_history_messages", 10)
                            raw_history = session.get_llm_history(max_history)
                            if raw_history:
                                history_summary = await summarize_conversation_history(
                                    raw_history,
                                    user_message,
                                    request_id=hashlib.md5(f"{user_message}{time.time()}".encode()).hexdigest()[:8]
                                )
                    else:  # "full" mode
                        max_history = conv_settings.get("max_llm_history_messages", 10)
                        conversation_history = session.get_llm_history(max_history)

                start_time = time.time()

                # Extract room and interface_type from extra_body (passed from gateway)
                extra_body = request.extra_body or {}
                room = extra_body.get("room", "web")
                interface_type = extra_body.get("interface_type", "text")  # Default to text for backward compat

                initial_state = OrchestratorState(
                    query=user_message,
                    mode=mode_info.get("mode", "owner"),
                    room=room,
                    permissions=mode_info.get("permissions", {}),
                    conversation_history=conversation_history,
                    history_summary=history_summary,
                    session_id=session.session_id,
                    request_id=hashlib.md5(f"{user_message}{time.time()}".encode()).hexdigest()[:8],
                    start_time=start_time,
                    interface_type=interface_type
                )

                # TRUE STREAMING: Run orchestrator for RAG collection, then stream LLM tokens
                logger.info(
                    "streaming_request_started",
                    request_id=initial_state.request_id,
                    query_preview=user_message[:50]
                )

                # Run orchestrator through RAG collection (no synthesis)
                state = await run_orchestrator_for_streaming(initial_state)

                # Check if already answered by a handler (control, music, TV, SMS)
                if state.answer:
                    # These handlers provide pre-computed answers - use fake streaming
                    logger.info(
                        "streaming_precomputed_answer",
                        request_id=state.request_id,
                        intent=state.intent.value if state.intent else "unknown"
                    )
                    answer = state.answer
                    # Apply TTS normalization (expand abbreviations, zip codes, etc.)
                    answer = normalize_for_tts(answer)
                    words = answer.split()
                    for i, word in enumerate(words):
                        chunk = word + (" " if i < len(words) - 1 else "")
                        chunk_data = {
                            "id": initial_state.request_id,
                            "object": "chat.completion.chunk",
                            "created": int(start_time),
                            "model": request.model,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": chunk},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(chunk_data)}\n\n"
                        await asyncio.sleep(0.02)

                    # Save conversation to session for follow-up queries
                    session.add_message(role="user", content=user_message, metadata={"intent": state.intent.value if state.intent else "unknown"})
                    session.add_message(role="assistant", content=state.answer)
                    logger.info(f"Session {session.session_id} updated with {len(session.messages)} messages (precomputed)")
                else:
                    # TRUE STREAMING: Build prompt and stream directly from LLM
                    full_prompt, synthesis_model = await build_synthesis_prompt_for_streaming(state)

                    logger.info(
                        "streaming_llm_started",
                        request_id=state.request_id,
                        model=synthesis_model,
                        has_rag_data=bool(state.retrieved_data)
                    )

                    # Stream tokens directly from Ollama via LLM Router
                    token_count = 0
                    response_tokens = []  # Accumulate for session storage

                    # For voice interface: buffer all tokens first, then normalize and stream
                    # For text/chat: stream tokens directly (original behavior)
                    is_voice = interface_type == "voice"

                    async for chunk in llm_router.generate_stream(
                        model=synthesis_model,
                        prompt=full_prompt,
                        temperature=state.temperature,
                        max_tokens=2048
                    ):
                        token = chunk.get("token", "")
                        if token:
                            token_count += 1
                            response_tokens.append(token)  # Accumulate for session

                            # Only stream immediately for non-voice interfaces
                            if not is_voice:
                                chunk_data = {
                                    "id": initial_state.request_id,
                                    "object": "chat.completion.chunk",
                                    "created": int(start_time),
                                    "model": request.model,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"content": token},
                                        "finish_reason": None
                                    }]
                                }
                                yield f"data: {json.dumps(chunk_data)}\n\n"

                        # Check if done
                        if chunk.get("done", False):
                            break

                    stream_duration = time.time() - start_time

                    # For voice interface: normalize and stream the complete response
                    if is_voice and response_tokens:
                        full_response = "".join(response_tokens)
                        normalized_response = normalize_for_tts(full_response)
                        logger.info(
                            "tts_normalization_applied",
                            request_id=state.request_id,
                            original_len=len(full_response),
                            normalized_len=len(normalized_response)
                        )

                        # Fake-stream the normalized response word by word
                        words = normalized_response.split()
                        for i, word in enumerate(words):
                            chunk_text = word + (" " if i < len(words) - 1 else "")
                            chunk_data = {
                                "id": initial_state.request_id,
                                "object": "chat.completion.chunk",
                                "created": int(start_time),
                                "model": request.model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"content": chunk_text},
                                    "finish_reason": None
                                }]
                            }
                            yield f"data: {json.dumps(chunk_data)}\n\n"
                            await asyncio.sleep(0.02)  # Small delay for visual streaming effect

                    logger.info(
                        "streaming_llm_complete",
                        request_id=state.request_id,
                        tokens=token_count,
                        duration_ms=int(stream_duration * 1000),
                        tts_normalized=is_voice
                    )

                    # Save conversation to session for follow-up queries
                    full_response = "".join(response_tokens)
                    session.add_message(role="user", content=user_message, metadata={"intent": state.intent.value if state.intent else "unknown"})
                    session.add_message(role="assistant", content=full_response)
                    logger.info(f"Session {session.session_id} updated with {len(session.messages)} messages (streaming)")

                    # Record streaming synthesis LLM call for metrics
                    from shared.metrics import LLM_CALL_DURATION, LLM_TOKENS_GENERATED
                    LLM_CALL_DURATION.labels(
                        stage="stream_synthesis",
                        model=synthesis_model,
                        call_type="streaming"
                    ).observe(stream_duration)
                    if token_count > 0:
                        LLM_TOKENS_GENERATED.labels(stage="stream_synthesis", model=synthesis_model).inc(token_count)

                # Send final chunk with finish_reason
                final_chunk = {
                    "id": initial_state.request_id,
                    "object": "chat.completion.chunk",
                    "created": int(start_time),
                    "model": request.model,
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop"
                    }]
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                openai_stream_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no"
                }
            )

        # Non-streaming response (original behavior)
        query_request = QueryRequest(
            query=user_message,
            session_id="ha-voice-assistant"
        )

        result = await process_query(query_request)

        # Convert to OpenAI format
        response = OpenAIChatResponse(
            id=result.request_id,
            created=int(time.time()),
            model=request.model,
            choices=[{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result.answer
                },
                "finish_reason": "stop"
            }],
            usage={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        )

        return response

    except Exception as e:
        logger.error(f"Error in chat completions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Follow-Me Audio Endpoints
# ============================================================================

class MotionEventRequest(BaseModel):
    """Request model for motion event from Home Assistant webhook."""
    room: str = Field(..., description="Room name where motion occurred")
    motion_detected: bool = Field(..., description="True if motion started, False if cleared")
    entity_id: Optional[str] = Field(None, description="Motion sensor entity ID")
    timestamp: Optional[float] = Field(None, description="Event timestamp (Unix epoch)")


@app.post("/motion-event", tags=["follow-me"])
async def handle_motion_event(request: MotionEventRequest):
    """
    Handle motion event from Home Assistant for follow-me audio.

    This endpoint is called by HA automation when motion sensors change state.
    It triggers the follow-me audio service to potentially transfer music playback.

    Returns:
        Status of the motion event processing
    """
    global follow_me_service

    if not follow_me_service:
        return {
            "status": "disabled",
            "message": "Follow-me audio service not initialized"
        }

    try:
        await follow_me_service.handle_motion_event(
            room_name=request.room,
            motion_detected=request.motion_detected,
            timestamp=request.timestamp
        )

        return {
            "status": "ok",
            "room": request.room,
            "motion_detected": request.motion_detected,
            "service_status": follow_me_service.get_status()
        }

    except Exception as e:
        logger.error("motion_event_failed", room=request.room, error=str(e))
        return {
            "status": "error",
            "message": str(e)
        }


@app.get("/follow-me/status", tags=["follow-me"])
async def get_follow_me_status():
    """
    Get current follow-me audio status.

    Returns:
        Current mode, active rooms, and presence state
    """
    global follow_me_service

    if not follow_me_service:
        return {
            "status": "disabled",
            "message": "Follow-me audio service not initialized"
        }

    return {
        "status": "ok",
        **follow_me_service.get_status()
    }


@app.post("/follow-me/mode", tags=["follow-me"])
async def set_follow_me_mode(mode: str):
    """
    Set follow-me audio mode.

    Args:
        mode: "off", "single", or "party"

    Returns:
        Updated status
    """
    global follow_me_service

    if not follow_me_service:
        return {"status": "disabled", "message": "Service not initialized"}

    try:
        follow_me_service.set_mode(FollowMeMode(mode))
        return {"status": "ok", "mode": mode}
    except ValueError:
        return {"status": "error", "message": f"Invalid mode: {mode}"}


@app.post("/follow-me/enabled", tags=["follow-me"])
async def set_follow_me_enabled(enabled: bool):
    """
    Enable or disable follow-me audio.

    Args:
        enabled: True to enable, False to disable

    Returns:
        Updated status
    """
    global follow_me_service

    if not follow_me_service:
        return {"status": "disabled", "message": "Service not initialized"}

    follow_me_service.set_enabled(enabled)
    return {"status": "ok", "enabled": enabled}


# ============================================================================
# Health and Metrics Endpoints
# ============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    health = {
        "status": "healthy",
        "service": "orchestrator",
        "version": "1.0.0",
        "components": {
            "validator": True,
            "rag_validator": True
        }
    }

    # Check Home Assistant
    try:
        ha_healthy = await ha_client.health_check() if ha_client else False
        health["components"]["home_assistant"] = ha_healthy
    except:
        health["components"]["home_assistant"] = False

    # Check LLM Router (supports Ollama, MLX, etc.)
    try:
        health["components"]["llm_router"] = llm_router is not None
    except:
        health["components"]["llm_router"] = False

    # Check Redis
    try:
        health["components"]["redis"] = await cache_client.ping() if cache_client else False
    except:
        health["components"]["redis"] = False

    # Add resilience pattern status (circuit breakers, rate limiters)
    try:
        cb_registry = get_circuit_breaker_registry()
        rl_registry = get_rate_limiter_registry()

        # Get open circuits (services currently failing)
        open_circuits = cb_registry.get_open_circuits()
        health["resilience"] = {
            "circuit_breakers": cb_registry.get_all_status(),
            "open_circuits": open_circuits,
            "rate_limiters": rl_registry.get_all_status(),
            "rejection_stats": rl_registry.get_rejection_stats()
        }

        # If any circuits are open, service is degraded
        if open_circuits:
            health["components"]["circuit_breakers"] = False
            logger.warning(f"Open circuits detected: {open_circuits}")
        else:
            health["components"]["circuit_breakers"] = True

    except Exception as e:
        logger.error(f"Failed to get resilience status: {e}")
        health["resilience"] = {"error": str(e)}
        health["components"]["circuit_breakers"] = False

    # Check RAG services by pinging their health endpoints directly
    # This is more accurate than database checks as it verifies services are actually running
    try:
        for name, url in rag_client._service_urls.items():
            try:
                response = await rag_client.get(name, "/health", skip_circuit_breaker=True, skip_rate_limit=True)
                health["components"][f"rag_{name}"] = response.success
            except Exception as e:
                logger.warning(f"RAG service {name} health check failed: {e}")
                health["components"][f"rag_{name}"] = False
    except Exception as e:
        logger.error(f"Failed to check RAG services: {e}")
        for name in rag_client._service_urls.keys():
            health["components"][f"rag_{name}"] = False

    # Determine overall health (redis is optional - caching degrades gracefully)
    critical_components = ["llm_router"]
    if not all(health["components"].get(c, False) for c in critical_components):
        health["status"] = "unhealthy"
    elif not all(health["components"].values()):
        health["status"] = "degraded"

    return health


@app.post("/admin/invalidate-feature-cache")
async def invalidate_feature_cache(request: Request, flags: Optional[List[str]] = None):
    """
    Invalidate feature flag and configuration caches.

    Called by Admin backend when feature flags are toggled.
    Clears:
    - Config loader's memory and Redis caches
    - Any TV handler or other module-specific caches

    Args:
        request: FastAPI request object
        flags: Optional list of specific flag names (not used currently - clears all)

    Returns:
        dict with status
    """
    from orchestrator.config_loader import clear_cache

    client_host = request.client.host if request.client else "unknown"

    try:
        # Clear config loader cache (memory + Redis)
        await clear_cache()

        logger.info(
            "feature_cache_invalidated",
            source=client_host,
            flags=flags
        )

        return {
            "status": "ok",
            "message": "All caches cleared",
            "source": client_host
        }
    except Exception as e:
        logger.error(f"Cache invalidation failed: {e}")
        return {
            "status": "error",
            "message": str(e)
        }


@app.get("/session/{session_id}/warmup")
async def warmup_session(session_id: str) -> dict:
    """
    Pre-fetch session data to warm cache.

    Called by Gateway on wake word detection to pre-load session data
    before the actual query arrives. This hides session lookup latency
    behind STT processing time.

    Args:
        session_id: Session identifier to warm

    Returns:
        Status dict with session info
    """
    try:
        session_manager = await get_session_manager()
        session = await session_manager.get_session(session_id)

        if session:
            return {
                "status": "warmed",
                "session_id": session_id,
                "message_count": len(session.messages)
            }
        return {
            "status": "not_found",
            "session_id": session_id
        }
    except Exception as e:
        logger.warning(f"Session warmup failed for {session_id}: {e}")
        return {
            "status": "error",
            "session_id": session_id,
            "message": str(e)
        }


@app.get("/health/live")
async def liveness_probe():
    """
    Kubernetes liveness probe.

    Returns healthy if the process is running and responsive.
    K8s will restart the pod if this fails.
    """
    return {
        "status": "ok",
        "service": "orchestrator"
    }


@app.get("/health/ready")
async def readiness_probe():
    """
    Kubernetes readiness probe.

    Returns healthy if the service is ready to accept traffic.
    K8s will remove from load balancer if this fails.
    """
    ready = True
    components = {}

    # Check LLM Router (critical)
    try:
        components["llm_router"] = llm_router is not None
        if not components["llm_router"]:
            ready = False
    except:
        components["llm_router"] = False
        ready = False

    # Check Home Assistant (optional - degraded if down)
    try:
        ha_healthy = await ha_client.health_check() if ha_client else False
        components["home_assistant"] = ha_healthy
    except:
        components["home_assistant"] = False

    # Check Redis (optional)
    try:
        components["redis"] = await cache_client.ping() if cache_client else False
    except:
        components["redis"] = False

    status_code = 200 if ready else 503
    return Response(
        content=json.dumps({
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "components": components
        }),
        status_code=status_code,
        media_type="application/json"
    )


@app.get("/health/startup")
async def startup_probe():
    """
    Kubernetes startup probe.

    Returns healthy once startup is complete.
    K8s will wait before starting liveness/readiness checks.
    """
    # If we're responding, startup is complete
    return {
        "status": "ok",
        "initialized": True,
        "service": "orchestrator"
    }


@app.get("/metrics", tags=["monitoring"])
async def metrics_endpoint():
    """Prometheus metrics endpoint."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(get_metrics_text(), media_type="text/plain")


@app.get("/resilience")
async def resilience_status():
    """
    Detailed resilience pattern status endpoint.

    Returns circuit breaker states, rate limiter stats, and open circuits.
    Useful for operations dashboards and debugging.
    """
    try:
        cb_registry = get_circuit_breaker_registry()
        rl_registry = get_rate_limiter_registry()

        return {
            "status": "ok",
            "circuit_breakers": cb_registry.get_all_status(),
            "open_circuits": cb_registry.get_open_circuits(),
            "rate_limiters": rl_registry.get_all_status(),
            "rejection_stats": rl_registry.get_rejection_stats()
        }
    except Exception as e:
        logger.error(f"Failed to get resilience status: {e}")
        return {"status": "error", "error": str(e)}


@app.post("/admin/reset-circuit-breaker/{service_name}")
async def reset_circuit_breaker(service_name: str):
    """
    Reset a specific service's circuit breaker.

    Use this to manually recover a service after an outage is resolved.
    """
    try:
        cb_registry = get_circuit_breaker_registry()
        rl_registry = get_rate_limiter_registry()

        # Reset circuit breaker
        breaker = cb_registry.get_breaker(service_name)
        breaker.reset()

        # Also reset rate limiter for the service
        limiter = rl_registry.get_limiter(service_name)
        limiter.reset()

        logger.info(f"Manually reset circuit breaker and rate limiter for {service_name}")
        return {
            "status": "success",
            "service": service_name,
            "message": f"Circuit breaker and rate limiter reset for {service_name}"
        }
    except Exception as e:
        logger.error(f"Failed to reset circuit breaker for {service_name}: {e}")
        return {"status": "error", "error": str(e)}


@app.post("/admin/reset-all-circuits")
async def reset_all_circuits():
    """
    Reset all circuit breakers and rate limiters.

    Use this for a full recovery after a major outage.
    """
    try:
        cb_registry = get_circuit_breaker_registry()
        rl_registry = get_rate_limiter_registry()

        cb_registry.reset_all()
        rl_registry.reset_all()

        logger.info("Manually reset all circuit breakers and rate limiters")
        return {
            "status": "success",
            "message": "All circuit breakers and rate limiters have been reset"
        }
    except Exception as e:
        logger.error(f"Failed to reset all circuits: {e}")
        return {"status": "error", "error": str(e)}


@app.post("/admin/invalidate-model-cache")
async def invalidate_model_cache():
    """
    Invalidate component model cache.
    Called by admin backend when model assignments are changed.
    """
    try:
        admin_client = get_admin_client()
        admin_client.invalidate_component_model_cache()
        logger.info("component_model_cache_invalidated_via_api")
        return {"status": "success", "message": "Cache invalidated"}
    except Exception as e:
        logger.error(f"cache_invalidation_failed: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(content=generate_latest(), media_type="text/plain")

@app.get("/llm-metrics")
async def llm_metrics():
    """
    Get LLM performance metrics from the router.

    Returns aggregated metrics including:
    - Overall average latency and tokens/sec
    - Per-model breakdown
    - Per-backend breakdown
    """
    try:
        metrics_data = llm_router.report_metrics()
        return metrics_data
    except Exception as e:
        logger.error(f"Failed to retrieve LLM metrics: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve metrics: {str(e)}"
        )

# ============================================================================
# Session Management Endpoints (Phase 2)
# ============================================================================

class SessionListResponse(BaseModel):
    """Response model for session list."""
    sessions: List[Dict[str, Any]] = Field(default_factory=list)
    total: int = Field(..., description="Total number of sessions")

class SessionDetailResponse(BaseModel):
    """Response model for session details."""
    session_id: str
    user_id: Optional[str]
    zone: Optional[str]
    created_at: str
    last_activity: str
    message_count: int
    messages: List[Dict[str, Any]]
    metadata: Dict[str, Any]

@app.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    limit: int = 50,
    offset: int = 0
) -> SessionListResponse:
    """
    List all active conversation sessions.

    Query Parameters:
    - limit: Maximum number of sessions to return (default 50)
    - offset: Number of sessions to skip (default 0)
    """
    # Note: This is a simplified implementation
    # In production, you'd want to add pagination support to the session manager

    # For now, we'll return an empty list since session_manager stores sessions in Redis/memory
    # and doesn't have a built-in list_all method

    logger.info(f"Listing sessions (limit={limit}, offset={offset})")

    return SessionListResponse(
        sessions=[],
        total=0
    )

@app.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session_details(session_id: str) -> SessionDetailResponse:
    """
    Get details of a specific session including message history.

    Path Parameters:
    - session_id: Session identifier
    """
    session = await session_manager.get_session(session_id)

    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    logger.info(f"Retrieved session {session_id} with {len(session.messages)} messages")

    return SessionDetailResponse(
        session_id=session.session_id,
        user_id=session.user_id,
        zone=session.zone,
        created_at=session.created_at.isoformat(),
        last_activity=session.last_activity.isoformat(),
        message_count=len(session.messages),
        messages=session.messages,
        metadata=session.metadata
    )

@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """
    Delete a conversation session.

    Path Parameters:
    - session_id: Session identifier
    """
    session = await session_manager.get_session(session_id)

    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    await session_manager.delete_session(session_id)

    logger.info(f"Deleted session {session_id}")

    return {"status": "success", "message": f"Session {session_id} deleted"}

@app.get("/sessions/{session_id}/export")
async def export_session_history(session_id: str, format: str = "json"):
    """
    Export session history in various formats.

    Path Parameters:
    - session_id: Session identifier

    Query Parameters:
    - format: Export format (json, text, markdown) - default: json
    """
    session = await session_manager.get_session(session_id)

    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    if format == "json":
        return {
            "session_id": session.session_id,
            "messages": session.messages,
            "created_at": session.created_at.isoformat(),
            "last_activity": session.last_activity.isoformat()
        }

    elif format == "text":
        lines = [f"Conversation Session: {session.session_id}"]
        lines.append(f"Created: {session.created_at.isoformat()}")
        lines.append(f"Last Activity: {session.last_activity.isoformat()}")
        lines.append("=" * 80)
        lines.append("")

        for msg in session.messages:
            role = msg["role"].upper()
            content = msg["content"]
            timestamp = msg.get("timestamp", "")
            lines.append(f"[{timestamp}] {role}:")
            lines.append(content)
            lines.append("")

        return Response(content="\n".join(lines), media_type="text/plain")

    elif format == "markdown":
        lines = [f"# Conversation Session: {session.session_id}"]
        lines.append(f"**Created:** {session.created_at.isoformat()}")
        lines.append(f"**Last Activity:** {session.last_activity.isoformat()}")
        lines.append("")

        for msg in session.messages:
            role = msg["role"].capitalize()
            content = msg["content"]
            timestamp = msg.get("timestamp", "")
            lines.append(f"### {role} ({timestamp})")
            lines.append(content)
            lines.append("")

        return Response(content="\n".join(lines), media_type="text/markdown")

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {format}")


# =============================================================================
# Tool Registry Refresh Endpoint
# =============================================================================

@app.post("/tools/refresh")
async def refresh_tools():
    """
    Force a refresh of the tool registry.

    This endpoint is called by the admin backend to trigger a reload of all tools:
    1. Static tools (from admin database)
    2. MCP tools (from n8n if enabled)
    3. Legacy tools (from rag_tools.py if fallback enabled)

    Returns refresh statistics.
    """
    from shared.tool_registry import get_tool_registry

    logger.info("tool_registry_refresh_requested")

    registry = await get_tool_registry()
    await registry.refresh()

    stats = registry.get_tool_stats()

    logger.info(
        "tool_registry_refresh_complete",
        static_count=stats['static_count'],
        mcp_count=stats['mcp_count'],
        legacy_count=stats['legacy_count'],
        total_unique=stats['total_unique'],
    )

    return {
        "success": True,
        "static_count": stats['static_count'],
        "mcp_count": stats['mcp_count'],
        "legacy_count": stats['legacy_count'],
        "total_unique": stats['total_unique'],
    }


@app.get("/tools/list")
async def list_tools(source: str = None, guest_mode: bool = False):
    """
    List all available tools.

    Query Parameters:
    - source: Filter by source (static, mcp, legacy)
    - guest_mode: Only show guest-mode allowed tools

    Returns list of tools with their schemas.
    """
    from shared.tool_registry import get_tool_registry, ToolSource

    registry = await get_tool_registry()

    if source:
        try:
            tool_source = ToolSource[source.upper()]
            tools = registry.get_tools_by_source(tool_source)
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Invalid source: {source}")
    else:
        tools = registry.get_all_tools(guest_mode=guest_mode)

    return {
        "tools": [
            {
                "name": tool.name,
                "display_name": tool.display_name,
                "description": tool.description,
                "source": tool.source.name if tool.source else "unknown",
                "priority": tool.priority,
                "guest_mode_allowed": tool.guest_mode_allowed,
                "enabled": tool.enabled,
            }
            for tool in tools
        ],
        "count": len(tools),
    }


@app.get("/tools/stats")
async def get_tool_stats():
    """
    Get tool registry statistics.

    Returns counts of tools by source and status.
    """
    from shared.tool_registry import get_tool_registry

    registry = await get_tool_registry()
    return registry.get_tool_stats()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
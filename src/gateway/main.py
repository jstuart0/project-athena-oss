"""
Project Athena Gateway Service

OpenAI-compatible API that routes requests to the orchestrator for
Athena-specific queries or falls back to Ollama for general queries.
"""

import os
import json
import time
import uuid
import asyncio
import subprocess
import signal
from datetime import datetime, timezone
from typing import AsyncIterator, Dict, Any, List, Optional, Union
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from prometheus_client import Counter, Histogram, generate_latest
from starlette.responses import Response

# Add to Python path for imports
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.logging_config import configure_logging
from shared.ollama_client import OllamaClient
from shared.admin_config import get_admin_client
from shared.tracing import RequestTracingMiddleware, get_tracing_headers
from shared.errors import (
    register_exception_handlers,
    RateLimitError,
    ServiceUnavailableError,
    UpstreamError
)
from gateway.device_session_manager import get_device_session_manager, DeviceSessionManager
from gateway.simple_commands import detect_simple_command, execute_simple_command
from gateway.intent_prerouter import classify_intent, handle_simple_intent
from gateway.circuit_breaker import CircuitBreaker, CircuitState
from gateway.rate_limiter import TokenBucketRateLimiter

# LiveKit WebRTC support (optional)
try:
    from gateway.livekit_routes import router as livekit_router
    from gateway.livekit_integration import (
        initialize_livekit_integration,
        shutdown_livekit_integration,
        get_livekit_integration
    )
    LIVEKIT_ROUTES_AVAILABLE = True
except ImportError:
    LIVEKIT_ROUTES_AVAILABLE = False
    livekit_router = None
    initialize_livekit_integration = None
    shutdown_livekit_integration = None
    get_livekit_integration = None

# Configure logging
logger = configure_logging("gateway")

# Metrics
request_counter = Counter(
    'gateway_requests_total',
    'Total requests to gateway',
    ['endpoint', 'status']
)
request_duration = Histogram(
    'gateway_request_duration_seconds',
    'Request duration in seconds',
    ['endpoint']
)

# Voice pipeline timing metrics (for Prometheus/Grafana monitoring)
stt_duration = Histogram(
    'athena_stt_duration_seconds',
    'Speech-to-text transcription duration in seconds',
    ['engine', 'interface'],
    buckets=[0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0]
)
tts_duration = Histogram(
    'athena_tts_duration_seconds',
    'Text-to-speech synthesis duration in seconds',
    ['engine', 'voice', 'interface'],
    buckets=[0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0]
)
llm_duration = Histogram(
    'athena_llm_duration_seconds',
    'LLM query processing duration in seconds',
    ['model', 'interface'],
    buckets=[0.5, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 15.0, 20.0, 30.0]
)
voice_pipeline_duration = Histogram(
    'athena_voice_pipeline_duration_seconds',
    'Total voice pipeline duration (STT + LLM + TTS) in seconds',
    ['interface'],
    buckets=[1.0, 2.0, 3.0, 4.0, 5.0, 7.5, 10.0, 15.0, 20.0, 30.0]
)

# Counter for voice pipeline steps
voice_step_counter = Counter(
    'athena_voice_steps_total',
    'Voice pipeline step counts',
    ['step', 'status', 'interface']
)

# Global clients
orchestrator_client: Optional[httpx.AsyncClient] = None
ollama_client: Optional[OllamaClient] = None
device_session_mgr: Optional[DeviceSessionManager] = None
admin_client = None  # Admin API client for configuration
metric_client: Optional[httpx.AsyncClient] = None  # Shared client for metric logging
ha_client: Optional[httpx.AsyncClient] = None  # Shared client for Home Assistant API

# Gateway configuration (loaded from database)
# This is the centralized configuration object fetched from Admin API
gateway_config: Optional[Dict[str, Any]] = None

# Resilience patterns (circuit breaker and rate limiter)
orchestrator_circuit_breaker: Optional[CircuitBreaker] = None
global_rate_limiter: Optional[TokenBucketRateLimiter] = None

# Configuration (environment variable fallbacks - localhost defaults for development)
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_SERVICE_URL", "http://localhost:8001")
OLLAMA_URL = os.getenv("LLM_SERVICE_URL") or os.getenv("OLLAMA_URL", "http://localhost:11434")
API_KEY = os.getenv("GATEWAY_API_KEY", "dummy-key")  # Optional for Phase 1
ADMIN_API_URL = os.getenv("ADMIN_API_URL", "http://localhost:8080")

# Feature flag cache - per-flag caching with TTL
# Structure: {flag_name: (timestamp, value)}
_feature_flag_cache: Dict[str, tuple] = {}
_feature_flag_cache_ttl = 60.0  # seconds

# General cache TTL (used by LLM backends cache)
_cache_ttl = 60  # 60 seconds

# Room detection cache with TTL (for ha_room_detection_cache feature)
_room_cache: Dict[str, tuple] = {}
_room_cache_ttl = 3.0  # seconds


def _get_cached_room(device_id: str) -> Optional[str]:
    """Get room from cache if valid."""
    cache_key = f"room:{device_id}"
    if cache_key in _room_cache:
        cached_time, cached_room = _room_cache[cache_key]
        if time.time() - cached_time < _room_cache_ttl:
            return cached_room
    return None


def _set_cached_room(device_id: str, room: str):
    """Store room in cache."""
    cache_key = f"room:{device_id}"
    _room_cache[cache_key] = (time.time(), room)


# LLM backends cache (from database)
_llm_backends_cache = []
_llm_backends_cache_time = 0

# Model mapping (OpenAI -> Ollama) - Fallback if database is empty
MODEL_MAPPING = {
    "gpt-3.5-turbo": "phi3:mini",
    "gpt-4": "llama3.1:8b",
    "gpt-4-32k": "llama3.1:8b",
}

async def get_llm_backends():
    """
    Fetch enabled LLM backends from Admin API with caching.

    Returns list of backend configurations sorted by priority,
    or empty list if database is unavailable (triggers env var fallback).
    """
    global _llm_backends_cache, _llm_backends_cache_time

    now = time.time()
    if now > _llm_backends_cache_time + _cache_ttl:
        # Cache expired, refresh
        try:
            backends = await admin_client.get_llm_backends()
            if backends:
                _llm_backends_cache = backends
                _llm_backends_cache_time = now
                logger.info(f"LLM backends loaded from DB: {[b.get('model_name') for b in backends]}")
            else:
                logger.warning("No LLM backends found in database, using environment variable fallback")
        except Exception as e:
            logger.warning(f"Failed to load LLM backends from database: {e}")

    return _llm_backends_cache

async def kill_port(port: int, service_name: str = "service"):
    """Kill any process using the specified port (non-blocking)."""
    try:
        # Find process on port using lsof via asyncio subprocess
        proc = await asyncio.create_subprocess_exec(
            'lsof', '-ti', f':{port}',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()

        if proc.returncode == 0 and stdout.strip():
            pids = stdout.decode().strip().split('\n')
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    logger.info(f"Killed existing {service_name} process (PID {pid}) on port {port}")
                except ProcessLookupError:
                    pass  # Process already dead
            await asyncio.sleep(2)  # Non-blocking wait for port to be released
        else:
            logger.info(f"No existing process found on port {port}")
    except Exception as e:
        logger.warning(f"Error checking port {port}: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    global orchestrator_client, ollama_client, device_session_mgr, admin_client, gateway_config
    global orchestrator_circuit_breaker, global_rate_limiter
    global metric_client, ha_client

    # Kill any existing process on gateway port before starting
    gateway_port = int(os.getenv("GATEWAY_PORT", "8000"))
    await kill_port(gateway_port, "Gateway")

    # Startup
    logger.info("Starting Gateway service")

    # Initialize shared HTTP clients for reuse (performance optimization)
    metric_client = httpx.AsyncClient(timeout=5.0)  # For metric logging
    ha_client = httpx.AsyncClient(timeout=3.0, verify=False)  # For HA API calls
    logger.info("Shared HTTP clients initialized (metric_client, ha_client)")

    # Initialize admin config client for database-driven configuration
    admin_client = get_admin_client()
    logger.info("Admin config client initialized")

    # Fetch gateway configuration from database (with env var fallbacks)
    gateway_config = await admin_client.get_gateway_config()
    if gateway_config:
        logger.info(
            "gateway_config_loaded",
            orchestrator_url=gateway_config.get("orchestrator_url"),
            ollama_fallback_url=gateway_config.get("ollama_fallback_url"),
            intent_model=gateway_config.get("intent_model"),
            orchestrator_timeout=gateway_config.get("orchestrator_timeout_seconds")
        )
        # Use config values with env var fallbacks
        orchestrator_url = gateway_config.get("orchestrator_url", ORCHESTRATOR_URL)
        orchestrator_timeout = gateway_config.get("orchestrator_timeout_seconds", 60)
    else:
        logger.warning("Gateway config not available from database, using environment variables")
        orchestrator_url = ORCHESTRATOR_URL
        orchestrator_timeout = 60

    orchestrator_client = httpx.AsyncClient(
        base_url=orchestrator_url,
        timeout=float(orchestrator_timeout)
    )

    # Load LLM backends from database (with fallback to centralized system_settings)
    backends = await get_llm_backends()
    if backends:
        # Use first backend as primary Ollama URL
        primary_backend = backends[0]
        # Fetch centralized URL for fallback
        centralized_ollama_url = await admin_client.get_ollama_url()
        ollama_url = primary_backend.get("endpoint_url") or centralized_ollama_url
        logger.info(f"Using LLM backend from database: {primary_backend.get('model_name')} @ {ollama_url}")
    else:
        # Fall back to centralized system_settings
        ollama_url = await admin_client.get_ollama_url()
        logger.info(f"Using centralized Ollama URL from system_settings: {ollama_url}")

    ollama_client = OllamaClient(url=ollama_url)
    device_session_mgr = await get_device_session_manager()
    logger.info("Device session manager initialized")

    # Initialize circuit breaker for orchestrator calls
    if gateway_config:
        cb_failure_threshold = gateway_config.get("circuit_breaker_failure_threshold", 5)
        cb_recovery_timeout = gateway_config.get("circuit_breaker_recovery_timeout_seconds", 30)
    else:
        cb_failure_threshold = 5
        cb_recovery_timeout = 30

    orchestrator_circuit_breaker = CircuitBreaker(
        failure_threshold=cb_failure_threshold,
        recovery_timeout=cb_recovery_timeout
    )
    logger.info(
        "circuit_breaker_initialized",
        failure_threshold=cb_failure_threshold,
        recovery_timeout=cb_recovery_timeout
    )

    # Initialize rate limiter
    if gateway_config:
        rate_limit_rpm = gateway_config.get("rate_limit_requests_per_minute", 60)
    else:
        rate_limit_rpm = 60

    global_rate_limiter = TokenBucketRateLimiter(
        requests_per_minute=rate_limit_rpm,
        burst_multiplier=2.0
    )
    logger.info(
        "rate_limiter_initialized",
        requests_per_minute=rate_limit_rpm
    )

    # Check orchestrator health
    try:
        response = await orchestrator_client.get("/health")
        if response.status_code == 200:
            logger.info("Orchestrator is healthy")
        else:
            logger.warning(f"Orchestrator unhealthy: {response.status_code}")
    except Exception as e:
        logger.warning(f"Orchestrator not available: {e}")

    # Initialize LiveKit WebRTC if feature is enabled
    livekit_enabled = await is_feature_enabled("livekit_webrtc")
    if livekit_enabled and LIVEKIT_ROUTES_AVAILABLE and initialize_livekit_integration:
        try:
            await initialize_livekit_integration()
            logger.info("LiveKit WebRTC integration initialized")
        except Exception as e:
            logger.warning(f"LiveKit initialization failed: {e}")
    else:
        if not LIVEKIT_ROUTES_AVAILABLE:
            logger.info("LiveKit routes not available (missing dependencies)")
        elif not livekit_enabled:
            logger.info("LiveKit WebRTC disabled via feature flag")

    # Pre-load Music Assistant auth token from admin API
    global _ma_auth_token_cache
    try:
        ma_token = await _fetch_ma_auth_token()
        if ma_token:
            _ma_auth_token_cache = ma_token
            logger.info("ma_auth_token_loaded", source="admin_api")
        else:
            logger.info("ma_auth_token_not_configured")
    except Exception as e:
        logger.warning(f"Failed to pre-load MA auth token: {e}")

    yield

    # Shutdown
    logger.info("Shutting down Gateway service")

    # Shutdown LiveKit if initialized
    if LIVEKIT_ROUTES_AVAILABLE and shutdown_livekit_integration:
        try:
            await shutdown_livekit_integration()
            logger.info("LiveKit WebRTC integration shutdown")
        except Exception as e:
            logger.warning(f"LiveKit shutdown error: {e}")

    if device_session_mgr:
        await device_session_mgr.close()
    if orchestrator_client:
        await orchestrator_client.aclose()
    if ollama_client:
        await ollama_client.close()
    if admin_client:
        await admin_client.close()
    if metric_client:
        await metric_client.aclose()
    if ha_client:
        await ha_client.aclose()


# Room to assist_satellite entity mapping
# Home Assistant Voice PE device IDs:
# - 0a2296: Office (confirmed via user testing)
# - 0a4332: Master Bedroom (confirmed via user testing)
ROOM_TO_SATELLITE = {
    "office": "assist_satellite.home_assistant_voice_0a2296_assist_satellite",
    "master_bedroom": "assist_satellite.home_assistant_voice_0a4332_assist_satellite",
    "master bedroom": "assist_satellite.home_assistant_voice_0a4332_assist_satellite",
    "bedroom": "assist_satellite.home_assistant_voice_0a4332_assist_satellite",  # Alias
    # Add more mappings as devices are added
}


async def send_satellite_announcement(room: str, message: str) -> bool:
    """
    Send an announcement directly to a Wyoming satellite.
    This plays immediately without waiting for the main response.

    Args:
        room: Room name (e.g., "office")
        message: Text to announce

    Returns:
        True if successful, False otherwise
    """
    ha_url = os.getenv("HA_URL", "http://localhost:8123")
    ha_token = os.getenv("HA_TOKEN")

    if not ha_token:
        logger.warning("HA_TOKEN not set, cannot send satellite announcement")
        return False

    # Map room to satellite entity
    satellite_entity = ROOM_TO_SATELLITE.get(room.lower())
    if not satellite_entity:
        logger.warning(f"No satellite mapping for room: {room}")
        return False

    try:
        headers = {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}
        payload = {
            "entity_id": satellite_entity,
            "message": message,
            "preannounce": False  # Don't play chime before acknowledgment
        }

        url = f"{ha_url}/api/services/assist_satellite/announce"
        logger.info(f"Satellite announcement: calling {url} with entity {satellite_entity}")

        # Fire and forget - don't wait for completion
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            resp = await client.post(
                f"{ha_url}/api/services/assist_satellite/announce",
                headers=headers,
                json=payload
            )

            if resp.status_code == 200:
                logger.info(f"Satellite announcement sent to {satellite_entity}: {message}")
                return True
            else:
                logger.warning(f"Satellite announcement failed: {resp.status_code}")
                return False

    except Exception as e:
        logger.warning(f"Failed to send satellite announcement: {e}")
        return False


app = FastAPI(
    title="Athena Gateway",
    description="OpenAI-compatible API gateway for Project Athena",
    version="1.0.0",
    lifespan=lifespan
)

# Add request tracing middleware (generates X-Request-ID for all requests)
app.add_middleware(RequestTracingMiddleware, service_name="gateway")

# Register unified exception handlers
register_exception_handlers(app)

# Include LiveKit routes if available
if LIVEKIT_ROUTES_AVAILABLE and livekit_router:
    app.include_router(livekit_router)
    logger.info("LiveKit routes enabled")

# Request/Response models (OpenAI-compatible)
class ChatMessage(BaseModel):
    role: str = Field(..., description="Message role: system, user, or assistant")
    content: str = Field(..., description="Message content")
    name: Optional[str] = Field(None, description="Optional name")

class ChatCompletionRequest(BaseModel):
    model: str = Field(..., description="Model to use")
    messages: List[ChatMessage] = Field(..., description="Chat messages")
    temperature: float = Field(0.7, ge=0, le=2, description="Sampling temperature")
    top_p: float = Field(1.0, ge=0, le=1, description="Top-p sampling")
    n: int = Field(1, ge=1, le=10, description="Number of completions")
    stream: bool = Field(False, description="Stream response")
    stop: Optional[List[str]] = Field(None, description="Stop sequences")
    max_tokens: Optional[int] = Field(None, description="Max tokens to generate")
    presence_penalty: float = Field(0, ge=-2, le=2)
    frequency_penalty: float = Field(0, ge=-2, le=2)
    user: Optional[str] = Field(None, description="User identifier")

class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatChoice]
    usage: Dict[str, int]


# OpenAI Responses API models (newer format used by some clients like HA OpenAI Conversation Plus)
class ResponsesAPIRequest(BaseModel):
    """OpenAI Responses API request format."""
    model: str = Field(..., description="Model to use")
    input: Union[str, List[Dict[str, Any]]] = Field(..., description="User input (string or messages)")
    instructions: Optional[str] = Field(None, description="System instructions")
    stream: bool = Field(False, description="Stream response")
    temperature: float = Field(0.7, ge=0, le=2, description="Sampling temperature")
    max_output_tokens: Optional[int] = Field(None, description="Max tokens to generate")


class ResponsesAPIOutput(BaseModel):
    """Single output item in Responses API."""
    type: str = "message"
    id: str
    status: str = "completed"
    role: str = "assistant"
    content: List[Dict[str, Any]]


class ResponsesAPIResponse(BaseModel):
    """OpenAI Responses API response format."""
    id: str
    object: str = "response"
    created_at: int
    status: str = "completed"
    model: str
    output: List[ResponsesAPIOutput]
    output_text: Optional[str] = None  # Convenience field for HA
    usage: Dict[str, int]


# Home Assistant Conversation API models
class HAConversationRequest(BaseModel):
    """
    Home Assistant conversation request from Voice PE devices.

    This matches the format that Home Assistant sends when a voice device
    (Wyoming protocol) triggers a conversation.
    """
    text: str = Field(..., description="User's voice query transcribed to text")
    language: str = Field("en", description="Language code (default: en)")
    conversation_id: Optional[str] = Field(None, description="HA conversation ID (optional)")
    device_id: Optional[str] = Field(None, description="Voice PE device identifier (e.g., 'office', 'kitchen')")
    agent_id: Optional[str] = Field(None, description="HA agent ID")

class HAConversationResponse(BaseModel):
    """
    Home Assistant conversation response format.

    This is returned to Home Assistant which then synthesizes it to speech
    and plays it back through the Voice PE device.

    If continue_conversation is True, the Voice PE device will keep listening
    after TTS playback for a follow-up response from the user.
    """
    response: Dict[str, Any] = Field(..., description="Response structure")
    conversation_id: Optional[str] = Field(None, description="Session ID for conversation context")
    continue_conversation: bool = Field(False, description="If True, Voice PE keeps listening after response")

# API key validation (now optional/off by default)
async def validate_api_key(request: Request):
    """Validate API key only if explicitly set via env."""
    if not API_KEY or API_KEY == "dummy-key":
        return True  # Auth disabled

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing API key")

    token = auth_header.replace("Bearer ", "")
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return True

async def is_feature_enabled(feature_name: str) -> bool:
    """
    Check if feature is enabled via Admin API with caching.

    Args:
        feature_name: Name of feature flag to check (e.g., 'llm_based_routing')

    Returns:
        True if feature is enabled, False otherwise

    Note:
        Uses AdminConfigClient's built-in caching (60-second TTL).
        Reuses the shared HTTP client instead of creating new connections.
        If Admin API is unavailable, returns False (safe default).

    Performance:
        Optimized to reuse admin_client's HTTP connection pool instead of
        creating a new httpx.AsyncClient for every feature check.
        Saves ~30-50ms per call (TCP handshake + TLS overhead).
    """
    # Use AdminConfigClient's is_feature_enabled method which has proper caching
    # and reuses the shared HTTP connection pool
    if admin_client:
        return await admin_client.is_feature_enabled(feature_name)

    # Fallback: If admin_client not initialized yet, return False
    logger.warning("Admin client not initialized, feature flag check returning False")
    return False


async def get_feature_flag(flag_name: str, default: bool = False) -> bool:
    """
    Get feature flag value with local caching.

    This function provides an additional layer of caching on top of
    AdminConfigClient's caching, with support for instant cache invalidation
    via the /admin/invalidate-feature-cache endpoint.

    Args:
        flag_name: Name of feature flag to check
        default: Default value if flag unavailable

    Returns:
        True if feature is enabled, False otherwise
    """
    global _feature_flag_cache

    # Check local cache first
    if flag_name in _feature_flag_cache:
        cached_time, cached_value = _feature_flag_cache[flag_name]
        if time.time() - cached_time < _feature_flag_cache_ttl:
            return cached_value

    # Fetch from admin API
    try:
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
                        _feature_flag_cache[flag_name] = (time.time(), value)
                        return value
    except Exception as e:
        logger.warning(f"Feature flag fetch failed for {flag_name}: {e}")

    return default


async def _log_metric_to_db(
    timestamp: float,
    model: str,
    backend: str,
    latency_seconds: float,
    tokens: int,
    tokens_per_second: float,
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    zone: Optional[str] = None,
    intent: Optional[str] = None,
    source: Optional[str] = None
):
    """
    Log LLM performance metric to admin database (fire-and-forget).

    Args:
        timestamp: Unix timestamp of request start
        model: Model name used
        backend: Backend type (ollama, mlx, auto)
        latency_seconds: Total request latency
        tokens: Number of tokens generated
        tokens_per_second: Token generation speed
        request_id: Optional request ID
        session_id: Optional session ID
        user_id: Optional user ID
        zone: Optional zone/location
        intent: Optional intent classification
        source: Optional source service (gateway, orchestrator, etc.)

    Note:
        Failures are logged but don't raise exceptions to avoid
        impacting the main LLM request flow.

    Performance:
        Uses shared metric_client instead of creating new httpx.AsyncClient
        for every metric write. Saves ~10-50ms per call (TCP/TLS overhead).
    """
    try:
        metric_payload = {
            "timestamp": timestamp,
            "model": model,
            "backend": backend,
            "latency_seconds": latency_seconds,
            "tokens": tokens,
            "tokens_per_second": tokens_per_second,
            "request_id": request_id,
            "session_id": session_id,
            "user_id": user_id,
            "zone": zone,
            "intent": intent,
            "source": source
        }

        url = f"{ADMIN_API_URL}/api/llm-backends/metrics"

        # Use shared metric_client for connection reuse (performance optimization)
        if metric_client:
            response = await metric_client.post(url, json=metric_payload)
        else:
            # Fallback if client not initialized (shouldn't happen normally)
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=metric_payload)

        if response.status_code == 201:
            logger.info(
                "metric_logged_to_db",
                model=model,
                backend=backend,
                tokens_per_sec=round(tokens_per_second, 2)
            )
        else:
            logger.warning(
                "failed_to_log_metric",
                status_code=response.status_code,
                error=response.text[:200]
            )
    except Exception as e:
        logger.error(f"Metric logging error: {e}", exc_info=False)


async def classify_intent_llm(query: str) -> bool:
    """
    Use LLM to classify if query should route to orchestrator.

    Uses phi3:mini-q8 model for fast, accurate intent classification.
    Classifies queries into two categories:
    - athena: Home control, weather, sports, airports, local info (Baltimore context)
    - general: General knowledge, math, coding, explanations

    Args:
        query: User query to classify

    Returns:
        True if orchestrator should handle (athena), False for Ollama (general)

    Note:
        Falls back to keyword matching if LLM call fails.
        Target latency: 50-200ms
        Configuration is loaded from gateway_config (database) with fallbacks.
    """
    prompt = f"""Classify this query into ONE category:

Query: "{query}"

Categories:
- athena: Home control, weather, SPORTS (games/scores/schedules/teams), airports, local info (Baltimore context)
- general: General knowledge, math, coding, explanations

Examples of athena queries:
- "turn on the lights"
- "what's the weather?"
- "when do the Ravens play?" or "football schedule" (SPORTS - always athena)
- "BWI flight delays?"

Respond with ONLY the category name (athena or general)."""

    # Get configuration from database or use defaults
    # Use centralized Ollama URL from system_settings
    admin_client = get_admin_client()
    centralized_ollama_url = await admin_client.get_ollama_url()

    # Always use centralized Ollama URL from system_settings
    ollama_url = centralized_ollama_url

    if gateway_config:
        intent_model = gateway_config.get("intent_model", "phi3:mini")
        intent_temperature = gateway_config.get("intent_temperature", 0.1)
        intent_max_tokens = gateway_config.get("intent_max_tokens", 10)
        intent_timeout = gateway_config.get("intent_timeout_seconds", 5)
    else:
        intent_model = "phi3:mini"
        intent_temperature = 0.1
        intent_max_tokens = 10
        intent_timeout = 5

    try:
        async with httpx.AsyncClient(timeout=float(intent_timeout)) as client:
            response = await client.post(
                f"{ollama_url}/api/generate",
                json={
                    "model": intent_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": float(intent_temperature),
                        "num_predict": int(intent_max_tokens)
                    }
                }
            )
            response.raise_for_status()
            result = response.json()
            classification = result.get("response", "").strip().lower()

            is_athena = "athena" in classification
            logger.info(f"LLM classified '{query}' as {'athena' if is_athena else 'general'} (model={intent_model})")
            return is_athena

    except Exception as e:
        logger.error(f"LLM classification failed: {e}, falling back to keyword matching")
        # Fallback to keyword matching
        # Create a temporary ChatMessage for keyword matching
        from pydantic import BaseModel
        temp_messages = [ChatMessage(role="user", content=query)]
        return is_athena_query_keywords(temp_messages)


def is_athena_query_keywords(messages: List[ChatMessage]) -> bool:
    """
    Keyword-based classification (fast, 0ms overhead).

    Used as fallback when LLM is disabled or fails.
    Matches queries against predefined keyword patterns for:
    - Home automation control (lights, switches, climate)
    - Weather queries
    - Airport/flight information
    - Sports information (all major leagues and teams)
    - Location-specific queries (Baltimore context)
    - Recipes and cooking
    - Entertainment and events

    Args:
        messages: Chat messages list

    Returns:
        True if orchestrator should handle, False for Ollama
    """
    # Get the last user message
    last_user_msg = None
    for msg in reversed(messages):
        if msg.role == "user":
            last_user_msg = msg.content.lower()
            break

    if not last_user_msg:
        return False

    # Athena-specific patterns
    athena_patterns = [
        # Home control
        "turn on", "turn off", "set", "dim", "brighten",
        "lights", "switch", "temperature", "thermostat",
        # Weather
        "weather", "forecast", "rain", "snow", "temperature outside",
        # Airports/flights
        "airport", "flight", "delay", "departure", "arrival",
        "bwi", "dca", "iad", "phl", "jfk", "lga", "ewr",
        # Sports - General
        "game", "score", "team", "schedule", "match", "vs", "versus",
        "playoff", "championship", "tournament", "season", "league",
        # Sports - Types
        "football", "soccer", "basketball", "baseball", "hockey", "olympics",
        # Sports - Leagues
        "nfl", "nba", "mlb", "nhl", "mls", "ncaa", "fifa", "ufc", "pga",
        # NFL Teams
        "ravens", "steelers", "browns", "bengals", "cowboys", "eagles",
        "giants", "commanders", "packers", "bears", "vikings", "lions",
        "saints", "falcons", "panthers", "buccaneers", "49ers", "seahawks",
        "rams", "cardinals", "patriots", "bills", "dolphins", "jets",
        "chiefs", "broncos", "raiders", "chargers", "colts", "texans",
        "jaguars", "titans",
        # MLB Teams
        "orioles", "yankees", "red sox", "blue jays", "rays", "white sox",
        "guardians", "tigers", "royals", "twins", "astros", "angels",
        "athletics", "mariners", "rangers", "braves", "marlins", "mets",
        "phillies", "nationals", "cubs", "reds", "brewers", "pirates",
        "cardinals", "diamondbacks", "rockies", "dodgers", "padres", "giants",
        # NBA Teams
        "celtics", "nets", "knicks", "76ers", "raptors", "bulls", "cavaliers",
        "pistons", "pacers", "bucks", "hawks", "hornets", "heat", "magic",
        "wizards", "nuggets", "timberwolves", "thunder", "trail blazers",
        "jazz", "warriors", "clippers", "lakers", "suns", "kings",
        "mavericks", "rockets", "grizzlies", "pelicans", "spurs",
        # NHL Teams
        "bruins", "sabres", "red wings", "panthers", "canadiens", "senators",
        "lightning", "maple leafs", "hurricanes", "blue jackets", "devils",
        "islanders", "rangers", "flyers", "penguins", "capitals", "blackhawks",
        "avalanche", "stars", "wild", "predators", "blues", "jets",
        "ducks", "flames", "oilers", "kings", "sharks", "kraken", "canucks",
        "golden knights", "coyotes",
        # MLS Teams (Soccer)
        "atlanta united", "austin fc", "charlotte fc", "chicago fire", "fc cincinnati",
        "colorado rapids", "columbus crew", "dc united", "fc dallas", "houston dynamo",
        "la galaxy", "lafc", "inter miami", "minnesota united", "montreal", "nashville sc",
        "new england revolution", "new york red bulls", "new york city fc", "orlando city",
        "philadelphia union", "portland timbers", "real salt lake", "san jose earthquakes",
        "seattle sounders", "sporting kansas city", "toronto fc", "vancouver whitecaps",
        # Major International Soccer Teams
        "manchester united", "manchester city", "liverpool", "chelsea", "arsenal", "tottenham",
        "barcelona", "real madrid", "atletico madrid", "bayern munich", "borussia dortmund",
        "juventus", "ac milan", "inter milan", "psg", "paris saint-germain",
        # Location context
        "baltimore", "home", "office", "bedroom", "kitchen",
        # Recipes and cooking (RAG + web search)
        "recipe", "cook", "how to make", "ingredients", "cooking",
        # Dining and restaurants (RAG)
        "restaurant", "restaurants", "dining", "eat", "food", "cuisine",
        "pizza", "burger", "sushi", "chinese", "italian", "mexican", "indian",
        "thai", "japanese", "korean", "vietnamese", "mediterranean", "greek",
        "french", "spanish", "american", "seafood", "steakhouse", "bbq",
        "breakfast", "lunch", "dinner", "brunch", "cafe", "coffee",
        "bar", "pub", "brewery", "takeout", "delivery", "dine-in",
        "reservation", "menu", "vegetarian", "vegan", "gluten-free",
        "near me", "nearby", "best", "top rated", "popular", "recommend",
        # News and current events (RAG)
        "news", "headline", "breaking", "latest", "current events", "happening",
        "article", "report", "story", "press", "media", "journalism",
        # Entertainment and events (web search)
        "concert", "perform", "tour", "show", "event", "when does",
        "who is", "what is", "tell me about"
    ]

    return any(pattern in last_user_msg for pattern in athena_patterns)


async def is_athena_query(messages: List[ChatMessage]) -> bool:
    """
    Main routing decision function.

    Uses LLM or keywords based on feature flag configuration.
    Checks 'llm_based_routing' feature flag to determine routing method:
    - If enabled: Use LLM-based intent classification (more accurate, +50-200ms)
    - If disabled: Use keyword-based pattern matching (fast, 0ms overhead)

    Args:
        messages: Chat messages list

    Returns:
        True if orchestrator should handle, False for Ollama

    Note:
        Feature flag is cached for 60 seconds to avoid hitting Admin API on every request.
        LLM classification falls back to keyword matching if it fails.
    """
    # Get the last user message for LLM classification
    last_user_msg = None
    for msg in reversed(messages):
        if msg.role == "user":
            last_user_msg = msg.content
            break

    if not last_user_msg:
        return False

    # Check feature flag (cached for 60 seconds)
    use_llm = await is_feature_enabled("llm_based_routing")

    if use_llm:
        logger.info("Using LLM-based routing")
        return await classify_intent_llm(last_user_msg)
    else:
        logger.info("Using keyword-based routing")
        return is_athena_query_keywords(messages)


async def route_to_orchestrator(
    request: ChatCompletionRequest,
    device_id: Optional[str] = None,
    session_id: Optional[str] = None,
    return_session_id: bool = False
) -> Union[ChatCompletionResponse, tuple]:
    """
    Route request to Athena orchestrator.

    Args:
        request: OpenAI-compatible chat completion request
        device_id: Optional Voice PE device identifier for session management
        session_id: Optional session ID to continue conversation
        return_session_id: If True, returns tuple of (response, session_id)

    Returns:
        ChatCompletionResponse with orchestrator's answer, or tuple if return_session_id=True

    Note:
        Uses circuit breaker pattern to prevent cascade failures.
        If circuit is open, falls back to Ollama immediately.
    """
    # Check circuit breaker if enabled
    circuit_breaker_enabled = gateway_config.get("circuit_breaker_enabled", True) if gateway_config else True

    if circuit_breaker_enabled and orchestrator_circuit_breaker:
        can_proceed = await orchestrator_circuit_breaker.can_execute()
        if not can_proceed:
            logger.warning(
                "circuit_breaker_open",
                state=orchestrator_circuit_breaker.state.value,
                message="Falling back to Ollama due to circuit breaker"
            )
            # Fall back to Ollama immediately
            if return_session_id:
                fallback_response = await route_to_ollama(request)
                return fallback_response, f"session-{uuid.uuid4().hex[:8]}"
            return await route_to_ollama(request)

    try:
        # Extract user message
        user_message = ""
        for msg in request.messages:
            if msg.role == "user":
                user_message = msg.content

        # Call orchestrator with session support
        with request_duration.labels(endpoint="orchestrator").time():
            payload = {
                "query": user_message,
                "mode": "owner",  # Default to owner mode
                "room": device_id or "unknown",  # Use device_id as room if available
                "temperature": request.temperature,
                "model": MODEL_MAPPING.get(request.model, "phi3:mini")
            }

            # Include session_id if provided (for conversation context)
            if session_id:
                payload["session_id"] = session_id

            response = await orchestrator_client.post("/query", json=payload)
            response.raise_for_status()

        result = response.json()

        # Record success with circuit breaker
        if circuit_breaker_enabled and orchestrator_circuit_breaker:
            await orchestrator_circuit_breaker.record_success()

        # Format as OpenAI response
        chat_response = ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:8]}",
            created=int(time.time()),
            model=request.model,
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant",
                        content=result.get("answer", "I couldn't process that request.")
                    ),
                    finish_reason="stop"
                )
            ],
            usage={
                "prompt_tokens": len(user_message.split()),
                "completion_tokens": len(result.get("answer", "").split()),
                "total_tokens": len(user_message.split()) + len(result.get("answer", "").split())
            }
        )

        # Return session_id if requested (for HA conversation endpoint)
        if return_session_id:
            orchestrator_session_id = result.get("session_id", f"session-{uuid.uuid4().hex[:8]}")
            return chat_response, orchestrator_session_id

        return chat_response

    except httpx.HTTPStatusError as e:
        logger.error(f"Orchestrator error: {e}")
        # Record failure with circuit breaker
        if circuit_breaker_enabled and orchestrator_circuit_breaker:
            await orchestrator_circuit_breaker.record_failure()
        raise HTTPException(status_code=502, detail="Orchestrator error")
    except Exception as e:
        logger.error(f"Failed to route to orchestrator: {e}", exc_info=True)
        # Record failure with circuit breaker
        if circuit_breaker_enabled and orchestrator_circuit_breaker:
            await orchestrator_circuit_breaker.record_failure()
        # Fall back to Ollama
        if return_session_id:
            fallback_response = await route_to_ollama(request)
            return fallback_response, f"session-{uuid.uuid4().hex[:8]}"
        return await route_to_ollama(request)

async def route_to_ollama(
    request: ChatCompletionRequest,
    device_id: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None
) -> ChatCompletionResponse:
    """Route request directly to Ollama with metric logging."""
    start_time = time.time()

    try:
        # Map model name
        ollama_model = MODEL_MAPPING.get(request.model, request.model)

        # Convert messages format
        messages = [
            {"role": msg.role, "content": msg.content}
            for msg in request.messages
        ]

        # Call Ollama
        with request_duration.labels(endpoint="ollama").time():
            response_text = ""
            eval_count = 0
            async for chunk in ollama_client.chat(
                model=ollama_model,
                messages=messages,
                temperature=request.temperature,
                stream=False
            ):
                if chunk.get("done"):
                    response_text = chunk.get("message", {}).get("content", "")
                    eval_count = chunk.get("eval_count", 0)
                    break

        # Calculate metrics
        latency_seconds = time.time() - start_time
        tokens = eval_count or len(response_text.split())  # Fallback to word count
        tokens_per_second = tokens / latency_seconds if latency_seconds > 0 and tokens > 0 else 0

        # Log metrics to database (fire-and-forget)
        import asyncio
        asyncio.create_task(_log_metric_to_db(
            timestamp=start_time,
            model=ollama_model,
            backend="ollama",
            latency_seconds=latency_seconds,
            tokens=tokens,
            tokens_per_second=tokens_per_second,
            request_id=f"gateway-{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            user_id=user_id,
            zone=device_id,
            intent=None,
            source="gateway"
        ))

        # Format as OpenAI response
        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:8]}",
            created=int(time.time()),
            model=request.model,
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant",
                        content=response_text
                    ),
                    finish_reason="stop"
                )
            ],
            usage={
                "prompt_tokens": sum(len(msg.content.split()) for msg in request.messages),
                "completion_tokens": len(response_text.split()),
                "total_tokens": sum(len(msg.content.split()) for msg in request.messages) + len(response_text.split())
            }
        )

    except Exception as e:
        logger.error(f"Ollama error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="LLM service error")

async def stream_response(request: ChatCompletionRequest) -> AsyncIterator[str]:
    """Stream response from Ollama (orchestrator doesn't support streaming yet)."""
    try:
        # Only Ollama supports streaming for now
        ollama_model = MODEL_MAPPING.get(request.model, request.model)
        messages = [
            {"role": msg.role, "content": msg.content}
            for msg in request.messages
        ]

        # Stream from Ollama
        async for chunk in ollama_client.chat(
            model=ollama_model,
            messages=messages,
            temperature=request.temperature,
            stream=True
        ):
            if not chunk.get("done"):
                # Format as OpenAI streaming chunk
                data = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": request.model,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "content": chunk.get("message", {}).get("content", "")
                        },
                        "finish_reason": None
                    }]
                }
                yield f"data: {json.dumps(data)}\n\n"

        # Send final chunk
        yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error(f"Streaming error: {e}", exc_info=True)
        yield f"data: {{\"error\": \"{str(e)}\"}}\n\n"

async def stream_orchestrator_response(
    request: ChatCompletionRequest,
    device_id: Optional[str] = None
) -> AsyncIterator[str]:
    """Stream response from orchestrator's OpenAI-compatible endpoint."""
    try:
        # Forward streaming request to orchestrator's /v1/chat/completions endpoint
        # Note: room context passed via extra_body for orchestrator to use
        payload = {
            "model": request.model,
            "messages": [
                {"role": msg.role, "content": msg.content}
                for msg in request.messages
            ],
            "temperature": request.temperature,
            "stream": True,
            "extra_body": {"room": device_id or "unknown"}  # Pass room context
        }

        # Stream from orchestrator
        async with orchestrator_client.stream(
            "POST",
            "/v1/chat/completions",
            json=payload,
            timeout=120.0
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    # Forward the SSE line as-is
                    yield f"{line}\n\n"

    except Exception as e:
        logger.error(f"Orchestrator streaming error: {e}", exc_info=True)
        yield f"data: {{\"error\": \"{str(e)}\"}}\n\n"

@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    request: ChatCompletionRequest,
    _: bool = Depends(validate_api_key)
):
    """
    OpenAI-compatible chat completions endpoint.
    Routes to orchestrator for Athena queries, Ollama for general queries.

    Rate limiting and circuit breaker are applied based on gateway configuration.
    """
    request_counter.labels(endpoint="chat_completions", status="started").inc()

    # Check rate limiter if enabled
    rate_limit_enabled = gateway_config.get("rate_limit_enabled", True) if gateway_config else True
    if rate_limit_enabled and global_rate_limiter:
        if not await global_rate_limiter.acquire():
            request_counter.labels(endpoint="chat_completions", status="rate_limited").inc()
            logger.warning("rate_limit_exceeded", message="Request rejected due to rate limiting")
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Please try again later."
            )

    try:
        # Detect room from active Voice PE satellite for context
        # This allows commands like "turn off the lights" to know which room
        room = await _detect_room_from_active_satellite("unknown")
        logger.info(f"Detected room from satellite: {room}")

        # Route based on query type (LLM or keyword-based, controlled by feature flag)
        # Check routing BEFORE streaming decision
        route_to_orch = await is_athena_query(request.messages)

        # Handle streaming - ALWAYS use orchestrator for tool support
        if request.stream:
            request_counter.labels(endpoint="chat_completions", status="streaming").inc()
            if route_to_orch:
                logger.info("Streaming from orchestrator (keyword/LLM match)")
            else:
                logger.info("Streaming from orchestrator (default - tools always available)")

            # Always stream from orchestrator to ensure tools are available
            return StreamingResponse(
                stream_orchestrator_response(request, device_id=room),
                media_type="text/event-stream"
            )

        # ALWAYS route to orchestrator to ensure tools are available
        # The routing check is just for logging/optimization hints now
        if route_to_orch:
            logger.info("Routing to orchestrator (keyword/LLM match)")
        else:
            logger.info("Routing to orchestrator (default - tools always available)")

        # Always use orchestrator so tools are available, pass room for context
        response = await route_to_orchestrator(request, device_id=room)

        request_counter.labels(endpoint="chat_completions", status="success").inc()
        return response

    except HTTPException:
        request_counter.labels(endpoint="chat_completions", status="error").inc()
        raise
    except Exception as e:
        request_counter.labels(endpoint="chat_completions", status="error").inc()
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/v1/responses")
async def responses_api(
    request: ResponsesAPIRequest,
    raw_request: Request,
    _: bool = Depends(validate_api_key)
):
    """
    OpenAI Responses API endpoint (newer format).

    Converts to Chat Completions format and routes to orchestrator.
    Used by some clients like HA OpenAI Conversation Plus.
    """
    request_counter.labels(endpoint="responses_api", status="started").inc()

    # Debug: Log raw request body to see exactly what HA sends
    try:
        body = await raw_request.body()
        import json as json_module
        raw_data = json_module.loads(body.decode())
        logger.info(f"Responses API RAW request: stream={raw_data.get('stream')}, keys={list(raw_data.keys())}")
    except Exception as e:
        logger.warning(f"Could not log raw request: {e}")

    # Debug: Log the incoming request to see if streaming is enabled
    logger.info(f"Responses API request - model: {request.model}, stream: {request.stream}, temp: {request.temperature}")

    try:
        # Convert Responses API format to Chat Completions format
        messages = []

        # Add system instructions if provided
        if request.instructions:
            messages.append(ChatMessage(role="system", content=request.instructions))

        # Handle input - can be string or list of messages
        if isinstance(request.input, str):
            messages.append(ChatMessage(role="user", content=request.input))
        elif isinstance(request.input, list):
            # Input is a list of message-like objects
            for item in request.input:
                if isinstance(item, dict):
                    role = item.get("role", "user")
                    content = item.get("content", "")
                    if isinstance(content, list):
                        # Handle content array format
                        text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                        content = " ".join(text_parts)
                    messages.append(ChatMessage(role=role, content=content))

        # Create ChatCompletionRequest
        chat_request = ChatCompletionRequest(
            model=request.model,
            messages=messages,
            temperature=request.temperature,
            stream=request.stream,
            max_tokens=request.max_output_tokens
        )

        # Detect room
        room = await _detect_room_from_active_satellite("unknown")
        logger.info(f"Responses API - detected room: {room}")

        # Handle streaming
        if request.stream:
            request_counter.labels(endpoint="responses_api", status="streaming").inc()
            # For streaming, we need to convert the SSE format
            return StreamingResponse(
                stream_responses_api(chat_request, room),
                media_type="text/event-stream"
            )

        # Non-streaming: route to orchestrator
        response = await route_to_orchestrator(chat_request, device_id=room)

        # Convert ChatCompletionResponse to ResponsesAPIResponse
        response_text = ""
        if response.choices:
            response_text = response.choices[0].message.content or ""

        responses_response = ResponsesAPIResponse(
            id=f"resp_{response.id}",
            object="response",
            created_at=response.created,
            status="completed",
            model=response.model,
            output=[
                ResponsesAPIOutput(
                    type="message",
                    id=f"msg_{uuid.uuid4().hex[:8]}",
                    status="completed",
                    role="assistant",
                    content=[{"type": "output_text", "text": response_text}]
                )
            ],
            output_text=response_text,
            usage=response.usage
        )

        request_counter.labels(endpoint="responses_api", status="success").inc()
        return responses_response

    except HTTPException:
        request_counter.labels(endpoint="responses_api", status="error").inc()
        raise
    except Exception as e:
        request_counter.labels(endpoint="responses_api", status="error").inc()
        logger.error(f"Responses API error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


async def stream_responses_api(request: ChatCompletionRequest, device_id: str) -> AsyncIterator[str]:
    """Stream responses in Responses API format.

    OpenAI Responses API streaming requires specific events in order:
    1. response.created - Initial response creation
    2. response.output_item.added - Output item (message) being added
    3. response.content_part.added - Content part being added
    4. response.output_text.delta - Text deltas as they arrive
    5. response.output_text.done - Text generation complete
    6. response.content_part.done - Content part complete
    7. response.output_item.done - Output item complete
    8. response.done - Response complete
    """
    import uuid
    response_id = f"resp_{uuid.uuid4().hex[:24]}"
    item_id = f"item_{uuid.uuid4().hex[:16]}"
    content_idx = 0
    full_text = ""

    try:
        # 1. Send response.created event first (REQUIRED by OpenAI SDK)
        created_event = {
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "in_progress",
                "output": []
            }
        }
        yield f"data: {json.dumps(created_event)}\n\n"

        # 2. Send output item added event
        item_added_event = {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "content": []
            }
        }
        yield f"data: {json.dumps(item_added_event)}\n\n"

        # 3. Send content part added event
        content_added_event = {
            "type": "response.content_part.added",
            "item_id": item_id,
            "output_index": 0,
            "content_index": content_idx,
            "part": {
                "type": "output_text",
                "text": ""
            }
        }
        yield f"data: {json.dumps(content_added_event)}\n\n"

        # 4. TRUE PARALLEL: Start orchestrator request FIRST (background task),
        # then send acknowledgment while orchestrator is processing
        import random
        import time as time_module

        ack_start_time = time_module.time()
        logger.info(f"ACK_TIMING: Starting parallel flow at {ack_start_time}")

        # Create queue to collect orchestrator chunks
        result_queue = asyncio.Queue()

        async def collect_orchestrator_chunks():
            """Background task to collect orchestrator chunks into queue."""
            try:
                logger.info(f"ACK_TIMING: Orchestrator task STARTED at {time_module.time() - ack_start_time:.3f}s")
                first_chunk = True
                async for chunk in stream_orchestrator_response(request, device_id=device_id):
                    if first_chunk:
                        logger.info(f"ACK_TIMING: First orchestrator chunk at {time_module.time() - ack_start_time:.3f}s")
                        first_chunk = False
                    await result_queue.put(chunk)
            except Exception as e:
                logger.error(f"Orchestrator stream error: {e}")
                await result_queue.put(f"ERROR:{e}")
            finally:
                await result_queue.put(None)  # Sentinel to signal end

        # START ORCHESTRATOR IMMEDIATELY (runs in background while we send acknowledgment)
        orchestrator_task = asyncio.create_task(collect_orchestrator_chunks())

        # NOW generate and send acknowledgment (orchestrator HTTP request already in flight)
        query_text = ""
        if request.messages:
            for msg in reversed(request.messages):
                if msg.role == "user":
                    query_text = msg.content.lower() if isinstance(msg.content, str) else ""
                    break

        # Context-aware acknowledgments - end with period for TTS sentence boundary
        ack_text = None
        if any(w in query_text for w in ["weather", "temperature", "forecast", "rain"]):
            ack_text = random.choice(["Checking the weather.", "Looking up the forecast."])
        elif any(w in query_text for w in ["restaurant", "food", "eat", "dining"]):
            cuisines = ["italian", "mexican", "chinese", "japanese", "thai", "indian", "greek",
                       "french", "korean", "vietnamese", "jamaican", "american", "sushi", "pizza", "cajun"]
            for cuisine in cuisines:
                if cuisine in query_text:
                    ack_text = f"Looking up {cuisine} restaurants."
                    break
            if not ack_text:
                ack_text = random.choice(["Finding restaurants.", "Looking up dining options."])
        elif any(w in query_text for w in ["score", "game", "sports", "ravens", "orioles"]):
            ack_text = random.choice(["Checking the scores.", "Looking up the game."])
        elif any(w in query_text for w in ["flight", "airport", "plane"]):
            ack_text = random.choice(["Checking flight status.", "Looking up flights."])
        elif any(w in query_text for w in ["news", "headline"]):
            ack_text = random.choice(["Checking the news.", "Looking up headlines."])
        elif any(w in query_text for w in ["stock", "market", "price"]):
            ack_text = random.choice(["Checking the markets.", "Looking up prices."])
        elif any(w in query_text for w in ["recipe", "cook", "make"]):
            ack_text = random.choice(["Looking up recipes.", "Finding that recipe."])
        elif any(w in query_text for w in ["light", "turn on", "turn off", "switch"]):
            ack_text = random.choice(["Right away.", "On it."])
        else:
            generic_acks = [
                "One moment.",
                "Let me check.",
                "Looking into it.",
                "Just a moment.",
                "Checking now.",
            ]
            ack_text = random.choice(generic_acks)

        # NOTE: Satellite announcement DISABLED - satellite can't play audio while in "processing" state
        # (it's waiting for our response). assist_satellite.announce gets ignored when satellite is busy.
        # The acknowledgment text is still generated but not sent.
        logger.info(f"ACK_TIMING: Skipping satellite announcement (satellite busy in processing state) - would have said: '{ack_text}'")

        # 5. Consume orchestrator results from queue (chunks arrive as orchestrator streams them)
        while True:
            chunk = await result_queue.get()
            if chunk is None:
                break  # Sentinel - orchestrator stream finished
            if isinstance(chunk, str) and chunk.startswith("ERROR:"):
                logger.error(f"Orchestrator error in parallel stream: {chunk}")
                break
            if chunk.startswith("data: "):
                data = chunk[6:].strip()
                if data == "[DONE]":
                    # Don't emit [DONE] yet - we'll send proper completion events
                    pass
                else:
                    try:
                        parsed = json.loads(data)
                        delta = parsed.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            full_text += content
                            delta_event = {
                                "type": "response.output_text.delta",
                                "item_id": item_id,
                                "output_index": 0,
                                "content_index": content_idx,
                                "delta": content
                            }
                            yield f"data: {json.dumps(delta_event)}\n\n"
                    except json.JSONDecodeError:
                        pass  # Skip malformed chunks

        # Ensure background task is complete
        await orchestrator_task

        # 5. Send output_text.done event
        text_done_event = {
            "type": "response.output_text.done",
            "item_id": item_id,
            "output_index": 0,
            "content_index": content_idx,
            "text": full_text
        }
        yield f"data: {json.dumps(text_done_event)}\n\n"

        # 6. Send content_part.done event
        content_done_event = {
            "type": "response.content_part.done",
            "item_id": item_id,
            "output_index": 0,
            "content_index": content_idx,
            "part": {
                "type": "output_text",
                "text": full_text
            }
        }
        yield f"data: {json.dumps(content_done_event)}\n\n"

        # 7. Send output_item.done event
        item_done_event = {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": full_text}]
            }
        }
        yield f"data: {json.dumps(item_done_event)}\n\n"

        # 8. Send response.done event (final event)
        done_event = {
            "type": "response.done",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "completed",
                "output": [{
                    "id": item_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": full_text}]
                }]
            }
        }
        yield f"data: {json.dumps(done_event)}\n\n"

    except Exception as e:
        logger.error(f"Responses API streaming error: {e}", exc_info=True)
        # Send error event
        error_event = {
            "type": "error",
            "error": {"message": str(e), "type": "server_error"}
        }
        yield f"data: {json.dumps(error_event)}\n\n"


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    health = {
        "status": "healthy",
        "service": "gateway",
        "version": "1.0.0"
    }

    # Check orchestrator
    try:
        response = await orchestrator_client.get("/health")
        health["orchestrator"] = response.status_code == 200
    except:
        health["orchestrator"] = False

    # Check Ollama
    try:
        models = await ollama_client.list_models()
        health["ollama"] = len(models.get("models", [])) > 0
    except:
        health["ollama"] = False

    # Circuit breaker status
    if orchestrator_circuit_breaker:
        health["circuit_breaker"] = orchestrator_circuit_breaker.get_status()
    else:
        health["circuit_breaker"] = {"enabled": False}

    # Rate limiter status
    if global_rate_limiter:
        health["rate_limiter"] = global_rate_limiter.get_status()
    else:
        health["rate_limiter"] = {"enabled": False}

    # Overall health
    if not health["orchestrator"] and not health["ollama"]:
        health["status"] = "unhealthy"
    elif not health["orchestrator"] or not health["ollama"]:
        health["status"] = "degraded"
    # Check if circuit breaker is open
    if orchestrator_circuit_breaker and orchestrator_circuit_breaker.state == CircuitState.OPEN:
        health["status"] = "degraded"

    return health

@app.get("/health/live")
async def liveness_probe():
    """
    Kubernetes liveness probe.

    Returns healthy if the process is running and responsive.
    K8s will restart the pod if this fails.
    """
    return {
        "status": "ok",
        "service": "gateway"
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

    # Check orchestrator
    try:
        response = await orchestrator_client.get("/health")
        components["orchestrator"] = response.status_code == 200
    except:
        components["orchestrator"] = False
        ready = False

    # Check Ollama (optional - degraded if down)
    try:
        models = await ollama_client.list_models()
        components["ollama"] = len(models.get("models", [])) > 0
    except:
        components["ollama"] = False
        # Ollama is optional, don't mark not ready

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
        "service": "gateway"
    }


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(content=generate_latest(), media_type="text/plain")


@app.get("/config")
async def get_config():
    """
    Get current gateway configuration.

    Returns the active configuration loaded from the database,
    or indicates that environment variable fallbacks are being used.
    Useful for debugging and verifying configuration changes.
    """
    # Always fetch centralized Ollama URL from system_settings
    admin_client = get_admin_client()
    centralized_ollama_url = await admin_client.get_ollama_url()

    if gateway_config:
        return {
            "source": "database",
            "config": {
                "orchestrator_url": gateway_config.get("orchestrator_url"),
                "ollama_url": centralized_ollama_url,  # Always from system_settings
                "intent_model": gateway_config.get("intent_model"),
                "intent_temperature": gateway_config.get("intent_temperature"),
                "intent_max_tokens": gateway_config.get("intent_max_tokens"),
                "intent_timeout_seconds": gateway_config.get("intent_timeout_seconds"),
                "orchestrator_timeout_seconds": gateway_config.get("orchestrator_timeout_seconds"),
                "session_timeout_seconds": gateway_config.get("session_timeout_seconds"),
                "session_max_age_seconds": gateway_config.get("session_max_age_seconds"),
                "cache_ttl_seconds": gateway_config.get("cache_ttl_seconds"),
                "rate_limit_enabled": gateway_config.get("rate_limit_enabled"),
                "rate_limit_requests_per_minute": gateway_config.get("rate_limit_requests_per_minute"),
                "circuit_breaker_enabled": gateway_config.get("circuit_breaker_enabled"),
                "circuit_breaker_failure_threshold": gateway_config.get("circuit_breaker_failure_threshold"),
                "circuit_breaker_recovery_timeout_seconds": gateway_config.get("circuit_breaker_recovery_timeout_seconds"),
            },
            "updated_at": gateway_config.get("updated_at")
        }
    else:
        return {
            "source": "fallback",
            "config": {
                "orchestrator_url": ORCHESTRATOR_URL,
                "ollama_url": centralized_ollama_url,
                "intent_model": "phi3:mini",
                "intent_temperature": 0.1,
                "intent_max_tokens": 10,
                "intent_timeout_seconds": 5,
                "orchestrator_timeout_seconds": 60,
            },
            "note": "Gateway config not in database, using defaults with centralized Ollama URL"
        }


@app.post("/config/refresh")
async def refresh_config():
    """
    Refresh gateway configuration from database.

    Forces a reload of the configuration cache.
    Also updates circuit breaker and rate limiter configurations.
    Useful after making changes in the admin UI.
    """
    global gateway_config

    if admin_client:
        admin_client.invalidate_gateway_config_cache()
        gateway_config = await admin_client.get_gateway_config()

        if gateway_config:
            # Update circuit breaker config
            if orchestrator_circuit_breaker:
                orchestrator_circuit_breaker.update_config(
                    failure_threshold=gateway_config.get("circuit_breaker_failure_threshold", 5),
                    recovery_timeout=gateway_config.get("circuit_breaker_recovery_timeout_seconds", 30)
                )

            # Update rate limiter config
            if global_rate_limiter:
                global_rate_limiter.update_config(
                    requests_per_minute=gateway_config.get("rate_limit_requests_per_minute", 60)
                )

            return {
                "status": "refreshed",
                "message": "Gateway configuration reloaded from database",
                "orchestrator_url": gateway_config.get("orchestrator_url"),
                "intent_model": gateway_config.get("intent_model"),
                "circuit_breaker_updated": orchestrator_circuit_breaker is not None,
                "rate_limiter_updated": global_rate_limiter is not None
            }
        else:
            return {
                "status": "fallback",
                "message": "Database configuration not available, using environment variables"
            }
    else:
        return {
            "status": "error",
            "message": "Admin client not initialized"
        }


@app.post("/admin/invalidate-feature-cache")
async def invalidate_feature_cache(
    request: Request,
    flags: Optional[List[str]] = None
):
    """
    Invalidate feature flag cache. Called by Admin backend on flag changes.

    This endpoint enables instant propagation of feature flag changes from
    the Admin UI to the Gateway without waiting for cache TTL expiration.

    Args:
        request: FastAPI request object
        flags: Optional list of specific flag names to invalidate.
               If None, invalidates all cached flags.

    Returns:
        dict with status and invalidated flags/count
    """
    global _feature_flag_cache

    client_host = request.client.host if request.client else "unknown"

    if flags:
        # Invalidate specific flags
        invalidated = []
        for flag_name in flags:
            if flag_name in _feature_flag_cache:
                del _feature_flag_cache[flag_name]
                invalidated.append(flag_name)
        logger.info("feature_cache_invalidated", flags=invalidated, source=client_host)
        return {"status": "ok", "invalidated": invalidated}
    else:
        # Invalidate all
        count = len(_feature_flag_cache)
        _feature_flag_cache.clear()
        logger.info("feature_cache_cleared", count=count, source=client_host)
        return {"status": "ok", "invalidated_count": count}


@app.get("/debug/feature-flags")
async def debug_feature_flags():
    """
    Debug endpoint to view current feature flag cache state.

    Returns:
        dict with current cache state and TTL info
    """
    now = time.time()
    flags_state = {}

    for flag_name, (cached_time, value) in _feature_flag_cache.items():
        age_seconds = now - cached_time
        flags_state[flag_name] = {
            "value": value,
            "cached_at": cached_time,
            "age_seconds": round(age_seconds, 2),
            "expires_in_seconds": round(max(0, _feature_flag_cache_ttl - age_seconds), 2)
        }

    return {
        "cache_ttl_seconds": _feature_flag_cache_ttl,
        "cached_flags": flags_state,
        "total_cached": len(_feature_flag_cache)
    }


@app.get("/v1/models")
async def list_models():
    """List available models (OpenAI-compatible) - returns actual available LLM backends."""
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
        # Fallback to default models if API fails
        return {
            "object": "list",
            "data": [
                {"id": "gpt-3.5-turbo", "object": "model", "owned_by": "athena"},
                {"id": "gpt-4", "object": "model", "owned_by": "athena"}
            ]
        }

async def _detect_room_from_active_satellite(device_id: str) -> str:
    """
    Detect which room the conversation is coming from by checking active Voice PE satellites.

    Since HA doesn't pass device_id in conversation requests, we query HA's assist_satellite
    entities to find which one is currently active (not idle) and extract the room from its
    friendly_name.

    Args:
        device_id: The device_id from the request (usually "unknown")

    Returns:
        Room name (e.g., "office", "master_bedroom") or "office" as default

    Performance:
        - Uses shared ha_client instead of creating new httpx.AsyncClient for every call.
        - When ha_room_detection_cache is enabled, caches room for 3 seconds.
        - Saves ~100-200ms per request during continued conversations.
    """
    import re

    # If device_id is already a valid room, use it
    known_rooms = ["office", "kitchen", "living_room", "master_bedroom", "bedroom", "dining_room"]
    if device_id.lower() in known_rooms:
        return device_id.lower()

    # Check if caching is enabled via feature flag
    cache_enabled = await get_feature_flag("ha_room_detection_cache", default=False)

    if cache_enabled:
        # Try cache first
        cached_room = _get_cached_room(device_id)
        if cached_room:
            logger.debug(f"Room cache hit for {device_id}: {cached_room}")
            return cached_room

    # Query HA for active satellite
    try:
        ha_url = os.getenv("HA_URL", "http://localhost:8123")
        ha_token = os.getenv("HA_TOKEN")

        if not ha_token:
            logger.warning("HA_TOKEN not set, cannot detect room from satellite")
            return "office"  # Default

        headers = {"Authorization": f"Bearer {ha_token}"}

        # Use shared ha_client for connection reuse (performance optimization)
        if ha_client:
            resp = await ha_client.get(f"{ha_url}/api/states", headers=headers)
        else:
            # Fallback if client not initialized
            async with httpx.AsyncClient(timeout=3.0, verify=False) as client:
                resp = await client.get(f"{ha_url}/api/states", headers=headers)

        if resp.status_code != 200:
            logger.warning(f"Failed to query HA states: {resp.status_code}")
            return "office"

        states = resp.json()

        # Collect all assist_satellite entities
        satellites = []
        for state in states:
            entity_id = state.get("entity_id", "")
            if "assist_satellite" in entity_id:
                current_state = state.get("state", "idle")
                friendly_name = state.get("attributes", {}).get("friendly_name", "")
                last_changed = state.get("last_changed", "")
                satellites.append({
                    "entity_id": entity_id,
                    "state": current_state,
                    "friendly_name": friendly_name,
                    "last_changed": last_changed
                })

        # First pass: Look for any currently active (not idle) satellite
        for sat in satellites:
            if sat["state"] != "idle":
                match = re.search(r"Voice\s*-\s*(.+?)\s*(Assist|$)", sat["friendly_name"], re.IGNORECASE)
                if match:
                    room_name = match.group(1).strip().lower().replace(" ", "_")
                    logger.info(f"Detected active satellite in room: {room_name} (state: {sat['state']})")
                    if cache_enabled:
                        _set_cached_room(device_id, room_name)
                    return room_name

        # Second pass: No active satellite - check for recently changed (within 10 seconds)
        # This handles the race condition where satellite went back to idle
        now = datetime.now(timezone.utc)
        recent_threshold_seconds = 10

        recently_changed = []
        for sat in satellites:
            try:
                # Parse ISO timestamp
                last_changed_str = sat["last_changed"]
                if last_changed_str:
                    last_changed_dt = datetime.fromisoformat(last_changed_str.replace("Z", "+00:00"))
                    age_seconds = (now - last_changed_dt).total_seconds()
                    if age_seconds < recent_threshold_seconds:
                        recently_changed.append((sat, age_seconds))
            except Exception as e:
                logger.debug(f"Error parsing last_changed for {sat['entity_id']}: {e}")

        # If we found recently changed satellites, use the most recent one
        if recently_changed:
            # Sort by age (most recent first)
            recently_changed.sort(key=lambda x: x[1])
            sat, age = recently_changed[0]
            match = re.search(r"Voice\s*-\s*(.+?)\s*(Assist|$)", sat["friendly_name"], re.IGNORECASE)
            if match:
                room_name = match.group(1).strip().lower().replace(" ", "_")
                logger.info(f"Detected recently active satellite in room: {room_name} (changed {age:.1f}s ago)")
                if cache_enabled:
                    _set_cached_room(device_id, room_name)
                return room_name

        # No active satellite found - might be a race condition or satellite already went idle
        # Fall back to most recently used or default
        logger.info("No active or recently changed satellite found, defaulting to office")
        return "office"

    except Exception as e:
        logger.warning(f"Error detecting room from satellite: {e}")
        return "office"


# =============================================================================
# Session Warmup for Wake Word Detection
# =============================================================================


async def _warmup_session(device_id: str):
    """
    Background task to warm session cache on wake word detection.

    Pre-fetches session data before the actual query arrives, hiding the
    session lookup latency behind STT processing time.
    """
    try:
        # Get session ID for device
        session_id = await device_session_mgr.get_session_for_device(device_id)

        if session_id:
            # Pre-fetch from orchestrator session manager to warm cache
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.get(f"{ORCHESTRATOR_URL}/session/{session_id}/warmup")
                logger.debug(f"Session warmed for device {device_id}")
        else:
            # No existing session - nothing to warm
            logger.debug(f"No session to warm for device {device_id}")
    except Exception as e:
        logger.warning(f"Session warmup failed for {device_id}: {e}")


@app.post("/ha/wake-word-detected")
async def ha_wake_word_detected(request: dict) -> dict:
    """
    Handle wake word detection event from Home Assistant.

    Called by HA automation when a Voice PE satellite detects the wake word.
    Pre-fetches session data to warm cache before the actual query arrives.

    This endpoint should be called immediately when wake word is detected,
    allowing session data to be fetched in parallel with STT processing.

    Args:
        request: Dict with device_id from HA automation

    Returns:
        Status dict indicating warmup state
    """
    warmup_enabled = await get_feature_flag("ha_session_warmup", default=False)

    if not warmup_enabled:
        return {"status": "disabled", "message": "Session warmup feature flag is disabled"}

    device_id = request.get("device_id")
    if not device_id:
        return {"status": "error", "message": "No device_id provided"}

    # Pre-fetch session in background (non-blocking)
    asyncio.create_task(_warmup_session(device_id))

    logger.info("wake_word_detected_warmup_started", device_id=device_id)
    return {"status": "warming", "device_id": device_id}


@app.post("/ha/conversation", response_model=HAConversationResponse)
async def ha_conversation(request: HAConversationRequest):
    """
    Home Assistant conversation endpoint for Voice PE devices.

    This endpoint receives voice queries from Home Assistant's conversation integration
    (Wyoming protocol). It manages device-to-session mapping to maintain conversation
    context across multiple interactions with the same Voice PE device.

    Flow:
    1. Voice PE device captures wake word + voice input
    2. Wyoming protocol sends audio to HA
    3. HA transcribes to text and calls this endpoint
    4. Gateway gets/creates session for device
    5. Routes to orchestrator with session_id for context
    6. Updates device session mapping
    7. Returns response to HA
    8. HA synthesizes to speech and plays on Voice PE device

    Args:
        request: HAConversationRequest with text, device_id, etc.

    Returns:
        HAConversationResponse with answer and session_id
    """
    request_counter.labels(endpoint="ha_conversation", status="started").inc()

    try:
        device_id = request.device_id or "unknown"

        # Check if parallel initialization is enabled
        parallel_enabled = await get_feature_flag("ha_parallel_init", default=False)

        if parallel_enabled:
            # Run room detection and session lookup in parallel
            room_task = _detect_room_from_active_satellite(device_id)
            session_task = device_session_mgr.get_session_for_device(device_id)

            room, existing_session_id = await asyncio.gather(room_task, session_task)
            logger.debug(f"Parallel init completed: room={room}, session={existing_session_id}")
        else:
            # Original sequential logic
            room = await _detect_room_from_active_satellite(device_id)
            existing_session_id = await device_session_mgr.get_session_for_device(device_id)

        logger.info(
            f"HA conversation request from device: {device_id}, "
            f"detected room: {room}, query: {request.text}"
        )

        if existing_session_id:
            logger.info(f"Using existing session {existing_session_id} for device {device_id}")
        else:
            logger.info(f"Creating new session for device {device_id}")

        # Check if simple command fast-path is enabled
        fastpath_enabled = await get_feature_flag("ha_simple_command_fastpath", default=False)

        if fastpath_enabled:
            # Try to detect and execute simple command
            simple_cmd = await detect_simple_command(request.text)

            if simple_cmd:
                command_type, params = simple_cmd
                logger.info(f"Simple command detected: {command_type}, params: {params}")

                ha_url = os.getenv("HA_URL", "http://localhost:8123")
                ha_token = os.getenv("HA_TOKEN", "")

                response_text = await execute_simple_command(
                    command_type, params, ha_client, ha_url, ha_token
                )

                if response_text:
                    # Return fast-path response
                    request_counter.labels(endpoint="ha_conversation", status="fastpath").inc()
                    logger.info(f"Fast-path response: {response_text}")

                    return HAConversationResponse(
                        response=HAResponseContent(
                            speech=HASpeechContent(
                                plain=HAPlainSpeech(speech=response_text)
                            )
                        ),
                        conversation_id=request.conversation_id or "fastpath",
                        continue_conversation=False
                    )
                # If execution failed, fall through to orchestrator
                logger.info("Fast-path execution failed, falling back to orchestrator")

        # Check if intent pre-routing is enabled
        prerouting_enabled = await get_feature_flag("ha_intent_prerouting", default=False)

        if prerouting_enabled:
            # Classify intent using lightweight model
            intent = await classify_intent(request.text)
            logger.info(f"Pre-routed intent classification: {intent}")

            if intent == "SIMPLE":
                # Handle directly with fast LLM (greetings, time, chitchat)
                response_text = await handle_simple_intent(request.text)

                if response_text:
                    request_counter.labels(endpoint="ha_conversation", status="prerouted_simple").inc()
                    logger.info(f"Pre-routed SIMPLE response: {response_text[:50]}...")

                    return HAConversationResponse(
                        response=HAResponseContent(
                            speech=HASpeechContent(
                                plain=HAPlainSpeech(speech=response_text)
                            )
                        ),
                        conversation_id=request.conversation_id or "prerouted",
                        continue_conversation=False
                    )

            elif intent == "HOME":
                # Try simple command handler for device control
                simple_cmd = await detect_simple_command(request.text)
                if simple_cmd:
                    command_type, params = simple_cmd
                    ha_url = os.getenv("HA_URL", "http://localhost:8123")
                    ha_token = os.getenv("HA_TOKEN", "")

                    response_text = await execute_simple_command(
                        command_type, params, ha_client, ha_url, ha_token
                    )

                    if response_text:
                        request_counter.labels(endpoint="ha_conversation", status="prerouted_home").inc()
                        logger.info(f"Pre-routed HOME response: {response_text}")

                        return HAConversationResponse(
                            response=HAResponseContent(
                                speech=HASpeechContent(
                                    plain=HAPlainSpeech(speech=response_text)
                                )
                            ),
                            conversation_id=request.conversation_id or "prerouted",
                            continue_conversation=False
                        )
                # Fall through to orchestrator if HOME command not handled

            # COMPLEX intent falls through to full orchestrator

        # Create OpenAI-compatible request for routing
        chat_request = ChatCompletionRequest(
            model="gpt-4",  # Default model
            messages=[
                ChatMessage(role="user", content=request.text)
            ],
            temperature=0.7
        )

        # Route to orchestrator with room (mapped from device) and session info
        with request_duration.labels(endpoint="ha_conversation").time():
            chat_response, orchestrator_session_id = await route_to_orchestrator(
                request=chat_request,
                device_id=room,  # Use mapped room name for audio routing
                session_id=existing_session_id,
                return_session_id=True
            )

        # Extract answer from chat response
        answer = chat_response.choices[0].message.content if chat_response.choices else "I couldn't process that request."

        # Update device session mapping with the orchestrator's session_id
        await device_session_mgr.update_session_for_device(device_id, orchestrator_session_id)
        logger.info(f"Updated session mapping: device {device_id} → session {orchestrator_session_id}")

        # Rule: If the response contains a question mark OR a follow-up phrase, continue listening
        # This ensures the system waits for a response when it asks a question
        answer_lower = answer.lower()
        follow_up_phrases = [
            "anything else",
            "is there anything else",
            "what else",
            "help you with",
            "can i help",
            "need anything",
            "something else",
            "let me know"
        ]
        should_continue = "?" in answer or any(phrase in answer_lower for phrase in follow_up_phrases)

        # Format response for Home Assistant
        # continue_conversation at root level signals Voice PE to keep listening after TTS playback
        ha_response = HAConversationResponse(
            response={
                "speech": {
                    "plain": {
                        "speech": answer,
                        "extra_data": None
                    }
                },
                "card": {},
                "language": request.language,
                "response_type": "action_done",
                "data": {
                    "success": True,
                    "targets": []
                }
            },
            conversation_id=orchestrator_session_id,
            continue_conversation=should_continue
        )

        if should_continue:
            logger.info(f"Continued conversation enabled for device {device_id} (response contains question)")

        request_counter.labels(endpoint="ha_conversation", status="success").inc()
        logger.info(f"HA conversation completed: device {device_id}, session {orchestrator_session_id}")

        return ha_response

    except HTTPException:
        request_counter.labels(endpoint="ha_conversation", status="error").inc()
        raise
    except Exception as e:
        request_counter.labels(endpoint="ha_conversation", status="error").inc()
        logger.error(f"HA conversation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# =============================================================================
# Music Assistant WebSocket Proxy and Browser Playback Support
# =============================================================================

# WebSocket support for Music Assistant proxy
try:
    from fastapi import WebSocket, WebSocketDisconnect
    import websockets
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    logger.warning("WebSocket dependencies not available for MA proxy")


@app.get("/api/music/config")
async def get_music_config(request: Request):
    """
    Get Music Assistant configuration for browser playback.

    Returns MA server URL, stream base URL, and whether browser playback is enabled.
    Configuration is fetched from admin API's external_api_keys table.
    """
    try:
        # Fetch MA config from admin API
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{ADMIN_API_URL}/api/external-api-keys/public/music-assistant/credentials"
            )

            if response.status_code == 200:
                config = response.json()
                ws_scheme = "wss" if request.url.scheme == "https" else "ws"
                # Map admin API response fields to expected config:
                # - endpoint_url -> WebSocket URL
                # - api_key -> stream base URL
                # - api_secret -> browser player name
                return {
                    "enabled": True,
                    "ws_url": config.get("endpoint_url", "ws://localhost:8095/ws"),
                    "stream_base_url": config.get("api_key", "http://localhost:8095"),
                    "browser_player_name": config.get("api_secret", "Jarvis Web Browser"),
                    # Proxy URL for clients that can't reach MA directly
                    "proxy_ws_url": f"{ws_scheme}://{request.url.netloc}/ma/ws",
                    "proxy_stream_url": f"{request.url.scheme}://{request.url.netloc}/api/music/stream"
                }
            else:
                return {
                    "enabled": False,
                    "error": "Music Assistant not configured"
                }

    except Exception as e:
        logger.warning(f"Failed to fetch MA config: {e}")
        return {
            "enabled": False,
            "error": str(e)
        }


@app.get("/api/music/stream/{uri:path}")
async def proxy_music_stream(uri: str, request: Request):
    """
    Proxy music stream from Music Assistant to browser.

    This allows browser playback even when MA server isn't directly accessible
    from the browser (e.g., different network segment, CORS issues).
    """
    try:
        # Get MA stream base URL from config
        ma_stream_base = os.getenv("MA_STREAM_URL", "http://localhost:8095")

        # Construct full stream URL
        stream_url = f"{ma_stream_base}/stream/{uri}"

        # Proxy the stream
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", stream_url) as response:
                headers = {
                    "Content-Type": response.headers.get("Content-Type", "audio/mpeg"),
                    "Accept-Ranges": "bytes"
                }

                if "Content-Length" in response.headers:
                    headers["Content-Length"] = response.headers["Content-Length"]

                async def stream_generator():
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        yield chunk

                return StreamingResponse(
                    stream_generator(),
                    media_type=headers["Content-Type"],
                    headers=headers
                )

    except Exception as e:
        logger.error(f"Music stream proxy error: {e}")
        raise HTTPException(status_code=502, detail="Failed to proxy music stream")


class MusicSearchRequest(BaseModel):
    query: str
    media_types: list[str] = ["track", "artist"]
    limit: int = 25


@app.post("/api/music/search")
async def music_search(request: MusicSearchRequest):
    """
    Search Music Assistant for tracks/artists via HTTP.

    This makes a one-shot WebSocket connection to MA, performs the search,
    and returns the results. More reliable than maintaining a persistent connection.
    """
    import websockets
    import json

    ma_ws_url = os.getenv("MA_WS_URL", "ws://localhost:8095/ws")
    ma_token = await _fetch_ma_auth_token()

    try:
        logger.info("music_search_start", query=request.query)

        async with websockets.connect(ma_ws_url, open_timeout=10) as ws:
            # Receive server info
            server_info = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            logger.info("music_search_ma_connected", version=server_info.get("server_version"))

            # Authenticate if we have a token
            if ma_token:
                auth_msg = {
                    "message_id": "1",
                    "command": "auth",
                    "args": {"token": ma_token}
                }
                await ws.send(json.dumps(auth_msg))
                auth_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if "error" in auth_response:
                    logger.error("music_search_auth_failed", error=auth_response.get("details"))

            # Send search command
            search_msg = {
                "message_id": "2",
                "command": "music/search",
                "args": {
                    "search_query": request.query,
                    "media_types": request.media_types,
                    "limit": request.limit
                }
            }
            await ws.send(json.dumps(search_msg))

            # Wait for search results
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))

            if "result" in response:
                results = response["result"]
                logger.info("music_search_success",
                           tracks=len(results.get("tracks", [])),
                           artists=len(results.get("artists", [])))
                return results
            elif "error" in response:
                logger.error("music_search_ma_error", error=response.get("details"))
                raise HTTPException(status_code=500, detail=response.get("details", "Search failed"))
            else:
                return response

    except asyncio.TimeoutError:
        logger.error("music_search_timeout")
        raise HTTPException(status_code=504, detail="Search timeout")
    except websockets.exceptions.WebSocketException as e:
        logger.error("music_search_ws_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"WebSocket error: {e}")
    except Exception as e:
        logger.error("music_search_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


class MusicPlayRequest(BaseModel):
    player_id: str
    uri: str
    radio_mode: bool = True


@app.post("/api/music/play")
async def music_play(request: MusicPlayRequest):
    """
    Play media to a specific Music Assistant player.

    This is used by the browser music player to send audio to a Sendspin-registered
    player. The browser connects as a Sendspin player, then uses this endpoint
    to tell MA to stream audio to that player.
    """
    import websockets
    import json

    ma_ws_url = os.getenv("MA_WS_URL", "ws://localhost:8095/ws")
    ma_token = await _fetch_ma_auth_token()

    try:
        logger.info("music_play_start", player_id=request.player_id, uri=request.uri)

        async with websockets.connect(ma_ws_url, open_timeout=10) as ws:
            # Receive server info
            server_info = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            logger.info("music_play_ma_connected", version=server_info.get("server_version"))

            # Authenticate
            if ma_token:
                auth_msg = {
                    "message_id": "1",
                    "command": "auth",
                    "args": {"token": ma_token}
                }
                await ws.send(json.dumps(auth_msg))
                auth_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if "error" in auth_response:
                    logger.error("music_play_auth_failed", error=auth_response.get("details"))
                    raise HTTPException(status_code=401, detail="MA authentication failed")

            # Send play command
            play_msg = {
                "message_id": "2",
                "command": "player_queues/play_media",
                "args": {
                    "queue_id": request.player_id,
                    "media": request.uri,
                    "option": "replace",
                    "radio_mode": request.radio_mode
                }
            }
            await ws.send(json.dumps(play_msg))

            # Wait for response
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))

            if "error" in response:
                error_detail = response.get("details", "Play command failed")
                logger.error("music_play_ma_error", error=error_detail)
                raise HTTPException(status_code=500, detail=error_detail)

            logger.info("music_play_success", player_id=request.player_id)
            return {"status": "ok", "player_id": request.player_id}

    except asyncio.TimeoutError:
        logger.error("music_play_timeout")
        raise HTTPException(status_code=504, detail="Play command timeout")
    except websockets.exceptions.WebSocketException as e:
        logger.error("music_play_ws_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"WebSocket error: {e}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("music_play_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# In-memory MA auth token (can be set via /internal/ma-token endpoint)
_ma_auth_token_cache: str | None = None


async def _fetch_ma_auth_token() -> str | None:
    """
    Fetch Music Assistant authentication token.

    Priority:
    1. In-memory cache (set via /internal/ma-token endpoint)
    2. Environment variable MA_AUTH_TOKEN
    3. Admin API external_api_keys (api_key2 field for music-assistant)
    """
    global _ma_auth_token_cache

    # 1. Check in-memory cache
    if _ma_auth_token_cache:
        return _ma_auth_token_cache

    # 2. Check environment variable
    env_token = os.getenv("MA_AUTH_TOKEN")
    if env_token:
        return env_token

    # 3. Try admin API
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{ADMIN_API_URL}/api/external-api-keys/public/music-assistant/credentials"
            )
            if response.status_code == 200:
                config = response.json()
                # MA long-lived token is stored in api_key field
                auth_token = config.get("api_key")
                if auth_token:
                    return auth_token
            return None
    except Exception as e:
        logger.warning(f"Failed to fetch MA auth token: {e}")
        return None


@app.post("/internal/ma-token")
async def set_ma_auth_token(request: Request):
    """
    Set the Music Assistant authentication token.

    This stores the token in memory for the gateway to use when
    authenticating with Music Assistant. The token persists until
    the gateway is restarted.

    Request body: {"token": "your-ma-long-lived-token"}
    """
    global _ma_auth_token_cache
    try:
        data = await request.json()
        token = data.get("token")
        if not token:
            raise HTTPException(status_code=400, detail="Token is required")

        _ma_auth_token_cache = token
        logger.info("MA auth token set via internal endpoint")
        return {"status": "ok", "message": "MA auth token stored"}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")


@app.get("/internal/ma-token/status")
async def get_ma_token_status():
    """Check if MA auth token is configured."""
    global _ma_auth_token_cache
    has_memory_token = bool(_ma_auth_token_cache)
    has_env_token = bool(os.getenv("MA_AUTH_TOKEN"))

    return {
        "configured": has_memory_token or has_env_token,
        "source": "memory" if has_memory_token else ("env" if has_env_token else "none")
    }


if WEBSOCKET_AVAILABLE:
    @app.websocket("/ma/ws")
    async def music_assistant_websocket_proxy(websocket: WebSocket):
        """
        WebSocket proxy for Music Assistant with automatic authentication.

        Allows browser to connect to MA server through the gateway.
        Handles MA 2.7+ authentication transparently - the gateway
        authenticates with MA using a stored token, then forwards
        all messages to the browser client.
        """
        await websocket.accept()
        logger.info("MA WebSocket proxy: Client connected")

        # Get MA WebSocket URL
        ma_ws_url = os.getenv("MA_WS_URL", "ws://localhost:8095/ws")

        ma_websocket = None
        authenticated = False

        try:
            # Connect to Music Assistant
            ma_websocket = await websockets.connect(
                ma_ws_url,
                ping_interval=20,
                ping_timeout=20
            )
            logger.info(f"MA WebSocket proxy: Connected to {ma_ws_url}")

            # Step 1: Wait for server_info from MA
            server_info_msg = await asyncio.wait_for(ma_websocket.recv(), timeout=10)
            server_info = json.loads(server_info_msg)
            logger.info(f"MA WebSocket proxy: Received server_info, version={server_info.get('server_version')}")

            # Step 2: Authenticate with MA using stored token
            auth_token = await _fetch_ma_auth_token()
            if auth_token:
                auth_command = {
                    "message_id": 0,
                    "command": "auth",
                    "args": {
                        "token": auth_token,
                        "device_name": "Jarvis Gateway"
                    }
                }
                await ma_websocket.send(json.dumps(auth_command))
                auth_response = await asyncio.wait_for(ma_websocket.recv(), timeout=10)
                auth_result = json.loads(auth_response)

                if auth_result.get("error_code"):
                    logger.error(f"MA auth failed: {auth_result.get('details', 'Unknown error')}")
                    # Send error to client and close
                    await websocket.send_text(json.dumps({
                        "error": "ma_auth_failed",
                        "details": auth_result.get("details", "Music Assistant authentication failed")
                    }))
                    return
                else:
                    logger.info("MA WebSocket proxy: Authenticated successfully")
                    authenticated = True
            else:
                logger.warning("MA WebSocket proxy: No auth token configured, connection may fail")
                # Still forward server_info - let client handle the auth requirement
                # This allows the system to work if MA has auth disabled

            # Step 3: Forward server_info to browser client (with auth status)
            if authenticated:
                server_info["_gateway_authenticated"] = True
            await websocket.send_text(json.dumps(server_info))

            # Step 4: Bidirectional forwarding
            async def forward_to_client():
                """Forward messages from MA to browser client."""
                try:
                    async for message in ma_websocket:
                        await websocket.send_text(message)
                except Exception as e:
                    logger.debug(f"MA->Client forward ended: {e}")

            async def forward_to_ma():
                """Forward messages from browser client to MA."""
                try:
                    while True:
                        message = await websocket.receive_text()
                        # Intercept auth commands from client - we already handled auth
                        try:
                            msg_data = json.loads(message)
                            if msg_data.get("command") == "auth" and authenticated:
                                # Already authenticated by gateway, send success response
                                await websocket.send_text(json.dumps({
                                    "message_id": msg_data.get("message_id"),
                                    "result": {"success": True, "authenticated_by": "gateway"}
                                }))
                                continue
                        except json.JSONDecodeError:
                            pass
                        await ma_websocket.send(message)
                except WebSocketDisconnect:
                    logger.info("MA WebSocket proxy: Client disconnected")
                except Exception as e:
                    logger.debug(f"Client->MA forward ended: {e}")

            # Run both forwarding tasks concurrently
            await asyncio.gather(
                forward_to_client(),
                forward_to_ma(),
                return_exceptions=True
            )

        except asyncio.TimeoutError:
            logger.error("MA WebSocket proxy: Timeout during connection/auth")
            await websocket.send_text(json.dumps({
                "error": "ma_timeout",
                "details": "Timeout connecting to Music Assistant"
            }))
        except Exception as e:
            logger.error(f"MA WebSocket proxy error: {e}")
        finally:
            if ma_websocket:
                await ma_websocket.close()
            logger.info("MA WebSocket proxy: Connection closed")


if WEBSOCKET_AVAILABLE:
    @app.websocket("/ma/sendspin")
    async def sendspin_websocket_proxy(websocket: WebSocket):
        """
        WebSocket proxy for Music Assistant Sendspin audio streaming.

        Sendspin is an open-source multi-room audio streaming protocol
        that enables real-time audio playback in the browser. This proxy:
        - Connects to MA's /sendspin endpoint (Sendspin protocol, not MA API)
        - Passes through Sendspin protocol messages (client/hello, server/hello, audio)
        - Handles HA token auth on behalf of browser (browser doesn't have token)
        - Forwards binary audio frames to the browser
        """
        await websocket.accept()
        logger.info("Sendspin proxy: Client connected")

        # MA Sendspin endpoint - this speaks Sendspin protocol, not MA API
        ma_sendspin_url = os.getenv("MA_SENDSPIN_URL", "ws://localhost:8095/sendspin")

        ma_websocket = None

        # Parse client_id from query params
        query_params = websocket.scope.get("query_string", b"").decode()
        client_id = "jarvis-web-" + str(int(time.time() * 1000))[-8:]  # Unique per session
        if "client_id=" in query_params:
            for param in query_params.split("&"):
                if param.startswith("client_id="):
                    client_id = param.split("=", 1)[1]
                    break

        try:
            # Connect to Music Assistant Sendspin endpoint
            ma_websocket = await websockets.connect(
                ma_sendspin_url,
                ping_interval=20,
                ping_timeout=20,
                max_size=None  # Allow large binary messages
            )
            logger.info(f"Sendspin proxy: Connected to {ma_sendspin_url}")

            # Step 1: Authenticate with Sendspin endpoint using HA token
            # Sendspin auth format: {type: "auth", token: ..., client_id: ...}
            auth_token = await _fetch_ma_auth_token()
            authenticated = False
            if auth_token:
                auth_msg = {
                    "type": "auth",
                    "token": auth_token,
                    "client_id": client_id
                }
                await ma_websocket.send(json.dumps(auth_msg))
                logger.info(f"Sendspin proxy: Sent auth for client_id={client_id}")

                try:
                    auth_response = await asyncio.wait_for(ma_websocket.recv(), timeout=10.0)
                    auth_result = json.loads(auth_response)
                    if auth_result.get("type") == "auth_ok":
                        authenticated = True
                        logger.info("Sendspin proxy: Authentication successful (auth_ok)")
                    else:
                        logger.warning(f"Sendspin proxy: Unexpected auth response: {auth_result}")
                        # Continue anyway - might still work
                        authenticated = True
                except asyncio.TimeoutError:
                    logger.warning("Sendspin proxy: Auth response timeout, continuing anyway")
                    authenticated = True

            # Step 2: Send client/hello on behalf of browser to register as player
            # Must be sent on the same connection immediately after auth_ok
            client_hello = {
                "type": "client/hello",
                "payload": {
                    "client_id": client_id,
                    "name": "Jarvis Web Browser",
                    "version": 1,
                    "supported_roles": ["player@v1"],
                    "device_info": {
                        "product_name": "Jarvis Web",
                        "manufacturer": "Project Athena",
                        "software_version": "1.0.0"
                    },
                    "player_support": {
                        "supported_formats": [
                            # PCM first - universally supported by all browsers
                            {"codec": "pcm", "channels": 2, "sample_rate": 48000, "bit_depth": 16},
                            # Opus as fallback (only works on desktop browsers with WebCodecs)
                            {"codec": "opus", "channels": 2, "sample_rate": 48000, "bit_depth": 16}
                        ],
                        "buffer_capacity": 524288,
                        "supported_commands": ["volume", "mute"]
                    }
                }
            }
            await ma_websocket.send(json.dumps(client_hello))
            logger.info(f"Sendspin proxy: Sent client/hello for client_id={client_id}")

            # Wait for server/hello response
            server_hello_received = False
            try:
                server_hello_msg = await asyncio.wait_for(ma_websocket.recv(), timeout=10.0)
                server_hello_data = json.loads(server_hello_msg)
                if server_hello_data.get("type") == "server/hello":
                    server_hello_received = True
                    logger.info(f"Sendspin proxy: Received server/hello: {str(server_hello_data)[:100]}")
                    # Forward server/hello to browser
                    await websocket.send_text(server_hello_msg)
                else:
                    logger.warning(f"Sendspin proxy: Expected server/hello, got: {server_hello_data}")
                    # Forward whatever we got
                    await websocket.send_text(server_hello_msg)
            except asyncio.TimeoutError:
                logger.warning("Sendspin proxy: server/hello timeout")

            # Step 3: Send client/state (required within 5 seconds of server/hello)
            if server_hello_received:
                client_state = {
                    "type": "client/state",
                    "payload": {
                        "state": "synchronized",
                        "player": {"volume": 80, "muted": False}
                    }
                }
                await ma_websocket.send(json.dumps(client_state))
                logger.info("Sendspin proxy: Sent client/state")

            # Send connection status to browser client
            connected_msg = json.dumps({
                "type": "connected",
                "endpoint": ma_sendspin_url,
                "authenticated": authenticated,
                "client_id": client_id,
                "protocol": "sendspin",
                "player_registered": server_hello_received
            })
            await websocket.send_text(connected_msg)
            logger.info(f"Sendspin proxy: Sent 'connected' message to client, player_registered={server_hello_received}")

            # Bidirectional forwarding (supports both text and binary)
            async def forward_to_client():
                """Forward messages from MA Sendspin to browser client."""
                logger.info("Sendspin proxy: forward_to_client task started")
                try:
                    async for message in ma_websocket:
                        if isinstance(message, bytes):
                            logger.info(f"Sendspin MA->Client: binary {len(message)} bytes")
                            await websocket.send_bytes(message)
                        else:
                            # Log JSON messages (truncated)
                            msg_preview = message[:200] if len(message) > 200 else message
                            logger.info(f"Sendspin MA->Client: {msg_preview}")
                            await websocket.send_text(message)
                except Exception as e:
                    logger.info(f"Sendspin MA->Client forward ended: {e}")

            async def forward_to_ma():
                """Forward messages from browser client to MA Sendspin."""
                logger.info("Sendspin proxy: forward_to_ma task started")
                try:
                    while True:
                        message = await websocket.receive()
                        if "bytes" in message:
                            logger.info(f"Sendspin Client->MA: binary {len(message['bytes'])} bytes")
                            await ma_websocket.send(message["bytes"])
                        elif "text" in message:
                            # Log JSON messages (truncated)
                            msg_preview = message["text"][:200] if len(message["text"]) > 200 else message["text"]
                            logger.info(f"Sendspin Client->MA: {msg_preview}")
                            await ma_websocket.send(message["text"])
                        else:
                            # Disconnected
                            logger.info(f"Sendspin Client->MA: non-text/bytes message, disconnecting: {message}")
                            break
                except WebSocketDisconnect:
                    logger.info("Sendspin proxy: Client disconnected")
                except Exception as e:
                    logger.info(f"Sendspin Client->MA forward ended: {e}")

            # Run both forwarding tasks concurrently
            logger.info("Sendspin proxy: Starting bidirectional forwarding tasks")
            await asyncio.gather(
                forward_to_client(),
                forward_to_ma(),
                return_exceptions=True
            )
            logger.info("Sendspin proxy: Both forwarding tasks completed")

        except asyncio.TimeoutError:
            logger.error("Sendspin proxy: Timeout during connection")
            await websocket.send_text(json.dumps({
                "error": "sendspin_timeout",
                "details": "Timeout connecting to Sendspin endpoint"
            }))
        except Exception as e:
            logger.error(f"Sendspin proxy error: {e}")
            try:
                await websocket.send_text(json.dumps({
                    "error": "sendspin_error",
                    "details": str(e)
                }))
            except:
                pass
        finally:
            if ma_websocket:
                await ma_websocket.close()
            logger.info("Sendspin proxy: Connection closed")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

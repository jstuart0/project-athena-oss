"""
Orchestrator Constants

Service URLs, model configurations, and other constants used throughout the orchestrator.
"""

import os
from typing import Dict

# ============================================================================
# RAG Service URLs
# ============================================================================

# Phase 1 RAG Services
WEATHER_SERVICE_URL = os.getenv("RAG_WEATHER_URL", "http://localhost:8010")
AIRPORTS_SERVICE_URL = os.getenv("RAG_AIRPORTS_URL", "http://localhost:8011")
STOCKS_SERVICE_URL = os.getenv("RAG_STOCKS_URL", "http://localhost:8012")

# Phase 2 RAG Services
FLIGHTS_SERVICE_URL = os.getenv("RAG_FLIGHTS_URL", "http://localhost:8013")
EVENTS_SERVICE_URL = os.getenv("RAG_EVENTS_URL", "http://localhost:8014")
STREAMING_SERVICE_URL = os.getenv("RAG_STREAMING_URL", "http://localhost:8015")
NEWS_SERVICE_URL = os.getenv("RAG_NEWS_URL", "http://localhost:8016")
SPORTS_SERVICE_URL = os.getenv("RAG_SPORTS_URL", "http://localhost:8017")
WEBSEARCH_SERVICE_URL = os.getenv("RAG_WEBSEARCH_URL", "http://localhost:8018")
DINING_SERVICE_URL = os.getenv("RAG_DINING_URL", "http://localhost:8019")
RECIPES_SERVICE_URL = os.getenv("RAG_RECIPES_URL", "http://localhost:8020")
DIRECTIONS_SERVICE_URL = os.getenv("RAG_DIRECTIONS_URL", "http://localhost:8022")

# Phase 2: Mode service
MODE_SERVICE_URL = os.getenv("MODE_SERVICE_URL", "http://localhost:8021")

# LLM (supports multiple env var names for flexibility)
OLLAMA_URL = os.getenv("LLM_SERVICE_URL") or os.getenv("OLLAMA_URL", "http://localhost:11434")

# URL map for RAG service lookup
RAG_SERVICE_URL_MAP: Dict[str, str] = {
    "weather": WEATHER_SERVICE_URL,
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

# ============================================================================
# Model Configuration
# ============================================================================

# Fallback model values if database unavailable
# These can be overridden via environment variables: ATHENA_FALLBACK_MODEL_<COMPONENT_NAME>
FALLBACK_MODELS: Dict[str, str] = {
    "intent_classifier": os.getenv("ATHENA_FALLBACK_MODEL_INTENT_CLASSIFIER", "qwen3:4b"),
    "tool_calling_simple": os.getenv("ATHENA_FALLBACK_MODEL_TOOL_CALLING_SIMPLE", "qwen3:4b-instruct-2507-q4_K_M"),
    "tool_calling_complex": os.getenv("ATHENA_FALLBACK_MODEL_TOOL_CALLING_COMPLEX", "qwen3:4b-instruct-2507-q4_K_M"),
    "tool_calling_super_complex": os.getenv("ATHENA_FALLBACK_MODEL_TOOL_CALLING_SUPER_COMPLEX", "qwen3:8b"),
    "response_synthesis": os.getenv("ATHENA_FALLBACK_MODEL_RESPONSE_SYNTHESIS", "qwen3:4b-instruct-2507-q4_K_M"),
    "fact_check_validation": os.getenv("ATHENA_FALLBACK_MODEL_FACT_CHECK_VALIDATION", "qwen3:8b"),
    "conversation_summarizer": os.getenv("ATHENA_FALLBACK_MODEL_CONVERSATION_SUMMARIZER", "qwen3:4b"),
}

# ============================================================================
# Location Configuration
# ============================================================================

DEFAULT_CITY = "Baltimore"
DEFAULT_STATE = "MD"
DEFAULT_LOCATION = f"{DEFAULT_CITY}, {DEFAULT_STATE}"

# City to state mapping (used in multiple places)
CITY_STATE_MAP: Dict[str, str] = {
    "Baltimore": "MD",
    "Washington": "DC",
    "New York": "NY",
    "Los Angeles": "CA",
    "Chicago": "IL",
    "Philadelphia": "PA",
    "Boston": "MA",
    "San Francisco": "CA",
    "Seattle": "WA",
    "Miami": "FL",
    "Atlanta": "GA",
    "Dallas": "TX",
    "Houston": "TX",
    "Denver": "CO",
    "Phoenix": "AZ",
}

# ============================================================================
# Control Command Patterns
# ============================================================================
# Note: CONTEXT_REF_PATTERNS lives in context/detector.py (more comprehensive)

CONTROL_PATTERNS = [
    "turn on", "turn off", "set", "dim", "brighten",
    "light", "lights", "switch", "temperature", "thermostat", "scene",
    "color", "colors", "random", "fan", "lamp", "blind", "shade"
]

# ============================================================================
# Cache and Timeout Configuration
# ============================================================================

DEFAULT_CACHE_TTL = 300  # 5 minutes
DEFAULT_CONTEXT_TTL = 300  # 5 minutes
DEFAULT_REQUEST_TIMEOUT = 30.0  # seconds

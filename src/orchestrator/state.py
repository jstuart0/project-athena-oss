"""
Orchestrator State Definitions

Contains the core state classes and enums used throughout the LangGraph state machine.
"""

import os
import time
import hashlib
from typing import Dict, Any, Optional, List, Literal
from enum import Enum

from pydantic import BaseModel, Field

# Model configuration from environment (defaults to qwen3:4b-instruct-2507-q4_K_M for portability)
_DEFAULT_MODEL = os.getenv("ATHENA_DEFAULT_MODEL", "qwen3:4b-instruct-2507-q4_K_M")


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


class ModelTier(str, Enum):
    """Model tiers for different query complexities (all preloaded with keep_alive=-1)."""
    CLASSIFIER = os.getenv("ATHENA_MODEL_CLASSIFIER", _DEFAULT_MODEL)  # Fast classification
    SMALL = os.getenv("ATHENA_MODEL_SMALL", _DEFAULT_MODEL)  # Fast tool calling
    MEDIUM = os.getenv("ATHENA_MODEL_MEDIUM", _DEFAULT_MODEL)  # Fast for most tasks
    LARGE = os.getenv("ATHENA_MODEL_LARGE", _DEFAULT_MODEL)  # Complex queries
    SYNTHESIS = os.getenv("ATHENA_MODEL_SYNTHESIS", _DEFAULT_MODEL)  # Response synthesis


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
    model_used: Optional[str] = None  # Actual model name used for synthesis (e.g. mlx path or ollama tag)

    # Retrieved data
    retrieved_data: Dict[str, Any] = Field(default_factory=dict)
    data_source: Optional[str] = None
    base_knowledge_populated: bool = Field(False, description="True when base knowledge was successfully injected into the system prompt")

    # Response
    answer: Optional[str] = None
    citations: List[str] = Field(default_factory=list)
    skip_synthesis: bool = Field(False, description="Skip LLM synthesis (used by status query optimization)")
    was_truncated: bool = Field(False, description="Whether response was truncated due to token limit")
    is_fallback: bool = Field(False, description="Whether response is a fallback/error (should not be cached)")

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

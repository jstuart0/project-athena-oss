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

# Model configuration from environment (defaults to qwen3:4b for portability)
_DEFAULT_MODEL = os.getenv("ATHENA_DEFAULT_MODEL", "qwen3:4b")


class IntentCategory(str, Enum):
    """Intent categories for query classification."""
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
    DIRECTIONS = "directions"  # Navigation and route planning
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

    # Conversation context
    conversation_history: List[Dict[str, str]] = Field(default_factory=list, description="Previous conversation messages")
    context_ref_info: Dict[str, Any] = Field(default_factory=dict, description="Detected context reference info")
    prev_context: Optional[Dict[str, Any]] = Field(None, description="Previous conversation context from Redis")

    # Phase 2: Guest Mode permissions
    permissions: Dict[str, Any] = Field(default_factory=dict, description="User permissions from mode service")

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

    # Validation
    validation_passed: bool = True
    validation_reason: Optional[str] = None
    validation_details: List[str] = Field(default_factory=list)

    # Metadata
    request_id: str = Field(default_factory=lambda: hashlib.md5(str(time.time()).encode()).hexdigest()[:8])
    start_time: float = Field(default_factory=time.time)
    node_timings: Dict[str, float] = Field(default_factory=dict)
    error: Optional[str] = None

"""
Database connection and session management for Athena Admin.

Provides SQLAlchemy engine, session factory, and dependency injection
for FastAPI endpoints.

DEV_MODE Support:
    When DEV_MODE=true environment variable is set, uses SQLite in-memory
    database instead of PostgreSQL. This allows local development and testing
    without requiring network access to the production database.
"""
import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool, QueuePool, StaticPool
import structlog

from app.models import Base

logger = structlog.get_logger()

# Check for DEV_MODE
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"

# Database configuration from environment
if DEV_MODE:
    DATABASE_URL = "sqlite:///:memory:"
    logger.info("dev_mode_enabled", database="sqlite_in_memory")
else:
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        raise ValueError(
            "DATABASE_URL environment variable is required. "
            "Set it to your PostgreSQL connection string, e.g.: "
            "postgresql://user:password@host:5432/database"
        )

# Database password from environment (for security)
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
if DB_PASSWORD and "@" in DATABASE_URL:
    # Insert password into connection string
    # postgresql://user@host -> postgresql://user:password@host
    parts = DATABASE_URL.split("@")
    user_part = parts[0].split("//")[1]  # Extract user
    DATABASE_URL = f"postgresql://{user_part}:{DB_PASSWORD}@{'@'.join(parts[1:])}"

# Connection pool settings
POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "20"))
POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "3600"))  # 1 hour
POOL_PRE_PING = os.getenv("DB_POOL_PRE_PING", "true").lower() == "true"

# Create engine with connection pooling
if DEV_MODE:
    # SQLite in-memory requires StaticPool to maintain single connection
    engine = create_engine(
        DATABASE_URL,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        echo=os.getenv("SQL_ECHO", "false").lower() == "true",
    )
else:
    engine = create_engine(
        DATABASE_URL,
        poolclass=QueuePool,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_recycle=POOL_RECYCLE,
        pool_pre_ping=POOL_PRE_PING,  # Test connections before using them
        echo=os.getenv("SQL_ECHO", "false").lower() == "true",  # Log SQL queries if enabled
        connect_args={"client_encoding": "utf8"},  # Ensure UTF-8 encoding for Unicode support
    )

# Session factory
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False  # Don't expire objects after commit
)


# SQLAlchemy event listeners for logging
@event.listens_for(Engine, "connect")
def receive_connect(dbapi_conn, connection_record):
    """Log database connections."""
    logger.debug("database_connection_established")


@event.listens_for(Engine, "close")
def receive_close(dbapi_conn, connection_record):
    """Log database disconnections."""
    logger.debug("database_connection_closed")


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency for database sessions.

    Usage in FastAPI endpoints:
        @app.get("/api/policies")
        def get_policies(db: Session = Depends(get_db)):
            return db.query(Policy).all()

    Yields:
        Session: SQLAlchemy session

    Note:
        Session is automatically closed after request completes,
        even if an exception occurs.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_context():
    """
    Context manager for database sessions outside FastAPI.

    Usage:
        with get_db_context() as db:
            policy = db.query(Policy).first()

    Yields:
        Session: SQLAlchemy session

    Note:
        Session is automatically closed when context exits.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    Initialize database schema.

    Creates all tables defined in models if they don't exist.
    Should only be called during initial setup or testing.

    For production, use Alembic migrations instead.
    """
    logger.info("initializing_database_schema")
    Base.metadata.create_all(bind=engine)
    logger.info("database_schema_initialized")


def check_db_connection() -> bool:
    """
    Check if database connection is healthy.

    Returns:
        bool: True if connection is healthy, False otherwise
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.debug("database_health_check_passed")
        return True
    except Exception as e:
        logger.error("database_health_check_failed", error=str(e))
        return False


def get_db_stats() -> dict:
    """
    Get database connection pool statistics.

    Returns:
        dict: Pool statistics including size, checked out connections, etc.
    """
    if DEV_MODE:
        return {
            "pool_size": 1,
            "checked_out": 0,
            "overflow": 0,
            "checked_in": 1,
            "recycle_time": 0,
            "mode": "dev_sqlite_memory",
        }
    pool = engine.pool
    return {
        "pool_size": pool.size(),
        "checked_out": pool.checkedout(),
        "overflow": pool.overflow(),
        "checked_in": pool.checkedin(),
        "recycle_time": POOL_RECYCLE,
    }


def seed_dev_data():
    """
    Seed development database with default admin user and essential data.

    Only called when DEV_MODE=true and using SQLite in-memory database.
    """
    if not DEV_MODE:
        return

    from datetime import datetime
    from app.models import User, ConversationSettings

    with get_db_context() as db:
        # Create default admin user
        admin_user = db.query(User).filter(User.username == "dev-admin").first()
        if not admin_user:
            admin_user = User(
                authentik_id="dev-admin-001",
                username="dev-admin",
                email="dev-admin@localhost",
                full_name="Development Admin",
                role="owner",
                active=True,
                last_login=datetime.utcnow()
            )
            db.add(admin_user)
            logger.info("dev_mode_admin_user_created", username="dev-admin")

        # Create default conversation settings
        settings = db.query(ConversationSettings).first()
        if not settings:
            settings = ConversationSettings(
                enabled=True,
                use_context=True,
                max_messages=20,
                timeout_seconds=1800,
                cleanup_interval_seconds=60,
                session_ttl_seconds=3600,
                max_llm_history_messages=10
            )
            db.add(settings)
            logger.info("dev_mode_conversation_settings_created")

        db.commit()
        logger.info("dev_mode_seed_data_complete")


# OSS default model configuration
OSS_DEFAULT_MODEL = os.getenv("ATHENA_DEFAULT_MODEL", "qwen3:4b")
OSS_OLLAMA_URL = os.getenv("OLLAMA_URL") or os.getenv("LLM_SERVICE_URL", "http://localhost:11434")
OSS_AUTO_PULL_MODELS = os.getenv("ATHENA_AUTO_PULL_MODELS", "true").lower() == "true"
OSS_SEED_DEFAULTS = os.getenv("ATHENA_SEED_DEFAULTS", "true").lower() == "true"


def seed_oss_defaults():
    """
    Seed OSS default configuration for LLM backends and component assignments.

    This ensures a working out-of-the-box experience with qwen3:4b as the default model.
    Called during production startup to ensure configuration exists.
    """
    from datetime import datetime
    from app.models import LLMBackend, ComponentModelAssignment, SystemSetting

    with get_db_context() as db:
        # Get centralized Ollama URL from system_settings (set by migration 051)
        setting = db.query(SystemSetting).filter(SystemSetting.key == "ollama_url").first()
        ollama_url = setting.value if setting and setting.value else OSS_OLLAMA_URL

        # Check if LLM backend exists for the default model
        backend = db.query(LLMBackend).filter(LLMBackend.model_name == OSS_DEFAULT_MODEL).first()

        if not backend:
            # Create default LLM backend
            backend = LLMBackend(
                model_name=OSS_DEFAULT_MODEL,
                backend_type="ollama",
                endpoint_url=ollama_url,
                enabled=True,
                priority=50,
                max_tokens=4096,
                temperature_default=0.7,
                timeout_seconds=90,
                keep_alive_seconds=-1,  # Keep model loaded indefinitely
                description=f"{OSS_DEFAULT_MODEL} - Default OSS model for all components",
                total_requests=0,
                total_errors=0
            )
            db.add(backend)
            logger.info("oss_llm_backend_created", model=OSS_DEFAULT_MODEL, endpoint_url=ollama_url)

        # Component model assignments to create/update
        components = [
            ("intent_classifier", "Intent Classification", "Classifies user queries into intent categories", "orchestrator", 0.3),
            ("tool_calling_simple", "Tool Calling (Simple)", "Selects RAG tools for simple queries", "orchestrator", 0.7),
            ("tool_calling_complex", "Tool Calling (Complex)", "Selects RAG tools for complex queries", "orchestrator", 0.7),
            ("tool_calling_super_complex", "Tool Calling (Super Complex)", "Selects RAG tools for highly complex queries", "orchestrator", 0.7),
            ("response_synthesis", "Response Synthesis", "Generates natural language responses from RAG results", "orchestrator", 0.7),
            ("fact_check_validation", "Fact-Check Validation", "Validates responses for accuracy", "validation", 0.1),
            ("smart_home_control", "Smart Home Control", "Extracts device commands from natural language", "control", 0.1),
            ("response_validator_primary", "Response Validator (Primary)", "Primary model for cross-validation", "validation", 0.1),
            ("response_validator_secondary", "Response Validator (Secondary)", "Secondary model for cross-validation", "validation", 0.1),
            ("conversation_summarizer", "Conversation Summarizer", "Compresses conversation history", "orchestrator", 0.3),
        ]

        created_count = 0
        updated_count = 0

        for comp_name, display_name, description, category, temperature in components:
            assignment = db.query(ComponentModelAssignment).filter(
                ComponentModelAssignment.component_name == comp_name
            ).first()

            if not assignment:
                # Create new assignment
                assignment = ComponentModelAssignment(
                    component_name=comp_name,
                    display_name=display_name,
                    description=description,
                    category=category,
                    model_name=OSS_DEFAULT_MODEL,
                    backend_type="ollama",
                    temperature=temperature,
                    enabled=True
                )
                db.add(assignment)
                created_count += 1
            elif assignment.model_name != OSS_DEFAULT_MODEL:
                # Update existing assignment to use default model if it's using an unavailable model
                # Only update if model_name looks like an old default (qwen2.5, phi3, llama3.1)
                old_model = assignment.model_name.lower()
                if any(m in old_model for m in ["qwen2.5", "phi3:mini", "llama3.1", "llama3.2"]):
                    assignment.model_name = OSS_DEFAULT_MODEL
                    assignment.updated_at = datetime.utcnow()
                    updated_count += 1

        db.commit()

        if created_count > 0 or updated_count > 0:
            logger.info("oss_component_models_seeded",
                       model=OSS_DEFAULT_MODEL,
                       created=created_count,
                       updated=updated_count)
        else:
            logger.debug("oss_component_models_already_configured")


def seed_oss_features():
    """
    Seed OSS default feature flags for the Athena system.

    This ensures all feature flags exist out-of-the-box for the OSS release.
    Called during production startup to ensure configuration exists.
    Uses ON CONFLICT to only insert missing features, preserving existing settings.
    """
    from app.models import Feature

    # Define all OSS feature flags
    # Format: (name, display_name, description, category, enabled, required, priority, config)
    features = [
        # Core Processing Features
        (
            "intent_classification",
            "Intent Classification",
            "Classify user query intent",
            "processing",
            True, True, 1, None
        ),
        (
            "multi_intent_detection",
            "Multi-Intent Detection",
            "Detect and parse multiple intents in a single query",
            "processing",
            True, False, 2, None
        ),
        (
            "conversation_context",
            "Conversation Context",
            "Preserve context between queries in a conversation",
            "processing",
            True, False, 3, None
        ),
        (
            "automation_system_mode",
            "Automation System Mode",
            "Controls which automation system handles sequence commands. 'pattern_matching' uses keyword detection. 'dynamic_agent' uses LLM with tools.",
            "processing",
            True, False, 50,
            {"mode": "pattern_matching", "available_modes": ["pattern_matching", "dynamic_agent"]}
        ),
        # Routing Features
        (
            "llm_based_routing",
            "Use LLM for Intent Classification",
            "Use AI to intelligently classify query intent instead of keyword matching. More accurate but adds 50-200ms latency.",
            "routing",
            True, False, 10, None
        ),
        (
            "enable_llm_intent_classification",
            "LLM Intent Classification",
            "Use LLM for intent classification in Orchestrator instead of pattern matching. Adds 50-200ms latency but improves accuracy.",
            "llm",
            True, False, 10, None
        ),
        # RAG Features
        (
            "rag_weather",
            "Weather RAG",
            "Retrieve live weather data from National Weather Service",
            "rag",
            True, False, 10, None
        ),
        (
            "rag_sports",
            "Sports RAG",
            "Retrieve sports scores and schedules from ESPN",
            "rag",
            True, False, 11, None
        ),
        (
            "rag_airports",
            "Airports RAG",
            "Retrieve airport and flight information",
            "rag",
            True, False, 12, None
        ),
        (
            "weather_provider",
            "Weather Provider",
            "Select weather data provider: standard (free tier, 5-day forecast) or onecall (OneCall 3.0, 8-day forecast with alerts)",
            "rag",
            True, False, 10,
            {"mode": "standard", "available_modes": ["standard", "onecall"]}
        ),
        # Optimization Features
        (
            "redis_caching",
            "Redis Caching",
            "Cache responses in Redis for faster retrieval",
            "optimization",
            True, False, 20, None
        ),
        (
            "mlx_backend",
            "MLX Backend",
            "Use MLX-optimized backend for LLM inference",
            "optimization",
            True, False, 21, None
        ),
        (
            "response_streaming",
            "Response Streaming",
            "Stream LLM responses in real-time",
            "optimization",
            True, False, 22, None
        ),
        (
            "search_pre_classification",
            "Search Pre-Classification",
            "Use embedding similarity to pre-classify obvious search queries without LLM inference. Saves ~1.3s on high-confidence matches.",
            "optimization",
            True, False, 50,
            {"confidence_threshold": 0.85, "fallback_to_llm": True}
        ),
        (
            "status_skip_synthesis",
            "Skip Status Synthesis",
            "Skip LLM synthesis for simple status queries (e.g., 'what lights are on'). Returns templated response directly. Saves ~1.5s.",
            "optimization",
            True, False, 51,
            {"enabled_patterns": ["lights.*on", "what.*status", "is.*locked"], "max_entities": 20}
        ),
        (
            "status_bulk_query",
            "Bulk HA State Query",
            "Use bulk Home Assistant state query instead of per-entity queries. Reduces HA API calls and saves 1-2s on multi-entity queries.",
            "optimization",
            True, False, 52,
            {"batch_size": 50, "timeout_ms": 5000}
        ),
        (
            "entity_type_filtering",
            "Entity Type Filtering",
            "Filter HA entities by type before status queries. Only query relevant entity domains instead of all entities.",
            "optimization",
            True, False, 54,
            {"entity_type_map": {"lights": ["light"], "locks": ["lock"], "doors": ["lock", "binary_sensor.door", "cover.garage"], "fans": ["fan"], "climate": ["climate"], "sensors": ["sensor"]}}
        ),
        (
            "hybrid_memory_search",
            "Hybrid Memory Search",
            "Combines keyword matching with semantic vector search for memory retrieval. Improves recall for queries with specific keywords.",
            "optimization",
            False, False, 50,
            {"keyword_weight": 0.3, "semantic_weight": 0.7, "min_keyword_score": 0.5}
        ),
        # Integration Features
        (
            "home_assistant",
            "Home Assistant",
            "Integrate with Home Assistant for device control",
            "integration",
            True, False, 30, None
        ),
        (
            "clarification_questions",
            "Clarification Questions",
            "Ask clarifying questions for ambiguous queries",
            "integration",
            True, False, 31, None
        ),
        (
            "airport_code_lookup",
            "Airport Code Lookup",
            "Automatically resolve city names to airport codes before calling flights API. Fixes FlightAware 400 errors from natural language destinations.",
            "integration",
            True, False, 53,
            {"cache_ttl_seconds": 86400, "fallback_airports": {"new york": "JFK", "los angeles": "LAX", "chicago": "ORD", "miami": "MIA", "san francisco": "SFO"}}
        ),
        # Voice Features
        (
            "ai_follow_ups_enabled",
            "AI-Initiated Follow-ups",
            "After responding, Athena asks 'Is there anything else?' after 3 seconds of silence. Allows natural conversation continuation.",
            "voice",
            False, False, 50, None
        ),
        # Fallback Features
        (
            "post_synthesis_fallback",
            "Post-Synthesis Web Search Fallback",
            "When LLM synthesis indicates it could not find information, automatically retry with web search. Adds latency but improves answer quality.",
            "fallback",
            False, False, 60,
            {
                "detection_patterns": ["couldn't find", "could not find", "don't have information", "no information available", "unable to find"],
                "excluded_intents": ["control", "automation", "scene", "timer", "reminder"],
                "max_latency_ms": 5000,
                "log_triggers": True
            }
        ),
    ]

    with get_db_context() as db:
        created_count = 0
        for name, display_name, description, category, enabled, required, priority, config in features:
            # Check if feature already exists
            existing = db.query(Feature).filter(Feature.name == name).first()

            if not existing:
                feature = Feature(
                    name=name,
                    display_name=display_name,
                    description=description,
                    category=category,
                    enabled=enabled,
                    required=required,
                    priority=priority,
                    config=config
                )
                db.add(feature)
                created_count += 1

        db.commit()

        if created_count > 0:
            logger.info("oss_features_seeded", created=created_count, total=len(features))
        else:
            logger.debug("oss_features_already_configured", total=len(features))


def seed_oss_conversation_settings():
    """
    Seed OSS default conversation settings.

    Ensures conversation settings exist for the OSS release.
    Called during production startup to ensure configuration exists.
    """
    from app.models import ConversationSettings

    with get_db_context() as db:
        settings = db.query(ConversationSettings).first()
        if not settings:
            settings = ConversationSettings(
                enabled=True,
                use_context=True,
                max_messages=20,
                timeout_seconds=1800,  # 30 minutes
                cleanup_interval_seconds=60,
                session_ttl_seconds=3600,  # 1 hour
                max_llm_history_messages=10,
                history_mode='full'
            )
            db.add(settings)
            db.commit()
            logger.info("oss_conversation_settings_seeded")
        else:
            logger.debug("oss_conversation_settings_already_configured")

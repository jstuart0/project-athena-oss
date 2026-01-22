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

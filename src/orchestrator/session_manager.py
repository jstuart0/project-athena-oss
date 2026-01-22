"""
Conversation Session Manager.

Manages conversation sessions with Redis storage for context tracking.
Integrates with config_loader for dynamic configuration.
"""

import os
import uuid
import json
import asyncio
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import structlog

from orchestrator.config_loader import get_config

logger = structlog.get_logger()

# Redis connection details
# Note: Default to false due to Homebrew Python socket issues on Mac Studio
# The system gracefully falls back to in-memory storage which works fine
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_ENABLED = os.getenv("REDIS_ENABLED", "false").lower() == "true"

# Session key prefix
SESSION_KEY_PREFIX = "athena:session:"

# In-memory fallback storage
_memory_sessions: Dict[str, Dict[str, Any]] = {}


class ConversationSession:
    """Represents a conversation session with history and metadata."""

    def __init__(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        zone: Optional[str] = None,
        created_at: Optional[datetime] = None
    ):
        """
        Initialize conversation session.

        Args:
            session_id: Unique session identifier
            user_id: Optional user identifier for multi-user tracking
            zone: Optional zone identifier (office, kitchen, etc.)
            created_at: Session creation timestamp
        """
        self.session_id = session_id
        self.user_id = user_id
        self.zone = zone
        self.created_at = created_at or datetime.utcnow()
        self.last_activity = datetime.utcnow()
        self.messages: List[Dict[str, Any]] = []
        self.metadata: Dict[str, Any] = {}

    def add_message(self, role: str, content: str, metadata: Optional[Dict] = None):
        """
        Add a message to the conversation history.

        Args:
            role: Message role ('user' or 'assistant')
            content: Message content
            metadata: Optional message metadata (intent, confidence, etc.)
        """
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat(),
            "metadata": metadata or {}
        }
        self.messages.append(message)
        self.last_activity = datetime.utcnow()

    def get_recent_messages(self, max_messages: int) -> List[Dict[str, Any]]:
        """
        Get recent messages for LLM context.

        Args:
            max_messages: Maximum number of messages to return

        Returns:
            List of recent messages (most recent last)
        """
        if max_messages <= 0:
            return []
        return self.messages[-max_messages:]

    def get_llm_history(self, max_messages: int) -> List[Dict[str, str]]:
        """
        Get conversation history formatted for LLM.

        Args:
            max_messages: Maximum number of messages to include

        Returns:
            List of message dicts with 'role' and 'content' keys
        """
        recent = self.get_recent_messages(max_messages)
        return [
            {"role": msg["role"], "content": msg["content"]}
            for msg in recent
        ]

    def get_precomputed_summary(self) -> Optional[str]:
        """
        Get precomputed conversation summary from metadata.

        Returns:
            Precomputed summary string if available, None otherwise
        """
        return self.metadata.get("current_summary")

    def get_summary_message_count(self) -> int:
        """
        Get message count when summary was last computed.

        Returns:
            Message count at last summary computation
        """
        return self.metadata.get("summary_message_count", 0)

    def set_precomputed_summary(self, summary: str, message_count: int):
        """
        Store precomputed summary in metadata.

        Args:
            summary: The computed summary string
            message_count: Current message count when summary was computed
        """
        self.metadata["current_summary"] = summary
        self.metadata["summary_message_count"] = message_count

    def trim_history(self, max_messages: int):
        """
        Trim message history to maximum size.

        Args:
            max_messages: Maximum number of messages to keep
        """
        if len(self.messages) > max_messages:
            # Keep most recent messages
            self.messages = self.messages[-max_messages:]
            logger.info("session_history_trimmed",
                       session_id=self.session_id,
                       kept=len(self.messages))

    def is_expired(self, timeout_seconds: int) -> bool:
        """
        Check if session has expired based on inactivity.

        Args:
            timeout_seconds: Timeout duration in seconds

        Returns:
            True if session is expired
        """
        elapsed = (datetime.utcnow() - self.last_activity).total_seconds()
        return elapsed > timeout_seconds

    def to_dict(self) -> Dict[str, Any]:
        """Convert session to dictionary for storage."""
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "zone": self.zone,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "messages": self.messages,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationSession":
        """Create session from dictionary."""
        session = cls(
            session_id=data["session_id"],
            user_id=data.get("user_id"),
            zone=data.get("zone"),
            created_at=datetime.fromisoformat(data["created_at"])
        )
        session.last_activity = datetime.fromisoformat(data["last_activity"])
        session.messages = data.get("messages", [])
        session.metadata = data.get("metadata", {})
        return session


class SessionManager:
    """Manages conversation sessions with Redis storage."""

    def __init__(self):
        """Initialize session manager."""
        self.redis_client = None
        self._initialized = False
        self._cleanup_task = None

    async def initialize(self):
        """Initialize Redis connection and start cleanup task."""
        if self._initialized:
            return

        # Initialize Redis if enabled
        if REDIS_ENABLED:
            try:
                import redis.asyncio as redis
                self.redis_client = redis.Redis(
                    host=REDIS_HOST,
                    port=REDIS_PORT,
                    decode_responses=True,
                    socket_connect_timeout=2
                )
                # Test connection
                await self.redis_client.ping()
                logger.info("session_manager_redis_connected", host=REDIS_HOST)
            except Exception as e:
                logger.warning("session_manager_redis_unavailable",
                             error=str(e),
                             fallback="memory")
                self.redis_client = None
        else:
            logger.info("session_manager_redis_disabled", storage="memory")

        # Start cleanup task
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        self._initialized = True
        logger.info("session_manager_initialized",
                   storage="redis" if self.redis_client else "memory")

    async def close(self):
        """Close Redis connection and stop cleanup task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self.redis_client:
            await self.redis_client.close()

        self._initialized = False

    async def create_session(
        self,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        zone: Optional[str] = None
    ) -> ConversationSession:
        """
        Create a new conversation session.

        Args:
            session_id: Optional session ID (will generate UUID if not provided)
            user_id: Optional user identifier
            zone: Optional zone identifier

        Returns:
            New ConversationSession instance
        """
        # Use provided session_id or generate new one
        session_id = session_id or str(uuid.uuid4())
        session = ConversationSession(
            session_id=session_id,
            user_id=user_id,
            zone=zone
        )

        # Save to storage
        await self._save_session(session)

        # Log analytics event
        try:
            config = await get_config()
            await config.log_analytics_event(
                session_id=session_id,
                event_type="session_created",
                metadata={"user_id": user_id, "zone": zone}
            )
        except Exception as e:
            logger.warning("analytics_log_failed", error=str(e))

        logger.info("session_created",
                   session_id=session_id,
                   user_id=user_id,
                   zone=zone)

        return session

    async def get_session(self, session_id: str) -> Optional[ConversationSession]:
        """
        Get existing session by ID.

        Args:
            session_id: Session identifier

        Returns:
            ConversationSession if found, None otherwise
        """
        # Try Redis first
        if self.redis_client:
            try:
                data = await self.redis_client.get(f"{SESSION_KEY_PREFIX}{session_id}")
                if data:
                    session_dict = json.loads(data)
                    session = ConversationSession.from_dict(session_dict)
                    logger.debug("session_retrieved",
                               session_id=session_id,
                               source="redis")
                    return session
            except Exception as e:
                logger.warning("redis_get_failed",
                             session_id=session_id,
                             error=str(e))

        # Fallback to memory
        if session_id in _memory_sessions:
            session_dict = _memory_sessions[session_id]
            session = ConversationSession.from_dict(session_dict)
            logger.debug("session_retrieved",
                       session_id=session_id,
                       source="memory")
            return session

        return None

    async def get_or_create_session(
        self,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        zone: Optional[str] = None
    ) -> ConversationSession:
        """
        Get existing session or create new one.

        Args:
            session_id: Optional existing session ID
            user_id: Optional user identifier
            zone: Optional zone identifier

        Returns:
            ConversationSession instance
        """
        if session_id:
            session = await self.get_session(session_id)
            if session:
                # Check if session is expired
                config = await get_config()
                settings = await config.get_conversation_settings()
                timeout = settings.get("timeout_seconds", 1800)

                if not session.is_expired(timeout):
                    return session
                else:
                    logger.info("session_expired",
                              session_id=session_id,
                              elapsed=(datetime.utcnow() - session.last_activity).total_seconds())
                    # Session expired, create new one with same ID for continuity

        # Create new session with provided session_id if given
        return await self.create_session(session_id=session_id, user_id=user_id, zone=zone)

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict] = None
    ):
        """
        Add message to session.

        Args:
            session_id: Session identifier
            role: Message role ('user' or 'assistant')
            content: Message content
            metadata: Optional message metadata
        """
        session = await self.get_session(session_id)
        if not session:
            logger.warning("session_not_found", session_id=session_id)
            return

        session.add_message(role, content, metadata)

        # Trim history if needed
        config = await get_config()
        settings = await config.get_conversation_settings()
        max_messages = settings.get("max_messages", 20)
        session.trim_history(max_messages)

        # Save updated session
        await self._save_session(session)

        logger.debug("message_added",
                    session_id=session_id,
                    role=role,
                    message_count=len(session.messages))

    async def get_llm_context(
        self,
        session_id: str,
        max_history: Optional[int] = None
    ) -> List[Dict[str, str]]:
        """
        Get conversation history for LLM context.

        Args:
            session_id: Session identifier
            max_history: Optional override for max history messages

        Returns:
            List of message dicts for LLM
        """
        session = await self.get_session(session_id)
        if not session:
            return []

        # Get max history from config if not specified
        if max_history is None:
            config = await get_config()
            settings = await config.get_conversation_settings()
            max_history = settings.get("max_llm_history_messages", 10)

        return session.get_llm_history(max_history)

    async def delete_session(self, session_id: str):
        """
        Delete session.

        Args:
            session_id: Session identifier
        """
        # Delete from Redis
        if self.redis_client:
            try:
                await self.redis_client.delete(f"{SESSION_KEY_PREFIX}{session_id}")
            except Exception as e:
                logger.warning("redis_delete_failed",
                             session_id=session_id,
                             error=str(e))

        # Delete from memory
        if session_id in _memory_sessions:
            del _memory_sessions[session_id]

        logger.info("session_deleted", session_id=session_id)

    async def _save_session(self, session: ConversationSession):
        """Save session to storage."""
        session_dict = session.to_dict()

        # Get TTL from config
        config = await get_config()
        settings = await config.get_conversation_settings()
        ttl = settings.get("session_ttl_seconds", 3600)

        # Save to Redis
        if self.redis_client:
            try:
                await self.redis_client.setex(
                    f"{SESSION_KEY_PREFIX}{session.session_id}",
                    ttl,
                    json.dumps(session_dict)
                )
                logger.debug("session_saved",
                           session_id=session.session_id,
                           source="redis")
                return
            except Exception as e:
                logger.warning("redis_save_failed",
                             session_id=session.session_id,
                             error=str(e))

        # Fallback to memory
        _memory_sessions[session.session_id] = session_dict
        logger.debug("session_saved",
                   session_id=session.session_id,
                   source="memory")

    async def _cleanup_loop(self):
        """Background task to cleanup expired sessions."""
        while True:
            try:
                # Get cleanup interval from config
                config = await get_config()
                settings = await config.get_conversation_settings()
                interval = settings.get("cleanup_interval_seconds", 60)
                timeout = settings.get("timeout_seconds", 1800)

                await asyncio.sleep(interval)

                # Cleanup memory sessions (Redis has TTL)
                expired_sessions = []
                for session_id, session_dict in list(_memory_sessions.items()):
                    session = ConversationSession.from_dict(session_dict)
                    if session.is_expired(timeout):
                        expired_sessions.append(session_id)

                for session_id in expired_sessions:
                    del _memory_sessions[session_id]
                    logger.info("session_cleaned_up",
                              session_id=session_id,
                              reason="expired")

                if expired_sessions:
                    logger.info("cleanup_completed",
                              cleaned=len(expired_sessions),
                              remaining=len(_memory_sessions))

            except asyncio.CancelledError:
                logger.info("cleanup_task_cancelled")
                raise
            except Exception as e:
                logger.error("cleanup_error", error=str(e))
                await asyncio.sleep(60)  # Wait before retrying


# Global instance
_session_manager: Optional[SessionManager] = None


async def get_session_manager() -> SessionManager:
    """
    Get global session manager instance.

    Returns:
        SessionManager instance
    """
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
        await _session_manager.initialize()
    return _session_manager


# Convenience functions

async def create_session(
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    zone: Optional[str] = None
) -> ConversationSession:
    """Create new conversation session."""
    manager = await get_session_manager()
    return await manager.create_session(session_id=session_id, user_id=user_id, zone=zone)


async def get_session(session_id: str) -> Optional[ConversationSession]:
    """Get existing session by ID."""
    manager = await get_session_manager()
    return await manager.get_session(session_id)


async def add_user_message(
    session_id: str,
    content: str,
    metadata: Optional[Dict] = None
):
    """Add user message to session."""
    manager = await get_session_manager()
    await manager.add_message(session_id, "user", content, metadata)


async def add_assistant_message(
    session_id: str,
    content: str,
    metadata: Optional[Dict] = None
):
    """Add assistant message to session."""
    manager = await get_session_manager()
    await manager.add_message(session_id, "assistant", content, metadata)


async def get_conversation_context(
    session_id: str,
    max_history: Optional[int] = None
) -> List[Dict[str, str]]:
    """Get conversation history for LLM."""
    manager = await get_session_manager()
    return await manager.get_llm_context(session_id, max_history)


async def update_session_summary(
    session_id: str,
    summary: str
) -> bool:
    """
    Update precomputed summary for a session.

    Args:
        session_id: Session identifier
        summary: The computed summary string

    Returns:
        True if update successful, False otherwise
    """
    manager = await get_session_manager()
    session = await manager.get_session(session_id)
    if not session:
        logger.warning("session_not_found_for_summary_update", session_id=session_id)
        return False

    message_count = len(session.messages)
    session.set_precomputed_summary(summary, message_count)
    await manager._save_session(session)

    logger.info("session_summary_updated",
               session_id=session_id,
               message_count=message_count,
               summary_length=len(summary))
    return True


async def get_session_summary(session_id: str) -> Optional[str]:
    """
    Get precomputed summary for a session if available and fresh.

    A summary is considered fresh if it was computed within the last 4 messages.

    Args:
        session_id: Session identifier

    Returns:
        Precomputed summary if available and fresh, None otherwise
    """
    manager = await get_session_manager()
    session = await manager.get_session(session_id)
    if not session:
        return None

    precomputed = session.get_precomputed_summary()
    if not precomputed:
        return None

    # Check if summary is still fresh (within 4 messages)
    current_count = len(session.messages)
    summary_count = session.get_summary_message_count()

    if current_count - summary_count <= 4:
        logger.debug("using_precomputed_summary",
                    session_id=session_id,
                    messages_since_summary=current_count - summary_count)
        return precomputed

    logger.debug("precomputed_summary_stale",
                session_id=session_id,
                messages_since_summary=current_count - summary_count)
    return None

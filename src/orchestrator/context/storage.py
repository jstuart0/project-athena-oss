"""
Context Storage

Stores and retrieves conversation context from Redis for session continuity.
"""

import json
import time
from typing import Optional

from shared.logging_config import configure_logging
from orchestrator.state import ConversationContext

logger = configure_logging("orchestrator.context.storage")


async def get_conversation_context(
    cache_client,
    session_id: str
) -> Optional[ConversationContext]:
    """
    Retrieve conversation context from Redis for a session.

    Args:
        cache_client: Redis cache client instance
        session_id: Session identifier

    Returns:
        ConversationContext if found, None otherwise
    """
    if not cache_client or not session_id:
        return None

    try:
        context_key = f"athena:context:{session_id}"
        context_json = await cache_client.client.get(context_key)
        if context_json:
            data = json.loads(context_json)
            return ConversationContext(**data)
    except Exception as e:
        logger.warning(f"Failed to retrieve conversation context: {e}")

    return None


async def store_conversation_context(
    cache_client,
    session_id: str,
    intent: str,
    query: str,
    entities: dict,
    parameters: dict,
    response: str,
    ttl: int = 300  # 5 minute default TTL
) -> bool:
    """
    Store conversation context in Redis for a session.

    Args:
        cache_client: Redis cache client instance
        session_id: Session identifier
        intent: The classified intent
        query: Original query text
        entities: Extracted entities (room, location, team, etc.)
        parameters: Action parameters (colors, brightness, etc.)
        response: The response given to user
        ttl: Time to live in seconds (default 5 minutes)

    Returns:
        True if stored successfully, False otherwise
    """
    if not cache_client or not session_id:
        return False

    try:
        context = ConversationContext(
            intent=intent,
            query=query,
            entities=entities,
            parameters=parameters,
            response=response,
            timestamp=time.time()
        )
        context_key = f"athena:context:{session_id}"
        await cache_client.client.setex(context_key, ttl, context.model_dump_json())
        logger.info(f"Stored conversation context for session {session_id[:8]}...: intent={intent}")
        return True
    except Exception as e:
        logger.warning(f"Failed to store conversation context: {e}")
        return False

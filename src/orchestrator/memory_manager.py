"""
Memory Manager for Orchestrator

Handles scoped memory retrieval and creation for LLM context augmentation.
Integrates with the hierarchical memory system (Global, Owner, Guest scopes)
via the Admin Backend API.

Architecture:
    Orchestrator -> Admin Backend API (HTTP) -> PostgreSQL + Qdrant
    (No direct database access from orchestrator)
"""

import os
import structlog
from typing import Optional, List, Dict, Any
from datetime import datetime

logger = structlog.get_logger()

# Admin API configuration - follows config_loader.py pattern
ADMIN_API_URL = os.getenv(
    "ADMIN_API_URL",
    os.getenv("ADMIN_BACKEND_URL", "http://localhost:8080")
)

# Fallback to internal cluster URL
if os.getenv("IN_CLUSTER", "false").lower() == "true":
    ADMIN_API_URL = os.getenv(
        "ADMIN_API_URL",
        "http://athena-admin-backend.athena-admin.svc.cluster.local:8080"
    )


class MemoryManager:
    """Manages memory retrieval and creation for conversations."""

    def __init__(self):
        self.enabled = True
        self._client = None
        self._initialized = False

    async def initialize(self):
        """Initialize HTTP client for Admin API."""
        if self._initialized:
            return

        try:
            import httpx
            self._client = httpx.AsyncClient(
                base_url=ADMIN_API_URL,
                timeout=5.0
            )
            logger.info("memory_manager_initialized", admin_api_url=ADMIN_API_URL)
            self._initialized = True
        except Exception as e:
            logger.error("memory_manager_init_failed", error=str(e))
            self.enabled = False

    async def close(self):
        """Close HTTP client connection."""
        if self._client:
            await self._client.aclose()
        self._initialized = False

    @property
    def client(self):
        """Get HTTP client, initializing if needed."""
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                base_url=ADMIN_API_URL,
                timeout=5.0
            )
        return self._client

    async def get_relevant_memories(
        self,
        query: str,
        mode: str = "owner",
        guest_session_id: Optional[int] = None,
        limit: int = 3
    ) -> List[Dict[str, Any]]:
        """
        Retrieve relevant memories for the current query.

        Uses semantic search via Qdrant to find memories similar to the query.
        Returns empty list gracefully if Qdrant unavailable.

        Args:
            query: The user's query text
            mode: "owner" or "guest" - determines which scopes to search
            guest_session_id: Optional guest session ID for guest-scoped memories
            limit: Maximum number of memories to return

        Returns:
            List of memory dictionaries with content, scope, and score
        """
        if not self.enabled:
            return []

        try:
            params = {
                "query": query,
                "mode": mode,
                "limit": limit
            }
            if guest_session_id:
                params["guest_session_id"] = guest_session_id

            response = await self.client.get(
                "/api/memories/internal/search",
                params=params
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("qdrant_available"):
                    memories = data.get("results", [])
                    logger.debug(
                        "memories_retrieved",
                        count=len(memories),
                        mode=mode,
                        query_preview=query[:50]
                    )
                    return memories
                else:
                    logger.warning("qdrant_unavailable_for_memory_retrieval")
            else:
                logger.warning(
                    "memory_search_api_error",
                    status_code=response.status_code
                )

        except Exception as e:
            logger.warning("memory_retrieval_failed", error=str(e))

        return []

    async def create_memory(
        self,
        content: str,
        mode: str = "owner",
        guest_session_id: Optional[int] = None,
        category: str = "conversation",
        importance: float = 0.5,
        source_query: Optional[str] = None
    ) -> bool:
        """
        Create a new memory from a conversation.

        The Admin Backend handles scope determination, embedding generation,
        and storage in both PostgreSQL and Qdrant.

        Args:
            content: The memory content to store
            mode: "owner" or "guest" - affects scope determination
            guest_session_id: Required if mode is "guest"
            category: Memory category (preference, fact, interaction, etc.)
            importance: 0-1 importance score
            source_query: Original query that led to this memory

        Returns:
            True if memory was created, False otherwise
        """
        if not self.enabled:
            return False

        try:
            params = {
                "content": content,
                "mode": mode,
                "category": category,
                "importance": importance
            }
            if guest_session_id:
                params["guest_session_id"] = guest_session_id
            if source_query:
                params["source_query"] = source_query

            response = await self.client.post(
                "/api/memories/internal/create",
                params=params
            )

            if response.status_code == 200:
                data = response.json()
                created = data.get("created", False)
                if created:
                    logger.info(
                        "memory_created",
                        memory_id=data.get("memory_id"),
                        mode=mode,
                        category=category
                    )
                else:
                    logger.debug(
                        "memory_not_created",
                        reason=data.get("reason", "unknown")
                    )
                return created
            else:
                logger.warning(
                    "memory_create_api_error",
                    status_code=response.status_code
                )

        except Exception as e:
            logger.warning("memory_creation_failed", error=str(e))

        return False

    async def get_active_guest_session(self) -> Optional[Dict[str, Any]]:
        """
        Get the currently active guest session.

        Used to determine guest_session_id for guest-scoped memories.

        Returns:
            Guest session dict or None if no active session
        """
        try:
            response = await self.client.get(
                "/api/memories/guest-sessions/active"
            )

            if response.status_code == 200:
                session = response.json()
                if session:
                    logger.debug(
                        "active_guest_session",
                        guest_name=session.get("guest_name"),
                        session_id=session.get("id")
                    )
                return session

        except Exception as e:
            logger.warning("get_active_guest_session_failed", error=str(e))

        return None

    def format_memory_context(self, memories: List[Dict[str, Any]]) -> str:
        """
        Format memories for LLM context injection.

        Creates a formatted string suitable for appending to system prompts.

        Args:
            memories: List of memory dictionaries from get_relevant_memories

        Returns:
            Formatted string for LLM context, or empty string if no memories
        """
        if not memories:
            return ""

        lines = ["\n\nRelevant memories about this user/property:"]
        for m in memories:
            # Scope indicators for visual clarity
            scope = m.get("scope", "unknown")
            scope_marker = {
                "global": "[General]",
                "owner": "[Owner]",
                "guest": "[Guest]"
            }.get(scope, "[Unknown]")

            content = m.get("content", "")
            score = m.get("score", 0)

            # Include score if significant
            if score >= 0.9:
                lines.append(f"- {scope_marker} {content}")
            else:
                lines.append(f"- {scope_marker} {content}")

        return "\n".join(lines)

    def should_create_memory(
        self,
        query: str,
        response: str,
        intent: Optional[str] = None
    ) -> bool:
        """
        Determine if a memory should be created from this interaction.

        Uses heuristics to identify memorable interactions:
        - Personal preferences expressed
        - Facts about the user/property shared
        - Specific requests or feedback

        Args:
            query: User's query
            response: Assistant's response
            intent: Detected intent type (optional)

        Returns:
            True if memory should be created
        """
        # Keywords that suggest memorable content
        preference_keywords = [
            "i like", "i prefer", "i love", "i hate", "my favorite",
            "i always", "i never", "i usually", "don't like"
        ]

        fact_keywords = [
            "i am", "i'm", "my name is", "i work", "i have",
            "i live", "i need", "allergic to", "i can't"
        ]

        request_keywords = [
            "remember", "don't forget", "please note", "keep in mind",
            "for next time", "in the future"
        ]

        query_lower = query.lower()

        # Check for explicit memory requests
        if any(kw in query_lower for kw in request_keywords):
            return True

        # Check for preferences
        if any(kw in query_lower for kw in preference_keywords):
            return True

        # Check for personal facts
        if any(kw in query_lower for kw in fact_keywords):
            return True

        return False

    def should_forget_memory(self, query: str) -> bool:
        """
        Determine if the user wants to forget/delete a memory.

        Args:
            query: User's query

        Returns:
            True if this is a forget request
        """
        forget_keywords = [
            "forget that",
            "forget about",
            "don't remember",
            "stop remembering",
            "remove the memory",
            "delete the memory",
            "forget my",
            "forget i",
            "forget the",
            "nevermind about",
            "never mind about",
        ]

        # Exclusion patterns - these are NOT memory forget requests
        # "forget the lights" means turn off lights, not forget memory about lights
        exclusion_patterns = [
            "forget the lights", "forget the light",
            "forget it",  # Usually means "nevermind" in conversation, not memory
        ]

        query_lower = query.lower()

        # Check exclusions first
        if any(ex in query_lower for ex in exclusion_patterns):
            return False

        return any(kw in query_lower for kw in forget_keywords)

    def extract_forget_content(self, query: str) -> str:
        """
        Extract what the user wants to forget from the query.

        Args:
            query: User's query

        Returns:
            The content/topic to search for and delete
        """
        query_lower = query.lower()

        # Patterns to remove to get the core content
        prefixes = [
            "forget that ",
            "forget about ",
            "don't remember ",
            "stop remembering ",
            "remove the memory about ",
            "remove the memory that ",
            "delete the memory about ",
            "delete the memory that ",
            "forget my ",
            "forget i ",
            "forget the ",
            "nevermind about ",
            "never mind about ",
            "please ",
            "can you ",
            "could you ",
        ]

        content = query_lower
        for prefix in prefixes:
            if content.startswith(prefix):
                content = content[len(prefix):]
            content = content.replace(prefix, " ")

        # Clean up
        content = " ".join(content.split())
        return content.strip()

    async def delete_memory_by_content(
        self,
        search_query: str,
        mode: str = "owner"
    ) -> Dict[str, Any]:
        """
        Search for and delete memories matching the search query.

        Args:
            search_query: Content to search for
            mode: "owner" or "guest"

        Returns:
            Dict with deleted count and details
        """
        if not self.enabled:
            return {"deleted": 0, "error": "Memory manager disabled"}

        try:
            # Call admin API to delete by content search
            response = await self.client.post(
                "/api/memories/internal/forget",
                params={
                    "search_query": search_query,
                    "mode": mode
                }
            )

            if response.status_code == 200:
                data = response.json()
                deleted_count = data.get("deleted", 0)
                if deleted_count > 0:
                    logger.info(
                        "memories_deleted",
                        count=deleted_count,
                        search_query=search_query[:50]
                    )
                return data
            else:
                logger.warning(
                    "memory_delete_api_error",
                    status_code=response.status_code
                )
                return {"deleted": 0, "error": f"API error: {response.status_code}"}

        except Exception as e:
            logger.warning("memory_deletion_failed", error=str(e))
            return {"deleted": 0, "error": str(e)}

    def extract_memorable_fact(
        self,
        query: str,
        response: str,
        intent: Optional[str] = None
    ) -> str:
        """
        Extract the memorable fact from a query/response pair.

        Converts first-person statements to third-person for storage.

        Args:
            query: User's query
            response: Assistant's response
            intent: Detected intent type

        Returns:
            Extracted memorable content
        """
        # For now, use the query as the memorable content
        # Future: Use LLM to extract/summarize the key fact
        content = query

        # Simple first-to-third person conversion
        replacements = [
            ("i like", "User likes"),
            ("i prefer", "User prefers"),
            ("i love", "User loves"),
            ("i hate", "User hates"),
            ("i'm allergic", "User is allergic"),
            ("i am allergic", "User is allergic"),
            ("my favorite", "User's favorite"),
            ("i can't", "User can't"),
            ("i don't", "User doesn't"),
            ("i'm", "User is"),
            ("i am", "User is"),
        ]

        for old, new in replacements:
            if old in content.lower():
                # Case-insensitive replacement
                import re
                content = re.sub(re.escape(old), new, content, flags=re.IGNORECASE)
                break

        return content

    def calculate_importance(
        self,
        query: str,
        response: str,
        intent: Optional[str] = None
    ) -> float:
        """
        Calculate importance score for a potential memory.

        Args:
            query: User's query
            response: Assistant's response
            intent: Detected intent type

        Returns:
            Importance score 0-1
        """
        importance = 0.5  # Base importance
        query_lower = query.lower()

        # Explicit memory requests are high importance
        if any(kw in query_lower for kw in ["remember", "don't forget", "keep in mind"]):
            importance = 0.9

        # Allergies and safety info are high importance
        elif any(kw in query_lower for kw in ["allergic", "allergy", "can't eat", "medical"]):
            importance = 0.85

        # Preferences are medium-high importance
        elif any(kw in query_lower for kw in ["favorite", "prefer", "love", "hate"]):
            importance = 0.7

        # General facts are medium importance
        elif any(kw in query_lower for kw in ["i am", "i'm", "my name", "i work"]):
            importance = 0.6

        return importance

    def classify_memory_category(
        self,
        query: str,
        response: str,
        intent: Optional[str] = None
    ) -> str:
        """
        Classify the category of a potential memory.

        Args:
            query: User's query
            response: Assistant's response
            intent: Detected intent type

        Returns:
            Category string (preference, fact, interaction, request, feedback)
        """
        query_lower = query.lower()

        # Check for preferences
        if any(kw in query_lower for kw in ["like", "prefer", "favorite", "love", "hate"]):
            return "preference"

        # Check for feedback
        if any(kw in query_lower for kw in ["thank", "great", "terrible", "feedback", "suggest"]):
            return "feedback"

        # Check for explicit requests
        if any(kw in query_lower for kw in ["remember", "don't forget", "note"]):
            return "request"

        # Check for personal facts
        if any(kw in query_lower for kw in ["i am", "i'm", "my name", "allergic"]):
            return "fact"

        # Default to interaction
        return "interaction"


# Global instance
_memory_manager: Optional[MemoryManager] = None


async def get_memory_manager() -> MemoryManager:
    """
    Get global memory manager instance.

    Returns:
        MemoryManager instance
    """
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
        await _memory_manager.initialize()
    return _memory_manager


async def close_memory_manager():
    """Close memory manager and clean up resources."""
    global _memory_manager
    if _memory_manager:
        await _memory_manager.close()
        _memory_manager = None

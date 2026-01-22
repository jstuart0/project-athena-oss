"""
Conversation Configuration Loader.

Loads conversation context and clarification settings from the Admin API
with optional Redis caching for performance.

Architecture:
    Orchestrator -> Admin API (HTTP) -> PostgreSQL databases
    (No direct database access from orchestrator)
"""

import os
import json
import structlog
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

logger = structlog.get_logger()

# Admin API configuration
# Priority: ADMIN_API_URL env var > detect K8s vs external
def _get_admin_url() -> str:
    """Determine the correct Admin API URL based on environment."""
    # Explicit env var takes priority
    explicit_url = os.getenv("ADMIN_API_URL") or os.getenv("ADMIN_BACKEND_URL")
    if explicit_url:
        return explicit_url

    # Check if running inside Kubernetes
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return "http://athena-admin-backend.athena-admin.svc.cluster.local:8080"

    # Running outside K8s (local dev) - use localhost
    return "http://localhost:8080"

ADMIN_API_URL = _get_admin_url()

# Redis connection (optional - graceful degradation if not available)
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_ENABLED = os.getenv("REDIS_ENABLED", "false").lower() == "true"
CACHE_TTL = 300  # 5 minutes

# In-memory fallback cache
_memory_cache: Dict[str, tuple[Any, datetime]] = {}


class ConversationConfig:
    """Configuration manager for conversation context and clarification features."""

    def __init__(self):
        """Initialize configuration manager."""
        self.redis_client = None
        self.http_client = None
        self._initialized = False

    async def initialize(self):
        """Initialize HTTP client and optional Redis client."""
        if self._initialized:
            return

        try:
            # Initialize HTTP client for Admin API
            import httpx
            self.http_client = httpx.AsyncClient(
                base_url=ADMIN_API_URL,
                timeout=3.0  # Reduced from 10s - analytics/config should be fast
            )
            logger.info("config_loader_http_client_ready", admin_api_url=ADMIN_API_URL)

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
                    logger.info("config_loader_redis_connected", host=REDIS_HOST)
                except Exception as e:
                    logger.warning("config_loader_redis_unavailable", error=str(e))
                    self.redis_client = None

            self._initialized = True

        except Exception as e:
            logger.error("config_loader_init_failed", error=str(e))
            raise

    async def close(self):
        """Close HTTP and Redis connections."""
        if self.http_client:
            await self.http_client.aclose()
        if self.redis_client:
            await self.redis_client.close()
        self._initialized = False

    async def _get_from_cache(self, key: str) -> Optional[Any]:
        """Get value from cache (Redis or memory fallback)."""
        # Try Redis first
        if self.redis_client:
            try:
                value = await self.redis_client.get(key)
                if value:
                    logger.debug("config_cache_hit", key=key, source="redis")
                    return json.loads(value)
            except Exception as e:
                logger.warning("config_redis_get_failed", key=key, error=str(e))

        # Fallback to memory cache
        if key in _memory_cache:
            cached_value, cached_time = _memory_cache[key]
            if datetime.utcnow() - cached_time < timedelta(seconds=CACHE_TTL):
                logger.debug("config_cache_hit", key=key, source="memory")
                return cached_value
            else:
                # Expired
                del _memory_cache[key]

        return None

    async def _set_to_cache(self, key: str, value: Any):
        """Set value in cache (Redis or memory fallback)."""
        # Try Redis first
        if self.redis_client:
            try:
                await self.redis_client.setex(
                    key,
                    CACHE_TTL,
                    json.dumps(value)
                )
                logger.debug("config_cached", key=key, source="redis")
                return
            except Exception as e:
                logger.warning("config_redis_set_failed", key=key, error=str(e))

        # Fallback to memory cache
        _memory_cache[key] = (value, datetime.utcnow())
        logger.debug("config_cached", key=key, source="memory")

    async def _fetch_from_api(self, endpoint: str, default: Any = None) -> Any:
        """Fetch data from Admin API."""
        try:
            response = await self.http_client.get(endpoint)
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(
                    "admin_api_error",
                    endpoint=endpoint,
                    status_code=response.status_code
                )
                return default
        except Exception as e:
            logger.error("admin_api_fetch_failed", endpoint=endpoint, error=str(e))
            return default

    async def get_conversation_settings(self) -> Dict[str, Any]:
        """
        Get conversation context settings.

        Returns:
            Dictionary with conversation settings (enabled, max_messages, timeout, etc.)
        """
        cache_key = "conversation:settings"

        # Check cache first
        cached = await self._get_from_cache(cache_key)
        if cached:
            return cached

        # Fetch from Admin API
        settings = await self._fetch_from_api(
            "/api/internal/config/conversation",
            default={
                "enabled": True,
                "use_context": True,
                "max_messages": 20,
                "timeout_seconds": 1800,
                "cleanup_interval_seconds": 60,
                "session_ttl_seconds": 3600,
                "max_llm_history_messages": 10,
                "history_mode": "full"
            }
        )

        # Cache and return
        await self._set_to_cache(cache_key, settings)
        logger.info("conversation_settings_loaded", enabled=settings.get("enabled"))
        return settings

    async def get_clarification_settings(self) -> Dict[str, Any]:
        """
        Get global clarification settings.

        Returns:
            Dictionary with clarification settings (enabled, timeout_seconds)
        """
        cache_key = "clarification:settings"

        # Check cache first
        cached = await self._get_from_cache(cache_key)
        if cached:
            return cached

        # Fetch from Admin API
        settings = await self._fetch_from_api(
            "/api/internal/config/clarification",
            default={
                "enabled": True,
                "timeout_seconds": 300
            }
        )

        # Cache and return
        await self._set_to_cache(cache_key, settings)
        logger.info("clarification_settings_loaded", enabled=settings.get("enabled"))
        return settings

    async def get_clarification_types(self) -> List[Dict[str, Any]]:
        """
        Get all clarification types with their configurations.

        Returns:
            List of dictionaries, each containing a clarification type configuration
        """
        cache_key = "clarification:types"

        # Check cache first
        cached = await self._get_from_cache(cache_key)
        if cached:
            return cached

        # Fetch from Admin API
        types = await self._fetch_from_api(
            "/api/internal/config/clarification-types",
            default=[]
        )

        # Cache and return
        await self._set_to_cache(cache_key, types)
        logger.info("clarification_types_loaded", count=len(types))
        return types

    async def get_sports_teams(self) -> List[Dict[str, Any]]:
        """
        Get sports team disambiguation rules.

        Returns:
            List of dictionaries with team names and disambiguation options
        """
        cache_key = "clarification:sports_teams"

        # Check cache first
        cached = await self._get_from_cache(cache_key)
        if cached:
            return cached

        # Fetch from Admin API
        teams = await self._fetch_from_api(
            "/api/internal/config/sports-teams",
            default=[]
        )

        # Cache and return
        await self._set_to_cache(cache_key, teams)
        logger.info("sports_teams_loaded", count=len(teams))
        return teams

    async def get_device_rules(self) -> List[Dict[str, Any]]:
        """
        Get device disambiguation rules.

        Returns:
            List of dictionaries with device types and disambiguation rules
        """
        cache_key = "clarification:device_rules"

        # Check cache first
        cached = await self._get_from_cache(cache_key)
        if cached:
            return cached

        # Fetch from Admin API
        rules = await self._fetch_from_api(
            "/api/internal/config/device-rules",
            default=[]
        )

        # Cache and return
        await self._set_to_cache(cache_key, rules)
        logger.info("device_rules_loaded", count=len(rules))
        return rules

    async def get_all_config(self) -> Dict[str, Any]:
        """
        Get all configuration in a single request.
        More efficient for orchestrator startup.
        """
        cache_key = "config:all"

        # Check cache first
        cached = await self._get_from_cache(cache_key)
        if cached:
            return cached

        # Fetch bundled config from Admin API
        config = await self._fetch_from_api(
            "/api/internal/config/all",
            default={}
        )

        # Cache and return
        await self._set_to_cache(cache_key, config)
        logger.info("all_config_loaded")
        return config

    async def log_analytics_event(
        self,
        session_id: str,
        event_type: str,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Log a conversation analytics event to the Admin API.

        Args:
            session_id: Conversation session ID
            event_type: Type of event (e.g., 'session_created', 'followup_detected', 'query_intent')
            metadata: Optional event metadata
        """
        try:
            # Log locally for debugging
            logger.debug(
                "analytics_event",
                session_id=session_id,
                event_type=event_type,
                metadata=metadata
            )

            # POST to Admin API for persistent storage
            if self.http_client:
                response = await self.http_client.post(
                    "/api/internal/analytics/log",
                    json={
                        "session_id": session_id,
                        "event_type": event_type,
                        "metadata": metadata
                    }
                )
                if response.status_code != 200:
                    logger.warning(
                        "analytics_api_error",
                        status_code=response.status_code,
                        event_type=event_type
                    )
            else:
                logger.warning("analytics_http_client_not_initialized")

        except Exception as e:
            # Don't fail the request if analytics logging fails
            logger.warning("analytics_event_log_failed", error=str(e), event_type=event_type)

    async def reload_config(self):
        """
        Reload all configuration (bypass cache).

        Useful when configuration changes in Admin Panel.
        """
        logger.info("config_reload_requested")

        # Clear cache
        if self.redis_client:
            try:
                # Only clear config-related keys
                keys = await self.redis_client.keys("conversation:*")
                keys += await self.redis_client.keys("clarification:*")
                keys += await self.redis_client.keys("config:*")
                if keys:
                    await self.redis_client.delete(*keys)
                logger.info("redis_cache_cleared", keys_cleared=len(keys))
            except Exception as e:
                logger.warning("redis_cache_clear_failed", error=str(e))

        # Clear memory cache
        _memory_cache.clear()
        logger.info("memory_cache_cleared")

        # Reload all configs
        await self.get_conversation_settings()
        await self.get_clarification_settings()
        await self.get_clarification_types()
        await self.get_sports_teams()
        await self.get_device_rules()

        logger.info("config_reloaded")


# Global instance
_config: Optional[ConversationConfig] = None


async def get_config() -> ConversationConfig:
    """
    Get global configuration instance.

    Returns:
        ConversationConfig instance
    """
    global _config
    if _config is None:
        _config = ConversationConfig()
        await _config.initialize()
    return _config


async def reload_config():
    """Reload configuration from Admin API."""
    config = await get_config()
    await config.reload_config()


async def clear_cache():
    """
    Clear all configuration caches without reloading.

    Used by the cache invalidation endpoint for instant flag updates.
    """
    global _memory_cache
    _memory_cache.clear()

    if _config and _config.redis_client:
        try:
            # Clear config-related keys from Redis
            keys = await _config.redis_client.keys("conversation:*")
            keys += await _config.redis_client.keys("clarification:*")
            keys += await _config.redis_client.keys("config:*")
            if keys:
                await _config.redis_client.delete(*keys)
            logger.info("cache_cleared", redis_keys=len(keys), memory=True)
        except Exception as e:
            logger.warning("redis_cache_clear_failed", error=str(e))
    else:
        logger.info("cache_cleared", memory=True, redis=False)


# Convenience functions for common operations

async def is_conversation_enabled() -> bool:
    """Check if conversation context is enabled."""
    config = await get_config()
    settings = await config.get_conversation_settings()
    return settings.get("enabled", True)


async def is_clarification_enabled() -> bool:
    """Check if clarification system is enabled."""
    config = await get_config()
    settings = await config.get_clarification_settings()
    return settings.get("enabled", True)


async def get_max_messages() -> int:
    """Get maximum number of messages to keep in conversation history."""
    config = await get_config()
    settings = await config.get_conversation_settings()
    return settings.get("max_messages", 20)


async def get_session_timeout() -> int:
    """Get conversation session timeout in seconds."""
    config = await get_config()
    settings = await config.get_conversation_settings()
    return settings.get("timeout_seconds", 1800)

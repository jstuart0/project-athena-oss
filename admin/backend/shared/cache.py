"""Redis caching for Project Athena

Redis URL is fetched from admin backend, with fallback to REDIS_URL env var.
No hardcoded defaults - configuration must come from admin or environment.
"""

import os
import json
import httpx
import redis.asyncio as redis
from typing import Optional, Any
from functools import wraps
import logging

logger = logging.getLogger(__name__)

# Admin backend URL for fetching configuration
ADMIN_BACKEND_URL = os.getenv("ADMIN_BACKEND_URL", "http://localhost:8080")

# Cached Redis URL (fetched once from admin backend)
_cached_redis_url: Optional[str] = None


def _fetch_redis_url_sync() -> Optional[str]:
    """Synchronously fetch Redis URL from admin backend."""
    try:
        response = httpx.get(
            f"{ADMIN_BACKEND_URL}/api/external-api-keys/public/redis/credentials",
            timeout=5.0
        )
        if response.status_code == 200:
            data = response.json()
            url = data.get("endpoint_url")
            if url:
                logger.info(f"Redis URL fetched from admin backend: {url}")
                return url
    except Exception as e:
        logger.warning(f"Failed to fetch Redis URL from admin backend: {e}")
    return None


async def _fetch_redis_url_async() -> Optional[str]:
    """Asynchronously fetch Redis URL from admin backend."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{ADMIN_BACKEND_URL}/api/external-api-keys/public/redis/credentials"
            )
            if response.status_code == 200:
                data = response.json()
                url = data.get("endpoint_url")
                if url:
                    logger.info(f"Redis URL fetched from admin backend: {url}")
                    return url
    except Exception as e:
        logger.warning(f"Failed to fetch Redis URL from admin backend: {e}")
    return None


def get_redis_url() -> str:
    """
    Get Redis URL with priority:
    1. Cached URL (already fetched from admin)
    2. Admin backend (external_api_keys table)
    3. REDIS_URL environment variable

    Raises ValueError if no Redis URL is available.
    """
    global _cached_redis_url

    # Return cached URL if available
    if _cached_redis_url:
        return _cached_redis_url

    # Try to fetch from admin backend
    url = _fetch_redis_url_sync()
    if url:
        _cached_redis_url = url
        return url

    # Fall back to environment variable
    env_url = os.getenv("REDIS_URL")
    if env_url:
        logger.info(f"Using Redis URL from environment: {env_url}")
        _cached_redis_url = env_url
        return env_url

    # No URL available
    raise ValueError(
        "Redis URL not configured. Set via admin backend (external_api_keys.redis) "
        "or REDIS_URL environment variable."
    )


class CacheClient:
    """Redis cache client with async support.

    Fetches Redis URL from admin backend with fallback to REDIS_URL env var.
    """

    def __init__(self, url: Optional[str] = None):
        """Initialize Redis client.

        Args:
            url: Optional Redis URL. If not provided, fetches from admin backend
                 or REDIS_URL environment variable.
        """
        if url:
            self.url = url
        else:
            try:
                self.url = get_redis_url()
            except ValueError as e:
                logger.error(f"Redis initialization failed: {e}")
                # Set to None to allow graceful degradation
                self.url = None
                self.client = None
                return

        self.client = redis.from_url(self.url, decode_responses=True)

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache. Returns None on connection errors."""
        if not self.client:
            return None
        try:
            value = await self.client.get(key)
            if value:
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return value
            return None
        except Exception:
            # Redis unavailable - return None gracefully
            return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """Set value in cache with optional TTL (seconds). Silently fails on connection errors."""
        if not self.client:
            return
        try:
            serialized = json.dumps(value) if not isinstance(value, str) else value
            if ttl:
                await self.client.setex(key, ttl, serialized)
            else:
                await self.client.set(key, serialized)
        except Exception:
            # Redis unavailable - silently continue
            pass

    async def delete(self, key: str):
        """Delete key from cache. Silently fails on connection errors."""
        if not self.client:
            return
        try:
            await self.client.delete(key)
        except Exception:
            pass

    async def exists(self, key: str) -> bool:
        """Check if key exists in cache. Returns False on connection errors."""
        if not self.client:
            return False
        try:
            return await self.client.exists(key) > 0
        except Exception:
            return False

    async def ping(self) -> bool:
        """Check if Redis is available. Returns False on connection errors."""
        if not self.client:
            return False
        try:
            await self.client.ping()
            return True
        except Exception:
            return False

    async def connect(self):
        """Connect to Redis (no-op for compatibility with RAG services)"""
        # Connection is established in __init__, this is for compatibility
        pass

    async def disconnect(self):
        """Disconnect from Redis (alias for close)"""
        await self.close()

    async def close(self):
        """Close Redis connection"""
        if self.client:
            await self.client.aclose()


# Global cache client singleton
_global_cache_client: Optional[CacheClient] = None


def get_cache_client() -> CacheClient:
    """Get or create global cache client singleton."""
    global _global_cache_client
    if _global_cache_client is None:
        _global_cache_client = CacheClient()
    return _global_cache_client


def reset_cache_client():
    """Reset the global cache client (for testing or reconfiguration)."""
    global _global_cache_client, _cached_redis_url
    if _global_cache_client and _global_cache_client.client:
        # Note: This should ideally be awaited, but we provide sync interface
        pass
    _global_cache_client = None
    _cached_redis_url = None


def cached(ttl: int = 3600, key_prefix: str = "athena"):
    """Decorator to cache async function results

    Args:
        ttl: Time to live in seconds
        key_prefix: Prefix for cache keys
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Generate cache key from function name and args
            cache_key = f"{key_prefix}:{func.__name__}:{hash(str(args) + str(kwargs))}"

            # OPTIMIZATION: Reuse global cache client
            cache = get_cache_client()

            try:
                # Try to get from cache
                cached_result = await cache.get(cache_key)

                if cached_result is not None:
                    return cached_result
            except Exception:
                # Cache read error, continue to function call
                pass

            # Call function and cache result
            result = await func(*args, **kwargs)

            try:
                await cache.set(cache_key, result, ttl)
            except Exception:
                # Cache write error, return result anyway
                pass

            return result
        return wrapper
    return decorator

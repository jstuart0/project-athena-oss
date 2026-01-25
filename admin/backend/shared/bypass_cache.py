"""
Local Caching for Bypass Configurations

Provides local caching for bypass configurations to eliminate admin backend
as a single point of failure. Falls back to cached values if admin is unavailable.

Open Source Compatible - No vendor-specific dependencies.
"""

import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
import httpx
import structlog

logger = structlog.get_logger(__name__)

# Local cache with TTL
_bypass_cache: Dict[str, Dict[str, Any]] = {}
_cache_timestamps: Dict[str, datetime] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes


async def get_bypass_config(service_name: str, admin_url: str) -> Optional[Dict[str, Any]]:
    """
    Get bypass config with local caching.

    Falls back to cached value if admin backend is unavailable.
    This ensures the system continues to function even if admin is down.

    Args:
        service_name: Name of the RAG service
        admin_url: Admin backend URL

    Returns:
        Bypass configuration dict or None if not configured
    """
    cache_key = service_name
    now = datetime.now(timezone.utc)

    # Check if we have a valid cached value
    cached = _bypass_cache.get(cache_key)
    cached_at = _cache_timestamps.get(cache_key)

    cache_valid = (
        cached is not None and
        cached_at is not None and
        (now - cached_at).total_seconds() < CACHE_TTL_SECONDS
    )

    if cache_valid:
        logger.debug("bypass_cache_hit", service=service_name)
        return cached

    # Try to fetch from admin backend
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{admin_url}/api/rag-service-bypass/public/{service_name}/config"
            )
            if response.status_code == 200:
                config = response.json()
                # Update cache
                _bypass_cache[cache_key] = config
                _cache_timestamps[cache_key] = now
                logger.debug("bypass_cache_updated", service=service_name)
                return config
            elif response.status_code == 404:
                # Service not configured for bypass - cache the null result
                _bypass_cache[cache_key] = {}
                _cache_timestamps[cache_key] = now
                return None

    except Exception as e:
        logger.warning("bypass_cache_fetch_failed", service=service_name, error=str(e))

        # Return stale cache if available (better than nothing)
        if cached is not None:
            stale_age = (now - cached_at).total_seconds() if cached_at else None
            logger.info(
                "bypass_cache_stale_fallback",
                service=service_name,
                age_seconds=stale_age
            )
            return cached

    return None


async def refresh_all_bypass_configs(admin_url: str, services: List[str]) -> None:
    """
    Pre-fetch all bypass configs on startup.

    Call this during service initialization to warm the cache.

    Args:
        admin_url: Admin backend URL
        services: List of service names to pre-fetch
    """
    logger.info("bypass_cache_warmup_starting", service_count=len(services))

    for service in services:
        try:
            await get_bypass_config(service, admin_url)
        except Exception as e:
            logger.warning(
                "bypass_cache_warmup_failed",
                service=service,
                error=str(e)
            )

    logger.info("bypass_cache_warmup_complete", cached_count=len(_bypass_cache))


def invalidate_bypass_cache(service_name: Optional[str] = None) -> None:
    """
    Invalidate bypass config cache.

    Args:
        service_name: Specific service to invalidate, or None to clear all
    """
    if service_name:
        _bypass_cache.pop(service_name, None)
        _cache_timestamps.pop(service_name, None)
        logger.debug("bypass_cache_invalidated", service=service_name)
    else:
        _bypass_cache.clear()
        _cache_timestamps.clear()
        logger.debug("bypass_cache_cleared_all")


def get_cache_status() -> Dict[str, Any]:
    """
    Get current cache status for monitoring.

    Returns:
        Dict with cache statistics
    """
    now = datetime.now(timezone.utc)
    return {
        "cached_services": list(_bypass_cache.keys()),
        "cache_size": len(_bypass_cache),
        "ages": {
            service: (now - ts).total_seconds() if ts else None
            for service, ts in _cache_timestamps.items()
        },
        "ttl_seconds": CACHE_TTL_SECONDS,
    }

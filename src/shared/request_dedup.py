"""
Request Deduplication for Cloud LLM Calls

Prevents duplicate cloud LLM calls when the same request is made multiple
times in quick succession. This saves costs and reduces latency.

How it works:
1. Hash the request (model + prompt + key params)
2. If identical request is in-flight, wait for its result
3. Brief result caching for burst protection

Open Source Compatible - No vendor-specific dependencies.
"""

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Dict, Optional, Any, Callable, Awaitable
import structlog

logger = structlog.get_logger(__name__)

# In-flight requests tracking
_inflight: Dict[str, asyncio.Future] = {}
_inflight_lock = asyncio.Lock()

# Short-term result cache (for very fast repeated queries)
_result_cache: Dict[str, tuple[datetime, Any]] = {}
RESULT_CACHE_TTL_SECONDS = 5  # Very short - just for burst protection


def _hash_request(model: str, prompt: str, **kwargs) -> str:
    """
    Generate hash for request deduplication.

    Only includes deterministic parameters to ensure proper deduplication.

    Args:
        model: Model name
        prompt: User prompt
        **kwargs: Additional parameters (temperature, max_tokens, etc.)

    Returns:
        32-character hex hash
    """
    # Include model, prompt, and key generation parameters
    temperature = kwargs.get('temperature', 0.7)
    max_tokens = kwargs.get('max_tokens', 1024)

    content = f"{model}:{prompt}:{temperature}:{max_tokens}"
    return hashlib.sha256(content.encode()).hexdigest()[:32]


async def deduplicated_call(
    model: str,
    prompt: str,
    call_func: Callable[..., Awaitable[Any]],
    **kwargs
) -> Any:
    """
    Execute call with deduplication.

    If an identical request is in-flight, wait for its result instead
    of making a duplicate call. This saves costs on cloud providers.

    Args:
        model: Model name
        prompt: User prompt
        call_func: Async function to call (signature: model, prompt, **kwargs)
        **kwargs: Additional parameters to pass to call_func

    Returns:
        Result from call_func (possibly cached or from another in-flight request)
    """
    request_hash = _hash_request(model, prompt, **kwargs)
    now = datetime.now(timezone.utc)

    # Check short-term cache first
    if request_hash in _result_cache:
        cached_at, result = _result_cache[request_hash]
        cache_age = (now - cached_at).total_seconds()
        if cache_age < RESULT_CACHE_TTL_SECONDS:
            logger.info(
                "request_dedup_cache_hit",
                hash=request_hash[:8],
                age_seconds=cache_age
            )
            return result

    async with _inflight_lock:
        # Check if request is already in-flight
        if request_hash in _inflight:
            logger.info("request_dedup_waiting", hash=request_hash[:8])
            # Release lock and wait for the in-flight request to complete
            future = _inflight[request_hash]

    # If we found an in-flight request, wait for it outside the lock
    if request_hash in _inflight:
        try:
            return await future
        except Exception:
            # If the in-flight request failed, we'll try again
            pass

    # Create a new future for this request
    future: asyncio.Future = asyncio.get_event_loop().create_future()

    async with _inflight_lock:
        # Double-check no one else started this request
        if request_hash in _inflight:
            return await _inflight[request_hash]

        _inflight[request_hash] = future

    try:
        # Execute the actual call
        result = await call_func(model, prompt, **kwargs)

        # Cache result briefly
        _result_cache[request_hash] = (now, result)

        # Clean up old cache entries
        _cleanup_result_cache()

        # Complete the future for any waiters
        if not future.done():
            future.set_result(result)

        return result

    except Exception as e:
        if not future.done():
            future.set_exception(e)
        raise

    finally:
        async with _inflight_lock:
            _inflight.pop(request_hash, None)


def _cleanup_result_cache() -> None:
    """Remove expired entries from result cache."""
    now = datetime.now(timezone.utc)
    expired = [
        key for key, (cached_at, _) in _result_cache.items()
        if (now - cached_at).total_seconds() > RESULT_CACHE_TTL_SECONDS * 2
    ]
    for key in expired:
        _result_cache.pop(key, None)


def get_dedup_stats() -> Dict[str, Any]:
    """
    Get deduplication statistics for monitoring.

    Returns:
        Dict with statistics about in-flight requests and cache
    """
    now = datetime.now(timezone.utc)
    return {
        "inflight_count": len(_inflight),
        "cache_entries": len(_result_cache),
        "cache_ages": {
            k[:8]: (now - ts).total_seconds()
            for k, (ts, _) in _result_cache.items()
        },
        "result_cache_ttl_seconds": RESULT_CACHE_TTL_SECONDS,
    }


def clear_dedup_cache() -> None:
    """Clear all deduplication caches. Useful for testing."""
    _result_cache.clear()
    logger.debug("request_dedup_cache_cleared")

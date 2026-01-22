"""
Rate Limiter Implementation for Orchestrator RAG Services

Token bucket rate limiting with per-service and per-session support.
Designed to protect RAG services from overload and integrate with circuit breakers.

The token bucket algorithm allows burst traffic up to bucket capacity,
then enforces a steady-state rate limit.

Usage:
    from orchestrator.rate_limiter import (
        get_rate_limiter_registry,
        with_rate_limit
    )

    # Simple usage with wrapper
    result = await with_rate_limit(
        "weather",
        fetch_weather_data,
        location="Baltimore"
    )

    # Per-session limiting
    result = await with_rate_limit(
        "dining",
        search_restaurants,
        session_id=user_session,
        cuisine="italian"
    )

    # Manual usage
    registry = get_rate_limiter_registry()
    limiter = registry.get_limiter("sports")

    if await limiter.acquire():
        result = await fetch_sports()
    else:
        raise RateLimitError("Rate limit exceeded")
"""
import asyncio
import time
from typing import Dict, Optional, Callable, Any, TypeVar
from dataclasses import dataclass, field
import structlog

logger = structlog.get_logger()

T = TypeVar('T')


@dataclass
class RateLimitConfig:
    """Configuration for a rate limiter."""
    requests_per_minute: int = 60
    burst_multiplier: float = 2.0
    per_session: bool = False  # If true, each session gets its own bucket


# Default configurations per service type
DEFAULT_RATE_CONFIGS: Dict[str, RateLimitConfig] = {
    # High-frequency services (fast, cheap calls)
    "weather": RateLimitConfig(
        requests_per_minute=120,
        burst_multiplier=2.0
    ),

    # Medium-frequency services
    "sports": RateLimitConfig(
        requests_per_minute=60,
        burst_multiplier=2.0
    ),
    "news": RateLimitConfig(
        requests_per_minute=60,
        burst_multiplier=2.0
    ),
    "events": RateLimitConfig(
        requests_per_minute=60,
        burst_multiplier=2.0
    ),

    # Lower-frequency services (expensive API calls)
    "dining": RateLimitConfig(
        requests_per_minute=30,
        burst_multiplier=1.5
    ),
    "flights": RateLimitConfig(
        requests_per_minute=30,
        burst_multiplier=1.5
    ),
    "streaming": RateLimitConfig(
        requests_per_minute=30,
        burst_multiplier=1.5
    ),

    # Rate-limited external APIs
    "stocks": RateLimitConfig(
        requests_per_minute=20,
        burst_multiplier=1.5
    ),
    "websearch": RateLimitConfig(
        requests_per_minute=20,
        burst_multiplier=1.5
    ),

    # Default for unknown services
    "default": RateLimitConfig(
        requests_per_minute=60,
        burst_multiplier=2.0
    )
}


class TokenBucketRateLimiter:
    """
    Token bucket rate limiter for a single service.

    Allows bursts up to capacity, then limits to refill_rate tokens per second.
    Tokens are automatically refilled based on elapsed time.
    Thread-safe using asyncio locks.
    """

    def __init__(
        self,
        name: str,
        requests_per_minute: int = 60,
        burst_multiplier: float = 2.0
    ):
        """
        Initialize rate limiter.

        Args:
            name: Service name for logging
            requests_per_minute: Steady-state rate limit
            burst_multiplier: Bucket capacity as multiple of rate (allows bursts)
        """
        self.name = name
        self.requests_per_minute = requests_per_minute
        self.burst_multiplier = burst_multiplier

        # Calculate token parameters
        self.refill_rate = requests_per_minute / 60.0  # tokens per second
        self.capacity = requests_per_minute * burst_multiplier

        # Current state
        self.tokens = self.capacity
        self.last_refill = time.time()
        self.total_acquired = 0
        self.total_rejected = 0
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> bool:
        """
        Attempt to acquire tokens.

        Args:
            tokens: Number of tokens to acquire (default 1)

        Returns:
            True if tokens acquired, False if rate limited
        """
        async with self._lock:
            now = time.time()

            # Refill tokens based on time elapsed
            time_passed = now - self.last_refill
            tokens_to_add = time_passed * self.refill_rate
            self.tokens = min(self.capacity, self.tokens + tokens_to_add)
            self.last_refill = now

            if self.tokens >= tokens:
                self.tokens -= tokens
                self.total_acquired += 1
                return True

            self.total_rejected += 1
            logger.warning(
                "rate_limit_exceeded",
                service=self.name,
                tokens_available=round(self.tokens, 2),
                tokens_requested=tokens
            )
            return False

    async def wait_and_acquire(self, tokens: float = 1.0, timeout: float = 5.0) -> bool:
        """
        Wait for tokens to become available (with timeout).

        Args:
            tokens: Number of tokens to acquire
            timeout: Maximum wait time in seconds

        Returns:
            True if tokens acquired, False if timeout exceeded
        """
        start_time = time.time()

        while True:
            if await self.acquire(tokens):
                return True

            # Check timeout
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                return False

            # Calculate wait time until enough tokens available
            async with self._lock:
                needed = tokens - self.tokens
                wait_time = min(needed / self.refill_rate, timeout - elapsed)

            await asyncio.sleep(min(wait_time, 0.1))  # Poll at most every 100ms

    def update_config(self, requests_per_minute: int) -> None:
        """
        Update rate limiter configuration.

        Args:
            requests_per_minute: New rate limit
        """
        self.requests_per_minute = requests_per_minute
        self.refill_rate = requests_per_minute / 60.0
        self.capacity = requests_per_minute * self.burst_multiplier
        # Don't reset current tokens - allow gradual adjustment
        self.tokens = min(self.tokens, self.capacity)
        logger.info(
            "rate_limiter_config_updated",
            service=self.name,
            requests_per_minute=requests_per_minute
        )

    def get_status(self) -> Dict[str, Any]:
        """Get current rate limiter status."""
        return {
            "name": self.name,
            "requests_per_minute": self.requests_per_minute,
            "capacity": self.capacity,
            "tokens_available": round(self.tokens, 2),
            "refill_rate_per_second": round(self.refill_rate, 3),
            "total_acquired": self.total_acquired,
            "total_rejected": self.total_rejected
        }

    def reset(self) -> None:
        """Reset rate limiter to initial state."""
        self.tokens = self.capacity
        self.total_acquired = 0
        self.total_rejected = 0
        logger.info("rate_limiter_reset", service=self.name)


class PerSessionRateLimiter:
    """
    Per-session rate limiter.

    Maintains separate token buckets for each session/client.
    Automatically cleans up old sessions to prevent memory leaks.
    """

    def __init__(
        self,
        name: str,
        requests_per_minute: int = 60,
        burst_multiplier: float = 2.0,
        cleanup_interval: int = 300  # 5 minutes
    ):
        """
        Initialize per-session rate limiter.

        Args:
            name: Service name for logging
            requests_per_minute: Rate limit per session
            burst_multiplier: Burst capacity multiplier
            cleanup_interval: Seconds between cleanup of old buckets
        """
        self.name = name
        self.requests_per_minute = requests_per_minute
        self.burst_multiplier = burst_multiplier
        self.cleanup_interval = cleanup_interval

        self._buckets: Dict[str, TokenBucketRateLimiter] = {}
        self._last_access: Dict[str, float] = {}
        self._last_cleanup = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self, session_id: str, tokens: float = 1.0) -> bool:
        """
        Attempt to acquire tokens for a session.

        Args:
            session_id: Session or client identifier
            tokens: Number of tokens to acquire

        Returns:
            True if tokens acquired, False if rate limited
        """
        async with self._lock:
            await self._maybe_cleanup()

            # Get or create bucket for session
            if session_id not in self._buckets:
                self._buckets[session_id] = TokenBucketRateLimiter(
                    name=f"{self.name}:{session_id[:8]}",
                    requests_per_minute=self.requests_per_minute,
                    burst_multiplier=self.burst_multiplier
                )

            self._last_access[session_id] = time.time()

        # Acquire from session's bucket (outside lock for better concurrency)
        return await self._buckets[session_id].acquire(tokens)

    async def _maybe_cleanup(self) -> None:
        """Remove old buckets to prevent memory leaks."""
        now = time.time()
        if now - self._last_cleanup < self.cleanup_interval:
            return

        # Find and remove old sessions
        expired_sessions = [
            session_id for session_id, last_access in self._last_access.items()
            if now - last_access > self.cleanup_interval
        ]

        for session_id in expired_sessions:
            del self._buckets[session_id]
            del self._last_access[session_id]

        if expired_sessions:
            logger.debug(
                "rate_limiter_session_cleanup",
                service=self.name,
                sessions_removed=len(expired_sessions)
            )

        self._last_cleanup = now

    def update_config(self, requests_per_minute: int) -> None:
        """Update rate limiter configuration for all sessions."""
        self.requests_per_minute = requests_per_minute
        for bucket in self._buckets.values():
            bucket.update_config(requests_per_minute)
        logger.info(
            "per_session_rate_limiter_updated",
            service=self.name,
            requests_per_minute=requests_per_minute,
            active_sessions=len(self._buckets)
        )

    def get_status(self) -> Dict[str, Any]:
        """Get current rate limiter status."""
        return {
            "name": self.name,
            "requests_per_minute": self.requests_per_minute,
            "active_sessions": len(self._buckets),
            "burst_multiplier": self.burst_multiplier
        }


class RateLimiterRegistry:
    """
    Registry of rate limiters for all RAG services.

    Manages individual rate limiters per service and provides
    aggregate status for health checks.
    """
    _instance: Optional["RateLimiterRegistry"] = None

    def __init__(self, configs: Optional[Dict[str, RateLimitConfig]] = None):
        """
        Initialize registry with optional custom configurations.

        Args:
            configs: Custom configurations per service. Merged with defaults.
        """
        self._configs = {**DEFAULT_RATE_CONFIGS}
        if configs:
            self._configs.update(configs)

        self._limiters: Dict[str, TokenBucketRateLimiter] = {}
        self._session_limiters: Dict[str, PerSessionRateLimiter] = {}

    def get_limiter(self, service_name: str) -> TokenBucketRateLimiter:
        """
        Get or create a rate limiter for a service.

        Args:
            service_name: Name of the service (e.g., "weather", "sports")

        Returns:
            TokenBucketRateLimiter instance for the service
        """
        if service_name not in self._limiters:
            config = self._configs.get(service_name, self._configs["default"])
            self._limiters[service_name] = TokenBucketRateLimiter(
                name=service_name,
                requests_per_minute=config.requests_per_minute,
                burst_multiplier=config.burst_multiplier
            )
            logger.debug("rate_limiter_created", service=service_name)

        return self._limiters[service_name]

    def get_session_limiter(self, service_name: str) -> PerSessionRateLimiter:
        """
        Get or create a per-session rate limiter for a service.

        Args:
            service_name: Name of the service

        Returns:
            PerSessionRateLimiter instance for the service
        """
        if service_name not in self._session_limiters:
            config = self._configs.get(service_name, self._configs["default"])
            self._session_limiters[service_name] = PerSessionRateLimiter(
                name=service_name,
                requests_per_minute=config.requests_per_minute,
                burst_multiplier=config.burst_multiplier
            )
            logger.debug("session_rate_limiter_created", service=service_name)

        return self._session_limiters[service_name]

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all rate limiters."""
        status = {}
        for name, limiter in self._limiters.items():
            status[name] = limiter.get_status()
        for name, limiter in self._session_limiters.items():
            status[f"{name}_sessions"] = limiter.get_status()
        return status

    def get_rejection_stats(self) -> Dict[str, Dict[str, int]]:
        """Get rejection statistics for all limiters."""
        return {
            name: {
                "acquired": limiter.total_acquired,
                "rejected": limiter.total_rejected
            }
            for name, limiter in self._limiters.items()
        }

    def update_config(
        self,
        service_name: str,
        requests_per_minute: int
    ) -> None:
        """Update configuration for a specific service."""
        if service_name in self._limiters:
            self._limiters[service_name].update_config(requests_per_minute)
        if service_name in self._session_limiters:
            self._session_limiters[service_name].update_config(requests_per_minute)

        # Update config for future limiters
        if service_name in self._configs:
            self._configs[service_name] = RateLimitConfig(
                requests_per_minute=requests_per_minute,
                burst_multiplier=self._configs[service_name].burst_multiplier
            )

    def reset_all(self) -> None:
        """Reset all rate limiters."""
        for limiter in self._limiters.values():
            limiter.reset()
        logger.info("all_rate_limiters_reset")


# Global registry instance
_registry: Optional[RateLimiterRegistry] = None


def get_rate_limiter_registry() -> RateLimiterRegistry:
    """Get the global rate limiter registry."""
    global _registry
    if _registry is None:
        _registry = RateLimiterRegistry()
    return _registry


def reset_rate_limiter_registry() -> None:
    """Reset the global registry (useful for testing)."""
    global _registry
    _registry = None


class RateLimitExceeded(Exception):
    """Exception raised when rate limit is exceeded."""

    def __init__(self, service_name: str, message: Optional[str] = None):
        self.service_name = service_name
        self.message = message or f"Rate limit exceeded for {service_name}"
        super().__init__(self.message)


async def with_rate_limit(
    service_name: str,
    func: Callable[..., Any],
    *args,
    session_id: Optional[str] = None,
    wait_for_token: bool = False,
    wait_timeout: float = 5.0,
    **kwargs
) -> Any:
    """
    Execute a function with rate limiting.

    If rate limit is exceeded, raises RateLimitExceeded exception.
    Optionally waits for tokens to become available.

    Args:
        service_name: Name of the service (for rate limiter lookup)
        func: Async function to execute
        *args: Positional arguments for func
        session_id: Optional session ID for per-session limiting
        wait_for_token: If True, wait for tokens instead of rejecting
        wait_timeout: Maximum wait time if wait_for_token is True
        **kwargs: Keyword arguments for func

    Returns:
        Result from func

    Raises:
        RateLimitExceeded: If rate limit exceeded and not waiting
        RateLimitExceeded: If wait timeout exceeded

    Usage:
        # Global rate limiting
        result = await with_rate_limit(
            "weather",
            fetch_weather,
            location="Baltimore"
        )

        # Per-session rate limiting
        result = await with_rate_limit(
            "dining",
            search_restaurants,
            session_id=user_session_id,
            cuisine="italian"
        )

        # Wait for token availability
        result = await with_rate_limit(
            "stocks",
            fetch_stock_quote,
            symbol="AAPL",
            wait_for_token=True,
            wait_timeout=10.0
        )
    """
    registry = get_rate_limiter_registry()

    # Use per-session or global limiter
    if session_id:
        limiter = registry.get_session_limiter(service_name)
        acquired = await limiter.acquire(session_id)
    else:
        limiter = registry.get_limiter(service_name)
        if wait_for_token:
            acquired = await limiter.wait_and_acquire(timeout=wait_timeout)
        else:
            acquired = await limiter.acquire()

    if not acquired:
        logger.warning(
            "rate_limit_rejected",
            service=service_name,
            session_id=session_id[:8] if session_id else None
        )
        raise RateLimitExceeded(service_name)

    # Execute the function
    return await func(*args, **kwargs)

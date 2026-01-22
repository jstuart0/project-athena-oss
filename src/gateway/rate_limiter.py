"""
Token Bucket Rate Limiter for Gateway

Implements rate limiting using the token bucket algorithm.
Configurable via gateway_config database table.

The token bucket allows burst traffic up to the bucket capacity,
then enforces the steady-state rate limit.
"""
import asyncio
import time
from typing import Dict, Optional
import structlog

logger = structlog.get_logger()


class TokenBucketRateLimiter:
    """
    Token bucket rate limiter.

    Allows bursts up to capacity, then limits to refill_rate tokens per second.
    Tokens are automatically refilled based on elapsed time.

    Usage:
        limiter = TokenBucketRateLimiter(requests_per_minute=60)

        if await limiter.acquire():
            # Process request
        else:
            # Return 429 Too Many Requests
    """

    def __init__(
        self,
        requests_per_minute: int = 60,
        burst_multiplier: float = 2.0
    ):
        """
        Initialize rate limiter.

        Args:
            requests_per_minute: Steady-state rate limit
            burst_multiplier: Bucket capacity as multiple of rate (allows bursts)
        """
        self.requests_per_minute = requests_per_minute
        self.burst_multiplier = burst_multiplier

        # Calculate token parameters
        self.refill_rate = requests_per_minute / 60.0  # tokens per second
        self.capacity = requests_per_minute * burst_multiplier

        # Current state
        self.tokens = self.capacity
        self.last_refill = time.time()
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
                return True

            logger.warning("rate_limit_exceeded",
                          tokens_available=self.tokens,
                          tokens_requested=tokens)
            return False

    def update_config(self, requests_per_minute: int):
        """
        Update rate limiter configuration (from database).

        Args:
            requests_per_minute: New rate limit
        """
        self.requests_per_minute = requests_per_minute
        self.refill_rate = requests_per_minute / 60.0
        self.capacity = requests_per_minute * self.burst_multiplier
        # Don't reset current tokens - allow gradual adjustment
        self.tokens = min(self.tokens, self.capacity)
        logger.info("rate_limiter_config_updated",
                   requests_per_minute=requests_per_minute)

    def get_status(self) -> dict:
        """Get current rate limiter status."""
        return {
            "requests_per_minute": self.requests_per_minute,
            "capacity": self.capacity,
            "tokens_available": self.tokens,
            "refill_rate_per_second": self.refill_rate
        }


class PerSessionRateLimiter:
    """
    Per-session rate limiter.

    Maintains separate token buckets for each session/client.
    Automatically cleans up old sessions to prevent memory leaks.
    """

    def __init__(
        self,
        requests_per_minute: int = 60,
        burst_multiplier: float = 2.0,
        cleanup_interval: int = 300  # 5 minutes
    ):
        """
        Initialize per-session rate limiter.

        Args:
            requests_per_minute: Rate limit per session
            burst_multiplier: Burst capacity multiplier
            cleanup_interval: Seconds between cleanup of old buckets
        """
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
                    requests_per_minute=self.requests_per_minute,
                    burst_multiplier=self.burst_multiplier
                )

            self._last_access[session_id] = time.time()

        # Acquire from session's bucket (outside lock for better concurrency)
        return await self._buckets[session_id].acquire(tokens)

    async def _maybe_cleanup(self):
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
            logger.debug("rate_limiter_cleanup",
                        sessions_removed=len(expired_sessions))

        self._last_cleanup = now

    def update_config(self, requests_per_minute: int):
        """
        Update rate limiter configuration for all sessions.

        Args:
            requests_per_minute: New rate limit
        """
        self.requests_per_minute = requests_per_minute
        for bucket in self._buckets.values():
            bucket.update_config(requests_per_minute)
        logger.info("per_session_rate_limiter_config_updated",
                   requests_per_minute=requests_per_minute,
                   active_sessions=len(self._buckets))

    def get_status(self) -> dict:
        """Get current rate limiter status."""
        return {
            "requests_per_minute": self.requests_per_minute,
            "active_sessions": len(self._buckets),
            "burst_multiplier": self.burst_multiplier
        }


# Global rate limiter instance
global_rate_limiter = TokenBucketRateLimiter(requests_per_minute=60)

# Per-session rate limiter (optional, for per-client limits)
session_rate_limiter = PerSessionRateLimiter(requests_per_minute=30)

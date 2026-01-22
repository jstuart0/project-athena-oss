"""
Circuit Breaker Pattern Implementation for Gateway

Prevents cascade failures when the orchestrator is unavailable.
Uses states: CLOSED (normal), OPEN (failing), HALF_OPEN (testing recovery).

Configuration is loaded from gateway_config database table.
"""
import asyncio
import time
from enum import Enum
from typing import Optional
import structlog

logger = structlog.get_logger()


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation - requests pass through
    OPEN = "open"          # Service failing - requests rejected
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker for orchestrator calls.

    When the orchestrator fails repeatedly, the circuit opens and
    requests are rejected immediately (falling back to Ollama).
    After a recovery timeout, the circuit enters half-open state
    to test if the service has recovered.

    Usage:
        breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30)

        if await breaker.can_execute():
            try:
                result = await call_orchestrator()
                await breaker.record_success()
            except Exception:
                await breaker.record_failure()
                raise
        else:
            # Fall back to Ollama
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 30,
        half_open_max_calls: int = 3
    ):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Seconds to wait before trying again (half-open)
            half_open_max_calls: Successful calls needed to close circuit
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[float] = None
        self._lock = asyncio.Lock()

    async def can_execute(self) -> bool:
        """
        Check if a request should be allowed through.

        Returns:
            True if request can proceed, False if circuit is open
        """
        async with self._lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                # Check if recovery timeout has elapsed
                if self.last_failure_time and \
                   time.time() - self.last_failure_time >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                    logger.info("circuit_breaker_half_open",
                               message="Testing if orchestrator recovered")
                    return True
                return False

            # HALF_OPEN: allow limited requests to test recovery
            return True

    async def record_success(self):
        """Record a successful call."""
        async with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.half_open_max_calls:
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    self.success_count = 0
                    logger.info("circuit_breaker_closed",
                               message="Orchestrator recovered, circuit closed")
            elif self.state == CircuitState.CLOSED:
                # Reset failure count on success
                self.failure_count = 0

    async def record_failure(self):
        """Record a failed call."""
        async with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                # Failed during recovery test - reopen circuit
                self.state = CircuitState.OPEN
                logger.warning("circuit_breaker_reopened",
                              message="Orchestrator still failing")
            elif self.state == CircuitState.CLOSED:
                if self.failure_count >= self.failure_threshold:
                    self.state = CircuitState.OPEN
                    logger.warning("circuit_breaker_opened",
                                  failures=self.failure_count,
                                  message="Orchestrator failures exceeded threshold")

    def update_config(self, failure_threshold: int, recovery_timeout: int):
        """
        Update circuit breaker configuration (from database).

        Args:
            failure_threshold: New failure threshold
            recovery_timeout: New recovery timeout in seconds
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        logger.info("circuit_breaker_config_updated",
                   failure_threshold=failure_threshold,
                   recovery_timeout=recovery_timeout)

    def get_status(self) -> dict:
        """Get current circuit breaker status."""
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
            "last_failure_time": self.last_failure_time
        }


# Global circuit breaker instance for orchestrator
orchestrator_circuit_breaker = CircuitBreaker()

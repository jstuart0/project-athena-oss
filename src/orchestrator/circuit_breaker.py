"""
Circuit Breaker Implementation for Orchestrator RAG Services

Prevents cascading failures by temporarily disabling failing RAG services.
Uses a registry pattern to manage circuit breakers for multiple services.

States:
- CLOSED: Normal operation, requests pass through
- OPEN: Service is failing, requests rejected immediately
- HALF_OPEN: Testing if service has recovered

Usage:
    from orchestrator.circuit_breaker import (
        get_circuit_breaker_registry,
        with_circuit_breaker
    )

    # Simple usage with decorator-style wrapper
    result = await with_circuit_breaker(
        "weather",
        fetch_weather_data,
        location="Baltimore",
        fallback=lambda: {"error": "Weather service unavailable"}
    )

    # Manual usage
    registry = get_circuit_breaker_registry()
    breaker = registry.get_breaker("weather")

    if await breaker.can_execute():
        try:
            result = await fetch_weather()
            await breaker.record_success()
        except Exception:
            await breaker.record_failure()
            raise
"""
import asyncio
import time
from enum import Enum
from typing import Dict, Optional, Callable, Any, TypeVar
from dataclasses import dataclass, field
import structlog

logger = structlog.get_logger()

T = TypeVar('T')


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"       # Normal operation - requests pass through
    OPEN = "open"           # Service failing - requests rejected
    HALF_OPEN = "half_open" # Testing if service recovered


@dataclass
class CircuitBreaker:
    """
    Circuit breaker for a single service.

    Tracks failures and automatically opens/closes based on thresholds.
    Thread-safe using asyncio locks.
    """
    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 3

    # State tracking (defaults via field)
    state: CircuitState = field(default=CircuitState.CLOSED)
    failure_count: int = field(default=0)
    success_count: int = field(default=0)
    last_failure_time: float = field(default=0.0)
    half_open_calls: int = field(default=0)

    # Lock for thread safety
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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
                if time.time() - self.last_failure_time >= self.recovery_timeout:
                    self._half_open()
                    return True
                return False

            # HALF_OPEN: allow limited requests to test recovery
            return self.half_open_calls < self.half_open_max_calls

    async def record_success(self) -> None:
        """Record a successful call."""
        async with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                self.half_open_calls += 1
                if self.success_count >= self.half_open_max_calls:
                    self._close()
            elif self.state == CircuitState.CLOSED:
                # Reset failure count on success
                self.failure_count = 0

    async def record_failure(self) -> None:
        """Record a failed call."""
        async with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                # Failed during recovery test - reopen circuit
                self._open()
            elif self.state == CircuitState.CLOSED:
                if self.failure_count >= self.failure_threshold:
                    self._open()

    def _open(self) -> None:
        """Transition to OPEN state (internal, no lock)."""
        logger.warning(
            "circuit_breaker_opened",
            service=self.name,
            failures=self.failure_count
        )
        self.state = CircuitState.OPEN

    def _close(self) -> None:
        """Transition to CLOSED state (internal, no lock)."""
        logger.info(
            "circuit_breaker_closed",
            service=self.name
        )
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.half_open_calls = 0

    def _half_open(self) -> None:
        """Transition to HALF_OPEN state (internal, no lock)."""
        logger.info(
            "circuit_breaker_half_open",
            service=self.name
        )
        self.state = CircuitState.HALF_OPEN
        self.success_count = 0
        self.half_open_calls = 0

    def get_status(self) -> Dict[str, Any]:
        """Get current circuit breaker status."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
            "last_failure_time": self.last_failure_time
        }

    def reset(self) -> None:
        """Reset circuit breaker to initial state."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = 0.0
        self.half_open_calls = 0
        logger.info("circuit_breaker_reset", service=self.name)


class CircuitBreakerRegistry:
    """
    Registry of circuit breakers for all RAG services.

    Manages individual circuit breakers per service and provides
    aggregate status for health checks.
    """
    _instance: Optional["CircuitBreakerRegistry"] = None

    def __init__(
        self,
        default_failure_threshold: int = 5,
        default_recovery_timeout: float = 30.0
    ):
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._default_failure_threshold = default_failure_threshold
        self._default_recovery_timeout = default_recovery_timeout

    def get_breaker(self, service_name: str) -> CircuitBreaker:
        """
        Get or create a circuit breaker for a service.

        Args:
            service_name: Name of the service (e.g., "weather", "sports")

        Returns:
            CircuitBreaker instance for the service
        """
        if service_name not in self._breakers:
            self._breakers[service_name] = CircuitBreaker(
                name=service_name,
                failure_threshold=self._default_failure_threshold,
                recovery_timeout=self._default_recovery_timeout
            )
            logger.debug(
                "circuit_breaker_created",
                service=service_name
            )
        return self._breakers[service_name]

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all circuit breakers."""
        return {name: breaker.get_status() for name, breaker in self._breakers.items()}

    def get_open_circuits(self) -> list:
        """Get list of services with open circuits."""
        return [
            name for name, breaker in self._breakers.items()
            if breaker.state == CircuitState.OPEN
        ]

    def reset_all(self) -> None:
        """Reset all circuit breakers."""
        for breaker in self._breakers.values():
            breaker.reset()
        logger.info("all_circuit_breakers_reset")

    def update_defaults(
        self,
        failure_threshold: Optional[int] = None,
        recovery_timeout: Optional[float] = None
    ) -> None:
        """Update default thresholds for new breakers."""
        if failure_threshold is not None:
            self._default_failure_threshold = failure_threshold
        if recovery_timeout is not None:
            self._default_recovery_timeout = recovery_timeout


# Global registry instance
_registry: Optional[CircuitBreakerRegistry] = None


def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    """Get the global circuit breaker registry."""
    global _registry
    if _registry is None:
        _registry = CircuitBreakerRegistry()
    return _registry


def reset_circuit_breaker_registry() -> None:
    """Reset the global registry (useful for testing)."""
    global _registry
    _registry = None


async def with_circuit_breaker(
    service_name: str,
    func: Callable[..., Any],
    *args,
    fallback: Optional[Callable[[], Any]] = None,
    **kwargs
) -> Any:
    """
    Execute a function with circuit breaker protection.

    If the circuit is open, returns fallback result immediately.
    Otherwise executes the function and records success/failure.

    Args:
        service_name: Name of the service (for circuit breaker lookup)
        func: Async function to execute
        *args: Positional arguments for func
        fallback: Optional fallback function to call if circuit is open
        **kwargs: Keyword arguments for func

    Returns:
        Result from func, or fallback result if circuit is open

    Raises:
        Exception: If circuit is open and no fallback provided
        Exception: Re-raises exception from func (after recording failure)

    Usage:
        # With fallback
        result = await with_circuit_breaker(
            "weather",
            fetch_weather,
            location="Baltimore",
            fallback=lambda: {"error": "Service unavailable"}
        )

        # Without fallback (raises on open circuit)
        result = await with_circuit_breaker(
            "sports",
            fetch_sports_scores,
            team="Ravens"
        )
    """
    registry = get_circuit_breaker_registry()
    breaker = registry.get_breaker(service_name)

    if not await breaker.can_execute():
        logger.warning(
            "circuit_breaker_rejected",
            service=service_name
        )
        if fallback:
            return await fallback() if asyncio.iscoroutinefunction(fallback) else fallback()
        raise Exception(f"Service {service_name} circuit breaker is open")

    try:
        result = await func(*args, **kwargs)
        await breaker.record_success()
        return result
    except Exception as e:
        await breaker.record_failure()
        logger.error(
            "circuit_breaker_failure",
            service=service_name,
            error=str(e)
        )
        raise

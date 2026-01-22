"""
Unified RAG Client for Project Athena Orchestrator

Provides a single interface for all RAG service calls with integrated:
- HTTP connection pooling (via http_pool)
- Circuit breaker protection (via circuit_breaker)
- Rate limiting (via rate_limiter)
- Request tracing headers
- Dynamic service discovery from admin backend registry

This replaces inline httpx.AsyncClient creation throughout the orchestrator.

Usage:
    from orchestrator.rag_client import get_rag_client

    client = get_rag_client()

    # Simple GET request with all resilience patterns
    data = await client.get("weather", "/weather/current", params={"location": "Baltimore"})

    # POST request
    data = await client.post("dining", "/restaurants/search", json={"cuisine": "italian"})

    # With session-level rate limiting
    data = await client.get("stocks", "/quote", params={"symbol": "AAPL"}, session_id="user123")

    # Get service status for health checks
    status = client.get_health_status()
"""
import asyncio
import os
from typing import Any, Dict, Optional
from dataclasses import dataclass
import httpx
import structlog

from orchestrator.circuit_breaker import (
    get_circuit_breaker_registry,
    CircuitState,
)
from orchestrator.rate_limiter import (
    get_rate_limiter_registry,
    RateLimitExceeded,
)
from orchestrator.http_pool import get_http_pool
from orchestrator.utils.constants import RAG_SERVICE_URL_MAP

logger = structlog.get_logger()

# Admin backend URL for service registry
def _get_admin_url() -> str:
    """Determine the correct Admin API URL based on environment."""
    explicit_url = os.getenv("ADMIN_BACKEND_URL") or os.getenv("ADMIN_API_URL")
    if explicit_url:
        return explicit_url
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return "http://athena-admin-backend.athena-admin.svc.cluster.local:8080"
    return "http://localhost:8080"

ADMIN_BACKEND_URL = _get_admin_url()


async def fetch_service_urls_from_registry() -> Dict[str, str]:
    """
    Fetch RAG service URLs from the admin backend service registry.

    Returns:
        Dict mapping service name to endpoint URL

    Falls back to hardcoded constants if registry is unavailable.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{ADMIN_BACKEND_URL}/api/internal/config/rag-services"
            )
            if response.status_code == 200:
                service_urls = response.json()
                logger.info(
                    "service_urls_loaded_from_registry",
                    services=list(service_urls.keys()),
                    count=len(service_urls)
                )
                return service_urls
            else:
                logger.warning(
                    "service_registry_request_failed",
                    status=response.status_code,
                    falling_back_to_constants=True
                )
    except Exception as e:
        logger.warning(
            "service_registry_unavailable",
            error=str(e),
            falling_back_to_constants=True
        )

    # Fall back to hardcoded constants
    logger.info("using_fallback_service_urls", services=list(RAG_SERVICE_URL_MAP.keys()))
    return RAG_SERVICE_URL_MAP.copy()


@dataclass
class RAGResponse:
    """Response from a RAG service call."""
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    status_code: Optional[int] = None
    service_name: Optional[str] = None


class RAGClientError(Exception):
    """Base exception for RAG client errors."""
    def __init__(self, service: str, message: str):
        self.service = service
        self.message = message
        super().__init__(f"[{service}] {message}")


class ServiceUnavailableError(RAGClientError):
    """Raised when service circuit breaker is open."""
    pass


class ServiceTimeoutError(RAGClientError):
    """Raised when service request times out."""
    pass


class RAGClient:
    """
    Unified client for RAG service communication with resilience patterns.

    Combines HTTP pooling, circuit breakers, and rate limiting into a single
    interface for all RAG service calls.

    Supports dynamic service discovery from admin backend registry.
    """
    _instance: Optional["RAGClient"] = None

    def __init__(
        self,
        service_urls: Optional[Dict[str, str]] = None,
        default_timeout: float = 30.0
    ):
        """
        Initialize RAG client.

        Args:
            service_urls: Override URLs for services (default from constants)
            default_timeout: Default request timeout in seconds
        """
        self._service_urls = service_urls or RAG_SERVICE_URL_MAP.copy()
        self._default_timeout = default_timeout
        self._urls_loaded_from_registry = False

        # Get registries (lazy initialization)
        self._circuit_registry = None
        self._rate_registry = None
        self._http_pool = None

        logger.info(
            "rag_client_created",
            services=list(self._service_urls.keys()),
            from_registry=False
        )

    async def load_service_urls_from_registry(self) -> bool:
        """
        Load service URLs from the admin backend registry.

        Returns:
            True if successfully loaded from registry, False if using fallback
        """
        try:
            service_urls = await fetch_service_urls_from_registry()
            if service_urls:
                self._service_urls = service_urls
                self._urls_loaded_from_registry = True
                logger.info(
                    "rag_client_urls_updated_from_registry",
                    services=list(self._service_urls.keys()),
                    count=len(self._service_urls)
                )
                return True
        except Exception as e:
            logger.error("load_service_urls_failed", error=str(e))
        return False

    @property
    def urls_loaded_from_registry(self) -> bool:
        """Check if URLs were loaded from registry vs fallback."""
        return self._urls_loaded_from_registry

    @property
    def circuit_registry(self):
        """Lazy-load circuit breaker registry."""
        if self._circuit_registry is None:
            self._circuit_registry = get_circuit_breaker_registry()
        return self._circuit_registry

    @property
    def rate_registry(self):
        """Lazy-load rate limiter registry."""
        if self._rate_registry is None:
            self._rate_registry = get_rate_limiter_registry()
        return self._rate_registry

    @property
    def http_pool(self):
        """Lazy-load HTTP pool."""
        if self._http_pool is None:
            self._http_pool = get_http_pool()
        return self._http_pool

    def get_service_url(self, service_name: str) -> str:
        """
        Get base URL for a service.

        Args:
            service_name: Name of the RAG service

        Returns:
            Base URL for the service

        Raises:
            ValueError: If service is not configured
        """
        if service_name not in self._service_urls:
            raise ValueError(f"Unknown service: {service_name}")
        return self._service_urls[service_name]

    def update_service_url(self, service_name: str, url: str) -> None:
        """Update URL for a service at runtime."""
        self._service_urls[service_name] = url
        logger.info("service_url_updated", service=service_name, url=url)

    async def close(self) -> None:
        """Close the RAG client and release resources."""
        if self._http_pool is not None:
            await self._http_pool.close()
            self._http_pool = None
        logger.info("rag_client_closed")

    async def _check_circuit_breaker(self, service_name: str) -> bool:
        """
        Check if circuit breaker allows request.

        Returns:
            True if request can proceed, False if circuit is open
        """
        breaker = self.circuit_registry.get_breaker(service_name)
        return await breaker.can_execute()

    async def _record_success(self, service_name: str) -> None:
        """Record successful call to circuit breaker."""
        breaker = self.circuit_registry.get_breaker(service_name)
        await breaker.record_success()

    async def _record_failure(self, service_name: str) -> None:
        """Record failed call to circuit breaker."""
        breaker = self.circuit_registry.get_breaker(service_name)
        await breaker.record_failure()

    async def _check_rate_limit(
        self,
        service_name: str,
        session_id: Optional[str] = None
    ) -> bool:
        """
        Check if rate limit allows request.

        Args:
            service_name: Name of the service
            session_id: Optional session ID for per-session limiting

        Returns:
            True if request can proceed, False if rate limited
        """
        if session_id:
            limiter = self.rate_registry.get_session_limiter(service_name)
            return await limiter.acquire(session_id)
        else:
            limiter = self.rate_registry.get_limiter(service_name)
            return await limiter.acquire()

    async def request(
        self,
        service_name: str,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        session_id: Optional[str] = None,
        timeout: Optional[float] = None,
        skip_circuit_breaker: bool = False,
        skip_rate_limit: bool = False
    ) -> RAGResponse:
        """
        Make a request to a RAG service with full resilience support.

        Args:
            service_name: Name of the RAG service (weather, sports, etc.)
            method: HTTP method (GET, POST)
            path: URL path (e.g., "/weather/current")
            params: Query parameters
            json: JSON body for POST requests
            headers: Additional headers
            session_id: Optional session ID for per-session rate limiting
            timeout: Override default timeout
            skip_circuit_breaker: Bypass circuit breaker (for health checks)
            skip_rate_limit: Bypass rate limiting

        Returns:
            RAGResponse with success status and data or error
        """
        # Check circuit breaker first
        if not skip_circuit_breaker:
            if not await self._check_circuit_breaker(service_name):
                logger.warning(
                    "rag_request_blocked_circuit_open",
                    service=service_name,
                    path=path
                )
                return RAGResponse(
                    success=False,
                    error="Service temporarily unavailable (circuit open)",
                    service_name=service_name
                )

        # Check rate limit
        if not skip_rate_limit:
            if not await self._check_rate_limit(service_name, session_id):
                logger.warning(
                    "rag_request_blocked_rate_limit",
                    service=service_name,
                    path=path,
                    session_id=session_id[:8] if session_id else None
                )
                return RAGResponse(
                    success=False,
                    error="Rate limit exceeded",
                    service_name=service_name
                )

        # Build full URL
        try:
            base_url = self.get_service_url(service_name)
        except ValueError as e:
            return RAGResponse(
                success=False,
                error=str(e),
                service_name=service_name
            )

        full_url = f"{base_url}{path}"

        # Prepare headers
        request_headers = headers.copy() if headers else {}

        try:
            # Use pooled HTTP client
            client = await self.http_pool.get_client("rag")

            # Make request with timeout
            request_timeout = timeout or self._default_timeout

            response = await asyncio.wait_for(
                client.request(
                    method=method,
                    url=full_url,
                    params=params,
                    json=json,
                    headers=request_headers
                ),
                timeout=request_timeout
            )

            # Record success to circuit breaker
            await self._record_success(service_name)

            if response.status_code == 200:
                try:
                    data = response.json()
                    return RAGResponse(
                        success=True,
                        data=data,
                        status_code=response.status_code,
                        service_name=service_name
                    )
                except Exception as e:
                    logger.warning(
                        "rag_response_parse_error",
                        service=service_name,
                        error=str(e)
                    )
                    return RAGResponse(
                        success=False,
                        error=f"Failed to parse response: {e}",
                        status_code=response.status_code,
                        service_name=service_name
                    )
            else:
                # Non-200 response - still record success (service is responding)
                return RAGResponse(
                    success=False,
                    error=f"Service returned status {response.status_code}",
                    status_code=response.status_code,
                    service_name=service_name
                )

        except asyncio.TimeoutError:
            await self._record_failure(service_name)
            logger.error(
                "rag_request_timeout",
                service=service_name,
                path=path,
                timeout=timeout or self._default_timeout
            )
            return RAGResponse(
                success=False,
                error="Request timed out",
                service_name=service_name
            )

        except httpx.ConnectError as e:
            await self._record_failure(service_name)
            logger.error(
                "rag_request_connect_error",
                service=service_name,
                path=path,
                error=str(e)
            )
            return RAGResponse(
                success=False,
                error=f"Connection failed: {e}",
                service_name=service_name
            )

        except Exception as e:
            await self._record_failure(service_name)
            logger.error(
                "rag_request_error",
                service=service_name,
                path=path,
                error=str(e),
                error_type=type(e).__name__
            )
            return RAGResponse(
                success=False,
                error=str(e),
                service_name=service_name
            )

    async def get(
        self,
        service_name: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> RAGResponse:
        """Convenience method for GET requests."""
        return await self.request(service_name, "GET", path, params=params, **kwargs)

    async def post(
        self,
        service_name: str,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> RAGResponse:
        """Convenience method for POST requests."""
        return await self.request(service_name, "POST", path, json=json, **kwargs)

    def get_health_status(self) -> Dict[str, Any]:
        """
        Get health status of all services including circuit breakers and rate limiters.

        Returns:
            Dict with circuit_breakers, rate_limiters, and open_circuits lists
        """
        return {
            "circuit_breakers": self.circuit_registry.get_all_status(),
            "rate_limiters": self.rate_registry.get_all_status(),
            "open_circuits": self.circuit_registry.get_open_circuits(),
            "rejection_stats": self.rate_registry.get_rejection_stats()
        }

    def is_service_available(self, service_name: str) -> bool:
        """
        Check if a service is currently available (circuit not open).

        Args:
            service_name: Name of the service

        Returns:
            True if service is available
        """
        breaker = self.circuit_registry.get_breaker(service_name)
        return breaker.state != CircuitState.OPEN

    def get_available_services(self) -> list:
        """Get list of services with circuits not in OPEN state."""
        return [
            name for name in self._service_urls.keys()
            if self.is_service_available(name)
        ]

    def reset_service(self, service_name: str) -> None:
        """Reset circuit breaker and rate limiter for a service."""
        breaker = self.circuit_registry.get_breaker(service_name)
        breaker.reset()

        limiter = self.rate_registry.get_limiter(service_name)
        limiter.reset()

        logger.info("service_reset", service=service_name)

    def reset_all(self) -> None:
        """Reset all circuit breakers and rate limiters."""
        self.circuit_registry.reset_all()
        self.rate_registry.reset_all()
        logger.info("all_services_reset")


# Global instance
_client: Optional[RAGClient] = None
_client_initialized: bool = False


def get_rag_client() -> RAGClient:
    """Get the global RAG client instance."""
    global _client
    if _client is None:
        _client = RAGClient()
    return _client


async def initialize_rag_client() -> RAGClient:
    """
    Initialize the global RAG client with service URLs from registry.

    Call this once at orchestrator startup to load URLs from the registry.
    If registry is unavailable, falls back to hardcoded constants.

    Returns:
        Initialized RAGClient instance
    """
    global _client, _client_initialized

    if _client is None:
        _client = RAGClient()

    if not _client_initialized:
        await _client.load_service_urls_from_registry()
        _client_initialized = True
        logger.info(
            "rag_client_initialized",
            from_registry=_client.urls_loaded_from_registry,
            services=list(_client._service_urls.keys())
        )

    return _client


def reset_rag_client() -> None:
    """Reset the global RAG client (useful for testing)."""
    global _client, _client_initialized
    _client = None
    _client_initialized = False


async def fetch_rag_data(
    service_name: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Convenience function to fetch data from a RAG service.

    Returns data dict on success, None on failure.

    Usage:
        data = await fetch_rag_data("weather", "/weather/current", {"location": "Baltimore"})
        if data:
            # Process weather data
    """
    client = get_rag_client()
    response = await client.get(service_name, path, params=params, session_id=session_id)
    return response.data if response.success else None

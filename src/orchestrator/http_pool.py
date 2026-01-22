"""
Shared HTTP Client Pool for Project Athena Orchestrator

Provides a singleton HTTP client pool to avoid creating new connections
for each request. Supports configurable timeouts per service type.

Benefits:
- Connection reuse reduces TCP handshake overhead (~100-200ms per request)
- Configurable limits prevent connection exhaustion
- Per-service timeout configuration
- Automatic cleanup on shutdown

Usage:
    from orchestrator.http_pool import get_http_pool, close_http_pool

    # Get a client for RAG services
    pool = get_http_pool()
    client = await pool.get_client("rag")
    response = await client.get(f"{service_url}/weather/current")

    # With tracing headers
    from shared.tracing import get_tracing_headers
    headers = get_tracing_headers(request)
    response = await client.get(url, headers=headers)

    # Cleanup on shutdown
    await close_http_pool()
"""
import httpx
from typing import Optional, Dict, Any
import structlog

logger = structlog.get_logger()


class ServiceConfig:
    """Configuration for a service type's HTTP client."""

    def __init__(
        self,
        timeout: float = 30.0,
        max_connections: int = 50,
        max_keepalive_connections: int = 20,
        keepalive_expiry: float = 30.0
    ):
        self.timeout = timeout
        self.limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry
        )


# Default configurations per service type
DEFAULT_CONFIGS: Dict[str, ServiceConfig] = {
    # RAG services - moderate timeout, high concurrency
    "rag": ServiceConfig(
        timeout=30.0,
        max_connections=50,
        max_keepalive_connections=20
    ),

    # LLM services - long timeout, limited concurrency
    "llm": ServiceConfig(
        timeout=120.0,
        max_connections=10,
        max_keepalive_connections=5
    ),

    # Admin API - short timeout, moderate concurrency
    "admin": ServiceConfig(
        timeout=10.0,
        max_connections=20,
        max_keepalive_connections=10
    ),

    # Home Assistant - moderate timeout, limited concurrency
    "ha": ServiceConfig(
        timeout=30.0,
        max_connections=10,
        max_keepalive_connections=5
    ),

    # Metrics/logging - very short timeout, fire-and-forget
    "metrics": ServiceConfig(
        timeout=5.0,
        max_connections=5,
        max_keepalive_connections=2
    ),

    # Default for unknown service types
    "default": ServiceConfig(
        timeout=30.0,
        max_connections=20,
        max_keepalive_connections=10
    )
}


class HTTPClientPool:
    """
    Singleton HTTP client pool with per-service configuration.

    Manages a collection of httpx.AsyncClient instances, one per service type,
    with appropriate timeout and connection limit configurations.
    """
    _instance: Optional["HTTPClientPool"] = None

    def __init__(self, configs: Optional[Dict[str, ServiceConfig]] = None):
        """
        Initialize the HTTP client pool.

        Args:
            configs: Optional custom configurations per service type.
                     Merged with DEFAULT_CONFIGS, custom values take precedence.
        """
        self._configs = {**DEFAULT_CONFIGS}
        if configs:
            self._configs.update(configs)

        self._clients: Dict[str, httpx.AsyncClient] = {}
        self._initialized = False

        logger.info("http_pool_created", service_types=list(self._configs.keys()))

    async def get_client(self, service_type: str = "default") -> httpx.AsyncClient:
        """
        Get or create an HTTP client for the given service type.

        Args:
            service_type: Type of service (rag, llm, admin, ha, metrics, default)

        Returns:
            httpx.AsyncClient configured for the service type
        """
        if service_type not in self._clients:
            config = self._configs.get(service_type, self._configs["default"])

            self._clients[service_type] = httpx.AsyncClient(
                timeout=httpx.Timeout(config.timeout),
                limits=config.limits,
                follow_redirects=True
            )

            logger.info(
                "http_client_created",
                service_type=service_type,
                timeout=config.timeout,
                max_connections=config.limits.max_connections
            )

        return self._clients[service_type]

    async def request(
        self,
        service_type: str,
        method: str,
        url: str,
        **kwargs
    ) -> httpx.Response:
        """
        Make an HTTP request using the appropriate client.

        Args:
            service_type: Type of service (rag, llm, admin, etc.)
            method: HTTP method (GET, POST, etc.)
            url: Full URL to request
            **kwargs: Additional arguments to pass to httpx request

        Returns:
            httpx.Response
        """
        client = await self.get_client(service_type)
        return await client.request(method, url, **kwargs)

    async def get(
        self,
        service_type: str,
        url: str,
        **kwargs
    ) -> httpx.Response:
        """Convenience method for GET requests."""
        return await self.request(service_type, "GET", url, **kwargs)

    async def post(
        self,
        service_type: str,
        url: str,
        **kwargs
    ) -> httpx.Response:
        """Convenience method for POST requests."""
        return await self.request(service_type, "POST", url, **kwargs)

    def update_config(self, service_type: str, config: ServiceConfig) -> None:
        """
        Update configuration for a service type.

        Note: This won't affect already-created clients.
        Call close() and recreate if you need to apply new config.
        """
        self._configs[service_type] = config
        logger.info(
            "http_pool_config_updated",
            service_type=service_type,
            timeout=config.timeout
        )

    async def close(self) -> None:
        """Close all HTTP clients and release connections."""
        for service_type, client in self._clients.items():
            try:
                await client.aclose()
                logger.info("http_client_closed", service_type=service_type)
            except Exception as e:
                logger.warning(
                    "http_client_close_failed",
                    service_type=service_type,
                    error=str(e)
                )

        self._clients.clear()
        logger.info("http_pool_closed")

    def get_stats(self) -> Dict[str, Any]:
        """
        Get statistics about the connection pool.

        Returns:
            Dict with stats per service type
        """
        stats = {}
        for service_type, client in self._clients.items():
            # httpx doesn't expose detailed stats, but we can show config
            config = self._configs.get(service_type, self._configs["default"])
            stats[service_type] = {
                "timeout": config.timeout,
                "max_connections": config.limits.max_connections,
                "max_keepalive": config.limits.max_keepalive_connections
            }
        return stats


# Global pool instance
_pool: Optional[HTTPClientPool] = None


def get_http_pool() -> HTTPClientPool:
    """
    Get the global HTTP client pool.

    Creates the pool on first call if it doesn't exist.
    """
    global _pool
    if _pool is None:
        _pool = HTTPClientPool()
    return _pool


async def close_http_pool() -> None:
    """
    Close the global HTTP client pool.

    Call this during application shutdown to release connections.
    """
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def reset_http_pool() -> None:
    """
    Reset the global HTTP client pool.

    Useful for testing. Does NOT close existing connections.
    """
    global _pool
    _pool = None


async def make_request(
    service_type: str,
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    **kwargs
) -> httpx.Response:
    """
    Convenience function to make HTTP request using the global pool.

    Usage:
        response = await make_request("rag", "GET", "http://service:8010/health")
    """
    pool = get_http_pool()
    if headers:
        kwargs["headers"] = headers
    return await pool.request(service_type, method, url, **kwargs)

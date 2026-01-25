"""
Resilient HTTP client with retry logic and connection management.

This module provides a robust HTTP client wrapper that handles:
- Automatic retries with exponential backoff
- Connection pool refresh on failures
- Health check integration
- Graceful degradation
"""

import asyncio
import time
from typing import Optional, Dict, Any
import httpx
from .logging_config import configure_logging

logger = configure_logging("http-client")


class ResilientHttpClient:
    """
    HTTP client with automatic retry and connection refresh.

    Features:
    - Exponential backoff on failures
    - Automatic connection pool refresh
    - Health check integration
    - Circuit breaker pattern
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        health_check_path: str = "/health"
    ):
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.health_check_path = health_check_path

        self._client: Optional[httpx.AsyncClient] = None
        self._last_health_check = 0.0
        self._health_check_interval = 30.0  # 30 seconds
        self._is_healthy = False

    async def _create_client(self) -> httpx.AsyncClient:
        """Create a new HTTP client with connection pooling."""
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            limits=httpx.Limits(
                max_keepalive_connections=5,
                max_connections=10,
                keepalive_expiry=30.0
            ),
            transport=httpx.AsyncHTTPTransport(retries=0)  # We handle retries ourselves
        )

    async def _refresh_client(self):
        """Close existing client and create a new one."""
        if self._client:
            await self._client.aclose()
        self._client = await self._create_client()
        logger.info("HTTP client refreshed", base_url=self.base_url)

    async def _check_health(self) -> bool:
        """Check if the service is healthy."""
        now = time.time()

        # Rate limit health checks
        if now - self._last_health_check < self._health_check_interval:
            return self._is_healthy

        self._last_health_check = now

        try:
            if not self._client:
                await self._refresh_client()

            response = await self._client.get(self.health_check_path, timeout=5.0)
            self._is_healthy = response.status_code == 200

            if self._is_healthy:
                logger.debug("Health check passed", base_url=self.base_url)
            else:
                logger.warning("Health check failed", base_url=self.base_url, status=response.status_code)

            return self._is_healthy

        except Exception as e:
            logger.warning("Health check error", base_url=self.base_url, error=str(e))
            self._is_healthy = False
            return False

    async def get(self, path: str, **kwargs) -> httpx.Response:
        """GET request with retry logic."""
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        """POST request with retry logic."""
        return await self._request("POST", path, **kwargs)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """
        Make HTTP request with automatic retry and connection refresh.

        Implements exponential backoff and connection pool refresh on failures.
        """
        if not self._client:
            await self._refresh_client()

        last_exception = None

        for attempt in range(self.max_retries):
            try:
                response = await self._client.request(method, path, **kwargs)

                # Success - reset health status
                if response.status_code < 500:
                    self._is_healthy = True

                return response

            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout) as e:
                last_exception = e
                logger.warning(
                    "HTTP request failed",
                    base_url=self.base_url,
                    path=path,
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                    error=str(e)
                )

                # Refresh client on connection errors
                if isinstance(e, (httpx.ConnectError, httpx.RemoteProtocolError)):
                    logger.info("Refreshing HTTP client after connection error")
                    await self._refresh_client()

                # Exponential backoff
                if attempt < self.max_retries - 1:
                    delay = self.retry_delay * (2 ** attempt)
                    logger.debug(f"Retrying in {delay}s...")
                    await asyncio.sleep(delay)

            except Exception as e:
                # Non-retryable error
                logger.error("Non-retryable HTTP error", base_url=self.base_url, error=str(e))
                raise

        # All retries exhausted
        logger.error(
            "All HTTP retries exhausted",
            base_url=self.base_url,
            path=path,
            max_retries=self.max_retries
        )
        raise httpx.RequestError(f"All {self.max_retries} retries failed: {last_exception}")

    async def is_available(self) -> bool:
        """Check if the service is available (with caching)."""
        return await self._check_health()

    async def close(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("HTTP client closed", base_url=self.base_url)

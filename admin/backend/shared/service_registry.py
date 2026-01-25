"""
Service Registry Client

Allows services to register themselves and orchestrator to discover service URLs.
Uses the Admin API instead of direct database access.

Architecture:
    Service -> Admin API (HTTP) -> PostgreSQL (athena database)
"""
import os
import time
import httpx
from typing import Optional, Dict
import structlog

logger = structlog.get_logger()

# Admin API configuration
# Priority: ADMIN_API_URL env var > ADMIN_BACKEND_URL env var > auto-detect
_default_admin_url = "http://athena-admin-backend.athena-admin.svc.cluster.local:8080"

# Auto-detect environment based on hostname
import socket
_hostname = socket.gethostname().lower()
if 'mac-studio' in _hostname or 'jays-mac' in _hostname or os.getenv("LOCAL_DEV", "false").lower() == "true":
    # Local development uses localhost
    _default_admin_url = "http://localhost:8080"

ADMIN_API_URL = os.getenv(
    "ADMIN_API_URL",
    os.getenv("ADMIN_BACKEND_URL", _default_admin_url)
)

# Cache for service URLs (30 second TTL to avoid excessive API calls)
_url_cache: Dict[str, str] = {}
_cache_time: Dict[str, float] = {}
_CACHE_TTL = 30.0


async def get_service_url(service_name: str) -> Optional[str]:
    """
    Get service URL from registry via Admin API.

    Args:
        service_name: Name of the service (e.g., "sports", "weather")

    Returns:
        Service URL or None if not found
    """
    # Check cache first
    if service_name in _url_cache:
        if time.time() - _cache_time.get(service_name, 0) < _CACHE_TTL:
            return _url_cache[service_name]

    # Query Admin API
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{ADMIN_API_URL}/api/service-registry/services/{service_name}/url"
            )

            if response.status_code == 200:
                data = response.json()
                url = data.get('url')
                # Update cache
                _url_cache[service_name] = url
                _cache_time[service_name] = time.time()
                logger.debug(f"Service registry lookup: {service_name} → {url}")
                return url
            elif response.status_code == 404:
                logger.warning(f"Service not found in registry: {service_name}")
                return None
            elif response.status_code == 503:
                logger.warning(f"Service disabled in registry: {service_name}")
                return None
            else:
                logger.error(f"Service registry error: {response.status_code}")
                return None

    except httpx.ConnectError:
        logger.error("Cannot connect to admin API", url=ADMIN_API_URL)
        return None
    except Exception as e:
        logger.error(f"Service registry lookup failed: {e}")
        return None


async def register_service(
    service_name: str,
    port: int,
    description: str = "",
    metadata: Optional[Dict] = None
) -> bool:
    """
    Register a service in the registry via Admin API.

    Args:
        service_name: Name of the service
        port: Port the service is running on
        description: Service description
        metadata: Optional metadata dict

    Returns:
        True if registration succeeded
    """
    try:
        # Use localhost for service URLs (environment-agnostic)
        service_url = f"http://localhost:{port}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{ADMIN_API_URL}/api/service-registry/services",
                params={
                    "name": service_name,
                    "endpoint_url": service_url,
                    "display_name": description or service_name.replace('-', ' ').title(),
                    "service_type": "api"
                }
            )

            if response.status_code in (200, 201):
                logger.info(f"Service registered: {service_name} → {service_url}")
                return True
            else:
                logger.error(f"Service registration failed: {response.status_code}")
                return False

    except Exception as e:
        logger.error(f"Service registration failed: {e}")
        return False


async def unregister_service(service_name: str) -> bool:
    """
    Unregister a service from the registry via Admin API.

    Args:
        service_name: Name of the service

    Returns:
        True if unregistration succeeded
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Use toggle to disable rather than delete
            response = await client.post(
                f"{ADMIN_API_URL}/api/service-registry/services/{service_name}/toggle"
            )

            if response.status_code == 200:
                logger.info(f"Service unregistered: {service_name}")

                # Clear cache
                if service_name in _url_cache:
                    del _url_cache[service_name]
                if service_name in _cache_time:
                    del _cache_time[service_name]

                return True
            else:
                logger.error(f"Service unregistration failed: {response.status_code}")
                return False

    except Exception as e:
        logger.error(f"Service unregistration failed: {e}")
        return False


def clear_cache():
    """Clear the URL cache."""
    _url_cache.clear()
    _cache_time.clear()
    logger.info("Service registry cache cleared")


def kill_process_on_port(port: int) -> bool:
    """
    Kill any process using the specified port.

    This is useful for cleaning up stale service instances before starting.

    Args:
        port: Port number to check and clear

    Returns:
        True if a process was killed, False if port was already free
    """
    import subprocess
    import signal

    try:
        # Find process using the port (works on macOS and Linux)
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            for pid_str in pids:
                try:
                    pid = int(pid_str.strip())
                    os.kill(pid, signal.SIGTERM)
                    logger.info(f"Killed stale process {pid} on port {port}")
                except (ValueError, ProcessLookupError):
                    continue
            # Give processes time to terminate
            import time
            time.sleep(1)
            return True
        return False
    except FileNotFoundError:
        # lsof not available, try ss (Linux)
        try:
            result = subprocess.run(
                ["ss", "-tlnp", f"sport = :{port}"],
                capture_output=True,
                text=True,
                timeout=5
            )
            # Parse ss output for PIDs
            if "LISTEN" in result.stdout:
                logger.warning(f"Port {port} is in use, but could not kill process (ss available)")
            return False
        except FileNotFoundError:
            logger.warning(f"Neither lsof nor ss available to check port {port}")
            return False
    except Exception as e:
        logger.warning(f"Failed to check/kill process on port {port}: {e}")
        return False


async def startup_service(
    service_name: str,
    port: int,
    description: str = "",
    kill_stale: bool = True
) -> bool:
    """
    Full service startup sequence:
    1. Kill any stale process on the port
    2. Register service in the registry

    Args:
        service_name: Name of the service (e.g., "weather", "sports")
        port: Port the service will run on
        description: Human-readable description
        kill_stale: Whether to kill stale processes (default True)

    Returns:
        True if startup succeeded
    """
    # 1. Kill stale process if requested
    if kill_stale:
        killed = kill_process_on_port(port)
        if killed:
            logger.info(f"Cleared stale process on port {port} for {service_name}")

    # 2. Register service
    registered = await register_service(service_name, port, description)
    if registered:
        logger.info(f"Service {service_name} started and registered on port {port}")
    else:
        logger.warning(f"Service {service_name} started but registration failed (will retry)")

    return registered

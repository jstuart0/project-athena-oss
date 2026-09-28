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
from shared.admin_url import get_admin_url

logger = structlog.get_logger()

# Admin API URL (resolved by shared.admin_url.get_admin_url)
ADMIN_API_URL = get_admin_url()

# Cache for service URLs (30 second TTL to avoid excessive API calls)
_url_cache: Dict[str, str] = {}
_cache_time: Dict[str, float] = {}
_CACHE_TTL = 30.0


def to_rag_registry_name(service_name: str) -> str:
    """Registry ``name`` for a RAG connector's self-registration.

    Every RAG registry row (seeded via OSS_SERVICE_REGISTRY or created by an
    operator) is matched by admin/backend/app/database.py::_infer_oss_service_type
    and the Control Agent's registry-sync using the same "-rag" suffix
    convention on the identifying string. Bare connector names like
    "weather" (what every ``src/rag/*/main.py`` passes today) become
    "weather-rag"; a name that already carries the suffix is left alone so
    this is idempotent.
    """
    return service_name if service_name.endswith("-rag") else f"{service_name}-rag"


def to_rag_host_label(service_name: str) -> str:
    """K8s DNS service host hint for a RAG connector's self-registration.

    Mirrors OSS_SERVICE_REGISTRY's host column convention
    (``athena-rag-<name>``), e.g. "weather" -> "athena-rag-weather". Sent
    alongside ``name`` so the admin upsert can still locate an existing
    seeded row by host when the derived registry name doesn't match it
    (ATHENA-108 follow-up).
    """
    base = service_name[:-len("-rag")] if service_name.endswith("-rag") else service_name
    return f"athena-rag-{base}"


def _get_service_api_key() -> str:
    """SERVICE_API_KEY for the X-Service-Key header POST /api/service-registry/services
    requires (ATHENA-108). Prefer get_config() -- the centralized,
    pydantic-settings-backed source of truth -- and fall back to the raw
    env var if shared.config isn't importable in this process (e.g. a
    standalone script running outside the full app environment)."""
    try:
        from shared.config import get_config
        return get_config().service_api_key or ""
    except Exception:
        return os.getenv("SERVICE_API_KEY", "")


def _get_service_registry_endpoint_url() -> str:
    """SERVICE_REGISTRY_ENDPOINT_URL override for register_service()'s
    payload (xander diff-review Critical, 2026-09-28). Empty by default --
    see the field's own docstring in shared.config.AthenaConfig for why
    that's the safe default."""
    try:
        from shared.config import get_config
        return get_config().service_registry_endpoint_url or ""
    except Exception:
        return os.getenv("SERVICE_REGISTRY_ENDPOINT_URL", "")


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
        port: Port the service is running on. Kept for call-site compatibility
            (18 RAG connectors pass it) and used by startup_service()'s
            kill_process_on_port(); no longer interpolated into a payload
            URL here (xander diff-review Critical, 2026-09-28 -- see the
            comment below on why endpoint_url is omitted by default).
        description: Service description
        metadata: Optional metadata dict

    Returns:
        True if registration succeeded
    """
    try:
        service_key = _get_service_api_key()
        if not service_key:
            logger.warning(
                f"SERVICE_API_KEY is unset; registration of {service_name} will be "
                "rejected with 401 by the admin API's X-Service-Key write gate."
            )

        # xander diff-review Critical (2026-09-28): this client owns only
        # its own identity, never its network location. The POST
        # /api/service-registry/services upsert unconditionally overwrote
        # host/port/protocol/endpoint_url on an EXISTING row before
        # ATHENA-108 fixed the 401 -- once auth actually worked, every RAG
        # startup was silently stomping its own seeded K8s host
        # ("athena-rag-weather") with this process's own view of itself
        # ("http://localhost:<port>"), breaking k8s manager resolution.
        #
        # Every caller of register_service()/startup_service() in this repo
        # is a RAG connector under src/rag/* (confirmed by repo-wide grep),
        # so service_type is always "rag" here -- the "-rag"-in-name rule
        # _infer_oss_service_type()/the Control Agent's sync apply to the
        # K8s DNS host string doesn't discriminate anything against the
        # short service_name this function receives ("weather", not
        # "athena-rag-weather"), so reusing it literally would misclassify
        # every real caller as "core".
        #
        # endpoint_url is included ONLY when SERVICE_REGISTRY_ENDPOINT_URL
        # is explicitly set -- omitted by default so the upsert's partial-
        # update semantics (ATHENA-109) leave an existing row's host/port/
        # protocol untouched. A brand-new, never-seeded service name still
        # needs SOME endpoint_url to be created at all; set the env var for
        # that deployment shape.
        #
        # ATHENA-108 follow-up: registry rows are named "<name>-rag" (the
        # same "-rag" suffix convention _infer_oss_service_type()/the
        # Control Agent apply), not the bare connector name this function
        # receives -- posting "weather" against a "weather-rag" row found
        # no match and 422'd, so registration silently never landed.
        # host_label is sent alongside so the upsert can still find a
        # seeded row (e.g. OSS_SERVICE_REGISTRY's "weather"/"athena-rag-
        # weather") by its K8s host when the derived name doesn't match.
        registry_name = to_rag_registry_name(service_name)
        params = {
            "name": registry_name,
            "host_label": to_rag_host_label(service_name),
            "display_name": description or service_name.replace('-', ' ').title(),
            "service_type": "rag",
        }
        explicit_endpoint_url = _get_service_registry_endpoint_url()
        if explicit_endpoint_url:
            params["endpoint_url"] = explicit_endpoint_url

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{ADMIN_API_URL}/api/service-registry/services",
                params=params,
                headers={"X-Service-Key": service_key},
            )

            if response.status_code in (200, 201):
                logger.info(f"Service registered: {service_name}")
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
        service_key = _get_service_api_key()
        if not service_key:
            logger.warning(
                f"SERVICE_API_KEY is unset; unregistration of {service_name} will be "
                "rejected with 401 by the admin API's X-Service-Key write gate, leaving "
                "the row enabled=True after this shutdown."
            )

        async with httpx.AsyncClient(timeout=10.0) as client:
            # Use toggle to disable rather than delete
            response = await client.post(
                f"{ADMIN_API_URL}/api/service-registry/services/{service_name}/toggle",
                headers={"X-Service-Key": service_key},
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

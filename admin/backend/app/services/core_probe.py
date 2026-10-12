"""The ``/api/status`` fallback probe for the gateway and the orchestrator.

Only consulted when the service registry has no row for the service. It answers
with the registry's own vocabulary: ``healthy``, ``unreachable``, ``timeout``,
``error`` or ``error: HTTP nnn``. A 401 is an error like any other status: the
probe sends no credentials, so it can't tell a healthy service from one that
refuses it, and it no longer reports a separate "healthy but needs auth" state.
"""
from typing import Optional, Tuple

import httpx

ProbeResult = Tuple[bool, str, Optional[str]]


async def probe_health(client: httpx.AsyncClient, name: str, base_url: str) -> ProbeResult:
    """``(healthy, status word, error text)`` for ``GET {base_url}/health``."""
    try:
        response = await client.get(f"{base_url}/health")
    except httpx.ConnectError:
        return False, "unreachable", f"{name} not accessible"
    except httpx.TimeoutException:
        return False, "timeout", "Service did not respond within timeout"
    except Exception as exc:
        return False, "error", str(exc)
    if response.status_code == 200:
        return True, "healthy", None
    return False, f"error: HTTP {response.status_code}", f"Unexpected status code: {response.status_code}"

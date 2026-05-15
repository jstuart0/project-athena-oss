"""Shared URL / host validators for SSRF protection at write boundaries.

These helpers were originally inline in app/routes/services.py (Phase 1
reconcile, xander HIGH-2).  They are extracted here so that
app/routes/service_registry.py can apply the same rules to its
endpoint_url query parameter without duplicating the logic.

Rules enforced:
- protocol: must be http or https (blocks file://, gopher://, etc.)
- host: blocks cloud IMDS (169.254.169.254), loopback, unspecified,
  multicast, and Kubernetes cluster-local DNS suffixes.  RFC1918 allowed
  with a logger.warning (homelab deployments need private-IP services).
- health_endpoint: blocks CRLF injection, null bytes, path traversal;
  normalises None/empty to '/health'.
- endpoint_url (full URL): combines protocol + host checks on a parsed URL;
  enforces a 2048-character length cap (xander L-2).
"""
import ipaddress
from typing import Optional
from urllib.parse import urlparse

import structlog

logger = structlog.get_logger()


def validate_protocol(v: str) -> str:
    """Protocol must be http or https. Other schemes are rejected."""
    if v not in ('http', 'https'):
        raise ValueError('protocol must be http or https')
    return v


def validate_host(v: str) -> str:
    """
    Reject empty hosts and known SSRF-exploit targets at the write boundary.

    Blocks:
    - Cloud IMDS addresses (169.254.169.254, IPv6 equivalents)
    - Loopback / unspecified / multicast IP ranges
    - Kubernetes control-plane cluster-local DNS suffixes

    RFC1918 private addresses (10.x, 172.16-31.x, 192.168.x) are ALLOWED
    with a warning because homelab services legitimately run on RFC1918.
    A runtime allowlist for the background health poller lands in Phase 4
    (xander HIGH-1 from r1 codex); this validator is the write-boundary layer.
    """
    if not v:
        raise ValueError('host is required')
    v = v.strip()
    if not v:
        raise ValueError('host cannot be blank')

    # Reject Kubernetes control-plane DNS suffixes regardless of whether the
    # value is also a valid IP (belt-and-suspenders).
    if v.endswith('.cluster.local') or v.endswith('.svc'):
        raise ValueError(
            f'host {v!r} resolves to a k8s cluster-internal address (forbidden)'
        )

    try:
        ip = ipaddress.ip_address(v)
    except ValueError:
        # Not a parseable IP — it's a hostname; allow it after suffix check.
        return v

    # Cloud IMDS — always reject
    if v in ('169.254.169.254', '::ffff:169.254.169.254', 'fe80::1'):
        raise ValueError(f'host {v} is a forbidden cloud metadata address')
    if ip.is_link_local:
        raise ValueError(f'host {v} is a link-local address (forbidden SSRF range)')
    if ip.is_loopback:
        raise ValueError(f'host {v} is a loopback address')
    if ip.is_multicast:
        raise ValueError(f'host {v} is a multicast address')
    if ip.is_unspecified:
        raise ValueError(f'host {v} is an unspecified address')
    # RFC1918 allowed — homelab services run on private IPs
    if ip.is_private:
        logger.warning(
            "service_host_is_rfc1918",
            host=v,
            note="allowed for homelab deployments; Phase 4 adds runtime allowlist",
        )
    return v


def validate_health_endpoint(v: Optional[str]) -> Optional[str]:
    """
    Reject CRLF injection, null bytes, path traversal, and non-/ prefixes.
    Returns '/health' for None/empty inputs.
    """
    if not v:
        return '/health'
    if any(c in v for c in ('\r', '\n', '\0')):
        raise ValueError('health_endpoint contains invalid characters (CRLF/null)')
    if '..' in v:
        raise ValueError('health_endpoint contains path traversal (..)')
    if not v.startswith('/'):
        raise ValueError('health_endpoint must start with /')
    if len(v) > 256:
        raise ValueError('health_endpoint exceeds 256 characters')
    return v


def validate_endpoint_url(v: str) -> str:
    """
    Validate a full endpoint URL at the write boundary.

    Enforces:
    - 2048-character length cap (xander L-2)
    - scheme must be http or https
    - host extracted from the URL must pass validate_host() SSRF rules

    Used by service_registry.py's POST /services upsert route, which accepts
    endpoint_url as a query parameter rather than through a Pydantic model.
    """
    if not v:
        raise ValueError('endpoint_url is required')
    if len(v) > 2048:
        raise ValueError('endpoint_url exceeds 2048 characters')

    try:
        parsed = urlparse(v)
    except Exception:
        raise ValueError('endpoint_url is not a valid URL')

    scheme = parsed.scheme.lower()
    if scheme not in ('http', 'https'):
        raise ValueError(
            f'endpoint_url scheme {scheme!r} is not allowed; use http or https'
        )

    # Access .port to trigger ValueError for malformed authorities (e.g. bare
    # unbracketed IPv6 like "http://fe80::1:8080/" which urlparse cannot
    # unambiguously decompose — treat as malformed and reject).
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError(
            'endpoint_url has a malformed authority (unbracketed IPv6 or invalid port)'
        )

    host = parsed.hostname or ''
    # validate_host raises ValueError on SSRF targets; let the message propagate.
    validate_host(host)

    return v


def is_local_host(hostname: str | None) -> bool:
    """True iff hostname is loopback or RFC1918 or ULA, AND the process is not in a K8s pod.

    Returns False when KUBERNETES_SERVICE_HOST is set in the environment,
    regardless of hostname — K8s pods are never a "local dev" environment
    even if cluster service IPs fall in RFC1918 ranges (xander M4).

    Link-local (169.254/16, fe80::/10) is intentionally excluded — APIPA
    addresses are not legitimate database targets (bob M1).
    """
    import os

    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return False
    if not hostname:
        return False
    if hostname in ("localhost", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if ip.is_link_local:
        return False
    return ip.is_loopback or ip.is_private


def parse_endpoint_url(endpoint_url: str) -> dict:
    """Parse endpoint_url into host/port/protocol/health_endpoint components.

    Used by POST /api/service-registry/services to populate the NOT-NULL
    host/port columns from the legacy endpoint_url query param so the Phase 4
    health poller can poll newly-CA-upserted services.  (codex r2 H-2)

    Returns a dict with keys: host, port, protocol, health_endpoint.
    Raises ValueError with a descriptive message on malformed input.

    The caller is responsible for running validate_endpoint_url() on the same
    value first (SSRF + scheme checks).  This helper only decomposes the URL.
    """
    try:
        parsed = urlparse(endpoint_url)
    except Exception:
        raise ValueError(f"endpoint_url {endpoint_url!r} is not a valid URL")

    scheme = parsed.scheme.lower()
    if scheme not in ('http', 'https'):
        raise ValueError(
            f"endpoint_url scheme {scheme!r} is not allowed; use http or https"
        )

    host = parsed.hostname or ''
    if not host:
        raise ValueError(f"endpoint_url {endpoint_url!r} has no hostname")

    port = parsed.port
    if port is None:
        port = 443 if scheme == 'https' else 80

    # Use the path as the health_endpoint, defaulting to '/health' when absent.
    path = parsed.path or '/health'
    # Normalise empty or root-only paths that carry no information to '/health'.
    if path in ('', '/'):
        path = '/health'

    return {
        'host': host,
        'port': port,
        'protocol': scheme,
        'health_endpoint': path,
    }

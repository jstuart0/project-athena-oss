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
import re
from typing import Optional
from urllib.parse import urlparse

import structlog

from shared.url_safety import is_local_host as _is_local_host
from shared.url_safety import validate_service_host

logger = structlog.get_logger()


def validate_protocol(v: str) -> str:
    """Protocol must be http or https. Other schemes are rejected."""
    if v not in ('http', 'https'):
        raise ValueError('protocol must be http or https')
    return v


def validate_host(v: str) -> str:
    """
    Reject empty hosts and known SSRF-exploit targets at the write boundary.

    The rule itself lives in ``shared.url_safety.validate_service_host`` so the
    runtime readers of a service address apply the same one. This wrapper
    keeps the admin warning for RFC1918 addresses, which homelab services
    legitimately use; a runtime allowlist for the background health poller is
    the other layer (xander HIGH-1 from r1 codex).
    """
    host = validate_service_host(v)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.is_private:
        logger.warning(
            "service_host_is_rfc1918",
            host=host,
            note="allowed for homelab deployments; Phase 4 adds runtime allowlist",
        )
    return host


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
    """See ``shared.url_safety.is_local_host``; kept here for existing callers."""
    return _is_local_host(hostname)


def redact_url_userinfo(url: str) -> str:
    """`scheme://host:port` only -- strips userinfo, path, query, and
    fragment before a configured URL is ever written to a log line (codex
    r2 diff-review High, 2026-09-28: main.py's startup warning and
    rag_urls.py's local-dev-carveout warning both logged the raw
    operator-configured Ollama URL verbatim; a URL shaped
    `http://user:pass@host:port` would put the credential straight into
    structured log output).

    Never raises: an unparseable URL is returned as the fixed literal
    `"<unparseable-url>"` rather than risk logging the raw (possibly
    credential-bearing) input in an exception message. A malformed port
    (out of range, e.g. `:99999`, or non-numeric, e.g. `:abc`) makes
    `parsed.port` itself raise `ValueError` -- caught separately (codex r3
    diff-review Low, 2026-09-28) and handled by stripping userinfo from
    the raw `netloc` by hand instead of reconstructing from
    `hostname`/`port`, since `.hostname` and the raising `.port` share the
    same malformed authority and a hand split is the only piece of that
    authority guaranteed not to raise.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "<unparseable-url>"

    scheme = parsed.scheme or ""
    hostname = parsed.hostname or ""
    if not hostname:
        return "<unparseable-url>"

    try:
        port = parsed.port
    except ValueError:
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return f"{scheme}://{netloc}" if scheme else netloc

    # urlparse().hostname strips IPv6 brackets; re-bracket so the redacted
    # form round-trips as a valid authority (unbracketed "::1:8080" is
    # ambiguous / invalid).
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"

    netloc = hostname
    if port:
        netloc = f"{hostname}:{port}"

    return f"{scheme}://{netloc}" if scheme else netloc


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


_URL_USERINFO_RE = re.compile(r"(\w[\w+.-]*://)[^\s'\"<>]*@")


def redact_urls_in_text(text: str) -> str:
    """Strip userinfo from every URL inside free text (an exception message,
    a status detail): ``error "http://u:p@h:6333/x"`` becomes
    ``error "http://h:6333/x"``. Everything from the scheme to the last
    ``@`` before whitespace, a quote or an angle bracket is treated as
    userinfo, so a password containing ``/``, ``#`` or ``?`` is removed too."""
    return _URL_USERINFO_RE.sub(r"\1", str(text))

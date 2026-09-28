"""Shared per-service RAG URL resolution (ATHENA-113b).

`dashboard.py`'s Mission Control voice-health card and `voice_tests.py`'s RAG
probes each assumed every RAG service is reachable through a single
`RAG_HOST` / `RAG_SERVICE_HOST` + a hardcoded port. In Kubernetes each RAG is
its own Service, so that single-host assumption is an OSS-First violation
(no deployment-specific infrastructure assumptions, per CLAUDE.md) -- it
silently reports every RAG unreachable whenever the shared host env var
isn't set, which is the normal case for a per-Service deployment.

This module centralizes the resolution order any caller should use for a
given RAG service name:

    1. service-registry row (`athena_service_registry`, via the `RagService`
       ORM model) -- the operator's own configured host/port/endpoint.
    2. canonical `RAG_<NAME>_URL` env var (spelling matches
       `src/orchestrator/urls.py`, e.g. `RAG_WEATHER_URL`).
    3. legacy `RAG_HOST` / `RAG_SERVICE_HOST` + the caller-supplied port,
       for single-host deployments -- logs one WARNING per service name the
       first time it's used.
    4. unconfigured -- caller should render "not configured", not
       "unreachable"; there is nothing to probe.
"""
from __future__ import annotations

import os
from typing import Optional

import structlog
from sqlalchemy.orm import Session

logger = structlog.get_logger()

# service_name -> canonical env var. Spelling matches src/orchestrator/urls.py.
CANONICAL_RAG_ENV_NAMES = {
    "weather": "RAG_WEATHER_URL",
    "sports": "RAG_SPORTS_URL",
    "dining": "RAG_DINING_URL",
    "news": "RAG_NEWS_URL",
    "stocks": "RAG_STOCKS_URL",
    "flights": "RAG_FLIGHTS_URL",
    "airports": "RAG_AIRPORTS_URL",
}

_warned_legacy_fallback: set[str] = set()


def _reset_legacy_warning_cache() -> None:
    """Test-only: clear the one-time-warning dedup set between test cases."""
    _warned_legacy_fallback.clear()


def resolve_rag_base_url(
    service_name: str,
    legacy_port: int,
    db: Optional[Session] = None,
) -> tuple[Optional[str], str]:
    """Resolve a RAG service's base URL (no trailing slash, no path).

    Returns (base_url, source). `source` is one of "registry", "env",
    "legacy", or "unconfigured" (`base_url` is None only for
    "unconfigured").
    """
    if db is not None:
        try:
            from app.models import RagService  # local import: avoid a hard
            # dependency on the ORM for callers that only want env/legacy
            # resolution (e.g. no db session available).
            svc = db.query(RagService).filter(RagService.name == service_name).first()
            if svc is not None and svc.enabled:
                base = svc.endpoint_url or f"{svc.protocol or 'http'}://{svc.host}:{svc.port}"
                base = (base or "").strip().rstrip("/")
                if base:
                    return base, "registry"
        except Exception as e:
            logger.warning("rag_url_registry_lookup_failed", service=service_name, error=str(e))

    env_name = CANONICAL_RAG_ENV_NAMES.get(service_name)
    if env_name:
        env_value = os.getenv(env_name, "").strip().rstrip("/")
        if env_value:
            return env_value, "env"

    legacy_env_name = "RAG_HOST" if os.getenv("RAG_HOST") else "RAG_SERVICE_HOST"
    legacy_host = (os.getenv("RAG_HOST") or os.getenv("RAG_SERVICE_HOST") or "").strip().rstrip("/")
    if legacy_host:
        if service_name not in _warned_legacy_fallback:
            _warned_legacy_fallback.add(service_name)
            logger.warning(
                "rag_url_legacy_host_fallback",
                service=service_name,
                env_used=legacy_env_name,
                message=(
                    f"No service-registry row or {env_name or 'RAG_<NAME>_URL'} set for "
                    f"'{service_name}'; falling back to legacy {legacy_env_name}. Each RAG "
                    "is its own Kubernetes Service -- set the canonical env var or a "
                    "service-registry row instead."
                ),
            )
        host = legacy_host if "://" in legacy_host else f"http://{legacy_host}"
        return f"{host}:{legacy_port}", "legacy"

    return None, "unconfigured"


def resolve_rag_url(
    service_name: str,
    legacy_port: int,
    path: str = "",
    db: Optional[Session] = None,
) -> tuple[Optional[str], str]:
    """`resolve_rag_base_url` plus a path suffix.

    Returns (None, "unconfigured") when no source resolves.
    """
    base, source = resolve_rag_base_url(service_name, legacy_port, db)
    if base is None:
        return None, source
    if path and not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}", source


async def check_ssrf_safe(url: str) -> tuple[bool, str]:
    """Validate a resolved RAG/service URL before issuing a live probe
    against it (codex BLOCK, 2026-09-27-diagnose-athena-mission-control
    review): registry rows and env vars are operator-set, but that's a
    write-time trust decision -- DNS can change afterward (rebinding), so
    every caller that actually probes a resolved URL must still pass it
    through the health poller's SSRF/runtime-DNS allowlist, not skip it
    because the host "is operator data".

    Validates the FULL url -- host, port, AND the actual path (plus any
    query string) that will be sent on the wire (codex r2 delta: the
    previous version defaulted the path check to "" regardless of what
    was actually in `url`, so CRLF/NUL/traversal injected via an appended
    path or query string -- e.g. user-supplied text in a RAG probe URL --
    was never actually checked). Callers must build the final request URL
    (path, query string, any user-supplied text already URL-encoded) BEFORE
    calling this, then use that same URL for the request.

    Imports (not reimplements) app.services.health_poller._validate_service_url
    -- the same allowlist (HEALTH_POLL_ALLOWED_PRIVATE_HOSTS, the k8s
    control-plane hostname block, CRLF/NUL/traversal path rejection) the
    background poller and the service-registry quick-checks already use.

    Returns (allowed, reason); reason is non-empty only when blocked.
    """
    from urllib.parse import urlparse
    from app.services.health_poller import _validate_service_url

    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path_and_query = parsed.path or ""
    if parsed.query:
        path_and_query = f"{path_and_query}?{parsed.query}"
    return await _validate_service_url(host, port, path_and_query)


async def check_ollama_ssrf_safe(url: str) -> tuple[bool, str]:
    """check_ssrf_safe(), plus the not-in-cluster loopback carve-out this
    repo already applies at the Ollama write-boundary (POST
    /api/settings/ollama-url -- see docs/CONFIGURATION.md "Ollama URL write
    validation") but never extended to the runtime probes themselves
    (codex diff-review Medium, 2026-09-28: component_models.py's model
    discovery and voice_tests.py's Ollama probes inherit the health
    poller's default-deny allowlist with no carve-out, so the OSS default
    http://localhost:11434 stops working for bare-metal dev unless the
    operator sets HEALTH_POLL_ALLOWED_PRIVATE_HOSTS -- a genuine
    works-out-of-the-box regression this wrapper closes for Ollama
    specifically, without touching the shared allowlist's posture for
    anything else (RAG probes, the health poller, service-registry writes
    all keep calling check_ssrf_safe() directly, unchanged).

    Posture, unchanged for everything this carve-out does NOT cover:
    - Inside a Kubernetes pod (KUBERNETES_SERVICE_HOST set): no carve-out,
      ever -- an in-cluster Ollama Service still needs its CIDR/hostname in
      HEALTH_POLL_ALLOWED_PRIVATE_HOSTS, same as any other in-cluster
      Service (is_local_host() returns False unconditionally in a pod).
    - A private host that ISN'T loopback/RFC1918/ULA reachable outside a
      pod (there is no such thing -- is_local_host() covers exactly
      loopback/RFC1918/ULA) gets no carve-out either.

    Returns (allowed, reason); reason is non-empty only when blocked (by
    check_ssrf_safe AND not covered by the carve-out).
    """
    from urllib.parse import urlparse
    from app.utils.url_validators import is_local_host

    allowed, reason = await check_ssrf_safe(url)
    if allowed:
        return allowed, reason

    hostname = urlparse(url).hostname or ""
    if is_local_host(hostname):
        logger.warning(
            "ollama_ssrf_local_dev_carveout",
            url=url,
            original_reason=reason,
            message=(
                "Ollama probe host is loopback/RFC1918/ULA and this process is not "
                "running inside a Kubernetes pod -- allowing as local dev, bypassing "
                "the HEALTH_POLL_ALLOWED_PRIVATE_HOSTS allowlist for this probe only."
            ),
        )
        return True, ""

    return allowed, reason

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

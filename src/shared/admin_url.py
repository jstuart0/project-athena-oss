"""Canonical admin-backend URL resolver for Project Athena.

Single source of truth for ``ADMIN_API_URL`` / ``ADMIN_BACKEND_URL`` /
``ADMIN_INTERNAL_URL`` resolution across every Athena service.  Replaces 32
independent resolution sites that each had subtly different env-var priority and
fallback values.

OSS-First: no maintainer-specific defaults.  Empty resolution returns ``""`` plus
a WARNING log rather than a working homelab URL.  ``LOCAL_DEV=true`` is an
explicit escape hatch for local-dev workflows that previously relied on the
hostname-string branch in ``service_registry.py``.

Resolution order (first non-empty wins):
    1. ``ADMIN_API_URL`` env var — canonical primary
    2. ``ADMIN_BACKEND_URL`` env var — alias, kept for backward compat
    3. ``ADMIN_INTERNAL_URL`` env var — DEPRECATED alias; kept for jarvis-web
       backwards compat; will be removed in a future release.  Use
       ``ADMIN_API_URL`` instead.
    4. ``LOCAL_DEV=true`` → ``http://localhost:8080``
       Escape hatch for running services outside Kubernetes during local
       development.  Set this explicitly rather than relying on env-var fallback.
    5. K8s in-cluster (``KUBERNETES_SERVICE_HOST`` set OR ``IN_CLUSTER=true``)
       → ``http://athena-admin-backend:8080`` (short-form, same-namespace DNS)
    6. Otherwise → ``""`` + WARNING log.  Callers must handle the empty string;
       the first downstream HTTP call will fail with a clear error at that point.

Replaces:
- librarian:1 (audit) — 5+ admin-URL resolution functions
- bob:7 (audit) — 3 competing admin-URL resolution mechanisms
- librarian:7 (audit) — cache.py ignoring ADMIN_API_URL
"""
import functools
import logging
import os

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def get_admin_url() -> str:
    """Resolve the admin-backend base URL.

    Cached: resolution runs once per process.  The result is memoised via
    ``functools.lru_cache`` so subsequent calls are O(1) dict lookups with no
    env-var reads.  Tests can reset the cache with ``_clear_cache_for_tests()``.

    Returns:
        The resolved admin-backend URL with no trailing slash, or ``""`` if
        no configuration is found.  The caller is responsible for handling the
        empty-string case; this function never raises.

    Local development:
        If no admin env var is configured and you are running outside Kubernetes,
        set ``LOCAL_DEV=true`` in your shell or ``.env`` file to have the helper
        return ``http://localhost:8080`` automatically.  The recommended approach
        is to set ``ADMIN_API_URL`` explicitly.

    Note on ``ADMIN_INTERNAL_URL``:
        This alias is DEPRECATED.  If you are currently setting it, migrate to
        ``ADMIN_API_URL``.  It will be removed in a future release.
    """
    # Priority 1 — canonical env var
    url = os.getenv("ADMIN_API_URL", "").strip().rstrip("/")
    if url:
        logger.info("admin_url_resolved", extra={"source": "ADMIN_API_URL", "url": url})
        return url

    # Priority 2 — backward-compat alias
    url = os.getenv("ADMIN_BACKEND_URL", "").strip().rstrip("/")
    if url:
        logger.info("admin_url_resolved", extra={"source": "ADMIN_BACKEND_URL", "url": url})
        return url

    # Priority 3 — deprecated alias (jarvis-web backwards compat)
    url = os.getenv("ADMIN_INTERNAL_URL", "").strip().rstrip("/")
    if url:
        logger.info(
            "admin_url_resolved",
            extra={"source": "ADMIN_INTERNAL_URL", "url": url},
        )
        logger.debug(
            "ADMIN_INTERNAL_URL is deprecated; migrate to ADMIN_API_URL",
            extra={"source": "ADMIN_INTERNAL_URL"},
        )
        return url

    # Priority 4 — local-dev escape hatch
    if os.getenv("LOCAL_DEV", "").lower() == "true":
        url = "http://localhost:8080"
        logger.info("admin_url_resolved", extra={"source": "local_dev", "url": url})
        return url

    # Priority 5 — Kubernetes in-cluster auto-detection
    if os.getenv("KUBERNETES_SERVICE_HOST") or os.getenv("IN_CLUSTER", "").lower() == "true":
        url = "http://athena-admin-backend:8080"
        logger.info("admin_url_resolved", extra={"source": "in_cluster_default", "url": url})
        return url

    # Priority 6 — unresolvable; return empty string so callers fail loudly.
    # Emit admin_url_resolved first (consistent logging contract across all 6
    # paths), then a WARNING so operators know misconfiguration occurred.
    logger.info("admin_url_resolved", extra={"source": "empty", "url": ""})
    logger.warning(
        "admin_url_not_configured: ADMIN_API_URL is not set and the process is "
        "not running inside Kubernetes.  Admin-backend calls will fail.  "
        "Set ADMIN_API_URL=http://your-admin-backend:8080 or LOCAL_DEV=true.",
        extra={"source": "empty"},
    )
    return ""


def _clear_cache_for_tests() -> None:
    """Reset the ``lru_cache`` on ``get_admin_url``.

    PRIVATE — production code must never call this.  Unit tests should call it
    from a ``@pytest.fixture(autouse=True)`` so that each test starts with a
    fresh cache and does not inherit env-var state from a previous test.

    Important note for downstream test authors
    ------------------------------------------
    ``_clear_cache_for_tests()`` resets only the helper's cache.  If a module
    under test captures ``get_admin_url()`` at *import time* into a module-level
    constant (e.g. ``ADMIN_API_URL = get_admin_url()`` in ``config_loader.py``
    or ``cache.py``), that constant retains the value from the first import and
    is NOT affected by later ``_clear_cache_for_tests()`` calls.

    To exercise different env-var configurations against those modules you must
    either:
    - Set env vars via ``monkeypatch.setenv(...)`` BEFORE importing the affected
      module (i.e. before it appears anywhere in the import chain), or
    - Call ``importlib.reload(module)`` after ``_clear_cache_for_tests()`` to
      force the module to re-execute its module-level statements.
    """
    get_admin_url.cache_clear()

"""Canonical configuration for Project Athena.

Single source of truth for environment-variable-driven configuration across
every Athena service.  Backed by ``pydantic_settings.BaseSettings`` so that
type coercion, ``.env`` file loading, and validation come for free.

Resolution order (per pydantic-settings default):
    1. Constructor kwargs (used by tests)
    2. Environment variables (case-insensitive — ``OLLAMA_URL`` populates
       the ``ollama_url`` field automatically because pydantic-settings
       auto-uppercases snake_case field names)
    3. ``.env`` file (if present in CWD; disabled in tests via ``_TestConfig``)
    4. Field defaults

Lazy single-instantiation pattern
----------------------------------
``AthenaConfig`` is instantiated exactly once per process via the
``@lru_cache``-decorated ``get_config()`` factory.  This mirrors the
``get_admin_url()`` pattern from ``src/shared/admin_url.py`` and avoids
import-order traps where module-level eager instantiation would read env
vars before test fixtures have set them.

Tests can reset the cache with ``_clear_cache_for_tests()``.

Currently-modelled fields (Campaign 4)
----------------------------------------
Fields below cover 11 high-leverage env vars migrated in Campaign 4.  The
remaining ~220 env vars in the codebase are migrated per-PR as those areas
are touched — see ``CONTRIBUTING.md`` for the extension pattern.

LLM endpoint resolution
------------------------
``LLM_SERVICE_URL`` and ``OLLAMA_URL`` are loaded as two distinct fields.
The ``llm_endpoint`` computed property exposes the dominant precedence
``LLM_SERVICE_URL > OLLAMA_URL > default``.  Sites that historically read
only ``OLLAMA_URL`` should use ``ollama_url``; sites that read the paired
``LLM_SERVICE_URL or OLLAMA_URL`` expression should use ``llm_endpoint``.

admin_url — NOT env-loadable (Campaign 4 round-2 R2-C1)
----------------------------------------------------------
``admin_url`` is a ``@computed_field`` ``@property``, NOT a settings field.
Setting ``ADMIN_URL`` in the environment has zero effect on this attribute.
Resolution always delegates to ``get_admin_url()`` (Campaign 3 helper), which
is the canonical single source of truth for admin-backend URL resolution.

OSS-First
----------
All defaults match the existing ``os.getenv("FOO", default)`` resolutions in
the migrated call sites.  No field is required.  Empty values are returned
as-is; per-service code is responsible for validating critical inputs.
"""
from __future__ import annotations

import functools
import logging

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.admin_url import get_admin_url

logger = logging.getLogger(__name__)


class AthenaConfig(BaseSettings):
    """Centralized Athena configuration backed by pydantic-settings.

    See module docstring for the full resolution order and OSS-First contract.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # case_sensitive intentionally NOT set (default False).
        # pydantic-settings auto-uppercases snake_case field names when matching
        # env vars, so ``OLLAMA_URL`` populates ``ollama_url``, ``DEV_MODE``
        # populates ``dev_mode``, etc.  Setting case_sensitive=True would require
        # an explicit Field(validation_alias=...) on every field.
        extra="ignore",  # ignore env vars we don't model yet; don't crash
    )

    # ------------------------------------------------------------------
    # admin_url — Campaign 3 helper (NOT a settings field)
    # ------------------------------------------------------------------
    # Round-2 R2-C1: must NOT be a settings Field so that pydantic-settings
    # cannot load it from an ADMIN_URL env var.  Computed at access time
    # and always resolved via the canonical Campaign 3 helper.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def admin_url(self) -> str:
        """Admin-backend URL, always resolved via the Campaign 3 helper.

        NOT env-loadable.  ``ADMIN_URL`` env var has no effect here.
        Set ``ADMIN_API_URL`` (or the other vars the helper checks) to
        influence the resolved value.  See ``src/shared/admin_url.py``
        for the full six-priority resolution chain.
        """
        return get_admin_url()

    # ------------------------------------------------------------------
    # LLM endpoints
    # ------------------------------------------------------------------
    ollama_url: str = Field(default="http://localhost:11434")
    llm_service_url: str = Field(default="")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def llm_endpoint(self) -> str:
        """LLM endpoint with dominant precedence: LLM_SERVICE_URL > OLLAMA_URL > default.

        Use this at sites that previously had the paired expression
        ``os.getenv("LLM_SERVICE_URL") or os.getenv("OLLAMA_URL", default)``.

        Sites that previously read only ``OLLAMA_URL`` (e.g. ``ollama_client.py``)
        should use ``ollama_url`` directly — they never consulted ``LLM_SERVICE_URL``
        and their behavior should be preserved.
        """
        return self.llm_service_url or self.ollama_url

    # ------------------------------------------------------------------
    # Cache / data store
    # ------------------------------------------------------------------
    # redis_url default targets the in-cluster DNS shortname (matches
    # admin/backend/main.py:109 production branch and manifests/athena-prod/
    # config.yaml).  Local-dev users should set REDIS_URL=redis://localhost:6379
    # in their .env file — see .env.example.  (Round-2 R2-H4.)
    redis_url: str = Field(default="redis://redis:6379/0")
    database_url: str = Field(default="")

    # ------------------------------------------------------------------
    # Service-to-service auth
    # ------------------------------------------------------------------
    service_api_key: str = Field(default="")

    # ------------------------------------------------------------------
    # Personalization
    # ------------------------------------------------------------------
    default_timezone: str = Field(default="UTC")
    default_city: str = Field(default="")

    # ------------------------------------------------------------------
    # OIDC — initial-load values only
    # ------------------------------------------------------------------
    # NOTE: runtime mutation of the OIDC module-level globals (oidc.py:48)
    # via configure_oauth_client() is unchanged.  These fields capture the
    # env-var values at service startup (the same slot that was previously
    # module-level os.getenv("OIDC_ISSUER", "")).
    oidc_issuer: str = Field(default="")
    oidc_client_id: str = Field(default="")

    @field_validator("oidc_issuer", "oidc_client_id", mode="before")
    @classmethod
    def _strip_oidc_whitespace(cls, v: object) -> object:
        """Strip surrounding whitespace from OIDC string values.

        Existing call sites (e.g. ``admin/backend/main.py:261, 249``) all
        call ``.strip()`` on the env-var value.  Centralizing the strip here
        removes that burden from every caller.
        """
        if isinstance(v, str):
            return v.strip()
        return v

    # ------------------------------------------------------------------
    # Mode flags
    # ------------------------------------------------------------------
    dev_mode: bool = Field(default=False)
    demo_mode: bool = Field(default=False)

    # ------------------------------------------------------------------
    # Deferred fields — see CONTRIBUTING.md for the extension pattern
    # ------------------------------------------------------------------
    # Fields below are NOT yet migrated to AthenaConfig.  They are listed
    # here as a reference for future contributors.  Each field should follow
    # the same pattern as the fields above (plain Field with default, or
    # @computed_field for derived values).
    #
    # LOCAL_DEV — CIRCULAR IMPORT RISK.  admin_url.py reads LOCAL_DEV
    #   directly; adding it here would create a config → admin_url → config
    #   circular dependency.  Future path: extract LOCAL_DEV into
    #   src/shared/_env_flags.py, importable from both modules.
    #
    # CONTROL_AGENT_URL (4 sites), HA_URL / HA_TOKEN (10+13 sites),
    # OIDC_* aliases beyond OIDC_ISSUER / OIDC_CLIENT_ID — runtime-mutable
    # globals in oidc.py; deferred pending separate analysis.


@functools.lru_cache(maxsize=1)
def get_config() -> AthenaConfig:
    """Return the singleton ``AthenaConfig`` for this process.

    Cached: ``AthenaConfig`` is instantiated exactly once.  This mirrors the
    ``get_admin_url()`` pattern from ``src/shared/admin_url.py``.

    Tests can reset the cache with ``_clear_cache_for_tests()``.
    """
    config = AthenaConfig()
    logger.info(
        "athena_config_loaded",
        extra={
            "ollama_url_set": bool(config.ollama_url),
            "llm_service_url_set": bool(config.llm_service_url),
            "redis_url_set": bool(config.redis_url),
            "database_url_set": bool(config.database_url),
            "service_api_key_set": bool(config.service_api_key),
            "oidc_issuer_set": bool(config.oidc_issuer),
            "oidc_client_id_set": bool(config.oidc_client_id),
            "dev_mode": config.dev_mode,
            "demo_mode": config.demo_mode,
        },
    )
    return config


def _clear_cache_for_tests() -> None:
    """Reset the ``get_config`` lru_cache.

    PRIVATE — production code must never call this.  Unit tests should call
    it from a ``@pytest.fixture(autouse=True)`` so each test starts with a
    fresh config instance that re-reads env vars.

    Important: modules that capture config values at *import time* into a
    module-level constant (e.g. ``REDIS_URL = get_config().redis_url``) will
    NOT be affected by this call — the constant already holds the value from
    the first import.  To test different env-var values against those modules,
    either use ``monkeypatch.setenv(...)`` BEFORE importing the affected module,
    or call ``importlib.reload(module)`` after ``_clear_cache_for_tests()``
    to force re-execution of the module-level statements.

    See the equivalent note in ``src/shared/admin_url.py::_clear_cache_for_tests``.
    """
    get_config.cache_clear()

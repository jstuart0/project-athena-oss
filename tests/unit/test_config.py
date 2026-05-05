"""Unit tests for ``src/shared/config.py``.

Covers all ~21 cases specified in the Phase 1 plan
(2026-05-06-deliver-config-py-rebuild.md, round-2).

Cache isolation
---------------
``get_config`` uses ``functools.lru_cache(maxsize=1)`` so it resolves env vars
exactly once per process.  The ``autouse`` fixture below calls
``_clear_cache_for_tests()`` before every test so each test starts with a clean
slate regardless of test execution order.

Note: modules that capture ``get_config().foo`` at *import time* into a
module-level constant will NOT be affected by ``_clear_cache_for_tests()``.
Use ``monkeypatch.setenv`` BEFORE importing the affected module, or call
``importlib.reload(module)`` to force re-resolution.

Test-config isolation
---------------------
Default-value tests use ``_TestConfig`` (a subclass of ``AthenaConfig`` with
``env_file=None``) so that a developer's local ``.env`` file in the repo root
does not leak into assertions about default values.  (Round-1 M1.)
"""
import logging
import sys

import pytest

# Insert src/ so `shared.*` imports resolve without the package being installed.
sys.path.insert(0, "src")

import shared.admin_url as _admin_url_module  # noqa: E402
from shared.config import (  # noqa: E402
    AthenaConfig,
    _clear_cache_for_tests,
    get_config,
)
from pydantic_settings import SettingsConfigDict  # noqa: E402


# ---------------------------------------------------------------------------
# Test-config subclass — env_file=None so local .env does not pollute tests
# ---------------------------------------------------------------------------

class _TestConfig(AthenaConfig):
    """AthenaConfig with env_file disabled for unit-test isolation."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_ignore_empty=False,
        extra="ignore",
    )


# ---------------------------------------------------------------------------
# Env vars controlled by these tests — used for blanket cleanup
# ---------------------------------------------------------------------------

_CONTROLLED_VARS = (
    "OLLAMA_URL",
    "LLM_SERVICE_URL",
    "REDIS_URL",
    "DATABASE_URL",
    "SERVICE_API_KEY",
    "DEFAULT_TIMEZONE",
    "DEFAULT_CITY",
    "OIDC_ISSUER",
    "OIDC_CLIENT_ID",
    "DEV_MODE",
    "DEMO_MODE",
    # admin_url helper vars
    "ADMIN_URL",
    "ADMIN_API_URL",
    "ADMIN_BACKEND_URL",
    "ADMIN_INTERNAL_URL",
    "LOCAL_DEV",
    "KUBERNETES_SERVICE_HOST",
    "IN_CLUSTER",
)


def _clear_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every env var these tests care about."""
    for var in _CONTROLLED_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Autouse cache-reset fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_caches():
    """Clear both the get_config and get_admin_url caches before/after each test."""
    _clear_cache_for_tests()
    _admin_url_module._clear_cache_for_tests()
    yield
    _clear_cache_for_tests()
    _admin_url_module._clear_cache_for_tests()


# ---------------------------------------------------------------------------
# 1. Default values — no env vars set
# ---------------------------------------------------------------------------


class TestDefaults:
    """Each field returns its documented default when no env var is set."""

    def test_01_ollama_url_default(self, monkeypatch):
        """OLLAMA_URL unset → http://localhost:11434."""
        _clear_all(monkeypatch)
        cfg = _TestConfig()
        assert cfg.ollama_url == "http://localhost:11434"

    def test_02_llm_service_url_default(self, monkeypatch):
        """LLM_SERVICE_URL unset → empty string."""
        _clear_all(monkeypatch)
        cfg = _TestConfig()
        assert cfg.llm_service_url == ""

    def test_03_llm_endpoint_default(self, monkeypatch):
        """llm_endpoint with no overrides → falls back to ollama_url default."""
        _clear_all(monkeypatch)
        cfg = _TestConfig()
        assert cfg.llm_endpoint == "http://localhost:11434"

    def test_04_redis_url_default(self, monkeypatch):
        """REDIS_URL unset → in-cluster DNS shortname (round-2 R2-H4)."""
        _clear_all(monkeypatch)
        cfg = _TestConfig()
        assert cfg.redis_url == "redis://redis:6379/0"

    def test_05_database_url_default(self, monkeypatch):
        """DATABASE_URL unset → empty string."""
        _clear_all(monkeypatch)
        cfg = _TestConfig()
        assert cfg.database_url == ""

    def test_06_service_api_key_default(self, monkeypatch):
        """SERVICE_API_KEY unset → empty string."""
        _clear_all(monkeypatch)
        cfg = _TestConfig()
        assert cfg.service_api_key == ""

    def test_07_default_timezone_default(self, monkeypatch):
        """DEFAULT_TIMEZONE unset → UTC."""
        _clear_all(monkeypatch)
        cfg = _TestConfig()
        assert cfg.default_timezone == "UTC"

    def test_08_default_city_default(self, monkeypatch):
        """DEFAULT_CITY unset → empty string."""
        _clear_all(monkeypatch)
        cfg = _TestConfig()
        assert cfg.default_city == ""

    def test_09_oidc_issuer_default(self, monkeypatch):
        """OIDC_ISSUER unset → empty string."""
        _clear_all(monkeypatch)
        cfg = _TestConfig()
        assert cfg.oidc_issuer == ""

    def test_10_oidc_client_id_default(self, monkeypatch):
        """OIDC_CLIENT_ID unset → empty string."""
        _clear_all(monkeypatch)
        cfg = _TestConfig()
        assert cfg.oidc_client_id == ""

    def test_11_dev_mode_default(self, monkeypatch):
        """DEV_MODE unset → False."""
        _clear_all(monkeypatch)
        cfg = _TestConfig()
        assert cfg.dev_mode is False

    def test_12_demo_mode_default(self, monkeypatch):
        """DEMO_MODE unset → False."""
        _clear_all(monkeypatch)
        cfg = _TestConfig()
        assert cfg.demo_mode is False


# ---------------------------------------------------------------------------
# 2. Env-var loading — each field reads from its env var
# ---------------------------------------------------------------------------


class TestEnvVarLoading:
    """Setting the env var causes the field to pick up the new value."""

    def test_13_ollama_url_from_env(self, monkeypatch):
        """OLLAMA_URL env var → ollama_url field."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("OLLAMA_URL", "http://my-ollama:11434")
        cfg = _TestConfig()
        assert cfg.ollama_url == "http://my-ollama:11434"

    def test_14_redis_url_from_env(self, monkeypatch):
        """REDIS_URL env var → redis_url field."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        cfg = _TestConfig()
        assert cfg.redis_url == "redis://localhost:6379"

    def test_15_dev_mode_coercion(self, monkeypatch):
        """DEV_MODE coerces string 'true' / 'false' / 'TRUE' correctly."""
        _clear_all(monkeypatch)
        for truthy in ("true", "True", "TRUE", "1"):
            _clear_cache_for_tests()
            monkeypatch.setenv("DEV_MODE", truthy)
            assert _TestConfig().dev_mode is True, f"Expected True for DEV_MODE={truthy!r}"

        for falsy in ("false", "False", "FALSE", "0"):
            _clear_cache_for_tests()
            monkeypatch.setenv("DEV_MODE", falsy)
            assert _TestConfig().dev_mode is False, f"Expected False for DEV_MODE={falsy!r}"


# ---------------------------------------------------------------------------
# 3. LLM endpoint precedence
# ---------------------------------------------------------------------------


class TestLlmEndpointPrecedence:
    """llm_endpoint computed property: LLM_SERVICE_URL > OLLAMA_URL > default."""

    def test_16_llm_service_url_wins_over_ollama_url(self, monkeypatch):
        """LLM_SERVICE_URL set → llm_endpoint returns LLM_SERVICE_URL."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("LLM_SERVICE_URL", "http://llm-service:8000")
        monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
        cfg = _TestConfig()
        assert cfg.llm_endpoint == "http://llm-service:8000"

    def test_17_ollama_url_fallback_when_llm_service_url_empty(self, monkeypatch):
        """LLM_SERVICE_URL unset → llm_endpoint falls back to ollama_url."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
        cfg = _TestConfig()
        assert cfg.llm_endpoint == "http://ollama:11434"
        assert cfg.ollama_url == "http://ollama:11434"

    def test_17b_constructor_injection_precedence(self):
        """Constructor kwargs work for precedence testing without env-var manipulation."""
        cfg = _TestConfig(llm_service_url="http://svc:1", ollama_url="http://oll:2")
        assert cfg.llm_endpoint == "http://svc:1"

        cfg2 = _TestConfig(llm_service_url="", ollama_url="http://oll:2")
        assert cfg2.llm_endpoint == "http://oll:2"


# ---------------------------------------------------------------------------
# 4. OIDC whitespace stripping
# ---------------------------------------------------------------------------


class TestOidcStripping:
    """OIDC field_validator strips surrounding whitespace."""

    def test_18_oidc_issuer_whitespace_stripped(self, monkeypatch):
        """OIDC_ISSUER with surrounding spaces → stripped."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("OIDC_ISSUER", "  https://auth.example.com  ")
        cfg = _TestConfig()
        assert cfg.oidc_issuer == "https://auth.example.com"

    def test_19_oidc_client_id_whitespace_stripped(self, monkeypatch):
        """OIDC_CLIENT_ID with surrounding spaces → stripped."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("OIDC_CLIENT_ID", "\tmy-client-id\n")
        cfg = _TestConfig()
        assert cfg.oidc_client_id == "my-client-id"

    def test_19b_oidc_constructor_stripping(self):
        """Whitespace stripped even when passed via constructor kwargs."""
        cfg = _TestConfig(oidc_issuer="  https://example.com  ", oidc_client_id=" id ")
        assert cfg.oidc_issuer == "https://example.com"
        assert cfg.oidc_client_id == "id"


# ---------------------------------------------------------------------------
# 5. admin_url — computed property wiring + R2-C1 env-var isolation
# ---------------------------------------------------------------------------


class TestAdminUrl:
    """admin_url is computed via get_admin_url(), not loaded from env."""

    def test_20_admin_url_wired_to_admin_api_url(self, monkeypatch):
        """ADMIN_API_URL env var → get_config().admin_url resolves to that value."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("ADMIN_API_URL", "http://admin-backend:8080")
        cfg = _TestConfig()
        assert cfg.admin_url == "http://admin-backend:8080"

    def test_20b_admin_url_not_env_loadable(self, monkeypatch):
        """ADMIN_URL env var must NOT override the get_admin_url() helper (R2-C1).

        pydantic-settings would read ADMIN_URL into a field named admin_url if
        admin_url were a settings Field.  It is a @computed_field @property, so
        ADMIN_URL is silently ignored.
        """
        _clear_all(monkeypatch)
        monkeypatch.setenv("ADMIN_URL", "http://wrong:1")  # must be ignored
        monkeypatch.setenv("ADMIN_API_URL", "http://right:2")  # canonical
        # clear both caches so fresh resolution uses the new env state
        _clear_cache_for_tests()
        _admin_url_module._clear_cache_for_tests()
        cfg = _TestConfig()
        assert cfg.admin_url == "http://right:2", (
            "ADMIN_URL env var must not override admin_url — use ADMIN_API_URL"
        )
        assert cfg.admin_url != "http://wrong:1"

    def test_20c_admin_url_empty_when_no_env(self, monkeypatch):
        """With no admin env vars set, admin_url returns '' (helper's empty path)."""
        _clear_all(monkeypatch)
        cfg = _TestConfig()
        assert cfg.admin_url == ""


# ---------------------------------------------------------------------------
# 6. Lazy factory — get_config() caching and cache-clear
# ---------------------------------------------------------------------------


class TestLazyFactory:
    """get_config() returns the same instance on repeat calls; _clear_cache_for_tests resets it."""

    def test_21_get_config_returns_same_instance(self, monkeypatch):
        """Two calls to get_config() return the identical object (cache works)."""
        _clear_all(monkeypatch)
        first = get_config()
        second = get_config()
        assert first is second

    def test_22_get_config_single_log_record(self, monkeypatch, caplog):
        """Two get_config() calls emit exactly one athena_config_loaded INFO record."""
        _clear_all(monkeypatch)
        with caplog.at_level(logging.INFO, logger="shared.config"):
            get_config()
            get_config()
        loaded_records = [r for r in caplog.records if "athena_config_loaded" in r.message]
        assert len(loaded_records) == 1

    def test_23_clear_cache_forces_fresh_instance(self, monkeypatch):
        """_clear_cache_for_tests() causes the next get_config() call to build a new instance."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("OLLAMA_URL", "http://first:11434")
        first = get_config()
        assert first.ollama_url == "http://first:11434"

        _clear_cache_for_tests()
        monkeypatch.setenv("OLLAMA_URL", "http://second:11434")
        second = get_config()
        assert second.ollama_url == "http://second:11434"

        assert first is not second

    def test_24_demo_mode_lazy(self, monkeypatch):
        """Round-1 M6: DEMO_MODE is read lazily at first get_config() call.

        After setting DEMO_MODE=true and clearing the cache, get_config().demo_mode
        returns True.  Without a cache clear, subsequent calls return the cached
        value (True) even after the env var changes.  After a second cache clear,
        the fresh instance reflects the updated env var.
        """
        _clear_all(monkeypatch)
        monkeypatch.setenv("DEMO_MODE", "true")
        _clear_cache_for_tests()
        assert get_config().demo_mode is True

        monkeypatch.setenv("DEMO_MODE", "false")
        # Without cache clear, value should still be True (cached).
        assert get_config().demo_mode is True

        _clear_cache_for_tests()
        assert get_config().demo_mode is False

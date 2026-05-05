"""Unit tests for ``src/shared/admin_url.py``.

Covers all 15 cases specified in the Phase 1 plan
(2026-05-06-deliver-admin-url-consolidation.md, round-2).

Cache isolation
---------------
``get_admin_url`` uses ``functools.lru_cache(maxsize=1)`` so it resolves env vars
exactly once per process.  The ``autouse`` fixture below calls
``_clear_cache_for_tests()`` before every test so each test starts with a clean
slate regardless of test execution order.

Note for downstream authors (copied from ``admin_url._clear_cache_for_tests``):
Modules that capture ``get_admin_url()`` at *import time* into a module-level
constant (e.g. ``ADMIN_API_URL = get_admin_url()`` in ``config_loader.py``) will
NOT be affected by ``_clear_cache_for_tests()``.  Use ``monkeypatch.setenv`` BEFORE
importing the affected module, or call ``importlib.reload(module)`` to force
re-resolution.
"""
import logging
import sys

import pytest

# Insert src/ so `shared.*` imports resolve without the package being installed.
sys.path.insert(0, "src")

from shared.admin_url import _clear_cache_for_tests, get_admin_url  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_ADMIN_ENV_VARS = ("ADMIN_API_URL", "ADMIN_BACKEND_URL", "ADMIN_INTERNAL_URL")
_K8S_ENV_VARS = ("KUBERNETES_SERVICE_HOST", "IN_CLUSTER")
_DEV_ENV_VARS = ("LOCAL_DEV",)
_ALL_CONTROLLED = _ADMIN_ENV_VARS + _K8S_ENV_VARS + _DEV_ENV_VARS


def _clear_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every env var the helper inspects so tests start from a blank slate."""
    for var in _ALL_CONTROLLED:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear the lru_cache before (and after) every test."""
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestPriorityOrder:
    """Env-var priority and fallback chain."""

    def test_01_admin_api_url_wins(self, monkeypatch):
        """Case 1: ADMIN_API_URL set → returns its value."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("ADMIN_API_URL", "http://admin-api:9000")
        assert get_admin_url() == "http://admin-api:9000"

    def test_02_admin_backend_url_when_api_unset(self, monkeypatch):
        """Case 2: ADMIN_BACKEND_URL set, ADMIN_API_URL absent → returns BACKEND."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("ADMIN_BACKEND_URL", "http://admin-backend:8080")
        assert get_admin_url() == "http://admin-backend:8080"

    def test_03_admin_internal_url_when_others_unset(self, monkeypatch):
        """Case 3: ADMIN_INTERNAL_URL set, others absent → returns INTERNAL."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("ADMIN_INTERNAL_URL", "http://admin-internal:8080")
        assert get_admin_url() == "http://admin-internal:8080"

    def test_04_api_beats_backend_and_internal(self, monkeypatch):
        """Case 4: All three URL vars set → ADMIN_API_URL wins."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("ADMIN_API_URL", "http://api:1")
        monkeypatch.setenv("ADMIN_BACKEND_URL", "http://backend:2")
        monkeypatch.setenv("ADMIN_INTERNAL_URL", "http://internal:3")
        assert get_admin_url() == "http://api:1"

    def test_05_backend_beats_internal(self, monkeypatch):
        """Case 5: ADMIN_BACKEND_URL + ADMIN_INTERNAL_URL set, API absent → BACKEND wins."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("ADMIN_BACKEND_URL", "http://backend:2")
        monkeypatch.setenv("ADMIN_INTERNAL_URL", "http://internal:3")
        assert get_admin_url() == "http://backend:2"


class TestNormalisation:
    """Trailing-slash stripping and whitespace trimming."""

    def test_06_trailing_slash_stripped(self, monkeypatch):
        """Case 6: ADMIN_API_URL with trailing slash → slash removed."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("ADMIN_API_URL", "http://admin:8080/")
        assert get_admin_url() == "http://admin:8080"

    def test_07_whitespace_trimmed(self, monkeypatch):
        """Case 7: ADMIN_API_URL with surrounding whitespace → whitespace removed."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("ADMIN_API_URL", "  http://admin:8080  ")
        assert get_admin_url() == "http://admin:8080"


class TestLocalDevEscapeHatch:
    """LOCAL_DEV=true behaviour."""

    def test_08_local_dev_no_admin_env(self, monkeypatch):
        """Case 8: LOCAL_DEV=true, all admin/K8s vars absent → http://localhost:8080."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("LOCAL_DEV", "true")
        assert get_admin_url() == "http://localhost:8080"

    def test_09_admin_api_url_beats_local_dev(self, monkeypatch):
        """Case 9: LOCAL_DEV=true AND ADMIN_API_URL set → ADMIN_API_URL wins."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("ADMIN_API_URL", "http://real-admin:8080")
        monkeypatch.setenv("LOCAL_DEV", "true")
        assert get_admin_url() == "http://real-admin:8080"

    def test_10_local_dev_beats_k8s(self, monkeypatch):
        """Case 10: LOCAL_DEV=true AND KUBERNETES_SERVICE_HOST set, no admin env → LOCAL_DEV wins."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("LOCAL_DEV", "true")
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        assert get_admin_url() == "http://localhost:8080"


class TestKubernetesInCluster:
    """In-cluster auto-detection."""

    def test_11_kubernetes_service_host_triggers_in_cluster(self, monkeypatch):
        """Case 11: KUBERNETES_SERVICE_HOST set, no admin/dev vars → short-form K8s URL."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
        assert get_admin_url() == "http://athena-admin-backend:8080"

    def test_12_in_cluster_true_triggers_in_cluster(self, monkeypatch):
        """Case 12: IN_CLUSTER=true, no admin/dev vars → short-form K8s URL."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("IN_CLUSTER", "true")
        assert get_admin_url() == "http://athena-admin-backend:8080"


class TestEmptyFallback:
    """Unresolvable configuration."""

    def test_13_all_empty_returns_empty_and_warns(self, monkeypatch, caplog):
        """Case 13: All vars unset → returns '' and emits admin_url_not_configured warning."""
        _clear_all(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="shared.admin_url"):
            result = get_admin_url()
        assert result == ""
        assert any("admin_url_not_configured" in r.message for r in caplog.records)


class TestCachingBehaviour:
    """lru_cache semantics — log-once and cache-clear."""

    def test_14_cache_produces_single_info_log(self, monkeypatch, caplog):
        """Case 14: Two consecutive calls emit exactly one admin_url_resolved INFO record."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("ADMIN_API_URL", "http://admin:8080")
        with caplog.at_level(logging.INFO, logger="shared.admin_url"):
            get_admin_url()
            get_admin_url()
        resolved_records = [
            r for r in caplog.records if "admin_url_resolved" in r.message
        ]
        assert len(resolved_records) == 1

    def test_15_clear_cache_allows_re_resolution(self, monkeypatch):
        """Case 15: _clear_cache_for_tests() resets cache; next call re-reads env vars."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("ADMIN_API_URL", "http://first:8080")
        first = get_admin_url()
        assert first == "http://first:8080"

        _clear_cache_for_tests()
        monkeypatch.setenv("ADMIN_API_URL", "http://second:9090")
        second = get_admin_url()
        assert second == "http://second:9090"

        assert first != second

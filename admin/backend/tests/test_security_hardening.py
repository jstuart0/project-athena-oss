"""
Tests for the security hardening changes:
  - verify_service_api_key dependency (service_auth.py)
  - /api/internal/* endpoints require X-Service-Key header
  - /api/external-api-keys/public/* endpoints require X-Service-Key header
  - /api/service-control/public endpoint is removed (dead code deleted)
  - /services and /test-query endpoints are removed
  - /status and /api/status require authentication
  - /health remains open (k8s probe)
  - CORS_ALLOWED_ORIGINS env var is respected
  - cookie_https_only is tied to DEV_MODE
  - Production startup rejects insecure default secrets
"""
import os
import sys
import hmac

import pytest

# Set test environment BEFORE importing anything from the app
os.environ["DEV_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
# SERVICE_API_KEY must be non-empty so verify_service_api_key can evaluate wrong-key → 401.
# Without this the fail-closed guard returns 503 before comparing the key.
os.environ.setdefault("SERVICE_API_KEY", "test-service-key-for-hardening-tests")

# -------------------------------------------------------------------
# Unit tests — service_auth utility (no DB or app needed)
# -------------------------------------------------------------------

def _make_mini_app():
    """Return a minimal FastAPI app with a single /ping endpoint gated by verify_service_api_key."""
    import app.utils.service_auth as sa
    from fastapi import FastAPI, Depends

    mini = FastAPI()

    @mini.get("/ping")
    def ping(_: bool = Depends(sa.verify_service_api_key)):
        return {"ok": True}

    return mini


class TestVerifyServiceApiKey:
    """Unit tests for the verify_service_api_key FastAPI dependency.

    Since xander:40 refactored service_auth.py to read SERVICE_API_KEY at
    call-time via get_config() (rather than capturing it at module import),
    these tests patch os.environ and clear the lru_cache to control what
    get_config().service_api_key returns.
    """

    def test_correct_key_returns_true(self):
        """Correct service key should pass through and return True."""
        from fastapi.testclient import TestClient
        from shared.config import _clear_cache_for_tests

        os.environ["SERVICE_API_KEY"] = "test-secret-key-abc123"
        _clear_cache_for_tests()
        try:
            mini = _make_mini_app()
            with TestClient(mini, raise_server_exceptions=True) as c:
                r = c.get("/ping", headers={"X-Service-Key": "test-secret-key-abc123"})
            assert r.status_code == 200
        finally:
            os.environ["SERVICE_API_KEY"] = "test-service-key-for-hardening-tests"
            _clear_cache_for_tests()

    def test_wrong_key_returns_401(self):
        """Wrong service key should raise 401."""
        from fastapi.testclient import TestClient
        from shared.config import _clear_cache_for_tests

        os.environ["SERVICE_API_KEY"] = "real-secret"
        _clear_cache_for_tests()
        try:
            mini = _make_mini_app()
            with TestClient(mini, raise_server_exceptions=False) as c:
                r = c.get("/ping", headers={"X-Service-Key": "wrong-secret"})
            assert r.status_code == 401
        finally:
            os.environ["SERVICE_API_KEY"] = "test-service-key-for-hardening-tests"
            _clear_cache_for_tests()

    def test_missing_header_returns_422(self):
        """Missing X-Service-Key header should return 422 (required field missing)."""
        from fastapi.testclient import TestClient
        from shared.config import _clear_cache_for_tests

        os.environ["SERVICE_API_KEY"] = "real-secret"
        _clear_cache_for_tests()
        try:
            mini = _make_mini_app()
            with TestClient(mini, raise_server_exceptions=False) as c:
                r = c.get("/ping")  # No X-Service-Key header
            assert r.status_code == 422
        finally:
            os.environ["SERVICE_API_KEY"] = "test-service-key-for-hardening-tests"
            _clear_cache_for_tests()

    def test_uses_constant_time_comparison(self):
        """verify_service_api_key should use hmac.compare_digest (timing-safe)."""
        import inspect
        import app.utils.service_auth as sa

        source = inspect.getsource(sa.verify_service_api_key)
        assert "hmac.compare_digest" in source, (
            "verify_service_api_key must use hmac.compare_digest to prevent timing attacks"
        )

    def test_reads_from_config(self):
        """Key must be read via get_config().service_api_key at call time (xander:40)."""
        import inspect
        import app.utils.service_auth as sa

        source = inspect.getsource(sa)
        assert "get_config().service_api_key" in source, (
            "service_api_key must be read via get_config().service_api_key at call time "
            "(xander:40 — not captured at module import)"
        )

    def test_no_module_level_key_capture(self):
        """MODULE-LEVEL SERVICE_API_KEY constant must not exist (xander:40 regression guard)."""
        import app.utils.service_auth as sa

        assert not hasattr(sa, "SERVICE_API_KEY"), (
            "service_auth.SERVICE_API_KEY must not exist as a module-level attribute; "
            "the key must be read inside verify_service_api_key at call time (xander:40)"
        )

    def test_empty_secret_returns_503(self):
        """When SERVICE_API_KEY is empty-string (set but blank), verify_service_api_key returns 503.

        os.getenv returns "" (not None) when the key is set to an empty string in the
        environment (e.g. CONTROL_AGENT_URL: "" in a ConfigMap). This must be treated
        the same as unset — fail-closed with 503, not 401 — to prevent hmac.compare_digest
        accepting "" == "" and granting access to any caller sending an empty header.
        """
        from fastapi.testclient import TestClient
        from shared.config import _clear_cache_for_tests

        os.environ["SERVICE_API_KEY"] = ""
        _clear_cache_for_tests()
        try:
            mini = _make_mini_app()
            with TestClient(mini, raise_server_exceptions=False) as c:
                r = c.get("/ping", headers={"X-Service-Key": ""})
            assert r.status_code == 503, (
                f"Empty SERVICE_API_KEY must return 503 (fail-closed), got {r.status_code}"
            )
        finally:
            os.environ["SERVICE_API_KEY"] = "test-service-key-for-hardening-tests"
            _clear_cache_for_tests()


# -------------------------------------------------------------------
# Integration tests — full app with TestClient
# -------------------------------------------------------------------

@pytest.fixture(scope="module")
def app_client():
    """
    Module-scoped TestClient so app startup only runs once for this file.
    DEV_MODE=true means no real DB or OIDC needed.
    """
    from fastapi.testclient import TestClient
    from app.database import Base, get_db
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    from main import app
    from shared.config import _clear_cache_for_tests

    app.dependency_overrides[get_db] = override_get_db

    # SERVICE_API_KEY is read at call time via get_config() (xander:40 refactor).
    # os.environ is set at module level (line 25); clear the lru_cache so
    # get_config() picks up that value for every request in this test session.
    _clear_cache_for_tests()

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

    app.dependency_overrides.clear()


class TestRemovedEndpoints:
    """Endpoints that were dead code should no longer exist."""

    def test_services_endpoint_removed(self, app_client):
        """/services was dead code leaking internal topology — must return 404."""
        r = app_client.get("/services")
        assert r.status_code == 404, f"Expected 404 for removed /services, got {r.status_code}"

    def test_test_query_endpoint_removed(self, app_client):
        """/test-query was an unauthenticated LLM proxy — must return 404.

        Note: GET is used because StaticFiles middleware intercepts POST to unknown
        paths and returns 405; GET to a non-existent route correctly returns 404.
        """
        r = app_client.get("/test-query")
        assert r.status_code == 404, f"Expected 404 for removed /test-query, got {r.status_code}"

    def test_service_control_public_removed(self, app_client):
        """/api/service-control/public leaked service details without auth — must be gone."""
        r = app_client.get("/api/service-control/public")
        assert r.status_code == 404, f"Expected 404 for removed /api/service-control/public, got {r.status_code}"


class TestHealthEndpointOpen:
    """k8s liveness probe must stay unauthenticated."""

    def test_health_returns_200_no_auth(self, app_client):
        """/health must be open so k8s probes don't need a token."""
        r = app_client.get("/health")
        assert r.status_code == 200


class TestStatusEndpointProtected:
    """
    /status used to be unauthenticated — it now requires auth.
    In DEV_MODE get_current_user returns the dev-admin, so these
    should succeed (200) rather than 401, confirming the dependency
    is wired without breaking DEV_MODE.
    """

    def test_status_requires_auth_wired(self, app_client):
        """/status must have get_current_user dependency (returns 200 in DEV_MODE)."""
        r = app_client.get("/status")
        # In DEV_MODE the dep always succeeds, so 200 confirms the dep is wired
        assert r.status_code == 200

    def test_api_status_requires_auth_wired(self, app_client):
        """/api/status must have get_current_user dependency."""
        r = app_client.get("/api/status")
        assert r.status_code == 200


class TestInternalEndpointsProtected:
    """All /api/internal/* routes must require X-Service-Key."""

    def test_internal_config_without_key_returns_422(self, app_client):
        """Missing X-Service-Key header → 422 (required header absent)."""
        r = app_client.get("/api/internal/config/conversation")
        assert r.status_code == 422, (
            f"Expected 422 (missing required header) for /api/internal/config/conversation "
            f"without X-Service-Key, got {r.status_code}"
        )

    def test_internal_config_wrong_key_returns_401(self, app_client):
        """Wrong X-Service-Key → 401."""
        r = app_client.get(
            "/api/internal/config/conversation",
            headers={"X-Service-Key": "definitely-wrong-key"},
        )
        assert r.status_code == 401, (
            f"Expected 401 for wrong X-Service-Key on /api/internal/*, got {r.status_code}"
        )

    def test_internal_analytics_post_without_key_returns_422(self, app_client):
        """POST /api/internal/analytics/log also requires service key."""
        r = app_client.post(
            "/api/internal/analytics/log",
            json={"event": "test"},
        )
        assert r.status_code == 422

    def test_internal_service_usage_post_without_key_returns_422(self, app_client):
        """POST /api/internal/service-usage/{name}/increment requires service key."""
        r = app_client.post("/api/internal/service-usage/weather/increment")
        assert r.status_code == 422


class TestExternalApiKeyPublicEndpointsProtected:
    """Public key-retrieval routes must also require X-Service-Key."""

    def test_public_key_route_without_key_returns_422(self, app_client):
        """/api/external-api-keys/public/{name}/key requires X-Service-Key."""
        r = app_client.get("/api/external-api-keys/public/google-places/key")
        assert r.status_code == 422, (
            f"Expected 422 (missing required service header), got {r.status_code}"
        )

    def test_public_key_route_wrong_key_returns_401(self, app_client):
        """Wrong X-Service-Key on public key route → 401."""
        r = app_client.get(
            "/api/external-api-keys/public/google-places/key",
            headers={"X-Service-Key": "bad-key"},
        )
        assert r.status_code == 401

    def test_public_credentials_route_without_key_returns_422(self, app_client):
        """/api/external-api-keys/public/{name}/credentials requires X-Service-Key."""
        r = app_client.get("/api/external-api-keys/public/google-places/credentials")
        assert r.status_code == 422

    def test_public_credentials_route_wrong_key_returns_401(self, app_client):
        """Wrong X-Service-Key on credentials route → 401."""
        r = app_client.get(
            "/api/external-api-keys/public/google-places/credentials",
            headers={"X-Service-Key": "bad-key"},
        )
        assert r.status_code == 401


class TestCORSConfiguration:
    """CORS should be driven by env var, not hardcoded '*'."""

    def test_cors_middleware_present(self, app_client):
        """CORS middleware must be registered (preflight OPTIONS should respond)."""
        r = app_client.options(
            "/health",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "GET",
            },
        )
        # 200 or 204 means CORS middleware is handling the preflight
        assert r.status_code in (200, 204)

    def test_cors_origin_env_var_used(self):
        """CORS_ALLOWED_ORIGINS env var must be respected (code review)."""
        import inspect
        import sys
        # Re-read the source file directly to avoid cached module state
        main_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
        )
        with open(main_path) as f:
            source = f.read()

        assert "CORS_ALLOWED_ORIGINS" in source, "main.py must read CORS_ALLOWED_ORIGINS env var"
        assert 'allow_origins=["*"]' not in source, (
            "main.py must NOT use hardcoded allow_origins=['*']"
        )

    def test_cors_defaults_to_localhost_not_wildcard(self):
        """When CORS_ALLOWED_ORIGINS is unset, default must be localhost, not '*'."""
        main_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
        )
        with open(main_path) as f:
            source = f.read()

        # The default fallback in the code should be localhost, not '*'
        assert "localhost" in source
        # Confirm the wildcard is gone
        assert 'allow_origins=["*"]' not in source


class TestCookieHTTPSOnlyConfiguration:
    """Session cookie https_only must be False in DEV_MODE and True in production."""

    def test_cookie_https_only_tied_to_dev_mode(self):
        """cookie_https_only must be `not DEV_MODE`, not a hardcoded False."""
        main_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
        )
        with open(main_path) as f:
            source = f.read()

        assert "cookie_https_only=not DEV_MODE" in source, (
            "cookie_https_only must be `not DEV_MODE` so it's True in production"
        )
        assert "cookie_https_only=False" not in source, (
            "cookie_https_only must NOT be hardcoded False"
        )


class TestStartupSecretValidation:
    """Production startup must reject insecure default secrets."""

    def test_insecure_defaults_checked_in_startup(self):
        """main.py startup must check for insecure default values."""
        main_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
        )
        with open(main_path) as f:
            source = f.read()

        assert "dev-service-key-change-in-production" in source, (
            "startup_event must validate SERVICE_API_KEY against insecure default"
        )
        assert "dev-secret-change-in-production" in source, (
            "startup_event must validate SESSION_SECRET_KEY and JWT_SECRET against insecure defaults"
        )
        assert "SystemExit" in source or "raise SystemExit" in source, (
            "startup_event must raise SystemExit (or equivalent) on insecure defaults in production"
        )

    def test_startup_validation_only_in_production_branch(self):
        """Secret validation must be inside the `else` (non-DEV_MODE) branch."""
        main_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
        )
        with open(main_path) as f:
            source = f.read()

        # The insecure default check must come after the `else:` that handles non-DEV_MODE
        else_pos = source.find("else:\n")
        fatal_pos = source.find("FATAL:")
        assert else_pos != -1 and fatal_pos != -1, (
            "Both 'else:' branch and 'FATAL:' message must exist in main.py"
        )
        assert else_pos < fatal_pos, (
            "FATAL secret validation must appear inside the production (non-DEV_MODE) else branch"
        )


class TestRagBypassPublicEndpointsProtected:
    """RAG bypass public routes must require X-Service-Key (xander:11)."""

    def test_bypass_config_without_key_returns_422(self, app_client):
        """/api/rag-service-bypass/public/{name}/config requires X-Service-Key."""
        r = app_client.get("/api/rag-service-bypass/public/weather/config")
        assert r.status_code == 422, (
            f"Expected 422 (missing required service header) for bypass config, got {r.status_code}"
        )

    def test_bypass_config_wrong_key_returns_401(self, app_client):
        """Wrong X-Service-Key on bypass config route → 401."""
        r = app_client.get(
            "/api/rag-service-bypass/public/weather/config",
            headers={"X-Service-Key": "bad-key"},
        )
        assert r.status_code == 401

    def test_bypass_enabled_without_key_returns_422(self, app_client):
        """/api/rag-service-bypass/public/enabled requires X-Service-Key."""
        r = app_client.get("/api/rag-service-bypass/public/enabled")
        assert r.status_code == 422, (
            f"Expected 422 (missing required service header) for bypass enabled, got {r.status_code}"
        )

    def test_bypass_enabled_wrong_key_returns_401(self, app_client):
        """Wrong X-Service-Key on bypass enabled route → 401."""
        r = app_client.get(
            "/api/rag-service-bypass/public/enabled",
            headers={"X-Service-Key": "bad-key"},
        )
        assert r.status_code == 401

    # --- Positive cases: correct key → 200 (already-gated endpoints) ---

    def test_bypass_config_correct_key_accepted(self, app_client):
        """Correct X-Service-Key on bypass config route → 200 with sane response."""
        r = app_client.get(
            "/api/rag-service-bypass/public/weather/config",
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert r.status_code == 200
        data = r.json()
        assert "bypass_enabled" in data

    def test_bypass_enabled_correct_key_accepted(self, app_client):
        """Correct X-Service-Key on bypass enabled route → 200 with list response."""
        r = app_client.get(
            "/api/rag-service-bypass/public/enabled",
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    # --- Regression guards: 3 new endpoints gated in Phase 1 (xander:34/35/36) ---

    def test_cloud_providers_enabled_without_key_returns_422(self, app_client):
        """/api/cloud-providers/public/enabled requires X-Service-Key (xander:34)."""
        r = app_client.get("/api/cloud-providers/public/enabled")
        assert r.status_code == 422, (
            f"Expected 422 (missing required service header) for cloud-providers/public/enabled, "
            f"got {r.status_code}"
        )

    def test_cloud_providers_enabled_wrong_key_returns_401(self, app_client):
        """Wrong X-Service-Key on cloud-providers/public/enabled → 401 (xander:34)."""
        r = app_client.get(
            "/api/cloud-providers/public/enabled",
            headers={"X-Service-Key": "bad-key"},
        )
        assert r.status_code == 401

    def test_cloud_providers_enabled_correct_key_accepted(self, app_client):
        """Correct X-Service-Key on cloud-providers/public/enabled → 200 (xander:34)."""
        r = app_client.get(
            "/api/cloud-providers/public/enabled",
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_cloud_providers_config_without_key_returns_422(self, app_client):
        """/api/cloud-providers/public/{provider}/config requires X-Service-Key (xander:35)."""
        r = app_client.get("/api/cloud-providers/public/openai/config")
        assert r.status_code == 422, (
            f"Expected 422 for cloud-providers/public/openai/config, got {r.status_code}"
        )

    def test_cloud_providers_config_wrong_key_returns_401(self, app_client):
        """Wrong X-Service-Key on cloud-providers/public/{provider}/config → 401 (xander:35)."""
        r = app_client.get(
            "/api/cloud-providers/public/openai/config",
            headers={"X-Service-Key": "bad-key"},
        )
        assert r.status_code == 401

    def test_cloud_providers_config_correct_key_accepted(self, app_client):
        """Correct X-Service-Key on cloud-providers/public/{provider}/config → 200 (xander:35)."""
        r = app_client.get(
            "/api/cloud-providers/public/openai/config",
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert r.status_code == 200
        data = r.json()
        # Provider not configured in test DB → {"enabled": False}
        assert "enabled" in data

    def test_site_scraper_config_public_without_key_returns_422(self, app_client):
        """/api/site-scraper/config/public requires X-Service-Key (xander:36)."""
        r = app_client.get("/api/site-scraper/config/public")
        assert r.status_code == 422, (
            f"Expected 422 for site-scraper/config/public, got {r.status_code}"
        )

    def test_site_scraper_config_public_wrong_key_returns_401(self, app_client):
        """Wrong X-Service-Key on site-scraper/config/public → 401 (xander:36)."""
        r = app_client.get(
            "/api/site-scraper/config/public",
            headers={"X-Service-Key": "bad-key"},
        )
        assert r.status_code == 401

    def test_site_scraper_config_public_correct_key_accepted(self, app_client):
        """Correct X-Service-Key on site-scraper/config/public → 200 (xander:36)."""
        r = app_client.get(
            "/api/site-scraper/config/public",
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert r.status_code == 200
        data = r.json()
        assert "allowed_domains" in data

    def test_tool_calling_tools_public_without_key_returns_422(self, app_client):
        """/api/tool-calling/tools/public requires X-Service-Key."""
        r = app_client.get("/api/tool-calling/tools/public")
        assert r.status_code == 422, (
            f"Expected 422 for tool-calling/tools/public, got {r.status_code}"
        )

    def test_tool_calling_tools_public_wrong_key_returns_401(self, app_client):
        """Wrong X-Service-Key on tool-calling/tools/public → 401."""
        r = app_client.get(
            "/api/tool-calling/tools/public",
            headers={"X-Service-Key": "bad-key"},
        )
        assert r.status_code == 401

    def test_tool_calling_tools_public_correct_key_accepted(self, app_client):
        """Correct X-Service-Key on tool-calling/tools/public → 200 with list response."""
        r = app_client.get(
            "/api/tool-calling/tools/public",
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert r.status_code == 200
        assert isinstance(r.json(), list)


class TestToolCallingMetricsRecordProtected:
    """POST /api/tool-calling/metrics/record must require X-Service-Key (xander:41)."""

    def test_metrics_record_without_key_returns_422(self, app_client):
        """Missing X-Service-Key → 422 (required header absent)."""
        r = app_client.post(
            "/api/tool-calling/metrics/record",
            json={
                "tool_name": "weather",
                "success": True,
                "latency_ms": 100,
            },
        )
        assert r.status_code == 422, (
            f"Expected 422 (missing required service header) for /api/tool-calling/metrics/record, "
            f"got {r.status_code}"
        )

    def test_metrics_record_wrong_key_returns_401(self, app_client):
        """Wrong X-Service-Key → 401."""
        r = app_client.post(
            "/api/tool-calling/metrics/record",
            json={
                "tool_name": "weather",
                "success": True,
                "latency_ms": 100,
            },
            headers={"X-Service-Key": "bad-key"},
        )
        assert r.status_code == 401, (
            f"Expected 401 for wrong X-Service-Key on /api/tool-calling/metrics/record, "
            f"got {r.status_code}"
        )

    def test_metrics_record_correct_key_accepted(self, app_client):
        """Correct X-Service-Key → 200 with metric_id in response."""
        r = app_client.post(
            "/api/tool-calling/metrics/record",
            json={
                "tool_name": "weather",
                "success": True,
                "latency_ms": 100,
            },
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert r.status_code == 200, (
            f"Expected 200 for correct X-Service-Key on /api/tool-calling/metrics/record, "
            f"got {r.status_code}"
        )
        data = r.json()
        assert data.get("success") is True
        assert "metric_id" in data


class TestModelDownloadProgressOpen:
    """
    Model download progress callback is intentionally open (no service key required).

    Auth on this endpoint is deferred because the Control Agent runs out-of-cluster
    on the Ollama host and distributing SERVICE_API_KEY there was descoped (xander:2).
    When xander:2 is resolved, this class should be replaced with
    TestModelDownloadProgressProtected (422/401 assertions).
    """

    def test_progress_callback_without_key_not_rejected_by_auth(self, app_client):
        """/api/model-downloads/internal/{id}/progress must NOT return 422/401 for missing key."""
        r = app_client.post(
            "/api/model-downloads/internal/1/progress",
            json={"status": "downloading", "progress_percent": 50},
        )
        # 404 (download not found in test DB) or 200 is acceptable; 422/401 is not
        assert r.status_code not in (422, 401), (
            f"Progress callback endpoint must not require X-Service-Key (auth deferred, xander:2); "
            f"got {r.status_code}"
        )


class TestServiceToggleProtected:
    """Service toggle endpoint must use verify_service_api_key, not inline check (librarian:5)."""

    def test_service_toggle_without_key_returns_422(self, app_client):
        """/api/features/service/{id}/toggle requires X-Service-Key."""
        r = app_client.put("/api/features/service/1/toggle")
        assert r.status_code == 422, (
            f"Expected 422 (missing required service header) for service toggle, got {r.status_code}"
        )

    def test_service_toggle_wrong_key_returns_401(self, app_client):
        """Wrong X-Service-Key on service toggle → 401."""
        r = app_client.put(
            "/api/features/service/1/toggle",
            headers={"X-Service-Key": "bad-key"},
        )
        assert r.status_code == 401

    def test_service_toggle_no_longer_accepts_x_api_key(self, app_client):
        """X-API-Key header must no longer be accepted on the service toggle endpoint.

        The old inline check used X-API-Key; the new dependency uses X-Service-Key.
        Sending X-API-Key (without X-Service-Key) must return 422, not 401/200.
        """
        r = app_client.put(
            "/api/features/service/1/toggle",
            headers={"X-API-Key": "some-key"},
        )
        assert r.status_code == 422, (
            f"Expected 422 (X-API-Key no longer accepted), got {r.status_code}"
        )


class TestPhase2InsecureDefaults:
    """Phase 2 — _INSECURE_DEFAULTS consolidation: xander:13, codex-M2, HIGH-A, codex-M4.

    These tests drive startup_event() via subprocess (same pattern as Phase 1) so
    pydantic-settings whitespace normalization and module-level import-time captures
    are handled correctly.  A prod_secrets helper provides valid values for all
    SECRET-kind keys so each test isolates exactly the variable under examination.
    """

    # Valid non-placeholder values for all SECRET-kind keys. Any test that sets a
    # specific key to a bad value must also supply valid values for the other two so
    # the loop reaches the key under test before bailing on an earlier one.
    _PROD_SECRETS: dict = {
        "SESSION_SECRET_KEY": "a" * 64,
        "JWT_SECRET": "b" * 64,
        "SERVICE_API_KEY": "c" * 64,
    }

    def _run_startup_subprocess(self, env_overrides: dict) -> "subprocess.CompletedProcess":
        """Run startup_event() in a subprocess with env_overrides applied on top of os.environ."""
        import subprocess
        import sys

        env = os.environ.copy()

        _repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..")
        )
        _src_path = os.path.join(_repo_root, "src")
        _backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [_src_path, _backend_path, existing_pythonpath])
        )

        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v

        script = (
            "from fastapi.testclient import TestClient; "
            "from main import app; "
            "TestClient(app).__enter__()"
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=_backend_path,
        )

    def _prod_env(self, overrides: dict | None = None) -> dict:
        """Return a base production env (valid secrets, DEV_MODE=false, valid OIDC vars)
        with optional per-test overrides applied on top."""
        base = {
            "DEV_MODE": "false",
            "OIDC_ISSUER": "https://idp.example.com/",
            "OIDC_CLIENT_ID": "real-client-id-for-testing",
            **self._PROD_SECRETS,
        }
        if overrides:
            base.update(overrides)
        return base

    # ------------------------------------------------------------------
    # xander:13 — OIDC_CLIENT_ID="demo-mode" must abort production startup
    # ------------------------------------------------------------------

    def test_phase2_demo_mode_oidc_client_id_exits(self):
        """xander:13 — OIDC_CLIENT_ID=demo-mode in production must SystemExit."""
        result = self._run_startup_subprocess(
            self._prod_env({"OIDC_CLIENT_ID": "demo-mode"})
        )
        assert result.returncode != 0, (
            f"Startup must exit non-zero for OIDC_CLIENT_ID=demo-mode; "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "OIDC_CLIENT_ID" in combined, (
            f"Error output must mention OIDC_CLIENT_ID; got: {combined!r}"
        )
        assert "demo-mode" in combined, (
            f"Error output must name the offending placeholder; got: {combined!r}"
        )

    # ------------------------------------------------------------------
    # HIGH-A / xander:1 — whitespace bypass is closed (get_config() strips whitespace)
    # xander:20 — leading, trailing, and both-sides variants all blocked
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("client_id,description", [
        (" demo-mode", "leading space"),
        ("demo-mode ", "trailing space"),
        (" demo-mode ", "leading and trailing space"),
    ])
    def test_phase2_whitespace_bypass_blocked(self, client_id: str, description: str):
        """HIGH-A / xander:1 / xander:20 — whitespace-padded 'demo-mode' still triggers.

        os.getenv would return the padded string and the equality check would pass.
        _read_var() reads through get_config().oidc_client_id which applies pydantic's
        _strip_oidc_whitespace validator, returning 'demo-mode', so the gate fires.
        Tests leading, trailing, and both-sides whitespace variants (xander:20).
        """
        result = self._run_startup_subprocess(
            self._prod_env({"OIDC_CLIENT_ID": client_id})
        )
        assert result.returncode != 0, (
            f"Whitespace-bypass ({description}) must be blocked; "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "OIDC_CLIENT_ID" in combined, (
            f"Error must mention OIDC_CLIENT_ID ({description}); got: {combined!r}"
        )

    # ------------------------------------------------------------------
    # xander:19 — whitespace bypass for CONFIGURE_ME placeholder also blocked
    # ------------------------------------------------------------------

    def test_phase2_configure_me_whitespace_bypass_blocked(self):
        """xander:19 — OIDC_CLIENT_ID=' CONFIGURE_ME_OIDC_CLIENT_ID' (leading space) triggers.

        Same whitespace-normalization invariant as test_phase2_whitespace_bypass_blocked
        but for the CONFIGURE_ME placeholder. pydantic's _strip_oidc_whitespace strips
        the leading space so the exact-match gate fires.
        """
        result = self._run_startup_subprocess(
            self._prod_env({"OIDC_CLIENT_ID": " CONFIGURE_ME_OIDC_CLIENT_ID"})  # leading space
        )
        assert result.returncode != 0, (
            f"Whitespace-bypass for CONFIGURE_ME placeholder must be blocked; "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "OIDC_CLIENT_ID" in combined, (
            f"Error must mention OIDC_CLIENT_ID for CONFIGURE_ME whitespace bypass; "
            f"got: {combined!r}"
        )

    # ------------------------------------------------------------------
    # codex-M2 — CONFIGURE_ME_OIDC_CLIENT_ID placeholder must abort production startup
    # ------------------------------------------------------------------

    def test_phase2_configure_me_placeholder_exits(self):
        """codex-M2 — OIDC_CLIENT_ID=CONFIGURE_ME_OIDC_CLIENT_ID must SystemExit.

        scripts/create-secrets.sh writes this placeholder when OIDC_CLIENT_ID was
        absent at deploy time; the backend must honor the script's rejection contract.
        """
        result = self._run_startup_subprocess(
            self._prod_env({"OIDC_CLIENT_ID": "CONFIGURE_ME_OIDC_CLIENT_ID"})
        )
        assert result.returncode != 0, (
            f"Startup must exit for CONFIGURE_ME_OIDC_CLIENT_ID placeholder; "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "CONFIGURE_ME_OIDC_CLIENT_ID" in combined, (
            f"Error output must name the placeholder; got: {combined!r}"
        )
        # xander:18 / ian:2 — display-var mapping must show "OIDC_CLIENT_ID", not the
        # synthetic dict key "_OIDC_CLIENT_ID_CONFIGURE_ME". Pin this invariant here so
        # a regression in _display_var is caught immediately.
        assert "OIDC_CLIENT_ID" in combined, (
            f"Error output must show 'OIDC_CLIENT_ID' (synthetic key must be mapped back "
            f"to real env var name via _display_var); got: {combined!r}"
        )

    # ------------------------------------------------------------------
    # Regression: SESSION_SECRET_KEY bad placeholder still exits (Phase 1 regression check)
    # ------------------------------------------------------------------

    def test_phase2_session_secret_key_placeholder_exits(self):
        """Regression — SESSION_SECRET_KEY=dev-secret-change-in-production still aborts."""
        result = self._run_startup_subprocess(
            self._prod_env({"SESSION_SECRET_KEY": "dev-secret-change-in-production"})
        )
        assert result.returncode != 0, (
            f"SESSION_SECRET_KEY placeholder must still abort startup; "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "SESSION_SECRET_KEY" in combined, (
            f"Error must mention SESSION_SECRET_KEY; got: {combined!r}"
        )

    # ------------------------------------------------------------------
    # codex-M4 — secret-kind error message must NOT say "IdP"; must say "openssl rand"
    # ------------------------------------------------------------------

    def test_phase2_secret_message_is_kind_appropriate(self):
        """codex-M4 — SESSION_SECRET_KEY error must mention 'openssl rand', not 'IdP'."""
        result = self._run_startup_subprocess(
            self._prod_env({"SESSION_SECRET_KEY": "dev-secret-change-in-production"})
        )
        combined = result.stdout + result.stderr
        assert "openssl rand" in combined, (
            f"Secret-kind message must reference openssl rand; got: {combined!r}"
        )
        assert "IdP" not in combined, (
            f"Secret-kind message must NOT reference IdP; got: {combined!r}"
        )

    # ------------------------------------------------------------------
    # DEV_MODE bypass: OIDC_CLIENT_ID=demo-mode + DEV_MODE=true must NOT exit
    # ------------------------------------------------------------------

    def test_phase2_dev_mode_bypasses_oidc_client_id_gate(self):
        """DEV_MODE=true skips the production gate — demo-mode client id is allowed."""
        import subprocess
        import sys

        env = os.environ.copy()
        _repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..")
        )
        _src_path = os.path.join(_repo_root, "src")
        _backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [_src_path, _backend_path, existing_pythonpath])
        )
        env["DEV_MODE"] = "true"
        env["OIDC_CLIENT_ID"] = "demo-mode"
        env["DATABASE_URL"] = "sqlite:///:memory:"
        # Remove any non-SQLite DATABASE_URL that might trigger the xander:6 gate
        for k in list(env):
            if k == "DATABASE_URL" and not env[k].startswith("sqlite"):
                env[k] = "sqlite:///:memory:"

        script = (
            "from fastapi.testclient import TestClient; "
            "from main import app; "
            "TestClient(app).__enter__()"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=_backend_path,
        )
        assert result.returncode == 0, (
            f"DEV_MODE=true must bypass the OIDC_CLIENT_ID=demo-mode production gate; "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )

    # ------------------------------------------------------------------
    # xander:16 — DEMO_MODE=true in production must abort startup
    # ------------------------------------------------------------------

    def test_phase2_demo_mode_in_production_exits(self):
        """xander:16 — DEMO_MODE=true + DEV_MODE=false must SystemExit.

        DEMO_MODE activates the demo-admin bypass at main.py:474, issuing an
        unauthenticated owner JWT for admin@demo.local without OIDC. This standalone
        gate catches the privilege-escalation path that the _INSECURE_DEFAULTS loop
        (which handles placeholder strings) does not cover.
        """
        result = self._run_startup_subprocess(
            self._prod_env({"DEMO_MODE": "true"})
        )
        assert result.returncode != 0, (
            f"Startup must exit non-zero for DEMO_MODE=true in production; "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "DEMO_MODE" in combined, (
            f"Error output must mention DEMO_MODE; got: {combined!r}"
        )
        assert "FATAL" in combined, (
            f"Error output must contain 'FATAL'; got: {combined!r}"
        )

    def test_phase2_demo_mode_in_dev_no_fire(self):
        """xander:16 — DEMO_MODE=true + DEV_MODE=true must NOT fire the production gate.

        The xander:16 check lives in the production `else` branch; DEV_MODE=true takes
        the early-return path and never reaches it. Confirm no spurious exit.
        """
        # Use in-process run (same pattern as test_phase1_dev_mode_sqlite_continues_normally)
        # because we're verifying the app reaches a healthy started state, not subprocess exit.
        import sys
        from shared.config import get_config

        env_overrides = {
            "DEV_MODE": "true",
            "DEMO_MODE": "true",
            "DATABASE_URL": "sqlite:///:memory:",
        }
        saved = {}
        for k, v in env_overrides.items():
            saved[k] = os.environ.get(k)
            os.environ[k] = v

        try:
            get_config.cache_clear()
            _modules_to_evict = [
                name for name in sys.modules
                if name == "main" or name.startswith(("main.", "app.", "shared."))
            ]
            for name in _modules_to_evict:
                del sys.modules[name]

            import main as _main
            from fastapi.testclient import TestClient

            with TestClient(_main.app, raise_server_exceptions=True):
                pass  # Reaches here means no SystemExit in the production branch
        finally:
            for k, orig_v in saved.items():
                if orig_v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = orig_v
            get_config.cache_clear()

    # ------------------------------------------------------------------
    # xander:17 — empty OIDC_CLIENT_ID must abort startup
    # ------------------------------------------------------------------

    def test_phase2_empty_oidc_client_id_exits(self):
        """xander:17 — empty OIDC_CLIENT_ID + DEV_MODE=false must SystemExit.

        Empty string passes the _INSECURE_DEFAULTS loop (exact-match only for
        client_id kinds) but lands at oauth.register(client_id=""), which permissive
        IdPs may accept — confused-deputy risk. The standalone xander:17 gate catches
        this before configure_oauth_client() is reached.
        """
        result = self._run_startup_subprocess(
            self._prod_env({"OIDC_CLIENT_ID": ""})
        )
        assert result.returncode != 0, (
            f"Startup must exit non-zero for empty OIDC_CLIENT_ID; "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "OIDC_CLIENT_ID" in combined, (
            f"Error output must mention OIDC_CLIENT_ID; got: {combined!r}"
        )
        assert "empty" in combined.lower(), (
            f"Error output must indicate OIDC_CLIENT_ID is empty; got: {combined!r}"
        )

    # ------------------------------------------------------------------
    # LOW-C / tessa:6,7 — boundary: case variants and prefix forms must NOT trigger
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("client_id", [
        "Demo-Mode",
        "DEMO-MODE",
        "demo-mode-extended",
        "x-demo-mode",
    ])
    def test_phase2_boundary_case_variants_no_false_positive(self, client_id: str):
        """LOW-C / tessa:6 — case variations and prefix forms of 'demo-mode' must NOT trigger."""
        result = self._run_startup_subprocess(
            self._prod_env({"OIDC_CLIENT_ID": client_id})
        )
        # The process will still exit non-zero (production startup has other gates that
        # fire for a test environment), but the OIDC_CLIENT_ID gate must NOT be the cause.
        combined = result.stdout + result.stderr
        assert "publicly-known placeholder" not in combined, (
            f"OIDC_CLIENT_ID={client_id!r} must NOT trigger the insecure-placeholder gate "
            f"(exact match only); got: {combined!r}"
        )
        assert "placeholder written by" not in combined, (
            f"OIDC_CLIENT_ID={client_id!r} must NOT match configure-me gate; got: {combined!r}"
        )


class TestTwilioSignatureValidation:
    """SMS webhook must validate Twilio signatures when token is configured."""

    def test_webhook_dependency_present(self):
        """validate_twilio_signature dep must be on handle_incoming_sms."""
        import inspect
        from app.routes.sms_webhook import handle_incoming_sms, validate_twilio_signature

        # Check the source of the route function references the dependency
        source = inspect.getsource(handle_incoming_sms)
        assert "validate_twilio_signature" in source, (
            "handle_incoming_sms must declare Depends(validate_twilio_signature)"
        )

    def test_validator_skips_when_no_token_configured(self):
        """When TWILIO_AUTH_TOKEN is empty, validator should pass through."""
        import asyncio
        import app.routes.sms_webhook as sw

        original = sw.TWILIO_AUTH_TOKEN
        try:
            sw.TWILIO_AUTH_TOKEN = ""

            from unittest.mock import MagicMock
            mock_request = MagicMock()
            mock_request.headers = {}

            # Should not raise — graceful skip when token is not configured
            asyncio.get_event_loop().run_until_complete(
                sw.validate_twilio_signature(mock_request)
            )
        finally:
            sw.TWILIO_AUTH_TOKEN = original

    def test_validator_rejects_missing_signature_when_token_set(self):
        """When TWILIO_AUTH_TOKEN is set, missing X-Twilio-Signature → 403."""
        import asyncio
        from fastapi import HTTPException
        import app.routes.sms_webhook as sw

        original = sw.TWILIO_AUTH_TOKEN
        try:
            sw.TWILIO_AUTH_TOKEN = "test-auth-token"

            from unittest.mock import MagicMock, AsyncMock
            mock_request = MagicMock()
            mock_request.headers = {}  # No X-Twilio-Signature
            mock_request.body = AsyncMock(return_value=b"From=%2B15551234567&Body=hi")
            mock_request.url = "https://example.com/api/sms/webhook/incoming"

            with pytest.raises(HTTPException) as exc_info:
                asyncio.get_event_loop().run_until_complete(
                    sw.validate_twilio_signature(mock_request)
                )
            assert exc_info.value.status_code == 403
        finally:
            sw.TWILIO_AUTH_TOKEN = original

    def test_validator_rejects_invalid_signature(self):
        """When TWILIO_AUTH_TOKEN is set, bad X-Twilio-Signature → 403."""
        import asyncio
        from fastapi import HTTPException
        import app.routes.sms_webhook as sw

        original = sw.TWILIO_AUTH_TOKEN
        try:
            sw.TWILIO_AUTH_TOKEN = "test-auth-token-xyz"

            from unittest.mock import MagicMock, AsyncMock
            mock_request = MagicMock()
            mock_request.headers = {"X-Twilio-Signature": "totallywrongsignature"}
            mock_request.body = AsyncMock(return_value=b"From=%2B15551234567&Body=hi")
            mock_request.url = "https://example.com/api/sms/webhook/incoming"

            with pytest.raises(HTTPException) as exc_info:
                asyncio.get_event_loop().run_until_complete(
                    sw.validate_twilio_signature(mock_request)
                )
            assert exc_info.value.status_code == 403
        finally:
            sw.TWILIO_AUTH_TOKEN = original


# -------------------------------------------------------------------
# Phase 1 — xander:6 — DEV_MODE + real DATABASE_URL fail-fast gate
# -------------------------------------------------------------------
# The misconfig test uses subprocess.run() to drive startup_event() because
# TestClient on Python 3.14 + anyio wraps SystemExit in a BaseExceptionGroup
# that is logged as an asyncio shutdown error and re-surfaces as CancelledError —
# losing the original message in the test-visible exception chain (codex-H3).
# subprocess.run() gives clean isolation: env vars are exact, process exit code
# is authoritative, and stderr contains the FATAL message verbatim.
#
# The "no abort" cases (sqlite and unset DATABASE_URL) still use TestClient
# because they must verify the app reaches a healthy started state, not just
# that it didn't print to stderr.
#
# get_config.cache_clear() is required between cases because pydantic-settings
# BaseSettings is wrapped with @lru_cache; without it, env-var mutations are
# invisible to subsequent get_config() calls in the same process.


class TestPhase1DevModePostgresGate:
    """xander:6: DEV_MODE=true + non-SQLite DATABASE_URL must abort startup."""

    def _run_startup_subprocess(self, env_overrides: dict) -> "subprocess.CompletedProcess":
        """Run startup_event() in a subprocess with env_overrides applied on top of os.environ."""
        import subprocess
        import sys

        env = os.environ.copy()

        # Ensure `shared` package is resolvable in the subprocess just as conftest.py does it.
        # __file__ is admin/backend/tests/test_security_hardening.py; three levels up is repo root.
        _repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..")
        )
        _src_path = os.path.join(_repo_root, "src")
        _backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [_src_path, _backend_path, existing_pythonpath])
        )

        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v

        # Drive startup via TestClient inside the subprocess — this triggers the lifespan.
        # The subprocess exits with code 1 when startup raises SystemExit.
        script = (
            "from fastapi.testclient import TestClient; "
            "from main import app; "
            "TestClient(app).__enter__()"
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=_backend_path,
        )

    def _run_startup_in_process(self, env_overrides: dict) -> None:
        """
        Run startup_event() inside the current process using TestClient.
        Used for the "no abort" cases where we need to verify the app starts cleanly.
        get_config.cache_clear() is called before/after to isolate pydantic-settings cache.
        """
        import sys
        from shared.config import get_config

        saved = {}
        for k, v in env_overrides.items():
            saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

        try:
            get_config.cache_clear()

            # Re-import main (and all app/shared modules) so module-level captures
            # like `DEV_MODE = get_config().dev_mode` in app/database.py reflect
            # the updated env rather than a stale import-time value (xander:14).
            _modules_to_evict = [
                name for name in sys.modules
                if name == "main" or name.startswith(("main.", "app.", "shared."))
            ]
            for name in _modules_to_evict:
                del sys.modules[name]

            import main as _main
            from fastapi.testclient import TestClient

            with TestClient(_main.app, raise_server_exceptions=True):
                pass
        finally:
            for k, orig_v in saved.items():
                if orig_v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = orig_v
            get_config.cache_clear()

    def test_phase1_dev_mode_postgres_raises_system_exit(self):
        """DEV_MODE=true + postgresql DATABASE_URL must abort startup (xander:6)."""
        result = self._run_startup_subprocess({
            "DEV_MODE": "true",
            "DATABASE_URL": "postgresql://user:pass@localhost/testdb",
        })
        assert result.returncode != 0, (
            "Startup must exit non-zero when DEV_MODE=true and DATABASE_URL is non-SQLite; "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "FATAL" in combined, (
            f"Startup output must contain 'FATAL' for xander:6 misconfig; got: {combined!r}"
        )
        assert "DEV_MODE" in combined, (
            f"Startup output must mention 'DEV_MODE'; got: {combined!r}"
        )

    def test_phase1_dev_mode_sqlite_continues_normally(self):
        """DEV_MODE=true + sqlite DATABASE_URL must NOT abort startup."""
        self._run_startup_in_process({
            "DEV_MODE": "true",
            "DATABASE_URL": "sqlite:///:memory:",
        })

    def test_phase1_dev_mode_no_db_url_continues_normally(self):
        """DEV_MODE=true + unset DATABASE_URL must NOT abort startup (in-memory SQLite fallback)."""
        self._run_startup_in_process({
            "DEV_MODE": "true",
            "DATABASE_URL": None,  # None → pop from env
        })

    def test_phase1_production_postgres_no_fire(self):
        """DEV_MODE=false + postgresql DATABASE_URL must NOT fire the DEV_MODE gate.

        Production startup fails on its own terms (insecure default secrets) — the
        xander:6 gate message must not appear in the output.
        """
        result = self._run_startup_subprocess({
            "DEV_MODE": "false",
            "DATABASE_URL": "postgresql://user:pass@localhost/testdb",
            # Insecure defaults so production startup exits predictably on its own check
            "SESSION_SECRET_KEY": "dev-secret-change-in-production",
            "JWT_SECRET": "dev-secret-change-in-production",
            "SERVICE_API_KEY": "dev-service-key-change-in-production",
        })
        combined = result.stdout + result.stderr
        assert "DEV_MODE=true is incompatible" not in combined, (
            "xander:6 DEV_MODE gate must NOT fire when DEV_MODE=false; "
            f"got output: {combined!r}"
        )


# -------------------------------------------------------------------
# Phase 3 — xander:3 — Runtime-issuer assertion and discovery-doc gate
# -------------------------------------------------------------------
# MED-A: after configure_oauth_client() updates oidc_auth.OIDC_ISSUER from DB,
# a second assertion catches an empty or CONFIGURE_ME placeholder issuer.
# MED-E: the discovery-doc gate fetches metadata and asserts "issuer" is present
# and matches the configured value (fail-closed; SystemExit on fetch failure too).
#
# Subprocess pattern is used (same as Phase 1/2) to drive startup_event() and
# capture exact FATAL messages from stderr/stdout.


class TestPhase3RuntimeIssuerAndDiscoveryGate:
    """Phase 3 MED-A + MED-E startup gates (xander:3, ATHENA-12)."""

    _PROD_SECRETS: dict = {
        "SESSION_SECRET_KEY": "a" * 64,
        "JWT_SECRET": "b" * 64,
        "SERVICE_API_KEY": "c" * 64,
    }

    def _run_startup_subprocess(self, env_overrides: dict) -> "subprocess.CompletedProcess":
        """Run startup_event() in a subprocess via TestClient (Phase 1/2 pattern)."""
        import subprocess
        import sys as _sys

        env = os.environ.copy()
        _repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..")
        )
        _src_path = os.path.join(_repo_root, "src")
        _backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [_src_path, _backend_path, existing_pythonpath])
        )
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v

        script = (
            "from fastapi.testclient import TestClient; "
            "from main import app; "
            "TestClient(app).__enter__()"
        )
        return subprocess.run(
            [_sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=_backend_path,
        )

    def _prod_env(self, overrides: dict | None = None) -> dict:
        base = {
            "DEV_MODE": "false",
            "OIDC_ISSUER": "https://idp.example.com/",
            "OIDC_CLIENT_ID": "real-client-id-for-phase3-testing",
            **self._PROD_SECRETS,
        }
        if overrides:
            base.update(overrides)
        return base

    def test_phase3_configure_me_oidc_issuer_exits(self):
        """MED-A: OIDC_ISSUER=CONFIGURE_ME_... must abort startup.

        Both the env-var gate (main.py:352-358) and the runtime-issuer assertion
        (MED-A gate added in Phase 3) reject CONFIGURE_ME-prefixed issuers.
        This ensures neither gate can be bypassed.
        """
        result = self._run_startup_subprocess(
            self._prod_env({"OIDC_ISSUER": "CONFIGURE_ME_OIDC_ISSUER"})
        )
        assert result.returncode != 0, (
            f"Startup must exit non-zero for CONFIGURE_ME OIDC_ISSUER (MED-A); "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "FATAL" in combined, (
            f"Error output must contain 'FATAL'; got: {combined!r}"
        )
        assert "OIDC" in combined, (
            f"Error output must reference OIDC; got: {combined!r}"
        )

    def test_phase3_empty_oidc_issuer_exits(self):
        """MED-A: empty OIDC_ISSUER must abort startup (env-var gate fires first)."""
        env = self._prod_env()
        env.pop("OIDC_ISSUER", None)  # simulate missing env var
        result = self._run_startup_subprocess(env)
        assert result.returncode != 0, (
            f"Startup must exit non-zero for empty OIDC_ISSUER (MED-A); "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "FATAL" in combined, (
            f"Error output must contain 'FATAL'; got: {combined!r}"
        )

    def test_phase3_claims_options_call_removed_static(self):
        """Static: authorize_access_token() must NOT be called with a claims_options= kwarg (xander:3).

        The pre-Phase-3 call was:
            authorize_access_token(request, claims_options={"iss": {"essential": False}, ...})
        Post-Phase-3 it must be:
            authorize_access_token(request)
        Check non-comment lines only.
        """
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(backend_dir, "main.py")) as f:
            src = f.read()

        # Strip comment lines before checking so references in explanatory comments don't fire
        code_lines = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
        code = "\n".join(code_lines)
        assert "claims_options=" not in code, (
            "claims_options= kwarg found in non-comment code of main.py — "
            "the xander:3 token-validation bypass may have regressed."
        )

    def test_phase3_stale_comment_removed_static(self):
        """Static: 'Skip ID token validation' comment must be absent from main.py (MED-B).

        The old comment contradicted post-Phase-3 behavior; its presence would mislead
        operators who read the code expecting validation to be disabled.
        """
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(backend_dir, "main.py")) as f:
            src = f.read()
        assert "Skip ID token validation" not in src, (
            "'Skip ID token validation' comment must not appear in main.py (MED-B); "
            "it misrepresents post-Phase-3 behavior."
        )

    def test_phase3_oidc_discovery_gates_present_static(self):
        """Static: MED-E and MED-A gate log keys must be present in main.py.

        Confirms the discovery-doc gate and runtime-issuer assertion were not
        accidentally removed by a future editor.
        """
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(backend_dir, "main.py")) as f:
            src = f.read()

        for key in (
            "oidc_runtime_issuer_unset",
            "oidc_discovery_metadata_fetch_failed",
            "oidc_discovery_missing_issuer",
            "oidc_discovery_issuer_mismatch",
        ):
            assert key in src, (
                f"Log key {key!r} must be present in main.py (MED-E/MED-A gate); "
                "it may have been accidentally removed."
            )

    def test_phase3_med_a_post_configure_fires_independently_of_env_gate(self):
        """MED-A post-configure assertion fires when oidc_auth.OIDC_ISSUER is bad at
        runtime — independent of the env-var gate (ian:26 fix).

        The env-var gate at startup rejects CONFIGURE_ME-prefixed OIDC_ISSUER before
        configure_oauth_client() runs.  But if the DB-loaded value is bad (empty or
        placeholder), the env-var gate never sees it.  This test bypasses the env-var
        gate entirely: it stubs oidc_auth.OIDC_ISSUER directly (simulating a DB-loaded
        bad value) and calls _enforce_oidc_runtime_gates() to confirm MED-A fires.
        """
        import asyncio
        from unittest.mock import AsyncMock, patch

        _backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        _src_path = os.path.abspath(os.path.join(_backend_dir, "..", "..", "src"))
        for _p in (_src_path, _backend_dir):
            if _p not in sys.path:
                sys.path.insert(0, _p)

        import main as _main

        # Stub OIDC_ISSUER to CONFIGURE_ME placeholder — simulates a DB row with a bad value
        # written before the operator configured their IdP.
        with patch("main.oidc_auth") as mock_oidc_auth:
            mock_oidc_auth.OIDC_ISSUER = "CONFIGURE_ME_OIDC_ISSUER"

            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(_main._enforce_oidc_runtime_gates())

        combined = str(exc_info.value)
        assert "FATAL" in combined, f"Expected 'FATAL' in SystemExit message; got: {combined!r}"
        assert "OIDC_ISSUER" in combined, f"Expected 'OIDC_ISSUER' in message; got: {combined!r}"


# -------------------------------------------------------------------
# Phase 4 — xander:4 — JWT removed from OIDC/DEMO_MODE redirect URL
# -------------------------------------------------------------------
# Single coordinated commit: backend emits ?logged_in=1 (not ?token=<jwt>),
# session is stored server-side, frontend fetches JWT via /api/auth/session-token.
#
# D1 (hybrid A+B): ?logged_in=1 is the callback-landing signal that tells the
# frontend to clear stale localStorage before fetching from session.  Without
# this signal the bookmark/revisit path (localStorage-first) is preserved unchanged.
#
# Two fixture shapes:
#   test_client_with_demo_mode — DEV_MODE=true, DEMO_MODE=true, SQLite; uses the
#     module-level app_client approach but with DEMO_MODE forced on.
#   Static tests — read source files for structural invariants.
#
# The OIDC callback path (auth_callback) requires a real OIDC provider to drive
# in integration; that fixture is out of scope here (Phase 3's pytest-httpserver
# fixture is the correct vehicle).  We cover it with a static assertion.


@pytest.fixture
def demo_mode_client():
    """
    TestClient with DEV_MODE=true and DEMO_MODE=true.

    DEMO_MODE triggers the auth_login bypass that issues a demo JWT and redirects.
    Uses module-reload pattern so module-level DEV_MODE / DEMO_MODE captures
    in database.py and main.py reflect the env vars.
    """
    import sys
    from shared.config import get_config
    from fastapi.testclient import TestClient
    from app.database import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    env_overrides = {
        "DEV_MODE": "true",
        "DEMO_MODE": "true",
        "DATABASE_URL": "sqlite:///:memory:",
    }
    saved = {}
    for k, v in env_overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    get_config.cache_clear()
    _modules_to_evict = [
        name for name in sys.modules
        if name == "main" or name.startswith(("main.", "app.", "shared."))
    ]
    for name in _modules_to_evict:
        del sys.modules[name]

    import main as _main
    from app.database import get_db

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    _main.app.dependency_overrides[get_db] = override_get_db

    # SERVICE_API_KEY is read at call time via get_config() (xander:40 refactor).
    # Ensure a known key is in the environment and the lru_cache is fresh so
    # verify_service_api_key returns the correct key on each request.
    os.environ.setdefault("SERVICE_API_KEY", "test-service-key-for-hardening-tests")
    get_config.cache_clear()

    with TestClient(_main.app, raise_server_exceptions=False) as client:
        yield client

    _main.app.dependency_overrides.clear()

    for k, orig_v in saved.items():
        if orig_v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = orig_v
    get_config.cache_clear()


class TestPhase4JwtRemovedFromRedirectUrl:
    """Phase 4 — xander:4: OIDC/DEMO_MODE redirects must not carry JWT in URL (ATHENA-12)."""

    def test_phase4_demo_redirect_omits_token(self, demo_mode_client):
        """xander:4 — DEMO_MODE /auth/login redirect must NOT contain ?token=."""
        r = demo_mode_client.get("/auth/login", follow_redirects=False)
        assert r.status_code in (302, 307), (
            f"Expected redirect from /auth/login in DEMO_MODE; got {r.status_code}"
        )
        location = r.headers.get("location", "")
        assert "token=" not in location, (
            f"DEMO_MODE redirect must not carry JWT in URL; location={location!r}"
        )

    def test_phase4_demo_redirect_carries_logged_in_signal(self, demo_mode_client):
        """xander:4 — DEMO_MODE /auth/login redirect must include ?logged_in=1 signal."""
        r = demo_mode_client.get("/auth/login", follow_redirects=False)
        assert r.status_code in (302, 307)
        location = r.headers.get("location", "")
        assert "logged_in=1" in location, (
            f"DEMO_MODE redirect must include ?logged_in=1 callback-landing signal; "
            f"location={location!r}"
        )

    def test_phase4_session_token_round_trip_after_demo_login(self, demo_mode_client):
        """MED-D / tessa:5 — two-step assertion: callback stores JWT in session;
        /api/auth/session-token returns the same JWT via the session cookie.

        Step 1: hit /auth/login in DEMO_MODE → backend writes access_token to session.
        Step 2: same TestClient (cookies persisted) → GET /api/auth/session-token → 200 + JWT.
        """
        # Step 1 — trigger the DEMO_MODE bypass and capture session cookie
        r1 = demo_mode_client.get("/auth/login", follow_redirects=False)
        assert r1.status_code in (302, 307), (
            f"Expected redirect from /auth/login; got {r1.status_code}"
        )

        # Step 2 — same client carries the session cookie; fetch token via session endpoint
        r2 = demo_mode_client.get("/api/auth/session-token")
        assert r2.status_code == 200, (
            f"session-token endpoint must return 200 after DEMO_MODE login; got {r2.status_code}"
        )
        data = r2.json()
        assert data.get("token"), (
            f"session-token response must contain a non-empty 'token' field; got: {data!r}"
        )
        # Sanity: it must be a three-segment JWT
        assert data["token"].count(".") == 2, (
            f"token must be a JWT (three dot-separated segments); got: {data['token']!r}"
        )

    def test_phase4_static_no_token_in_redirect_lines(self):
        """Static safety net — no RedirectResponse with ?token= in main.py (xander:4)."""
        import re
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(backend_dir, "main.py")) as f:
            src = f.read()

        # Strip comment lines so we don't flag explanatory comments
        code_lines = [line for line in src.splitlines() if not line.lstrip().startswith("#")]
        code = "\n".join(code_lines)

        matches = re.findall(r'RedirectResponse.*?\?token=', code, re.DOTALL)
        assert matches == [], (
            f"Found ?token= inside a RedirectResponse call in main.py — "
            f"xander:4 may have regressed: {matches}"
        )

    def test_phase4_static_logged_in_signal_in_backend(self):
        """Static: backend must emit ?logged_in=1 in at least two redirect sites
        (DEMO_MODE and OIDC callback)."""
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(backend_dir, "main.py")) as f:
            src = f.read()

        count = src.count("logged_in=1")
        assert count >= 2, (
            f"Expected at least 2 occurrences of 'logged_in=1' in main.py "
            f"(DEMO_MODE redirect + OIDC callback redirect); found {count}"
        )

    def test_phase4_static_frontend_no_url_token_reader(self):
        """Static: admin frontend must not read ?token= from the URL (xander:4)."""
        frontend_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
        )
        for filename in ("auth.js", "app.js"):
            fpath = os.path.join(frontend_dir, filename)
            with open(fpath) as f:
                content = f.read()
            assert "urlParams.get('token')" not in content, (
                f"{filename}: URL ?token= reader must be removed (xander:4); "
                "frontend must fetch JWT via /api/auth/session-token after OIDC callback."
            )

    def test_phase4_static_frontend_logged_in_handler_present(self):
        """Static: admin frontend must handle ?logged_in=1 in both auth.js and app.js."""
        frontend_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
        )
        for filename in ("auth.js", "app.js"):
            fpath = os.path.join(frontend_dir, filename)
            with open(fpath) as f:
                content = f.read()
            assert "logged_in" in content, (
                f"{filename}: must contain ?logged_in=1 handler for OIDC callback landing; "
                "this clears stale localStorage on shared devices (codex-H1 fix)."
            )


# ---------------------------------------------------------------------------
# Campaign 3 / ATHENA-14 Phase 2: Alembic migration 054 + SQLite guard
# ---------------------------------------------------------------------------

class TestUserLockoutMigration:
    """Phase 2 — alembic migration 054 adds failed_login_count + locked_until.

    Fixture creates a 053-state DB so we can verify backfill behavior on
    existing rows (tessa:3: migration tests must use a pre-migration fixture,
    not the HEAD-schema conftest fixture).

    Implementation note: the migration chain before 053 uses postgresql.ARRAY
    which cannot compile to SQLite.  We therefore bootstrap the 053 DB state
    directly: create only the users table via raw SQL at the exact 053 schema,
    stamp alembic_version to "053", then run command.upgrade/downgrade to
    exercise only migration 054.  This correctly tests the migration logic
    (ADD COLUMN with server_default, DROP COLUMN) without needing to replay
    the full PG-specific migration history on SQLite.
    """

    _backend_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..")
    )

    # Users table at exactly the 053 schema — no failed_login_count / locked_until.
    # Column set derived from app/models.py User class as of revision 053.
    _USERS_053_DDL = """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            authentik_id VARCHAR(255) UNIQUE,
            username VARCHAR(255) UNIQUE NOT NULL,
            email VARCHAR(255) UNIQUE NOT NULL,
            full_name VARCHAR(255),
            auth_provider VARCHAR(32) NOT NULL DEFAULT 'oidc',
            password_hash VARCHAR(512),
            role VARCHAR(32) NOT NULL DEFAULT 'viewer',
            active BOOLEAN NOT NULL DEFAULT 1,
            last_login DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """

    def _make_053_state_db(self, tmp_path):
        """Create a SQLite DB with the users table at revision 053 schema.

        Bootstraps the 053 state by:
          1. Building an alembic Config pointing at a fresh SQLite file
          2. Temporarily removing DATABASE_URL from os.environ so env.py uses
             cfg.set_main_option("sqlalchemy.url") instead of the test-suite's
             sqlite:///:memory: (which is discarded after stamp)
          3. Stamping alembic_version to '053' + '93bea4659785' (the side branch
             that chains off '003') so command.upgrade('054') sees both branch
             heads as current and runs only migration 054
          4. Creating the users table via raw DDL at the exact 053 column set

        Returns (engine, alembic_cfg).
        """
        from sqlalchemy import create_engine, text
        from alembic.config import Config
        from alembic import command

        db_path = str(tmp_path / "phase2_migration_test.db")

        cfg = Config(os.path.join(self._backend_dir, "alembic.ini"))
        cfg.set_main_option(
            "script_location", os.path.join(self._backend_dir, "alembic")
        )
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

        # Temporarily remove DATABASE_URL so alembic env.py uses the URL we
        # set via cfg.set_main_option above.  The test module sets
        # DATABASE_URL=sqlite:///:memory: globally; if left in the environment,
        # alembic stamp writes to :memory: (discarded) instead of db_path.
        _saved_db_url = os.environ.pop("DATABASE_URL", None)
        try:
            # Stamp both branch heads so upgrade sees the DB as already at 053.
            # '93bea4659785' chains off '003' (a PG-only migration); without it
            # alembic would try to apply '003 → 93bea4659785' on SQLite and fail.
            command.stamp(cfg, "053")
            command.stamp(cfg, "93bea4659785")
        finally:
            if _saved_db_url is not None:
                os.environ["DATABASE_URL"] = _saved_db_url

        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            conn.execute(text(self._USERS_053_DDL))
            conn.commit()

        return engine, cfg

    def _run_alembic(self, cfg, direction, target):
        """Run alembic upgrade/downgrade with DATABASE_URL temporarily removed.

        The test module sets DATABASE_URL=sqlite:///:memory: globally; alembic
        env.py prefers os.environ["DATABASE_URL"] over cfg.get_main_option().
        We must hide it so env.py uses our file-backed URL.
        """
        from alembic import command as _cmd

        _saved = os.environ.pop("DATABASE_URL", None)
        try:
            if direction == "upgrade":
                _cmd.upgrade(cfg, target)
            else:
                _cmd.downgrade(cfg, target)
        finally:
            if _saved is not None:
                os.environ["DATABASE_URL"] = _saved

    def test_phase2_054_upgrade_adds_columns_with_defaults(self, tmp_path):
        """Migration 054 adds both columns; existing rows backfill to 0 / NULL."""
        from sqlalchemy import text

        engine, cfg = self._make_053_state_db(tmp_path)
        # Insert a row at 053 schema (no lockout columns yet)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (username, email, role) "
                    "VALUES ('alice', 'a@x.test', 'admin')"
                )
            )
            conn.commit()

        self._run_alembic(cfg, "upgrade", "054")

        # Both columns must exist and backfill correctly
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT failed_login_count, locked_until "
                    "FROM users WHERE username='alice'"
                )
            ).first()
        assert row.failed_login_count == 0, (
            f"failed_login_count backfill must be 0; got {row.failed_login_count!r}"
        )
        assert row.locked_until is None, (
            f"locked_until backfill must be NULL; got {row.locked_until!r}"
        )

    def test_phase2_054_downgrade_drops_columns(self, tmp_path):
        """Migration 054 downgrade removes both columns cleanly."""
        from sqlalchemy import text

        engine, cfg = self._make_053_state_db(tmp_path)
        self._run_alembic(cfg, "upgrade", "054")
        self._run_alembic(cfg, "downgrade", "053")

        # Confirm columns no longer exist in the schema
        with engine.connect() as conn:
            cols = [
                r[1]
                for r in conn.execute(text("PRAGMA table_info(users)")).fetchall()
            ]
        assert "failed_login_count" not in cols, (
            "downgrade must drop failed_login_count from users"
        )
        assert "locked_until" not in cols, (
            "downgrade must drop locked_until from users"
        )

    def test_phase2_sqlite_capability_guard_present_in_startup(self):
        """Static check: startup_event must contain the SQLite >= 3.35 capability guard."""
        from pathlib import Path

        source = Path(os.path.join(self._backend_dir, "main.py")).read_text()
        assert "sqlite_version_info" in source, (
            "startup_event must check sqlite_version_info for UPDATE...RETURNING guard"
        )
        assert "(3, 35, 0)" in source, (
            "startup_event guard must compare against (3, 35, 0)"
        )
        assert "UPDATE...RETURNING" in source or "UPDATE ... RETURNING" in source, (
            "startup_event guard comment must reference UPDATE...RETURNING"
        )

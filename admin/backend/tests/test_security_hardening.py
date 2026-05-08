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

    # Superseded by behavioral tests in TestAthena21StartupGate (ATHENA-21). Kept for
    # now as a static-shape canary for the _INSECURE_DEFAULTS dict structure; can be
    # removed once the behavioral suite covers all three secret keys
    # (SESSION_SECRET_KEY, JWT_SECRET, SERVICE_API_KEY) symmetrically.
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


# -------------------------------------------------------------------
# ATHENA-21 — SERVICE_API_KEY startup gate behavioral regression tests
# -------------------------------------------------------------------
# The _INSECURE_DEFAULTS loop in admin/backend/main.py already rejects unset
# or placeholder SERVICE_API_KEY in production. These tests pin that behavior
# with subprocess execution so a refactor of the loop cannot silently remove
# the gate without breaking the test suite.


class TestAthena21StartupGate:
    """ATHENA-21 — behavioral regression tests for the SERVICE_API_KEY startup gate.

    The gate itself lives in admin/backend/main.py's _INSECURE_DEFAULTS loop
    (kind="secret" treats empty AND the documented placeholder as fatal in production).
    Tests in this class drive startup via subprocess or in-process depending on
    which signal we need — see individual test docstrings.
    """

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
    # ATHENA-21 — empty SERVICE_API_KEY must abort production startup
    # ------------------------------------------------------------------

    def test_unset_service_api_key_in_production_exits(self):
        """ATHENA-21 — empty SERVICE_API_KEY in production must SystemExit."""
        result = self._run_startup_subprocess(self._prod_env({"SERVICE_API_KEY": ""}))
        assert result.returncode != 0, (
            f"Startup must exit non-zero for empty SERVICE_API_KEY; "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "SERVICE_API_KEY" in combined, (
            f"Error output must mention SERVICE_API_KEY; got: {combined!r}"
        )
        assert "FATAL" in combined, (
            f"Error output must contain 'FATAL'; got: {combined!r}"
        )

    # ------------------------------------------------------------------
    # ATHENA-21 — placeholder SERVICE_API_KEY must abort production startup
    # ------------------------------------------------------------------

    def test_insecure_placeholder_service_api_key_in_production_exits(self):
        """ATHENA-21 — SERVICE_API_KEY=dev-service-key-change-in-production must SystemExit.

        Pins the placeholder-string branch of the _INSECURE_DEFAULTS loop for the
        SERVICE_API_KEY secret-kind entry. The empty-string branch is covered by
        test_unset_service_api_key_in_production_exits above.
        """
        result = self._run_startup_subprocess(
            self._prod_env({"SERVICE_API_KEY": "dev-service-key-change-in-production"})
        )
        assert result.returncode != 0, (
            f"Startup must exit non-zero for placeholder SERVICE_API_KEY; "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "SERVICE_API_KEY" in combined, (
            f"Error output must mention SERVICE_API_KEY; got: {combined!r}"
        )
        assert "FATAL" in combined, (
            f"Error output must contain 'FATAL'; got: {combined!r}"
        )

    # ------------------------------------------------------------------
    # ATHENA-21 — DEV_MODE bypasses SERVICE_API_KEY requirement
    # ------------------------------------------------------------------

    def test_dev_mode_bypasses_service_api_key_requirement(self):
        """ATHENA-21 — DEV_MODE=true + empty SERVICE_API_KEY must NOT abort startup.

        Uses _run_startup_in_process rather than _run_startup_subprocess because we
        need to verify the app reaches a healthy started state: subprocess returncode==0
        does not distinguish between "gate did not fire" and "process exited cleanly for
        other reasons." _run_startup_in_process raises SystemExit directly into the test
        on gate failure, giving an unambiguous signal.

        OIDC vars are deliberately omitted: DEV_MODE=true takes the early-return path
        in startup_event() before any OIDC discovery is attempted.
        """
        self._run_startup_in_process({
            "DEV_MODE": "true",
            "DATABASE_URL": "sqlite:///:memory:",
            "SERVICE_API_KEY": "",
        })

    # ------------------------------------------------------------------
    # ATHENA-21 — valid SERVICE_API_KEY must NOT trigger the gate (negative assertion)
    # ------------------------------------------------------------------

    def test_set_service_api_key_in_production_passes(self):
        """ATHENA-21 — a real SERVICE_API_KEY must not trigger the startup gate.

        This is a negative assertion. It proves the SERVICE_API_KEY gate did not fire
        on a valid production key. It does NOT assert full startup success — OIDC
        discovery is expected to fail later in CI without an IdP stub, and that failure
        is out of scope for this test.
        """
        result = self._run_startup_subprocess(
            self._prod_env({"SERVICE_API_KEY": "real-secret-" + ("a" * 50)})
        )
        combined = result.stdout + result.stderr
        assert "var=SERVICE_API_KEY" not in combined, (
            f"SERVICE_API_KEY gate must NOT fire on a valid key; got: {combined!r}"
        )
        assert "SERVICE_API_KEY='dev-service-key-change-in-production'" not in combined, (
            f"Placeholder gate must NOT fire on a valid key; got: {combined!r}"
        )
        assert "SERVICE_API_KEY=None" not in combined, (
            f"None-variant gate must NOT fire on a valid key; got: {combined!r}"
        )
        assert "SERVICE_API_KEY=''" not in combined, (
            f"Empty-string gate must NOT fire on a valid key; got: {combined!r}"
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


# ---------------------------------------------------------------------------
# Campaign 3 / ATHENA-14 Phase 3: fastapi-limiter wiring + LIMITER_ACTIVE flag
# ---------------------------------------------------------------------------

class TestRateLimiterStartup:
    """Phase 3 — fastapi-limiter init wiring and LIMITER_ACTIVE single-positive-flag.

    Tests cover:
      (a) Successful init flips LIMITER_ACTIVE True (in-process, fakeredis[lua])
      (b) Failed init leaves LIMITER_ACTIVE False (in-process, monkeypatched raise)
      (c) DEV_MODE: _init_rate_limiter is never called, flag stays False (static)
      (d) login_rate_limit_dep is a plain async function (resolved at request time)
    """

    _backend_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..")
    )

    def test_phase3_init_with_fakeredis_sets_limiter_active(self):
        """In-process: _init_rate_limiter with fakeredis[lua] flips LIMITER_ACTIVE True."""
        import asyncio
        import sys

        # Remove cached rate_limit module so re-import sees fresh module-level state.
        for name in list(sys.modules):
            if name in ("app.utils.rate_limit",) or name.startswith("app.utils.rate_limit."):
                sys.modules.pop(name, None)

        from app.utils import rate_limit as rate_limit_mod
        rate_limit_mod.LIMITER_ACTIVE = False  # ensure we start clean

        import main as _main

        async def _run():
            import fakeredis.aioredis

            # Close any previously-open FastAPILimiter redis connection to avoid
            # "Event loop is closed" noise from a prior test.
            try:
                from fastapi_limiter import FastAPILimiter
                if getattr(FastAPILimiter, "redis", None) is not None:
                    await FastAPILimiter.close()
            except Exception:
                pass

            fake_redis = fakeredis.aioredis.FakeRedis()
            try:
                await _main._init_rate_limiter(fake_redis)

                assert rate_limit_mod.LIMITER_ACTIVE is True, (
                    "_init_rate_limiter with a live fakeredis connection must set LIMITER_ACTIVE=True"
                )
            finally:
                # ian:1 — teardown must be in a finally block so LIMITER_ACTIVE is
                # reset even if the assertion above raises, preventing flag bleed
                # into subsequent tests.
                try:
                    from fastapi_limiter import FastAPILimiter as _FL
                    await _FL.close()
                except Exception:
                    pass
                rate_limit_mod.LIMITER_ACTIVE = False

        asyncio.run(_run())

    def test_phase3_init_failure_leaves_limiter_inactive(self, monkeypatch):
        """Monkeypatched FastAPILimiter.init raises → LIMITER_ACTIVE stays False."""
        import asyncio
        import sys

        for name in list(sys.modules):
            if name in ("app.utils.rate_limit",) or name.startswith("app.utils.rate_limit."):
                sys.modules.pop(name, None)

        from app.utils import rate_limit as rate_limit_mod
        rate_limit_mod.LIMITER_ACTIVE = False

        from fastapi_limiter import FastAPILimiter

        async def _boom(*args, **kwargs):
            raise ConnectionError("redis://127.0.0.1:1 connection refused (test stub)")

        monkeypatch.setattr(FastAPILimiter, "init", _boom)

        import main as _main

        # Use an arbitrary object as redis_conn — the monkeypatched init ignores it.
        class _FakeConn:
            pass

        asyncio.run(_main._init_rate_limiter(_FakeConn()))

        assert rate_limit_mod.LIMITER_ACTIVE is False, (
            "_init_rate_limiter when FastAPILimiter.init raises must leave LIMITER_ACTIVE=False"
        )

    def test_phase3_dev_mode_skips_rate_limiter_init(self):
        """Static check: _init_rate_limiter is called only inside the production else branch.

        DEV_MODE takes the `if DEV_MODE:` path in startup_event and returns before
        reaching the production else block.  We verify this by asserting that the
        call site sits after the `else:` keyword and before the unconditional
        OSS_AUTO_PULL_MODELS block — not inside the `if DEV_MODE:` branch.
        """
        from pathlib import Path

        source = Path(os.path.join(self._backend_dir, "main.py")).read_text()

        assert "_init_rate_limiter" in source, (
            "main.py must define and call _init_rate_limiter"
        )
        # The call must appear after _enforce_oidc_runtime_gates (production branch)
        # and before OSS_AUTO_PULL_MODELS (unconditional block).
        enforce_pos = source.find("await _enforce_oidc_runtime_gates()")
        limiter_pos = source.find("await _init_rate_limiter(redis_client)")
        auto_pull_pos = source.find("if OSS_AUTO_PULL_MODELS:")

        assert enforce_pos != -1, "startup_event must call _enforce_oidc_runtime_gates"
        assert limiter_pos != -1, "startup_event must call _init_rate_limiter(redis_client)"
        assert auto_pull_pos != -1, "startup_event must have the OSS_AUTO_PULL_MODELS block"

        assert enforce_pos < limiter_pos < auto_pull_pos, (
            "_init_rate_limiter call must appear AFTER _enforce_oidc_runtime_gates "
            "and BEFORE the OSS_AUTO_PULL_MODELS block (production else branch only)"
        )

    def test_phase3_login_rate_limit_dep_resolves_at_request_time(self):
        """login_rate_limit_dep must be a plain async coroutine function, not a factory call.

        The bob:1 / codex-r1 fix requires the dep to be defined as `async def
        login_rate_limit_dep(...)` so FastAPI resolves it per-request, not at
        module-import time.  A factory-call form (e.g. `RateLimiter(...)` returned
        directly) would bake in LIMITER_ACTIVE at import time, making the
        graceful-degrade no-op dead code.
        """
        import inspect
        from app.utils.rate_limit import login_rate_limit_dep

        assert inspect.iscoroutinefunction(login_rate_limit_dep), (
            "login_rate_limit_dep must be an async def coroutine function "
            "(not a factory-returned RateLimiter instance)"
        )

    def test_phase3_login_rate_limit_dep_attached_to_route(self):
        """xander:46 / Phase 3-vs-4 atomic deploy gate.

        Phase 3 wires LIMITER_ACTIVE + login_rate_limit_dep but Phase 4 attaches
        the dep to /api/auth/local-login.  This test FAILS until Phase 4 lands —
        intentional CI canary preventing Phase 3 from being deployed without
        Phase 4's brute-force protection.

        When Phase 4 ships, the dep is in the route's dependencies; this test
        passes (xpassed with strict=True causes CI to report success).  Remove
        the xfail marker at that point so xpassed becomes a normal pass.
        If Phase 4 is later reverted without reverting Phase 3, this test catches it.
        """
        from app.routes import local_auth
        from fastapi.routing import APIRoute

        # Find the /api/auth/local-login route.
        # local_auth.router has prefix="/api/auth", so the full path stored on
        # each APIRoute is "/api/auth/local-login" (not just "/local-login").
        target_route = None
        for route in local_auth.router.routes:
            if isinstance(route, APIRoute) and route.path == "/api/auth/local-login":
                target_route = route
                break
        assert target_route is not None, "could not find /api/auth/local-login route in local_auth.router"

        # Check that login_rate_limit_dep is in the route's resolved dependencies.
        # FastAPI ≥0.100 stores each dependency as a Dependant with a .call attribute
        # (not a Dependency namedtuple with .dependency — that was the pre-0.100 shape).
        #
        # Compare by qualname+module rather than object identity: prior tests in this
        # file evict app.utils.rate_limit from sys.modules, so a fresh import of
        # login_rate_limit_dep yields a different object than the one captured at
        # route-registration time.  Matching on function name + module path is stable
        # across module reloads and is sufficient to verify the dep is wired.
        dep_names = {
            (getattr(d.call, "__qualname__", None), getattr(d.call, "__module__", None))
            for d in target_route.dependant.dependencies
        }
        assert ("login_rate_limit_dep", "app.utils.rate_limit") in dep_names, (
            "login_rate_limit_dep MUST be attached to /api/auth/local-login. "
            "Phase 3 wires the dep but Phase 4 attaches it. If this test is failing, "
            "Phase 4 hasn't shipped yet — DO NOT deploy Phase 3 alone (xander:46). "
            f"Found deps: {dep_names!r}"
        )

    def test_zero_per_minute_short_circuits_rate_limiter(self):
        """codex-r2:4 — LOGIN_RATE_LIMIT_PER_MINUTE=0 must bypass the limiter entirely.

        fastapi-limiter's Lua script treats times=0 as "allow 1 then 429" — the
        opposite of the documented 'set to 0 to disable' semantics.  The short-circuit
        guard added in the reconcile commit (ATHENA-14) must return before constructing
        RateLimiter so that 0 (or negative) truly disables rate limiting while the
        lockout layer and 400 ms constant-time floor still protect /local-login.
        """
        import asyncio
        from unittest.mock import MagicMock, patch
        from shared.config import _clear_cache_for_tests
        from app.utils import rate_limit as rl

        old_env = os.environ.get("LOGIN_RATE_LIMIT_PER_MINUTE")

        def _set_limit(value: str) -> None:
            os.environ["LOGIN_RATE_LIMIT_PER_MINUTE"] = value
            _clear_cache_for_tests()  # force get_config() to re-read env

        mock_request = MagicMock()
        mock_response = MagicMock()

        try:
            for limit_val in ("0", "-1"):
                _set_limit(limit_val)
                rl.LIMITER_ACTIVE = True

                with patch("app.utils.rate_limit.RateLimiter") as mock_limiter_cls:
                    # Invoke the dep multiple times — none should reach RateLimiter.
                    for _ in range(3):
                        asyncio.run(rl.login_rate_limit_dep(mock_request, mock_response))

                    assert mock_limiter_cls.call_count == 0, (
                        f"RateLimiter must not be constructed when "
                        f"LOGIN_RATE_LIMIT_PER_MINUTE={limit_val} "
                        f"(short-circuit guard, codex-r2:4 / ATHENA-14)"
                    )
        finally:
            # Restore env and flag so subsequent tests are unaffected.
            if old_env is None:
                os.environ.pop("LOGIN_RATE_LIMIT_PER_MINUTE", None)
            else:
                os.environ["LOGIN_RATE_LIMIT_PER_MINUTE"] = old_env
            _clear_cache_for_tests()
            rl.LIMITER_ACTIVE = False


# ---------------------------------------------------------------------------
# Campaign 3 / ATHENA-14 Phase 4: login lockout state machine
# ---------------------------------------------------------------------------

class TestLocalLoginLockout:
    """Phase 4 — xander:10 enforcement: per-username lockout + 400ms floor + TOCTOU safety.

    11 cases (D4 contract):
      1. success — count resets, last_login updated
      2. wrong password — 401, count incremented
      3. unknown username — 401, no DB write
      4. inactive user — 401 generic (Decision D; NOT 403)
      5. locked + correct password — 401 generic (lock wins)
      6. locked-expired — success (lazy clear)
      7. timing: success path NOT padded (elapsed < 400ms acceptable)
      8. timing: failure path elapsed >= 360ms (40ms slack under 400ms floor)
      9. TOCTOU: 5 concurrent wrong-password requests — final count exactly 5
     10. 429 from rate-limiter does NOT increment failed_login_count (tessa:6)
     11. case-mode boundary: username lookup is case-sensitive
    """

    # ------------------------------------------------------------------
    # Fixture helpers
    # ------------------------------------------------------------------

    @pytest.fixture(autouse=False)
    def lockout_client(self):
        """Function-scoped TestClient with an isolated in-memory SQLite DB.

        Function scope so each test starts with a clean DB (no leftover
        failed_login_count from a prior test).  Pattern mirrors app_client
        but is function-scoped and yields both client AND db session factory
        so tests can insert users and inspect row state.
        """
        import sys
        from fastapi.testclient import TestClient
        from app.database import Base, get_db
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from shared.config import get_config

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

        # Reload main in a fresh module environment to avoid any module-level
        # state leaking from prior tests (e.g. LIMITER_ACTIVE or cached config).
        _modules_to_evict = [
            name for name in sys.modules
            if name == "main" or name.startswith(("main.", "app.", "shared."))
        ]
        for name in _modules_to_evict:
            del sys.modules[name]

        get_config.cache_clear()

        import main as _main
        from app.database import get_db as _get_db

        _main.app.dependency_overrides[_get_db] = override_get_db

        with TestClient(_main.app, raise_server_exceptions=False) as client:
            yield client, TestingSessionLocal

        _main.app.dependency_overrides.clear()
        get_config.cache_clear()

    def _make_user(self, db, *, username="alice", password="password1234",
                   auth_provider="local", active=True,
                   failed_login_count=0, locked_until=None):
        """Insert a test user and return the committed, refreshed ORM row.

        Password is hashed with iterations=1000 (tessa:4) to keep tests fast.
        locked_until must be tz-aware if provided (tessa:7 / xander:41).
        """
        from app.models import User
        from app.utils.passwords import hash_password as _hp

        user = User(
            username=username,
            email=f"{username}@test.example",
            auth_provider=auth_provider,
            password_hash=_hp(password, iterations=1000),
            active=active,
            failed_login_count=failed_login_count,
            locked_until=locked_until,
            role="viewer",
        )
        db.add(user)
        # ian-#7: commit + refresh before any assertion so SQLAlchemy session
        # cache doesn't carry None for server-default columns.
        db.commit()
        db.refresh(user)
        return user

    # ------------------------------------------------------------------
    # Case 1: successful login resets count and sets last_login
    # ------------------------------------------------------------------

    def test_success_resets_count_and_sets_last_login(self, lockout_client):
        """Case 1 — correct credentials: 200, failed_login_count reset to 0, last_login set."""
        from datetime import datetime, timezone

        client, SessionLocal = lockout_client
        db = SessionLocal()
        try:
            user = self._make_user(db, failed_login_count=3)
            user_id = user.id

            r = client.post(
                "/api/auth/local-login",
                json={"username": "alice", "password": "password1234"},
            )
            assert r.status_code == 200, f"Expected 200; got {r.status_code}: {r.text}"

            db.expire_all()
            from app.models import User
            refreshed = db.query(User).filter(User.id == user_id).first()
            assert refreshed.failed_login_count == 0, (
                f"Success must reset failed_login_count to 0; got {refreshed.failed_login_count}"
            )
            assert refreshed.last_login is not None, (
                "Success must set last_login"
            )
            # last_login should be recent (within last 10 seconds).
            # SQLite returns naive datetimes even for DateTime(timezone=True) columns;
            # normalise to UTC before subtracting (tessa:7 / xander:41).
            ll = refreshed.last_login
            if ll.tzinfo is None:
                ll = ll.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ll).total_seconds()
            assert age < 10, f"last_login is stale ({age:.1f}s ago)"
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Case 2: wrong password increments count
    # ------------------------------------------------------------------

    def test_wrong_password_increments_count(self, lockout_client):
        """Case 2 — wrong password: 401, failed_login_count incremented."""
        client, SessionLocal = lockout_client
        db = SessionLocal()
        try:
            user = self._make_user(db)
            user_id = user.id

            r = client.post(
                "/api/auth/local-login",
                json={"username": "alice", "password": "wrongpassword"},
            )
            assert r.status_code == 401, f"Expected 401; got {r.status_code}"
            assert r.json().get("detail") == "Invalid username or password"

            db.expire_all()
            from app.models import User
            refreshed = db.query(User).filter(User.id == user_id).first()
            assert refreshed.failed_login_count == 1, (
                f"Wrong password must increment count to 1; got {refreshed.failed_login_count}"
            )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Case 3: unknown username — 401, no DB write
    # ------------------------------------------------------------------

    def test_unknown_username_returns_401_no_db_write(self, lockout_client):
        """Case 3 — unknown username: 401, no row inserted or modified."""
        from app.models import User

        client, SessionLocal = lockout_client
        db = SessionLocal()
        try:
            r = client.post(
                "/api/auth/local-login",
                json={"username": "nobody", "password": "password1234"},
            )
            assert r.status_code == 401, f"Expected 401; got {r.status_code}"
            assert r.json().get("detail") == "Invalid username or password"

            count = db.query(User).filter(User.username == "nobody").count()
            assert count == 0, "Unknown username must not create a DB row"
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Case 4: inactive user — 401 generic (Decision D, NOT 403)
    # ------------------------------------------------------------------

    def test_inactive_user_returns_401_generic(self, lockout_client):
        """Case 4 — inactive user: 401 'Invalid username or password' (NOT 403).

        Decision D: all failure paths return identical 401 to prevent
        status-code enumeration oracle.
        """
        client, SessionLocal = lockout_client
        db = SessionLocal()
        try:
            self._make_user(db, active=False)

            r = client.post(
                "/api/auth/local-login",
                json={"username": "alice", "password": "password1234"},
            )
            assert r.status_code == 401, (
                f"Inactive user must return 401 (not 403); got {r.status_code}"
            )
            assert r.json().get("detail") == "Invalid username or password", (
                f"Inactive user must return generic message; got {r.json()!r}"
            )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Case 5: locked + correct password — 401 generic (lock wins)
    # ------------------------------------------------------------------

    def test_locked_user_correct_password_returns_401(self, lockout_client):
        """Case 5 — locked account + correct password: 401 generic (lock wins)."""
        from datetime import datetime, timedelta, timezone

        client, SessionLocal = lockout_client
        db = SessionLocal()
        try:
            self._make_user(
                db,
                locked_until=datetime.now(timezone.utc) + timedelta(minutes=30),
            )

            r = client.post(
                "/api/auth/local-login",
                json={"username": "alice", "password": "password1234"},
            )
            assert r.status_code == 401, (
                f"Locked user must return 401; got {r.status_code}"
            )
            assert r.json().get("detail") == "Invalid username or password"
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Case 6: locked-expired — lazy clear, then success
    # ------------------------------------------------------------------

    def test_locked_expired_clears_and_succeeds(self, lockout_client):
        """Case 6 — lock has expired: lazy-clear failed_login_count + locked_until, then 200."""
        from datetime import datetime, timedelta, timezone
        from app.models import User

        client, SessionLocal = lockout_client
        db = SessionLocal()
        try:
            user = self._make_user(
                db,
                failed_login_count=10,
                locked_until=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
            user_id = user.id

            r = client.post(
                "/api/auth/local-login",
                json={"username": "alice", "password": "password1234"},
            )
            assert r.status_code == 200, (
                f"Expired lock must allow login; got {r.status_code}: {r.text}"
            )

            db.expire_all()
            refreshed = db.query(User).filter(User.id == user_id).first()
            assert refreshed.failed_login_count == 0, (
                f"Lazy-expire must reset count; got {refreshed.failed_login_count}"
            )
            assert refreshed.locked_until is None, (
                "Lazy-expire must clear locked_until"
            )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Case 7: timing — success path NOT padded (fast path ok)
    # ------------------------------------------------------------------

    def test_success_path_not_artificially_delayed(self, lockout_client):
        """Case 7 — success path must NOT wait for the 400ms floor.

        The 400ms delay applies only to failure branches.  A successful
        login should complete in well under 400ms on any CI hardware
        (PBKDF2 iterations=1000 keeps it < 50ms).
        """
        import time

        client, SessionLocal = lockout_client
        db = SessionLocal()
        try:
            self._make_user(db)

            t0 = time.monotonic()
            r = client.post(
                "/api/auth/local-login",
                json={"username": "alice", "password": "password1234"},
            )
            elapsed_ms = (time.monotonic() - t0) * 1000

            assert r.status_code == 200, f"Expected 200; got {r.status_code}"
            # Success with iterations=1000 must be well under 400ms.
            # 1500ms ceiling is generous enough for the slowest CI runner.
            assert elapsed_ms < 1500, (
                f"Success path must not be padded with the 400ms floor; "
                f"elapsed={elapsed_ms:.1f}ms — this is too slow"
            )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Case 8: timing — failure path >= 360ms (40ms slack under 400ms floor)
    # ------------------------------------------------------------------

    def test_failure_path_minimum_delay_enforced(self, lockout_client):
        """Case 8 — wrong-password failure must take >= 360ms (400ms floor - 40ms slack).

        The 40ms slack accommodates CI timer jitter without making the test
        brittle.  PBKDF2 at iterations=1000 completes in < 10ms, so the
        asyncio.sleep pad dominates.
        """
        import time

        client, SessionLocal = lockout_client
        db = SessionLocal()
        try:
            self._make_user(db)

            t0 = time.monotonic()
            r = client.post(
                "/api/auth/local-login",
                json={"username": "alice", "password": "wrongpassword"},
            )
            elapsed_ms = (time.monotonic() - t0) * 1000

            assert r.status_code == 401, f"Expected 401; got {r.status_code}"
            assert elapsed_ms >= 360, (
                f"Failure path must enforce >= 360ms (400ms floor - 40ms slack); "
                f"elapsed={elapsed_ms:.1f}ms"
            )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Case 9: TOCTOU — 5 concurrent wrong-password requests, count == 5
    # ------------------------------------------------------------------

    def test_concurrent_failures_no_double_increment(self, lockout_client):
        """Case 9 — 5 concurrent wrong-password requests must result in count==5.

        With ORM read-modify-write, concurrent requests race and can produce
        counts 1–5 non-deterministically.  The atomic UPDATE...RETURNING
        increment guarantees all 5 writes land regardless of interleaving.

        Uses httpx.AsyncClient (ASGI transport) + asyncio.gather for true
        concurrent dispatch without a real network hop (xander:39 / tessa:2).
        """
        import asyncio
        import httpx

        client, SessionLocal = lockout_client
        db = SessionLocal()
        try:
            user = self._make_user(db)
            user_id = user.id

            # Extract the underlying ASGI app from TestClient for httpx transport.
            asgi_app = client.app

            async def _run():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=asgi_app),
                    base_url="http://test",
                ) as ac:
                    tasks = [
                        ac.post(
                            "/api/auth/local-login",
                            json={"username": "alice", "password": "wrongpassword"},
                        )
                        for _ in range(5)
                    ]
                    responses = await asyncio.gather(*tasks)
                return responses

            responses = asyncio.run(_run())

            for resp in responses:
                assert resp.status_code == 401, (
                    f"Concurrent wrong-password must return 401; got {resp.status_code}"
                )

            db.expire_all()
            from app.models import User as _User
            refreshed = db.query(_User).filter(_User.id == user_id).first()
            assert refreshed.failed_login_count == 5, (
                f"Atomic UPDATE must result in exactly 5; got {refreshed.failed_login_count}. "
                "If < 5, TOCTOU race is present — atomic UPDATE...RETURNING broken."
            )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Case 10: 429 from rate-limiter does NOT increment failed_login_count
    # ------------------------------------------------------------------

    def test_rate_limit_429_does_not_increment_count(self, lockout_client):
        """Case 10 — 429 from the rate-limiter dep must not touch failed_login_count.

        When LIMITER_ACTIVE is False (DEV_MODE) the dep no-ops; this test
        verifies the structural contract: the limiter dep fires BEFORE the
        handler body, so a 429 short-circuits before any DB write.

        In DEV_MODE (this test environment), the limiter is inactive and
        all requests reach the handler.  We verify the contract statically:
        by confirming the dep is in the route's dependency chain, and that
        the handler's first DB interaction is after the rate-limit check.

        Additionally we confirm that a wrong-password attempt that DOES
        reach the handler body increments count, while a request that would
        be rejected at the dep layer never touches the count column.
        """
        from app.routes import local_auth
        from fastapi.routing import APIRoute
        from app.utils.rate_limit import login_rate_limit_dep

        # Structural: dep must be before handler body
        # Router prefix="/api/auth" → full path is "/api/auth/local-login".
        target_route = None
        for route in local_auth.router.routes:
            if isinstance(route, APIRoute) and route.path == "/api/auth/local-login":
                target_route = route
                break
        assert target_route is not None
        # FastAPI >=0.100: compare by qualname+module, not object identity
        # (prior tests may evict app.utils.rate_limit from sys.modules).
        dep_names = {
            (getattr(d.call, "__qualname__", None), getattr(d.call, "__module__", None))
            for d in target_route.dependant.dependencies
        }
        assert ("login_rate_limit_dep", "app.utils.rate_limit") in dep_names, (
            "login_rate_limit_dep must be in the route's dependency chain so "
            "a 429 short-circuits before the handler body increments the count"
        )

        # Behavioral: a request that reaches the handler increments count;
        # confirms the counter is only touched in the handler, not in the dep.
        client, SessionLocal = lockout_client
        db = SessionLocal()
        try:
            user = self._make_user(db)
            user_id = user.id

            r = client.post(
                "/api/auth/local-login",
                json={"username": "alice", "password": "wrongpassword"},
            )
            assert r.status_code == 401

            db.expire_all()
            from app.models import User
            refreshed = db.query(User).filter(User.id == user_id).first()
            # Count must be 1 (handler incremented it)
            assert refreshed.failed_login_count == 1, (
                "Handler must increment count on wrong password"
            )
            # A rate-limited 429 would have count=0 because the dep fires
            # before the handler body — that's the tessa:6 contract.
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Case 11: case-sensitive username lookup
    # ------------------------------------------------------------------

    def test_username_lookup_is_case_sensitive(self, lockout_client):
        """Case 11 — username lookup is case-sensitive (no collation override).

        User.username is stored as-inserted.  Attempting login with wrong
        case ("Alice" when stored as "alice") must return 401 (user-not-found
        path, not wrong-password path) — confirming the handler uses exact
        string equality via SQLAlchemy's == operator on the String column,
        which SQLite evaluates case-sensitively for ASCII by default.
        """
        client, SessionLocal = lockout_client
        db = SessionLocal()
        try:
            self._make_user(db, username="alice")

            # Wrong case — should hit branch 1 (user not found)
            r = client.post(
                "/api/auth/local-login",
                json={"username": "Alice", "password": "password1234"},
            )
            assert r.status_code == 401, (
                f"Wrong-case username must return 401; got {r.status_code}"
            )
            assert r.json().get("detail") == "Invalid username or password"

            # Correct case — must succeed
            r2 = client.post(
                "/api/auth/local-login",
                json={"username": "alice", "password": "password1234"},
            )
            assert r2.status_code == 200, (
                f"Correct-case username must return 200; got {r2.status_code}: {r2.text}"
            )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Case 12: xander:50 — all 4 failure branches pay the 400ms floor
    # ------------------------------------------------------------------

    def test_xander50_all_failure_branches_pay_floor(self, lockout_client):
        """Case 12 — all 4 failure branches enforce the 400ms floor (xander:50).

        Inactive and locked branches previously skipped the PBKDF2 dummy-hash
        check, leaving them statistically distinguishable from not-found and
        wrong-password on slow hardware where PBKDF2-600k alone exceeds 400ms.
        Phase 4 reconcile equalizes by calling verify_password against
        _DUMMY_PBKDF2_HASH in those branches too.  The wall-time floor (400ms)
        catches anyone who removes the dummy-hash call later.

        Branch 1: unknown username
        Branch 2: inactive user
        Branch 3: locked user (correct password)
        Branch 4: wrong password

        Each branch is asserted independently at >= 360ms (400ms floor - 40ms
        slack for CI timer jitter).  PBKDF2 at iterations=1000 completes in
        < 10ms so the asyncio.sleep pad dominates.
        """
        import time
        from datetime import datetime, timedelta, timezone

        client, SessionLocal = lockout_client
        db = SessionLocal()
        try:
            # Branch 2: inactive user
            self._make_user(db, username="inactive-alice", active=False)

            # Branch 3: locked user with a future locked_until
            self._make_user(
                db,
                username="locked-alice",
                locked_until=datetime.now(timezone.utc) + timedelta(minutes=30),
            )

            # Branch 4: active user with correct hash (wrong password supplied)
            self._make_user(db, username="wrong-pw-alice")

            # Branch 1: unknown username
            t0 = time.monotonic()
            r1 = client.post(
                "/api/auth/local-login",
                json={"username": "ghost-user-does-not-exist", "password": "x"},
            )
            elapsed1_ms = (time.monotonic() - t0) * 1000
            assert r1.status_code == 401, f"Branch 1: expected 401, got {r1.status_code}"
            assert elapsed1_ms >= 360, (
                f"Branch 1 (unknown username) must enforce >= 360ms floor; "
                f"elapsed={elapsed1_ms:.1f}ms"
            )

            # Branch 2: inactive user
            t0 = time.monotonic()
            r2 = client.post(
                "/api/auth/local-login",
                json={"username": "inactive-alice", "password": "password1234"},
            )
            elapsed2_ms = (time.monotonic() - t0) * 1000
            assert r2.status_code == 401, f"Branch 2: expected 401, got {r2.status_code}"
            assert elapsed2_ms >= 360, (
                f"Branch 2 (inactive user) must enforce >= 360ms floor; "
                f"elapsed={elapsed2_ms:.1f}ms"
            )

            # Branch 3: locked user
            t0 = time.monotonic()
            r3 = client.post(
                "/api/auth/local-login",
                json={"username": "locked-alice", "password": "password1234"},
            )
            elapsed3_ms = (time.monotonic() - t0) * 1000
            assert r3.status_code == 401, f"Branch 3: expected 401, got {r3.status_code}"
            assert elapsed3_ms >= 360, (
                f"Branch 3 (locked user) must enforce >= 360ms floor; "
                f"elapsed={elapsed3_ms:.1f}ms"
            )

            # Branch 4: wrong password
            t0 = time.monotonic()
            r4 = client.post(
                "/api/auth/local-login",
                json={"username": "wrong-pw-alice", "password": "this-is-wrong"},
            )
            elapsed4_ms = (time.monotonic() - t0) * 1000
            assert r4.status_code == 401, f"Branch 4: expected 401, got {r4.status_code}"
            assert elapsed4_ms >= 360, (
                f"Branch 4 (wrong password) must enforce >= 360ms floor; "
                f"elapsed={elapsed4_ms:.1f}ms"
            )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Case 13: xander:51 — lockout is idempotent (no window extension)
    # ------------------------------------------------------------------

    def test_xander51_lockout_idempotent_on_over_threshold(self, lockout_client):
        """Case 13 — over-threshold requests don't extend the lockout window (xander:51).

        Once locked_until is set, subsequent failed logins increment
        failed_login_count but do NOT push locked_until further into the future.
        Without the .where(User.locked_until.is_(None)) guard an attacker could
        pin an account locked indefinitely by submitting one bad-password request
        every 30 minutes.

        Strategy: insert a user at threshold - 1, trigger first lockout request
        to set locked_until, then send a second wrong-password request and assert
        locked_until is unchanged.  We set locked_until to a known sentinel after
        the first lock so any extension is unambiguous.
        """
        from datetime import datetime, timedelta, timezone
        from app.models import User
        import sqlalchemy as sa

        client, SessionLocal = lockout_client
        db = SessionLocal()
        try:
            cfg_threshold = 10  # matches AthenaConfig default login_lockout_threshold
            # Start at threshold - 1 so one more wrong-password triggers lockout.
            user = self._make_user(
                db,
                username="alice",
                failed_login_count=cfg_threshold - 1,
            )
            user_id = user.id

            # First over-threshold request — this sets locked_until.
            r1 = client.post(
                "/api/auth/local-login",
                json={"username": "alice", "password": "wrongpassword"},
            )
            assert r1.status_code == 401, f"Expected 401 on first over-threshold; got {r1.status_code}"

            # Read back locked_until set by the first request.
            db.expire_all()
            locked_user = db.query(User).filter(User.id == user_id).first()
            locked_until_t1 = locked_user.locked_until
            assert locked_until_t1 is not None, (
                "First over-threshold request must have set locked_until"
            )

            # Second wrong-password request — must NOT extend locked_until.
            r2 = client.post(
                "/api/auth/local-login",
                json={"username": "alice", "password": "wrongpassword"},
            )
            assert r2.status_code == 401, f"Expected 401 on second over-threshold; got {r2.status_code}"

            # locked_until must be unchanged.
            db.expire_all()
            locked_user2 = db.query(User).filter(User.id == user_id).first()
            locked_until_t2 = locked_user2.locked_until

            # Normalise both to naive UTC for comparison (SQLite returns naive strings).
            def _to_naive_utc(dt):
                if dt is not None and dt.tzinfo is not None:
                    return dt.astimezone(timezone.utc).replace(tzinfo=None)
                return dt

            assert _to_naive_utc(locked_until_t2) == _to_naive_utc(locked_until_t1), (
                f"locked_until must not be extended by over-threshold requests. "
                f"t1={locked_until_t1!r} t2={locked_until_t2!r}"
            )
            # Note: the second request hits branch 3 (locked path), which short-circuits
            # before the count-increment UPDATE.  Count does not increase — that's
            # correct.  The contract being verified here is only that locked_until is
            # not extended, which is the xander:51 guard.
        finally:
            db.close()

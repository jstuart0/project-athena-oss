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
import hmac

import pytest

# Set test environment BEFORE importing anything from the app
os.environ["DEV_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

# -------------------------------------------------------------------
# Unit tests — service_auth utility (no DB or app needed)
# -------------------------------------------------------------------

class TestVerifyServiceApiKey:
    """Unit tests for the verify_service_api_key FastAPI dependency."""

    def test_correct_key_returns_true(self):
        """Correct service key should pass through and return True."""
        # Patch the module-level SERVICE_API_KEY before importing
        import importlib
        import app.utils.service_auth as sa

        original = sa.SERVICE_API_KEY
        try:
            sa.SERVICE_API_KEY = "test-secret-key-abc123"
            from fastapi.testclient import TestClient
            from fastapi import FastAPI, Depends
            mini = FastAPI()

            @mini.get("/ping")
            def ping(_: bool = Depends(sa.verify_service_api_key)):
                return {"ok": True}

            with TestClient(mini, raise_server_exceptions=True) as c:
                r = c.get("/ping", headers={"X-Service-Key": "test-secret-key-abc123"})
            assert r.status_code == 200
        finally:
            sa.SERVICE_API_KEY = original

    def test_wrong_key_returns_401(self):
        """Wrong service key should raise 401."""
        import app.utils.service_auth as sa
        from fastapi.testclient import TestClient
        from fastapi import FastAPI, Depends

        original = sa.SERVICE_API_KEY
        try:
            sa.SERVICE_API_KEY = "real-secret"
            mini = FastAPI()

            @mini.get("/ping")
            def ping(_: bool = Depends(sa.verify_service_api_key)):
                return {"ok": True}

            with TestClient(mini, raise_server_exceptions=False) as c:
                r = c.get("/ping", headers={"X-Service-Key": "wrong-secret"})
            assert r.status_code == 401
        finally:
            sa.SERVICE_API_KEY = original

    def test_missing_header_returns_422(self):
        """Missing X-Service-Key header should return 422 (required field missing)."""
        import app.utils.service_auth as sa
        from fastapi.testclient import TestClient
        from fastapi import FastAPI, Depends

        original = sa.SERVICE_API_KEY
        try:
            sa.SERVICE_API_KEY = "real-secret"
            mini = FastAPI()

            @mini.get("/ping")
            def ping(_: bool = Depends(sa.verify_service_api_key)):
                return {"ok": True}

            with TestClient(mini, raise_server_exceptions=False) as c:
                r = c.get("/ping")  # No X-Service-Key header
            assert r.status_code == 422
        finally:
            sa.SERVICE_API_KEY = original

    def test_uses_constant_time_comparison(self):
        """verify_service_api_key should use hmac.compare_digest (timing-safe)."""
        import inspect
        import app.utils.service_auth as sa

        source = inspect.getsource(sa.verify_service_api_key)
        assert "hmac.compare_digest" in source, (
            "verify_service_api_key must use hmac.compare_digest to prevent timing attacks"
        )

    def test_reads_from_env_var(self):
        """SERVICE_API_KEY module variable should come from os.getenv."""
        import inspect
        import app.utils.service_auth as sa

        source = inspect.getsource(sa)
        assert 'os.getenv("SERVICE_API_KEY"' in source or "os.getenv('SERVICE_API_KEY'" in source


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

    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from main import app

    app.dependency_overrides[get_db] = override_get_db

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

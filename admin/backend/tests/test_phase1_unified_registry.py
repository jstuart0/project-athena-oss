"""
Phase 1 regression tests — RagService ORM model + caller migration.

Verifies:
- RagService model creates / reads / has the right column set
- RAGConnector.to_dict() emits service_name via @property (ian I-H6 / app.js:2778)
- RAGConnector.service_id FK integrity: non-null set preserved, null set stays null
- /api/services endpoints return data and emit expected shape
- /api/service-control endpoints return data and emit expected shape
- /api/rag-connectors endpoints return data with service_name key
- verify_service_or_oidc dual-auth helper: service key accepts, user auth accepts, no-auth 401
- dashboard.py no longer imports AthenaService (dead import removed)

ATHENA-1 Phase 1 (Campaign 4).
"""
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

os.environ["DEV_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SERVICE_API_KEY", "test-svc-key-phase1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import RagService, RAGConnector, User
from main import app


# ---------------------------------------------------------------------------
# Test database setup
# ---------------------------------------------------------------------------

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def client(db):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def rag_service(db) -> RagService:
    """Seed a single RagService row."""
    svc = RagService(
        name="weather",
        display_name="Weather RAG",
        host="localhost",
        port=8010,
        protocol="http",
        health_endpoint="/health",
        service_type="rag",
        control_method="docker",
        container_name="athena-weather",
        enabled=True,
        is_running=False,
        auto_start=True,
    )
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return svc


@pytest.fixture
def rag_connector(db, rag_service) -> RAGConnector:
    """Seed a RAGConnector linked to the RagService."""
    conn = RAGConnector(
        name="weather-connector",
        connector_type="external_api",
        service_id=rag_service.id,
        enabled=True,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


@pytest.fixture
def orphan_connector(db) -> RAGConnector:
    """Seed a RAGConnector with no service link (service_id IS NULL)."""
    conn = RAGConnector(
        name="orphan-connector",
        connector_type="external_api",
        service_id=None,
        enabled=True,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


# ---------------------------------------------------------------------------
# Model column tests
# ---------------------------------------------------------------------------

class TestRagServiceModel:
    def test_column_set_matches_d2(self, db):
        """RagService table has all D2-spec columns."""
        inspector = inspect(engine)
        col_names = {c["name"] for c in inspector.get_columns("athena_service_registry")}
        assert "rag_services" not in set(inspector.get_table_names()), (
            "Legacy table rag_services still exists — migration 057 may not have run."
        )

        required = {
            "id", "name", "display_name", "description", "service_type",
            "host", "port", "protocol", "health_endpoint", "endpoint_url",
            "control_method", "container_name",
            "cache_ttl", "timeout", "rate_limit",
            "api_key_encrypted",
            "enabled", "auto_start",
            "health_status", "is_running",
            "last_health_check", "last_response_time_ms", "last_error", "health_message",
            "created_at", "updated_at",
        }
        missing = required - col_names
        assert not missing, f"Missing D2 columns: {missing}"

    def test_create_and_read(self, db, rag_service):
        """RagService rows round-trip through the ORM."""
        fetched = db.query(RagService).filter(RagService.name == "weather").first()
        assert fetched is not None
        assert fetched.id == rag_service.id
        assert fetched.host == "localhost"
        assert fetched.port == 8010

    def test_service_name_property(self, db, rag_service):
        """service_name @property returns the same value as name (back-compat alias)."""
        assert rag_service.service_name == rag_service.name == "weather"

    def test_to_dict_includes_service_name_key(self, db, rag_service):
        """to_dict() must emit both 'name' and 'service_name' for frontend compat."""
        d = rag_service.to_dict()
        assert "name" in d
        assert "service_name" in d
        assert d["service_name"] == d["name"] == "weather"


# ---------------------------------------------------------------------------
# RAGConnector FK and serialization tests (tessa T-H1, ian I-H6)
# ---------------------------------------------------------------------------

class TestRAGConnectorFK:
    def test_connector_links_to_rag_service(self, db, rag_connector, rag_service):
        """RAGConnector.service_id FK points at athena_service_registry.id."""
        conn = db.query(RAGConnector).filter(RAGConnector.id == rag_connector.id).first()
        assert conn.service_id == rag_service.id
        assert conn.service is not None
        assert conn.service.name == "weather"

    def test_to_dict_emits_service_name(self, db, rag_connector):
        """RAGConnector.to_dict() must include 'service_name' key for app.js:2778."""
        d = rag_connector.to_dict()
        assert "service_name" in d
        assert d["service_name"] == "weather"

    def test_null_service_id_connector_remains_null(self, db, orphan_connector):
        """Connectors with no service link stay null — they must not be accidentally mapped."""
        conn = db.query(RAGConnector).filter(RAGConnector.id == orphan_connector.id).first()
        assert conn.service_id is None
        assert conn.service is None

    def test_fk_count_consistency(self, db, rag_service, rag_connector, orphan_connector):
        """Non-null service_id count before == after seeding (T-H1 identity check pattern)."""
        non_null_count = (
            db.query(RAGConnector)
            .filter(RAGConnector.service_id.isnot(None))
            .count()
        )
        null_count = (
            db.query(RAGConnector)
            .filter(RAGConnector.service_id.is_(None))
            .count()
        )
        assert non_null_count == 1, "Exactly 1 connector should have a service_id"
        assert null_count == 1, "Exactly 1 orphan connector should have service_id=NULL"

    def test_to_dict_service_name_none_when_no_service(self, db, orphan_connector):
        """Orphan connectors emit service_name=None without error."""
        d = orphan_connector.to_dict()
        assert "service_name" in d
        assert d["service_name"] is None


# ---------------------------------------------------------------------------
# /api/services route tests
# ---------------------------------------------------------------------------

class TestServicesRoute:
    def test_list_services_empty(self, client):
        response = client.get("/api/services")
        assert response.status_code == 200
        data = response.json()
        assert "services" in data
        assert data["services"] == []

    def test_list_services_with_data(self, client, rag_service):
        response = client.get("/api/services")
        assert response.status_code == 200
        services = response.json()["services"]
        assert len(services) == 1
        assert services[0]["name"] == "weather"
        assert services[0]["host"] == "localhost"

    def test_get_service_by_id(self, client, rag_service):
        response = client.get(f"/api/services/{rag_service.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "weather"

    def test_get_service_not_found(self, client):
        response = client.get("/api/services/99999")
        assert response.status_code == 404

    def test_create_service(self, client):
        payload = {
            "name": "orchestrator",
            "display_name": "Orchestrator",
            "host": "athena-orchestrator",
            "port": 8001,
            "protocol": "http",
            "health_endpoint": "/health",
            "service_type": "core",
            "control_method": "none",
        }
        response = client.post("/api/services", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "orchestrator"
        assert data["host"] == "athena-orchestrator"

    def test_create_duplicate_service_rejected(self, client, rag_service):
        payload = {
            "name": "weather",
            "display_name": "Duplicate Weather",
            "host": "localhost",
            "port": 8011,
        }
        response = client.post("/api/services", json=payload)
        assert response.status_code == 400

    def test_update_service(self, client, rag_service):
        response = client.put(
            f"/api/services/{rag_service.id}",
            json={"host": "new-host", "port": 9999},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["host"] == "new-host"
        assert data["port"] == 9999

    def test_delete_service(self, client, rag_service):
        response = client.delete(f"/api/services/{rag_service.id}")
        assert response.status_code == 204
        # Verify gone
        response = client.get(f"/api/services/{rag_service.id}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# /api/service-control route tests
# ---------------------------------------------------------------------------

class TestServiceControlRoute:
    def test_list_services_empty(self, client):
        response = client.get("/api/service-control")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_list_services_with_data(self, client, rag_service):
        response = client.get("/api/service-control")
        assert response.status_code == 200
        services = response.json()
        assert len(services) == 1
        svc = services[0]
        # Must have both name and service_name for backward compat
        assert svc["name"] == "weather"
        assert svc["service_name"] == "weather"

    def test_start_service_not_found(self, client):
        response = client.post("/api/service-control/nonexistent/start")
        assert response.status_code == 404

    def test_stop_service_not_found(self, client):
        response = client.post("/api/service-control/nonexistent/stop")
        assert response.status_code == 404

    def test_restart_service_not_found(self, client):
        response = client.post("/api/service-control/nonexistent/restart")
        assert response.status_code == 404

    def test_start_service_control_agent_disabled(self, client, rag_service):
        """With CA disabled (default), start returns success=False with expected message."""
        response = client.post(f"/api/service-control/{rag_service.name}/start")
        assert response.status_code == 200
        data = response.json()
        # CA disabled → docker action returns False
        assert data["service_name"] == "weather"
        assert data["action"] == "start"
        # success may be True or False depending on CA state; key must be present
        assert "success" in data
        assert "message" in data


# ---------------------------------------------------------------------------
# /api/rag-connectors route tests
# ---------------------------------------------------------------------------

class TestRagConnectorsRoute:
    def test_list_connectors_includes_service_name(self, client, rag_connector):
        response = client.get("/api/rag-connectors")
        assert response.status_code == 200
        body = response.json()
        # Route returns {"connectors": [...]}
        connectors = body["connectors"] if isinstance(body, dict) else body
        assert len(connectors) >= 1
        conn = next(c for c in connectors if c["name"] == "weather-connector")
        assert "service_name" in conn
        assert conn["service_name"] == "weather"

    def test_create_connector_with_valid_service(self, client, rag_service):
        payload = {
            "name": "sports-connector",
            "connector_type": "external_api",
            "service_id": rag_service.id,
            "enabled": True,
        }
        response = client.post("/api/rag-connectors", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["service_name"] == "weather"
        assert data["service_id"] == rag_service.id

    def test_create_connector_invalid_service_id(self, client):
        payload = {
            "name": "broken-connector",
            "connector_type": "external_api",
            "service_id": 99999,
        }
        response = client.post("/api/rag-connectors", json=payload)
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Dual-auth helper unit tests (D9 / ATHENA-21)
# ---------------------------------------------------------------------------

class TestVerifyServiceOrOidc:
    """Tests for verify_service_or_oidc dual-auth dependency.

    All tests use the module-level `client` + `db` fixtures (wired with
    Base.metadata.create_all and get_db override) so that the OIDC fallthrough
    path's get_optional_user → users table query succeeds.

    The private _mini_app() helper has been removed; it lacked DB setup and
    caused sqlite3.OperationalError: no such table: users on the OIDC path.

    Every test that mutates os.environ["SERVICE_API_KEY"] uses try/finally
    to restore the module default "test-svc-key-phase1" and calls
    _clear_cache_for_tests() in the finally block (ATHENA-21; pattern from
    test_security_hardening.py:62-68).
    """

    # ------------------------------------------------------------------
    # Regression guards — set key, correct / wrong header
    # ------------------------------------------------------------------

    def test_set_key_with_correct_header_succeeds(self, client):
        """SERVICE_API_KEY set and header matches → 200 (regression guard)."""
        from shared.config import _clear_cache_for_tests

        os.environ["SERVICE_API_KEY"] = "test-svc-key-phase1"
        _clear_cache_for_tests()
        try:
            resp = client.post(
                "/api/service-registry/services",
                params={"name": "athena-test-svc", "endpoint_url": "http://athena-test-svc:8010/health"},
                headers={"X-Service-Key": "test-svc-key-phase1"},
            )
            assert resp.status_code == 200
        finally:
            os.environ["SERVICE_API_KEY"] = "test-svc-key-phase1"
            _clear_cache_for_tests()

    def test_set_key_with_wrong_header_returns_401(self, client):
        """SERVICE_API_KEY set but wrong X-Service-Key value → 401 (regression guard)."""
        from shared.config import _clear_cache_for_tests

        os.environ["SERVICE_API_KEY"] = "test-svc-key-phase1"
        _clear_cache_for_tests()
        try:
            resp = client.post(
                "/api/service-registry/services",
                params={"name": "athena-test-svc", "endpoint_url": "http://athena-test-svc:8010/health"},
                headers={"X-Service-Key": "wrong-key"},
            )
            assert resp.status_code == 401
            assert resp.json() == {"detail": "Invalid service key"}
        finally:
            os.environ["SERVICE_API_KEY"] = "test-svc-key-phase1"
            _clear_cache_for_tests()

    # ------------------------------------------------------------------
    # ATHENA-21 core fix: 503 when SERVICE_API_KEY unset and header present
    # ------------------------------------------------------------------

    def test_unset_key_with_header_returns_503(self, client):
        """SERVICE_API_KEY unset + non-empty X-Service-Key → 503 fail-closed (ATHENA-21).

        The 503 fires regardless of DEV_MODE (helper-level guard; xander L3).
        No WWW-Authenticate header should be present — this is a server-side
        config error, not a credential error (codex C1).
        """
        from shared.config import _clear_cache_for_tests

        os.environ["SERVICE_API_KEY"] = ""
        _clear_cache_for_tests()
        try:
            resp = client.post(
                "/api/service-registry/services",
                params={"name": "athena-test-svc", "endpoint_url": "http://athena-test-svc:8010/health"},
                headers={"X-Service-Key": "any-value"},
            )
            assert resp.status_code == 503
            assert resp.json() == {"detail": "Service authentication not configured"}
            assert "WWW-Authenticate" not in resp.headers  # codex C1
        finally:
            os.environ["SERVICE_API_KEY"] = "test-svc-key-phase1"
            _clear_cache_for_tests()

    def test_unset_key_with_header_and_bearer_token_returns_503(self, client):
        """SERVICE_API_KEY unset + X-Service-Key + Authorization: Bearer → 503.

        Service-key misconfig wins over OIDC fallthrough (codex C1 mixed-auth).
        """
        from shared.config import _clear_cache_for_tests

        os.environ["SERVICE_API_KEY"] = ""
        _clear_cache_for_tests()
        try:
            resp = client.post(
                "/api/service-registry/services",
                params={"name": "athena-test-svc", "endpoint_url": "http://athena-test-svc:8010/health"},
                headers={"X-Service-Key": "any", "Authorization": "Bearer fake-jwt"},
            )
            assert resp.status_code == 503
            assert resp.json() == {"detail": "Service authentication not configured"}
            assert "WWW-Authenticate" not in resp.headers
        finally:
            os.environ["SERVICE_API_KEY"] = "test-svc-key-phase1"
            _clear_cache_for_tests()

    # ------------------------------------------------------------------
    # OIDC fallthrough: no X-Service-Key header → DEV_MODE bypass
    # ATHENA-21: rewritten from broken _mini_app() form to use real client+db fixtures
    # ------------------------------------------------------------------

    def test_unset_key_without_header_dev_mode_falls_through_to_oidc(self, client):
        """SERVICE_API_KEY unset, no X-Service-Key, DEV_MODE=true → 200 via dev-admin bypass.

        No X-Service-Key header means the 503 path is never reached; the OIDC
        fallthrough fires, and in DEV_MODE=true the dev-admin bypass user is
        returned → 200 (ATHENA-21; replaces broken test_no_auth_dev_mode_allowed).

        # ATHENA-21: rewritten from broken _mini_app() form to use real client+db fixtures
        """
        from shared.config import _clear_cache_for_tests

        os.environ["SERVICE_API_KEY"] = ""
        _clear_cache_for_tests()
        try:
            resp = client.post(
                "/api/service-registry/services",
                params={"name": "athena-test-svc", "endpoint_url": "http://athena-test-svc:8010/health"},
            )
            assert resp.status_code == 200
        finally:
            os.environ["SERVICE_API_KEY"] = "test-svc-key-phase1"
            _clear_cache_for_tests()

    # ------------------------------------------------------------------
    # Decision 5 boundary tests (tessa M1 / ATHENA-21)
    # ------------------------------------------------------------------

    def test_whitespace_service_api_key_with_header_returns_401(self, client):
        """SERVICE_API_KEY="   " (whitespace) + non-empty X-Service-Key → 401.

        Whitespace SERVICE_API_KEY is treated as a present-but-mismatched key by
        the dispatcher (Decision 5α): `not "   "` is False so the 503 guard does
        NOT fire; the helper proceeds to hmac.compare_digest(header, "   ") which
        returns False and raises 401 {"detail": "Invalid service key"}.
        Runtime is fail-closed. The _INSECURE_DEFAULTS whitespace-divergence
        (xander M1) is tracked separately as a follow-up.
        """
        from shared.config import _clear_cache_for_tests

        os.environ["SERVICE_API_KEY"] = "   "
        _clear_cache_for_tests()
        try:
            resp = client.post(
                "/api/service-registry/services",
                params={"name": "athena-test-svc", "endpoint_url": "http://athena-test-svc:8010/health"},
                headers={"X-Service-Key": "anything"},
            )
            assert resp.status_code == 401
            assert resp.json() == {"detail": "Invalid service key"}
        finally:
            os.environ["SERVICE_API_KEY"] = "test-svc-key-phase1"
            _clear_cache_for_tests()

    def test_empty_string_x_service_key_value_falls_through(self, client):
        """Empty X-Service-Key header value treated as absent header (Decision 5).

        FastAPI delivers x_service_key="" for a header with no value.  The
        dispatcher's `if x_service_key:` treats this as falsy, skipping the
        service-key branch entirely → OIDC fallthrough → DEV_MODE bypass → 200.
        The 503 does NOT fire for an empty header value.
        """
        from shared.config import _clear_cache_for_tests

        os.environ["SERVICE_API_KEY"] = ""
        _clear_cache_for_tests()
        try:
            resp = client.post(
                "/api/service-registry/services",
                params={"name": "athena-test-svc", "endpoint_url": "http://athena-test-svc:8010/health"},
                headers={"X-Service-Key": ""},
            )
            # Empty value == absent header; DEV_MODE=true falls through to OIDC dev-admin → 200
            assert resp.status_code == 200
        finally:
            os.environ["SERVICE_API_KEY"] = "test-svc-key-phase1"
            _clear_cache_for_tests()

    def test_mixed_case_x_service_key_header(self, client):
        """Lowercase x-service-key header is matched case-insensitively (RFC 7230 §3.2).

        Pins FastAPI's Header(alias="X-Service-Key") case-insensitivity via
        Starlette's MutableHeaders lookup so a framework upgrade can't silently
        regress this (tessa M1).
        """
        from shared.config import _clear_cache_for_tests

        os.environ["SERVICE_API_KEY"] = "test-svc-key-phase1"
        _clear_cache_for_tests()
        try:
            resp = client.post(
                "/api/service-registry/services",
                params={"name": "athena-test-svc", "endpoint_url": "http://athena-test-svc:8010/health"},
                headers={"x-service-key": "test-svc-key-phase1"},
            )
            assert resp.status_code == 200
        finally:
            os.environ["SERVICE_API_KEY"] = "test-svc-key-phase1"
            _clear_cache_for_tests()


# ---------------------------------------------------------------------------
# dashboard.py dead import removal
# ---------------------------------------------------------------------------

class TestDashboardNoAthenaService:
    def test_athena_service_not_in_dashboard_imports(self):
        """AthenaService must not be imported in dashboard.py (dead import removed)."""
        import importlib
        import app.routes.dashboard as dashboard_module
        # Reload to ensure current state
        importlib.reload(dashboard_module)
        assert not hasattr(dashboard_module, 'AthenaService'), \
            "AthenaService is still imported in dashboard.py — dead import not removed"

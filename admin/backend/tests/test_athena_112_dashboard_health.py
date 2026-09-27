"""
ATHENA-112 — dashboard overall_health / counts must be computed over ENABLED
service-registry rows only, so an intentionally disabled row cannot drag the
dashboard into 'degraded' or 'unhealthy'.

Covers:
- GET /api/service-registry/services: healthy_services / overall_health are
  computed over enabled rows only.
- New enabled_services / disabled_services fields; total_services keeps
  counting every row (backward compatibility).
- A disabled row's health_status in the response is always the literal
  string 'disabled', regardless of its last cached poller value.
"""
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

_SERVICE_KEY = "test-service-key-athena-112"

os.environ["DEV_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SERVICE_API_KEY"] = _SERVICE_KEY
os.environ.setdefault("CONTROL_AGENT_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import RagService
from app.routes.service_registry import _overall_health
from main import app

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def _reset_config_cache():
    from shared.config import get_config
    os.environ["SERVICE_API_KEY"] = _SERVICE_KEY
    os.environ["DEV_MODE"] = "true"
    get_config.cache_clear()
    yield
    get_config.cache_clear()


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
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _svc(db, name, *, enabled, health_status, host="127.0.0.1", port=8000):
    svc = RagService(
        name=name,
        display_name=name.replace('-', ' ').title(),
        host=host,
        port=port,
        protocol="http",
        health_endpoint="/health",
        service_type="rag",
        enabled=enabled,
        health_status=health_status,
    )
    db.add(svc)
    return svc


class TestOverallHealthEnabledOnly:
    """3 enabled healthy + 2 disabled unhealthy must read overall_health='healthy'."""

    def test_disabled_unhealthy_rows_do_not_degrade_overall_health(self, client, db):
        _svc(db, "enabled-a", enabled=True, health_status="healthy", port=8001)
        _svc(db, "enabled-b", enabled=True, health_status="healthy", port=8002)
        _svc(db, "enabled-c", enabled=True, health_status="healthy", port=8003)
        _svc(db, "disabled-a", enabled=False, health_status="unhealthy", port=8004)
        _svc(db, "disabled-b", enabled=False, health_status="unhealthy", port=8005)
        db.commit()

        resp = client.get('/api/service-registry/services')
        assert resp.status_code == 200
        data = resp.json()

        assert data['overall_health'] == 'healthy'
        assert data['healthy_services'] == 3
        assert data['enabled_services'] == 3
        assert data['disabled_services'] == 2
        assert data['total_services'] == 5

    def test_one_enabled_unhealthy_among_healthy_reads_degraded(self, client, db):
        _svc(db, "enabled-a", enabled=True, health_status="healthy", port=8001)
        _svc(db, "enabled-b", enabled=True, health_status="healthy", port=8002)
        _svc(db, "enabled-c", enabled=True, health_status="unhealthy", port=8003)
        _svc(db, "disabled-a", enabled=False, health_status="unhealthy", port=8004)
        _svc(db, "disabled-b", enabled=False, health_status="unhealthy", port=8005)
        db.commit()

        resp = client.get('/api/service-registry/services')
        assert resp.status_code == 200
        data = resp.json()

        assert data['overall_health'] == 'degraded'
        assert data['healthy_services'] == 2
        assert data['enabled_services'] == 3
        assert data['disabled_services'] == 2
        assert data['total_services'] == 5

    def test_disabled_row_health_status_reported_as_disabled(self, client, db):
        """A disabled row's stale cached health_status must never leak through --
        the response always reports 'disabled' for a disabled row, regardless
        of what the poller last wrote before it was turned off."""
        _svc(db, "stale-healthy-but-disabled", enabled=False, health_status="healthy", port=8006)
        db.commit()

        resp = client.get('/api/service-registry/services')
        assert resp.status_code == 200
        data = resp.json()
        row = next(s for s in data['services'] if s['name'] == 'stale-healthy-but-disabled')
        assert row['health_status'] == 'disabled'

    def test_all_disabled_reads_unknown_not_healthy(self, client, db):
        """An empty enabled set must read 'unknown', not silently 'healthy'."""
        _svc(db, "disabled-only", enabled=False, health_status="healthy", port=8007)
        db.commit()

        resp = client.get('/api/service-registry/services')
        data = resp.json()
        assert data['overall_health'] == 'unknown'
        assert data['enabled_services'] == 0
        assert data['disabled_services'] == 1


class TestOverallHealthUnitFunction:
    """_overall_health itself is a pure function over whatever list it's given --
    covered directly to pin the averaging behavior independent of the route."""

    def test_empty_list_is_unknown(self):
        assert _overall_health([]) == 'unknown'

    def test_all_healthy_is_healthy(self):
        assert _overall_health([{'health_status': 'healthy'}] * 3) == 'healthy'

    def test_some_healthy_is_degraded(self):
        rows = [{'health_status': 'healthy'}, {'health_status': 'unhealthy'}]
        assert _overall_health(rows) == 'degraded'

    def test_none_healthy_is_unhealthy(self):
        rows = [{'health_status': 'unhealthy'}, {'health_status': 'pending'}]
        assert _overall_health(rows) == 'unhealthy'


class TestGetSingleServiceDisabledOverride:
    """GET /services/{service_name} must apply the same disabled-row override
    as GET /services -- a disabled row always reports health_status='disabled',
    never a stale cached value from before it was turned off. (codex diff
    review, LOW)."""

    def test_disabled_row_reports_disabled_via_single_service_endpoint(self, client, db):
        _svc(db, "single-disabled-svc", enabled=False, health_status="healthy", port=8010)
        db.commit()

        resp = client.get('/api/service-registry/services/single-disabled-svc')
        assert resp.status_code == 200
        assert resp.json()['health_status'] == 'disabled'

    def test_enabled_row_with_null_status_reports_pending_via_single_service_endpoint(self, client, db):
        svc = _svc(db, "single-pending-svc", enabled=True, health_status=None, port=8011)
        db.commit()

        resp = client.get('/api/service-registry/services/single-pending-svc')
        assert resp.status_code == 200
        assert resp.json()['health_status'] == 'pending'


class TestRegisterServicePartialUpdate:
    """POST /api/service-registry/services on an EXISTING row must be a
    partial update: fields omitted from the request are preserved, not reset
    to defaults. Motivated by the admin UI's row editor calling this same
    upsert route to change just protocol/host/port/display_name -- before
    this fix, editing a row silently reset cache_ttl/timeout/rate_limit to
    defaults and force-re-enabled a disabled row. (codex diff review, HIGH)"""

    def _register(self, client, **params):
        return client.post(
            '/api/service-registry/services',
            params=params,
            headers={'X-Service-Key': _SERVICE_KEY},
        )

    def test_edit_on_disabled_row_without_enabled_param_leaves_it_disabled(self, client, db):
        svc = _svc(
            db, "partial-update-svc", enabled=False, health_status="unhealthy",
            host="192.168.1.60", port=9200,
        )
        svc.cache_ttl = 111
        svc.timeout = 2222
        svc.rate_limit = 7
        db.commit()

        resp = self._register(
            client,
            name="partial-update-svc",
            protocol="tcp",
            host="192.168.1.61",
            port=9201,
            display_name="Renamed Service",
        )
        assert resp.status_code == 200, resp.text

        db.expire_all()
        row = db.query(RagService).filter(RagService.name == "partial-update-svc").first()
        assert row.enabled is False, "omitting enabled must never implicitly re-enable a disabled row"
        assert row.cache_ttl == 111, "cache_ttl must be preserved when omitted"
        assert row.timeout == 2222, "timeout must be preserved when omitted"
        assert row.rate_limit == 7, "rate_limit must be preserved when omitted"
        # The fields actually sent DO apply.
        assert row.display_name == "Renamed Service"
        assert row.protocol == "tcp"
        assert row.host == "192.168.1.61"
        assert row.port == 9201

    def test_explicit_enabled_false_does_disable(self, client, db):
        """The partial-update fix must not make `enabled` unsettable -- an
        explicit enabled=false still disables the row."""
        _svc(db, "explicit-disable-svc", enabled=True, health_status="healthy", port=9210)
        db.commit()

        resp = self._register(
            client,
            name="explicit-disable-svc",
            protocol="http",
            endpoint_url="http://192.168.1.62:9210/health",
            enabled=False,
        )
        assert resp.status_code == 200, resp.text

        db.expire_all()
        row = db.query(RagService).filter(RagService.name == "explicit-disable-svc").first()
        assert row.enabled is False

    def test_insert_still_applies_documented_defaults(self, client, db):
        """A brand-new row created without service_type/cache_ttl/timeout/
        rate_limit/enabled must still get the documented defaults, not None."""
        resp = self._register(
            client,
            name="fresh-insert-svc",
            protocol="tcp",
            host="192.168.1.63",
            port=9220,
        )
        assert resp.status_code == 200, resp.text

        row = db.query(RagService).filter(RagService.name == "fresh-insert-svc").first()
        assert row.service_type == 'api'
        assert row.cache_ttl == 300
        assert row.timeout == 5000
        assert row.rate_limit == 100
        assert row.enabled is True

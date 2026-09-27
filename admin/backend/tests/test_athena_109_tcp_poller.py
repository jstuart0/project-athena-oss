"""
ATHENA-109 — TCP health-check mode for service-registry rows.

Covers:
- _tcp_check: real ephemeral listener (healthy), a closed port (refused),
  and a mocked-hang connect (timeout).
- _poll_one(protocol='tcp'): SSRF allowlist semantics identical to the HTTP
  path (blocked without allowlist, allowed with it) -- no banner read.
- _poll_all_services: a tcp-protocol row gets health_status written by the
  full poll cycle, same as an http row.
- POST /api/service-registry/services upsert with protocol='tcp': stores
  host/port/protocol without requiring (or storing) an endpoint_url, and
  rejects missing host/out-of-range port.
"""
import asyncio
import contextlib
import os
import socket
import sys
import unittest.mock as mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

_SERVICE_KEY = "test-service-key-athena-109"

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


def _free_closed_port() -> int:
    """Reserve then release a port so nothing is listening on it.

    SO_REUSEADDR is left off (Python's default) so the OS doesn't hand this
    exact port back out from TIME_WAIT sooner than it otherwise would. There
    is an inherent (tiny) TOCTOU window between close() here and the caller's
    connect attempt where some other process could grab the same ephemeral
    port; callers that need certainty should retry once on an unexpected
    result rather than treat a single connect as authoritative (codex diff
    review, LOW)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _noop_handler(reader, writer):
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


# ---------------------------------------------------------------------------
# _tcp_check — the raw socket primitive
# ---------------------------------------------------------------------------

class TestTcpCheckPrimitive:
    @pytest.mark.asyncio
    async def test_healthy_on_real_listener(self):
        from app.services.health_poller import _tcp_check

        server = await asyncio.start_server(_noop_handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        try:
            result = await _tcp_check(1, '127.0.0.1', port, timeout_seconds=2.0)
        finally:
            server.close()
            await server.wait_closed()

        svc_id, status, elapsed_ms, category, detail, health_message = result
        assert status == 'healthy'
        assert category == 'ok'
        assert elapsed_ms is not None
        assert health_message is None

    @pytest.mark.asyncio
    async def test_refused_on_closed_port(self):
        from app.services.health_poller import _tcp_check

        # Retry once on an unexpected result: there's a tiny TOCTOU window
        # between reserving+releasing the port and this connect attempt where
        # some other process could grab it (codex diff review, LOW).
        for attempt in range(2):
            port = _free_closed_port()
            result = await _tcp_check(1, '127.0.0.1', port, timeout_seconds=2.0)
            if result[3] == 'tcp_refused':
                break
        svc_id, status, elapsed_ms, category, detail, health_message = result
        assert status == 'unhealthy'
        assert category == 'tcp_refused'

    @pytest.mark.asyncio
    async def test_redis_ping_pong_is_healthy(self):
        """verify_redis=True: a listener that replies +PONG to PING is healthy."""
        from app.services.health_poller import _tcp_check

        async def _pong_handler(reader, writer):
            data = await reader.read(64)
            if data.startswith(b'PING'):
                writer.write(b'+PONG\r\n')
                await writer.drain()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        server = await asyncio.start_server(_pong_handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        try:
            result = await _tcp_check(1, '127.0.0.1', port, timeout_seconds=2.0, verify_redis=True)
        finally:
            server.close()
            await server.wait_closed()

        svc_id, status, elapsed_ms, category, detail, health_message = result
        assert status == 'healthy'
        assert category == 'ok'

    @pytest.mark.asyncio
    async def test_redis_noauth_reply_is_healthy(self):
        """verify_redis=True: a password-protected Redis replies -NOAUTH to an
        unauthenticated PING. That reply is itself proof the target is
        reachable and speaking the Redis protocol -- must count as healthy,
        not tcp_bad_banner (policy decision, codex diff review)."""
        from app.services.health_poller import _tcp_check

        async def _noauth_handler(reader, writer):
            await reader.read(64)
            writer.write(b'-NOAUTH Authentication required.\r\n')
            await writer.drain()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        server = await asyncio.start_server(_noauth_handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        try:
            result = await _tcp_check(1, '127.0.0.1', port, timeout_seconds=2.0, verify_redis=True)
        finally:
            server.close()
            await server.wait_closed()

        svc_id, status, elapsed_ms, category, detail, health_message = result
        assert status == 'healthy'
        assert category == 'ok'

    @pytest.mark.asyncio
    async def test_redis_bad_banner_is_unhealthy(self):
        """verify_redis=True: a connect that succeeds but doesn't reply +PONG
        or -NOAUTH (e.g. some other service happens to be listening on that
        port, or a genuine -ERR) must not be reported healthy."""
        from app.services.health_poller import _tcp_check

        async def _wrong_handler(reader, writer):
            await reader.read(64)
            writer.write(b'-ERR unknown command\r\n')
            await writer.drain()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        server = await asyncio.start_server(_wrong_handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        try:
            result = await _tcp_check(1, '127.0.0.1', port, timeout_seconds=2.0, verify_redis=True)
        finally:
            server.close()
            await server.wait_closed()

        svc_id, status, elapsed_ms, category, detail, health_message = result
        assert status == 'unhealthy'
        assert category == 'tcp_bad_banner'

    @pytest.mark.asyncio
    async def test_timeout_when_connect_hangs(self):
        """A connect attempt that never resolves must classify as tcp_timeout,
        not hang the poll cycle. Deterministic via a mocked hang rather than
        an OS-level backlog trick (which is not portably reproducible)."""
        from app.services import health_poller as hp

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(5)

        with mock.patch('asyncio.open_connection', _hang):
            result = await hp._tcp_check(1, '127.0.0.1', 9, timeout_seconds=0.05)

        svc_id, status, elapsed_ms, category, detail, health_message = result
        assert status == 'unhealthy'
        assert category == 'tcp_timeout'


# ---------------------------------------------------------------------------
# _poll_one(protocol='tcp') — SSRF allowlist parity with the HTTP path
# ---------------------------------------------------------------------------

class TestPollOneTcpMode:
    @pytest.mark.asyncio
    async def test_ssrf_blocked_without_allowlist(self):
        from app.services.health_poller import _poll_one

        fake_client = mock.AsyncMock()
        result = await _poll_one(
            fake_client, asyncio.Semaphore(1), 1, 'tcp-svc', '127.0.0.1', 9999, '',
            protocol='tcp',
        )
        svc_id, status, elapsed_ms, category, detail, health_message = result
        assert status == 'unhealthy'
        assert category == 'ssrf_blocked'

    @pytest.mark.asyncio
    async def test_redis_named_row_requires_pong_via_poll_one(self):
        """A row named 'redis' with protocol='tcp' must go through the PING/PONG
        check inside the full _poll_one path, not just the _tcp_check unit."""
        from shared.config import get_config
        os.environ['HEALTH_POLL_ALLOWED_PRIVATE_HOSTS'] = '127.0.0.1'
        get_config.cache_clear()

        from app.services.health_poller import _poll_one

        async def _pong_handler(reader, writer):
            await reader.read(64)
            writer.write(b'+PONG\r\n')
            await writer.drain()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        server = await asyncio.start_server(_pong_handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        try:
            fake_client = mock.AsyncMock()
            result = await _poll_one(
                fake_client, asyncio.Semaphore(1), 1, 'redis', '127.0.0.1', port, '',
                protocol='tcp',
            )
        finally:
            server.close()
            await server.wait_closed()

        svc_id, status, elapsed_ms, category, detail, health_message = result
        assert status == 'healthy'

    @pytest.mark.asyncio
    async def test_healthy_with_allowlist_and_real_listener(self):
        from shared.config import get_config
        os.environ['HEALTH_POLL_ALLOWED_PRIVATE_HOSTS'] = '127.0.0.1'
        get_config.cache_clear()

        from app.services.health_poller import _poll_one

        server = await asyncio.start_server(_noop_handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        try:
            fake_client = mock.AsyncMock()
            result = await _poll_one(
                fake_client, asyncio.Semaphore(1), 1, 'tcp-svc', '127.0.0.1', port, '',
                protocol='tcp',
            )
        finally:
            server.close()
            await server.wait_closed()

        svc_id, status, elapsed_ms, category, detail, health_message = result
        assert status == 'healthy'


# ---------------------------------------------------------------------------
# _poll_all_services — end-to-end write for a tcp-protocol row
# ---------------------------------------------------------------------------

class TestPollAllServicesTcpRow:
    @pytest.mark.asyncio
    async def test_tcp_row_health_status_written_by_full_cycle(self, db):
        from shared.config import get_config
        os.environ['HEALTH_POLL_ALLOWED_PRIVATE_HOSTS'] = '127.0.0.1'
        get_config.cache_clear()

        server = await asyncio.start_server(_noop_handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]

        svc = RagService(
            name="tcp-poll-row",
            display_name="TCP Poll Row",
            host="127.0.0.1",
            port=port,
            protocol="tcp",
            health_endpoint=None,
            service_type="infra",
            enabled=True,
        )
        db.add(svc)
        db.commit()

        @contextlib.contextmanager
        def _patched_db_context():
            yield db

        try:
            with mock.patch('app.services.health_poller.get_db_context', _patched_db_context):
                from app.services.health_poller import _poll_all_services
                summary = await _poll_all_services(asyncio.Semaphore(4))
        finally:
            server.close()
            await server.wait_closed()

        db.expire_all()
        row = db.query(RagService).filter(RagService.name == "tcp-poll-row").first()
        assert row.health_status == 'healthy'
        assert summary['healthy'] >= 1


# ---------------------------------------------------------------------------
# POST /api/service-registry/services upsert with protocol='tcp'
# ---------------------------------------------------------------------------

class TestRegisterServiceTcpUpsert:
    def test_creates_tcp_row_without_endpoint_url(self, client, db):
        # validate_host() rejects loopback categorically at the write boundary
        # (same rule an http/https endpoint_url upsert would hit) -- use an
        # RFC1918 address, which is allowed (with a warning log) for homelab
        # deployments, same as the http path.
        resp = client.post(
            '/api/service-registry/services',
            params={
                'name': 'tcp-registered-svc',
                'protocol': 'tcp',
                'host': '192.168.1.50',
                'port': 9100,
            },
            headers={'X-Service-Key': _SERVICE_KEY},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data['action'] == 'created'
        assert data['url'] == 'tcp://192.168.1.50:9100'

        row = db.query(RagService).filter(RagService.name == 'tcp-registered-svc').first()
        assert row is not None
        assert row.protocol == 'tcp'
        assert row.host == '192.168.1.50'
        assert row.port == 9100
        assert row.endpoint_url is None

    def test_missing_host_rejected(self, client):
        resp = client.post(
            '/api/service-registry/services',
            params={'name': 'tcp-missing-host', 'protocol': 'tcp', 'port': 9100},
            headers={'X-Service-Key': _SERVICE_KEY},
        )
        assert resp.status_code == 422

    def test_out_of_range_port_rejected(self, client):
        resp = client.post(
            '/api/service-registry/services',
            params={'name': 'tcp-bad-port', 'protocol': 'tcp', 'host': '192.168.1.50', 'port': 70000},
            headers={'X-Service-Key': _SERVICE_KEY},
        )
        assert resp.status_code == 422

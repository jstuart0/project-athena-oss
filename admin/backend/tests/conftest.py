"""
Test configuration and fixtures for API key testing.
"""
import os
import sys

# Ensure src/ is on sys.path so `from shared.config import get_config` works when
# pytest is invoked from the repo root.  Containers install shared via
# `pip install -e /app/shared` (admin/backend/Dockerfile), so this is test-only.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Set test environment before importing app.
# ORDER MATTERS: these must be set before any app module is imported so that
# get_config() (via pydantic-settings) picks up the correct env values.
# service_auth.py reads SERVICE_API_KEY at call time via get_config() — no
# module-level capture occurs.
os.environ["DEV_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SERVICE_API_KEY", "test-service-key-for-hardening-tests")
# The memory vector store must never reach a real Qdrant from the unit suite:
# a closed loopback port, so even a module-scoped TestClient(app) (which runs
# the app's startup) can't touch localhost:6333.
os.environ["QDRANT_URL"] = "http://127.0.0.1:1"

from app.database import Base, get_db
from app.models import User, UserAPIKey
from app.auth.oidc import get_current_user
from app.services import memory_vectors
from app.services.telemetry import sender as telemetry_sender
from main import app

memory_vectors.set_background_enabled(False)


# Create test database
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ---------------------------------------------------------------------------
# Memory vector store harness (every test gets an in-memory Qdrant, a
# deterministic embedder, a controllable clock and the test session factory)
# ---------------------------------------------------------------------------

def fake_embed(texts):
    """384-d unit vectors seeded from sha256(text): deterministic, distinct."""
    import hashlib
    import math
    import random

    vectors = []
    for text in texts:
        rng = random.Random(int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big"))
        raw = [rng.gauss(0.0, 1.0) for _ in range(memory_vectors.EMBEDDING_DIM)]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        vectors.append([x / norm for x in raw])
    return vectors


class FakeClock:
    def __init__(self):
        from datetime import datetime, timezone

        self._mono = 1000.0
        self._now = datetime.now(timezone.utc)

    def monotonic(self):
        return self._mono

    def utcnow(self):
        return self._now

    def advance(self, seconds):
        from datetime import timedelta

        self._mono += seconds
        self._now += timedelta(seconds=seconds)


def failing_client():
    """A client pointed at a port that was just free: every call is refused."""
    import socket
    from qdrant_client import QdrantClient

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return QdrantClient(url=f"http://127.0.0.1:{port}", timeout=1, check_compatibility=False)


class DyingClient:
    """Delegates to ``inner`` until ``die()``; then every call is refused."""

    def __init__(self, inner):
        self._inner = inner
        self._dead = False

    def die(self):
        self._dead = True

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def _call(*args, **kwargs):
            if self._dead:
                from qdrant_client.http.exceptions import ResponseHandlingException

                raise ResponseHandlingException(ConnectionRefusedError("connection refused"))
            return attr(*args, **kwargs)

        return _call


@pytest.fixture(autouse=True)
def memory_vector_test_env():
    from qdrant_client import QdrantClient

    memory_vectors.reset_for_tests()
    clock = FakeClock()
    memory_vectors.set_client_for_tests(QdrantClient(":memory:"))
    memory_vectors.set_embedder_for_tests(fake_embed)
    memory_vectors.set_clock_for_tests(clock)
    memory_vectors.set_session_factory_for_tests(TestingSessionLocal)
    try:
        yield clock
    finally:
        memory_vectors.reset_for_tests()


@pytest.fixture(scope="function")
def db():
    """Create fresh database for each test."""
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _calendar_sync_lease_on_test_db(monkeypatch):
    """calendar_sync takes its per-source lease on its own session; point
    that at the test database instead of the app's SessionLocal."""
    from app.services import calendar_sync

    monkeypatch.setattr(calendar_sync, "LEASE_SESSION_FACTORY", TestingSessionLocal)


@pytest.fixture(autouse=True)
def _telemetry_off_and_on_the_test_db(monkeypatch):
    """Telemetry is opted out for every test (effective: the switches are
    read at call time), its lease and state use the test database, and a
    stray `.env` in the working directory can't reach it. The module is the
    one imported at collection (several suites evict `app.*` from
    sys.modules mid-run; a call-time import would patch a fresh copy)."""
    monkeypatch.setenv("ATHENA_TELEMETRY", "off")
    monkeypatch.setattr(telemetry_sender, "LEASE_SESSION_FACTORY", TestingSessionLocal)
    monkeypatch.setattr(telemetry_sender, "DOTENV_PATH", "/nonexistent-athena-telemetry-test/.env")
    telemetry_sender._reset_for_tests()
    yield
    telemetry_sender._reset_for_tests()


@pytest.fixture
def telemetry_env(monkeypatch):
    """Opt a test back in: an explicit install class (pytest would otherwise
    classify as `test`), a persistent-looking database, a fresh config."""
    from shared.config import get_config

    get_config.cache_clear()
    monkeypatch.setenv("ATHENA_TELEMETRY_MODE", "self_hosted_real")
    monkeypatch.setenv("ATHENA_TELEMETRY", "")
    for name in ("DO_NOT_TRACK", "ATHENA_TELEMETRY_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(telemetry_sender, "EPHEMERAL_DB_CHECK", lambda: False)
    yield
    get_config.cache_clear()


@pytest.fixture(scope="function")
def client(db):
    """Create test client with database override."""
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
def test_user(db):
    """Create a test user."""
    user = User(
        authentik_id="test-user-001",
        username="testuser",
        email="test@example.com",
        full_name="Test User",
        role="owner",
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def viewer_user(db):
    """Create a viewer (limited permissions) user."""
    user = User(
        authentik_id="viewer-001",
        username="viewer",
        email="viewer@example.com",
        full_name="Viewer User",
        role="viewer",
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def operator_user(db):
    """Create an operator user: `read`, `write`, `view_audit` -- notably NOT
    `manage_infrastructure` (ATHENA-118 / D20). Used to prove the owner gate
    on critical targets without conflating it with the viewer's 403."""
    user = User(
        authentik_id="operator-001",
        username="operator",
        email="operator@example.com",
        full_name="Operator User",
        role="operator",
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def owner_client(client, test_user):
    """`client` with get_current_user overridden to the owner (`test_user`).
    Centralized here (ATHENA-118) so every test module shares one
    definition instead of redeclaring it (mirrors the pre-existing pattern
    in test_base_knowledge_settings.py, now the single source)."""
    async def _get_user():
        return test_user
    app.dependency_overrides[get_current_user] = _get_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def operator_client(client, operator_user):
    async def _get_user():
        return operator_user
    app.dependency_overrides[get_current_user] = _get_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def viewer_client(client, viewer_user):
    async def _get_user():
        return viewer_user
    app.dependency_overrides[get_current_user] = _get_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def test_api_key(db, test_user):
    """Create a test API key."""
    from app.utils.api_keys import generate_api_key, hash_api_key, extract_key_prefix

    raw_key = generate_api_key()
    key = UserAPIKey(
        user_id=test_user.id,
        name="Test Key",
        key_prefix=extract_key_prefix(raw_key),
        key_hash=hash_api_key(raw_key),
        scopes=["read:*", "write:*"],
        created_by_id=test_user.id,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key, raw_key  # Return both record and raw key


@pytest.fixture
def expired_api_key(db, test_user):
    """Create an expired API key."""
    from app.utils.api_keys import generate_api_key, hash_api_key, extract_key_prefix

    raw_key = generate_api_key()
    key = UserAPIKey(
        user_id=test_user.id,
        name="Expired Key",
        key_prefix=extract_key_prefix(raw_key),
        key_hash=hash_api_key(raw_key),
        scopes=["read:*"],
        expires_at=datetime.utcnow() - timedelta(days=1),  # Expired yesterday
        created_by_id=test_user.id,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key, raw_key


@pytest.fixture
def revoked_api_key(db, test_user):
    """Create a revoked API key."""
    from app.utils.api_keys import generate_api_key, hash_api_key, extract_key_prefix

    raw_key = generate_api_key()
    key = UserAPIKey(
        user_id=test_user.id,
        name="Revoked Key",
        key_prefix=extract_key_prefix(raw_key),
        key_hash=hash_api_key(raw_key),
        scopes=["read:*"],
        revoked=True,
        revoked_at=datetime.utcnow(),
        revoked_reason="Test revocation",
        created_by_id=test_user.id,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key, raw_key

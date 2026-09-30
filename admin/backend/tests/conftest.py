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

from app.database import Base, get_db
from app.models import User, UserAPIKey
from app.auth.oidc import get_current_user
from main import app


# Create test database
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


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

"""
Test configuration and fixtures for API key testing.
"""
import os
import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Set test environment before importing app
os.environ["DEV_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.database import Base, get_db
from app.models import User, UserAPIKey
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

"""Admin routes that used to hand back exception text now return a fixed string; the cause stays in the log."""

import pytest

import app.routes.memories as memories_module
from app.auth.oidc import get_current_user
from app.models import SystemSetting
from main import app
from shared.config import get_config

SECRET = "connection to server at 10.0.0.5 port 5432 failed: password authentication for DB_PASSWORD"


@pytest.fixture
def owner_client(client, test_user):
    async def _get_user():
        return test_user

    app.dependency_overrides[get_current_user] = _get_user
    yield client


def test_a_failed_memory_create_returns_internal_error_not_the_exception(client, db, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(db, "commit", boom)
    resp = client.post(
        "/api/memories/internal/create",
        params={"content": "the garage code is hidden", "mode": "owner", "importance": 0.9},
        headers={"X-Service-Key": get_config().service_api_key},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"created": False, "reason": "internal_error"}
    assert "10.0.0.5" not in resp.text and "DB_PASSWORD" not in resp.text


def test_a_failed_profile_save_returns_the_fixed_detail(owner_client, db, monkeypatch):
    body = owner_client.get("/api/settings/assistant-profile").json()

    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(db, "commit", boom)
    resp = owner_client.post("/api/settings/assistant-profile", json=body)
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Failed to save assistant profile"}
    assert "10.0.0.5" not in resp.text and "DB_PASSWORD" not in resp.text
    assert db.query(SystemSetting).filter(SystemSetting.key == "assistant_profile_config").count() == 0


def test_the_source_has_no_exception_text_in_those_responses():
    import inspect

    assert "str(e)" not in inspect.getsource(memories_module.internal_create_memory).split("except Exception")[1].split("vector_stored")[0].replace(
        'logger.error("internal_create_failed", error=str(e))', "")
    from app.routes import settings

    source = inspect.getsource(settings.save_assistant_profile)
    assert 'detail="Failed to save assistant profile")' in source and "{str(e)}" not in source

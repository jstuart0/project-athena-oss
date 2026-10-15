"""Household settings saves on real Postgres: two sessions race the same revision.

    POSTGRES_TEST_URL=postgresql://postgres:t@localhost:5432/postgres \\
        pytest -m integration tests/integration/test_household_settings_postgres.py

The SQLite tests prove the 409 logic; only a real server proves that the
compare-and-swap UPDATE (and the unique-key INSERT) serialises two writers.
Both requests read the same revision, are held at the barrier in
``stored_value`` (after their reads and validation, before their writes), and
are then released together. Exactly one wins; the loser's value is not stored.

Without a reachable server every test fails (never skips). Each test runs in
its own throwaway database.
"""
from __future__ import annotations

import json
import os
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.auth.oidc import create_access_token
from app.database import Base, get_db
from app.models import AuditLog, SystemSetting, User
from app.routes import household_settings as route_module
from main import app
from shared.config import _clear_cache_for_tests

pytestmark = pytest.mark.integration

URL = os.environ.get("POSTGRES_TEST_URL", "")
PATH = "/api/settings/household/sms"


@pytest.fixture
def pg_url():
    reachable = False
    if URL:
        try:
            with create_engine(URL).connect() as conn:
                reachable = conn.execute(text("SELECT 1")).scalar() == 1
        except Exception:
            reachable = False
    if not reachable:
        pytest.fail("POSTGRES_TEST_URL unset or server unreachable: point it at a running Postgres "
                    "to run the real-database tier", pytrace=False)
    name = f"household_it_{uuid.uuid4().hex[:12]}"
    admin = create_engine(URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(URL).set(database=name)
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def sessions(pg_url, monkeypatch):
    from tests.conftest import get_current_user as served_get_current_user

    monkeypatch.setitem(served_get_current_user.__globals__, "DEV_MODE", False)
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)
    _clear_cache_for_tests()
    route_module._refusal_audited_at.clear()
    engine = create_engine(pg_url)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield factory
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()
        _clear_cache_for_tests()


def _owner(factory, name):
    with factory() as db:
        user = User(authentik_id=f"{name}-1", username=name, email=f"{name}@example.com", full_name=name,
                    role="owner", active=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        token = create_access_token({"user_id": user.id, "username": user.username, "role": user.role})
    return {"Authorization": f"Bearer {token}"}


def _race(monkeypatch, headers, codes, revision):
    """Both writers read ``revision``, wait for each other, then write."""
    barrier = threading.Barrier(2, timeout=30)
    real = route_module.stored_value

    def held(*args, **kwargs):
        barrier.wait()
        return real(*args, **kwargs)

    monkeypatch.setattr(route_module, "stored_value", held)
    results = {}

    def worker(who, code):
        client = TestClient(app)
        results[who] = client.put(PATH, json={"value": {"default_country_code": code}, "revision": revision},
                                  headers=headers[who])

    threads = [threading.Thread(target=worker, args=(who, code)) for who, code in codes.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(results) == 2
    return results


def _stored(factory):
    with factory() as db:
        row = db.query(SystemSetting).filter(SystemSetting.key == "household.sms").one()
        return json.loads(row.value)


def test_two_first_saves_race_one_wins_and_the_loser_is_not_stored(sessions, monkeypatch):
    headers = {"a": _owner(sessions, "owner_a"), "b": _owner(sessions, "owner_b")}
    results = _race(monkeypatch, headers, {"a": "44", "b": "33"}, revision=0)
    statuses = sorted(r.status_code for r in results.values())
    assert statuses == [200, 409]
    winner = next(who for who, r in results.items() if r.status_code == 200)
    loser = next(who for who, r in results.items() if r.status_code == 409)
    assert results[loser].json()["detail"] == "stale_revision"
    stored = _stored(sessions)
    assert stored["revision"] == 1
    assert stored["value"]["default_country_code"] == {"a": "44", "b": "33"}[winner]
    with sessions() as db:
        assert db.query(AuditLog).filter(AuditLog.action == "household_settings_update").count() == 1


def test_two_updates_race_one_wins_and_the_loser_is_not_stored(sessions, monkeypatch):
    headers = {"a": _owner(sessions, "owner_a"), "b": _owner(sessions, "owner_b")}
    first = TestClient(app).put(PATH, json={"value": {"default_country_code": "1"}, "revision": 0}, headers=headers["a"])
    assert first.status_code == 200
    results = _race(monkeypatch, headers, {"a": "44", "b": "33"}, revision=1)
    assert sorted(r.status_code for r in results.values()) == [200, 409]
    winner = next(who for who, r in results.items() if r.status_code == 200)
    stored = _stored(sessions)
    assert stored["revision"] == 2 and stored["value"]["default_country_code"] == {"a": "44", "b": "33"}[winner]
    with sessions() as db:
        assert db.query(AuditLog).filter(AuditLog.action == "household_settings_update").count() == 2


def test_a_stale_save_after_the_fact_is_409_and_changes_nothing(sessions):
    headers = _owner(sessions, "owner_a")
    client = TestClient(app)
    assert client.put(PATH, json={"value": {"default_country_code": "44"}, "revision": 0}, headers=headers).status_code == 200
    before = _stored(sessions)
    stale = client.put(PATH, json={"value": {"default_country_code": "33"}, "revision": 0}, headers=headers)
    assert stale.status_code == 409 and _stored(sessions) == before

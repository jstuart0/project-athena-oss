"""Telemetry's lease and identity on real Postgres: two replicas race a
cycle (exactly one request, one ID), and a forced ID collision takes the
rollback-then-read path without InFailedSqlTransaction.

    POSTGRES_TEST_URL=postgresql://postgres:t@localhost:5432/postgres \\
        pytest -m integration tests/integration/test_telemetry_postgres.py

Without a reachable server every test fails (never skips). Each test runs in
its own throwaway database.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import threading
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import SystemSetting
from app.services.telemetry import sender

pytestmark = pytest.mark.integration

URL = os.environ.get("POSTGRES_TEST_URL", "")


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
    name = f"telemetry_it_{uuid.uuid4().hex[:12]}"
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


def _ids(url):
    engine = create_engine(url)
    session = sessionmaker(bind=engine)()
    try:
        return [r.value for r in session.query(SystemSetting).filter(SystemSetting.key == "telemetry.installation_id")]
    finally:
        session.close()
        engine.dispose()


def _recording(monkeypatch):
    lock = threading.Lock()
    requests = []

    def handler(request):
        with lock:
            requests.append(request.read())
        return httpx.Response(200, content=b"{}")

    monkeypatch.setattr(sender, "TRANSPORT", httpx.MockTransport(handler))
    return requests


def test_two_replicas_send_once(telemetry_env, pg_url, monkeypatch):
    for _ in range(5):
        requests = _recording(monkeypatch)
        engines = [create_engine(pg_url), create_engine(pg_url)]
        maker_var = contextvars.ContextVar("maker")
        monkeypatch.setattr(sender, "LEASE_SESSION_FACTORY", lambda: maker_var.get()())
        barrier = threading.Barrier(2)
        outcomes = []

        def replica(engine):
            maker_var.set(sessionmaker(bind=engine))
            barrier.wait()
            outcomes.append(asyncio.run(sender.run_cycle()))

        threads = [threading.Thread(target=replica, args=(e,)) for e in engines]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        for e in engines:
            e.dispose()
        assert len(requests) <= 1, outcomes
        # The first round sends; later rounds are within 23 h and send nothing.
        assert len(_ids(pg_url)) == 1, outcomes
    assert len(set(_ids(pg_url))) == 1


def test_forced_id_collision_rolls_back_then_reads(telemetry_env, pg_url, monkeypatch):
    requests = _recording(monkeypatch)
    engine = create_engine(pg_url)
    maker = sessionmaker(bind=engine)
    monkeypatch.setattr(sender, "LEASE_SESSION_FACTORY", maker)
    foreign = "11111111-2222-4333-8444-555555555555"

    def insert_foreign():
        session = maker()
        try:
            session.add(SystemSetting(key="telemetry.installation_id", value=foreign, category="telemetry"))
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(sender, "_BEFORE_ID_INSERT", insert_foreign)
    try:
        assert asyncio.run(sender.run_cycle()) == "sent"
    finally:
        engine.dispose()
    assert _ids(pg_url) == [foreign]
    assert len(requests) == 1
    assert foreign.encode() in requests[0]

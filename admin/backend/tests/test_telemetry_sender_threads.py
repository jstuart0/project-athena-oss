"""Two replicas, for real: two threads with independent sessionmakers on one
file-backed SQLite database race a cycle behind a barrier. Exactly one
request and one installation ID, every time."""
from __future__ import annotations

import asyncio
import contextvars
import threading

import httpx
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import SystemSetting
from app.services.telemetry import sender

ROUNDS = 20


def _engine(path):
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _wal(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA journal_mode=WAL")

    return engine


def _one_round(tmp_path, n, monkeypatch):
    path = tmp_path / f"round-{n}.db"
    engines = [_engine(path), _engine(path)]
    Base.metadata.create_all(bind=engines[0])
    maker_var = contextvars.ContextVar("maker")
    lock = threading.Lock()
    requests = []

    def handler(request):
        with lock:
            requests.append(request.read())
        return httpx.Response(200, content=b"{}")

    monkeypatch.setattr(sender, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(sender, "LEASE_SESSION_FACTORY", lambda: maker_var.get()())
    barrier = threading.Barrier(2)
    outcomes = []

    def replica(engine):
        maker_var.set(sessionmaker(bind=engine, autocommit=False, autoflush=False))
        barrier.wait()
        outcomes.append(asyncio.run(sender.run_cycle()))

    threads = [threading.Thread(target=replica, args=(e,)) for e in engines]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    session = sessionmaker(bind=engines[0])()
    try:
        ids = session.query(SystemSetting).filter(SystemSetting.key == "telemetry.installation_id").count()
    finally:
        session.close()
        for e in engines:
            e.dispose()
    return requests, ids, sorted(outcomes)


def test_two_concurrent_cycles_send_once(telemetry_env, tmp_path, monkeypatch):
    for n in range(ROUNDS):
        requests, ids, outcomes = _one_round(tmp_path, n, monkeypatch)
        assert len(requests) == 1, (n, outcomes)
        assert ids == 1, (n, outcomes)
        assert "sent" in outcomes, outcomes

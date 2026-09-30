"""The memory vector store against a real Qdrant server (the in-memory
client raises ValueError where a server returns 404/409, and servers older
than 1.16 drop collection metadata). CI runs this against v1.19.1 and
v1.12.1:

    QDRANT_TEST_URL=http://localhost:6333 QDRANT_EXPECTED_VERSION=1.19.1 \\
        pytest -m integration tests/integration/test_memory_vectors_real_qdrant.py

Without a reachable server every test fails (never skips).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import httpx
import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from structlog.testing import capture_logs

from app.models import Memory
from app.services import memory_vectors as mv
from tests.conftest import fake_embed

pytestmark = pytest.mark.integration

URL = os.environ.get("QDRANT_TEST_URL", "")


@pytest.fixture(scope="module", autouse=True)
def _server_required():
    reachable = False
    if URL:
        try:
            reachable = httpx.get(f"{URL}/readyz", timeout=5).status_code == 200
        except httpx.HTTPError:
            reachable = False
    if not reachable:
        pytest.fail("QDRANT_TEST_URL unset or server unreachable: point it at a running Qdrant "
                    "(e.g. http://localhost:6333) to run the real-server tier", pytrace=False)


def _server_version():
    return httpx.get(f"{URL}/", timeout=5).json()["version"]


def _stores_metadata():
    major, minor = (int(p) for p in _server_version().split(".")[:2])
    return (major, minor) >= (1, 16)


@pytest.fixture(autouse=True)
def real_qdrant(memory_vector_test_env, monkeypatch):
    client = QdrantClient(url=URL, timeout=10, check_compatibility=False)
    name = f"athena_memories_it_{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr(mv, "COLLECTION_NAME", name)
    mv.set_client_for_tests(client)
    try:
        yield client
    finally:
        try:
            client.delete_collection(name)
        except Exception:
            pass


def _row(db, content, status="pending"):
    row = Memory(content=content, scope="owner", vector_id=str(uuid.uuid4()), importance=0.5,
                 vector_status=status)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _status(db, row):
    db.expire_all()
    return db.query(Memory).get(row.id).vector_status


def _point_ids(client):
    ids, offset = set(), None
    while True:
        points, offset = client.scroll(mv.COLLECTION_NAME, limit=100, offset=offset, with_payload=False)
        ids.update(str(p.id) for p in points)
        if offset is None:
            return ids


def test_canary_module_client_is_the_server(db):
    assert mv.refresh_state().status == "ready"
    independent = QdrantClient(url=URL, timeout=10, check_compatibility=False)
    assert independent.collection_exists(mv.COLLECTION_NAME)
    expected = os.environ.get("QDRANT_EXPECTED_VERSION")
    assert expected, "QDRANT_EXPECTED_VERSION must name the server version under test"
    assert _server_version() == expected


def test_create_records_model_where_the_server_can(db, real_qdrant):
    state = mv.refresh_state()
    assert state.status == "ready"
    info = real_qdrant.get_collection(mv.COLLECTION_NAME)
    assert info.config.params.vectors.size == 384
    if _stores_metadata():
        assert info.config.metadata == mv.collection_metadata()
        assert state.model_recorded is True
    else:
        assert not info.config.metadata
        assert state.model_recorded is False


def test_adopt_merges_metadata(db, real_qdrant):
    kwargs = {"metadata": {"owner_note": "kept"}} if _stores_metadata() else {}
    real_qdrant.create_collection(mv.COLLECTION_NAME, vectors_config=VectorParams(size=384, distance=Distance.COSINE),
                                  **kwargs)
    with capture_logs() as logs:
        state = mv.refresh_state()
    assert state.status == "ready"
    if _stores_metadata():
        metadata = real_qdrant.get_collection(mv.COLLECTION_NAME).config.metadata
        assert metadata["owner_note"] == "kept" and metadata["embedding_model"] == mv.EMBEDDING_MODEL
        assert [e["event"] for e in logs].count("memory_vector_collection_adopted") == 1
    else:
        assert state.model_recorded is False


class _AbsentOnce:
    def __init__(self, inner):
        self._inner = inner
        self._first = True

    def collection_exists(self, name):
        if self._first:
            self._first = False
            return False
        return self._inner.collection_exists(name)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_lost_create_race_409_is_ready(db, real_qdrant):
    real_qdrant.create_collection(mv.COLLECTION_NAME, vectors_config=VectorParams(size=384, distance=Distance.COSINE))
    mv.set_client_for_tests(_AbsentOnce(real_qdrant))
    with capture_logs() as logs:
        assert mv.refresh_state().status == "ready"
    assert not [e for e in logs if e["log_level"] == "error"]


def test_wrong_shape_is_refused_unmodified(db, real_qdrant):
    real_qdrant.create_collection(mv.COLLECTION_NAME, vectors_config=VectorParams(size=768, distance=Distance.COSINE))
    assert mv.refresh_state().status == "shape_mismatch"
    assert real_qdrant.get_collection(mv.COLLECTION_NAME).config.params.vectors.size == 768


def test_collection_deleted_between_stores_is_recreated(db, real_qdrant):
    assert mv.refresh_state().status == "ready"
    first = _row(db, "first")
    assert mv.store_vector(first)
    db.commit()
    real_qdrant.delete_collection(mv.COLLECTION_NAME)
    second = _row(db, "second")
    assert mv.store_vector(second) is True
    db.commit()
    assert real_qdrant.collection_exists(mv.COLLECTION_NAME)
    assert second.vector_id in _point_ids(real_qdrant)


def test_create_marks_pending_then_tick_restores(db, real_qdrant):
    assert mv.refresh_state().status == "ready"
    one, two = _row(db, "one"), _row(db, "two")
    assert mv.store_vector(one) and mv.store_vector(two)
    db.commit()
    real_qdrant.delete_collection(mv.COLLECTION_NAME)
    three = _row(db, "three")
    assert mv.store_vector(three)
    db.commit()
    assert [_status(db, one), _status(db, two), _status(db, three)] == ["pending", "pending", "stored"]
    asyncio.run(mv.tick())
    assert [_status(db, one), _status(db, two)] == ["stored", "stored"]
    assert {one.vector_id, two.vector_id, three.vector_id} <= _point_ids(real_qdrant)


def test_query_404_self_heals(db, real_qdrant):
    assert mv.refresh_state().status == "ready"
    real_qdrant.delete_collection(mv.COLLECTION_NAME)
    hits = mv.query(fake_embed(["x"])[0], readable_scopes=("owner",), guest_session_id=None, limit=5,
                    score_threshold=0.0)
    assert hits == [] and real_qdrant.collection_exists(mv.COLLECTION_NAME)


def test_owner_all_prunes_across_pages(db, real_qdrant, memory_vector_test_env, monkeypatch):
    clock = memory_vector_test_env
    monkeypatch.setattr(mv, "_SCROLL_PAGE", 5)
    assert mv.refresh_state().status == "ready"
    live = [_row(db, f"live {i}") for i in range(2)]
    old = (clock.utcnow() - timedelta(hours=1)).isoformat()
    orphans = [str(uuid.uuid4()) for _ in range(12)]
    real_qdrant.upsert(mv.COLLECTION_NAME, points=[
        PointStruct(id=o, vector=fake_embed([o])[0],
                    payload={"embedding_model": mv.EMBEDDING_MODEL, "vector_written_at": old})
        for o in orphans
    ], wait=True)
    report = mv.reindex("all", prune=True)
    assert report.orphans_pruned == 12 and report.failed == 0
    assert _point_ids(real_qdrant) == {r.vector_id for r in live}


def test_foreign_stamped_points_recover_on_both_versions(db, real_qdrant, memory_vector_test_env):
    clock = memory_vector_test_env
    assert mv.refresh_state().status == "ready"
    rows = [_row(db, f"live {i}", status="stored") for i in range(2)]
    orphan = str(uuid.uuid4())
    now = clock.utcnow().isoformat()
    real_qdrant.upsert(mv.COLLECTION_NAME, points=[
        PointStruct(id=pid, vector=fake_embed([pid])[0], payload={"embedding_model": "other/model",
                                                                  "vector_written_at": now})
        for pid in [r.vector_id for r in rows] + [orphan]
    ], wait=True)
    state = mv.refresh_state()
    assert state.status == "model_mismatch" and state.foreign_points == 3
    report = mv.reindex("all", prune=True)
    assert report.foreign_orphans_pruned == 1
    state = mv.get_state()
    assert state.status == "ready" and state.foreign_points == 0
    assert _point_ids(real_qdrant) == {r.vector_id for r in rows}

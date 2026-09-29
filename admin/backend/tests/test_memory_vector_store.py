"""The memory vector store module: collection ensure, recorded model,
single-flight probing off the request path, self-heal, the embed lock, and
the rule that nothing outside the module touches Qdrant directly.

Every test runs against an in-memory Qdrant (conftest) unless it installs
its own client.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Distance, PointStruct, VectorParams
from structlog.testing import capture_logs

from app.models import Memory
from app.services import memory_vectors as mv
from tests.conftest import DyingClient, failing_client, fake_embed

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _unexpected(status):
    return UnexpectedResponse(status_code=status, reason_phrase="x", content=b"", headers=httpx.Headers())


def _memory(db, content, *, status="stored", deleted=False, scope="owner", vector_id=None):
    row = Memory(
        content=content, scope=scope, vector_id=vector_id or str(uuid.uuid4()),
        category="fact", importance=0.5, source_type="manual", vector_status=status, is_deleted=deleted,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class _Spy:
    """Delegates to ``inner``; counts probes (collection_exists calls)."""

    def __init__(self, inner):
        self._inner = inner
        self.probes = 0

    def collection_exists(self, *args, **kwargs):
        self.probes += 1
        return self._inner.collection_exists(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _raw():
    """The in-memory client the conftest fixture installed."""
    return mv._get_client()


# ---------------------------------------------------------------------------
# Ensure and states
# ---------------------------------------------------------------------------

def test_missing_collection_created_with_384_cosine_and_metadata(db):
    state = mv.refresh_state()
    assert state.status == "ready"
    assert state.model_recorded is True
    info = _raw().get_collection(mv.COLLECTION_NAME)
    assert info.config.params.vectors.size == 384
    assert info.config.params.vectors.distance == Distance.COSINE
    assert info.config.metadata == {
        "embedding_model": mv.EMBEDDING_MODEL, "embedding_dim": 384, "distance": "Cosine", "payload_schema": 1,
    }


def test_create_branch_marks_live_rows_pending(db):
    live_a = _memory(db, "a")
    live_b = _memory(db, "b")
    gone = _memory(db, "c", deleted=True)
    assert not _raw().collection_exists(mv.COLLECTION_NAME)

    assert mv.refresh_state().status == "ready"

    db.expire_all()
    assert [live_a.vector_status, live_b.vector_status, gone.vector_status] == ["pending", "pending", "stored"]
    assert _raw().collection_exists(mv.COLLECTION_NAME)


def test_mark_failure_creates_nothing(db):
    def _broken_factory():
        raise RuntimeError("database down")

    mv.set_session_factory_for_tests(_broken_factory)
    assert mv.refresh_state().status == "unavailable"
    assert not _raw().collection_exists(mv.COLLECTION_NAME)


def test_existing_collection_marks_nothing(db):
    mv.refresh_state()
    row = _memory(db, "a")
    mv.reset_state_for_tests()
    assert mv.refresh_state().status == "ready"
    db.expire_all()
    assert row.vector_status == "stored"


def test_shape_mismatch_is_refused_and_logged_error(db):
    _raw().create_collection(mv.COLLECTION_NAME, vectors_config=VectorParams(size=768, distance=Distance.COSINE))
    with capture_logs() as logs:
        state = mv.refresh_state()
    assert state.status == "shape_mismatch"
    assert _raw().get_collection(mv.COLLECTION_NAME).config.params.vectors.size == 768
    assert any(e["log_level"] == "error" and e["event"] == "memory_vector_collection_shape_mismatch" for e in logs)


def test_metadata_model_mismatch(db):
    _raw().create_collection(
        mv.COLLECTION_NAME, vectors_config=VectorParams(size=384, distance=Distance.COSINE),
        metadata={"embedding_model": "other/model", "embedding_dim": 384},
    )
    state = mv.refresh_state()
    assert state.status == "model_mismatch"
    assert state.collection_model == "other/model"


def test_foreign_stamped_point_is_model_mismatch_with_sample(db):
    _raw().create_collection(mv.COLLECTION_NAME, vectors_config=VectorParams(size=384, distance=Distance.COSINE))
    foreign_id = str(uuid.uuid4())
    _raw().upsert(mv.COLLECTION_NAME, points=[
        PointStruct(id=foreign_id, vector=fake_embed(["x"])[0], payload={"embedding_model": "other/model"}),
        PointStruct(id=str(uuid.uuid4()), vector=fake_embed(["y"])[0], payload={"embedding_model": mv.EMBEDDING_MODEL}),
        PointStruct(id=str(uuid.uuid4()), vector=fake_embed(["z"])[0], payload={}),
    ])
    state = mv.refresh_state()
    assert state.status == "model_mismatch"
    assert state.foreign_points == 1
    assert foreign_id in state.foreign_point_ids_sample


class _MetadataDroppingClient:
    """Qdrant < 1.16: metadata accepted, silently dropped."""

    def __init__(self, inner, reject_metadata=False):
        self._inner = inner
        self._reject = reject_metadata
        self.create_calls = []

    def create_collection(self, collection_name, vectors_config=None, metadata=None, **kwargs):
        self.create_calls.append(metadata)
        if metadata is not None and self._reject:
            raise _unexpected(400)
        return self._inner.create_collection(collection_name, vectors_config=vectors_config, **kwargs)

    def update_collection(self, collection_name, metadata=None, **kwargs):
        if metadata is not None and self._reject:
            raise _unexpected(400)
        return True

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_metadata_dropped_by_server_is_ready_unrecorded(db):
    mv.set_client_for_tests(_MetadataDroppingClient(QdrantClient(":memory:")))
    state = mv.refresh_state()
    assert state.status == "ready"
    assert state.model_recorded is False


def test_create_with_metadata_rejected_retries_without(db):
    fake = _MetadataDroppingClient(QdrantClient(":memory:"), reject_metadata=True)
    mv.set_client_for_tests(fake)
    state = mv.refresh_state()
    assert state.status == "ready"
    assert state.model_recorded is False
    assert fake.create_calls[0] is not None and fake.create_calls[-1] is None


def test_adoption_logs_once(db):
    _raw().create_collection(mv.COLLECTION_NAME, vectors_config=VectorParams(size=384, distance=Distance.COSINE))
    with capture_logs() as logs:
        assert mv.refresh_state().status == "ready"
        assert mv.refresh_state().status == "ready"
    adopted = [e for e in logs if e["event"] == "memory_vector_collection_adopted"]
    assert len(adopted) == 1 and adopted[0]["log_level"] == "warning"
    assert _raw().get_collection(mv.COLLECTION_NAME).config.metadata["embedding_model"] == mv.EMBEDDING_MODEL


def test_adoption_on_metadata_dropping_server_logs_once(db):
    inner = QdrantClient(":memory:")
    inner.create_collection(mv.COLLECTION_NAME, vectors_config=VectorParams(size=384, distance=Distance.COSINE))
    mv.set_client_for_tests(_MetadataDroppingClient(inner))
    with capture_logs() as logs:
        assert mv.refresh_state().model_recorded is False
        assert mv.refresh_state().model_recorded is False
    assert len([e for e in logs if e["event"] == "memory_vector_model_unrecorded"]) == 1


def test_embedding_dim_matches_fastembed_descriptor():
    from fastembed import TextEmbedding

    [descriptor] = [m for m in TextEmbedding.list_supported_models() if m["model"] == mv.EMBEDDING_MODEL]
    assert descriptor["dim"] == mv.EMBEDDING_DIM == 384


# ---------------------------------------------------------------------------
# Race (tessa r1 H3)
# ---------------------------------------------------------------------------

class _RaceClient:
    """collection_exists reports absent once; create_collection creates the
    collection (as the other replica would, with ``race_size``) and then
    raises the 409 the losing replica sees."""

    def __init__(self, inner, race_size=384):
        self._inner = inner
        self._size = race_size
        self._first = True

    def collection_exists(self, name):
        if self._first:
            self._first = False
            return False
        return self._inner.collection_exists(name)

    def create_collection(self, name, **kwargs):
        self._inner.create_collection(name, vectors_config=VectorParams(size=self._size, distance=Distance.COSINE))
        raise _unexpected(409)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_race_lost_to_matching_shape_is_ready(db):
    mv.set_client_for_tests(_RaceClient(QdrantClient(":memory:")))
    with capture_logs() as logs:
        assert mv.refresh_state().status == "ready"
    assert not [e for e in logs if e["log_level"] == "error"]


def test_race_lost_to_mismatched_shape_is_shape_mismatch(db):
    mv.set_client_for_tests(_RaceClient(QdrantClient(":memory:"), race_size=768))
    assert mv.refresh_state().status == "shape_mismatch"


def test_concurrent_refresh_creates_one_collection(db):
    results = []
    barrier = threading.Barrier(2)

    def _run():
        barrier.wait()
        results.append(mv.refresh_state().status)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == ["ready", "ready"]
    assert [c.name for c in _raw().get_collections().collections] == [mv.COLLECTION_NAME]


# ---------------------------------------------------------------------------
# Cadence and blocking (bob M5, tessa N7)
# ---------------------------------------------------------------------------

def test_request_path_does_not_reprobe_cached_unavailable():
    spy = _Spy(failing_client())
    mv.set_client_for_tests(spy)
    assert mv.get_state().status == "unavailable"
    for _ in range(10):
        assert mv.get_state().status == "unavailable"
    assert spy.probes == 1


def test_tick_reprobes_after_30s(memory_vector_test_env):
    clock = memory_vector_test_env
    spy = _Spy(failing_client())
    mv.set_client_for_tests(spy)
    mv.get_state()
    clock.advance(29)
    asyncio.run(mv.tick())
    assert spy.probes == 1
    clock.advance(2)
    asyncio.run(mv.tick())
    assert spy.probes == 2


def test_tick_revalidates_ready_after_300s(db, memory_vector_test_env):
    clock = memory_vector_test_env
    spy = _Spy(QdrantClient(":memory:"))
    mv.set_client_for_tests(spy)
    assert mv.get_state().status == "ready"
    clock.advance(299)
    asyncio.run(mv.tick())
    assert spy.probes == 1
    clock.advance(2)
    asyncio.run(mv.tick())
    assert spy.probes == 2


class _BlockingClient:
    def __init__(self, inner):
        self._inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()

    def collection_exists(self, name):
        self.entered.set()
        self.release.wait(10)
        return self._inner.collection_exists(name)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_get_state_never_blocks_on_inflight_probe(db):
    blocking = _BlockingClient(QdrantClient(":memory:"))
    mv.set_client_for_tests(blocking)
    prober = threading.Thread(target=mv.get_state)
    prober.start()
    assert blocking.entered.wait(5)
    started = time.monotonic()
    state = mv.get_state()
    elapsed = time.monotonic() - started
    blocking.release.set()
    prober.join(10)
    assert state.status == "unavailable"
    assert elapsed < 0.05
    assert mv.get_state().status == "ready"


def test_get_state_returns_cached_while_tick_probe_inflight(db):
    inner = QdrantClient(":memory:")
    mv.set_client_for_tests(inner)
    assert mv.get_state().status == "ready"
    blocking = _BlockingClient(inner)
    mv.set_client_for_tests(blocking, keep_state=True)
    prober = threading.Thread(target=mv.refresh_state)
    prober.start()
    assert blocking.entered.wait(5)
    started = time.monotonic()
    state = mv.get_state()
    elapsed = time.monotonic() - started
    blocking.release.set()
    prober.join(10)
    assert state.status == "ready" and elapsed < 0.05


def test_loop_survives_tick_exception(monkeypatch):
    calls = []

    async def _tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        raise asyncio.CancelledError()

    monkeypatch.setattr(mv, "tick", _tick)
    monkeypatch.setattr(mv, "_TICK_INTERVAL_SECONDS", 0)
    with capture_logs() as logs:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(mv._maintenance_loop())
    assert len(calls) == 2
    assert any(e["event"] == "memory_vector_tick_failed" for e in logs)


def test_start_is_noop_when_background_disabled():
    mv.set_background_enabled(False)

    async def _run():
        mv.start_vector_store_maintenance()
        assert mv._task is None
        await mv.stop_vector_store_maintenance()

    asyncio.run(_run())


def test_start_and_stop_hold_one_task(monkeypatch):
    monkeypatch.setattr(mv, "_TICK_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(mv, "_background_enabled", True)

    async def _noop_tick():
        return None

    monkeypatch.setattr(mv, "tick", _noop_tick)

    async def _run():
        mv.start_vector_store_maintenance()
        first = mv._task
        mv.start_vector_store_maintenance()
        assert mv._task is first and first is not None
        await mv.stop_vector_store_maintenance()
        assert first.done() and mv._task is None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Self-heal
# ---------------------------------------------------------------------------

class _NotFoundOnceClient:
    def __init__(self, inner, exc):
        self._inner = inner
        self._exc = exc
        self.probes = 0

    def collection_exists(self, name):
        self.probes += 1
        return self._inner.collection_exists(name)

    def query_points(self, *args, **kwargs):
        if self._exc is not None:
            exc, self._exc = self._exc, None
            raise exc
        return self._inner.query_points(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize("exc", [_unexpected(404), ValueError("Collection athena_memories not found")],
                         ids=["server_404", "local_value_error"])
def test_query_404_self_heals(db, exc):
    inner = QdrantClient(":memory:")
    fake = _NotFoundOnceClient(inner, exc)
    mv.set_client_for_tests(fake)
    assert mv.get_state().status == "ready"
    point_id = str(uuid.uuid4())
    inner.upsert(mv.COLLECTION_NAME, points=[
        PointStruct(id=point_id, vector=fake_embed(["hello"])[0], payload={"scope": "global"}),
    ])
    hits = mv.query(fake_embed(["hello"])[0], readable_scopes=("global",), guest_session_id=None,
                    limit=5, score_threshold=0.5)
    assert [h[0] for h in hits] == [point_id]
    assert fake.probes == 2


def test_query_transport_error_marks_unavailable(db):
    dying = DyingClient(QdrantClient(":memory:"))
    mv.set_client_for_tests(dying)
    assert mv.get_state().status == "ready"
    dying.die()
    with pytest.raises(mv.VectorStoreNotReady):
        mv.query(fake_embed(["x"])[0], readable_scopes=("global",), guest_session_id=None, limit=5,
                 score_threshold=0.0)
    assert mv.get_state().status == "unavailable"


def test_query_when_not_ready_raises():
    mv.set_client_for_tests(failing_client())
    with pytest.raises(mv.VectorStoreNotReady) as info:
        mv.query([0.0] * 384, readable_scopes=("global",), guest_session_id=None, limit=5, score_threshold=0.0)
    assert info.value.state.status == "unavailable"


def test_query_empty_scopes_returns_nothing(db):
    assert mv.query([0.0] * 384, readable_scopes=(), guest_session_id=None, limit=5, score_threshold=0.0) == []


def test_is_not_found_classifier():
    assert mv._is_not_found(_unexpected(404))
    assert mv._is_not_found(ValueError("Collection x not found"))
    assert not mv._is_not_found(_unexpected(409))
    assert not mv._is_not_found(RuntimeError("not found"))


def test_delete_points_never_raises():
    mv.set_client_for_tests(failing_client())
    assert mv.delete_points(["00000000-0000-0000-0000-000000000001"]) is False


# ---------------------------------------------------------------------------
# Scope filter (moved from routes/memories.py)
# ---------------------------------------------------------------------------

def test_scope_filter_guest_branch_requires_session():
    flt = mv._scope_qdrant_filter(("global", "guest"), 5)
    rendered = repr(flt)
    assert "value='guest'" in rendered and "value=5" in rendered and "value='global'" in rendered
    assert mv._scope_qdrant_filter((), None) is None


# ---------------------------------------------------------------------------
# Embed (xander N1)
# ---------------------------------------------------------------------------

def test_embed_lock_serializes():
    active = [0]
    peak = [0]
    guard = threading.Lock()

    def _slow_embed(texts):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with guard:
            active[0] -= 1
        return fake_embed(texts)

    mv.set_embedder_for_tests(_slow_embed)
    threads = [threading.Thread(target=mv.embed, args=([f"text {i}"],)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak[0] == 1


def test_embed_truncates_to_4000_chars():
    seen = []

    def _recording(texts):
        seen.extend(len(t) for t in texts)
        return fake_embed(texts)

    mv.set_embedder_for_tests(_recording)
    vectors = mv.embed(["a" * 9000, "b" * 10, "c" * 4000])
    assert len(vectors) == 3 and all(len(v) == 384 for v in vectors)
    assert seen and max(seen) <= 4000


def test_embed_chunks_by_batch():
    sizes = []

    def _recording(texts):
        sizes.append(len(texts))
        return fake_embed(texts)

    mv.set_embedder_for_tests(_recording)
    mv.embed([f"t{i}" for i in range(40)])
    assert max(sizes) <= mv.EMBED_BATCH and sum(sizes) == 40


def test_embedder_constructed_offline_single_thread(monkeypatch):
    import fastembed
    import numpy as np

    calls = []

    class _FakeTextEmbedding:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def embed(self, texts, **kwargs):
            return iter([np.zeros(384, dtype=np.float32) for _ in texts])

    monkeypatch.setattr(fastembed, "TextEmbedding", _FakeTextEmbedding)
    mv.set_embedder_for_tests(None)
    assert len(mv.embed(["x"])[0]) == 384
    [(args, kwargs)] = calls
    assert kwargs.get("local_files_only") is True and kwargs.get("threads") == 1
    assert mv.EMBEDDING_MODEL in args or kwargs.get("model_name") == mv.EMBEDDING_MODEL


def test_embedder_failure_marks_embedder_unavailable_then_clears(db):
    def _broken(texts):
        raise RuntimeError("model files missing")

    assert mv.get_state().status == "ready"
    mv.set_embedder_for_tests(_broken)
    with pytest.raises(mv.EmbeddingUnavailable):
        mv.embed(["x"])
    assert mv.get_state().status == "embedder_unavailable"
    mv.set_embedder_for_tests(fake_embed)
    mv.embed(["x"])
    assert mv.get_state().status == "ready"


# ---------------------------------------------------------------------------
# Harness guard (tessa N4)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def module_client():
    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as client:
        yield client


def test_module_scoped_client_starts_no_maintenance(module_client):
    assert mv._task is None
    assert ":6333" not in mv.QDRANT_URL
    assert mv.QDRANT_URL == "http://127.0.0.1:1"


# ---------------------------------------------------------------------------
# Drift guard (bob H1): nothing outside the module touches Qdrant
# ---------------------------------------------------------------------------

DRIFT_PATTERNS = [
    r"QdrantClient\(",
    r"athena_memories",
    r"query_points\(",
    r"from qdrant_client import QdrantClient",
]
DRIFT_ALLOWLIST = {
    ("app/routes/rag_connectors.py", r"QdrantClient\("),
    ("app/models.py", r"athena_memories"),
}


def test_no_direct_qdrant_access_outside_module():
    scanned = []
    violations = set()
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(APP_DIR.parent).as_posix()
        if rel == "app/services/memory_vectors.py":
            continue
        scanned.append(rel)
        text = path.read_text(encoding="utf-8")
        for pattern in DRIFT_PATTERNS:
            if re.search(pattern, text) and (rel, pattern) not in DRIFT_ALLOWLIST:
                violations.add((rel, pattern))
    assert len(scanned) >= 90
    assert "app/routes/memories.py" in scanned
    assert violations == set()

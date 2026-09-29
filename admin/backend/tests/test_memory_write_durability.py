"""Postgres first: a memory is never lost because the vector store failed,
semantic results come only from live stored rows, and recall falls back to
keyword search when semantic search can't run (D9, D12, D16, D17).
"""
from __future__ import annotations

import ast
import math
import threading
import uuid
from datetime import date
from pathlib import Path

import httpx
import pytest
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Distance, VectorParams
from structlog.testing import capture_logs

import app.routes.memories as memories_module
from app.models import GuestSession, Memory
from app.services import memory_vectors as mv
from shared.config import get_config
from tests.conftest import DyingClient, failing_client, fake_embed

MEMORIES_PY = Path(__file__).resolve().parents[1] / "app" / "routes" / "memories.py"
D17_KEYS = {"results", "qdrant_available", "semantic_available", "error"}


def _key():
    return {"X-Service-Key": get_config().service_api_key}


def _create(client, content, scope="owner", **extra):
    return client.post("/api/memories", json={"content": content, "scope": scope, **extra})


def _internal_create(client, content, mode="owner", **params):
    return client.post("/api/memories/internal/create",
                       params={"content": content, "mode": mode, "importance": 0.9, **params}, headers=_key())


def _search(client, query, mode="owner", **body):
    return client.post("/api/memories/search", json={"query": query, "mode": mode, **body}, headers=_key())


def _internal_search(client, query, mode="owner", **params):
    return client.get("/api/memories/internal/search", params={"query": query, "mode": mode, **params},
                      headers=_key())


def _row(db, memory_id):
    db.expire_all()
    return db.query(Memory).filter(Memory.id == memory_id).one()


def _store_failures(logs):
    return [e for e in logs if e["event"] == "memory_vector_store_failed"]


def _unexpected(status):
    return UnexpectedResponse(status_code=status, reason_phrase="x", content=b"", headers=httpx.Headers())


@pytest.fixture
def hybrid(monkeypatch):
    def _set(enabled):
        monkeypatch.setattr(memories_module, "is_hybrid_search_enabled", lambda db: enabled)
    return _set


class _CallSpy:
    """Counts upsert/query_points on an inner client."""

    def __init__(self, inner):
        self._inner = inner
        self.upserts = 0
        self.queries = 0

    def upsert(self, *args, **kwargs):
        self.upserts += 1
        return self._inner.upsert(*args, **kwargs)

    def query_points(self, *args, **kwargs):
        self.queries += 1
        return self._inner.query_points(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# Request caps (xander N1)
# ---------------------------------------------------------------------------

def test_create_content_cap(owner_client, db):
    assert _create(owner_client, "x" * 8193).status_code == 422
    assert _create(owner_client, "x" * 8192).status_code == 201


def test_create_summary_cap(owner_client, db):
    assert _create(owner_client, "fine", summary="s" * 256).status_code == 422
    assert _create(owner_client, "fine", summary="s" * 255).status_code == 201


def test_update_caps(owner_client, db):
    memory_id = _create(owner_client, "original").json()["id"]
    assert owner_client.put(f"/api/memories/{memory_id}", json={"content": "x" * 8193}).status_code == 422
    assert owner_client.put(f"/api/memories/{memory_id}", json={"summary": "s" * 256}).status_code == 422
    assert owner_client.put(f"/api/memories/{memory_id}", json={"content": "x" * 8192}).status_code == 200


def test_internal_create_content_cap(client, db):
    assert _internal_create(client, "x" * 8193).status_code == 422
    resp = _internal_create(client, "x" * 8192)
    assert resp.status_code == 200 and resp.json()["created"] is True


# ---------------------------------------------------------------------------
# store_vector
# ---------------------------------------------------------------------------

class _UpsertNotFoundOnce:
    def __init__(self, inner):
        self._inner = inner
        self.failed = False

    def upsert(self, *args, **kwargs):
        if not self.failed:
            self.failed = True
            raise _unexpected(404)
        return self._inner.upsert(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_store_vector_upsert_404_self_heals(db):
    fake = _UpsertNotFoundOnce(QdrantClient(":memory:"))
    mv.set_client_for_tests(fake)
    assert mv.get_state().status == "ready"
    row = Memory(content="self heal", scope="owner", vector_id=str(uuid.uuid4()), importance=0.5)
    db.add(row)
    db.commit()
    assert mv.store_vector(row) is True
    assert fake.failed and row.vector_status == "stored"
    assert fake.retrieve(mv.COLLECTION_NAME, ids=[row.vector_id])


# ---------------------------------------------------------------------------
# Failure injection, fresh state (tessa H4)
# ---------------------------------------------------------------------------

def test_internal_create_survives_unavailable_store(client, db):
    mv.set_client_for_tests(failing_client())
    with capture_logs() as logs:
        resp = _internal_create(client, "the garage code is 4417")
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True and body["vector_stored"] is False
    row = _row(db, body["memory_id"])
    assert row.vector_status == "pending" and row.vector_id
    failures = _store_failures(logs)
    assert len(failures) == 1
    assert failures[0]["log_level"] == "error" and failures[0]["reason"] == "unavailable"


def test_admin_create_survives_unavailable_store(owner_client, db):
    mv.set_client_for_tests(failing_client())
    with capture_logs() as logs:
        resp = _create(owner_client, "the garage code is 4417")
    assert resp.status_code == 201
    row = _row(db, resp.json()["id"])
    assert row.vector_status == "pending" and row.vector_id
    failures = _store_failures(logs)
    assert len(failures) == 1 and failures[0]["reason"] == "unavailable"


def test_happy_path_stores_point_with_row_payload(owner_client, db):
    resp = _create(owner_client, "owner likes jazz", category="preference", importance=0.8)
    assert resp.status_code == 201
    body = resp.json()
    assert body["vector_status"] == "stored"
    [point] = mv._get_client().retrieve(mv.COLLECTION_NAME, ids=[body["vector_id"]], with_vectors=True)
    assert str(point.id) == body["vector_id"]
    payload = point.payload
    assert payload["content"] == "owner likes jazz"
    assert payload["scope"] == "owner" and payload["guest_session_id"] is None
    assert payload["category"] == "preference" and payload["importance"] == 0.8
    assert payload["memory_id"] == body["id"]
    assert payload["embedding_model"] == mv.EMBEDDING_MODEL
    assert payload["vector_written_at"]
    assert point.vector == pytest.approx(fake_embed(["owner likes jazz"])[0], abs=1e-6)


class _BadRequestUpsert:
    def __init__(self, inner):
        self._inner = inner

    def upsert(self, *args, **kwargs):
        raise _unexpected(400)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_non_transport_upsert_error_keeps_row(owner_client, db):
    mv.set_client_for_tests(_BadRequestUpsert(QdrantClient(":memory:")))
    with capture_logs() as logs:
        resp = _create(owner_client, "kept anyway")
    assert resp.status_code == 201
    assert _row(db, resp.json()["id"]).vector_status == "pending"
    assert [f["reason"] for f in _store_failures(logs)] == ["upsert_error"]


# ---------------------------------------------------------------------------
# Ready, then dead (tessa N2, bob L6)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hybrid_on", [True, False], ids=["hybrid_on", "hybrid_off"])
def test_outage_after_ready_degrades_to_keyword(client, owner_client, db, hybrid, hybrid_on):
    hybrid(hybrid_on)
    dying = DyingClient(QdrantClient(":memory:"))
    mv.set_client_for_tests(dying)
    created = _create(owner_client, "the garage code is 4417").json()
    assert created["vector_status"] == "stored"
    assert mv.get_state().status == "ready"
    dying.die()

    resp = _internal_search(client, "garage code")
    assert resp.status_code == 200
    body = resp.json()
    assert "the garage code is 4417" in [r["content"] for r in body["results"]]
    assert body["qdrant_available"] is True and body["semantic_available"] is False
    assert body["search_type"] == ("hybrid" if hybrid_on else "keyword_fallback")

    resp = _search(client, "garage code")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == D17_KEYS
    assert body["results"] == [] and body["qdrant_available"] is False and body["semantic_available"] is False
    assert isinstance(body["error"], str)
    assert mv.get_state().status == "unavailable"


def test_search_while_ready_but_query_raises_is_d17_body(client, owner_client, db):
    dying = DyingClient(QdrantClient(":memory:"))
    mv.set_client_for_tests(dying)
    _create(owner_client, "the garage code is 4417")
    assert mv.get_state().status == "ready"
    dying.die()
    body = _search(client, "garage code").json()
    assert set(body) == D17_KEYS and body["results"] == []
    assert body["qdrant_available"] is False and body["semantic_available"] is False
    assert mv.get_state().status == "unavailable"


def test_create_after_ready_then_dead_is_upsert_error(owner_client, db):
    dying = DyingClient(QdrantClient(":memory:"))
    mv.set_client_for_tests(dying)
    assert mv.get_state().status == "ready"
    dying.die()
    with capture_logs() as logs:
        resp = _create(owner_client, "written during the outage")
    assert resp.status_code == 201 and resp.json()["vector_status"] == "pending"
    assert [f["reason"] for f in _store_failures(logs)] == ["upsert_error"]
    assert mv.get_state().status == "unavailable"


# ---------------------------------------------------------------------------
# State-level branches (tessa N12)
# ---------------------------------------------------------------------------

def test_semantic_only_not_ready_falls_back_to_keyword(client, db, hybrid):
    hybrid(False)
    db.add(Memory(content="the garage code is 4417", scope="owner", vector_id=str(uuid.uuid4()),
                  importance=0.5, vector_status="pending"))
    db.commit()
    mv.set_client_for_tests(failing_client())
    body = _internal_search(client, "garage code").json()
    assert body["search_type"] == "keyword_fallback"
    assert body["qdrant_available"] is True and body["semantic_available"] is False
    assert [r["content"] for r in body["results"]] == ["the garage code is 4417"]


def test_semantic_only_ready_is_semantic(client, owner_client, db, hybrid):
    hybrid(False)
    _create(owner_client, "the garage code is 4417")
    body = _internal_search(client, "the garage code is 4417").json()
    assert body["search_type"] == "semantic"
    assert body["qdrant_available"] is True and body["semantic_available"] is True
    assert [r["content"] for r in body["results"]] == ["the garage code is 4417"]


def test_outer_except_pins_qdrant_available_false(client, db, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("postgres is down")

    monkeypatch.setattr(memories_module, "keyword_search_memories", _boom)
    monkeypatch.setattr(memories_module, "is_hybrid_search_enabled", _boom)
    body = _internal_search(client, "garage").json()
    assert body == {"results": [], "qdrant_available": False, "semantic_available": False}


# ---------------------------------------------------------------------------
# Mismatch matrix (shape and model, from real collections)
# ---------------------------------------------------------------------------

def _make_mismatch(kind):
    inner = QdrantClient(":memory:")
    if kind == "shape":
        inner.create_collection(mv.COLLECTION_NAME, vectors_config=VectorParams(size=768, distance=Distance.COSINE))
    else:
        inner.create_collection(mv.COLLECTION_NAME, vectors_config=VectorParams(size=384, distance=Distance.COSINE),
                                metadata={"embedding_model": "other/model"})
    spy = _CallSpy(inner)
    mv.set_client_for_tests(spy)
    return spy


@pytest.mark.parametrize("kind", ["shape", "model"])
def test_mismatch_matrix(client, owner_client, db, hybrid, kind):
    spy = _make_mismatch(kind)
    assert mv.get_state().status == f"{kind}_mismatch"

    created = _create(owner_client, "the garage code is 4417")
    assert created.status_code == 201 and created.json()["vector_status"] == "pending"
    memory_id = created.json()["id"]

    updated = owner_client.put(f"/api/memories/{memory_id}", json={"content": "the garage code is 5528"})
    assert updated.status_code == 200 and updated.json()["vector_status"] == "pending"

    promoted = owner_client.post(f"/api/memories/{memory_id}/promote", json={"target_scope": "global"})
    assert promoted.status_code == 200
    assert _row(db, promoted.json()["new_id"]).vector_status == "pending"

    body = _search(client, "garage code").json()
    assert set(body) == D17_KEYS and body["results"] == [] and body["semantic_available"] is False

    for on in (True, False):
        hybrid(on)
        body = _internal_search(client, "garage code").json()
        assert "the garage code is 5528" in [r["content"] for r in body["results"]]
        assert body["qdrant_available"] is True and body["semantic_available"] is False

    assert spy.upserts == 0 and spy.queries == 0


# ---------------------------------------------------------------------------
# Threshold (bob M2)
# ---------------------------------------------------------------------------

def test_hit_below_min_score_not_returned(client, owner_client, db, hybrid):
    """The row sits at cosine 0.5 from the query: below /search's default
    min_score 0.6, above internal search's similarity_threshold 0.35."""
    e1 = [0.0] * mv.EMBEDDING_DIM
    e1[0] = 1.0
    at_half = [0.0] * mv.EMBEDDING_DIM
    at_half[0], at_half[1] = 0.5, math.sqrt(0.75)

    def _controlled(texts):
        return [e1 if t == "what is the gate code" else at_half for t in texts]

    mv.set_embedder_for_tests(_controlled)
    hybrid(False)
    _create(owner_client, "the gate code is 1234")

    assert _search(client, "what is the gate code").json()["results"] == []
    body = _internal_search(client, "what is the gate code").json()
    assert body["search_type"] == "semantic"
    assert [r["content"] for r in body["results"]] == ["the gate code is 1234"]
    assert body["results"][0]["score"] == pytest.approx(0.5, abs=1e-3)


# ---------------------------------------------------------------------------
# Update ordering (xander N3, codex M1)
# ---------------------------------------------------------------------------

def test_update_marks_pending_for_every_field(owner_client, db, monkeypatch):
    seen = []
    real = mv.store_vector

    def _spy(memory):
        seen.append(memory.vector_status)
        return real(memory)

    monkeypatch.setattr(mv, "store_vector", _spy)
    memory_id = _create(owner_client, "original").json()["id"]
    seen.clear()
    for change in ({"summary": "new summary"}, {"category": "fact"}, {"importance": 0.9}, {"content": "changed"}):
        resp = owner_client.put(f"/api/memories/{memory_id}", json=change)
        assert resp.status_code == 200 and resp.json()["vector_status"] == "stored"
    assert seen == ["pending"] * 4


def test_update_crash_after_commit_leaves_pending(client, owner_client, db, test_user, monkeypatch):
    """A process death between the content commit and the vector write
    leaves the row pending with its new content, so it's never served from
    the old vector. The route coroutine is driven directly: a SystemExit
    through the TestClient would take its event-loop portal down with it."""
    import asyncio

    memory_id = _create(owner_client, "the old gate code is 1111").json()["id"]
    assert _row(db, memory_id).vector_status == "stored"

    def _crash(memory):
        raise SystemExit("killed mid-update")

    monkeypatch.setattr(mv, "store_vector", _crash)
    with pytest.raises(SystemExit):
        asyncio.run(memories_module.update_memory(
            memory_id, memories_module.MemoryUpdate(content="the new gate code is 2222"),
            db=db, current_user=test_user,
        ))

    row = _row(db, memory_id)
    assert row.content == "the new gate code is 2222" and row.vector_status == "pending"
    for query in ("the old gate code is 1111", "the new gate code is 2222"):
        assert _search(client, query, min_score=0.0).json()["results"] == []


def test_update_slow_store_not_served_with_old_vector(client, owner_client, db, monkeypatch):
    memory_id = _create(owner_client, "the old gate code is 1111").json()["id"]
    real = mv.store_vector
    entered = threading.Event()
    release = threading.Event()

    def _slow(memory):
        entered.set()
        release.wait(10)
        return real(memory)

    monkeypatch.setattr(mv, "store_vector", _slow)
    result = {}
    worker = threading.Thread(target=lambda: result.update(resp=owner_client.put(
        f"/api/memories/{memory_id}", json={"content": "the new gate code is 2222"})))
    worker.start()
    assert entered.wait(10)
    try:
        old_hits = _search(client, "the old gate code is 1111").json()["results"]
        assert memory_id not in [r["id"] for r in old_hits]
    finally:
        release.set()
        worker.join(10)
    assert result["resp"].status_code == 200
    assert _row(db, memory_id).vector_status == "stored"
    new_hits = _search(client, "the new gate code is 2222").json()["results"]
    assert [r["id"] for r in new_hits] == [memory_id]


# ---------------------------------------------------------------------------
# Promote (tessa r1 M3)
# ---------------------------------------------------------------------------

def _guest_session(db, sid=5):
    db.add(GuestSession(id=sid, guest_name="Guest", check_in_date=date(2026, 9, 1),
                        check_out_date=date(2026, 9, 30), status="active"))
    db.commit()


def test_promote_reembeds_into_owner_scope(client, owner_client, db):
    _guest_session(db, 5)
    guest = _create(owner_client, "guest prefers green tea", scope="guest", guest_session_id=5).json()
    resp = owner_client.post(f"/api/memories/{guest['id']}/promote", json={"target_scope": "owner"})
    assert resp.status_code == 200
    new = _row(db, resp.json()["new_id"])
    assert new.vector_status == "stored" and new.vector_id != guest["vector_id"]
    [point] = mv._get_client().retrieve(mv.COLLECTION_NAME, ids=[new.vector_id], with_vectors=True)
    assert point.payload["scope"] == "owner" and point.payload["guest_session_id"] is None
    assert point.vector == pytest.approx(fake_embed(["guest prefers green tea"])[0], abs=1e-6)
    hits = _search(client, "guest prefers green tea").json()["results"]
    assert new.id in [h["id"] for h in hits]


def test_promote_with_store_down_is_pending(owner_client, db):
    memory_id = _create(owner_client, "owner fact").json()["id"]
    mv.set_client_for_tests(failing_client())
    resp = owner_client.post(f"/api/memories/{memory_id}/promote", json={"target_scope": "global"})
    assert resp.status_code == 200
    assert _row(db, resp.json()["new_id"]).vector_status == "pending"


# ---------------------------------------------------------------------------
# Deletes: commit first, then the point
# ---------------------------------------------------------------------------

def test_delete_commits_then_removes_point(owner_client, db):
    created = _create(owner_client, "to be deleted").json()
    assert owner_client.delete(f"/api/memories/{created['id']}").status_code == 200
    assert _row(db, created["id"]).is_deleted is True
    assert mv._get_client().retrieve(mv.COLLECTION_NAME, ids=[created["vector_id"]]) == []


def test_forget_commits_before_point_delete_and_skips_pending(client, owner_client, db, monkeypatch):
    stored = _create(owner_client, "the garage code is 4417").json()
    pending = _create(owner_client, "the garage code is 4417 too").json()
    db.query(Memory).filter(Memory.id == pending["id"]).update({"vector_status": "pending"})
    db.commit()
    order = []
    real_delete = mv.delete_points

    def _delete(ids):
        order.append(("delete", _row(db, stored["id"]).is_deleted))
        return real_delete(ids)

    monkeypatch.setattr(mv, "delete_points", _delete)
    resp = client.post("/api/memories/internal/forget",
                       params={"search_query": "the garage code is 4417", "mode": "owner"}, headers=_key())
    assert resp.json()["deleted"] == 1
    assert order == [("delete", True)]
    assert _row(db, pending["id"]).is_deleted is False


# ---------------------------------------------------------------------------
# Search leaks (whole-body absence)
# ---------------------------------------------------------------------------

def _alias_queries(**aliases):
    """Queries embed like the text they alias, so a search can target a row
    without echoing its content back in the response body."""
    mv.set_embedder_for_tests(lambda texts: fake_embed([aliases.get(t, t) for t in texts]))


def _set_payload(point_id, **payload):
    mv._get_client().set_payload(mv.COLLECTION_NAME, payload=payload, points=[point_id])


def test_orphan_of_soft_deleted_row_not_served(client, owner_client, db):
    created = _create(owner_client, "a soft deleted secret").json()
    db.query(Memory).filter(Memory.id == created["id"]).update({"is_deleted": True})
    db.commit()
    _alias_queries(q_leak="a soft deleted secret")
    assert "a soft deleted secret" not in _search(client, "q_leak", min_score=0.0).text


def test_orphan_of_hard_deleted_row_not_served(client, owner_client, db):
    _guest_session(db, 5)
    created = _create(owner_client, "a hard deleted guest note", scope="guest", guest_session_id=5).json()
    db.query(Memory).filter(Memory.id == created["id"]).delete()
    db.commit()
    _alias_queries(q_leak="a hard deleted guest note")
    resp = _search(client, "q_leak", mode="guest", guest_session_id=5, min_score=0.0)
    assert "a hard deleted guest note" not in resp.text


@pytest.mark.parametrize("route", ["/search", "/internal/search"])
def test_cross_session_guest_row_claiming_global_not_served(client, owner_client, db, hybrid, route):
    hybrid(False)
    _guest_session(db, 5)
    _guest_session(db, 6)
    other = _create(owner_client, "guest six private note", scope="guest", guest_session_id=6).json()
    _set_payload(other["vector_id"], scope="global", guest_session_id=None)
    _alias_queries(q_leak="guest six private note")
    if route == "/search":
        resp = _search(client, "q_leak", mode="guest", guest_session_id=5, min_score=0.0)
    else:
        resp = _internal_search(client, "q_leak", mode="guest", guest_session_id=5)
    assert "guest six private note" not in resp.text


def test_payload_differing_from_row_returns_row_content(client, owner_client, db):
    created = _create(owner_client, "the real row content").json()
    _set_payload(created["vector_id"], content="tampered payload content")
    assert mv._get_client().retrieve(mv.COLLECTION_NAME, ids=[created["vector_id"]])[0].payload["content"] \
        == "tampered payload content"
    body = _search(client, "the real row content").json()
    assert [r["content"] for r in body["results"]] == ["the real row content"]
    assert "tampered" not in _search(client, "the real row content").text


def test_pending_row_with_old_point_not_served(client, owner_client, db):
    created = _create(owner_client, "stale vector row").json()
    db.query(Memory).filter(Memory.id == created["id"]).update({"vector_status": "pending"})
    db.commit()
    _alias_queries(q_leak="stale vector row")
    assert "stale vector row" not in _search(client, "q_leak", min_score=0.0).text


def test_access_count_only_for_kept_hits(client, owner_client, db):
    kept = _create(owner_client, "kept hit row").json()
    dropped = _create(owner_client, "kept hit row!").json()
    db.query(Memory).filter(Memory.id == dropped["id"]).update({"vector_status": "pending"})
    db.commit()
    _search(client, "kept hit row", min_score=0.0)
    assert _row(db, kept["id"]).access_count == 1
    assert _row(db, dropped["id"]).access_count == 0


def test_search_overfetches_past_unservable_hits(client, owner_client, db):
    """Two unservable rows outrank the live one; limit 1 still finds it
    because the query over-fetches (limit x 3)."""
    def _unit(cos):
        v = [0.0] * mv.EMBEDDING_DIM
        v[0], v[1] = cos, math.sqrt(1 - cos * cos)
        return v

    scores = {"overfetch pending a": 0.9, "overfetch pending b": 0.8, "overfetch live": 0.7}
    mv.set_embedder_for_tests(lambda texts: [_unit(scores.get(t, 1.0)) for t in texts])
    for content in ("overfetch pending a", "overfetch pending b"):
        row = _create(owner_client, content).json()
        db.query(Memory).filter(Memory.id == row["id"]).update({"vector_status": "pending"})
    db.commit()
    live = _create(owner_client, "overfetch live").json()
    body = _search(client, "overfetch query", limit=1, min_score=0.0).json()
    assert [r["id"] for r in body["results"]] == [live["id"]]


# ---------------------------------------------------------------------------
# The collection deleted between two creates
# ---------------------------------------------------------------------------

def test_collection_deleted_between_creates_recreates_and_marks_first_pending(owner_client, db):
    first = _create(owner_client, "first memory").json()
    assert first["vector_status"] == "stored"
    mv._get_client().delete_collection(mv.COLLECTION_NAME)
    second = _create(owner_client, "second memory").json()
    assert second["vector_status"] == "stored"
    assert mv._get_client().collection_exists(mv.COLLECTION_NAME)
    assert _row(db, first["id"]).vector_status == "pending"


# ---------------------------------------------------------------------------
# Pattern parity 6: module calls from async routes go through the threadpool
# ---------------------------------------------------------------------------

def test_async_routes_offload_module_calls():
    tree = ast.parse(MEMORIES_PY.read_text(encoding="utf-8"))
    direct = []
    offloaded = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "run_in_threadpool":
                offloaded += 1
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id == "memory_vectors" and func.attr != "get_state"):
                direct.append(f"{fn.name}: memory_vectors.{func.attr}(")
    assert offloaded >= 8
    assert direct == []

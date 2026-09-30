"""GET /api/memories/qdrant/health: Postgres-vs-vector counts and the
store's state, always with the Postgres counts, never with credentials."""
from __future__ import annotations

import uuid
from datetime import timedelta

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.auth.oidc import create_access_token
from app.models import Memory
from app.services import memory_vectors as mv
from shared.config import get_config
from tests.conftest import failing_client, fake_embed

HEALTH = "/api/memories/qdrant/health"


def _health(client):
    resp = client.get(HEALTH, headers={"X-Service-Key": get_config().service_api_key})
    assert resp.status_code == 200
    return resp


def _row(db, content, status="stored", deleted=False):
    row = Memory(content=content, scope="owner", vector_id=str(uuid.uuid4()), importance=0.5,
                 vector_status=status, is_deleted=deleted)
    db.add(row)
    db.commit()
    return row


def _point(point_id, text, model=mv.EMBEDDING_MODEL):
    mv._get_client().upsert(mv.COLLECTION_NAME, points=[
        PointStruct(id=point_id, vector=fake_embed([text])[0], payload={"embedding_model": model}),
    ])


def test_healthy_when_in_sync(client, db):
    mv.refresh_state()
    row = _row(db, "a")
    _point(row.vector_id, "a")
    body = _health(client).json()
    assert body["status"] == "healthy" and body["in_sync"] is True
    assert body["points_count"] == 1 and body["pg_live_count"] == 1 and body["pending_count"] == 0
    assert body["embedding_model"] == mv.EMBEDDING_MODEL
    assert body["collection_embedding_model"] == mv.EMBEDDING_MODEL and body["model_recorded"] is True


def test_pending_row_makes_degraded(client, db):
    mv.refresh_state()
    stored = _row(db, "a")
    _point(stored.vector_id, "a")
    _row(db, "b", status="pending")
    body = _health(client).json()
    assert body["status"] == "degraded" and body["in_sync"] is False
    assert (body["pg_live_count"], body["points_count"], body["pending_count"]) == (2, 1, 1)


def test_orphan_point_makes_degraded_not_healthy(client, db):
    mv.refresh_state()
    stored = _row(db, "a")
    _point(stored.vector_id, "a")
    _point(str(uuid.uuid4()), "orphan")
    body = _health(client).json()
    assert body["points_count"] == 2 and body["pg_live_count"] == 1
    assert body["status"] == "degraded" and body["in_sync"] is False


def test_soft_deleted_rows_excluded(client, db):
    mv.refresh_state()
    live = _row(db, "a")
    _point(live.vector_id, "a")
    _row(db, "gone", deleted=True)
    _row(db, "gone pending", status="pending", deleted=True)
    body = _health(client).json()
    assert body["pg_live_count"] == 1 and body["pending_count"] == 0 and body["status"] == "healthy"


def test_shape_mismatch_is_error(client, db):
    mv._get_client().create_collection(mv.COLLECTION_NAME,
                                       vectors_config=VectorParams(size=768, distance=Distance.COSINE))
    _row(db, "a")
    body = _health(client).json()
    assert body["status"] == "error" and body["state"] == "shape_mismatch"
    assert body["pg_live_count"] == 1
    assert "768" in body["detail"]


def test_model_mismatch_is_error_with_foreign_sample(client, db):
    mv.refresh_state()
    foreign = str(uuid.uuid4())
    _point(foreign, "x", model="other/model")
    mv.reset_state_for_tests()
    body = _health(client).json()
    assert body["status"] == "error" and body["state"] == "model_mismatch"
    assert body["foreign_points"] == 1 and body["foreign_point_ids_sample"] == [foreign]


def test_metadata_model_mismatch_names_collection_model(client, db):
    mv._get_client().create_collection(mv.COLLECTION_NAME,
                                       vectors_config=VectorParams(size=384, distance=Distance.COSINE),
                                       metadata={"embedding_model": "other/model"})
    body = _health(client).json()
    assert body["status"] == "error" and body["collection_embedding_model"] == "other/model"


def test_unavailable_still_reports_postgres_counts(client, db):
    _row(db, "a")
    _row(db, "b", status="pending")
    mv.set_client_for_tests(failing_client())
    body = _health(client).json()
    assert body["status"] == "unavailable" and body["state"] == "unavailable"
    assert body["pg_live_count"] == 2 and body["pending_count"] == 1
    assert body["in_sync"] is False


def test_url_userinfo_absent_from_body(client, db, monkeypatch):
    monkeypatch.setattr(mv, "QDRANT_URL", "http://qadmin:qsecret@127.0.0.1:1")
    mv.set_client_for_tests(failing_client())
    resp = _health(client)
    assert "qsecret" not in resp.text and "qadmin" not in resp.text
    assert resp.json()["url"] == "http://127.0.0.1:1"


class _LeakyClient:
    def __init__(self, inner):
        self._inner = inner

    def get_collection(self, *args, **kwargs):
        raise RuntimeError('failed "http://svc:topsecret@qdrant.internal:6333/collections/athena_memories"')

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_exception_userinfo_absent_from_body(client, db):
    mv.set_client_for_tests(_LeakyClient(QdrantClient(":memory:")))
    resp = _health(client)
    assert "topsecret" not in resp.text and "svc:" not in resp.text
    assert "qdrant.internal:6333" in resp.text


def test_healthy_after_owner_reindex(client, db, test_user, monkeypatch):
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)
    mv.refresh_state()
    _row(db, "a", status="pending")
    stored = _row(db, "b")
    _point(str(uuid.uuid4()), "orphan", model="other/model")
    assert _health(client).json()["status"] != "healthy"
    del stored
    token = create_access_token({"user_id": test_user.id, "username": test_user.username, "role": test_user.role})
    resp = client.post("/api/memories/vector-store/reindex", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    mv.reset_state_for_tests()
    body = _health(client).json()
    assert body["status"] == "healthy" and body["in_sync"] is True
    assert body["points_count"] == body["pg_live_count"] == 2


def test_pending_row_with_stale_point_is_not_in_sync(client, db):
    """Counts can match while a row still waits for its vector (an edit
    left its old point in place): pending alone keeps it out of sync."""
    mv.refresh_state()
    stored = _row(db, "a")
    _point(stored.vector_id, "a")
    edited = _row(db, "b edited", status="pending")
    _point(edited.vector_id, "b original")
    body = _health(client).json()
    assert body["points_count"] == body["pg_live_count"] == 2 and body["pending_count"] == 1
    assert body["status"] == "degraded" and body["in_sync"] is False


# ---------------------------------------------------------------------------
# Review round 1: in_sync is an exact id comparison (codex High)
# ---------------------------------------------------------------------------

def test_one_missing_plus_one_orphan_is_not_in_sync(client, db):
    """Counts match (2 live, 2 points, 0 pending) but one stored row has no
    point and one point has no row."""
    mv.refresh_state()
    present = _row(db, "a")
    missing = _row(db, "b")
    orphan = str(uuid.uuid4())
    _point(present.vector_id, "a")
    _point(orphan, "orphan")
    body = _health(client).json()
    assert (body["pg_live_count"], body["points_count"], body["pending_count"]) == (2, 2, 0)
    assert body["in_sync"] is False and body["status"] == "degraded"
    assert body["sync_scan"] == "complete"
    assert body["missing_count"] == 1 and body["missing_vector_ids_sample"] == [missing.vector_id]
    assert body["orphan_count"] == 1 and body["orphan_point_ids_sample"] == [orphan]


def test_samples_are_bounded(client, db):
    mv.refresh_state()
    live = _row(db, "a")
    _point(live.vector_id, "a")
    for i in range(12):
        _point(str(uuid.uuid4()), f"orphan {i}")
    body = _health(client).json()
    assert body["orphan_count"] == 12 and len(body["orphan_point_ids_sample"]) == 10
    assert body["in_sync"] is False


def test_partial_scan_is_never_reported_in_sync(client, db, monkeypatch):
    monkeypatch.setattr(mv, "_SCROLL_PAGE", 2)
    monkeypatch.setattr(mv, "SYNC_SCAN_MAX_PAGES", 1)
    mv.refresh_state()
    for text in ("a", "b", "c"):
        row = _row(db, text)
        _point(row.vector_id, text)
    body = _health(client).json()
    assert body["sync_scan"] == "partial"
    assert body["in_sync"] is None and body["status"] == "degraded"


def test_exact_in_sync_is_healthy(client, db):
    mv.refresh_state()
    for text in ("a", "b"):
        row = _row(db, text)
        _point(row.vector_id, text)
    body = _health(client).json()
    assert body["in_sync"] is True and body["status"] == "healthy"
    assert body["missing_count"] == 0 and body["orphan_count"] == 0
    assert body["missing_vector_ids_sample"] == [] and body["orphan_point_ids_sample"] == []


def test_unavailable_scan_is_skipped(client, db):
    _row(db, "a")
    mv.set_client_for_tests(failing_client())
    body = _health(client).json()
    assert body["sync_scan"] == "skipped" and body["in_sync"] is False


def test_scroll_budget_exhausted_is_partial(client, db, monkeypatch):
    """Rows fit the budget but the points don't (orphans): the id scan stops
    at SYNC_SCAN_MAX_PAGES and reports partial, not a verdict."""
    monkeypatch.setattr(mv, "_SCROLL_PAGE", 2)
    monkeypatch.setattr(mv, "SYNC_SCAN_MAX_PAGES", 2)
    mv.refresh_state()
    for text in ("a", "b"):
        row = _row(db, text)
        _point(row.vector_id, text)
    for i in range(3):
        _point(str(uuid.uuid4()), f"orphan {i}")
    body = _health(client).json()
    assert body["pg_live_count"] == 2
    assert body["sync_scan"] == "partial" and body["in_sync"] is None and body["status"] == "degraded"


def test_missing_point_alone_is_not_in_sync(client, db):
    """A stored memory whose point is gone, nothing orphaned or pending."""
    mv.refresh_state()
    present = _row(db, "a")
    missing = _row(db, "b")
    _point(present.vector_id, "a")
    body = _health(client).json()
    assert body["orphan_count"] == 0 and body["pending_count"] == 0
    assert body["missing_count"] == 1 and body["missing_vector_ids_sample"] == [missing.vector_id]
    assert body["in_sync"] is False and body["status"] == "degraded"

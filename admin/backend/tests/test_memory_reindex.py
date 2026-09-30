"""Rebuild from Postgres: reindex modes, the race-safe prune, the lease and
cooldown, the automatic pending pass, the CLI and the HTTP route (D7, D8,
D15, D19)."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, timedelta

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

import app.routes.memories as memories_module
from app.auth.oidc import create_access_token
from app.models import AuditLog, GuestSession, Memory, SystemSetting
from app.services import memory_vectors as mv
from app.services import settings_lease
from shared.config import get_config
from tests.conftest import TestingSessionLocal, fake_embed

REINDEX = "/api/memories/vector-store/reindex"


def _key():
    return {"X-Service-Key": get_config().service_api_key}


def _bearer(user):
    token = create_access_token({"user_id": user.id, "username": user.username, "role": user.role})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def no_dev_auth(monkeypatch):
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)


def _raw():
    return mv._get_client()


def _row(db, content, *, status="stored", deleted=False, scope="owner", guest_session_id=None):
    row = Memory(content=content, scope=scope, guest_session_id=guest_session_id, vector_id=str(uuid.uuid4()),
                 importance=0.5, category="fact", source_type="manual", vector_status=status, is_deleted=deleted)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _point(point_id, text, *, age=None, written_at=None, model=mv.EMBEDDING_MODEL, clock=None, content=None,
           vector=None, **payload):
    if written_at is None:
        written_at = (clock.utcnow() - (age or timedelta(0))).isoformat()
    body = {"content": content if content is not None else text, "scope": "owner",
            "vector_written_at": written_at, **payload}
    if model is not None:
        body["embedding_model"] = model
    _raw().upsert(mv.COLLECTION_NAME, points=[
        PointStruct(id=point_id, vector=vector or fake_embed([text])[0], payload=body),
    ])


def _point_ids():
    ids, offset = set(), None
    while True:
        points, offset = _raw().scroll(mv.COLLECTION_NAME, limit=100, offset=offset, with_payload=False)
        ids.update(str(p.id) for p in points)
        if offset is None:
            return ids


def _vector(point_id):
    return _raw().retrieve(mv.COLLECTION_NAME, ids=[point_id], with_vectors=True)[0].vector


def _expire_lease(db):
    db.expire_all()
    row = db.query(SystemSetting).filter(SystemSetting.key == mv.LEASE_KEY).first()
    if row is None:
        return
    value = json.loads(row.value)
    value["expires_at"] = (mv._clock.utcnow() - timedelta(hours=1)).isoformat()
    row.value = json.dumps(value, sort_keys=True)
    db.commit()


def _statuses(db, *rows):
    db.expire_all()
    return [db.query(Memory).get(r.id).vector_status for r in rows]


@pytest.fixture
def world(db, memory_vector_test_env):
    """Live A (stored, point), B (stored, no point), C (pending, stale point);
    soft-deleted D with a point; orphans E (1 h), F (now), G (foreign, now),
    H (written a day in the future)."""
    clock = memory_vector_test_env
    assert mv.refresh_state().status == "ready"
    a = _row(db, "alpha memory")
    b = _row(db, "bravo memory")
    c = _row(db, "charlie memory", status="pending")
    d = _row(db, "delta memory", deleted=True)
    _point(a.vector_id, "alpha memory", age=timedelta(hours=1), clock=clock)
    _point(c.vector_id, "charlie OLD text", age=timedelta(hours=1), clock=clock)
    _point(d.vector_id, "delta memory", age=timedelta(hours=1), clock=clock)
    orphans = {name: str(uuid.uuid4()) for name in "EFGH"}
    _point(orphans["E"], "echo", age=timedelta(hours=1), clock=clock)
    _point(orphans["F"], "foxtrot", clock=clock)
    _point(orphans["G"], "golf", clock=clock, model="other/model")
    _point(orphans["H"], "hotel", written_at=(clock.utcnow() + timedelta(days=1)).isoformat())
    return {"a": a, "b": b, "c": c, "d": d, "orphans": orphans, "clock": clock,
            "snapshot": {r.id: r.vector_id for r in (a, b, c)}}


# ---------------------------------------------------------------------------
# mode=missing (owner, prune)
# ---------------------------------------------------------------------------

def test_missing_reuses_snapshot_ids_and_reembeds_pending(db, world):
    report = mv.reindex("missing", prune=True, caller="cli")
    assert report.refused is None and report.aborted is None
    db.expire_all()
    assert {r.id: r.vector_id for r in db.query(Memory).filter(Memory.id.in_(world["snapshot"]))} == world["snapshot"]
    expected = set(world["snapshot"].values()) | {world["orphans"]["F"]}
    assert len(world["snapshot"]) >= 3 and world["b"].vector_id in expected
    assert _point_ids() == expected
    assert report.embedded == 2
    assert report.orphans_pruned == 4
    assert report.foreign_orphans_pruned == 1
    assert report.prune_deferred_recent == 1
    assert _vector(world["c"].vector_id) == pytest.approx(fake_embed(["charlie memory"])[0], abs=1e-6)
    assert _raw().retrieve(mv.COLLECTION_NAME, ids=[world["c"].vector_id])[0].payload["content"] == "charlie memory"
    assert _statuses(db, world["a"], world["b"], world["c"]) == ["stored"] * 3


def test_second_run_after_cooldown_is_a_noop(db, world):
    mv.reindex("missing", prune=True)
    _expire_lease(db)
    report = mv.reindex("missing", prune=True)
    assert report.embedded == 0 and report.orphans_pruned == 0


def test_foreign_orphan_pruned_at_any_age(db, world):
    report = mv.reindex("missing", prune=True)
    assert world["orphans"]["G"] not in _point_ids()
    assert report.foreign_orphans_pruned == 1


def test_future_dated_orphan_counts_as_old(db, world):
    mv.reindex("missing", prune=True)
    assert world["orphans"]["H"] not in _point_ids()


def test_dry_run_changes_nothing(db, world):
    before_points = _point_ids()
    before_status = _statuses(db, world["a"], world["b"], world["c"])
    report = mv.reindex("missing", prune=True, dry_run=True)
    assert report.would_prune == 4 and report.orphans_pruned == 0 and report.embedded == 0
    assert _point_ids() == before_points
    assert _statuses(db, world["a"], world["b"], world["c"]) == before_status


def test_dry_run_releases_lease(db, world):
    mv.reindex("missing", dry_run=True)
    db.expire_all()
    assert db.query(SystemSetting).filter(SystemSetting.key == mv.LEASE_KEY).first() is None


def test_real_run_arms_cooldown(db, world):
    mv.reindex("missing")
    with pytest.raises(mv.ReindexBusy) as busy:
        mv.reindex("missing")
    assert 1 <= busy.value.retry_after_seconds <= mv.COOLDOWN_SECONDS


def test_pagination_prunes_every_page(db, memory_vector_test_env, monkeypatch):
    clock = memory_vector_test_env
    monkeypatch.setattr(mv, "_SCROLL_PAGE", 10)
    mv.refresh_state()
    live = _row(db, "the one live row", status="pending")
    orphans = [str(uuid.uuid4()) for _ in range(25)]
    for i, oid in enumerate(orphans):
        _point(oid, f"orphan {i}", age=timedelta(hours=2), clock=clock)
    report = mv.reindex("missing", prune=True)
    assert report.orphans_pruned == 25
    assert _point_ids() == {live.vector_id}


def test_empty_source_floor_prunes_nothing(db, memory_vector_test_env):
    clock = memory_vector_test_env
    mv.refresh_state()
    orphan = str(uuid.uuid4())
    _point(orphan, "orphan", age=timedelta(hours=2), clock=clock)
    report = mv.reindex("missing", prune=True)
    assert report.prune_skipped is True and report.orphans_pruned == 0
    assert _point_ids() == {orphan}


def test_prune_spares_row_and_point_created_mid_run(db, world, monkeypatch):
    real = mv._prune_orphans
    added = {}

    def _insert_then_prune(*args, **kwargs):
        row = _row(db, "india memory", status="pending")
        mv.store_vector(mv.snapshot(row))
        added["row"] = row
        return real(*args, **kwargs)

    monkeypatch.setattr(mv, "_prune_orphans", _insert_then_prune)
    mv.reindex("missing", prune=True)
    assert added["row"].vector_id in _point_ids()
    assert _statuses(db, added["row"]) == ["stored"]


def test_content_changed_mid_batch_is_reembedded(db, world):
    changed = {}

    def _embed_and_edit(texts):
        if "alpha memory" in texts and not changed:
            changed["done"] = True
            session = TestingSessionLocal()
            session.query(Memory).filter(Memory.id == world["a"].id).update({"content": "alpha EDITED"})
            session.commit()
            session.close()
        return fake_embed(texts)

    mv.set_embedder_for_tests(_embed_and_edit)
    report = mv.reindex("all")
    assert changed and report.failed == 0
    assert _vector(world["a"].vector_id) == pytest.approx(fake_embed(["alpha EDITED"])[0], abs=1e-6)
    assert _statuses(db, world["a"]) == ["stored"]


# ---------------------------------------------------------------------------
# mode=all + model_mismatch
# ---------------------------------------------------------------------------

def _other_embed(texts):
    return fake_embed([f"other-model::{t}" for t in texts])


@pytest.fixture
def foreign_world(db, memory_vector_test_env):
    clock = memory_vector_test_env
    _raw().create_collection(mv.COLLECTION_NAME, vectors_config=VectorParams(size=384, distance=Distance.COSINE),
                             metadata={"embedding_model": "other/model"})
    rows = [_row(db, "alpha memory"), _row(db, "bravo memory"), _row(db, "charlie memory")]
    for row in rows:
        _point(row.vector_id, row.content, age=timedelta(hours=1), clock=clock, model="other/model",
               vector=_other_embed([row.content])[0])
    assert mv.refresh_state().status == "model_mismatch"
    return rows


def test_all_rebuilds_model_mismatch_in_place(db, foreign_world):
    snapshot = {r.id: r.vector_id for r in foreign_world}
    report = mv.reindex("all", prune=True)
    assert report.failed == 0
    db.expire_all()
    assert {r.id: r.vector_id for r in db.query(Memory).filter(Memory.id.in_(snapshot))} == snapshot
    assert _point_ids() == set(snapshot.values())
    for row in foreign_world:
        assert _vector(row.vector_id) == pytest.approx(fake_embed([row.content])[0], abs=1e-6)
    assert _raw().get_collection(mv.COLLECTION_NAME).config.metadata["embedding_model"] == mv.EMBEDDING_MODEL
    assert mv.get_state().status == "ready"


def test_missing_refused_on_model_mismatch(db, foreign_world):
    assert mv.reindex("missing").refused == "model_mismatch"


class _MetadataDroppingClient:
    def __init__(self, inner):
        self._inner = inner

    def create_collection(self, name, vectors_config=None, metadata=None, **kwargs):
        return self._inner.create_collection(name, vectors_config=vectors_config, **kwargs)

    def update_collection(self, name, metadata=None, **kwargs):
        return True

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_metadata_dropping_server_recovers_by_foreign_recount(db, memory_vector_test_env):
    clock = memory_vector_test_env
    mv.set_client_for_tests(_MetadataDroppingClient(QdrantClient(":memory:")))
    mv.refresh_state()
    rows = [_row(db, "alpha memory"), _row(db, "bravo memory")]
    for row in rows:
        _point(row.vector_id, row.content, age=timedelta(hours=1), clock=clock, model="other/model")
    state = mv.refresh_state()
    assert state.status == "model_mismatch" and state.foreign_points == 2
    mv.reindex("all")
    state = mv.get_state()
    assert state.status == "ready" and state.foreign_points == 0 and state.model_recorded is False


def test_finalize_guard_keeps_mismatch_when_a_row_fails(db, foreign_world, monkeypatch):
    monkeypatch.setattr(mv, "ROW_BATCH", 1)

    def _fails_on_bravo(texts):
        if "bravo memory" in texts:
            raise RuntimeError("embedder broke on bravo")
        return fake_embed(texts)

    mv.set_embedder_for_tests(_fails_on_bravo)
    assert mv.main(["reindex", "--mode", "all"]) == 1
    assert _raw().get_collection(mv.COLLECTION_NAME).config.metadata["embedding_model"] == "other/model"
    assert mv.refresh_state().status == "model_mismatch"
    assert _statuses(db, foreign_world[1]) == ["pending"]


# ---------------------------------------------------------------------------
# Recreate (CLI only)
# ---------------------------------------------------------------------------

@pytest.fixture
def wide_world(db):
    _raw().create_collection(mv.COLLECTION_NAME, vectors_config=VectorParams(size=768, distance=Distance.COSINE))
    rows = [_row(db, "alpha memory"), _row(db, "bravo memory")]
    assert mv.refresh_state().status == "shape_mismatch"
    return rows


def _size():
    return _raw().get_collection(mv.COLLECTION_NAME).config.params.vectors.size


def test_shape_mismatch_without_recreate_is_refused(db, wide_world):
    assert mv.reindex("all").refused == "shape_mismatch"
    assert _size() == 768


def test_recreate_with_confirmation_rebuilds_with_same_ids(db, wide_world, capsys):
    snapshot = {r.id: r.vector_id for r in wide_world}
    assert mv.main(["reindex", "--recreate", "--confirm-collection", "athena_memories"]) == 0
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert report["embedded"] == 2
    assert _size() == 384
    assert _point_ids() == set(snapshot.values())
    assert mv.get_state().status == "ready"


def test_recreate_wrong_confirmation_exits_2(db, wide_world):
    assert mv.main(["reindex", "--recreate", "--confirm-collection", "wrong"]) == 2
    assert mv.main(["reindex", "--recreate"]) == 2
    assert _size() == 768


def test_recreate_dry_run_changes_nothing(db, wide_world, capsys):
    assert mv.main(["reindex", "--recreate", "--confirm-collection", "athena_memories", "--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert report["would_recreate"] is True
    assert _size() == 768


def test_recreate_while_lease_held_exits_2(db, wide_world):
    _hold_rebuild_lease(ttl=300)
    assert mv.main(["reindex", "--recreate", "--confirm-collection", "athena_memories"]) == 2
    assert _size() == 768


def test_cli_batch_size_is_bounded(db):
    with pytest.raises(SystemExit) as info:
        mv.main(["reindex", "--batch-size", "65"])
    assert info.value.code == 2


def test_cli_help_needs_no_database(capsys):
    with pytest.raises(SystemExit) as info:
        mv.main(["--help"])
    assert info.value.code == 0
    assert "reindex" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Guest isolation after a rebuild (xander L4)
# ---------------------------------------------------------------------------

def test_guest_isolation_after_rebuild(client, db):
    for sid in (5, 6):
        db.add(GuestSession(id=sid, guest_name="G", check_in_date=date(2026, 9, 1),
                            check_out_date=date(2026, 9, 30), status="active"))
    db.commit()
    mv.refresh_state()
    mine = _row(db, "guest five note", status="pending", scope="guest", guest_session_id=5)
    theirs = _row(db, "guest six note", status="pending", scope="guest", guest_session_id=6)
    mv.reindex("missing")
    payload = _raw().retrieve(mv.COLLECTION_NAME, ids=[theirs.vector_id])[0].payload
    assert payload["scope"] == "guest" and payload["guest_session_id"] == 6
    resp = client.post("/api/memories/search", headers=_key(),
                       json={"query": "guest six note", "mode": "guest", "guest_session_id": 5, "min_score": 0.0})
    assert theirs.id not in [r["id"] for r in resp.json()["results"]]
    resp = client.post("/api/memories/search", headers=_key(),
                       json={"query": "guest five note", "mode": "guest", "guest_session_id": 5})
    assert [r["id"] for r in resp.json()["results"]] == [mine.id]


# ---------------------------------------------------------------------------
# Lease (tessa N6)
# ---------------------------------------------------------------------------

def test_lease_lost_aborts(client, db, test_user, no_dev_auth, monkeypatch, capsys):
    mv.refresh_state()
    monkeypatch.setattr(mv, "ROW_BATCH", 1)
    rows = [_row(db, f"row {i}", status="pending") for i in range(3)]
    monkeypatch.setattr(settings_lease, "renew", lambda *args, **kwargs: False)

    resp = client.post(REINDEX, params={"mode": "missing"}, headers=_bearer(test_user))
    assert resp.status_code == 409 and resp.json()["error"] == "reindex_aborted"
    assert resp.json()["aborted"] == "lease_lost"
    assert _statuses(db, *rows) == ["stored", "pending", "pending"]

    assert mv.main(["reindex"]) == 1
    assert _statuses(db, *rows) == ["stored", "pending", "pending"]


def test_cooldown_blocks_then_expires(client, db, test_user, no_dev_auth):
    mv.refresh_state()
    assert client.post(REINDEX, headers=_bearer(test_user)).status_code == 200
    resp = client.post(REINDEX, headers=_bearer(test_user))
    assert resp.status_code == 409 and resp.json()["error"] == "reindex_busy"
    assert 1 <= resp.json()["retry_after_seconds"] <= 60
    _expire_lease(db)
    assert client.post(REINDEX, headers=_bearer(test_user)).status_code == 200


def test_live_run_retry_after_bounds(client, db, test_user, no_dev_auth, memory_vector_test_env):
    clock = memory_vector_test_env
    mv.refresh_state()
    _hold_rebuild_lease(ttl=300)
    resp = client.post(REINDEX, headers=_bearer(test_user))
    assert resp.status_code == 409
    assert 1 <= resp.json()["retry_after_seconds"] <= 300
    # Derived from the holder's expiry, not a constant: exact under the fake clock.
    assert resp.json()["retry_after_seconds"] == 300
    clock.advance(100)
    assert client.post(REINDEX, headers=_bearer(test_user)).json()["retry_after_seconds"] == 200
    clock.advance(199.5)
    assert client.post(REINDEX, headers=_bearer(test_user)).json()["retry_after_seconds"] == 1


def test_dry_run_does_not_arm_cooldown(client, db, test_user, no_dev_auth):
    mv.refresh_state()
    assert client.post(REINDEX, params={"dry_run": "true"}, headers=_bearer(test_user)).status_code == 200
    assert client.post(REINDEX, headers=_bearer(test_user)).status_code == 200


def test_auto_pass_releases(client, db, test_user, no_dev_auth):
    _row(db, "pending row", status="pending")
    asyncio.run(mv.tick())
    assert client.post(REINDEX, headers=_bearer(test_user)).status_code == 200


# ---------------------------------------------------------------------------
# Automatic pending-only pass (D19)
# ---------------------------------------------------------------------------

def test_first_tick_after_start_runs_pending_pass(db):
    rows = [_row(db, "one", status="pending"), _row(db, "two", status="pending")]
    assert mv._prev_status is None
    asyncio.run(mv.tick())
    assert _statuses(db, *rows) == ["stored", "stored"]


def test_pending_only_reaches_high_ids(db, monkeypatch):
    monkeypatch.setattr(mv, "PENDING_PASS_MAX_ROWS", 2)
    mv.refresh_state()
    rows = [_row(db, "low one"), _row(db, "low two"), _row(db, "high", status="pending")]
    asyncio.run(mv.tick())
    assert _statuses(db, *rows)[-1] == "stored"


def test_pending_pass_cap(db, monkeypatch):
    monkeypatch.setattr(mv, "PENDING_PASS_MAX_ROWS", 2)
    rows = [_row(db, f"p{i}", status="pending") for i in range(3)]
    asyncio.run(mv.tick())
    assert _statuses(db, *rows) == ["stored", "stored", "pending"]


def test_interval_branch(db, memory_vector_test_env):
    clock = memory_vector_test_env
    asyncio.run(mv.tick())
    row = _row(db, "late pending", status="pending")
    clock.advance(599)
    asyncio.run(mv.tick())
    assert _statuses(db, row) == ["pending"]
    clock.advance(2)
    asyncio.run(mv.tick())
    assert _statuses(db, row) == ["stored"]


def test_no_pass_without_pending_rows_after_first(db, memory_vector_test_env, monkeypatch):
    clock = memory_vector_test_env
    asyncio.run(mv.tick())
    calls = []
    monkeypatch.setattr(mv, "reindex", lambda *a, **k: calls.append(1))
    clock.advance(601)
    asyncio.run(mv.tick())
    assert calls == []


def test_busy_lease_skips_then_retries(db, memory_vector_test_env):
    clock = memory_vector_test_env
    row = _row(db, "waiting", status="pending")
    lease = _hold_rebuild_lease(ttl=300)
    asyncio.run(mv.tick())
    assert _statuses(db, row) == ["pending"]
    settings_lease.release(TestingSessionLocal, lease)
    asyncio.run(mv.tick())
    assert _statuses(db, row) == ["stored"]


def test_auto_pass_never_prunes(db, memory_vector_test_env):
    clock = memory_vector_test_env
    mv.refresh_state()
    _row(db, "live", status="pending")
    orphan = str(uuid.uuid4())
    _point(orphan, "echo", age=timedelta(hours=1), clock=clock)
    mv.reset_state_for_tests()
    asyncio.run(mv.tick())
    assert orphan in _point_ids()


def test_not_ready_tick_runs_no_pass(db, monkeypatch):
    from tests.conftest import failing_client

    mv.set_client_for_tests(failing_client())
    calls = []
    monkeypatch.setattr(mv, "reindex", lambda *a, **k: calls.append(1))
    asyncio.run(mv.tick())
    assert calls == []


def test_collection_lost_rows_restored_by_tick(owner_client, db):
    """Phase 2's lost-collection case, finished: the first row, marked
    pending by the re-create, is stored again by the next tick."""
    first = owner_client.post("/api/memories", json={"content": "first memory", "scope": "owner"}).json()
    _raw().delete_collection(mv.COLLECTION_NAME)
    owner_client.post("/api/memories", json={"content": "second memory", "scope": "owner"})
    db.expire_all()
    assert db.query(Memory).get(first["id"]).vector_status == "pending"
    asyncio.run(mv.tick())
    db.expire_all()
    assert db.query(Memory).get(first["id"]).vector_status == "stored"
    assert first["vector_id"] in _point_ids()


# ---------------------------------------------------------------------------
# Auth kind (xander N4) and the HTTP matrix
# ---------------------------------------------------------------------------

def test_garbage_key_with_owner_bearer_is_401(client, db, test_user, no_dev_auth):
    mv.refresh_state()
    resp = client.post(REINDEX, headers={**_bearer(test_user), "X-Service-Key": "garbage"})
    assert resp.status_code == 401


def test_valid_key_with_operator_bearer_is_service(client, db, operator_user, no_dev_auth):
    mv.refresh_state()
    headers = {**_bearer(operator_user), **_key()}
    resp = client.post(REINDEX, params={"mode": "missing"}, headers=headers)
    assert resp.status_code == 200 and resp.json()["orphans_pruned"] == 0
    resp = client.post(REINDEX, params={"mode": "all"}, headers=headers)
    assert resp.status_code == 403 and resp.json()["detail"] == "service_key_limited_to_missing"


def test_garbage_key_with_operator_bearer_on_health_is_401(client, db, operator_user, no_dev_auth):
    resp = client.get("/api/memories/qdrant/health", headers={**_bearer(operator_user), "X-Service-Key": "garbage"})
    assert resp.status_code == 401


def test_http_matrix(client, db, test_user, operator_user, no_dev_auth):
    mv.refresh_state()
    assert client.post(REINDEX).status_code == 401
    assert client.post(REINDEX, headers=_bearer(operator_user)).status_code == 403
    for mode in ("missing", "all"):
        _expire_lease(db)
        resp = client.post(REINDEX, params={"mode": mode}, headers=_bearer(test_user))
        assert resp.status_code == 200, resp.text
        assert resp.json()["mode"] == mode
    _expire_lease(db)
    resp = client.post(REINDEX, params={"mode": "missing"}, headers=_key())
    assert resp.status_code == 200 and resp.json()["orphans_pruned"] == 0
    _expire_lease(db)
    assert client.post(REINDEX, params={"mode": "all"}, headers=_key()).status_code == 403
    assert client.post(REINDEX, params={"mode": "bogus"}, headers=_bearer(test_user)).status_code == 422


def test_service_run_never_prunes(client, db, memory_vector_test_env):
    clock = memory_vector_test_env
    mv.refresh_state()
    _row(db, "live", status="pending")
    orphan = str(uuid.uuid4())
    _point(orphan, "echo", age=timedelta(hours=1), clock=clock)
    resp = client.post(REINDEX, params={"mode": "missing"}, headers=_key())
    assert resp.status_code == 200 and resp.json()["orphans_pruned"] == 0
    assert orphan in _point_ids()


def test_owner_run_is_audited(client, db, test_user, no_dev_auth):
    mv.refresh_state()
    assert client.post(REINDEX, headers=_bearer(test_user)).status_code == 200
    db.expire_all()
    audit = db.query(AuditLog).filter(AuditLog.action == "memory_vector_reindex").one()
    assert audit.user_id == test_user.id and audit.success is True


def test_refused_is_409(client, db, test_user, no_dev_auth):
    _raw().create_collection(mv.COLLECTION_NAME, vectors_config=VectorParams(size=768, distance=Distance.COSINE))
    mv.refresh_state()
    resp = client.post(REINDEX, params={"mode": "all"}, headers=_bearer(test_user))
    assert resp.status_code == 409
    assert resp.json()["error"] == "reindex_refused" and resp.json()["refused"] == "shape_mismatch"


def test_vector_store_route_population():
    gated = []
    for route in memories_module.router.routes:
        if "/vector-store/" in route.path:
            calls = [d.call for d in route.dependant.dependencies]
            assert memories_module.require_memory_maintainer in calls, route.path
            gated.append(route.path)
    assert len(gated) >= 1
    assert REINDEX in gated


# ---------------------------------------------------------------------------
# The rebuild lease lives on the shared settings_lease core
# ---------------------------------------------------------------------------

def _hold_rebuild_lease(ttl):
    """Another replica's rebuild holding the main lease."""
    return settings_lease.acquire(TestingSessionLocal, mv.LEASE_KEY, category="memory_vectors", ttl=ttl,
                                  busy_message="busy", fields={"action": "all"}, now=mv._clock.utcnow)


def test_rebuild_lease_row_is_the_core_format(db):
    mv.refresh_state()
    mv.reindex("missing")
    info = settings_lease.read(db, mv.LEASE_KEY)
    assert info["action"] == "missing" and info["holder"] and info["expires_at"]
    db.expire_all()
    assert db.query(SystemSetting).filter(SystemSetting.key == mv.LEASE_KEY).one().category == "memory_vectors"


def test_memory_vectors_uses_no_service_control_lease_wrappers():
    import inspect

    source = inspect.getsource(mv)
    assert "service_control_settings" not in source and "settings_lease" in source


# ---------------------------------------------------------------------------
# Review round 1
# ---------------------------------------------------------------------------

def test_reindex_skips_row_deleted_mid_batch(db):
    """A forget that commits while its row is being embedded wins: no point,
    and the row never reads stored (xander L1)."""
    mv.refresh_state()
    keep = _row(db, "keep this memory", status="pending")
    doomed = _row(db, "forget this memory", status="pending")

    def _embed_and_forget(texts):
        if "forget this memory" in texts:
            session = TestingSessionLocal()
            session.query(Memory).filter(Memory.id == doomed.id).update({"is_deleted": True})
            session.commit()
            session.close()
        return fake_embed(texts)

    mv.set_embedder_for_tests(_embed_and_forget)
    report = mv.reindex("missing")
    assert report.failed == 0
    assert _statuses(db, keep, doomed) == ["stored", "pending"]
    assert _point_ids() == {keep.vector_id}


def test_dry_runs_use_their_own_lease(db):
    mv.refresh_state()
    mv.reindex("missing", dry_run=True)
    db.expire_all()
    keys = {row.key for row in db.query(SystemSetting).all()}
    assert mv.LEASE_KEY_DRYRUN in keys and mv.LEASE_KEY not in keys
    with pytest.raises(mv.ReindexBusy) as busy:
        mv.reindex("missing", dry_run=True)
    assert 1 <= busy.value.retry_after_seconds <= mv.COOLDOWN_SECONDS


def test_back_to_back_service_dry_runs_never_block_auto_pass_or_owner(client, db, test_user, no_dev_auth):
    """xander L2: a service-key caller looping dry runs can't starve the
    automatic pass or the owner."""
    mv.refresh_state()
    row = _row(db, "waiting for its vector", status="pending")
    assert client.post(REINDEX, params={"dry_run": "true"}, headers=_key()).status_code == 200
    second = client.post(REINDEX, params={"dry_run": "true"}, headers=_key())
    assert second.status_code == 409 and second.json()["error"] == "reindex_busy"
    mv.reset_state_for_tests()
    asyncio.run(mv.tick())
    assert _statuses(db, row) == ["stored"]
    assert client.post(REINDEX, headers=_bearer(test_user)).status_code == 200


def test_real_run_does_not_block_a_dry_run(db):
    mv.refresh_state()
    mv.reindex("missing")
    assert mv.reindex("missing", dry_run=True).refused is None


def test_tick_never_probes_on_the_event_loop(db, monkeypatch):
    """xander L3: when the cache was invalidated between the due-check and
    the read, the probe still runs off the loop thread."""
    import threading

    inner = QdrantClient(":memory:")
    probe_threads = []

    class _Recording:
        def collection_exists(self, name):
            probe_threads.append(threading.get_ident())
            return inner.collection_exists(name)

        def __getattr__(self, name):
            return getattr(inner, name)

    mv.set_client_for_tests(_Recording())
    monkeypatch.setattr(mv, "_revalidation_due", lambda: False)

    async def _run():
        loop_thread = threading.get_ident()
        await mv.tick()
        return loop_thread

    loop_thread = asyncio.run(_run())
    assert probe_threads and loop_thread not in probe_threads

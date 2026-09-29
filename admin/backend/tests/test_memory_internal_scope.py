"""Memory scope and auth (V3.1, D7, PP8).

One scope helper decides what a caller may read, delete and create: owner
reads and deletes global + owner and creates owner; a guest with a session
reads global + that session's guest memories, deletes only those, and
creates guest; a guest without a session reads global only, deletes
nothing and creates nothing. Any mode other than "owner" is a guest.

The /internal/* routes require X-Service-Key (401 missing/wrong, 503 when
the server has none), and the four read routes that used to be open
require a service key or a signed-in user.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest import mock

import pytest

import app.routes.memories as memories_module
from app.auth.oidc import create_access_token
from datetime import date

from app.models import GuestSession, Memory
from shared.config import get_config

INTERNAL_ROUTES = [
    ("GET", "/api/memories/internal/search", {"query": "garage"}),
    ("POST", "/api/memories/internal/create", {"content": "x", "mode": "owner", "importance": 0.1}),
    ("POST", "/api/memories/internal/forget", {"search_query": "garage"}),
]

READ_ROUTES = [
    ("GET", "/api/memories", None),
    ("POST", "/api/memories/search", {"query": "garage", "mode": "owner"}),
    ("GET", "/api/memories/guest-sessions/active", None),
    ("GET", "/api/memories/qdrant/health", None),
]


def _key_headers():
    key = get_config().service_api_key
    assert key, "conftest sets SERVICE_API_KEY"
    return {"X-Service-Key": key}


def _call(client, method, path, params_or_body, headers=None):
    if method == "GET":
        return client.get(path, params=params_or_body, headers=headers or {})
    if path.endswith("/search") and "/internal/" not in path:
        return client.post(path, json=params_or_body, headers=headers or {})
    return client.post(path, params=params_or_body, headers=headers or {})


@pytest.fixture
def no_qdrant(monkeypatch):
    monkeypatch.setattr(memories_module, "check_qdrant_available", mock.AsyncMock(return_value=False))
    monkeypatch.setattr(memories_module, "get_qdrant", lambda: None)


def _session(db, guest_id=5):
    session = GuestSession(
        id=guest_id, guest_name="Guest", check_in_date=date(2026, 9, 1),
        check_out_date=date(2026, 9, 30), status="active",
    )
    db.add(session)
    db.commit()
    return session


def _memory(db, content, scope, guest_session_id=None, vector_id=None):
    memory = Memory(
        content=content, scope=scope, guest_session_id=guest_session_id,
        vector_id=vector_id or str(uuid.uuid4()), category="fact", importance=0.9, source_type="conversation",
    )
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return memory


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method, path, params", INTERNAL_ROUTES, ids=[r[1] for r in INTERNAL_ROUTES])
def test_internal_routes_require_service_key(client, no_qdrant, method, path, params):
    """Floor 3; named member /api/memories/internal/forget."""
    assert _call(client, method, path, params).status_code == 401
    assert _call(client, method, path, params, {"X-Service-Key": "wrong"}).status_code == 401
    assert _call(client, method, path, params, _key_headers()).status_code == 200


def test_internal_route_population():
    """PP8: every /internal/ route carries require_service_key_401."""
    from app.routes.internal import require_service_key_401

    paths = []
    for route in memories_module.router.routes:
        if "/internal/" in route.path:
            deps = [d.call for d in route.dependant.dependencies]
            assert require_service_key_401 in deps, route.path
            paths.append(route.path)
    assert len(paths) >= 3
    assert "/api/memories/internal/forget" in paths


def test_internal_route_503_when_server_has_no_key(client, no_qdrant, monkeypatch):
    import app.routes.internal as internal_module

    monkeypatch.setattr(internal_module, "get_config", lambda: SimpleNamespace(service_api_key=""))
    resp = client.post("/api/memories/internal/forget", params={"search_query": "x"}, headers={"X-Service-Key": "any"})
    assert resp.status_code == 503


@pytest.mark.parametrize("method, path, body", READ_ROUTES, ids=[r[0] + " " + r[1] for r in READ_ROUTES])
def test_public_memory_routes_require_auth(client, db, test_user, no_qdrant, monkeypatch, method, path, body):
    """Floor 4; named member GET /api/memories/guest-sessions/active."""
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)
    assert _call(client, method, path, body).status_code == 401
    token = create_access_token({"user_id": test_user.id, "username": test_user.username, "role": test_user.role})
    assert _call(client, method, path, body, {"Authorization": f"Bearer {token}"}).status_code == 200
    assert _call(client, method, path, body, _key_headers()).status_code == 200


def test_read_route_population():
    """The read routes carry require_memory_reader, which authenticates
    through verify_service_or_oidc before its own role check."""
    import inspect
    from app.routes.memories import require_memory_reader

    assert "verify_service_or_oidc(" in inspect.getsource(require_memory_reader)
    gated = set()
    for route in memories_module.router.routes:
        deps = [d.call for d in route.dependant.dependencies]
        if require_memory_reader in deps:
            gated.update((m, route.path) for m in route.methods)
    expected = {(m, p) for m, p, _ in READ_ROUTES}
    assert expected <= gated
    assert ("GET", "/api/memories/guest-sessions/active") in gated


# ---------------------------------------------------------------------------
# Scope rules
# ---------------------------------------------------------------------------

def test_guest_create_without_session_never_owner_scope(client, db, no_qdrant):
    """Named: an anonymous or session-less guest turn never creates an
    owner-scope memory."""
    resp = client.post(
        "/api/memories/internal/create",
        params={"content": "the garage code is 4417", "mode": "guest", "importance": 0.95},
        headers=_key_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["created"] is False
    assert resp.json()["reason"] == "guest_without_session"
    assert db.query(Memory).filter(Memory.scope == "owner").count() == 0


@pytest.mark.parametrize("mode", ["public", "", "OWNER", "Guest"])
def test_unknown_modes_behave_as_guest(client, db, no_qdrant, mode):
    resp = client.post(
        "/api/memories/internal/create",
        params={"content": "x", "mode": mode, "importance": 0.95},
        headers=_key_headers(),
    )
    assert resp.json()["created"] is False
    assert db.query(Memory).count() == 0


def test_owner_create_still_owner_scope(client, db, no_qdrant):
    resp = client.post(
        "/api/memories/internal/create",
        params={"content": "owner likes jazz", "mode": "owner", "importance": 0.95},
        headers=_key_headers(),
    )
    assert resp.json()["created"] is True
    assert db.query(Memory).one().scope == "owner"


def test_guest_with_session_creates_guest_scope(client, db, no_qdrant):
    _session(db, 5)
    resp = client.post(
        "/api/memories/internal/create",
        params={"content": "guest likes tea", "mode": "guest", "guest_session_id": 5, "importance": 0.95},
        headers=_key_headers(),
    )
    assert resp.json()["created"] is True
    memory = db.query(Memory).one()
    assert (memory.scope, memory.guest_session_id) == ("guest", 5)


class _FakeQdrant:
    def __init__(self, hits):
        self.hits = hits
        self.filters = []
        self.deleted = []

    def query_points(self, collection_name=None, query=None, query_filter=None, **kwargs):
        self.filters.append(query_filter)
        return SimpleNamespace(points=[
            SimpleNamespace(id=h.vector_id, score=0.9, payload={"content": h.content, "scope": h.scope})
            for h in self.hits
        ])

    def delete(self, collection_name=None, points_selector=None):
        self.deleted.extend(points_selector.points)


@pytest.fixture
def qdrant_with(monkeypatch):
    def _install(hits):
        fake = _FakeQdrant(hits)
        monkeypatch.setattr(memories_module, "check_qdrant_available", mock.AsyncMock(return_value=True))
        monkeypatch.setattr(memories_module, "get_qdrant", lambda: fake)
        monkeypatch.setattr(memories_module, "embed_text", lambda text: [0.1, 0.2])
        return fake
    return _install


def _forget(client, **params):
    return client.post("/api/memories/internal/forget", params={"search_query": "garage", **params}, headers=_key_headers())


def test_guest_forget_never_deletes_global(client, db, qdrant_with):
    global_mem = _memory(db, "the garage code is 4417", "global", vector_id="v-global")
    qdrant_with([global_mem])
    resp = _forget(client, mode="guest")
    assert resp.json()["deleted"] == 0
    db.refresh(global_mem)
    assert global_mem.is_deleted is False


def test_guest_forget_deletes_only_own_session(client, db, qdrant_with):
    _session(db, 5)
    _session(db, 6)
    own = _memory(db, "garage note mine", "guest", 5, "v-own")
    other = _memory(db, "garage note theirs", "guest", 6, "v-other")
    owner = _memory(db, "garage owner note", "owner", None, "v-owner")
    glob = _memory(db, "garage global note", "global", None, "v-global")
    fake = qdrant_with([own, other, owner, glob])
    resp = _forget(client, mode="guest", guest_session_id=5)
    assert resp.json()["deleted"] == 1
    for m in (own, other, owner, glob):
        db.refresh(m)
    assert [own.is_deleted, other.is_deleted, owner.is_deleted, glob.is_deleted] == [True, False, False, False]
    assert fake.deleted == ["v-own"]


def test_owner_forget_unchanged(client, db, qdrant_with):
    owner = _memory(db, "garage owner note", "owner", None, "v-owner")
    glob = _memory(db, "garage global note", "global", None, "v-global")
    qdrant_with([owner, glob])
    assert _forget(client, mode="owner").json()["deleted"] == 2


def test_guest_keyword_search_without_session_excludes_owner(db):
    import asyncio

    _memory(db, "garage owner secret", "owner")
    _memory(db, "garage global fact", "global")
    results = asyncio.run(memories_module.keyword_search_memories(db, ["garage"], mode="guest", guest_session_id=None))
    assert [r["scope"] for r in results] == ["global"]
    results = asyncio.run(memories_module.keyword_search_memories(db, ["garage"], mode="OWNER", guest_session_id=None))
    assert [r["scope"] for r in results] == ["global"]


def test_semantic_search_filter_excludes_owner_for_guest_without_session(client, db, test_user, qdrant_with):
    fake = qdrant_with([])
    resp = client.post("/api/memories/search", json={"query": "garage", "mode": "guest"}, headers=_key_headers())
    assert resp.status_code == 200
    rendered = repr(fake.filters[-1])
    assert "'owner'" not in rendered and "value='owner'" not in rendered
    assert "global" in rendered


def test_memory_scopes_table():
    from app.routes.memories import _memory_scopes

    owner = _memory_scopes("owner", None)
    assert set(owner.readable) == {"global", "owner"} and owner.create_scope == "owner"
    guest = _memory_scopes("guest", 5)
    assert set(guest.readable) == {"global", "guest"} and set(guest.deletable) == {"guest"}
    assert guest.create_scope == "guest" and guest.guest_session_id == 5
    lone = _memory_scopes("guest", None)
    assert lone.readable == ("global",) and lone.deletable == () and lone.create_scope is None
    for mode in ("public", "", None, "OWNER"):
        assert _memory_scopes(mode, None) == lone


# ---------------------------------------------------------------------------
# Fix round: no owner default (xander L1), the Bearer branch needs 'read'
# and owner/operator (xander L2), guest-to-guest isolation (tessa H4)
# ---------------------------------------------------------------------------

def test_internal_create_without_mode_is_not_owner(client, db, no_qdrant):
    resp = client.post(
        "/api/memories/internal/create",
        params={"content": "the garage code is 4417", "importance": 0.95},
        headers=_key_headers(),
    )
    assert resp.json()["created"] is False
    assert db.query(Memory).count() == 0


def test_internal_forget_without_mode_deletes_nothing(client, db, qdrant_with):
    owner = _memory(db, "garage owner note", "owner", None, "v-owner")
    glob = _memory(db, "garage global note", "global", None, "v-global")
    qdrant_with([owner, glob])
    assert _forget(client).json()["deleted"] == 0
    db.refresh(owner)
    assert owner.is_deleted is False


def test_internal_search_without_mode_reads_global_only(client, db, qdrant_with, monkeypatch):
    fake = qdrant_with([])
    monkeypatch.setattr(memories_module, "is_hybrid_search_enabled", lambda db: False)
    resp = client.get("/api/memories/internal/search", params={"query": "garage"}, headers=_key_headers())
    assert resp.status_code == 200
    rendered = repr(fake.filters[-1])
    assert "value='owner'" not in rendered and "value='global'" in rendered


def test_keyword_search_default_mode_is_not_owner(db):
    import asyncio

    _memory(db, "garage owner secret", "owner")
    _memory(db, "garage global fact", "global")
    results = asyncio.run(memories_module.keyword_search_memories(db, ["garage"]))
    assert [r["scope"] for r in results] == ["global"]


def _bearer(user):
    token = create_access_token({"user_id": user.id, "username": user.username, "role": user.role})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("method, path, body", READ_ROUTES, ids=[r[0] + " " + r[1] for r in READ_ROUTES])
def test_bearer_reader_needs_read_permission(client, db, operator_user, no_qdrant, monkeypatch, method, path, body):
    from app.models import User

    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)
    assert _call(client, method, path, body, _bearer(operator_user)).status_code == 200
    monkeypatch.setitem(User.ROLE_PERMISSIONS, "operator", {"write", "view_audit"})
    assert _call(client, method, path, body, _bearer(operator_user)).status_code == 403


@pytest.mark.parametrize("method, path, body", READ_ROUTES, ids=[r[0] + " " + r[1] for r in READ_ROUTES])
def test_viewer_cannot_read_memories(client, db, viewer_user, no_qdrant, monkeypatch, method, path, body):
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)
    assert _call(client, method, path, body, _bearer(viewer_user)).status_code in (401, 403)


def test_guest_keyword_search_isolated_between_guests(db):
    import asyncio

    _session(db, 5)
    _session(db, 6)
    _memory(db, "garage note from guest five", "guest", 5)
    _memory(db, "garage note from guest six", "guest", 6)
    _memory(db, "garage owner note", "owner")
    _memory(db, "garage global note", "global")
    results = asyncio.run(memories_module.keyword_search_memories(db, ["garage"], mode="guest", guest_session_id=5))
    assert sorted(r["content"] for r in results) == ["garage global note", "garage note from guest five"]


def _conditions(flt):
    """Flatten a rendered Qdrant filter to (key, value) leaves per branch."""
    branches = []
    for cond in flt.should or []:
        if getattr(cond, "must", None):
            branches.append(tuple(sorted((c.key, c.match.value) for c in cond.must)))
        else:
            branches.append(((cond.key, cond.match.value),))
    return sorted(branches)


@pytest.mark.parametrize("path", ["/api/memories/search", "/api/memories/internal/search"])
def test_guest_semantic_search_isolated_between_guests(client, db, qdrant_with, monkeypatch, path):
    fake = qdrant_with([])
    monkeypatch.setattr(memories_module, "is_hybrid_search_enabled", lambda db: False)
    if path.endswith("/internal/search"):
        client.get(path, params={"query": "garage", "mode": "guest", "guest_session_id": 5}, headers=_key_headers())
    else:
        client.post(path, json={"query": "garage", "mode": "guest", "guest_session_id": 5}, headers=_key_headers())
    assert _conditions(fake.filters[-1]) == [
        (("guest_session_id", 5), ("scope", "guest")),
        (("scope", "global"),),
    ]


def test_forget_filter_isolated_between_guests(client, db, qdrant_with):
    fake = qdrant_with([])
    _forget(client, mode="guest", guest_session_id=5)
    assert _conditions(fake.filters[-1]) == [(("guest_session_id", 5), ("scope", "guest"))]

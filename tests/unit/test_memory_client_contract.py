"""The orchestrator's memory client against admin-backend's memory routes (V3.3).

Waiver (as planned): driving the admin app over ASGI from the orchestrator
test environment collides on the `main`/`app` package names on sys.path, so
the admin side is read by introspection (AST) of its route declarations and
auth dependency. The client side is exercised for real over an
httpx.MockTransport.

Waiver limit: this proves the client sends only parameter names the admin
routes declare, with the right header name. It does not prove the admin
side honours them (value types, scope behaviour); that is
admin/backend/tests/test_memory_internal_scope.py's job, over the real app.
"""
from __future__ import annotations

import ast
import asyncio
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from . import _public_audience_harness as h
import orchestrator.memory_manager as memory_manager_module

ADMIN_MEMORIES = h.REPO_ROOT / "admin" / "backend" / "app" / "routes" / "memories.py"
ADMIN_INTERNAL = h.REPO_ROOT / "admin" / "backend" / "app" / "routes" / "internal.py"
SERVICE_KEY = h.shared_config.get_config().service_api_key


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


class _Recorder:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/internal/search"):
            return httpx.Response(200, json={"qdrant_available": True, "results": []})
        if path.endswith("/internal/create"):
            return httpx.Response(200, json={"created": True, "memory_id": 1})
        if path.endswith("/internal/forget"):
            return httpx.Response(200, json={"deleted": 0})
        if path.endswith("/guest-sessions/active"):
            return httpx.Response(200, json={"id": 5, "guest_name": "G"})
        return httpx.Response(404)


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    real = httpx.AsyncClient

    class _Client(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(rec.handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return rec


def _exercise_client():
    async def _run():
        manager = memory_manager_module.MemoryManager()
        await manager.initialize()
        await manager.get_relevant_memories("garage", mode="guest", guest_session_id=5)
        await manager.create_memory("likes tea", mode="guest", guest_session_id=5, importance=0.9)
        await manager.delete_memory_by_content("tea", mode="guest", guest_session_id=5)
        await manager.get_active_guest_session()
        await manager.close()
    asyncio.run(_run())


def test_memory_client_sends_service_key(recorder):
    """Every admin memory call carries X-Service-Key. Floor 3 internal
    calls; named member /internal/forget."""
    _exercise_client()
    paths = [r.url.path for r in recorder.requests]
    internal = [p for p in paths if "/internal/" in p]
    assert len(internal) >= 3
    assert "/api/memories/internal/forget" in paths
    assert "/api/memories/guest-sessions/active" in paths
    for request in recorder.requests:
        assert request.headers.get("X-Service-Key") == SERVICE_KEY, request.url.path


@pytest.mark.parametrize("route", ["/internal/search", "/internal/create", "/internal/forget"])
def test_guest_session_id_and_mode_forwarded(recorder, route):
    """Search, create and forget all forward the guest's session and an
    explicit mode (admin-backend defaults a missing mode to guest)."""
    _exercise_client()
    request = next(r for r in recorder.requests if r.url.path.endswith(route))
    query = parse_qs(urlparse(str(request.url)).query)
    assert query["guest_session_id"] == ["5"]
    assert query["mode"] == ["guest"]


def _route_params(tree, method, path):
    for fn in tree.body:
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for dec in fn.decorator_list:
            if (
                isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == method and dec.args and ast.literal_eval(dec.args[0]) == path
            ):
                deps = [ast.unparse(k.value) for k in dec.keywords if k.arg == "dependencies"]
                return {a.arg for a in fn.args.args}, " ".join(deps)
    raise AssertionError(f"{method.upper()} {path} not declared")


def test_client_params_are_declared_by_admin_routes(recorder):
    """Every query param the client sends is a declared parameter of the
    matching admin route, and each route carries the auth the client
    satisfies."""
    _exercise_client()
    tree = ast.parse(ADMIN_MEMORIES.read_text(encoding="utf-8"))
    for request in recorder.requests:
        route_path = request.url.path[len("/api/memories"):]
        params, deps = _route_params(tree, request.method.lower(), route_path)
        sent = set(parse_qs(urlparse(str(request.url)).query))
        assert sent <= params, (route_path, sent - params)
        expected_dep = "require_service_key_401" if "/internal/" in route_path else "require_memory_reader"
        assert expected_dep in deps, route_path


def test_header_name_matches_admin_dependency():
    tree = ast.parse(ADMIN_INTERNAL.read_text(encoding="utf-8"))
    dep = next(fn for fn in tree.body if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "require_service_key_401")
    aliases = [k.value.value for n in ast.walk(dep) if isinstance(n, ast.Call) for k in n.keywords if k.arg == "alias"]
    assert aliases == ["X-Service-Key"]


def test_client_without_key_sends_no_header(recorder, monkeypatch):
    monkeypatch.setattr(memory_manager_module, "get_config", lambda: mock.MagicMock(service_api_key=""))
    _exercise_client()
    assert all("X-Service-Key" not in r.headers for r in recorder.requests)


# ---------------------------------------------------------------------------
# /query: the public audience never touches memories; guests forget within
# their own session
# ---------------------------------------------------------------------------

class _Graph:
    async def ainvoke(self, state):
        return {"intent": h.IntentCategory.WEATHER, "answer": "It is sunny and I will remember that.",
                "confidence": 1.0, "citations": [], "request_id": "r", "node_timings": {}}


@pytest.fixture
def client(monkeypatch):
    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "orchestrator_graph", _Graph())
    return TestClient(h.main.app)


def test_public_audience_skips_memory_calls(client, monkeypatch):
    h.install_mode_client(server_mode="owner")
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: None)
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    get_manager = mock.AsyncMock(side_effect=AssertionError("no memory manager for the public audience"))
    monkeypatch.setattr(h.main, "get_memory_manager", get_manager)
    resp = client.post(
        "/query",
        json={"query": "forget that I like jazz, and remember my name is Pat", "caller_trust": "web_public"},
        headers=h.service_headers(),
    )
    assert resp.status_code == 200
    get_manager.assert_not_awaited()


def test_guest_forget_passes_session(client, monkeypatch):
    h.install_mode_client(server_mode="owner")
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    monkeypatch.setattr(
        h.main, "get_admin_client", lambda: h.fake_admin_client(guest_info={"guest_id": 7, "guest_name": "G"})
    )
    manager = mock.MagicMock()
    manager.should_forget_memory.return_value = True
    manager.extract_forget_content.return_value = "jazz"
    manager.get_active_guest_session = mock.AsyncMock(return_value={"id": 5})
    manager.delete_memory_by_content = mock.AsyncMock(return_value={"deleted": 0})
    monkeypatch.setattr(h.main, "get_memory_manager", mock.AsyncMock(return_value=manager))
    resp = client.post(
        "/query",
        json={"query": "forget that I like jazz", "device_id": "dev7", "caller_trust": "household"},
        headers=h.service_headers(),
    )
    assert resp.status_code == 200
    manager.delete_memory_by_content.assert_awaited_once()
    assert manager.delete_memory_by_content.await_args.kwargs.get("guest_session_id") == 5


def test_keyword_fallback_results_are_kept(monkeypatch):
    """Semantic search down, keyword fallback up: the admin reports results
    usable (qdrant_available true) and the manager keeps them."""
    kw = {"content": "the garage code is 4417", "scope": "owner", "score": 1.0}

    def handler(request):
        return httpx.Response(200, json={
            "results": [kw], "qdrant_available": True, "semantic_available": False,
            "search_type": "keyword_fallback",
        })

    real = httpx.AsyncClient

    class _Client(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def _run():
        manager = memory_manager_module.MemoryManager()
        await manager.initialize()
        try:
            return await manager.get_relevant_memories("garage code", mode="owner")
        finally:
            await manager.close()

    assert asyncio.run(_run()) == [kw]

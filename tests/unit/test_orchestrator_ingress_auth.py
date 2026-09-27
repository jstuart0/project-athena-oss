"""ATHENA-89 Phase 3b — orchestrator ingress authentication (D10).

Covers plan/contract AU1-AU6. Same orchestrator-import harness as
tests/unit/test_openai_session_key.py. `require_service_caller` (the real
dependency, not mocked) is wired onto the 8 gated routes; every other
dependency the route bodies need (SessionManager, cache client,
process_query) is stubbed so a "not 401" assertion doesn't require a full
working orchestrator stack.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, "src")

for _mod in ("langgraph", "langgraph.graph", "prometheus_client"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()
os.environ.setdefault("SERVICE_API_KEY", "test-key-ingress-auth")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

from shared.config import get_config as _shared_get_config, _clear_cache_for_tests  # noqa: E402
import shared.config as _shared_config  # noqa: E402

_config_loader_mock = mock.MagicMock()
_config_loader_mock.get_config = _shared_get_config
_config_loader_mock.ADMIN_API_URL = os.environ["ADMIN_API_URL"]
_config_loader_mock.get_feature_flag = mock.AsyncMock(return_value=False)
_config_loader_mock.get_feature_flags = mock.AsyncMock(return_value={})
_config_loader_mock.clear_cache = mock.AsyncMock()
sys.modules.setdefault("orchestrator.config_loader", _config_loader_mock)

import orchestrator.nodes  # noqa: E402,F401
import orchestrator.main as _main_module  # noqa: E402
from orchestrator.ingress_auth import require_service_caller  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.session_manager import SessionManager  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REAL_SERVICE_KEY = "test-key-ingress-auth"


class _FakeSessionCacheClient:
    def __init__(self):
        self.client = SimpleNamespace(
            delete=mock.AsyncMock(),
            get=mock.AsyncMock(return_value=None),
            setex=mock.AsyncMock(),
        )


@pytest.fixture(autouse=True)
def _reset_config_cache():
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


@pytest.fixture
def client():
    sm = SessionManager()
    sm.redis_client = None
    _runtime.set_session_manager(sm)
    _runtime.set_cache_client(_FakeSessionCacheClient())

    async def _fake_process_query(query_request):
        return SimpleNamespace(
            request_id="req-fake", answer="ok", intent="general_info",
            confidence=1.0, citations=[], session_id="sess-fake",
        )

    original = _main_module.process_query
    _main_module.process_query = _fake_process_query
    try:
        yield TestClient(_main_module.app)
    finally:
        _main_module.process_query = original


def _query_body():
    return {"query": "hi", "mode": "owner", "room": "kitchen"}


def _chat_body():
    return {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": False}


# Each: (method, path, body-or-None). Population is 8 (AU1).
_GATED_ROUTES = [
    ("POST", "/query", _query_body()),
    ("POST", "/query/stream", _query_body()),
    ("POST", "/query/stream/v2", _query_body()),
    ("POST", "/v1/chat/completions", _chat_body()),
    ("GET", "/sessions", None),
    ("GET", "/sessions/nonexistent-session-id", None),
    ("DELETE", "/sessions/nonexistent-session-id", None),
    ("GET", "/sessions/nonexistent-session-id/export", None),
]


def _hit(client, method, path, body):
    if method == "GET":
        return client.get(path)
    if method == "DELETE":
        return client.delete(path)
    return client.post(path, json=body)


def test_AU1_population_is_8():
    assert len(_GATED_ROUTES) == 8


def test_AU1_named_member_delete_sessions_present():
    assert ("DELETE", "/sessions/nonexistent-session-id", None) in _GATED_ROUTES


@pytest.mark.parametrize("method,path,body", _GATED_ROUTES)
def test_AU1_enforce_no_header_401(monkeypatch, client, method, path, body):
    monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", "enforce")
    monkeypatch.setenv("SERVICE_API_KEY", REAL_SERVICE_KEY)
    monkeypatch.delenv("DEV_MODE", raising=False)
    _clear_cache_for_tests()

    resp = _hit(client, method, path, body)
    assert resp.status_code == 401


@pytest.mark.parametrize("method,path,body", _GATED_ROUTES)
def test_AU1_enforce_correct_header_not_401(monkeypatch, client, method, path, body):
    monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", "enforce")
    monkeypatch.setenv("SERVICE_API_KEY", REAL_SERVICE_KEY)
    monkeypatch.delenv("DEV_MODE", raising=False)
    _clear_cache_for_tests()

    # Send get_config().service_api_key at call time, not a hardcoded
    # literal (r3 test-harness hygiene note) -- proves the route's real
    # dependency reads the same config this test just set.
    resp = client.request(
        method, path, json=body if method not in ("GET", "DELETE") else None,
        headers={"X-Service-Key": _shared_get_config().service_api_key},
    )
    assert resp.status_code != 401


@pytest.mark.parametrize("method,path,body", _GATED_ROUTES)
def test_AU1_enforce_wrong_header_401(monkeypatch, client, method, path, body):
    monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", "enforce")
    monkeypatch.setenv("SERVICE_API_KEY", REAL_SERVICE_KEY)
    monkeypatch.delenv("DEV_MODE", raising=False)
    _clear_cache_for_tests()

    resp = client.request(
        method, path, json=body if method not in ("GET", "DELETE") else None,
        headers={"X-Service-Key": "definitely-the-wrong-key"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# AU2: warn mode allows through, one WARNING with path/client_host/user_agent
# ---------------------------------------------------------------------------


def test_AU2_warn_mode_allows_through_and_logs(monkeypatch, client):
    monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", "warn")
    monkeypatch.setenv("SERVICE_API_KEY", REAL_SERVICE_KEY)
    monkeypatch.delenv("DEV_MODE", raising=False)
    _clear_cache_for_tests()

    import orchestrator.ingress_auth as ingress_auth_module
    calls = []
    monkeypatch.setattr(
        ingress_auth_module.logger, "warning",
        lambda event, **kw: calls.append({"event": event, **kw}),
    )

    resp = client.get("/sessions", headers={"User-Agent": "my-test-agent/1.0"})
    assert resp.status_code != 401

    warn_events = [c for c in calls if c["event"] == "orchestrator_unauthenticated_request"]
    assert len(warn_events) == 1
    assert warn_events[0]["path"] == "/sessions"
    assert "client_host" in warn_events[0]
    assert warn_events[0]["user_agent"] == "my-test-agent/1.0"


# ---------------------------------------------------------------------------
# AU3: DEV_MODE bypass (no log); invalid mode behaves as enforce (+ ERROR log)
# ---------------------------------------------------------------------------


def test_AU3_dev_mode_no_header_allowed_no_log(monkeypatch, client):
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("SERVICE_API_KEY", REAL_SERVICE_KEY)
    monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", "enforce")
    _clear_cache_for_tests()

    import orchestrator.ingress_auth as ingress_auth_module
    calls = []
    monkeypatch.setattr(
        ingress_auth_module.logger, "warning",
        lambda event, **kw: calls.append({"event": event, **kw}),
    )

    resp = client.get("/sessions")
    assert resp.status_code != 401
    assert not calls


def test_AU3_invalid_mode_behaves_as_enforce_logs_error(monkeypatch, client):
    monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", "not-a-real-mode")
    monkeypatch.setenv("SERVICE_API_KEY", REAL_SERVICE_KEY)
    monkeypatch.delenv("DEV_MODE", raising=False)
    _clear_cache_for_tests()

    import orchestrator.ingress_auth as ingress_auth_module
    errors = []
    monkeypatch.setattr(
        ingress_auth_module.logger, "error",
        lambda event, **kw: errors.append({"event": event, **kw}),
    )

    resp = client.get("/sessions")
    assert resp.status_code == 401
    assert any(e["event"] == "orchestrator_ingress_auth_invalid_mode" for e in errors)


# ---------------------------------------------------------------------------
# AU3b: a WRONG key is 401 in every mode, including DEV_MODE and warn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode,dev_mode", [("enforce", "false"), ("warn", "false"), ("enforce", "true"), ("warn", "true")])
def test_AU3b_wrong_key_401_in_every_mode(monkeypatch, client, mode, dev_mode):
    monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", mode)
    monkeypatch.setenv("SERVICE_API_KEY", REAL_SERVICE_KEY)
    monkeypatch.setenv("DEV_MODE", dev_mode)
    _clear_cache_for_tests()

    resp = client.get("/sessions", headers={"X-Service-Key": "wrong-key-value"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# AU5: empty configured key -> 401 outside DEV_MODE; the DEV_MODE+header edge case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_AU5_empty_configured_key_401_outside_dev_mode(monkeypatch, client, mode):
    monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", mode)
    monkeypatch.setenv("SERVICE_API_KEY", "")
    monkeypatch.delenv("DEV_MODE", raising=False)
    _clear_cache_for_tests()

    resp = client.get("/sessions")
    assert resp.status_code == 401


def test_AU5_dev_mode_empty_key_header_present_still_allowed(monkeypatch, client):
    """Edge case: DEV_MODE=true AND empty configured key AND a header
    present -> step 1 (wrong-key check) is skipped because there's no
    configured key to compare against, falls through to step 3 (DEV_MODE),
    and is allowed."""
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("SERVICE_API_KEY", "")
    monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", "enforce")
    _clear_cache_for_tests()

    resp = client.get("/sessions", headers={"X-Service-Key": "anything-at-all"})
    assert resp.status_code != 401


# ---------------------------------------------------------------------------
# AU6: config read fresh on every call, not captured once
# ---------------------------------------------------------------------------


def test_AU6_config_read_fresh_per_call(monkeypatch, client):
    monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", "enforce")
    monkeypatch.delenv("DEV_MODE", raising=False)

    monkeypatch.setenv("SERVICE_API_KEY", "first-key-value")
    _clear_cache_for_tests()
    resp1 = client.get("/sessions", headers={"X-Service-Key": "first-key-value"})
    assert resp1.status_code != 401

    # Rotate the key mid-test-process; the SAME dependency instance must
    # track it without a restart.
    monkeypatch.setenv("SERVICE_API_KEY", "second-key-value")
    _clear_cache_for_tests()
    resp2 = client.get("/sessions", headers={"X-Service-Key": "first-key-value"})
    assert resp2.status_code == 401  # the old key is now wrong

    resp3 = client.get("/sessions", headers={"X-Service-Key": "second-key-value"})
    assert resp3.status_code != 401

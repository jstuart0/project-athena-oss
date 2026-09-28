"""ATHENA-69 Pass C — server-derived mode/permissions and the owner-PIN
trust gate, driven end-to-end through the real FastAPI routes (D6/D7/D16/
D24, bob M8).

Same orchestrator-import harness as tests/unit/test_openai_session_key.py.

 - test_query_owner_claim_while_server_guest_is_guest (process_query)
 - test_stream_v2_owner_claim_while_server_guest_is_guest
 - test_device_fingerprinted_guest_gets_guest_permissions_while_house_owner
 - test_stream_v2_device_fingerprint_lookup
 - test_post_graph_check_skipped_when_node_refused
 - test_query_pin_branch_uses_trust_helper
 - test_source_field_never_grants_pin_path
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
os.environ.setdefault("SERVICE_API_KEY", "test-key-mode-escalation")
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
import orchestrator.session_manager as _session_manager_module  # noqa: E402
from orchestrator import mode_permission  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.session_manager import SessionManager  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REAL_SERVICE_KEY = "test-key-mode-escalation"


def _service_headers() -> dict:
    return {"X-Service-Key": _shared_config.get_config().service_api_key}


def _make_response(status_code: int, json_data: dict) -> mock.MagicMock:
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


class _FakeGraph:
    """Records the initial_state each call was invoked with and returns a
    minimal, valid final_state dict (LangGraph's ainvoke returns a dict-
    shaped result, accessed via .get(...) in main.py)."""

    _DEFAULTS = {
        "intent": None, "node_timings": {}, "error": None,
        "confidence": 1.0, "citations": [], "request_id": "fake-req-id",
        "answer": "ok", "retrieved_data": {},
    }

    def __init__(self, final_state: dict | None = None):
        self.calls = []
        self._final_state = {**self._DEFAULTS, **(final_state or {})}
        self.ainvoke = mock.AsyncMock(side_effect=self._invoke)

    async def _invoke(self, initial_state):
        self.calls.append(initial_state)
        return self._final_state


@pytest.fixture(autouse=True)
def _reset_state():
    _clear_cache_for_tests()
    _runtime.reset_for_test()
    mode_permission._reset_pin_authority_cache_for_tests()
    mode_permission._reset_owner_override_throttle_for_tests()
    sm = SessionManager()
    sm.redis_client = None
    _runtime.set_session_manager(sm)
    yield
    _clear_cache_for_tests()
    _runtime.reset_for_test()
    mode_permission._reset_pin_authority_cache_for_tests()
    mode_permission._reset_owner_override_throttle_for_tests()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(_main_module, "_direct_general_info_response", lambda q: True)
    fake_conv_config = SimpleNamespace(
        get_conversation_settings=mock.AsyncMock(return_value={"enabled": False})
    )
    monkeypatch.setattr(_main_module, "get_config", mock.AsyncMock(return_value=fake_conv_config))

    async def _fake_sm_get_config():
        return SimpleNamespace(
            get_conversation_settings=mock.AsyncMock(return_value={"session_ttl_seconds": 3600})
        )
    monkeypatch.setattr(_session_manager_module, "get_config", _fake_sm_get_config)

    return TestClient(_main_module.app)


def _install_mode_client(*, server_mode: str, guest_lookup_response=None):
    """Install a fake mode_client covering get_current_mode's two GETs
    (/mode then /mode/permissions), plus /mode/permissions?mode=guest for
    get_guest_permissions when the caller needs it."""
    perms = {"mode": server_mode}
    if server_mode == "guest":
        perms.update({"allowed_intents": ["weather"], "restricted_entities": [], "allowed_domains": []})

    async def _get(url, params=None, **kwargs):
        if url == "/mode":
            return _make_response(200, {"mode": server_mode, "override_active": False, "reason": "ok"})
        if url == "/mode/permissions" and params and params.get("mode") == "guest":
            if guest_lookup_response is not None:
                return guest_lookup_response
            return _make_response(200, {
                "mode": "guest", "allowed_intents": ["fetched_weather"],
                "restricted_entities": [], "allowed_domains": [],
            })
        if url == "/mode/permissions":
            return _make_response(200, perms)
        if url == "/health":
            return _make_response(200, {"pin_authority": "admin"})
        raise AssertionError(f"unexpected GET {url}")

    client = mock.AsyncMock()
    client.get = mock.AsyncMock(side_effect=_get)
    client.post = mock.AsyncMock(return_value=_make_response(200, {"message": "Owner mode active.", "expires_at": "later"}))
    _runtime.set_mode_client(client)
    return client


class TestQueryOwnerClaimWhileServerGuestIsGuest:
    def test_query_owner_claim_while_server_guest_is_guest(self, client, monkeypatch):
        _install_mode_client(server_mode="guest")
        graph = _FakeGraph()
        monkeypatch.setattr(_main_module, "orchestrator_graph", graph)

        resp = client.post(
            "/query",
            json={"query": "what's the weather", "mode": "owner", "room": "kitchen"},
            headers=_service_headers(),
        )
        assert resp.status_code == 200
        assert len(graph.calls) == 1
        initial_state = graph.calls[0]
        assert initial_state.mode == "guest"
        assert initial_state.permissions["mode"] == "guest"


class TestStreamV2OwnerClaimWhileServerGuestIsGuest:
    def test_stream_v2_owner_claim_while_server_guest_is_guest(self, client, monkeypatch):
        _install_mode_client(server_mode="guest")
        graph = _FakeGraph({"intent": SimpleNamespace(value="weather")})
        monkeypatch.setattr(_main_module, "orchestrator_graph", graph)

        with client.stream(
            "POST", "/query/stream/v2",
            json={"query": "what's the weather", "mode": "owner", "room": "kitchen"},
            headers=_service_headers(),
        ) as resp:
            assert resp.status_code == 200
            for _ in resp.iter_lines():
                pass

        assert len(graph.calls) == 1
        initial_state = graph.calls[0]
        assert initial_state.mode == "guest"
        assert initial_state.permissions["mode"] == "guest"


class TestDeviceFingerprintedGuestGetsGuestPermissionsWhileHouseOwner:
    def test_device_fingerprinted_guest_gets_guest_permissions_while_house_owner(self, client, monkeypatch):
        _install_mode_client(server_mode="owner")
        fake_admin = mock.MagicMock()
        fake_admin.get_user_session_by_device = mock.AsyncMock(return_value={
            "guest_id": "g1", "guest_name": "Alice",
        })
        monkeypatch.setattr(_main_module, "get_admin_client", lambda: fake_admin)
        graph = _FakeGraph()
        monkeypatch.setattr(_main_module, "orchestrator_graph", graph)

        resp = client.post(
            "/query",
            json={"query": "what's the weather", "mode": "owner", "room": "kitchen", "device_id": "fp-123"},
            headers=_service_headers(),
        )
        assert resp.status_code == 200
        fake_admin.get_user_session_by_device.assert_awaited_once_with("fp-123")
        initial_state = graph.calls[0]
        assert initial_state.mode == "guest"
        assert initial_state.permissions["mode"] == "guest"
        # server is owner, not degraded -- get_guest_permissions was fetched
        assert initial_state.permissions["allowed_intents"] == ["fetched_weather"]


class TestStreamV2DeviceFingerprintLookup:
    def test_stream_v2_device_fingerprint_lookup(self, client, monkeypatch):
        """The device-fingerprint lookup added to /query/stream/v2 (it had
        none before ATHENA-69) actually runs and its result narrows mode."""
        _install_mode_client(server_mode="owner")
        fake_admin = mock.MagicMock()
        fake_admin.get_user_session_by_device = mock.AsyncMock(return_value={
            "guest_id": "g2", "guest_name": "Bob",
        })
        monkeypatch.setattr(_main_module, "get_admin_client", lambda: fake_admin)
        graph = _FakeGraph({"intent": SimpleNamespace(value="weather")})
        monkeypatch.setattr(_main_module, "orchestrator_graph", graph)

        with client.stream(
            "POST", "/query/stream/v2",
            json={"query": "what's the weather", "mode": "owner", "room": "kitchen", "device_id": "fp-456"},
            headers=_service_headers(),
        ) as resp:
            assert resp.status_code == 200
            for _ in resp.iter_lines():
                pass

        fake_admin.get_user_session_by_device.assert_awaited_once_with("fp-456")
        initial_state = graph.calls[0]
        assert initial_state.mode == "guest"


class TestPostGraphCheckSkippedWhenNodeRefused:
    def test_post_graph_check_skipped_when_node_refused(self, client, monkeypatch):
        """bob M8: when a node (route_control_node et al) already refused
        (final_state.error == "permission_denied"), the post-graph
        check_intent_permission must not run at all -- no double refusal,
        no override of the node's own specific answer."""
        _install_mode_client(server_mode="guest")
        node_refusal_text = "Sorry, I can't control the locks in guest mode."
        graph = _FakeGraph({
            "intent": SimpleNamespace(value="control"),
            "error": "permission_denied",
            "node_timings": {},
        })
        monkeypatch.setattr(_main_module, "orchestrator_graph", graph)

        check_spy = mock.MagicMock(wraps=mode_permission.check_intent_permission)
        monkeypatch.setattr(_main_module, "check_intent_permission", check_spy)

        resp = client.post(
            "/query",
            json={"query": "unlock the front door", "mode": "guest", "room": "kitchen"},
            headers=_service_headers(),
        )
        assert resp.status_code == 200
        check_spy.assert_not_called()
        # The response must NOT be the generic post-graph refusal text --
        # process_query falls through to normal answer synthesis in the
        # test double (answer defaults to None/empty via the fake state,
        # but the important assertion is that the guest-mode generic
        # message was never substituted in).
        assert resp.json()["answer"] != "I'm sorry, that feature is not available in guest mode."

    def test_early_return_response_validates_when_intent_blocked_without_node_refusal(self, client, monkeypatch):
        """tessa's Pass C mutation review, item 3: the OTHER (non-M8) trigger
        for the post-graph early-return -- an intent no node gates (TESLA is
        documented as "owner mode only - blocked for guests") reaching
        classification with error=None -- must still construct a valid
        QueryResponse. Before this fix the branch passed model_used/
        reasoning_path/node_timings/total_time (none are QueryResponse
        fields) and omitted the required request_id/processing_time,
        raising a pydantic ValidationError (surfaced by TestClient as a
        raised exception, i.e. a 500) on every guest query hitting this
        path -- not just under an M8-guard mutation."""
        _install_mode_client(server_mode="guest")
        graph = _FakeGraph({
            "intent": SimpleNamespace(value="tesla"),
            "error": None,
            "node_timings": {"classify": 0.01},
            "request_id": "req-tesla-1",
        })
        monkeypatch.setattr(_main_module, "orchestrator_graph", graph)

        check_spy = mock.MagicMock(wraps=mode_permission.check_intent_permission)
        monkeypatch.setattr(_main_module, "check_intent_permission", check_spy)

        resp = client.post(
            "/query",
            json={"query": "is my tesla charged", "mode": "guest", "room": "kitchen"},
            headers=_service_headers(),
        )

        assert resp.status_code == 200
        check_spy.assert_called_once()
        body = resp.json()
        assert body["answer"] == "I'm sorry, that feature is not available in guest mode."
        assert body["request_id"] == "req-tesla-1"
        assert body["session_id"]
        assert isinstance(body["processing_time"], float)


class TestQueryPinBranchUsesTrustHelper:
    def test_query_pin_branch_uses_trust_helper(self, client, monkeypatch):
        server = _install_mode_client(server_mode="owner")
        graph = _FakeGraph()
        monkeypatch.setattr(_main_module, "orchestrator_graph", graph)

        resp = client.post(
            "/query",
            json={
                "query": "switch to owner mode pin 123456",
                "mode": "owner",
                "room": "kitchen",
                "caller_trust": "web_public",
            },
            headers=_service_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["intent"] == "mode_override"
        assert body["metadata"]["refused_reason"] == "untrusted_surface"
        # 0 override calls -- /health/override never reached (refused before
        # any mode-service call).
        server.post.assert_not_awaited()
        assert len(graph.calls) == 0


class TestSourceFieldNeverGrantsPinPath:
    def test_source_field_never_grants_pin_path(self, client, monkeypatch):
        server = _install_mode_client(server_mode="owner")
        graph = _FakeGraph()
        monkeypatch.setattr(_main_module, "orchestrator_graph", graph)

        resp = client.post(
            "/query",
            json={
                "query": "switch to owner mode pin 123456",
                "mode": "owner",
                "room": "kitchen",
                "source": "voice",
            },
            headers=_service_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["intent"] == "mode_override"
        assert body["metadata"]["refused_reason"] == "untrusted_surface"
        server.post.assert_not_awaited()

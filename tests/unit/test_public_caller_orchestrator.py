"""The public audience in the orchestrator (V1.1).

A `caller_trust="web_public"` request (an embedded website chatbot relayed by
jarvis-web) gets a hard-coded narrow allowlist, whatever the mode service's
guest profile says, and never receives guest identity. New symbols are
imported inside each test so every member fails on its own at base.
"""
from __future__ import annotations

import ast
import asyncio

import pytest
from fastapi.testclient import TestClient

from . import _public_audience_harness as h


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


@pytest.fixture
def client(monkeypatch):
    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    return TestClient(h.main.app)


def _authorize(**kwargs):
    return asyncio.run(h.mode_permission.resolve_request_authorization(**kwargs))


def test_public_ignores_guest_profile_with_control_allowed():
    from orchestrator.mode_permission import authorize_ha_write, check_intent_permission

    h.install_mode_client(server_mode="guest")  # profile allows control + switch
    authz = _authorize(request_mode="guest", guest_info=None, caller_trust="web_public")
    assert authz.mode == "guest"
    assert check_intent_permission(h.IntentCategory.CONTROL, authz.permissions) is False
    decision = authorize_ha_write("switch", "turn_on", {"entity_id": "switch.garage_door_relay"}, authz.permissions)
    assert decision.allowed is False
    assert "switch.garage_door_relay" in decision.denied_targets


def test_public_ignores_owner_house_and_owner_claim():
    h.install_mode_client(server_mode="owner")
    authz = _authorize(request_mode="owner", guest_info=None, caller_trust="web_public")
    assert authz.mode == "guest"
    assert authz.permissions["mode"] == "guest"
    assert authz.escalation_ignored is True


def test_public_ignores_degraded():
    from orchestrator.mode_permission import PUBLIC_ALLOWED_INTENTS, is_public_audience

    h.install_mode_client(degraded=True)
    authz = _authorize(request_mode=None, guest_info=None, caller_trust="web_public")
    assert authz.degraded is True
    assert is_public_audience(authz.permissions)
    assert set(authz.permissions["allowed_intents"]) == set(PUBLIC_ALLOWED_INTENTS)


def test_public_ignores_device_guest_info():
    from orchestrator.mode_permission import is_public_audience

    h.install_mode_client(server_mode="owner")
    authz = _authorize(request_mode=None, guest_info={"guest_id": 7}, caller_trust="web_public")
    assert is_public_audience(authz.permissions)


@pytest.mark.parametrize("intent", ["weather", "general_info", "news", "recipes", "streaming"])
def test_public_allowed_intents_exact(intent):
    from orchestrator.mode_permission import PUBLIC_ALLOWED_INTENTS, public_permissions

    assert set(PUBLIC_ALLOWED_INTENTS) == {"weather", "general_info", "news", "recipes", "streaming"}
    assert intent in public_permissions()["allowed_intents"]
    # every member is a real intent value
    assert intent in {i.value for i in h.IntentCategory}


def test_public_permissions_survive_normalization():
    from orchestrator.mode_permission import normalize_permissions, public_permissions

    perms = normalize_permissions(public_permissions())
    assert perms["audience"] == "public"
    assert ".*" in perms["restricted_entities"]
    assert perms["allowed_domains"] == ["__none__"]
    assert public_permissions() is not public_permissions()


def test_public_denies_every_ha_domain():
    from orchestrator.mode_permission import authorize_ha_write, normalize_permissions, public_permissions

    perms = normalize_permissions(public_permissions())
    for domain, entity in [("light", "light.porch"), ("media_player", "media_player.tv"), ("climate", "climate.home")]:
        assert authorize_ha_write(domain, "turn_on", {"entity_id": entity}, perms).allowed is False


def test_non_public_resolution_unchanged():
    from orchestrator.mode_permission import is_public_audience

    h.install_mode_client(server_mode="owner")
    for trust in (None, "household", "web_local", "web_authenticated", "sms"):
        authz = _authorize(request_mode=None, guest_info=None, caller_trust=trust)
        assert authz.mode == "owner"
        assert not is_public_audience(authz.permissions)


@pytest.mark.parametrize("trust", ["web_local", "web_guest_net"])
def test_browser_trust_accepted_by_query_model(trust):
    request = h.main.QueryRequest(query="hi", caller_trust=trust)
    assert request.caller_trust == trust


@pytest.mark.parametrize("trust", ["web_local", "web_guest_net"])
def test_browser_trust_is_not_pin_trusted(trust):
    assert trust not in h.mode_permission.PIN_TRUSTED_TIERS


def test_build_query_context_state_level_scrub():
    """V1.5: the public context keeps only location_override; guest_info
    is never merged."""
    from orchestrator.helpers import build_query_context

    request = h.main.QueryRequest(
        query="hi",
        caller_trust="web_public",
        context={"guest_id": 7, "guest_name": h.GUEST_NAME, "location_override": h.LOCATION_OVERRIDE, "phone_number": "+1555"},
    )
    context = build_query_context(request, {"guest_id": 9, "guest_name": "Bob"}, server_mode="guest", degraded=False)
    assert context == {"location_override": h.LOCATION_OVERRIDE}

    # The device leg names nobody (a device-fingerprinted guest keeps its
    # guest-scoped session and permissions from guest_info, not context).
    household = h.main.QueryRequest(query="hi", caller_trust="household", context={"phone_number": "+1555"})
    context = build_query_context(
        household, {"guest_id": 9, "guest_name": "Bob", "preferences": {"a": 1}}, server_mode="guest", degraded=False,
    )
    assert context == {"phone_number": "+1555", "device_type": "web", "guest_preferences": {"a": 1}}


@pytest.mark.parametrize("path", ["/query", "/query/stream", "/query/stream/v2"])
def test_query_web_public_context_has_no_guest_identity(client, monkeypatch, path):
    """Black-box per entry point: the state handed to the pipeline carries
    no guest identity, and the device lookup is never made."""
    h.install_mode_client(server_mode="owner")
    admin = h.fake_admin_client(guest_info={"guest_id": 7, "guest_name": h.GUEST_NAME})
    monkeypatch.setattr(h.main, "get_admin_client", lambda: admin)
    captured = []

    class _Graph:
        async def ainvoke(self, state):
            captured.append(state)
            return {"intent": h.IntentCategory.WEATHER, "answer": "ok", "confidence": 1.0,
                    "citations": [], "request_id": "r", "node_timings": {}}

    async def _stream_run(state):
        captured.append(state)
        state.answer = "ok"
        return state

    monkeypatch.setattr(h.main, "orchestrator_graph", _Graph())
    monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", _stream_run)

    body = {
        "query": "what's the weather",
        "caller_trust": "web_public",
        "device_id": "dev1",
        "context": {"guest_id": 7, "guest_name": h.GUEST_NAME, "location_override": h.LOCATION_OVERRIDE},
    }
    with client.stream("POST", path, json=body, headers=h.service_headers()) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    assert h.GUEST_NAME not in text
    assert len(captured) == 1
    state = captured[0]
    assert state.context == {"location_override": h.LOCATION_OVERRIDE}
    assert state.permissions.get("audience") == "public"
    admin.get_user_session_by_device.assert_not_awaited()


def test_web_public_literal_single_site():
    """PP14: the exact "web_public" literal appears in src/orchestrator only
    as mode_permission's PUBLIC_CALLER_TRUST and in the QueryRequest wire
    contract (the caller_trust Literal). Everything else asks
    is_public_caller / is_public_audience."""
    sites = []
    for path in sorted(h.ORCH_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        literal_annotation_ids = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "Literal":
                literal_annotation_ids.update(id(n) for n in ast.walk(node.slice))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "web_public":
                kind = "Literal" if id(node) in literal_annotation_ids else "value"
                sites.append((path.relative_to(h.REPO_ROOT).as_posix(), kind))
    assert ("src/orchestrator/mode_permission.py", "value") in sites
    assert sorted(sites) == sorted([
        ("src/orchestrator/main.py", "Literal"),
        ("src/orchestrator/mode_permission.py", "value"),
    ])


# resolve_addressee asks is_public_audience before it reaches _owner_name, and
# load_visible_knowledge fetches nothing for a public audience (no visible tiers).
AUDIENCE_GUARDED = {("helpers.py", "_owner_name")}


def test_base_knowledge_sites_guarded():
    """PP10: every function that fetches base knowledge or the home
    address asks is_public_audience first. Floor 3 functions + the home
    address site."""
    fetchers = {"get_knowledge_context_for_user", "load_visible_knowledge"}
    sites = []
    for path in sorted(h.ORCH_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [
                n for n in ast.walk(fn)
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) in fetchers
            ]
            if not calls:
                continue
            guard_lines = [
                n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "is_public_audience"
            ]
            if (path.name, fn.name) in AUDIENCE_GUARDED:
                continue
            for call in calls:
                assert guard_lines and min(guard_lines) < call.lineno, f"{path.name}:{fn.name}:{call.lineno}"
                sites.append((path.name, fn.name, call.func.id))
    functions = {(f, n) for f, n, _ in sites}
    assert len(functions) >= 3
    assert ("synthesize.py", "synthesize_node") in functions
    assert ("main.py", "build_synthesis_prompt_for_streaming") in functions
    assert ("main.py", "tool_call_node", "load_visible_knowledge") in sites

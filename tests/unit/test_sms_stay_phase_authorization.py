"""An SMS from outside the current stay is answer-only.

``resolve_request_authorization(..., sms_stay_phase=)`` is the single
authority. For ``caller_trust == "sms"`` and any phase but ``"current"``
(missing or unknown included) it overlays the guest permissions so that:

- every HA write is refused (``restricted_entities [".*"]``),
- the admin-DB writers refuse (``stay_read_only``): automation create and
  archive, notification preferences, SMS send,
- house reads are refused by narrowing the intents and offered tools to
  travel and checkout questions.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, "src")
sys.path.insert(0, "src/orchestrator")

import pytest

from orchestrator import mode_permission as mp
from orchestrator.state import IntentCategory, OrchestratorState

REPO_ROOT = Path(__file__).resolve().parents[2]

GUEST_PROFILE = {
    "mode": "guest",
    "allowed_intents": [
        "control", "weather", "general_info", "directions", "notification_pref", "text_me_that",
        "music_play", "tv_control", "tesla", "dining",
    ],
    "restricted_intents": [],
    "restricted_entities": [],
    "allowed_domains": ["light"],
}
LIGHT_ON = ("light", "turn_on", {"entity_id": "light.kitchen"})


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def guest_house(monkeypatch):
    monkeypatch.setattr(mp, "get_current_mode", AsyncMock(return_value={"mode": "owner", "permissions": {"mode": "owner"}}))
    monkeypatch.setattr(mp, "get_guest_permissions", AsyncMock(side_effect=lambda: mp.normalize_permissions(dict(GUEST_PROFILE))))


def _perms(phase, caller_trust="sms"):
    authz = _run(mp.resolve_request_authorization("guest", None, caller_trust, service_authenticated=False, sms_stay_phase=phase))
    return authz.permissions


# ---------------------------------------------------------------------------
# HA writes
# ---------------------------------------------------------------------------

def test_current_stay_can_write(guest_house):
    assert mp.authorize_ha_write(*LIGHT_ON, _perms("current")).allowed


@pytest.mark.parametrize("phase", ["recent", "upcoming", None, "bogus", ""])
def test_outside_the_stay_nothing_writes(guest_house, phase):
    perms = _perms(phase)
    assert not mp.authorize_ha_write(*LIGHT_ON, perms).allowed
    assert mp.is_stay_read_only(perms)
    assert perms["sms_stay_phase"] == (phase or "unknown")


def test_the_phase_is_ignored_for_other_callers(guest_house):
    perms = _perms(None, caller_trust="household")
    assert mp.authorize_ha_write(*LIGHT_ON, perms).allowed
    assert not mp.is_stay_read_only(perms)


def test_every_main_py_entry_passes_the_stay_phase():
    tree = ast.parse((REPO_ROOT / "src/orchestrator/main.py").read_text())
    calls = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "resolve_request_authorization"
                    and any(k.arg == "caller_trust" for k in node.keywords)):
                calls.setdefault(fn.name, []).append({k.arg for k in node.keywords})
    flat = [kw for group in calls.values() for kw in group]
    assert len(flat) >= 3, calls
    assert "process_query" in calls
    assert all("sms_stay_phase" in kw for kw in flat), calls


# ---------------------------------------------------------------------------
# House reads: intents and offered tools
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("intent", [IntentCategory.GENERAL_INFO, IntentCategory.WEATHER, IntentCategory.DIRECTIONS,
                                    IntentCategory.DINING])
def test_outside_the_stay_travel_questions_are_answered(guest_house, intent):
    assert mp.check_intent_permission(intent, _perms("recent"))


@pytest.mark.parametrize("intent", [IntentCategory.CONTROL, IntentCategory.MUSIC_PLAY, IntentCategory.TV_CONTROL,
                                    IntentCategory.NOTIFICATION_PREF, IntentCategory.TEXT_ME_THAT, IntentCategory.TESLA])
def test_outside_the_stay_house_intents_are_refused(guest_house, intent):
    perms = _perms("recent")
    assert not mp.check_intent_permission(intent, perms)


def test_a_house_state_question_never_reaches_route_control(guest_house):
    assert mp.intent_gate_refusal(IntentCategory.CONTROL, _perms("recent")) == mp.STAY_READ_ONLY_REFUSAL
    # Chit-chat still gets through, to the narrowed tools only.
    assert mp.intent_gate_refusal(IntentCategory.UNKNOWN, _perms("recent")) is None


def test_current_stay_keeps_the_guest_profile(guest_house):
    perms = _perms("current")
    assert mp.check_intent_permission(IntentCategory.CONTROL, perms)
    assert mp.intent_gate_refusal(IntentCategory.CONTROL, perms) is None


def _tools(*names):
    return [{"type": "function", "function": {"name": n}} for n in names]


def test_offered_tools_narrow_outside_the_stay(guest_house):
    tools = _tools("get_weather", "get_tesla_metrics", "search_web")
    offered = {t["function"]["name"] for t in mp.offered_tools(tools, _perms("recent"))}
    assert offered == {"get_weather"}
    kept = {t["function"]["name"] for t in mp.offered_tools(tools, _perms("current"))}
    assert kept == {"get_weather", "get_tesla_metrics", "search_web"}


def test_offered_tools_keep_the_public_narrowing():
    tools = _tools("get_weather", "get_tesla_metrics", "get_news")
    offered = {t["function"]["name"] for t in mp.offered_tools(tools, mp.normalize_permissions(mp.public_permissions()))}
    assert offered == {"get_weather", "get_news"}


def test_main_uses_offered_tools():
    source = (REPO_ROOT / "src/orchestrator/main.py").read_text()
    assert "tools = offered_tools(tools, state.permissions)" in source


# ---------------------------------------------------------------------------
# Admin-DB writers
# ---------------------------------------------------------------------------

class _RecordingAdmin:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def _record(*args, **kwargs):
            self.calls.append(name)
            return {"id": 1} if name == "create_voice_automation" else True
        return _record


CREATE_ARGS = {"name": "Porch", "trigger": {"type": "time", "time": "18:00"},
               "actions": [{"type": "service", "entity_id": "light.porch", "service": "turn_on"}]}


def _agent():
    from orchestrator.automation_agent import AutomationAgent

    admin = _RecordingAdmin()
    ha = MagicMock()
    ha.create_automation = AsyncMock(return_value=True)
    ha.call_service = AsyncMock(return_value=True)
    agent = AutomationAgent(ha, MagicMock(), admin_client=admin)
    agent.ha_client = ha
    return agent, admin, ha


@pytest.mark.parametrize("phase,refused", [("recent", True), ("upcoming", True), (None, True), ("current", False)])
def test_automation_agent_writes(guest_house, phase, refused):
    perms = _perms(phase)
    context = {"mode": "guest", "guest_name": "Ana", "guest_stay_id": 7, "room": "office"}
    for method, args in (("_create_automation", dict(CREATE_ARGS)), ("_delete_automation", {"automation_id": 3})):
        agent, admin, ha = _agent()
        with mp.ha_permission_scope(perms, mode="guest"):
            out = _run(getattr(agent, method)(args, context))
        if refused:
            assert out == mp.STAY_READ_ONLY_REFUSAL, (method, out)
            assert admin.calls == [] and ha.create_automation.await_count == 0
        else:
            assert out != mp.STAY_READ_ONLY_REFUSAL, (method, out)
            assert admin.calls, method


class _SpyAsyncClient:
    posts: list = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kw):
        _SpyAsyncClient.posts.append(url)
        return SimpleNamespace(status_code=200, json=lambda: {"success": True, "message": "ok"}, text="{}")


@pytest.mark.parametrize("phase,refused", [("recent", True), (None, True), ("current", False)])
def test_notification_preferences(guest_house, monkeypatch, phase, refused):
    import httpx

    from orchestrator.nodes import notification_pref as node

    _SpyAsyncClient.posts = []
    monkeypatch.setattr(httpx, "AsyncClient", _SpyAsyncClient)
    monkeypatch.setattr(node, "configured_assistant_names", AsyncMock(return_value=()))
    state = OrchestratorState(query="stop morning notifications")
    state.permissions = _perms(phase)
    state.node_timings = {}
    out = _run(node.notification_pref_node(state))
    if refused:
        assert out.answer == mp.STAY_READ_ONLY_REFUSAL
        assert _SpyAsyncClient.posts == []
    else:
        assert out.answer != mp.STAY_READ_ONLY_REFUSAL
        assert _SpyAsyncClient.posts, out.answer


@pytest.mark.parametrize("phase,refused", [("recent", True), (None, True), ("current", False)])
def test_send_sms(guest_house, monkeypatch, phase, refused):
    import sms.text_me_that as tmt
    from orchestrator.nodes import send_sms as node

    service_calls = []

    async def _service():
        service_calls.append(1)
        return MagicMock()

    monkeypatch.setattr(node, "get_sms_service", _service)
    monkeypatch.setattr(tmt, "handle_text_me_that", AsyncMock(return_value={"success": True, "answer": "Sent!"}))
    state = OrchestratorState(query="text me that")
    state.permissions = _perms(phase)
    state.conversation_history = [{"role": "assistant", "content": "The wifi is guest-net."}]
    state.context = {"phone_number": "+15550123456"}
    state.node_timings = {}
    out = _run(node.send_sms_node(state))
    if refused:
        assert out.answer == mp.STAY_READ_ONLY_REFUSAL
        assert service_calls == []
    else:
        assert out.answer == "Sent!"
        assert service_calls == [1]


def test_notification_node_refuses_on_its_own(monkeypatch):
    """Defence in depth: the intent gate already refuses notification_pref
    outside the stay (D16); the node's own stay check refuses even when the
    intent itself is allowed."""
    import httpx

    from orchestrator.nodes import notification_pref as node

    _SpyAsyncClient.posts = []
    monkeypatch.setattr(httpx, "AsyncClient", _SpyAsyncClient)
    monkeypatch.setattr(node, "configured_assistant_names", AsyncMock(return_value=()))
    perms = mp.off_stay_overlay(mp.normalize_permissions(dict(GUEST_PROFILE)), "recent")
    perms = {**perms, "allowed_intents": ["notification_pref"], "restricted_intents": []}
    assert mp.check_intent_permission(IntentCategory.NOTIFICATION_PREF, perms)
    state = OrchestratorState(query="stop morning notifications")
    state.permissions = perms
    state.node_timings = {}
    out = _run(node.notification_pref_node(state))
    assert out.answer == mp.STAY_READ_ONLY_REFUSAL
    assert _SpyAsyncClient.posts == []


def test_no_overlap_with_the_travel_intents_still_refuses_house_intents(monkeypatch):
    """A guest profile allowing only control has no overlap with the
    off-stay intents, so the guest baseline refills allowed_intents (news,
    recipes, streaming...). The deny list must still hold."""
    control_only = {"mode": "guest", "allowed_intents": ["control"], "restricted_intents": [],
                    "restricted_entities": [], "allowed_domains": ["light"]}
    monkeypatch.setattr(mp, "get_current_mode", AsyncMock(return_value={"mode": "owner", "permissions": {"mode": "owner"}}))
    monkeypatch.setattr(mp, "get_guest_permissions", AsyncMock(side_effect=lambda: mp.normalize_permissions(dict(control_only))))
    perms = _perms("recent")
    for intent in (IntentCategory.CONTROL, IntentCategory.NEWS, IntentCategory.STREAMING, IntentCategory.RECIPES):
        assert not mp.check_intent_permission(intent, perms), intent


def test_memory_writes_skip_an_off_stay_sms():
    """process_query's two memory writers (forget, create) write to the admin
    DB (/api/memories/internal/*). Like the public audience, an SMS from
    outside the stay gets no memory manager there. Source-level: the writes
    run after the graph, which this harness doesn't drive."""
    tree = ast.parse((REPO_ROOT / "src/orchestrator/main.py").read_text())
    (process_query,) = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "process_query"]
    writers = []
    for node in ast.walk(process_query):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == "memory_manager" and isinstance(node.value, ast.IfExp)):
            writers.append(ast.unparse(node.value.test))
    assert len(writers) == 2, writers
    for test in writers:
        assert "is_public_audience(permissions)" in test and "is_stay_read_only(permissions)" in test, test
